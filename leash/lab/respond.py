"""Lab 5: the customer's response and controls. A story runs until a purchase asks the customer; then the
customer answers on the phone, flags or blocks the shop, tightens, or withdraws permission, and the agent's
next purchases show what each choice changed."""
from __future__ import annotations

import copy
import threading
from datetime import timedelta

from fastapi import HTTPException
from pydantic import BaseModel

from ..data import load
from ..engine import RevokedError
from ..events import iso, new_live_id, parse_ts
from ..platform import PlatformError, SimPlatform
from ..session import Session
from .common import make_app

app = make_app("Lab 5 · Customer response", "respond.html")
PACK = load()
LOCK = threading.RLock()
ST: dict = {}


class StartIn(BaseModel):
    scenario_id: str = "SCEN0004"


class ResolveIn(BaseModel):
    authorization_id: str
    decision: str


class FlagIn(BaseModel):
    merchant_id: str
    mode: str


def _s() -> Session:
    if "session" not in ST:
        raise HTTPException(400, "Start a story first.")
    return ST["session"]


def _send() -> dict:
    """The agent sends the next queued purchase; the wallet decides it."""
    s = _s()
    env = s.platform.next_request(wait=0)
    if env is None:
        run = s.platform.runs[ST["run_id"]]
        raise HTTPException(409, "The platform queues nothing more: permission was withdrawn." if run["stopped"]
                            else "No purchase left in this story. Try 'the same shop again'.")
    r = s.handle(env)
    ST["events"].append({"kind": "purchase", "authorization_id": r["authorization_id"], "source": r["source_authorization_id"],
                         "merchant": r["merchant"]["merchant_name"], "merchant_id": r["merchant"]["merchant_id"],
                         "amount_chf": r["amount_chf"], "decision": r["decision"], "headline": r["headline"],
                         "reasons": [c["text"] for c in r["checks"] if c["status"] != "pass"],
                         "controls": [c["text"] for c in r["checks"] if c["tier"] == "your-controls"],
                         "flags_created": [f["reason"] for f in r.get("flags_created", [])],
                         "notified": r["decision"] == "step_up"})
    return r


@app.post("/api/start")
def start(body: StartIn):
    with LOCK:
        if body.scenario_id not in PACK.scenarios:
            raise HTTPException(404, "Unknown story.")
        s = Session(platform=SimPlatform())
        comp = s.compile(PACK.scenarios[body.scenario_id]["cardholder_instruction"], body.scenario_id)
        m = s.confirm(comp["draft"], body.scenario_id)
        rows = [copy.deepcopy(r) for r in PACK.scenario_attempts(body.scenario_id)]
        info = s.platform.inject_run(body.scenario_id, m["mandate_id"], rows)
        s.runs[info["run_id"]] = info
        cust, _ = s.customer_for(body.scenario_id)
        ST.clear()
        ST.update(session=s, run_id=info["run_id"], mandate_id=m["mandate_id"], customer_id=cust, scenario_id=body.scenario_id,
                  events=[], retries=0)
        for _ in range(len(rows)):                   # run until something needs the customer
            if _send()["decision"] == "step_up":
                break
        return state()


def _stories() -> list[dict]:
    return [{"scenario_id": k, "story": v["scenario_name"],
             "persona": PACK.customers[PACK.authorities[PACK.scenario_attempts(k)[0]["authority_id"]]["customer_id"]]["persona_name"]}
            for k, v in sorted(PACK.scenarios.items())]


