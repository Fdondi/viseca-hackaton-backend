"""(G) Injection detection on untrusted merchant text.

Definition: untrusted text that talks to the decision-maker instead of
describing the product. Five pattern classes:

  machine      addresses the machine (role markers, "note for AI agents")
  authority    claims authority or consent the merchant cannot hold
  directive    tells us how to decide or which rules to drop
  bypass       tries to route around the customer (unavailable, don't ask, urgency)
  obfuscation  hides any of the above (zero-width chars, homoglyphs, spacing,
               leetspeak, base64, hidden markup)

The text is normalised first and every pattern runs on the plain,
de-spaced and de-leeted variants. A hit found only in a transformed variant
is also reported as obfuscation.

A deterministic coverage residual (words not explained by product vocabulary)
is a weak extra signal. Detection only ever escalates a decision; nothing
here can make one more permissive.
"""
from __future__ import annotations

import base64
import binascii
import re
import unicodedata
from dataclasses import dataclass, field
from functools import lru_cache

ZERO_WIDTH = dict.fromkeys(map(ord, "​‌‍⁠﻿­‎‏‪‫‬‭‮"))

# Cyrillic and Greek letters that render like Latin ones.
CONFUSABLES = str.maketrans({
    "а": "a", "е": "e", "о": "o", "р": "p", "с": "c", "х": "x", "у": "y", "і": "i", "ј": "j",
    "ѕ": "s", "ԁ": "d", "ɡ": "g", "һ": "h", "ӏ": "l", "ո": "n", "ս": "u", "ν": "v", "ο": "o",
    "α": "a", "ε": "e", "ι": "i", "κ": "k", "ρ": "p", "τ": "t", "υ": "u", "χ": "x",
    "А": "A", "В": "B", "Е": "E", "К": "K", "М": "M", "Н": "H", "О": "O", "Р": "P", "С": "C",
    "Т": "T", "Х": "X", "І": "I",
})
LEET = str.maketrans({"0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "7": "t", "@": "a", "$": "s"})

_P = r"(?:cardholder|card holder|customer|account holder|owner|user|buyer|client)"

PATTERNS: dict[str, list[tuple[str, str]]] = {
    "machine": [
        ("role tag", r"(?:\[\s*|<\s*/?\s*|#+\s*|\*\*\s*)(?:system|assistant|developer|admin(?:istrator)?|inst)\s*[:\]>*]"),
        ("addresses an AI/agent", r"\b(?:automated|automatic|autonomous|ai|a\.i\.|llm|gpt|language model)\s+(?:purchasing\s+|shopping\s+|payment\s+|buying\s+)?(?:agents?|assistants?|systems?|bots?|models?)\b"),
        ("addresses a shopping agent", r"\b(?:shopping|purchasing|buying|payment)\s+(?:agents?|bots?)\b"),
        ("note for agents", r"\b(?:note|message|instructions?|notice|attention)\s+(?:for|to)\s+(?:the\s+|any\s+|all\s+)?(?:ai|agents?|assistants?|bots?|models?|automated|shopping)"),
        ("if you are an AI", r"\bif you are an? (?:ai|agent|assistant|bot|language model|llm)\b"),
        ("greets the machine", r"\b(?:dear|hey|hello|hi)\s+(?:ai|agent|assistant|bot)\b"),
        ("addresses an AI (de)", r"\b(?:ki|ai)[- ]?(?:agent|agenten|assistent|assistenten)\b|hinweis f(?:ü|u)r (?:ki|agenten|assistenten)"),
        ("addresses an AI (fr)", r"\bagents? (?:d'achat|automatis(?:é|e)s?|ia)\b|\bassistant (?:ia|virtuel)\b"),
    ],
    "authority": [
        ("pre-authorised", r"\bpre[- ]?(?:authori[sz]ed|approved|cleared|confirmed)\b"),
        ("already approved", r"\balready\s+(?:been\s+)?(?:approved|authori[sz]ed|confirmed|consented|agreed|cleared)\b|\b(?:has|have|was|were)\s+previously\s+been\s+(?:approved|authori[sz]ed|confirmed)"),
        ("customer consented", rf"\b{_P}\s+(?:has\s+|have\s+|had\s+)?(?:already\s+)?(?:approved|authori[sz]ed|agreed|consented|confirmed|pre-?authori[sz]ed|signed off)\b"),
        ("as agreed with customer", rf"\bas agreed with (?:the\s+)?{_P}"),
        ("verified/trusted status", r"\b(?:verified|certified|whitelisted|allow-?listed|trusted)\s+(?:safe|seller|merchant|store|shop|status)\b"),
        ("exempt/whitelisted", r"\b(?:whitelisted|allow-?listed|exempt from)\b"),
        ("limits lifted", r"\b(?:limits?|caps?|restrictions?|checks?)\s+(?:have been|has been|were|are|is)\s+(?:lifted|raised|waived|removed|increased|suspended|disabled)"),
        ("issuer approved", r"\b(?:issuer|bank|viseca|visa|mastercard|card network)\s+(?:has\s+)?(?:approved|verified|authori[sz]ed|whitelisted|cleared)"),
        ("no confirmation required", r"\bno (?:further\s+)?(?:confirmation|approval|verification|review|checks?)\s+(?:is\s+|are\s+)?(?:required|needed|necessary)"),
        ("on behalf of", rf"\bon behalf of (?:the\s+)?(?:{_P}|issuer|bank)"),
        ("already approved (de)", r"\b(?:bereits|schon|vorab)\s+(?:genehmigt|autorisiert|freigegeben|best(?:ä|a)tigt)|\bkarteninhaber (?:hat|h(?:ä|a)tte)\b"),
        ("already approved (fr)", r"\b(?:d(?:é|e)j(?:à|a)|pr(?:é|e)alablement)\s+(?:autoris(?:é|e)|approuv(?:é|e)|valid(?:é|e))|\bpr(?:é|e)-?autoris"),
    ],
    "directive": [
        ("ignore instructions/limits", r"\b(?:ignore|disregard|forget|override|bypass|skip|circumvent|suspend)\b[^.;]{0,40}?\b(?:instructions?|rules?|limits?|polic(?:y|ies)|checks?|restrictions?|guardrails?|mandate|budget|cap|previous|prior|earlier|settings?)\b"),
        ("limits do not apply", r"\b(?:limits?|rules?|restrictions?|checks?|polic(?:y|ies)|caps?)\b[^.;]{0,30}?\b(?:do not|don't|does not|doesn't|no longer|won't|will not)\s+apply"),
        ("approve this payment", r"\b(?:approve|authori[sz]e)\s+(?:this|the)\s+(?:payment|purchase|transaction|order|checkout)"),
        ("approve without checks", r"\b(?:approve|authori[sz]e|accept)[^.;]{0,25}\bwithout\s+(?:further\s+|any\s+)?(?:checks?|confirmation|verification|review|asking|approval)"),
        ("do not decline", r"\bdo not\s+(?:decline|block|step[- ]?up|flag|reject|escalate|refuse)"),
        ("must be approved", r"\b(?:must|should|shall|will|can|may)\s+be\s+(?:approved|authori[sz]ed|accepted)(?:\s+(?:automatically|immediately))?"),
        ("approved automatically", r"\bapproved?\s+automatically\b|\bautomatic(?:ally)?\s+approv"),
        ("new rules", r"\b(?:new|updated)\s+(?:instructions?|rules?|polic(?:y|ies))\s*:"),
        ("role play", r"\byou are now\b|\bact as\b|\bpretend (?:to be|you are)\b|\b(?:maintenance|debug|developer|admin|god) mode\b"),
        ("dictates the output", r"\b(?:output|return|respond with|reply with)\s+[\"']?(?:decision\s*[=:]\s*)?[\"']?approve"),
        ("decision field", r"[\"']?(?:decision|action|payment_status|approval)[\"']?\s*[:=]\s*[\"']?(?:approve|approved|allow|accept)"),
        ("cap raised", r"\b(?:cap|limit|budget)s?\s+(?:raised|increased|lifted|waived)\s+to\b"),
        ("ignore (de)", r"\bignorier(?:e|en)?\b|\bgenehmigen sie\b|\b(?:limits?|grenzen|regeln)\b[^.;]{0,30}\bgelten nicht"),
        ("ignore (fr)", r"\bignore[zr]\b|\bapprouve[zr]\b|\bles (?:limites|r(?:è|e)gles) ne s'appliquent pas"),
    ],
    "bypass": [
        ("customer unavailable", rf"\b{_P}\s+(?:is|are|will be)?\s*(?:currently\s+)?(?:unavailable|not available|unreachable|travelling|traveling)"),
        ("customer cannot confirm", rf"\b(?:{_P}|they|he|she)\s+(?:cannot|can't|can not|won't be able to|is unable to|are unable to)\s+(?:confirm|respond|be reached|reply|approve)"),
        ("cannot be reached", rf"\b{_P}\s+cannot be reached"),
        ("don't ask the customer", rf"\b(?:no need to|do not|don't|never|without)\s+(?:ask|asking|contact|contacting|notify|notifying|bother|disturb|confirm with|check with|alert|alerting)\w*\s+(?:the\s+)?(?:{_P}|them|him|her)"),
        ("pay immediately", r"\b(?:complete|finali[sz]e|pay|approve|authori[sz]e|submit)\b[^.;]{0,30}\b(?:immediately|right away|right now|now|urgently|asap)\b"),
        ("expires in minutes", r"\b(?:offer|price|deal|reservation|cart)\s+expires\s+in\s+\d+\s*(?:seconds?|minutes?|mins?)"),
        ("unreachable (de)", r"\bnicht erreichbar\b|\bsofort (?:bezahlen|zahlen|genehmigen)"),
        ("unreachable (fr)", r"\b(?:pas joignable|injoignable)\b|\bsans (?:demander|pr(?:é|e)venir|contacter)\b"),
    ],
}
COMPILED = {c: [(n, re.compile(p, re.IGNORECASE)) for n, p in ps] for c, ps in PATTERNS.items()}

# Payment-process vocabulary. Product copy describes products; text that talks
# about approval, consent, limits and the cardholder is talking to *us*.
# Score = 2 per distinct strong stem + 1 per distinct medium stem (en/de/fr/it/es).
STRONG_STEMS = [
    r"approv", r"authori[sz]", r"consent", r"card ?holder", r"mandate", r"uncertainty_policy", r"spending",
    r"override", r"whitelist", r"allow-?list", r"genehmig", r"freigab", r"karteninhaber", r"approuv", r"autoris",
    r"titulaire", r"autorizz", r"titolare", r"aprueb", r"aprob", r"autoriz", r"titular",
]
MEDIUM_STEMS = [
    r"payments?\b", r"\bpay\b", r"checkout", r"customer", r"buyer", r"\bowner", r"\blimits?\b", r"confirm",
    r"\bagents?\b", r"\bbots?\b", r"assistant", r"transaction", r"\bbank\b", r"issuer", r"trusted", r"verified",
    r"instructions?\b", r"\brules?\b", r"decision", r"polic(?:y|ies)", r"zahlung", r"\bkunde", r"bezahl",
    r"paiement", r"\bclient", r"pagamento", r"cliente", r"\bpago\b", r"procede",
]
VOCAB_THRESHOLD = 4       # prior; training data does not constrain it
RESIDUAL_THRESHOLD = 8   # chosen on training data only (see eval report)
RX_STRONG = [re.compile(p, re.I) for p in STRONG_STEMS]
RX_MEDIUM = [re.compile(p, re.I) for p in MEDIUM_STEMS]
RX_ROLE = re.compile(r"(?:^|[\s.!?(])(system|assistant|developer|admin(?:istrator)?|operator|note to (?:ai|agents?|bots?))\s*:", re.I)
RX_IMPERATIVE = re.compile(r"\b(?:ignore|approve|treat|proceed|confirm|complete|skip|set|follow|execute|pay|you|your|this (?:order|purchase|payment|transaction)|the following)\b", re.I)
RX_HIDDEN_STYLE = re.compile(r"display\s*:\s*none|visibility\s*:\s*hidden|font-size\s*:\s*0|opacity\s*:\s*0\b|color\s*:\s*(?:white|#fff)", re.I)


def vocab_score(text: str) -> tuple[int, list[str]]:
    strong = [p.pattern for p in RX_STRONG if p.search(text)]
    medium = [p.pattern for p in RX_MEDIUM if p.search(text)]
    return 2 * len(strong) + len(medium), strong + medium

# Generic product-copy vocabulary for the coverage residual. Written without
# looking at the attempt details, so the benign measurement is not circular.
PRODUCT_WORDS = """
a an the and or of in on for with to from by at as is are be this that these those it its our your
size sizes small medium large colour color black white blue red green grey gray navy material cotton
wool leather synthetic polyester waterproof breathable lightweight durable lined unlined fit regular
slim wide narrow panel inch cm mm kg g ml litre liter pack set piece pieces pair pairs unit units
returns return returned returnable accepted within days day weeks week months month year years
warranty guarantee seller manufacturer standard extended cover repair repairs service services
optional add addon included includes including excludes excluded not free delivery shipping ships
shipped dispatch dispatched delivered collection pickup store online stock available availability
final sale clearance discount discounted offer price prices priced vat tax sold as new used
refurbished condition original packaging box boxed gift wrapped wrap edition model version series
billed billing monthly annually yearly subscription renews renewal cancel cancellation cancellable
anytime period trial first after before beyond up to per each only also may might can will
compatible compatibility requires required works use usage suitable designed made
road trail running run runner shoe shoes sole soles lugged cushioning grip helmet cycling bike
jacket coat rain outerwear seasonal everyday casual sport sports training socks technical
monitor screen display ips resolution hz refresh stand mount cable charger accessory
fruit vegetables basket produce bread oats yoghurt rice pasta tinned goods dry cupboard staples
weekly food household breakfast pantry fresh selection groceries grocery restock
voucher card credit redeemable against any purchase issued immediately email digital code
fragrance beauty cosmetics care skincare toiletries personal treat
off extending extend beyond seller s m l xl xs xxl
"""


@dataclass
class Hit:
    klass: str
    rule: str
    excerpt: str
    via: str = "plain"

    def as_dict(self) -> dict:
        return {"class": self.klass, "rule": self.rule, "excerpt": self.excerpt, "via": self.via}


@dataclass
class InjectionReport:
    hits: list[Hit] = field(default_factory=list)
    unexplained: list[str] = field(default_factory=list)
    residual_threshold: int = 12
    vocab: int = 0

    @property
    def residual_flag(self) -> bool:
        return len(self.unexplained) >= self.residual_threshold

    @property
    def flagged(self) -> bool:
        return bool(self.hits) or self.residual_flag

    @property
    def classes(self) -> list[str]:
        return sorted({h.klass for h in self.hits})

    def as_dict(self) -> dict:
        return {
            "flagged": self.flagged,
            "hits": [h.as_dict() for h in self.hits],
            "classes": self.classes,
            "residual_words": len(self.unexplained),
            "residual_flag": self.residual_flag,
            "process_vocabulary_score": self.vocab,
        }


@lru_cache(maxsize=1)
def _vocabulary() -> frozenset[str]:
    words = set(PRODUCT_WORDS.split())
    try:
        from .data import load

        pack = load()
        for it in pack.items.values():
            words.update(_tokens(it["item_name"] + " " + it["item_description"] + " " + it["item_category"]))
        for m in pack.merchants.values():
            words.update(_tokens(m["merchant_name"] + " " + m["merchant_category"]))
    except Exception:  # vocabulary still works without the data pack
        pass
    return frozenset(words)


def _tokens(text: str) -> list[str]:
    return re.findall(r"[a-zà-ÿ]{3,}", text.lower())


def _stem(w: str) -> str:
    for suf in ("ies", "es", "s", "ed", "ing"):
        if w.endswith(suf) and len(w) - len(suf) >= 3:
            return w[: -len(suf)] + ("y" if suf == "ies" else "")
    return w


def normalise(text: str) -> tuple[str, list[Hit]]:
    """NFKC, strip invisible characters and map homoglyphs. Returns the clean
    text plus obfuscation hits for anything that had to be undone."""
    hits: list[Hit] = []
    t = unicodedata.normalize("NFKC", text)
    stripped = t.translate(ZERO_WIDTH)
    if stripped != t:
        hits.append(Hit("obfuscation", "invisible characters", "zero-width/bidi characters removed"))
    mixed = [w for w in re.findall(r"\w+", stripped) if re.search(r"[a-zA-Z]", w) and re.search(r"[Ͱ-ϿЀ-ӿ]", w)]
    if mixed:
        hits.append(Hit("obfuscation", "mixed-script word", mixed[0]))
    return stripped.translate(CONFUSABLES), hits


def _despace(t: str) -> str:
    return re.sub(r"\b(?:[a-zA-Z][\-._ ]){3,}[a-zA-Z]\b", lambda m: re.sub(r"[\-._ ]", "", m.group(0)), t)


def _deleet(t: str) -> str:
    return re.sub(r"\b\w*[a-zA-Z]\w*\b", lambda m: m.group(0).translate(LEET) if re.search(r"\d", m.group(0)) else m.group(0), t)


def _decoded_payloads(t: str) -> list[str]:
    out = []
    for tok in re.findall(r"[A-Za-z0-9+/]{20,}={0,2}", t):
        if len(tok) % 4 or not (re.search(r"[a-z]", tok) and re.search(r"[A-Z]", tok)):
            continue
        try:
            dec = base64.b64decode(tok, validate=True).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError, ValueError):
            continue
        if dec.count(" ") >= 2 and sum(c.isprintable() for c in dec) / len(dec) > 0.95:
            out.append(dec)
    return out


def _scan(text: str, via: str) -> list[Hit]:
    hits = []
    for klass, pats in COMPILED.items():
        for name, rx in pats:
            m = rx.search(text)
            if m:
                s, e = max(0, m.start() - 20), min(len(text), m.end() + 20)
                hits.append(Hit(klass, name, text[s:e].strip(), via))
    return hits


def detect(text: str | None) -> InjectionReport:
    report = InjectionReport()
    if not text:
        return report
    clean, report.hits = normalise(text)
    plain = _scan(clean, "plain")
    report.hits += plain
    seen = {(h.klass, h.rule) for h in plain}
    variants = [("de-spaced", _despace(clean)), ("de-leeted", _deleet(clean))]
    variants += [("base64", d) for d in _decoded_payloads(clean)]
    if re.search(r"<!--.*?-->", clean, re.S):
        report.hits.append(Hit("obfuscation", "hidden markup comment", re.search(r"<!--.*?-->", clean, re.S).group(0)[:60]))
    for via, variant in variants:
        if variant == clean:
            continue
        new = [h for h in _scan(variant, via) if (h.klass, h.rule) not in seen]
        if new:
            report.hits += new
            report.hits.append(Hit("obfuscation", f"hidden via {via}", new[0].excerpt, via))
            seen |= {(h.klass, h.rule) for h in new}
    if RX_HIDDEN_STYLE.search(clean):
        report.hits.append(Hit("obfuscation", "hidden styling", RX_HIDDEN_STYLE.search(clean).group(0)))
    for m in RX_ROLE.finditer(clean):
        after = clean[m.end(): m.end() + 90]
        score, _ = vocab_score(after)
        if RX_IMPERATIVE.search(after) or score >= 2:
            report.hits.append(Hit("machine", "role marker with instructions", clean[max(0, m.start() - 10): m.end() + 50].strip()))
            break
    report.vocab, stems = vocab_score(clean)
    if report.vocab >= VOCAB_THRESHOLD and not any(h.klass != "obfuscation" for h in report.hits):
        report.hits.append(Hit("process-talk", f"talks about payment approval (score {report.vocab})", ", ".join(stems[:5])))
    vocab = _vocabulary()
    report.unexplained = [w for w in _tokens(clean) if w not in vocab and _stem(w) not in vocab]
    report.residual_threshold = RESIDUAL_THRESHOLD
    return report
