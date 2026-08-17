"""Normalise published fund-holdings files into one composition dataset.

Every provider publishes a different file. One puts nine lines of preamble above
the header; another uses different column names; a third exports a sheet with a
title row; some quote weights as percentages and others as fractions. This module
is the single place that mess is absorbed, so `desk.analytics.lookthrough` can
take one clean structure.

Two decisions worth stating.

**Weights are renormalised, never assumed.** A published file may sum to 99.4%
or 100.6% depending on rounding and on whether cash is listed. The parser records
the raw sum and rescales the security weights so the fund's own cash residual is
computed rather than inherited from a rounding error.

**A file that cannot be understood fails loudly.** It is not turned into a
partial composition. Silently importing three of a fund's five hundred lines
would produce a look-through that is wrong in a way nothing downstream could
detect — so an unrecognised layout raises and names the file.

Source files live outside the repository (`inbox/`, which is gitignored) because
holdings files for the specific funds someone owns are themselves a disclosure of
what they own. Only the normalised output is committed.
"""

from __future__ import annotations

import csv
import datetime as dt
import gzip
import io
import json
import re
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from desk.analytics.lookthrough import (
    COMMODITY,
    DIRECT,
    EQUITY,
    SECURITIES,
    SYNTHETIC,
    FundComposition,
    Holding,
)

DATA_VERSION = 2

# Column aliases, lowercased and stripped of punctuation. Providers disagree on
# every one of these, and the disagreement is not going to stop.
TICKER_KEYS = ("ticker", "symbol", "holding ticker", "issue ticker", "sedol ticker")
NAME_KEYS = ("name", "security name", "holding name", "issuer name", "description")
WEIGHT_KEYS = (
    "weight (%)",
    "weight",
    "% of net assets",
    "percent of net assets",
    "portfolio weight",
    "% of fund",
    "market value percentage",
    "weighting",
    "% weight",
)
SECTOR_KEYS = ("sector", "gics sector", "industry", "sub industry")
COUNTRY_KEYS = ("country", "location", "domicile", "country of risk")
ASSET_KEYS = ("asset class", "asset type", "security type", "type")

# Provider asset-class vocabulary mapped onto the reporting classes.
ASSET_ALIASES = {
    "equity": EQUITY,
    "equities": EQUITY,
    "common stock": EQUITY,
    "stock": EQUITY,
    "preferred": EQUITY,
    "reit": EQUITY,
    "fixed income": "Bond",
    "bond": "Bond",
    "corporate bond": "Bond",
    "government bond": "Bond",
    "cash": "Cash",
    "cash and/or derivatives": "Cash",
    "money market": "Cash",
    "commodity": "Commodity",
    "cryptocurrency": "Digital asset",
    "digital asset": "Digital asset",
}

# Sector names normalised to the GICS wording used for display.
SECTOR_ALIASES = {
    "info tech": "Information Technology",
    "information technology": "Information Technology",
    "tech": "Information Technology",
    "financial services": "Financials",
    "financials": "Financials",
    "health care": "Health Care",
    "healthcare": "Health Care",
    "consumer discretionary": "Consumer Discretionary",
    "consumer staples": "Consumer Staples",
    "communication": "Communication Services",
    "communication services": "Communication Services",
    "telecommunication services": "Communication Services",
    "real estate": "Real Estate",
    "utilities": "Utilities",
    "energy": "Energy",
    "materials": "Materials",
    "basic materials": "Materials",
    "industrials": "Industrials",
}

COUNTRY_ALIASES = {
    "us": "United States",
    "usa": "United States",
    "u.s.": "United States",
    "united states of america": "United States",
    "ca": "Canada",
    "can": "Canada",
    "uk": "United Kingdom",
    "gb": "United Kingdom",
    "great britain": "United Kingdom",
    "britain": "United Kingdom",
    "korea, republic of": "South Korea",
    "korea": "South Korea",
    "taiwan, province of china": "Taiwan",
    "russian federation": "Russia",
}

# Share-class tickers that differ between providers for the same company.
TICKER_ALIASES = {
    "BRK/B": "BRK.B",
    "BRKB": "BRK.B",
    "GOOG.L": "GOOGL",
}


class IntakeError(RuntimeError):
    """A source file could not be understood. Never a partial import."""


