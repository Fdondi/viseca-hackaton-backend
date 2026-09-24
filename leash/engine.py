"""(H) Decision engine: evaluate every rule three-valued, then combine.

    any hard rule fails                  → decline
    any unknown (incl. any risk signal)  → the customer's uncertainty_policy
                                           (ask → step_up; security signals are never approved away)
    otherwise                            → approve

The decision path is deterministic and model-free.
"""
from __future__ import annotations

import time
from datetime import datetime

from . import __version__
from .data import Pack, load
from .events import parse_ts
from .explain import customer_message, headline
from .fields import FAIL, PASS, SAFETY_NET, UNK, Check, Ctx, chf, evaluate
from .injection import detect
from .ledger import Store, record_from_event
from .lookalike import find_lookalike
from .profile import CardProfile, build_profile
from .wall import extract

ENGINE_VERSION = f"leash-{__version__}"


class RevokedError(PermissionError):
    pass


class Engine:
    def __init__(self, pack: Pack | None = None, store: Store | None = None, extractor_fallback=None) -> None:
        self.pack = pack or load()
        self.store = store or Store()
        self._profiles: dict[str, CardProfile] = {}
        self.extractor_fallback = extractor_fallback

    # ----------------------------------------------------------- helpers
    def profile(self, card_id: str) -> CardProfile:
        if card_id not in self._profiles:
            self._profiles[card_id] = build_profile(self.pack, card_id)
        return self._profiles[card_id]

    def _injection_hits(self, a: dict, facts) -> list[dict]:
        hits = []
        for f, line in zip(facts, a["items"]):
            if f.injection.flagged:
                hits.append({"where": f"line {line['line_no']} details", "text": line["item_details"],
                             "hits": [h.as_dict() for h in f.injection.hits] or
                                     [{"class": "residual", "rule": "unexplained prose", "excerpt": "", "via": "plain"}]})
        for where, text in (("order description", a["purchase_description"]), ("shop name", a["merchant"]["merchant_name"])):
            rep = detect(text)
            if rep.hits:
                hits.append({"where": where, "text": text, "hits": [h.as_dict() for h in rep.hits]})
        return hits

    def _control_checks(self, controls, merchant: dict) -> list[Check]:
        flag = controls.merchant_flags.get(merchant["merchant_id"])
        if not flag:
            return []
        if flag["mode"] == "block":
            return [Check("controls.merchant_flag", FAIL,
                          f"You blocked {merchant['merchant_name']}: {flag['reason']}",
                          "merchant_blocked_by_customer", tier="your-controls", provenance="customer", security=True,
                          extra={"flag": flag})]
        return [Check("controls.merchant_flag", UNK,
                      f"Asking you every time for {merchant['merchant_name']}, because {flag['reason']}",
                      "merchant_flagged_manipulation", tier="your-controls", provenance="customer", security=True,
                      extra={"flag": flag})]

    # ----------------------------------------------------------- decide
    def decide(self, event: dict, run_id: str | None = None) -> dict:
        t0 = time.perf_counter()
        a, mandate = event["authorization"], event["mandate"]
        run_id = run_id or f"run-{mandate['mandate_id']}"
        customer_id = mandate["customer_id"]
        with self.store.lock:
            ledger = self.store.run(run_id, customer_id=customer_id, card_id=a["card_id"], mandate_id=mandate["mandate_id"])
            controls = self.store.customer(customer_id)
            saved = ledger.get(a["authorization_id"])
            if saved:  # redelivery: same answer, never double-counted
                return {**saved.result, "redelivery": True}

            ts: datetime = parse_ts(a["timestamp"])
            profile = self.profile(a["card_id"])
            facts = extract(a["items"])
            if self.extractor_fallback:
                facts = self.extractor_fallback(a["items"], facts)
            rules = list(mandate["hard_rules"]) + controls.extra_rules.get(mandate["mandate_id"], [])
            have = {r["field"] for r in rules}
            rules += [r for r in SAFETY_NET if r["field"] not in have]

            familiar = dict(profile.familiar_merchants)
            familiar.update({r.merchant_id: r.merchant_name for r in ledger.records.values() if r.status == "approved"})
            ctx = Ctx(event=event, pack=self.pack, profile=profile, ledger=ledger, controls=controls, ts=ts,
                      facts=facts, rules=rules,
                      lookalike=find_lookalike(a["merchant"]["merchant_id"], a["merchant"]["merchant_name"], familiar),
                      injection_hits=self._injection_hits(a, facts))

            platform = self._platform_checks(event, controls)
            checks = platform + [evaluate(ctx, r) for r in rules] + self._control_checks(controls, a["merchant"])
            for c in checks:
                if c.tier == "customer" and c.field in {r["field"] for r in controls.extra_rules.get(mandate["mandate_id"], [])}:
                    c.tier = "added-by-you"
            policy = controls.uncertainty_override.get(mandate["mandate_id"], mandate["uncertainty_policy"])
            decision, reasons = self.combine(checks, policy)

            security_flags = []
            if ctx.injection_hits:
                security_flags.append("prompt injection")
            if ctx.lookalike:
                security_flags.append(f"lookalike of {ctx.lookalike['imitates_name']}")

            result = {
                "authorization_id": a["authorization_id"],
                "source_authorization_id": a["source_authorization_id"],
                "decision": decision,
                "reason_codes": reasons,
                "customer_message": customer_message(decision, checks, a["merchant"]["merchant_name"], a["billing_amount_chf"], policy),
                "headline": headline(decision, checks),
                "evidence": self._evidence(checks),
                "engine_version": ENGINE_VERSION,
                "checks": [c.as_dict() for c in checks],
                "facts": [f.as_dict() for f in facts],
                "uncertainty_policy": policy,
                "security_flags": security_flags,
                "merchant": a["merchant"],
                "amount_chf": a["billing_amount_chf"],
                "timestamp": a["timestamp"],
                "items": a["items"],
                "run_id": run_id,
                "mandate_id": mandate["mandate_id"],
                "rules": rules,
            }
            rec = record_from_event(event, decision, result)
            rec.security_flags = security_flags
            ledger.add(rec)
            result["flags_created"] = self._flag_and_alert(controls, ctx, result, run_id)
            result["latency_ms"] = round((time.perf_counter() - t0) * 1000, 2)
            return result

    def _platform_checks(self, event: dict, controls) -> list[Check]:
        a, m = event["authorization"], event["mandate"]
        out = []
        if m["status"] != "active" or m["mandate_id"] in controls.revoked_mandates:
            status = "revoked" if m["mandate_id"] in controls.revoked_mandates else m["status"]
            out.append(Check("mandate.status", FAIL, f"Your permission for the agent is {status}.", "mandate_not_active",
                             tier="platform", actual=status, expected="active"))
        if a["authority_status"] != "active" or a["card_status_at_attempt"] != "active":
            out.append(Check("authorization.card_status_at_attempt", FAIL,
                             f"The card or authority is not active ({a['card_status_at_attempt']}/{a['authority_status']}).",
                             "card_not_active", tier="platform"))
        return out

    @staticmethod
    def combine(checks: list[Check], policy: str) -> tuple[str, list[str]]:
        fails = [c for c in checks if c.status == FAIL]
        unknowns = [c for c in checks if c.status == UNK]
        if fails:
            # security signals ride along so the manipulation stays visible on a decline
            return "decline", _codes(fails + [c for c in unknowns if c.security])
        if unknowns:
            decision = {"ask": "step_up", "decline": "decline", "approve": "approve"}[policy]
            if decision == "approve" and any(c.security for c in unknowns):
                decision = "step_up"  # manipulation signals are never approved away
            return decision, _codes(unknowns)
        return "approve", ["all_checks_passed"]

    @staticmethod
    def _evidence(checks: list[Check]) -> list[dict]:
        order = {FAIL: 0, UNK: 1, PASS: 2}
        return [
            {"fact": c.field, "status": c.status, "value": c.actual, "provenance": c.provenance, "explanation": c.text}
            for c in sorted(checks, key=lambda c: order[c.status])
        ]

    def _flag_and_alert(self, controls, ctx: Ctx, result: dict, run_id: str) -> list[dict]:
        created = []
        m = ctx.merchant
        date = f"{ctx.ts:%d %b}"
        outcome = {"approve": "approved", "decline": "declined", "step_up": "sent to you"}[result["decision"]]
        incident = {"authorization_id": result["authorization_id"], "source_authorization_id": result["source_authorization_id"],
                    "run_id": run_id, "amount_chf": result["amount_chf"], "date": date, "decision": result["decision"]}
        if ctx.injection_hits:
            hit = ctx.injection_hits[0]
            rules = sorted({h["rule"] for h in hit["hits"]})
            reason = (f"on {date} its text tried to instruct the payment system ({', '.join(rules[:3])}) "
                      f"on a {chf(result['amount_chf'])} order, which we {outcome}.")
            known = m["merchant_id"] in controls.merchant_flags
            flag = controls.flag_merchant(m["merchant_id"], m["merchant_name"], kind="injection", reason=reason,
                                          incident={**incident, "verbatim": hit["text"], "where": hit["where"]})
            created.append({**flag, "repeat": known})
            controls.add_alert(type="manipulation", merchant_id=m["merchant_id"], merchant_name=m["merchant_name"],
                               title=f"{m['merchant_name']} tried to manipulate your shopping agent",
                               message=(f"On {date} a {chf(result['amount_chf'])} order from {m['merchant_name']} contained text aimed at "
                                        f"the payment system. We {outcome} it. From now on we'll ask you before every purchase "
                                        f"from this shop. You can keep that, block the shop, or remove the rule."),
                               verbatim=hit["text"], where=hit["where"], decision=result["decision"],
                               authorization_id=result["authorization_id"], run_id=run_id)
        if ctx.lookalike:
            lk = ctx.lookalike
            reason = (f"on {date} it presented itself with a name very similar to {lk['imitates_name']} "
                      f"(a shop you know) but it is a different merchant ({m['merchant_id']}).")
            known = m["merchant_id"] in controls.merchant_flags
            flag = controls.flag_merchant(m["merchant_id"], m["merchant_name"], kind="lookalike", reason=reason, incident=incident)
            created.append({**flag, "repeat": known})
            controls.add_alert(type="lookalike", merchant_id=m["merchant_id"], merchant_name=m["merchant_name"],
                               title=f"'{m['merchant_name']}' looks like '{lk['imitates_name']}' but is a different shop",
                               message=(f"On {date} your agent tried to pay {chf(result['amount_chf'])} to {m['merchant_name']} "
                                        f"({m['merchant_id']}), which imitates {lk['imitates_name']} ({lk['imitates_id']}). "
                                        f"We {outcome} it and will ask you before any purchase from it."),
                               verbatim=None, where="shop name", decision=result["decision"],
                               authorization_id=result["authorization_id"], run_id=run_id)
        return created

    # ----------------------------------------------------- human + controls
    def resolve(self, run_id: str, live_id: str, decision: str, by: str = "customer") -> dict:
        if decision not in ("approve", "decline"):
            raise ValueError(decision)
        with self.store.lock:
            ledger = self.store.runs[run_id]
            rec = ledger.get(live_id)
            if rec is None or rec.status != "pending":
                raise ValueError(f"{live_id} is not waiting for you")
            controls = self.store.customer(ledger.customer_id)
            if decision == "approve" and ledger.mandate_id in controls.revoked_mandates:
                raise RevokedError("Permission was withdrawn; this purchase can no longer be approved.")
            warning = None
            if decision == "approve":
                warning = self._breach_warning(ledger, rec)
            rec.status = "approved" if decision == "approve" else "declined"
            rec.resolved_by, rec.resolved_at = by, time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            controls.log("step_up_resolved", by, authorization_id=live_id, decision=decision)
            msg = ("The customer confirmed this purchase." if decision == "approve"
                   else "The customer declined this purchase.")
            return {"decision": decision, "customer_message": msg, "evidence": [], "warning": warning}

    def _breach_warning(self, ledger, rec) -> str | None:
        """Approving a paused purchase must not silently break a period limit."""
        for r in rec.result.get("rules", []):
            if r["field"] != "derived.period_spend_chf" or not r.get("period_days"):
                continue
            limit = self.pack.to_chf(float(r["value"]), r.get("currency") or "CHF")
            total = round(ledger.approved_spend(rec.timestamp, r["period_days"]) + rec.amount_chf, 2)
            if total > limit:
                return (f"Approving this brings your {r['period_days']}-day total to {chf(total)}, "
                        f"over your {chf(limit)} limit.")
        return None

    def expire(self, run_id: str, live_id: str) -> None:
        with self.store.lock:
            rec = self.store.runs[run_id].get(live_id)
            if rec and rec.status == "pending":
                rec.status = "expired"

    def set_merchant_flag(self, customer_id: str, merchant_id: str, mode: str, by: str = "customer") -> dict | None:
        """mode: ask | block | remove. Returns the rule to PATCH for a block (append-only, future runs)."""
        with self.store.lock:
            controls = self.store.customer(customer_id)
            controls.set_flag_mode(merchant_id, mode, by)
            for al in controls.alerts:
                if al["merchant_id"] == merchant_id and al["status"] == "open":
                    al["status"] = f"answered: {mode}"
            if mode == "block":
                return {"field": "authorization.merchant.merchant_id", "operator": "not_in", "value": [merchant_id]}
            return None

    def revoke(self, customer_id: str, mandate_id: str, by: str = "customer") -> None:
        with self.store.lock:
            controls = self.store.customer(customer_id)
            controls.revoked_mandates.add(mandate_id)
            controls.log("mandate_revoked", by, mandate_id=mandate_id)

    def tighten(self, customer_id: str, mandate_id: str, rule: dict | None = None, uncertainty: str | None = None,
                by: str = "customer") -> None:
        """Customer-sourced tightening, applied immediately (stricter only)."""
        with self.store.lock:
            controls = self.store.customer(customer_id)
            if rule:
                controls.extra_rules.setdefault(mandate_id, []).append(rule)
                controls.log("rule_added", by, mandate_id=mandate_id, rule=rule)
            if uncertainty:
                if uncertainty != "decline":
                    raise ValueError("uncertainty handling can only be tightened to 'decline'")
                controls.uncertainty_override[mandate_id] = "decline"
                controls.log("uncertainty_tightened", by, mandate_id=mandate_id)


def _codes(checks: list[Check]) -> list[str]:
    out: list[str] = []
    for c in checks:
        for code in c.extra.get("codes", [c.code]) if isinstance(c.extra, dict) else [c.code]:
            if code not in out:
                out.append(code)
    return out


def api_body(result: dict) -> dict:
    """Only the fields the decision endpoint accepts."""
    return {
        "authorization_id": result["authorization_id"],
        "decision": result["decision"],
        "reason_codes": result["reason_codes"],
        "customer_message": result["customer_message"][:1000],
        "evidence": result["evidence"],
        "engine_version": result["engine_version"],
    }
