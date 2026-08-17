"""Command line entry points.

desk doctor          validate everything before you rely on it
desk hash-passcode   produce the argon2 hash to put in your environment
desk demo            build the synthetic portfolio and summarise it
desk backfill        load opening holdings from a statement
desk fetch-prices    record one market-value snapshot (what the scheduled job runs)
desk push-config     store the config as a row, for a read-only host
desk serve           run the dashboard
"""

from __future__ import annotations

import datetime as dt
import getpass
import sys
from collections.abc import Mapping
from typing import Any
from zoneinfo import ZoneInfo

import typer
from rich.console import Console
from rich.table import Table

from desk.analytics.positions import aggregate_by_ticker, build_ledger
from desk.config.loader import ConfigError, load, load_example, resolve_path
from desk.jurisdictions.ca import get_jurisdiction
from desk.security import hash_passcode
from desk.services import demo as demo_service

app = typer.Typer(add_completion=False, help="Portfolio analytics.")
console = Console()

OK = "[green]ok[/green]"
WARN = "[yellow]warn[/yellow]"
BAD = "[red]fail[/red]"


@app.command()
def doctor(
    config: str = typer.Option(None, "--config", "-c", help="path to portfolio.yaml"),
) -> None:
    """Validate configuration, settings and reference data.

    Runs in CI against the shipped example on every commit, so the example
    cannot rot into a file that documents a schema the code no longer accepts.
    """
    table = Table(title="desk doctor", show_header=True, header_style="bold")
    table.add_column("check")
    table.add_column("result")
    table.add_column("detail", overflow="fold")
    problems = 0

    # -- configuration --
    path = resolve_path(config)
    try:
        cfg = load(config) if path else load_example()
        source = str(path) if path else "config/portfolio.example.yaml (no config yet)"
        table.add_row("configuration", OK, source)
    except ConfigError as exc:
        table.add_row("configuration", BAD, str(exc))
        console.print(table)
        raise typer.Exit(1) from exc

    # -- accounts and room groups --
    if not cfg.accounts:
        table.add_row("accounts", WARN, "none declared — add them to config/portfolio.yaml")
        problems += 1
    else:
        groups = cfg.room_groups()
        shared = {g: a for g, a in groups.items() if len(a) > 1}
        detail = f"{len(cfg.accounts)} account(s)"
        if shared:
            detail += "; sharing one limit: " + ", ".join(
                f"{g} <- {len(a)} accounts" for g, a in shared.items()
            )
        table.add_row("accounts", OK, detail)

    # -- jurisdiction --
    jur = get_jurisdiction(cfg.jurisdiction.id)
    params = cfg.jurisdiction.params
    if cfg.jurisdiction.id == "ca":
        missing = []
        kinds = {a.type.value for a in cfg.accounts}
        if "tfsa" in kinds and params.birth_year is None:
            missing.append("birth_year")
        if "fhsa" in kinds and params.fhsa_open_year is None:
            missing.append("fhsa_open_year")
        if missing:
            table.add_row(
                "jurisdiction", WARN, f"room cannot be computed without: {', '.join(missing)}"
            )
            problems += 1
        else:
            table.add_row("jurisdiction", OK, f"{jur.id}, {len(jur.room_group_labels())} groups")
    else:
        table.add_row("jurisdiction", OK, f"{jur.id} (contribution room reported as unlimited)")

    # -- instruments --
    if not cfg.instruments:
        table.add_row("instruments", WARN, "none declared")
        problems += 1
    else:
        unquotable = [i.ticker for i in cfg.instruments if not i.symbol and i.kind != "private"]
        if unquotable:
            table.add_row("instruments", BAD, f"no quote symbol: {', '.join(unquotable)}")
            problems += 1
        else:
            table.add_row("instruments", OK, f"{len(cfg.instruments)} declared")

    # -- benchmarks: the field the reference conflated --
    if cfg.benchmarks.daily and cfg.benchmarks.risk:
        same = cfg.benchmarks.daily == cfg.benchmarks.risk
        table.add_row(
            "benchmarks",
            WARN if same else OK,
            "daily and risk benchmarks are the same symbol — intended?"
            if same
            else f"daily {cfg.benchmarks.daily}, risk {cfg.benchmarks.risk}",
        )
        problems += int(same)
    else:
        table.add_row("benchmarks", WARN, "not configured")
        problems += 1

    # -- policy --
    if cfg.policy.source.value == "sleeves":
        total = sum(s.weight for s in cfg.policy.sleeves) + cfg.policy.cash_target
        table.add_row(
            "policy", OK, f"{len(cfg.policy.sleeves)} sleeves, weights sum to {total:.4f}"
        )
    else:
        table.add_row("policy", OK, f"source is '{cfg.policy.source.value}'")

    # -- settings and auth --
    try:
        from desk.settings import get_settings

        settings = get_settings()
        table.add_row("settings", OK, f"env {settings.app_env.value}, auth {settings.auth_mode}")
    except Exception as exc:
        first = str(exc).splitlines()[0]
        table.add_row("settings", WARN, f"{first} (fine for CLI use; required to serve)")

    console.print(table)
    if problems:
        console.print(f"\n[yellow]{problems} item(s) need attention before this is trustworthy.")
    else:
        console.print("\n[green]All checks passed.")