@dataclass(frozen=True)
class SourceSpec:
    """One held position and where its composition comes from.

    Four shapes. A `file` names a full published holdings export. Inline
    `holdings` carry a partial list transcribed from a fact sheet, which for some
    providers is the only document published. `resolution: direct` marks a
    directly-held share, which is already a company and needs no source at all.
    Anything else — swap-based, commodity — expects no source and records a reason.
    """

    ticker: str
    name: str
    file: str = ""
    resolution: str = SECURITIES
    note: str = ""
    tracks: str = ""
    region_mix: Mapping[str, float] = None  # type: ignore[assignment]
    # A fund of one fund (a Canadian wrapper holding a US-listed ETF) resolves
    # through its underlying's file, so the file is named for the underlying.
    via: str = ""
    # A partial list transcribed from a fact sheet, plus the fund's true holding
    # count — that count is what tells the analytics layer the list is a subset,
    # so the remainder is reported unresolved instead of counted as cash.
    holdings: tuple[Mapping[str, object], ...] = ()
    total_holdings: int | None = None
    # For `direct`: the company's own classification.
    sector: str = ""
    country: str = ""


def _norm_key(key: str) -> str:
    return re.sub(r"\s+", " ", key.strip().lower()).strip()


def _pick(row: Mapping[str, str], keys: Sequence[str]) -> str:
    for key in keys:
        if key in row and str(row[key]).strip():
            return str(row[key]).strip()
    return ""


def _to_weight(raw: str) -> float | None:
    """Parse a weight cell.

    Accepts percentages, fractions, a trailing percent sign, thousands
    separators, and the placeholders providers use for a blank.
    """
    text = raw.replace("%", "").replace(",", "").replace("$", "").strip()
    if not text or text in {"-", "--", "N/A", "n/a"}:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _find_header(lines: Sequence[str], source: str = "") -> int:
    """Index of the header row, skipping provider preamble.

    The header is the first row containing both something ticker-like and
    something weight-like — not a fixed line number, because the number of
    preamble lines changes without notice.
    """
    for index, line in enumerate(lines[:40]):
        keys = {_norm_key(k) for k in next(csv.reader([line]), [])}
        if keys & set(WEIGHT_KEYS) and (keys & set(TICKER_KEYS) or keys & set(NAME_KEYS)):
            return index
    seen = sorted({_norm_key(k) for line in lines[:40] for k in next(csv.reader([line]), [])})
    # Naming the file and listing what was actually seen: with nine of these to
    # keep current, "no header found" alone sends you opening files one by one.
    raise IntakeError(
        f"{source or 'file'}: no header row found in the first 40 lines. Expected a row "
        f"naming both a ticker/name column and a weight column. Columns seen: {seen}. "
        f"If this provider uses a different heading, add it to the *_KEYS tuples."
    )


def parse_holdings(text: str, *, source: str = "") -> tuple[tuple[Holding, ...], float]:
    """Parse one published holdings file into rows and their raw weight sum."""
    lines = text.splitlines()
    if not lines:
        raise IntakeError(f"{source or 'file'} is empty")
    start = _find_header(lines, source)
    reader = csv.DictReader(io.StringIO("\n".join(lines[start:])))
    if reader.fieldnames is None:
        raise IntakeError(f"{source or 'file'} has no parsable header")
    reader.fieldnames = [_norm_key(f) for f in reader.fieldnames]

    rows: list[Holding] = []
    raw_total = 0.0
    for record in reader:
        weight = _to_weight(_pick(record, WEIGHT_KEYS))
        if weight is None or weight <= 0:
            continue
        ticker = _pick(record, TICKER_KEYS).upper()
        ticker = TICKER_ALIASES.get(ticker, ticker)
        name = _pick(record, NAME_KEYS)
        if not ticker and not name:
            continue
        asset_raw = _norm_key(_pick(record, ASSET_KEYS))
        sector_raw = _norm_key(_pick(record, SECTOR_KEYS))
        country_raw = _norm_key(_pick(record, COUNTRY_KEYS))
        rows.append(
            Holding(
                ticker=ticker,
                name=name or ticker,
                weight=weight,
                asset_class=ASSET_ALIASES.get(asset_raw, EQUITY if not asset_raw else "Other"),
                sector=SECTOR_ALIASES.get(sector_raw, _title(sector_raw)),
                country=COUNTRY_ALIASES.get(country_raw, _title(country_raw)),
            )
        )
        raw_total += weight
    if not rows:
        raise IntakeError(
            f"{source or 'file'}: a header was found but no holding rows parsed. "
            "Check that the weight column contains numbers."
        )
    return tuple(rows), raw_total


def _title(text: str) -> str:
    return " ".join(w.capitalize() for w in text.split()) if text else ""


