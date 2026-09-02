# ALPHA COUNCIL v2.5
## Alpha Evolution Implementation Addendum

**Status:** Normative addendum to Alpha Council v2.4 Dynamic-Discovery Edition  
**Primary purpose:** Add generative scenario reasoning, post-trade learning, and slow strategy evolution without compromising deterministic risk or execution safety.  
**Competition priority:** P&L first, innovation second, presentation third, long-term extensibility fourth.  
**Target environment:** Windows 11, Python 3.11, VS Code, Claude Code and/or OpenAI Codex  
**Execution venue:** Alpaca competition paper account  
**AI budget:** $50 OpenAI + $50 Anthropic  

---

# 0. How Claude Code / Codex Should Use This Addendum

This document **extends Alpha Council v2.4**. It does not replace the v2.4 implementation specification.

If this addendum conflicts with v2.4 only on the features explicitly described here, this addendum governs those features. Otherwise, v2.4 remains authoritative.

Implementation rules:

1. **Finish the existing v2.4 core trading loop first.** Do not delay scoring, agents, journaling, position monitoring, risk, or first supervised autonomous trading to build Alpha Evolution.
2. Build the additions in the order specified in Section 18.
3. Preserve the core principle:

> **Generative AI may interpret, hypothesize, critique, explain, and propose. Deterministic software decides what is true, what is permitted, and what is executed.**

4. Alpha Evolution MUST NOT modify the live Risk Constitution, order sizing code, strategy whitelist, liquidity floor, drawdown limits, paper/live mode, or Alpaca execution code.
5. During the competition, Alpha Evolution is **advisory + shadow only**. It may propose challengers, but challengers MUST NOT automatically replace the live Champion.
6. Intraday strategy self-modification is forbidden.
7. Every generated lesson, proposed strategy change, challenger version, shadow result, and promotion decision MUST be auditable in SQLite.
8. Never let an LLM fabricate prices, Greeks, RVOL, P&L, fills, contracts, thresholds, or performance statistics. Those values come from deterministic code/database rows.
9. Do not introduce vector databases, fine-tuning, reinforcement learning, autonomous code rewriting, or live self-modifying strategy logic before the competition submission.
10. If sample size is insufficient, the correct Evolution output is **NO CHANGE**.

---

# 1. Why Alpha Evolution Exists

Alpha Council already makes decisions through:

```text
Dynamic Discovery
    -> Quant / Opportunity Score
    -> Bull / Bear / Catalyst GPT
    -> GPT Portfolio Manager
    -> Deterministic Options Engine
    -> Claude Red Team
    -> Deterministic Risk Constitution
    -> Alpaca Execution
    -> Counterfactual + Gate Attribution
```

Alpha Evolution adds a slow learning loop around those decisions:

```text
Trade / Rejection Outcomes
        |
        v
Post-Trade Lessons
        |
        v
Strategy Hypotheses
        |
        v
Challenger Proposal
        |
        v
Shadow Evaluation
        |
        v
Evidence Accumulation
        |
        v
Promotion Recommendation
```

The live strategy is the **Champion**. A proposed alternative is a **Challenger**.

Alpha Evolution is deliberately conservative. It is designed to answer:

> **Can the system propose a better version of itself and prove that the proposed version is better before adopting it?**

That is the innovation.

---

# 2. Judging-Criteria Mapping

Alpha Evolution must improve the hackathon submission without damaging P&L.

| Judging criterion | Alpha Council / Evolution response |
|---|---|
| P&L Performance | Dynamic discovery, scenario reasoning, stronger trade-expression analysis, post-trade learning |
| Technology Implementation | Alpaca REST for scale, Alpaca MCP for control/execution, autonomous decision loop, structured GenAI |
| Creativity & Originality | Counterfactual Decision Attribution + Gate Attribution + Champion/Challenger Strategy Evolution |
| Presentation & Execution | Explain why a trade was found, why it was modified/rejected, whether governance added value, and what the system learned |

P&L remains the first priority. No Alpha Evolution feature may delay or destabilize the core trading loop.

---

# 3. Generative AI Role Boundaries

## 3.1 Generative AI is allowed to

- interpret SEC/news evidence;
- generate bull/bear/catalyst arguments;
- generate plausible market scenarios;
- synthesize a portfolio thesis;
- select among deterministic option structures;
- adversarially attack the thesis and trade expression;
- explain counterfactual P&L attribution;
- summarize post-trade lessons;
- propose bounded scoring/selection changes;
- generate Champion/Challenger hypotheses;
- explain why a gate helped or hurt;
- produce pre-market and post-close strategy briefs.

## 3.2 Generative AI is forbidden from

- inventing stock/option prices;
- inventing Greeks;
- inventing contracts or strikes;
- inventing fills or P&L;
- changing hard risk rules;
- changing Alpaca execution behavior;
- directly editing source code during a trading session;
- automatically changing live scoring weights intraday;
- bypassing Claude VETO;
- bypassing Risk Constitution decisions;
- promoting a Challenger during the competition without explicit operator approval.

---

# 4. Updated Alpha Council Architecture

```text
                         ALPHA COUNCIL v2.5

                    PRE-MARKET STRATEGIST
                        GPT Generative AI
                               |
                               v
                        SESSION CONTEXT
                               |
                               v

 DYNAMIC DISCOVERY -> QUANT / EVENT / MOMENTUM -> TOP CANDIDATES
                               |
                               v
              +----------------------------------+
              | GENERATIVE RESEARCH COUNCIL      |
              |                                  |
              | Bull GPT                         |
              | Bear GPT                         |
              | Catalyst GPT                     |
              | Scenario Generator GPT           |
              +----------------+-----------------+
                               |
                               v
                     GPT PORTFOLIO MANAGER
                               |
                               v
                  DETERMINISTIC OPTIONS ENGINE
                     five real valid spreads
                               |
                               v
                    GPT STRUCTURE SELECTION
                               |
                               v
                     CLAUDE CHIEF SKEPTIC
                    PASS / MODIFY / VETO
                               |
                      one PM revision max
                               |
                               v
                    DETERMINISTIC RISK
                               |
                               v
                           ALPACA
                               |
             +-----------------+-----------------+
             |                                   |
             v                                   v
       ACTUAL OUTCOME                      SHADOW OUTCOMES
             |                                   |
             +-----------------+-----------------+
                               |
                               v
                    COUNTERFACTUAL LAB
                               |
                               v
                   POST-TRADE LESSONS AGENT
                               |
                               v
                      ALPHA EVOLUTION
                  STRATEGY IMPROVEMENT AGENT
                               |
                        proposes challenger
                               |
                               v
                    CHAMPION / CHALLENGER
                         SHADOW ENGINE
                               |
                               v
                  PROMOTION RECOMMENDATION
                   (competition: advisory)
```

