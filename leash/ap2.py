"""AP2 (Agent Payments Protocol V2, v0.2) for the autonomous (Human Not Present) flow.

Viseca plays two AP2 roles:
  * Trusted Surface (the "one" app): after the customer confirms their rules it signs the
    *open* Checkout and Payment Mandates, bound to the shopping agent's key (`cnf`).
  * Credential Provider (the issuer): before releasing a card token it verifies the
    agent-signed *closed* mandates and the shop-signed checkout, then runs the engine.

    approve → success receipt
    decline → `invalid_mandate` (`invalid_credential` when a signature or binding is wrong)
    step_up → `unresolved_constraint`: the spec's way to bring the user back. The customer
              then signs the closed mandate on the Trusted Surface (Human Present).

The open Checkout Mandate carries only standard constraints a shop can evaluate. The
customer's full rules stay with Viseca; the open Payment Mandate carries a fingerprint of
them as the custom constraint `viseca.leash_rules.1` (AP2's constraint extension point).
AP2 only standardises "must contain" line items, while our item rules mean "may only
contain", so item rules stay in the leash rules.

Simplification: mandates are plain ES256 JWS with the V2 field names. AP2 wraps them in
SD-JWT delegation chains with selective disclosure; here the closed mandate binds to the
open one with an `sd_hash` claim over the open token, as the KB-SD-JWT hop would.

Deterministic, no model. A signature proves who wrote something, not that it is safe:
free text inside a signed cart still goes behind the wall.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from jwcrypto import jwk, jws

from .data import Pack
from .fields import FAIL, PASS, UNK, Check, chf
from .wall import UNKNOWN, extract_line

OPEN_TTL_S = 30 * 24 * 3600
CLOSED_TTL_S = 15 * 60
CP_ISSUER = "viseca-credential-provider"
TS_ISSUER = "viseca-one"


class Ap2Error(ValueError):
    pass


# ------------------------------------------------------------------ JWS helpers
def _b64u(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def digest(token: str) -> str:
    """base64url(sha-256) of a token: `checkout_hash`, `transaction_id`, `sd_hash`, receipt `reference`."""
    return _b64u(hashlib.sha256(token.encode()).digest())


def rules_fingerprint(rules: list[dict]) -> str:
    return _b64u(hashlib.sha256(json.dumps(rules, sort_keys=True, separators=(",", ":")).encode()).digest())


def _new_key(kid: str) -> jwk.JWK:
    return jwk.JWK.generate(kty="EC", crv="P-256", kid=kid)


def public_jwk(key: jwk.JWK) -> dict:
    return json.loads(key.export_public())


def sign(payload: dict, key: jwk.JWK, typ: str) -> str:
    """Compact ES256 JWS. ECDSA is randomised, as AP2 requires for the checkout JWT."""
    s = jws.JWS(json.dumps(payload, separators=(",", ":")).encode())
    s.add_signature(key, alg="ES256", protected=json.dumps({"alg": "ES256", "kid": key["kid"], "typ": typ}))
    return s.serialize(compact=True)


def verify_jws(token: str, key: jwk.JWK) -> dict:
    try:
        s = jws.JWS()
        s.deserialize(token)
        s.verify(key, alg="ES256")
        return json.loads(s.payload)
    except Exception as exc:  # any parse or signature problem is the same answer: not verified
        raise Ap2Error(type(exc).__name__) from exc


def peek(token: str) -> tuple[dict, dict]:
    """Header and payload WITHOUT verifying: only to find which key to verify with, and for display."""
    try:
        h, p, _ = token.split(".")
        pad = lambda s: s + "=" * (-len(s) % 4)  # noqa: E731
        return json.loads(base64.urlsafe_b64decode(pad(h))), json.loads(base64.urlsafe_b64decode(pad(p)))
    except Exception as exc:
        raise Ap2Error("malformed token") from exc


def cents(x: float) -> int:
    return int(round(float(x) * 100))


def card_token(card_id: str) -> str:
    return "ntk_" + hashlib.sha256(card_id.encode()).hexdigest()[:12]


def _now() -> int:
    return int(time.time())


def _day(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%d")


# ------------------------------------------------------------------ keys
class Keyring:
    """Every party's key in one place, because the simulator plays all of them.

    `merchant(id)` stands for the acquirer's key directory: each onboarded merchant ID has
    its own key, so a lookalike shop can sign as itself but never as the shop it imitates.
    """

    def __init__(self) -> None:
        self.ts = _new_key("viseca-one-trusted-surface")
        self.cp = _new_key("viseca-credential-provider")
        self.agent = _new_key("shopping-agent")
        self.rogue_agent = _new_key("unknown-agent")
        self._merchants: dict[str, jwk.JWK] = {}

    def merchant(self, merchant_id: str) -> jwk.JWK:
        if merchant_id not in self._merchants:
            self._merchants[merchant_id] = _new_key(f"merchant:{merchant_id}")
        return self._merchants[merchant_id]


# ------------------------------------------------------------------ open mandates (Trusted Surface)
@dataclass
class OpenMandates:
    checkout: str
    payment: str
    visibility: dict

    def as_dict(self) -> dict:
        return {"open_checkout": self.checkout, "open_payment": self.payment,
                "decoded": {"open_checkout": peek(self.checkout)[1], "open_payment": peek(self.payment)[1]},
                "visibility": self.visibility}


def _merchant_names(pack: Pack, ids) -> list[dict]:
    return [{"id": m, "name": pack.merchants.get(m, {}).get("merchant_name", m)} for m in ids]


def open_mandates(pack: Pack, mandate: dict, card_id: str, keyring: Keyring, now: int | None = None,
                  ttl_s: int = OPEN_TTL_S) -> OpenMandates:
    """What Viseca one signs once the customer confirms: the permission the agent carries."""
    now = now or _now()
    exp = now + ttl_s
    rules = mandate["hard_rules"]
    cnf = {"jwk": public_jwk(keyring.agent)}
    shops = [v for r in rules if r["field"] == "authorization.merchant.merchant_id" and r["operator"] in ("in", "=")
             for v in (r["value"] if isinstance(r["value"], list) else [r["value"]])]
    caps = [pack.to_chf(float(r["value"]), r.get("currency") or "CHF") for r in rules
            if r["field"] == "authorization.billing_amount_chf" and r["operator"] in ("<=", "<")]

    co_constraints = [{"type": "checkout.allowed_merchants", "allowed": _merchant_names(pack, shops)}] if shops else []
    checkout = sign({"vct": "mandate.checkout.open.1", "constraints": co_constraints, "cnf": cnf, "iat": now, "exp": exp},
                    keyring.ts, "mandate+jwt")

    instrument = {"type": "card", "id": card_token(card_id), "description": f"Viseca card {card_id}"}
    pay_constraints: list[dict] = []
    if caps:
        pay_constraints.append({"type": "payment.amount_range", "currency": "CHF", "max": cents(min(caps)), "min": 0})
    if shops:
        pay_constraints.append({"type": "payment.allowed_payees", "allowed": _merchant_names(pack, shops)})
    pay_constraints += [
        {"type": "payment.allowed_payment_instruments", "allowed": [instrument]},
        {"type": "payment.agent_recurrence", "frequency": "ON_DEMAND"},
        {"type": "payment.execution_date", "not_after": _day(exp)},
        {"type": "payment.reference", "conditional_transaction_id": digest(checkout)},
        {"type": "viseca.leash_rules.1", "mandate_id": mandate["mandate_id"], "rule_count": len(rules),
         "rules_sha256": rules_fingerprint(rules), "uncertainty_policy": mandate["uncertainty_policy"],
         "evaluated_by": CP_ISSUER},
    ]
    payment = sign({"vct": "mandate.payment.open.1", "constraints": pay_constraints, "cnf": cnf,
                    "payment_instrument": instrument, "iat": now, "exp": exp}, keyring.ts, "mandate+jwt")

    cap_text = f"at most {chf(min(caps))} per order" if caps else "no per-order amount limit"
    shop_text = ", ".join(m["name"] for m in _merchant_names(pack, shops)) if shops else "any shop"
    visibility = {
        "signed_by": "Viseca one (Trusted Surface), after you confirmed",
        "agent_key": keyring.agent["kid"],
        "valid_until": _day(exp),
        "shop_sees": [f"Shops allowed: {shop_text}.", "That the agent holding key "
                      f"'{keyring.agent['kid']}' may check out for you until {_day(exp)}."],
        "viseca_sees": [f"Amount: {cap_text}.", f"Card: {instrument['description']} (as a network token).",
                        f"A fingerprint of all {len(rules)} of your rules; Viseca checks every one before paying."],
        "stays_private": "Your full rules and instruction never go to the shop or the agent's other counterparties.",
    }
    return OpenMandates(checkout, payment, visibility)


# ------------------------------------------------------------------ checkout (the shop)
def _row_merchant(pack: Pack, row: dict) -> dict:
    return {**pack.merchants.get(row["merchant_id"], {}), **row.get("_merchant_override", {}), "merchant_id": row["merchant_id"]}


def shop_attributes(line: dict) -> dict:
    """What a shop declares as structured product terms. In the simulator the shop derives them
    from its own catalogue text (the shop knows its terms; it doesn't need our wall)."""
    f = extract_line(line, wall=False)
    out: dict = {}
    if f.size != UNKNOWN:
        out["size"] = f.size
    if f.return_days != UNKNOWN:
        out["return_window_days"] = int(f.return_days)
    if f.final_sale != UNKNOWN:
        out["final_sale"] = f.final_sale
    return out


def checkout_from_row(pack: Pack, row: dict, *, attributes: dict | None = None, terms: dict | None = None,
                      now: int | None = None) -> dict:
    """A UCP Checkout (dev.ucp.shopping.checkout) as the shop would sign it.

    Extensions: `terms` (order terms) and `item.attributes` (size, return window, final sale).
    `attributes` overrides are keyed by line_no; a value of None removes that attribute."""
    now = now or _now()
    m = _row_merchant(pack, row)
    lines = row.get("_items") or pack.attempt_items.get(row["authorization_id"], [])
    cur = row["currency"]
    line_items = []
    for l in lines:
        attrs = shop_attributes(l)
        for k, v in ((attributes or {}).get(str(l["line_no"])) or (attributes or {}).get(l["line_no"]) or {}).items():
            if v in (None, ""):
                attrs.pop(k, None)
            else:
                attrs[k] = v
        line_items.append({
            "id": f"li_{l['line_no']}",
            "item": {"id": l["item_id"], "title": l["item_name"], "price": cents(l["unit_price"]),
                     "category": l["item_category"], "description": l["item_details"], "attributes": attrs},
            "quantity": l["quantity"],
            "totals": [{"type": "subtotal", "amount": cents(l["unit_price"] * l["quantity"])}],
        })
    t = {"returnable": row.get("order_returnable"), "cancellable": row.get("order_cancellable"),
         "fulfillment_method": row.get("fulfillment_method"), "delivery_by": row.get("delivery_by")}
    for k, v in (terms or {}).items():
        t[k] = v
    t = {k: v for k, v in t.items() if v not in (None, "", "unknown")}
    return {
        "id": "co_" + digest(f"{row['authorization_id']}:{now}:{time.perf_counter_ns()}")[:12],
        "merchant": {"id": m["merchant_id"], "name": m.get("merchant_name", m["merchant_id"])},
        "line_items": line_items,
        "status": "ready_for_complete",
        "currency": cur,
        "totals": [{"type": "subtotal", "amount": cents(row["items_subtotal"])},
                   {"type": "fulfillment", "amount": cents(row["delivery_fee"] or 0)},
                   {"type": "total", "amount": cents(row["amount"])}],
        "links": [],
        "terms": t,
        "expires_at": datetime.fromtimestamp(now + CLOSED_TTL_S, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def _total(checkout: dict) -> int:
    return next(t["amount"] for t in checkout["totals"] if t["type"] == "total")


# ------------------------------------------------------------------ the shop + the shopping agent (simulator)
class Ap2Sim:
    """Plays the shop (signs the checkout) and the shopping agent (signs the closed mandates).

    Demo knobs on a scenario row, under `_ap2`:
      off                 the shop doesn't support AP2: no presentation at all
      signed_row          the shop signed this row; the row itself is what the agent submits (tampering)
      replay_of           present the signed cart of an earlier attempt (source ID) again
      withhold_checkout   the agent doesn't show Viseca the shop-signed cart
      merchant_key_of     sign the cart with this merchant's key instead of the named shop's
      wrong_agent_key     the closed mandates are signed by an agent the customer never authorised
      attributes, terms   the shop's structured terms (see checkout_from_row)
    """

    def __init__(self, pack: Pack, keyring: Keyring) -> None:
        self.pack = pack
        self.keyring = keyring
        self.open: dict[str, OpenMandates] = {}
        self.presented: dict[str, dict] = {}

    def register(self, mandate_id: str, om: OpenMandates) -> None:
        self.open[mandate_id] = om

    def present(self, row: dict, mandate_id: str) -> dict | None:
        om = self.open.get(mandate_id)
        if om is None:
            return None
        knobs = row.get("_ap2") or {}
        if knobs.get("off"):
            return None                      # this shop doesn't sign its carts: a plain card purchase
        if knobs.get("replay_of") in self.presented:
            pres = copy.deepcopy(self.presented[knobs["replay_of"]])
            self.presented[row["authorization_id"]] = pres
            return pres
        now = _now()
        signed_row = knobs.get("signed_row") or row
        checkout = checkout_from_row(self.pack, signed_row, attributes=knobs.get("attributes"), terms=knobs.get("terms"), now=now)
        shop_key = self.keyring.merchant(knobs.get("merchant_key_of") or checkout["merchant"]["id"])
        checkout_jwt = sign(checkout, shop_key, "checkout+jwt")
        agent = self.keyring.rogue_agent if knobs.get("wrong_agent_key") else self.keyring.agent
        exp = now + CLOSED_TTL_S
        closed_checkout = sign({"vct": "mandate.checkout.1", "checkout_jwt": checkout_jwt, "checkout_hash": digest(checkout_jwt),
                                "sd_hash": digest(om.checkout), "aud": checkout["merchant"]["id"], "iat": now, "exp": exp},
                               agent, "kb+sd-jwt")
        instrument = peek(om.payment)[1]["payment_instrument"]
        closed_payment = sign({"vct": "mandate.payment.1", "transaction_id": digest(checkout_jwt),
                               "payee": checkout["merchant"],
                               "payment_amount": {"amount": cents(row["amount"]), "currency": row["currency"]},
                               "payment_instrument": instrument, "sd_hash": digest(om.payment), "aud": CP_ISSUER,
                               "iat": now, "exp": exp}, agent, "kb+sd-jwt")
        withhold = bool(knobs.get("withhold_checkout"))
        pres = {"open_payment": om.payment, "closed_payment": closed_payment,
                "open_checkout": None if withhold else om.checkout,
                "closed_checkout": None if withhold else closed_checkout}
        self.presented[row["authorization_id"]] = pres
        return pres


# ------------------------------------------------------------------ verification (Credential Provider)
@dataclass
class Ap2Result:
    checks: list[Check] = field(default_factory=list)
    checkout: dict | None = None
    checkout_hash: str | None = None
    merchant_verified: bool = False
    disclosed: bool = False
    signed_attrs: dict[int, dict] = field(default_factory=dict)
    signed_terms: dict = field(default_factory=dict)
    decoded: dict = field(default_factory=dict)
    reference: str | None = None
    covered: set[str] = field(default_factory=set)   # rule fields the signed limits repeat (checked once, by the rule)

    def summary(self) -> dict:
        return {"checkout_hash": self.checkout_hash, "merchant_verified": self.merchant_verified,
                "disclosed": self.disclosed, "signed_attrs": self.signed_attrs, "signed_terms": self.signed_terms,
                "decoded": self.decoded, "checks": [c.as_dict() for c in self.checks], "covered": sorted(self.covered)}


def _chk(res: Ap2Result, fld: str, status: str, text: str, code: str, trace: list[str] | None = None, **extra) -> None:
    res.checks.append(Check(fld, status, text, code, tier="ap2", provenance="cryptographic",
                            security=status != PASS, extra=extra, trace=trace or []))


def _report_vs_signed(pack: Pack, a: dict, checkout: dict, paid: dict) -> list[str]:
    diffs = []
    signed_total = _total(checkout)
    if a["merchant"]["merchant_id"] != checkout["merchant"]["id"]:
        diffs.append(f"shop {a['merchant']['merchant_id']} submitted, {checkout['merchant']['id']} signed")
    if a["currency"] != checkout["currency"] or cents(a["amount"]) != signed_total:
        diffs.append(f"total {a['currency']} {a['amount']:.2f} submitted, {checkout['currency']} {signed_total / 100:.2f} signed")
    paid_differs = paid["currency"] != checkout["currency"] or paid["amount"] != signed_total
    if paid_differs and (paid["currency"], paid["amount"]) != (a["currency"], cents(a["amount"])):
        diffs.append(f"payment mandate for {paid['currency']} {paid['amount'] / 100:.2f}, cart total "
                     f"{checkout['currency']} {signed_total / 100:.2f}")
    sub = lambda lines: sorted((l[0], l[1], l[2]) for l in lines)  # noqa: E731
    got = sub((l["item_id"], l["quantity"], cents(l["unit_price"])) for l in a["items"])
    want = sub((li["item"]["id"], li["quantity"], li["item"]["price"]) for li in checkout["line_items"])
    if got != want:
        names = {li["item"]["id"]: li["item"]["title"] for li in checkout["line_items"]}
        names.update({l["item_id"]: l["item_name"] for l in a["items"]})
        fmt = lambda xs: ", ".join(f"{q}× {names.get(i, i)} at {p / 100:.2f}" for i, q, p in xs) or "nothing"  # noqa: E731
        diffs.append(f"basket submitted: {fmt(got)}; basket signed: {fmt(want)}")
    return diffs


def verify(presentation: dict, event: dict, keyring: Keyring, pack: Pack, now: int | None = None) -> Ap2Result:
    """Everything Viseca, as Credential Provider, checks before the rules. Replay is checked by the
    engine, under its lock, against every signed cart it has seen."""
    now = now or _now()
    a, mandate = event["authorization"], event["mandate"]
    res = Ap2Result()

    # 1. the permission the agent carries was signed by Viseca one
    try:
        op = verify_jws(presentation["open_payment"], keyring.ts)
    except (Ap2Error, KeyError, TypeError):
        _chk(res, "ap2.open_mandate", FAIL, "The agent's permission was not signed by Viseca one, so it isn't yours.",
             "ap2_signature_invalid")
        return res
    res.decoded["open_payment"] = op
    if op.get("vct") != "mandate.payment.open.1" or op.get("exp", 0) < now:
        _chk(res, "ap2.open_mandate", FAIL, f"The permission you signed on Viseca one expired on {_day(op.get('exp', 0))}.",
             "ap2_expired")
        return res

    # The signed limits (amount, shops) were compiled from the customer's rules. When the rules in force
    # are the signed ones, the engine checks them as rules: one check, marked as also signed.
    leash = next((c for c in op.get("constraints", []) if c["type"] == "viseca.leash_rules.1"), None)
    rules_ok = bool(leash and leash["mandate_id"] == mandate["mandate_id"]
                    and rules_fingerprint(mandate["hard_rules"][:leash["rule_count"]]) == leash["rules_sha256"])

    # 2. the purchase was signed by the agent the customer authorised, on top of that permission
    try:
        agent_key = jwk.JWK(**op["cnf"]["jwk"])
        cp = verify_jws(presentation["closed_payment"], agent_key)
    except (Ap2Error, KeyError, TypeError):
        kid = "?"
        try:
            kid = peek(presentation["closed_payment"])[0].get("kid", "?")
        except Ap2Error:
            pass
        _chk(res, "ap2.agent_binding", FAIL,
             f"This purchase was signed by an agent key you never authorised ('{kid}'); yours is '{op['cnf']['jwk'].get('kid')}'.",
             "ap2_signature_invalid",
             trace=["open payment mandate: ES256 signature valid with Viseca one's key",
                    f"authorised agent key (cnf): '{op['cnf']['jwk'].get('kid')}'",
                    f"closed payment mandate signed with '{kid}': ES256 check with the authorised key → invalid"])
        return res
    res.decoded["closed_payment"] = cp
    res.reference = digest(presentation["closed_payment"])
    if cp.get("sd_hash") != digest(presentation["open_payment"]) or cp.get("vct") != "mandate.payment.1":
        _chk(res, "ap2.agent_binding", FAIL, "The purchase is not bound to the permission you signed.", "ap2_checkout_mismatch")
        return res
    if cp.get("exp", 0) < now:
        _chk(res, "ap2.agent_binding", FAIL, "The agent's signed purchase has expired.", "ap2_expired")
        return res
    _chk(res, "ap2.agent_binding", PASS,
         f"Signed by the shopping agent you authorised on Viseca one (key '{op['cnf']['jwk'].get('kid')}'), "
         f"under the permission valid until {_day(op['exp'])}.", "ap2_signature_invalid",
         trace=["open payment mandate: ES256 signature valid with Viseca one's key ('viseca-one-trusted-surface')",
                f"closed payment mandate: ES256 signature valid with the authorised agent key (cnf '{op['cnf']['jwk'].get('kid')}')",
                f"sd_hash {cp['sd_hash'][:10]}… = sha-256(open mandate) → bound",
                f"expires {_day(op['exp'])} > today → valid"])

    # 3. the shop-signed cart
    cc_tok, oc_tok = presentation.get("closed_checkout"), presentation.get("open_checkout")
    if not cc_tok or not oc_tok:
        res.checks.append(Check("ap2.checkout_disclosed", UNK,
                                "The agent didn't show us the shop-signed cart, so we can't confirm what is in it "
                                "or that the shop stands behind it.", "ap2_checkout_not_disclosed",
                                tier="ap2", provenance="cryptographic", security=True))
    else:
        res.disclosed = True
        try:
            oc = verify_jws(oc_tok, keyring.ts)
            cc = verify_jws(cc_tok, agent_key)
        except Ap2Error:
            _chk(res, "ap2.checkout_signature", FAIL, "The checkout mandate is not signed by your agent under your permission.",
                 "ap2_signature_invalid")
            return res
        res.decoded.update(open_checkout=oc, closed_checkout={k: v for k, v in cc.items() if k != "checkout_jwt"})
        cjwt = cc.get("checkout_jwt", "")
        try:
            claimed = peek(cjwt)[1]
            mid, mname = claimed["merchant"]["id"], claimed["merchant"]["name"]
        except (Ap2Error, KeyError, TypeError):
            _chk(res, "ap2.merchant_signature", FAIL, "The cart is unreadable.", "ap2_signature_invalid")
            return res
        try:
            checkout = verify_jws(cjwt, keyring.merchant(mid))
        except Ap2Error:
            _chk(res, "ap2.merchant_signature", FAIL,
                 f"The cart claims to come from {mname} ({mid}) but is not signed with that shop's registered key.",
                 "ap2_signature_invalid", signer=peek(cjwt)[0].get("kid"),
                 trace=[f"cart says merchant = {mid} ({mname})", f"cart signed with key '{peek(cjwt)[0].get('kid')}'",
                        f"ES256 check with {mid}'s registered key → invalid"])
            return res
        res.merchant_verified = True
        res.checkout = checkout
        res.decoded["checkout"] = checkout
        _chk(res, "ap2.merchant_signature", PASS, f"The cart is signed by {mname} ({mid}) with its registered key.",
             "ap2_signature_invalid",
             trace=[f"cart says merchant = {mid} ({mname}), signed with key '{peek(cjwt)[0].get('kid')}'",
                    f"ES256 check with {mid}'s registered key → valid"])

        h = digest(cjwt)
        res.checkout_hash = h
        if not (cc.get("checkout_hash") == h == cp.get("transaction_id")) or cc.get("sd_hash") != digest(oc_tok):
            _chk(res, "ap2.binding", FAIL, "The payment is not bound to this signed cart.", "ap2_checkout_mismatch")
            return res
        if cp.get("payee", {}).get("id") != mid:
            _chk(res, "ap2.binding", FAIL, f"The payment goes to {cp.get('payee', {}).get('id')}, but the cart is from {mid}.",
                 "ap2_checkout_mismatch")
            return res

        diffs = _report_vs_signed(pack, a, checkout, cp["payment_amount"])
        binding = f"sha-256(cart) {h[:10]}… = checkout_hash = payment's transaction_id → bound"
        if diffs:
            _chk(res, "ap2.checkout_matches", FAIL,
                 "What the agent submitted differs from the cart the shop signed: " + "; ".join(diffs) + ".",
                 "ap2_checkout_mismatch", differences=diffs, trace=[binding] + [f"differs: {d}" for d in diffs])
        else:
            signed_lines = ", ".join(f"{li['quantity']}× {li['item']['id']} at {li['item']['price'] / 100:.2f}" for li in checkout["line_items"])
            _chk(res, "ap2.checkout_matches", PASS,
                 f"Shop, basket and total ({checkout['currency']} {_total(checkout) / 100:.2f}) are exactly what the shop signed.",
                 "ap2_checkout_mismatch",
                 trace=[binding, f"shop: submitted {a['merchant']['merchant_id']} = signed {mid}",
                        f"total: submitted {a['currency']} {a['amount']:.2f} = signed {checkout['currency']} {_total(checkout) / 100:.2f}",
                        f"basket: submitted = signed ({signed_lines})"])
        exp_at = datetime.fromisoformat(checkout["expires_at"].replace("Z", "+00:00")).timestamp()
        if exp_at < now:
            _chk(res, "ap2.checkout_expiry", FAIL, "The shop's signed offer has expired.", "ap2_expired")

        for c in oc.get("constraints", []):
            if c["type"] == "checkout.allowed_merchants" and rules_ok:
                res.covered.add("authorization.merchant.merchant_id")
            elif c["type"] == "checkout.allowed_merchants" and mid not in {m["id"] for m in c["allowed"]}:
                _chk(res, "ap2.constraints", FAIL, f"{mname} is not among the shops you allowed when you signed.",
                     "ap2_constraint_violated")
        res.signed_attrs = {int(li["id"].split("_")[-1]): li["item"].get("attributes") or {}
                            for li in checkout["line_items"] if li["id"].startswith("li_")}
        res.signed_terms = checkout.get("terms") or {}

    # 4. the constraints in the permission the customer signed
    notes, bad, unresolved = [], [], []
    amount_chf = pack.to_chf(cp["payment_amount"]["amount"] / 100, cp["payment_amount"]["currency"])
    for c in op.get("constraints", []):
        t = c["type"]
        if t == "payment.amount_range" and rules_ok:
            res.covered.add("authorization.billing_amount_chf")
        elif t == "payment.allowed_payees" and rules_ok:
            res.covered.add("authorization.merchant.merchant_id")
        elif t == "payment.amount_range":
            limit = pack.to_chf(c["max"] / 100, c["currency"])
            (bad if amount_chf > limit else notes).append(
                f"{chf(amount_chf)} {'is over' if amount_chf > limit else 'within'} the {chf(limit)} per order you signed")
        elif t == "payment.allowed_payees":
            if cp["payee"]["id"] not in {m["id"] for m in c["allowed"]}:
                bad.append(f"{cp['payee']['name']} is not a shop you allowed")
        elif t == "payment.allowed_payment_instruments":
            if cp["payment_instrument"]["id"] not in {i["id"] for i in c["allowed"]}:
                bad.append("the card is not the one you signed for")
            else:
                notes.append("your card, as a network token")
        elif t == "payment.execution_date":
            if c.get("not_after") and _day(now) > c["not_after"]:
                bad.append(f"the permission ended on {c['not_after']}")
        elif t == "payment.reference":
            if oc_tok and c["conditional_transaction_id"] != digest(oc_tok):
                bad.append("the checkout permission is not the one signed with this payment permission")
        elif t == "payment.agent_recurrence":
            pass
        elif t == "viseca.leash_rules.1":
            rules = mandate["hard_rules"]
            n = c["rule_count"]
            if c["mandate_id"] != mandate["mandate_id"] or rules_fingerprint(rules[:n]) != c["rules_sha256"]:
                bad.append("the rules in force are not the ones you signed")
            else:
                added = len(rules) - n
                notes.append(f"the {n} rules you signed" + (f", plus {added} you added since (stricter only)" if added else ""))
        else:
            unresolved.append(t)
    if bad:
        _chk(res, "ap2.constraints", FAIL, "Outside the permission you signed on Viseca one: " + "; ".join(bad) + ".",
             "ap2_constraint_violated")
    elif unresolved:
        res.checks.append(Check("ap2.constraints", UNK, f"The permission contains conditions we can't evaluate ({', '.join(unresolved)}).",
                                "rule_not_understood", tier="ap2", provenance="cryptographic", security=True))
    else:
        _chk(res, "ap2.constraints", PASS, "Within the permission you signed on Viseca one: " + "; ".join(notes) + ".",
             "ap2_constraint_violated",
             trace=[f"{n} → true" for n in notes] + ([f"per-order amount and allowed shops: checked by your rules ({', '.join(sorted(res.covered))})"] if res.covered else []))
    return res


# ------------------------------------------------------------------ receipts
def receipt(result: dict, ver: Ap2Result, keyring: Keyring) -> dict:
    """The Credential Provider's signed answer to the agent."""
    payload = {"iss": CP_ISSUER, "iat": _now(), "reference": ver.reference,
               "transaction_id": ver.checkout_hash, "authorization_id": result["authorization_id"]}
    if result["decision"] == "approve":
        payload.update(result="success", payment_token="ntk_" + digest(f"{ver.reference}:{payload['iat']}")[:16])
    elif result["decision"] == "decline":
        crypto = any(c in result["reason_codes"] for c in ("ap2_signature_invalid", "ap2_checkout_mismatch", "ap2_replay"))
        payload.update(result="error", error="invalid_credential" if crypto else "invalid_mandate",
                       error_description=result["customer_message"])
    else:
        payload.update(result="error", error="unresolved_constraint", error_description=result["customer_message"],
                       next="the customer confirms on Viseca one (Human Present)")
    return {"jwt": sign(payload, keyring.cp, "receipt+jwt"), "payload": payload}


def human_present(presentation: dict, ver: Ap2Result, decision: str, authorization_id: str, keyring: Keyring) -> dict:
    """After a step-up: the customer answers on Viseca one. Approving signs the closed mandate on the
    Trusted Surface (Direct mode), which replaces the agent's signature as the authority to pay."""
    now = _now()
    out: dict = {}
    if decision == "approve":
        cp = peek(presentation["closed_payment"])[1]
        signed = {**{k: v for k, v in cp.items() if k != "sd_hash"}, "mode": "human_present",
                  "confirmed_on": TS_ISSUER, "iat": now, "exp": now + CLOSED_TTL_S}
        tok = sign(signed, keyring.ts, "mandate+jwt")
        out["closed_payment_by_customer"] = {"jwt": tok, "payload": signed}
        payload = {"iss": CP_ISSUER, "iat": now, "reference": digest(tok), "transaction_id": ver.checkout_hash,
                   "authorization_id": authorization_id, "result": "success",
                   "payment_token": "ntk_" + digest(f"{tok}:{now}")[:16]}
    else:
        payload = {"iss": CP_ISSUER, "iat": now, "reference": ver.reference, "transaction_id": ver.checkout_hash,
                   "authorization_id": authorization_id, "result": "error", "error": "invalid_mandate",
                   "error_description": "The customer declined this purchase on Viseca one."}
    out["receipt"] = {"jwt": sign(payload, keyring.cp, "receipt+jwt"), "payload": payload}
    return out

