"""Data lineage: which data each check reads, and where that data comes from.

Used by the flow demo (`leash flow-web`) to draw how a purchase is evaluated. A test keeps it in
step with the field catalogue (every catalogue field must say what it reads).

Every check is computed by fixed code. A model is only ever used at setup (proposing rules the
customer then confirms) and, if LEASH_LLM_EXTRACT=1, to fill a size or return window the regexes
missed, only when that value appears verbatim in the shop's text.
"""
from __future__ import annotations

A = "authorization."

CLASSES = {
    "structured": "Transaction data: inherent in the purchase (acquirer / network), the shop's text can't fake it",
    "free_text": "Vendor-supplied text: untrusted, read only by fixed patterns into typed facts",
    "signed": "Signed by the shop (AP2)",
    "viseca": "Viseca's own records (card history, this run, reference prices)",
    "customer": "The customer's mandate and controls",
}

# Written by the shop and shown to people, never parsed into permissions.
FREE_TEXT = {A + "items[].item_details", A + "items[].item_name", A + "purchase_description", A + "merchant.merchant_name"}

# Viseca-side processing between what is received and the checks.
NODES = {
    "wall.size": {"label": "size", "group": "wall"},
    "wall.return_days": {"label": "return window (days)", "group": "wall"},
    "wall.final_sale": {"label": "final sale", "group": "wall"},
    "wall.recurring_billing": {"label": "billed again later", "group": "wall"},
    "wall.quasi_cash": {"label": "voucher / store credit", "group": "wall"},
    "wall.addon": {"label": "add-on / protection plan", "group": "wall"},
    "detector.injection": {"label": "instructions aimed at the payment system", "group": "detectors",
                           "reads": [A + "items[].item_details", A + "purchase_description", A + "merchant.merchant_name"]},
    "detector.lookalike": {"label": "name imitates a known shop", "group": "detectors",
                           "reads": [A + "merchant.merchant_id", A + "merchant.merchant_name", "viseca.card_history"]},
    "viseca.card_history": {"label": "card history (12 months): shops, devices, countries", "group": "viseca"},
    "viseca.run_ledger": {"label": "this run: approved and pending purchases", "group": "viseca"},
    "viseca.catalogue_prices": {"label": "reference price ranges per item", "group": "viseca"},
    "customer.controls": {"label": "flags, blocks, withdrawals you set", "group": "customer"},
    "ap2.presentation": {"label": "signed permission, cart and payment (AP2)", "group": "signed"},
}
WALL_READS = [A + "items[].item_details"]

_SHOP_HISTORY = [A + "merchant.merchant_id", A + "timestamp", "viseca.card_history", "viseca.run_ledger"]
READS: dict[str, list[str]] = {
    "authorization.billing_amount_chf": [A + "billing_amount_chf", A + "amount", A + "currency", A + "delivery_fee"],
    "derived.period_spend_chf": [A + "billing_amount_chf", A + "timestamp", "viseca.run_ledger",
                                 "context.approved_spend_in_period_chf"],
    "derived.basket_units": [A + "items[].quantity"],
    "derived.unrequested_lines": [A + "items[].item_id", A + "items[].item_category", "wall.addon",
                                  "wall.recurring_billing", "wall.quasi_cash"],
    "derived.quasi_cash_lines": [A + "items[].item_category", "wall.quasi_cash"],
    "derived.recurring_lines": [A + "items[].item_category", "wall.recurring_billing"],
    "derived.merchant_purchases_365d": _SHOP_HISTORY,
    "derived.merchant_purchases_ever": _SHOP_HISTORY,
    "extracted.size": ["wall.size", A + "items[].item_id"],
    "extracted.return_days": ["wall.return_days", A + "order_returnable"],
    "security.merchant_text_clean": ["detector.injection"],
    "derived.not_lookalike": ["detector.lookalike"],
    "derived.not_duplicate": [A + "merchant.merchant_id", A + "items[].item_id", A + "billing_amount_chf", A + "timestamp",
                              "viseca.run_ledger"],
    "derived.no_split_order": [A + "merchant.merchant_id", A + "billing_amount_chf", A + "timestamp", "viseca.run_ledger"],
    "derived.price_plausible": [A + "items[].item_id", A + "items[].unit_price", A + "currency", "viseca.catalogue_prices"],
    "derived.session_integrity": [A + "customer_device_id", A + "recent_attempt_count_10m", A + "merchant.merchant_country",
                                  "viseca.card_history", "viseca.run_ledger"],
    "derived.requote_clean": [A + "related_authorization_id", "viseca.run_ledger", "customer.controls"],
    "controls.merchant_flag": [A + "merchant.merchant_id", "customer.controls"],
    "mandate.status": ["mandate.status", "customer.controls"],
    "authorization.card_status_at_attempt": [A + "card_status_at_attempt", A + "authority_status"],
    "ap2.checkout_matches": ["ap2.presentation", A + "merchant.merchant_id", A + "amount", A + "items[].item_id",
                             A + "items[].quantity", A + "items[].unit_price"],
}

HOW = {
    "authorization.billing_amount_chf": "compare the CHF total (fixed FX rates) with your limit",
    "derived.period_spend_chf": "add this order to purchases approved in the window (simulated time)",
    "derived.basket_units": "count the units in the basket",
    "derived.unrequested_lines": "count lines that aren't what you asked for, or are add-ons / recurring / vouchers",
    "derived.quasi_cash_lines": "count gift-card lines (category, or voucher wording behind the wall)",
    "derived.recurring_lines": "count subscription lines (category, or 'billed monthly' wording behind the wall)",
    "derived.merchant_purchases_365d": "count earlier purchases at this merchant ID",
    "derived.merchant_purchases_ever": "count earlier purchases at this merchant ID",
    "extracted.size": "compare the size the shop states (regex; canonical EU sizes by lookup table)",
    "extracted.return_days": "compare the stated return window with the order's 'returnable' flag",
    "security.merchant_text_clean": "fixed patterns + unexplained-prose residual; can only escalate",
    "derived.not_lookalike": "new merchant ID whose name resembles a familiar one; can only escalate",
    "derived.not_duplicate": "same shop, items and price within 24 h",
    "derived.no_split_order": "orders at the same shop within 30 min that together break the per-order limit",
    "derived.price_plausible": "unit price against the item's reference range",
    "derived.session_integrity": "unknown device, burst of attempts, or never-used country",
    "derived.requote_clean": "re-quote of an attempt that tried to manipulate",
}


def reads(field: str) -> list[str]:
    if field in READS:
        return READS[field]
    if field.startswith("ap2."):
        return ["ap2.presentation"]
    if field.startswith("authorization."):
        return [field]
    if field.startswith("items."):
        return [A + "items[]." + field.split(".", 1)[1]]
    if field.startswith("extracted."):
        return ["wall." + field.split(".", 1)[1]]
    return []


def how(field: str) -> str:
    if field in HOW:
        return HOW[field]
    if field.startswith("ap2."):
        return "verify signatures and bindings (ES256)"
    if field.startswith(("authorization.", "items.")):
        return "compare the field with the rule's value"
    if field.startswith("extracted."):
        return "compare the fact the shop states (behind the wall)"
    return "fixed code"


def classify(key: str) -> str:
    """Trust class of a received field, by its generic key (items[] for any line)."""
    if key in FREE_TEXT:
        return "free_text"
    if key.startswith("mandate."):
        return "customer"
    if key.startswith("ap2."):
        return "signed"
    return "structured"