---

# 5. New Generative Component: Scenario Generator

## 5.1 Purpose

The Scenario Generator sits between the Research Council and the Portfolio Manager.

It answers:

> **What are the most plausible ways this candidate evolves over the intended holding period?**

This is not a price-prediction engine. It generates a small set of structured hypotheses that Python then evaluates against each real option spread.

## 5.2 Required scenarios

Every Council candidate receives exactly three scenarios:

1. **Continuation / thesis-valid scenario**
2. **Base / consolidation scenario**
3. **Failure / adverse scenario**

Optional fourth scenario only when strongly justified:

4. **Macro/regime override scenario**

## 5.3 Scenario constraints

The Scenario Generator MUST NOT invent a precise future price target from thin air.

It should express future movement as a bounded percentage range using evidence already supplied by the system, for example:

```text
Continuation: +3% to +6%
Base: -1% to +2%
Failure: -4% to -2%
```

If evidence does not support a range, the model MUST mark the range confidence LOW.

Python, not the LLM, maps those percentage ranges to underlying prices and option-spread payoff estimates.

---

# 6. Deterministic Scenario Payoff Engine

Create:

```text
alpha_council/options_engine/scenario_payoff.py
```

For each scenario and each of the five real option structures, Python calculates terminal intrinsic payoff at scenario lower/base/upper underlying values.

For a bull call debit spread:

```text
payoff(S) = max(S - K_long, 0) - max(S - K_short, 0) - debit
```

For a bear put debit spread:

```text
payoff(S) = max(K_long - S, 0) - max(K_short - S, 0) - debit
```

Multiply per-share payoff by 100 for one spread.

Required output per structure:

```text
continuation_low_pnl
continuation_mid_pnl
continuation_high_pnl
base_low_pnl
base_mid_pnl
base_high_pnl
failure_low_pnl
failure_mid_pnl
failure_high_pnl
```

This is intentionally simplified terminal payoff mapping. It does not pretend to forecast IV/theta exactly.

The PM receives the deterministic scenario-payoff table and reasons over it.

---

# 7. Updated Claude Red Team Behavior

Claude remains Chief Skeptic.

Add one mandatory challenge:

> **Assume the Portfolio Manager is directionally correct. Explain how the selected option spread can still lose money.**

Claude must explicitly analyze:

- move magnitude insufficient for breakeven;
- move occurs too slowly;
- implied-volatility collapse risk;
- theta/time decay;
- poor strike placement;
- unfavorable indicated-to-fill bias;
- concentration/correlation;
- scheduled event timing;
- invalidation weakness.

This turns Claude from a stock-thesis critic into a trade-expression critic.

---

# 8. New Generative Component: Pre-Market Strategist

## 8.1 Purpose

One GPT Sol call around 08:45 ET generates session-level context.

Inputs:

- overnight Alpaca News summaries;
- fresh SEC events;
- known earnings/calendar blackouts;
- prior session lessons;
- SPY/QQQ/IWM/DIA regime context;
- current portfolio exposure;
- current Champion strategy version.

Output is a structured `PreMarketBrief`.

This output is context only. It does not alter hard gates or scoring weights.

## 8.2 Example output

```text
Session bias: moderately risk-on
Highest-impact themes:
- semiconductors strong overnight
- financials neutral
- macro event at 10:00 ET

Candidate themes to watch:
- high-RVOL semiconductors
- post-earnings continuation

Risks:
- crowded QQQ exposure
- macro blackout window
```

The brief may influence PM reasoning but cannot directly increase a candidate score.

---

# 9. New Generative Component: Post-Trade Lessons Agent

## 9.1 Purpose

After every closed trade and every meaningful rejected shadow candidate, generate a structured lesson.

Inputs MUST come from the database:

- candidate features;
- discovery source;
- EVENT/MOMENTUM track;
- Bull/Bear/Catalyst outputs;
- scenarios;
- PM proposal;
- five option structures;
- selected structure;
- Claude review;
- risk decision;
- order/fill details;
- execution calibration;
- actual P&L;
- GPT-original shadow P&L;
- Claude-modified shadow P&L;
- rejected-shadow P&L where applicable;
- market/regime context.

The Lessons Agent MUST distinguish:

- observation;
- explanation hypothesis;
- confidence;
- evidence count/sample size;
- proposed future test.

It MUST NOT produce a direct live configuration change.

---

# 10. Alpha Evolution: Strategy Improvement Agent

## 10.1 Purpose

Alpha Evolution consumes accumulated lessons and deterministic performance statistics and proposes bounded Challenger configurations.

It answers:

> **What one small change is most worth testing next?**

Not:

> **Rewrite the strategy.**

## 10.2 Permitted competition-era Challenger changes

During the hackathon, Evolution MAY propose changes to:

- EVENT vs MOMENTUM Council quota;
- scoring weights within configured bounded ranges;
- PreScore/FinalOpportunityScore quality thresholds within bounded ranges;
- discovery-source priority;
- maximum number of Councils per scan/day;
- preferred DTE target within existing hard DTE range;
- preferred long/short delta targets within existing hard delta range;
- Scenario Generator emphasis or PM evidence weighting prompts;
- Red Team evidence emphasis.

