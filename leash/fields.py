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
    "ap2_signature_invalid", "ap2_checkout_mismatch", "ap2_replay", "ap2_expired", "ap2_constraint_violated",
    "ap2_checkout_not_disclosed", "country_outside_profile",
}


@dataclass
class Check:
    field: str
    status: str
    text: str
    code: str
    tier: str = "customer"          # customer | safety-net | your-controls | platform | ap2
    provenance: str = "structured"  # structured | claimed | shop-signed | derived | history | run | detector | customer | cryptographic
    actual: object = None
    expected: object = None
    security: bool = False
    extra: dict = field(default_factory=dict)
    trace: list[str] = field(default_factory=list)   # how it was decided: the values compared, step by step
    rule: dict | None = None        # the rule this check evaluated (None for platform/AP2/control checks)

    def as_dict(self) -> dict:
        d = {
            "field": self.field, "status": self.status, "text": self.text, "code": self.code,
            "tier": self.tier, "provenance": self.provenance, "actual": self.actual,
            "expected": self.expected, "security": self.security,
        }
        if self.extra:
            d["extra"] = self.extra
        if self.rule is not None:
            d["rule"] = self.rule
        if self.trace:
            d["trace"] = self.trace
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
    ap2: object | None = None   # ap2.Ap2Result when the shop signed the cart

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

    def signed_ok(self) -> bool:
        """Shop-signed terms count only from a verified shop, in a cart without manipulation,
        from a shop the customer hasn't flagged. Otherwise we fall back to the text, behind the wall."""
        return bool(self.ap2 and self.ap2.merchant_verified and not self.injection_hits
                    and self.merchant["merchant_id"] not in self.controls.merchant_flags)

    def signed_attr(self, line: dict, name: str):
        if not self.signed_ok():
            return None
        return self.ap2.signed_attrs.get(line["line_no"], {}).get(name)

    def signed_term(self, name: str):
        if not self.signed_ok():
            return None
        return self.ap2.signed_terms.get(name)

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
# order terms a shop can sign in an AP2 checkout (`terms`), by event path
SIGNED_TERMS = {"order_returnable": "returnable", "order_cancellable": "cancellable",
                "fulfillment_method": "fulfillment_method", "delivery_by": "delivery_by"}


def ev_authorization(ctx: Ctx, rule: dict) -> Check:
    path = rule["field"].split(".", 1)[1]
    label = AUTH_LABELS.get(path, path)
    code = AUTH_CODES.get(path, "rule_failed")
    try:
        actual = _resolve_path(ctx.a, path)
    except KeyError:
        return Check(rule["field"], UNK, f"We don't have a fact called '{path}'.", "rule_not_understood")
    prov = "structured"
    signed = ctx.signed_term(SIGNED_TERMS[path]) if path in SIGNED_TERMS else None
    if signed is not None and actual in (None, "unknown", "not_applicable"):
        actual, prov = signed, "shop-signed"
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
    if prov == "shop-signed":
        text = text[:-1] + " (signed by the shop)."
    return Check(rule["field"], PASS if ok else FAIL, text, code, provenance=prov, actual=actual, expected=rule["value"])


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
    statuses, seen, vals, signed_used = [], [], [], False
    for l in lines:
        f = _facts_for(ctx, l)
        signed = ctx.signed_attr(l, "size")
        if signed is not None and f.size not in (UNKNOWN, str(signed)):
            statuses.append(UNK)
            vals.append(UNKNOWN)
            seen.append(f"'{l['item_name']}': the shop signed size {signed} but its product text says {f.size}")
        elif signed is not None:
            signed_used = True
            ok = compare(str(signed), rule["operator"], rule["value"])
            statuses.append(PASS if ok else FAIL)
            vals.append(str(signed))
            seen.append(f"'{l['item_name']}': size {signed} (signed by the shop)")
        elif f.size == UNKNOWN:
            statuses.append(UNK)
            vals.append(UNKNOWN)
            seen.append(f"'{l['item_name']}': size not stated" + (" (text withheld: manipulation)" if f.walled_off else ""))
        else:
            ok = compare(f.size, rule["operator"], rule["value"])
            statuses.append(PASS if ok else FAIL)
            vals.append(f.size)
            seen.append(f"'{l['item_name']}': size {f.size}")
    status = FAIL if FAIL in statuses else UNK if UNK in statuses else PASS
    want = _fmt_value(rule["value"])
    text = f"Size (as stated by the shop) — {'; '.join(seen)}; you asked for {want}."
    return Check(rule["field"], status, text, "size_mismatch" if status == FAIL else "size_unknown",
                 provenance="shop-signed" if signed_used else "claimed", actual=vals, expected=rule["value"])


