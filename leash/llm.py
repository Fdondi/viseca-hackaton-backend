"""Optional language model: Apertus (Swisscom, preferred by the organisers) or OpenAI.

Provider selection (LEASH_LLM_PROVIDER = apertus | openai | none; default auto):
  * apertus  APERTUS_KEY, APERTUS_BASE_URL, APERTUS_MODEL (defaults: Swiss AI Weeks endpoint, Apertus-v1.5-70B)
  * openai   OPENAI_API_KEY, LEASH_OPENAI_MODEL
Keys are read from the environment or from leash/.env (the environment wins). Swisscom
tokens expire after 60 minutes; an expired key simply means "no model".

The model is never trusted and never required:
  * setup-time compiler: proposes rules as JSON; every rule must pass `validate_rule`
    (schema + field catalogue) and is shown as "suggested by AI, please review";
  * extraction fallback (off unless LEASH_LLM_EXTRACT=1): fills a fact regex left unknown,
    only if the value literally appears in the shop text, never for a line the injection
    detector flagged, with a hard timeout. Failure = unknown.
The purchase decision itself stays deterministic.
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

from .compiler import Draft, validate_rule
from .fields import FIELDS, OP_WORDS
from .wall import UNKNOWN, LineFacts, canonical_size

APERTUS_BASE_URL = "https://api.swisscom.com/products/swiss-ai-weeks/apertus-1.5-70b/v1"
APERTUS_MODEL = "swiss-ai/Apertus-v1.5-70B"
ENV_FILE = Path(__file__).resolve().parent.parent / ".env"
STATUS = {"provider": None, "model": None, "last_error": None, "calls": 0, "last_latency_s": None}


def _load_env_file() -> None:
    if not ENV_FILE.exists():
        return
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip().removeprefix("export ").strip(), v.strip().strip('"').strip("'"))


def config() -> dict | None:
    """Which provider to use, or None."""
    _load_env_file()
    want = os.environ.get("LEASH_LLM_PROVIDER", "auto").lower()
    if want == "none":
        return None
    if want in ("auto", "apertus") and os.environ.get("APERTUS_KEY"):
        return {"provider": "apertus", "api_key": os.environ["APERTUS_KEY"],
                "base_url": os.environ.get("APERTUS_BASE_URL", APERTUS_BASE_URL),
                "model": os.environ.get("APERTUS_MODEL", APERTUS_MODEL)}
    if want in ("auto", "openai") and os.environ.get("OPENAI_API_KEY"):
        return {"provider": "openai", "api_key": os.environ["OPENAI_API_KEY"], "base_url": None,
                "model": os.environ.get("LEASH_OPENAI_MODEL", "luna")}
    return None


def available() -> bool:
    if config() is None:
        return False
    try:
        import openai  # noqa: F401  (both providers speak the OpenAI API)
    except ImportError:
        return False
    return True


def _extract_json(text: str) -> dict:
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    try:
        return json.loads(text)
    except ValueError:
        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            raise
        return json.loads(m.group(0))


TRANSCRIPT: list[dict] = []   # every model exchange, for audits and the model-comparison report


def chat_json(prompt: str, schema: dict, name: str, timeout: float, max_tokens: int = 800) -> dict | None:
    """One JSON answer from the configured model; None on any failure (recorded in STATUS and TRANSCRIPT)."""
    cfg = config()
    if cfg is None:
        return None
    from openai import OpenAI

    client = OpenAI(api_key=cfg["api_key"], base_url=cfg["base_url"], timeout=timeout, max_retries=0)
    STATUS.update(provider=cfg["provider"], model=cfg["model"])
    system = "You convert text into JSON. Output only JSON that matches the schema. Never add explanations inside values."
    messages = [{"role": "system", "content": system}, {"role": "user", "content": prompt}]
    if cfg["provider"] == "openai":   # reasoning models: completion budget includes reasoning; no temperature
        params = {"max_completion_tokens": max(max_tokens * 6, 4000)}
    else:
        params = {"max_tokens": max_tokens, "temperature": 0}
    attempts = [{"response_format": {"type": "json_schema", "json_schema": {"name": name, "schema": schema, "strict": True}}},
                {"response_format": {"type": "json_object"}},
                {}]
    entry = {"provider": cfg["provider"], "model": cfg["model"], "task": name, "system": system, "prompt": prompt,
             "raw": None, "parsed": None, "error": None, "format": None, "latency_s": None}
    TRANSCRIPT.append(entry)
    t0 = time.perf_counter()
    deadline = t0 + timeout
    for extra in attempts:
        retries = 0
        while True:
            try:
                left = deadline - time.perf_counter()
                if left <= 0.05:
                    entry["error"] = STATUS["last_error"] = "time budget used up (retries included)"
                    return None
                resp = client.with_options(timeout=left).chat.completions.create(
                    model=cfg["model"], messages=messages, **params, **extra)
                raw = resp.choices[0].message.content or ""
                entry.update(raw=raw, format=(extra.get("response_format") or {}).get("type", "plain"),
                             latency_s=round(time.perf_counter() - t0, 2), error=None, retries=retries)
                STATUS.update(calls=STATUS["calls"] + 1, last_latency_s=entry["latency_s"], last_error=None)
                entry["parsed"] = _extract_json(raw)
                return entry["parsed"]
            except Exception as exc:  # unsupported parameter → adjust; throttled → back off; auth/network → give up
                msg = str(exc)
                entry["error"] = STATUS["last_error"] = f"{type(exc).__name__}: {msg[:200]}"
                entry["latency_s"] = round(time.perf_counter() - t0, 2)
                status = getattr(exc, "status_code", None)
                if "temperature" in msg and "temperature" in params:
                    params.pop("temperature")
                    continue
                if status == 429 and retries < MAX_RETRIES:
                    wait = _retry_after(exc, retries)
                    if time.perf_counter() + wait < deadline - 0.2:   # only if a retry can still finish in time
                        retries += 1
                        STATUS["throttled"] = STATUS.get("throttled", 0) + 1
                        time.sleep(wait)
                        continue
                    return None
                if status in (401, 403, 429) or "Timeout" in type(exc).__name__ or "Connection" in type(exc).__name__:
                    return None
                break
    return None


MAX_RETRIES = 4


def _retry_after(exc, attempt: int) -> float:
    """Server hint first (Retry-After or Swisscom's x-ratelimit-reset like '1s'), else exponential backoff with jitter."""
    import random
    headers = getattr(getattr(exc, "response", None), "headers", None) or {}
    for h in ("retry-after", "x-ratelimit-reset", "x-ratelimit-reset-requests", "x-ratelimit-reset-tokens"):
        v = headers.get(h)
        if v:
            m = re.fullmatch(r"\s*([\d.]+)\s*(ms|s|m)?\s*", str(v))
            if m:
                n = float(m.group(1)) * {"ms": 0.001, "s": 1, "m": 60, None: 1}[m.group(2)]
                return min(max(n, 0.2), 30.0) + random.uniform(0, 0.25)
    return min(0.5 * 2 ** attempt, 8.0) + random.uniform(0, 0.25)


RULE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["rules"],
    "properties": {
        "rules": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["field", "operator", "value", "period_days"],
                "properties": {
                    "field": {"type": "string", "enum": sorted(set(FIELDS) | {"items.item_category", "items.item_id",
                                                                              "authorization.merchant.merchant_mcc",
                                                                              "authorization.merchant.merchant_country",
                                                                              "authorization.fulfillment_method",
                                                                              "authorization.order_returnable"})},
                    "operator": {"type": "string", "enum": list(OP_WORDS)},
                    "value": {"anyOf": [{"type": "number"}, {"type": "string"}, {"type": "array", "items": {"type": "string"}}]},
                    "period_days": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
                },
            },
        }
    },
}