@app.get("/api/state")
def state():
    with LOCK:
        if "session" not in ST:
            return {"started": False, "stories": _stories()}
        s = _s()
        ledger = s.engine.store.runs.get(ST["run_id"])
        ctl = s.engine.store.customer(ST["customer_id"])
        for e in ST["events"]:
            if e["kind"] == "purchase":
                rec = ledger.get(e["authorization_id"]) if ledger else None
                e["status"] = rec.status if rec else None
                e["resolved_by"] = rec.resolved_by if rec else None
                if rec and e["decision"] == "step_up":
                    e["shop_text"] = [l["item_details"] for l in rec.result["items"]]
        mandate = s.platform.get_mandate(ST["mandate_id"])
        from ..fields import describe
        run = s.platform.runs[ST["run_id"]]
        nxt = run["rows"][run["released"]] if run["released"] < len(run["rows"]) and not run["stopped"] else None
        return {
            "started": True, "stories": _stories(), "scenario_id": ST["scenario_id"], "persona": PACK.customers[ST["customer_id"]]["persona_name"],
            "events": ST["events"], "flags": list(ctl.merchant_flags.values()), "alerts": ctl.alerts[-6:],
            "audit": ctl.audit[-14:], "revoked": ST["mandate_id"] in ctl.revoked_mandates,
            "uncertainty": ctl.uncertainty_override.get(ST["mandate_id"], mandate["uncertainty_policy"]),
            "mandate": {"mandate_id": mandate["mandate_id"], "status": mandate["status"],
                        "rules": [describe(r) for r in mandate["hard_rules"]], "extra": [describe(r) for r in ctl.extra_rules.get(ST["mandate_id"], [])]},
            "next": {"source": nxt["authorization_id"], "merchant": PACK.merchants.get(nxt["merchant_id"], {}).get("merchant_name", nxt["merchant_id"]),
                     "amount": nxt["amount"], "currency": nxt["currency"]} if nxt else None,
            "last_merchant": next((e["merchant"] for e in reversed(ST["events"]) if e["kind"] == "purchase"), None),
        }


@app.post("/api/resolve")
def resolve(body: ResolveIn):
    with LOCK:
        try:
            _s().resolve(ST["run_id"], body.authorization_id, body.decision)
        except (RevokedError, ValueError) as exc:
            raise HTTPException(409, str(exc))
        ST["events"].append({"kind": "action", "text": f"You {'approved' if body.decision == 'approve' else 'declined'} the purchase on the phone."})
        return state()


@app.post("/api/flag")
def flag(body: FlagIn):
    with LOCK:
        if body.mode not in ("ask", "block", "remove"):
            raise HTTPException(422, "mode must be ask, block or remove")
        out = _s().set_merchant_flag(ST["customer_id"], body.merchant_id, body.mode)
        name = PACK.merchants.get(body.merchant_id, {}).get("merchant_name", body.merchant_id)
        ST["events"].append({"kind": "action", "text": {
            "ask": f"You kept 'ask every time' for {name}.",
            "block": f"You blocked {name}; the block was also added to your permissions ({len(out['persisted_to'])} mandate(s)).",
            "remove": f"You said {name} is not a concern: its purchases are treated normally again."}[body.mode]})
        return state()


@app.post("/api/tighten")
def tighten():
    with LOCK:
        try:
            _s().tighten(ST["mandate_id"], uncertainty="decline")
        except (PlatformError, ValueError) as exc:
            raise HTTPException(409, getattr(exc, "message", str(exc)))
        ST["events"].append({"kind": "action", "text": "You chose: when unsure, decline instead of asking."})
        return state()


@app.post("/api/revoke")
def revoke():
    with LOCK:
        _s().revoke(ST["mandate_id"])
        ST["events"].append({"kind": "action", "text": "You withdrew the agent's permission."})
        return state()


@app.post("/api/next")
def next_purchase():
    with LOCK:
        _send()
        return state()


@app.post("/api/retry")
def retry():
    """The agent tries the last shop again, two days later, with its last ordinary (approved) basket there."""
    with LOCK:
        s = _s()
        run = s.platform.runs[ST["run_id"]]
        if run["stopped"]:
            raise HTTPException(409, "The platform queues nothing more: permission was withdrawn.")
        buys = [e for e in ST["events"] if e["kind"] == "purchase"]
        if not buys:
            raise HTTPException(409, "Nothing to retry yet.")
        shop = buys[-1]["merchant_id"]
        # an ordinary basket at that shop (the last approved one), so the retry shows what the customer's choice changed
        tmpl = next((e for e in reversed(buys) if e["merchant_id"] == shop and e["decision"] == "approve"), buys[-1])
        base = next(r for r in run["rows"] if r["authorization_id"] == tmpl["source"])
        ST["retries"] += 1
        row = copy.deepcopy(base)
        row["authorization_id"] = f"AUR{ST['retries']:03d}"
        row["timestamp"] = iso(parse_ts(base["timestamp"]) + timedelta(days=2 * ST["retries"])).replace(".000000", "")
        row["related_authorization_id"], row["related_authorization_status"] = None, None
        if not row.get("_items"):
            row["_items"] = copy.deepcopy(PACK.attempt_items.get(base["authorization_id"], []))
        run["rows"].insert(run["released"], row)
        run["live"][row["authorization_id"]] = new_live_id()
        _send()
        return state()
