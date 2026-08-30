# JSON Payload Reference

Every `*_json` column in the Alpha Council schema, with its exact shape.
These are serialized Pydantic models from `alpha_council/models/`. Parse
them with `json.loads()`; the structures below are authoritative.

**Defensive parsing note:** any of these columns may be `NULL` or `'[]'` on
a partially-completed decision. Always guard with
`json.loads(row["x_json"] or "[]")` and handle a missing key rather than
assuming a completed pipeline.

---

## red_team_reviews.problems_json

List of Red Team objections. The `category` values are queryable — the Gate
Lab should be able to count how often Claude objected on each grounds.

```json
[
  {
    "category": "EXPRESSION",
    "severity": 7,
    "description": "Breakeven requires a 3.6% move; the thesis supports ~2%.",
    "evidence": ["20-day ATR implies 1.8% typical move"]
  }
]
```

| Field | Type | Notes |
|---|---|---|
| `category` | str | See list below |
| `severity` | int | 1–10 |
| `description` | str | Free text |
| `evidence` | list[str] | May be empty |

**Categories:** `DATA`, `SOURCE`, `NOVELTY`, `THESIS`, `CATALYST`,
`VOLATILITY`, `STRUCTURE`, `LIQUIDITY`, `CONCENTRATION`, `CORRELATION`,
`INVALIDATION`, `TIMING`, `STALENESS`, `EXPRESSION`, `BREAKEVEN`, `OTHER`.

`EXPRESSION` and `BREAKEVEN` come from the mandatory trade-expression
challenge: Claude is required to assume the direction is right and then
explain how the spread still loses. Those two categories are the most
interesting to surface, because they represent objections no stock-thesis
critic would raise.

## red_team_reviews.information_to_reverse_json

```json
["Confirmation that the contract award covers FY27, not just FY26"]
```

Plain list of strings. What evidence would change Claude's mind. Good
material for the Council Decision tab.

---

## risk_evaluations.violations_json

Every rule the Risk Constitution flagged. Note that violations are
**collected, not short-circuited** — a single evaluation may carry six.

```json
[
  {
    "rule_id": "RISK_COST_TO_WIDTH",
    "severity": "BLOCK",
    "message": "cost/width 0.612 above the tier ceiling 0.550",
    "observed_value": 0.612,
    "allowed_value": 0.55
  }
]
```

| Field | Type | Notes |
|---|---|---|
| `rule_id` | str | See list below |
| `severity` | str | `INFO`, `WARN`, `BLOCK`, `HALT` |
| `message` | str | Human-readable |
| `observed_value` | float \| str \| null | |
| `allowed_value` | float \| str \| null | |

**Rule IDs:** `RISK_PAPER_MODE`, `RISK_MARKET_CLOSED`, `RISK_AFTER_CUTOFF`,
`RISK_DATA_BLOCKED`, `RISK_RED_TEAM_VETO`, `RISK_EVENT_BLACKOUT`,
`RISK_PM_CONFIDENCE`, `RISK_SCORE_FLOOR`, `RISK_STRATEGY_NOT_ALLOWED`,
`RISK_LEG_COUNT`, `RISK_DTE_OUT_OF_BOUNDS`, `RISK_0DTE`,
`RISK_COST_TO_WIDTH`, `RISK_LEG_OPEN_INTEREST`, `RISK_LEG_SPREAD`,
`RISK_LIMIT_ABOVE_NATURAL`, `RISK_DUPLICATE_ORDER`, `RISK_MAX_POSITIONS`,
`RISK_PORTFOLIO_FULL`, `RISK_RESIZED`, `RISK_QTY_ZERO`,
`RISK_DAILY_DRAWDOWN`, `RISK_COMPETITION_DRAWDOWN`.

`HALT` severity only ever comes from `RISK_PAPER_MODE`,
`RISK_DAILY_DRAWDOWN`, or `RISK_COMPETITION_DRAWDOWN`. Surface a HALT
banner in the Command Center when one appears.

`RISK_RESIZED` has severity `WARN`, not `BLOCK` — it accompanies a `RESIZE`
decision, which is an approval at reduced size, not a rejection.

---

## trade_proposals.invalidation_json

The PM's stated exit conditions. All are expressible against the
**underlying**, never the option price, because the system cannot observe
option prices in real time.

