"""(D) The wall: untrusted text in, typed facts out.

Only facts a merchant legitimately owns come from text: size, return window,
final sale, recurring billing, warranty, quasi-cash (voucher / store credit).
Every output is a number, an enum, or "unknown". Nothing that crosses the
wall can change a rule, an amount, merchant identity or state.

If the injection detector fires on a line, every fact extracted from that
line becomes unknown: a hijacked text is no better than a lying merchant.
An optional LLM fallback (see llm.py) may fill facts regex left unknown; if the two
disagree, the fact is unknown.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .injection import InjectionReport, detect, normalise

UNKNOWN = "unknown"

# Shoe-size conversion (men's, approximate); exact-match lookup only, never a model.
UK_TO_EU = {6: "39.5", 6.5: "40", 7: "40.5", 7.5: "41", 8: "42", 8.5: "42.5", 9: "43", 9.5: "44", 10: "44.5", 10.5: "45", 11: "46", 12: "47"}
US_TO_EU = {7: "40", 7.5: "40.5", 8: "41", 8.5: "42", 9: "42.5", 9.5: "43", 10: "44", 10.5: "44.5", 11: "45", 12: "46"}
LETTER_SIZES = {"xxs", "xs", "s", "m", "l", "xl", "xxl", "xxxl"}

RX_SIZE = re.compile(r"\bsize\s*[:=]?\s*(?:(eu|uk|us)\s*)?(\d{2}(?:[.,]5)?|\d{1,2}(?:[.,]5)?|xxs|xs|s|m|l|xl|xxl|xxxl)\b", re.I)
RX_RETURN_DAYS = [
    re.compile(r"\breturns?\s+(?:accepted|allowed|possible|within|up to)\s*(?:within\s+|up to\s+|for\s+)?(\d{1,3})\s*days?\b", re.I),
    re.compile(r"\b(\d{1,3})[- ]day\s+(?:returns?|return policy|return window|money[- ]back)", re.I),
    re.compile(r"\breturn(?:able)?\s+(?:within|for|up to)\s+(\d{1,3})\s*days?\b", re.I),
    re.compile(r"\bR(?:ü|u)cksendungen?\s+innerhalb\s+von\s+(\d{1,3})\s+Tagen", re.I),
    re.compile(r"\bretours?\s+accept(?:é|e)s?\s+sous\s+(\d{1,3})\s+jours", re.I),
]
RX_NO_RETURNS = re.compile(r"\bfinal sale\b|\bno returns?\b|\bnon[- ]?returnable\b|\bnot returnable\b|\breturns? not (?:accepted|possible)\b", re.I)
RX_RETURNS_UNSTATED = re.compile(r"\breturn (?:policy|terms?) (?:not stated|unknown|not specified|unclear)\b", re.I)
RX_RECURRING = re.compile(r"\bbilled\s+(?:monthly|annually|yearly|weekly|quarterly)\b|\bauto[- ]?renew|\brenews automatically\b|\brecurring (?:fee|charge|payment)\b|\bper month\b|\b/\s*month\b|\bsubscription\b", re.I)
RX_WARRANTY = re.compile(r"\b(\d{1,2})[- ](year|month)s?\s+(?:\w+\s+){0,2}warranty\b", re.I)
RX_QUASI_CASH = re.compile(r"\b(?:gift\s*card|gift\s*voucher|voucher|store credit|prepaid card|top[- ]?up)\b", re.I)
RX_ADDON = re.compile(r"\badd[- ]?on\b|\bprotection plan\b|\bextended (?:warranty|cover|protection)\b|\binsurance\b", re.I)


@dataclass
class LineFacts:
    line_no: int
    size: str = UNKNOWN            # canonical: EU number as string, or letter size
    return_days: object = UNKNOWN  # int (0 = final sale) or "unknown"
    final_sale: str = UNKNOWN      # "true" / "false" / "unknown"
    recurring_billing: str = "false"
    warranty_months: object = UNKNOWN
    quasi_cash: str = "false"
    addon: str = "false"
    injection: InjectionReport = field(default_factory=InjectionReport)
    sources: dict = field(default_factory=dict)

    @property
    def walled_off(self) -> bool:
        return self.injection.flagged

    def as_dict(self) -> dict:
        return {
            "line_no": self.line_no,
            "size": self.size,
            "return_days": self.return_days,
            "final_sale": self.final_sale,
            "recurring_billing": self.recurring_billing,
            "warranty_months": self.warranty_months,
            "quasi_cash": self.quasi_cash,
            "addon": self.addon,
            "injection": self.injection.as_dict(),
        }


def canonical_size(system: str | None, raw: str) -> str:
    raw = raw.lower().replace(",", ".")
    if raw in LETTER_SIZES:
        return raw.upper()
    try:
        num = float(raw)
    except ValueError:
        return UNKNOWN
    if system == "uk":
        return UK_TO_EU.get(num, UNKNOWN)
    if system == "us":
        return US_TO_EU.get(num, UNKNOWN)
    return str(int(num)) if num.is_integer() else str(num)


def extract_line(line: dict) -> LineFacts:
    text = line.get("item_details") or ""
    facts = LineFacts(line_no=line.get("line_no", 0))
    facts.injection = detect(text)
    clean, _ = normalise(text)

    sizes = {canonical_size((m.group(1) or "").lower() or None, m.group(2)) for m in RX_SIZE.finditer(clean)}
    if len(sizes) == 1:
        facts.size = sizes.pop()
        facts.sources["size"] = "claimed"

    if RX_NO_RETURNS.search(clean):
        facts.return_days, facts.final_sale = 0, "true"
    elif RX_RETURNS_UNSTATED.search(clean):
        facts.return_days = UNKNOWN
    else:
        days = {int(m.group(1)) for rx in RX_RETURN_DAYS for m in rx.finditer(clean)}
        if len(days) == 1:
            facts.return_days, facts.final_sale = days.pop(), "false"
    if facts.return_days != UNKNOWN:
        facts.sources["return_days"] = "claimed"

    if RX_RECURRING.search(clean):
        facts.recurring_billing = "true"
    w = RX_WARRANTY.search(clean)
    if w:
        facts.warranty_months = int(w.group(1)) * (12 if w.group(2).lower() == "year" else 1)
    if RX_QUASI_CASH.search(clean):
        facts.quasi_cash = "true"
    if RX_ADDON.search(clean):
        facts.addon = "true"

    if facts.walled_off:
        # Behind the wall: a text that talks to us cannot also be trusted for facts.
        facts.size = facts.return_days = facts.final_sale = facts.warranty_months = UNKNOWN
        facts.sources = {k: "withheld (injection)" for k in ("size", "return_days", "warranty_months")}
    return facts


def extract(items: list[dict]) -> list[LineFacts]:
    return [extract_line(l) for l in items]
