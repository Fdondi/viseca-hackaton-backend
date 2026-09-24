"""Decision API for a separately built frontend: our rule engine as a JSON service.

The frontend sends a customer's instruction, gets back the rules we understood in
plain language, confirms them, then sends purchases and gets `approve`, `decline`
or `step_up` with the reasons. It never talks to the hosted challenge API.

    POST /v1/mandates/compile   instruction → proposed rules (not stored)
    POST /v1/mandates           the customer confirms → active mandate
    POST /v1/mandates/{id}/rules/parse   new words → extra rules (not stored)
    POST /v1/mandates/{id}/rules         the customer approves → saved, used from the next decision
    POST /v1/decisions          a proposed purchase → approve | decline | step_up
    POST /v1/decisions/{id}/resolve   the customer answers a step_up
    POST /v1/ap2/simulate-checkout    simulator: shop + agent sign a purchase (AP2 presentation)

Interactive docs: /docs. Everything is in memory; a restart forgets it.
Optional auth: set LEASH_API_KEY and send `Authorization: Bearer <key>`.
Allowed browser origins: LEASH_API_CORS (comma-separated, default "*").
"""
from __future__ import annotations

import os
import re
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from .. import ap2, llm
from ..backtest import backtest
from ..compiler import Compiler, refresh_gaps, validate_rule
from ..data import load
from ..engine import ENGINE_VERSION, Engine, RevokedError
from ..events import assemble_event, iso, parse_ts, utcnow, validation_errors
from ..fields import SAFETY_NET, describe

STEP_UP_TIMEOUT_S = float(os.environ.get("LEASH_STEP_UP_TIMEOUT", "120"))
API_SCENARIO_ID = "SCEN9999"   # the event schema wants a scenario id; API purchases belong to none

app = FastAPI(
    title="Agent on a Leash: decision API",
    version="1.0",
    description="Turn a customer's instruction into confirmed rules, then decide each purchase an AI shopping agent "
                "proposes: approve, decline or step_up (ask the customer), with the reasons.",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in os.environ.get("LEASH_API_CORS", "*").split(",") if o.strip()],
    allow_methods=["*"], allow_headers=["*"],
)

PACK = load()
LOCK = threading.RLock()
USE_LLM = llm.available()
ENGINE = Engine(PACK, extractor_fallback=llm.extract_fallback if (USE_LLM and llm.extraction_enabled()) else None)
COMPILER = Compiler(PACK)
KEYRING = ap2.Keyring()                      # AP2 keys: Viseca one, the credential provider, the agent, shops
AP2_SIM = ap2.Ap2Sim(PACK, KEYRING)          # plays the shop and the agent for /v1/ap2/simulate-checkout
MANDATES: dict[str, dict] = {}
DECISIONS: dict[str, dict] = {}    # authorization_id → {mandate_id, customer_id, created, result}


def _auth(authorization: str | None = Header(default=None)) -> None:
    key = os.environ.get("LEASH_API_KEY")
    if key and authorization != f"Bearer {key}":
        raise HTTPException(401, "Send Authorization: Bearer <LEASH_API_KEY>.")


# ------------------------------------------------------------------ customers
def _card_for(customer_id: str) -> str:
    if customer_id not in PACK.customers:
        raise HTTPException(404, f"Unknown customer {customer_id}. GET /v1/customers lists them.")
    # the customer's own active online card with the most history (scenario data is not used here)
    accounts = {a["account_id"] for a in PACK.accounts.values() if a["customer_id"] == customer_id}
    cards = [c for c in PACK.cards.values()
             if c["account_id"] in accounts and c["status"] == "active" and c["online_enabled"] == "true"]
    if not cards:
        raise HTTPException(404, f"Customer {customer_id} has no active online card.")
    return max(cards, key=lambda c: (len(PACK.history_by_card.get(c["card_id"], [])), c["card_id"]))["card_id"]


def _controls(customer_id: str) -> dict:
    c = ENGINE.store.customer(customer_id)
    return {"merchant_flags": list(c.merchant_flags.values()), "alerts": c.alerts, "audit": c.audit[-50:]}


@app.get("/v1/health", tags=["service"])
def health():
    return {"status": "ok", "engine_version": ENGINE_VERSION, "model": _model_info()}


def _model_info() -> dict | None:
    cfg = llm.config() if USE_LLM else None
    return {"provider": cfg["provider"], "model": cfg["model"]} if cfg else None


@app.get("/v1/customers", tags=["customers"], dependencies=[Depends(_auth)])
def customers():
    out = []
    for cid, c in sorted(PACK.customers.items()):
        out.append({"customer_id": cid, "name": c["persona_name"], "home_region": c["home_region"],
                    "card_id": _card_for(cid), "shopping_preferences": c["shopping_preferences"]})
    return out


