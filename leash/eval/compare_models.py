"""Apertus vs Luna: what each model did, and which decisions it changed.

Each case runs three times: no model (baseline), Apertus, Luna.
  * compiler cases: the model proposes rules; the review keeps or drops each one; kept rules are
    treated as accepted by the customer; the scenario is replayed (cautious customer).
  * extraction cases: the model may fill a size / return window the regexes could not read
    (decision path, LEASH_LLM_EXTRACT=1); a value counts only if it appears verbatim in the shop text.
Every model exchange is recorded verbatim (prompt, raw answer, parse, latency).

Usage: uv run python -m leash.eval.compare_models   → reports/model_comparison.{json,html}
"""
from __future__ import annotations

import copy
import html
import json
import os
import re
import sys
import time
from pathlib import Path

from .. import llm
from ..fields import describe
from ..session import Session

REPORTS = Path(__file__).resolve().parents[2] / "reports"
PROVIDERS = [("none", "No model (baseline)"), ("apertus", "Apertus"), ("openai", "Luna")]

COMPILER_CASES = [
    ("C1", "SCEN0000", None, "Public instruction"),
    ("C2", "SCEN0001", None, "Public instruction"),
    ("C3", "SCEN0002", None, "Public instruction"),
    ("C4", "SCEN0003", None, "Public instruction"),
    ("C5", "SCEN0004", None, "Public instruction"),
    ("C6", "SCEN0004", "Buy the 27-inch monitor I chose, only from Swiss sellers I have bought from before, for CHF 400 or less. "
                       "Do not add anything I did not ask for. Ask me when uncertain.",
     "Adds 'only Swiss sellers' (no country rule in the regex compiler)"),
    ("C7", "SCEN0002", "Replace my worn road-running shoes, size 43. Specialist sports retailer only, returnable for at least "
                       "two weeks, max two hundred francs. Ask me when uncertain.",
     "Return window in weeks, amount in words"),
    ("C8", "SCEN0001", "Keep our fridge stocked: food only, delivered, max 120 francs per delivery and 300 francs per week at "
                       "most. Ask me when unsure.",
     "'food only' instead of 'groceries'"),
    ("C9", "SCEN0003", "Clothes for me from shops I already know, under 250 francs each time, and only shops in Switzerland or "
                       "Italy. Pause if someone else seems to be using my account.",
     "Two allowed countries"),
    ("C10", "SCEN0000", "Grab a single grocery item under twenty francs from my usual supermarket.",
     "'under twenty' (strict), 'usual supermarket'"),
]
EXTRACTION_CASES = [
    ("E1", "AU0012", "Road-running shoe in 43; can be sent back within 30 days", "No 'size' keyword, unusual return phrasing"),
    ("E2", "AU0019", "Road-running shoe, EU 43, two-week return window", "Return window in words"),
    ("E3", "AU0013", "Road-running shoe in 42 (EU); returns within 30 days", "Wrong size, no 'size' keyword"),
    ("E4", "AU0015", "Road-running shoe, size 43, one week to return it", "Short return window in words"),
    ("E5", "AU0016", None, "Seller does not state a return policy (original text)"),
]


def _use(provider: str) -> None:
    os.environ["LEASH_LLM_PROVIDER"] = provider


def _replay(s: Session, sid: str, draft: dict, rows=None) -> dict:
    m = s.confirm(draft, sid)
    rows = rows if rows is not None else [copy.deepcopy(r) for r in s.pack.scenario_attempts(sid)]
    run = s.platform.inject_run(sid, m["mandate_id"], rows)
    res = s.drive(run["run_id"], customer=lambda r: "decline")
    return {r["source_authorization_id"]: {"decision": r["decision"], "headline": r["headline"],
                                           "reason_codes": r["reason_codes"], "facts": r["facts"]} for r in res}


