"""(F) Instruction → draft mandate.

Deterministic, catalogue-driven compiler (always available, offline). An
optional LLM pass (llm.py) may propose extra rules; those are validated
against the same field catalogue before the customer sees them.

Tiers shown to the customer:
  1. safety net   — always-on checks (manipulation, lookalikes, duplicates, …)
  2. your rules   — composed from your words (amounts, items, shops, terms)
  3. open questions — what we could not turn into a check
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .data import Pack, load
from .fields import SAFETY_NET, describe, spec_for
from .wall import canonical_size

NUM_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
             "ten": 10, "fourteen": 14, "thirty": 30, "a": 1, "an": 1, "single": 1, "a single": 1}
RX_MONEY = re.compile(r"(?:chf|fr\.?|sfr\.?)\s*(\d+(?:[.,']\d{3})*(?:[.,]\d{1,2})?)|(\d+(?:[.,]\d{1,2})?)\s*(?:chf|francs?)\b", re.I)
RX_PERIOD = re.compile(r"\b(?:across|over|within|in|per|every|each|for)\s+(?:any\s+|a\s+|one\s+|the\s+|a\s+rolling\s+|rolling\s+)?(\d+|one|two|three|four|five|six|seven|eight|nine|ten|fourteen|thirty)?[\s-]*(day|days|week|weeks|month|months)\b|\b(weekly|monthly)\b|\ba\s+(week|month)\b", re.I)
CATEGORY_WORDS = {
    "groceries": [r"grocer(?:y|ies)", r"food shopping"],
    "clothing": [r"cloth(?:ing|es)", r"apparel", r"garments?"],
    "electronics": [r"electronics?"],
    "sporting_goods": [r"sporting goods", r"sports? (?:gear|equipment)"],
    "household": [r"household (?:supplies|goods|items|essentials)", r"cleaning supplies"],
    "cosmetics": [r"cosmetics", r"beauty products?", r"toiletries"],
    "books": [r"\bbooks?\b"],
    "pet_care": [r"(?:pet|dog|cat) (?:food|supplies)"],
    "health": [r"pharmacy", r"medicines?"],
    "home_improvement": [r"home improvement", r"\bdiy\b", r"hardware"],
    "kids_family": [r"\btoys?\b", r"children'?s (?:items|products|clothes)"],
    "gift_card": [r"gift ?cards?", r"vouchers?"],
    "subscriptions": [r"subscriptions?"],
    "membership": [r"memberships?"],
    "transport": [r"(?:train|rail|bus|transit) (?:tickets?|pass(?:es)?)"],
    "hotel": [r"hotels?"],
    "software": [r"software"],
    "food_delivery": [r"meal delivery", r"takeaway", r"take-out"],
    "fuel": [r"\bfuel\b", r"charging"],
}
RETAILER_WORDS = {
    "sporting_goods": r"sports?|sporting goods|running|outdoor",
    "electronics": r"electronics?|computer|tech",
    "clothing": r"clothing|fashion|clothes",
    "groceries": r"grocery|food",
    "books": r"book",
}
ITEM_STOP = {"order", "selection", "supplies", "set", "plan", "session", "stop", "the", "and", "a", "of"}


@dataclass
class Draft:
    instruction: str
    hard_rules: list[dict] = field(default_factory=list)
    uncertainty_policy: str = "ask"
    guidance: list[str] = field(default_factory=list)
    open_questions: list[str] = field(default_factory=list)
    notes: list[dict] = field(default_factory=list)  # [{rule, tier, source}]

    def add(self, rule: dict, tier: str, source: str) -> None:
        key = (rule["field"], rule["operator"], str(rule["value"]), rule.get("scope"), rule.get("period_days"))
        for r in self.hard_rules:
            if (r["field"], r["operator"], str(r["value"]), r.get("scope"), r.get("period_days")) == key:
                return
        self.hard_rules.append(rule)
        self.notes.append({"rule": rule, "tier": tier, "source": source, "text": describe(rule)})

    def api_payload(self) -> dict:
        return {"instruction": self.instruction, "hard_rules": self.hard_rules,
                "uncertainty_policy": self.uncertainty_policy, "guidance": self.guidance,
                "open_questions": self.open_questions}

    def as_dict(self) -> dict:
        return {**self.api_payload(), "notes": self.notes}


UNITS = {w: i for i, w in enumerate("zero one two three four five six seven eight nine ten eleven twelve thirteen "
                                    "fourteen fifteen sixteen seventeen eighteen nineteen".split())}
TENS = {w: 10 * i for i, w in enumerate("_ _ twenty thirty forty fifty sixty seventy eighty ninety".split()) if w != "_"}
RX_NUMWORDS = re.compile(r"\b(?:(?:" + "|".join(list(UNITS) + list(TENS)) + r")(?:[\s-]+(?:and[\s-]+)?"
                         r"(?:hundred|thousand|" + "|".join(list(UNITS) + list(TENS)) + r"))*|a hundred|a thousand)\b", re.I)


def words_to_numbers(text: str) -> str:
    """'two hundred and fifty' → '250', 'fifty' → '50'. Leaves 'a'/'one item' phrasing intact otherwise."""
    def conv(m: re.Match) -> str:
        total, cur = 0, 0
        for w in re.split(r"[\s-]+", m.group(0).lower()):
            if w == "and":
                continue
            if w == "a":
                cur = 1
            elif w in UNITS:
                cur += UNITS[w]
            elif w in TENS:
                cur += TENS[w]
            elif w == "hundred":
                cur = max(cur, 1) * 100
            elif w == "thousand":
                total += max(cur, 1) * 1000
                cur = 0
        return str(total + cur)
    return RX_NUMWORDS.sub(conv, text)


def _num(s: str) -> float:
    s = s.replace("'", "")
    if re.fullmatch(r"\d+,\d{1,2}", s):
        s = s.replace(",", ".")
    return float(s.replace(",", ""))


def _period_days(m: re.Match) -> int:
    if m.group(3):
        return 7 if m.group(3).lower() == "weekly" else 30
    if m.group(4):
        return 7 if m.group(4).lower() == "week" else 30
    n = m.group(1)
    n = 1 if n is None else int(n) if n.isdigit() else NUM_WORDS[n.lower()]
    unit = m.group(2).lower()
    return n * (7 if unit.startswith("week") else 30 if unit.startswith("month") else 1)


def _stem(w: str) -> str:
    w = w.lower()
    if w.endswith("ies") and len(w) > 4:
        return w[:-3] + "y"
    if w.endswith("es") and w[:-2].endswith(("sh", "ch", "x", "ss")):
        return w[:-2]
    if w.endswith("s") and not w.endswith("ss") and len(w) > 3:
        return w[:-1]
    return w


def _tokens(text: str) -> list[str]:
    return [_stem(t) for t in re.findall(r"[a-z0-9]+(?:-[a-z0-9]+)*", text.lower())]


class Compiler:
    def __init__(self, pack: Pack | None = None) -> None:
        self.pack = pack or load()

    def match_items(self, text: str) -> list[dict]:
        toks = set(_tokens(text))
        scored = []
        for it in self.pack.items.values():
            name_toks = [t for t in _tokens(it["item_name"]) if t not in ITEM_STOP]
            if not name_toks:
                continue
            hit = sum(1 for t in name_toks if t in toks)
            ratio = hit / len(name_toks)
            if hit >= 2 and ratio >= 0.6:
                scored.append((ratio, hit, it))
        if not scored:
            return []
        best = max((r, h) for r, h, _ in scored)
        return [it for r, h, it in scored if (r, h) == best]

    def compile(self, instruction: str) -> Draft:
        d = Draft(instruction=instruction)
        text = words_to_numbers(instruction.strip())
        low = text.lower()
        used_sentences: set[int] = set()
        sentences = [s for s in re.split(r"(?<=[.;!?])\s+", text) if s.strip()]

        def mark(span_start: int) -> None:
            pos = 0
            for i, s in enumerate(sentences):
                idx = text.find(s, pos)
                if idx <= span_start < idx + len(s) + 1:
                    used_sentences.add(i)
                    return
                pos = idx + len(s)

        # 1. uncertainty
        policy_rx = [
            ("ask", r"(?:ask|check with|confirm with)\s+me\b[^.;]*?\b(?:uncertain|unsure|in doubt|not sure)|(?:uncertain|unsure|in doubt|not sure)[^.;]*?\b(?:ask|check with|confirm with)\s+me"),
            ("decline", r"(?:decline|don't buy|do not buy|skip it|reject)[^.;]*?\b(?:uncertain|unsure|in doubt|not sure)|(?:uncertain|unsure|in doubt|not sure)[^.;]*?\b(?:decline|don't buy|do not buy|skip)"),
            ("approve", r"(?:approve|buy it anyway|go ahead)[^.;]*?\b(?:uncertain|unsure|in doubt|not sure)"),
        ]
        for pol, rx in policy_rx:
            m = re.search(rx, low)
            if m:
                d.uncertainty_policy = pol
                mark(m.start())
                break
        else:
            d.guidance.append("You didn't say what to do when something is unclear, so we'll ask you.")
        # "ask" is the safest starting point: the API only lets it move to "decline" later.

        # 2. amounts
        money = list(RX_MONEY.finditer(text))
        clause_breaks = [m.start() for m in re.finditer(r"[.;,]|\band\s+(?=(?:keep|up to|at most|no more|max))", text, re.I)]
        for i, m in enumerate(money):
            value = _num(m.group(1) or m.group(2))
            prev_end = money[i - 1].end() if i else 0
            next_start = money[i + 1].start() if i + 1 < len(money) else len(text)
            cstart = max([b for b in clause_breaks if b < m.start()] + [prev_end, 0])
            cend = min([b for b in clause_breaks if b > m.end()] + [next_start])
            window = text[cstart:cend]
            pre = text[cstart:m.start()].lower()
            op = "<" if re.search(r"(?<!or )\b(?:less than|under|below)\s*$", pre) else "<="
            # a spending period, unless it is really a return window or a per-order phrase is closer
            here = m.start() - cstart
            periods = [p for p in RX_PERIOD.finditer(window)
                       if not re.search(r"return", window[max(0, p.start() - 40):p.start()], re.I)]
            per_order = [p for p in re.finditer(r"\b(?:per|each|every|an?)\s+(?:order|purchase)\b", window, re.I)]
            pm = min(periods, key=lambda p: abs(p.start() - here), default=None)
            po = min(per_order, key=lambda p: abs(p.start() - here), default=None)
            if pm and (po is None or abs(pm.start() - here) < abs(po.start() - here)):
                days = _period_days(pm)
                d.add({"field": "derived.period_spend_chf", "operator": op, "value": value, "currency": "CHF",
                       "scope": "period", "period_days": days}, "your rules", m.group(0))
            else:
                d.add({"field": "authorization.billing_amount_chf", "operator": op, "value": value, "currency": "CHF",
                       "scope": "purchase"}, "your rules", m.group(0))
            mark(m.start())
        if money and re.search(r"includ(?:ing|es?)\s+delivery|delivery included", low):
            d.guidance.append("Amounts include delivery: we check the total you are charged, converted to CHF at the fixed rates.")
        elif money:
            d.guidance.append("Amounts are the total you are charged (delivery included), converted to CHF at the fixed rates.")
        if not money:
            d.open_questions.append("You didn't set a spending limit. What is the most the agent may spend per order?")

        # 3. what may be bought: specific catalogue items first, then categories
        retailer = re.search(r"\bspecialist\s+(?:(\w+(?:\s+goods)?)\s+)?(?:retailer|shop|store|seller)s?\b|\b(sports?|electronics?|clothing|grocery|book)\s+(?:retailer|shop|store|seller)s?\b", low)
        scan = low[: retailer.start()] + " " * (retailer.end() - retailer.start()) + low[retailer.end():] if retailer else low
        items = self.match_items(scan)
        cats: list[str] = []
        if items:
            d.add({"field": "items.item_id", "operator": "in", "value": sorted(i["item_id"] for i in items)}, "your rules",
                  ", ".join(i["item_name"] for i in items))
            for it in items:
                d.guidance.append(f"We matched your request to the catalogue item '{it['item_name']}' ({it['item_id']}).")
                idx = scan.find(it["item_name"].split()[-1].lower().rstrip("s"))
                if idx >= 0:
                    mark(idx)
            if re.search(r"\b(?:i|we)\s+(?:chose|picked|selected|want(?:ed)?)\b|\bmy chosen\b", low):
                d.open_questions.append(
                    f"We can't see which exact product you chose, so we accept any '{items[0]['item_name']}'. "
                    "Tell us the model or seller if you want to be stricter.")
        else:
            for cat, pats in CATEGORY_WORDS.items():
                for p in pats:
                    for m in re.finditer(p, scan):
                        clause = re.split(r"[.;,:]|\bbut\b", scan[:m.start()])[-1]
                        if re.search(r"\b(?:no|not|never|avoid|except|without|don't|do not|nothing)\b", clause):
                            d.add({"field": "items.item_category", "operator": "not_in", "value": [cat]}, "your rules", m.group(0))
                        elif cat not in cats:
                            cats.append(cat)
                        mark(m.start())
            pos_cats = list(cats)
            if pos_cats:
                d.add({"field": "items.item_category", "operator": "in", "value": sorted(pos_cats)}, "your rules",
                      ", ".join(pos_cats))
                d.guidance.append("Every line in the basket is checked, not just the shop: a supermarket can also sell perfume.")
            if re.search(r"\bhousehold\s+grocer", low):
                d.open_questions.append("Does 'household groceries' include household supplies such as cleaning products? "
                                        "For now we only allow groceries.")
        if not items and not cats:
            d.open_questions.append("We couldn't tell what the agent may buy, so any product is allowed. Which products or categories?")

        m = re.search(r"\b(one|a single|single|1|two|2|three|3)\s+(?:\w+\s+){0,3}?(?:item|product|thing|piece)s?\b", low)
        if m:
            n = NUM_WORDS.get(m.group(1), None) or int(m.group(1))
            d.add({"field": "derived.basket_units", "operator": "<=", "value": n}, "your rules", m.group(0))
            mark(m.start())

        # 4. which shops
        regular = re.search(r"(?:shops?|stores?|sellers?|merchants?|retailers?)\s+(?:that\s+)?(?:i|we)\s+(?:regularly|usually|often)\s+(?:use|shop at|buy from)|(?:shops?|stores?|sellers?|merchants?)\s+(?:i|we)\s+(?:use|shop at|buy from|order from)\s+(?:regularly|often|usually)|\b(?:regular|usual)\s+(?:shop|store|seller|merchant)s?", low)
        before = re.search(r"(?:shops?|stores?|sellers?|merchants?|retailers?)\s+(?:that\s+)?(?:i|we)(?:'ve|\s+have)?\s+(?:already\s+)?(?:used|bought from|shopped at|ordered from|purchased from)|\b(?:used|bought from|shopped at)\s+before\b|\b(?:familiar|known)\s+(?:shops?|sellers?|stores?|merchants?)|(?:shops?|stores?|sellers?|merchants?)\s+(?:i|we)\s+(?:already\s+)?(?:know|trust)\b", low)
        if regular:
            d.add({"field": "derived.merchant_purchases_365d", "operator": ">=", "value": 3}, "your rules", regular.group(0))
            d.guidance.append("'A shop you use regularly' = at least 3 approved purchases on this card in the last 12 months (shop identified by its merchant ID, not its name).")
            mark(regular.start())
        elif before:
            d.add({"field": "derived.merchant_purchases_ever", "operator": ">=", "value": 1}, "your rules", before.group(0))
            d.guidance.append("'A shop you have used before' = at least 1 approved purchase on this card (identified by merchant ID, not name).")
            mark(before.start())

        if retailer:
            word = (retailer.group(1) or retailer.group(2) or "").lower()
            cat = next((c for c, rx in RETAILER_WORDS.items() if word and re.fullmatch(rf"(?:{rx})", word)), None)
            if cat is None and items:
                cat = items[0]["item_category"]
            mccs = sorted({m["merchant_mcc"] for m in self.pack.merchants.values() if m["merchant_category"] == cat}) if cat else []
            if mccs:
                d.add({"field": "authorization.merchant.merchant_mcc", "operator": "in", "value": mccs}, "your rules", retailer.group(0))
                d.guidance.append(f"'{retailer.group(0)}' = shops registered as {cat.replace('_', ' ')} stores (merchant code {', '.join(mccs)}); "
                                  "general or department stores don't count.")
            else:
                d.open_questions.append(f"What counts as a '{retailer.group(0)}'?")
            mark(retailer.start())

        # 5. item terms
        m = re.search(r"\bsize\s+(?:(eu|uk|us)\s*)?(\d{1,2}(?:[.,]5)?|xxs|xs|s|m|l|xl|xxl)\b", low)
        if m:
            d.add({"field": "extracted.size", "operator": "in", "value": [canonical_size(m.group(1), m.group(2))]}, "your rules", m.group(0))
            d.guidance.append("Sizes come from the shop's product description; if it doesn't state one, we ask you.")
            mark(m.start())
        m = re.search(r"return(?:ed|s|able)?\b[^.;]{0,40}?within\s+(\d+|fourteen|thirty|seven)\s+days|(\d+)[- ]days?\s+returns?", low)
        if m:
            n = m.group(1) or m.group(2)
            n = int(n) if n.isdigit() else NUM_WORDS[n]
            d.add({"field": "extracted.return_days", "operator": ">=", "value": n}, "your rules", m.group(0))
            d.guidance.append("Return windows come from the shop's terms and must agree with the order's 'returnable' flag; "
                              "if the window isn't stated, we ask you. Final sale = no returns.")
            mark(m.start())
        elif re.search(r"\b(?:returnable|can be returned|can be sent back)\b|(?:can't|cannot|can not)\s+be\s+(?:returned|sent back)", low):
            d.add({"field": "authorization.order_returnable", "operator": "=", "value": "true"}, "your rules", "returnable")

        m = re.search(r"(?:do not|don't|never)\s+add\s+anything|nothing\s+(?:else|extra)|\bno\s+(?:add-?ons|extras)\b|anything i (?:did not|didn't) ask for|only what i (?:asked|chose)", low)
        if m:
            d.add({"field": "derived.unrequested_lines", "operator": "=", "value": 0}, "your rules", m.group(0))
            mark(m.start())
        m = re.search(r"\bfor delivery\b|\bdelivered\b", low)
        if m:
            d.add({"field": "authorization.fulfillment_method", "operator": "=", "value": "delivery"}, "your rules", m.group(0))
            mark(m.start())
        m = re.search(r"someone (?:other than me|else)|\bnot me\b|hijack|compromised", low)
        if m:
            d.add({"field": "derived.session_integrity", "operator": "=", "value": "true"}, "your rules", m.group(0))
            d.guidance.append("'Someone else driving' = a device this card has never used, 2+ other attempts within 10 minutes, "
                              "or a country the card has never bought from. Any of these pauses the purchase for you.")
            mark(m.start())

        # 6. tier-1 defaults
        wants_quasi = any(c in cats for c in ("gift_card",)) or any(i["item_category"] == "gift_card" for i in items)
        wants_recur = any(c in cats for c in ("subscriptions", "membership")) or any(i["item_category"] in ("subscriptions", "membership") for i in items)
        if not wants_quasi:
            d.add({"field": "derived.quasi_cash_lines", "operator": "=", "value": 0}, "safety net", "default")
        if not wants_recur:
            d.add({"field": "derived.recurring_lines", "operator": "=", "value": 0}, "safety net", "default")
        for r in SAFETY_NET:
            d.add(dict(r), "safety net", "default")
        d.guidance.append("Text written by shops can never change these rules; if a shop's text tries to instruct the "
                          "payment system, we ask you and remember that shop.")

        for i, s in enumerate(sentences):
            if i not in used_sentences and len(s.split()) > 2:
                d.open_questions.append(f"We did not turn this into a check: \"{s.strip()}\". Is anything here important?")
        return d


def validate_rule(rule: dict) -> str | None:
    """Schema + catalogue validation for any proposed rule (e.g. from an LLM)."""
    allowed = {"field", "operator", "value", "currency", "scope", "period_days"}
    if set(rule) - allowed:
        return f"unexpected keys {set(rule) - allowed}"
    if not isinstance(rule.get("field"), str) or not rule["field"]:
        return "field must be a non-empty string"
    if rule.get("operator") not in ("<", "<=", "=", "!=", ">", ">=", "in", "not_in"):
        return "bad operator"
    v = rule.get("value")
    if isinstance(v, bool) or v is None or (isinstance(v, list) and not all(isinstance(x, str) for x in v)) \
            or not isinstance(v, (int, float, str, list)):
        return "value must be a number, string or list of strings"
    if rule.get("currency") not in (None, "CHF", "EUR", "GBP", "USD"):
        return "bad currency"
    if rule.get("scope") not in (None, "purchase", "period"):
        return "bad scope"
    pd = rule.get("period_days")
    if pd is not None and (not isinstance(pd, int) or pd < 1):
        return "bad period_days"
    if spec_for(rule["field"]) is None or not known_field(rule["field"]):
        return f"unknown field {rule['field']}"
    return None


LINE_ATTRS = {"item_id", "item_name", "item_category", "quantity", "unit_price", "currency"}
EXTRACTED = {"size", "return_days", "final_sale", "recurring_billing", "warranty_months", "quasi_cash", "addon"}


def known_field(name: str) -> bool:
    """A field the engine can actually evaluate (prefix matching alone would accept typos)."""
    from .fields import FIELDS
    if name in FIELDS:
        return True
    head, _, rest = name.partition(".")
    if head == "items":
        return rest in LINE_ATTRS
    if head == "extracted":
        return rest in EXTRACTED
    if head == "authorization":
        from .data import load
        props = load().load_schema()["properties"]["authorization"]["properties"]
        if rest.startswith("merchant."):
            return rest.split(".", 1)[1] in load().load_schema()["$defs"]["merchant"]["properties"]
        return rest in props and rest not in ("merchant", "items")
    return False


def comparable(rules: list[dict]) -> set[tuple]:
    """Rule set minus the always-on defaults, for paraphrase comparisons."""
    default_fields = {r["field"] for r in SAFETY_NET} | {"derived.quasi_cash_lines", "derived.recurring_lines"}
    out = set()
    for r in rules:
        if r["field"] in default_fields and r["field"] != "derived.session_integrity":
            continue
        v = tuple(sorted(r["value"])) if isinstance(r["value"], list) else r["value"]
        out.add((r["field"], r["operator"], v, r.get("period_days")))
    return out