@app.command("hash-passcode")
def hash_passcode_cmd() -> None:
    """Hash a passcode for DESK_PASSCODE_HASH.

    Prompted, never taken as an argument: a passcode passed on the command line
    lands in your shell history.
    """
    entered = getpass.getpass("Passcode (at least 12 characters): ")
    again = getpass.getpass("Again: ")
    if entered != again:
        console.print("[red]They do not match.")
        raise typer.Exit(1)
    try:
        digest = hash_passcode(entered)
    except ValueError as exc:
        console.print(f"[red]{exc}")
        raise typer.Exit(1) from exc
    console.print("\nAdd to your environment (or your host's secret store):\n")
    console.print(f"  DESK_AUTH_MODE=passcode\n  DESK_PASSCODE_HASH='{digest}'\n")
    console.print("[dim]The passcode itself is not stored anywhere.")


@app.command()
def demo(
    years: int = typer.Option(5, help="years of synthetic history"),
) -> None:
    """Generate the synthetic portfolio and summarise it.

    No real holdings are ever used as a fixture, which is what makes the demo
    safe to share and the screenshots safe to publish.
    """
    today = dt.date.today()
    book = demo_service.generate(today=today, years=years)
    result = build_ledger(book.entries)
    rolled = aggregate_by_ticker(result.positions)

    table = Table(
        title=f"Synthetic portfolio — {len(book.entries)} ledger entries", show_header=True
    )
    table.add_column("ticker")
    table.add_column("units", justify="right")
    table.add_column("avg cost", justify="right")
    table.add_column("book value", justify="right")
    for position in rolled:
        table.add_row(
            position.ticker,
            f"{position.quantity:,.2f}",
            f"{position.acb_base:,.2f}",
            f"{position.book_value_base:,.0f}",
        )
    console.print(table)

    total = sum(p.book_value_base for p in rolled)
    realized = sum(r.gain_base for r in result.realized)
    console.print(f"\nbook value      {total:>14,.0f}")
    console.print(f"realized gains  {realized:>14,.0f}  ({len(result.realized)} disposals)")
    console.print(f"accounts        {len({p.account_id for p in result.positions}):>14}")
    console.print(f"contributions   {len(book.contributions):>14}")
    console.print(f"private marks   {len(book.marks):>14}  (a time series, not one scalar)")
    console.print("\n[dim]Generated from a fixed seed. Nothing here belongs to anybody.")


