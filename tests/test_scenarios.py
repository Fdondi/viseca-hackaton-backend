import json
from pathlib import Path

import pytest

from conftest import run_scenario

LABELS = json.loads((Path(__file__).parent.parent / "leash" / "eval" / "expected.json").read_text())["labels"]


@pytest.mark.parametrize("sid", ["SCEN0000", "SCEN0001", "SCEN0002", "SCEN0003", "SCEN0004"])
def test_matches_preregistered_labels(session, sid):
    _, _, res = run_scenario(session, sid)
    for aid, r in res.items():
        e = LABELS[aid]
        assert r["decision"] == e["expected"], (aid, r["decision"], r["headline"])


def test_rolling_window_boundary_exactly_300(session):
    _, _, res = run_scenario(session, "SCEN0001")
    period = next(c for c in res["AU0008"]["checks"] if c["field"] == "derived.period_spend_chf")
    assert period["actual"] == 300.0 and res["AU0008"]["decision"] == "approve"
    assert res["AU0009"]["decision"] == "decline"


def test_trusting_customer_changes_the_rolling_total(session):
    _, _, res = run_scenario(session, "SCEN0001", answer="approve")
    assert res["AU0006"]["decision"] == "step_up"
    assert res["AU0008"]["decision"] == "decline"   # the approved split order now counts


def test_eur_price_compared_in_chf(session):
    _, _, res = run_scenario(session, "SCEN0003")
    assert res["AU0032"]["amount_chf"] == 247.0 and res["AU0032"]["decision"] == "approve"


def test_injection_flags_merchant_and_alerts(session):
    m, _, res = run_scenario(session, "SCEN0004")
    ctl = session.engine.store.customer(session.mandates[m["mandate_id"]]["customer_id"])
    assert "prompt_injection_detected" in res["AU0037"]["reason_codes"]
    assert ctl.merchant_flags["ME0022"]["mode"] == "ask"
    assert any(a["merchant_id"] == "ME0022" for a in ctl.alerts)
    assert "merchant_flagged_manipulation" in res["AU0045"]["reason_codes"]


def test_block_turns_flag_into_decline_and_patches_mandate(session):
    def act(r):
        if r["source_authorization_id"] == "AU0040":
            session.set_merchant_flag(session.mandates[r["mandate_id"]]["customer_id"], "ME0022", "block")
    m, _, res = run_scenario(session, "SCEN0004", on_result=act)
    assert res["AU0042"]["decision"] == "decline" and res["AU0045"]["decision"] == "decline"
    rules = session.platform.get_mandate(m["mandate_id"])["hard_rules"]
    assert rules[-1] == {"field": "authorization.merchant.merchant_id", "operator": "not_in", "value": ["ME0022"]}
