"""Declarative configuration: policy, not state and not secrets.

The partition rule the whole design rests on:

    Config = policy.  Database = state.  Environment = secrets.

Would you want it in a diffable pull request? YAML. Would you change it from
your phone at 9pm? Database. Does leaking it cost money? Environment.

`extra="forbid"` throughout, so a typo surfaces when `desk doctor` runs rather
than as a feature that silently does nothing.
"""

from __future__ import annotations

import datetime as dt
import re
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

HEX_COLOUR = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")
Weight = Annotated[float, Field(ge=0.0, le=1.0)]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Identity(Strict):
    display_name: str = "Portfolio"
    initials: str = Field(default="", max_length=4)
    subtitle: str = ""


class Branding(Strict):
    """Deliberately not the reference's gold-on-black. Slate and copper so the
    two products do not read as the same thing at a glance."""

    primary: str = "#35424D"
    accent: str = "#B06E4F"
    positive: str = "#5E8B7E"
    negative: str = "#A85C4C"
    categorical: tuple[str, ...] = ("#35424D", "#B06E4F", "#5E8B7E", "#8A7E6D", "#6B7A8F")
    serif: str = "Georgia, 'Times New Roman', serif"

    # Chart text and the page behind it. These were hardcoded in the app for one
    # deployment's palette, so a second deployment rendered its axis labels and
    # legends in the first one's warm grey — legible, but visibly the wrong hue,
    # and not something a config-driven theme should be unable to change.
    ink: str = "#DED8CE"
    # A benchmark is deliberately not one of the categorical series colours. A
    # portfolio and the thing it is measured against should not look like two
    # members of the same set, and a palette built from one hue cannot separate
    # three lines on a dark ground by brightness alone.
    benchmark: str = "#7E97A6"

    @model_validator(mode="after")
    def _colours_are_hex(self) -> Branding:
        named = {
            "primary": self.primary,
            "accent": self.accent,
            "positive": self.positive,
            "negative": self.negative,
        }
        for name, value in named.items():
            if not HEX_COLOUR.match(value):
                raise ValueError(f"branding.{name} must be a hex colour, got {value!r}")
        for i, value in enumerate(self.categorical):
            if not HEX_COLOUR.match(value):
                raise ValueError(f"branding.categorical[{i}] must be a hex colour, got {value!r}")
        return self


class Locale(Strict):
    base_currency: str = "CAD"
    timezone: str = "America/Toronto"
    market_calendar: str = "XTSE"

    @model_validator(mode="after")
    def _currency_is_iso(self) -> Locale:
        if len(self.base_currency) != 3 or not self.base_currency.isupper():
            raise ValueError("locale.base_currency must be an ISO-4217 code such as CAD")
        return self


class JurisdictionParams(Strict):
    birth_year: int | None = Field(default=None, ge=1900, le=2100)
    fhsa_open_year: int | None = Field(default=None, ge=1900, le=2100)
    rrsp_deduction_limit: float | None = Field(default=None, ge=0)


class Jurisdiction(Strict):
    id: Literal["ca", "generic"] = "generic"
    params: JurisdictionParams = JurisdictionParams()


class AccountKind(StrEnum):
    TFSA = "tfsa"  # pii-ok: this enum is the canonical list the scanner exempts
    FHSA = "fhsa"  # pii-ok: canonical account-type enum
    RRSP = "rrsp"  # pii-ok: canonical account-type enum
    RESP = "resp"  # pii-ok: canonical account-type enum
    TAXABLE = "taxable"
    MARGIN = "margin"
    OTHER = "other"


class Account(Strict):
    """An account is data. The reference baked the two it happened to own into
    function bodies, so a third could not be added without editing Python.

    `room_group` is the field that build could not express: two accounts of the
    same type at different custodians hold separate balances but share one
    contribution limit. Its answer was to merge them and lose per-account
    reporting.
    """

    id: str = Field(pattern=r"^[a-z0-9_]{2,32}$")
    label: str = ""
    type: AccountKind = AccountKind.OTHER
    room_group: str | None = None
    custodian: str = ""
    opened: dt.date | None = None