def ev_return_days(ctx: Ctx, rule: dict) -> Check:
    structured = ctx.a["order_returnable"]
    want = f"{OP_WORDS[rule['operator']]} {rule['value']} days"
    prov = "claimed+structured"
    signed_ret = ctx.signed_term("returnable")
    if signed_ret is not None and structured in (None, "unknown", "not_applicable"):
        structured, prov = str(signed_ret), "shop-signed"
    elif signed_ret is not None and str(signed_ret) != structured:
        return Check(rule["field"], UNK, f"The shop signed returnable = {signed_ret} but the order says {structured}; "
                     f"we can't confirm {want}.", "return_terms_unknown", provenance="shop-signed", expected=rule["value"])
    lines = _requested_lines(ctx) or ctx.a["items"]
    claims = []
    for l in lines:
        text_days = _facts_for(ctx, l).return_days
        signed = ctx.signed_attr(l, "return_window_days")
        if signed is None:
            claims.append(text_days)
        elif text_days != UNKNOWN and int(text_days) != int(signed):
            return Check(rule["field"], UNK, f"The shop signed a {signed}-day return window for '{l['item_name']}' but its "
                         f"product text says {text_days} days; we can't confirm {want}.", "return_terms_unknown",
                         provenance="shop-signed", actual=UNKNOWN, expected=rule["value"])
        else:
            claims.append(int(signed))
            prov = "shop-signed"
    days = claims[0] if len(set(map(str, claims))) == 1 else UNKNOWN
    if prov == "shop-signed" and days != UNKNOWN and structured not in ("true", "false"):
        structured = "true" if days > 0 else "false"   # a signed return window is a signed "returnable"
    claim_ok = None if days == UNKNOWN else compare(days, rule["operator"], rule["value"])
    if structured == "false" or claim_ok is False:
        what = "final sale / not returnable" if (structured == "false" or days == 0) else f"returns within {days} days"
        return Check(rule["field"], FAIL, f"Return terms: {what}; you asked for returns within {want}.",
                     "return_terms_insufficient", provenance=prov, actual=days, expected=rule["value"])
    if claim_ok is None:
        return Check(rule["field"], UNK, f"The shop does not state a return window (order returnable: {structured}); you asked for {want}.",
                     "return_terms_unknown", provenance=prov, actual=UNKNOWN, expected=rule["value"])
    if structured != "true":
        return Check(rule["field"], UNK,
                     f"The shop text says returns within {days} days, but the order itself says returnable = {structured}; we can't confirm {want}.",
                     "return_terms_unknown", provenance=prov, actual=days, expected=rule["value"])
    terms = "signed by the shop" if prov == "shop-signed" else "shop's terms, order marked returnable"
    return Check(rule["field"], PASS, f"Returns accepted within {days} days ({terms}); you asked for {want}.",
                 "return_terms_insufficient", provenance=prov, actual=days, expected=rule["value"])


