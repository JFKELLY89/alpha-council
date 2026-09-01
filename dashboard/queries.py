"""Read-only SQLite queries for the Alpha Council dashboard.

Every SQL statement used by the dashboard lives in this module so the
presentation layer's read-only guarantee can be audited in one place.
Public query functions return pandas DataFrames, including correctly
labelled empty DataFrames when a table has no rows yet.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Mapping
from contextlib import closing
from pathlib import Path
from typing import Any

import pandas as pd
import streamlit as st


CACHE_TTL_SECONDS = 30

# REPLACE is also a SQLite string function, so only REPLACE INTO is a write.
_FORBIDDEN_SQL = re.compile(
    r"\b(?:INSERT|UPDATE|DELETE|ALTER|CREATE|DROP|VACUUM|ATTACH|DETACH|PRAGMA)\b"
    r"|\bREPLACE\s+INTO\b",
    flags=re.IGNORECASE,
)


def _database_uri(database_path: str | Path) -> str:
    """Return a SQLite URI that cannot create or mutate the database."""
    resolved = Path(database_path).expanduser().resolve()
    return f"file:{resolved.as_posix()}?mode=ro"


def _assert_read_only(sql: str) -> None:
    """Fail closed if a dashboard query is not plainly read-only."""
    statement = sql.lstrip()
    if not statement.upper().startswith(("SELECT", "WITH")):
        raise ValueError("Dashboard SQL must begin with SELECT or WITH")
    if _FORBIDDEN_SQL.search(statement):
        raise ValueError("Dashboard SQL contains a forbidden statement")


def _read_frame(
    database_path: str | Path,
    sql: str,
    parameters: Mapping[str, Any] | None = None,
) -> pd.DataFrame:
    """Execute one guarded query using a short-lived read-only connection."""
    _assert_read_only(sql)
    with closing(
        sqlite3.connect(_database_uri(database_path), uri=True, timeout=5.0)
    ) as connection:
        connection.row_factory = sqlite3.Row
        cursor = connection.execute(sql, dict(parameters or {}))
        rows = cursor.fetchall()
        columns = [description[0] for description in cursor.description or ()]
    return pd.DataFrame.from_records(rows, columns=columns)


# ---------------------------------------------------------------------------
# Command Center
# ---------------------------------------------------------------------------

_COMMAND_CENTER_SQL = """
WITH latest_risk AS (
    SELECT
        decision,
        account_equity,
        total_open_risk_pct_after,
        sector_risk_pct_after,
        daily_drawdown_pct,
        competition_drawdown_pct,
        evaluated_at
    FROM risk_evaluations
    ORDER BY evaluated_at DESC
    LIMIT 1
),
risk_history AS (
    SELECT MAX(account_equity) AS peak_equity
    FROM risk_evaluations
),
journal_totals AS (
    SELECT
        COALESCE(SUM(realized_pnl), 0.0) AS competition_pnl,
        COALESCE(SUM(
            CASE
                WHEN closed_at >= :session_start_utc THEN realized_pnl
                ELSE 0.0
            END
        ), 0.0) AS day_pnl,
        COALESCE(SUM(
            CASE WHEN status = 'OPEN' THEN 1 ELSE 0 END
        ), 0) AS active_trade_count
    FROM trade_journal
),
latest_config AS (
    SELECT config_version, tier, activated_at, note
    FROM config_versions
    WHERE deactivated_at IS NULL
    ORDER BY activated_at DESC
    LIMIT 1
),
spend AS (
    SELECT COALESCE(SUM(cost_usd), 0.0) AS provider_spend
    FROM api_usage
),
halt_state AS (
    -- Only HALT is a system state. REJECT is a normal per-candidate
    -- outcome and must never present as a system-wide status.
    SELECT
        COUNT(*) AS halt_count,
        MAX(evaluated_at) AS last_halt_at
    FROM risk_evaluations
    WHERE decision = 'HALT'
)
SELECT
    latest_risk.account_equity,
    journal_totals.competition_pnl,
    journal_totals.day_pnl,
    risk_history.peak_equity,
    latest_risk.daily_drawdown_pct,
    latest_risk.competition_drawdown_pct,
    latest_risk.total_open_risk_pct_after,
    latest_risk.sector_risk_pct_after,
    journal_totals.active_trade_count,
    latest_config.config_version,
    latest_config.tier,
    latest_config.activated_at AS tier_activated_at,
    latest_config.note AS tier_note,
    spend.provider_spend,
    latest_risk.decision AS latest_risk_decision,
    latest_risk.evaluated_at AS latest_risk_evaluated_at,
    halt_state.halt_count,
    halt_state.last_halt_at,
    CASE
        WHEN latest_risk.decision IS NULL THEN NULL
        WHEN latest_risk.decision = 'HALT' THEN 'HALTED'
        ELSE 'GREEN'
    END AS risk_constitution_state