def run_compiler_case(case) -> dict:
    cid, sid, text, what = case
    out = {"id": cid, "kind": "compiler", "scenario": sid, "what": what, "runs": {}}
    for prov, label in PROVIDERS:
        _use(prov)
        s = Session(use_llm=prov != "none")
        pack = s.pack
        instruction = text or pack.scenarios[sid]["cardholder_instruction"]
        out["instruction"] = instruction
        t_before = len(llm.TRANSCRIPT)
        comp = s.compile(instruction, sid)
        draft = comp["draft"]
        rev = llm.STATUS.get("last_review") if prov != "none" else None
        out["runs"][prov] = {
            "label": label, "model": (comp.get("model") or {}).get("model"),
            "rules": [n["text"] for n in draft["notes"] if n["tier"] != "safety net"],
            "kept": [describe(r) for r in (rev or {}).get("kept", [])],
            "dropped": [{"text": describe(d["rule"]), "why": d["why"], "rule": d["rule"]} for d in (rev or {}).get("dropped", [])],
            "transcript": llm.TRANSCRIPT[t_before:],
            "decisions": _replay(s, sid, draft),
        }
        if prov != "none":
            llm.STATUS["last_review"] = None
    return out


def run_extraction_case(case) -> dict:
    cid, target, text, what = case
    sid = "SCEN0002"
    out = {"id": cid, "kind": "extraction", "scenario": sid, "target": target, "what": what, "runs": {}}
    for prov, label in PROVIDERS:
        _use(prov)
        os.environ["LEASH_LLM_EXTRACT"] = "1" if prov != "none" else "0"
        s = Session(use_llm=prov != "none")
        s.use_llm = False                      # the mandate is the deterministic one; only extraction uses the model
        comp = s.compile(s.pack.scenarios[sid]["cardholder_instruction"], sid)
        rows = [copy.deepcopy(r) for r in s.pack.scenario_attempts(sid)]
        for r in rows:
            if r["authorization_id"] == target:
                lines = [dict(l) for l in s.pack.attempt_items[target]]
                if text:
                    lines[0]["item_details"] = text
                r["_items"] = lines
                out["shop_text"] = lines[0]["item_details"]
        t_before = len(llm.TRANSCRIPT)
        dec = _replay(s, sid, comp["draft"], rows)
        tr = [t for t in llm.TRANSCRIPT[t_before:]]
        out["runs"][prov] = {"label": label, "decisions": {target: dec[target]}, "transcript": tr,
                             "facts": dec[target]["facts"][0]}
    os.environ["LEASH_LLM_EXTRACT"] = "0"
    return out


def changes(case: dict, prov: str) -> list[dict]:
    base = case["runs"]["none"]["decisions"]
    got = case["runs"][prov]["decisions"]
    return [{"id": k, "before": base[k]["decision"], "after": v["decision"], "headline": v["headline"]}
            for k, v in got.items() if k in base and base[k]["decision"] != v["decision"]]


def run_all() -> list[dict]:
    os.environ.setdefault("LEASH_LLM_EXTRACT_TIMEOUT", "4")
    cases = [run_compiler_case(c) for c in COMPILER_CASES] + [run_extraction_case(c) for c in EXTRACTION_CASES]
    _use("auto")
    for c in cases:
        c["changes"] = {p: changes(c, p) for p in ("apertus", "openai")}
        c["changed"] = bool(c["changes"]["apertus"] or c["changes"]["openai"])
    cases.sort(key=lambda c: (not c["changed"], c["kind"] != "compiler", c["id"]))
    return cases