def ev_extracted_generic(ctx: Ctx, rule: dict) -> Check:
    name = rule["field"].split(".", 1)[1]
    statuses, vals, signed_used = [], [], False
    for l in _requested_lines(ctx) or ctx.a["items"]:
        v = getattr(_facts_for(ctx, l), name, UNKNOWN)
        signed = ctx.signed_attr(l, name)
        if signed is not None:
            v = signed if v in (UNKNOWN, signed) else UNKNOWN   # the shop contradicting itself is unknown
            signed_used = True
        vals.append(v)
        statuses.append(UNK if v == UNKNOWN else PASS if compare(v, rule["operator"], rule["value"]) else FAIL)
    status = FAIL if FAIL in statuses else UNK if UNK in statuses else PASS
    how = "signed by the shop" if signed_used else "as stated by the shop"
    text = f"{name.replace('_', ' ').capitalize()} ({how}): {_fmt_value(vals)}; you asked for {OP_WORDS[rule['operator']]} {_fmt_value(rule['value'])}."
    return Check(rule["field"], status, text, "fact_unknown" if status == UNK else "rule_failed",
                 provenance="shop-signed" if signed_used else "claimed", actual=vals, expected=rule["value"])


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
        if ctx.ap2 and ctx.ap2.merchant_verified:
            text += (f" Its cart is signed with its own key ({ctx.merchant['merchant_id']}), not {lk['imitates_name']}'s: "
                     "the signature proves who it is, not that it's the shop you know.")
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


APPROVED_BAND = (0.8, 1.25)   # a price this close to one the customer approved before counts as usual for them


def sig_price(ctx: Ctx, rule: dict) -> Check:
    """Catalogue range, widened by what this customer has approved before (they decide what is usual)."""
    odd, learned = [], []
    for l in ctx.a["items"]:
        ref = ctx.pack.items.get(l["item_id"])
        if not ref:
            continue
        unit_chf = ctx.pack.to_chf(l["unit_price"], l["currency"])
        if ref["unit_price_min_chf"] <= unit_chf <= ref["unit_price_max_chf"]:
            continue
        rng = f"{chf(ref['unit_price_min_chf'])}–{chf(ref['unit_price_max_chf'])}"
        mine = [p for p in ctx.controls.approved_prices.get(l["item_id"], [])
                if p * APPROVED_BAND[0] <= unit_chf <= p * APPROVED_BAND[1]]
        if mine:
            learned.append(f"'{l['item_name']}' at {chf(unit_chf)} is outside the catalogue's range ({rng}) but in line "
                           f"with what you approved before ({', '.join(chf(p) for p in mine)})")
        else:
            odd.append(f"'{l['item_name']}' at {chf(unit_chf)} (usual range {rng})")
    if odd:
        return Check(rule["field"], UNK, "Unusual price: " + "; ".join(odd) + ".", "price_implausible", tier="safety-net", provenance="derived")
    if learned:
        return Check(rule["field"], PASS, "; ".join(learned) + ".", "price_implausible",
                     tier="safety-net", provenance="customer")
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


def sig_country_expected(ctx: Ctx, rule: dict) -> Check:
    """Permanent rule from the profile: a shop outside the countries the customer shops in → ask. Never declines."""
    cc = ctx.merchant["merchant_country"]
    usual = rule["value"] if isinstance(rule["value"], list) else [rule["value"]]
    if cc in usual:
        return Check(rule["field"], PASS, f"The shop is in {cc}, where you usually shop.", "country_outside_profile",
                     provenance="customer", actual=cc, expected=usual)
    return Check(rule["field"], UNK, f"The shop is in {cc}; your profile says you shop in {', '.join(usual)}.",
                 "country_outside_profile", provenance="customer", actual=cc, expected=usual)


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
    "derived.shop_country_expected": FieldSpec(sig_country_expected,
                                               lambda r: f"A shop outside {_fmt_value(r['value'])} → we ask you.", "profile"),
}

SAFETY_NET = [
    {"field": f, "operator": "=", "value": "true"}
    for f, spec in FIELDS.items() if spec.kind == "signal"
]


# ---------------------------------------------------------------- traces
# How each check was decided, as the values it compared. Built from the same context the
# evaluator used; shown to people (flow demo, evidence), never used to decide.

