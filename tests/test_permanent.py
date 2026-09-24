"""Permanent rules: read from the customer's profile (customers.csv), applied to every mandate."""
from conftest import run_scenario
from leash.permanent import parse_profile
from leash.session import Session


def test_prohibitions_become_rules_and_preferences_are_only_noted(pack):
    d = parse_profile(pack, pack.customers["CU0001"])          # "…; avoids gift vouchers." careful, neighbouring countries
    fields = {r.rule["field"]: r for r in d.rules}
    assert fields["derived.quasi_cash_lines"].phrase == "avoids gift vouchers"
    assert fields["derived.shop_country_expected"].kind == "signal"
    assert d.uncertainty_policy == "ask"
    assert any(n.phrase == "Practical groceries" and "belongs in a mandate" in n.why for n in d.noted)


def test_automatic_upgrades_are_unrequested_items_not_all_subscriptions(pack):
    d = parse_profile(pack, pack.customers["CU0005"])          # "no automatic premium upgrades", but has subscriptions
    assert [r.rule["field"] for r in d.rules if r.source_field == "shopping_preferences"] == ["derived.unrequested_lines"]


def test_unenforceable_statements_say_why(pack):
    reasons = {n.phrase: n.why for n in parse_profile(pack, pack.customers["CU0015"]).noted}
    assert "financed" in reasons["no financing"]
    reasons = {n.phrase: n.why for n in parse_profile(pack, pack.customers["CU0004"]).noted}
    assert "no item in the catalogue" in reasons["avoids energy drinks"]


def test_permanent_rules_apply_on_top_of_a_mandate():
    s = Session()
    cust, _ = s.customer_for("SCEN0004")                        # a US seller appears in this story
    s.engine.set_permanent(cust, [{"field": "derived.shop_country_expected", "operator": "in", "value": ["CH"]}], "ask")
    _, _, res = run_scenario(s, "SCEN0004")
    us = next(r for r in res.values() if r["merchant"]["merchant_country"] == "US")
    chk = next(c for c in us["checks"] if c["field"] == "derived.shop_country_expected")
    assert chk["status"] == "unknown" and chk["tier"] == "permanent"
    assert us["decision"] != "approve"