FIELD_DOCS = {
    "authorization.billing_amount_chf": "order total in CHF incl. delivery (per order; use <= with a number)",
    "derived.period_spend_chf": "sum of approved orders over a rolling window incl. this one (use <= with a number and period_days)",
    "items.item_category": "category of EVERY basket line (use in / not_in with a list of categories)",
    "items.item_id": "catalogue item of every basket line (use in with a list of item ids)",
    "derived.basket_units": "total number of units in the basket (number)",
    "derived.unrequested_lines": "number of basket lines the customer did not ask for (use = 0 to forbid add-ons)",
    "derived.quasi_cash_lines": "number of gift-card/voucher lines (= 0 forbids them)",
    "derived.recurring_lines": "number of subscription/membership/recurring-billing lines (= 0 forbids them)",
    "derived.merchant_purchases_ever": "earlier purchases at this shop on this card (>= 1 means 'a shop I have used')",
    "derived.merchant_purchases_365d": "purchases at this shop in the last 12 months (>= 3 means 'a shop I use regularly')",
    "authorization.merchant.merchant_mcc": "shop type code, e.g. 5941 sports, 5732 electronics, 5651 clothing, 5411 grocery",
    "authorization.merchant.merchant_country": "two-letter shop country (use in / not_in with a list)",
    "authorization.fulfillment_method": "delivery / pickup / digital",
    "authorization.order_returnable": "'true' if the order must be returnable",
    "extracted.size": "product size as stated by the shop (use in with a list of strings)",
    "extracted.return_days": "return window in days stated by the shop (use >= with a number)",
    "derived.session_integrity": "'true' = pause when someone else may be using the card",
}


