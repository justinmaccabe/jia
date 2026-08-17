"""Market-data orchestration: fetch marks, build base-currency history, and
record snapshots. Services may reach the data providers, the analytics engine
and the store; none of those call back up.

Providers are constructed here with sensible defaults but the functions take
plain, cacheable inputs and return plain outputs, so the app can wrap the slow
network calls in its own cache without dragging a provider object through it.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from zoneinfo import ZoneInfo

import pandas as pd
from sqlalchemy import select

from desk.data.providers.market import YFinanceFx, YFinanceProvider
from desk.store.engine import build_engine, create_all, session_factory, session_scope
from desk.store.models import PriceCache, Snapshot


def fetch_marks(
    symbols: Sequence[str], currencies: Sequence[str], base: str
) -> tuple[dict[str, float], dict[str, float]]:
    """Latest native prices per symbol and FX rates per currency into base.

    Only usable (live) values are returned; a missing symbol simply does not
    appear, so the caller values what it can and reports coverage on the rest.
    """
    prices, _, fx = fetch_marks_with_previous(symbols, currencies, base)
    return prices, fx


def fetch_marks_with_previous(
    symbols: Sequence[str], currencies: Sequence[str], base: str
) -> tuple[dict[str, float], dict[str, float], dict[str, float]]:
    """As `fetch_marks`, and the prior session's close per symbol.

    The third mapping is what makes a daily P&L a market move rather than a
    difference between two snapshots: measured price-to-price, it is unaffected
    by a contribution landing between them.
    """
    provider = YFinanceProvider()
    fx_provider = YFinanceFx()
    quotes, previous = provider.quotes_with_previous(list(symbols))
    prices = {s: q.price for s, q in quotes.items() if q.is_usable and q.price is not None}
    fx: dict[str, float] = {}
    for currency in currencies:
        if currency == base:
            fx[currency] = 1.0
            continue
        rate = fx_provider.rate(currency, base)
        if rate.is_usable and rate.price is not None:
            fx[currency] = rate.price
    return prices, dict(previous), fx


def benchmark_daily_pct(symbol: str | None) -> float | None:
    """The configured daily benchmark's own one-session return.

    None when unset or when only one observation arrived — a benchmark that
    could not be measured is not the same thing as a flat one.
    """
    if not symbol:
        return None
    quotes, previous = YFinanceProvider().quotes_with_previous([symbol])
    quote = quotes.get(symbol)
    prior = previous.get(symbol)
    if quote is None or not quote.is_usable or quote.price is None or not prior:
        return None
    return quote.price / prior - 1.0


def base_history(
    symbols: Mapping[str, str],
    currencies: Mapping[str, str],
    base: str,
    period: str,
) -> pd.DataFrame:
    """Adjusted-close history per ticker, converted to base currency.

    `symbols` maps ticker -> quote symbol; `currencies` maps ticker -> the
    holding's currency. A foreign series is multiplied by the FX series aligned
    on date, so a Canadian investor's USD holding shows the return they actually
    experienced, FX move included.
    """
    provider = YFinanceProvider()
    fx_provider = YFinanceFx()
    raw = provider.history(list(symbols.values()), period)
    if raw.empty:
        return pd.DataFrame()
    fx_cache: dict[str, pd.Series] = {}
    out: dict[str, pd.Series] = {}
    for ticker, symbol in symbols.items():
        if symbol not in raw.columns:
            continue
        series = raw[symbol].dropna()
        currency = currencies.get(ticker, base)
        if currency != base:
            if currency not in fx_cache:
                fx_cache[currency] = fx_provider.series(currency, base, period)
            fx_series = fx_cache[currency]
            if not fx_series.empty:
                series = (series * fx_series.reindex(series.index).ffill().bfill()).dropna()
        out[ticker] = series
    return pd.DataFrame(out).dropna(how="all") if out else pd.DataFrame()


# Local-market wall-clock targets for the two daily records. The open is taken
# a few minutes after the bell so the first prints have landed; the close a few
# minutes after it so the closing auction is reflected.
OPEN_TARGET = (9, 35)
CLOSE_TARGET = (16, 5)


def _slot_for(now: dt.datetime) -> str:
    """Before noon local time is the 'open' record, otherwise 'close'."""
    return "open" if now.hour < 12 else "close"


def _utc_hour_of_local(target: tuple[int, int], now: dt.datetime, tz: str) -> int:
    """The UTC hour at which a local wall-clock time falls on `now`'s date."""
    local = now.astimezone(ZoneInfo(tz)).replace(
        hour=target[0], minute=target[1], second=0, microsecond=0
    )
    return local.astimezone(dt.UTC).hour


