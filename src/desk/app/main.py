"""Dashboard shell.

Contains no financial math and no SQL. Everything it renders comes from
`desk.services` or `desk.analytics`, which is what makes a second frontend a
leaf-node swap rather than a rewrite.

Branding is read from configuration — the monogram letters, the palette, the
name. Nothing identifying is hardcoded anywhere in this package.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from typing import Any

import pandas as pd
import streamlit as st

from desk.analytics.positions import aggregate_by_ticker, build_ledger
from desk.config.loader import ConfigError, load
from desk.config.schema import PortfolioConfig
from desk.domain.types import LedgerResult, Position
from desk.services import demo as demo_service
from desk.settings import AuthMode, Settings, SettingsError, get_settings

CashRows = tuple[tuple[str, str, float], ...]

# Faint white, for chart gridlines. Named because an inline rgba() literal reads
# to the data-hygiene scanner as a currency amount.
GRID_COLOUR = "rgba(255, 255, 255, 0.06)"


def _load_book(settings: Settings, *, is_demo: bool) -> tuple[LedgerResult | None, CashRows]:
    """Source the book: synthetic in demo, the store otherwise.

    A leaf-node swap — both paths hand the same `LedgerResult` and cash rows to
    the renderer, which is why the dashboard needs no branch of its own.
    """
    if is_demo:
        book = demo_service.generate(today=dt.date.today())
        return build_ledger(book.entries), book.cash
    if settings.database_url is None:
        return None, ()
    from desk.services import portfolio as portfolio_service

    loaded = portfolio_service.load(settings.database_url.get_secret_value())
    return build_ledger(loaded.entries), loaded.cash


st.set_page_config(page_title="Portfolio", layout="wide", initial_sidebar_state="collapsed")


def _theme_css(cfg: PortfolioConfig) -> str:
    b = cfg.branding
    return f"""
    <style>
      .block-container {{ padding-top: 2.2rem; max-width: 1400px; }}
      /* Tabular numerals: figures in a column must line up to be comparable. */
      [data-testid="stMetricValue"], .dataframe td, .mono {{
          font-variant-numeric: tabular-nums;
          font-feature-settings: "tnum";
      }}
      .desk-header {{
          display: flex; align-items: center; gap: 0.9rem;
          border-bottom: 1px solid {b.primary}40;
          padding-bottom: 0.7rem; margin-bottom: 1.4rem;
      }}
      .desk-mark {{
          font-family: {b.serif}; font-size: 1.05rem; letter-spacing: 0.06em;
          color: {b.accent}; border: 1px solid {b.accent}; border-radius: 2px;
          padding: 0.32rem 0.6rem; line-height: 1;
      }}
      .desk-name {{
          font-family: {b.serif}; font-size: 1.32rem; letter-spacing: 0.01em;
      }}
      .desk-sub {{ opacity: 0.6; font-size: 0.85rem; margin-left: auto; }}
      .pos {{ color: {b.positive}; }}
      .neg {{ color: {b.negative}; }}
      .note {{ opacity: 0.62; font-size: 0.83rem; }}
    </style>
    """


def _header(cfg: PortfolioConfig, *, banner: str = "") -> None:
    mark = cfg.identity.initials or "··"
    st.markdown(_theme_css(cfg), unsafe_allow_html=True)
    st.markdown(
        f'<div class="desk-header">'
        f'<span class="desk-mark">{mark}</span>'
        f'<span class="desk-name">{cfg.identity.display_name}</span>'
        f'<span class="desk-sub">{cfg.identity.subtitle}</span>'
        f"</div>",
        unsafe_allow_html=True,
    )
    if banner:
        st.warning(banner, icon="⚠️")


def _fatal(title: str, detail: str) -> None:
    st.error(f"**{title}**\n\n```\n{detail}\n```")
    st.stop()


def main() -> None:
    # Settings first. An invalid configuration must stop the app before a
    # single widget renders, not after.
    try:
        settings = get_settings()
    except SettingsError as exc:
        _fatal("The application will not start", str(exc))
        return

    is_demo = settings.auth_mode is AuthMode.DEMO

    # Authenticate before anything else renders. An unauthenticated visitor
    # should see a login form and nothing else — not the app's structure, not
    # a configuration path, not an error describing what is installed.
    from desk.app.auth import require_auth, sign_out

    subject = require_auth(settings)

    def _db_config() -> Mapping[str, Any] | None:
        if settings.database_url is None:
            return None
        from desk.services import portfolio as portfolio_service

        return portfolio_service.load_config_payload(settings.database_url.get_secret_value())

    try:
        cfg = load(settings.config_path, allow_example=is_demo, db_fallback=_db_config)
    except ConfigError as exc:
        _fatal("Configuration problem", str(exc))
        return

    banner = (
        "Demonstration data. Generated from a fixed seed — these are not real holdings."
        if is_demo
        else ""
    )
    _header(cfg, banner=banner)

    tabs = st.tabs(["Overview", "Accounts", "Analytics", "Risk", "Manage", "Policy"])

    result, cash = _load_book(settings, is_demo=is_demo)
    db_url = settings.database_url.get_secret_value() if settings.database_url else None
    with tabs[0]:
        _overview(cfg, result, cash, is_demo=is_demo)
        # Performance above the per-holding tables: it is the thing most people
        # open the dashboard for, and it used to sit below both of them.
        _performance(cfg, db_url)
        _positions_tables(cfg, result)
    with tabs[2]:
        _analytics(cfg, result)
    with tabs[3]:
        _risk(cfg, result)
    for index, name in ((1, "Accounts"), (4, "Manage"), (5, "Policy")):
        with tabs[index]:
            st.markdown(
                f'<p class="note">{name} arrives in a later phase. '
                "The shell, the gate and the ledger engine are in place.</p>",
                unsafe_allow_html=True,
            )

    with st.sidebar:
        st.caption(f"signed in as {subject}")
        if settings.auth_mode is AuthMode.PASSCODE:
            st.button("Sign out", on_click=sign_out)


def _overview(
    cfg: PortfolioConfig,
    result: LedgerResult | None,
    cash: CashRows,
    *,
    is_demo: bool,
) -> None:
    if result is None or not result.positions:
        st.info(
            "No positions yet. Load a statement with `desk backfill`, or add trades "
            "on the Manage tab.",
            icon="📄",
        )
        return

    rolled = aggregate_by_ticker(result.positions)
    total_book = sum(p.book_value_base for p in rolled)
    realized = sum(r.gain_base for r in result.realized)
    ccy = cfg.locale.base_currency

    a, b, c, d = st.columns(4)
    a.metric("Book value", f"{total_book:,.0f} {ccy}")
    b.metric("Realized gains", f"{realized:,.0f} {ccy}")
    c.metric("Positions", f"{len(rolled)}")
    d.metric("Accounts", f"{len({p.account_id for p in result.positions})}")

    if cash:
        base_cash = sum(amt for _, cur, amt in cash if cur == ccy)
        other = [(cur, amt) for _, cur, amt in cash if cur != ccy]
        note = f"Uninvested cash: {base_cash:,.2f} {ccy}"
        if other:
            note += " · " + " · ".join(f"{amt:,.2f} {cur}" for cur, amt in other)
        st.caption(note)

    _allocation(cfg, result, rolled)


def _positions_tables(cfg: PortfolioConfig, result: LedgerResult | None) -> None:
    """Holding-by-holding detail, below the summary and the performance chart.

    Split out so the performance history sits above two long tables rather than
    beneath them. It was the last element on the tab, which put the chart most
    people open the dashboard to see off the bottom of the screen.
    """
    if result is None or not result.positions:
        return
    rolled = aggregate_by_ticker(result.positions)
    total_book = sum(p.book_value_base for p in rolled)
    ccy = cfg.locale.base_currency

    frame = pd.DataFrame(
        [
            {
                "Ticker": p.ticker,
                "Units": round(p.quantity, 4),
                "Average cost": round(p.acb_base, 4),
                f"Book value ({ccy})": round(p.book_value_base, 2),
                # ProgressColumn formats the raw value, so weights are carried
                # as percentage points rather than fractions.
                "Weight": 100.0 * p.book_value_base / total_book if total_book else 0.0,
            }
            for p in rolled
        ]
    )
    st.dataframe(
        frame,
        width="stretch",
        hide_index=True,
        column_config={
            "Weight": st.column_config.ProgressColumn(
                format="%.1f%%", min_value=0.0, max_value=100.0
            )
        },
    )

    st.subheader("By account")
    per_account = pd.DataFrame(
        [
            {
                "Account": next(
                    (a.label or a.id for a in cfg.accounts if a.id == p.account_id), p.account_id
                ),
                "Ticker": p.ticker,
                "Units": round(p.quantity, 4),
                f"Book value ({ccy})": round(p.book_value_base, 2),
            }
            for p in result.positions
        ]
    )
    st.dataframe(per_account, width="stretch", hide_index=True)
    st.markdown(
        '<p class="note">Positions are keyed by account, not spread across fixed '
        "columns, so adding an account is configuration rather than a code change.</p>",
        unsafe_allow_html=True,
    )


def _allocation(cfg: PortfolioConfig, result: LedgerResult, rolled: Sequence[Position]) -> None:
    """Allocation by market value, with the unrealized gain the marks imply.

    Falls back to a book-value note when no price arrives, rather than drawing a
    pie of cost basis and labelling it market value.
    """
    import plotly.graph_objects as go

    from desk.analytics.valuation import (
        portfolio_market_value,
        priced_coverage,
        value_positions,
    )

    ccy = cfg.locale.base_currency
    symbols, currencies = _instrument_maps(cfg, result)
    if not symbols:
        st.markdown(
            '<p class="note">No quotable holdings, so market value is unavailable. '
            "Figures below come from the ledger alone.</p>",
            unsafe_allow_html=True,
        )
        return

    try:
        prices, fx = _cached_marks(tuple(symbols.values()), tuple({*currencies.values(), ccy}), ccy)
    except Exception:
        prices, fx = {}, {}
    by_ticker = {t: prices.get(sym) for t, sym in symbols.items()}
    valued = value_positions(rolled, by_ticker, fx)
    priced = [v for v in valued if v.market_value_base is not None]
    if not priced:
        st.markdown(
            '<p class="note">Prices are unavailable right now, so the figures below '
            "come from the ledger alone.</p>",
            unsafe_allow_html=True,
        )
        return

    market_value = portfolio_market_value(valued)
    book = sum(v.position.book_value_base for v in priced)
    gain = market_value - book
    coverage = priced_coverage(valued)

    a, b_col, c = st.columns(3)
    a.metric("Market value", f"{market_value:,.0f} {ccy}")
    b_col.metric(
        "Unrealized gain",
        f"{gain:,.0f} {ccy}",
        delta=f"{(gain / book):.2%}" if book else None,
    )
    c.metric("Priced", f"{coverage:.0%} of book")

    b = cfg.branding
    ordered = sorted(priced, key=lambda v: v.market_value_base or 0.0, reverse=True)
    palette = list(b.categorical) or [b.primary, b.accent]
    fig = go.Figure(
        go.Pie(
            labels=[v.position.ticker for v in ordered],
            values=[v.market_value_base for v in ordered],
            hole=0.62,
            sort=False,
            marker={
                "colors": [palette[i % len(palette)] for i in range(len(ordered))],
                "line": {"color": b.primary, "width": 1},
            },
            textinfo="label+percent",
            textfont={"family": b.serif, "size": 12},
            hovertemplate="%{label}: %{value:,.0f} " + ccy + " (%{percent})<extra></extra>",
        )
    )
    fig.update_layout(
        height=340,
        margin={"l": 8, "r": 8, "t": 8, "b": 8},
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font={"family": b.serif, "color": b.ink, "size": 13},
        showlegend=False,
        annotations=[
            {
                "text": f"{market_value:,.0f}<br><span style='font-size:0.7em'>{ccy}</span>",
                "x": 0.5,
                "y": 0.5,
                "showarrow": False,
                "font": {"family": b.serif, "size": 17, "color": b.ink},
            }
        ],
    )
    chart, gains = st.columns([1, 1])
    with chart:
        st.markdown("##### Allocation by market value")
        st.plotly_chart(fig, use_container_width=True)
    with gains:
        st.markdown("##### Gain / loss by holding")
        ranked = sorted(priced, key=lambda v: v.gain_base or 0.0)
        bars = go.Figure(
            go.Bar(
                x=[v.gain_base for v in ranked],
                y=[v.position.ticker for v in ranked],
                orientation="h",
                marker={
                    "color": [
                        b.positive if (v.gain_base or 0.0) >= 0 else b.negative for v in ranked
                    ]
                },
                hovertemplate="%{y}: %{x:,.0f} " + ccy + "<extra></extra>",
            )
        )
        bars.update_layout(
            height=340,
            margin={"l": 8, "r": 8, "t": 8, "b": 8},
            paper_bgcolor="rgba(0,0,0,0)",
            plot_bgcolor="rgba(0,0,0,0)",
            font={"family": b.serif, "color": b.ink, "size": 13},
            xaxis={"gridcolor": GRID_COLOUR, "tickformat": ",.0f", "title": ccy},
            yaxis={"showgrid": False},
        )
        st.plotly_chart(bars, use_container_width=True)
    if coverage < 0.999:
        st.markdown(
            f'<p class="note">{coverage:.0%} of book value carried a live price; '
            "unpriced holdings are left out of the chart rather than shown at cost.</p>",
            unsafe_allow_html=True,
        )

    _attribution_report(cfg, valued, fx)


def _attribution_report(
    cfg: PortfolioConfig, valued: Sequence[Any], fx: Mapping[str, float]
) -> None:
    """Where the gain came from: per holding, and price versus currency."""
    from desk.analytics.valuation import attribution

    ccy = cfg.locale.base_currency
    report = attribution(valued, fx)
    if not report.rows or report.total_book <= 0:
        return

    with st.expander("Attribution report", expanded=False):
        a, b_col, c = st.columns(3)
        a.metric(
            "Total unrealized",
            f"{report.total_gain:,.0f} {ccy}",
            delta=f"{report.total_return:.2%}" if report.total_return else None,
        )
        b_col.metric("From prices", f"{report.price_gain:,.0f} {ccy}")
        c.metric("From currency", f"{report.fx_gain:,.0f} {ccy}")

        frame = pd.DataFrame(
            [
                {
                    "Ticker": r.ticker,
                    "Cur": r.currency,
                    "Units": round(r.quantity, 4),
                    f"ACB ({ccy})": round(r.acb_base, 4),
                    "Price": None if r.price_native is None else round(r.price_native, 4),
                    f"Book ({ccy})": round(r.book_value_base, 2),
                    f"Market ({ccy})": (
                        None if r.market_value_base is None else round(r.market_value_base, 2)
                    ),
                    f"Gain ({ccy})": None if r.gain_base is None else round(r.gain_base, 2),
                    "Return": r.return_pct,
                    "Weight": r.weight,
                    "Contribution": r.contribution,
                }
                for r in report.rows
            ]
        )
        st.dataframe(
            frame,
            width="stretch",
            hide_index=True,
            column_config={
                "Return": st.column_config.NumberColumn(format="percent"),
                "Weight": st.column_config.NumberColumn(format="percent"),
                "Contribution": st.column_config.NumberColumn(format="percent"),
            },
        )

        best = report.winners[0] if report.winners else None
        worst = report.losers[-1] if report.losers else None
        lines: list[str] = []
        if best is not None and best.gain_base is not None:
            lines.append(
                f"<strong>{best.ticker}</strong> contributed most, "
                f"{best.gain_base:,.0f} {ccy} "
                f"({(best.contribution or 0.0):+.2%} of the portfolio's return)"
            )
        if worst is not None and worst.gain_base is not None:
            lines.append(
                f"<strong>{worst.ticker}</strong> detracted most, "
                f"{worst.gain_base:,.0f} {ccy} "
                f"({(worst.contribution or 0.0):+.2%})"
            )
        if abs(report.fx_gain) > 0.005 * max(abs(report.total_gain), 1.0):
            share = report.fx_gain / report.total_gain if report.total_gain else 0.0
            lines.append(
                f"the exchange rate accounts for {report.fx_gain:,.0f} {ccy} "
                f"of the gain ({share:.0%}), separate from what the securities did"
            )
        if report.unpriced:
            lines.append("excluded for want of a price: " + ", ".join(report.unpriced))
        if lines:
            st.markdown(
                '<p class="note">' + ". ".join(lines).capitalize() + ".</p>",
                unsafe_allow_html=True,
            )
        st.markdown(
            '<p class="note">Contribution is each holding\'s gain over the '
            "portfolio's total cost, so the column sums to the portfolio return. "
            "Gain is market value less adjusted cost base, with cost frozen at the "
            "exchange rate on the trade date and market value at today's.</p>",
            unsafe_allow_html=True,
        )


# ---------------------------------------------------------------- market data
# Network calls are cached, not the provider object: a cached plain value is
# safe to share across sessions, a live client is not.


@st.cache_data(ttl=900, show_spinner=False)
def _cached_marks(
    symbols: tuple[str, ...], currencies: tuple[str, ...], base: str
) -> tuple[dict[str, float], dict[str, float]]:
    from desk.services.market import fetch_marks

    return fetch_marks(symbols, currencies, base)


@st.cache_data(ttl=3600, show_spinner=False)
def _cached_history(
    symbols: tuple[tuple[str, str], ...],
    currencies: tuple[tuple[str, str], ...],
    base: str,
    period: str,
) -> pd.DataFrame:
    from desk.services.market import base_history

    return base_history(dict(symbols), dict(currencies), base, period)


def _instrument_maps(
    cfg: PortfolioConfig, result: LedgerResult
) -> tuple[dict[str, str], dict[str, str]]:
    """Ticker -> quote symbol, and ticker -> currency, for held positions only.

    One implementation, in the service layer, shared with the scheduled job: the
    dashboard and the snapshot must resolve the same book to the same symbols or
    they will quietly report different market values for the same day.
    """
    from desk.services.market import instrument_maps

    return instrument_maps(cfg.instruments, [p.ticker for p in result.positions])


@st.cache_data(ttl=900, show_spinner=False)
def _cached_comparators(
    comparators: tuple[tuple[str, str, str], ...], base: str
) -> dict[str, pd.Series]:
    from desk.services.market import comparator_history

    return comparator_history(comparators, base)


def _performance(cfg: PortfolioConfig, db_url: str | None) -> None:
    """Fetch prices, record a snapshot, and chart the recorded history."""
    import plotly.graph_objects as go

    from desk.services.market import read_snapshots

    st.divider()
    st.subheader("Performance history")
    if db_url is None:
        st.markdown(
            '<p class="note">Snapshots need a database. Set DESK_DATABASE_URL.</p>',
            unsafe_allow_html=True,
        )
        return

    b = cfg.branding
    left, right = st.columns([1, 3])
    if left.button("↻ Fetch prices", use_container_width=True):
        _record_now(cfg, db_url)
    right.markdown(
        '<p class="note">Fetches live quotes, values the book, and records one '
        "open/close point. Market value is only ever built from prices that "
        "actually arrived — the coverage figure says how much.</p>",
        unsafe_allow_html=True,
    )

    snaps = read_snapshots(db_url)
    if snaps.empty:
        st.markdown(
            '<p class="note">No snapshots yet. Press <em>Fetch prices</em> to record '
            "the first one, or run <code>desk backfill-snapshots</code> to fill the "
            "chart from price history straight away.</p>",
            unsafe_allow_html=True,
        )
        return

    from desk.analytics.risk import rebase
    from desk.services.market import RECONSTRUCTED

    # Reconstructed points value today's units at past prices. They are a backtest
    # of the current book, not observations, so they are drawn as their own series
    # and never merged into the recorded one.
    history = snaps[snaps["slot"] == RECONSTRUCTED].sort_values("date")
    recorded = snaps[snaps["slot"] != RECONSTRUCTED]
    closes = recorded[recorded["slot"] == "close"]
    series = closes if not closes.empty else recorded
    labels = [d.strftime("%b %d") for d in pd.to_datetime(series["date"])]
    ccy = cfg.locale.base_currency
    # One recorded point draws an invisible line, so markers carry the series
    # until there is a history to join up.
    sparse = len(series) < 2
    mode = "markers" if sparse else "lines+markers"
    fig = go.Figure()
    overlaid: list[str] = []
    if not history.empty:
        history_dates = pd.to_datetime(history["date"])
        fig.add_trace(
            go.Scatter(
                x=[d.strftime("%b %d") for d in history_dates],
                y=history["market_value"],
                mode="lines",
                name="This portfolio",
                # The hero series: brightest colour, and clearly the thickest line
                # on the chart. Dashed still carries "reconstructed, not observed",
                # but weight and brightness say "read this one first".
                line={"color": b.accent, "width": 3.0, "dash": "dash"},
                hovertemplate="<b>%{y:,.0f} " + ccy + "</b><extra>Portfolio</extra>",
            )
        )
        # Comparators are rebased to the reconstructed series' own starting value, so
        # the question they answer is "what would the same money have become". That
        # comparison is fair precisely because the reconstruction holds units
        # constant: both sides are buy-and-hold from the same date, with no
        # contributions on either.
        start_value = float(history["market_value"].iloc[0])
        # Benchmarks share one hue, separated from each other by dash pattern and
        # opacity rather than by a second and third colour. Three lines in three
        # shades of one palette is what made this unreadable; a portfolio and the
        # things it is measured against are two categories, not three peers.
        dashes = ("dot", "longdash", "dashdot")
        for index, (label, prices) in enumerate(
            _cached_comparators(
                tuple((c.label, c.symbol, c.currency or "") for c in cfg.benchmarks.comparators),
                ccy,
            ).items()
        ):
            aligned = rebase(prices.reindex(history_dates).ffill().dropna(), start_value)
            if aligned.empty:
                continue
            overlaid.append(label)
            fig.add_trace(
                go.Scatter(
                    x=[d.strftime("%b %d") for d in pd.DatetimeIndex(aligned.index)],
                    y=aligned.to_numpy(),
                    mode="lines",
                    name=label,
                    line={
                        "color": b.benchmark,
                        "width": 1.7,
                        "dash": dashes[index % len(dashes)],
                    },
                    opacity=1.0 - 0.25 * index,
                    hovertemplate="%{y:,.0f} "
                    + ccy
                    + f"<extra>{label} — same starting amount</extra>",
                )
            )
    fig.add_trace(
        go.Scatter(
            x=labels,
            y=series["market_value"],
            mode=mode,
            name="Market value",
            line={"color": b.primary, "width": 2},
            marker={"color": b.primary, "size": 10 if sparse else 7},
            hovertemplate="%{x}: %{y:,.0f} " + ccy + "<extra>Market value</extra>",
        )
    )
    fig.add_trace(
        go.Scatter(
            x=labels,
            y=series["book_value"],
            mode="markers" if sparse else "lines+markers",
            name="Book value",
            line={"color": b.accent, "width": 1.6, "dash": "dot", "shape": "hv"},
            marker={
                "color": b.accent,
                "size": 10 if sparse else 6,
                "symbol": "diamond",
            },
            hovertemplate="%{x}: %{y:,.0f} " + ccy + "<extra>Book value</extra>",
        )
    )
    # A category axis prints every label, which at weekly sampling over eighteen
    # months is roughly eighty of them overlapping into a grey band. Thin to a
    # readable number and let plotly space them evenly.
    tick_count = max(len(labels), len(history))
    fig.update_layout(
        height=440,
        margin={"l": 8, "r": 8, "t": 44, "b": 8},
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font={"family": b.serif, "color": b.ink, "size": 13},
        xaxis={
            "type": "category",
            "showgrid": False,
            "nticks": 8,
            "tickangle": 0,
            "tickfont": {"size": 12},
        },
        yaxis={
            "gridcolor": GRID_COLOUR,
            "tickformat": ",.0f",
            "title": None,
            "ticksuffix": f"  {ccy}",
            "tickfont": {"size": 12},
        },
        # Hover on the nearest x across every series at once, so the portfolio and
        # its benchmarks are read together rather than one at a time.
        hovermode="x unified",
        hoverlabel={"font": {"family": b.serif, "size": 13}},
        legend={
            "orientation": "h",
            "y": 1.14,
            "x": 0,
            "font": {"size": 13},
            "itemwidth": 40,
        },
    )
    if tick_count > 40:
        fig.update_xaxes(nticks=6)
    st.plotly_chart(fig, use_container_width=True)
    if not history.empty:
        st.markdown(
            '<p class="note">The dashed line is <strong>reconstructed</strong>: the units '
            "held today, valued at the prices of each past date. It is a backtest of the "
            "current book, not a record of it — a position opened last month is projected "
            "backwards as though it had always been there, and contributions are invisible. "
            "Solid markers are real recorded snapshots. Only those carry book value, which "
            "is why the reconstructed line has no companion.</p>",
            unsafe_allow_html=True,
        )
    if overlaid:
        st.markdown(
            f'<p class="note">Dotted lines are <strong>{" and ".join(overlaid)}</strong>, '
            "each started at the same amount on the same date, so the gap is the "
            "difference in return rather than in size. The comparison is a fair one "
            "here because both sides are buy-and-hold from that date — once recorded "
            "snapshots include contributions, a like-for-like comparison needs a "
            "money-weighted return instead.</p>",
            unsafe_allow_html=True,
        )
    if sparse:
        st.markdown(
            '<p class="note">One recorded point so far, shown as markers — the gap '
            "between them is the unrealized gain. Once the daily job has run a few "
            "times the recorded series joins up into a history of its own.</p>",
            unsafe_allow_html=True,
        )

    # Recorded rows only. The reconstructed series has no open/close distinction,
    # so passing it here would add a row per reconstructed date with both columns
    # empty — dozens of blank lines above the handful of real ones.
    if not recorded.empty:
        _open_close_table(recorded, ccy)


def _record_now(cfg: PortfolioConfig, db_url: str) -> None:
    """Value the book at current marks and store the result as a snapshot.

    Delegates the whole cycle to the same service function the scheduled job
    calls, so a snapshot taken by hand is indistinguishable from one taken at
    the bell — same marks, same daily-move basis, same columns populated.
    """
    from zoneinfo import ZoneInfo

    from desk.services.market import build_snapshot, resolve_slot

    base = cfg.locale.base_currency
    tz = cfg.locale.timezone
    now = dt.datetime.now(tz=ZoneInfo(tz))
    with st.spinner("Fetching quotes…"):
        # The cached-marks helper feeds the allocation donut on the same page;
        # clearing it keeps that from showing older prices than the row just
        # written.
        _cached_marks.clear()
        slot = resolve_slot(None, now, tz) or "close"
        outcome = build_snapshot(cfg, db_url, slot=slot, now=now)
    if outcome is None:
        st.warning("No positions to value yet.")
        return
    if outcome.coverage < 0.999:
        missing = f" Unpriced: {', '.join(outcome.unpriced)}." if outcome.unpriced else ""
        st.warning(
            f"Recorded, but only {outcome.coverage:.0%} of book value carried a "
            f"live price.{missing}",
            icon="⚠️",
        )
    else:
        st.success(
            f"Recorded {outcome.market_value:,.0f} {base} ({outcome.slot}) at full coverage."
        )


def _open_close_table(snaps: pd.DataFrame, ccy: str) -> None:
    """Open, intraday, close, overnight and the 24-hour move, per day."""
    wide = snaps.pivot_table(index="date", columns="slot", values="market_value")
    for column in ("open", "close"):
        if column not in wide.columns:
            wide[column] = pd.NA
    wide = wide.sort_index()
    prior_close = wide["close"].shift(1)
    table = pd.DataFrame(
        {
            "Date": [d.strftime("%b %d, %Y") for d in pd.to_datetime(wide.index)],
            f"Open ({ccy})": wide["open"].round(2),
            "Intraday": (wide["close"] - wide["open"]).round(2),
            f"Close ({ccy})": wide["close"].round(2),
            "Overnight": (wide["open"] - prior_close).round(2),
            "24h return": (wide["close"] / prior_close - 1.0),
        }
    ).iloc[::-1]
    st.markdown("##### Daily open and close")
    st.dataframe(
        table,
        width="stretch",
        hide_index=True,
        column_config={
            "24h return": st.column_config.NumberColumn(format="%.2f%%"),
        },
    )
    st.markdown(
        '<p class="note">Intraday is close minus open; overnight is open minus the '
        "prior close; the 24-hour move is the two combined. Blank cells are days "
        "with only one recorded slot.</p>",
        unsafe_allow_html=True,
    )


def _analytics(cfg: PortfolioConfig, result: LedgerResult | None) -> None:
    """Analytics views, grouped so the tab strip stays readable."""
    correlations, factors, xray = st.tabs(["Correlations", "Factor Exposure", "Holdings X-Ray"])
    with correlations:
        _correlations(cfg, result)
    with factors:
        _factor_exposure(cfg, result)
    with xray:
        _holdings_xray(cfg, result)


def _correlations(cfg: PortfolioConfig, result: LedgerResult | None) -> None:
    """Correlation of monthly returns across held positions."""
    import plotly.graph_objects as go

    from desk.analytics.risk import correlation_matrix

    st.subheader("Correlation matrix")
    if result is None or not result.positions:
        st.markdown('<p class="note">No positions yet.</p>', unsafe_allow_html=True)
        return
    symbols, currencies = _instrument_maps(cfg, result)
    if len(symbols) < 2:
        st.markdown('<p class="note">Two quotable holdings are needed.</p>', unsafe_allow_html=True)
        return
    with st.spinner("Loading price history…"):
        history = _cached_history(
            tuple(symbols.items()), tuple(currencies.items()), cfg.locale.base_currency, "5y"
        )
    corr = correlation_matrix(history)
    if corr.empty:
        st.markdown(
            '<p class="note">Not enough overlapping history yet.</p>', unsafe_allow_html=True
        )
        return

    labels = list(corr.columns)
    k = len(labels)
    z, text = [], []
    for i in range(1, k):  # lower triangle only; the mirror adds no information
        z.append([corr.values[i][j] if j < i else None for j in range(k - 1)])
        text.append([f"{corr.values[i][j]:.2f}" if j < i else "" for j in range(k - 1)])
    b = cfg.branding
    fig = go.Figure(
        go.Heatmap(
            z=z,
            x=labels[:-1],
            y=labels[1:],
            zmin=0,
            zmax=1,
            # Two plain-hex stops from the configured palette. A sequential ramp in
            # one hue is the right encoding for a magnitude, and plotly rejects both
            # eight-digit hex and a transparent stop here.
            colorscale=[[0.0, b.primary], [1.0, b.accent]],
            text=text,
            texttemplate="%{text}",
            textfont={"size": 12, "color": b.ink},
            hoverongaps=False,
            showscale=False,
            hovertemplate="%{y} x %{x}: %{z:.2f}<extra></extra>",
        )
    )
    fig.update_layout(
        height=460,
        margin={"l": 8, "r": 8, "t": 8, "b": 8},
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font={"family": b.serif, "color": b.ink, "size": 13},
        yaxis={"autorange": "reversed"},
    )
    st.plotly_chart(fig, use_container_width=True)
    st.markdown(
        '<p class="note">Monthly returns in base currency, computed pairwise so two '
        "holdings with different inception dates are compared over the history they "
        "share. Darker is more diversifying.</p>",
        unsafe_allow_html=True,
    )


COMPOSITION_PATH = "data/lookthrough/composition.json.gz"


@st.cache_data(ttl=3600, show_spinner=False)
def _cached_compositions(path: str) -> object:
    from pathlib import Path

    from desk.intake.lookthrough import read

    return read(Path(path))


def _holdings_xray(cfg: PortfolioConfig, result: LedgerResult | None) -> None:
    """Every fund resolved to the companies inside it, with overlap made visible."""
    import plotly.graph_objects as go

    from desk.analytics.lookthrough import companies_frame, look_through, overlap_table

    st.subheader("Holdings X-Ray")
    if result is None or not result.positions:
        st.markdown('<p class="note">No positions yet.</p>', unsafe_allow_html=True)
        return

    compositions = _cached_compositions(COMPOSITION_PATH)
    if not compositions or not isinstance(compositions, tuple):
        st.markdown(
            '<p class="note">No composition data yet. Download each fund\'s published '
            "holdings file into <code>inbox/</code>, describe them in a manifest "
            "(copy <code>data/lookthrough/manifest.example.yaml</code>), then run "
            "<code>desk build-lookthrough</code>. See <code>docs/lookthrough.md</code>.</p>",
            unsafe_allow_html=True,
        )
        return

    symbols, currencies = _instrument_maps(cfg, result)
    weights = _exposure_weights(cfg, result, symbols, currencies)
    report = look_through(weights, compositions)
    if report.total <= 0:
        st.markdown('<p class="note">Nothing to value.</p>', unsafe_allow_html=True)
        return

    ccy = cfg.locale.base_currency
    b = cfg.branding

    # Coverage first, before any chart. Every percentage below is a share of the
    # resolved sleeve, and the reader needs to know how big that sleeve is before
    # reading them.
    if report.coverage < 0.999:
        parts: list[str] = []
        if report.unresolved:
            parts.append(
                "Holding no securities to resolve: "
                + " · ".join(
                    f"<strong>{t}</strong> {v:,.0f} {ccy} ({why})"
                    for t, (v, why) in sorted(report.unresolved.items(), key=lambda kv: -kv[1][0])
                )
            )
        if report.partial:
            parts.append(
                "Published in part only, so the rest of each is unseen rather than "
                "absent: "
                + " · ".join(
                    f"<strong>{t}</strong> {shown} of {held:,}"
                    for t, (_, shown, held) in sorted(
                        report.partial.items(), key=lambda kv: -kv[1][0]
                    )
                )
            )
        st.markdown(
            f'<p class="note">Security detail covers <strong>{report.coverage:.0%}</strong> '
            f"of the portfolio. Everything else is excluded from the company, sector and "
            f"concentration figures below rather than diluted into them. "
            + ". ".join(parts)
            + ".</p>",
            unsafe_allow_html=True,
        )
    if report.stats_are_lower_bounds:
        st.markdown(
            '<p class="note">Because some funds publish only their largest holdings, '
            "every figure here is a <em>floor</em>. A company shown at 2% holds at least "
            "2%; it may also sit in the unpublished tail of another fund. Overlap counts "
            "understate for the same reason — the doubling shown is real, and there is "
            "likely more of it. Supplying full holdings files turns these into exact "
            "figures.</p>",
            unsafe_allow_html=True,
        )

    if report.is_empty:
        st.markdown(
            '<p class="note">No fund in the portfolio resolves to individual '
            "securities, so there is nothing to X-ray.</p>",
            unsafe_allow_html=True,
        )
        return

    s = report.stats
    top = report.companies[0]
    a, bb, c, d = st.columns(4)
    a.metric(
        f"Largest company ({top.ticker})",
        f"{top.weight:.2%}",
        help=f"{top.total:,.0f} {ccy}, arriving through {top.funds} "
        f"{'fund' if top.funds == 1 else 'separate funds'}.",
    )
    floor = "at least " if report.stats_are_lower_bounds else ""
    bb.metric("Top 10 companies", f"{floor}{s.top10:.1%}")
    c.metric("Companies seen", f"{floor}{s.distinct:,}")
    d.metric(
        "Held via 2+ sources",
        f"{floor}{sum(x.total for x in report.companies if x.funds >= 2) / report.total:.1%}",
        help="Share of the portfolio in companies arriving through more than one "
        "holding at once — a fund and a direct position, or two funds. This is the "
        "concentration a fund-level allocation chart cannot show.",
    )

    st.markdown(
        f'<p class="note">You hold <strong>{s.distinct:,} distinct securities</strong> '
        f"through {len([x for x in compositions if x.resolves_to_securities])} funds. "
        f"The ten largest are <strong>{s.top10:.0%}</strong> of the book, and "
        f"<strong>{s.overlap_3plus:.0%}</strong> sits in names carried by three or more "
        f"funds simultaneously. None of it was bought directly.</p>",
        unsafe_allow_html=True,
    )

    resolved_funds = [x.ticker for x in compositions if x.resolves_to_securities]
    palette = list(cfg.branding.categorical) or [b.primary, b.accent]
    colours = {f: palette[i % len(palette)] for i, f in enumerate(sorted(resolved_funds))}

    # ---- where each company comes from -----------------------------------
    st.markdown("##### Where each company comes from")
    count = st.slider("Companies shown", 5, 40, 15, step=5, label_visibility="collapsed")
    head = list(report.companies[:count])[::-1]
    fig = go.Figure()
    for fund in sorted(resolved_funds):
        values = [c.by_fund.get(fund, 0.0) for c in head]
        if not any(values):
            continue
        fig.add_bar(
            y=[c.ticker for c in head],
            x=values,
            name=fund,
            orientation="h",
            marker_color=colours[fund],
            hovertemplate="%{y} · " + fund + ": %{x:,.0f} " + ccy + "<extra></extra>",
        )
    fig.update_layout(
        barmode="stack",
        height=max(320, 26 * count),
        margin={"l": 8, "r": 8, "t": 8, "b": 8},
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font={"family": b.serif, "color": b.ink, "size": 13},
        xaxis={"gridcolor": GRID_COLOUR, "tickformat": ",.0f", "title": ccy},
        yaxis={"showgrid": False},
        legend={"orientation": "h", "y": 1.04, "x": 0},
    )
    st.plotly_chart(fig, use_container_width=True)
    st.markdown(
        '<p class="note">Bar length is total exposure to that company across every '
        "fund you own. A bar in several colours is one company you are buying more "
        "than once.</p>",
        unsafe_allow_html=True,
    )

    # ---- rollups ---------------------------------------------------------
    left, right = st.columns(2)
    with left:
        st.markdown("##### By region")
        _donut(cfg, report.region, ccy)
    with right:
        st.markdown("##### By sector")
        _sector_bars(cfg, report.sector)
    st.markdown(
        f'<p class="note">Region covers every holding whose contents carry a country, '
        f"including the synthetic sleeve. Sector covers the "
        f"{report.sector_base:,.0f} {ccy} equity sleeve of funds publishing "
        f"per-security sectors.</p>",
        unsafe_allow_html=True,
    )

    st.markdown("##### By asset class")
    _asset_bar(cfg, report.asset)

    # ---- overlap detail --------------------------------------------------
    overlap = overlap_table(report, minimum=2)
    if not overlap.empty:
        st.markdown("##### Companies you own more than once")
        st.dataframe(
            overlap,
            width="stretch",
            hide_index=True,
            column_config={
                "Total": st.column_config.NumberColumn(format="%.0f"),
                "Weight": st.column_config.NumberColumn(format="%.2f%%"),
                "Funds": st.column_config.NumberColumn(format="%d"),
            },
        )
        st.markdown(
            f'<p class="note">{len(overlap):,} companies arrive through more than one '
            f"fund. Buying the same name twice is not diversification, and at the fund "
            f"level it is invisible.</p>",
            unsafe_allow_html=True,
        )

    # ---- full table ------------------------------------------------------
    with st.expander(f"Every company ({s.distinct:,})"):
        frame = companies_frame(report, sorted(resolved_funds), limit=250)
        st.dataframe(
            frame,
            width="stretch",
            hide_index=True,
            height=420,
            column_config={
                "Weight": st.column_config.NumberColumn(format="%.3f%%"),
                "Total": st.column_config.NumberColumn(format="%.0f"),
                "Funds": st.column_config.NumberColumn(format="%d"),
                **{f: st.column_config.NumberColumn(format="%.0f") for f in sorted(resolved_funds)},
            },
        )
        st.markdown(
            f'<p class="note">Top 250 of {s.distinct:,}. {s.tail_count:,} holdings are '
            f"worth under 10 {ccy} each, {s.tail_value:,.0f} {ccy} in total.</p>",
            unsafe_allow_html=True,
        )

    stamp = report.as_of.strftime("%d %B %Y") if report.as_of else "an unrecorded date"
    st.markdown(
        f'<p class="note">Compositions are the funds\' published holdings as of '
        f"<strong>{stamp}</strong>, weighted by your live market values. The date is the "
        f"oldest of the files in play, not the newest — a blended figure is only as "
        f"current as its stalest input. Refresh with <code>desk build-lookthrough</code>.</p>",
        unsafe_allow_html=True,
    )


def _donut(cfg: PortfolioConfig, shares: Mapping[str, float], ccy: str) -> None:
    """A donut of fractional shares, largest first."""
    import plotly.graph_objects as go

    items = sorted(((k, v) for k, v in shares.items() if v > 0), key=lambda kv: -kv[1])
    if not items:
        st.markdown('<p class="note">Not available.</p>', unsafe_allow_html=True)
        return
    b = cfg.branding
    palette = list(b.categorical) or [b.primary, b.accent]
    fig = go.Figure(
        go.Pie(
            labels=[k for k, _ in items],
            values=[v for _, v in items],
            hole=0.62,
            marker={"colors": [palette[i % len(palette)] for i in range(len(items))]},
            textinfo="label+percent",
            hovertemplate="%{label}: %{percent}<extra></extra>",
        )
    )
    fig.update_layout(
        height=340,
        margin={"l": 8, "r": 8, "t": 8, "b": 8},
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font={"family": b.serif, "color": b.ink, "size": 13},
        showlegend=False,
    )
    st.plotly_chart(fig, use_container_width=True)


def _sector_bars(cfg: PortfolioConfig, shares: Mapping[str, float]) -> None:
    import plotly.graph_objects as go

    items = sorted(((k, v) for k, v in shares.items() if v > 0), key=lambda kv: kv[1])
    if not items:
        st.markdown(
            '<p class="note">No fund publishes per-security sectors.</p>',
            unsafe_allow_html=True,
        )
        return
    b = cfg.branding
    fig = go.Figure(
        go.Bar(
            x=[v for _, v in items],
            y=[k for k, _ in items],
            orientation="h",
            marker_color=b.accent,
            text=[f"{v:.1%}" for _, v in items],
            textposition="outside",
            cliponaxis=False,
            hovertemplate="%{y}: %{x:.2%}<extra></extra>",
        )
    )
    fig.update_layout(
        height=340,
        margin={"l": 8, "r": 8, "t": 8, "b": 8},
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font={"family": b.serif, "color": b.ink, "size": 13},
        xaxis={"gridcolor": GRID_COLOUR, "tickformat": ".0%"},
        yaxis={"showgrid": False},
    )
    st.plotly_chart(fig, use_container_width=True)


def _asset_bar(cfg: PortfolioConfig, shares: Mapping[str, float]) -> None:
    """One stacked bar of the asset mix."""
    import plotly.graph_objects as go

    order = (
        "Public equity",
        "Synthetic index exposure",
        "Digital assets",
        "Commodities",
        "Bonds",
        "Private markets",
        "Cash",
        "Other",
    )
    items = [(k, shares[k]) for k in order if shares.get(k, 0.0) > 0]
    items += [(k, v) for k, v in shares.items() if k not in order and v > 0]
    if not items:
        return
    b = cfg.branding
    palette = list(b.categorical) or [b.primary, b.accent]
    fig = go.Figure()
    for index, (label, value) in enumerate(items):
        fig.add_bar(
            x=[value],
            y=["mix"],
            name=label,
            orientation="h",
            marker_color=palette[index % len(palette)],
            hovertemplate=f"{label}: %{{x:.2%}}<extra></extra>",
        )
    fig.update_layout(
        barmode="stack",
        height=150,
        margin={"l": 8, "r": 8, "t": 8, "b": 8},
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font={"family": b.serif, "color": b.ink, "size": 13},
        xaxis={"tickformat": ".0%", "range": [0, 1], "gridcolor": GRID_COLOUR},
        yaxis={"showticklabels": False, "showgrid": False},
        legend={"orientation": "h", "y": -0.3, "x": 0},
    )
    st.plotly_chart(fig, use_container_width=True)


@st.cache_data(ttl=3600, show_spinner=False)
def _cached_factor_frame(provider_name: str, cache_dir: str) -> pd.DataFrame:
    from desk.services.factors import load_factors

    return load_factors(provider_name, cache_dir or None)


@st.cache_data(ttl=3600, show_spinner=False)
def _cached_exposure(
    symbols: tuple[tuple[str, str], ...],
    currencies: tuple[tuple[str, str], ...],
    weights: tuple[tuple[str, float], ...],
    provider_name: str,
    cache_dir: str,
    eligible: tuple[str, ...],
) -> object:
    from desk.services.factors import exposure

    return exposure(
        dict(symbols),
        dict(currencies),
        dict(weights),
        provider_name=provider_name,
        cache_dir=cache_dir or None,
        eligible=set(eligible),
    )


def _factor_exposure(cfg: PortfolioConfig, result: LedgerResult | None) -> None:
    """Fama-French five-factor plus momentum loadings, weighted by market value."""
    import plotly.graph_objects as go

    from desk.analytics.factors import (
        FactorExposure,
        annualised_alpha,
        blended_r_squared,
        factor_names,
        summarise,
        tilt_summary,
    )
    from desk.services.factors import factor_eligible

    st.subheader("Factor exposure")
    if result is None or not result.positions:
        st.markdown('<p class="note">No positions yet.</p>', unsafe_allow_html=True)
        return
    if cfg.data.factor_provider == "none":
        st.markdown(
            '<p class="note">No factor provider configured. Set '
            "<code>data.factor_provider: kenfrench</code> in your config.</p>",
            unsafe_allow_html=True,
        )
        return

    symbols, currencies = _instrument_maps(cfg, result)
    if not symbols:
        st.markdown('<p class="note">No quotable holdings.</p>', unsafe_allow_html=True)
        return

    # Weights are market value where a price arrived and book value otherwise, so
    # the denominator is the whole portfolio. Weighting by book alone would let a
    # holding that has doubled count as its cost.
    weights = _exposure_weights(cfg, result, symbols, currencies)
    # Holdings labelled as something other than equity are kept out of the
    # regression but keep their weight, so they show up as unattributed.
    eligible = factor_eligible(cfg.instruments, list(symbols))
    with st.spinner("Fetching factor returns and price history…"):
        exposure = _cached_exposure(
            tuple(symbols.items()),
            tuple(currencies.items()),
            tuple(sorted(weights.items())),
            cfg.data.factor_provider,
            cfg.data.cache_dir,
            tuple(sorted(eligible)),
        )
    if exposure is None or not isinstance(exposure, FactorExposure) or exposure.is_empty:
        st.markdown(
            '<p class="note">Factor data is unavailable right now — either the '
            "Dartmouth data library could not be reached, or no holding has the "
            "two years of overlapping monthly history the regression needs. "
            "No loadings are shown rather than unreliable ones.</p>",
            unsafe_allow_html=True,
        )
        return

    b = cfg.branding
    loadings = [exposure.portfolio[f] for f in exposure.factors]
    fig = go.Figure(
        go.Bar(
            x=list(exposure.factors),
            y=loadings,
            marker_color=[b.positive if v >= 0 else b.negative for v in loadings],
            text=[f"{v:+.2f}" for v in loadings],
            textposition="outside",
            hovertemplate="%{x}: %{y:+.3f}<extra></extra>",
        )
    )
    fig.update_layout(
        height=380,
        margin={"l": 8, "r": 8, "t": 20, "b": 8},
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font={"family": b.serif, "color": b.ink, "size": 13},
        xaxis={"showgrid": False},
        yaxis={"gridcolor": GRID_COLOUR, "title": "Loading (beta)", "zerolinecolor": "#5A4A50"},
    )
    st.plotly_chart(fig, use_container_width=True)

    tilts = tilt_summary(exposure)
    if tilts:
        st.markdown(
            f'<p class="note">Read as: the book is {"; ".join(tilts)}.</p>',
            unsafe_allow_html=True,
        )

    a, bb, c = st.columns(3)
    r2 = blended_r_squared(exposure)
    alpha = annualised_alpha(exposure)
    a.metric("Explained variation", "—" if r2 is None else f"{r2:.0%}")
    bb.metric("Annualised alpha", "—" if alpha is None else f"{alpha:+.2%}")
    c.metric("Holdings fitted", f"{len(exposure.fits)}")

    if exposure.unattributed > 0.001:
        excluded = ", ".join(exposure.excluded)
        st.markdown(
            f'<p class="note">{exposure.unattributed:.0%} of the portfolio carries no '
            f"loading and is excluded from the figures above rather than spread across "
            f"the rest: <strong>{excluded}</strong>. A holding is excluded when it has "
            f"no return history, less than two years of overlap with the factor window, "
            f"or no exchange-rate series to convert it into the factors' currency.</p>",
            unsafe_allow_html=True,
        )

    frame = summarise(exposure)
    st.dataframe(
        frame,
        width="stretch",
        hide_index=True,
        column_config={
            "Weight": st.column_config.NumberColumn(format="%.1f%%"),
            "R²": st.column_config.NumberColumn(format="%.2f"),
            "Alpha (monthly)": st.column_config.NumberColumn(format="%.2f%%"),
            **{f: st.column_config.NumberColumn(format="%.2f") for f in exposure.factors},
        },
    )

    window = exposure.window
    span = f"{window[0]} to {window[1]}" if window else "the available window"
    with st.expander("What these factors mean"):
        for name, meaning in factor_names().items():
            st.markdown(f"**{name}** — {meaning}")
    st.markdown(
        f'<p class="note">Monthly regressions over {span}, against the developed-markets '
        f"Fama-French five-factor set plus momentum. Every holding's price series is "
        f"converted into US dollars first, because the factors are USD-denominated — "
        f"skipping that step loads the {cfg.locale.base_currency}/USD move onto the "
        f"market beta and makes an index tracker look defensive. R² says how much of "
        f"each holding's movement the model explains at all; a low value means the "
        f"loadings beside it describe only a small part of what happened.</p>",
        unsafe_allow_html=True,
    )


def _exposure_weights(
    cfg: PortfolioConfig,
    result: LedgerResult,
    symbols: dict[str, str],
    currencies: dict[str, str],
) -> dict[str, float]:
    """Market value per ticker, falling back to book value when unpriced.

    Covers every held ticker, not just the quotable ones, so the unattributed
    share the factor model reports is measured against the whole portfolio.
    """
    from desk.analytics.positions import aggregate_by_ticker

    rolled = aggregate_by_ticker(result.positions)
    book = {p.ticker: p.book_value_base for p in rolled}
    ccy = cfg.locale.base_currency
    try:
        prices, fx = _cached_marks(tuple(symbols.values()), tuple({*currencies.values(), ccy}), ccy)
    except Exception:
        return book
    weights: dict[str, float] = {}
    for p in rolled:
        symbol = symbols.get(p.ticker)
        price = prices.get(symbol) if symbol else None
        rate = fx.get(p.currency, 1.0)
        weights[p.ticker] = p.quantity * price * rate if price is not None else book[p.ticker]
    return weights


def _risk(cfg: PortfolioConfig, result: LedgerResult | None) -> None:
    """Risk and return metrics for the book, against the configured benchmark."""
    from desk.analytics.risk import risk_stats

    st.subheader("Risk and return metrics")
    if result is None or not result.positions:
        st.markdown('<p class="note">No positions yet.</p>', unsafe_allow_html=True)
        return
    symbols, currencies = _instrument_maps(cfg, result)
    if not symbols:
        st.markdown('<p class="note">No quotable holdings.</p>', unsafe_allow_html=True)
        return

    base = cfg.locale.base_currency
    bench_symbol = cfg.benchmarks.risk
    fetch = dict(symbols)
    fetch_ccy = dict(currencies)
    if bench_symbol:
        fetch["__benchmark__"] = bench_symbol
        fetch_ccy["__benchmark__"] = base
    with st.spinner("Loading price history…"):
        history = _cached_history(tuple(fetch.items()), tuple(fetch_ccy.items()), base, "5y")
    if history.empty:
        st.markdown('<p class="note">Price history unavailable.</p>', unsafe_allow_html=True)
        return

    benchmark = history.pop("__benchmark__") if "__benchmark__" in history.columns else None
    units = {p.ticker: p.quantity for p in result.positions}
    columns = [c for c in history.columns if c in units]
    if not columns:
        st.markdown('<p class="note">No matching history.</p>', unsafe_allow_html=True)
        return
    # Current units held constant over history: a like-for-like backtest of the
    # book as it stands today, not a replay of when each lot was bought.
    values = (history[columns] * pd.Series({c: units[c] for c in columns})).sum(axis=1)
    # A zero risk-free rate turns every Sharpe-family ratio from an excess return
    # into a total return, which overstates them by roughly the cash rate over
    # volatility. The config already asks for the Ken French RF series; this uses it.
    rf = 0.0
    if cfg.risk.risk_free == "kenfrench_rf" and cfg.data.factor_provider != "none":
        from desk.analytics.risk import annual_risk_free

        try:
            rf = annual_risk_free(
                _cached_factor_frame(cfg.data.factor_provider, cfg.data.cache_dir)
            )
        except Exception:
            rf = 0.0
    stats = risk_stats(values.dropna(), benchmark, risk_free_rate=rf)

    if stats.periods < 6:
        st.markdown(
            f'<p class="note">Only {stats.periods} monthly periods available — too few '
            "for reliable statistics.</p>",
            unsafe_allow_html=True,
        )
        return

    pct = {
        "Arithmetic mean",
        "Geometric mean",
        "Volatility",
        "Downside deviation",
        "Maximum drawdown",
        "Alpha",
        "Active return",
        "Tracking error",
        "Historical VaR (5%)",
        "Analytical VaR (5%)",
        "Conditional VaR (5%)",
        "Up capture",
        "Down capture",
        "Positive periods",
    }
    rows = [
        ("Arithmetic mean", stats.arithmetic_mean),
        ("Geometric mean", stats.geometric_mean),
        ("Volatility", stats.volatility),
        ("Downside deviation", stats.downside_deviation),
        ("Maximum drawdown", stats.max_drawdown),
        ("Sharpe ratio", stats.sharpe),
        ("Sortino ratio", stats.sortino),
        ("Calmar ratio", stats.calmar),
        ("Beta", stats.beta),
        ("Alpha", stats.alpha),
        ("R squared", stats.r_squared),
        ("Treynor ratio", stats.treynor),
        ("Tracking error", stats.tracking_error),
        ("Information ratio", stats.information_ratio),
        ("Active return", stats.active_return),
        ("Skewness", stats.skew),
        ("Excess kurtosis", stats.excess_kurtosis),
        ("Historical VaR (5%)", stats.var_historical),
        ("Analytical VaR (5%)", stats.var_analytical),
        ("Conditional VaR (5%)", stats.cvar),
        ("Up capture", stats.up_capture),
        ("Down capture", stats.down_capture),
        ("Positive periods", stats.positive_periods),
        ("Gain/loss ratio", stats.gain_loss_ratio),
    ]
    from desk.analytics.risk import METRIC_BASIS, UNITLESS

    formatted = [
        {
            "Metric": label,
            "Value": (
                "—" if value is None else (f"{value:.2%}" if label in pct else f"{value:.2f}")
            ),
            # Stated per row rather than in a footnote. Twelve of these are
            # annualised and three are not, and under bare labels a monthly 5% VaR
            # reads as an annual one — an understatement of roughly three and a half
            # times, in the direction that makes a portfolio look safer.
            "Basis": METRIC_BASIS.get(label, UNITLESS),
        }
        for label, value in rows
    ]
    half = (len(formatted) + 1) // 2
    left, right = st.columns(2)
    left.dataframe(pd.DataFrame(formatted[:half]), width="stretch", hide_index=True)
    right.dataframe(pd.DataFrame(formatted[half:]), width="stretch", hide_index=True)
    bench_note = f" against {bench_symbol}" if bench_symbol else " (no benchmark configured)"
    rf_note = (
        f"Excess returns are measured over a {rf:.2%} annual risk-free rate, taken from "
        "the factor library's own cash series."
        if rf > 0
        else "The risk-free rate is zero, so the Sharpe family are total-return ratios "
        "rather than excess-return ones and read high."
    )
    st.markdown(
        f'<p class="note">Monthly returns over {stats.periods} periods{bench_note}, in '
        f"{base}, holding current units constant. {rf_note} Statistics needing more "
        "history than is available are left blank rather than estimated.</p>",
        unsafe_allow_html=True,
    )
    st.markdown(
        '<p class="note"><strong>Value at risk is left at its monthly horizon on '
        "purpose.</strong> Scaling a tail quantile to a year by the square root of "
        "twelve assumes returns are independent and normally distributed — which is "
        "the assumption a tail statistic exists to test, so the annualised figure "
        "would be least reliable in exactly the conditions it gets consulted for. "
        "Maximum drawdown is likewise the worst decline actually observed; scaling it "
        "would describe a loss nobody suffered.</p>",
        unsafe_allow_html=True,
    )


main()