FROM journal_totals
CROSS JOIN risk_history
CROSS JOIN spend
CROSS JOIN halt_state
LEFT JOIN latest_risk ON 1 = 1
LEFT JOIN latest_config ON 1 = 1
"""


@st.cache_data(ttl=CACHE_TTL_SECONDS, show_spinner=False)
def get_command_center_metrics(
    database_path: str | Path, session_start_utc: str
) -> pd.DataFrame:
    """Return the Command Center headline metrics as a single row."""
    return _read_frame(
        database_path,
        _COMMAND_CENTER_SQL,
        {"session_start_utc": session_start_utc},
    )


_ACTIVE_POSITIONS_SQL = """
WITH latest_capture AS (
    SELECT MAX(captured_at) AS captured_at
    FROM position_snapshots
)
SELECT
    p.symbol,
    p.qty,
    p.market_value,
    p.cost_basis,
    p.unrealized_pl,
    p.unrealized_plpc,
    p.captured_at
FROM position_snapshots AS p
JOIN latest_capture AS latest ON latest.captured_at = p.captured_at
WHERE p.qty <> 0
ORDER BY ABS(COALESCE(p.market_value, 0)) DESC, p.symbol
"""


@st.cache_data(ttl=CACHE_TTL_SECONDS, show_spinner=False)
def get_active_positions(database_path: str | Path) -> pd.DataFrame:
    return _read_frame(database_path, _ACTIVE_POSITIONS_SQL)


_PROVIDER_SPEND_SQL = """
SELECT
    provider,
    model,
    COUNT(*) AS requests,
    SUM(input_tokens) AS input_tokens,
    SUM(output_tokens) AS output_tokens,
    ROUND(SUM(cost_usd), 4) AS cost_usd,
    MAX(occurred_at) AS last_used_at
FROM api_usage
GROUP BY provider, model
ORDER BY cost_usd DESC, provider, model
"""


@st.cache_data(ttl=CACHE_TTL_SECONDS, show_spinner=False)
def get_provider_spend(database_path: str | Path) -> pd.DataFrame:
    return _read_frame(database_path, _PROVIDER_SPEND_SQL)

_SESSION_STATUS_SQL = """
SELECT
    (SELECT MAX(started_at) FROM scan_runs) AS last_scan_at,
    (SELECT COUNT(*) FROM scan_runs
      WHERE started_at >= :session_start_utc) AS scans_today,
    (SELECT MAX(created_at) FROM decisions) AS last_decision_at,
    (SELECT COUNT(*) FROM decisions
      WHERE created_at >= :session_start_utc) AS decisions_today,
    (SELECT MAX(occurred_at) FROM system_events) AS last_event_at,
    (SELECT COUNT(*) FROM gate_rejections
      WHERE occurred_at >= :session_start_utc) AS rejections_today,
    (SELECT MAX(ts) FROM market_bars) AS last_bar_at
"""


@st.cache_data(ttl=CACHE_TTL_SECONDS, show_spinner=False)
def get_session_status(
    database_path: str | Path, session_start_utc: str
) -> pd.DataFrame:
    """Liveness signals. A stale last_scan_at means the scheduler stopped."""
    return _read_frame(
        database_path,
        _SESSION_STATUS_SQL,
        {"session_start_utc": session_start_utc},
    )


# ---------------------------------------------------------------------------
# Discovery Funnel
# ---------------------------------------------------------------------------

_DISCOVERY_FUNNEL_SQL = """
SELECT
    scan_id,
    as_of,
    discovery_count,
    stage0_survivors,
    prescore_survivors,
    options_prescreened,
    final_candidates,
    councils_started,
    event_track_count,
    momentum_track_count,
    survival_rate