@app.command()
def backfill(
    holdings: str = typer.Argument(..., help="path to a holdings YAML (kept outside the repo)"),
    db: str = typer.Option(..., "--db", help="database URL to load into"),
    config: str = typer.Option(None, "--config", "-c", help="path to portfolio.yaml"),
    reset: bool = typer.Option(
        False, "--reset", help="clear existing opening ledger, cash and contributions first"
    ),
) -> None:
    """Load opening holdings from a month-end statement into the ledger.

    Idempotent: each row carries a content hash, so re-running the same file is
    a no-op rather than a duplicate ledger. Instruments come from the config;
    positions, cash and contributions come from the holdings file. Use --reset
    when the account structure changed, so stale rows under old account ids do
    not linger alongside the new ones.
    """
    import datetime as _dt
    import hashlib
    from pathlib import Path

    import yaml
    from sqlalchemy import delete, select

    from desk.domain.types import Action
    from desk.store.engine import build_engine, create_all, session_factory, session_scope
    from desk.store.models import Cash, ContributionRow, Instrument, Transaction

    cfg = load(config) if resolve_path(config) else load_example()
    spec = yaml.safe_load(Path(holdings).read_text(encoding="utf-8"))
    usd_cad = float(spec.get("usd_cad", 1.0))
    as_of = spec.get("as_of", _dt.date.today())
    if isinstance(as_of, str):
        as_of = _dt.date.fromisoformat(as_of)

    engine = build_engine(db)
    create_all(engine)
    factory = session_factory(engine)

    entries = []  # (LedgerEntry-shaped) for the reconciliation print
    with session_scope(factory) as s:
        if reset:
            s.execute(delete(Transaction))
            s.execute(delete(Cash))
            s.execute(delete(ContributionRow))
        # instruments from config (currency declared, never inferred)
        for inst in cfg.instruments:
            s.merge(
                Instrument(
                    ticker=inst.ticker,
                    quote_symbol=inst.symbol,
                    currency=inst.currency,
                    kind=inst.kind.value,
                )
            )
        # Existing opening lots, keyed two ways: by content hash to recognise an
        # unchanged row, and by (account, ticker) to recognise a changed one.
        #
        # `merge` cannot do this. It matches on the primary key, which here is an
        # autoincrement id nobody supplies, so every call is an INSERT — which then
        # collides with the unique index on source_hash. The idempotency this
        # command has always claimed was never actually implemented.
        existing = (
            s.execute(select(Transaction).where(Transaction.note == "opening lot")).scalars().all()
        )
        seen_hashes = {t.source_hash for t in existing}
        seen_lots = {(t.account_id, t.ticker): t.source_hash for t in existing}

        unchanged, inserted, conflicts = 0, 0, []
        for h in spec.get("holdings", []):
            units = float(h["units"])
            book_native = float(h["book_native"])
            ccy = h.get("currency", "CAD")
            fx = usd_cad if ccy == "USD" else 1.0
            unit_cost = book_native / units if units else 0.0
            digest = hashlib.sha256(
                f"open|{h['account']}|{h['ticker']}|{units}|{book_native}".encode()
            ).hexdigest()
            entries.append((h["ticker"], h["account"], units, unit_cost, fx, ccy))

            if digest in seen_hashes:
                unchanged += 1
                continue
            prior = seen_lots.get((h["account"], h["ticker"]))
            if prior is not None and prior != digest:
                # Inserting would leave both lots in the ledger and double the
                # position — silently, and in the direction that flatters the
                # portfolio. Refuse and name the flag that does the right thing.
                conflicts.append(str(h["ticker"]))
                continue
            s.add(
                Transaction(
                    date=as_of,
                    ticker=h["ticker"],
                    account_id=h["account"],
                    action=Action.BUY.value,
                    quantity=units,
                    price=unit_cost,
                    fees=0.0,
                    fx_rate=fx,
                    source_hash=digest,
                    note="opening lot",
                )
            )
            inserted += 1

        if conflicts:
            console.print(
                f"[red]{', '.join(conflicts)} already have an opening lot with different "
                "units or cost.\n"
                "  Loading these would add a second lot and double the position rather "
                "than correct it.\n"
                "  Re-run with [bold]--reset[/bold] to replace the opening ledger."
            )
            raise typer.Exit(1)
        # cash and contributions
        for c in spec.get("cash", []):
            s.merge(
                Cash(account_id=c["account"], currency=c["currency"], amount=float(c["amount"]))
            )
        # Deduplicated on the natural key. ContributionRow's primary key is an
        # autoincrement id and it carries no unique constraint, so `merge` inserted
        # a fresh row on every run — silently, with nothing to collide against.
        # Duplicated contributions overstate room used, which is the direction that
        # wrongly reports someone as over-contributed.
        known = {
            (r.date, r.account_id, round(r.amount, 6), r.kind)
            for r in s.execute(select(ContributionRow)).scalars()
        }
        for c in spec.get("contributions", []):
            d = c["date"]
            d = _dt.date.fromisoformat(d) if isinstance(d, str) else d
            key = (d, c["account"], round(float(c["amount"]), 6), "contribution")
            if key in known:
                continue
            known.add(key)
            s.add(
                ContributionRow(
                    date=d,
                    account_id=c["account"],
                    amount=float(c["amount"]),
                    kind="contribution",
                    note=c.get("note"),
                )
            )

    # reconciliation, straight from the pure ledger engine
    from desk.analytics.positions import LedgerEntry, aggregate_by_ticker, build_ledger

    ledger = [
        LedgerEntry(
            date=as_of,
            ticker=t,
            account_id=a,
            action=Action.BUY,
            quantity=u,
            price=p,
            fx_rate=fx,
            currency=ccy,
        )
        for (t, a, u, p, fx, ccy) in entries
    ]
    result = build_ledger(ledger)
    rolled = aggregate_by_ticker(result.positions)
    table = Table(title="Backfilled holdings (book value, CAD)", show_header=True)
    table.add_column("ticker")
    table.add_column("units", justify="right")
    table.add_column("book (CAD)", justify="right")
    for p in rolled:
        table.add_row(p.ticker, f"{p.quantity:,.2f}", f"{p.book_value_base:,.2f}")
    console.print(table)
    if unchanged:
        console.print(f"[dim]{inserted} lot(s) loaded, {unchanged} already present and unchanged.")
    total = sum(p.book_value_base for p in rolled)
    console.print(
        f"\n[bold]book value[/bold]  {total:>14,.2f} CAD across "
        f"{len({p.account_id for p in result.positions})} accounts"
    )
    console.print("[dim]Market value needs the price layer; book value is exact from the ledger.")


