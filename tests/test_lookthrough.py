"""Look-through resolution, overlap arithmetic, and the honesty properties.

The tests that matter most are the ones asserting what happens to holdings that
*cannot* be resolved. A look-through that silently drops a swap-based fund from
its denominators reports a 100%-covered picture of 70% of a portfolio, and every
percentage on the page is then wrong in a way no downstream check would catch.
"""

from __future__ import annotations

import datetime as dt

import pytest

from desk.analytics.lookthrough import (
    COMMODITY,
    DIRECT,
    SYNTHETIC,
    FundComposition,
    Holding,
    companies_frame,
    look_through,
    overlap_table,
    region_of,
)


def equity(ticker: str, weight: float, *, sector: str = "Financials", country: str = "Canada"):
    return Holding(
        ticker=ticker, name=f"{ticker} Inc", weight=weight, sector=sector, country=country
    )


def fund(ticker: str, holdings, **kwargs) -> FundComposition:
    return FundComposition(ticker=ticker, name=f"{ticker} ETF", holdings=tuple(holdings), **kwargs)


class TestCompanyResolution:
    def test_one_fund_resolves_to_its_holdings(self) -> None:
        report = look_through(
            {"AAA": 1000.0},
            [fund("AAA", [equity("RY", 0.6), equity("TD", 0.4)])],
        )
        assert [c.ticker for c in report.companies] == ["RY", "TD"]
        assert report.companies[0].total == pytest.approx(600.0)
        assert report.companies[0].weight == pytest.approx(0.6)
        assert report.coverage == pytest.approx(1.0)

    def test_a_company_in_two_funds_is_summed_once(self) -> None:
        """The whole reason this view exists."""
        report = look_through(
            {"AAA": 1000.0, "BBB": 500.0},
            [
                fund("AAA", [equity("RY", 0.5), equity("TD", 0.5)]),
                fund("BBB", [equity("RY", 1.0)]),
            ],
        )
        royal = next(c for c in report.companies if c.ticker == "RY")
        assert royal.total == pytest.approx(1000.0)  # 500 + 500
        assert royal.funds == 2
        assert royal.by_fund == {"AAA": pytest.approx(500.0), "BBB": pytest.approx(500.0)}
        # Two funds, three lines, but only two distinct companies.
        assert report.stats.distinct == 2

    def test_overlap_statistic_counts_three_or_more(self) -> None:
        holdings = [equity("SHARED", 1.0)]
        report = look_through(
            {"A": 100.0, "B": 100.0, "C": 100.0, "D": 100.0},
            [
                fund("A", holdings),
                fund("B", holdings),
                fund("C", holdings),
                fund("D", [equity("SOLO", 1.0)]),
            ],
        )
        shared = next(c for c in report.companies if c.ticker == "SHARED")
        assert shared.funds == 3
        assert report.stats.overlap_3plus == pytest.approx(0.75)
        assert report.stats.max_overlap == 3

    def test_overlap_table_lists_only_repeats(self) -> None:
        report = look_through(
            {"A": 100.0, "B": 100.0},
            [
                fund("A", [equity("SHARED", 0.5), equity("SOLO", 0.5)]),
                fund("B", [equity("SHARED", 1.0)]),
            ],
        )
        table = overlap_table(report, minimum=2)
        assert list(table["Ticker"]) == ["SHARED"]
        assert table.iloc[0]["Delivered by"] == "A, B"

    def test_concentration_statistics(self) -> None:
        report = look_through(
            {"A": 1000.0},
            [fund("A", [equity(f"C{i}", 0.01) for i in range(100)])],
        )
        assert report.stats.distinct == 100
        assert report.stats.top10 == pytest.approx(0.10)
        assert report.stats.top25 == pytest.approx(0.25)


