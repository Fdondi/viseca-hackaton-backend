"""Permanent rules: a customer's standing rules, read from their profile (customers.csv).

They apply to every purchase of that customer, on top of whatever mandate the agent carries.
Because they are global, only three kinds of statements become rules:

  * prohibitions ("avoids gift vouchers", "no automatic premium upgrades")  → hard rules
  * where the customer shops and travels                                      → a signal: ask, never decline
  * a careful budget style                                                    → ask when uncertain

Everything else is reported as "noted, not enforced", with the reason: a positive preference
("specialist sports shops") is about some purchases, not all of them, and belongs in a mandate;
some statements have no field to check them with, or no catalogue item to point at.

Fixed patterns only, no model: a rule that applies to every purchase must be traceable to exact words.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

from .data import Pack
from .fields import describe

PROFILE_FIELDS = ("shopping_preferences", "budget_style", "travel_pattern", "typical_spending")

COUNTRIES = {"switzerland": "CH", "swiss": "CH", "germany": "DE", "german": "DE", "france": "FR", "french": "FR",
             "italy": "IT", "italian": "IT", "austria": "AT", "austrian": "AT", "sweden": "SE", "spain": "ES",
             "portugal": "PT", "liechtenstein": "LI", "netherlands": "NL", "belgium": "BE", "uk": "GB", "britain": "GB"}
NEIGHBOURS = ["AT", "DE", "FR", "IT", "LI"]
RX_HOME_ONLY = re.compile(r"\brarely travels abroad\b|\bdomestic (?:trips|travel) only\b|\bdomestic trips only\b|\bonly domestic\b", re.I)
RX_NEIGHBOURS = re.compile(r"\bneighbouring countries\b|\bneighboring countries\b", re.I)
RX_BROAD = re.compile(r"\b(?:southern |northern )?europe(?:an)?\b|\babroad\b|\bholidays?\b", re.I)

# "avoids X", "no X", "without X": the object runs to the next comma, semicolon or full stop
RX_PROHIBIT = re.compile(r"\b(avoids?|no|never|without|not)\s+([^,;.]+)", re.I)
PROHIBITIONS = [  # (pattern on the object, rule, what it means)
    (re.compile(r"voucher|gift card|store credit", re.I),
     {"field": "derived.quasi_cash_lines", "operator": "=", "value": 0}, "no gift cards or vouchers"),
    # "no automatic premium upgrades": the objection is to things added without asking, not to subscriptions
    (re.compile(r"automatic|upgrade|enrol|add-?ons?|bundled|substitution|extras?", re.I),
     {"field": "derived.unrequested_lines", "operator": "=", "value": 0}, "nothing that wasn't asked for"),
    (re.compile(r"membership|subscription", re.I),
     {"field": "derived.recurring_lines", "operator": "=", "value": 0}, "nothing that bills again later"),
]
NO_FIELD = [  # things we can't check, with the honest reason
    (re.compile(r"financing|instal?ments?", re.I), "no field in a purchase says whether it's financed"),
    (re.compile(r"\bvip\b|resale", re.I), "no field tells a VIP or resale ticket apart from a standard one"),
]


@dataclass
class PermanentRule:
    rule: dict
    text: str
    phrase: str
    source_field: str
    kind: str = "rule"          # rule (fail → decline) | signal (outside → ask)


@dataclass
class Noted:
    phrase: str
    source_field: str
    why: str


@dataclass
class PermanentDraft:
    customer_id: str
    rules: list[PermanentRule] = field(default_factory=list)
    noted: list[Noted] = field(default_factory=list)
    uncertainty_policy: str | None = None
    policy_phrase: str | None = None

    def hard_rules(self) -> list[dict]:
        return [r.rule for r in self.rules]

    def as_dict(self) -> dict:
        return {"customer_id": self.customer_id, "rules": [asdict(r) for r in self.rules],
                "noted": [asdict(n) for n in self.noted], "uncertainty_policy": self.uncertainty_policy,
                "policy_phrase": self.policy_phrase}

    def add(self, rule: dict, phrase: str, source_field: str, kind: str = "rule") -> None:
        for r in self.rules:
            if r.rule == rule:
                r.phrase += f" · {phrase}"
                return
        self.rules.append(PermanentRule(rule, describe(rule), phrase, source_field, kind))


def _clauses(text: str) -> list[str]:
    return [c.strip(" .") for c in re.split(r"[;,]|(?<!-)\band\b(?!-)", text) if c.strip(" .")]


def parse_profile(pack: Pack, profile: dict) -> PermanentDraft:
    """Profile row (or its edited fields) → permanent rules, with the words each came from."""
    d = PermanentDraft(profile.get("customer_id", "?"))

    # shopping preferences: prohibitions become rules, everything else is noted
    prefs = profile.get("shopping_preferences") or ""
    used: list[tuple[int, int]] = []
    for m in RX_PROHIBIT.finditer(prefs):
        obj, phrase = m.group(2).strip(), m.group(0).strip()
        rule = next(((r, what) for rx, r, what in PROHIBITIONS if rx.search(obj)), None)
        if rule:
            d.add(dict(rule[0]), phrase, "shopping_preferences")
            used.append(m.span())
            continue
        no_field = next((why for rx, why in NO_FIELD if rx.search(obj)), None)
        if no_field:
            d.noted.append(Noted(phrase, "shopping_preferences", no_field))
            used.append(m.span())
            continue
        words = [w for w in re.findall(r"[a-z]+", obj.lower()) if len(w) > 3]
        items = sorted(i for i, it in pack.items.items() if words and all(w.rstrip("s") in it["item_name"].lower() for w in words))
        if items:
            d.add({"field": "items.item_id", "operator": "not_in", "value": items}, phrase, "shopping_preferences")
        else:
            d.noted.append(Noted(phrase, "shopping_preferences",
                                 f"no item in the catalogue matches '{obj}', so there's nothing to block yet"))
        used.append(m.span())
    for c in _clauses(prefs):
        start = prefs.find(c)
        if start >= 0 and any(a <= start < b or a < start + len(c) <= b for a, b in used):
            continue
        d.noted.append(Noted(c, "shopping_preferences",
                             "a preference about some purchases, not a limit on all of them: it belongs in a mandate"))

    # where they shop and travel → ask about shops elsewhere (a signal: it never declines)
    travel = profile.get("travel_pattern") or ""
    spending = profile.get("typical_spending") or ""
    countries, phrases = {"CH"}, []
    for text in (travel, spending):
        for word, code in COUNTRIES.items():
            if m := re.search(rf"\b{word}\b", text, re.I):
                countries.add(code)
                phrases.append(m.group(0))
        if m := RX_NEIGHBOURS.search(text):
            countries.update(NEIGHBOURS)
            phrases.append(m.group(0))
    if m := RX_HOME_ONLY.search(travel):
        phrases.append(m.group(0))
    if phrases or RX_HOME_ONLY.search(travel):
        d.add({"field": "derived.shop_country_expected", "operator": "in", "value": sorted(countries)},
              ", ".join(dict.fromkeys(phrases)), "travel_pattern", kind="signal")
    elif travel:
        why = "too broad to name countries" if RX_BROAD.search(travel) else "no country named"
        d.noted.append(Noted(travel, "travel_pattern", why))

    # budget style → how to handle uncertainty (only ever stricter than a mandate)
    style = (profile.get("budget_style") or "").strip()
    if style == "careful":
        d.uncertainty_policy, d.policy_phrase = "ask", style
    elif style:
        d.noted.append(Noted(style, "budget_style", {
            "planned_high_value": "large purchases are planned: each mandate should state its own budget",
        }.get(style, "no change: each mandate's own setting applies")))

    # typical spending describes history; the card profile already learns it from past purchases
    for c in _clauses(spending):
        if re.search(r"\blate-night\b|\bnight\b", c, re.I):
            why = "fine as it is: no check treats night-time purchases as suspicious"
        elif re.search(r"\bdaytime\b", c, re.I):
            why = "no time-of-day check exists"
        else:
            why = "describes past spending; the card history already captures it"
        d.noted.append(Noted(c, "typical_spending", why))
    return d
