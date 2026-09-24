"""Lab 1: permanent rules, parsed from the customer's profile (customers.csv)."""
from __future__ import annotations

from datetime import timedelta

from fastapi import HTTPException
from pydantic import BaseModel

from ..data import load
from ..events import parse_ts
from ..flow_web.app import DATA_KIND, _data_kinds
from ..lineage import ai_role, how
from ..permanent import PROFILE_FIELDS, parse_profile
from .common import make_app

app = make_app("Lab 1 · Permanent rules", "permanent.html")
PACK = load()


class ParseIn(BaseModel):
    customer_id: str
    profile: dict


def _card(customer_id: str) -> str | None:
    return next((c["card_id"] for c in PACK.cards.values() if PACK.accounts.get(c["account_id"], {}).get("customer_id") == customer_id), None)


def _backtest(card_id: str | None, rules: list[dict]) -> dict:
    """What the country signal would have done over the last 12 months of this card (basket rules can't be
    replayed: history has no basket)."""
    country = next((r for r in rules if r["field"] == "derived.shop_country_expected"), None)
    rows = [r for r in PACK.history_by_card.get(card_id or "", []) if r["transaction_type"] == "purchase" and r["status"] == "approved"]
    if not rows or not country:
        return {"checked": len(rows), "asked": 0, "summary": "Nothing to replay: history has no basket details." if rows else "No purchase history."}
    end = parse_ts(rows[-1]["timestamp"])
    recent = [r for r in rows if parse_ts(r["timestamp"]) >= end - timedelta(days=365)]
    asked = [r for r in recent if r["merchant_country"] not in country["value"]]
    by_cc = {}
    for r in asked:
        by_cc[r["merchant_country"]] = by_cc.get(r["merchant_country"], 0) + 1
    return {"checked": len(recent), "asked": len(asked), "by_country": by_cc,
            "summary": f"Of {len(recent)} purchases in the last 12 months, {len(asked)} were at shops outside "
                       f"{', '.join(country['value'])} and would have asked the customer"
                       + (f" ({', '.join(f'{k}: {v}' for k, v in sorted(by_cc.items()))})." if by_cc else ".")}


@app.get("/api/customers")
def customers():
    return {"customers": [{"customer_id": cid, "persona": c["persona_name"], "region": c["home_region"],
                           "background": c["background"], **{f: c[f] for f in PROFILE_FIELDS}}
                          for cid, c in sorted(PACK.customers.items())], "fields": PROFILE_FIELDS}


@app.post("/api/parse")
def parse(body: ParseIn):
    if body.customer_id not in PACK.customers:
        raise HTTPException(404, "Unknown customer.")
    d = parse_profile(PACK, {"customer_id": body.customer_id, **{f: body.profile.get(f, "") for f in PROFILE_FIELDS}})
    out = d.as_dict()
    for r in out["rules"]:
        r["data"] = [DATA_KIND[k] for k in _data_kinds(r["rule"]["field"])]
        r["how"] = how(r["rule"]["field"])
        r["ai"] = ai_role(r["rule"]["field"])
    out["backtest"] = _backtest(_card(body.customer_id), d.hard_rules())
    return out