@app.command("fetch-prices")
def fetch_prices(
    config: str = typer.Option(None, "--config", "-c", help="path to portfolio.yaml"),
    slot: str = typer.Option(
        "auto", "--slot", help="open | close | auto (from --schedule-cron, else time of day)"
    ),
    schedule_cron: str = typer.Option(
        "", "--schedule-cron", help="the cron that triggered this run; empty for a manual run"
    ),
) -> None:
    """Fetch quotes, value the book, and record one snapshot.

    This is what the scheduled job runs. The database URL comes from the
    environment via `desk.settings`, never an argument — a connection string
    passed on the command line is visible in the process list and lands in shell
    history and CI logs.

    With `--slot auto` and a `--schedule-cron`, a run whose cron does not match
    the current UTC offset exits without recording. That is how one local target
    can be registered twice (summer and winter) and still produce exactly one
    snapshot per day. See `resolve_slot` for why the decision keys off the
    scheduled cron rather than the wall clock.
    """
    from desk.services.market import build_snapshot, resolve_slot
    from desk.settings import SettingsError, get_settings

    try:
        settings = get_settings()
    except SettingsError as exc:
        console.print(f"[red]{exc}")
        raise typer.Exit(1) from exc
    if settings.database_url is None:
        console.print("[red]DESK_DATABASE_URL is not set; there is nowhere to record a snapshot.")
        raise typer.Exit(1)

    # Same resolution order as the dashboard: file, then the database row that
    # `desk push-config` writes, then the example. The scheduled job therefore
    # needs no config committed and no config secret — it reads the same policy
    # the app is running on, which is also the only way the two cannot disagree
    # about the instrument list.
    db_url = settings.database_url.get_secret_value()

    def _db_config() -> Mapping[str, Any] | None:
        from desk.services.portfolio import load_config_payload

        return load_config_payload(db_url)

    try:
        cfg = load(config, db_fallback=_db_config)
    except ConfigError as exc:
        console.print(f"[red]{exc}")
        raise typer.Exit(1) from exc
    tz = cfg.locale.timezone
    now = dt.datetime.now(tz=ZoneInfo(tz))

    if slot == "auto":
        resolved = resolve_slot(schedule_cron, now, tz)
        if resolved is None:
            console.print(
                f"[dim]skipped: cron {schedule_cron!r} is not the offset-correct run "
                f"for {tz} today."
            )
            return
    elif slot in ("open", "close"):
        resolved = slot
    else:
        console.print(f"[red]--slot must be open, close or auto (got {slot!r})")
        raise typer.Exit(1)

    outcome = build_snapshot(cfg, db_url, slot=resolved, now=now)
    if outcome is None:
        console.print("[yellow]no positions to value; nothing recorded.")
        return

    ccy = cfg.locale.base_currency
    console.print(f"recorded {outcome.date} ({outcome.slot})")
    console.print(f"  market value   {outcome.market_value:>14,.2f} {ccy}")
    console.print(f"  book value     {outcome.book_value:>14,.2f} {ccy}")
    if outcome.daily_pnl is not None:
        pct = f" ({outcome.daily_pnl_pct:+.2%})" if outcome.daily_pnl_pct is not None else ""
        console.print(f"  daily move     {outcome.daily_pnl:>+14,.2f} {ccy}{pct}")
    else:
        console.print("  daily move     [dim]no prior close available[/dim]")
    if outcome.benchmark_pct is not None:
        console.print(f"  benchmark      {outcome.benchmark_pct:>+14.2%}  {cfg.benchmarks.daily}")
    console.print(f"  price coverage {outcome.coverage:>14.1%}")
    if outcome.unpriced:
        console.print(f"[yellow]  unpriced: {', '.join(outcome.unpriced)}")
    # A partial valuation is recorded, but it must not pass for a clean one.
    if outcome.coverage < 0.999:
        console.print(
            f"[yellow]\nonly {outcome.coverage:.0%} of book value carried a live price. "
            "The snapshot is stored with its coverage so the gap stays visible."
        )


