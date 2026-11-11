"""Typed loader for config/risk_rules.yaml. Invalid config = governor refuses to start."""
from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Regime = Literal["trend_momentum", "high_vol_catalyst", "chop_defensive"]
Structure = Literal[
    "long_call", "long_put", "call_debit_spread", "put_debit_spread",
    "long_straddle", "long_strangle", "leaps_call",
]
Pct = Field(gt=0, le=1)


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Account(_Strict):
    starting_capital_usd: float = Field(gt=0)
    target_equity_usd: float = Field(gt=0)


class PositionLimits(_Strict):
    max_allocation_pct_per_position: float = Field(gt=0, le=0.25)  # mandate ceiling
    max_concurrent_positions: int = Field(ge=1, le=4)              # mandate ceiling
    max_risk_pct_per_trade: float = Pct
    max_sector_allocation_pct: float = Pct
    min_cash_buffer_pct: float = Field(ge=0, lt=1)


class Options(_Strict):
    allow_naked_short_options: Literal[False]   # cannot be enabled via config
    allowed_structures: list[Structure] = Field(min_length=1)
    min_payoff_asymmetry: float = Field(ge=1)
    min_days_to_expiry: int = Field(ge=0)
    max_days_to_expiry: int = Field(gt=0)
    min_open_interest: int = Field(ge=0)
    max_bid_ask_spread_pct: float = Pct
    close_before_expiry_days: int = Field(ge=0)

    @model_validator(mode="after")
    def _dte_order(self):
        if self.min_days_to_expiry >= self.max_days_to_expiry:
            raise ValueError("min_days_to_expiry must be < max_days_to_expiry")
        return self


class Pyramiding(_Strict):
    enabled: bool
    max_adds: int = Field(ge=0)
    add_trigger_r_multiple: float = Field(gt=0)
    add_size_fraction: float = Pct


class EquityRules(_Strict):
    allow_short_equity: Literal[False]
    allow_margin: Literal[False]
    pyramiding: Pyramiding


class Stops(_Strict):
    atr_period: int = Field(ge=2)
    atr_multiple_by_regime: dict[Regime, float]
    trail_only_upward: Literal[True]
    option_premium_stop_pct: float = Pct

    @field_validator("atr_multiple_by_regime")
    @classmethod
    def _all_regimes(cls, v):
        missing = {"trend_momentum", "high_vol_catalyst", "chop_defensive"} - set(v)
        if missing:
            raise ValueError(f"missing ATR multiple for regimes: {sorted(missing)}")
        if any(m <= 0 for m in v.values()):
            raise ValueError("ATR multiples must be > 0")
        return v


class CircuitBreakers(_Strict):
    max_daily_loss_pct: float = Pct
    max_weekly_loss_pct: float = Pct
    max_drawdown_from_peak_pct: float = Pct
    max_orders_per_day: int = Field(ge=1)
    max_consecutive_losses: int = Field(ge=1)


class Milestone(_Strict):
    equity_usd: float = Field(gt=0)
    lock_pct_of_gains: float = Field(ge=0, le=1)


class ProfitRatchet(_Strict):
    enabled: bool
    milestones: list[Milestone]

    @field_validator("milestones")
    @classmethod
    def _ascending(cls, v):
        eq = [m.equity_usd for m in v]
        if eq != sorted(eq) or len(set(eq)) != len(eq):
            raise ValueError("milestones must be strictly ascending by equity_usd")
        return v


class Execution(_Strict):
    order_type_default: Literal["LMT"]
    max_slippage_pct_from_mid: float = Pct
    require_unique_client_order_id: Literal[True]
    trading_hours_only: bool
    no_new_entries_first_minutes: int = Field(ge=0)
    no_new_entries_last_minutes: int = Field(ge=0)


class RiskRules(_Strict):
    version: int
    account: Account
    position_limits: PositionLimits
    options: Options
    equity: EquityRules
    stops: Stops
    circuit_breakers: CircuitBreakers
    profit_ratchet: ProfitRatchet
    execution: Execution

    @model_validator(mode="after")
    def _milestones_above_start(self):
        start = self.account.starting_capital_usd
        if any(m.equity_usd <= start for m in self.profit_ratchet.milestones):
            raise ValueError("every ratchet milestone must exceed starting capital")
        return self


def load_risk_rules(path: str | Path) -> RiskRules:
    with open(path, encoding="utf-8") as fh:
        return RiskRules.model_validate(yaml.safe_load(fh))
