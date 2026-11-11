import copy
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from quant_common.risk_rules import RiskRules, load_risk_rules

RULES_PATH = Path(__file__).resolve().parents[1] / "config" / "risk_rules.yaml"


@pytest.fixture
def raw():
    return yaml.safe_load(RULES_PATH.read_text())


def test_shipped_rules_validate():
    rules = load_risk_rules(RULES_PATH)
    assert rules.position_limits.max_allocation_pct_per_position == 0.25
    assert rules.position_limits.max_concurrent_positions == 4
    assert rules.options.allow_naked_short_options is False
    assert [m.equity_usd for m in rules.profit_ratchet.milestones] == [50000, 100000, 150000]


@pytest.mark.parametrize("path,value", [
    (("options", "allow_naked_short_options"), True),
    (("equity", "allow_margin"), True),
    (("equity", "allow_short_equity"), True),
    (("position_limits", "max_allocation_pct_per_position"), 0.30),
    (("position_limits", "max_concurrent_positions"), 5),
    (("stops", "trail_only_upward"), False),
    (("execution", "require_unique_client_order_id"), False),
])
def test_mandate_guardrails_cannot_be_loosened(raw, path, value):
    bad = copy.deepcopy(raw)
    node = bad
    for k in path[:-1]:
        node = node[k]
    node[path[-1]] = value
    with pytest.raises(ValidationError):
        RiskRules.model_validate(bad)


def test_milestones_must_ascend(raw):
    bad = copy.deepcopy(raw)
    bad["profit_ratchet"]["milestones"].reverse()
    with pytest.raises(ValidationError):
        RiskRules.model_validate(bad)


def test_unknown_keys_rejected(raw):
    bad = copy.deepcopy(raw)
    bad["position_limits"]["max_allocation_pct_per_positon"] = 0.1  # typo
    with pytest.raises(ValidationError):
        RiskRules.model_validate(bad)


def test_missing_regime_atr_multiple(raw):
    bad = copy.deepcopy(raw)
    del bad["stops"]["atr_multiple_by_regime"]["chop_defensive"]
    with pytest.raises(ValidationError):
        RiskRules.model_validate(bad)
