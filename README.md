# Agent on a Leash

An independent wallet-control layer for AI shopping agents (Viseca challenge, Swiss {ai} Weeks 2026).
It turns a customer's instruction into confirmed rules, then answers each proposed purchase with
`approve`, `decline` or `step_up` (ask the customer), with a plain-language explanation and the evidence behind it.
Shop text is read only behind a "wall" that outputs numbers, enums or `unknown`; it can never change a rule.

Design: [`../agent-on-a-leash-plan.md`](../agent-on-a-leash-plan.md). Data: `../viseca-2026/data` (override with `LEASH_DATA`).

## Quick start

```bash
uv sync
uv run leash demo-web              # interactive demo at http://127.0.0.1:8001 (see below)
uv run leash demo                  # narrated demo + reports/demo_report.html + reports/stats.json
uv run leash ui                    # customer UI at http://127.0.0.1:8000 (offline simulator)
uv run leash eval                  # all statistics
uv run leash replay SCEN0004       # one scenario, cautious customer (--customer trusting)
uv run leash compile --scenario SCEN0002 --instruction "…"
uv run pytest
```

## Interactive demo (`leash demo-web`)

You play the shopping agent; the wallet decides.

1. **Choose a customer**: the five personas from the data pack, with their usual shops and their instruction
   (editable). The wallet compiles it into rules, shows them in plain language, and confirms the mandate.
2. **See the next purchase** from that customer's scenario as an editable form: shop (identity is the merchant ID),
   basket lines, prices, the shop's product text, currency, delivery fee, device, time, velocity, order terms,
   re-quote link. Your edits are highlighted; totals and the CHF conversion update as you type.
   One-click presets: hidden instruction in the shop text, lookalike shop, new device, burst of attempts,
   1 cent over the limit, protection-plan add-on, bill in EUR, repeat the previous order.
3. **Send it** and see the wallet's answer: the decision, every check (failed/uncertain first), what you changed,
   any shop it now flags, and the exact body posted to the platform. On "ask the customer", you answer as the customer.
4. Keep going: block or clear flagged shops, withdraw permission, and compose extra purchases after the scenario ends.

Every purchase goes through the real path (simulator → schema-valid event → engine → decision posted back).
`scripts/demo_web_walkthrough.py` drives it headlessly and saves screenshots.

Event day (hosted API): `export LEASH_BASE_URL=… TEAM_API_KEY=…`, then `uv run leash live --scenario SCEN0000`
or `uv run leash ui` (the UI switches to the live API when both variables are set).

Optional model: **Apertus** (`swiss-ai/Apertus-v1.5-70B`, Swisscom Swiss AI Weeks endpoint) when `APERTUS_KEY` is set,
otherwise OpenAI (`OPENAI_API_KEY`, model in `LEASH_OPENAI_MODEL`). Keys may live in `leash/.env`; Swisscom keys expire
after 60 minutes. Override with `LEASH_LLM_PROVIDER=apertus|openai|none`. Install with `uv sync --extra llm`.
The model only (1) proposes rules at setup, which a review keeps only if they are valid fields, grounded in the customer's
own words, consistent with what they asked to buy, and not already covered; dropped suggestions are shown with the reason;
and (2) when `LEASH_LLM_EXTRACT=1`, fills a size/return window the regexes missed, only if the value appears verbatim in the
shop text. Purchase decisions never come from a model. `uv run python -m leash.eval.compare_models` writes a case-by-case
Apertus vs Luna transcript report to `reports/model_comparison.html`. Tests always run with the model disabled.

## How a decision is made

1. **Rules** (`hard_rules`, three-valued: pass / fail / unknown). Fields are named `authorization.*` (raw event),
   `items.*` (every basket line), `derived.*` (computed: rolling spend, familiarity, …), `extracted.*` (behind the wall).
2. **Safety net** added to every mandate: manipulative shop text, lookalike shop, duplicate, split order,
   implausible price, session integrity (new device / burst / new country), re-quote of a flagged order.
   These never *fail*; they make a rule *unknown*.
3. **Customer controls**: a shop caught manipulating gets a visible "ask every time" rule the customer can keep,
   remove ("not a concern") or turn into a block (also `PATCH`ed into the mandate for future runs).
4. **Combine**: any fail → decline; any unknown → the customer's uncertainty policy (ask → step_up; manipulation
   signals are never approved away); otherwise approve.

## Layout

| Module | Role |
|---|---|
| `data.py`, `events.py` | typed data pack; schema-valid live events from the CSVs |
| `profile.py` | card profile from history (merchants by ID, devices, countries) |
| `ledger.py` | per-run ledger (idempotent, simulated time) and per-customer controls |
| `wall.py`, `injection.py`, `lookalike.py` | untrusted text → facts; injection and lookalike detectors |
| `fields.py`, `engine.py`, `explain.py` | field catalogue, combiner, deterministic explanations |
| `compiler.py`, `backtest.py`, `llm.py` | instruction → draft mandate; confirmation backtest; optional model |
| `platform.py`, `session.py`, `worker.py` | live API client + simulator; session controller; long-poll worker |
| `ui/` | FastAPI + one-page customer UI |
| `eval/` | pre-registered labels, corpora, holdouts, external set, evaluation runner |

## Evaluation honesty

`eval/expected.json` was written before any engine code; `eval/round1.json` freezes the first-run numbers.
The holdout sets were written after round 1 and before fixes. The independent set
(`eval/external/`, deepset/prompt-injections, Apache-2.0) set thresholds from its train split only.
See the "Method and caveats" section of the report.