@app.get("/v1/customers/{customer_id}", tags=["customers"], dependencies=[Depends(_auth)])
def customer(customer_id: str):
    card = _card_for(customer_id)
    with LOCK:
        return {**PACK.customers[customer_id], "card_id": card, "profile": ENGINE.profile(card).summary(),
                "controls": _controls(customer_id),
                "mandates": [_mandate_out(m) for m in MANDATES.values() if m["customer_id"] == customer_id]}


class FlagIn(BaseModel):
    merchant_id: str
    mode: Literal["ask", "block", "remove"] = Field(description="ask = step_up every time; block = always decline; "
                                                                 "remove = not a concern")


@app.post("/v1/customers/{customer_id}/merchant-flags", tags=["customers"], dependencies=[Depends(_auth)])
def merchant_flag(customer_id: str, body: FlagIn):
    """The customer keeps, blocks or clears a shop the engine flagged (or flags one themselves)."""
    _card_for(customer_id)
    with LOCK:
        ENGINE.set_merchant_flag(customer_id, body.merchant_id, body.mode)
        return _controls(customer_id)


@app.get("/v1/merchants", tags=["customers"], dependencies=[Depends(_auth)])
def merchants():
    return [{k: m[k] for k in ("merchant_id", "merchant_name", "merchant_category", "merchant_mcc", "merchant_country",
                               "merchant_city")} for m in PACK.merchants.values()]


# ------------------------------------------------------------------ mandates
class CompileIn(BaseModel):
    customer_id: str = Field(examples=["CU0001"])
    instruction: str = Field(examples=["Buy one ordinary grocery item for CHF 20 or less from a shop I use regularly. "
                                       "Ask me when uncertain."])
    use_model: bool = Field(True, description="also ask the language model (Apertus) for rules; it can only add "
                                              "rules grounded in the customer's words")


def _parse(text: str, use_model: bool):
    """Customer's words → Draft (rules + notes), optionally with grounded model suggestions."""
    draft = COMPILER.compile(text)
    model = None
    if use_model and USE_LLM:
        llm.augment(draft, sorted(PACK.item_categories))
        rev = llm.STATUS.get("last_review") or {"proposed": 0, "dropped": []}
        model = {**{k: llm.STATUS[k] for k in ("provider", "model", "last_error", "last_latency_s")},
                 "proposed": rev["proposed"], "kept": len(rev.get("kept_view", [])),
                 "suggestions": [{"result": "kept", "text": _describe(k["rule"]), "rule": k["rule"], "quote": k["quote"],
                                  **({"ai_added": k["ai_added"]} if k.get("ai_added") else {})}
                                 for k in rev.get("kept_view", [])]
                                + [{"result": "dropped", "text": describe(d["rule"]) if d["rule"].get("value") not in (None, "", [])
                                    else f"A rule on '{d['rule']['field']}' with no value", "rule": d["rule"],
                                    "quote": d.get("quote"), "why": d["why"]} for d in rev["dropped"]],
                 "dropped": [{"text": describe(d["rule"]), "why": d["why"]} for d in rev["dropped"]]}
    refresh_gaps(draft)   # gaps are judged on the final rules: ours and the model's
    return draft, model


def _not_understood(text: str, draft, model, reasons: list[str] | None = None) -> HTTPException:
    """Nothing enforceable came out of the customer's words (after our parser, shop names and the model)."""
    return HTTPException(422, {
        "code": "no_rule_understood",
        "message": "We could not turn these words into any rule we can enforce. Say how much the agent may spend, "
                   "what it may buy, or which shops (e.g. 'groceries only, at most CHF 50 per order').",
        "text": text,
        "reasons": reasons or [],
        "unparsed": [g["text"] for g in draft.gaps if g["kind"] == "unparsed"],
        "model": model,
    })


def _describe(rule: dict) -> str:
    if rule["field"] == "authorization.merchant.merchant_id" and rule["operator"] in ("in", "="):
        ids = rule["value"] if isinstance(rule["value"], list) else [rule["value"]]
        names = ", ".join(f"{PACK.merchants.get(i, {}).get('merchant_name', i)} ({i})" for i in ids)
        return f"Only buy from: {names}."
    return describe(rule)


def _rule_key(r: dict) -> tuple:
    v = tuple(sorted(r["value"])) if isinstance(r["value"], list) else r["value"]
    return (r["field"], r["operator"], v, r.get("currency") or None, r.get("scope") or None, r.get("period_days"))


