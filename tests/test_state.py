import pytest

from leash.engine import Engine, RevokedError
from leash.fields import Check
from leash.platform import PlatformError, SimPlatform
from conftest import run_scenario


def test_redelivery_is_idempotent(session):
    comp = session.compile(session.pack.scenarios["SCEN0001"]["cardholder_instruction"], "SCEN0001")
    m = session.confirm(comp["draft"], "SCEN0001")
    run = session.start("SCEN0001", m["mandate_id"])
    env = session.platform.next_request(wait=0)
    first = session.handle(env)
    again = session.handle(session.platform.redeliver(env["authorization_id"]))
    assert again["redelivery"] and again["decision"] == first["decision"]
    ledger = session.engine.store.runs[run["run_id"]]
    assert len(ledger.records) == 1 and ledger.total_approved() == 44.5


def test_second_automated_decision_is_rejected():
    sim = SimPlatform()
    d = sim.create_mandate({"instruction": "x", "hard_rules": [], "uncertainty_policy": "ask"})
    m = sim.confirm_mandate(d["draft_id"])
    sim.start_run("SCEN0000", m["mandate_id"])
    env = sim.next_request(wait=0)
    aid = env["authorization_id"]
    sim.post_decision(aid, {"authorization_id": aid, "decision": "step_up"})
    with pytest.raises(PlatformError) as e:
        sim.post_decision(aid, {"authorization_id": aid, "decision": "approve"})
    assert e.value.status == 409


def test_patch_is_append_only_and_policy_only_tightens():
    sim = SimPlatform()
    rule = {"field": "authorization.billing_amount_chf", "operator": "<=", "value": 20}
    m = sim.confirm_mandate(sim.create_mandate({"instruction": "x", "hard_rules": [rule], "uncertainty_policy": "ask"})["draft_id"])
    with pytest.raises(PlatformError):
        sim.patch_mandate(m["mandate_id"], {"hard_rules": []})
    with pytest.raises(PlatformError):
        sim.patch_mandate(m["mandate_id"], {"uncertainty_policy": "approve"})
    sim.patch_mandate(m["mandate_id"], {"hard_rules": [rule, {**rule, "value": 10}], "uncertainty_policy": "decline"})
    assert sim.get_mandate(m["mandate_id"])["uncertainty_policy"] == "decline"


def test_revoke_blocks_pending_approval_and_stops_the_run(session):
    comp = session.compile(session.pack.scenarios["SCEN0001"]["cardholder_instruction"], "SCEN0001")
    m = session.confirm(comp["draft"], "SCEN0001")
    run = session.start("SCEN0001", m["mandate_id"])
    res = session.drive(run["run_id"], customer=None)     # nobody answers step-ups
    pending = [r for r in res if r["decision"] == "step_up"]
    assert pending
    session.revoke(m["mandate_id"])
    with pytest.raises(RevokedError):
        session.resolve(run["run_id"], pending[0]["authorization_id"], "approve")
    session.resolve(run["run_id"], pending[0]["authorization_id"], "decline")   # declining is still allowed

    run2_mandate = session.confirm(comp["draft"], "SCEN0001")
    run2 = session.start("SCEN0001", run2_mandate["mandate_id"])
    first = session.handle(session.platform.next_request(wait=0))
    session.revoke(run2_mandate["mandate_id"])
    assert session.platform.next_request(wait=0) is None           # no further events are queued
    assert session.platform.run_status(run2["run_id"])["stopped"] == "mandate_revoked"
    assert first["decision"] == "approve"


def test_security_signal_is_never_approved_away():
    checks = [Check("security.merchant_text_clean", "unknown", "x", "prompt_injection_detected", security=True)]
    assert Engine.combine(checks, "approve")[0] == "step_up"
    assert Engine.combine([Check("f", "unknown", "x", "fact_unknown")], "approve")[0] == "approve"
    assert Engine.combine([Check("f", "fail", "x", "amount_over_limit")] + checks, "ask")[0] == "decline"
