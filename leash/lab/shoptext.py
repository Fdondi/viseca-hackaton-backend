"""Lab 4: reading the shop's text. Any product text → the typed facts it yields (and the exact words each came
from), and whether the injection detector fires, on which pattern and which words."""
from __future__ import annotations

import csv
from pathlib import Path

from pydantic import BaseModel

from .. import llm
from ..data import load
from ..injection import COMPILED, RESIDUAL_THRESHOLD, VOCAB_THRESHOLD, detect, normalise
from ..wall import extract_line
from .common import make_app

app = make_app("Lab 4 · Shop text", "shoptext.html")
PACK = load()
EVAL = Path(__file__).resolve().parent.parent / "eval"
FACTS = ("size", "return_days", "final_sale", "recurring_billing", "warranty_months", "quasi_cash", "addon")


class ReadIn(BaseModel):
    lines: list[str]
    description: str = ""
    shop_name: str = ""
    use_model: bool = False


def _tsv(name: str, n: int = 12) -> list[dict]:
    with open(EVAL / name, encoding="utf-8") as fh:
        return list(csv.DictReader(fh, delimiter="\t"))[:n]


def _detector(text: str) -> dict:
    rep = detect(text)
    clean, _ = normalise(text or "")
    exact = []                                      # the exact words each plain-text pattern matched
    for klass, pats in COMPILED.items():
        for name, rx in pats:
            if m := rx.search(clean):
                exact.append({"class": klass, "rule": name, "match": m.group(0)})
    return {"flagged": rep.flagged, "hits": [h.as_dict() for h in rep.hits], "exact": exact,
            "unexplained": rep.unexplained, "residual": len(rep.unexplained), "residual_threshold": RESIDUAL_THRESHOLD,
            "residual_flag": rep.residual_flag, "vocab": rep.vocab, "vocab_threshold": VOCAB_THRESHOLD}


@app.get("/api/examples")
def examples():
    seen, story = set(), []
    for rows in PACK.attempt_items.values():
        for l in rows:
            if l["item_details"] not in seen:
                seen.add(l["item_details"])
                story.append({"text": l["item_details"], "label": f"{l['item_name']} ({l['item_id']})"})
    return {"story": story, "attacks": [{"text": r["text"], "label": r["class"]} for r in _tsv("injection_attacks.tsv", 40)],
            "benign": [{"text": r["text"], "label": "benign but tricky"} for r in _tsv("benign_hard.tsv", 30)],
            "model": llm.available() and llm.extraction_enabled()}


@app.post("/api/read")
def read(body: ReadIn):
    items = [{"line_no": i + 1, "item_details": t} for i, t in enumerate(body.lines)]
    facts = [extract_line(l) for l in items]
    used_model = False
    if body.use_model and llm.available():
        before = [dict(f.sources) for f in facts]
        facts = llm.extract_fallback(items, facts)
        used_model = any(f.sources != b for f, b in zip(facts, before))
    lines = []
    for l, f in zip(items, facts):
        d = f.as_dict()
        lines.append({"line_no": l["line_no"], "text": l["item_details"], "facts": {k: d[k] for k in FACTS},
                      "matches": d.get("matches", {}), "sources": d.get("sources", {}), "withheld": f.walled_off,
                      "detector": _detector(l["item_details"])})
    return {"lines": lines, "description": _detector(body.description), "shop_name": _detector(body.shop_name),
            "used_model": used_model}
