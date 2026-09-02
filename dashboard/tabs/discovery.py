"""Discovery Funnel tab."""

from __future__ import annotations

from pathlib import Path

import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from dashboard import queries, theme
from dashboard.formatting import empty_message, format_et, format_percent


def render(database_path: str | Path) -> None:
    st.subheader("Discovery Funnel")
    st.caption("Why did Alpha Council notice this symbol?")

    funnel = queries.get_discovery_funnel(database_path)
    if funnel.empty:
        st.info(empty_message("funnel"))
    else:
        latest = funnel.iloc[0]
        stages = ["Discovered", "Stage 0", "Pre-score", "Options", "Final", "Councils"]
        values = [
            latest["discovery_count"],
            latest["stage0_survivors"],
            latest["prescore_survivors"],
            latest["options_prescreened"],
            latest["final_candidates"],
            latest["councils_started"],
        ]
        palette = theme.active_palette()
        chart_col, track_col = st.columns([2, 1])
        with chart_col:
            # Gold at the wide end fading toward the muted tone as the
            # funnel narrows - the narrowing IS the brand story.
            funnel_shades = [palette.gold, palette.gold_bright, palette.blue,
                             palette.purple, palette.green, palette.muted]
            figure = go.Figure(go.Funnel(
                y=stages, x=values, textinfo="value+percent initial",
                marker={"color": funnel_shades[: len(stages)]},
                connector={"line": {"color": palette.border}},
            ))
            figure.update_layout(margin=dict(l=20, r=20, t=20, b=20))
            theme.plot(figure, height=380)
            st.caption(
                f"Latest scan: {latest['scan_id']} · {format_et(latest['as_of'])} · "
                f"survival {format_percent(latest['survival_rate'], fraction=True)}"
            )
        with track_col:
            track = {
                "Track": ["EVENT", "MOMENTUM"],
                "Candidates": [latest["event_track_count"], latest["momentum_track_count"]],
            }
            figure = px.pie(
                track, names="Track", values="Candidates", hole=0.58,
                color="Track",
                color_discrete_map={"EVENT": palette.gold,
                                    "MOMENTUM": palette.blue},
            )
            figure.update_layout(margin=dict(l=10, r=10, t=25, b=10))
            theme.plot(figure, height=330)

    source_yield = queries.get_discovery_source_yield(database_path)
    st.markdown("#### Source yield")
    if source_yield.empty:
        st.info(empty_message("discovery"))
    else:
        melted = source_yield.melt(
            id_vars="source",
            value_vars=["symbols_discovered", "reached_candidate", "reached_council"],
            var_name="stage",
            value_name="symbols",
        )
        figure = px.bar(
            melted,
            x="source",
            y="symbols",
            color="stage",
            barmode="group",
            labels={"source": "Discovery source", "symbols": "Distinct symbols"},
        )
        figure.update_layout(margin=dict(l=10, r=10, t=20, b=10))
        theme.plot(figure)

    reason_col, status_col = st.columns([2, 1])
    with reason_col:
        st.markdown("#### Why each symbol was noticed")
        discoveries = queries.get_discovery_candidates(database_path)
        if discoveries.empty:
            st.info(empty_message("discovery"))
        else:
            display = discoveries[
                ["symbol", "source", "discovery_reason", "fast_score", "discovered_at"]
            ].copy()
            display["discovered_at"] = display["discovered_at"].map(format_et)
            st.dataframe(display, width="stretch", hide_index=True)
            st.caption("discovery_reason is shown verbatim from SQLite.")

    with status_col:
        st.markdown("#### Source availability")
        status = queries.get_discovery_source_status(database_path)
        if status.empty:
            st.info(empty_message("source_status"))
        else:
            display = status.copy()
            display["status"] = display["enabled"].map(
                {1: "available", 0: "unavailable"}
            )
            display["probed_at"] = display["probed_at"].map(format_et)
            st.dataframe(
                display[
                    ["source", "status", "disable_reason", "symbols_contributed", "probed_at"]
                ],
                width="stretch",
                hide_index=True,
            )
            st.caption("A 403 from most-actives is expected and is reported as unavailable, not as a dashboard error.")