FROM v_discovery_funnel
ORDER BY as_of DESC
LIMIT :limit
"""


@st.cache_data(ttl=CACHE_TTL_SECONDS, show_spinner=False)
def get_discovery_funnel(
    database_path: str | Path, limit: int = 20
) -> pd.DataFrame:
    return _read_frame(database_path, _DISCOVERY_FUNNEL_SQL, {"limit": limit})


_DISCOVERY_SOURCE_YIELD_SQL = """
SELECT source, symbols_discovered, reached_candidate, reached_council
FROM v_discovery_source_yield
ORDER BY reached_council DESC, symbols_discovered DESC, source
"""


@st.cache_data(ttl=CACHE_TTL_SECONDS, show_spinner=False)
def get_discovery_source_yield(database_path: str | Path) -> pd.DataFrame:
    return _read_frame(database_path, _DISCOVERY_SOURCE_YIELD_SQL)


_DISCOVERY_CANDIDATES_SQL = """
SELECT
    discovery_id,
    scan_id,
    symbol,
    discovered_at,
    expires_at,
    source,
    source_rank,
    discovery_reason,
    is_core,
    asset_tradable,
    has_options,
    data_density_ok,
    fast_score,
    discovery_boost
FROM discovery_candidates
ORDER BY discovered_at DESC, fast_score DESC, symbol
LIMIT :limit
"""


@st.cache_data(ttl=CACHE_TTL_SECONDS, show_spinner=False)
def get_discovery_candidates(
    database_path: str | Path, limit: int = 500
) -> pd.DataFrame:
    """Return discovery_reason verbatim with no rewriting or aggregation."""
    return _read_frame(database_path, _DISCOVERY_CANDIDATES_SQL, {"limit": limit})


_DISCOVERY_SOURCE_STATUS_SQL = """
WITH latest_session AS (
    SELECT MAX(session_date) AS session_date
    FROM discovery_source_status
)
SELECT
    status.session_date,
    status.source,
    status.enabled,
    status.probed_at,
    status.disabled_at,
    status.disable_reason,
    status.symbols_contributed,
    status.consecutive_errors
FROM discovery_source_status AS status
JOIN latest_session AS latest ON latest.session_date = status.session_date
ORDER BY status.enabled DESC, status.source
"""


@st.cache_data(ttl=CACHE_TTL_SECONDS, show_spinner=False)
def get_discovery_source_status(database_path: str | Path) -> pd.DataFrame:
    return _read_frame(database_path, _DISCOVERY_SOURCE_STATUS_SQL)


# ---------------------------------------------------------------------------
# Scanner
# ---------------------------------------------------------------------------

_SCANNER_CANDIDATES_SQL = """
SELECT
    candidate.candidate_id,
    candidate.scan_id,
    candidate.symbol,
    candidate.direction,
    candidate.as_of,
    candidate.fast_score,
    candidate.pre_score,
    candidate.raw_opportunity_score,
    candidate.data_confidence_factor,
    candidate.regime_factor,
    candidate.event_risk_factor,
    candidate.final_opportunity_score,
    candidate.discovery_source,
    candidate.candidate_track,
    candidate.key_metrics_json,
    candidate.config_version,
    CASE WHEN decision.decision_id IS NULL THEN 0 ELSE 1 END AS reached_council,
    decision.decision_id,
    decision.state AS decision_state
FROM candidate_scores AS candidate
LEFT JOIN decisions AS decision ON decision.candidate_id = candidate.candidate_id
ORDER BY candidate.as_of DESC, candidate.final_opportunity_score DESC, candidate.symbol
LIMIT :limit
"""


@st.cache_data(ttl=CACHE_TTL_SECONDS, show_spinner=False)
def get_scanner_candidates(
    database_path: str | Path, limit: int = 500
) -> pd.DataFrame:
    return _read_frame(database_path, _SCANNER_CANDIDATES_SQL, {"limit": limit})


# ---------------------------------------------------------------------------
# Council Decision
# ---------------------------------------------------------------------------

_DECISIONS_SQL = """
SELECT
    decision_id,
    candidate_id,
    config_version,
    strategy_id,
    symbol,
    state,
    discovery_source,
    candidate_track,
    created_at,
    updated_at
