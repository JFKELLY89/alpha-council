# Alpha Council — Dashboard Build Brief

**For:** OpenAI Codex / ChatGPT
**Date:** Sunday, August 30, 2026
**Task:** build `dashboard/app.py` — a Streamlit dashboard over an existing, complete, tested trading system
**Repo:** https://github.com/JFKELLY89/alpha-council

---

## 0. Read this first

The trading system is **built, tested, and committed**. Your job is a
read-only presentation layer on top of it. You are not modifying trading
logic, and you should not need to.

Three hard rules:

1. **The dashboard NEVER writes to the database.** Read-only SQL only. No
   `INSERT`, `UPDATE`, `DELETE`, or `ALTER`, ever.
2. **The dashboard NEVER calls an API or an LLM.** No Alpaca calls, no
   OpenAI, no Anthropic. Everything renders from SQLite. This keeps the
   dashboard safe to open during a live session and keeps it off the AI
   budget.
3. **Every table is currently EMPTY except `market_bars`,
   `config_versions`, and `source_registry`.** The first live trading
   session is Monday, August 31. Every panel must render cleanly with zero
   rows and say so plainly. A dashboard that crashes on an empty table is
   useless on the one morning it matters.

Streamlit + Plotly only. No Next.js, no React, no separate frontend. This
is explicitly forbidden before submission.

---

## 1. What the system does

Alpha Council is an autonomous options trading desk for the Alpaca AI
Trading Agents Hackathon (Aug 28 – Sep 4, 2026). It trades two-leg
defined-risk debit verticals (bull call, bear put) on a $100,000 Alpaca
paper account.

> AI decides what it wants to do. Deterministic software decides what it is
> allowed to do.

**Pipeline:** ~250 discovered symbols → 30 fast-screen → 12 pre-score → 5
options-qualified → up to 3 councils → Bull/Bear/Catalyst analysts (GPT) →
Portfolio Manager (GPT) → deterministic options engine returns 5 real
spreads → PM selects one → Claude Red Team (PASS/MODIFY/VETO) → one PM
revision → Risk Constitution (APPROVE/RESIZE/REJECT/HALT) → Alpaca
multi-leg paper order.

**The differentiator is measurement, not the agents.** Other teams are
building bull/bear/risk debates. Nobody else can say: *"our risk engine
added $310 and our red team cost us $240, and here is the arithmetic."*

Two ledgers make that possible:

- **Counterfactual attribution** — every decision keeps three shadow
  variants (GPT original, Claude modified, Executed), marked side by side,
  with P&L differences decomposed into **selection effect** (did the layer
  pick a worse trade?) and **sizing effect** (did it just pick a smaller
  one?).
- **Gate attribution** — every rejected candidate is also shadow-marked,
  so each individual gate's cost or saving is measurable.

**The dashboard exists to make those two ledgers visible.** That is its
primary purpose. Everything else is context.

---

## 2. What a judge must be able to answer without narration

1. **Why did Alpha Council notice this symbol?**
2. **Why did it take or reject the trade?**
3. **Did the Red Team, the risk engine, and the execution layer add or
   destroy value?**

If a tab does not serve one of those three questions, it is decoration.

---

## 3. Environment

| Item | Value |
|---|---|
| Python | 3.11, managed by `uv` |
| Database | SQLite at `./data/alpha_council.db` (path from `settings.database_path`) |
| Schema | v2.5.0, 42 tables, 6 views |
| Charts | Plotly (already a dependency) |
| Timezone | **All timestamps stored UTC ISO-8601 TEXT. Convert to America/New_York for display.** |
| Tests | 440 passing; do not break them |

Run with:

```bash
uv run streamlit run dashboard/app.py
```

Useful existing helpers you may import:

```python
from alpha_council.settings import get_settings, load_yaml
from alpha_council.utils.time import to_et, parse_alpaca_ts, ET
```

For read-only access, plain `sqlite3` with `row_factory = sqlite3.Row` is
simpler than the async engine and entirely sufficient. Open the database in
read-only mode:

```python
con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
```

Cache queries with `@st.cache_data(ttl=30)` so a mid-session refresh does
not hammer the file.

---

## 4. Database reference

