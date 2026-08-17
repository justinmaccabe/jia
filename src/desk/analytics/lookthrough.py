"""Resolve funds to what is actually inside them. Pure: pandas and dataclasses.

A portfolio of nine ETFs is not nine holdings. It is a few thousand companies,
many of them arriving through more than one fund at once, and the concentration
that matters is invisible at the fund level: two index trackers that look like
diversification can be sixty percent the same twenty companies.

The thing this module refuses to do is pretend everything resolves. Three of the
holdings in the portfolio it was written for cannot be looked through to
securities at all, for three different reasons:

  * **A swap-based fund holds no securities.** It holds a total-return swap with
    a bank, and its economic exposure is to an index. Listing the index
    constituents as if they were held would be a fiction — the counterparty
    risk is real and the shares are not there.
  * **A commodity or digital-asset fund has no companies in it.** Bitcoin has no
    sector and no country of domicile.
  * **A fund whose composition was never supplied** must be visible as a gap,
    not silently dropped from every denominator.

Each of these carries a `resolution` other than `securities`, is excluded from
the company-level rollup, and is reported by name. `coverage` says what fraction
of the portfolio the security detail actually covers, and the UI is expected to
print it. A look-through chart that quietly describes 60% of a book while
looking like it describes all of it is worse than no chart.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

import pandas as pd

# How a fund's contents were determined. `securities` and `direct` yield
# company-level detail; the rest are honest labels for the cases that cannot.
SECURITIES = "securities"
# A directly-held share is already a company and resolves to itself. Modelling it
# this way is what lets a name owned outright *and* held inside a fund appear once
# with both contributions summed — which is the overlap most easily missed, since
# no statement shows it.
DIRECT = "direct"
SYNTHETIC = "synthetic"
COMMODITY = "commodity"
UNMAPPED = "unmapped"

# Weight a fund's published list covers but which resolves to no security. Kept
# distinct from cash: a fact sheet publishing its ten largest of five hundred
# holdings leaves ~62% unaccounted for, and calling that cash would report a
# diversified equity fund as mostly uninvested.
UNRESOLVED = "Unresolved inside funds"

EQUITY = "Equity"
BOND = "Bond"
CASH = "Cash"

# Asset-class labels as reported, mapped from the per-row classes above.
ASSET_LABELS = {
    EQUITY: "Public equity",
    BOND: "Bonds",
    CASH: "Cash",
    "Commodity": "Commodities",
    "Digital asset": "Digital assets",
    "Private": "Private markets",
    "Synthetic": "Synthetic index exposure",
}

DEVELOPED = frozenset(
    {
        "Australia",
        "Austria",
        "Belgium",
        "Britain",
        "United Kingdom",
        "Canada",
        "Denmark",
        "Finland",
        "France",
        "Germany",
        "Hong Kong",
        "Ireland",
        "Israel",
        "Italy",
        "Japan",
        "Netherlands",
        "New Zealand",
        "Norway",
        "Portugal",
        "Singapore",
        "South Korea",
        "Spain",
        "Sweden",
        "Switzerland",
        "United States",
        "Luxembourg",
        "Bermuda",
        "Jersey",
        "Cayman Islands",
        "Curacao",
        "Panama",
    }
)


def region_of(country: str) -> str:
    """Bucket a country of domicile into a reporting region."""
    if not country:
        return "Unclassified"
    if country == "United States":
        return "United States"
    if country == "Canada":
        return "Canada"
    return "Developed ex-North America" if country in DEVELOPED else "Emerging markets"


@dataclass(frozen=True, slots=True)
class Holding:
    """One line inside a fund. `weight` is a fraction of that fund."""

    ticker: str
    name: str
    weight: float
    asset_class: str = EQUITY
    sector: str = ""
    country: str = ""


@dataclass(frozen=True, slots=True)
class FundComposition:
    """What one held fund contains, and how confidently we know it."""

    ticker: str
    name: str = ""
    resolution: str = SECURITIES
    as_of: dt.date | None = None
    holdings: tuple[Holding, ...] = ()
    note: str = ""
    # For a swap-based fund: the index it tracks, and where that index sits.
    tracks: str = ""
    region_mix: Mapping[str, float] = field(default_factory=dict)
    # How many securities the fund actually holds, when the published list is only
    # its largest few. None means the list is believed complete. This single field
    # is what separates "the fund holds 2% cash" from "we can see 10 of its 506
    # holdings", which are indistinguishable from the weights alone.
    total_holdings: int | None = None

    @property
    def covered(self) -> float:
        """Sum of published weights."""
        return sum(h.weight for h in self.holdings)

    @property
    def is_partial(self) -> bool:
        """Whether the published list is known to be a subset of the fund."""
        return self.total_holdings is not None and self.total_holdings > len(self.holdings)

    @property
    def resolves_to_securities(self) -> bool:
        return self.resolution in (SECURITIES, DIRECT) and bool(self.holdings)


@dataclass(frozen=True, slots=True)
class CompanyExposure:
    """One company's total exposure, and which funds delivered it."""

    ticker: str
    name: str
    by_fund: Mapping[str, float]
    total: float
    weight: float

    @property
    def funds(self) -> int:
        return len([v for v in self.by_fund.values() if v > 0])