FROM decisions
ORDER BY created_at DESC
LIMIT :limit
"""


@st.cache_data(ttl=CACHE_TTL_SECONDS, show_spinner=False)
def get_decisions(database_path: str | Path, limit: int = 250) -> pd.DataFrame:
    return _read_frame(database_path, _DECISIONS_SQL, {"limit": limit})


_DECISION_CANDIDATE_SQL = """
SELECT
    decision.decision_id,
    decision.symbol,
    decision.state,
    decision.discovery_source,
    decision.candidate_track,
    decision.config_version,
    decision.created_at AS decision_created_at,
    candidate.*
FROM decisions AS decision
LEFT JOIN candidate_scores AS candidate
    ON candidate.candidate_id = decision.candidate_id
WHERE decision.decision_id = :decision_id
"""


@st.cache_data(ttl=CACHE_TTL_SECONDS, show_spinner=False)
def get_decision_candidate(
    database_path: str | Path, decision_id: str
) -> pd.DataFrame:
    return _read_frame(
        database_path, _DECISION_CANDIDATE_SQL, {"decision_id": decision_id}
    )


_DECISION_DISCOVERY_SQL = """
SELECT
    discovery.discovery_id,
    discovery.scan_id,
    discovery.symbol,
    discovery.discovered_at,
    discovery.expires_at,
    discovery.source,
    discovery.source_rank,
    discovery.discovery_reason,
    discovery.is_core,
    discovery.asset_tradable,
    discovery.has_options,
    discovery.data_density_ok,
    discovery.fast_score,
    discovery.discovery_boost
FROM decisions AS decision
JOIN candidate_scores AS candidate
    ON candidate.candidate_id = decision.candidate_id
JOIN discovery_candidates AS discovery
    ON discovery.scan_id = candidate.scan_id
   AND discovery.symbol = candidate.symbol
WHERE decision.decision_id = :decision_id
ORDER BY discovery.source_rank, discovery.discovered_at
"""


@st.cache_data(ttl=CACHE_TTL_SECONDS, show_spinner=False)
def get_decision_discovery(
    database_path: str | Path, decision_id: str
) -> pd.DataFrame:
    return _read_frame(
        database_path, _DECISION_DISCOVERY_SQL, {"decision_id": decision_id}
    )

_DECISION_INTELLIGENCE_SQL = """
SELECT
    event.event_type,
    event.direction,
    event.direction_confidence,
    event.catalyst_score,
    event.materiality_score,
    event.freshness_score,
    event.novelty_score,
    event.corroboration_score,
    event.market_confirmation_score,
    event.provisional,
    event.extracted_facts_json,
    event.evidence_urls_json,
    event.created_at,
    item.title,
    item.url,
    item.source_tier
FROM intelligence_events AS event
JOIN decisions AS decision ON decision.symbol = event.symbol
LEFT JOIN intelligence_items AS item ON item.item_id = event.item_id
WHERE decision.decision_id = :decision_id
  AND event.created_at <= decision.created_at
  AND event.created_at >= datetime(decision.created_at, '-8 hours')
ORDER BY event.catalyst_score DESC
LIMIT 12
"""


@st.cache_data(ttl=CACHE_TTL_SECONDS, show_spinner=False)
def get_decision_intelligence(
    database_path: str | Path, decision_id: str
) -> pd.DataFrame:
    """News events that were in scope when this decision was made.

    Joined on symbol and time window rather than a foreign key: events are
    collected per scan, not per decision, so there is no direct link.
    """
    return _read_frame(
        database_path, _DECISION_INTELLIGENCE_SQL, {"decision_id": decision_id}
    )

_AGENT_RUNS_SQL = """
SELECT
    run_id,
    decision_id,
    agent_name,
    provider,
    model,
    purpose,
    started_at,
    completed_at,
    input_hash,
    prompt_text,
    output_json,
    input_tokens,
    output_tokens,
    cost_usd,
    status,
    error