def _q(text, n: int = 70) -> str:
    t = " ".join(str(text).split())
    return f"“{t if len(t) <= n else t[:n - 1] + '…'}”"


def _yes(ok: bool) -> str:
    return "true" if ok else "false"


def _cmp(actual, rule: dict, value=None) -> str:
    v = rule["value"] if value is None else value
    return f"{_fmt_value(actual)} {rule['operator']} {_fmt_value(v)} → {_yes(compare(actual, rule['operator'], v))}"


def _line(l: dict) -> str:
    return f"line {l['line_no']} '{l['item_name']}'"


SIGNED_ATTR = {"return_days": "return_window_days"}   # fact name → the AP2 cart's attribute name


def _fact_origin(ctx: Ctx, l: dict, name: str) -> str:
    """Where a fact about one basket line came from, in words."""
    f = _facts_for(ctx, l)
    signed = ctx.signed_attr(l, SIGNED_ATTR.get(name, name))
    if signed is not None:
        m = f.matches.get(name)
        also = f"; its text {_q(m, 40)} → {getattr(f, name, UNKNOWN)}" if m and not f.walled_off else ""
        return f"shop-signed cart: {name.replace('_', ' ')} = {signed}{also}"
    if f.walled_off and name in ("size", "return_days", "final_sale", "warranty_months"):
        return "not read: the text instructs the payment system, so its facts are withheld"
    m = f.matches.get(name)
    v = getattr(f, name, UNKNOWN)
    src = f.sources.get(name, "")
    if m:
        return f"shop text {_q(m, 50)} → {v}" + (" (model, verified verbatim)" if "model" in src else "")
    if v not in (UNKNOWN, "false"):
        return f"{v}" + (" (model, verified verbatim)" if "model" in src else "")
    return f"no {name.replace('_', ' ')} wording in the shop text → {v}"


def t_amount(ctx, rule, c):
    a = ctx.a
    if a["currency"] != "CHF":
        out = [f"{a['currency']} {a['amount']:.2f} × fixed rate {ctx.pack.fx[a['currency']]} = {chf(a['billing_amount_chf'])}"]
    else:
        out = [f"billing amount {chf(a['billing_amount_chf'])} (delivery {a['delivery_fee'] or 0:.2f} included)"]
    lim = ctx.limit_chf(rule)
    if (rule.get("currency") or "CHF") != "CHF":
        out.append(f"limit {rule['currency']} {float(rule['value']):.2f} = {chf(lim)} at the fixed rate")
    out.append(f"{a['billing_amount_chf']:.2f} {rule['operator']} {lim:.2f} → {_yes(c.status == PASS)}")
    return out


def t_period(ctx, rule, c):
    e = c.extra or {}
    if not rule.get("period_days"):
        return ["the rule has no period length → can't evaluate"]
    lim = ctx.limit_chf(rule)
    out = [f"approved in the {rule['period_days']} days up to {ctx.ts:%d %b %H:%M} (this run's ledger): {e.get('prior_approved', 0):.2f}",
           f"+ this order {e.get('this_order', 0):.2f} = {e.get('total', 0):.2f}",
           f"{e.get('total', 0):.2f} {rule['operator']} {lim:.2f} → {_yes(c.status == PASS)}"]
    if e.get("platform_counter") is not None:
        out.append(f"cross-check: platform's approved counter = {e['platform_counter']:.2f}")
    return out


def t_authorization(ctx, rule, c):
    path = rule["field"].split(".", 1)[1]
    if c.status == UNK:
        return [f"{path} = {c.actual!r} → not stated, can't compare"]
    src = " (from the shop-signed cart)" if c.provenance == "shop-signed" else ""
    return [f"{path} = {c.actual!r}{src}", _cmp(c.actual, rule)]


def t_items(ctx, rule, c):
    attr = rule["field"].split(".", 1)[1]
    return [f"{_line(l)}: {attr} = {l.get(attr)!r} → {_cmp(l.get(attr), rule).split('→ ')[1]}" for l in ctx.a["items"]]