## 10.3 Forbidden Evolution changes

Evolution MUST NOT propose or apply changes to:

- paper-only lock;
- maximum daily drawdown;
- maximum competition drawdown;
- maximum per-trade risk;
- maximum total open option risk;
- maximum sector risk;
- naked/credit/0DTE strategy bans;
- minimum hard liquidity floor;
- order idempotency;
- Alpaca live/paper account selection;
- execution safety checks;
- Claude VETO semantics;
- Risk Constitution authority.

These are constitutional rules, not optimization parameters.

---

# 11. Champion / Challenger Engine

## 11.1 Champion

The current live configuration version.

Example:

```text
champion_id = alpha_v2_5_c0
```

The Champion alone may create live competition paper orders.

## 11.2 Challenger

A proposed strategy variation generated by Alpha Evolution.

Example:

```text
challenger_id = alpha_v2_5_c1
parent_id = alpha_v2_5_c0
```

The Challenger evaluates the same stored candidates and/or future candidates in shadow.

## 11.3 Competition behavior

During the competition:

- Challenger is shadow-only.
- Promotion requires explicit operator approval.
- No automatic intraday promotion.
- Prefer one Challenger at a time.
- Do not create a new Challenger until the previous one has a minimally useful evidence set unless a fatal flaw is discovered.

## 11.4 Post-competition behavior

After the competition, automatic promotion MAY be enabled only if all promotion safeguards in Section 13 are implemented and tested.

---

# 12. Slow-Learning / Anti-Chasing Rules

Alpha Evolution MUST have inertia.

## 12.1 Competition rules

For the hackathon:

```text
minimum trades before claiming a pattern: 5
minimum observations before proposing a parameter change: 8
minimum observations before recommending promotion: 12
minimum trading sessions before recommending promotion: 2
```

Because the competition sample is tiny, even these counts are weak evidence. Evolution MUST label conclusions as LOW/MEDIUM/HIGH confidence.

Default hackathon behavior should usually be:

```text
Observation -> Challenger hypothesis -> Shadow test -> Do not auto-promote
```

## 12.2 Post-competition promotion defaults

Suggested initial post-competition requirements:

```text
minimum closed challenger trades: 25
minimum challenger trading days: 10
minimum Champion baseline trades: 25
minimum promotion holdout sample: 10
```

Promotion requires:

```text
challenger total return > champion total return
AND challenger max drawdown <= champion max drawdown
AND challenger expectancy > champion expectancy
AND no material deterioration in execution quality
AND no hard-risk rule changes
AND no single trade contributes > 35% of challenger total P&L
AND improvement is not isolated to one ticker
```

Promotion SHOULD also require either:

- improvement in both EVENT and MOMENTUM subsets; or
- documented evidence that the Challenger intentionally specializes and the portfolio impact remains acceptable.

---

# 13. Hysteresis and Change Bounds

The strategy must not oscillate.

Default post-competition bounds:

```yaml
alpha_evolution:
  max_weight_change_per_version_pct: 10
  max_threshold_change_per_version_pct: 10
  min_version_lifetime_sessions: 5
  min_days_between_promotions: 5
  max_active_challengers: 1
  require_operator_approval_during_competition: true
  auto_promotion_enabled: false
```

A newly promoted Champion cannot be replaced immediately because of one poor day.

If a promoted version materially underperforms, rollback requires the same deterministic process or an explicit operator emergency action.

---

# 14. Pydantic Models

Add:

```python
from datetime import datetime
from enum import StrEnum
from typing import Literal
from pydantic import Field


class ScenarioType(StrEnum):
    CONTINUATION = "CONTINUATION"
    BASE = "BASE"
    FAILURE = "FAILURE"
    MACRO_OVERRIDE = "MACRO_OVERRIDE"


class MarketScenario(StrictModel):
    scenario_type: ScenarioType
    description: str
    lower_return_pct: float
    midpoint_return_pct: float
    upper_return_pct: float
    confidence: float = Field(ge=0, le=1)
    evidence_ids: list[str]
    invalidation_conditions: list[str]


class ScenarioSet(StrictModel):
    decision_id: str
    symbol: str
    generated_at: datetime
    scenarios: list[MarketScenario] = Field(min_length=3, max_length=4)
    overall_uncertainty: Literal["LOW", "MEDIUM", "HIGH"]


class StructureScenarioPayoff(StrictModel):
    structure_id: str
    scenario_type: ScenarioType
    underlying_low: float
    underlying_mid: float
    underlying_high: float
    pnl_low: float
    pnl_mid: float
    pnl_high: float


class PreMarketBrief(StrictModel):
    session_date: str
    generated_at: datetime
    regime_summary: str
    session_bias: Literal["RISK_ON", "RISK_OFF", "MIXED", "NEUTRAL"]
    important_themes: list[str]
    candidate_themes: list[str]
    risk_windows: list[str]
    portfolio_concerns: list[str]
    prior_session_lessons: list[str]
    confidence: float = Field(ge=0, le=1)


class LessonType(StrEnum):
    TRADE = "TRADE"
    REJECTED_CANDIDATE = "REJECTED_CANDIDATE"
    EXECUTION = "EXECUTION"
    GATE = "GATE"
    RED_TEAM = "RED_TEAM"


class StrategyLesson(StrictModel):
    lesson_id: str
    source_decision_id: str | None = None
    lesson_type: LessonType
    created_at: datetime
    observation: str
    explanation_hypothesis: str
    evidence_for: list[str]
    evidence_against: list[str]
    sample_size: int = Field(ge=1)
    confidence: Literal["LOW", "MEDIUM", "HIGH"]
    proposed_test: str
    recommends_change: bool


class ChangeCategory(StrEnum):
    SCORING_WEIGHT = "SCORING_WEIGHT"
    QUALITY_THRESHOLD = "QUALITY_THRESHOLD"
    TRACK_QUOTA = "TRACK_QUOTA"
    DISCOVERY_PRIORITY = "DISCOVERY_PRIORITY"
    OPTIONS_PREFERENCE = "OPTIONS_PREFERENCE"
    PROMPT_EMPHASIS = "PROMPT_EMPHASIS"


class ParameterChange(StrictModel):
    category: ChangeCategory
    parameter_path: str
    champion_value: float | int | str
    challenger_value: float | int | str
    relative_change_pct: float | None = None
    rationale: str


class ChallengerProposal(StrictModel):
    challenger_id: str
    parent_champion_id: str
    created_at: datetime
    hypothesis: str
    evidence_summary: list[str]
    changes: list[ParameterChange] = Field(min_length=1, max_length=3)
    expected_benefit: str
    expected_failure_mode: str
    minimum_shadow_observations: int = Field(ge=5)
    confidence: Literal["LOW", "MEDIUM", "HIGH"]


class StrategyPerformance(StrictModel):
    strategy_id: str
    observations: int = Field(ge=0)
    closed_trades: int = Field(ge=0)
    total_pnl: float
    return_pct: float
    win_rate: float | None = Field(default=None, ge=0, le=1)
    expectancy: float | None = None
    max_drawdown_pct: float = Field(ge=0)
    average_win: float | None = None
    average_loss: float | None = None
    profit_factor: float | None = None
    event_pnl: float
    momentum_pnl: float
    execution_bias_mean: float | None = None
    execution_bias_median: float | None = None


class PromotionRecommendation(StrictModel):
    champion_id: str
    challenger_id: str
    generated_at: datetime
    champion_performance: StrategyPerformance
    challenger_performance: StrategyPerformance
    recommendation: Literal["KEEP_CHAMPION", "CONTINUE_SHADOW", "PROMOTE_CHALLENGER"]
    evidence_strength: Literal["INSUFFICIENT", "LOW", "MEDIUM", "HIGH"]
    reasons: list[str]
    failed_promotion_rules: list[str]
    operator_approval_required: bool = True
```