```json
[
  {
    "rule_type": "PRICE",
    "description": "loses the prior swing low",
    "threshold": 198.0,
    "comparator": "LT"
  }
]
```

| Field | Type | Notes |
|---|---|---|
| `rule_type` | str | `PRICE`, `VWAP`, `TIME`, `CATALYST`, `COMPOSITE` |
| `description` | str | |
| `threshold` | float \| null | `TIME` is in elapsed days |
| `comparator` | str \| null | `LT`, `LTE`, `GT`, `GTE` |

`CATALYST` and `COMPOSITE` rules are recorded but not machine-evaluated by
the position monitor — it cannot observe them. Worth showing them
distinctly in the UI so a judge sees the difference between rules the
system acts on and rules it merely records.

## trade_proposals.key_supporting_evidence_json / key_contrary_evidence_json

```json
["Q3 guidance raised 8% above consensus", "Volume 3.2x the 20-day median"]
```

Plain lists of strings. Displaying both side by side in the Council
Decision tab shows the PM engaged with the bear case.

## trade_journal.invalidation_json

Same shape as `trade_proposals.invalidation_json`. Copied at fill time so
the position monitor has the rules even if the proposal row changes.

---

## agent_runs.output_json

The parsed model response, varying by `agent_name`.

**`bull`, `bear`, `catalyst`** — an `AnalystAssessment`:

```json
{
  "symbol": "NVDA",
  "analyst": "BULL",
  "score": 78.0,
  "confidence": 0.72,
  "thesis": "...",
  "evidence_for": ["..."],
  "evidence_against": ["..."],
  "missing_information": ["..."],
  "invalidation_conditions": ["..."],
  "source_event_ids": ["e_abc123"]
}
```

`score` is 0–100, `confidence` is 0–1. Every assessment carries
`evidence_against` even from the Bull, by design — an analyst returning
nothing on the other side has not done the job.

**`portfolio_manager`, `structure_selection`, `pm_revision`** — a
`PortfolioProposal`, same shape as the `trade_proposals` row.

**`red_team`** — a `RedTeamReview`, same shape as the
`red_team_reviews` row.

On failure, `output_json` holds the raw text instead and `status` is one of
`REFUSED`, `ERROR`, `INVALID_SCHEMA`, or `BUDGET_BLOCKED`. Guard your
parsing accordingly — a failed call is a legitimate state, not corruption.

## agent_runs.prompt_text

The literal prompt sent to the model, up to 20,000 characters. Not JSON.
Worth exposing behind an expander in the Council Decision tab: being able
to show exactly what a model saw is a credibility asset.

---

## candidate_scores.key_metrics_json

```json
{
  "rvol": 2.4,
  "rs15": 0.0042,
  "rs60": 0.0081,
  "intel_events": 3,
  "data_gaps": 0
}
```

`data_gaps` counts missing 5-minute windows in the session — a thin-quoting
signal on the IEX feed. `intel_events` is 0 on MOMENTUM candidates by
definition.

---

## decision_attribution.notes_json

```json
{
  "narrative": "Red Team structure change cost $135.00. Risk Constitution sizing cost $150.00. Governance overall cost $285.00."
}
```

**Display the `narrative` string verbatim in the Counterfactual Lab.** It is
generated by `alpha_council/journal/shadow_book.py:describe()` and is
already phrased for a reader. It never suppresses a negative result, and
neither should the UI.

---

## shadow_trades.structure_json / gate_rejections.shadow_structure_json / rejected_shadows.structure_json

A full serialized `OptionStructure`. The fields you are most likely to want:

```json
{
  "structure_id": "st_NVDA_1_a3f9c2",
  "symbol": "NVDA",
  "strategy": "BULL_CALL_DEBIT",
  "rank": 1,
  "expiration": "2026-09-18",
  "dte": 18,
  "width": 10.0,
  "initial_limit_debit": 5.2,
  "cost_to_width_ratio": 0.52,
  "max_loss_per_spread": 520.0,
  "max_profit_per_spread": 480.0,
  "reward_risk_ratio": 0.923,
  "breakeven": 205.2,
  "stale_adjusted": false,
  "max_quote_lag_seconds": 4.0,
  "staleness_buffer": 0.0,
  "legs": [
    {
      "symbol": "NVDA260918C00200000",
      "strike": 200.0,
      "option_type": "CALL",
      "side": "BUY",
      "delta": 0.6,
      "bid": 5.0,
      "ask": 5.2,
      "raw_mid": 5.1,
      "adjusted_mid": 5.1,
      "quote_lag_seconds": 4.0,
      "open_interest": 3000,
      "volume": 250,
      "implied_volatility": 0.31
    }
  ]
}
```

