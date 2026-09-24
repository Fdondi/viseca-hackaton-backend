from fastapi.testclient import TestClient

from leash import lineage
from leash.fields import FIELDS
from leash.flow_web.app import app


def test_every_catalogue_field_says_what_it_reads():
    for f in FIELDS:
        assert lineage.reads(f), f
    for key in (k for f in FIELDS for k in lineage.reads(f)):
        assert key in lineage.NODES or key.startswith(("authorization.", "context.", "mandate.")), key


def test_flow_explains_a_purchase_field_by_field():
    c = TestClient(app)
    c.post("/api/reset")
    cust = next(x for x in c.get("/api/customers").json()["customers"] if x["scenario_id"] == "SCEN0004")
    st = c.post("/api/compile", json={"scenario_id": "SCEN0004", "instruction": cust["instruction"]}).json()
    assert st["step"] == "review" and all(p["phrase"] for p in st["setup"]["parser"])
    cap = next(n for n in st["notes"] if n["rule"]["field"] == "authorization.billing_amount_chf")
    st = c.post("/api/confirm", json={"edits": [{"index": cap["index"], "value": "350"}]}).json()
    assert st["step"] == "shopping" and st["setup"]["edits"]
    st = c.post("/api/next", json={}).json()       # the monitor, under the edited cap
    assert st["explain"]["answer"]["decision"] == "approve"
    st = c.post("/api/next", json={}).json()       # the same monitor, over the cap
    over = st["explain"]
    assert over["combine"]["decision"] == "decline" and over["combine"]["branch"] == "fail"
    amt = next(r for r in over["rules"] if r["field"] == "authorization.billing_amount_chf")
    assert amt["origin"]["kind"] == "words" and amt["origin"]["edited"] and not amt["text_input"] and amt["status"] == "fail"
    ex = None
    while st.get("next"):                          # the same monitor, with instructions hidden in the shop text
        st = c.post("/api/next", json={}).json()
        if any("AUTOMATED PURCHASING AGENTS" in t["text"] for t in st["explain"]["shop_text"]):
            ex = st["explain"]
            break
        if st["explain"]["answer"]["decision"] == "step_up":
            st = c.post("/api/resolve", json={"authorization_id": st["explain"]["authorization_id"], "decision": "decline"}).json()
    assert ex is not None
    free = {r["key"] for r in ex["received"] if r["class"] == "free_text"}
    assert "authorization.items[].item_details" in free and "authorization.billing_amount_chf" not in free
    assert any("AUTOMATED PURCHASING AGENTS" in r["value"] for r in ex["received"] if r["key"] == "authorization.items[].item_details")
    inj = next(r for r in ex["rules"] if r["field"] == "security.merchant_text_clean")
    assert inj["status"] == "unknown" and inj["text_input"] and not inj["model_fact"]
    assert ex["combine"]["decision"] == "step_up" and ex["combine"]["branch"] == "unknown"
    assert "prompt_injection_detected" in ex["combine"]["reason_codes"]                    # the manipulation stays visible
    assert all(w["flagged"] for w in ex["wall"])
    assert ex["ap2_diff"] is None and not ex["view"]["ap2"]        # the shop didn't sign: plain card purchase


def test_orders_follow_an_edited_mandate():
    c = TestClient(app)
    c.post("/api/reset")
    c.post("/api/compile", json={"scenario_id": "SCEN0004",
                                 "instruction": "Only buy lactose-free milk. Only from Migros or Coop. At most CHF 30 per order."})
    st = c.post("/api/confirm", json={}).json()
    names = []
    while st.get("next"):
        st = c.post("/api/next", json={}).json()
        names.append(st["explain"]["items"])
        if st["explain"]["answer"]["decision"] == "step_up":
            st = c.post("/api/resolve", json={"authorization_id": st["explain"]["authorization_id"], "decision": "decline"}).json()
    flat = " ".join(x for row in names for x in row).lower()
    assert "milk" in flat and "monitor" in flat          # milk, as asked, plus one order for something else
    assert st["feed"][0]["decision"] == "approve"
    assert any(f["decision"] == "decline" for f in st["feed"])


def test_shop_signed_cart_shows_what_ap2_changed():
    c = TestClient(app)
    c.post("/api/reset")
    cust = next(x for x in c.get("/api/customers").json()["customers"] if x["scenario_id"] == "SCEN0002")
    c.post("/api/compile", json={"scenario_id": "SCEN0002", "instruction": cust["instruction"]})
    c.post("/api/confirm", json={})
    ex = c.post("/api/next", json={"ap2": True}).json()["explain"]   # AU0012: size 43, 30-day returns
    d = ex["ap2_diff"]
    assert d["decision"] == {"without": "approve", "with": "approve"}
    assert {a["field"] for a in d["added"]} >= {"ap2.merchant_signature", "ap2.checkout_matches", "ap2.replay"}
    effects = {c["field"]: c["effect"] for c in d["changed"]}
    assert effects["extracted.size"] == "uses a shop-signed term"
    assert effects["authorization.billing_amount_chf"] == "also in the signed permission"
    assert any(r.get("ap2_effect") for r in ex["rules"])


def test_every_check_says_how_it_was_decided():
    from conftest import run_scenario
    from leash.session import Session
    for sid in ("SCEN0000", "SCEN0001", "SCEN0002", "SCEN0003", "SCEN0004"):
        _, _, res = run_scenario(Session(ap2=True), sid)
        for r in res.values():
            for c in r["checks"]:
                assert c.get("trace"), (sid, r["source_authorization_id"], c["field"])
                assert not any("trace unavailable" in t for t in c["trace"]), (sid, c["field"], c["trace"])
