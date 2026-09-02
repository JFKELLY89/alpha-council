"""Council Decision tab."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd
import streamlit as st

from dashboard import queries
from dashboard.formatting import (
    empty_message,
    format_currency,
    format_et,
    format_percent,
    parse_json,
    pretty_json,
)


ANALYSTS = {"bull", "bear", "catalyst"}

# state -> (Streamlit banner style, plain-language outcome)
OUTCOME = {
    "CANDIDATE": ("info", "Scored but the council never started."),
    "COUNCIL_STARTED": ("warning", "Council started but did not complete."),
    "PM_PROPOSED": ("warning", "PM proposed; stopped before structure selection."),
    "STRUCTURES_GENERATED": ("warning", "Structures built but none selected."),
    "STRUCTURE_SELECTED": ("warning", "Structure selected; Red Team did not complete."),
    "RED_TEAMED": ("warning", "Red Team reviewed; stopped before risk evaluation."),
    "REVISED": ("warning", "PM revised; stopped before risk evaluation."),
    "RISK_APPROVED": ("info", "Risk approved; no order outcome recorded yet."),
    "RISK_REJECTED": ("error", "STOPPED — blocked before an order was submitted."),
    "ORDER_SUBMITTED": ("info", "Order submitted; awaiting a terminal state."),
    "ORDER_WORKING": ("info", "Order working."),
    "NO_FILL": ("error", "NO FILL — the limit walk completed without a fill."),
    "CANCELED": ("error", "Order canceled."),
    "REJECTED": ("error", "Order rejected by the broker."),
    "FILLED": ("success", "FILLED — the order executed."),
    "POSITION_OPEN": ("success", "POSITION OPEN — currently held."),
    "POSITION_CLOSED": ("success", "POSITION CLOSED — see realized P&L below."),
    "ATTRIBUTED": ("success", "COMPLETE — attribution computed."),
}


def _outcome_banner(decision: pd.Series, reviews: pd.DataFrame,
                    risks: pd.DataFrame, proposals: pd.DataFrame) -> None:
    """State the outcome before the evidence.

    Most decisions stop before a fill, and that is the design working. The
    reason belongs at the top, not eight sections down.
    """
    state = str(decision["state"])
    style, message = OUTCOME.get(state, ("info", f"State: {state}"))

    detail = ""
    if not reviews.empty and str(reviews.iloc[-1]["verdict"]) == "VETO":
        detail = f" Red Team VETO: {reviews.iloc[-1]['summary']}"
    elif not risks.empty and str(risks.iloc[-1]["decision"]) in ("REJECT", "HALT"):
        violations = parse_json(risks.iloc[-1]["violations_json"], fallback=[])
        blocking = [v for v in violations
                    if isinstance(v, dict)
                    and v.get("severity") in ("BLOCK", "HALT")]
        if blocking:
            detail = (f" Risk Constitution {risks.iloc[-1]['decision']}: "
                      f"{blocking[0].get('rule_id')} — "
                      f"{blocking[0].get('message')}")
    elif not proposals.empty:
        abstained = proposals[proposals["trade"] == 0]
        if not abstained.empty and abstained.iloc[-1].get("abstain_reason"):
            detail = f" PM abstained: {abstained.iloc[-1]['abstain_reason']}"

    getattr(st, style)(message + detail)

def _decision_selector(decisions: pd.DataFrame) -> str:
    labels = {
        row.decision_id: f"{row.symbol} · {row.state} · {format_et(row.created_at)} · {row.decision_id}"
        for row in decisions.itertuples()
    }
    return st.selectbox(
        "Decision",
        decisions["decision_id"].tolist(),
        format_func=lambda value: labels[value],
        key="council_decision_id",
    )


def _string_list(value: Any) -> list[str]:
    parsed = parse_json(value, fallback=[])
    return [str(item) for item in parsed] if isinstance(parsed, list) else []


def _write_list(title: str, items: list[str]) -> None:
    st.markdown(f"**{title}**")
    if not items:
        st.caption("None recorded.")
        return
    for item in items:
        st.markdown(f"- {item}")


def _render_proposal(row: pd.Series, label: str) -> None:
    st.markdown(f"##### {label}")
    summary = st.columns(4)
    summary[0].metric("Action", "TRADE" if bool(row["trade"]) else "ABSTAIN")
    summary[1].metric("Direction", str(row["direction"]))
    summary[2].metric("Confidence", format_percent(row["confidence"], fraction=True))
    summary[3].metric(
        "Requested risk", format_percent(row["desired_portfolio_risk_pct"])
    )
    if row.get("thesis"):
        st.write(row["thesis"])
    if not bool(row["trade"]) and row.get("abstain_reason"):
        st.info(str(row["abstain_reason"]))
    support, contrary = st.columns(2)
    with support:
        _write_list("Supporting evidence", _string_list(row["key_supporting_evidence_json"]))
    with contrary:
        _write_list("Contrary evidence", _string_list(row["key_contrary_evidence_json"]))
    invalidations = parse_json(row["invalidation_json"], fallback=[])
    if invalidations:
        st.markdown("**Invalidation rules**")
        st.dataframe(pd.DataFrame(invalidations), width="stretch", hide_index=True)


def render(database_path: str | Path) -> None:
    st.subheader("Council Decision")
    st.caption("Why did Alpha Council take or reject this trade?")

    decisions = queries.get_decisions(database_path)
    if decisions.empty:
        st.info(empty_message("decisions"))
        return

    decision_id = _decision_selector(decisions)
    decision = decisions.loc[decisions["decision_id"] == decision_id].iloc[0]
    st.caption(
        f"{decision['symbol']} · {decision['candidate_track'] or 'track unavailable'} · "
        f"{decision['discovery_source'] or 'source unavailable'} · state {decision['state']}"
    )

    # Loaded once here and reused by sections 3, 5 and 7 below.
    proposals = queries.get_trade_proposals(database_path, decision_id)
    reviews = queries.get_red_team_reviews(database_path, decision_id)
    risks = queries.get_risk_evaluations(database_path, decision_id)
    _outcome_banner(decision, reviews, risks, proposals)

    st.markdown("### 1. Evidence and candidate features")
    candidate = queries.get_decision_candidate(database_path, decision_id)
    discoveries = queries.get_decision_discovery(database_path, decision_id)
    if candidate.empty:
        st.info("The decision exists, but its candidate score row is not available.")
    else:
        feature_columns = [
            "direction",
            "fast_score",
            "pre_score",
            "raw_opportunity_score",
            "data_confidence_factor",
            "regime_factor",
            "event_risk_factor",
            "final_opportunity_score",
            "key_metrics_json",
        ]
        st.dataframe(
            candidate[feature_columns], width="stretch", hide_index=True
        )
    if discoveries.empty:
        st.caption("No discovery row is linked to this decision.")
    else:
        reasons = discoveries[["source", "discovery_reason", "discovered_at"]].copy()
        reasons["discovered_at"] = reasons["discovered_at"].map(format_et)
        st.dataframe(reasons, width="stretch", hide_index=True)
        st.caption("Discovery reasons are shown verbatim.")

    intel = queries.get_decision_intelligence(database_path, decision_id)
    if not intel.empty:
        st.markdown("**Intelligence in scope**")
        display = intel[["title", "event_type", "direction",
                         "catalyst_score", "materiality_score",
                         "freshness_score", "created_at"]].copy()
        display["created_at"] = display["created_at"].map(format_et)
        st.dataframe(
            display, width="stretch", hide_index=True,
            column_config={
                "catalyst_score": st.column_config.NumberColumn(
                    "Catalyst", format="%.1f"),
                "materiality_score": st.column_config.NumberColumn(
                    "Material", format="%.0f"),
                "freshness_score": st.column_config.NumberColumn(
                    "Fresh", format="%.0f"),
            })
        st.caption(
            "Events within 8 hours before the decision. Direction is "
            "resolved from price response, not headline tone.")
    else:
        st.caption("No intelligence events were in scope for this decision.")

    runs = queries.get_agent_runs(database_path, decision_id)
    st.markdown("### 2. Bull, Bear, and Catalyst assessments")
    analyst_runs = runs[runs["agent_name"].str.lower().isin(ANALYSTS)] if not runs.empty else runs
    if analyst_runs.empty:
        st.info("No analyst run has been recorded for this decision.")
    else:
        columns = st.columns(3)
        for index, (_, run) in enumerate(analyst_runs.iterrows()):
            with columns[index % 3]:
                output = parse_json(run["output_json"], fallback={})
                st.markdown(f"#### {str(run['agent_name']).title()}")
                if isinstance(output, dict):
                    st.metric("Score", output.get("score", "—"))
                    st.metric(
                        "Confidence",
                        format_percent(output.get("confidence"), fraction=True),
                    )
                    st.write(output.get("thesis") or "No thesis returned.")
                    _write_list("Evidence for", output.get("evidence_for", []))
                    _write_list("Evidence against", output.get("evidence_against", []))
                else:
                    st.warning(f"Run status: {run['status']}")

    st.markdown("### 3. Portfolio Manager proposal")
    original = proposals[proposals["revision"] == 0]
    if original.empty:
        st.info("No initial PM proposal has been recorded.")
    else:
        _render_proposal(original.iloc[0], "Initial proposal")

    st.markdown("### 4. Real option structures")
    structures = queries.get_option_structures(database_path, decision_id)
    if structures.empty:
        st.info("No option structures have been generated for this decision.")
    else:
        display = structures[
            [
                "rank",
                "strategy",
                "expiration",
                "dte",
                "long_strike",
                "long_delta",
                "short_strike",
                "short_delta",
                "adjusted_mid_debit",
                "initial_limit_debit",
                "cost_to_width_ratio",
                "breakeven",
                "structure_score",
                "stale_adjusted",
            ]
        ]
        st.dataframe(
            display,
            width="stretch",
            hide_index=True,
            column_config={
                "adjusted_mid_debit": st.column_config.NumberColumn(format="$%.2f"),
                "initial_limit_debit": st.column_config.NumberColumn(format="$%.2f"),
                "cost_to_width_ratio": st.column_config.NumberColumn(format="%.3f"),
                "breakeven": st.column_config.NumberColumn(format="$%.2f"),
            },
        )

    st.markdown("### 5. Red Team review")
    if reviews.empty:
        st.info("No Red Team review has been recorded.")
    else:
        review = reviews.iloc[-1]
        summary = st.columns(4)
        summary[0].metric("Verdict", str(review["verdict"]))
        summary[1].metric("Risk score", f"{int(review['risk_score'])}/10")
        summary[2].metric("Fatal flaw", "YES" if bool(review["fatal_flaw"]) else "NO")
        summary[3].metric(
            "Recommended max risk",
            format_percent(review["recommended_max_risk_pct"]),
        )
        st.write(review["summary"])
        st.markdown(f"**Strongest counterargument:** {review['strongest_counterargument']}")
        problems = parse_json(review["problems_json"], fallback=[])
        if problems:
            st.dataframe(pd.DataFrame(problems), width="stretch", hide_index=True)
        _write_list(
            "Information that would reverse the verdict",
            _string_list(review["information_to_reverse_json"]),
        )

    st.markdown("### 6. Portfolio Manager revision")
    revised = proposals[proposals["revision"] == 1]
    if revised.empty:
        st.caption("No revision was recorded; the initial proposal remained in force or the pipeline stopped.")
    else:
        _render_proposal(revised.iloc[0], "Revision 1")
        if not original.empty:
            fields = [
                "trade",
                "direction",
                "confidence",
                "desired_portfolio_risk_pct",
                "selected_structure_rank",
                "thesis",
            ]
            comparison = pd.DataFrame(
                {
                    "field": fields,
                    "initial": [original.iloc[0][field] for field in fields],
                    "revised": [revised.iloc[0][field] for field in fields],
                }
            )
            comparison = comparison[comparison["initial"].astype(str) != comparison["revised"].astype(str)]
            if not comparison.empty:
                st.dataframe(comparison, width="stretch", hide_index=True)

    st.markdown("### 7. Risk Constitution")
    if risks.empty:
        st.info("No deterministic risk evaluation has been recorded.")
    else:
        risk = risks.iloc[-1]
        summary = st.columns(4)
        summary[0].metric("Decision", str(risk["decision"]))
        summary[1].metric("Requested quantity", int(risk["requested_qty"]))
        summary[2].metric("Approved quantity", int(risk["approved_qty"]))
        summary[3].metric("Approved max loss", format_currency(risk["approved_max_loss"]))
        violations = parse_json(risk["violations_json"], fallback=[])
        if violations:
            st.dataframe(pd.DataFrame(violations), width="stretch", hide_index=True)
        else:
            st.caption("No Risk Constitution violations were recorded.")

    st.markdown("### 8. Order, fill, and realized outcome")
    orders = queries.get_orders(database_path, decision_id)
    fills = queries.get_fills(database_path, decision_id)
    outcome = queries.get_trade_outcome(database_path, decision_id)
    if orders.empty:
        st.info("No order has been submitted for this decision.")
    else:
        order_display = orders.drop(columns=["raw_json"], errors="ignore").copy()
        for column in ["submitted_at", "updated_at"]:
            order_display[column] = order_display[column].map(format_et)
        st.dataframe(order_display, width="stretch", hide_index=True)
    if not fills.empty:
        fill_display = fills.drop(columns=["raw_json"], errors="ignore").copy()
        fill_display["filled_at"] = fill_display["filled_at"].map(format_et)
        st.dataframe(fill_display, width="stretch", hide_index=True)
    if not outcome.empty:
        outcome_display = outcome.copy()
        for column in ["opened_at", "closed_at"]:
            outcome_display[column] = outcome_display[column].map(format_et)
        st.dataframe(outcome_display, width="stretch", hide_index=True)

    with st.expander("Exact agent prompts and raw outputs"):
        if runs.empty:
            st.caption("No agent runs are available.")
        for _, run in runs.iterrows():
            st.markdown(f"#### {run['agent_name']} · {run['purpose']} · {run['status']}")
            st.markdown("**Prompt**")
            st.code(run["prompt_text"] or "No prompt recorded.", language=None)
            st.markdown("**Output**")
            st.code(pretty_json(run["output_json"]), language="json")
