"""Interactive demo: pick a customer, see the agent's next proposed purchase,
edit it, send it, and see the wallet's answer.

Each transaction goes through the same path as a real run: the edited row is
queued by the simulator as a schema-valid event, the session hands it to the
engine, and the decision is posted back to the simulator. Nothing is faked
for the demo.
"""
from __future__ import annotations

import copy
import threading
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from ..ap2 import shop_attributes
from ..data import load
from ..engine import RevokedError
from ..events import new_live_id, validation_errors
from ..platform import PlatformError, SimPlatform
from ..session import Session

STATIC = Path(__file__).parent / "static"
app = FastAPI(title="Agent on a Leash: interactive demo")
LOCK = threading.RLock()
ST: dict = {}

# AP2: what the agent does with the shop-signed cart (the form edits are what it submits)
AP2_ATTACKS = {
    "": "honest",
    "tamper": "changed the purchase after the shop signed it",
    "replay": "re-presented the previous signed cart",
    "wrong_shop_key": "brought a cart signed with another shop's key",
    "wrong_agent_key": "signed with an agent key the customer never authorised",
    "withhold": "withheld the shop-signed cart",
}

EDITABLE = ("timestamp", "currency", "delivery_fee", "customer_device_id", "recent_attempt_count_10m",
            "fulfillment_method", "order_returnable", "order_cancellable", "purchase_description",
            "related_authorization_id")


# ------------------------------------------------------------------ helpers
def _session() -> Session:
    if "session" not in ST:
        raise HTTPException(400, "Choose a customer first.")
    return ST["session"]


def _run() -> dict:
    return _session().platform.runs[ST["run_id"]]


def _merchant(pack, row: dict) -> dict:
    m = {**pack.merchants.get(row["merchant_id"], {}), **row.get("_merchant_override", {})}
    m.setdefault("availability", "online")
    m.setdefault("recurring_capable", "false")
    m["merchant_id"] = row["merchant_id"]
    return m


def _to_form(pack, row: dict) -> dict:
    """Scenario row → the editable transaction shown in the page."""
    lines = row.get("_items") or pack.attempt_items.get(row["authorization_id"], [])
    m = _merchant(pack, row)
    form = {
        "source_id": row["authorization_id"],
        "merchant": {k: m.get(k, "") for k in ("merchant_id", "merchant_name", "merchant_category", "merchant_mcc",
                                                "merchant_country", "merchant_city")},
        "lines": [{k: l[k] for k in ("item_id", "item_name", "item_category", "quantity", "unit_price", "item_details")}
                  for l in lines],
        **{k: row.get(k) for k in EDITABLE},
    }
    if ST.get("ap2"):
        # empty = the shop signs what its own product text says (shown as the placeholder)
        for fl, l in zip(form["lines"], lines):
            fl["signed"] = {"size": "", "return_window_days": ""}
            fl["signed_default"] = shop_attributes(l)
        form["ap2"] = {"attack": "", "returnable": ""}
    return form


def _from_form(pack, base: dict, form: dict) -> dict:
    """Editable transaction → a scenario row the simulator can queue. Totals are recomputed."""
    row = copy.deepcopy(base)
    cur = form["currency"]
    if cur not in pack.fx:
        raise HTTPException(422, f"Currency must be one of {', '.join(pack.fx)}.")
    lines = []
    for n, l in enumerate(form["lines"], 1):
        if int(l["quantity"]) < 1 or float(l["unit_price"]) <= 0:
            raise HTTPException(422, "Every line needs a quantity of at least 1 and a price above 0.")
        lines.append({"line_no": n, "item_id": l["item_id"], "item_name": l["item_name"], "item_category": l["item_category"],
                      "quantity": int(l["quantity"]), "unit_price": round(float(l["unit_price"]), 2), "currency": cur,
                      "item_details": l["item_details"]})
    if not lines:
        raise HTTPException(422, "A purchase needs at least one basket line.")
    subtotal = round(sum(l["quantity"] * l["unit_price"] for l in lines), 2)
    delivery = round(float(form.get("delivery_fee") or 0), 2)
    amount = round(subtotal + delivery, 2)
    m = form["merchant"]
    mid = (m.get("merchant_id") or "").strip() or "ME9999"
    row.update({
        "merchant_id": mid,
        "_merchant_override": {
            "merchant_name": m["merchant_name"], "merchant_category": m["merchant_category"],
            "merchant_mcc": str(m["merchant_mcc"]).zfill(4)[:4], "merchant_country": m["merchant_country"].upper()[:2],
            "merchant_city": m["merchant_city"] or "Unknown",
            "availability": pack.merchants.get(mid, {}).get("availability", "online"),
            "recurring_capable": pack.merchants.get(mid, {}).get("recurring_capable", "false"),
        },
        "_items": lines, "items_subtotal": subtotal, "delivery_fee": delivery, "amount": amount,
        "billing_amount_chf": pack.to_chf(amount, cur),
    })
    for k in EDITABLE:
        if k in form and k not in ("delivery_fee",):
            row[k] = form[k]
    row["recent_attempt_count_10m"] = int(row["recent_attempt_count_10m"] or 0)
    row["related_authorization_id"] = row["related_authorization_id"] or None
    rel = row["related_authorization_id"]
    if rel:
        live = _run()["live"].get(rel)
        a = _session().platform.auths.get(live) if live else None
        st = a["status"] if a else None
        row["related_authorization_status"] = {"queued": "pending", "expired": "declined", "timed_out": "declined"}.get(st, st)
    else:
        row["related_authorization_status"] = None
    return row


