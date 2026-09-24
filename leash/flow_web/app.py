"""Flow demo: a phone on the left (the customer writes an instruction, checks the parsed rules,
answers the wallet), and on the right a diagram of exactly what the wallet received for each
purchase and how every rule was evaluated: which data is merchant free text and which is
structured, where a model was involved and where fixed code decided.

Same path as everything else: simulator → schema-valid event → Session.handle → engine.
"""
from __future__ import annotations

import copy
import threading
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from .. import lineage
from ..data import load
from ..engine import RevokedError
from ..fields import describe
from ..platform import SimPlatform
from ..session import Session

STATIC = Path(__file__).parent / "static"
app = FastAPI(title="Agent on a Leash: how a purchase is evaluated")
LOCK = threading.RLock()
ST: dict = {}
EDITABLE_TIERS = ("your rules", "suggested by AI — please review")


# ------------------------------------------------------------------ explanation of one purchase
def _row(path: str, key: str, value, group: str, line: int | None = None) -> dict:
    return {"path": path, "key": key, "value": value, "class": lineage.classify(key), "group": group, "line": line}


def _received(env: dict) -> list[dict]:
    """The envelope exactly as delivered, flattened into rows tagged with their trust class."""
    ev = env["data"]
    a = ev["authorization"]
    rows = [_row(f"{k}", f"{k}", ev[k], "envelope") for k in ("type", "request_id", "deadline_at")]
    for k, v in a.items():
        if k in ("merchant", "items"):
            continue
        rows.append(_row(f"authorization.{k}", f"authorization.{k}", v, "authorization"))
    for k, v in a["merchant"].items():
        rows.append(_row(f"authorization.merchant.{k}", f"authorization.merchant.{k}", v, "merchant"))
    for i, line in enumerate(a["items"]):
        for k, v in line.items():
            rows.append(_row(f"authorization.items[{i}].{k}", f"authorization.items[].{k}", v, "items", line["line_no"]))
    ctx = ev.get("context") or {}
    rows.append(_row("context.approved_spend_in_period_chf", "context.approved_spend_in_period_chf",
                     ctx.get("approved_spend_in_period_chf"), "context"))
    rows.append(_row("context.recent_authorizations", "context.recent_authorizations",
                     f"{len(ctx.get('recent_authorizations') or [])} earlier attempt(s) in 10 min", "context"))
    m = ev["mandate"]
    for k in ("mandate_id", "status", "uncertainty_policy"):
        rows.append(_row(f"mandate.{k}", f"mandate.{k}", m[k], "mandate"))
    rows.append(_row("mandate.hard_rules", "mandate.hard_rules", f"{len(m['hard_rules'])} rules (your confirmed permissions)", "mandate"))
    pres = env.get("ap2")
    if pres:
        from ..ap2 import peek
        for k in ("open_payment", "open_checkout", "closed_payment", "closed_checkout"):
            tok = pres.get(k)
            signer = peek(tok)[0].get("kid") if tok else None
            rows.append(_row(f"ap2.{k}", "ap2.presentation", f"signed by {signer}" if tok else "not presented", "ap2"))
        cc = pres.get("closed_checkout")
        if cc:
            h, p = peek(peek(cc)[1]["checkout_jwt"])
            rows.append(_row("ap2.checkout_jwt", "ap2.presentation",
                             f"cart from {p['merchant']['id']}, {p['currency']} {p['totals'][-1]['amount'] / 100:.2f}, signed by {h.get('kid')}",
                             "ap2"))
    return rows


def _wall(event: dict, result: dict) -> list[dict]:
    out = []
    for line, f in zip(event["authorization"]["items"], result.get("facts", [])):
        model = any("model" in str(v) for v in f.get("sources", {}).values())
        out.append({"line_no": f["line_no"], "text": line["item_details"], "matches": f.get("matches", {}), "facts": {k: f[k] for k in (
            "size", "return_days", "final_sale", "recurring_billing", "quasi_cash", "addon", "warranty_months")},
            "sources": f.get("sources", {}), "flagged": f["injection"]["flagged"],
            "hits": [h["rule"] for h in f["injection"].get("hits", [])], "model": model})
    return out


