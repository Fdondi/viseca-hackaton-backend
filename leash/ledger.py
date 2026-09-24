"""(C) State: per-run ledger and per-customer controls.

Run ledger
  * each live authorization_id is handled exactly once (redelivery returns the
    saved result and never double-counts);
  * only final approvals count toward limits; pending step-ups are tracked
    separately so "approved + pending" breaches can be flagged;
  * rolling windows use simulated time (authorization.timestamp).

Customer controls (outlive a run)
  * flagged merchants (auto-rules the customer can remove / keep / turn into a block),
  * alerts, revocations, and customer-sourced tightenings applied immediately.
  Every change is logged with who and when.
"""
from __future__ import annotations

import itertools
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .events import iso, parse_ts, utcnow

_ids = itertools.count(1)


@dataclass
class AttemptRecord:
    live_id: str
    source_id: str
    timestamp: datetime
    merchant_id: str
    merchant_name: str
    amount_chf: float
    item_ids: tuple[str, ...]
    device: str
    country: str
    decision: str                 # automated decision
    status: str                   # approved / declined / pending / expired
    result: dict = field(default_factory=dict)
    security_flags: list[str] = field(default_factory=list)
    resolved_by: str | None = None
    resolved_at: str | None = None
    checkout_hash: str | None = None   # AP2: the shop-signed cart this attempt paid for


@dataclass
class RunLedger:
    run_id: str
    customer_id: str
    card_id: str
    mandate_id: str
    records: dict[str, AttemptRecord] = field(default_factory=dict)
    order: list[str] = field(default_factory=list)

    def get(self, live_id: str) -> AttemptRecord | None:
        return self.records.get(live_id)

    def add(self, rec: AttemptRecord) -> None:
        if rec.live_id in self.records:
            return
        self.records[rec.live_id] = rec
        self.order.append(rec.live_id)

    def prior(self, before: datetime) -> list[AttemptRecord]:
        return [r for r in (self.records[i] for i in self.order) if r.timestamp < before]

    def window(self, at: datetime, days: float, statuses=("approved",)) -> list[AttemptRecord]:
        lo = at - timedelta(days=days)
        return [r for r in self.records.values() if r.status in statuses and lo < r.timestamp <= at]

    def approved_spend(self, at: datetime, days: float) -> float:
        return round(sum(r.amount_chf for r in self.window(at, days)), 2)

    def pending_spend(self, at: datetime, days: float) -> float:
        return round(sum(r.amount_chf for r in self.window(at, days, ("pending",))), 2)

    def approved_at_merchant(self, merchant_id: str, before: datetime) -> int:
        return sum(1 for r in self.records.values() if r.status == "approved" and r.merchant_id == merchant_id and r.timestamp < before)

    def approved_devices(self) -> set[str]:
        return {r.device for r in self.records.values() if r.status == "approved" and r.device}

    def approved_countries(self) -> set[str]:
        return {r.country for r in self.records.values() if r.status == "approved"}

    def total_approved(self) -> float:
        return round(sum(r.amount_chf for r in self.records.values() if r.status == "approved"), 2)


@dataclass
class CustomerControls:
    customer_id: str
    merchant_flags: dict[str, dict] = field(default_factory=dict)
    alerts: list[dict] = field(default_factory=list)
    revoked_mandates: set[str] = field(default_factory=set)
    extra_rules: dict[str, list[dict]] = field(default_factory=dict)       # mandate_id → rules added mid-run
    uncertainty_override: dict[str, str] = field(default_factory=dict)     # mandate_id → "decline"
    audit: list[dict] = field(default_factory=list)
    cleared_merchants: dict[str, str] = field(default_factory=dict)       # merchant_id → when the customer said "not a concern"

    def log(self, action: str, by: str, **details) -> dict:
        entry = {"at": iso(utcnow()), "action": action, "by": by, **details}
        self.audit.append(entry)
        return entry

    def flag_merchant(self, merchant_id: str, merchant_name: str, *, kind: str, reason: str, incident: dict) -> dict:
        """Create the 'ask every time' auto-rule. Never downgrades a block."""
        existing = self.merchant_flags.get(merchant_id)
        if existing:
            existing.setdefault("incidents", []).append(incident)
            return existing
        flag = {
            "merchant_id": merchant_id,
            "merchant_name": merchant_name,
            "mode": "ask",
            "kind": kind,
            "reason": reason,
            "incident": incident,
            "incidents": [incident],
            "created_at": iso(utcnow()),
            "created_by": "engine",
        }
        self.merchant_flags[merchant_id] = flag
        self.log("merchant_flagged", "engine", merchant_id=merchant_id, kind=kind, reason=reason)
        return flag

    def set_flag_mode(self, merchant_id: str, mode: str, by: str = "customer") -> dict | None:
        flag = self.merchant_flags.get(merchant_id)
        if mode == "remove":
            if flag:
                del self.merchant_flags[merchant_id]
            self.cleared_merchants[merchant_id] = iso(utcnow())
            self.log("merchant_flag_removed", by, merchant_id=merchant_id)
            return None
        if mode not in ("ask", "block"):
            raise ValueError(mode)
        if flag is None:
            flag = {"merchant_id": merchant_id, "merchant_name": merchant_id, "kind": "customer",
                    "reason": "Added by you.", "incident": None, "incidents": [], "created_at": iso(utcnow()),
                    "created_by": by}
            self.merchant_flags[merchant_id] = flag
        flag["mode"] = mode
        self.log(f"merchant_flag_{mode}", by, merchant_id=merchant_id)
        return flag

    def add_alert(self, **alert) -> dict:
        alert = {"alert_id": f"al_{next(_ids)}", "created_at": iso(utcnow()), "status": "open", **alert}
        self.alerts.append(alert)
        return alert


class Store:
    """Engine state shared by the worker, the engine and the UI."""

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.runs: dict[str, RunLedger] = {}
        self.controls: dict[str, CustomerControls] = {}
        self.checkout_hashes: dict[str, str] = {}   # AP2: shop-signed cart → the attempt that used it

    def run(self, run_id: str, *, customer_id: str, card_id: str, mandate_id: str) -> RunLedger:
        with self.lock:
            if run_id not in self.runs:
                self.runs[run_id] = RunLedger(run_id, customer_id, card_id, mandate_id)
            return self.runs[run_id]

    def customer(self, customer_id: str) -> CustomerControls:
        with self.lock:
            if customer_id not in self.controls:
                self.controls[customer_id] = CustomerControls(customer_id)
            return self.controls[customer_id]


def record_from_event(event: dict, decision: str, result: dict) -> AttemptRecord:
    a = event["authorization"]
    return AttemptRecord(
        live_id=a["authorization_id"],
        source_id=a["source_authorization_id"],
        timestamp=parse_ts(a["timestamp"]),
        merchant_id=a["merchant"]["merchant_id"],
        merchant_name=a["merchant"]["merchant_name"],
        amount_chf=a["billing_amount_chf"],
        item_ids=tuple(sorted(l["item_id"] for l in a["items"] for _ in range(l["quantity"]))),
        device=a["customer_device_id"],
        country=a["merchant"]["merchant_country"],
        decision=decision,
        status={"approve": "approved", "decline": "declined", "step_up": "pending"}[decision],
        result=result,
    )