---

# 15. SQLite Schema Additions

Additive migration only. Do not rebuild the existing database.

```sql
CREATE TABLE IF NOT EXISTS scenario_sets (
    scenario_set_id TEXT PRIMARY KEY,
    decision_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    generated_at TEXT NOT NULL,
    overall_uncertainty TEXT NOT NULL,
    scenarios_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_scenario_sets_decision
    ON scenario_sets(decision_id, generated_at DESC);

CREATE TABLE IF NOT EXISTS scenario_payoffs (
    payoff_id TEXT PRIMARY KEY,
    decision_id TEXT NOT NULL,
    structure_id TEXT NOT NULL,
    scenario_type TEXT NOT NULL,
    underlying_low REAL NOT NULL,
    underlying_mid REAL NOT NULL,
    underlying_high REAL NOT NULL,
    pnl_low REAL NOT NULL,
    pnl_mid REAL NOT NULL,
    pnl_high REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_scenario_payoff_structure
    ON scenario_payoffs(structure_id, scenario_type);

CREATE TABLE IF NOT EXISTS premarket_briefs (
    brief_id TEXT PRIMARY KEY,
    session_date TEXT NOT NULL UNIQUE,
    generated_at TEXT NOT NULL,
    model TEXT NOT NULL,
    output_json TEXT NOT NULL,
    input_hash TEXT NOT NULL,
    cost_usd REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS strategy_lessons (
    lesson_id TEXT PRIMARY KEY,
    source_decision_id TEXT,
    lesson_type TEXT NOT NULL,
    created_at TEXT NOT NULL,
    observation TEXT NOT NULL,
    explanation_hypothesis TEXT NOT NULL,
    evidence_for_json TEXT NOT NULL,
    evidence_against_json TEXT NOT NULL,
    sample_size INTEGER NOT NULL,
    confidence TEXT NOT NULL,
    proposed_test TEXT NOT NULL,
    recommends_change INTEGER NOT NULL CHECK(recommends_change IN (0,1))
);
CREATE INDEX IF NOT EXISTS idx_strategy_lessons_time
    ON strategy_lessons(created_at DESC, lesson_type);

CREATE TABLE IF NOT EXISTS strategy_versions (
    strategy_id TEXT PRIMARY KEY,
    parent_strategy_id TEXT,
    status TEXT NOT NULL, -- CHAMPION, CHALLENGER, RETIRED
    created_at TEXT NOT NULL,
    promoted_at TEXT,
    retired_at TEXT,
    config_version TEXT NOT NULL,
    config_json TEXT NOT NULL,
    hypothesis TEXT,
    operator_approved INTEGER NOT NULL DEFAULT 0 CHECK(operator_approved IN (0,1))
);
CREATE INDEX IF NOT EXISTS idx_strategy_versions_status
    ON strategy_versions(status, created_at DESC);

CREATE TABLE IF NOT EXISTS challenger_proposals (
    challenger_id TEXT PRIMARY KEY,
    parent_champion_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    hypothesis TEXT NOT NULL,
    evidence_summary_json TEXT NOT NULL,
    changes_json TEXT NOT NULL,
    expected_benefit TEXT NOT NULL,
    expected_failure_mode TEXT NOT NULL,
    minimum_shadow_observations INTEGER NOT NULL,
    confidence TEXT NOT NULL,
    status TEXT NOT NULL -- PROPOSED, SHADOWING, REJECTED, PROMOTABLE, PROMOTED
);

CREATE TABLE IF NOT EXISTS strategy_shadow_decisions (
    shadow_decision_id TEXT PRIMARY KEY,
    source_decision_id TEXT NOT NULL,
    strategy_id TEXT NOT NULL,
    evaluated_at TEXT NOT NULL,
    would_trade INTEGER NOT NULL CHECK(would_trade IN (0,1)),
    selected_structure_id TEXT,
    requested_risk_pct REAL,
    hypothetical_qty INTEGER,
    rationale_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_strategy_shadow_strategy
    ON strategy_shadow_decisions(strategy_id, evaluated_at DESC);

CREATE TABLE IF NOT EXISTS strategy_performance_snapshots (
    snapshot_id TEXT PRIMARY KEY,
    strategy_id TEXT NOT NULL,
    as_of TEXT NOT NULL,
    observations INTEGER NOT NULL,
    closed_trades INTEGER NOT NULL,
    total_pnl REAL NOT NULL,
    return_pct REAL NOT NULL,
    win_rate REAL,
    expectancy REAL,
    max_drawdown_pct REAL NOT NULL,
    average_win REAL,
    average_loss REAL,
    profit_factor REAL,
    event_pnl REAL NOT NULL,
    momentum_pnl REAL NOT NULL,
    execution_bias_mean REAL,
    execution_bias_median REAL,
    metrics_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_strategy_perf
    ON strategy_performance_snapshots(strategy_id, as_of DESC);

CREATE TABLE IF NOT EXISTS promotion_recommendations (
    recommendation_id TEXT PRIMARY KEY,
    champion_id TEXT NOT NULL,
    challenger_id TEXT NOT NULL,
    generated_at TEXT NOT NULL,
    recommendation TEXT NOT NULL,
    evidence_strength TEXT NOT NULL,
    reasons_json TEXT NOT NULL,
    failed_rules_json TEXT NOT NULL,
    operator_approval_required INTEGER NOT NULL CHECK(operator_approval_required IN (0,1)),
    approved_by_operator INTEGER CHECK(approved_by_operator IN (0,1)),
    approved_at TEXT
);
```

