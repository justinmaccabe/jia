"""Re-running `desk backfill` must not duplicate the ledger.

The command has always documented itself as idempotent. It was not. `Session.merge`
matches on the primary key, and both `transactions` and `contributions` use an
autoincrement id nobody supplies — so every call was an INSERT.

For transactions that surfaced as a unique-constraint violation on `source_hash`:
ugly, but loud. For contributions there is no unique constraint at all, so a second
run silently doubled every contribution, overstating room used and reporting an
over-contribution that never happened. A wrong number nobody is warned about is the
worse of the two failures, and it is the reason these tests exist.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest
import yaml
from sqlalchemy import select
from typer.testing import CliRunner

from desk.cli.main import app
from desk.store.engine import build_engine, session_factory, session_scope
from desk.store.models import ContributionRow, Transaction

runner = CliRunner()

HOLDINGS = {
    "as_of": "2026-08-14",
    "usd_cad": 1.35,
    "holdings": [
        {"account": "main", "ticker": "AAA", "units": 10, "book_native": 1000.0, "currency": "CAD"},
        {"account": "main", "ticker": "BBB", "units": 5, "book_native": 500.0, "currency": "USD"},
    ],
    "cash": [{"account": "main", "currency": "CAD", "amount": 250.0}],
    "contributions": [
        {"date": "2026-01-15", "account": "main", "amount": 7000.0},
        {"date": "2026-03-01", "account": "main", "amount": 1000.0},
    ],
}

CONFIG = {
    "version": 1,
    "accounts": [{"id": "main", "label": "Main", "type": "other"}],
    "instruments": [
        {"ticker": "AAA", "symbol": "AAA.TO", "currency": "CAD", "kind": "etf"},
        {"ticker": "BBB", "symbol": "BBB", "currency": "USD", "kind": "etf"},
    ],
}


@pytest.fixture
def paths(tmp_path: Path) -> tuple[str, str, str]:
    holdings = tmp_path / "holdings.yaml"
    holdings.write_text(yaml.safe_dump(HOLDINGS), encoding="utf-8")
    config = tmp_path / "portfolio.yaml"
    config.write_text(yaml.safe_dump(CONFIG), encoding="utf-8")
    return str(holdings), str(config), f"sqlite:///{tmp_path / 'test.db'}"


def run(holdings: str, config: str, db: str, *extra: str) -> object:
    return runner.invoke(app, ["backfill", holdings, "--db", db, "-c", config, *extra])


def counts(db: str) -> tuple[int, int, float]:
    factory = session_factory(build_engine(db))
    with session_scope(factory) as s:
        tx = list(s.execute(select(Transaction)).scalars())
        contribs = list(s.execute(select(ContributionRow)).scalars())
        return len(tx), len(contribs), sum(c.amount for c in contribs)


class TestRerunIsANoOp:
    def test_transactions_are_not_duplicated(self, paths: tuple[str, str, str]) -> None:
        holdings, config, db = paths
        assert run(holdings, config, db).exit_code == 0
        assert counts(db)[0] == 2
        # The run that used to raise UniqueViolation.
        second = run(holdings, config, db)
        assert second.exit_code == 0
        assert counts(db)[0] == 2

    def test_contributions_are_not_duplicated(self, paths: tuple[str, str, str]) -> None:
        """The silent one. Doubling these overstates room used."""
        holdings, config, db = paths
        run(holdings, config, db)
        assert counts(db)[1:] == (2, 8000.0)
        run(holdings, config, db)
        assert counts(db)[1:] == (2, 8000.0)

    def test_a_third_run_is_also_clean(self, paths: tuple[str, str, str]) -> None:
        holdings, config, db = paths
        for _ in range(3):
            run(holdings, config, db)
        assert counts(db) == (2, 2, 8000.0)

    def test_it_reports_what_it_skipped(self, paths: tuple[str, str, str]) -> None:
        holdings, config, db = paths
        run(holdings, config, db)
        assert "already present and unchanged" in run(holdings, config, db).stdout


class TestChangedNumbersAreRefused:
    def _changed(self, tmp: Path) -> str:
        spec = {**HOLDINGS}
        spec["holdings"] = [
            {**HOLDINGS["holdings"][0], "units": 12, "book_native": 1200.0},  # type: ignore[index]
            HOLDINGS["holdings"][1],  # type: ignore[index]
        ]
        path = tmp / "changed.yaml"
        path.write_text(yaml.safe_dump(spec), encoding="utf-8")
        return str(path)

    def test_a_changed_lot_refuses_rather_than_doubling(
        self, paths: tuple[str, str, str], tmp_path: Path
    ) -> None:
        """Adding a second lot would inflate the position — and in the flattering
        direction, which is exactly the sort of error that goes unquestioned."""
        holdings, config, db = paths
        run(holdings, config, db)
        result = run(self._changed(tmp_path), config, db)

        assert result.exit_code == 1
        assert "--reset" in result.stdout
        # Nothing was written.
        assert counts(db)[0] == 2
        factory = session_factory(build_engine(db))
        with session_scope(factory) as s:
            rows = list(s.execute(select(Transaction).where(Transaction.ticker == "AAA")).scalars())
            assert len(rows) == 1
            assert rows[0].quantity == 10

    def test_reset_replaces_the_opening_ledger(
        self, paths: tuple[str, str, str], tmp_path: Path
    ) -> None:
        holdings, config, db = paths
        run(holdings, config, db)
        assert run(self._changed(tmp_path), config, db, "--reset").exit_code == 0
        factory = session_factory(build_engine(db))
        with session_scope(factory) as s:
            rows = list(s.execute(select(Transaction).where(Transaction.ticker == "AAA")).scalars())
            assert len(rows) == 1
            assert rows[0].quantity == 12
        assert counts(db)[0] == 2


class TestTradeDateFx:
    def test_the_usd_rate_is_frozen_onto_the_lot(self, paths: tuple[str, str, str]) -> None:
        """Cost basis must not drift with the exchange rate, so the rate travels
        onto the row rather than being looked up later."""
        holdings, config, db = paths
        run(holdings, config, db)
        factory = session_factory(build_engine(db))
        with session_scope(factory) as s:
            usd = s.execute(select(Transaction).where(Transaction.ticker == "BBB")).scalar_one()
            cad = s.execute(select(Transaction).where(Transaction.ticker == "AAA")).scalar_one()
        assert usd.fx_rate == pytest.approx(1.35)
        assert cad.fx_rate == pytest.approx(1.0)
        assert usd.date == dt.date(2026, 8, 14)