@app.post("/v1/mandates/compile", tags=["mandates"], dependencies=[Depends(_auth)])
def compile_mandate(body: CompileIn):
    """Instruction → proposed rules, in plain language, plus how they would have treated past purchases.
    Nothing is stored: show it to the customer, then POST /v1/mandates with what they confirm."""
    card = _card_for(body.customer_id)
    draft, model = _parse(body.instruction, body.use_model)
    if not draft.own_notes():
        raise _not_understood(body.instruction, draft, model)
    d = draft.as_dict()
    return {
        "customer_id": body.customer_id,
        "instruction": d["instruction"],
        "hard_rules": d["hard_rules"],
        "uncertainty_policy": d["uncertainty_policy"],
        "guidance": d["guidance"],
        "open_questions": d["open_questions"],
        "gaps": d["gaps"],
        "rules_explained": [{"rule": n["rule"], "text": _describe(n["rule"]), "source": n["tier"], "from_words": n["source"],
                             **({"ai_added": n["ai_added"]} if n.get("ai_added") else {})}
                            for n in d["notes"]],
        "safety_net": "Also always on: manipulative shop text, lookalike shops, duplicates, split orders, implausible "
                      "prices and signs someone else is driving the session make a purchase uncertain.",
        "backtest": backtest(PACK, ENGINE.profile(card), draft.hard_rules),
        "model": model,
    }


class MandateIn(BaseModel):
    customer_id: str = Field(examples=["CU0001"])
    instruction: str
    hard_rules: list[dict] = Field(description="the rules the customer confirmed (from /v1/mandates/compile)")
    uncertainty_policy: Literal["ask", "decline", "approve"] = "ask"
    guidance: list[str] = []
    open_questions: list[str] = []
    confirmed: bool = Field(description="must be true: the customer saw the rules and agreed")
    card_id: str | None = Field(None, description="which of the customer's cards the agent pays with "
                                                  "(default: their main online card)")


def _check_rules(rules: list[dict]) -> None:
    bad = [{"rule": r, "error": err} for r in rules if (err := validate_rule(r))]
    if bad:
        raise HTTPException(422, {"message": "Some rules cannot be enforced.", "rules": bad})


def _mandate_out(m: dict) -> dict:
    return {**m, "rules_explained": [{"rule": r, "text": _describe(r)} for r in m["hard_rules"]]}


def _mandate(mandate_id: str) -> dict:
    m = MANDATES.get(mandate_id)
    if m is None:
        raise HTTPException(404, f"Unknown mandate {mandate_id}.")
    return m


@app.post("/v1/mandates", tags=["mandates"], status_code=201, dependencies=[Depends(_auth)])
def create_mandate(body: MandateIn):
    """Store the customer's confirmed permissions. Only after they agreed (confirmed=true)."""
    if not body.confirmed:
        raise HTTPException(422, "The customer has to confirm the rules first (confirmed: true).")
    card = _card_for(body.customer_id)
    if body.card_id and body.card_id != card:
        owner = PACK.accounts.get(PACK.cards.get(body.card_id, {}).get("account_id"), {}).get("customer_id")
        if owner != body.customer_id or PACK.cards[body.card_id]["status"] != "active":
            raise HTTPException(422, f"{body.card_id} is not an active card of {body.customer_id}.")
        card = body.card_id
    _check_rules(body.hard_rules)
    if all(r["field"] in DEFAULT_FIELDS for r in body.hard_rules):
        raise HTTPException(422, {"code": "no_customer_rule",
                                  "message": "A mandate needs at least one rule from the customer (an amount, what to "
                                             "buy, or which shops); the always-on safety checks alone allow anything."})
    mid = "LM" + uuid.uuid4().hex[:12].upper()
    with LOCK:
        m = {"mandate_id": mid, "status": "active", "customer_id": body.customer_id, "card_id": card,
             "instruction": body.instruction, "hard_rules": body.hard_rules,
             "uncertainty_policy": body.uncertainty_policy, "guidance": body.guidance,
             "open_questions": body.open_questions, "confirmed_at": iso(utcnow()),
             "confirmed_rule_count": len(body.hard_rules), "amendments": []}
        # AP2: Viseca one (Trusted Surface) signs the open mandates the shopping agent will carry
        om = ap2.open_mandates(PACK, m, card, KEYRING)
        AP2_SIM.register(mid, om)
        m["ap2"] = om.as_dict()
        MANDATES[mid] = m
        return _mandate_out(m)


@app.get("/v1/mandates/{mandate_id}", tags=["mandates"], dependencies=[Depends(_auth)])
def get_mandate(mandate_id: str):
    with LOCK:
        return _mandate_out(_mandate(mandate_id))


class MandatePatch(BaseModel):
    add_rules: list[dict] = Field([], description="extra restrictions; existing rules can never be removed")
    uncertainty_policy: Literal["decline"] | None = Field(None, description="can only be tightened to decline")
    guidance: list[str] | None = None
    open_questions: list[str] | None = None


