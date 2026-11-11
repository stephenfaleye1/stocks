"""Broker safety interlocks. Pure functions — no I/O — so they are unit-testable.

Rules:
* Paper mode must target a paper API port and only see paper ("DU…") accounts.
* Live mode is refused unless TRADING_MODE=live AND LIVE_TRADING_ENABLED=true AND
  the exact confirmation phrase is set — and even then only when the caller
  explicitly passes `allow_live=True` (no Phase 1-4 code path does).
"""
from __future__ import annotations

from collections.abc import Iterable

LIVE_CONFIRMATION_PHRASE = "I UNDERSTAND REAL MONEY IS AT RISK"

# IB Gateway: 4002 paper / 4001 live. TWS: 7497 paper / 7496 live.
# gnzsnz/ib-gateway socat relays: 4004 paper / 4003 live.
PAPER_PORTS = frozenset({4002, 4004, 7497})
LIVE_PORTS = frozenset({4001, 4003, 7496})


class TradingSafetyError(RuntimeError):
    """Raised when a configuration could route orders somewhere unintended."""


def assert_broker_target_safe(
    *,
    trading_mode: str,
    port: int,
    live_trading_enabled: bool = False,
    live_trading_confirmation: str = "",
    allow_live: bool = False,
) -> None:
    if trading_mode == "paper":
        if port in LIVE_PORTS:
            raise TradingSafetyError(f"TRADING_MODE=paper but IB port {port} is a LIVE port")
        if port not in PAPER_PORTS:
            raise TradingSafetyError(
                f"IB port {port} is not a known paper port {sorted(PAPER_PORTS)}"
            )
        return

    if trading_mode != "live":
        raise TradingSafetyError(f"Unknown TRADING_MODE {trading_mode!r}")

    problems = []
    if not allow_live:
        problems.append("live routing is not permitted by this code path")
    if not live_trading_enabled:
        problems.append("LIVE_TRADING_ENABLED is not true")
    if live_trading_confirmation != LIVE_CONFIRMATION_PHRASE:
        problems.append("LIVE_TRADING_CONFIRMATION phrase missing or wrong")
    if port not in LIVE_PORTS:
        problems.append(f"port {port} is not a live port")
    if problems:
        raise TradingSafetyError("Live trading refused: " + "; ".join(problems))


def assert_accounts_match_mode(
    accounts: Iterable[str], *, trading_mode: str, paper_prefix: str = "DU"
) -> None:
    accounts = [a for a in accounts if a]
    if not accounts:
        raise TradingSafetyError("Broker reported no managed accounts")
    if trading_mode == "paper":
        bad = [a for a in accounts if not a.startswith(paper_prefix)]
        if bad:
            raise TradingSafetyError(
                f"Paper mode but broker exposed non-paper account(s): {bad}"
            )
    elif trading_mode == "live":
        bad = [a for a in accounts if a.startswith(paper_prefix)]
        if bad:
            raise TradingSafetyError(f"Live mode but broker exposed paper account(s): {bad}")
