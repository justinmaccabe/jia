"""Which statistics are annualised, which are not, and why.

The Risk tab shows twenty-four figures in one table. Twelve are annualised, three
are single-period, one is a point-in-time extreme and eight are dimensionless. Under
bare labels a monthly 5% VaR reads as an annual one — an understatement of roughly
three and a half times, in the direction that makes a portfolio look safer than it
is. These tests pin both the arithmetic and the labelling.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from desk.analytics.risk import (
    ANNUALISED,
    METRIC_BASIS,
    PER_PERIOD,
    POINT_IN_TIME,
    annual_risk_free,
    risk_stats,
)

ANNUAL_DRIFT = 0.10
ANNUAL_VOL = 0.15


def values(months: int = 120, seed: int = 3) -> pd.Series:
    """A value series with known annual drift and volatility."""
    rng = np.random.default_rng(seed)
    index = pd.date_range("2016-01-31", periods=months, freq="ME")
    returns = rng.normal(ANNUAL_DRIFT / 12, ANNUAL_VOL / np.sqrt(12), months)
    return pd.Series(100_000 * np.cumprod(1 + returns), index=index)


class TestAnnualisation:
    def test_volatility_is_scaled_by_root_twelve(self) -> None:
        stats = risk_stats(values())
        assert stats.volatility is not None
        # Ten years of monthly data recovers the annual figure to within noise.
        assert stats.volatility == pytest.approx(ANNUAL_VOL, abs=0.03)

    def test_geometric_mean_is_compounded_not_multiplied(self) -> None:
        """Multiplying a monthly mean by twelve overstates a compounded return."""
        stats = risk_stats(values())
        assert stats.geometric_mean is not None
        assert stats.geometric_mean == pytest.approx(ANNUAL_DRIFT, abs=0.04)

    def test_sharpe_uses_the_annualised_pair(self) -> None:
        stats = risk_stats(values(), risk_free_rate=0.0)
        assert stats.sharpe is not None and stats.volatility is not None
        assert stats.sharpe == pytest.approx(
            (stats.arithmetic_mean or 0.0) / stats.volatility, rel=1e-9
        )

    def test_tracking_error_is_annualised(self) -> None:
        series = values()
        stats = risk_stats(series, series * 0.99)
        assert stats.tracking_error is not None
        assert stats.tracking_error >= 0.0


class TestRiskFreeRate:
    def test_a_positive_rate_lowers_the_sharpe_family(self) -> None:
        """Zero is not a neutral default; it inflates every excess-return ratio."""
        series = values()
        free = risk_stats(series, risk_free_rate=0.0)
        charged = risk_stats(series, risk_free_rate=0.04)
        assert free.sharpe is not None and charged.sharpe is not None
        assert charged.sharpe < free.sharpe

    def test_volatility_is_unaffected_by_the_rate(self) -> None:
        series = values()
        assert risk_stats(series, risk_free_rate=0.0).volatility == pytest.approx(
            risk_stats(series, risk_free_rate=0.04).volatility
        )

    def test_it_is_read_from_the_factor_library(self) -> None:
        factors = pd.DataFrame(
            {"RF": [0.003] * 12},
            index=pd.date_range("2025-01-31", periods=12, freq="ME"),
        )
        assert annual_risk_free(factors) == pytest.approx(0.036)

    def test_only_recent_months_count(self) -> None:
        """The relevant rate is the one available lately, not a decade average."""
        factors = pd.DataFrame(
            {"RF": [0.0] * 24 + [0.004] * 12},
            index=pd.date_range("2023-01-31", periods=36, freq="ME"),
        )
        assert annual_risk_free(factors, months=12) == pytest.approx(0.048)

    def test_a_missing_column_yields_zero_rather_than_raising(self) -> None:
        assert annual_risk_free(pd.DataFrame({"Mkt-RF": [0.01]})) == 0.0
        assert annual_risk_free(pd.DataFrame()) == 0.0


class TestMetricBasis:
    def test_every_rendered_metric_has_a_basis(self) -> None:
        """A metric with no basis would render blank and read as unlabelled."""
        rendered = {
            "Arithmetic mean",
            "Geometric mean",
            "Volatility",
            "Downside deviation",
            "Maximum drawdown",
            "Sharpe ratio",
            "Sortino ratio",
            "Calmar ratio",
            "Beta",
            "Alpha",
            "R squared",
            "Treynor ratio",
            "Tracking error",
            "Information ratio",
            "Active return",
            "Skewness",
            "Excess kurtosis",
            "Historical VaR (5%)",
            "Analytical VaR (5%)",
            "Conditional VaR (5%)",
            "Up capture",
            "Down capture",
            "Positive periods",
            "Gain/loss ratio",
        }
        assert rendered <= set(METRIC_BASIS)

    def test_value_at_risk_is_not_claimed_to_be_annual(self) -> None:
        """Scaling a tail quantile by root-twelve assumes the normality that a tail
        statistic exists to question, so it is left at its measured horizon."""
        for label in ("Historical VaR (5%)", "Analytical VaR (5%)", "Conditional VaR (5%)"):
            assert METRIC_BASIS[label] == PER_PERIOD

    def test_drawdown_is_point_in_time(self) -> None:
        assert METRIC_BASIS["Maximum drawdown"] == POINT_IN_TIME

    def test_the_sharpe_family_is_marked_annualised(self) -> None:
        for label in ("Sharpe ratio", "Sortino ratio", "Treynor ratio", "Calmar ratio"):
            assert METRIC_BASIS[label] == ANNUALISED