@dataclass(frozen=True, slots=True)
class Concentration:
    """Concentration statistics for the resolved equity sleeve."""

    distinct: int
    top1: float
    top10: float
    top25: float
    overlap_3plus: float
    max_overlap: int
    tail_count: int
    tail_value: float


@dataclass(frozen=True, slots=True)
class LookThrough:
    """Company-level exposure plus rollups, with coverage stated."""

    companies: tuple[CompanyExposure, ...]
    total: float
    resolved: float
    region: Mapping[str, float]
    sector: Mapping[str, float]
    asset: Mapping[str, float]
    sector_base: float
    stats: Concentration
    unresolved: Mapping[str, tuple[float, str]]
    as_of: dt.date | None
    # fund -> (market value, holdings published, holdings the fund actually has)
    partial: Mapping[str, tuple[float, int, int]] = field(default_factory=dict)

    @property
    def coverage(self) -> float:
        """Share of market value that resolved to individual securities."""
        return self.resolved / self.total if self.total > 0 else 0.0

    @property
    def is_empty(self) -> bool:
        return not self.companies

    @property
    def has_partial_funds(self) -> bool:
        return bool(self.partial)

    @property
    def stats_are_lower_bounds(self) -> bool:
        """Whether concentration figures understate the truth.

        With any fund published only in part, every company total is a floor: the
        named holdings are certainly there, and unseen ones may add more. The
        distinct-securities count is the worst affected — ten of five hundred — and
        the overlap figure is a floor too, since a company could be hiding in the
        unpublished tail of a second fund. Read as "at least", never "exactly".
        """
        return self.has_partial_funds


def _add(store: dict[str, float], key: str, amount: float) -> None:
    if key:
        store[key] = store.get(key, 0.0) + amount


def look_through(
    market_values: Mapping[str, float],
    compositions: Sequence[FundComposition],
) -> LookThrough:
    """Resolve held funds to companies and roll up region, sector and asset class.

    `market_values` is base-currency market value per held ticker. Funds present
    in `market_values` with no composition supplied are reported as unresolved
    rather than dropped, which is what keeps every percentage below honest.
    """
    total = sum(v for v in market_values.values() if v > 0)
    by_ticker = {c.ticker: c for c in compositions}

    companies: dict[str, dict[str, float]] = {}
    names: dict[str, str] = {}
    region: dict[str, float] = {}
    sector: dict[str, float] = {}
    asset: dict[str, float] = {}
    unresolved: dict[str, tuple[float, str]] = {}
    # fund -> (market value, holdings published, holdings the fund actually has)
    partial: dict[str, tuple[float, int, int]] = {}
    sector_base = 0.0
    resolved = 0.0

    for fund, value in market_values.items():
        if value <= 0:
            continue
        composition = by_ticker.get(fund)
        if composition is None:
            unresolved[fund] = (value, "no composition on file")
            _add(asset, ASSET_LABELS[CASH], value)
            continue

        if not composition.resolves_to_securities:
            reason = {
                SYNTHETIC: (
                    f"swap-based; economic exposure to {composition.tracks or 'an index'} "
                    "with no securities held"
                ),
                COMMODITY: "holds no companies",
                UNMAPPED: "no composition on file",
                SECURITIES: "composition file is empty",
            }[composition.resolution]
            unresolved[fund] = (value, composition.note or reason)
            # Synthetic exposure still has a region; a commodity does not.
            if composition.resolution == SYNTHETIC and composition.region_mix:
                for name, share in composition.region_mix.items():
                    _add(region, name, value * share)
                _add(asset, ASSET_LABELS["Synthetic"], value)
            elif composition.resolution == COMMODITY:
                _add(asset, ASSET_LABELS.get("Digital asset", "Other"), value)
            else:
                _add(asset, ASSET_LABELS[CASH], value)
            continue

        # Only the published portion is resolved. For a complete list `covered` is
        # ~1.0 and this is the whole position; for a top-ten-of-506 fact sheet it
        # is about a third, and reporting the rest as resolved would overstate the
        # coverage figure the entire tab is read against.
        resolved += value * min(composition.covered, 1.0)
        if composition.is_partial:
            partial[fund] = (value, len(composition.holdings), composition.total_holdings or 0)
        equity_covered = 0.0
        for holding in composition.holdings:
            amount = value * holding.weight
            if holding.asset_class == EQUITY:
                if holding.ticker:
                    row = companies.setdefault(holding.ticker, {})
                    row[fund] = row.get(fund, 0.0) + amount
                    names.setdefault(holding.ticker, holding.name or holding.ticker)
                if holding.sector:
                    _add(sector, holding.sector, amount)
                    equity_covered += amount
            if holding.country:
                _add(region, region_of(holding.country), amount)
            _add(asset, ASSET_LABELS.get(holding.asset_class, "Other"), amount)
        sector_base += equity_covered
        # What the published weights do not cover. For a complete list this is the
        # fund's own cash balance. For a partial list it is holdings we simply
        # cannot see, and calling that cash would describe an equity fund as
        # largely uninvested — so the two go to different buckets.
        residual = 1.0 - composition.covered
        if residual > 5e-4:
            bucket = UNRESOLVED if composition.is_partial else ASSET_LABELS[CASH]
            _add(asset, bucket, value * residual)

    exposures = tuple(
        sorted(
            (
                CompanyExposure(
                    ticker=ticker,
                    name=names.get(ticker, ticker),
                    by_fund=dict(funds),
                    total=sum(funds.values()),
                    weight=(sum(funds.values()) / total) if total > 0 else 0.0,
                )
                for ticker, funds in companies.items()
            ),
            key=lambda c: -c.total,
        )
    )
    return LookThrough(
        companies=exposures,
        total=total,
        resolved=resolved,
        region={k: v / total for k, v in region.items()} if total > 0 else {},
        sector={k: v / sector_base for k, v in sector.items()} if sector_base > 0 else {},
        asset={k: v / total for k, v in asset.items()} if total > 0 else {},
        sector_base=sector_base,
        stats=concentration(exposures, total),
        unresolved=unresolved,
        as_of=_earliest_as_of(compositions),
        partial=partial,
    )


