"""(J) Customer UI backend. Decoupled from the engine: it only calls Session.

Offline by default (SimPlatform, paced so a person can follow along). With
TEAM_API_KEY and LEASH_BASE_URL set it talks to the hosted API instead.
"""
from __future__ import annotations

import os
import threading
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from ..engine import RevokedError
from ..fields import describe
from ..platform import HttpPlatform, PlatformError, SimPlatform
from ..session import Session
from ..worker import Worker

STATIC = Path(__file__).parent / "static"
app = FastAPI(title="Agent on a Leash")
_lock = threading.RLock()
_state: dict = {}


def _new_session() -> None:
    if _state.get("worker"):
        _state["worker"].stop()
    live = bool(os.environ.get("TEAM_API_KEY") and os.environ.get("LEASH_BASE_URL"))
    platform = HttpPlatform() if live else SimPlatform(pace_s=float(os.environ.get("LEASH_PACE", "1.2")))
    s = Session(platform=platform, use_llm=True)
    w = Worker(s, wait=25 if live else 1)
    w.start()
    _state.update(session=s, worker=w, live=live, draft=None, scenario=None, mandate_id=None, run_id=None,
                  compiled=None, started_at=None, ap2_open=None)


_new_session()


def S() -> Session:
    return _state["session"]


class CompileIn(BaseModel):
    scenario_id: str
    instruction: str


class ResolveIn(BaseModel):
    authorization_id: str
    decision: str


class FlagIn(BaseModel):
    merchant_id: str
    mode: str


class Ap2In(BaseModel):
    enabled: bool


class TightenIn(BaseModel):
    max_per_order_chf: float | None = None
    uncertainty: str | None = None


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/scenarios")
def scenarios():
    s = S()
    out = []
    for sid, sc in sorted(s.pack.scenarios.items()):
        cust, card = s.customer_for(sid)
        out.append({"scenario_id": sid, "name": sc["scenario_name"], "instruction": sc["cardholder_instruction"],
                    "persona": s.pack.customers[cust]["persona_name"], "card_id": card, "events": int(sc["event_count"])})
    return {"scenarios": out, "live": _state["live"], "ap2": bool(s.keyring)}


@app.post("/api/ap2")
def ap2(body: Ap2In):
    """Simulator only: shops sign carts, Viseca one signs the agent's permission (AP2)."""
    with _lock:
        if _state["live"]:
            raise HTTPException(409, "The hosted API does not carry AP2 mandates.")
        S().enable_ap2(body.enabled)
        return {"ap2": bool(S().keyring)}


@app.post("/api/compile")
def compile_(body: CompileIn):
    with _lock:
        out = S().compile(body.instruction, body.scenario_id)
        _state.update(compiled=out, scenario=body.scenario_id)
        return out


@app.post("/api/confirm")
def confirm():
    with _lock:
        if not _state.get("compiled"):
            raise HTTPException(400, "Compile an instruction first.")
        m = S().confirm(_state["compiled"]["draft"], _state["scenario"])
        _state["mandate_id"] = m["mandate_id"]
        _state["ap2_open"] = m.get("ap2")
        return m


@app.post("/api/start")
def start():
    with _lock:
        if not _state.get("mandate_id"):
            raise HTTPException(400, "Confirm the permissions first.")
        try:
            run = S().start(_state["scenario"], _state["mandate_id"])
        except PlatformError as exc:
            raise HTTPException(409, exc.message)
        _state.update(run_id=run["run_id"], started_at=time.time())
        return run


@app.post("/api/resolve")
def resolve(body: ResolveIn):
    try:
        return S().resolve(_state["run_id"], body.authorization_id, body.decision)
    except RevokedError as exc:
        raise HTTPException(409, str(exc))
    except ValueError as exc:
        raise HTTPException(409, str(exc))


@app.post("/api/merchant-flag")
def merchant_flag(body: FlagIn):
    if body.mode not in ("ask", "block", "remove"):
        raise HTTPException(422, "mode must be ask, block or remove")
    cust = S().customer_for(_state["scenario"])[0]
    return S().set_merchant_flag(cust, body.merchant_id, body.mode)