def _diff(before: dict, after: dict) -> list[str]:
    out = []
    for k in ("timestamp", "currency", "delivery_fee", "customer_device_id", "recent_attempt_count_10m",
              "fulfillment_method", "order_returnable", "purchase_description", "related_authorization_id"):
        if str(before.get(k)) != str(after.get(k)):
            out.append(f"{k.replace('_', ' ')}: {before.get(k)} → {after.get(k)}")
    for k in ("merchant_id", "merchant_name", "merchant_mcc", "merchant_country"):
        if str(before["merchant"].get(k)) != str(after["merchant"].get(k)):
            out.append(f"shop {k.replace('merchant_', '')}: {before['merchant'].get(k)} → {after['merchant'].get(k)}")
    if len(before["lines"]) != len(after["lines"]):
        out.append(f"basket lines: {len(before['lines'])} → {len(after['lines'])}")
    for i, (b, a) in enumerate(zip(before["lines"], after["lines"]), 1):
        for k in ("item_id", "quantity", "unit_price"):
            if str(b[k]) != str(a[k]):
                out.append(f"line {i} {k.replace('_', ' ')}: {b[k]} → {a[k]}")
        if b["item_details"] != a["item_details"]:
            out.append(f"line {i} shop text edited")
    return out


def _ap2_knobs(pack, form: dict, base: dict, idx: int, run: dict) -> tuple[dict, list[str]]:
    """The demo form's AP2 block → the simulator's knobs (ap2.Ap2Sim), plus lines for the edit list."""
    ap = form.get("ap2") or {}
    knobs, notes = {}, []
    attrs = {}
    for n, l in enumerate(form["lines"], 1):
        for k, v in (l.get("signed") or {}).items():
            if str(v).strip():
                attrs.setdefault(n, {})[k] = int(float(v)) if k == "return_window_days" else str(v).strip()
                notes.append(f"shop signs line {n} {k.replace('_', ' ')} = {v}")
    if attrs:
        knobs["attributes"] = attrs
    if ap.get("returnable"):
        knobs["terms"] = {"returnable": ap["returnable"]}
        notes.append(f"shop signs returnable = {ap['returnable']}")
    attack = ap.get("attack") or ""
    if attack not in AP2_ATTACKS:
        raise HTTPException(422, "Unknown agent behaviour.")
    if attack == "tamper":
        knobs["signed_row"] = copy.deepcopy(base)
    elif attack == "replay":
        if idx == 0:
            raise HTTPException(409, "There is no earlier signed cart to replay yet.")
        knobs["replay_of"] = run["rows"][idx - 1]["authorization_id"]
    elif attack == "wrong_shop_key":
        mid = (form["merchant"].get("merchant_id") or "").strip()
        knobs["merchant_key_of"] = next(m for m in sorted(pack.merchants) if m != mid)
    elif attack == "wrong_agent_key":
        knobs["wrong_agent_key"] = True
    elif attack == "withhold":
        knobs["withhold_checkout"] = True
    if attack:
        notes.append(f"agent {AP2_ATTACKS[attack]}")
    return knobs, notes


def _caps(rules: list[dict]) -> dict:
    per_order = [r["value"] for r in rules if r["field"] == "authorization.billing_amount_chf"]
    return {"per_order_chf": min(per_order) if per_order else None}