@app.command()
def status(
    db: str = typer.Option(None, "--db", help="database URL; defaults to DESK_DATABASE_URL"),
) -> None:
    """Report what is actually in the database.

    Exists because every other command is quiet on success, which makes "did that
    work?" a question you cannot answer without opening a SQL client. This reads
    only, changes nothing, and prints the counts that determine what the dashboard
    can show.
    """
    from sqlalchemy import func, select

    from desk.store.engine import build_engine, create_all, session_factory, session_scope
    from desk.store.models import (
        AppConfig,
        Cash,
        ContributionRow,
        Instrument,
        Snapshot,
        Transaction,
    )

    url = db
    if not url:
        from desk.settings import SettingsError, get_settings

        try:
            settings = get_settings()
        except SettingsError as exc:
            console.print(f"[red]{exc}")
            raise typer.Exit(1) from exc
        if settings.database_url is None:
            console.print("[red]no --db given and DESK_DATABASE_URL is not set.")
            raise typer.Exit(1)
        url = settings.database_url.get_secret_value()

    engine = build_engine(url)
    create_all(engine)
    factory = session_factory(engine)
    table = Table(title="desk status", show_header=True, header_style="bold")
    table.add_column("what")
    table.add_column("state", overflow="fold")

    with session_scope(factory) as s:

        def count(model: type[Any]) -> int:
            return int(s.execute(select(func.count()).select_from(model)).scalar() or 0)

        config_row = s.get(AppConfig, 1)
        table.add_row(
            "config row",
            "present — the dashboard reads its accounts and instruments from here"
            if config_row
            else "[yellow]missing — run `desk push-config`",
        )
        table.add_row("instruments", f"{count(Instrument)} rows")

        positions = list(s.execute(select(Transaction)).scalars())
        if positions:
            tickers = len({t.ticker for t in positions})
            accounts = len({t.account_id for t in positions})
            table.add_row(
                "ledger", f"{len(positions)} rows over {tickers} tickers, {accounts} account(s)"
            )
        else:
            table.add_row("ledger", "[yellow]empty — run `desk backfill`")

        table.add_row("cash rows", f"{count(Cash)}")
        table.add_row("contributions", f"{count(ContributionRow)}")

        snaps = list(s.execute(select(Snapshot).order_by(Snapshot.date)).scalars())
        recorded = [x for x in snaps if x.slot != "recon"]
        recon = [x for x in snaps if x.slot == "recon"]
        if recorded:
            table.add_row(
                "recorded snapshots",
                f"{len(recorded)} — {recorded[0].date} to {recorded[-1].date}. "
                "The performance chart draws a line from two or more.",
            )
        else:
            table.add_row(
                "recorded snapshots",
                "[yellow]none — press Fetch prices, or let the daily job run",
            )
        if recon:
            table.add_row(
                "reconstructed", f"{len(recon)} — {recon[0].date} to {recon[-1].date} (dashed line)"
            )
        else:
            table.add_row("reconstructed", "[dim]none — run `desk backfill-snapshots`")

    console.print(table)
    console.print("[dim]Read-only. Nothing was changed.")


