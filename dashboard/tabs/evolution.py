"""Alpha Evolution tab (v2.5 §25).

Champion vs Challenger, the lessons feed, the promotion checklist, and
today's pre-market brief. The tab's job is to communicate that the model
LEARNS CAUTIOUSLY: a challenger held in shadow with its failed promotion
rules on display is the feature, not a shortfall.
"""

from __future__ import annotations

import json
from pathlib import Path

import streamlit as st

from dashboard import queries
from dashboard.formatting import format_currency, format_et


def _loads(raw: object) -> object:
    try:
        return json.loads(raw) if raw else None
    except (TypeError, ValueError):
        return None


def render(database_path: str | Path) -> None:
    st.subheader("Alpha Evolution")
    st.caption(
        "Champion trades. Challenger shadows. Promotion is advisory and "
        "operator-gated. Refusing to overreact to a five-day sample is "
        "the design, not a limitation."
    )

    versions = queries.get_strategy_versions(database_path)
    if versions.empty:
        st.info("No strategy versions yet. The champion is created at the "
                "next autonomous session start.")
        return

    champion = versions[versions["status"] == "CHAMPION"]
    challenger = versions[versions["status"] == "CHALLENGER"]

    left, right = st.columns(2)
    with left:
        st.markdown("#### Champion")
        if champion.empty:
            st.warning("No CHAMPION row — run_alpha_council creates one at "
                       "startup.")
        else:
            row = champion.iloc[-1]
            st.metric("Strategy", str(row["strategy_id"]))
            st.caption(f"Config {row['config_version']} · active since "
                       f"{format_et(row['created_at'])}")
    with right:
        st.markdown("#### Challenger")
        if challenger.empty:
            st.info("None shadowing. Alpha Evolution proposes at most one "
                    "bounded challenger post-close, and only past the "
                    "evidence floor.")
        else:
            row = challenger.iloc[-1]
            st.metric("Strategy", str(row["strategy_id"]))
            st.caption(f"Hypothesis: {row['hypothesis']}")

    # ---- proposals ---------------------------------------------------
    proposals = queries.get_challenger_proposals(database_path)
    if not proposals.empty:
        st.markdown("#### Proposals")
        for row in proposals.itertuples():
            title = (f"{row.challenger_id} · {row.status} · "
                     f"{row.confidence} confidence")
            with st.expander(title, expanded=(row.status == "SHADOWING")):
                st.write(row.hypothesis)
                changes = _loads(row.changes_json) or []
                if changes:
                    st.table([{
                        "parameter": c.get("parameter_path"),
                        "champion": c.get("champion_value"),
                        "challenger": c.get("challenger_value"),
                        "category": c.get("category"),
                    } for c in changes])
                st.caption(f"Expected benefit: {row.expected_benefit}")
                st.caption(f"Expected failure mode: "
                           f"{row.expected_failure_mode}")

    # ---- performance -------------------------------------------------
    performance = queries.get_strategy_performance(database_path)
    shadows = queries.get_shadow_decision_summary(database_path)
    if not performance.empty:
        st.markdown("#### Champion vs Challenger")
        display = performance[[
            "strategy_id", "observations", "closed_trades", "total_pnl",
            "return_pct", "win_rate", "expectancy", "max_drawdown_pct",
            "event_pnl", "momentum_pnl"]].copy()
        st.dataframe(display, use_container_width=True, hide_index=True,
                     column_config={
                         "total_pnl": st.column_config.NumberColumn(
                             format="$%.2f"),
                         "event_pnl": st.column_config.NumberColumn(
                             format="$%.2f"),
                         "momentum_pnl": st.column_config.NumberColumn(
                             format="$%.2f"),
                     })
        for row in performance.itertuples():
            metrics = _loads(row.metrics_json) or {}
            unmeasured = metrics.get("unmeasured_observations", 0)
            if unmeasured:
                st.caption(
                    f"{row.strategy_id}: {unmeasured} observation(s) "
                    "unmeasurable (challenger-only trades with no marked "
                    "shadow). Stated, not smoothed over.")
    if not shadows.empty:
        st.markdown("#### Shadow decisions")
        st.dataframe(shadows, use_container_width=True, hide_index=True)

    # ---- promotion checklist ----------------------------------------
    recommendations = queries.get_promotion_recommendations(database_path)
    if not recommendations.empty:
        st.markdown("#### Promotion status")
        latest = recommendations.iloc[0]
        st.metric("Recommendation", str(latest["recommendation"]),
                  help="Deterministic rules; a model cannot override a "
                       "failed rule, and competition promotion always "
                       "requires operator approval.")
        st.caption(f"Evidence strength: {latest['evidence_strength']} · "
                   f"generated {format_et(latest['generated_at'])}")
        for reason in (_loads(latest["reasons_json"]) or []):
            st.write(f"- {reason}")
        failed = _loads(latest["failed_rules_json"]) or []
        if failed:
            st.markdown("**Failed promotion rules**")
            for rule in failed:
                st.write(f"- :red[{rule}]")

    # ---- lessons feed ------------------------------------------------
    lessons = queries.get_strategy_lessons(database_path)
    st.markdown("#### Lessons feed")
    if lessons.empty:
        st.info("No lessons yet. The post-close cycle writes them at 16:15 "
                "ET.")
    else:
        for row in lessons.itertuples():
            flag = " · recommends change" if row.recommends_change else ""
            with st.expander(
                    f"[{row.lesson_type}] {row.confidence} confidence, "
                    f"n={row.sample_size}{flag}"):
                st.write(f"**Observation:** {row.observation}")
                st.write(f"**Hypothesis:** {row.explanation_hypothesis}")
                st.write(f"**Proposed test:** {row.proposed_test}")

    # ---- pre-market brief -------------------------------------------
    brief = queries.get_latest_premarket_brief(database_path)
    st.markdown("#### Pre-market brief")
    if brief.empty:
        st.info("No brief stored yet. The strategist runs at 08:45 ET.")
    else:
        row = brief.iloc[0]
        payload = _loads(row["output_json"]) or {}
        st.caption(f"Session {row['session_date']} · "
                   f"{format_currency(row['cost_usd'])} · {row['model']}")
        if payload:
            st.write(f"**Bias:** {payload.get('session_bias')} "
                     f"(confidence {payload.get('confidence')})")
            st.write(payload.get("regime_summary", ""))
            for theme in payload.get("important_themes", [])[:5]:
                st.write(f"- {theme}")