---

# 16. Configuration Additions

Add to `config/scoring.yaml` or create `config/alpha_evolution.yaml`.

Recommended separate file:

```yaml
config_version: v2.5

scenario_generator:
  enabled: true
  max_scenarios: 4
  default_scenarios: 3
  model: gpt-5.6-sol
  reasoning: medium
  max_calls_per_council: 1

premarket_strategist:
  enabled: true
  time_et: "08:45"
  model: gpt-5.6-sol
  max_calls_per_day: 1

post_trade_lessons:
  enabled: true
  model: gpt-5.6-sol
  generate_for_closed_trades: true
  generate_for_rejected_shadows: true
  min_rejected_shadow_abs_pnl: 100.0

alpha_evolution:
  enabled: true
  competition_mode: true
  auto_promotion_enabled: false
  operator_approval_required: true
  max_active_challengers: 1
  max_changes_per_challenger: 3

  competition:
    min_observations_to_propose: 8
    min_observations_to_recommend_promotion: 12
    min_sessions_to_recommend_promotion: 2

  post_competition:
    min_challenger_closed_trades: 25
    min_challenger_sessions: 10
    min_champion_closed_trades: 25
    min_holdout_trades: 10
    max_single_trade_pnl_contribution_pct: 35

  hysteresis:
    max_weight_change_per_version_pct: 10
    max_threshold_change_per_version_pct: 10
    min_version_lifetime_sessions: 5
    min_days_between_promotions: 5

  immutable_paths:
    - risk.max_risk_per_trade_pct
    - risk.max_total_open_option_risk_pct
    - risk.max_sector_open_risk_pct
    - risk.max_daily_drawdown_pct
    - risk.max_competition_peak_drawdown_pct
    - risk.paper_only
    - risk.allow_naked_options
    - risk.allow_credit_spreads
    - risk.no_0dte
    - execution.idempotency
    - execution.broker
    - liquidity.hard_floor
```

---

# 17. Exact Generative Prompts

## 17.1 Scenario Generator system prompt

Create `config/prompts/scenario_generator_system.txt`:

```text
You are Alpha Council's Scenario Generator.

Your task is to generate a small set of plausible market scenarios for the supplied candidate over the Portfolio Manager's intended holding period.

You are not forecasting with certainty. You are constructing bounded hypotheses from supplied evidence.

Required scenarios:
1. CONTINUATION - thesis-valid / directional continuation.
2. BASE - consolidation or modest move.
3. FAILURE - thesis failure / adverse move.
4. MACRO_OVERRIDE only if strongly justified by supplied event/regime context.

Rules:
- Use only evidence in the supplied package.
- Do not invent news, analyst estimates, option prices, or market data.
- Express each scenario as a lower/mid/upper return-percent range for the underlying.
- Keep ranges realistic relative to the supplied volatility, recent price behavior, holding period, and event context.
- If the evidence does not support a precise range, widen the range and lower confidence.
- Scenarios must be meaningfully distinct.
- Do not select an option structure or risk amount.
- Output only the structured ScenarioSet schema.
```

## 17.2 Updated PM scenario instruction

Append to PM evidence package:

```text
You are also given deterministic payoff tables for each real option structure under the Scenario Generator's scenarios.

Use those payoff tables to judge whether the selected spread expresses the thesis efficiently.
Do not recompute or alter the deterministic payoff numbers.
Do not choose a structure solely because it has the largest optimistic-scenario payoff.
Balance:
- thesis fit;
- downside under failure scenario;
- breakeven difficulty;
- liquidity;
- expected holding period;
- Red Team concerns when revising.
```

## 17.3 Updated Claude Red Team prompt

Append:

```text
Mandatory trade-expression challenge:
Assume the Portfolio Manager's directional thesis is correct. Explain the most plausible way the selected option spread still loses money.

Explicitly inspect:
- insufficient move magnitude;
- move occurs too slowly;
- theta/time decay;
- implied-volatility compression;
- strike placement;
- breakeven difficulty;
- indicative-to-fill pricing bias;
- event timing;
- concentration/correlation.

If directional correctness is not enough to make the spread attractive, MODIFY or VETO as appropriate.
```

## 17.4 Pre-Market Strategist prompt

Create `config/prompts/premarket_strategist_system.txt`:

