"""Customer session controller: glues platform, engine and customer actions.

Used by the UI, the terminal demo and the evaluation. The worker thread
(worker.py) calls `handle()` for each delivered event.
"""
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from datetime import datetime, timezone

from .backtest import backtest
from .compiler import Compiler, Draft
from .data import Pack, load
from .engine import Engine, RevokedError, api_body
from .events import validation_errors
from .platform import Platform, PlatformError, SimPlatform

DEADLINE_MARGIN_S = 1.5


class Session:
    def __init__(self, platform: Platform | None = None, pack: Pack | None = None, engine: Engine | None = None,
                 use_llm: bool = False) -> None:
        from . import llm
        self.pack = pack or load()
        self.platform = platform or SimPlatform(self.pack)
        self.use_llm = use_llm and llm.available()
        fallback = llm.extract_fallback if (self.use_llm and llm.extraction_enabled()) else None
        self.engine = engine or Engine(self.pack, extractor_fallback=fallback)
        self.compiler = Compiler(self.pack)
        self.lock = threading.RLock()
        self.runs: dict[str, dict] = {}          # run_id → run info
        self.mandates: dict[str, dict] = {}      # mandate_id → {scenario_id, customer_id, draft}
        self.log: list[dict] = []                # decisions in arrival order
        self.errors: list[dict] = []
        self.listeners: list = []
        self._pool = ThreadPoolExecutor(max_workers=4)

    # ------------------------------------------------------------ setup
    def customer_for(self, scenario_id: str) -> tuple[str, str]:
        rows = self.pack.scenario_attempts(scenario_id)
        auth = self.pack.authorities[rows[0]["authority_id"]]
        return auth["customer_id"], auth["card_id"]

    def compile(self, instruction: str, scenario_id: str) -> dict:
        from . import llm
        draft = self.compiler.compile(instruction)
        model = None
        if self.use_llm:
            before = len(draft.hard_rules)
            llm.augment(draft, sorted(self.pack.item_categories))
            from .fields import describe
            rev = llm.STATUS.get("last_review") or {"proposed": 0, "dropped": []}
            model = {**{k: llm.STATUS[k] for k in ("provider", "model", "last_error", "last_latency_s")},
                     "proposed": rev["proposed"], "suggested": len(draft.hard_rules) - before,
                     "dropped": [{"text": describe(d["rule"]), "why": d["why"]} for d in rev["dropped"]]}
        _, card = self.customer_for(scenario_id)
        bt = backtest(self.pack, self.engine.profile(card), draft.hard_rules)
        return {"draft": draft.as_dict(), "backtest": bt, "profile": self.engine.profile(card).summary(), "model": model}

    def confirm(self, draft: dict | Draft, scenario_id: str) -> dict:
        payload = draft.api_payload() if isinstance(draft, Draft) else {
            k: draft[k] for k in ("instruction", "hard_rules", "uncertainty_policy", "guidance", "open_questions")}
        d = self.platform.create_mandate(payload)
        m = self.platform.confirm_mandate(d["draft_id"])
        customer, card = self.customer_for(scenario_id)
        with self.lock:
            self.mandates[m["mandate_id"]] = {"scenario_id": scenario_id, "customer_id": customer, "card_id": card,
                                              "draft": draft if isinstance(draft, dict) else draft.as_dict()}
        return m

    def start(self, scenario_id: str, mandate_id: str) -> dict:
        info = self.platform.start_run(scenario_id, mandate_id)
        with self.lock:
            self.runs[info["run_id"]] = {**info, "started": time.time()}
        return info

    # ------------------------------------------------------------ decisions
    def handle(self, envelope: dict) -> dict:
        event = envelope["data"]
        run_id = envelope.get("run_id")
        errs = validation_errors(event)
        if errs:
            self.errors.append({"authorization_id": envelope.get("authorization_id"), "errors": errs[:5]})
        deadline = _parse(event.get("deadline_at"))
        budget = (deadline - datetime.now(timezone.utc)).total_seconds() - DEADLINE_MARGIN_S if deadline else 6.5
        fut = self._pool.submit(self.engine.decide, event, run_id)
        try:
            result = fut.result(timeout=max(budget, 0.1))
        except FutureTimeout:
            result = self._fallback(event, run_id, "deadline_fallback")
        except Exception as exc:  # never leave a purchase without an answer
            result = self._fallback(event, run_id, "rule_not_understood", repr(exc))
        if not result.get("redelivery"):
            try:
                ack = self.platform.post_decision(result["authorization_id"], api_body(result))
                result["platform_ack"] = ack
            except PlatformError as exc:
                result["platform_error"] = exc.message
                self.errors.append({"authorization_id": result["authorization_id"], "error": exc.message})
            with self.lock:
                self.log.append(result)
        for fn in self.listeners:
            fn(result)
        return result

    def _fallback(self, event: dict, run_id: str | None, code: str, detail: str = "") -> dict:
        a, m = event["authorization"], event["mandate"]
        decision = "decline" if m.get("uncertainty_policy") == "decline" else "step_up"
        msg = ("We couldn't finish checking this purchase in time, so we're asking you." if decision == "step_up"
               else "We couldn't finish checking this purchase in time, so we declined it as you asked.")
        return {"authorization_id": a["authorization_id"], "source_authorization_id": a.get("source_authorization_id"),
                "decision": decision, "reason_codes": [code], "customer_message": msg, "headline": msg,
                "evidence": [{"fact": "engine", "status": "unknown", "value": detail, "provenance": "engine",
                              "explanation": msg}], "engine_version": "leash-fallback", "checks": [], "facts": [],
                "security_flags": [], "merchant": a["merchant"], "amount_chf": a["billing_amount_chf"],
                "timestamp": a["timestamp"], "items": a["items"], "run_id": run_id, "mandate_id": m["mandate_id"]}

    # ------------------------------------------------------------ customer actions
    def resolve(self, run_id: str, authorization_id: str, decision: str) -> dict:
        res = self.engine.resolve(run_id, authorization_id, decision)
        try:
            self.platform.resolve(authorization_id, {k: res[k] for k in ("decision", "customer_message", "evidence")})
        except PlatformError as exc:
            res["platform_error"] = exc.message
        return res

    def set_merchant_flag(self, customer_id: str, merchant_id: str, mode: str) -> dict:
        rule = self.engine.set_merchant_flag(customer_id, merchant_id, mode)
        persisted = []
        if rule:
            for mid, info in self.mandates.items():
                if info["customer_id"] != customer_id:
                    continue
                try:
                    cur = self.platform.get_mandate(mid)
                    if cur["status"] == "active":
                        self.platform.patch_mandate(mid, {"hard_rules": cur["hard_rules"] + [rule]})
                        persisted.append(mid)
                except PlatformError as exc:
                    self.errors.append({"mandate_id": mid, "error": exc.message})
        return {"mode": mode, "persisted_to": persisted, "rule": rule}

    def revoke(self, mandate_id: str) -> dict:
        info = self.mandates.get(mandate_id) or {}
        self.engine.revoke(info.get("customer_id", "?"), mandate_id)
        try:
            return self.platform.revoke_mandate(mandate_id)
        except PlatformError as exc:
            return {"error": exc.message}

    def tighten(self, mandate_id: str, rule: dict | None = None, uncertainty: str | None = None) -> dict:
        info = self.mandates[mandate_id]
        self.engine.tighten(info["customer_id"], mandate_id, rule=rule, uncertainty=uncertainty)
        cur = self.platform.get_mandate(mandate_id)
        patch = {}
        if rule:
            patch["hard_rules"] = cur["hard_rules"] + [rule]
        if uncertainty:
            patch["uncertainty_policy"] = uncertainty
        return self.platform.patch_mandate(mandate_id, patch)

    # ------------------------------------------------------------ offline driver
    def drive(self, run_id: str, customer=None, max_idle: float = 2.0) -> list[dict]:
        """Synchronously process a simulator run. `customer(result) -> 'approve'|'decline'|None`
        answers each step-up (None = leave it waiting)."""
        out = []
        idle_since = time.monotonic()
        while True:
            status = self.platform.run_status(run_id)
            if status["done"] and not status["pending_human"]:
                break
            env = self.platform.next_request(wait=0)
            if env is None:
                if status["released"] >= status["event_count"] or status["stopped"]:
                    break
                if time.monotonic() - idle_since > max_idle:
                    break
                time.sleep(0.01)
                continue
            idle_since = time.monotonic()
            res = self.handle(env)
            out.append(res)
            if res["decision"] == "step_up" and customer:
                answer = customer(res)
                if answer:
                    try:
                        res["resolution"] = self.resolve(run_id, res["authorization_id"], answer)
                    except RevokedError as exc:
                        res["resolution"] = {"error": str(exc)}
        return out


def _parse(value):
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))