`legs` always has exactly two entries: one `"side": "BUY"` and one
`"side": "SELL"`. The long leg is the BUY.

`raw_mid` vs `adjusted_mid` differ only when `stale_adjusted` is true,
meaning the price was delta-adjusted for underlying movement since the
quote timestamp. Showing both makes the staleness handling visible.

## shadow_trades.close_policy_json

`{}` in the current build. Reserved.

---

## funnel_snapshots.source_counts_json

```json
{"CORE": 3, "MOST_ACTIVE": 1, "ALPACA_NEWS": 1}
```

Counts of final candidates by discovery source. Note this is written with a
naive `str().replace("'", '"')` in `quant/scanner.py`, so it is valid JSON
for these simple values but **wrap the parse in a try/except** and fall
back to an empty dict.

---

## config_versions.scoring_json / risk_json

Full serialized `config/scoring.yaml` and `config/risk_constitution.yaml`
as they were when that version activated. Large. Useful for the Audit tab
behind an expander — showing exactly which thresholds were in force when a
trade was taken is a strong answer to "how did you pick those numbers?"

The most useful extract is `tiers.{tier}` for the active tier, giving
`pre_score_floor`, `final_score_floor`, `pm_confidence_floor`,
`max_cost_to_width`, `min_open_interest`, `min_volume`,
`max_leg_spread_pct`, and `dte`.

---

## system_events.context_json

Free-form per event type. Common shapes:

```json
{"state": "POSITION_OPEN", "note": ""}
{"source": "MOST_ACTIVE", "reason": "FORBIDDEN_403", "detail": "..."}
{"config_version": "v2.5-t3-142201", "tier": 3}
{"provider": "openai", "spent": 44.12}
```

Event types worth surfacing: `STATE_TRANSITION`, `TIER_CHANGE`,
`DISCOVERY_SOURCE_FORBIDDEN`, `EXIT_TRIGGERED`, `EXIT_NOT_FILLED`,
`ORDER_RECOVERED_AFTER_TIMEOUT`, `PARAM_DROPPED`, `SCHEMA_APPLIED`,
`BUDGET_RESERVE`, `BUDGET_BLOCKED`.

`EXIT_NOT_FILLED` and `ORDER_RECOVERED_AFTER_TIMEOUT` matter operationally —
the first means a position is still open when the system tried to close it,
the second means an order was adopted rather than duplicated after a
timeout. Both belong in the Audit tab.

---

## orders.raw_json / fills.raw_json / market_observations.raw_json

Raw Alpaca API responses, truncated to 20,000 characters. Shape is
whatever Alpaca returned. Show behind an expander in the Audit tab; do not
parse specific fields, since the shape is outside our control.

---

## Empty until Alpha Evolution Phase 1

`scenario_sets.scenarios_json`, `scenario_payoffs`, `premarket_briefs`,
`strategy_lessons`, `strategy_versions`, `challenger_proposals`,
`strategy_shadow_decisions`, `strategy_performance_snapshots`,
`promotion_recommendations`.

Do not build tabs for these. If you want to leave room, a disabled
placeholder is acceptable, but working tabs for existing data come first.

For reference, `scenario_sets.scenarios_json` will hold:

```json
[
  {
    "scenario_type": "CONTINUATION",
    "narrative": "...",
    "underlying_low": 210.0,
    "underlying_mid": 213.0,
    "underlying_high": 218.0,
    "horizon_days": 5,
    "likelihood": "LIKELY",
    "key_drivers": ["..."]
  }
]
```

`scenario_type` ∈ `CONTINUATION`, `STALL`, `REVERSAL`. `likelihood` ∈
`UNLIKELY`, `POSSIBLE`, `LIKELY`. There is deliberately **no numeric
probability field** — the evidence cannot support one.