def t_units(ctx, rule, c):
    qs = [l["quantity"] for l in ctx.a["items"]]
    return [f"units = {' + '.join(map(str, qs))} = {sum(qs)}", _cmp(sum(qs), rule)]


def _t_lines(ctx, rule, c, reasons) -> list[str]:
    out, counted = [], 0
    for l in ctx.a["items"]:
        why = reasons(l)
        counted += bool(why)
        out.append(f"{_line(l)}: " + ("; ".join(why) + " → counted" if why else "nothing matched → not counted"))
    out.append(f"count = {counted}; {_cmp(counted, rule)}")
    return out


def t_unrequested(ctx, rule, c):
    def reasons(l):
        f = _facts_for(ctx, l)
        r = []
        req = ctx.line_requested(l)
        if req is False:
            r.append(f"not what you asked for (item {l['item_id']}, category {l['item_category']})")
        if req is None:
            if l["item_category"] in RECURRING_CATEGORIES | QUASI_CASH_CATEGORIES:
                r.append(f"category {l['item_category']}")
            for k in ("addon", "recurring_billing", "quasi_cash"):
                if getattr(f, k) == "true":
                    r.append(f"shop text {_q(f.matches.get(k, k), 40)} → {k.replace('_', ' ')}")
        return r
    return _t_lines(ctx, rule, c, reasons)


def t_quasi_cash(ctx, rule, c):
    def reasons(l):
        f = _facts_for(ctx, l)
        return ([f"category {l['item_category']}"] if l["item_category"] in QUASI_CASH_CATEGORIES else []) + \
               ([f"shop text {_q(f.matches.get('quasi_cash', ''), 40)} → voucher"] if f.quasi_cash == "true" else [])
    return _t_lines(ctx, rule, c, reasons)


def t_recurring(ctx, rule, c):
    def reasons(l):
        f = _facts_for(ctx, l)
        return ([f"category {l['item_category']}"] if l["item_category"] in RECURRING_CATEGORIES else []) + \
               ([f"shop text {_q(f.matches.get('recurring_billing', ''), 40)} → billed again"] if f.recurring_billing == "true" else [])
    return _t_lines(ctx, rule, c, reasons)


def t_merchant_count(ctx, rule, c):
    e = c.extra or {}
    span = "last 12 months" if rule["field"].endswith("_365d") else "all history"
    mid = ctx.merchant["merchant_id"]
    return [f"approved purchases at merchant ID {mid} in the card history ({span}): {e.get('history', 0)}",
            f"approved at {mid} earlier in this run: {e.get('this_run', 0)}",
            f"{e.get('history', 0)} + {e.get('this_run', 0)} = {c.actual}; {_cmp(c.actual, rule)}"]


def t_size(ctx, rule, c):
    lines = _requested_lines(ctx)
    if not lines:
        return ["no basket line is one you asked for → nothing to compare"]
    out = [f"{_line(l)}: {_fact_origin(ctx, l, 'size')}" for l in lines]
    out += [f"size {v} in {_fmt_value(rule['value'])} → {_yes(compare(v, rule['operator'], rule['value']))}"
            for v in (c.actual or []) if v != UNKNOWN]
    if UNKNOWN in (c.actual or []):
        out.append("a size is unknown → uncertain")
    return out


def t_return_days(ctx, rule, c):
    out = [f"order returnable = {ctx.a['order_returnable']!r} (transaction data)"]
    if ctx.signed_term("returnable") is not None:
        out.append(f"shop-signed returnable = {ctx.signed_term('returnable')!r}")
    out += [f"{_line(l)}: {_fact_origin(ctx, l, 'return_days')}" for l in (_requested_lines(ctx) or ctx.a["items"])]
    if c.actual not in (None, UNKNOWN):
        out.append(f"{c.actual} days {rule['operator']} {rule['value']} → {_yes(compare(c.actual, rule['operator'], rule['value']))}")
    out.append({PASS: "window and order flag agree → pass", FAIL: "→ fail", UNK: "not confirmed → uncertain"}[c.status])
    return out


