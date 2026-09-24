"""Confirmation backtest: "under these rules, what would have happened to your
past purchases?" Shown before the customer confirms. It catches a
mistranslated rule better than reading the rules.

History rows have no basket lines, so only structured checks are replayed:
amount, rolling period, shop familiarity (as of that date), shop type, and
device novelty. Item facts (size, returns) are not in history; that is said
explicitly.
"""
from __future__ import annotations

from collections import deque
from datetime import timedelta

from .data import Pack
from .events import parse_ts
from .fields import compare
from .profile import CardProfile


def _scope_categories(pack: Pack, rules: list[dict]) -> set[str] | None:
    cats: set[str] = set()
    for r in rules:
        if r["field"] == "items.item_category" and r["operator"] == "in":
            cats |= set(r["value"])
        if r["field"] == "items.item_id" and r["operator"] == "in":
            cats |= {pack.items[i]["item_category"] for i in r["value"] if i in pack.items}
    return cats or None


def backtest(pack: Pack, profile: CardProfile, rules: list[dict], months: int = 12) -> dict:
    rows = [r for r in pack.history_by_card.get(profile.card_id, []) if r["transaction_type"] == "purchase"]
    if not rows:
        return {"in_scope": 0, "summary": "No purchase history on this card.", "examples": []}
    end = parse_ts(rows[-1]["timestamp"])
    start = end - timedelta(days=30 * months)
    cats = _scope_categories(pack, rules)
    scope = [r for r in rows if parse_ts(r["timestamp"]) >= start and r["status"] == "approved"
             and (cats is None or r["merchant_category"] in cats)]

    merchant_seen: dict[str, deque] = {}
    devices: set[str] = set()
    period_rules = [r for r in rules if r["field"] == "derived.period_spend_chf" and r.get("period_days")]
    approved_window: list[tuple] = []
    out = {"approve": 0, "step_up": 0, "decline": 0}
    examples = []
    # familiarity must reflect only purchases strictly before each row
    all_prior = [r for r in rows if r["status"] == "approved"]
    idx = 0
    for row in scope:
        ts = parse_ts(row["timestamp"])
        while idx < len(all_prior) and (all_prior[idx]["timestamp"], all_prior[idx]["authorization_id"]) < (row["timestamp"], row["authorization_id"]):
            p = all_prior[idx]
            merchant_seen.setdefault(p["merchant_id"], deque()).append(parse_ts(p["timestamp"]))
            if p["customer_device_id"]:
                devices.add(p["customer_device_id"])
            idx += 1
        fails, asks = [], []
        for r in rules:
            f = r["field"]
            if f == "authorization.billing_amount_chf":
                lim = pack.to_chf(float(r["value"]), r.get("currency") or "CHF")
                if not compare(row["billing_amount_chf"], r["operator"], lim):
                    fails.append(f"CHF {row['billing_amount_chf']:.2f} over the CHF {lim:.0f} limit")
            elif f in ("derived.merchant_purchases_365d", "derived.merchant_purchases_ever"):
                times = merchant_seen.get(row["merchant_id"], deque())
                n = sum(1 for t in times if f.endswith("_ever") or t >= ts - timedelta(days=365))
                if not compare(n, r["operator"], r["value"]):
                    fails.append(f"first purchase at {row['merchant_name']}" if n == 0
                                 else f"only {n} earlier purchase{'s' if n != 1 else ''} at {row['merchant_name']}")
            elif f == "authorization.merchant.merchant_mcc":
                if not compare(row["merchant_mcc"], r["operator"], r["value"]):
                    fails.append(f"{row['merchant_name']} is not that type of shop")
            elif f == "derived.session_integrity":
                dev = row["customer_device_id"]
                if dev and dev not in devices:
                    asks.append(f"first purchase from device {dev}")
        for r in period_rules:
            lim = float(r["value"])
            lo = ts - timedelta(days=r["period_days"])
            tot = sum(a for t, a in approved_window if t > lo) + row["billing_amount_chf"]
            if not compare(tot, r["operator"], lim):
                fails.append(f"{r['period_days']}-day total CHF {tot:.2f} over CHF {lim:.0f}")
        decision = "decline" if fails else "step_up" if asks else "approve"
        out[decision] += 1
        if decision == "approve":
            approved_window.append((ts, row["billing_amount_chf"]))
        elif len(examples) < 8:
            examples.append({"date": row["timestamp"][:10], "merchant": row["merchant_name"],
                             "amount_chf": row["billing_amount_chf"], "outcome": decision,
                             "why": "; ".join(fails or asks), "initiator": row["initiator_type"]})
    n = len(scope)
    what = f"{', '.join(sorted(cats)).replace('_', ' ')} " if cats else ""
    summary = (f"Under these rules, of your last {n} {what}purchases ({months} months) {out['approve']} would have gone "
               f"through automatically, {out['step_up']} would have needed you, and {out['decline']} would have been stopped.")
    return {"in_scope": n, **out, "summary": summary, "examples": examples,
            "caveat": "History has no basket details, so item checks (size, returns, add-ons) are not replayed."}