### 4.1 Views — use these first

Six views already compute the hard aggregations. Prefer them over hand-written SQL.

| View | Columns | Use in |
|---|---|---|
| `v_discovery_funnel` | scan_id, as_of, discovery_count, stage0_survivors, prescore_survivors, options_prescreened, final_candidates, councils_started, event_track_count, momentum_track_count, survival_rate | Discovery Funnel |
| `v_gate_histogram` | stage, gate_id, tier, hard_gate, rejections, distinct_symbols, last_seen | Gate Lab |
| `v_gate_value` | gate_id, stage, shadow_n, avg_blocked_pnl_per_spread, gate_value | Gate Lab |
| `v_fill_bias` | side, direction, n_fills, mean_bias, mean_slippage_pct, mean_seconds_to_fill, mean_walk_steps | Execution Quality |
| `v_discovery_source_yield` | source, symbols_discovered, reached_candidate, reached_council | Discovery Funnel |
| `v_attribution_totals` | decisions, claude_selection, claude_sizing, claude_total, risk_selection, risk_sizing, risk_total, governance_total | Counterfactual Lab |

### 4.2 Core tables

**`decisions`** — one row per council session
`decision_id, candidate_id, config_version, strategy_id, symbol, state,
discovery_source, candidate_track, created_at, updated_at`

`state` walks: `CANDIDATE → COUNCIL_STARTED → PM_PROPOSED →
STRUCTURES_GENERATED → STRUCTURE_SELECTED → RED_TEAMED → REVISED →
RISK_APPROVED | RISK_REJECTED → ORDER_SUBMITTED → ORDER_WORKING → FILLED |
CANCELED | REJECTED | NO_FILL → POSITION_OPEN → POSITION_CLOSED →
ATTRIBUTED`

**`candidate_scores`** — scanner output
`candidate_id, scan_id, config_version, symbol, direction, as_of,
momentum_score, relative_volume_score, trend_regime_score,
relative_strength_score, options_opportunity_score, options_liquidity_score,
catalyst_score, corroboration_score, novelty_score,
data_confidence_factor, regime_factor, event_risk_factor, fast_score,
pre_score, raw_opportunity_score, final_opportunity_score,
discovery_source, candidate_track, key_metrics_json, created_at`

**`discovery_candidates`** — why a symbol entered the pool
`discovery_id, scan_id, symbol, discovered_at, expires_at, source,
source_rank, discovery_reason, is_core, asset_tradable, has_options,
data_density_ok, fast_score, discovery_boost`

`source` ∈ `CORE | ALPACA_NEWS | SEC_EVENT | MOST_ACTIVE | MOVER |
OTHER_DYNAMIC`. **`discovery_reason` is a human-readable string** — it is
the literal answer to "why did we notice this symbol?" Show it verbatim.

**`option_structures`** — the five real spreads per decision
`structure_id, decision_id, candidate_id, rank, symbol, strategy,
expiration, dte, long_symbol, long_strike, long_delta, long_bid, long_ask,
long_raw_mid, long_adjusted_mid, short_symbol, short_strike, short_delta,
short_bid, short_ask, short_raw_mid, short_adjusted_mid, net_delta, width,
raw_mid_debit, adjusted_mid_debit, natural_debit, staleness_buffer,
indicative_buffer, initial_limit_debit, cost_to_width_ratio,
max_loss_per_spread, max_profit_per_spread, reward_risk_ratio, breakeven,
max_quote_lag_seconds, underlying_price, underlying_move, stale_adjusted,
liquidity_score, delta_fit_score, dte_fit_score, cost_efficiency_score,
structure_score, raw_json, created_at`

**`trade_proposals`** — PM output, revision 0 and 1
`proposal_id, decision_id, revision, symbol, trade, direction, confidence,
expected_horizon_days, desired_portfolio_risk_pct, thesis,
catalyst_summary, key_supporting_evidence_json, key_contrary_evidence_json,
invalidation_json, selected_structure_rank, abstain_reason, created_at`

**`red_team_reviews`** — Claude's verdict
`review_id, decision_id, proposal_id, verdict, risk_score, fatal_flaw,
confidence_adjustment, recommended_max_risk_pct, problems_json,
strongest_counterargument, information_to_reverse_json, summary, created_at`