class InstrumentKind(StrEnum):
    ETF = "etf"
    STOCK = "stock"
    FUND = "fund"
    CASH = "cash"
    PRIVATE = "private"
    # Directly-held crypto, as distinct from a fund that holds it. It is quotable
    # (so not `private`) but it is not a fund and owns no companies, and calling
    # it an ETF would be the kind of small inaccuracy this schema exists to
    # prevent. Behaviour is unaffected — `kind` is a label, and the analytics
    # exclusion is driven by `asset_class`.
    CRYPTO = "crypto"


class Instrument(Strict):
    """Currency is declared, never inferred. The reference read it off a `.TO`
    suffix and then needed a hardcoded override for the one holding that broke
    the rule."""

    ticker: str = Field(min_length=1, max_length=16)
    symbol: str | None = None
    currency: str = "CAD"
    kind: InstrumentKind = InstrumentKind.ETF
    lookthrough: str | None = None
    asset_class: str | None = None
    region: str | None = None

    @model_validator(mode="after")
    def _quotable_unless_private(self) -> Instrument:
        if len(self.currency) != 3 or not self.currency.isupper():
            raise ValueError(f"instrument {self.ticker}: currency must be ISO-4217")
        if self.kind is not InstrumentKind.PRIVATE and not self.symbol:
            raise ValueError(
                f"instrument {self.ticker}: a quote symbol is required unless kind is 'private'"
            )
        return self


class Comparator(Strict):
    """A series to draw alongside the portfolio on the performance chart."""

    label: str
    symbol: str
    # Declared, never inferred from the symbol suffix — the same rule the
    # instrument list follows. None means it is already in the base currency, so a
    # Canadian investor comparing against CAD-listed proxies needs no FX at all.
    currency: str | None = None


class Benchmarks(Strict):
    """`daily` and `risk` are separate on purpose.

    The reference used one global for both the "market was up today" comparator
    and the beta/alpha regression benchmark, and its own README already
    disagreed with the code about which symbol that was.
    """

    daily: str | None = None
    risk: str | None = None
    comparators: tuple[Comparator, ...] = ()


class Sleeve(Strict):
    id: str = Field(pattern=r"^[a-z0-9_]{2,32}$")
    label: str
    weight: Weight
    band_pp: float = Field(default=5.0, ge=0.0, le=100.0)
    proxy: str
    vehicles: tuple[str, ...] = ()


class PolicySource(StrEnum):
    MIRROR_HOLDINGS = "mirror_holdings"
    SLEEVES = "sleeves"
    NONE = "none"


class Policy(Strict):
    """The single source for the policy benchmark series, drift bands, the
    next-dollar allocator, and the IPS policy table.

    The reference kept three divergent copies of these weights and put the
    bands only in prose, so nothing in code could enforce them.
    """

    source: PolicySource = PolicySource.MIRROR_HOLDINGS
    review_frequency: Literal["monthly", "quarterly", "semiannual", "annual"] = "quarterly"
    sleeves: tuple[Sleeve, ...] = ()
    cash_target: Weight = 0.0

    @model_validator(mode="after")
    def _sleeves_are_coherent(self) -> Policy:
        if self.source is PolicySource.SLEEVES and not self.sleeves:
            raise ValueError("policy.source is 'sleeves' but policy.sleeves is empty")
        if not self.sleeves:
            return self
        ids = [s.id for s in self.sleeves]
        if len(ids) != len(set(ids)):
            raise ValueError("policy.sleeves contains duplicate ids")
        total = sum(s.weight for s in self.sleeves) + self.cash_target
        if abs(total - 1.0) > 1e-6:
            raise ValueError(f"policy weights plus cash_target must sum to 1.0, got {total:.6f}")
        return self