def propose_rules(instruction: str, draft: Draft, categories: list[str], timeout: float = 30.0) -> list[dict]:
    """Rules the deterministic compiler may have missed, validated before anyone sees them."""
    if not available():
        return []
    docs = "\n".join(f"- {k}: {v}" for k, v in FIELD_DOCS.items())
    prompt = (
        "Translate this bank customer's instruction to their shopping agent into wallet rules. Include EVERY limit or "
        "restriction the customer states and nothing they do not state. Convert amounts written in words to numbers. "
        "A thing the customer forbids ('never', 'no', 'don't') becomes a not_in / = 0 rule, never an 'in' rule.\n"
        f"Allowed fields:\n{docs}\nItem categories: {', '.join(sorted(categories))}\n"
        f"Instruction: {instruction}\n"
        'Answer as {"rules": [{"field": ..., "operator": ..., "value": ..., "period_days": null or a number}]}.'
    )
    data = chat_json(prompt, RULE_SCHEMA, "rules", timeout)
    out = []
    for r in (data or {}).get("rules", []) if isinstance(data, dict) else []:
        if not isinstance(r, dict):
            continue
        r = {k: v for k, v in r.items() if v is not None and k in ("field", "operator", "value", "period_days")}
        if r.get("field") == "derived.period_spend_chf":
            r["scope"] = "period"
        if validate_rule(r) is None:
            out.append(r)
    return out


def _key(r: dict) -> tuple:
    v = r["value"]
    v = tuple(sorted(map(str, v))) if isinstance(v, list) else (float(v) if isinstance(v, (int, float)) else str(v))
    pd = r.get("period_days") if r["field"] == "derived.period_spend_chf" else None
    return r["field"], r["operator"], v, pd


def _requested_categories(rules: list[dict]) -> set[str]:
    from .data import load
    pack = load()
    cats: set[str] = set()
    for r in rules:
        if r["field"] == "items.item_id" and r["operator"] == "in":
            cats |= {pack.items[i]["item_category"] for i in r["value"] if i in pack.items}
        if r["field"] == "items.item_category" and r["operator"] == "in":
            cats |= set(r["value"])
    return cats


