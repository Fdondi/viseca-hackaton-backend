"""(E) Field catalogue: what each rule `field` means, how it is evaluated, and
how it is described to the customer.

Every evaluation is three-valued: pass / fail / unknown. null, "unknown"
and "not_applicable" never mean permission. Rules on `items.*` and
`extracted.*` hold for every basket line (all must pass).

Naming: `authorization.*` raw event fields, `derived.*` computed,
`extracted.*` facts from behind the wall, `security.*` detectors.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable

from .data import Pack
from .ledger import CustomerControls, RunLedger
from .profile import CardProfile
from .wall import UNKNOWN, LineFacts

PASS, FAIL, UNK = "pass", "fail", "unknown"
RECURRING_CATEGORIES = {"subscriptions", "membership"}
QUASI_CASH_CATEGORIES = {"gift_card"}

# The fixed reason-code enum posted to the API.
REASON_CODES = {
    "all_checks_passed", "amount_over_limit", "period_limit_exceeded", "category_not_allowed",
    "item_not_requested", "unrequested_item", "basket_too_large", "quasi_cash_item", "recurring_addon",
    "merchant_unfamiliar", "merchant_type_not_allowed", "merchant_blocked_by_customer",
    "fulfillment_mismatch", "return_terms_insufficient", "return_terms_unknown", "size_mismatch",
    "size_unknown", "fact_unknown", "prompt_injection_detected", "lookalike_merchant",
    "possible_duplicate", "possible_split_order", "price_implausible", "new_device", "high_velocity",
    "new_country", "requote_of_flagged_attempt", "merchant_flagged_manipulation", "mandate_not_active",
    "card_not_active", "rule_not_understood", "rule_failed", "deadline_fallback",
}


@dataclass
class Check:
    field: str
    status: str
    text: str
    code: str
    tier: str = "customer"          # customer | safety-net | your-controls | platform
    provenance: str = "structured"  # structured | claimed | derived | history | run | detector | customer
    actual: object = None
    expected: object = None
    security: bool = False
    extra: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        d = {
            "field": self.field, "status": self.status, "text": self.text, "code": self.code,
            "tier": self.tier, "provenance": self.provenance, "actual": self.actual,
            "expected": self.expected, "security": self.security,
        }
        if self.extra:
            d["extra"] = self.extra
        return d


@dataclass
class Ctx:
    event: dict
    pack: Pack
    profile: CardProfile
    ledger: RunLedger
    controls: CustomerControls
    ts: datetime
    facts: list[LineFacts]
    rules: list[dict]
    lookalike: dict | None = None
    injection_hits: list[dict] = field(default_factory=list)

    @property
    def a(self) -> dict:
        return self.event["authorization"]

    @property
    def merchant(self) -> dict:
        return self.a["merchant"]

    def purchase_cap(self) -> float | None:
        caps = [
            self.limit_chf(r) for r in self.rules
            if r["field"] == "authorization.billing_amount_chf" and r["operator"] in ("<=", "<")
            and (r.get("scope") in (None, "purchase"))
        ]
        return min(caps) if caps else None

    def limit_chf(self, rule: dict) -> float:
        cur = rule.get("currency") or "CHF"
        return self.pack.to_chf(float(rule["value"]), cur)

    def item_rules(self) -> list[dict]:
        return [r for r in self.rules if r["field"] in ("items.item_id", "items.item_category")]

    def line_requested(self, line: dict) -> bool | None:
        """Does this line match what the customer asked for? None if no item rules exist."""
        rules = self.item_rules()
        if not rules:
            return None
        return all(compare(line[r["field"].split(".", 1)[1]], r["operator"], r["value"]) for r in rules)


def compare(actual, op: str, value) -> bool:
    if op in ("in", "not_in"):
        vals = value if isinstance(value, list) else [value]
        hit = str(actual) in [str(v) for v in vals]
        return hit if op == "in" else not hit
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        a = float(actual)
        v = float(value)
    else:
        a, v = str(actual), str(value)
    return {"<": a < v, "<=": a <= v, "=": a == v, "!=": a != v, ">": a > v, ">=": a >= v}[op]


def chf(x: float) -> str:
    return f"CHF {x:,.2f}".replace(",", "'")


OP_WORDS = {"<": "below", "<=": "at most", "=": "exactly", "!=": "not", ">": "above", ">=": "at least",
            "in": "one of", "not_in": "none of"}


def _fmt_value(v) -> str:
    return ", ".join(map(str, v)) if isinstance(v, list) else str(v)


# ---------------------------------------------------------------- evaluators

def ev_amount(ctx: Ctx, rule: dict) -> Check:
    amt = ctx.a["billing_amount_chf"]
    limit = ctx.limit_chf(rule)
    ok = compare(amt, rule["operator"], limit)
    conv = "" if ctx.a["currency"] == "CHF" else f" ({ctx.a['currency']} {ctx.a['amount']:.2f} converted at the fixed rate)"
    text = (f"{chf(amt)}{conv} is within your {chf(limit)} limit per order" if ok
            else f"{chf(amt)}{conv} is over your {chf(limit)} limit per order")
    if ctx.a["delivery_fee"]:
        text += f", delivery ({chf(ctx.pack.to_chf(ctx.a['delivery_fee'], ctx.a['currency']))}) included"
    return Check(rule["field"], PASS if ok else FAIL, text + ".", "amount_over_limit", actual=amt, expected=f"{rule['operator']} {limit}")


def ev_period(ctx: Ctx, rule: dict) -> Check:
    days = rule.get("period_days")
    if not days:
        return Check(rule["field"], UNK, "The period for this limit is not specified.", "rule_not_understood", provenance="run")
    prior = ctx.ledger.approved_spend(ctx.ts, days)
    pending = ctx.ledger.pending_spend(ctx.ts, days)
    amt = ctx.a["billing_amount_chf"]
    total = round(prior + amt, 2)
    limit = ctx.limit_chf(rule)
    ok = compare(total, rule["operator"], limit)
    text = (f"Approved in the last {days} days: {chf(prior)}; with this order {chf(total)}, "
            f"{'within' if ok else 'over'} your {chf(limit)} limit.")
    extra = {"prior_approved": prior, "this_order": amt, "total": total, "pending": pending,
             "platform_counter": ctx.event.get("context", {}).get("approved_spend_in_period_chf")}
    if ok and pending and total + pending > limit:
        text += f" Note: approving the {chf(pending)} still waiting for you would exceed it."
    return Check(rule["field"], PASS if ok else FAIL, text, "period_limit_exceeded", provenance="run",
                 actual=total, expected=f"{rule['operator']} {limit}", extra=extra)


def _resolve_path(obj: dict, path: str):
    cur = obj
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            raise KeyError(path)
        cur = cur[part]
    return cur


AUTH_CODES = {
    "merchant.merchant_mcc": "merchant_type_not_allowed",
    "merchant.merchant_category": "merchant_type_not_allowed",
    "merchant.merchant_id": "merchant_blocked_by_customer",
    "fulfillment_method": "fulfillment_mismatch",
    "order_returnable": "return_terms_insufficient",
}
AUTH_LABELS = {
    "merchant.merchant_mcc": "shop type (MCC)", "merchant.merchant_category": "shop category",
    "merchant.merchant_id": "shop", "merchant.merchant_country": "shop country", "currency": "currency",
    "fulfillment_method": "fulfilment", "order_returnable": "returnable", "order_cancellable": "cancellable",
    "channel": "channel",
}


def ev_authorization(ctx: Ctx, rule: dict) -> Check:
    path = rule["field"].split(".", 1)[1]
    label = AUTH_LABELS.get(path, path)
    code = AUTH_CODES.get(path, "rule_failed")
    try:
        actual = _resolve_path(ctx.a, path)
    except KeyError:
        return Check(rule["field"], UNK, f"We don't have a fact called '{path}'.", "rule_not_understood")
    if actual is None or (actual in ("unknown", "not_applicable") and rule["value"] not in ("unknown", "not_applicable")):
        return Check(rule["field"], UNK, f"The {label} is not stated ({actual}).", "fact_unknown", actual=actual)
    ok = compare(actual, rule["operator"], rule["value"])
    shown = actual
    if path == "merchant.merchant_mcc":
        shown = f"{actual} ({ctx.merchant['merchant_category'].replace('_', ' ')})"
    text = f"The {label} is {shown}; you allow {OP_WORDS[rule['operator']]} {_fmt_value(rule['value'])}."
    if path == "merchant.merchant_id" and rule["operator"] == "not_in":
        text = (f"{ctx.merchant['merchant_name']} is on your blocked list." if not ok
                else f"{ctx.merchant['merchant_name']} is not on your blocked list.")
    return Check(rule["field"], PASS if ok else FAIL, text, code, actual=actual, expected=rule["value"])


def ev_items(ctx: Ctx, rule: dict) -> Check:
    attr = rule["field"].split(".", 1)[1]
    code = {"item_category": "category_not_allowed", "item_id": "item_not_requested"}.get(attr, "rule_failed")
    bad, unknown = [], []
    for line in ctx.a["items"]:
        v = line.get(attr)
        if v is None:
            unknown.append(line)
        elif not compare(v, rule["operator"], rule["value"]):
            bad.append(line)
    if bad:
        names = "; ".join(f"line {l['line_no']} '{l['item_name']}' ({l['item_category'].replace('_', ' ')})" for l in bad)
        return Check(rule["field"], FAIL, f"Not what you asked for: {names}.", code,
                     actual=[l.get(attr) for l in bad], expected=rule["value"])
    if unknown:
        return Check(rule["field"], UNK, "Some basket lines don't say what they are.", "fact_unknown")
    what = ", ".join(sorted({l["item_name"] for l in ctx.a["items"]}))
    return Check(rule["field"], PASS, f"Every basket line is something you asked for ({what}).", code,
                 actual=[l.get(attr) for l in ctx.a["items"]], expected=rule["value"])


def ev_basket_units(ctx: Ctx, rule: dict) -> Check:
    units = sum(l["quantity"] for l in ctx.a["items"])
    ok = compare(units, rule["operator"], rule["value"])
    return Check(rule["field"], PASS if ok else FAIL,
                 f"The basket has {units} item{'s' if units != 1 else ''}; you allow {OP_WORDS[rule['operator']]} {rule['value']}.",
                 "basket_too_large", provenance="derived", actual=units, expected=rule["value"])


def _count_rule(ctx: Ctx, rule: dict, lines: list[dict], what: str, code: str) -> Check:
    n = len(lines)
    ok = compare(n, rule["operator"], rule["value"])
    if n:
        names = "; ".join(f"'{l['item_name']}' ({chf(ctx.pack.to_chf(l['unit_price'] * l['quantity'], l['currency']))})" for l in lines)
        text = f"The basket contains {what}: {names}."
    else:
        text = f"No {what} in the basket."
    return Check(rule["field"], PASS if ok else FAIL, text, code, provenance="derived", actual=n, expected=rule["value"])


def _facts_for(ctx: Ctx, line: dict) -> LineFacts:
    return next(f for f in ctx.facts if f.line_no == line["line_no"])


def ev_unrequested(ctx: Ctx, rule: dict) -> Check:
    extra = []
    for line in ctx.a["items"]:
        req = ctx.line_requested(line)
        f = _facts_for(ctx, line)
        if req is False or (req is None and (
            line["item_category"] in RECURRING_CATEGORIES | QUASI_CASH_CATEGORIES
            or f.addon == "true" or f.recurring_billing == "true" or f.quasi_cash == "true")):
            extra.append(line)
    return _count_rule(ctx, rule, extra, "something you didn't ask for", "unrequested_item")


def ev_quasi_cash(ctx: Ctx, rule: dict) -> Check:
    lines = [l for l in ctx.a["items"] if l["item_category"] in QUASI_CASH_CATEGORIES or _facts_for(ctx, l).quasi_cash == "true"]
    return _count_rule(ctx, rule, lines, "a gift card or voucher (works like cash)", "quasi_cash_item")


def ev_recurring(ctx: Ctx, rule: dict) -> Check:
    lines = [l for l in ctx.a["items"] if l["item_category"] in RECURRING_CATEGORIES or _facts_for(ctx, l).recurring_billing == "true"]
    return _count_rule(ctx, rule, lines, "a subscription or recurring charge", "recurring_addon")


def ev_merchant_count(ctx: Ctx, rule: dict) -> Check:
    days = 365 if rule["field"].endswith("_365d") else None
    mid = ctx.merchant["merchant_id"]
    hist = ctx.profile.merchant_count(mid, before=ctx.ts, days=days)
    run = ctx.ledger.approved_at_merchant(mid, ctx.ts)
    n = hist + run
    ok = compare(n, rule["operator"], rule["value"])
    span = "in the last 12 months" if days else "before"
    name = ctx.merchant["merchant_name"]
    need = f"{OP_WORDS[rule['operator']]} {rule['value']}"
    if n == 0:
        plural = "" if rule["value"] == 1 else "s"
        text = f"You have never bought from {name} (merchant {mid}) on this card; you asked for a shop with {need} earlier purchase{plural}."
    else:
        text = f"You have bought from {name} {n} time{'s' if n != 1 else ''} {span} (you asked for {need})."
    return Check(rule["field"], PASS if ok else FAIL, text, "merchant_unfamiliar", provenance="history",
                 actual=n, expected=rule["value"], extra={"history": hist, "this_run": run})


def _requested_lines(ctx: Ctx) -> list[dict]:
    lines = [l for l in ctx.a["items"] if ctx.line_requested(l) is not False]
    return lines


def ev_size(ctx: Ctx, rule: dict) -> Check:
    lines = _requested_lines(ctx)
    if not lines:
        return Check(rule["field"], PASS, "No line needs a size check.", "size_mismatch", provenance="claimed")
    statuses, seen = [], []
    for l in lines:
        f = _facts_for(ctx, l)
        if f.size == UNKNOWN:
            statuses.append(UNK)
            seen.append(f"'{l['item_name']}': size not stated" + (" (text withheld: manipulation)" if f.walled_off else ""))
        else:
            ok = compare(f.size, rule["operator"], rule["value"])
            statuses.append(PASS if ok else FAIL)
            seen.append(f"'{l['item_name']}': size {f.size}")
    status = FAIL if FAIL in statuses else UNK if UNK in statuses else PASS
    want = _fmt_value(rule["value"])
    text = f"Size (as stated by the shop) — {'; '.join(seen)}; you asked for {want}."
    return Check(rule["field"], status, text, "size_mismatch" if status == FAIL else "size_unknown",
                 provenance="claimed", actual=[s.split("size ")[-1] for s in seen], expected=rule["value"])


def ev_return_days(ctx: Ctx, rule: dict) -> Check:
    structured = ctx.a["order_returnable"]
    lines = _requested_lines(ctx) or ctx.a["items"]
    claims = [_facts_for(ctx, l).return_days for l in lines]
    want = f"{OP_WORDS[rule['operator']]} {rule['value']} days"
    days = claims[0] if len(set(map(str, claims))) == 1 else UNKNOWN
    claim_ok = None if days == UNKNOWN else compare(days, rule["operator"], rule["value"])
    if structured == "false" or claim_ok is False:
        what = "final sale / not returnable" if (structured == "false" or days == 0) else f"returns within {days} days"
        return Check(rule["field"], FAIL, f"Return terms: {what}; you asked for returns within {want}.",
                     "return_terms_insufficient", provenance="claimed+structured", actual=days, expected=rule["value"])
    if claim_ok is None:
        return Check(rule["field"], UNK, f"The shop does not state a return window (order returnable: {structured}); you asked for {want}.",
                     "return_terms_unknown", provenance="claimed+structured", actual=UNKNOWN, expected=rule["value"])
    if structured != "true":
        return Check(rule["field"], UNK,
                     f"The shop text says returns within {days} days, but the order itself says returnable = {structured}; we can't confirm {want}.",
                     "return_terms_unknown", provenance="claimed+structured", actual=days, expected=rule["value"])
    return Check(rule["field"], PASS, f"Returns accepted within {days} days (shop's terms, order marked returnable); you asked for {want}.",
                 "return_terms_insufficient", provenance="claimed+structured", actual=days, expected=rule["value"])


def ev_extracted_generic(ctx: Ctx, rule: dict) -> Check:
    name = rule["field"].split(".", 1)[1]
    statuses, vals = [], []
    for l in _requested_lines(ctx) or ctx.a["items"]:
        v = getattr(_facts_for(ctx, l), name, UNKNOWN)
        vals.append(v)
        statuses.append(UNK if v == UNKNOWN else PASS if compare(v, rule["operator"], rule["value"]) else FAIL)
    status = FAIL if FAIL in statuses else UNK if UNK in statuses else PASS
    text = f"{name.replace('_', ' ').capitalize()} (as stated by the shop): {_fmt_value(vals)}; you asked for {OP_WORDS[rule['operator']]} {_fmt_value(rule['value'])}."
    return Check(rule["field"], status, text, "fact_unknown" if status == UNK else "rule_failed", provenance="claimed", actual=vals, expected=rule["value"])


# -------------------------------------------------------------- risk signals
# Signals never fail: they return unknown, so they go through the customer's
# uncertainty policy. They can only make a decision stricter.

def sig_injection(ctx: Ctx, rule: dict) -> Check:
    if ctx.injection_hits:
        classes = sorted({h["class"] for hit in ctx.injection_hits for h in hit["hits"]})
        where = ", ".join(sorted({hit["where"] for hit in ctx.injection_hits}))
        text = (f"The shop's text ({where}) tries to instruct the payment system ({', '.join(classes)}). "
                "We ignored it; it cannot change your rules, and facts from that text were not used.")
        return Check(rule["field"], UNK, text, "prompt_injection_detected", tier="safety-net", provenance="detector",
                     security=True, extra={"hits": ctx.injection_hits})
    return Check(rule["field"], PASS, "No instructions hidden in the shop's text.", "prompt_injection_detected",
                 tier="safety-net", provenance="detector")


def sig_lookalike(ctx: Ctx, rule: dict) -> Check:
    lk = ctx.lookalike
    if lk:
        text = (f"'{ctx.merchant['merchant_name']}' ({ctx.merchant['merchant_id']}) is a different shop whose name looks like "
                f"'{lk['imitates_name']}' ({lk['imitates_id']}), a shop you know (similarity {lk['score']:.2f}).")
        return Check(rule["field"], UNK, text, "lookalike_merchant", tier="safety-net", provenance="detector",
                     security=True, extra=lk)
    return Check(rule["field"], PASS, "The shop's name doesn't imitate a shop you know.", "lookalike_merchant",
                 tier="safety-net", provenance="detector")


def sig_duplicate(ctx: Ctx, rule: dict) -> Check:
    cur_items = tuple(sorted(l["item_id"] for l in ctx.a["items"] for _ in range(l["quantity"])))
    amt = ctx.a["billing_amount_chf"]
    for r in ctx.ledger.prior(ctx.ts):
        if (r.status in ("approved", "pending") and r.merchant_id == ctx.merchant["merchant_id"]
                and r.item_ids == cur_items and abs(r.amount_chf - amt) <= 0.05 * max(amt, r.amount_chf)
                and ctx.ts - r.timestamp <= timedelta(hours=24)):
            mins = int((ctx.ts - r.timestamp).total_seconds() // 60)
            text = (f"Looks like a repeat: the same items from the same shop for {chf(r.amount_chf)} were "
                    f"{r.status} {mins} minutes earlier.")
            return Check(rule["field"], UNK, text, "possible_duplicate", tier="safety-net", provenance="run",
                         extra={"previous": r.live_id, "previous_source": r.source_id})
    return Check(rule["field"], PASS, "Not a repeat of a recent order.", "possible_duplicate", tier="safety-net", provenance="run")


def sig_split(ctx: Ctx, rule: dict) -> Check:
    cap = ctx.purchase_cap()
    if cap is None:
        return Check(rule["field"], PASS, "No per-order limit to split around.", "possible_split_order", tier="safety-net", provenance="run")
    cur_items = tuple(sorted(l["item_id"] for l in ctx.a["items"] for _ in range(l["quantity"])))
    recent = [r for r in ctx.ledger.prior(ctx.ts)
              if r.status in ("approved", "pending") and r.merchant_id == ctx.merchant["merchant_id"]
              and ctx.ts - r.timestamp <= timedelta(minutes=30)
              and r.item_ids != cur_items]  # the same basket again is a duplicate, not a split
    combined = round(sum(r.amount_chf for r in recent) + ctx.a["billing_amount_chf"], 2)
    if recent and combined > cap and ctx.a["billing_amount_chf"] <= cap:
        mins = int((ctx.ts - recent[-1].timestamp).total_seconds() // 60)
        text = (f"This may be one order split in two: {chf(recent[-1].amount_chf)} at the same shop {mins} minutes ago; "
                f"together {chf(combined)}, over your {chf(cap)} per-order limit.")
        return Check(rule["field"], UNK, text, "possible_split_order", tier="safety-net", provenance="run",
                     extra={"combined": combined, "cap": cap})
    return Check(rule["field"], PASS, "Not a split order.", "possible_split_order", tier="safety-net", provenance="run")


def sig_price(ctx: Ctx, rule: dict) -> Check:
    odd = []
    for l in ctx.a["items"]:
        ref = ctx.pack.items.get(l["item_id"])
        if not ref:
            continue
        unit_chf = ctx.pack.to_chf(l["unit_price"], l["currency"])
        if unit_chf > ref["unit_price_max_chf"] or unit_chf < ref["unit_price_min_chf"]:
            odd.append(f"'{l['item_name']}' at {chf(unit_chf)} (usual range {chf(ref['unit_price_min_chf'])}–{chf(ref['unit_price_max_chf'])})")
    if odd:
        return Check(rule["field"], UNK, "Unusual price: " + "; ".join(odd) + ".", "price_implausible", tier="safety-net", provenance="derived")
    return Check(rule["field"], PASS, "Prices are within the usual range for these items.", "price_implausible", tier="safety-net", provenance="derived")


def sig_session(ctx: Ctx, rule: dict) -> Check:
    issues, codes = [], []
    dev = ctx.a["customer_device_id"]
    if dev and dev not in ctx.profile.devices and dev not in ctx.ledger.approved_devices():
        issues.append(f"a device this card has never used ({dev})")
        codes.append("new_device")
    if ctx.a["recent_attempt_count_10m"] >= 2:
        issues.append(f"{ctx.a['recent_attempt_count_10m']} other purchase attempts in the last 10 minutes")
        codes.append("high_velocity")
    country = ctx.merchant["merchant_country"]
    if country not in ctx.profile.countries and country not in ctx.ledger.approved_countries():
        issues.append(f"a country this card has never bought from ({country})")
        codes.append("new_country")
    if issues:
        return Check(rule["field"], UNK, "Someone else may be driving: " + "; ".join(issues) + ".", codes[0],
                     tier="safety-net", provenance="history", extra={"codes": codes})
    return Check(rule["field"], PASS, "Your usual device and a normal pace.", "new_device", tier="safety-net", provenance="history")


def sig_requote(ctx: Ctx, rule: dict) -> Check:
    rel = ctx.a.get("related_authorization_id")
    if not rel:
        return Check(rule["field"], PASS, "Not a re-quote.", "requote_of_flagged_attempt", tier="safety-net", provenance="run")
    prev = ctx.ledger.get(rel)
    if prev and prev.security_flags and prev.merchant_id in ctx.controls.cleared_merchants:
        return Check(rule["field"], PASS,
                     f"A re-quote of the {chf(prev.amount_chf)} order from {prev.timestamp:%d %b}; you told us that shop's "
                     "earlier text was not a concern.", "requote_of_flagged_attempt", tier="safety-net", provenance="customer")
    if prev and prev.security_flags:
        text = (f"This is a new offer replacing the {chf(prev.amount_chf)} order from {prev.timestamp:%d %b} "
                f"({prev.status}), whose text tried to manipulate the payment ({', '.join(prev.security_flags)}).")
        return Check(rule["field"], UNK, text, "requote_of_flagged_attempt", tier="safety-net", provenance="run",
                     security=True, extra={"previous": rel, "previous_source": prev.source_id})
    what = f"the {chf(prev.amount_chf)} order from {prev.timestamp:%d %b} ({prev.status})" if prev else f"an earlier attempt ({rel})"
    return Check(rule["field"], PASS, f"A re-quote of {what}; nothing suspicious about it.", "requote_of_flagged_attempt",
                 tier="safety-net", provenance="run")


# ---------------------------------------------------------------- registry

@dataclass
class FieldSpec:
    evaluate: Callable[[Ctx, dict], Check]
    describe: Callable[[dict], str]
    kind: str = "rule"  # rule | signal


def _d_amount(r):
    return f"Each order costs {OP_WORDS[r['operator']]} {r.get('currency') or 'CHF'} {float(r['value']):.2f}, delivery included."


def _d_period(r):
    return f"All approved orders in any {r.get('period_days')} days add up to {OP_WORDS[r['operator']]} {r.get('currency') or 'CHF'} {float(r['value']):.2f}."


def _d_merchant(r):
    if r["field"].endswith("_365d"):
        return f"Only shops you bought from {OP_WORDS[r['operator']]} {r['value']} times in the last 12 months (your regular shops)."
    if r["operator"] == ">=" and r["value"] == 1:
        return "Only shops you have bought from before on this card."
    return f"Only shops you have bought from {OP_WORDS[r['operator']]} {r['value']} times before on this card."


def _names(field_name: str, values) -> str:
    """Human names for catalogue IDs and merchant codes (IDs kept in brackets)."""
    from .data import load
    pack = load()
    vals = values if isinstance(values, list) else [values]
    if field_name == "items.item_id":
        return ", ".join(f"{pack.items[v]['item_name']} ({v})" if v in pack.items else v for v in vals)
    if field_name == "authorization.merchant.merchant_mcc":
        cats = {m["merchant_mcc"]: m["merchant_category"].replace("_", " ") for m in pack.merchants.values()}
        return ", ".join(f"{cats.get(str(v).zfill(4), 'other')} stores (MCC {v})" for v in vals)
    if field_name == "authorization.merchant.merchant_id":
        return ", ".join(f"{pack.merchants[v]['merchant_name']} ({v})" if v in pack.merchants else v for v in vals)
    return _fmt_value(values)


def describe_generic(r: dict) -> str:
    f = r["field"]
    positive = r["operator"] in ("in", "=")
    if f == "items.item_id" and r["operator"] in ("in", "=", "not_in", "!="):
        return f"Every line in the basket {'is' if positive else 'is not'}: {_names(f, r['value'])}."
    if f == "items.item_category" and r["operator"] in ("in", "=", "not_in", "!="):
        cats = ", ".join(str(v).replace("_", " ") for v in (r["value"] if isinstance(r["value"], list) else [r["value"]]))
        return f"Every line in the basket is {cats}." if positive else f"Nothing in the basket is {cats}."
    if f.startswith("items."):
        return f"Every basket line's {f.split('.', 1)[1].replace('_', ' ')} is {OP_WORDS[r['operator']]} {_fmt_value(r['value'])}."
    if f == "authorization.merchant.merchant_mcc" and r["operator"] in ("in", "=", "not_in", "!="):
        return f"Only {_names(f, r['value'])}." if positive else f"Never {_names(f, r['value'])}."
    if f == "authorization.order_returnable" and r["operator"] == "=" and r["value"] == "true":
        return "The order must be returnable."
    if f.startswith("authorization."):
        path = f.split(".", 1)[1]
        if path == "merchant.merchant_id" and r["operator"] == "not_in":
            return f"Never buy from: {_names(f, r['value'])}."
        return f"The {AUTH_LABELS.get(path, path)} is {OP_WORDS[r['operator']]} {_fmt_value(r['value'])}."
    if f.startswith("extracted."):
        return f"The shop states a {f.split('.', 1)[1].replace('_', ' ')} {OP_WORDS[r['operator']]} {_fmt_value(r['value'])}."
    return f"{f} {r['operator']} {_fmt_value(r['value'])} (we don't know this check; it will count as uncertain)."


FIELDS: dict[str, FieldSpec] = {
    "authorization.billing_amount_chf": FieldSpec(ev_amount, _d_amount),
    "derived.period_spend_chf": FieldSpec(ev_period, _d_period),
    "derived.basket_units": FieldSpec(ev_basket_units, lambda r: f"The basket holds {OP_WORDS[r['operator']]} {r['value']} item(s)."),
    "derived.unrequested_lines": FieldSpec(ev_unrequested, lambda r: "Nothing in the basket that you didn't ask for (no add-ons or extras)."),
    "derived.quasi_cash_lines": FieldSpec(ev_quasi_cash, lambda r: "No gift cards, vouchers or store credit (they work like cash)."),
    "derived.recurring_lines": FieldSpec(ev_recurring, lambda r: "No subscriptions, memberships or anything billed again later."),
    "derived.merchant_purchases_365d": FieldSpec(ev_merchant_count, _d_merchant),
    "derived.merchant_purchases_ever": FieldSpec(ev_merchant_count, _d_merchant),
    "extracted.size": FieldSpec(ev_size, lambda r: f"The size is {_fmt_value(r['value'])} (as stated by the shop; if not stated, we ask you)."),
    "extracted.return_days": FieldSpec(ev_return_days, lambda r: f"The order can be returned within {r['value']} days or more (if not stated, we ask you)."),
    "security.merchant_text_clean": FieldSpec(sig_injection, lambda r: "Shop text that tries to instruct the payment system → we ask you.", "signal"),
    "derived.not_lookalike": FieldSpec(sig_lookalike, lambda r: "A new shop whose name imitates one you know → we ask you.", "signal"),
    "derived.not_duplicate": FieldSpec(sig_duplicate, lambda r: "The same order again within 24 hours → we ask you.", "signal"),
    "derived.no_split_order": FieldSpec(sig_split, lambda r: "An order split into pieces to stay under your per-order limit → we ask you.", "signal"),
    "derived.price_plausible": FieldSpec(sig_price, lambda r: "A price far outside the usual range for that item → we ask you.", "signal"),
    "derived.session_integrity": FieldSpec(sig_session, lambda r: "A new device, a burst of attempts, or a new country (someone else may be driving) → we ask you.", "signal"),
    "derived.requote_clean": FieldSpec(sig_requote, lambda r: "A re-quote of an order that tried to manipulate us → we ask you.", "signal"),
}

SAFETY_NET = [
    {"field": f, "operator": "=", "value": "true"}
    for f, spec in FIELDS.items() if spec.kind == "signal"
]


def spec_for(field_name: str) -> FieldSpec | None:
    if field_name in FIELDS:
        return FIELDS[field_name]
    if field_name.startswith("authorization."):
        return FieldSpec(ev_authorization, describe_generic)
    if field_name.startswith("items."):
        return FieldSpec(ev_items, describe_generic)
    if field_name.startswith("extracted."):
        return FieldSpec(ev_extracted_generic, describe_generic)
    return None


def describe(rule: dict) -> str:
    spec = spec_for(rule["field"])
    return spec.describe(rule) if spec else describe_generic(rule)


def evaluate(ctx: Ctx, rule: dict) -> Check:
    spec = spec_for(rule["field"])
    if spec is None:
        return Check(rule["field"], UNK, f"We don't know how to check '{rule['field']}', so we treat it as uncertain.",
                     "rule_not_understood")
    try:
        return spec.evaluate(ctx, rule)
    except (KeyError, TypeError, ValueError) as exc:
        return Check(rule["field"], UNK, f"Could not evaluate this check ({type(exc).__name__}); treated as uncertain.",
                     "rule_not_understood")