FROM agent_runs
WHERE decision_id = :decision_id
ORDER BY started_at, run_id
"""


@st.cache_data(ttl=CACHE_TTL_SECONDS, show_spinner=False)
def get_agent_runs(database_path: str | Path, decision_id: str) -> pd.DataFrame:
    return _read_frame(database_path, _AGENT_RUNS_SQL, {"decision_id": decision_id})


_TRADE_PROPOSALS_SQL = """
SELECT *
FROM trade_proposals
WHERE decision_id = :decision_id
ORDER BY revision, created_at
"""


@st.cache_data(ttl=CACHE_TTL_SECONDS, show_spinner=False)
def get_trade_proposals(
    database_path: str | Path, decision_id: str
) -> pd.DataFrame:
    return _read_frame(
        database_path, _TRADE_PROPOSALS_SQL, {"decision_id": decision_id}
    )


_OPTION_STRUCTURES_SQL = """
SELECT *
FROM option_structures
WHERE decision_id = :decision_id
ORDER BY rank, structure_id
"""


@st.cache_data(ttl=CACHE_TTL_SECONDS, show_spinner=False)
def get_option_structures(
    database_path: str | Path, decision_id: str
) -> pd.DataFrame:
    return _read_frame(
        database_path, _OPTION_STRUCTURES_SQL, {"decision_id": decision_id}
    )


_RED_TEAM_REVIEWS_SQL = """
SELECT *
FROM red_team_reviews
WHERE decision_id = :decision_id
ORDER BY created_at, review_id
"""


@st.cache_data(ttl=CACHE_TTL_SECONDS, show_spinner=False)
def get_red_team_reviews(
    database_path: str | Path, decision_id: str
) -> pd.DataFrame:
    return _read_frame(
        database_path, _RED_TEAM_REVIEWS_SQL, {"decision_id": decision_id}
    )


_RISK_EVALUATIONS_SQL = """
SELECT *
FROM risk_evaluations
WHERE decision_id = :decision_id
ORDER BY evaluated_at, risk_evaluation_id
"""


@st.cache_data(ttl=CACHE_TTL_SECONDS, show_spinner=False)
def get_risk_evaluations(
    database_path: str | Path, decision_id: str
) -> pd.DataFrame:
    return _read_frame(
        database_path, _RISK_EVALUATIONS_SQL, {"decision_id": decision_id}
    )


_ORDERS_SQL = """
SELECT *
FROM orders
WHERE decision_id = :decision_id
ORDER BY COALESCE(submitted_at, updated_at), attempt, order_pk
"""


@st.cache_data(ttl=CACHE_TTL_SECONDS, show_spinner=False)
def get_orders(database_path: str | Path, decision_id: str) -> pd.DataFrame:
    return _read_frame(database_path, _ORDERS_SQL, {"decision_id": decision_id})


_FILLS_SQL = """
SELECT
    fill.*,
    orders.decision_id,
    orders.intent,
    orders.attempt,
    orders.limit_walk_step
FROM fills AS fill
JOIN orders ON orders.order_pk = fill.order_pk
WHERE orders.decision_id = :decision_id
ORDER BY fill.filled_at, fill.fill_id
"""


@st.cache_data(ttl=CACHE_TTL_SECONDS, show_spinner=False)
def get_fills(database_path: str | Path, decision_id: str) -> pd.DataFrame:
    return _read_frame(database_path, _FILLS_SQL, {"decision_id": decision_id})


_TRADE_OUTCOME_SQL = """
SELECT *
FROM trade_journal
WHERE decision_id = :decision_id
"""


@st.cache_data(ttl=CACHE_TTL_SECONDS, show_spinner=False)
def get_trade_outcome(database_path: str | Path, decision_id: str) -> pd.DataFrame:
    return _read_frame(
        database_path, _TRADE_OUTCOME_SQL, {"decision_id": decision_id}
    )



# ---------------------------------------------------------------------------
# Counterfactual Lab
# ---------------------------------------------------------------------------

_ATTRIBUTION_TOTALS_SQL = """
SELECT
    decisions,
    claude_selection,
    claude_sizing,
    claude_total,
    risk_selection,
    risk_sizing,
    risk_total,
    governance_total