@app.patch("/v1/mandates/{mandate_id}", tags=["mandates"], dependencies=[Depends(_auth)])
def patch_mandate(mandate_id: str, body: MandatePatch):
    """Tighten an active mandate. Applies to the next purchase."""
    _check_rules(body.add_rules)
    with LOCK:
        m = _mandate(mandate_id)
        if m["status"] != "active":
            raise HTTPException(409, f"The mandate is {m['status']}.")
        m["hard_rules"] = m["hard_rules"] + body.add_rules
        if body.uncertainty_policy:
            m["uncertainty_policy"] = body.uncertainty_policy
        for k in ("guidance", "open_questions"):
            if getattr(body, k) is not None:
                m[k] = getattr(body, k)
        return _mandate_out(m)


class RuleTextIn(BaseModel):
    text: str = Field(examples=["Never spend more than CHF 50 on a single order."])
    use_model: bool = True


@app.post("/v1/mandates/{mandate_id}/rules/parse", tags=["mandates"], dependencies=[Depends(_auth)])
def parse_rules(mandate_id: str, body: RuleTextIn):
    """The customer's new words → the rules they would add to this mandate, in plain language.
    Nothing is saved: show `proposed` to the customer, then POST /v1/mandates/{id}/rules with what they approve.
    Rules only ever add restrictions; a request to loosen is reported in `not_applied`, never applied."""
    with LOCK:
        m = dict(_mandate(mandate_id))
    if m["status"] != "active":
        raise HTTPException(409, f"The mandate is {m['status']}.")
    draft, model = _parse(body.text, body.use_model)
    have = {_rule_key(r) for r in m["hard_rules"]}
    proposed, already = [], []
    for n in draft.notes:
        if n["tier"] == "safety net":   # always-on defaults, not the customer's new words
            continue
        item = {"rule": n["rule"], "text": _describe(n["rule"]), "source": n["tier"], "from_words": n["source"]}
        (already if _rule_key(n["rule"]) in have else proposed).append(item)
    not_applied, policy = [], None
    stated = not any("didn't say what to do" in g for g in draft.guidance)
    if stated and draft.uncertainty_policy != m["uncertainty_policy"]:
        if draft.uncertainty_policy == "decline":
            policy = "decline"
        else:
            not_applied.append(f"When uncertain we {'ask you' if m['uncertainty_policy'] == 'ask' else 'decline'}; "
                               f"that can only become stricter, so '{draft.uncertainty_policy}' was not applied.")
    if not proposed and not policy:
        if not already:
            raise _not_understood(body.text, draft, model, not_applied)
        not_applied.append("Everything in these words is already in your rules.")
    return {
        "mandate_id": mandate_id, "text": body.text, "proposed": proposed, "uncertainty_policy": policy,
        "already_in_mandate": already, "not_applied": not_applied,
        # the mandate already sets limits and scope; only questions about these words are relevant
        "open_questions": [q for q in draft.open_questions
                           if q not in {g["question"] for g in draft.gaps if g["kind"] != "unparsed"}],
        "unparsed": [g["text"] for g in draft.gaps if g["kind"] == "unparsed"],
        "backtest": backtest(PACK, ENGINE.profile(m["card_id"]), m["hard_rules"] + [p["rule"] for p in proposed]),
        "model": model,
    }


class RulesIn(BaseModel):
    text: str = Field(description="the customer's words these rules came from (kept for the record)")
    rules: list[dict] = Field([], description="the proposed rules the customer approved")
    uncertainty_policy: Literal["decline"] | None = None
    confirmed: bool = Field(description="must be true: the customer saw the rules and agreed")


@app.post("/v1/mandates/{mandate_id}/rules", tags=["mandates"], dependencies=[Depends(_auth)])
def add_rules(mandate_id: str, body: RulesIn):
    """Save rules the customer approved. They apply from the next POST /v1/decisions on."""
    if not body.confirmed:
        raise HTTPException(422, "The customer has to approve the rules first (confirmed: true).")
    _check_rules(body.rules)
    with LOCK:
        m = _mandate(mandate_id)
        if m["status"] != "active":
            raise HTTPException(409, f"The mandate is {m['status']}.")
        have = {_rule_key(r) for r in m["hard_rules"]}
        new = [r for r in body.rules if _rule_key(r) not in have]
        m["hard_rules"] = m["hard_rules"] + new
        if body.uncertainty_policy:
            m["uncertainty_policy"] = body.uncertainty_policy
        m.setdefault("amendments", []).append({"at": iso(utcnow()), "text": body.text, "rules_added": new,
                                               "uncertainty_policy": body.uncertainty_policy})
        return _mandate_out(m)


@app.delete("/v1/mandates/{mandate_id}", tags=["mandates"], dependencies=[Depends(_auth)])
def revoke_mandate(mandate_id: str):
    """Withdraw permission. Later purchases are declined and waiting step-ups can no longer be approved."""
    with LOCK:
        m = _mandate(mandate_id)
        m["status"] = "revoked"
        m["revoked_at"] = iso(utcnow())
        ENGINE.revoke(m["customer_id"], mandate_id)
        return _mandate_out(m)