# ------------------------------------------------------------------ report
E = html.escape
CSS = """
:root{--paper:#F2F4F3;--surface:#FFFFFF;--ink:#15201E;--muted:#5A6966;--rule:#D3DBD9;--accent:#0E5A66;--accent-soft:#E1ECEC;
--ok:#2E7A4C;--ok-soft:#E3F0E7;--ask:#A86C12;--ask-soft:#F6ECDA;--no:#B0362C;--no-soft:#F6E2DF;--ap:#8A3B12;--ap-soft:#F7E7DC;--lu:#3B4F9A;--lu-soft:#E4E8F6;--code:#EEF2F1;
--display:"Bricolage Grotesque",ui-sans-serif,system-ui,sans-serif;--body:"Instrument Sans",ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif;--mono:"JetBrains Mono",ui-monospace,Menlo,Consolas,monospace}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--paper:#0F1615;--surface:#16201F;--ink:#E2EAE8;--muted:#95A6A2;--rule:#2A3735;--accent:#63B6C2;--accent-soft:#1B2F31;
--ok:#6CC08C;--ok-soft:#18291F;--ask:#E0A94F;--ask-soft:#2E2515;--no:#EA7B70;--no-soft:#321B18;--ap:#F0A77E;--ap-soft:#33211A;--lu:#9DB0F2;--lu-soft:#1D2338;--code:#1C2826;color-scheme:dark}}
:root[data-theme="dark"]{--paper:#0F1615;--surface:#16201F;--ink:#E2EAE8;--muted:#95A6A2;--rule:#2A3735;--accent:#63B6C2;--accent-soft:#1B2F31;
--ok:#6CC08C;--ok-soft:#18291F;--ask:#E0A94F;--ask-soft:#2E2515;--no:#EA7B70;--no-soft:#321B18;--ap:#F0A77E;--ap-soft:#33211A;--lu:#9DB0F2;--lu-soft:#1D2338;--code:#1C2826;color-scheme:dark}
*{box-sizing:border-box}
body{background:var(--paper);color:var(--ink);font:14.5px/1.55 var(--body);margin:0;padding-inline:16px;padding-block:28px 64px}
main{max-width:1180px;margin:0 auto;display:flex;flex-direction:column;gap:40px}
h1,h2,h3{font-family:var(--display);margin:0;line-height:1.15;text-wrap:balance}
h1{font-size:clamp(26px,4.5vw,40px);font-weight:700}h2{font-size:22px;font-weight:650}h3{font-size:15px;font-weight:650}
p{margin:0;max-width:75ch}
.eyebrow{font:600 11.5px/1 var(--mono);letter-spacing:.08em;text-transform:uppercase;color:var(--accent)}
.note{color:var(--muted);font-size:13.5px}
.mono,code{font-family:var(--mono);font-size:.86em}
code{background:var(--code);padding:1px 5px;border-radius:4px}
.pill{display:inline-flex;font:600 10.5px/1 var(--mono);letter-spacing:.05em;text-transform:uppercase;padding:4px 7px;border-radius:999px;white-space:nowrap}
.pill.approve{color:var(--ok);background:var(--ok-soft)}.pill.step_up{color:var(--ask);background:var(--ask-soft)}.pill.decline{color:var(--no);background:var(--no-soft)}
.pill.ap{color:var(--ap);background:var(--ap-soft)}.pill.lu{color:var(--lu);background:var(--lu-soft)}
.table-wrap{overflow-x:auto;border:1px solid var(--rule);border-radius:10px;background:var(--surface)}
table{border-collapse:collapse;width:100%;font-size:13px}
th,td{text-align:left;padding:7px 10px;border-bottom:1px solid var(--rule);vertical-align:top}
th{font:600 10.5px/1.3 var(--mono);text-transform:uppercase;letter-spacing:.06em;color:var(--muted);background:var(--paper)}
tr:last-child td{border-bottom:0}
.case{background:var(--surface);border:1px solid var(--rule);border-radius:12px;display:flex;flex-direction:column}
.case-head{padding:14px 16px;border-bottom:1px dashed var(--rule);display:flex;flex-direction:column;gap:6px}
.case-body{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:0}
@media (max-width:820px){.case-body{grid-template-columns:1fr}}
.side{padding:14px 16px;display:flex;flex-direction:column;gap:10px;min-width:0}
.side+.side{border-left:1px solid var(--rule)}
@media (max-width:820px){.side+.side{border-left:0;border-top:1px solid var(--rule)}}
.side.ap h3{color:var(--ap)}.side.lu h3{color:var(--lu)}
.quote{border-left:3px solid var(--accent);background:var(--accent-soft);padding:8px 12px;border-radius:0 6px 6px 0;font-size:14px}
.shoptext{border-left:3px solid var(--ask);background:var(--ask-soft);padding:8px 12px;border-radius:0 6px 6px 0;font:12.5px/1.5 var(--mono)}
pre{background:var(--code);border-radius:8px;padding:10px;margin:0;font:12px/1.5 var(--mono);white-space:pre-wrap;overflow-wrap:anywhere;max-height:320px;overflow:auto}
ul.v{list-style:none;margin:0;padding:0;display:flex;flex-direction:column;gap:4px;font-size:13px}
ul.v li{display:grid;grid-template-columns:auto 1fr;gap:8px;align-items:baseline}
.k{font:600 10px/1.6 var(--mono);text-transform:uppercase;letter-spacing:.05em;padding:1px 6px;border-radius:4px}
.k.kept{color:var(--ok);background:var(--ok-soft)}.k.drop{color:var(--muted);background:var(--code)}
.chg{border:1px solid var(--rule);border-radius:8px;padding:8px 10px;font-size:13px;display:flex;flex-direction:column;gap:4px}
details summary{cursor:pointer;color:var(--accent);font-weight:600;font-size:13px}
.base{font-size:13px}
"""


