"""Scanner tab."""

from __future__ import annotations

from pathlib import Path

import streamlit as st

from dashboard import queries
from dashboard.formatting import empty_message, format_et


def render(database_path: str | Path) -> None:
    st.subheader("Scanner")
    st.caption(
        "FinalOpportunityScore is stored by the scanner: raw opportunity × data confidence × regime × event risk."
    )

    candidates = queries.get_scanner_candidates(database_path)
    if candidates.empty:
        st.info(empty_message("scanner"))
        return

    filters = st.columns(3)
    tracks = ["All", *sorted(candidates["candidate_track"].dropna().unique().tolist())]
    sources = ["All", *sorted(candidates["discovery_source"].dropna().unique().tolist())]
    track = filters[0].selectbox("Track", tracks, key="scanner_track")
    source = filters[1].selectbox("Source", sources, key="scanner_source")
    councils_only = filters[2].toggle("Reached council only", value=False)

    filtered = candidates.copy()
    if track != "All":
        filtered = filtered[filtered["candidate_track"] == track]
    if source != "All":
        filtered = filtered[filtered["discovery_source"] == source]
    if councils_only:
        filtered = filtered[filtered["reached_council"] == 1]

    display = filtered[
        [
            "symbol",
            "direction",
            "fast_score",
            "pre_score",
            "raw_opportunity_score",
            "data_confidence_factor",
            "regime_factor",
            "event_risk_factor",
            "final_opportunity_score",
            "discovery_source",
            "candidate_track",
            "reached_council",
            "decision_state",
            "as_of",
        ]
    ].copy()
    display["reached_council"] = display["reached_council"].astype(bool)
    display["as_of"] = display["as_of"].map(format_et)
    display = display.sort_values("final_opportunity_score", ascending=False)

    if display.empty:
        st.info("No candidates match the selected filters.")
        return

    st.dataframe(
        display,
        width="stretch",
        hide_index=True,
        column_config={
            "fast_score": st.column_config.NumberColumn("FastScore", format="%.1f"),
            "pre_score": st.column_config.NumberColumn("PreScore", format="%.1f"),
            "raw_opportunity_score": st.column_config.NumberColumn("Raw", format="%.1f"),
            "data_confidence_factor": st.column_config.NumberColumn("Data ×", format="%.2f"),
            "regime_factor": st.column_config.NumberColumn("Regime ×", format="%.2f"),
            "event_risk_factor": st.column_config.NumberColumn("Event ×", format="%.2f"),
            "final_opportunity_score": st.column_config.NumberColumn("FinalOpportunityScore", format="%.1f"),
            "reached_council": st.column_config.CheckboxColumn("Council"),
        },
    )
    st.caption("Rows with Council checked advanced to a recorded decision.")