# ------------------------------------------------------------------ purchases
class MerchantIn(BaseModel):
    merchant_id: str = Field(examples=["ME0001"], description="the shop's identity; familiarity is judged by ID, not name")
    merchant_name: str | None = None
    merchant_category: str | None = None
    merchant_mcc: str | None = None
    merchant_country: str | None = None
    merchant_city: str | None = None


class ItemIn(BaseModel):
    item_id: str | None = None
    item_name: str | None = None
    item_category: str | None = None
    quantity: int = Field(1, ge=1)
    unit_price: float = Field(gt=0, description="in the purchase currency")
    item_details: str = Field("", description="the shop's own text (untrusted: read for facts only)")


class Ap2Presentation(BaseModel):
    open_payment: str = Field(description="open Payment Mandate signed by Viseca one (from the mandate's ap2 block)")
    closed_payment: str = Field(description="closed Payment Mandate signed by the shopping agent")
    open_checkout: str | None = Field(None, description="open Checkout Mandate signed by Viseca one")
    closed_checkout: str | None = Field(None, description="closed Checkout Mandate signed by the agent; it embeds "
                                                          "the shop-signed cart (checkout_jwt)")


class PurchaseIn(BaseModel):
    mandate_id: str
    authorization_id: str | None = Field(None, description="optional idempotency key: the same id returns the same "
                                                           "decision and is counted once")
    merchant: MerchantIn
    items: list[ItemIn] = Field(min_length=1)
    currency: Literal["CHF", "EUR", "GBP", "USD"] = "CHF"
    delivery_fee: float = Field(0, ge=0)
    timestamp: datetime | None = Field(None, description="when the purchase happens (default: now); spending "
                                                         "windows use this clock")
    customer_device_id: str | None = Field(None, description="default: the card's usual device")
    recent_attempt_count_10m: int | None = Field(None, ge=0, description="default: counted from earlier purchases")
    channel: Literal["ecommerce", "in_store", "mobile_wallet", "recurring", "atm"] = "ecommerce"
    fulfillment_method: str = "delivery"
    delivery_by: str | None = None
    order_returnable: Literal["true", "false", "unknown", "not_applicable"] = "unknown"
    order_cancellable: Literal["true", "false", "unknown", "not_applicable"] = "unknown"
    purchase_description: str | None = None
    related_authorization_id: str | None = Field(None, description="an earlier decision this re-quotes or follows")
    ap2: Ap2Presentation | None = Field(None, description="AP2 mandates the agent carries for this purchase "
                                                          "(from /v1/ap2/simulate-checkout or a real agent)")