# What the customer's own words must contain for a suggested rule on this field to be grounded.
GROUNDING = {
    "authorization.merchant.merchant_country": r"swiss|switzerland|\bch\b|country|countr|abroad|foreign|outside|domestic|local|"
                                               r"german|france|french|ital|austria|\beu\b|europe|\buk\b|brit|\bus\b|americ",
    "authorization.merchant.merchant_mcc": r"specialist|retailer|type of (?:shop|store)|\bstore\b|\bshop\b.*\b(?:sport|electronic|cloth|grocer|book)",
    "authorization.order_returnable": r"return|sent back|send back|refund",
    "extracted.return_days": r"return|sent back|send back",
    "extracted.size": r"\bsize\b",
    "derived.session_integrity": r"someone else|other than me|not me|hijack|stolen|compromis|driving|device",
    "derived.unrequested_lines": r"\badd\b|add-on|addon|extra|anything else|nothing else|only (?:what|the)|did not ask|didn't ask",
    "derived.merchant_purchases_ever": r"before|used|know|trust|familiar|usual|regular|bought from|shopped",
    "derived.merchant_purchases_365d": r"regular|usual|often|always",
    "derived.basket_units": r"\b(?:1|2|3|one|two|three|single|a pair of|a pack of)\s+(?:\w+\s+){0,3}?(?:items?|products?|packs?|pieces?|units?|pairs?|things?)\b",
    "derived.period_spend_chf": r"week|month|\bday|daily|total|across|altogether|in sum|overall",
    "authorization.fulfillment_method": r"deliver|pick ?up|collect|digital|download|email",
    "derived.quasi_cash_lines": r"gift|voucher|store credit|cash",
    "derived.recurring_lines": r"subscri|member|recurring|monthly|renew|sign me up",
}


def grounded(instruction: str, rule: dict) -> bool:
    low = instruction.lower()
    f = rule["field"]
    if f == "items.item_category":
        from .compiler import CATEGORY_WORDS
        vals = rule["value"] if isinstance(rule["value"], list) else [rule["value"]]
        return all(any(re.search(p, low) for p in CATEGORY_WORDS.get(v, [re.escape(v.replace("_", " "))])) or
                   re.search(re.escape(v.split("_")[0][:5]), low) for v in map(str, vals))
    if f == "items.item_id":
        return True  # catalogue matches are checked against the item list separately
    rx = GROUNDING.get(f)
    return True if rx is None else bool(re.search(rx, low))


def _in_requested_product(vals: list, existing: list[dict], pack) -> str | None:
    """'27-inch' proposed as a fact, when the customer already picked '27-inch computer monitor'."""
    names = [pack.items[i]["item_name"] for r in existing if r["field"] == "items.item_id" and r["operator"] in ("in", "=")
             for i in (r["value"] if isinstance(r["value"], list) else [r["value"]]) if i in pack.items]
    for n in names:
        if vals and all(str(v).strip() and str(v).strip().lower() in n.lower() for v in vals):
            return n
    return None