FROM v_attribution_totals
"""


@st.cache_data(ttl=CACHE_TTL_SECONDS, show_spinner=False)
def get_attribution_totals(database_path: str | Path) -> pd.DataFrame:
    return _read_frame(database_path, _ATTRIBUTION_TOTALS_SQL)


_ATTRIBUTION_DECISIONS_SQL = """
WITH ranked AS (
    SELECT
        attribution.*,
        ROW_NUMBER() OVER (
            PARTITION BY attribution.decision_id
            ORDER BY attribution.as_of DESC, attribution.attribution_id DESC
        ) AS recency_rank
    FROM decision_attribution AS attribution
)
SELECT
    ranked.*,
    ranked.claude_selection_effect + ranked.claude_sizing_effect
        AS claude_reconciled_total,
    ranked.risk_selection_effect + ranked.risk_sizing_effect
        AS risk_reconciled_total,
    decision.symbol,
    decision.state
FROM ranked
JOIN decisions AS decision ON decision.decision_id = ranked.decision_id
WHERE ranked.recency_rank = 1
ORDER BY ranked.as_of DESC, ranked.decision_id
"""


@st.cache_data(ttl=CACHE_TTL_SECONDS, show_spinner=False)
def get_attribution_decisions(database_path: str | Path) -> pd.DataFrame:
    return _read_frame(database_path, _ATTRIBUTION_DECISIONS_SQL)


_ATTRIBUTION_DETAIL_SQL = """
SELECT
    attribution.*,
    attribution.claude_selection_effect + attribution.claude_sizing_effect
        AS claude_reconciled_total,
    attribution.risk_selection_effect + attribution.risk_sizing_effect
        AS risk_reconciled_total,
    decision.symbol,
    decision.state
FROM decision_attribution AS attribution
JOIN decisions AS decision ON decision.decision_id = attribution.decision_id
WHERE attribution.decision_id = :decision_id
ORDER BY attribution.as_of DESC, attribution.attribution_id DESC
LIMIT 1
"""


@st.cache_data(ttl=CACHE_TTL_SECONDS, show_spinner=False)
def get_attribution_detail(
    database_path: str | Path, decision_id: str
) -> pd.DataFrame:
    return _read_frame(
        database_path, _ATTRIBUTION_DETAIL_SQL, {"decision_id": decision_id}
    )


_SHADOW_MARKS_SQL = """
SELECT
    shadow.variant,
    shadow.qty,
    shadow.entry_reference_debit,
    mark.marked_at,
    mark.mark_debit,
    mark.unrealized_pnl,
    mark.mark_method,
    mark.quote_lag_seconds
FROM shadow_marks AS mark
JOIN shadow_trades AS shadow ON shadow.shadow_id = mark.shadow_id
WHERE shadow.decision_id = :decision_id
ORDER BY mark.marked_at, shadow.variant
"""


@st.cache_data(ttl=CACHE_TTL_SECONDS, show_spinner=False)
def get_shadow_marks(
    database_path: str | Path, decision_id: str
) -> pd.DataFrame:
    """Time series of all three variants, for the Counterfactual Lab chart.

    Every mark in one cycle shares a timestamp and a mark_method by
    construction, so the three lines are directly comparable.
    """
    return _read_frame(
        database_path, _SHADOW_MARKS_SQL, {"decision_id": decision_id}
    )

# ---------------------------------------------------------------------------
# Gate Lab
# ---------------------------------------------------------------------------

_GATE_HISTOGRAM_SQL = """
SELECT stage, gate_id, tier, hard_gate, rejections, distinct_symbols, last_seen
FROM v_gate_histogram
ORDER BY rejections DESC, stage, gate_id, tier
"""


@st.cache_data(ttl=CACHE_TTL_SECONDS, show_spinner=False)
def get_gate_histogram(database_path: str | Path) -> pd.DataFrame:
    return _read_frame(database_path, _GATE_HISTOGRAM_SQL)


_GATE_VALUE_SQL = """
SELECT
    gate_id,
    stage,
    shadow_n,
    avg_blocked_pnl_per_spread,
    gate_value,
    CASE WHEN shadow_n < 5 THEN 1 ELSE 0 END AS low_sample
FROM v_gate_value
ORDER BY gate_value DESC, gate_id
"""


@st.cache_data(ttl=CACHE_TTL_SECONDS, show_spinner=False)
def get_gate_value(database_path: str | Path) -> pd.DataFrame:
    """Return GateValue together with its mandatory sample-size fields."""
    return _read_frame(database_path, _GATE_VALUE_SQL)


_TIER_TIMELINE_SQL = """
SELECT
    config_version,
    activated_at,
    deactivated_at,
    tier,
    scoring_json,
    risk_json,
    note