```text
You are Alpha Council's Pre-Market Strategist.

Create a concise session context brief from the supplied deterministic market, intelligence, calendar, portfolio, and prior-session lesson data.

Your output is context, not an order and not a scoring override.

Identify:
- current broad market regime;
- highest-impact overnight developments;
- sectors/themes worth monitoring;
- known earnings/macro risk windows;
- portfolio concentration concerns;
- prior-session lessons relevant today.

Do not:
- invent catalysts;
- change risk limits;
- change scoring weights;
- choose option contracts;
- tell the execution layer to trade.

Output only PreMarketBrief.
```

## 17.5 Post-Trade Lessons prompt

Create `config/prompts/post_trade_lessons_system.txt`:

```text
You are Alpha Council's Post-Trade Lessons Agent.

Your job is to explain what the completed decision teaches the system without overfitting to one outcome.

Use only the supplied deterministic decision record and P&L/counterfactual data.

Separate:
1. Observation - what happened.
2. Explanation hypothesis - why it may have happened.
3. Evidence for the hypothesis.
4. Evidence against the hypothesis.
5. Sample size / confidence.
6. Proposed future test.

Rules:
- One trade is not a robust pattern.
- Do not recommend a strategy change solely because a trade lost.
- Distinguish selection error, sizing error, execution error, timing error, and thesis error.
- If the Red Team or Risk Constitution changed the trade, use counterfactual attribution to explain whether that change added or destroyed value.
- If sample size is weak, recommends_change must be false.
- Output only StrategyLesson.
```

## 17.6 Alpha Evolution prompt

Create `config/prompts/alpha_evolution_system.txt`:

```text
You are Alpha Evolution, Alpha Council's slow strategy-improvement agent.

Your task is to propose at most one bounded Challenger strategy hypothesis from accumulated lessons and deterministic Champion performance statistics.

You are not allowed to rewrite the strategy broadly.

Rules:
- Prefer NO CHANGE when evidence is insufficient.
- Propose no more than 3 parameter changes.
- Each change must be small and testable.
- Do not modify any immutable constitutional/risk/execution parameter.
- Do not infer improvement from a single ticker, single trade, or one unusually large winner.
- Look for repeated patterns across multiple decisions.
- Explicitly state the expected benefit and the most plausible failure mode of the Challenger.
- During competition mode, all Challengers are shadow-only and promotion requires operator approval.
- Output only ChallengerProposal, or return no proposal if evidence is insufficient.
```

## 17.7 Promotion Analyst prompt

Create `config/prompts/promotion_analyst_system.txt`:

```text
You are Alpha Evolution's Promotion Analyst.

Compare the Champion and Challenger only using supplied deterministic performance statistics and promotion-rule results.

You cannot override a failed hard promotion rule.

Return:
- KEEP_CHAMPION;
- CONTINUE_SHADOW; or
- PROMOTE_CHALLENGER.

During competition mode, PROMOTE_CHALLENGER is advisory and operator approval remains mandatory.

Prefer CONTINUE_SHADOW when sample size is weak, performance is concentrated in one trade/ticker, or drawdown/execution quality worsens materially.

Output only PromotionRecommendation.
```

---

# 18. Build Order Relative to Current v2.4 Build

Do **not** build Alpha Evolution before the existing core loop works.

## Phase 0 - Mandatory core first

Finish the current v2.4 P0 items:

1. `quant/scoring.py`
2. OpenAI/Anthropic clients
3. Bull/Bear/Catalyst agents
4. Portfolio Manager
5. Claude Red Team
6. minimal journal
7. dry-run orchestrator
8. position monitor
9. first supervised calibration order
10. first end-to-end alpha decision

Only then proceed.

## Phase 1 - Scenario Generator

Build:

```text
models/evolution.py
agents/scenario_generator.py
options_engine/scenario_payoff.py
config/prompts/scenario_generator_system.txt
```

Tests:

```text
test_three_required_scenarios
test_scenario_ranges_ordered
test_scenario_payoff_bull_call
test_scenario_payoff_bear_put
test_pm_cannot_alter_payoff_values
```

Acceptance:

```text
one stored candidate -> 3 scenarios -> deterministic payoff table for 5 structures -> PM selection
```

## Phase 2 - Red Team upgrade

Patch Claude prompt and schema if needed.

Test:

```text
test_red_team_directionally_correct_but_bad_structure_case
```

## Phase 3 - Post-Trade Lessons

Build:

```text
journal/lessons.py
agents/post_trade_lessons.py
```

Acceptance:

```text
closed trade -> StrategyLesson with sample size/confidence/proposed test
```

## Phase 4 - Pre-Market Strategist

Build:

```text
agents/premarket_strategist.py
```

Run once/day. Store output. Feed as context to PM/Claude but not scoring/risk.

## Phase 5 - Alpha Evolution Challenger Generator

Build:

```text
evolution/champion.py
evolution/challenger.py
evolution/change_validator.py
evolution/shadow_runner.py
agents/alpha_evolution.py
```

`change_validator.py` MUST reject immutable paths before a Challenger can be stored.

## Phase 6 - Strategy Performance + Promotion

Build:

```text
evolution/performance.py
evolution/promotion.py
agents/promotion_analyst.py
```

Competition default:

```text
auto_promotion_enabled = false
```

## Phase 7 - Dashboard additions

Only after core dashboard exists.

Add:

- Alpha Evolution tab;
- Champion vs Challenger metrics;
- Lessons feed;
- Challenger hypothesis;
- promotion-rule checklist;
- strategy version timeline.

---

# 19. Strategy Shadow Evaluation

A Challenger should evaluate the same opportunity stream without touching Alpaca.

For each source candidate/decision:

1. Load deterministic candidate features and option structures from the original timestamp.
2. Apply Challenger scoring/selection configuration.
3. Determine whether the Challenger would have convened a Council.
4. If required for accurate comparison, reuse stored agent outputs when the change does not affect evidence/prompt behavior.
5. If the Challenger changes prompt behavior or Council selection materially, run a separate Challenger AI evaluation and record its API cost.
6. Select a hypothetical structure/qty.
7. Mark shadow P&L using the same marking/exit methodology as the Champion.
8. Store the decision in `strategy_shadow_decisions`.