def _pill(d):
    return f'<span class="pill {d}">{ {"approve": "approve", "step_up": "ask", "decline": "decline"}[d]}</span>'


def _side(case: dict, prov: str) -> str:
    run = case["runs"][prov]
    cls = "ap" if prov == "apertus" else "lu"
    tr = run["transcript"]
    lat = ", ".join(f"{t['latency_s']} s" for t in tr) or "no call"
    parts = [f'<h3>{E(run["label"])} <span class="mono" style="color:var(--muted);font-weight:400">'
             f'{E(tr[0]["model"] if tr else (run.get("model") or ""))} · {E(lat)}</span></h3>']
    ch = case["changes"][prov]
    if ch:
        parts.append("".join(f'<div class="chg"><div><b class="mono">{E(c["id"])}</b> {_pill(c["before"])} → {_pill(c["after"])}</div>'
                             f'<div class="note">{E(c["headline"])}</div></div>' for c in ch))
    else:
        parts.append('<p class="note">No decision changed.</p>')
    if case["kind"] == "compiler":
        items = "".join(f'<li><span class="k kept">kept</span><span>{E(t)}</span></li>' for t in run["kept"])
        items += "".join(f'<li><span class="k drop">dropped</span><span>{E(describe(d["rule"]))} <span class="note">— {E(d["why"])}</span></span></li>'
                         for d in run["dropped"])
        parts.append(f'<div><div class="eyebrow" style="margin-bottom:6px">What it proposed, after our review</div>'
                     f'<ul class="v">{items or "<li><span></span><span class=note>No rules proposed.</span></li>"}</ul></div>')
    else:
        f = run["facts"]
        parts.append(f'<p class="base">Facts used: size <b class="mono">{E(str(f["size"]))}</b>, return window '
                     f'<b class="mono">{E(str(f["return_days"]))}</b> days.</p>')
    for t in tr:
        err = f'<p class="note">Error: {E(t["error"])}</p>' if t.get("error") and not t.get("raw") else ""
        if t["task"] == "facts":
            text = t["prompt"].split("Text: ", 1)[-1].strip().strip('"')
            p = t.get("parsed") or {}
            verdict = []
            if p:
                for k, label in (("size", "size"), ("return_days", "return window")):
                    v = p.get(k)
                    if v in (None, "unknown"):
                        continue
                    ok = re.search(rf"\b{re.escape(str(v))}\b", text, re.I) is not None
                    verdict.append(f"{label} {v}: {'accepted (appears in the text)' if ok else 'rejected (not written in the text)'}")
            target = case.get("shop_text") == text
            parts.append(f'<div class="chg"><div class="note">{"Target purchase" if target else "Other purchase in the run"} · '
                         f'<span class="mono">{E(text)}</span></div><div class="mono">{E((t["raw"] or t.get("error") or "")[:160])}</div>'
                         f'<div class="note">{E("; ".join(verdict) or "nothing to use")}</div></div>')
            continue
        parts.append(f'<details><summary>Transcript: {E(t["task"])} ({E(t.get("format") or "failed")})</summary>'
                     f'<div class="eyebrow" style="margin:8px 0 4px">Prompt</div><pre>{E(t["prompt"])}</pre>'
                     f'<div class="eyebrow" style="margin:8px 0 4px">Raw answer, verbatim</div><pre>{E(t["raw"] or "(none)")}</pre>{err}</details>')
    if not tr:
        parts.append('<p class="note">The model was not called (nothing for it to fill in).</p>')
    return f'<div class="side {cls}">{"".join(parts)}</div>'