def normalise(
    rows: Sequence[Holding], raw_total: float, *, rescale: bool = True
) -> tuple[Holding, ...]:
    """Convert weights to fractions of the fund.

    With `rescale` (a complete holdings file), weights are divided by their own
    total so they sum to one and the fund's cash residual falls out of the
    arithmetic rather than out of a rounding error.

    With `rescale=False` (a partial list — a fact sheet's ten largest of five
    hundred), rescaling would be catastrophic: it would inflate a 37.9% slice into
    the whole fund and silently claim complete knowledge of it. Units are converted
    and nothing else. A total above 2 is read as percentages, below as fractions.
    """
    if raw_total <= 0:
        return ()
    scale = (1.0 / raw_total) if rescale else (0.01 if raw_total > 2 else 1.0)
    return tuple(
        Holding(
            ticker=h.ticker,
            name=h.name,
            weight=h.weight * scale,
            asset_class=h.asset_class,
            sector=h.sector,
            country=h.country,
        )
        for h in rows
    )


def build(
    specs: Sequence[SourceSpec], source_dir: Path, *, as_of: dt.date | None = None
) -> tuple[FundComposition, ...]:
    """Read every spec's file and produce the composition set.

    Specs needing no file (swap-based, commodity) become compositions carrying
    their reason. A spec that names a missing or unparsable file raises, because
    a look-through silently missing one of nine funds is not a look-through.
    """
    out: list[FundComposition] = []
    for spec in specs:
        # A directly-held share resolves to itself at full weight. Complete by
        # construction, so no total_holdings and no unresolved remainder.
        if spec.resolution == DIRECT:
            out.append(
                FundComposition(
                    ticker=spec.ticker,
                    name=spec.name,
                    resolution=DIRECT,
                    as_of=as_of,
                    note=spec.note or "held directly",
                    holdings=(
                        Holding(
                            ticker=spec.ticker,
                            name=spec.name or spec.ticker,
                            weight=1.0,
                            asset_class=EQUITY,
                            sector=spec.sector,
                            country=spec.country,
                        ),
                    ),
                )
            )
            continue

        # A partial list transcribed from a fact sheet. Weights are NOT rescaled:
        # the ten largest of five hundred must stay a third of the fund, not become
        # all of it.
        if spec.holdings:
            rows = tuple(
                Holding(
                    ticker=str(h.get("ticker", "")).upper(),
                    name=str(h.get("name") or h.get("ticker") or ""),
                    weight=float(h["weight"]),  # type: ignore[arg-type]
                    asset_class=str(h.get("asset_class", EQUITY)),
                    sector=str(h.get("sector", "")),
                    country=str(h.get("country", "")),
                )
                for h in spec.holdings
            )
            raw_total = sum(h.weight for h in rows)
            complete = spec.total_holdings is None or spec.total_holdings <= len(rows)
            out.append(
                FundComposition(
                    ticker=spec.ticker,
                    name=spec.name,
                    resolution=SECURITIES,
                    as_of=as_of,
                    holdings=normalise(rows, raw_total, rescale=complete),
                    note=spec.note,
                    total_holdings=spec.total_holdings,
                )
            )
            continue

        if spec.resolution != SECURITIES:
            out.append(
                FundComposition(
                    ticker=spec.ticker,
                    name=spec.name,
                    resolution=spec.resolution,
                    as_of=as_of,
                    note=spec.note,
                    tracks=spec.tracks,
                    region_mix=dict(spec.region_mix or {}),
                )
            )
            continue
        if not spec.file:
            raise IntakeError(f"{spec.ticker}: no source file named and no reason given")
        path = source_dir / spec.file
        if not path.exists():
            raise IntakeError(f"{spec.ticker}: {path} not found")
        try:
            text = path.read_text(encoding="utf-8-sig", errors="replace")
        except OSError as exc:
            raise IntakeError(f"{spec.ticker}: cannot read {path}: {exc}") from exc
        rows, raw_total = parse_holdings(text, source=str(path.name))
        out.append(
            FundComposition(
                ticker=spec.ticker,
                name=spec.name,
                resolution=SECURITIES,
                as_of=as_of,
                holdings=normalise(rows, raw_total),
                note=spec.note or (f"resolved through {spec.via}" if spec.via else ""),
            )
        )
    return tuple(out)


def to_json(compositions: Sequence[FundComposition]) -> str:
    """Serialise the composition set."""
    return json.dumps(
        {
            "version": DATA_VERSION,
            "funds": [
                {
                    "ticker": c.ticker,
                    "name": c.name,
                    "resolution": c.resolution,
                    "as_of": c.as_of.isoformat() if c.as_of else None,
                    "note": c.note,
                    "tracks": c.tracks,
                    "total_holdings": c.total_holdings,
                    "region_mix": dict(c.region_mix),
                    "holdings": [
                        {
                            "ticker": h.ticker,
                            "name": h.name,
                            "weight": round(h.weight, 10),
                            "asset_class": h.asset_class,
                            "sector": h.sector,
                            "country": h.country,
                        }
                        for h in c.holdings
                    ],
                }
                for c in compositions
            ],
        },
        separators=(",", ":"),
    )