def resolve_slot(cron: str | None, now: dt.datetime, tz: str) -> str | None:
    """Which snapshot this invocation should record, or None to skip.

    GitHub's scheduler is UTC and DST-blind, so the workflow registers each
    local target twice — once for the summer offset and once for the winter one
    — and passes the cron that fired. Only the variant matching the current
    offset proceeds; the other returns None. Classifying on the *scheduled* cron
    rather than the actual run time matters because GitHub delays scheduled jobs
    under load, and a delayed open must still record as an open.

    A manual run has no cron and is classified by local time of day.
    """
    if now.tzinfo is None:
        now = now.replace(tzinfo=ZoneInfo(tz))
    text = (cron or "").strip()
    if not text:
        return _slot_for(now.astimezone(ZoneInfo(tz)))
    fields = text.split()
    if len(fields) < 2 or not fields[1].isdigit():
        return None
    cron_hour = int(fields[1])
    for slot, target in (("open", OPEN_TARGET), ("close", CLOSE_TARGET)):
        # Both DST variants of a target are registered, so a cron hour within an
        # hour of the correct one belongs to this target; only the exact match
        # for today's offset runs.
        correct = _utc_hour_of_local(target, now, tz)
        if cron_hour in {correct, (correct + 1) % 24, (correct - 1) % 24}:
            return slot if cron_hour == correct else None
    return None


def save_price_cache(
    database_url: str, prices: Mapping[str, float], currency_of: Mapping[str, str], as_of: dt.date
) -> None:
    """Persist each live quote as that symbol's last known good mark.

    Without this the `last_known` step of the resolution order has nothing to
    read, so a failed fetch degrades straight past a dated number to cost basis.
    """
    if not prices:
        return
    engine = build_engine(database_url)
    create_all(engine)
    factory = session_factory(engine)
    with session_scope(factory) as s:
        for symbol, price in prices.items():
            s.merge(
                PriceCache(
                    symbol=symbol,
                    price=float(price),
                    currency=currency_of.get(symbol, ""),
                    as_of=as_of,
                )
            )


def instrument_maps(
    instruments: Sequence[object], held: Sequence[str]
) -> tuple[dict[str, str], dict[str, str]]:
    """(ticker -> quote symbol, ticker -> currency) for held, quotable holdings.

    Shared by the dashboard and the scheduled job so the two can never value the
    same book against a different set of symbols.
    """
    held_set = set(held)
    symbols: dict[str, str] = {}
    currencies: dict[str, str] = {}
    for inst in instruments:
        ticker = getattr(inst, "ticker", None)
        symbol = getattr(inst, "symbol", None)
        if ticker in held_set and symbol:
            symbols[ticker] = symbol
            currencies[ticker] = getattr(inst, "currency", "")
    return symbols, currencies


@dataclass(frozen=True)
class SnapshotResult:
    """What was recorded, for the CLI to print and the app to show."""

    date: dt.date
    slot: str
    market_value: float
    book_value: float
    cash_value: float
    coverage: float
    daily_pnl: float | None
    daily_pnl_pct: float | None
    benchmark_pct: float | None
    unpriced: tuple[str, ...]


def record_snapshot(
    database_url: str,
    *,
    market_value: float,
    book_value: float,
    cash_value: float,
    coverage: float,
    on_date: dt.date,
    slot: str,
    daily_pnl: float | None = None,
    daily_pnl_pct: float | None = None,
    benchmark_pct: float | None = None,
) -> None:
    """Upsert one snapshot row (idempotent per date+slot)."""
    engine = build_engine(database_url)
    create_all(engine)
    factory = session_factory(engine)
    with session_scope(factory) as s:
        s.merge(
            Snapshot(
                date=on_date,
                slot=slot,
                market_value=market_value,
                book_value=book_value,
                cash_value=cash_value,
                price_coverage=coverage,
                daily_pnl=daily_pnl,
                daily_pnl_pct=daily_pnl_pct,
                benchmark_pct=benchmark_pct,
            )
        )


