"""Platform adapters.

HttpPlatform talks to the hosted challenge API. SimPlatform is an in-process
simulator of the documented contract, so the worker, UI, demo and eval run
end-to-end without a team key:

  * drafts → confirm → active mandate; PATCH is append-only for hard_rules and
    uncertainty_policy can only move to "decline"; DELETE revokes;
  * a run freezes a snapshot of the mandate; events are queued one at a time
    in replay_order with an 8 s real-clock deadline set at queueing time;
  * a step_up waits up to 120 s for /resolve; a second automated decision is
    rejected (409);
  * a revoked mandate stops further events from being queued.

Assumptions where the docs are silent are marked SIM-ASSUMPTION.
"""
from __future__ import annotations

import os
import threading
import time
import uuid
from datetime import timedelta
from typing import Protocol

import httpx

from .data import Pack, load
from .events import assemble_event, authorization_from_attempt, iso, mandate_snapshot, new_live_id, parse_ts, utcnow


class PlatformError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"{status}: {message}")
        self.status = status
        self.message = message


class Platform(Protocol):
    def create_mandate(self, payload: dict) -> dict: ...
    def confirm_mandate(self, draft_id: str) -> dict: ...
    def get_mandate(self, mandate_id: str) -> dict: ...
    def patch_mandate(self, mandate_id: str, patch: dict) -> dict: ...
    def revoke_mandate(self, mandate_id: str) -> dict: ...
    def start_run(self, scenario_id: str, mandate_id: str) -> dict: ...
    def run_status(self, run_id: str) -> dict: ...
    def next_request(self, wait: int = 25) -> dict | None: ...
    def post_decision(self, authorization_id: str, body: dict) -> dict: ...
    def resolve(self, authorization_id: str, body: dict) -> dict: ...
    def bootstrap(self) -> dict: ...


# ----------------------------------------------------------------- live API
class HttpPlatform:
    def __init__(self, base_url: str | None = None, api_key: str | None = None, timeout: float = 30.0) -> None:
        self.base = (base_url or os.environ["LEASH_BASE_URL"]).rstrip("/")
        self.client = httpx.Client(
            base_url=self.base, timeout=timeout,
            headers={"Authorization": f"Bearer {api_key or os.environ['TEAM_API_KEY']}", "Content-Type": "application/json"},
        )

    def _req(self, method: str, path: str, **kw) -> dict | None:
        r = self.client.request(method, path, **kw)
        if r.status_code == 204:
            return None
        if r.status_code >= 400:
            try:
                msg = r.json().get("error", r.text)
            except ValueError:
                msg = r.text
            raise PlatformError(r.status_code, str(msg))
        return r.json() if r.content else {}

    def bootstrap(self) -> dict:
        return self._req("GET", "/v1/bootstrap")

    def create_mandate(self, payload: dict) -> dict:
        return self._req("POST", "/v1/mandates", json=payload)

    def confirm_mandate(self, draft_id: str) -> dict:
        return self._req("POST", f"/v1/mandates/{draft_id}/confirm", json={"confirmed": True})

    def get_mandate(self, mandate_id: str) -> dict:
        return self._req("GET", f"/v1/mandates/{mandate_id}")

    def patch_mandate(self, mandate_id: str, patch: dict) -> dict:
        return self._req("PATCH", f"/v1/mandates/{mandate_id}", json=patch)

    def revoke_mandate(self, mandate_id: str) -> dict:
        return self._req("DELETE", f"/v1/mandates/{mandate_id}") or {}

    def start_run(self, scenario_id: str, mandate_id: str) -> dict:
        return self._req("POST", "/v1/scenario-runs", json={"scenario_id": scenario_id, "mandate_id": mandate_id})

    def run_status(self, run_id: str) -> dict:
        return self._req("GET", f"/v1/scenario-runs/{run_id}")

    def next_request(self, wait: int = 25) -> dict | None:
        return self._req("GET", f"/v1/decision-requests/next?wait={wait}", timeout=wait + 10)

    def post_decision(self, authorization_id: str, body: dict) -> dict:
        return self._req("POST", f"/v1/authorizations/{authorization_id}/decision", json=body)

    def resolve(self, authorization_id: str, body: dict) -> dict:
        return self._req("POST", f"/v1/authorizations/{authorization_id}/resolve", json=body)


