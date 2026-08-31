"""Audit tab."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import streamlit as st

from dashboard import queries
from dashboard.formatting import empty_message, format_et, pretty_json


def _decision_selector(decisions: pd.DataFrame) -> str:
    labels = {
        row.decision_id: f"{row.symbol} · {row.state} · {format_et(row.created_at)} · {row.decision_id}"
        for row in decisions.itertuples()
    }
    return st.selectbox(
        "Decision audit",
        decisions["decision_id"].tolist(),
        format_func=lambda value: labels[value],
        key="audit_decision_id",
    )


def render(database_path: str | Path) -> None:
    st.subheader("Audit")
    st.caption("Decision timelines, configuration in force, raw system events, and recorded API cost.")

    decisions = queries.get_decisions(database_path)
    if decisions.empty:
        st.info("No decision timeline exists yet. The system-event browser below remains available.")
    else:
        decision_id = _decision_selector(decisions)
        timeline = queries.get_decision_audit_timeline(database_path, decision_id)
        st.markdown("#### Decision timeline")
        if timeline.empty:
            st.info(empty_message("audit"))
        else:
            display = timeline.drop(columns=["details_json"], errors="ignore").copy()
            display["occurred_at"] = display["occurred_at"].map(format_et)
            st.dataframe(display, use_container_width=True, hide_index=True)
            with st.expander("Timeline details"):
                for _, event in timeline.iterrows():
                    st.markdown(
                        f"**{format_et(event['occurred_at'])} · {event['component']} · {event['event_type']}**"
                    )
                    st.write(event["message"])
                    if event["details_json"]:
                        st.code(pretty_json(event["details_json"]), language="json")

        config = queries.get_decision_config(database_path, decision_id)
        st.markdown("#### Configuration in force")
        if config.empty:
            st.caption("No configuration version is linked to this decision.")
        else:
            row = config.iloc[0]
            cols = st.columns(3)
            cols[0].metric("Config version", row["config_version"] or "—")
            cols[1].metric(
                "Tier",
                f"Tier {int(row['tier'])}" if not pd.isna(row["tier"]) else "—",
            )
            cols[2].metric("Activated", format_et(row["activated_at"]))
            with st.expander("Scoring thresholds"):
                st.code(pretty_json(row["scoring_json"]), language="json")
            with st.expander("Risk Constitution thresholds"):
                st.code(pretty_json(row["risk_json"]), language="json")

        usage = queries.get_decision_api_usage(database_path, decision_id)
        st.markdown("#### Recorded API usage")
        if usage.empty:
            st.caption("No API usage is linked to this decision.")
        else:
            display = usage.copy()
            display["occurred_at"] = display["occurred_at"].map(format_et)
            st.dataframe(
                display,
                use_container_width=True,
                hide_index=True,
                column_config={"cost_usd": st.column_config.NumberColumn(format="$%.4f")},
            )

    st.divider()
    st.markdown("#### Raw system events")
    filters = queries.get_system_event_filters(database_path)
    levels = ["All", *sorted(filters["level"].dropna().unique().tolist())] if not filters.empty else ["All"]
    components = ["All", *sorted(filters["component"].dropna().unique().tolist())] if not filters.empty else ["All"]
    cols = st.columns(3)
    level = cols[0].selectbox("Level", levels, key="audit_level")
    component = cols[1].selectbox("Component", components, key="audit_component")
    limit = cols[2].selectbox("Rows", [100, 250, 500], index=1, key="audit_limit")
    events = queries.get_system_events(
        database_path,
        level="" if level == "All" else level,
        component="" if component == "All" else component,
        limit=limit,
    )
    if events.empty:
        st.info(empty_message("audit"))
    else:
        display = events.drop(columns=["context_json"], errors="ignore").copy()
        display["occurred_at"] = display["occurred_at"].map(format_et)
        st.dataframe(display, use_container_width=True, hide_index=True)
        with st.expander("Raw event context"):
            for _, event in events.iterrows():
                st.markdown(
                    f"**{format_et(event['occurred_at'])} · {event['component']} · {event['event_type']}**"
                )
                st.write(event["message"])
                st.code(pretty_json(event["context_json"]), language="json")