Never compare Champion actual fills directly against Challenger perfect theoretical mids without adjustment. Use the same fill-bias/calibration assumptions where feasible.

---

# 20. Strategy Performance Metrics

Calculate deterministically:

```text
total_pnl
return_pct
closed_trades
win_rate
average_win
average_loss
expectancy
profit_factor
max_drawdown_pct
EVENT pnl
MOMENTUM pnl
average risk per trade
median holding time
rejection rate
Council-to-trade conversion
execution fill bias
limit-walk frequency
```

Expectancy:

```text
expectancy = win_rate * average_win + (1 - win_rate) * average_loss
```

where `average_loss` is negative.

Profit factor:

```text
profit_factor = gross_profit / abs(gross_loss)
```

If gross loss is zero, store NULL rather than infinity.

---

# 21. Competition-Safe Alpha Evolution Mode

During Aug 31-Sep 3 competition trading:

```text
Champion trades
Challenger shadows
Evolution observes
Promotion is advisory only
```

Recommended schedule:

```text
08:45  Pre-Market Strategist
RTH    Normal Alpha Council trading
16:15  Post-close deterministic metrics
16:20  Post-Trade Lessons generation
16:30  Alpha Evolution reviews lessons/statistics
16:35  At most one Challenger proposal
Next day Challenger shadows
```

Do not modify the live Champion automatically because of Monday's or Tuesday's results.

The competition sample is too small for statistically reliable self-optimization.

The innovation to demonstrate is the **process**, not a false claim of learned optimality.

---

# 22. Post-Competition Alpha Evolution Mode

After the competition, the system may evolve into a persistent strategy laboratory.

Recommended progression:

## Stage A - human-approved promotions

- Challenger runs for at least 10 sessions / 25 trades.
- Promotion engine makes recommendation.
- Human approves/rejects.

## Stage B - semi-autonomous promotion

- automatic promotion only when all hard rules pass;
- operator receives notification and can rollback;
- immutable constitutional parameters remain locked.

## Stage C - broader portfolio manager

Once Alpha Council transitions from directional Option A toward portfolio Option B, Evolution may propose changes to:

- hedging frequency;
- capital allocation by strategy;
- sector exposure targets within constitutional caps;
- EVENT/MOMENTUM mix;
- volatility-regime strategy selection.

Still no autonomous modification of hard risk constraints.

---

# 23. Database / Strategy Versioning Rules

Every decision MUST record:

```text
config_version
strategy_id
champion_id
candidate_track
discovery_source
prompt_version
model_version
```

A strategy version is immutable after activation.

To change strategy parameters:

1. create new strategy version;
2. assign parent ID;
3. persist full config snapshot;
4. shadow-test;
5. recommend promotion;
6. promote or reject;
7. never mutate historical versions in place.

This is necessary for attribution credibility.

---

# 24. API Budget Strategy

The existing budget is underutilized. Alpha Evolution should spend more per **high-quality decision**, not widen AI scanning.

Recommended competition budget priorities:

1. Core Council + PM + Claude
2. Scenario Generator
3. Larger Claude evidence/context budget
4. Post-Trade Lessons
5. Pre-Market Strategist
6. Alpha Evolution post-close
7. Promotion Analyst only when a Challenger exists

Do not spend GenAI budget on the 250-symbol discovery layer.

Recommended soft limits:

```text
OpenAI working budget: $44
OpenAI emergency reserve: $6
Anthropic working budget: $44
Anthropic emergency reserve: $6
```

If budget unexpectedly rises:

- keep PM and Red Team;
- reduce analyst verbosity;
- skip pre-market brief before skipping Red Team;
- postpone Alpha Evolution calls before compromising execution safety.

---

# 25. Dashboard Additions

Add one new tab after the existing competition-critical dashboard is complete:

# Alpha Evolution

Show:

```text
CURRENT CHAMPION
alpha_v2_5_c0

ACTIVE CHALLENGER
alpha_v2_5_c1

Hypothesis:
Increase Momentum emphasis when RVOL > 3x and QQQ regime aligns.

Evidence:
8 observations
Confidence: LOW

CHAMPION vs CHALLENGER
P&L
Return
Max drawdown
Win rate
Expectancy
EVENT P&L
MOMENTUM P&L

Promotion status:
CONTINUE SHADOW

Failed rules:
- insufficient closed trades
- only one trading session observed
```

Also show recent Strategy Lessons.

The dashboard must communicate that **the model learns cautiously**.

---

# 26. Presentation / Demo Narrative

The hackathon story should become:

### Alpha Council

> Generates and debates trades.

### Alpha Constitution

> Prevents AI from violating hard risk policy.

### Counterfactual Lab

> Measures whether GPT, Claude, and risk decisions actually added value.

### Alpha Evolution

> Uses those measured outcomes to propose a better strategy, but forces the new version to prove itself in shadow before promotion.

Demo sequence:

```text
1. Dynamic discovery finds candidate.
2. Scenario Generator produces 3 plausible paths.
3. Python shows payoff of 5 real spreads under all scenarios.
4. GPT selects a trade.
5. Claude explains how it can lose even if direction is right.
6. Risk Constitution approves/resizes/rejects.
7. Alpaca executes.
8. Counterfactual Lab shows what each layer added/cost.
9. Post-Trade Lessons explains the outcome.
10. Alpha Evolution proposes a bounded Challenger.
11. Dashboard shows Champion vs Challenger in shadow.
12. Promotion remains blocked because evidence is insufficient.
```

The final point is important. Showing that Alpha Evolution **refuses to overreact** is a feature, not a limitation.

Suggested closing line:

> **Alpha Council does not just explain its trades. It measures whether its own reasoning helped, learns from those measurements, and tests better versions of itself before trusting them with capital.**

---

# 27. Test Plan

## Scenario Generator

```text
test_required_scenarios_present
test_scenario_return_bounds_valid
test_no_more_than_one_macro_override
test_low_confidence_allowed
test_invalid_extra_keys_rejected
```

