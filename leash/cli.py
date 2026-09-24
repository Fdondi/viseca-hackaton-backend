"""Command line: `leash demo | demo-web | eval | replay | compile | ui | live`."""
from __future__ import annotations

import argparse
import json
import sys


def cmd_compile(a):
    from .session import Session
    s = Session()
    sid = a.scenario
    text = a.instruction or s.pack.scenarios[sid]["cardholder_instruction"]
    out = s.compile(text, sid)
    if a.llm:
        from . import llm
        s.use_llm = llm.available()
        out = s.compile(text, sid)
        m = out["model"]
        if not m:
            print("model: not configured (set APERTUS_KEY or OPENAI_API_KEY)")
        else:
            print(f"model: {m['model']} ({m['provider']}), {m['last_latency_s']} s, proposed {m['proposed']}, kept {m['suggested']}"
                  + (f", error: {m['last_error']}" if m["last_error"] else ""))
            for d in m["dropped"]:
                print(f"  [dropped AI suggestion: {d['why']}] {d['text']}")
    d = out["draft"]
    print(f"Instruction: {text}\nWhen uncertain: {d['uncertainty_policy']}\n")
    for n in d["notes"]:
        print(f"  [{n['tier']}] {n['text']}")
    for q in d["open_questions"]:
        print(f"  ? {q}")
    print("\n" + out["backtest"]["summary"])
    if a.json:
        print(json.dumps(d["hard_rules"], indent=1))


def cmd_replay(a):
    from .demo import show_result
    from .session import Session
    s = Session()
    comp = s.compile(s.pack.scenarios[a.scenario]["cardholder_instruction"], a.scenario)
    m = s.confirm(comp["draft"], a.scenario)
    run = s.start(a.scenario, m["mandate_id"])
    answer = {"cautious": "decline", "trusting": "approve"}[a.customer]
    for r in s.drive(run["run_id"], customer=lambda r: answer):
        show_result(r)


def cmd_eval(a):
    from .eval.run import run_all, summary_lines
    st = run_all(use_llm=a.llm)
    print("\n".join(summary_lines(st)))
    if a.json:
        with open(a.json, "w") as fh:
            json.dump(st, fh, indent=1, default=str)


def cmd_demo(a):
    from .demo import main
    main()


def cmd_ui(a):
    import os

    import uvicorn
    os.environ["LEASH_PACE"] = str(a.pace)
    uvicorn.run("leash.ui.app:app", host=a.host, port=a.port, log_level="warning")


def cmd_demo_web(a):
    import uvicorn
    print(f"Interactive demo on http://{a.host}:{a.port}")
    uvicorn.run("leash.demo_web.app:app", host=a.host, port=a.port, log_level="warning")


def cmd_live(a):
    """Event day: talk to the hosted API (needs LEASH_BASE_URL and TEAM_API_KEY)."""
    import time

    from .platform import HttpPlatform
    from .session import Session
    from .worker import Worker
    s = Session(platform=HttpPlatform())
    print("bootstrap:", json.dumps(s.platform.bootstrap())[:400])
    comp = s.compile(s.pack.scenarios[a.scenario]["cardholder_instruction"], a.scenario)
    for n in comp["draft"]["notes"]:
        print(f"  [{n['tier']}] {n['text']}")
    print(comp["backtest"]["summary"])
    if input("Confirm these permissions? [y/N] ").strip().lower() != "y":
        return
    m = s.confirm(comp["draft"], a.scenario)
    worker = Worker(s)
    worker.start()
    run = s.start(a.scenario, m["mandate_id"])
    print("run", run.get("run_id"))
    seen = 0
    while True:
        time.sleep(0.5)
        while seen < len(s.log):
            r = s.log[seen]
            seen += 1
            print(f"{r['source_authorization_id']} {r['decision']:<8} {r['headline']}")
            if r["decision"] == "step_up":
                ans = input(f"  {r['customer_message']}\n  approve / decline? ").strip().lower()
                if ans in ("approve", "decline"):
                    print("  ", s.resolve(run["run_id"], r["authorization_id"], ans))
        st = s.platform.run_status(run["run_id"])
        if st.get("status") in ("completed", "done") or st.get("done"):
            break


def main(argv=None):
    p = argparse.ArgumentParser(prog="leash", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("compile", help="turn an instruction into rules and a backtest")
    c.add_argument("--scenario", default="SCEN0000")
    c.add_argument("--instruction")
    c.add_argument("--llm", action="store_true", help="also ask the model (Apertus by default)")
    c.add_argument("--json", action="store_true")
    c.set_defaults(fn=cmd_compile)
    r = sub.add_parser("replay", help="replay a public scenario offline")
    r.add_argument("scenario")
    r.add_argument("--customer", choices=["cautious", "trusting"], default="cautious")
    r.set_defaults(fn=cmd_replay)
    e = sub.add_parser("eval", help="compute all statistics")
    e.add_argument("--json")
    e.add_argument("--llm", action="store_true")
    e.set_defaults(fn=cmd_eval)
    d = sub.add_parser("demo", help="narrated demo + reports/demo_report.html")
    d.set_defaults(fn=cmd_demo)
    u = sub.add_parser("ui", help="customer UI on the offline simulator (or live with TEAM_API_KEY)")
    u.add_argument("--host", default="127.0.0.1")
    u.add_argument("--port", type=int, default=8000)
    u.add_argument("--pace", type=float, default=1.2, help="seconds between simulated purchases")
    u.set_defaults(fn=cmd_ui)
    dw = sub.add_parser("demo-web", help="interactive demo: pick a customer, edit and send each purchase")
    dw.add_argument("--host", default="127.0.0.1")
    dw.add_argument("--port", type=int, default=8001)
    dw.set_defaults(fn=cmd_demo_web)
    lv = sub.add_parser("live", help="event day: run against the hosted API")
    lv.add_argument("--scenario", default="SCEN0000")
    lv.set_defaults(fn=cmd_live)
    a = p.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
