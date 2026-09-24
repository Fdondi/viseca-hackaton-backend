"""Checks written in the customer's own words, judged flexibly (no fixed lists).

    derived.shop_named          "only from Migros or Coop": the shop, by name
    derived.product_is          "only lactose-free milk": what every basket line must be
    derived.period_order_count  "every week": how many orders in a rolling window

Plain matching comes first (whole words, hyphens and case ignored). Only when that can't
decide does the model judge, and it can never approve on its own say-so:

  * a product judgment must quote words that really are in the product's name or description;
  * shop text flagged as manipulation is never shown to the model;
  * a name that is a near-miss spelling of the one asked for is a possible lookalike → ask;
  * model unavailable, slow or unsure → uncertain → the customer's uncertainty policy (ask).

Registered into fields.FIELDS at import time (see the end of fields.py).
"""
from __future__ import annotations

import re
from difflib import SequenceMatcher

from typing import TYPE_CHECKING

if TYPE_CHECKING:   # fields imports this module at its end to register the checks: import it lazily here
    from .fields import Check, Ctx

NEAR_MISS = 0.8   # similarity above which a different spelling looks like an imitation


def norm(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[-_/.,;:()'\"]", " ", (text or "").lower())).strip()


def shown_in(text: str, evidence: str) -> bool:
    """The evidence words appear in the text, in order; a word may carry a short ending there
    ('laktosefrei' shows in 'Laktosefreie Milch'), but not be part of a longer word."""
    ev = norm(evidence).split()
    if not ev:
        return False
    rx = r"(?:^| )" + " ".join(re.escape(w) + r"\w{0,2}" for w in ev) + r"(?: |$)"
    return re.search(rx, norm(text)) is not None


def contains_words(haystack: str, phrase: str) -> bool:
    """Every word of `phrase` appears as a whole word in `haystack` (order free, hyphens ignored)."""
    words = norm(phrase).split()
    hay = f" {norm(haystack)} "
    return bool(words) and all(f" {w} " in hay for w in words)


def _values(rule: dict) -> list[str]:
    return [str(v) for v in (rule["value"] if isinstance(rule["value"], list) else [rule["value"]])]


# ------------------------------------------------------------------ the shop, by name
def _near_miss(merchant: str, wanted: str) -> bool:
    m, w = norm(merchant), norm(wanted)
    if not m or not w:
        return False
    parts = m.split() + [m]
    return any(SequenceMatcher(None, p, w).ratio() >= NEAR_MISS for p in parts)


def is_name(value: str) -> bool:
    """'Coop' is a shop's name; 'farmer shops' is a kind of shop (the customer's own words, as written)."""
    return value[:1].isupper()


def ev_shop_named(ctx: Ctx, rule: dict) -> Check:
    """The shop is one of the shops (names) or kinds of shop the customer gave."""
    from .fields import FAIL, PASS, UNK, Check
    from . import llm
    allowed, op = _values(rule), rule["operator"]
    names = [v for v in allowed if is_name(v)]
    m = ctx.merchant
    shop = m["merchant_name"]
    listed = ", ".join(allowed)
    hit = next((v for v in allowed if contains_words(shop, v)), None)   # 'Coop Pronto' is Coop
    how = "by name"
    if hit is None:
        close = next((n for n in names if _near_miss(shop, n)), None)
        if close:
            return Check(rule["field"], UNK, f"'{shop}' looks like '{close}' but is spelled differently: it may be an "
                         "imitation, so we ask you.", "lookalike_merchant", provenance="derived", security=True,
                         actual=shop, expected=allowed)
        if any(h["where"] == "shop name" for h in ctx.injection_hits):
            return Check(rule["field"], UNK, f"The shop name '{shop}' contains instructions, so we can't judge it.",
                         "fact_unknown", provenance="detector", security=True, actual=shop, expected=allowed)
        verdict = llm.judge_shop({k: m.get(k) for k in ("merchant_name", "merchant_category", "merchant_mcc",
                                                        "merchant_city", "merchant_country")}, allowed)
        if verdict is None or not verdict.get("sure", True):
            why = f" (the AI isn't sure: {verdict['reason']})" if verdict and verdict["reason"] else ""
            return Check(rule["field"], UNK, f"We couldn't tell whether '{shop}' is one of: {listed}{why}.", "fact_unknown",
                         provenance="model" if verdict else "derived", actual=shop, expected=allowed)
        hit, how = verdict["match"], "judged by AI" + (f": {verdict['reason']}" if verdict["reason"] else "")
    ok = (hit is not None) == (op in ("in", "="))
    if hit is not None:
        text = f"'{shop}' is {hit if is_name(hit) else 'one of the ' + hit} ({how})."
    else:
        text = f"'{shop}' is none of: {listed} ({how})."
    return Check(rule["field"], PASS if ok else FAIL, text, "rule_failed",
                 provenance="derived" if how == "by name" else "model", actual=shop, expected=allowed,
                 extra={"matched": hit, "how": how})


def d_shop_named(r: dict) -> str:
    allowed = ", ".join(_values(r))
    how = "names by their words; kinds of shop and related shops judged by AI"
    if r["operator"] in ("not_in", "!="):
        return f"Never from: {allowed} ({how})."
    return f"Only from: {allowed} ({how})."


# ------------------------------------------------------------------ what every line must be
def _line_texts(ctx: Ctx, line: dict) -> tuple[str, str]:
    """Name and description; a description flagged as manipulation is not used (the wall)."""
    facts = next((f for f in ctx.facts if f.line_no == line["line_no"]), None)
    details = "" if facts is not None and facts.walled_off else (line.get("item_details") or "")
    return line["item_name"], details


def _line_is(ctx: Ctx, line: dict, wanted: list[str]) -> tuple[bool | None, str]:
    """True/False with the words it rests on, or None when nobody can tell."""
    from . import llm
    name, details = _line_texts(ctx, line)
    for w in wanted:
        if contains_words(f"{name} {details}", w):
            return True, f"'{name}' matches '{w}' (in its name or description)"
    text = f"{name} {details}"
    unsure = []
    for w in wanted:
        verdict = llm.judge_product(name, details, w)
        if verdict is None:
            unsure.append(f"we couldn't confirm '{name}' is {w}")
            continue
        # every part of what was asked needs words that are really in the product's text
        missing = [p["part"] for p in verdict["parts"] if not shown_in(text, p["evidence"])]
        if verdict["parts"] and not missing:
            ev = "; ".join(f"{p['part']}: '{p['evidence']}'" for p in verdict["parts"])
            return True, f"'{name}' is {w} (judged by AI from the product's words: {ev})"
        unsure.append(f"nothing in the name or description of '{name}' shows '{', '.join(missing)}'")
    return None, "; ".join(unsure)


def ev_product_is(ctx: Ctx, rule: dict) -> Check:
    from .fields import FAIL, PASS, UNK, Check
    wanted, positive = _values(rule), rule["operator"] in ("in", "=")
    results = [(line, *_line_is(ctx, line, wanted)) for line in ctx.a["items"]]
    notes = [why for _, _, why in results]
    judged = any("judged by AI" in n for n in notes)
    prov = "model" if judged else "claimed"
    if positive:
        # the customer asked us to ask when it's not clearly what they wanted: never a decline
        if all(ok is True for _, ok, _ in results):
            return Check(rule["field"], PASS, "; ".join(notes) + ".", "item_not_requested", provenance=prov,
                         expected=wanted)
        missing = [why for _, ok, why in results if ok is not True]
        return Check(rule["field"], UNK, "Not clearly what you asked for: " + "; ".join(missing) + ".",
                     "item_not_requested", provenance=prov, expected=wanted)
    if any(ok is True for _, ok, _ in results):
        return Check(rule["field"], FAIL, "You asked for none of this: " + "; ".join(
            why for _, ok, why in results if ok is True) + ".", "item_not_requested", provenance=prov, expected=wanted)
    if any(ok is None for _, ok, _ in results):
        return Check(rule["field"], UNK, "; ".join(notes) + ".", "item_not_requested", provenance=prov, expected=wanted)
    return Check(rule["field"], PASS, "; ".join(notes) + ".", "item_not_requested", provenance=prov, expected=wanted)


def d_product_is(r: dict) -> str:
    what = " or ".join(_values(r))
    if r["operator"] in ("not_in", "!="):
        return f"Nothing that is {what} (checked in the product name and description)."
    return f"Every item is {what} (checked in the product name and description; if we can't confirm it, we ask you)."


# ------------------------------------------------------------------ how often
def ev_order_count(ctx: Ctx, rule: dict) -> Check:
    from .fields import PASS, UNK, Check
    days = int(rule.get("period_days") or 7)
    earlier = [r for r in ctx.ledger.window(ctx.ts, days) if r.live_id != ctx.a["authorization_id"]]
    n = len(earlier) + 1
    limit = float(rule["value"])
    ok = {"<=": n <= limit, "<": n < limit, "=": n <= limit}.get(rule["operator"], n <= limit)
    text = (f"This is order {n} in {days} days (you asked for at most {rule['value']:g})." if isinstance(rule["value"], (int, float))
            else f"This is order {n} in {days} days.")
    # more often than asked is unusual, not forbidden: the customer decides
    return Check(rule["field"], PASS if ok else UNK, text, "period_limit_exceeded", provenance="run",
                 actual=n, expected=rule["value"], extra={"earlier": [r.live_id for r in earlier]})


def d_order_count(r: dict) -> str:
    days = r.get("period_days") or 7
    n = r["value"]
    return f"At most {n:g} order{'s' if n != 1 else ''} in any {days} days (more often → we ask you)." \
        if isinstance(n, (int, float)) else f"Orders in any {days} days: {n}."


def register(fields: dict, traces: dict | None = None) -> None:
    from .fields import FieldSpec
    fields.update({
        "derived.shop_named": FieldSpec(ev_shop_named, d_shop_named),
        "derived.product_is": FieldSpec(ev_product_is, d_product_is),
        "derived.period_order_count": FieldSpec(ev_order_count, d_order_count),
    })
    if traces is not None:
        traces.update({
            "derived.shop_named": lambda ctx, rule, c: [f"shop name = {ctx.merchant['merchant_name']} (transaction data)",
                                                        c.text, f"→ {c.status}"],
            "derived.product_is": lambda ctx, rule, c: [c.text, f"→ {c.status}"],
            "derived.period_order_count": lambda ctx, rule, c: [c.text, f"→ {c.status}"],
        })

