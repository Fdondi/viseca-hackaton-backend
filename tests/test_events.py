from leash.events import assemble_event, authorization_from_attempt, mandate_snapshot, validation_errors


def test_every_attempt_builds_a_schema_valid_event(pack):
    mandate = {"mandate_id": "TM1", "instruction": "x", "hard_rules": [
        {"field": "authorization.billing_amount_chf", "operator": "<=", "value": 20, "currency": "CHF", "scope": "purchase"}],
        "uncertainty_policy": "ask"}
    for row in pack.attempts:
        a = authorization_from_attempt(pack, row, live_id="az_1", related_live_id="az_0" if row["related_authorization_id"] else None,
                                       mandate_id="TM1", profile_id="P1")
        ev = assemble_event(a, mandate_snapshot(mandate, customer_id="CU", card_id=row["card_id"], profile_id="P1"),
                            approved_spend_in_period_chf=0.0, recent_authorizations=[])
        assert validation_errors(ev) == [], row["authorization_id"]


def test_example_event_validates(pack):
    import json
    from leash.data import data_dir
    ev = json.loads((data_dir() / "scenario_fixtures" / "example_authorization_request.json").read_text())
    assert validation_errors(ev) == []


def test_currency_conversion_is_half_even(pack):
    assert pack.to_chf(199.00, "EUR") == 189.05
    assert pack.to_chf(219.00, "GBP") == 245.28
    assert pack.to_chf(450.00, "USD") == 391.50
