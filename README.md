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
uv run leash flow-web              # phone + diagram of how each purchase is evaluated, http://127.0.0.1:8002
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

## Flow demo (`leash flow-web`)

Three areas, for explaining the system:
1. **The customer's phone (Viseca one).**
   - The customer writes an instruction and gets it back as an editable form. Each rule shows the phrase it came from; AI suggestions are marked.
   - After confirming, the phone is passive: a status card and recent activity. Only a step-up sends a notification: a system banner that slides down over the screen.
   - The notification opens a page with the amount, why the wallet is asking, and the shop's text shown verbatim, plus Approve/Decline.
2. **The shopping agent (black box).** It sends the story's proposals one at a time, exactly as in the data (SCEN0004's shop texts already carry the injections).
   - Per proposal, a checkbox decides whether **the shop signs its cart (AP2)**. That is the shop's choice, not the customer's. Viseca one always signs the customer's AP2 permission at confirm, and it's used whenever a shop signs.
   - The proposals sent are listed with their answers; click one to inspect it in area 3.
3. **The Viseca server.**
   - **The rules as structure:**
     - the `Mandate` and `Rule` classes (field, operator, value, currency, scope, period) and how rules combine;
     - where a model is involved: setup only, with why each suggestion was dropped;
     - one object box per confirmed rule, grouped by origin (your words, with the phrase; model suggestion you confirmed; always on);
     - on each box, its parameters and meaning, and tags for fixed code, the data it relies on (transaction vs vendor-supplied vs Viseca's records) and any LLM involvement.
   - **For the selected proposal:**
     - every field received, tagged as transaction data, vendor-supplied text (untrusted), shop-signed (AP2) or customer;
     - Viseca-side processing: the wall, detectors, card history, the run ledger;
     - every check as **Rule → Input → Result**, each input colour-coded by source (click a check to draw the wires, including shop text → wall/detector → check);
     - the combiner, and the outgoing messages: to the agent always, to the phone only when asking.
   - **What AP2 changed:** for a signed proposal, the same event is also decided without AP2 on a copy of the state. A box shows:
     - the decision without vs with AP2;
     - the checks AP2 added (signatures, bindings, one-time use), the checks it changed (e.g. a size or return window from the signed cart, the per-order limit also in the signed permission), and the checks it made redundant;
     - the evidence kept for the shop's flag.

     Those checks get a blue outline in the diagram. An unsigned proposal gets a note on what can't be proven without it.
   - LLM steps are dashed purple. Fixed code is solid.

The data lineage lives in `leash/lineage.py`; a test keeps it in step with the field catalogue.
`scripts/flow_web_walkthrough.py` drives the demo headlessly and saves screenshots.

## AP2: the autonomous flow with shop-signed carts (simulator)

Tick "Shops sign their carts (AP2)" in either UI (or `Session(ap2=True)`). The flow is the same; AP2
([Agent Payments Protocol V2](https://github.com/google-agentic-commerce/AP2), v0.2) wraps it in signatures:

| AP2 role | Here |
|---|---|
| Trusted Surface (must be non-agentic) | Viseca one: after the customer confirms their rules, it signs the *open* Checkout and Payment Mandates, bound to the agent's key (`cnf`) |
| Merchant | signs the cart: a UCP Checkout JWT with lines, prices, total and terms (`checkout_from_row`) |
| Shopping Agent | signs the *closed* mandates, bound to the cart by `checkout_hash` = `transaction_id` |
| Credential Provider | Viseca: verifies everything below, then runs the same engine before issuing a token |

Viseca verifies:
- Viseca one signed the permission, and it hasn't expired.
- The purchase is signed by *that* agent's key.
- The cart is signed with the named shop's registered key, so a lookalike can't sign as the shop it imitates.
- What the agent submitted equals what the shop signed.
- The cart hasn't been paid before. A signed cart pays once.
- The payment is within the signed limits.
- The rules in force are the ones the customer signed (fingerprint; later additions may only tighten).
  Because of that, the signed per-order amount and allowed shops aren't checked a second time: the rule's own
  check is marked "also in the permission you signed". A replayed cart likewise isn't also flagged as a repeat.

Answers go back as signed receipts: approve → `success` plus a token; decline → `invalid_mandate` (or `invalid_credential` for a bad signature, tampering or replay); ask → `unresolved_constraint`. That is AP2's own "bring the user back". The customer then confirms on Viseca one, which signs the closed mandate (Human Present).

Privacy:
- The shop's open mandate carries only standard constraints (`checkout.allowed_merchants`).
- The customer's full rules stay with Viseca as the custom constraint `viseca.leash_rules.1`: a fingerprint, evaluated only by Viseca.
- Item rules stay there too, because AP2's `checkout.line_items` means "must contain" while ours mean "may only contain".

A shop-signed term (return window, size, returnable) counts as confirmed and can resolve an "unknown". It is ignored when:
- the cart tries to manipulate the wallet;
- the shop is flagged;
- it contradicts the shop's own text, which counts as unknown ("the shop contradicts itself").

A signature proves who wrote the text, not that it is safe, so free text stays behind the wall. Injection inside a signed cart is recorded as proof ("signed with its own key") on the shop's flag.

The demo-web AP2 presets exercise each attack: the agent lowers the price after signing, replays a signed cart, brings another shop's signature, uses an unauthorised agent key, or withholds the cart. There is also "shop signs a 30-day return window".

**Simplification:**
- Mandates are plain ES256 JWS with the V2 field names; real AP2 uses SD-JWT delegation chains with selective disclosure.
- The binding to the open mandate is an `sd_hash` claim.
- The simulated shop derives its structured terms from its own product text.
- The hosted challenge API has no AP2, so live runs are unchanged. Code: `leash/ap2.py`; tests: `tests/test_ap2.py`.

## Decision API for a frontend (`leash api`)

`uv run leash api` serves our engine as JSON on http://127.0.0.1:8003 (OpenAPI docs at `/docs`). It does not talk to
the hosted challenge API. CORS is open (`LEASH_API_CORS` to restrict); set `LEASH_API_KEY` to require a bearer key.
State is in memory. **Full reference: [`API.md`](API.md).**

| Call | What it does |
|---|---|
| `GET /v1/customers`, `GET /v1/customers/{id}`, `GET /v1/merchants` | personas, their usual shops (by ID), flags, alerts, mandates |
| `POST /v1/mandates/compile` `{customer_id, instruction}` | proposed rules in plain language + backtest (Apertus adds grounded rules); nothing stored |
| `POST /v1/mandates` `{customer_id, instruction, hard_rules, uncertainty_policy, confirmed: true}` | the customer confirms → active mandate |
| `POST /v1/mandates/{id}/rules/parse` `{text}` | new words → extra rules to add (nothing stored) |
| `POST /v1/mandates/{id}/rules` `{text, rules, confirmed: true}` | the customer approves → saved, used from the next decision |
| `PATCH /v1/mandates/{id}` `{add_rules, uncertainty_policy: "decline"}` / `DELETE` | tighten only / revoke |
| `POST /v1/decisions` `{mandate_id, merchant: {merchant_id}, items: [...], currency, delivery_fee, ...}` | **`approve` / `decline` / `step_up`**, each rule's result and the rules that caused it (`decided_by`); optional AP2 presentation |
| `GET /v1/decisions?status=pending_customer` | purchases waiting for the customer (step-ups expire after 120 s) |
| `POST /v1/decisions/{id}/resolve` `{decision: approve\|decline}` | the customer's answer to a step-up |
| `POST /v1/customers/{id}/merchant-flags` `{merchant_id, mode: ask\|block\|remove}` | keep, block or clear a flagged shop |
| `GET /v1/ap2/keys`, `POST /v1/ap2/simulate-checkout` | AP2: public keys; simulator that signs a purchase as the shop and the agent |

Totals and CHF are computed from the lines; omitted device/timestamp/attempt counts default to the card's usual
device, now, and the count from earlier decisions (listed in `assumptions`). An `authorization_id` makes a call idempotent.

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
