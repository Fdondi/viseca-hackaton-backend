"""(A) Build schema-valid live events from the CSV fixtures.

The simulator uses these builders, and so do the offline replay and the
mutation suite. Live IDs are fresh per run; `related_authorization_id` is
rewritten to the related attempt's live ID, exactly as the API does.
"""
from __future__ import annotations

import copy
import uuid
from datetime import datetime, timedelta, timezone
from functools import lru_cache

import jsonschema

from .data import Pack, load


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def new_live_id() -> str:
    return "az_" + uuid.uuid4().hex[:16]


def authorization_from_attempt(
    pack: Pack,
    row: dict,
    *,
    live_id: str,
    related_live_id: str | None,
    mandate_id: str,
    profile_id: str,
) -> dict:
    m = {**pack.merchants.get(row["merchant_id"], {}), **row.get("_merchant_override", {})}
    lines = row.get("_items") or pack.attempt_items.get(row["authorization_id"], [])
    return {
        "authorization_id": live_id,
        "source_authorization_id": row["authorization_id"],
        "scenario_id": row["scenario_id"],
        "replay_order": row["replay_order"],
        "mandate_id": mandate_id,
        "profile_id": profile_id,
        "card_id": row["card_id"],
        "initiator_type": "agent",
        "merchant": {
            "merchant_id": row["merchant_id"],
            "merchant_name": m["merchant_name"],
            "merchant_category": m["merchant_category"],
            "merchant_mcc": m["merchant_mcc"],
            "merchant_country": m["merchant_country"],
            "merchant_city": m["merchant_city"],
            "availability": m["availability"],
            "recurring_capable": m["recurring_capable"],
        },
        "timestamp": row["timestamp"],
        "amount": row["amount"],
        "currency": row["currency"],
        "billing_amount_chf": row["billing_amount_chf"],
        "items_subtotal": row["items_subtotal"],
        "delivery_fee": row["delivery_fee"],
        "channel": row["channel"],
        "customer_device_id": row["customer_device_id"],
        "authority_status": row["authority_status"],
        "card_status_at_attempt": row["card_status_at_attempt"],
        "spend_in_period_before_chf": row["spend_in_period_before_chf"],
        "recent_attempt_count_10m": row["recent_attempt_count_10m"],
        "fulfillment_method": row["fulfillment_method"],
        "delivery_by": row["delivery_by"],
        "order_returnable": row["order_returnable"],
        "order_cancellable": row["order_cancellable"],
        "related_authorization_id": related_live_id,
        "related_authorization_status": row["related_authorization_status"],
        "purchase_description": row["purchase_description"],
        "items": [
            {
                "line_no": l["line_no"],
                "item_id": l["item_id"],
                "item_name": l["item_name"],
                "item_category": l["item_category"],
                "quantity": l["quantity"],
                "unit_price": l["unit_price"],
                "currency": l["currency"],
                "item_details": l["item_details"],
            }
            for l in lines
        ],
    }


def mandate_snapshot(mandate: dict, *, customer_id: str, card_id: str, profile_id: str) -> dict:
    """The run's frozen copy of the mandate: no guidance, no open questions."""
    return {
        "mandate_id": mandate["mandate_id"],
        "status": mandate.get("status", "active"),
        "customer_id": customer_id,
        "card_id": card_id,
        "instruction": mandate["instruction"],
        "hard_rules": copy.deepcopy(mandate["hard_rules"]),
        "uncertainty_policy": mandate["uncertainty_policy"],
        "profile_id": profile_id,
    }


def assemble_event(
    authorization: dict,
    mandate: dict,
    *,
    approved_spend_in_period_chf: float | None,
    recent_authorizations: list[dict],
    deadline_seconds: float = 8.0,
    received_at: datetime | None = None,
) -> dict:
    now = received_at or utcnow()
    return {
        "type": "authorization.request",
        "request_id": "req_" + uuid.uuid4().hex[:12],
        "deadline_at": iso(now + timedelta(seconds=deadline_seconds)),
        "authorization": authorization,
        "mandate": mandate,
        "context": {
            "approved_spend_in_period_chf": approved_spend_in_period_chf,
            "recent_authorizations": recent_authorizations,
        },
        "runtime": {
            "received_at": iso(now),
            "history_window_minutes": 10,
            "context_basis": "run_decisions_and_scenario_timestamps",
        },
    }


@lru_cache(maxsize=1)
def _validator():
    schema = load().load_schema()
    return jsonschema.Draft202012Validator(schema)


def validation_errors(event: dict) -> list[str]:
    return [
        f"{'/'.join(str(p) for p in e.absolute_path) or '<root>'}: {e.message}"
        for e in _validator().iter_errors(event)
    ]