def _build_event(m: dict, p: PurchaseIn, auth_id: str, ledger) -> tuple[dict, list[str]]:
    assumed = []
    known = PACK.merchants.get(p.merchant.merchant_id, {})
    given = p.merchant.model_dump(exclude_none=True)
    if not known and "merchant_name" not in given:
        raise HTTPException(422, f"Shop {p.merchant.merchant_id} is not in the catalogue: send at least merchant_name.")
    merchant = {
        "merchant_id": p.merchant.merchant_id,
        "merchant_name": given.get("merchant_name", known.get("merchant_name")),
        "merchant_category": given.get("merchant_category", known.get("merchant_category", "unknown")),
        "merchant_mcc": str(given.get("merchant_mcc", known.get("merchant_mcc", "5999"))).zfill(4)[:4],
        "merchant_country": given.get("merchant_country", known.get("merchant_country", "CH")).upper(),
        "merchant_city": given.get("merchant_city", known.get("merchant_city", "Unknown")),
        "availability": known.get("availability", "online"),
        "recurring_capable": known.get("recurring_capable", "false"),
    }
    if not known:
        assumed.append(f"shop {merchant['merchant_id']} is not in the catalogue; missing details defaulted")
    items = []
    for n, it in enumerate(p.items, 1):
        cat = PACK.items.get(it.item_id or "", {})
        name, category = it.item_name or cat.get("item_name"), it.item_category or cat.get("item_category")
        if not name or not category:
            raise HTTPException(422, f"Line {n}: send item_name and item_category (or a catalogue item_id).")
        items.append({"line_no": n, "item_id": it.item_id or f"ITX{n:03d}", "item_name": name, "item_category": category,
                      "quantity": it.quantity, "unit_price": round(it.unit_price, 2), "currency": p.currency,
                      "item_details": it.item_details})
    subtotal = round(sum(i["quantity"] * i["unit_price"] for i in items), 2)
    amount = round(subtotal + p.delivery_fee, 2)
    ts = (p.timestamp or datetime.now(timezone.utc)).astimezone(timezone.utc)
    device = p.customer_device_id
    if not device:
        devices = ENGINE.profile(m["card_id"]).devices
        device = devices.most_common(1)[0][0] if devices else "DVC-UNKNOWN"
        assumed.append(f"device: the card's usual device {device}")
    earlier = [r for r in ledger.records.values() if ts - timedelta(minutes=10) <= r.timestamp < ts] if ledger else []
    attempts = p.recent_attempt_count_10m if p.recent_attempt_count_10m is not None else len(earlier)
    related_status = None
    if p.related_authorization_id:
        rel = ledger.get(p.related_authorization_id) if ledger else None
        related_status = {"expired": "declined"}.get(rel.status, rel.status) if rel else None
    authorization = {
        "authorization_id": auth_id, "source_authorization_id": auth_id, "scenario_id": API_SCENARIO_ID,
        "replay_order": (len(ledger.order) if ledger else 0) + 1, "mandate_id": m["mandate_id"],
        "profile_id": f"PROFILE_{m['customer_id']}", "card_id": m["card_id"], "initiator_type": "agent",
        "merchant": merchant, "timestamp": ts.strftime("%Y-%m-%dT%H:%M:%SZ"), "amount": amount, "currency": p.currency,
        "billing_amount_chf": PACK.to_chf(amount, p.currency), "items_subtotal": subtotal,
        "delivery_fee": round(p.delivery_fee, 2), "channel": p.channel, "customer_device_id": device,
        "authority_status": "active", "card_status_at_attempt": "active", "spend_in_period_before_chf": None,
        "recent_attempt_count_10m": attempts, "fulfillment_method": p.fulfillment_method, "delivery_by": p.delivery_by,
        "order_returnable": p.order_returnable, "order_cancellable": p.order_cancellable,
        "related_authorization_id": p.related_authorization_id, "related_authorization_status": related_status,
        "purchase_description": p.purchase_description or ", ".join(i["item_name"] for i in items),
        "items": items,
    }
    status_map = {"expired": "declined"}
    recent = [{"authorization_id": r.live_id, "timestamp": iso(r.timestamp), "merchant_id": r.merchant_id,
               "billing_amount_chf": r.amount_chf, "status": status_map.get(r.status, r.status)} for r in earlier]
    snapshot = {k: m[k] for k in ("mandate_id", "status", "customer_id", "card_id", "instruction", "hard_rules",
                                  "uncertainty_policy")}
    snapshot["profile_id"] = authorization["profile_id"]
    event = assemble_event(authorization, snapshot, recent_authorizations=recent,
                           approved_spend_in_period_chf=ledger.total_approved() if ledger else 0.0)
    return event, assumed


# the always-on checks every mandate gets; on their own they allow any product at any price
DEFAULT_FIELDS = {r["field"] for r in SAFETY_NET} | {"derived.quasi_cash_lines", "derived.recurring_lines"}

ORIGIN = {"customer": "your rules", "added-by-you": "added later", "safety-net": "safety net",
          "your-controls": "your controls", "platform": "platform", "ap2": "AP2"}
RESULT = {"pass": "passed", "fail": "failed", "unknown": "uncertain"}


def _rule_results(result: dict, m: dict) -> list[dict]:
    """Every rule and check with its result and what it did to the decision (mirrors Engine.combine):
    a failure causes a decline; with no failure, an uncertainty causes a step_up (or a decline/approval,
    following the customer's uncertainty policy; security signals are never approved away)."""
    decision, policy = result["decision"], result.get("uncertainty_policy")
    any_fail = any(c["status"] == "fail" for c in result["checks"])
    out = []
    for c in result["checks"]:
        rule = c.get("rule")
        origin = ORIGIN.get(c["tier"], c["tier"])
        if rule is not None and c["tier"] == "customer":
            idx = next((i for i, x in enumerate(m["hard_rules"]) if x == rule), None)
            if idx is not None and idx >= m.get("confirmed_rule_count", len(m["hard_rules"])):
                origin = "added later"
        effect = None
        if c["status"] == "fail":
            effect = "caused decline"
        elif c["status"] == "unknown" and not any_fail:
            if decision == "step_up" and (policy != "approve" or c["security"]):
                effect = "caused step_up"
            elif decision == "decline":
                effect = "caused decline"
            elif decision == "approve":
                effect = "approved anyway: your policy is to approve when uncertain"
        entry = {"rule": rule, "rule_text": _describe(rule) if rule else None, "origin": origin,
                 "result": RESULT[c["status"]], "explanation": c["text"], "effect": effect, "field": c["field"],
                 "code": c["code"], "security": c["security"], "provenance": c["provenance"],
                 "actual": c["actual"], "expected": c["expected"]}
        if c.get("extra"):
            entry["extra"] = c["extra"]
        out.append(entry)
    return out