def _earliest_as_of(compositions: Sequence[FundComposition]) -> dt.date | None:
    """The oldest composition date in play.

    The oldest rather than the newest deliberately: a blended figure is only as
    current as its stalest input, and reporting the freshest date would overstate
    how up to date the whole picture is.
    """
    dates = [c.as_of for c in compositions if c.as_of is not None]
    return min(dates) if dates else None


def concentration(
    companies: Sequence[CompanyExposure], total: float, tail_threshold: float = 10.0
) -> Concentration:
    """Concentration and overlap statistics over resolved companies."""
    if not companies:
        return Concentration(0, 0.0, 0.0, 0.0, 0.0, 0, 0, 0.0)
    ranked = sorted(companies, key=lambda c: -c.total)
    tail = [c for c in ranked if c.total < tail_threshold]
    return Concentration(
        distinct=len(ranked),
        top1=ranked[0].total / total if total > 0 else 0.0,
        top10=sum(c.total for c in ranked[:10]) / total if total > 0 else 0.0,
        top25=sum(c.total for c in ranked[:25]) / total if total > 0 else 0.0,
        # The headline number: money in companies arriving through three or more
        # funds at once. This is the concentration that fund-level allocation
        # charts cannot show.
        overlap_3plus=(sum(c.total for c in ranked if c.funds >= 3) / total if total > 0 else 0.0),
        max_overlap=max(c.funds for c in ranked),
        tail_count=len(tail),
        tail_value=sum(c.total for c in tail),
    )


def companies_frame(
    result: LookThrough, funds: Sequence[str], limit: int | None = None
) -> pd.DataFrame:
    """Company exposures as a frame, one column per delivering fund."""
    if result.is_empty:
        return pd.DataFrame()
    rows = [
        {
            "Ticker": c.ticker,
            "Name": c.name,
            **{f: c.by_fund.get(f, 0.0) for f in funds},
            "Total": c.total,
            "Weight": c.weight,
            "Funds": c.funds,
        }
        for c in (result.companies[:limit] if limit else result.companies)
    ]
    return pd.DataFrame(rows)


def overlap_table(result: LookThrough, minimum: int = 2) -> pd.DataFrame:
    """Only the companies arriving through more than one fund, largest first."""
    if result.is_empty:
        return pd.DataFrame()
    rows = [
        {
            "Ticker": c.ticker,
            "Name": c.name,
            "Funds": c.funds,
            "Delivered by": ", ".join(sorted(f for f, v in c.by_fund.items() if v > 0)),
            "Total": c.total,
            "Weight": c.weight,
        }
        for c in result.companies
        if c.funds >= minimum
    ]
    return pd.DataFrame(rows)