FROM config_versions
ORDER BY activated_at, config_version
"""


@st.cache_data(ttl=CACHE_TTL_SECONDS, show_spinner=False)
def get_tier_timeline(database_path: str | Path) -> pd.DataFrame:
    return _read_frame(database_path, _TIER_TIMELINE_SQL)


_PROFITABLE_BLOCKED_TRADES_SQL = """
SELECT
    rejection.rejection_id,
    rejection.occurred_at,
    rejection.symbol,
    rejection.direction,
    rejection.stage,
    rejection.gate_id,
    rejection.tier,
    rejection.hard_gate,
    rejection.observed_value,
    rejection.threshold_value,
    rejection.note,
    shadow.entry_timestamp,
    shadow.horizon_end,
    shadow.final_pnl_per_spread,
    shadow.mark_method,
    shadow.structure_json
FROM rejected_shadows AS shadow
JOIN gate_rejections AS rejection
    ON rejection.rejection_id = shadow.rejection_id
WHERE shadow.final_pnl_per_spread > 0
ORDER BY shadow.final_pnl_per_spread DESC, rejection.occurred_at DESC
LIMIT :limit
"""


@st.cache_data(ttl=CACHE_TTL_SECONDS, show_spinner=False)
def get_profitable_blocked_trades(
    database_path: str | Path, limit: int = 50
) -> pd.DataFrame:
    return _read_frame(
        database_path, _PROFITABLE_BLOCKED_TRADES_SQL, {"limit": limit}
    )


# ---------------------------------------------------------------------------
# Execution Quality
# ---------------------------------------------------------------------------

_FILL_BIAS_SQL = """
SELECT
    side,
    direction,
    n_fills,
    mean_bias,
    mean_slippage_pct,
    mean_seconds_to_fill,
    mean_walk_steps
FROM v_fill_bias
ORDER BY side, direction
"""


@st.cache_data(ttl=CACHE_TTL_SECONDS, show_spinner=False)
def get_fill_bias(database_path: str | Path) -> pd.DataFrame:
    return _read_frame(database_path, _FILL_BIAS_SQL)


_FILL_BIAS_ESTIMATES_SQL = """
SELECT
    computed_at,
    side,
    direction,
    sample_size,
    median_bias,
    p80_bias,
    median_seconds_to_fill,
    mean_limit_walk_steps,
    applied_buffer
FROM fill_bias_estimates
ORDER BY computed_at DESC, side, direction
"""


@st.cache_data(ttl=CACHE_TTL_SECONDS, show_spinner=False)
def get_fill_bias_estimates(database_path: str | Path) -> pd.DataFrame:
    return _read_frame(database_path, _FILL_BIAS_ESTIMATES_SQL)


_EXECUTION_CALIBRATIONS_SQL = """
SELECT *
FROM execution_calibrations
ORDER BY submitted_at DESC, calibration_id
LIMIT :limit
"""


@st.cache_data(ttl=CACHE_TTL_SECONDS, show_spinner=False)
def get_execution_calibrations(
    database_path: str | Path, limit: int = 500
) -> pd.DataFrame:
    return _read_frame(
        database_path, _EXECUTION_CALIBRATIONS_SQL, {"limit": limit}
    )


_LIMIT_WALK_HISTOGRAM_SQL = """
SELECT
    side,
    limit_walk_steps,
    COUNT(*) AS fills