@app.post("/api/tighten")
def tighten(body: TightenIn):
    if not _state.get("mandate_id"):
        raise HTTPException(400, "No active permissions.")
    rule = None
    if body.max_per_order_chf is not None:
        rule = {"field": "authorization.billing_amount_chf", "operator": "<=", "value": body.max_per_order_chf,
                "currency": "CHF", "scope": "purchase"}
    try:
        return S().tighten(_state["mandate_id"], rule=rule, uncertainty=body.uncertainty)
    except (PlatformError, ValueError) as exc:
        raise HTTPException(409, getattr(exc, "message", str(exc)))


@app.post("/api/revoke")
def revoke():
    if not _state.get("mandate_id"):
        raise HTTPException(400, "No active permissions.")
    return S().revoke(_state["mandate_id"])


@app.post("/api/reset")
def reset():
    with _lock:
        _new_session()
    return {"ok": True}


def _human_seconds_left(live_id: str) -> float | None:
    p = S().platform
    if isinstance(p, SimPlatform):
        a = p.auths.get(live_id)
        if a and a.get("human_deadline"):
            return max(0.0, a["human_deadline"] - time.monotonic())
    return None


@app.get("/api/state")
def state():
    s = S()
    run_id = _state.get("run_id")
    ledger = s.engine.store.runs.get(run_id) if run_id else None
    # keep the engine in step with the simulator's human window
    if ledger and isinstance(s.platform, SimPlatform):
        for a in s.platform.authorizations(run_id):
            if a["status"] == "expired":
                s.engine.expire(run_id, a["authorization_id"])
    feed = []
    for r in s.log:
        if r.get("run_id") != run_id:
            continue
        rec = ledger.get(r["authorization_id"]) if ledger else None
        feed.append({
            "authorization_id": r["authorization_id"], "source": r["source_authorization_id"], "timestamp": r["timestamp"],
            "merchant": r["merchant"], "amount_chf": r["amount_chf"], "items": r["items"], "decision": r["decision"],
            "status": rec.status if rec else None, "resolved_by": rec.resolved_by if rec else None,
            "headline": r["headline"], "message": r["customer_message"], "reason_codes": r["reason_codes"],
            "checks": r["checks"], "security_flags": r.get("security_flags", []),
            "ap2_receipt": (r.get("ap2_receipt") or {}).get("payload"),
            "ap2_resolution": ((rec.result.get("ap2_resolution") or {}).get("receipt") or {}).get("payload") if rec else None,
            "ap2_decoded": (r.get("ap2") or {}).get("decoded"),
            "seconds_left": _human_seconds_left(r["authorization_id"]) if rec and rec.status == "pending" else None,
        })
    cust = s.customer_for(_state["scenario"])[0] if _state.get("scenario") else None
    ctl = s.engine.store.controls.get(cust) if cust else None
    mandate = None
    if _state.get("mandate_id"):
        try:
            mandate = s.platform.get_mandate(_state["mandate_id"])
            mandate["rules_text"] = [describe(r) for r in mandate["hard_rules"]]
        except PlatformError:
            pass
    return {
        "live": _state["live"], "scenario": _state.get("scenario"), "mandate": mandate,
        "ap2_enabled": bool(s.keyring), "ap2": _state.get("ap2_open"),
        "revoked": bool(ctl and _state.get("mandate_id") in ctl.revoked_mandates),
        "run": s.platform.run_status(run_id) if run_id else None,
        "feed": feed,
        "alerts": ctl.alerts if ctl else [],
        "flags": list(ctl.merchant_flags.values()) if ctl else [],
        "audit": (ctl.audit[-12:] if ctl else []),
        "engine_extra_rules": (ctl.extra_rules.get(_state.get("mandate_id"), []) if ctl else []),
        "uncertainty_override": (ctl.uncertainty_override.get(_state.get("mandate_id")) if ctl else None),
        "worker_error": _state["worker"].last_error, "errors": s.errors[-5:],
    }