def _origin(check: dict, rule: dict | None, notes: list[dict]) -> dict:
    tier = check["tier"]
    if tier == "platform":
        return {"kind": "platform", "label": "platform status"}
    if tier == "ap2":
        return {"kind": "ap2", "label": "AP2 verification"}
    if tier == "your-controls":
        return {"kind": "controls", "label": "your controls"}
    if tier == "added-by-you":
        return {"kind": "added", "label": "added by you during the run"}
    for n in notes:
        if rule is not None and n["rule"] == rule:
            if n["tier"] == "your rules":
                return {"kind": "words", "label": "your words", "phrase": n["source"], "edited": n.get("edited", False)}
            if n["tier"].startswith("suggested"):
                return {"kind": "llm", "label": "AI suggestion, reviewed and confirmed by you", "edited": n.get("edited", False)}
            return {"kind": "default", "label": "always-on safety check"}
    return {"kind": "default", "label": "always-on safety check" if tier == "safety-net" else tier}


def _short(path: str) -> str:
    return (path.replace("authorization.", "").replace("merchant.", "shop ").replace("items[", "line[")
            .replace("ap2.", "").replace("_", " ") if path.startswith("ap2.") else
            path.replace("authorization.", "").replace("merchant.", "shop ").replace("items[", "line["))


def _inputs(c: dict, reads: list[str], received: list[dict], wall: list[dict]) -> list[dict]:
    """The actual values a check read, each tagged with where it came from."""
    by_key: dict[str, list[dict]] = {}
    for r in received:
        by_key.setdefault(r["key"], []).append(r)
    out = []
    for k in reads:
        if k == "ap2.presentation" and c["tier"] != "ap2":      # a rule using a signed term: only the signed cart matters
            out += [{"label": "shop-signed cart", "value": r["value"], "class": r["class"]}
                    for r in by_key.get(k, []) if r["path"] == "ap2.checkout_jwt"]
        elif k in by_key:
            out += [{"label": _short(r["path"]), "value": r["value"], "class": r["class"]} for r in by_key[k]]
        elif k.startswith("wall."):
            name = k.split(".", 1)[1]
            for w in wall:
                src = w["sources"].get(name, "")
                withheld = w["flagged"] and name in ("size", "return_days", "final_sale")
                out.append({"label": f"line {w['line_no']} {lineage.NODES.get(k, {}).get('label', name)}",
                            "value": w["facts"].get(name, "unknown"), "class": "llm" if "model" in src else "wall",
                            "from": "withheld: this line's text instructs the payment system" if withheld
                            else (f"“{w['matches'][name]}”" if w["matches"].get(name) else "no wording for this in the shop text")})
        elif k == "detector.injection":
            for key in lineage.NODES[k]["reads"]:          # the exact texts the detector scanned
                out += [{"label": _short(r["path"]), "value": r["value"], "class": r["class"]} for r in by_key.get(key, [])]
        elif k == "detector.lookalike":
            for key in lineage.NODES[k]["reads"]:
                out += [{"label": _short(r["path"]), "value": r["value"], "class": r["class"]} for r in by_key.get(key, [])]
        elif k == "ap2.presentation":
            out.append({"label": "AP2 permission, cart and payment", "value": "signed tokens", "class": "signed"})
        elif k in lineage.NODES:
            out.append({"label": lineage.NODES[k]["label"], "value": "consulted", "class": "viseca" if k.startswith("viseca.") else "customer"})
    for k, v in (c.get("extra") or {}).items():
        if isinstance(v, (int, float, str)) and not isinstance(v, bool) and k not in ("codes",):
            out.append({"label": k.replace("_", " "), "value": v, "class": "viseca"})
    return out