class CMA(Strict):
    """Capital market assumptions feeding the Black-Litterman views.

    `source` and `as_of` are required once any number is supplied: an expected
    return with no provenance and no date is not usable in a governance
    document, and the reference had these transcribed as bare constants.
    """

    source: str = ""
    as_of: dt.date | None = None
    basis: Literal["real_excess_cash", "nominal", "real"] = "real_excess_cash"
    regions: dict[str, float] = Field(default_factory=dict)
    factor_premia: dict[str, float] = Field(default_factory=dict)
    prior: Literal["market_cap", "explicit"] = "market_cap"
    confidence: Weight = 0.35

    @model_validator(mode="after")
    def _numbers_need_provenance(self) -> CMA:
        if (self.regions or self.factor_premia) and not (self.source and self.as_of):
            raise ValueError("cma.source and cma.as_of are required when assumptions are supplied")
        return self


class Leverage(Strict):
    enabled: bool = False
    rate_basis: Literal["prime", "fixed", "sofr"] = "prime"
    spread_bps: float = Field(default=0.0, ge=0.0)
    max_leverage: float = Field(default=1.0, ge=1.0)


class Features(Strict):
    lookthrough: bool = True
    factors: bool = True
    optimizer: bool = True
    contributions: bool = True
    ips: bool = True


class Risk(Strict):
    default_period: Literal["1y", "3y", "5y", "max"] = "3y"
    periods: tuple[str, ...] = ("1y", "3y", "5y", "max")
    risk_free: str = "kenfrench_rf"


class Data(Strict):
    price_provider: Literal["yfinance", "csv", "none"] = "yfinance"
    fx_provider: Literal["yfinance", "csv", "none"] = "yfinance"
    factor_provider: Literal["kenfrench", "none"] = "kenfrench"
    cache_dir: str = ".cache"
    max_price_staleness_days: int = Field(default=5, ge=0)


class IPS(Strict):
    enabled: bool = True
    template: str = "config/ips_template.md.j2"
    values: str = "config/ips_values.toml"
    pdf: bool = True


class PortfolioConfig(Strict):
    """The one file a new user edits, or that the setup wizard writes for them."""

    version: Literal[1] = 1
    identity: Identity = Identity()
    branding: Branding = Branding()
    locale: Locale = Locale()
    jurisdiction: Jurisdiction = Jurisdiction()
    data: Data = Data()
    accounts: tuple[Account, ...] = ()
    instruments: tuple[Instrument, ...] = ()
    benchmarks: Benchmarks = Benchmarks()
    policy: Policy = Policy()
    cma: CMA = CMA()
    leverage: Leverage = Leverage()
    risk: Risk = Risk()
    features: Features = Features()
    ips: IPS = IPS()

    @model_validator(mode="after")
    def _cross_references_resolve(self) -> PortfolioConfig:
        account_ids = [a.id for a in self.accounts]
        if len(account_ids) != len(set(account_ids)):
            raise ValueError("accounts contains duplicate ids")

        tickers = [i.ticker for i in self.instruments]
        if len(tickers) != len(set(tickers)):
            raise ValueError("instruments contains duplicate tickers")

        known = set(tickers)
        for sleeve in self.policy.sleeves:
            unknown = [v for v in sleeve.vehicles if v not in known]
            if unknown:
                raise ValueError(
                    f"policy sleeve {sleeve.id!r} names vehicles that are not declared "
                    f"instruments: {', '.join(sorted(unknown))}"
                )
        return self

    @property
    def account_ids(self) -> tuple[str, ...]:
        return tuple(a.id for a in self.accounts)

    def instrument(self, ticker: str) -> Instrument | None:
        return next((i for i in self.instruments if i.ticker == ticker), None)

    def room_groups(self) -> dict[str, tuple[str, ...]]:
        """Room group -> the accounts sharing that contribution limit."""
        groups: dict[str, list[str]] = {}
        for account in self.accounts:
            if account.room_group:
                groups.setdefault(account.room_group, []).append(account.id)
        return {k: tuple(v) for k, v in groups.items()}