class TestUnresolvableHoldings:
    """A fund that cannot be looked through must be named, not dropped."""

    def test_swap_based_fund_is_excluded_from_companies_but_keeps_its_region(self) -> None:
        report = look_through(
            {"REAL": 700.0, "SWAP": 300.0},
            [
                fund("REAL", [equity("RY", 1.0)]),
                FundComposition(
                    ticker="SWAP",
                    name="Swap ETF",
                    resolution=SYNTHETIC,
                    tracks="the S&P/TSX 60",
                    region_mix={"Canada": 1.0},
                ),
            ],
        )
        assert [c.ticker for c in report.companies] == ["RY"]
        assert "SWAP" in report.unresolved
        assert report.unresolved["SWAP"][0] == pytest.approx(300.0)
        assert "swap-based" in report.unresolved["SWAP"][1]
        # Coverage is the honest 70%, not 100%.
        assert report.coverage == pytest.approx(0.7)
        # Its economic region exposure still counts.
        assert report.region["Canada"] == pytest.approx(1.0)
        assert report.asset["Synthetic index exposure"] == pytest.approx(0.3)

    def test_commodity_fund_has_no_sector_or_region(self) -> None:
        report = look_through(
            {"REAL": 900.0, "COIN": 100.0},
            [
                fund("REAL", [equity("RY", 1.0)]),
                FundComposition(ticker="COIN", name="Bitcoin ETF", resolution=COMMODITY),
            ],
        )
        assert "COIN" in report.unresolved
        assert report.asset["Digital assets"] == pytest.approx(0.1)
        assert report.coverage == pytest.approx(0.9)
        # It contributes to no region bucket at all.
        assert sum(report.region.values()) == pytest.approx(0.9)

    def test_fund_with_no_composition_supplied_is_reported(self) -> None:
        report = look_through(
            {"REAL": 500.0, "MYSTERY": 500.0},
            [fund("REAL", [equity("RY", 1.0)])],
        )
        assert report.unresolved["MYSTERY"][0] == pytest.approx(500.0)
        assert "no composition" in report.unresolved["MYSTERY"][1]
        assert report.coverage == pytest.approx(0.5)

    def test_company_weights_are_of_the_whole_portfolio_not_the_resolved_part(self) -> None:
        """A 60% position inside a fund that is half the book is 30% of the book.

        Reporting it as 60% would be the natural bug: it is the weight the
        provider's file states.
        """
        report = look_through(
            {"AAA": 500.0, "SWAP": 500.0},
            [
                fund("AAA", [equity("RY", 0.6), equity("TD", 0.4)]),
                FundComposition(ticker="SWAP", resolution=SYNTHETIC, region_mix={"Canada": 1.0}),
            ],
        )
        royal = next(c for c in report.companies if c.ticker == "RY")
        assert royal.weight == pytest.approx(0.30)

    def test_everything_unresolvable_yields_an_empty_but_valid_report(self) -> None:
        report = look_through(
            {"SWAP": 100.0},
            [FundComposition(ticker="SWAP", resolution=SYNTHETIC, region_mix={"Canada": 1.0})],
        )
        assert report.is_empty
        assert report.coverage == pytest.approx(0.0)
        assert companies_frame(report, ["SWAP"]).empty