def from_json(payload: str) -> tuple[FundComposition, ...]:
    """Rebuild the composition set. Empty on anything unparsable."""
    try:
        parsed = json.loads(payload)
    except (ValueError, TypeError):
        return ()
    if not isinstance(parsed, dict) or parsed.get("version") != DATA_VERSION:
        return ()
    out: list[FundComposition] = []
    for fund in parsed.get("funds", []):
        raw_date = fund.get("as_of")
        out.append(
            FundComposition(
                ticker=fund["ticker"],
                name=fund.get("name", ""),
                resolution=fund.get("resolution", SECURITIES),
                as_of=dt.date.fromisoformat(raw_date) if raw_date else None,
                note=fund.get("note", ""),
                tracks=fund.get("tracks", ""),
                total_holdings=fund.get("total_holdings"),
                region_mix=fund.get("region_mix") or {},
                holdings=tuple(
                    Holding(
                        ticker=h.get("ticker", ""),
                        name=h.get("name", ""),
                        weight=float(h.get("weight", 0.0)),
                        asset_class=h.get("asset_class", EQUITY),
                        sector=h.get("sector", ""),
                        country=h.get("country", ""),
                    )
                    for h in fund.get("holdings", [])
                ),
            )
        )
    return tuple(out)


def write(compositions: Sequence[FundComposition], path: Path) -> None:
    """Write the gzipped composition set.

    Gzipped JSON rather than CSV: the data-hygiene gate refuses to let any `.csv`
    be tracked, and the constraint is a good one — this file is public reference
    data, but the rule that keeps holdings out of the repository should not have
    an exception carved into it for convenience.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(gzip.compress(to_json(compositions).encode("utf-8")))


def read(path: Path) -> tuple[FundComposition, ...]:
    """Read the gzipped composition set. Empty when absent or unreadable."""
    try:
        return from_json(gzip.decompress(path.read_bytes()).decode("utf-8"))
    except (OSError, ValueError, gzip.BadGzipFile):
        return ()


def specs_from_yaml(text: str) -> tuple[SourceSpec, ...]:
    """Parse the manifest describing each fund and its source file."""
    import yaml

    parsed = yaml.safe_load(text) or {}
    funds = parsed.get("funds")
    if not isinstance(funds, list):
        raise IntakeError("manifest must contain a 'funds:' list")
    out: list[SourceSpec] = []
    for entry in funds:
        if not isinstance(entry, dict) or "ticker" not in entry:
            raise IntakeError(f"each fund needs a 'ticker': got {entry!r}")
        resolution = entry.get("resolution", SECURITIES)
        if resolution not in (SECURITIES, DIRECT, SYNTHETIC, COMMODITY):
            raise IntakeError(
                f"{entry['ticker']}: resolution must be one of "
                f"{SECURITIES}, {DIRECT}, {SYNTHETIC}, {COMMODITY} (got {resolution!r})"
            )
        inline = tuple(entry.get("holdings") or ())
        total = entry.get("total_holdings")
        # A partial list without a holding count would be indistinguishable from a
        # complete one, and the remainder would be reported as fund cash. Refuse it
        # rather than produce a look-through that is quietly wrong.
        if inline and total is None:
            raise IntakeError(
                f"{entry['ticker']}: inline holdings need `total_holdings` — the number the "
                "fund actually holds. Without it a partial list cannot be told from a "
                "complete one, and the unpublished remainder would be counted as cash. "
                f"Set it equal to {len(inline)} if this list really is the whole fund."
            )
        out.append(
            SourceSpec(
                ticker=str(entry["ticker"]),
                name=str(entry.get("name") or entry["ticker"]),
                file=entry.get("file", ""),
                resolution=resolution,
                note=entry.get("note", ""),
                tracks=entry.get("tracks", ""),
                region_mix=entry.get("region_mix") or {},
                via=entry.get("via", ""),
                holdings=inline,
                total_holdings=int(total) if total is not None else None,
                sector=entry.get("sector", ""),
                country=entry.get("country", ""),
            )
        )
    return tuple(out)


def describe(compositions: Sequence[FundComposition]) -> Iterator[str]:
    """One human-readable line per fund, for the CLI to print."""
    for c in sorted(compositions, key=lambda x: x.ticker):
        if c.resolves_to_securities:
            equities = sum(1 for h in c.holdings if h.asset_class == EQUITY)
            countries = len({h.country for h in c.holdings if h.country})
            yield (
                f"{c.ticker:<6} {len(c.holdings):>5} rows  {equities:>5} equities  "
                f"{countries:>3} countries  {c.covered:>7.2%} covered"
            )
        else:
            yield f"{c.ticker:<6} {c.resolution:>10}  {c.note or '(no securities held)'}"