def build_html(cases: list[dict]) -> str:
    n_ch = {p: sum(1 for c in cases if c["changes"][p]) for p in ("apertus", "openai")}
    dec_ch = {p: sum(len(c["changes"][p]) for c in cases) for p in ("apertus", "openai")}
    lat = {p: [t["latency_s"] for c in cases for t in c["runs"][p]["transcript"] if t.get("latency_s")] for p in ("apertus", "openai")}
    errs = {p: sum(1 for c in cases for t in c["runs"][p]["transcript"] if t.get("error") and not t.get("raw")) for p in ("apertus", "openai")}
    kept = {p: sum(len(c["runs"][p].get("kept", [])) for c in cases) for p in ("apertus", "openai")}
    dropped = {p: sum(len(c["runs"][p].get("dropped", [])) for c in cases) for p in ("apertus", "openai")}

    def med(v):
        v = sorted(v)
        return f"{v[len(v) // 2]:.1f} s" if v else "–"

    rows = "".join(
        f"<tr><td class='mono'><a href='#{c['id']}'>{c['id']}</a></td><td>{E(c['what'])}</td><td class='mono'>{c['scenario']}</td>"
        f"<td>{len(c['changes']['apertus']) or '–'}</td><td>{len(c['changes']['openai']) or '–'}</td></tr>" for c in cases)
    blocks = []
    for c in cases:
        base = c["runs"]["none"]["decisions"]
        if c["kind"] == "compiler":
            intro = (f'<div class="quote">{E(c["instruction"])}</div>'
                     f'<p class="note">Rules without a model: {E(" · ".join(c["runs"]["none"]["rules"]))}</p>')
            counts = {d: sum(1 for v in base.values() if v["decision"] == d) for d in ("approve", "step_up", "decline")}
            intro += f'<p class="note">Baseline over {len(base)} purchases: {counts["approve"]} approve · {counts["step_up"]} ask · {counts["decline"]} decline.</p>'
        else:
            b = base[c["target"]]
            intro = (f'<div class="shoptext">{E(c.get("shop_text", ""))}</div>'
                     f'<p class="note">Purchase {c["target"]}, customer wants road-running shoes size 43 with ≥ 14-day returns. '
                     f'Without a model: {_pill(b["decision"])} {E(b["headline"])}</p>')
        blocks.append(f'<article class="case" id="{c["id"]}"><div class="case-head"><div class="eyebrow">{c["id"]} · '
                      f'{"rule compiler" if c["kind"] == "compiler" else "fact extraction (decision path)"} · {c["scenario"]}'
                      f'{" · decision changed" if c["changed"] else ""}</div><h2>{E(c["what"])}</h2>{intro}</div>'
                      f'<div class="case-body">{_side(c, "apertus")}{_side(c, "openai")}</div></article>')
    return f"""<title>Apertus vs Luna</title>
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:opsz,wght@12..96,650;12..96,700&family=Instrument+Sans:wght@400;600&family=JetBrains+Mono:wght@400;600&display=swap">
<style>{CSS}</style>
<main>
<header style="display:flex;flex-direction:column;gap:14px">
  <div class="eyebrow">Agent on a Leash · model comparison · {time.strftime('%Y-%m-%d %H:%M')}</div>
  <h1>Apertus vs Luna, case by case</h1>
  <p>Same {len(cases)} cases, same prompts, same review. <span class="pill ap">Apertus</span> is <code>swiss-ai/Apertus-v1.5-70B</code> on the Swisscom
  Swiss AI Weeks endpoint (temperature 0); <span class="pill lu">Luna</span> is <code>gpt-6-luna</code> (reasoning model, default sampling).
  Each run is a single sample, so answers can vary between runs. Cases where a decision changed come first.</p>
  <p class="note">How the models can affect a decision: (1) at setup, proposed rules that pass our review are treated here as accepted by the
  customer, then the whole scenario is replayed with a customer who declines every question; (2) in the decision path, a model may fill a size or
  return window the regexes could not read, and the value counts only if it appears word for word in the shop text. No model decides
  anything itself; the rules and the combiner do.</p>
  <h2>What we found in this run</h2>{FINDINGS}
  <div class="table-wrap"><table>
    <tr><th></th><th>Apertus</th><th>Luna</th></tr>
    <tr><td>Cases with a changed decision</td><td>{n_ch['apertus']}</td><td>{n_ch['openai']}</td></tr>
    <tr><td>Decisions changed in total</td><td>{dec_ch['apertus']}</td><td>{dec_ch['openai']}</td></tr>
    <tr><td>Proposed rules kept / dropped by review</td><td>{kept['apertus']} / {dropped['apertus']}</td><td>{kept['openai']} / {dropped['openai']}</td></tr>
    <tr><td>Median call latency</td><td>{med(lat['apertus'])}</td><td>{med(lat['openai'])}</td></tr>
    <tr><td>Failed calls</td><td>{errs['apertus']}</td><td>{errs['openai']}</td></tr>
  </table></div>
  <div class="table-wrap"><table><tr><th>Case</th><th>What it tests</th><th>Scenario</th><th>Decisions changed · Apertus</th><th>· Luna</th></tr>{rows}</table></div>
</header>
{''.join(blocks)}
</main>"""