@app.command("backfill-snapshots")
def backfill_snapshots(
    db: str = typer.Option(None, "--db", help="database URL; defaults to DESK_DATABASE_URL"),
    config: str = typer.Option(None, "--config", "-c", help="path to portfolio.yaml"),
    period: str = typer.Option("5y", "--period", help="how far back to reconstruct"),
    freq: str = typer.Option("W-FRI", "--freq", help="sampling frequency, e.g. W-FRI or B"),
) -> None:
    """Fill the performance chart from price history.

    A new deployment has no snapshots, so the chart needs two trading days to draw
    a line and months before it says anything. This values the units held today at
    each past date and stores the result.

    These are reconstructed points, not observations: a lot bought last month is
    projected backwards as though it had always been held. They are written under
    their own slot so nothing confuses them with recorded snapshots, and the chart
    draws them as a separate, dashed series.
    """
    from desk.services.market import reconstruct_history

    # `--db` like every other local command here. Only `fetch-prices` insists on the
    # environment, because that one runs in CI where a connection string in argv
    # lands in a log; a command typed at a prompt is better served by not needing a
    # three-variable prefix that truncates when the line wraps.
    db_url = db
    if not db_url:
        from desk.settings import SettingsError, get_settings

        try:
            settings = get_settings()
        except SettingsError as exc:
            console.print(f"[red]{exc}")
            raise typer.Exit(1) from exc
        if settings.database_url is None:
            console.print("[red]no --db given and DESK_DATABASE_URL is not set.")
            raise typer.Exit(1)
        db_url = settings.database_url.get_secret_value()

    def _db_config() -> Mapping[str, Any] | None:
        from desk.services.portfolio import load_config_payload

        return load_config_payload(db_url)

    try:
        cfg = load(config, db_fallback=_db_config)
    except ConfigError as exc:
        console.print(f"[red]{exc}")
        raise typer.Exit(1) from exc

    with console.status("fetching price history…"):
        outcome = reconstruct_history(cfg, db_url, period=period, freq=freq)
    if outcome is None:
        console.print("[yellow]nothing to reconstruct — no positions, or no price history.")
        return
    rows, first, last = outcome
    console.print(f"[green]wrote {rows} reconstructed points[/green] from {first} to {last}.")
    console.print(
        "[dim]These value today's units at past prices. They are a backtest of the "
        "current book, not a record of what was held, and the chart labels them so."
    )


