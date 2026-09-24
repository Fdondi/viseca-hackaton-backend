"""Lab 2: parsing a specific mandate (the customer's instruction to their agent) into rules."""
from __future__ import annotations

from fastapi import HTTPException
from pydantic import BaseModel

from .. import llm
from ..data import load
from ..flow_web.app import DATA_KIND, SAFETY_FIELDS, _data_kinds
from ..lineage import ai_role, how
from ..platform import SimPlatform
from ..session import Session
from .common import make_app

app = make_app("Lab 2 · Mandates", "mandate.html")
PACK = load()


class CompileIn(BaseModel):
    instruction: str
    scenario_id: str = "SCEN0000"
    use_model: bool = True


@app.get("/api/examples")
def examples():
    s = Session(platform=SimPlatform())
    out = []
    for sid, sc in sorted(PACK.scenarios.items()):
        cust, card = s.customer_for(sid)
        out.append({"scenario_id": sid, "persona": PACK.customers[cust]["persona_name"], "story": sc["scenario_name"],
                    "instruction": sc["cardholder_instruction"], "card_id": card})
    return {"examples": out, "model": {"available": llm.available(), "provider": llm.STATUS.get("provider")}}


@app.post("/api/compile")
def compile_(body: CompileIn):
    if body.scenario_id not in PACK.scenarios:
        raise HTTPException(404, "Unknown customer story.")
    s = Session(platform=SimPlatform(), use_llm=body.use_model)
    comp = s.compile(body.instruction.strip(), body.scenario_id)
    safety = {r["field"] for r in SAFETY_FIELDS}
    rules = []
    for i, n in enumerate(comp["draft"]["notes"]):
        f = n["rule"]["field"]
        origin = "words" if n["tier"] == "your rules" else "llm" if n["tier"].startswith("suggested") else "always"
        rules.append({"index": i, "origin": origin, "phrase": n["source"] if origin == "words" else None,
                      "ai_added": n.get("ai_added"),
                      "kind": "signal" if origin == "always" and f in safety else "rule", "text": n["text"], "rule": n["rule"],
                      "data": [DATA_KIND[k] for k in _data_kinds(f)], "how": how(f),
                      "ai": ai_role(f, s.use_llm and llm.extraction_enabled())})
    return {"rules": rules, "uncertainty_policy": comp["draft"]["uncertainty_policy"],
            "guidance": comp["draft"]["guidance"], "open_questions": comp["draft"]["open_questions"],
            "model": comp["model"] or {"provider": None, "model": None, "proposed": 0, "suggested": 0, "dropped": [],
                                       "last_error": "switched off" if not body.use_model else "no model configured"},
            "backtest": comp["backtest"], "used_model": s.use_llm}