# ---------------------------------------------------------------- simulator
class SimPlatform:
    DECISION_DEADLINE_S = 8.0
    HUMAN_WINDOW_S = 120.0

    def __init__(self, pack: Pack | None = None, pace_s: float = 0.0, human_window_s: float | None = None) -> None:
        self.pack = pack or load()
        self.pace_s = pace_s
        self.human_window_s = human_window_s or self.HUMAN_WINDOW_S
        self.lock = threading.RLock()
        self.cond = threading.Condition(self.lock)
        self.drafts: dict[str, dict] = {}
        self.mandates: dict[str, dict] = {}
        self.runs: dict[str, dict] = {}
        self.auths: dict[str, dict] = {}
        self.feed: list[dict] = []

    def bootstrap(self) -> dict:
        return {"mode": "simulator", "decision_deadline_seconds": self.DECISION_DEADLINE_S,
                "human_window_seconds": self.human_window_s, "scenarios": sorted(self.pack.scenarios)}

    # mandates -------------------------------------------------------------
    def create_mandate(self, payload: dict) -> dict:
        for k in ("instruction", "hard_rules", "uncertainty_policy"):
            if k not in payload:
                raise PlatformError(422, f"missing {k}")
        if payload["uncertainty_policy"] not in ("ask", "decline", "approve"):
            raise PlatformError(422, "bad uncertainty_policy")
        draft_id = "DR" + uuid.uuid4().hex[:10].upper()
        with self.lock:
            self.drafts[draft_id] = {**payload, "draft_id": draft_id, "status": "draft",
                                     "guidance": payload.get("guidance", []), "open_questions": payload.get("open_questions", [])}
        return dict(self.drafts[draft_id])

    def confirm_mandate(self, draft_id: str) -> dict:
        with self.lock:
            d = self.drafts.pop(draft_id, None)
            if d is None:
                raise PlatformError(404, "draft not found")
            mid = "TM" + uuid.uuid4().hex[:10].upper()
            self.mandates[mid] = {**d, "mandate_id": mid, "status": "active", "confirmed_at": iso(utcnow())}
            self.mandates[mid].pop("draft_id", None)
            return dict(self.mandates[mid])

    def get_mandate(self, mandate_id: str) -> dict:
        with self.lock:
            if mandate_id not in self.mandates:
                raise PlatformError(404, "mandate not found")
            return dict(self.mandates[mandate_id])

    def patch_mandate(self, mandate_id: str, patch: dict) -> dict:
        with self.lock:
            m = self.mandates.get(mandate_id)
            if m is None:
                raise PlatformError(404, "mandate not found")
            if m["status"] != "active":
                raise PlatformError(409, f"mandate is {m['status']}")
            if "hard_rules" in patch:
                old, new = m["hard_rules"], patch["hard_rules"]
                if len(new) < len(old) or new[: len(old)] != old:
                    raise PlatformError(422, "hard_rules are append-only: keep every existing rule unchanged")
            if "uncertainty_policy" in patch and patch["uncertainty_policy"] != m["uncertainty_policy"]:
                if patch["uncertainty_policy"] != "decline":
                    raise PlatformError(422, "uncertainty_policy can only change to 'decline'")
            for k in ("hard_rules", "uncertainty_policy", "guidance", "open_questions"):
                if k in patch:
                    m[k] = patch[k]
            return dict(m)

    def revoke_mandate(self, mandate_id: str) -> dict:
        with self.cond:
            m = self.mandates.get(mandate_id)
            if m is None:
                raise PlatformError(404, "mandate not found")
            m["status"] = "revoked"
            self.cond.notify_all()
            return dict(m)

    # runs -----------------------------------------------------------------
    def start_run(self, scenario_id: str, mandate_id: str) -> dict:
        with self.cond:
            m = self.mandates.get(mandate_id)
            if m is None or m["status"] != "active":
                raise PlatformError(409, "mandate is not active")
            rows = self.pack.scenario_attempts(scenario_id)
            if not rows:
                raise PlatformError(404, "unknown scenario")
            auth = self.pack.authorities[rows[0]["authority_id"]]
            run_id = "RUN" + uuid.uuid4().hex[:10].upper()
            profile_id = "PROFILE_" + auth["authority_id"]
            snapshot = mandate_snapshot(m, customer_id=auth["customer_id"], card_id=auth["card_id"], profile_id=profile_id)
            live = {r["authorization_id"]: new_live_id() for r in rows}
            self.runs[run_id] = {
                "run_id": run_id, "scenario_id": scenario_id, "mandate_id": mandate_id, "snapshot": snapshot,
                "customer_id": auth["customer_id"], "card_id": auth["card_id"], "profile_id": profile_id,
                "rows": rows, "live": live, "released": 0, "last_release": 0.0, "stopped": None,
                "started_at": iso(utcnow()),
            }
            self.cond.notify_all()
            return self._run_info(run_id)

    def inject_run(self, scenario_id: str, mandate_id: str, rows: list[dict]) -> dict:
        """Start a run over custom (mutated) rows. Used by the mutation suite.
        Rows may carry `_items` and `_merchant_override` (see events.py)."""
        info = self.start_run(scenario_id, mandate_id)
        with self.lock:
            run = self.runs[info["run_id"]]
            run["rows"] = rows
            run["live"] = {r["authorization_id"]: new_live_id() for r in rows}
        return self._run_info(info["run_id"])

    def _run_info(self, run_id: str) -> dict:
        run = self.runs[run_id]
        ids = [run["live"][r["authorization_id"]] for r in run["rows"]]
        statuses = [self.auths[i]["status"] for i in ids if i in self.auths]
        return {
            "run_id": run_id, "scenario_id": run["scenario_id"], "mandate_id": run["mandate_id"],
            "customer_id": run["customer_id"], "card_id": run["card_id"], "profile_id": run["profile_id"],
            "event_count": len(run["rows"]), "released": run["released"],
            "decided": sum(1 for s in statuses if s != "queued"),
            "pending_human": sum(1 for s in statuses if s == "pending"),
            "stopped": run["stopped"],
            "done": run["stopped"] is not None or (run["released"] == len(run["rows"]) and all(s not in ("queued", "pending") for s in statuses)),
        }

    def run_status(self, run_id: str) -> dict:
        with self.lock:
            self._expire()
            return self._run_info(run_id)

    def _context(self, run: dict, ts) -> dict:
        window = timedelta(minutes=10)
        recent, approved = [], 0.0
        for r in run["rows"]:
            lid = run["live"][r["authorization_id"]]
            a = self.auths.get(lid)
            if not a:
                continue
            if a["status"] == "approved":
                approved += a["billing_amount_chf"]  # SIM-ASSUMPTION: whole run; the API's period is not documented
            rts = parse_ts(r["timestamp"])
            if ts - window <= rts < ts:
                st = {"queued": "pending", "expired": "declined", "timed_out": "declined"}.get(a["status"], a["status"])
                recent.append({"authorization_id": lid, "timestamp": r["timestamp"], "merchant_id": r["merchant_id"],
                               "billing_amount_chf": a["billing_amount_chf"], "status": st})
        return {"approved_spend_in_period_chf": round(approved, 2), "recent_authorizations": recent}

    def _release(self) -> dict | None:
        """Queue the next event of any run whose previous event has been decided."""
        now = time.monotonic()
        for run in self.runs.values():
            if run["stopped"] or run["released"] >= len(run["rows"]):
                continue
            if self.mandates[run["mandate_id"]]["status"] != "active":
                run["stopped"] = "mandate_revoked"   # platform rejects revoked mandates before queueing
                continue
            if run["released"]:
                prev = run["live"][run["rows"][run["released"] - 1]["authorization_id"]]
                if self.auths[prev]["status"] == "queued":
                    continue
            if now - run["last_release"] < self.pace_s:
                continue
            row = run["rows"][run["released"]]
            lid = run["live"][row["authorization_id"]]
            rel = row["related_authorization_id"]
            authz = authorization_from_attempt(
                self.pack, row, live_id=lid, related_live_id=run["live"].get(rel) if rel else None,
                mandate_id=run["mandate_id"], profile_id=run["profile_id"])
            event = assemble_event(authz, dict(run["snapshot"]), deadline_seconds=self.DECISION_DEADLINE_S,
                                   **self._context(run, parse_ts(row["timestamp"])))
            self.auths[lid] = {"authorization_id": lid, "run_id": run["run_id"], "source": row["authorization_id"],
                               "status": "queued", "billing_amount_chf": row["billing_amount_chf"], "event": event,
                               "deadline": time.monotonic() + self.DECISION_DEADLINE_S, "decision": None,
                               "resolution": None, "delivered": 0}
            run["released"] += 1
            run["last_release"] = now
            return self.auths[lid]
        return None

    def _expire(self) -> None:
        now = time.monotonic()
        for a in self.auths.values():
            if a["status"] == "queued" and a["delivered"] and now > a["deadline"] + 2:
                a["status"] = "timed_out"  # SIM-ASSUMPTION: a missed deadline counts as declined
            elif a["status"] == "pending" and now > a["human_deadline"]:
                a["status"] = "expired"    # SIM-ASSUMPTION: no human answer → declined

    def next_request(self, wait: int = 25) -> dict | None:
        end = time.monotonic() + wait
        with self.cond:
            while True:
                self._expire()
                # redeliver anything queued but undecided (at-least-once delivery)
                for a in self.auths.values():
                    if a["status"] == "queued" and a["delivered"] == 0:
                        return self._envelope(a)
                a = self._release()
                if a:
                    return self._envelope(a)
                remaining = end - time.monotonic()
                if remaining <= 0:
                    return None
                self.cond.wait(timeout=min(remaining, max(self.pace_s / 4, 0.05)))

    def _envelope(self, a: dict) -> dict:
        a["delivered"] += 1
        return {"run_id": a["run_id"], "event_id": "evt_" + uuid.uuid4().hex[:10], "type": "authorization.request",
                "authorization_id": a["authorization_id"], "status": "queued", "occurred_at": iso(utcnow()),
                "data": a["event"]}

    def redeliver(self, authorization_id: str) -> dict:
        """Test hook: deliver the same event again."""
        with self.lock:
            return self._envelope(self.auths[authorization_id])

    def post_decision(self, authorization_id: str, body: dict) -> dict:
        with self.cond:
            a = self.auths.get(authorization_id)
            if a is None:
                raise PlatformError(404, "authorization not found")
            if body.get("authorization_id") != authorization_id:
                raise PlatformError(422, "authorization_id in body must match the URL")
            if a["decision"] is not None:
                raise PlatformError(409, "a decision was already recorded")
            if body.get("decision") not in ("approve", "decline", "step_up"):
                raise PlatformError(422, "bad decision")
            late = time.monotonic() > a["deadline"]
            a["decision"] = body
            a["late"] = late
            a["status"] = {"approve": "approved", "decline": "declined", "step_up": "pending"}[body["decision"]]
            if a["status"] == "pending":
                a["human_deadline"] = time.monotonic() + self.human_window_s
            self.feed.append({"at": iso(utcnow()), "type": "decision", "authorization_id": authorization_id,
                              "decision": body["decision"], "late": late})
            self.cond.notify_all()
            return {"authorization_id": authorization_id, "status": a["status"], "late": late}

    def resolve(self, authorization_id: str, body: dict) -> dict:
        with self.cond:
            self._expire()
            a = self.auths.get(authorization_id)
            if a is None:
                raise PlatformError(404, "authorization not found")
            if a["status"] != "pending":
                raise PlatformError(409, f"authorization is {a['status']}, not waiting for the customer")
            if body.get("decision") not in ("approve", "decline"):
                raise PlatformError(422, "bad decision")
            a["resolution"] = body
            a["status"] = "approved" if body["decision"] == "approve" else "declined"
            self.feed.append({"at": iso(utcnow()), "type": "resolution", "authorization_id": authorization_id,
                              "decision": body["decision"]})
            self.cond.notify_all()
            return {"authorization_id": authorization_id, "status": a["status"]}

    def authorizations(self, run_id: str | None = None) -> list[dict]:
        with self.lock:
            self._expire()
            return [{k: v for k, v in a.items() if k != "event"} for a in self.auths.values()
                    if run_id is None or a["run_id"] == run_id]