`verdict` ∈ `PASS | MODIFY | VETO`. `problems_json` is a list of
`{category, severity, description, evidence}` where category ∈ DATA,
SOURCE, NOVELTY, THESIS, CATALYST, VOLATILITY, STRUCTURE, LIQUIDITY,
CONCENTRATION, CORRELATION, INVALIDATION, TIMING, STALENESS, **EXPRESSION**,
**BREAKEVEN**, OTHER.

**`risk_evaluations`** — the deterministic gate
`risk_evaluation_id, decision_id, proposal_id, structure_id,
config_version, evaluated_at, decision, account_equity, requested_qty,
approved_qty, requested_max_loss, approved_max_loss,
total_open_risk_pct_after, sector_risk_pct_after, daily_drawdown_pct,
competition_drawdown_pct, violations_json`

`decision` ∈ `APPROVE | RESIZE | REJECT | HALT`. `violations_json` is a
list of `{rule_id, severity, message, observed_value, allowed_value}`.

**`agent_runs`** — every LLM call, with the full prompt
`run_id, decision_id, agent_name, provider, model, purpose, started_at,
completed_at, input_hash, prompt_text, output_json, input_tokens,
output_tokens, cost_usd, status, error`

**`trade_journal`** — realized outcomes
`trade_id, decision_id, opened_at, closed_at, status, qty, entry_debit,
exit_credit, realized_pnl, realized_return_pct, candidate_track, thesis,
invalidation_json, exit_reason, lesson`

**`decision_attribution`** — the four-way decomposition
`attribution_id, decision_id, as_of, gpt_original_pnl,
claude_modified_pnl, executed_pnl, gpt_original_pnl_per_spread,
claude_modified_pnl_per_spread, executed_pnl_per_spread, gpt_original_qty,
claude_modified_qty, executed_qty, claude_selection_effect,
claude_sizing_effect, risk_selection_effect, risk_sizing_effect,
claude_value_added, risk_constitution_value_added, mark_method, notes_json`

**The identity that must hold:** `selection_effect + sizing_effect =
total_effect`. Display it so a judge can verify the arithmetic.

**`gate_rejections`** — every non-decision
`rejection_id, occurred_at, config_version, scan_id, decision_id, symbol,
direction, stage, gate_id, observed_value, threshold_value, tier,
hard_gate, shadow_eligible, shadow_structure_json, note`

**`rejected_shadows`** — what the blocked trades would have done
`rejected_shadow_id, rejection_id, symbol, structure_json,
entry_timestamp, entry_reference_debit, horizon_end, status,
last_mark_debit, last_marked_at, final_pnl_per_spread, mark_method`

**`execution_calibrations`** — indicated-to-fill bias
`calibration_id, decision_id, symbol, side, candidate_track, direction,
submitted_at, filled_at, indicative_raw_mid, indicative_adjusted_mid,
natural_debit_estimate, initial_limit_debit, final_submitted_limit,
actual_fill_debit, seconds_to_fill, limit_walk_steps, quote_lag_seconds,
underlying_at_quote, underlying_at_submit, underlying_at_fill,
fill_bias_vs_adjusted, fill_bias_vs_limit, fill_slippage_pct`

**`config_versions`** — the tier ladder audit trail
`config_version, activated_at, deactivated_at, tier, scoring_json,
risk_json, note`

Also available: `funnel_snapshots`, `discovery_source_status`,
`shadow_trades`, `shadow_marks`, `orders`, `fills`, `position_snapshots`,
`api_usage`, `system_events`, `scan_runs`, `intelligence_events`,
`intelligence_items`, `market_bars`, `data_quality`, `fill_bias_estimates`.

Empty until Alpha Evolution Phase 1: `scenario_sets`, `scenario_payoffs`,
`premarket_briefs`, `strategy_lessons`, `strategy_versions`,
`challenger_proposals`, `strategy_shadow_decisions`,
`strategy_performance_snapshots`, `promotion_recommendations`.

---

## 5. The eight tabs

### 5.1 Command Center

Account equity, competition P&L, day P&L, peak equity and drawdown, open
defined-risk exposure, sector exposure, active positions, current tier,
provider spend, Risk Constitution state (GREEN / BLOCKED / HALTED).

