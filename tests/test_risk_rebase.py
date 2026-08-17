"""Rebasing a comparator onto the portfolio's own axis.

A benchmark's price history and a portfolio's value are in different units, so the
two cannot share an axis as they stand. The options are to index both to 100 —
which throws away the amounts people actually want to see — or to give the
benchmark a second y-axis, which invites comparing two differently-scaled lines by
eye and is how dual-axis charts mislead.

Rebasing keeps one axis in real currency and answers the question directly: what
would the same money have become. It must preserve every return in the series and
move only the starting level.
"""

from __future__ import annotations

import pandas as pd
import pytest

from desk.analytics.risk import rebase


def series(values: list[float]) -> pd.Series:
    return pd.Series(values, index=pd.date_range("2025-01-31", periods=len(values), freq="ME"))


class TestRebase:
    def test_the_first_value_becomes_the_target(self) -> None:
        out = rebase(series([50.0, 55.0, 60.0]), 1000.0)
        assert float(out.iloc[0]) == pytest.approx(1000.0)

    def test_returns_are_preserved_exactly(self) -> None:
        """The whole point: only the level moves."""
        original = series([50.0, 55.0, 45.0, 60.0])
        out = rebase(original, 1000.0)
        assert list(out.pct_change().dropna()) == pytest.approx(
            list(original.pct_change().dropna())
        )

    def test_a_scaled_series_ends_at_the_same_total_return(self) -> None:
        out = rebase(series([50.0, 60.0]), 1000.0)
        assert float(out.iloc[-1]) == pytest.approx(1200.0)

    def test_leading_gaps_do_not_set_the_base(self) -> None:
        """A NaN first observation would otherwise scale the whole series by nan."""
        raw = series([float("nan"), 50.0, 60.0])
        out = rebase(raw, 1000.0)
        assert float(out.iloc[0]) == pytest.approx(1000.0)
        assert float(out.iloc[-1]) == pytest.approx(1200.0)

    def test_empty_input_is_empty_output(self) -> None:
        assert rebase(pd.Series(dtype=float), 1000.0).empty

    def test_a_non_positive_target_yields_nothing(self) -> None:
        assert rebase(series([50.0, 60.0]), 0.0).empty

    def test_a_series_starting_at_zero_yields_nothing(self) -> None:
        """Scaling from zero is undefined, not infinite."""
        assert rebase(series([0.0, 60.0]), 1000.0).empty