def _rules(result: dict, notes: list[dict], wall: list[dict], received: list[dict]) -> list[dict]:
    rules = list(result.get("rules", []))
    used = [False] * len(rules)
    model_facts = {k for w in wall for k, v in w["sources"].items() if "model" in str(v)}
    out = []
    for c in result.get("checks", []):
        rule = None
        for i, r in enumerate(rules):
            if not used[i] and r["field"] == c["field"]:
                used[i], rule = True, r
                break
        reads = list(lineage.reads(c["field"]))
        if c["provenance"] == "shop-signed" and "ap2.presentation" not in reads:
            reads.append("ap2.presentation")
        wall_facts = [r.split(".", 1)[1] for r in reads if r.startswith("wall.")]
        out.append({
            "field": c["field"], "status": c["status"], "text": c["text"], "tier": c["tier"], "code": c["code"],
            "provenance": c["provenance"], "security": c["security"],
            "rule": rule, "rule_text": describe(rule) if rule else None,
            "origin": _origin(c, rule, notes),
            "reads": reads, "how": lineage.how(c["field"]),
            "text_input": bool(wall_facts) or any(r in lineage.FREE_TEXT for r in reads) or "detector.injection" in reads,
            "model_fact": bool(set(wall_facts) & model_facts),
            "signed": bool((c.get("extra") or {}).get("signed_permission")),
            "inputs": _inputs(c, reads, received, wall),
            "trace": c.get("trace", []),
            "actual": c.get("actual"), "expected": c.get("expected"),
        })
    return out


def _view(env: dict) -> dict:
    """The request, laid out for people: the proposal, the session, the terms, the mandate snapshot."""
    ev = env["data"]
    a, m, ctx = ev["authorization"], ev["mandate"], ev.get("context") or {}
    return {
        "envelope": {"type": ev["type"], "request_id": ev["request_id"], "deadline_at": ev["deadline_at"],
                     "authorization_id": a["authorization_id"], "source": a["source_authorization_id"], "run_id": env.get("run_id")},
        "shop": a["merchant"],
        "lines": [{k: l[k] for k in ("line_no", "quantity", "item_name", "item_id", "item_category", "unit_price", "currency", "item_details")}
                  for l in a["items"]],
        "totals": {k: a[k] for k in ("items_subtotal", "delivery_fee", "amount", "currency", "billing_amount_chf")},
        "session": {k: a[k] for k in ("timestamp", "channel", "customer_device_id", "recent_attempt_count_10m", "initiator_type",
                                      "card_id", "card_status_at_attempt", "authority_status")},
        "terms": {k: a[k] for k in ("fulfillment_method", "delivery_by", "order_returnable", "order_cancellable",
                                    "related_authorization_id", "related_authorization_status")},
        "description": a["purchase_description"],
        "context": {"approved_spend_in_period_chf": ctx.get("approved_spend_in_period_chf"),
                    "recent_authorizations": len(ctx.get("recent_authorizations") or [])},
        "mandate": {"mandate_id": m["mandate_id"], "status": m["status"], "uncertainty_policy": m["uncertainty_policy"],
                    "rules": [describe(r) for r in m["hard_rules"]]},
        "ap2": bool(env.get("ap2")),
    }


def _as_sent(env: dict) -> dict:
    """The envelope as delivered, with AP2 tokens shortened for display."""
    out = copy.deepcopy(env)
    for k, tok in (out.get("ap2") or {}).items():
        if isinstance(tok, str) and len(tok) > 48:
            out["ap2"][k] = tok[:40] + "…(signed JWS)"
    return out