def review(instruction: str, existing: list[dict], proposed: list[dict],
           safety_net: set[str] = frozenset()) -> tuple[list[dict], list[dict]]:
    """Keep only suggestions that add something, agree with the request, and are grounded in the customer's words.
    `safety_net`: fields of the always-on checks already in `existing`, so a duplicate of one says so."""
    have = {_key(r) for r in existing}
    fields_have = {r["field"] for r in existing}
    wanted = _requested_categories(existing)
    numbers = {float(n.replace("'", "")) for n in re.findall(r"\d+(?:'\d{3})*(?:\.\d+)?", instruction)}
    kept, dropped = [], []
    for r in proposed:
        why = None
        vals = r["value"] if isinstance(r["value"], list) else [r["value"]]
        op = {"=": "in", "!=": "not_in"}.get(r["operator"], r["operator"]) if r["field"].startswith("items.") else r["operator"]
        from .data import load
        pack = load()
        periods = [e for e in existing if e["field"] == "derived.period_spend_chf"]
        if isinstance(r["value"], list) and not [v for v in r["value"] if str(v).strip()]:
            why = "empty value"
        elif r["field"] == "items.item_category" and not set(map(str, vals)) <= pack.item_categories:
            why = f"not a category we know ({', '.join(sorted(set(map(str, vals)) - pack.item_categories))})"
        elif r["field"] == "items.item_id" and not set(map(str, vals)) <= set(pack.items):
            why = "not an item in the catalogue"
        elif r["field"] == "authorization.billing_amount_chf" and any(
                isinstance(r["value"], (int, float)) and float(r["value"]) >= float(p["value"]) for p in periods):
            why = "implied by your limit over a period"
        elif r["field"] in safety_net:
            why = "already one of the always-on safety checks"
        elif _key(r) in have or (r["field"] in fields_have and r["field"] not in ("items.item_category",)):
            why = "already covered by a rule from your words"
        elif r["field"] == "items.item_category" and op == "not_in" and wanted & set(map(str, vals)):
            why = "contradicts what you asked to buy"
        elif r["field"] == "items.item_category" and op == "in" and wanted and not wanted <= set(map(str, vals)):
            why = "contradicts what you asked to buy"
        elif r["field"] == "items.item_category" and op == "in" and wanted and wanted <= set(map(str, vals)):
            why = "implied by the item you asked for"
        elif r["field"] == "derived.period_spend_chf" and not r.get("period_days"):
            why = "a period limit without a period length"
        elif r["field"] == "authorization.order_returnable" and any(e["field"] == "extracted.return_days" for e in existing):
            why = "implied by your return-window rule"
        elif r["field"].startswith("extracted.") and (named := _in_requested_product(vals, existing, pack)):
            why = f"already part of the product you asked for ({named})"
        elif r["field"] == "extracted.size" and any(canonical_size(None, str(v)) == UNKNOWN for v in vals):
            why = "not a clothing or shoe size, which is what this check compares"
        elif not grounded(instruction, r):
            why = "nothing in your words asks for this"
        elif isinstance(r["value"], (int, float)) and not isinstance(r["value"], bool) \
                and r["field"] in ("authorization.billing_amount_chf", "derived.period_spend_chf") \
                and float(r["value"]) not in numbers:
            why = "this amount is not in your instruction"
        if why:
            dropped.append({"rule": r, "why": why})
        else:
            kept.append(r)
            have.add(_key(r))
    return kept, dropped


def augment(draft: Draft, categories: list[str]) -> Draft:
    proposed = propose_rules(draft.instruction, draft, categories)
    safety = {n["rule"]["field"] for n in draft.notes if n["tier"] == "safety net"}
    kept, dropped = review(draft.instruction, draft.hard_rules, proposed, safety)
    for r in kept:
        draft.add(r, "suggested by AI — please review", "llm")
    STATUS["last_review"] = {"proposed": len(proposed), "kept": kept, "dropped": dropped}
    from .compiler import refresh_gaps
    refresh_gaps(draft, grounded)   # the questions follow the final rules, model suggestions included
    return draft


EXTRACT_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["size", "return_days"],
    "properties": {
        "size": {"type": "string", "pattern": "^(unknown|[0-9]{1,2}(\\.5)?|XXS|XS|S|M|L|XL|XXL)$"},
        "return_days": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
    },
}


def extract_fallback(items: list[dict], facts: list[LineFacts], timeout: float | None = None) -> list[LineFacts]:
    if not available():
        return facts
    timeout = timeout or float(os.environ.get("LEASH_LLM_EXTRACT_TIMEOUT", "2"))
    for line, f in zip(items, facts):
        if f.walled_off or (f.size != UNKNOWN and f.return_days != UNKNOWN):
            continue
        text = line.get("item_details") or ""
        data = chat_json(
            "From this shop text, give the product size exactly as written (just the size, e.g. 43 or M) or 'unknown', "
            "and the number of days returns are accepted (null if not stated). Text: " + json.dumps(text),
            EXTRACT_SCHEMA, "facts", timeout, max_tokens=40)
        if not isinstance(data, dict):
            continue
        size = str(data.get("size", "unknown")).strip().split()[0] if data.get("size") else "unknown"
        if f.size == UNKNOWN and size.lower() != "unknown" and re.search(rf"\b{re.escape(size)}\b", text, re.I):
            f.size = canonical_size(None, size)
            f.sources["size"] = "claimed (model, grounded)"
        days = data.get("return_days")
        if f.return_days == UNKNOWN and isinstance(days, int) and re.search(rf"\b{days}\b", text):
            f.return_days = days
            f.sources["return_days"] = "claimed (model, grounded)"
    return facts


def extraction_enabled() -> bool:
    return os.environ.get("LEASH_LLM_EXTRACT") == "1" and available()