## Scenario payoff

```text
test_bull_call_payoff_formula
test_bear_put_payoff_formula
test_terminal_pnl_capped_by_spread_width
test_scenario_underlying_prices_derived_from_current_price
```

## Lessons

```text
test_one_trade_cannot_trigger_high_confidence_change
test_lesson_requires_sample_size
test_counterfactual_values_match_database
test_lesson_cannot_invent_pnl
```

## Change validator

```text
test_reject_risk_limit_change
test_reject_paper_mode_change
test_reject_liquidity_floor_change
test_reject_credit_spread_enablement
test_allow_bounded_scoring_weight_change
test_allow_bounded_track_quota_change
test_reject_change_over_10_percent_bound
```

## Champion / Challenger

```text
test_only_champion_can_execute
test_challenger_is_shadow_only_in_competition
test_strategy_version_immutable
test_single_active_challenger_limit
```

## Promotion

```text
test_insufficient_sample_continues_shadow
test_higher_return_but_higher_drawdown_does_not_auto_promote
test_single_trade_concentration_blocks_promotion
test_failed_hard_rule_cannot_be_overridden_by_llm
test_competition_promotion_requires_operator_approval
```

---

# 28. Failure Handling

| Failure | Behavior |
|---|---|
| Scenario Generator unavailable | run existing v2.4 Council without scenarios; log degraded mode |
| Scenario output malformed | no scenario context; do not invent replacement |
| Pre-market strategist unavailable | normal scanner continues |
| Lessons Agent unavailable | store deterministic outcome; generate lesson later |
| Alpha Evolution unavailable | Champion continues unchanged |
| Challenger engine fails | Champion unaffected |
| Promotion Analyst malformed output | KEEP_CHAMPION / no promotion |
| DB persistence failure | no strategy-version transition |
| Sample size insufficient | NO CHANGE / CONTINUE SHADOW |
| Challenger proposes immutable change | reject before storage/activation |
| Model proposes >3 changes | schema validation failure |

Alpha Evolution must be **non-load-bearing** for live trading safety.

---

# 29. Definition of Done - Competition Version

Alpha Evolution is competition-demo ready when:

```text
[ ] Existing v2.4 core autonomous loop works
[ ] Scenario Generator returns 3 structured scenarios
[ ] Python scenario payoff table works for five real spreads
[ ] PM consumes deterministic scenario payoffs
[ ] Claude performs directionally-correct-but-bad-structure challenge
[ ] One closed trade produces a structured StrategyLesson
[ ] Pre-Market Strategist runs once and stores its brief
[ ] Alpha Evolution can return NO CHANGE when evidence is insufficient
[ ] Alpha Evolution can propose one bounded Challenger when evidence is sufficient
[ ] Immutable risk/execution paths are programmatically blocked
[ ] Challenger runs shadow-only
[ ] Champion and Challenger have deterministic performance metrics
[ ] Competition promotion requires operator approval
[ ] Dashboard or CLI can show Champion vs Challenger
[ ] All strategy versions/config snapshots are auditable
```

---

# 30. Definition of Done - Post-Competition Version

```text
[ ] Challenger can shadow at least 25 closed hypothetical trades
[ ] Holdout period implemented
[ ] Promotion rules deterministic and tested
[ ] Hysteresis/version-lifetime rules implemented
[ ] Rollback/version history implemented
[ ] Operator approval workflow implemented
[ ] Optional auto-promotion remains off until separately enabled
[ ] Strategy learning can run for multiple weeks without intraday mutation
```

---

# 31. Master Claude Code / Codex Instruction

Paste this at the start of an implementation session:

```text
You are extending the existing Alpha Council v2.4 repository with the Alpha Council v2.5
Alpha Evolution addendum.

The v2.4 core trading system remains authoritative for discovery, scoring, options,
risk, execution, journaling, and paper-only safety. This addendum adds generative
scenario reasoning and slow Champion/Challenger strategy evolution.

PRIORITY: Do not delay or destabilize the core autonomous trading loop to implement
Alpha Evolution.

For the next incomplete item in Section 18:
1. inspect BUILD_STATUS.md and the existing repository;
2. preserve working v2.4 components;
3. implement the smallest complete change;
4. add focused tests;
5. run relevant tests;
6. report files changed, tests run, API/model behavior, and unresolved issues;
7. stop before broadening scope unless asked.

NON-NEGOTIABLE RULES:
- Generative AI may propose; deterministic code decides truth/permission/execution.
- Only the Champion may submit Alpaca orders.
- Challengers are shadow-only during the competition.
- No intraday self-modification.
- Alpha Evolution cannot change immutable Risk Constitution or execution-safety paths.
- No LLM may invent prices, contracts, Greeks, fills, P&L, or performance metrics.
- Every lesson, Challenger, strategy version, shadow decision, and promotion decision
  must be persisted and auditable.
- Prefer NO CHANGE when evidence is weak.
- Maximum 3 bounded changes per Challenger.
- During competition mode, promotion requires explicit operator approval.
- Do not add a vector DB, RL, fine-tuning, or autonomous code rewriting.
```

---

# 32. Immediate Recommendation

Given the current build schedule, Claude should **not** begin Alpha Evolution first.

The implementation sequence should be:

```text
1. Finish v2.4 scoring
2. Finish existing GPT/Claude Council
3. Finish minimal journal
4. Finish orchestrator + position monitor
5. Verify first calibration trade
6. Verify first end-to-end alpha trade
7. Add Scenario Generator
8. Add Post-Trade Lessons
9. Add Pre-Market Strategist
10. Add Alpha Evolution Challenger generator
11. Add Champion/Challenger shadow comparison
12. Add dashboard presentation
```

This keeps the project aligned with the actual judging priority:

> **First make Alpha Council trade well. Then make it visibly learn how to become better.**

---

*Alpha Council v2.5 - Alpha Evolution Implementation Addendum. Extends Alpha Council v2.4 Dynamic-Discovery Edition.*