def t_extracted(ctx, rule, c):
    name = rule["field"].split(".", 1)[1]
    return [f"{_line(l)}: {_fact_origin(ctx, l, name)}" for l in (_requested_lines(ctx) or ctx.a["items"])] + \
           [f"{_fmt_value(c.actual)} {rule['operator']} {_fmt_value(rule['value'])} → {c.status}"]


def t_injection(ctx, rule, c):
    texts = [f"line {l['line_no']} details" for l in ctx.a["items"]] + ["order description", "shop name"]
    if not ctx.injection_hits:
        return [f"scanned {len(texts)} texts ({', '.join(texts)}) with the fixed patterns and the unexplained-prose count",
                "no pattern matched, residual below threshold → pass"]
    out = []
    for hit in ctx.injection_hits:
        for h in hit["hits"]:
            if h.get("class") == "residual":
                out.append(f"{hit['where']}: too much text that isn't product description (unexplained-prose residual)")
            else:
                via = "" if h.get("via", "plain") == "plain" else f" (after {h['via']})"
                out.append(f"{hit['where']}: pattern “{h['rule']}” matched {_q(h['excerpt'], 60)}{via}")
    out.append("any match → uncertain (it can only make the decision stricter)")
    return out


def t_lookalike(ctx, rule, c):
    from .lookalike import THRESHOLD, similarity
    m = ctx.merchant
    familiar = dict(ctx.profile.familiar_merchants)
    if m["merchant_id"] in familiar:
        return [f"merchant ID {m['merchant_id']} is itself a shop you know → not a lookalike"]
    if ctx.lookalike:
        lk = ctx.lookalike
        return [f"new merchant ID {m['merchant_id']} named {_q(m['merchant_name'], 40)}",
                f"closest familiar shop: {_q(lk['imitates_name'], 40)} ({lk['imitates_id']}), name similarity {lk['score']:.2f} ≥ {THRESHOLD}",
                "different ID, similar name → uncertain"]
    best = max(((similarity(m["merchant_name"], n), n) for n in familiar.values()), default=(0.0, None))
    return [f"new merchant ID {m['merchant_id']} named {_q(m['merchant_name'], 40)}",
            f"closest familiar name: {_q(best[1], 40) if best[1] else 'none'}, similarity {best[0]:.2f} < {THRESHOLD} → pass"]