def daily_move(
    positions: Sequence[object],
    price_native: Mapping[str, float | None],
    previous_native: Mapping[str, float],
    fx_to_base: Mapping[str, float],
) -> tuple[float | None, float | None]:
    """The book's one-session move in base currency, and as a fraction.

    Summed per holding from its own two closes, so a contribution arriving
    between two snapshots cannot masquerade as a gain. Holdings without a prior
    close are excluded from both the move and the base it is measured against,
    which keeps the percentage honest rather than diluted.
    """
    move = 0.0
    base_value = 0.0
    counted = 0
    for p in positions:
        ticker = getattr(p, "ticker", None)
        price = price_native.get(ticker)  # type: ignore[arg-type]
        prior = previous_native.get(ticker)  # type: ignore[arg-type]
        if price is None or not prior:
            continue
        fx = fx_to_base.get(getattr(p, "currency", ""), 1.0)
        quantity = getattr(p, "quantity", 0.0)
        move += quantity * (price - prior) * fx
        base_value += quantity * prior * fx
        counted += 1
    if not counted:
        return None, None
    return move, (move / base_value if base_value > 0 else None)


def build_snapshot(
    cfg: object,
    database_url: str,
    *,
    slot: str,
    now: dt.datetime | None = None,
) -> SnapshotResult | None:
    """Value the book at current marks and record it. None when there is nothing
    to value.

    The whole cycle in one place — read the ledger, fetch marks, value, measure
    the daily move, persist the marks as last-known-good, write the row — so the
    dashboard button and the scheduled job produce identical rows. Two callers
    reimplementing this is how a manual snapshot and an automated one start
    disagreeing about the same day.
    """
    from desk.analytics.positions import build_ledger
    from desk.analytics.valuation import (
        portfolio_market_value,
        priced_coverage,
        value_positions,
    )
    from desk.services import portfolio as portfolio_service

    moment = now or dt.datetime.now(tz=ZoneInfo(cfg.locale.timezone))  # type: ignore[attr-defined]
    loaded = portfolio_service.load(database_url)
    result = build_ledger(loaded.entries)
    if not result.positions:
        return None

    base = cfg.locale.base_currency  # type: ignore[attr-defined]
    held = [p.ticker for p in result.positions]
    symbols, currencies = instrument_maps(cfg.instruments, held)  # type: ignore[attr-defined]
    prices, previous, fx = fetch_marks_with_previous(
        tuple(symbols.values()), tuple({*currencies.values(), base}), base
    )

    by_ticker = {t: prices.get(sym) for t, sym in symbols.items()}
    prior_by_ticker = {t: previous[sym] for t, sym in symbols.items() if sym in previous}
    valued = value_positions(result.positions, by_ticker, fx)
    # Measured once and carried on the result, so the row written to the database
    # and the figures reported back to the caller cannot drift apart.
    pnl, pnl_pct = daily_move(result.positions, by_ticker, prior_by_ticker, fx)
    outcome = SnapshotResult(
        date=moment.date(),
        slot=slot,
        market_value=portfolio_market_value(valued),
        book_value=sum(p.book_value_base for p in result.positions),
        cash_value=sum(amt for _, cur, amt in loaded.cash if cur == base),
        coverage=priced_coverage(valued),
        daily_pnl=pnl,
        daily_pnl_pct=pnl_pct,
        benchmark_pct=benchmark_daily_pct(cfg.benchmarks.daily),  # type: ignore[attr-defined]
        unpriced=tuple(v.position.ticker for v in valued if v.market_value_base is None),
    )

    save_price_cache(
        database_url,
        {sym: prices[sym] for sym in symbols.values() if sym in prices},
        {sym: currencies.get(t, "") for t, sym in symbols.items()},
        outcome.date,
    )
    record_snapshot(
        database_url,
        market_value=outcome.market_value,
        book_value=outcome.book_value,
        cash_value=outcome.cash_value,
        coverage=outcome.coverage,
        on_date=outcome.date,
        slot=outcome.slot,
        daily_pnl=outcome.daily_pnl,
        daily_pnl_pct=outcome.daily_pnl_pct,
        benchmark_pct=outcome.benchmark_pct,
    )
    return outcome