class TestRollups:
    def test_fund_cash_residual_is_derived(self) -> None:
        """Published weights summing to 98% mean 2% fund-level cash."""
        report = look_through({"AAA": 1000.0}, [fund("AAA", [equity("RY", 0.98)])])
        assert report.asset["Cash"] == pytest.approx(0.02)
        assert report.asset["Public equity"] == pytest.approx(0.98)

    def test_asset_mix_sums_to_one(self) -> None:
        report = look_through(
            {"AAA": 600.0, "SWAP": 300.0, "COIN": 100.0},
            [
                fund("AAA", [equity("RY", 0.95)]),
                FundComposition(ticker="SWAP", resolution=SYNTHETIC, region_mix={"Canada": 1.0}),
                FundComposition(ticker="COIN", resolution=COMMODITY),
            ],
        )
        assert sum(report.asset.values()) == pytest.approx(1.0)

    def test_sector_is_a_share_of_the_equity_sleeve_not_the_book(self) -> None:
        """Sector percentages must sum to 1 over what publishes sectors."""
        report = look_through(
            {"AAA": 500.0, "SWAP": 500.0},
            [
                fund(
                    "AAA",
                    [
                        equity("RY", 0.5, sector="Financials"),
                        equity("SHOP", 0.5, sector="Information Technology"),
                    ],
                ),
                FundComposition(ticker="SWAP", resolution=SYNTHETIC, region_mix={"Canada": 1.0}),
            ],
        )
        assert sum(report.sector.values()) == pytest.approx(1.0)
        assert report.sector["Financials"] == pytest.approx(0.5)
        assert report.sector_base == pytest.approx(500.0)

    def test_holdings_without_a_sector_are_left_out_of_the_sector_base(self) -> None:
        report = look_through(
            {"AAA": 1000.0},
            [
                fund(
                    "AAA",
                    [
                        equity("RY", 0.5, sector="Financials"),
                        equity("XXX", 0.5, sector=""),
                    ],
                )
            ],
        )
        assert report.sector["Financials"] == pytest.approx(1.0)
        assert report.sector_base == pytest.approx(500.0)

    def test_regions_are_bucketed(self) -> None:
        report = look_through(
            {"AAA": 1000.0},
            [
                fund(
                    "AAA",
                    [
                        equity("A", 0.4, country="United States"),
                        equity("B", 0.3, country="Canada"),
                        equity("C", 0.2, country="Japan"),
                        equity("D", 0.1, country="Brazil"),
                    ],
                )
            ],
        )
        assert report.region["United States"] == pytest.approx(0.4)
        assert report.region["Canada"] == pytest.approx(0.3)
        assert report.region["Developed ex-North America"] == pytest.approx(0.2)
        assert report.region["Emerging markets"] == pytest.approx(0.1)

    def test_as_of_is_the_oldest_not_the_newest(self) -> None:
        """A blend is only as current as its stalest input."""
        report = look_through(
            {"A": 100.0, "B": 100.0},
            [
                fund("A", [equity("RY", 1.0)], as_of=dt.date(2026, 1, 15)),
                fund("B", [equity("TD", 1.0)], as_of=dt.date(2026, 8, 1)),
            ],
        )
        assert report.as_of == dt.date(2026, 1, 15)


class TestRegionOf:
    @pytest.mark.parametrize(
        ("country", "expected"),
        [
            ("United States", "United States"),
            ("Canada", "Canada"),
            ("Japan", "Developed ex-North America"),
            ("United Kingdom", "Developed ex-North America"),
            ("Brazil", "Emerging markets"),
            ("India", "Emerging markets"),
            ("", "Unclassified"),
        ],
    )
    def test_buckets(self, country: str, expected: str) -> None:
        assert region_of(country) == expected


class TestEmptyInputs:
    def test_no_positions(self) -> None:
        report = look_through({}, [])
        assert report.is_empty
        assert report.total == 0.0
        assert report.coverage == 0.0

    def test_zero_and_negative_values_are_ignored(self) -> None:
        report = look_through(
            {"AAA": 100.0, "ZERO": 0.0, "SHORT": -50.0},
            [fund("AAA", [equity("RY", 1.0)])],
        )
        assert report.total == pytest.approx(100.0)
        assert "ZERO" not in report.unresolved


