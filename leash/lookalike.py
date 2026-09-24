"""Lookalike sellers: a NEW merchant_id whose display name resembles a familiar one.

Identity is merchant_id (the acquirer vouches for it). Names are display-only
and used solely here, so name noise on a familiar ID ("PixelHarbor AG") never
causes a decline, while a new ID named "PixelHarbour" is caught.
"""
from __future__ import annotations

import re
from difflib import SequenceMatcher

THRESHOLD = 0.85
LEGAL_NOISE = r"\b(ag|gmbh|sa|sarl|sàrl|ltd|limited|inc|llc|co|company|plc|bv|srl|spa|shop|store|online|official|the|ch|com|de)\b"


def normalise_name(name: str) -> str:
    n = name.lower().split(":")[0]
    n = re.sub(LEGAL_NOISE, " ", n)
    return re.sub(r"[^a-z0-9]", "", n)


def similarity(a: str, b: str) -> float:
    na, nb = normalise_name(a), normalise_name(b)
    if not na or not nb:
        return 0.0
    return SequenceMatcher(None, na, nb).ratio()


def find_lookalike(merchant_id: str, merchant_name: str, familiar: dict[str, str]) -> dict | None:
    """Return the familiar merchant this one imitates, or None.

    A familiar merchant_id is never a lookalike of anything."""
    if merchant_id in familiar:
        return None
    best = None
    for mid, name in familiar.items():
        score = similarity(merchant_name, name)
        if score >= THRESHOLD and (best is None or score > best["score"]):
            best = {"imitates_id": mid, "imitates_name": name, "score": round(score, 3)}
    return best
