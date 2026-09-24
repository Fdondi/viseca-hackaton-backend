"""Lab 3: applying rules to a purchase. Rules and purchase are pre-filled from a story and fully editable.

Every evaluation starts from a clean state: a fresh engine, the customer's card history, the permanent
rules and the mandate as edited. Optionally the story's earlier purchases are decided first (the customer
declines anything that asks), so repeat, split-order and period checks have something to look at.
"""
from __future__ import annotations

import copy

from fastapi import HTTPException
from pydantic import BaseModel

from ..compiler import Compiler
from ..data import load
from ..demo_web.app import _from_form, _to_form
from ..engine import Engine
from ..events import assemble_event, authorization_from_attempt, mandate_snapshot, new_live_id
from ..fields import FIELDS, describe
from ..flow_web.app import _received, _rules, _wall
from ..ledger import Store
from ..permanent import parse_profile
from .common import make_app

app = make_app("Lab 3 · Applying rules", "apply.html")
PACK = load()
PROFILES: dict = {}   # card profiles, shared between evaluations (they only depend on history)
FIELD_CHOICES = sorted(set(FIELDS) | {
    "authorization.merchant.merchant_id", "authorization.merchant.merchant_mcc", "authorization.merchant.merchant_country",
    "authorization.merchant.merchant_category", "authorization.currency", "authorization.fulfillment_method",
    "authorization.order_returnable", "authorization.channel", "items.item_id", "items.item_category"})


class EvalIn(BaseModel):
    scenario_id: str
    index: int
    purchase: dict
    permanent_rules: list[dict] = []
    permanent_policy: str | None = None
    mandate_rules: list[dict] = []
    policy: str = "ask"
    replay: bool = True


def _who(sid: str) -> tuple[str, str, str]:
    auth = PACK.authorities[PACK.scenario_attempts(sid)[0]["authority_id"]]
    return auth["customer_id"], auth["card_id"], "PROFILE_" + auth["authority_id"]


@app.get("/api/scenarios")
def scenarios():
    out = []
    for sid, sc in sorted(PACK.scenarios.items()):
        cust, card, _ = _who(sid)
        out.append({"scenario_id": sid, "persona": PACK.customers[cust]["persona_name"], "story": sc["scenario_name"]})
    return {"scenarios": out}


@app.get("/api/scenario/{sid}")
def scenario(sid: str):
    if sid not in PACK.scenarios:
        raise HTTPException(404, "Unknown story.")
    cust, card, _ = _who(sid)
    perm = parse_profile(PACK, PACK.customers[cust])
    draft = Compiler(PACK).compile(PACK.scenarios[sid]["cardholder_instruction"])
    mandate = [{"rule": n["rule"], "phrase": n["source"]} for n in draft.notes if n["tier"] == "your rules"]
    rows = PACK.scenario_attempts(sid)
    return {
        "customer": {"customer_id": cust, "persona": PACK.customers[cust]["persona_name"], "card_id": card},
        "instruction": PACK.scenarios[sid]["cardholder_instruction"],
        "permanent": {"rules": [{"rule": r.rule, "phrase": r.phrase} for r in perm.rules], "policy": perm.uncertainty_policy},
        "mandate": {"rules": mandate, "policy": draft.uncertainty_policy},
        "purchases": [{"source": r["authorization_id"], "form": _to_form(PACK, r)} for r in rows],
        "catalogue": {
            "items": [{"item_id": i["item_id"], "item_name": i["item_name"], "item_category": i["item_category"]} for i in PACK.items.values()],
            "merchants": [{k: m[k] for k in ("merchant_id", "merchant_name", "merchant_category", "merchant_mcc", "merchant_country",
                                             "merchant_city")} for m in PACK.merchants.values()],
            "currencies": list(PACK.fx), "fields": FIELD_CHOICES},
    }


