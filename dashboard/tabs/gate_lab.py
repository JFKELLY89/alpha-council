"""Gate Lab tab."""

from __future__ import annotations

from html import escape
from pathlib import Path

import plotly.express as px
import streamlit as st

from dashboard import queries, theme
from dashboard.formatting import empty_message, format_currency, format_et


def render(database_path: str | Path) -> None:
    st.subheader("Gate Lab")
    st.caption("Did each deterministic gate save money or block profitable trades?")

    histogram = queries.get_gate_histogram(database_path)
    if histogram.empty:
        st.info(empty_message("gates"))
    else:
        histogram = histogram.copy()
        histogram["last_seen"] = histogram["last_seen"].map(format_et)
        figure = px.bar(
            histogram,
            x="gate_id",
            y="rejections",
            color="stage",
            pattern_shape="hard_gate",
            hover_data=["tier", "distinct_symbols", "last_seen"],
            labels={"gate_id": "Gate", "rejections": "Rejections"},
        )
        figure.update_layout(margin=dict(l=10, r=10, t=25, b=10))
        theme.plot(figure)

    st.markdown("#### GateValue")
    st.caption("GateValue = −1 × mean hypothetical P&L per spread of blocked trades. Positive means the gate earned its place.")
    gate_value = queries.get_gate_value(database_path)
    if gate_value.empty:
        st.info(empty_message("gate_value"))
    else:
        rows = []
        muted = theme.active_palette().muted
        for row in gate_value.itertuples():
            row_style = f"color:{muted};opacity:.65" if row.low_sample else ""
            rows.append(
                "<tr style='{}'><td>{}</td><td>{}</td><td>{}</td>"
                "<td>{}</td><td>n = {}</td></tr>".format(
                    row_style,
                    escape(str(row.gate_id)),
                    escape(str(row.stage)),
                    escape(format_currency(row.gate_value)),
                    escape(format_currency(row.avg_blocked_pnl_per_spread)),
                    int(row.shadow_n),
                )
            )
        st.markdown(
            "<table style='width:100%'><thead><tr><th>Gate</th><th>Stage</th>"
            "<th>GateValue</th><th>Blocked mean P&amp;L</th><th>Sample</th>"
            "</tr></thead><tbody>" + "".join(rows) + "</tbody></table>",
            unsafe_allow_html=True,
        )
        st.caption("Grey rows have fewer than five completed shadows. Their value is shown, but the evidence is thin.")

    st.markdown("#### Tier timeline")
    tiers = queries.get_tier_timeline(database_path)
    if tiers.empty:
        st.info("No configuration version has been activated yet.")
    else:
        timeline = tiers[["config_version", "tier", "activated_at", "deactivated_at", "note"]].copy()
        timeline["activated_at"] = timeline["activated_at"].map(format_et)
        timeline["deactivated_at"] = timeline["deactivated_at"].map(format_et)
        st.dataframe(timeline, width="stretch", hide_index=True)

    st.markdown("#### Profitable trades that gates blocked")
    blocked = queries.get_profitable_blocked_trades(database_path)
    if blocked.empty:
        st.info("No completed rejected shadow has positive P&L yet.")
    else:
        display = blocked.drop(columns=["structure_json"], errors="ignore").copy()
        for column in ["occurred_at", "entry_timestamp", "horizon_end"]:
            display[column] = display[column].map(format_et)
        st.dataframe(
            display,
            width="stretch",
            hide_index=True,
            column_config={
                "final_pnl_per_spread": st.column_config.NumberColumn(format="$%.0f")
            },
        )