FINDINGS = """
<ul>
<li><b>The two models changed exactly the same decisions</b> (4 decisions in 4 of 15 cases), each time towards what the customer asked:
C6 declines the US seller once “only Swiss sellers” becomes a country rule; C7 declines the 7-day-returns shoe once “two weeks” becomes
a 14-day return rule; E1 approves a compliant shoe whose size and return window only the models could read; E3 declines a size-42 shoe
the regexes could not read.</li>
<li><b>Where they differ is in what they propose.</b> Apertus proposed more rules per case, including empty ones (“The fulfilment is one of .”)
that the review drops. Both proposed a “27-inch” shoe size for the monitor, a hallucination the grounding check drops.</li>
<li><b>Grounding cost one correct answer (E2).</b> Both read “two-week return window” as 14 days; the rule “a value must appear verbatim in the
shop text” rejected it, so the purchase still asks the customer. Safe, but over-strict for number words.</li>
<li><b>The review cost one correct rule (C8).</b> Both proposed “groceries” for “food only”; the review dropped it because “food” is not one of
its grounding words for groceries. The perfume in that basket therefore still goes through.</li>
<li><b>Operational:</b> Apertus answered HTTP 429 <code>EXPIRED_QUOTA</code> on 2 calls in this run; those facts simply stayed unknown.
Latency per call was 0.3–13 s for Apertus and 1.2–6.5 s for Luna, so neither fits comfortably inside the 8-second decision
deadline if called per basket line; that is why extraction is off by default.</li>
</ul>"""


def main() -> None:
    t0 = time.time()
    if "--from-json" in sys.argv:
        cases = json.loads((REPORTS / "model_comparison.json").read_text())
        (REPORTS / "model_comparison.html").write_text(build_html(cases), encoding="utf-8")
        print("rebuilt", REPORTS / "model_comparison.html")
        return
    cases = run_all()
    REPORTS.mkdir(exist_ok=True)
    (REPORTS / "model_comparison.json").write_text(json.dumps(cases, indent=1, default=str))
    (REPORTS / "model_comparison.html").write_text(build_html(cases), encoding="utf-8")
    for c in cases:
        print(c["id"], c["what"][:50].ljust(50), "Apertus:", [f"{x['id']} {x['before']}→{x['after']}" for x in c["changes"]["apertus"]],
              "Luna:", [f"{x['id']} {x['before']}→{x['after']}" for x in c["changes"]["openai"]])
    print(f"done in {time.time() - t0:.0f} s → {REPORTS / 'model_comparison.html'}")


if __name__ == "__main__":
    main()