@app.command("build-lookthrough")
def build_lookthrough(
    manifest: str = typer.Option(
        "inbox/lookthrough.yaml", "--manifest", "-m", help="manifest describing each fund"
    ),
    source_dir: str = typer.Option(
        "inbox", "--source-dir", "-s", help="directory holding the downloaded files"
    ),
    out: str = typer.Option(
        "data/lookthrough/composition.json.gz", "--out", "-o", help="where to write the dataset"
    ),
    as_of: str = typer.Option(
        None, "--as-of", help="composition date (YYYY-MM-DD); defaults to today"
    ),
) -> None:
    """Normalise downloaded fund-holdings files into the look-through dataset.

    Source files stay in `inbox/`, which is gitignored — a holdings file for a
    fund you own is itself a disclosure of what you own. Only the normalised
    output is committed, and it contains fund compositions only: no position, no
    balance, nothing about who holds it.

    Run `desk build-lookthrough --template` output into a manifest first if you
    have not written one.
    """
    from pathlib import Path

    from desk.intake.lookthrough import IntakeError, build, describe, read, specs_from_yaml, write

    manifest_path = Path(manifest)
    if not manifest_path.exists():
        console.print(f"[red]no manifest at {manifest_path}.")
        console.print(
            "\nWrite one describing each fund and the file its holdings came from. "
            "See [bold]docs/lookthrough.md[/bold] for the format and where to download each file."
        )
        raise typer.Exit(1)

    when = dt.date.fromisoformat(as_of) if as_of else dt.date.today()
    try:
        specs = specs_from_yaml(manifest_path.read_text(encoding="utf-8"))
        compositions = build(specs, Path(source_dir), as_of=when)
    except IntakeError as exc:
        console.print(f"[red]{exc}")
        raise typer.Exit(1) from exc

    out_path = Path(out)
    write(compositions, out_path)
    for line in describe(compositions):
        console.print(f"  {line}")

    resolved = [c for c in compositions if c.resolves_to_securities]
    console.print(
        f"\n[green]wrote {out_path}[/green] — {len(resolved)} of {len(compositions)} "
        f"funds resolved to securities, as of {when}."
    )
    if len(resolved) < len(compositions):
        unresolved = ", ".join(c.ticker for c in compositions if not c.resolves_to_securities)
        console.print(
            f"[dim]{unresolved} hold no securities to look through; the X-Ray tab reports "
            "them by name rather than omitting them."
        )
    # Prove the file round-trips before anyone relies on it in the dashboard.
    if len(read(out_path)) != len(compositions):
        console.print("[red]the written file did not read back correctly.")
        raise typer.Exit(1)


@app.command("push-config")
def push_config(
    db: str = typer.Option(..., "--db", help="database URL to write the config into"),
    config: str = typer.Option(None, "--config", "-c", help="path to portfolio.yaml"),
) -> None:
    """Store the portfolio config as a database row.

    A read-only host (Streamlit Cloud) has no committed config file, so the
    deployment keeps its config here. The file is validated before it is stored,
    so an invalid config fails on your machine rather than on the host.
    """
    from pathlib import Path

    from desk.services.portfolio import save_config_payload

    path = resolve_path(config)
    if path is None:
        console.print("[red]no config file found (looked for config/portfolio.yaml)")
        raise typer.Exit(1)
    try:
        load(str(path))  # validate before storing
    except ConfigError as exc:
        console.print(f"[red]{exc}")
        raise typer.Exit(1) from exc
    save_config_payload(db, Path(path).read_text(encoding="utf-8"))
    console.print(f"[green]stored config from {path} into the database.")


@app.command()
def serve() -> None:
    """Run the dashboard."""
    try:
        from streamlit.web import cli as stcli
    except ImportError as exc:
        console.print("[red]Streamlit is not installed. Try: pip install -e '.[app]'")
        raise typer.Exit(1) from exc

    from pathlib import Path

    entry = Path(__file__).resolve().parent.parent / "app" / "main.py"
    sys.argv = ["streamlit", "run", str(entry)]
    stcli.main()


if __name__ == "__main__":
    app()