def _explain(env: dict, result: dict) -> dict:
    event = env["data"]
    wall = _wall(event, result)
    received = _received(env)
    rules = _rules(result, ST["notes"], wall, received)
    counts = {s: sum(1 for r in rules if r["status"] == s) for s in ("fail", "unknown", "pass")}
    branch = "fail" if counts["fail"] else "unknown" if counts["unknown"] else "pass"
    return {
        "authorization_id": result["authorization_id"], "source": result["source_authorization_id"],
        "received": received, "wall": wall, "rules": rules, "view": _view(env),
        "nodes": lineage.NODES, "wall_reads": lineage.WALL_READS, "classes": lineage.CLASSES,
        "combine": {"counts": counts, "branch": branch, "policy": result.get("uncertainty_policy"),
                    "decision": result["decision"], "reason_codes": result["reason_codes"],
                    "security": [r["field"] for r in rules if r["status"] == "unknown" and r["security"]]},
        "answer": {k: result[k] for k in ("decision", "reason_codes", "customer_message", "engine_version")},
        "receipt": (result.get("ap2_receipt") or {}).get("payload"),
        "model_facts": any(w["model"] for w in wall),
        "request": _as_sent(env),
        "reasons": sorted(({"status": c["status"], "text": c["text"]} for c in result.get("checks", []) if c["status"] != "pass"),
                          key=lambda r: r["status"] != "fail"),
        "passed": sum(1 for c in result.get("checks", []) if c["status"] == "pass"),
        "shop_text": [{"line_no": l["line_no"], "item": l["item_name"], "text": l["item_details"]}
                      for l in event["authorization"]["items"]],
        "latency_ms": result.get("latency_ms"),
    }


# ------------------------------------------------------------------ what AP2 changed
def _without_ap2(s: Session, env: dict) -> dict:
    """The same proposal decided without AP2, on a copy of the state: nothing real is recorded."""
    from ..engine import Engine
    from ..ledger import Store
    clone = Store()
    clone.runs = copy.deepcopy(s.engine.store.runs)
    clone.controls = copy.deepcopy(s.engine.store.controls)
    clone.checkout_hashes = dict(s.engine.store.checkout_hashes)
    e = Engine(s.pack, store=clone, extractor_fallback=s.engine.extractor_fallback)
    e._profiles = s.engine._profiles
    return e.decide(copy.deepcopy(env["data"]), env.get("run_id"))


def _ap2_diff(real: dict, base: dict, rules: list[dict]) -> dict:
    """Check by check: what the signed cart added, changed or made redundant. Marks the rules in place."""
    base_checks = list(base["checks"])
    used = [False] * len(base_checks)
    added, changed = [], []
    for i, c in enumerate(real["checks"]):
        j = next((k for k, b in enumerate(base_checks) if not used[k] and b["field"] == c["field"]), None)
        if j is None:
            added.append({"field": c["field"], "status": c["status"], "text": c["text"]})
            rules[i]["ap2_effect"] = "added by AP2"
            continue
        used[j] = True
        b = base_checks[j]
        if b["status"] != c["status"]:
            effect = f"without AP2: {b['status']}"
        elif c["provenance"] == "shop-signed" and b["provenance"] != "shop-signed":
            effect = "uses a shop-signed term"
        elif (c.get("extra") or {}).get("signed_permission"):
            effect = "also in the signed permission"
        elif b["text"] != c["text"]:
            effect = "explained with the signature"
        else:
            continue
        rules[i]["ap2_effect"] = effect
        changed.append({"field": c["field"], "from": b["status"], "to": c["status"], "effect": effect, "text": c["text"],
                        "before": b["text"]})
    removed = [{"field": b["field"], "status": b["status"], "text": b["text"]} for k, b in enumerate(base_checks) if not used[k]]
    evidence = [f"The alert about {f['merchant_name']} keeps its signed cart: the shop can't deny sending that text."
                for f in real.get("flags_created", []) if (f.get("incidents") or [{}])[-1].get("signed_by_shop")]
    return {"decision": {"without": base["decision"], "with": real["decision"]},
            "reasons": {"without": base["reason_codes"], "with": real["reason_codes"]},
            "added": added, "changed": changed, "removed": removed, "evidence": evidence}


# ------------------------------------------------------------------ state
def _session() -> Session:
    if "session" not in ST:
        raise HTTPException(400, "Set up the agent first.")
    return ST["session"]