def _coerce(rules: list[dict]) -> list[tuple[dict, str | None]]:
    """Rules typed in the page → (rule, the phrase it came from). Numbers become numbers, in/not_in values lists."""
    out = []
    for r in rules:
        if not r.get("field") or not r.get("operator"):
            continue
        phrase = r.get("phrase")
        r = {k: v for k, v in r.items() if v not in (None, "") and k != "phrase"}
        v = r.get("value")
        if r["operator"] in ("in", "not_in") and not isinstance(v, list):
            r["value"] = [x.strip() for x in str(v).split(",") if x.strip()]
        elif isinstance(v, str):
            try:
                r["value"] = int(v) if v.strip().lstrip("-").isdigit() else float(v)
            except ValueError:
                pass
        if "period_days" in r:
            r["period_days"] = int(r["period_days"])
        out.append((r, phrase))
    return out


@app.post("/api/evaluate")
def evaluate(body: EvalIn):
    if body.scenario_id not in PACK.scenarios:
        raise HTTPException(404, "Unknown story.")
    cust, card, profile_id = _who(body.scenario_id)
    rows = [copy.deepcopy(r) for r in PACK.scenario_attempts(body.scenario_id)]
    if not 0 <= body.index < len(rows):
        raise HTTPException(422, "No such purchase in this story.")
    base = rows[body.index]
    form = {**body.purchase, "related_authorization_id": ""}      # relations are kept from the story, not typed
    try:
        edited = _from_form(PACK, base, form)
    except HTTPException:
        raise
    except (KeyError, ValueError, TypeError) as exc:
        raise HTTPException(422, f"The purchase isn't complete: {exc}")
    edited["related_authorization_id"] = base["related_authorization_id"] if body.replay else None
    edited["related_authorization_status"] = base["related_authorization_status"] if body.replay else None

    perm_in, mand_in = _coerce(body.permanent_rules), _coerce(body.mandate_rules)
    permanent, mandate_rules = [r for r, _ in perm_in], [r for r, _ in mand_in]
    engine = Engine(PACK, store=Store())
    engine._profiles = PROFILES
    engine.set_permanent(cust, permanent, body.permanent_policy)
    mandate = mandate_snapshot({"mandate_id": "TMLAB", "status": "active", "instruction": PACK.scenarios[body.scenario_id]["cardholder_instruction"],
                                "hard_rules": mandate_rules, "uncertainty_policy": body.policy},
                               customer_id=cust, card_id=card, profile_id=profile_id)
    run = (rows[:body.index] if body.replay else []) + [edited]
    live = {r["authorization_id"]: new_live_id() for r in run}
    history, env, result = [], None, None
    for row in run:
        rel = row.get("related_authorization_id")
        authz = authorization_from_attempt(PACK, row, live_id=live[row["authorization_id"]],
                                           related_live_id=live.get(rel) if rel else None, mandate_id="TMLAB", profile_id=profile_id)
        ledger = engine.store.runs.get("lab")
        approved = ledger.total_approved() if ledger else 0.0
        event = assemble_event(authz, mandate, approved_spend_in_period_chf=approved, recent_authorizations=[])
        result = engine.decide(event, "lab")
        env = {"data": event}
        if row is not edited:
            final = result["decision"]
            if final == "step_up":
                engine.resolve("lab", result["authorization_id"], "decline", by="lab")
                final = "step_up → customer declined"
            history.append({"source": row["authorization_id"], "merchant": result["merchant"]["merchant_name"],
                            "amount_chf": result["amount_chf"], "decision": final})

    notes = [{"rule": r, "tier": "your rules", "source": p} for r, p in mand_in] + \
            [{"rule": r, "tier": "permanent", "source": p} for r, p in perm_in]
    received = _received(env)
    wall = _wall(env["data"], result)
    checks = _rules(result, notes, wall, received)
    counts = {s: sum(1 for c in checks if c["status"] == s) for s in ("fail", "unknown", "pass")}
    return {"decision": result["decision"], "message": result["customer_message"], "reason_codes": result["reason_codes"],
            "policy": result["uncertainty_policy"], "counts": counts,
            "branch": "fail" if counts["fail"] else "unknown" if counts["unknown"] else "pass",
            "checks": checks, "history": history, "latency_ms": result.get("latency_ms"),
            "rule_texts": {"permanent": [describe(r) for r in permanent], "mandate": [describe(r) for r in mandate_rules]}}