class TestPartialCompositions:
    """A fact sheet publishes a fund's largest holdings, not all of them.

    The distinction between "the fund holds 2% cash" and "we can see 10 of its 506
    holdings" is invisible in the weights alone, and getting it wrong reports a
    diversified equity fund as two-thirds uninvested. That is not a cosmetic error:
    the asset mix, the coverage figure and every concentration statistic are read
    against it.
    """

    def test_uncovered_weight_is_unresolved_not_cash(self) -> None:
        report = look_through(
            {"AAA": 1000.0},
            [fund("AAA", [equity("RY", 0.10), equity("TD", 0.08)], total_holdings=500)],
        )
        assert report.asset["Unresolved inside funds"] == pytest.approx(0.82)
        assert "Cash" not in report.asset

    def test_a_complete_list_still_yields_cash(self) -> None:
        """The complete case must not regress: there `total_holdings` is None."""
        report = look_through({"AAA": 1000.0}, [fund("AAA", [equity("RY", 0.98)])])
        assert report.asset["Cash"] == pytest.approx(0.02)
        assert "Unresolved inside funds" not in report.asset

    def test_coverage_counts_only_the_published_portion(self) -> None:
        report = look_through(
            {"AAA": 1000.0},
            [fund("AAA", [equity("RY", 0.30)], total_holdings=500)],
        )
        assert report.coverage == pytest.approx(0.30)

    def test_partial_funds_are_reported_with_their_counts(self) -> None:
        report = look_through(
            {"AAA": 1000.0},
            [fund("AAA", [equity("RY", 0.30)], total_holdings=506)],
        )
        value, shown, held = report.partial["AAA"]
        assert (value, shown, held) == (pytest.approx(1000.0), 1, 506)
        assert report.stats_are_lower_bounds

    def test_a_complete_book_is_not_flagged_as_a_lower_bound(self) -> None:
        report = look_through({"AAA": 1000.0}, [fund("AAA", [equity("RY", 1.0)])])
        assert not report.stats_are_lower_bounds
        assert not report.partial

    def test_a_list_covering_the_whole_fund_is_not_partial(self) -> None:
        """total_holdings equal to the rows published means the list is complete."""
        report = look_through(
            {"AAA": 1000.0},
            [fund("AAA", [equity("RY", 0.6), equity("TD", 0.4)], total_holdings=2)],
        )
        assert not report.partial
        assert report.coverage == pytest.approx(1.0)

    def test_asset_mix_still_sums_to_one_with_partial_funds(self) -> None:
        report = look_through(
            {"AAA": 600.0, "BBB": 400.0},
            [
                fund("AAA", [equity("RY", 0.30)], total_holdings=500),
                fund("BBB", [equity("TD", 0.95)]),
            ],
        )
        assert sum(report.asset.values()) == pytest.approx(1.0)


class TestDirectHoldings:
    """A directly-held share is a company and resolves to itself.

    This is what makes the most easily missed overlap visible: a name owned
    outright that also sits inside a fund appears once, with both contributions
    summed. No brokerage statement puts those two numbers together.
    """

    def test_a_direct_holding_becomes_its_own_company(self) -> None:
        direct = FundComposition(
            ticker="NVDA",
            name="NVIDIA Corp",
            resolution=DIRECT,
            holdings=(
                Holding(
                    ticker="NVDA",
                    name="NVIDIA Corp",
                    weight=1.0,
                    sector="Information Technology",
                    country="United States",
                ),
            ),
        )
        report = look_through({"NVDA": 1000.0}, [direct])
        assert [c.ticker for c in report.companies] == ["NVDA"]
        assert report.coverage == pytest.approx(1.0)
        assert report.asset["Public equity"] == pytest.approx(1.0)

    def test_direct_and_fund_exposure_to_one_name_are_summed(self) -> None:
        direct = FundComposition(
            ticker="NVDA",
            resolution=DIRECT,
            holdings=(
                Holding(
                    ticker="NVDA",
                    name="NVIDIA Corp",
                    weight=1.0,
                    sector="Information Technology",
                    country="United States",
                ),
            ),
        )
        etf = fund(
            "SPMO",
            [equity("NVDA", 0.075, sector="Information Technology", country="United States")],
            total_holdings=99,
        )
        report = look_through({"NVDA": 1000.0, "SPMO": 2000.0}, [direct, etf])
        nvda = next(c for c in report.companies if c.ticker == "NVDA")
        assert nvda.total == pytest.approx(1150.0)  # 1000 direct + 150 via the fund
        assert nvda.funds == 2
        assert set(nvda.by_fund) == {"NVDA", "SPMO"}