def _state() -> dict:
    out = {"step": ST.get("step", "setup"), "customer": ST.get("customer"), "setup": ST.get("setup"),
           "notes": ST.get("notes"), "policy": ST.get("policy")}
    if ST.get("run_id"):
        s = _session()
        run = s.platform.runs[ST["run_id"]]
        ledger = s.engine.store.runs.get(ST["run_id"])
        feed = []
        for aid, ex in ST["explain"].items():
            rec = ledger.get(aid) if ledger else None
            feed.append({"authorization_id": aid, "source": ex["source"], "decision": ex["answer"]["decision"],
                         "status": rec.status if rec else None, "message": ex["answer"]["customer_message"],
                         "merchant": ex["merchant"], "amount_chf": ex["amount_chf"], "items": ex["items"],
                         "reasons": ex["reasons"], "passed": ex["passed"], "shop_text": ex["shop_text"], "ap2": ex["view"]["ap2"],
                         "resolved_by": rec.resolved_by if rec else None})
        nxt = None
        if run["released"] < len(run["rows"]) and not run["stopped"]:
            row = run["rows"][run["released"]]
            lines = row.get("_items") or s.pack.attempt_items.get(row["authorization_id"], [])
            nxt = {"source": row["authorization_id"], "merchant": s.pack.merchants.get(row["merchant_id"], {}).get("merchant_name", row["merchant_id"]),
                   "amount": row["amount"], "currency": row["currency"], "items": [l["item_name"] for l in lines]}
        out.update(feed=feed, next=nxt, total=len(run["rows"]), mandate_id=ST["mandate_id"],
                   selected=ST.get("selected"), explain=ST["explain"].get(ST.get("selected")))
    return out


# ------------------------------------------------------------------ API
class CompileIn(BaseModel):
    scenario_id: str
    instruction: str


class NextIn(BaseModel):
    ap2: bool = False      # does this shop sign its cart? The shop's choice, not the customer's


class RuleEdit(BaseModel):
    index: int
    value: str | float | int | list | None = None
    removed: bool = False


class ConfirmIn(BaseModel):
    edits: list[RuleEdit] = []
    uncertainty_policy: str = "ask"


class ResolveIn(BaseModel):
    authorization_id: str
    decision: str


class SelectIn(BaseModel):
    authorization_id: str


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/customers")
def customers():
    pack = load()
    s = Session()
    out = []
    for sid, sc in sorted(pack.scenarios.items()):
        cust, card = s.customer_for(sid)
        out.append({"scenario_id": sid, "persona": pack.customers[cust]["persona_name"], "story": sc["scenario_name"],
                    "instruction": sc["cardholder_instruction"], "card_id": card})
    return {"customers": out}


@app.get("/api/state")
def state():
    with LOCK:
        return _state()


@app.post("/api/compile")
def compile_(body: CompileIn):
    with LOCK:
        s = Session(platform=SimPlatform(), use_llm=True, ap2=True)
        pack = s.pack
        if body.scenario_id not in pack.scenarios:
            raise HTTPException(404, "Unknown customer.")
        comp = s.compile(body.instruction.strip(), body.scenario_id)
        cust, card = s.customer_for(body.scenario_id)
        notes = [{**n, "index": i} for i, n in enumerate(comp["draft"]["notes"])]
        from .. import llm
        model = comp.get("model")
        ST.clear()
        ST.update(session=s, scenario_id=body.scenario_id, draft=comp["draft"], notes=notes, step="review",
                  policy=comp["draft"]["uncertainty_policy"], explain={},
                  customer={"persona": pack.customers[cust]["persona_name"], "card_id": card,
                            "story": pack.scenarios[body.scenario_id]["scenario_name"]},
                  setup={"instruction": body.instruction.strip(),
                         "parser": [{"text": n["text"], "phrase": n["source"]} for n in notes if n["tier"] == "your rules"],
                         "model": model or {"provider": llm.STATUS.get("provider"), "model": None,
                                            "last_error": None if llm.available() else "no model configured",
                                            "proposed": 0, "suggested": 0, "dropped": []},
                         "suggested": [n["text"] for n in notes if n["tier"].startswith("suggested")],
                         "always_on": [n["text"] for n in notes if n["tier"] == "safety net"],
                         "backtest": comp["backtest"]["summary"], "edits": [], "confirmed": False})
        return _state()