def t_duplicate(ctx, rule, c):
    e = c.extra or {}
    if e.get("previous"):
        r = ctx.ledger.get(e["previous"])
        mins = int((ctx.ts - r.timestamp).total_seconds() // 60)
        return [f"earlier attempt {r.source_id}: same shop {r.merchant_id}, same items {', '.join(r.item_ids)}",
                f"price {chf(r.amount_chf)} vs {chf(ctx.a['billing_amount_chf'])} (within 5%), {mins} min earlier (≤ 24 h), {r.status}",
                "→ uncertain"]
    prior = [r for r in ctx.ledger.prior(ctx.ts) if r.merchant_id == ctx.merchant["merchant_id"]]
    return [f"earlier attempts at {ctx.merchant['merchant_id']} in this run: {len(prior)}",
            "none with the same items, price within 5% and within 24 h → pass"]


def t_split(ctx, rule, c):
    e = c.extra or {}
    cap = ctx.purchase_cap()
    if cap is None:
        return ["no per-order limit in your rules → nothing to split around"]
    if e.get("combined"):
        return [f"other orders at this shop in the last 30 min + this one = {chf(e['combined'])}",
                f"{e['combined']:.2f} > limit {cap:.2f}, while this order alone is within it → uncertain"]
    return [f"orders at this shop in the last 30 min don't add up past the {chf(cap)} limit → pass"]


def t_price(ctx, rule, c):
    out = []
    for l in ctx.a["items"]:
        ref = ctx.pack.items.get(l["item_id"])
        unit = ctx.pack.to_chf(l["unit_price"], l["currency"])
        if not ref:
            out.append(f"{_line(l)}: no reference price for {l['item_id']} → skipped")
            continue
        inside = ref["unit_price_min_chf"] <= unit <= ref["unit_price_max_chf"]
        out.append(f"{_line(l)}: unit {chf(unit)} vs reference {chf(ref['unit_price_min_chf'])}–{chf(ref['unit_price_max_chf'])} → "
                   f"{'inside' if inside else 'outside'}")
    return out


def t_session(ctx, rule, c):
    a, p = ctx.a, ctx.profile
    dev, cc = a["customer_device_id"], ctx.merchant["merchant_country"]
    known_dev = dev in p.devices or dev in ctx.ledger.approved_devices()
    known_cc = cc in p.countries or cc in ctx.ledger.approved_countries()
    return [f"device {dev}: {'used before on this card' if known_dev else 'never used on this card'} ({len(p.devices)} known)",
            f"attempts in the last 10 min = {a['recent_attempt_count_10m']} ({'≥ 2: a burst' if a['recent_attempt_count_10m'] >= 2 else '< 2'})",
            f"shop country {cc}: {'bought from before' if known_cc else 'never bought from'}",
            "→ " + ("pass" if c.status == PASS else "uncertain")]


def t_country_expected(ctx, rule, c):
    cc = ctx.merchant["merchant_country"]
    return [f"shop country = {cc} (transaction data)", f"{cc} in {_fmt_value(rule['value'])} → {_yes(c.status == PASS)}",
            "→ " + ("pass" if c.status == PASS else "uncertain (a profile signal can ask, never decline)")]


def t_requote(ctx, rule, c):
    rel = ctx.a.get("related_authorization_id")
    if not rel:
        return ["related_authorization_id = null → not a re-quote"]
    prev = ctx.ledger.get(rel)
    if not prev:
        return [f"re-quote of {rel}, not seen in this run → pass"]
    return [f"re-quote of {prev.source_id} ({prev.status}); its security flags: {', '.join(prev.security_flags) or 'none'}",
            f"→ {c.status}"]


TRACES = {
    "authorization.billing_amount_chf": t_amount, "derived.period_spend_chf": t_period,
    "derived.basket_units": t_units, "derived.unrequested_lines": t_unrequested,
    "derived.quasi_cash_lines": t_quasi_cash, "derived.recurring_lines": t_recurring,
    "derived.merchant_purchases_365d": t_merchant_count, "derived.merchant_purchases_ever": t_merchant_count,
    "extracted.size": t_size, "extracted.return_days": t_return_days,
    "security.merchant_text_clean": t_injection, "derived.not_lookalike": t_lookalike,
    "derived.not_duplicate": t_duplicate, "derived.no_split_order": t_split, "derived.price_plausible": t_price,
    "derived.session_integrity": t_session, "derived.requote_clean": t_requote,
    "derived.shop_country_expected": t_country_expected,
}


def trace_for(ctx: Ctx, rule: dict, check: Check) -> list[str]:
    f = rule["field"]
    fn = TRACES.get(f) or (t_authorization if f.startswith("authorization.") else t_items if f.startswith("items.")
                           else t_extracted if f.startswith("extracted.") else None)
    if fn is None:
        return []
    try:
        return fn(ctx, rule, check)
    except Exception as exc:  # a trace must never change or break a decision
        return [f"(trace unavailable: {type(exc).__name__})"]


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
        check = spec.evaluate(ctx, rule)
        check.trace = trace_for(ctx, rule, check)
        return check
    except (KeyError, TypeError, ValueError) as exc:
        return Check(rule["field"], UNK, f"Could not evaluate this check ({type(exc).__name__}); treated as uncertain.",
                     "rule_not_understood")


# checks in the customer's own words (shop names, product descriptions, how often), judged flexibly
from .judged import register as _register_judged  # noqa: E402

_register_judged(FIELDS, TRACES)
