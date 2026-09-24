"""Evaluation: everything the demo report quotes, computed from scratch.

  1. scenario replay vs pre-registered labels (cautious + trusting customer,
     plus the "flag removed" / "blocked" branches of the shady-merchant policy)
  2. decision latency
  3. injection detector: attack corpus vs benign corpora
  4. lookalike detector: all merchant pairs + generated typosquats
  5. mutation suite: hidden-scenario proxy with generator-defined expectations
  6. confirmation backtest (friction on history)
  7. compiler robustness on pre-written paraphrases
"""
from __future__ import annotations

import copy
import csv
import json
import statistics
import time
from collections import Counter, defaultdict
from pathlib import Path

from ..compiler import Compiler, comparable
from ..data import load
from ..injection import detect
from ..lookalike import THRESHOLD, similarity
from ..profile import build_profile
from ..session import Session

HERE = Path(__file__).parent
DECISIONS = ("approve", "step_up", "decline")


def labels() -> dict:
    return json.loads((HERE / "expected.json").read_text())["labels"]


def compiled_mandates(session: Session) -> dict[str, dict]:
    out = {}
    for sid, sc in sorted(session.pack.scenarios.items()):
        out[sid] = session.compile(sc["cardholder_instruction"], sid)
    return out


def replay(scenario_id: str, customer: str = "cautious", on_result=None, rows=None, compiled=None) -> tuple[Session, list[dict]]:
    """Fresh session (fresh controls) for one scenario run."""
    s = Session()
    comp = compiled or s.compile(s.pack.scenarios[scenario_id]["cardholder_instruction"], scenario_id)
    m = s.confirm(comp["draft"], scenario_id)
    if on_result:
        s.listeners.append(lambda r: on_result(s, r))
    if rows is None:
        run = s.start(scenario_id, m["mandate_id"])
    else:
        run = s.platform.inject_run(scenario_id, m["mandate_id"], rows)
        s.runs[run["run_id"]] = run
    answer = {"cautious": "decline", "trusting": "approve"}[customer]
    res = s.drive(run["run_id"], customer=lambda r: answer)
    return s, res


# ------------------------------------------------------------------ 1 + 2
def scenario_stats(compiled: dict) -> dict:
    lab = labels()
    out = {"cautious": [], "trusting": []}
    for cust in out:
        for sid in sorted(compiled):
            _, res = replay(sid, cust, compiled=compiled[sid])
            for r in res:
                e = lab[r["source_authorization_id"]]
                out[cust].append({
                    "id": r["source_authorization_id"], "scenario": sid, "decision": r["decision"],
                    "expected": e["expected"], "alt": e.get("alt", []), "why_expected": e["why"],
                    "policy_friction": e.get("policy_friction", False),
                    "reason_codes": r["reason_codes"], "headline": r["headline"],
                    "message": r["customer_message"], "latency_ms": r.get("latency_ms"),
                    "amount_chf": r["amount_chf"], "merchant": r["merchant"]["merchant_name"],
                    "security_flags": r.get("security_flags", []),
                })
    c = out["cautious"]
    strict = sum(x["decision"] == x["expected"] for x in c)
    lenient = sum(x["decision"] == x["expected"] or x["decision"] in x["alt"] for x in c)
    confusion = {e: {d: 0 for d in DECISIONS} for e in DECISIONS}
    for x in c:
        confusion[x["expected"]][x["decision"]] += 1
    exp_approve = [x for x in c if x["expected"] == "approve"]
    # friction relative to the "own facts" reading: the alt=approve cases stepped up by policy
    policy_friction = [x for x in c if x["policy_friction"] and x["decision"] != "approve"]
    stops_needed = [x for x in c if x["expected"] != "approve"]
    trusting_diff = [
        {"id": t["id"], "cautious": k["decision"], "trusting": t["decision"], "headline": t["headline"]}
        for k, t in zip(out["cautious"], out["trusting"]) if k["decision"] != t["decision"]
    ]
    lat = [x["latency_ms"] for x in c + out["trusting"] if x["latency_ms"] is not None]
    return {
        "n": len(c),
        "strict_agreement": strict, "lenient_agreement": lenient,
        "confusion": confusion,
        "decision_counts": dict(Counter(x["decision"] for x in c)),
        "disagreements": [x for x in c if x["decision"] != x["expected"]],
        "unnecessary_friction": [x for x in exp_approve if x["decision"] != "approve"],
        "policy_friction": policy_friction,
        "missed_stops": [x for x in stops_needed if x["decision"] == "approve"],
        "trusting_diff": trusting_diff,
        "rows": c,
        "latency_ms": {"p50": round(statistics.median(lat), 2), "p95": round(sorted(lat)[int(0.95 * (len(lat) - 1))], 2),
                       "max": round(max(lat), 2), "n": len(lat)},
    }