def _state() -> dict:
    s = _session()
    run = _run()
    ledger = s.engine.store.runs.get(ST["run_id"])
    timeline = []
    for r in s.log:
        rec = ledger.get(r["authorization_id"]) if ledger else None
        timeline.append({"authorization_id": r["authorization_id"], "source": r["source_authorization_id"],
                         "timestamp": r["timestamp"], "merchant": r["merchant"]["merchant_name"], "amount_chf": r["amount_chf"],
                         "decision": r["decision"], "status": rec.status if rec else None, "headline": r["headline"],
                         "edited": ST["edits"].get(r["source_authorization_id"], [])})
    ctl = s.engine.store.customer(ST["customer_id"])
    status = s.platform.run_status(ST["run_id"])
    idx = run["released"]
    nxt = None
    if not run["stopped"] and idx < len(run["rows"]):
        nxt = _to_form(s.pack, run["rows"][idx])
    return {
        "customer": ST["customer"], "mandate": ST["mandate"], "draft": ST["draft"], "backtest": ST["backtest"],
        "model": ST.get("model"),
        "caps": _caps(ST["mandate"]["hard_rules"]), "run": status,
        "next": nxt, "next_index": idx + 1, "total": len(run["rows"]),
        "original_next": _to_form(s.pack, ST["original_rows"][idx]) if nxt and idx < len(ST["original_rows"]) else None,
        "previous": ST.get("last_form"),
        "earlier_ids": [r["authorization_id"] for r in run["rows"][:idx]],
        "timeline": timeline, "last": ST.get("last"),
        "pending": [t for t in timeline if t["status"] == "pending"],
        "flags": list(ctl.merchant_flags.values()), "alerts": ctl.alerts[-6:],
        "revoked": ST["mandate"]["mandate_id"] in ctl.revoked_mandates,
        "ap2": ST.get("ap2"), "ap2_attacks": AP2_ATTACKS, "resolutions": ST.get("resolutions", {}),
    }


# ------------------------------------------------------------------ API
class StartIn(BaseModel):
    scenario_id: str
    instruction: str | None = None
    ap2: bool = False


class SendIn(BaseModel):
    transaction: dict


class ResolveIn(BaseModel):
    authorization_id: str
    decision: str


class FlagIn(BaseModel):
    merchant_id: str
    mode: str


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/customers")
def customers():
    pack = load()
    s = Session()
    out = []
    for sid, sc in sorted(pack.scenarios.items()):
        cust, card = s.customer_for(sid)
        c = pack.customers[cust]
        prof = s.engine.profile(card).summary()
        out.append({"scenario_id": sid, "story": sc["scenario_name"], "instruction": sc["cardholder_instruction"],
                    "persona": c["persona_name"], "region": c["home_region"], "background": c["background"],
                    "budget_style": c["budget_style"], "card_id": card, "events": int(sc["event_count"]),
                    "familiar": prof["familiar_merchants"][:4]})
    return {"customers": out}


@app.get("/api/catalogue")
def catalogue():
    pack = load()
    return {
        "items": [{"item_id": i["item_id"], "item_name": i["item_name"], "item_category": i["item_category"],
                   "typical_chf": i["unit_price_typical_chf"]} for i in pack.items.values()],
        "merchants": [{k: m[k] for k in ("merchant_id", "merchant_name", "merchant_category", "merchant_mcc",
                                         "merchant_country", "merchant_city")} for m in pack.merchants.values()],
        "currencies": list(pack.fx),
        "fx": {k: float(v) for k, v in pack.fx.items()},
    }


@app.post("/api/start")
def start(body: StartIn):
    with LOCK:
        s = Session(platform=SimPlatform(), use_llm=True, ap2=body.ap2)
        pack = s.pack
        if body.scenario_id not in pack.scenarios:
            raise HTTPException(404, "Unknown customer story.")
        text = (body.instruction or pack.scenarios[body.scenario_id]["cardholder_instruction"]).strip()
        comp = s.compile(text, body.scenario_id)
        m = s.confirm(comp["draft"], body.scenario_id)
        rows = [copy.deepcopy(r) for r in pack.scenario_attempts(body.scenario_id)]
        info = s.platform.inject_run(body.scenario_id, m["mandate_id"], rows)
        s.runs[info["run_id"]] = info
        cust, card = s.customer_for(body.scenario_id)
        ST.clear()
        ST.update(session=s, run_id=info["run_id"], mandate=s.platform.get_mandate(m["mandate_id"]), draft=comp["draft"],
                  model=comp.get("model"),
                  backtest=comp["backtest"], customer_id=cust, original_rows=copy.deepcopy(rows), edits={},
                  ap2=m.get("ap2"), resolutions={},
                  customer={"persona": pack.customers[cust]["persona_name"], "scenario_id": body.scenario_id,
                            "story": pack.scenarios[body.scenario_id]["scenario_name"], "card_id": card,
                            "profile": s.engine.profile(card).summary()})
        return _state()