def _coerce(old, new):
    if isinstance(old, bool) or new is None:
        return old
    if isinstance(old, (int, float)):
        v = float(new)
        return int(v) if v.is_integer() and isinstance(old, int) else v
    if isinstance(old, list):
        return [x.strip() for x in (new if isinstance(new, list) else str(new).split(",")) if str(x).strip()]
    return str(new)


@app.post("/api/confirm")
def confirm(body: ConfirmIn):
    with LOCK:
        s = _session()
        if ST.get("step") != "review":
            raise HTTPException(409, "Nothing to confirm.")
        notes = ST["notes"]
        edits = []
        for e in body.edits:
            n = notes[e.index]
            if n["tier"] not in EDITABLE_TIERS:
                raise HTTPException(422, "Always-on safety checks can't be edited.")
            if e.removed:
                n["removed"] = True
                edits.append(f"removed: {n['text']}")
                continue
            new = _coerce(n["rule"]["value"], e.value)
            if new != n["rule"]["value"]:
                n["rule"] = {**n["rule"], "value": new}
                old_text, n["text"], n["edited"] = n["text"], describe(n["rule"]), True
                edits.append(f"{old_text} → {n['text']}")
        if body.uncertainty_policy not in ("ask", "decline", "approve"):
            raise HTTPException(422, "Unknown uncertainty policy.")
        kept = [n for n in notes if not n.get("removed")]
        ST["notes"] = kept
        draft = {**ST["draft"], "hard_rules": [n["rule"] for n in kept], "uncertainty_policy": body.uncertainty_policy}
        m = s.confirm(draft, ST["scenario_id"])
        rows = [copy.deepcopy(r) for r in s.pack.scenario_attempts(ST["scenario_id"])]
        info = s.platform.inject_run(ST["scenario_id"], m["mandate_id"], rows)
        s.runs[info["run_id"]] = info
        ST["setup"].update(edits=edits, confirmed=True, mandate_id=m["mandate_id"], rules=len(draft["hard_rules"]),
                           ap2=(m.get("ap2") or {}).get("visibility"))
        ST.update(step="shopping", run_id=info["run_id"], mandate_id=m["mandate_id"], policy=body.uncertainty_policy)
        return _state()


@app.post("/api/next")
def next_purchase(body: NextIn):
    with LOCK:
        s = _session()
        if not ST.get("run_id"):
            raise HTTPException(409, "Confirm the rules first.")
        run = s.platform.runs[ST["run_id"]]
        idx = run["released"]
        if run["stopped"] or idx >= len(run["rows"]):
            raise HTTPException(409, "The agent has no more purchases in this story.")
        run["rows"][idx]["_ap2"] = {} if body.ap2 else {"off": True}
        env = s.platform.next_request(wait=0)
        if env is None:
            raise HTTPException(409, "The simulator is waiting for an earlier purchase to be decided.")
        without = _without_ap2(s, env) if env.get("ap2") else None
        result = s.handle(env)
        ex = _explain(env, result)
        ex["ap2_diff"] = _ap2_diff(result, without, ex["rules"]) if without else None
        ex.update(merchant=result["merchant"]["merchant_name"], amount_chf=result["amount_chf"],
                  items=[f"{l['quantity']}× {l['item_name']}" for l in result["items"]])
        ST["explain"][result["authorization_id"]] = ex
        ST["selected"] = result["authorization_id"]
        return _state()


@app.post("/api/select")
def select(body: SelectIn):
    with LOCK:
        if body.authorization_id not in ST.get("explain", {}):
            raise HTTPException(404, "Unknown purchase.")
        ST["selected"] = body.authorization_id
        return _state()


@app.post("/api/resolve")
def resolve(body: ResolveIn):
    with LOCK:
        try:
            _session().resolve(ST["run_id"], body.authorization_id, body.decision)
        except (RevokedError, ValueError) as exc:
            raise HTTPException(409, str(exc))
        return _state()


@app.post("/api/reset")
def reset():
    with LOCK:
        ST.clear()
        return _state()