def policy_branches(compiled: dict) -> dict:
    """SCEN0004 with the customer answering the PixelHarbor alert when AU0040 (the second
    injection) reaches them: keep asking / block the shop / remove the flag."""
    out = {}
    for mode in ("remove", "block", "ask"):
        def act(s, r, mode=mode):
            if r["source_authorization_id"] == "AU0040" and mode != "ask":
                s.set_merchant_flag(s.mandates[r["mandate_id"]]["customer_id"], r["merchant"]["merchant_id"], mode)
        _, res = replay("SCEN0004", "cautious", on_result=act, compiled=compiled["SCEN0004"])
        out[mode] = {r["source_authorization_id"]: {"decision": r["decision"], "headline": r["headline"]}
                     for r in res if r["source_authorization_id"] in ("AU0040", "AU0042", "AU0045")}
    return out


# ------------------------------------------------------------------ 3
def _tsv(name: str) -> list[dict]:
    return list(csv.DictReader(open(HERE / name, encoding="utf-8"), delimiter="\t"))


def injection_stats(holdout: bool = False) -> dict:
    pack = load()
    if holdout:
        attacks = _tsv("injection_attacks_holdout.tsv")
        benign = {"hand-written holdout product copy": [r["text"] for r in _tsv("benign_holdout.tsv")]}
    else:
        attacks = _tsv("injection_attacks.tsv")
        clean_details = [l["item_details"] for aid, ls in pack.attempt_items.items() for l in ls if aid not in ("AU0037", "AU0040")]
        benign = {
            "item catalogue descriptions": [i["item_description"] for i in pack.items.values()],
            "clean purchase-attempt details": clean_details,
            "history descriptions (unique)": sorted({r["description"] for r in pack.history}),
            "hand-written hard negatives": [r["text"] for r in _tsv("benign_hard.tsv")],
        }
    per_class = defaultdict(lambda: {"n": 0, "caught": 0, "caught_by_patterns": 0})
    misses = []
    for a in attacks:
        rep = detect(a["text"])
        pc = per_class[a["class"]]
        pc["n"] += 1
        pc["caught"] += rep.flagged
        pc["caught_by_patterns"] += bool(rep.hits)
        if not rep.flagged:
            misses.append({"class": a["class"], "text": a["text"]})
    fps, benign_out = [], {}
    for src, texts in benign.items():
        flagged = [t for t in texts if detect(t).flagged]
        benign_out[src] = {"n": len(texts), "flagged": len(flagged)}
        for t in flagged:
            rep = detect(t)
            fps.append({"source": src, "text": t, "rules": [h.rule for h in rep.hits] or ["unexplained prose (residual)"]})
    tp = sum(v["caught"] for v in per_class.values())
    n_att = sum(v["n"] for v in per_class.values())
    n_ben = sum(v["n"] for v in benign_out.values())
    n_fp = sum(v["flagged"] for v in benign_out.values())
    return {"attacks": {"n": n_att, "caught": tp, "recall": round(tp / n_att, 3), "per_class": dict(per_class), "misses": misses},
            "benign": {"n": n_ben, "flagged": n_fp, "fpr": round(n_fp / n_ben, 4), "per_source": benign_out, "false_positives": fps}}