FROM execution_calibrations
WHERE actual_fill_debit IS NOT NULL
GROUP BY side, limit_walk_steps
ORDER BY side, limit_walk_steps
"""


@st.cache_data(ttl=CACHE_TTL_SECONDS, show_spinner=False)
def get_limit_walk_histogram(database_path: str | Path) -> pd.DataFrame:
    return _read_frame(database_path, _LIMIT_WALK_HISTOGRAM_SQL)


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------

_DECISION_AUDIT_TIMELINE_SQL = """
WITH timeline AS (
    SELECT
        occurred_at,
        level,
        component,
        event_type,
        message,
        context_json AS details_json
    FROM system_events
    WHERE decision_id = :decision_id

    UNION ALL

    SELECT
        started_at AS occurred_at,
        'INFO' AS level,
        'agent' AS component,
        'AGENT_STARTED' AS event_type,
        agent_name || ' / ' || purpose AS message,
        output_json AS details_json
    FROM agent_runs
    WHERE decision_id = :decision_id

    UNION ALL

    SELECT
        COALESCE(submitted_at, updated_at) AS occurred_at,
        'INFO' AS level,
        'execution' AS component,
        'ORDER_' || UPPER(status) AS event_type,
        intent || ' attempt ' || attempt || ': ' || client_order_id AS message,
        raw_json AS details_json
    FROM orders
    WHERE decision_id = :decision_id

    UNION ALL

    SELECT
        fill.filled_at AS occurred_at,
        'INFO' AS level,
        'execution' AS component,
        'FILL' AS event_type,
        fill.side || ' ' || fill.qty || ' ' || fill.symbol || ' @ ' || fill.price
            AS message,
        fill.raw_json AS details_json
    FROM fills AS fill
    JOIN orders ON orders.order_pk = fill.order_pk
    WHERE orders.decision_id = :decision_id

    UNION ALL

    SELECT
        occurred_at,
        'INFO' AS level,
        'api_usage' AS component,
        'API_USAGE' AS event_type,
        provider || ' / ' || model || ' / ' || endpoint AS message,
        NULL AS details_json
    FROM api_usage
    WHERE decision_id = :decision_id
)
SELECT occurred_at, level, component, event_type, message, details_json
FROM timeline
ORDER BY occurred_at, component, event_type
"""


@st.cache_data(ttl=CACHE_TTL_SECONDS, show_spinner=False)
def get_decision_audit_timeline(
    database_path: str | Path, decision_id: str
) -> pd.DataFrame:
    return _read_frame(
        database_path,
        _DECISION_AUDIT_TIMELINE_SQL,
        {"decision_id": decision_id},
    )


_DECISION_CONFIG_SQL = """
SELECT
    decision.decision_id,
    decision.config_version,
    config.activated_at,
    config.deactivated_at,
    config.tier,
    config.scoring_json,
    config.risk_json,
    config.note
FROM decisions AS decision
LEFT JOIN config_versions AS config
    ON config.config_version = decision.config_version
WHERE decision.decision_id = :decision_id
"""


@st.cache_data(ttl=CACHE_TTL_SECONDS, show_spinner=False)
def get_decision_config(
    database_path: str | Path, decision_id: str
) -> pd.DataFrame:
    return _read_frame(
        database_path, _DECISION_CONFIG_SQL, {"decision_id": decision_id}
    )


_SYSTEM_EVENTS_BASE_SQL = """
SELECT
    system_event_id,
    occurred_at,
    level,
    component,
    event_type,
    decision_id,
    message,
    context_json
FROM system_events
WHERE (:level = '' OR level = :level)
  AND (:component = '' OR component = :component)
ORDER BY occurred_at DESC, system_event_id DESC
LIMIT :limit
"""


@st.cache_data(ttl=CACHE_TTL_SECONDS, show_spinner=False)
def get_system_events(
    database_path: str | Path,
    *,
    level: str = "",
    component: str = "",
    limit: int = 500,
) -> pd.DataFrame:
    return _read_frame(
        database_path,
        _SYSTEM_EVENTS_BASE_SQL,
        {"level": level, "component": component, "limit": limit},
    )


_SYSTEM_EVENT_FILTERS_SQL = """
SELECT DISTINCT level, component
FROM system_events
ORDER BY level, component
"""


@st.cache_data(ttl=CACHE_TTL_SECONDS, show_spinner=False)
def get_system_event_filters(database_path: str | Path) -> pd.DataFrame:
    return _read_frame(database_path, _SYSTEM_EVENT_FILTERS_SQL)


_DECISION_API_USAGE_SQL = """
SELECT
    usage_id,
    occurred_at,
    provider,
    model,
    endpoint,
    request_id,
    input_tokens,
    output_tokens,
    cached_tokens,
    cost_usd
FROM api_usage
WHERE decision_id = :decision_id
ORDER BY occurred_at, usage_id
"""


@st.cache_data(ttl=CACHE_TTL_SECONDS, show_spinner=False)
def get_decision_api_usage(
    database_path: str | Path, decision_id: str
) -> pd.DataFrame:
    return _read_frame(
        database_path, _DECISION_API_USAGE_SQL, {"decision_id": decision_id}
    )
