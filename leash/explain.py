"""Deterministic explanations built from the rule trace. Never model prose."""
from __future__ import annotations

from .fields import FAIL, PASS, UNK, Check, chf


def _join(texts: list[str], limit: int = 3) -> str:
    texts = [t.rstrip(".") for t in texts]
    more = len(texts) - limit
    body = " · ".join(texts[:limit])
    return body + (f" · (+{more} more)" if more > 0 else "")


def customer_message(decision: str, checks: list[Check], merchant: str, amount_chf: float, policy: str) -> str:
    fails = [c.text for c in checks if c.status == FAIL]
    unknowns = [c.text for c in checks if c.status == UNK]
    passed = sum(1 for c in checks if c.status == PASS)
    head = f"{chf(amount_chf)} at {merchant}"
    if decision == "approve":
        if unknowns:
            return f"Approved {head} because you chose 'approve when uncertain'. Uncertain: {_join(unknowns)}."
        return f"Approved {head}: all {passed} checks passed."
    if decision == "decline":
        if fails:
            return f"Declined {head}: {_join(fails)}."
        return f"Declined {head} because you chose 'decline when uncertain': {_join(unknowns)}."
    return f"Please confirm {head}. {_join(unknowns)}. {passed} other checks passed."


def headline(decision: str, checks: list[Check]) -> str:
    """One short line for feeds and the terminal demo."""
    fails = [c for c in checks if c.status == FAIL]
    unknowns = [c for c in checks if c.status == UNK]
    if decision == "decline" and fails:
        return fails[0].text
    if unknowns:
        return unknowns[0].text
    return "All checks passed."