# Slot marking a reconstructed point: market value implied by past prices for the
# units held *today*, not something anybody observed at the time.
#
# Carried in `slot` rather than a new column deliberately. This project has no
# migration files yet, so `create_all` cannot add a column to a table that already
# exists in a deployed database — an ALTER TABLE by hand against live data is a
# worse risk than reusing a key that is already part of the primary key and
# already sized for it. Every existing query filters `slot == "close"`, so these
# rows are invisible to anything that has not opted in.
RECONSTRUCTED = "recon"


def reconstruct_history(
    cfg: object,
    database_url: str,
    *,
    period: str = "5y",
    freq: str = "W-FRI",
) -> tuple[int, dt.date, dt.date] | None:
    """Fill the performance chart from price history. Returns (rows, first, last).

    The chart otherwise starts empty and takes two trading days to draw its first
    line, and months to be worth looking at. This values the units held *today*
    at each past date, which is a backtest of the current book rather than a record
    of what was actually held — a lot bought last month is projected backwards as
    though it had always been there.

    That makes these rows a different kind of fact from a recorded snapshot, and
    they are stored under their own slot so nothing conflates the two. Book value is
    deliberately left null: the ledger cannot say what the cost base was at each
    past date without trade dates for every lot, and a flat line at today's book
    would assert there had been no contributions.

    Weekly by default. Daily adds five times the rows to say the same thing on a
    multi-year chart.
    """
    from desk.analytics.positions import build_ledger
    from desk.services import portfolio as portfolio_service

    loaded = portfolio_service.load(database_url)
    result = build_ledger(loaded.entries)
    if not result.positions:
        return None

    base = cfg.locale.base_currency  # type: ignore[attr-defined]
    held = [p.ticker for p in result.positions]
    symbols, currencies = instrument_maps(cfg.instruments, held)  # type: ignore[attr-defined]
    if not symbols:
        return None
    history = base_history(symbols, currencies, base, period)
    if history.empty:
        return None

    units: dict[str, float] = {}
    for position in result.positions:
        units[position.ticker] = units.get(position.ticker, 0.0) + position.quantity
    columns = [c for c in history.columns if c in units]
    if not columns:
        return None

    # Every held name must be priced on a date for its total to mean anything;
    # a date missing one holding would dip the whole series.
    aligned = history[columns].dropna()
    if aligned.empty:
        return None
    values = (aligned * pd.Series({c: units[c] for c in columns})).sum(axis=1)
    sampled = values.resample(freq).last().dropna() if freq else values

    engine = build_engine(database_url)
    create_all(engine)
    factory = session_factory(engine)
    with session_scope(factory) as s:
        for stamp, amount in sampled.items():
            s.merge(
                Snapshot(
                    date=stamp.date() if hasattr(stamp, "date") else stamp,
                    slot=RECONSTRUCTED,
                    market_value=float(amount),
                    book_value=None,
                    cash_value=None,
                    price_coverage=1.0,
                )
            )
    index = pd.DatetimeIndex(sampled.index)
    return len(sampled), index.min().date(), index.max().date()


def comparator_history(
    comparators: Sequence[tuple[str, str, str]],
    base: str,
    period: str = "5y",
) -> dict[str, pd.Series]:
    """Price history per comparator label, converted into base currency.

    Each entry is (label, symbol, currency); an empty currency means the series is
    already in base. Reuses `base_history`, so a comparator quoted in a foreign
    currency carries the same FX treatment as a holding — the return a base-currency
    investor would actually have experienced, currency move included.
    """
    if not comparators:
        return {}
    symbols = {label: symbol for label, symbol, _ in comparators}
    currencies = {label: (currency or base) for label, _, currency in comparators}
    frame = base_history(symbols, currencies, base, period)
    return {label: frame[label].dropna() for label in frame.columns}


def read_snapshots(database_url: str) -> pd.DataFrame:
    """Every recorded snapshot, one row per date+slot, oldest first."""
    engine = build_engine(database_url)
    create_all(engine)
    factory = session_factory(engine)
    with session_scope(factory) as s:
        rows = s.execute(select(Snapshot).order_by(Snapshot.date, Snapshot.slot)).scalars().all()
        data = [
            {
                "date": r.date,
                "slot": r.slot,
                "market_value": r.market_value,
                "book_value": r.book_value,
                "cash_value": r.cash_value,
                "price_coverage": r.price_coverage,
            }
            for r in rows
        ]
    return pd.DataFrame(data)
