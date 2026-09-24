"""(B) Card profile precomputed from authorization history.

Everything is keyed by IDs (merchant_id, device id), never by names. Only
approved purchases count as completed behaviour; declines are attempts.
"""
from __future__ import annotations

import statistics
from bisect import bisect_left
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .data import Pack
from .events import parse_ts


@dataclass
class CardProfile:
    card_id: str
    merchant_times: dict[str, list[datetime]] = field(default_factory=dict)
    merchant_names: dict[str, str] = field(default_factory=dict)
    devices: Counter = field(default_factory=Counter)
    countries: Counter = field(default_factory=Counter)
    currencies: Counter = field(default_factory=Counter)
    typical_by_category: dict[str, float] = field(default_factory=dict)
    agent_purchases: int = 0
    purchases: list[dict] = field(default_factory=list)
    history_end: datetime | None = None

    def merchant_count(self, merchant_id: str, *, before: datetime, days: int | None = None) -> int:
        times = self.merchant_times.get(merchant_id, [])
        hi = bisect_left(times, before)
        if days is None:
            return hi
        lo = bisect_left(times, before - timedelta(days=days))
        return hi - lo

    @property
    def familiar_merchants(self) -> dict[str, str]:
        """merchant_id → name for every merchant with at least one approved purchase."""
        return dict(self.merchant_names)

    def summary(self) -> dict:
        top = sorted(self.merchant_times.items(), key=lambda kv: -len(kv[1]))[:8]
        return {
            "card_id": self.card_id,
            "familiar_merchants": [
                {"merchant_id": m, "name": self.merchant_names[m], "approved_purchases": len(t)} for m, t in top
            ],
            "devices": dict(self.devices),
            "countries": dict(self.countries),
            "currencies": dict(self.currencies),
            "agent_purchases": self.agent_purchases,
        }


def build_profile(pack: Pack, card_id: str) -> CardProfile:
    prof = CardProfile(card_id=card_id)
    rows = pack.history_by_card.get(card_id, [])
    times: dict[str, list[datetime]] = defaultdict(list)
    amounts: dict[str, list[float]] = defaultdict(list)
    for r in rows:
        ts = parse_ts(r["timestamp"])
        prof.history_end = ts if prof.history_end is None else max(prof.history_end, ts)
        if r["status"] != "approved" or r["transaction_type"] != "purchase":
            continue
        prof.purchases.append(r)
        times[r["merchant_id"]].append(ts)
        prof.merchant_names[r["merchant_id"]] = r["merchant_name"]
        if r["customer_device_id"]:
            prof.devices[r["customer_device_id"]] += 1
        prof.countries[r["merchant_country"]] += 1
        prof.currencies[r["currency"]] += 1
        amounts[r["merchant_category"]].append(r["billing_amount_chf"])
        if r["initiator_type"] == "agent":
            prof.agent_purchases += 1
    prof.merchant_times = {m: sorted(t) for m, t in times.items()}
    prof.typical_by_category = {c: statistics.median(v) for c, v in amounts.items()}
    return prof