Sources: `trade_journal`, `position_snapshots`, `risk_evaluations` (latest
`account_equity`), `api_usage`, `config_versions` (latest active `tier`).

Drawdown definitions, computed in SQL, not guessed:
```
daily_drawdown_pct       = (day_start_equity - current) / day_start_equity * 100
competition_drawdown_pct = (peak_equity - current) / peak_equity * 100
```

Show a HALT banner prominently if the latest `risk_evaluations.decision` is
`HALT`.

### 5.2 Discovery Funnel

**This tab answers "why did we notice this symbol?"**

- Funnel chart: discovery → stage0 → prescore → options → final → councils,
  from `v_discovery_funnel`
- Source breakdown from `v_discovery_source_yield`: which sources actually
  produced trades, not just symbols
- EVENT vs MOMENTUM track split
- Table of `discovery_candidates` with `discovery_reason` shown verbatim
- Screener availability from `discovery_source_status` — a 403 on
  most-actives is expected and should read as "unavailable", not "error"

### 5.3 Scanner

Candidate table with FastScore, PreScore, FinalOpportunityScore, direction,
discovery source, track, and data confidence. Sortable. Highlight rows that
reached a council.

Note: `final_opportunity_score = raw_opportunity_score ×
data_confidence_factor × regime_factor × event_risk_factor`. Showing the
multipliers separately explains why a strong raw score was suppressed.

### 5.4 Council Decision

**This tab answers "why did it take or reject the trade?"**

Pick a `decision_id`, then walk it in order:

1. Evidence and candidate features
2. Bull / Bear / Catalyst assessments (`agent_runs.output_json` where
   `agent_name` in bull, bear, catalyst)
3. PM proposal — thesis, confidence, requested risk, invalidation rules
4. The five real option structures with strikes, deltas, debit,
   cost/width, breakeven
5. Claude's verdict, risk score, and every problem with its category and
   severity
6. PM revision, if any, and what changed
7. Risk Constitution decision, requested vs approved quantity, and every
   violation
8. Order, fill, and realized outcome

Make `prompt_text` and `output_json` available behind expanders. Being able
to show the exact prompt a model saw is a credibility asset.

### 5.5 Counterfactual Lab

**The headline tab. This is the differentiator.**

Variant table: GPT Original / Claude Modified / Executed, each with
quantity, per-spread P&L, and total P&L.

Then the four effects, displayed so the arithmetic is visible:

```
Claude selection effect  : did the Red Team pick a worse structure?
Claude sizing effect     : did it just make the position smaller?
Risk selection effect    : did the risk engine change the expression?
Risk sizing effect       : did position sizing help or hurt?
```

**Never suppress a negative number.** "Our red team cost us $180 in
selection but saved $410 in sizing" is a more credible finding than a
uniformly positive result, and hiding losses would destroy the tab's whole
value. `decision_attribution.notes_json` contains a plain-language
`narrative` string — display it.

Portfolio totals from `v_attribution_totals`.

A VETO shows as `claude_modified_qty = 0`. Label that case explicitly as
"trade avoided" and show what the avoided trade would have done.

### 5.6 Gate Lab

Rejections by gate from `v_gate_histogram`. GateValue from `v_gate_value`,
**with sample size shown next to every number.**

```
GateValue(g) = -1 × mean(hypothetical P&L per spread of trades g blocked)
```

Positive means the gate earned its place. Negative means it blocked
profitable trades.

Sample size is not optional. With 3–8 trades in a competition week, every
one of these figures rests on a handful of observations, and presenting
them without n would be misleading. Consider greying out any GateValue with
n < 5.

Also show: the tier timeline for each session from `config_versions`, and a
table of the most profitable blocked trades.

### 5.7 Execution Quality

Indicative reference vs actual fill, from `execution_calibrations` and
`v_fill_bias`.

- Mean and median `fill_bias_vs_adjusted`
- `fill_slippage_pct` distribution
- `seconds_to_fill`
- `limit_walk_steps` histogram — how often did attempt 1 fill vs attempt 3?
- OPEN vs CLOSE kept separate