def _ap2_out(d: dict) -> dict | None:
    a = d.get("ap2")
    if not a:
        return None
    ver = a["verification"]
    out = {"verified": not any(c.status == "fail" for c in ver.checks), "merchant_verified": ver.merchant_verified,
           "checkout_disclosed": ver.disclosed, "checkout_hash": ver.checkout_hash,
           "signed_terms": ver.signed_terms, "signed_attributes": ver.signed_attrs,
           "checkout": ver.checkout, "receipt": a["receipt"]}
    if a.get("customer"):
        out["customer_signature"] = a["customer"]
    return out


def _expire_stale() -> None:
    now = time.time()
    for d in DECISIONS.values():
        if d["status"] == "pending_customer" and now - d["created"] > STEP_UP_TIMEOUT_S:
            ENGINE.expire(d["run_id"], d["authorization_id"])
            d["status"] = "expired"


def _decision_out(d: dict) -> dict:
    r = d["result"]
    out = {
        "authorization_id": d["authorization_id"],
        "mandate_id": d["mandate_id"],
        "decision": r["decision"],
        "status": d["status"],
        "headline": r["headline"],
        "customer_message": r["customer_message"],
        "reason_codes": r["reason_codes"],
        "decided_by": [x for x in d["rules"] if x["effect"] and x["effect"].startswith("caused")],
        "counts": {k: sum(1 for x in d["rules"] if x["result"] == k) for k in ("passed", "failed", "uncertain")},
        "rules": d["rules"],
        "uncertainty_policy": r.get("uncertainty_policy"),
        "security_flags": r.get("security_flags", []),
        "flags_created": r.get("flags_created", []),
        "assumptions": d["assumptions"],
        "amount_chf": r["amount_chf"],
        "merchant": r["merchant"],
        "items": r["items"],
        "timestamp": r["timestamp"],
        "engine_version": r["engine_version"],
        "latency_ms": r.get("latency_ms"),
    }
    if r["decision"] == "step_up":
        out["step_up_expires_at"] = iso(datetime.fromtimestamp(d["created"] + STEP_UP_TIMEOUT_S, timezone.utc))
    if d.get("resolution"):
        out["resolution"] = d["resolution"]
    if d.get("ap2"):
        out["ap2"] = _ap2_out(d)
    return out


@app.post("/v1/decisions", tags=["decisions"], dependencies=[Depends(_auth)])
def decide(p: PurchaseIn):
    """A proposed purchase → `approve`, `decline` or `step_up`, with every check and the reasons.
    `status` is where it stands now: approved | declined | pending_customer | expired."""
    with LOCK:
        _expire_stale()
        m = _mandate(p.mandate_id)
        auth_id = p.authorization_id or "az_" + uuid.uuid4().hex[:16]
        if auth_id in DECISIONS:
            if DECISIONS[auth_id]["mandate_id"] != p.mandate_id:
                raise HTTPException(409, f"{auth_id} was already used for another mandate.")
            return {**_decision_out(DECISIONS[auth_id]), "replayed": True}
        run_id = f"api-{m['mandate_id']}"
        event, assumed = _build_event(m, p, auth_id, ENGINE.store.runs.get(run_id))
        errors = validation_errors(event)
        if errors:
            raise HTTPException(422, {"message": "The purchase does not form a valid event.", "errors": errors[:10]})
        ver = ap2.verify(p.ap2.model_dump(), event, KEYRING, PACK) if p.ap2 else None
        result = ENGINE.decide(event, run_id, ap2=ver)
        DECISIONS[auth_id] = {
            "authorization_id": auth_id, "mandate_id": m["mandate_id"], "customer_id": m["customer_id"],
            "run_id": run_id, "created": time.time(), "assumptions": assumed, "result": result,
            "rules": _rule_results(result, m),
            "status": {"approve": "approved", "decline": "declined", "step_up": "pending_customer"}[result["decision"]],
        }
        if ver:
            DECISIONS[auth_id]["ap2"] = {"presentation": p.ap2.model_dump(), "verification": ver,
                                         "receipt": ap2.receipt(result, ver, KEYRING)}
        return _decision_out(DECISIONS[auth_id])


@app.get("/v1/decisions", tags=["decisions"], dependencies=[Depends(_auth)])
def list_decisions(mandate_id: str | None = None, customer_id: str | None = None,
                   status: Literal["approved", "declined", "pending_customer", "expired"] | None = None,
                   limit: int = Query(100, ge=1, le=1000)):
    """Newest first. Poll with status=pending_customer to find purchases waiting for the customer."""
    with LOCK:
        _expire_stale()
        rows = [d for d in DECISIONS.values()
                if (mandate_id is None or d["mandate_id"] == mandate_id)
                and (customer_id is None or d["customer_id"] == customer_id)
                and (status is None or d["status"] == status)]
        return [_decision_out(d) for d in sorted(rows, key=lambda d: -d["created"])[:limit]]