def external_stats() -> dict:
    """deepset/prompt-injections: independent, off-domain (chatbot prompts, not shop text)."""
    out = {}
    for split in ("train", "test"):
        rows = [json.loads(l) for l in open(HERE / "external" / f"deepset_prompt_injections_{split}.jsonl", encoding="utf-8")]
        P = [r["text"] for r in rows if r["label"] == 1]
        N = [r["text"] for r in rows if r["label"] == 0]
        caught = [t for t in P if detect(t).flagged]
        fps = [t for t in N if detect(t).flagged]
        out[split] = {"caught": len(caught), "n": len(P), "recall": round(len(caught) / len(P), 3),
                      "fp": len(fps), "benign_n": len(N), "fpr": round(len(fps) / len(N), 3),
                      "sample_misses": [t[:140] for t in P if t not in caught][:6], "sample_fps": [t[:140] for t in fps][:6]}
    return out


# ------------------------------------------------------------------ 4
def typosquats(name: str) -> list[str]:
    n = name
    letters = [i for i, c in enumerate(n) if c.isalpha()]
    out = set()
    if len(letters) > 4:
        i = letters[len(letters) // 2]
        out.add(n[:i] + n[i] + n[i:])                  # doubled letter
        out.add(n[:i] + n[i + 1:])                     # dropped letter
        j = letters[len(letters) // 3]
        if j + 1 < len(n) and n[j + 1].isalpha():
            out.add(n[:j] + n[j + 1] + n[j] + n[j + 2:])  # swapped letters
    out.add(n.replace("o", "0", 1) if "o" in n else n + "s")
    out.add(n.replace("or", "our", 1) if "or" in n else n.replace("er", "re", 1) if "er" in n else n + " Shop")
    out.add(n + " Official")
    out.discard(n)
    return sorted(out)


def lookalike_stats() -> dict:
    pack = load()
    ms = list(pack.merchants.values())
    pairs = []
    for i, a in enumerate(ms):
        for b in ms[i + 1:]:
            pairs.append((round(similarity(a["merchant_name"], b["merchant_name"]), 3), a["merchant_id"], a["merchant_name"], b["merchant_id"], b["merchant_name"]))
    pairs.sort(reverse=True)
    flagged = [p for p in pairs if p[0] >= THRESHOLD]
    squat_total, squat_hit, squat_miss = 0, 0, []
    for m in ms:
        for v in typosquats(m["merchant_name"]):
            squat_total += 1
            if similarity(v, m["merchant_name"]) >= THRESHOLD:
                squat_hit += 1
            else:
                squat_miss.append({"original": m["merchant_name"], "variant": v, "score": round(similarity(v, m["merchant_name"]), 3)})
    hist = Counter(min(int(p[0] * 10) / 10, 0.9) for p in pairs)
    return {"pairs": len(pairs), "threshold": THRESHOLD, "flagged_pairs": flagged, "top_pairs": pairs[:6],
            "similarity_histogram": {f"{k:.1f}": v for k, v in sorted(hist.items())},
            "typosquats": {"n": squat_total, "caught": squat_hit, "recall": round(squat_hit / squat_total, 3), "misses": squat_miss[:12]}}


# ------------------------------------------------------------------ 5
ATTACK_SAMPLES = None


def _attack_samples() -> list[str]:
    global ATTACK_SAMPLES
    if ATTACK_SAMPLES is None:
        rows = list(csv.DictReader(open(HERE / "injection_attacks.tsv", encoding="utf-8"), delimiter="\t"))
        by = defaultdict(list)
        for r in rows:
            by[r["class"]].append(r["text"])
        # one of each class, rotating: deterministic
        ATTACK_SAMPLES = [by[c][i % len(by[c])] for i in range(4) for c in sorted(by)]
    return ATTACK_SAMPLES


def _rows(pack, sid):
    return [copy.deepcopy(r) for r in pack.scenario_attempts(sid)]


def _lines(pack, row):
    return [dict(l) for l in pack.attempt_items[row["authorization_id"]]]


def _rescale(pack, row, target_chf):
    """Set the order total to target_chf by adjusting line 1 (currency preserved)."""
    lines = row.get("_items") or _lines(pack, row)
    rate = float(pack.fx[row["currency"]])
    target_amt = round(target_chf / rate, 2)
    delta = round(target_amt - row["amount"], 2)
    lines[0]["unit_price"] = round(lines[0]["unit_price"] + delta / lines[0]["quantity"], 2)
    row["_items"] = lines
    row["amount"] = target_amt
    row["items_subtotal"] = round(row["items_subtotal"] + delta, 2)
    row["billing_amount_chf"] = pack.to_chf(target_amt, row["currency"])


def mutation_stats(compiled: dict) -> dict:
    pack = load()
    lab = labels()
    base = {}
    for sid in compiled:
        _, res = replay(sid, "cautious", compiled=compiled[sid])
        base.update({r["source_authorization_id"]: r["decision"] for r in res})
    caps = {sid: next((r["value"] for r in c["draft"]["hard_rules"] if r["field"] == "authorization.billing_amount_chf"), None)
            for sid, c in compiled.items()}
    cases = []  # (generator, sid, target_id, rows, expectation-fn, description)
    k = 0
    for sid in sorted(compiled):
        rows = _rows(pack, sid)
        for idx, row in enumerate(rows):
            aid = row["authorization_id"]
            if lab[aid]["expected"] != "approve" or base.get(aid) != "approve":
                continue
            # injection appended to merchant text
            rs = copy.deepcopy(rows)
            lines = _lines(pack, rs[idx])
            attack = _attack_samples()[k % len(_attack_samples())]
            k += 1
            lines[0]["item_details"] = (lines[0]["item_details"] + " " + attack).strip()
            rs[idx]["_items"] = lines
            cases.append(("injection in item text", sid, aid, rs, lambda d: d != "approve", attack))
            # injection in the order description
            rs = copy.deepcopy(rows)
            rs[idx]["purchase_description"] = rs[idx]["purchase_description"] + ". " + attack
            cases.append(("injection in order description", sid, aid, rs, lambda d: d != "approve", attack))
            # one cent over the per-order cap
            if caps[sid]:
                rs = copy.deepcopy(rows)
                _rescale(pack, rs[idx], caps[sid] + 0.01)
                cases.append(("1 cent over the cap", sid, aid, rs, lambda d: d == "decline", f"CHF {caps[sid] + 0.01:.2f}"))
            # same CHF value billed in EUR
            if row["currency"] == "CHF":
                rs = copy.deepcopy(rows)
                r0 = rs[idx]
                eur = round(r0["billing_amount_chf"] / 0.95, 2)
                chf_new = pack.to_chf(eur, "EUR")
                lines = _lines(pack, r0)
                for l in lines:
                    l["unit_price"] = round(l["unit_price"] / 0.95, 2)
                    l["currency"] = "EUR"
                r0.update(_items=lines, currency="EUR", amount=eur, billing_amount_chf=chf_new,
                          items_subtotal=round(r0["items_subtotal"] / 0.95, 2), delivery_fee=round(r0["delivery_fee"] / 0.95, 2))
                ok_cap = caps[sid] is None or chf_new <= caps[sid]
                cases.append(("same price billed in EUR", sid, aid, rs,
                              (lambda d: d == "approve") if ok_cap else (lambda d: d == "decline"), f"EUR {eur:.2f} = CHF {chf_new:.2f}"))
            # basket line order reversed
            if len(_lines(pack, row)) > 1:
                rs = copy.deepcopy(rows)
                lines = list(reversed(_lines(pack, rs[idx])))
                for n, l in enumerate(lines, 1):
                    l["line_no"] = n
                rs[idx]["_items"] = lines
                cases.append(("basket lines reordered", sid, aid, rs, lambda d: d == "approve", "reversed"))
            # never-seen device
            rs = copy.deepcopy(rows)
            rs[idx]["customer_device_id"] = "DVC-NEW000"
            cases.append(("new device", sid, aid, rs, lambda d: d != "approve", "DVC-NEW000"))
            # exact duplicate 10 minutes later
            rs = copy.deepcopy(rows)
            dup = copy.deepcopy(rs[idx])
            from datetime import timedelta
            from ..events import iso, parse_ts
            dup["timestamp"] = iso(parse_ts(dup["timestamp"]) + timedelta(minutes=10)).replace(".000000Z", "Z")
            dup["authorization_id"] = "AU9" + aid[2:]
            dup["_items"] = _lines(pack, row)
            dup["recent_attempt_count_10m"] = 1
            rs.insert(idx + 1, dup)
            for n, r in enumerate(rs, 1):
                r["replay_order"] = n
            cases.append(("exact duplicate 10 min later", sid, dup["authorization_id"], rs, lambda d: d != "approve", "copy"))
            # merchant display-name noise, same merchant_id
            rs = copy.deepcopy(rows)
            rs[idx]["_merchant_override"] = {"merchant_name": pack.merchants[row["merchant_id"]]["merchant_name"] + " AG"}
            cases.append(("shop name noise, same ID", sid, aid, rs, lambda d: d == "approve", "+ ' AG'"))
            # lookalike: new merchant_id with a typosquatted name of a shop the customer knows
            # (round 1 also imitated unfamiliar shops; approving those is correct, so that was a generator bug)
            if row["merchant_id"] not in build_profile(pack, row["card_id"]).merchant_names:
                continue
            rs = copy.deepcopy(rows)
            name = pack.merchants[row["merchant_id"]]["merchant_name"]
            squat = typosquats(name)[0]
            rs[idx]["merchant_id"] = "ME9" + row["merchant_id"][2:]
            rs[idx]["_merchant_override"] = {**pack.merchants[row["merchant_id"]], "merchant_name": squat}
            cases.append(("lookalike shop (new ID)", sid, aid, rs, lambda d: d != "approve", squat))
    results = defaultdict(lambda: {"n": 0, "pass": 0, "failures": []})
    for gen, sid, target, rs, expect, desc in cases:
        _, res = replay(sid, "cautious", rows=rs, compiled=compiled[sid])
        got = next((r for r in res if r["source_authorization_id"] == target), None)
        g = results[gen]
        g["n"] += 1
        if got and expect(got["decision"]):
            g["pass"] += 1
        else:
            g["failures"].append({"scenario": sid, "target": target, "mutation": desc,
                                  "got": got["decision"] if got else "missing", "why": got["headline"] if got else ""})
    total = sum(g["n"] for g in results.values())
    passed = sum(g["pass"] for g in results.values())
    return {"n": total, "pass": passed, "rate": round(passed / total, 3), "generators": dict(results)}


# ------------------------------------------------------------------ 6 + 7
def backtest_stats(compiled: dict) -> dict:
    return {sid: {k: c["backtest"][k] for k in ("in_scope", "approve", "step_up", "decline", "summary", "examples")}
            for sid, c in compiled.items()}


def compiler_stats(use_llm: bool = False, holdout: bool = False) -> dict:
    pack = load()
    comp = Compiler(pack)
    para = json.loads((HERE / ("paraphrases_holdout.json" if holdout else "paraphrases.json")).read_text())
    out, total, same = {}, 0, 0
    for sid, variants in para.items():
        if sid.startswith("_"):
            continue
        canon = comp.compile(pack.scenarios[sid]["cardholder_instruction"])
        want = comparable(canon.hard_rules)
        rows = []
        for v in variants:
            d = comp.compile(v)
            if use_llm:
                from .. import llm
                d = llm.augment(d, sorted(pack.item_categories))
            got = comparable(d.hard_rules)
            ok = got == want and d.uncertainty_policy == canon.uncertainty_policy
            total += 1
            same += ok
            rows.append({"text": v, "same": ok, "missing": sorted(map(str, want - got)), "extra": sorted(map(str, got - want)),
                         "policy": d.uncertainty_policy})
        out[sid] = rows
    return {"n": total, "same": same, "rate": round(same / total, 3), "scenarios": out}


def run_all(use_llm: bool = False) -> dict:
    t0 = time.time()
    s = Session()
    compiled = compiled_mandates(s)
    stats = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "mandates": {sid: {"rules": c["draft"]["notes"], "open_questions": c["draft"]["open_questions"],
                           "guidance": c["draft"]["guidance"], "uncertainty_policy": c["draft"]["uncertainty_policy"],
                           "instruction": c["draft"]["instruction"]} for sid, c in compiled.items()},
        "scenarios": scenario_stats(compiled),
        "branches": policy_branches(compiled),
        "injection": injection_stats(),
        "lookalike": lookalike_stats(),
        "mutations": mutation_stats(compiled),
        "backtest": backtest_stats(compiled),
        "compiler": compiler_stats(use_llm),
        "holdout": {"injection": injection_stats(holdout=True), "compiler": compiler_stats(use_llm, holdout=True)},
        "external": external_stats(),
    }
    r1 = HERE / "round1.json"
    if r1.exists():
        stats["round1"] = json.loads(r1.read_text())
    stats["runtime_s"] = round(time.time() - t0, 1)
    return stats


def summary_lines(st: dict) -> list[str]:
    sc, inj, lk, mu, co = st["scenarios"], st["injection"], st["lookalike"], st["mutations"], st["compiler"]
    L = [
        f"Scenario replay: {sc['strict_agreement']}/{sc['n']} strict, {sc['lenient_agreement']}/{sc['n']} lenient vs pre-registered labels "
        f"(decisions: {sc['decision_counts']})",
        f"  unnecessary friction on expected approvals: {len(sc['unnecessary_friction'])}; "
        f"deliberate policy friction: {len(sc['policy_friction'])}; missed stops: {len(sc['missed_stops'])}",
        f"  latency p50 {sc['latency_ms']['p50']} ms, p95 {sc['latency_ms']['p95']} ms, max {sc['latency_ms']['max']} ms (budget 8000 ms)",
        f"Injection detector: caught {inj['attacks']['caught']}/{inj['attacks']['n']} attacks "
        f"(recall {inj['attacks']['recall']:.0%}); flagged {inj['benign']['flagged']}/{inj['benign']['n']} benign texts (FPR {inj['benign']['fpr']:.1%})",
        f"Lookalike: {len(lk['flagged_pairs'])} of {lk['pairs']} merchant pairs ≥ {lk['threshold']}; typosquats caught {lk['typosquats']['caught']}/{lk['typosquats']['n']}",
        f"Mutation suite: {mu['pass']}/{mu['n']} ({mu['rate']:.0%}) as expected",
        f"Compiler paraphrases: {co['same']}/{co['n']} compile to the same rules",
        f"Holdout: injection caught {st['holdout']['injection']['attacks']['caught']}/{st['holdout']['injection']['attacks']['n']}, "
        f"benign flagged {st['holdout']['injection']['benign']['flagged']}/{st['holdout']['injection']['benign']['n']}; "
        f"paraphrases {st['holdout']['compiler']['same']}/{st['holdout']['compiler']['n']}",
        f"External (deepset test, off-domain): caught {st['external']['test']['caught']}/{st['external']['test']['n']}, "
        f"benign flagged {st['external']['test']['fp']}/{st['external']['test']['benign_n']}",
    ]
    for g, v in mu["generators"].items():
        L.append(f"  - {g}: {v['pass']}/{v['n']}")
    return L
