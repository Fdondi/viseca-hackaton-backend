"""Typed loader for the synthetic data pack.

Numbers become numbers, empty nullable fields become None, and every CHF
conversion uses the row's currency with decimal half-even rounding, as the
data dictionary prescribes.
"""
from __future__ import annotations

import csv
import json
import os
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import ROUND_HALF_EVEN, Decimal
from functools import lru_cache
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATA_DIR = PACKAGE_ROOT.parent / "viseca-2026" / "data"


def data_dir() -> Path:
    return Path(os.environ.get("LEASH_DATA", DEFAULT_DATA_DIR))


def money(value) -> float:
    """Round to two decimals, half-even, and return a float for JSON."""
    return float(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN))


def _read(name: str) -> list[dict]:
    with open(data_dir() / name, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def _num(v: str):
    return None if v == "" else float(v)


def _int(v: str):
    return None if v == "" else int(v)


def _opt(v: str):
    return None if v == "" else v


@dataclass
class Pack:
    merchants: dict[str, dict]
    items: dict[str, dict]
    fx: dict[str, Decimal]
    customers: dict[str, dict]
    accounts: dict[str, dict]
    cards: dict[str, dict]
    scenarios: dict[str, dict]
    authorities: dict[str, dict]
    attempts: list[dict]
    attempt_items: dict[str, list[dict]]
    history: list[dict]
    history_by_card: dict[str, list[dict]] = field(default_factory=dict)

    def to_chf(self, amount: float, currency: str) -> float:
        return money(Decimal(str(amount)) * self.fx[currency])

    def scenario_attempts(self, scenario_id: str) -> list[dict]:
        rows = [a for a in self.attempts if a["scenario_id"] == scenario_id]
        return sorted(rows, key=lambda a: a["replay_order"])

    @property
    def item_categories(self) -> set[str]:
        return {i["item_category"] for i in self.items.values()}

    @property
    def merchant_categories(self) -> set[str]:
        return {m["merchant_category"] for m in self.merchants.values()}

    def load_schema(self, name: str = "authorization_event.schema.json") -> dict:
        return json.loads((data_dir() / "schemas" / name).read_text())


@lru_cache(maxsize=1)
def load() -> Pack:
    merchants = {r["merchant_id"]: r for r in _read("merchants.csv")}
    items = {}
    for r in _read("items.csv"):
        for k in ("unit_price_min_chf", "unit_price_typical_chf", "unit_price_max_chf"):
            r[k] = float(r[k])
        items[r["item_id"]] = r
    fx = {r["from_currency"]: Decimal(r["rate"]) for r in _read("fx_rates.csv")}

    attempts = []
    for r in _read("purchase_attempts.csv"):
        for k in ("amount", "billing_amount_chf", "items_subtotal", "delivery_fee"):
            r[k] = float(r[k])
        r["spend_in_period_before_chf"] = _num(r["spend_in_period_before_chf"])
        r["replay_order"] = int(r["replay_order"])
        r["recent_attempt_count_10m"] = int(r["recent_attempt_count_10m"])
        for k in ("delivery_by", "related_authorization_id", "related_authorization_status"):
            r[k] = _opt(r[k])
        attempts.append(r)

    attempt_items: dict[str, list[dict]] = defaultdict(list)
    for r in _read("purchase_attempt_items.csv"):
        r["line_no"] = int(r["line_no"])
        r["quantity"] = int(r["quantity"])
        r["unit_price"] = float(r["unit_price"])
        attempt_items[r["authorization_id"]].append(r)
    for lines in attempt_items.values():
        lines.sort(key=lambda l: l["line_no"])

    history = []
    by_card: dict[str, list[dict]] = defaultdict(list)
    for r in _read("authorization_history.csv"):
        r["amount"] = float(r["amount"])
        r["billing_amount_chf"] = float(r["billing_amount_chf"])
        history.append(r)
        by_card[r["card_id"]].append(r)
    for rows in by_card.values():
        rows.sort(key=lambda r: (r["timestamp"], r["authorization_id"]))

    return Pack(
        merchants=merchants,
        items=items,
        fx=fx,
        customers={r["customer_id"]: r for r in _read("customers.csv")},
        accounts={r["account_id"]: r for r in _read("accounts.csv")},
        cards={r["card_id"]: r for r in _read("cards.csv")},
        scenarios={r["scenario_id"]: r for r in _read("scenario_catalogue.csv")},
        authorities={r["authority_id"]: r for r in _read("scenario_authorities.csv")},
        attempts=attempts,
        attempt_items=dict(attempt_items),
        history=history,
        history_by_card=dict(by_card),
    )
