-- ======================================================================
-- Alpha Council v2.5 - consolidated SQLite schema
-- Place at: alpha_council/db/schema.sql
--
-- Single source of truth. Applying this file alone produces a complete,
-- current database. scripts/migrate_v24.py remains only for upgrading a
-- database created before this consolidation; a fresh init_db --reset no
-- longer needs a follow-up migration.
--
-- All timestamps are UTC ISO-8601 TEXT. Convert to America/New_York only
-- at display time.
--
-- Design notes worth keeping in view:
--   * intelligence_items dedupes per SOURCE, not globally, so two outlets
--     publishing identical text both persist and corroboration can exceed
--     one cluster.
--   * option_structures.decision_id is NULLABLE: the options engine runs
--     during the scan, before a decision exists.
--   * every decision-bearing row carries config_version, so results stay
--     attributable to the settings that produced them.
-- ======================================================================

PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;

-- ----------------------------------------------------------------------
-- metadata and configuration
-- ----------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS schema_meta (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS system_state (
    key         TEXT PRIMARY KEY,
    value_json  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS config_versions (
    config_version  TEXT PRIMARY KEY,
    activated_at    TEXT NOT NULL,
    deactivated_at  TEXT,
    tier            INTEGER NOT NULL CHECK(tier IN (1,2,3)),
    scoring_json    TEXT NOT NULL,
    risk_json       TEXT NOT NULL,
    note            TEXT NOT NULL
);

-- ----------------------------------------------------------------------
-- market data
-- ----------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS market_observations (
    observation_id    TEXT PRIMARY KEY,
    symbol            TEXT NOT NULL,
    asset_type        TEXT NOT NULL CHECK(asset_type IN ('EQUITY','ETF','OPTION')),
    source            TEXT NOT NULL,
    observed_at       TEXT NOT NULL,
    source_timestamp  TEXT,
    quote_lag_seconds REAL,
    bid REAL, ask REAL, last REAL, mark REAL, volume REAL,
    open REAL, high REAL, low REAL, close REAL,
    raw_json          TEXT NOT NULL DEFAULT '{}',
    UNIQUE(symbol, source, observed_at)
);
CREATE INDEX IF NOT EXISTS idx_market_obs_symbol_time
    ON market_observations(symbol, observed_at DESC);

CREATE TABLE IF NOT EXISTS market_bars (
    symbol      TEXT NOT NULL,
    source      TEXT NOT NULL,
    timeframe   TEXT NOT NULL,
    ts          TEXT NOT NULL,
    open        REAL NOT NULL,
    high        REAL NOT NULL,
    low         REAL NOT NULL,
    close       REAL NOT NULL,
    volume      REAL NOT NULL,
    vwap        REAL,
    trade_count INTEGER,
    raw_json    TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY(symbol, source, timeframe, ts)
);
CREATE INDEX IF NOT EXISTS idx_market_bars_lookup
    ON market_bars(symbol, timeframe, ts DESC);

CREATE TABLE IF NOT EXISTS data_quality (
    quality_id                TEXT PRIMARY KEY,
    symbol                    TEXT NOT NULL,
    asset_type                TEXT NOT NULL CHECK(asset_type IN ('EQUITY','ETF','OPTION')),
    evaluated_at              TEXT NOT NULL,
    source                    TEXT NOT NULL,
    quote_timestamp           TEXT,
    quote_lag_seconds         REAL,
    bid REAL, ask REAL,
    raw_mid                   REAL,
    adjusted_mid              REAL,
    underlying_move           REAL,
    spread_pct                REAL,
    confidence                TEXT NOT NULL
        CHECK(confidence IN ('HIGH','MEDIUM','DEGRADED','BLOCKED')),
    confidence_factor         REAL NOT NULL CHECK(confidence_factor BETWEEN 0 AND 1),
    signal_price              REAL,
    execution_reference_price REAL,
    reason                    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_quality_symbol_time
    ON data_quality(symbol, evaluated_at DESC);

-- ----------------------------------------------------------------------
-- discovery
-- ----------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS discovery_candidates (
    discovery_id     TEXT PRIMARY KEY,
    scan_id          TEXT NOT NULL,
    symbol           TEXT NOT NULL,
    discovered_at    TEXT NOT NULL,
    expires_at       TEXT,
    source           TEXT NOT NULL,
    source_rank      INTEGER,
    discovery_reason TEXT NOT NULL,
    is_core          INTEGER NOT NULL CHECK(is_core IN (0,1)),
    asset_tradable   INTEGER NOT NULL CHECK(asset_tradable IN (0,1)),
    has_options      INTEGER NOT NULL CHECK(has_options IN (0,1)),
    data_density_ok  INTEGER NOT NULL CHECK(data_density_ok IN (0,1)),
    fast_score       REAL NOT NULL DEFAULT 0,
    discovery_boost  REAL NOT NULL DEFAULT 0,
    UNIQUE(scan_id, symbol, source)
);
CREATE INDEX IF NOT EXISTS idx_discovery_scan_score
    ON discovery_candidates(scan_id, fast_score DESC);
CREATE INDEX IF NOT EXISTS idx_discovery_symbol_time
    ON discovery_candidates(symbol, discovered_at DESC);
CREATE INDEX IF NOT EXISTS idx_discovery_source
    ON discovery_candidates(source, discovered_at DESC);

CREATE TABLE IF NOT EXISTS discovery_source_status (
    status_id           TEXT PRIMARY KEY,
    session_date        TEXT NOT NULL,
    source              TEXT NOT NULL,
    enabled             INTEGER NOT NULL CHECK(enabled IN (0,1)),
    probed_at           TEXT,
    disabled_at         TEXT,
    disable_reason      TEXT,
    symbols_contributed INTEGER NOT NULL DEFAULT 0,
    consecutive_errors  INTEGER NOT NULL DEFAULT 0,
    UNIQUE(session_date, source)
);

CREATE TABLE IF NOT EXISTS funnel_snapshots (
    scan_id              TEXT PRIMARY KEY,
    as_of                TEXT NOT NULL,
    discovery_count      INTEGER NOT NULL,
    stage0_survivors     INTEGER NOT NULL,
    prescore_survivors   INTEGER NOT NULL,
    options_prescreened  INTEGER NOT NULL,
    final_candidates     INTEGER NOT NULL,
    councils_started     INTEGER NOT NULL,
    event_track_count    INTEGER NOT NULL DEFAULT 0,
    momentum_track_count INTEGER NOT NULL DEFAULT 0,
    source_counts_json   TEXT NOT NULL DEFAULT '{}'
);

-- ----------------------------------------------------------------------
-- intelligence
-- ----------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS source_registry (
    source_id        TEXT PRIMARY KEY,
    name             TEXT NOT NULL,
    domain           TEXT,
    source_type      TEXT NOT NULL,
    tier             TEXT NOT NULL,
    base_reliability REAL NOT NULL CHECK(base_reliability BETWEEN 0 AND 100),
    collector        TEXT NOT NULL,
    enabled          INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)),
    config_json      TEXT NOT NULL DEFAULT '{}',
    created_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS intelligence_items (
    item_id              TEXT PRIMARY KEY,
    source_id            TEXT NOT NULL REFERENCES source_registry(source_id),
    source_native_id     TEXT,
    source_tier          TEXT NOT NULL,
    retrieved_at         TEXT NOT NULL,
    published_at         TEXT,
    updated_at           TEXT,
    url                  TEXT,
    canonical_url        TEXT,
    title                TEXT NOT NULL,
    summary              TEXT,
    content_text         TEXT,
    content_hash         TEXT NOT NULL,
    duplicate_cluster_id TEXT,
    ingest_status        TEXT NOT NULL DEFAULT 'NEW',
    raw_json             TEXT NOT NULL DEFAULT '{}',
    UNIQUE(source_id, source_native_id),
    UNIQUE(source_id, content_hash)
);
CREATE INDEX IF NOT EXISTS idx_intel_items_time
    ON intelligence_items(published_at DESC, retrieved_at DESC);
CREATE INDEX IF NOT EXISTS idx_intel_items_cluster
    ON intelligence_items(duplicate_cluster_id);
CREATE INDEX IF NOT EXISTS idx_intel_items_hash
    ON intelligence_items(content_hash);

CREATE TABLE IF NOT EXISTS intelligence_events (
    event_id                  TEXT PRIMARY KEY,
    item_id                   TEXT NOT NULL REFERENCES intelligence_items(item_id),
    symbol                    TEXT NOT NULL,
    event_type                TEXT NOT NULL,
    direction                 TEXT NOT NULL CHECK(direction IN ('BULLISH','BEARISH','NEUTRAL')),
    direction_confidence      REAL NOT NULL,
    source_reliability_score  REAL NOT NULL,
    freshness_score           REAL NOT NULL,
    novelty_score             REAL NOT NULL,
    corroboration_score       REAL NOT NULL,
    materiality_score         REAL NOT NULL,
    surprise_score            REAL NOT NULL,
    market_confirmation_score REAL NOT NULL,
    catalyst_score            REAL NOT NULL,
    provisional               INTEGER NOT NULL DEFAULT 0 CHECK(provisional IN (0,1)),
    extracted_facts_json      TEXT NOT NULL DEFAULT '[]',
    evidence_urls_json        TEXT NOT NULL DEFAULT '[]',
    created_at                TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_intel_events_symbol_time
    ON intelligence_events(symbol, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_intel_events_score
    ON intelligence_events(catalyst_score DESC, created_at DESC);

-- ----------------------------------------------------------------------
-- scanning and candidates
-- ----------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS scan_runs (
    scan_id         TEXT PRIMARY KEY,
    mode            TEXT NOT NULL,
    config_version  TEXT,
    started_at      TEXT NOT NULL,
    completed_at    TEXT,
    universe_size   INTEGER NOT NULL DEFAULT 0,
    candidate_count INTEGER NOT NULL DEFAULT 0,
    status          TEXT NOT NULL,
    error           TEXT
);

CREATE TABLE IF NOT EXISTS candidate_scores (
    candidate_id              TEXT PRIMARY KEY,
    scan_id                   TEXT NOT NULL REFERENCES scan_runs(scan_id),
    config_version            TEXT,
    symbol                    TEXT NOT NULL,
    direction                 TEXT NOT NULL,
    as_of                     TEXT NOT NULL,
    momentum_score            REAL NOT NULL,
    relative_volume_score     REAL NOT NULL,
    trend_regime_score        REAL NOT NULL,
    relative_strength_score   REAL NOT NULL,
    options_opportunity_score REAL NOT NULL DEFAULT 0,
    options_liquidity_score   REAL NOT NULL DEFAULT 0,
    catalyst_score            REAL NOT NULL DEFAULT 0,
    corroboration_score       REAL NOT NULL DEFAULT 0,
    novelty_score             REAL NOT NULL DEFAULT 0,
    data_confidence_factor    REAL NOT NULL,
    regime_factor             REAL NOT NULL,
    event_risk_factor         REAL NOT NULL,
    fast_score                REAL DEFAULT 0,
    pre_score                 REAL NOT NULL,
    raw_opportunity_score     REAL NOT NULL DEFAULT 0,
    final_opportunity_score   REAL NOT NULL DEFAULT 0,
    discovery_source          TEXT,
    candidate_track           TEXT,
    key_metrics_json          TEXT NOT NULL DEFAULT '{}',
    created_at                TEXT NOT NULL,
    UNIQUE(scan_id, symbol)
);
CREATE INDEX IF NOT EXISTS idx_candidates_rank
    ON candidate_scores(scan_id, final_opportunity_score DESC);

-- ----------------------------------------------------------------------
-- decisions and agents
-- ----------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS decisions (
    decision_id      TEXT PRIMARY KEY,
    candidate_id     TEXT REFERENCES candidate_scores(candidate_id),
    config_version   TEXT,
    strategy_id      TEXT,
    symbol           TEXT NOT NULL,
    state            TEXT NOT NULL,
    discovery_source TEXT,
    candidate_track  TEXT,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_decisions_state
    ON decisions(state, updated_at DESC);

CREATE TABLE IF NOT EXISTS agent_runs (
    run_id        TEXT PRIMARY KEY,
    decision_id   TEXT REFERENCES decisions(decision_id),
    agent_name    TEXT NOT NULL,
    provider      TEXT NOT NULL,
    model         TEXT NOT NULL,
    purpose       TEXT NOT NULL,
    started_at    TEXT NOT NULL,
    completed_at  TEXT,
    input_hash    TEXT NOT NULL,
    prompt_text   TEXT,
    output_json   TEXT,
    input_tokens  INTEGER,
    output_tokens INTEGER,
    cost_usd      REAL,
    status        TEXT NOT NULL,
    error         TEXT
);
CREATE INDEX IF NOT EXISTS idx_agent_runs_decision
    ON agent_runs(decision_id, started_at);

CREATE TABLE IF NOT EXISTS trade_proposals (
    proposal_id                  TEXT PRIMARY KEY,
    decision_id                  TEXT NOT NULL REFERENCES decisions(decision_id),
    revision                     INTEGER NOT NULL CHECK(revision IN (0,1)),
    symbol                       TEXT NOT NULL,
    trade                        INTEGER NOT NULL CHECK(trade IN (0,1)),
    direction                    TEXT NOT NULL,
    confidence                   REAL NOT NULL,
    expected_horizon_days        INTEGER NOT NULL,
    desired_portfolio_risk_pct   REAL NOT NULL,
    thesis                       TEXT NOT NULL,
    catalyst_summary             TEXT NOT NULL,
    key_supporting_evidence_json TEXT NOT NULL DEFAULT '[]',
    key_contrary_evidence_json   TEXT NOT NULL DEFAULT '[]',
    invalidation_json            TEXT NOT NULL DEFAULT '[]',
    selected_structure_rank      INTEGER,
    abstain_reason               TEXT,
    created_at                   TEXT NOT NULL,
    UNIQUE(decision_id, revision)
);

CREATE TABLE IF NOT EXISTS option_structures (
    structure_id          TEXT PRIMARY KEY,
    decision_id           TEXT REFERENCES decisions(decision_id),
    candidate_id          TEXT REFERENCES candidate_scores(candidate_id),
    rank                  INTEGER NOT NULL CHECK(rank BETWEEN 1 AND 5),
    symbol                TEXT NOT NULL,
    strategy              TEXT NOT NULL
        CHECK(strategy IN ('BULL_CALL_DEBIT','BEAR_PUT_DEBIT')),
    expiration            TEXT NOT NULL,
    dte                   INTEGER NOT NULL,

    long_symbol           TEXT NOT NULL,
    long_strike           REAL NOT NULL,
    long_delta            REAL NOT NULL,
    long_bid              REAL NOT NULL,
    long_ask              REAL NOT NULL,
    long_raw_mid          REAL NOT NULL,
    long_adjusted_mid     REAL NOT NULL,

    short_symbol          TEXT NOT NULL,
    short_strike          REAL NOT NULL,
    short_delta           REAL NOT NULL,
    short_bid             REAL NOT NULL,
    short_ask             REAL NOT NULL,
    short_raw_mid         REAL NOT NULL,
    short_adjusted_mid    REAL NOT NULL,

    net_delta             REAL NOT NULL,
    width                 REAL NOT NULL CHECK(width > 0),
    raw_mid_debit         REAL NOT NULL,
    adjusted_mid_debit    REAL NOT NULL,
    natural_debit         REAL NOT NULL,
    staleness_buffer      REAL NOT NULL DEFAULT 0,
    indicative_buffer     REAL DEFAULT 0,
    initial_limit_debit   REAL NOT NULL CHECK(initial_limit_debit > 0),
    cost_to_width_ratio   REAL NOT NULL,
    max_loss_per_spread   REAL NOT NULL CHECK(max_loss_per_spread > 0),
    max_profit_per_spread REAL NOT NULL,
    reward_risk_ratio     REAL NOT NULL,
    breakeven             REAL NOT NULL,

    max_quote_lag_seconds REAL NOT NULL DEFAULT 0,
    underlying_price      REAL,
    underlying_move       REAL,
    stale_adjusted        INTEGER NOT NULL DEFAULT 0 CHECK(stale_adjusted IN (0,1)),

    liquidity_score       REAL NOT NULL,
    delta_fit_score       REAL NOT NULL,
    dte_fit_score         REAL NOT NULL,
    cost_efficiency_score REAL NOT NULL,
    structure_score       REAL NOT NULL,

    raw_json              TEXT NOT NULL DEFAULT '{}',
    created_at            TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_structures_decision
    ON option_structures(decision_id, rank);
CREATE INDEX IF NOT EXISTS idx_structures_candidate
    ON option_structures(candidate_id, rank);

CREATE TABLE IF NOT EXISTS red_team_reviews (
    review_id                   TEXT PRIMARY KEY,
    decision_id                 TEXT NOT NULL REFERENCES decisions(decision_id),
    proposal_id                 TEXT REFERENCES trade_proposals(proposal_id),
    verdict                     TEXT NOT NULL CHECK(verdict IN ('PASS','MODIFY','VETO')),
    risk_score                  INTEGER NOT NULL,
    fatal_flaw                  INTEGER NOT NULL CHECK(fatal_flaw IN (0,1)),
    confidence_adjustment       REAL NOT NULL,
    recommended_max_risk_pct    REAL NOT NULL,
    problems_json               TEXT NOT NULL DEFAULT '[]',
    strongest_counterargument   TEXT NOT NULL,
    information_to_reverse_json TEXT NOT NULL DEFAULT '[]',
    summary                     TEXT NOT NULL,
    created_at                  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS risk_evaluations (
    risk_evaluation_id        TEXT PRIMARY KEY,
    decision_id               TEXT NOT NULL REFERENCES decisions(decision_id),
    proposal_id               TEXT REFERENCES trade_proposals(proposal_id),
    structure_id              TEXT REFERENCES option_structures(structure_id),
    config_version            TEXT,
    evaluated_at              TEXT NOT NULL,
    decision                  TEXT NOT NULL
        CHECK(decision IN ('APPROVE','RESIZE','REJECT','HALT')),
    account_equity            REAL NOT NULL,
    requested_qty             INTEGER NOT NULL,
    approved_qty              INTEGER NOT NULL,
    requested_max_loss        REAL NOT NULL,
    approved_max_loss         REAL NOT NULL,
    total_open_risk_pct_after REAL NOT NULL,
    sector_risk_pct_after     REAL NOT NULL,
    daily_drawdown_pct        REAL NOT NULL,
    competition_drawdown_pct  REAL NOT NULL,
    violations_json           TEXT NOT NULL DEFAULT '[]'
);

-- ----------------------------------------------------------------------
-- execution
-- ----------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS orders (
    order_pk        TEXT PRIMARY KEY,
    decision_id     TEXT NOT NULL REFERENCES decisions(decision_id),
    structure_id    TEXT,
    client_order_id TEXT NOT NULL UNIQUE,
    alpaca_order_id TEXT,
    intent          TEXT NOT NULL DEFAULT 'OPEN' CHECK(intent IN ('OPEN','CLOSE')),
    status          TEXT NOT NULL,
    qty             INTEGER NOT NULL,
    limit_price     REAL NOT NULL,
    attempt         INTEGER NOT NULL DEFAULT 1,
    limit_walk_step INTEGER DEFAULT 1,
    submitted_at    TEXT,
    updated_at      TEXT NOT NULL,
    raw_json        TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_orders_status
    ON orders(status, updated_at DESC);

CREATE TABLE IF NOT EXISTS fills (
    fill_id            TEXT PRIMARY KEY,
    order_pk           TEXT NOT NULL REFERENCES orders(order_pk),
    alpaca_activity_id TEXT UNIQUE,
    symbol             TEXT NOT NULL,
    side               TEXT NOT NULL,
    qty                REAL NOT NULL,
    price              REAL NOT NULL,
    filled_at          TEXT NOT NULL,
    raw_json           TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS position_snapshots (
    snapshot_id     TEXT PRIMARY KEY,
    captured_at     TEXT NOT NULL,
    symbol          TEXT NOT NULL,
    qty             REAL NOT NULL,
    market_value    REAL,
    cost_basis      REAL,
    unrealized_pl   REAL,
    unrealized_plpc REAL,
    raw_json        TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_position_snapshots
    ON position_snapshots(captured_at DESC, symbol);

CREATE TABLE IF NOT EXISTS execution_calibrations (
    calibration_id          TEXT PRIMARY KEY,
    decision_id             TEXT NOT NULL,
    symbol                  TEXT NOT NULL,
    side                    TEXT NOT NULL CHECK(side IN ('OPEN','CLOSE')),
    candidate_track         TEXT NOT NULL,
    direction               TEXT NOT NULL,
    submitted_at            TEXT NOT NULL,
    filled_at               TEXT,
    indicative_raw_mid      REAL NOT NULL,
    indicative_adjusted_mid REAL NOT NULL,
    natural_debit_estimate  REAL NOT NULL,
    initial_limit_debit     REAL NOT NULL,
    final_submitted_limit   REAL NOT NULL,
    actual_fill_debit       REAL,
    seconds_to_fill         REAL,
    limit_walk_steps        INTEGER NOT NULL DEFAULT 0,
    quote_lag_seconds       REAL NOT NULL,
    underlying_at_quote     REAL NOT NULL,
    underlying_at_submit    REAL NOT NULL,
    underlying_at_fill      REAL,
    fill_bias_vs_adjusted   REAL,
    fill_bias_vs_limit      REAL,
    fill_slippage_pct       REAL
);
CREATE INDEX IF NOT EXISTS idx_exec_cal_symbol_time
    ON execution_calibrations(symbol, submitted_at DESC);
CREATE INDEX IF NOT EXISTS idx_exec_cal_side
    ON execution_calibrations(side, submitted_at DESC);

CREATE TABLE IF NOT EXISTS fill_bias_estimates (
    estimate_id            TEXT PRIMARY KEY,
    computed_at            TEXT NOT NULL,
    side                   TEXT NOT NULL CHECK(side IN ('OPEN','CLOSE')),
    direction              TEXT,
    sample_size            INTEGER NOT NULL,
    median_bias            REAL NOT NULL DEFAULT 0,
    p80_bias               REAL NOT NULL DEFAULT 0,
    median_seconds_to_fill REAL,
    mean_limit_walk_steps  REAL NOT NULL DEFAULT 0,
    applied_buffer         REAL NOT NULL DEFAULT 0
);

-- ----------------------------------------------------------------------
-- journal, shadows, attribution
-- ----------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS trade_journal (
    trade_id            TEXT PRIMARY KEY,
    decision_id         TEXT NOT NULL UNIQUE REFERENCES decisions(decision_id),
    opened_at           TEXT,
    closed_at           TEXT,
    status              TEXT NOT NULL,
    qty                 INTEGER NOT NULL DEFAULT 0,
    entry_debit         REAL,
    exit_credit         REAL,
    realized_pnl        REAL,
    realized_return_pct REAL,
    candidate_track     TEXT,
    thesis              TEXT NOT NULL,
    invalidation_json   TEXT NOT NULL DEFAULT '[]',
    exit_reason         TEXT,
    lesson              TEXT
);
CREATE INDEX IF NOT EXISTS idx_trade_journal_status
    ON trade_journal(status, opened_at DESC);

CREATE TABLE IF NOT EXISTS shadow_trades (
    shadow_id             TEXT PRIMARY KEY,
    decision_id           TEXT NOT NULL REFERENCES decisions(decision_id),
    variant               TEXT NOT NULL
        CHECK(variant IN ('GPT_ORIGINAL','CLAUDE_MODIFIED','EXECUTED')),
    structure_json        TEXT NOT NULL,
    qty                   INTEGER NOT NULL,
    entry_timestamp       TEXT NOT NULL,
    entry_reference_debit REAL NOT NULL,
    close_policy_json     TEXT NOT NULL DEFAULT '{}',
    status                TEXT NOT NULL,
    UNIQUE(decision_id, variant)
);

CREATE TABLE IF NOT EXISTS shadow_marks (
    shadow_mark_id    TEXT PRIMARY KEY,
    shadow_id         TEXT NOT NULL,
    marked_at         TEXT NOT NULL,
    mark_debit        REAL NOT NULL,
    unrealized_pnl    REAL NOT NULL,
    mark_method       TEXT NOT NULL DEFAULT 'ADJUSTED_MID',
    quote_lag_seconds REAL,
    source            TEXT NOT NULL,
    raw_json          TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_shadow_marks
    ON shadow_marks(shadow_id, marked_at DESC);

CREATE TABLE IF NOT EXISTS decision_attribution (
    attribution_id                 TEXT PRIMARY KEY,
    decision_id                    TEXT NOT NULL REFERENCES decisions(decision_id),
    as_of                          TEXT NOT NULL,
    gpt_original_pnl               REAL NOT NULL,
    claude_modified_pnl            REAL NOT NULL,
    executed_pnl                   REAL NOT NULL,
    gpt_original_pnl_per_spread    REAL NOT NULL DEFAULT 0,
    claude_modified_pnl_per_spread REAL NOT NULL DEFAULT 0,
    executed_pnl_per_spread        REAL NOT NULL DEFAULT 0,
    gpt_original_qty               INTEGER NOT NULL DEFAULT 0,
    claude_modified_qty            INTEGER NOT NULL DEFAULT 0,
    executed_qty                   INTEGER NOT NULL DEFAULT 0,
    claude_selection_effect        REAL NOT NULL DEFAULT 0,
    claude_sizing_effect           REAL NOT NULL DEFAULT 0,
    risk_selection_effect          REAL NOT NULL DEFAULT 0,
    risk_sizing_effect             REAL NOT NULL DEFAULT 0,
    claude_value_added             REAL NOT NULL,
    risk_constitution_value_added  REAL NOT NULL,
    mark_method                    TEXT NOT NULL DEFAULT 'ADJUSTED_MID',
    notes_json                     TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_attribution_decision
    ON decision_attribution(decision_id, as_of DESC);

-- ----------------------------------------------------------------------
-- gate rejection log
-- ----------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS gate_rejections (
    rejection_id          TEXT PRIMARY KEY,
    occurred_at           TEXT NOT NULL,
    config_version        TEXT NOT NULL REFERENCES config_versions(config_version),
    scan_id               TEXT,
    decision_id           TEXT,
    symbol                TEXT NOT NULL,
    direction             TEXT NOT NULL,
    stage                 TEXT NOT NULL,
    gate_id               TEXT NOT NULL,
    observed_value        TEXT,
    threshold_value       TEXT,
    tier                  INTEGER NOT NULL CHECK(tier IN (1,2,3)),
    hard_gate             INTEGER NOT NULL CHECK(hard_gate IN (0,1)),
    shadow_eligible       INTEGER NOT NULL DEFAULT 0 CHECK(shadow_eligible IN (0,1)),
    shadow_structure_json TEXT,
    note                  TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_gate_rejections_gate
    ON gate_rejections(gate_id, occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_gate_rejections_symbol
    ON gate_rejections(symbol, occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_gate_rejections_stage
    ON gate_rejections(stage, occurred_at DESC);

CREATE TABLE IF NOT EXISTS rejected_shadows (
    rejected_shadow_id    TEXT PRIMARY KEY,
    rejection_id          TEXT NOT NULL REFERENCES gate_rejections(rejection_id),
    symbol                TEXT NOT NULL,
    structure_json        TEXT NOT NULL,
    entry_timestamp       TEXT NOT NULL,
    entry_reference_debit REAL NOT NULL,
    horizon_end           TEXT NOT NULL,
    status                TEXT NOT NULL DEFAULT 'OPEN',
    last_mark_debit       REAL,
    last_marked_at        TEXT,
    final_pnl_per_spread  REAL,
    mark_method           TEXT NOT NULL DEFAULT 'ADJUSTED_MID'
);
CREATE INDEX IF NOT EXISTS idx_rejected_shadows_status
    ON rejected_shadows(status, horizon_end);

-- ----------------------------------------------------------------------
-- Alpha Evolution (v2.5) - schema only; logic lands after the core loop
-- ----------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS scenario_sets (
    scenario_set_id     TEXT PRIMARY KEY,
    decision_id         TEXT NOT NULL,
    symbol              TEXT NOT NULL,
    generated_at        TEXT NOT NULL,
    overall_uncertainty TEXT NOT NULL,
    scenarios_json      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_scenario_sets_decision
    ON scenario_sets(decision_id, generated_at DESC);

CREATE TABLE IF NOT EXISTS scenario_payoffs (
    payoff_id       TEXT PRIMARY KEY,
    decision_id     TEXT NOT NULL,
    structure_id    TEXT NOT NULL,
    scenario_type   TEXT NOT NULL,
    underlying_low  REAL NOT NULL,
    underlying_mid  REAL NOT NULL,
    underlying_high REAL NOT NULL,
    pnl_low         REAL NOT NULL,
    pnl_mid         REAL NOT NULL,
    pnl_high        REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_scenario_payoff_structure
    ON scenario_payoffs(structure_id, scenario_type);

CREATE TABLE IF NOT EXISTS premarket_briefs (
    brief_id     TEXT PRIMARY KEY,
    session_date TEXT NOT NULL UNIQUE,
    generated_at TEXT NOT NULL,
    model        TEXT NOT NULL,
    output_json  TEXT NOT NULL,
    input_hash   TEXT NOT NULL,
    cost_usd     REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS strategy_lessons (
    lesson_id              TEXT PRIMARY KEY,
    source_decision_id     TEXT,
    lesson_type            TEXT NOT NULL,
    created_at             TEXT NOT NULL,
    observation            TEXT NOT NULL,
    explanation_hypothesis TEXT NOT NULL,
    evidence_for_json      TEXT NOT NULL DEFAULT '[]',
    evidence_against_json  TEXT NOT NULL DEFAULT '[]',
    sample_size            INTEGER NOT NULL,
    confidence             TEXT NOT NULL CHECK(confidence IN ('LOW','MEDIUM','HIGH')),
    proposed_test          TEXT NOT NULL,
    recommends_change      INTEGER NOT NULL CHECK(recommends_change IN (0,1))
);
CREATE INDEX IF NOT EXISTS idx_strategy_lessons_time
    ON strategy_lessons(created_at DESC, lesson_type);

CREATE TABLE IF NOT EXISTS strategy_versions (
    strategy_id        TEXT PRIMARY KEY,
    parent_strategy_id TEXT,
    status             TEXT NOT NULL CHECK(status IN ('CHAMPION','CHALLENGER','RETIRED')),
    created_at         TEXT NOT NULL,
    promoted_at        TEXT,
    retired_at         TEXT,
    config_version     TEXT NOT NULL,
    config_json        TEXT NOT NULL,
    hypothesis         TEXT,
    operator_approved  INTEGER NOT NULL DEFAULT 0 CHECK(operator_approved IN (0,1))
);
CREATE INDEX IF NOT EXISTS idx_strategy_versions_status
    ON strategy_versions(status, created_at DESC);

CREATE TABLE IF NOT EXISTS challenger_proposals (
    challenger_id               TEXT PRIMARY KEY,
    parent_champion_id          TEXT NOT NULL,
    created_at                  TEXT NOT NULL,
    hypothesis                  TEXT NOT NULL,
    evidence_summary_json       TEXT NOT NULL DEFAULT '[]',
    changes_json                TEXT NOT NULL DEFAULT '[]',
    expected_benefit            TEXT NOT NULL,
    expected_failure_mode       TEXT NOT NULL,
    minimum_shadow_observations INTEGER NOT NULL,
    confidence                  TEXT NOT NULL,
    status                      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS strategy_shadow_decisions (
    shadow_decision_id    TEXT PRIMARY KEY,
    source_decision_id    TEXT NOT NULL,
    strategy_id           TEXT NOT NULL,
    evaluated_at          TEXT NOT NULL,
    would_trade           INTEGER NOT NULL CHECK(would_trade IN (0,1)),
    selected_structure_id TEXT,
    requested_risk_pct    REAL,
    hypothetical_qty      INTEGER,
    rationale_json        TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_strategy_shadow_strategy
    ON strategy_shadow_decisions(strategy_id, evaluated_at DESC);

CREATE TABLE IF NOT EXISTS strategy_performance_snapshots (
    snapshot_id           TEXT PRIMARY KEY,
    strategy_id           TEXT NOT NULL,
    as_of                 TEXT NOT NULL,
    observations          INTEGER NOT NULL,
    closed_trades         INTEGER NOT NULL,
    total_pnl             REAL NOT NULL,
    return_pct            REAL NOT NULL,
    win_rate              REAL,
    expectancy            REAL,
    max_drawdown_pct      REAL NOT NULL,
    average_win           REAL,
    average_loss          REAL,
    profit_factor         REAL,
    event_pnl             REAL NOT NULL DEFAULT 0,
    momentum_pnl          REAL NOT NULL DEFAULT 0,
    execution_bias_mean   REAL,
    execution_bias_median REAL,
    metrics_json          TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_strategy_perf
    ON strategy_performance_snapshots(strategy_id, as_of DESC);

CREATE TABLE IF NOT EXISTS promotion_recommendations (
    recommendation_id          TEXT PRIMARY KEY,
    champion_id                TEXT NOT NULL,
    challenger_id              TEXT NOT NULL,
    generated_at               TEXT NOT NULL,
    recommendation             TEXT NOT NULL,
    evidence_strength          TEXT NOT NULL,
    reasons_json               TEXT NOT NULL DEFAULT '[]',
    failed_rules_json          TEXT NOT NULL DEFAULT '[]',
    operator_approval_required INTEGER NOT NULL DEFAULT 1
        CHECK(operator_approval_required IN (0,1)),
    approved_by_operator       INTEGER CHECK(approved_by_operator IN (0,1)),
    approved_at                TEXT
);

-- ----------------------------------------------------------------------
-- observability
-- ----------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS api_usage (
    usage_id      TEXT PRIMARY KEY,
    provider      TEXT NOT NULL,
    model         TEXT NOT NULL,
    endpoint      TEXT NOT NULL,
    occurred_at   TEXT NOT NULL,
    decision_id   TEXT,
    request_id    TEXT,
    input_tokens  INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cached_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd      REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_api_usage_provider_time
    ON api_usage(provider, occurred_at DESC);

CREATE TABLE IF NOT EXISTS system_events (
    system_event_id TEXT PRIMARY KEY,
    occurred_at     TEXT NOT NULL,
    level           TEXT NOT NULL,
    component       TEXT NOT NULL,
    event_type      TEXT NOT NULL,
    decision_id     TEXT,
    message         TEXT NOT NULL,
    context_json    TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_system_events_time
    ON system_events(occurred_at DESC, level);

-- ----------------------------------------------------------------------
-- views
-- ----------------------------------------------------------------------

CREATE VIEW IF NOT EXISTS v_gate_histogram AS
SELECT stage, gate_id, tier, hard_gate,
       COUNT(*) AS rejections,
       COUNT(DISTINCT symbol) AS distinct_symbols,
       MAX(occurred_at) AS last_seen
FROM gate_rejections
GROUP BY stage, gate_id, tier, hard_gate
ORDER BY rejections DESC;

CREATE VIEW IF NOT EXISTS v_gate_value AS
SELECT g.gate_id, g.stage,
       COUNT(r.rejected_shadow_id) AS shadow_n,
       ROUND(AVG(r.final_pnl_per_spread), 2) AS avg_blocked_pnl_per_spread,
       ROUND(-1 * AVG(r.final_pnl_per_spread), 2) AS gate_value
FROM gate_rejections g
JOIN rejected_shadows r ON r.rejection_id = g.rejection_id
WHERE r.final_pnl_per_spread IS NOT NULL
GROUP BY g.gate_id, g.stage
ORDER BY gate_value DESC;

CREATE VIEW IF NOT EXISTS v_discovery_funnel AS
SELECT scan_id, as_of, discovery_count, stage0_survivors,
       prescore_survivors, options_prescreened, final_candidates,
       councils_started, event_track_count, momentum_track_count,
       ROUND(CAST(councils_started AS REAL)
             / NULLIF(discovery_count, 0), 5) AS survival_rate
FROM funnel_snapshots
ORDER BY as_of DESC;

CREATE VIEW IF NOT EXISTS v_fill_bias AS
SELECT side, direction,
       COUNT(*) AS n_fills,
       ROUND(AVG(fill_bias_vs_adjusted), 4) AS mean_bias,
       ROUND(AVG(fill_slippage_pct), 5) AS mean_slippage_pct,
       ROUND(AVG(seconds_to_fill), 1) AS mean_seconds_to_fill,
       ROUND(AVG(limit_walk_steps), 2) AS mean_walk_steps
FROM execution_calibrations
WHERE actual_fill_debit IS NOT NULL AND quote_lag_seconds <= 900
GROUP BY side, direction;

CREATE VIEW IF NOT EXISTS v_discovery_source_yield AS
SELECT d.source,
       COUNT(DISTINCT d.symbol) AS symbols_discovered,
       COUNT(DISTINCT c.candidate_id) AS reached_candidate,
       COUNT(DISTINCT dec.decision_id) AS reached_council
FROM discovery_candidates d
LEFT JOIN candidate_scores c
       ON c.symbol = d.symbol AND c.scan_id = d.scan_id
LEFT JOIN decisions dec ON dec.candidate_id = c.candidate_id
GROUP BY d.source
ORDER BY reached_council DESC, symbols_discovered DESC;

CREATE VIEW IF NOT EXISTS v_attribution_totals AS
SELECT COUNT(*) AS decisions,
       ROUND(SUM(claude_selection_effect), 2) AS claude_selection,
       ROUND(SUM(claude_sizing_effect), 2) AS claude_sizing,
       ROUND(SUM(claude_value_added), 2) AS claude_total,
       ROUND(SUM(risk_selection_effect), 2) AS risk_selection,
       ROUND(SUM(risk_sizing_effect), 2) AS risk_sizing,
       ROUND(SUM(risk_constitution_value_added), 2) AS risk_total,
       ROUND(SUM(executed_pnl - gpt_original_pnl), 2) AS governance_total
FROM decision_attribution;