Context worth stating on the tab: Alpaca's free Indicative feed is a
derived estimate, not OPRA NBBO. This tab measures what that costs in real
fills. That is a genuine engineering result, not an apology.

### 5.8 Audit

Queryable timeline for any `decision_id`: state transitions from
`system_events`, config version in force, input timestamps, order state
changes, and API usage with cost.

Also a raw `system_events` browser filtered by level and component.

---

## 6. Design guidance

**Handle empty gracefully.** Every panel needs a zero-row path that says
what is missing and why, e.g. *"No trades closed yet. The first session is
Monday, August 31."* Never a stack trace, never an empty chart with no
explanation.

**Small numbers are the reality.** At a $5.20 debit with 1.25% risk on
$100k, position size is **2 spreads**. Individual trade P&L will be in the
low hundreds of dollars. Format accordingly and do not pad with decimals
that imply precision.

**Timestamps.** Stored UTC, displayed ET. Use `to_et()` from
`alpha_council.utils.time`.

**Colour.** Green/red for P&L direction is fine. Do not colour a negative
attribution effect as "bad" — a negative Claude effect is a legitimate
finding, not an error state.

**No fake precision.** Do not add probabilities, confidence intervals, or
Sharpe ratios. The sample is a handful of trades and the system deliberately
avoids inventing statistics it cannot support.

---

## 7. What NOT to do

- No writes to the database, under any circumstance
- No API or LLM calls from the dashboard
- No Next.js, React, or any non-Streamlit frontend
- Do not modify anything under `alpha_council/` — if you need a query
  helper, put it in `dashboard/`
- Do not compute trading logic in the dashboard; every number should come
  from a table or a view. If a figure is missing, that is a gap in the
  system to report, not something to derive in the UI
- Do not break the 440 existing tests

---

## 8. Suggested file layout

```
dashboard/
├── app.py            # entry point, tab routing, sidebar
├── queries.py        # all SQL, read-only, cached
├── formatting.py     # currency, percent, ET timestamps, empty-state text
└── tabs/
    ├── command_center.py
    ├── discovery.py
    ├── scanner.py
    ├── council.py
    ├── counterfactual.py
    ├── gate_lab.py
    ├── execution_quality.py
    └── audit.py
```

Keeping every SQL statement in `queries.py` matters: it makes the read-only
guarantee auditable at a glance.

---

## 9. Recent build history (context)

Completed since the last status document, all tested:

| Component | Path |
|---|---|
| Track-aware scoring, EVENT/MOMENTUM quotas | `quant/scoring.py`, `quant/scanner.py` |
| Budget manager, evidence packs, LLM clients | `agents/budget.py`, `agents/evidence.py`, `agents/llm.py` |
| Five council agents, seven prompt files | `agents/council.py`, `config/prompts/` |
| Trade journal, rejection log | `journal/trade_journal.py` |
| Shadow book, four-way attribution | `journal/shadow_book.py` |
| Consolidated v2.5 schema, 42 tables, 6 views | `db/schema.sql` |
| Position monitor, underlying-driven exits | `execution/position_monitor.py` |
| Orchestrator, breadth-first tier ladder | `orchestrator.py` |
| Scenario payoff engine | `evolution/payoffs.py`, `models/scenario.py` |

Not yet built: the dashboard (this task), the Scenario Generator LLM call,
Post-Trade Lessons, and the Champion/Challenger engine.

**Priority order for the remaining four sessions:** dashboard first,
because Champion vs Challenger with no way to display it is invisible to a
judge.

---

## 10. Acceptance criteria

- [ ] Runs with `uv run streamlit run dashboard/app.py`
- [ ] Every tab renders with an empty database and explains what is missing
- [ ] Zero writes to the database, verifiable by reading `queries.py`
- [ ] Zero API or LLM calls
- [ ] Counterfactual Lab shows all four effects and the reconciling total
- [ ] Gate Lab shows sample size beside every GateValue
- [ ] Discovery Funnel shows `discovery_reason` verbatim
- [ ] Council Decision walks one decision end to end without narration
- [ ] All timestamps displayed in ET
- [ ] Negative attribution results displayed, never suppressed
- [ ] `uv run pytest tests/ -q` still passes 440