def _decision(authorization_id: str) -> dict:
    d = DECISIONS.get(authorization_id)
    if d is None:
        raise HTTPException(404, f"Unknown authorization {authorization_id}.")
    return d


@app.get("/v1/decisions/{authorization_id}", tags=["decisions"], dependencies=[Depends(_auth)])
def get_decision(authorization_id: str):
    with LOCK:
        _expire_stale()
        return _decision_out(_decision(authorization_id))


class ResolveIn(BaseModel):
    decision: Literal["approve", "decline"]


@app.post("/v1/decisions/{authorization_id}/resolve", tags=["decisions"], dependencies=[Depends(_auth)])
def resolve(authorization_id: str, body: ResolveIn):
    """The customer's answer to a step_up. Only real customer input belongs here."""
    with LOCK:
        _expire_stale()
        d = _decision(authorization_id)
        if d["status"] != "pending_customer":
            raise HTTPException(409, f"{authorization_id} is {d['status']}, not waiting for the customer.")
        try:
            res = ENGINE.resolve(d["run_id"], authorization_id, body.decision)
        except RevokedError as exc:
            raise HTTPException(409, str(exc))
        except ValueError as exc:
            raise HTTPException(409, str(exc))
        d["status"] = "approved" if body.decision == "approve" else "declined"
        d["resolution"] = {"decision": body.decision, "at": iso(utcnow()), "message": res["customer_message"],
                           "warning": res.get("warning")}
        if d.get("ap2"):   # the customer answers on Viseca one (AP2 Human Present)
            a = d["ap2"]
            a["customer"] = ap2.human_present(a["presentation"], a["verification"], body.decision, authorization_id,
                                              KEYRING)
        return _decision_out(d)


# ------------------------------------------------------------------ AP2
@app.get("/v1/ap2/keys", tags=["ap2"], dependencies=[Depends(_auth)])
def ap2_keys(merchant_id: str | None = None):
    """Public keys (JWK) to verify what this service signs, and the agent key the open mandates are bound to."""
    out = {"trusted_surface": ap2.public_jwk(KEYRING.ts), "credential_provider": ap2.public_jwk(KEYRING.cp),
           "authorised_agent": ap2.public_jwk(KEYRING.agent)}
    if merchant_id:
        out["merchant"] = ap2.public_jwk(KEYRING.merchant(merchant_id))
    return out


class Ap2SimIn(PurchaseIn):
    sign_as_merchant: str | None = Field(None, description="attack: sign the cart with this other shop's key")
    rogue_agent: bool = Field(False, description="attack: the agent signs with a key the customer never authorised")
    signed_attributes: dict[str, dict] = Field({}, description="per line_no, the shop's signed product terms, e.g. "
                                                              "{\"1\": {\"size\": \"43\", \"return_window_days\": 30}}; "
                                                              "default: what the shop's own text states; null removes one")
    signed_terms: dict = Field({}, description="order terms the shop signs, e.g. {\"returnable\": \"true\"}")


@app.post("/v1/ap2/simulate-checkout", tags=["ap2"], dependencies=[Depends(_auth)])
def simulate_checkout(p: Ap2SimIn):
    """SIMULATOR: plays the shop (signs the cart) and the shopping agent (signs the closed mandates) for a
    purchase, so a frontend without keys can exercise AP2. Records nothing. Returns `decision_request`: the
    body to POST to /v1/decisions. To demo attacks, change that body before sending (tampering), send the
    same `ap2` twice (replay), or drop closed_checkout/open_checkout (withheld cart)."""
    with LOCK:
        m = _mandate(p.mandate_id)
        if m["status"] != "active":
            raise HTTPException(409, f"The mandate is {m['status']}.")
        event, _ = _build_event(m, p, p.authorization_id or "sim_" + uuid.uuid4().hex[:12],
                                ENGINE.store.runs.get(f"api-{m['mandate_id']}"))
        a = event["authorization"]
        knobs = {"merchant_key_of": p.sign_as_merchant, "wrong_agent_key": p.rogue_agent,
                 "attributes": p.signed_attributes or None, "terms": p.signed_terms or None}
        row = {**a, "merchant_id": a["merchant"]["merchant_id"], "_merchant_override": a["merchant"],
               "_items": a["items"], "_ap2": {k: v for k, v in knobs.items() if v}}
        pres = AP2_SIM.present(row, m["mandate_id"])
        checkout = ap2.peek(ap2.peek(pres["closed_checkout"])[1]["checkout_jwt"])[1]
        body = p.model_dump(mode="json", exclude={"sign_as_merchant", "rogue_agent", "signed_attributes",
                                                  "signed_terms", "ap2"}, exclude_none=True)
        return {"presentation": pres, "checkout": checkout, "decision_request": {**body, "ap2": pres}}
