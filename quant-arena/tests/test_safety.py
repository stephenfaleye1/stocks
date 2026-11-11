import pytest

from quant_common.safety import (
    LIVE_CONFIRMATION_PHRASE,
    TradingSafetyError,
    assert_accounts_match_mode,
    assert_broker_target_safe,
)


@pytest.mark.parametrize("port", [4002, 4004, 7497])
def test_paper_ports_allowed(port):
    assert_broker_target_safe(trading_mode="paper", port=port)


@pytest.mark.parametrize("port", [4001, 4003, 7496, 9999])
def test_paper_mode_rejects_live_or_unknown_ports(port):
    with pytest.raises(TradingSafetyError):
        assert_broker_target_safe(trading_mode="paper", port=port)


def test_live_refused_by_default_even_with_all_flags():
    with pytest.raises(TradingSafetyError, match="not permitted"):
        assert_broker_target_safe(
            trading_mode="live", port=4001, live_trading_enabled=True,
            live_trading_confirmation=LIVE_CONFIRMATION_PHRASE,
        )


def test_live_requires_every_flag():
    with pytest.raises(TradingSafetyError, match="LIVE_TRADING_ENABLED"):
        assert_broker_target_safe(
            trading_mode="live", port=4001, live_trading_enabled=False,
            live_trading_confirmation=LIVE_CONFIRMATION_PHRASE, allow_live=True,
        )
    with pytest.raises(TradingSafetyError, match="phrase"):
        assert_broker_target_safe(
            trading_mode="live", port=4001, live_trading_enabled=True,
            live_trading_confirmation="yes", allow_live=True,
        )
    assert_broker_target_safe(
        trading_mode="live", port=4001, live_trading_enabled=True,
        live_trading_confirmation=LIVE_CONFIRMATION_PHRASE, allow_live=True,
    )


def test_unknown_mode_rejected():
    with pytest.raises(TradingSafetyError):
        assert_broker_target_safe(trading_mode="sim", port=4002)


def test_paper_accounts_must_be_du():
    assert_accounts_match_mode(["DU1234567"], trading_mode="paper")
    with pytest.raises(TradingSafetyError):
        assert_accounts_match_mode(["DU1234567", "U7654321"], trading_mode="paper")
    with pytest.raises(TradingSafetyError):
        assert_accounts_match_mode([], trading_mode="paper")


def test_live_mode_rejects_paper_accounts():
    with pytest.raises(TradingSafetyError):
        assert_accounts_match_mode(["DU1234567"], trading_mode="live")