@app.get("/api/state")
def state():
    with LOCK:
        return _state()


@app.post("/api/send")
def send(body: SendIn):
    with LOCK:
        s = _session()
        run = _run()
        idx = run["released"]
        if run["stopped"]:
            raise HTTPException(409, "The customer withdrew permission: the platform queues no more purchases.")
        if idx >= len(run["rows"]):
            raise HTTPException(409, "No purchase left. Compose another one.")
        original = _to_form(s.pack, run["rows"][idx])
        row = _from_form(s.pack, run["rows"][idx], body.transaction)
        ap2_notes = []
        if ST.get("ap2"):
            row["_ap2"], ap2_notes = _ap2_knobs(s.pack, body.transaction, run["rows"][idx], idx, run)
        run["rows"][idx] = row
        env = s.platform.next_request(wait=0)
        if env is None:
            raise HTTPException(409, "The simulator did not queue the purchase.")
        errors = validation_errors(env["data"])
        result = s.handle(env)
        after = _to_form(s.pack, row)
        edits = _diff(_to_form(s.pack, ST["original_rows"][idx]) if idx < len(ST["original_rows"]) else original, after) + ap2_notes
        ST["edits"][row["authorization_id"]] = edits
        ST["last_form"] = after
        ST["last"] = {
            "authorization_id": result["authorization_id"], "source": result["source_authorization_id"],
            "decision": result["decision"], "message": result["customer_message"], "headline": result["headline"],
            "reason_codes": result["reason_codes"], "checks": result["checks"], "facts": result["facts"],
            "security_flags": result.get("security_flags", []), "latency_ms": result.get("latency_ms"),
            "edits": edits, "schema_errors": errors, "sent": after, "amount_chf": result["amount_chf"],
            "api_body": {k: result[k] for k in ("authorization_id", "decision", "reason_codes", "customer_message", "engine_version")},
            "flags_created": [{"merchant_name": f["merchant_name"], "reason": f["reason"], "repeat": f.get("repeat")}
                              for f in result.get("flags_created", [])],
            "ap2": result.get("ap2"), "ap2_receipt": (result.get("ap2_receipt") or {}).get("payload"),
        }
        return _state()


@app.post("/api/compose")
def compose():
    """After the scenario ends: another purchase, starting from a copy of the last one."""
    with LOCK:
        run = _run()
        if run["stopped"]:
            raise HTTPException(409, "Permission was withdrawn.")
        base = copy.deepcopy(run["rows"][max(run["released"] - 1, 0)])
        n = sum(1 for r in run["rows"] if r["authorization_id"].startswith("AUX")) + 1
        base["authorization_id"] = f"AUX{n:03d}"
        base["replay_order"] = len(run["rows"]) + 1
        base["related_authorization_id"] = None
        base["related_authorization_status"] = None
        run["rows"].append(base)
        run["live"][base["authorization_id"]] = new_live_id()
        return _state()


@app.post("/api/resolve")
def resolve(body: ResolveIn):
    with LOCK:
        try:
            res = _session().resolve(ST["run_id"], body.authorization_id, body.decision)
        except (RevokedError, ValueError) as exc:
            raise HTTPException(409, str(exc))
        if res.get("ap2"):
            ST["resolutions"][body.authorization_id] = {
                "receipt": res["ap2"]["receipt"]["payload"],
                "signed": (res["ap2"].get("closed_payment_by_customer") or {}).get("payload")}
        return _state()


@app.post("/api/flag")
def flag(body: FlagIn):
    with LOCK:
        if body.mode not in ("ask", "block", "remove"):
            raise HTTPException(422, "mode must be ask, block or remove")
        _session().set_merchant_flag(ST["customer_id"], body.merchant_id, body.mode)
        ST["mandate"] = _session().platform.get_mandate(ST["mandate"]["mandate_id"])
        return _state()


@app.post("/api/revoke")
def revoke():
    with LOCK:
        _session().revoke(ST["mandate"]["mandate_id"])
        try:
            _session().platform.next_request(wait=0)   # lets the simulator notice and stop the run
        except PlatformError:
            pass
        return _state()
