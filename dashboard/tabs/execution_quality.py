"""Execution Quality tab."""

from __future__ import annotations

from pathlib import Path

import plotly.express as px
import streamlit as st

from dashboard import queries
from dashboard.formatting import empty_message, format_et


def render(database_path: str | Path) -> None:
    st.subheader("Execution Quality")
    st.info(
        "Alpaca's free Indicative feed is a derived estimate, not OPRA NBBO. "
        "This tab measures what that reference costs in actual fills."
    )

    bias = queries.get_fill_bias(database_path)
    estimates = queries.get_fill_bias_estimates(database_path)
    calibrations = queries.get_execution_calibrations(database_path)
    walks = queries.get_limit_walk_histogram(database_path)

    if bias.empty and calibrations.empty:
        st.info(empty_message("execution"))
        return

    st.markdown("#### OPEN and CLOSE summary")
    sides = sorted(set(bias["side"].dropna().tolist()) | set(estimates["side"].dropna().tolist()))
    for side in sides:
        st.markdown(f"##### {side}")
        side_bias = bias[bias["side"] == side].copy()
        side_est = estimates[estimates["side"] == side]
        if not side_est.empty:
            latest_time = side_est["computed_at"].max()
            side_est = side_est[side_est["computed_at"] == latest_time][
                ["direction", "sample_size", "median_bias", "median_seconds_to_fill"]
            ]
        else:
            st.caption(
                "No persisted median estimate exists for this side yet; the dashboard does not derive one from the fill rows."
            )
        if side_bias.empty:
            summary = side_est
        elif side_est.empty:
            summary = side_bias.drop(columns=["side"])
        else:
            summary = side_bias.drop(columns=["side"]).merge(
                side_est, on="direction", how="outer"
            )
        st.dataframe(
            summary,
            use_container_width=True,
            hide_index=True,
            column_config={
                "mean_bias": st.column_config.NumberColumn(format="$%.2f"),
                "median_bias": st.column_config.NumberColumn(format="$%.2f"),
                "mean_slippage_pct": st.column_config.NumberColumn(format="%.2f%%"),
                "mean_seconds_to_fill": st.column_config.NumberColumn(format="%.1fs"),
                "median_seconds_to_fill": st.column_config.NumberColumn(format="%.1fs"),
            },
        )

    if not calibrations.empty:
        plot_data = calibrations[calibrations["actual_fill_debit"].notna()].copy()
        chart_cols = st.columns(2)
        with chart_cols[0]:
            st.markdown("#### Fill slippage distribution")
            figure = px.histogram(
                plot_data,
                x="fill_slippage_pct",
                color="side",
                barmode="overlay",
                labels={"fill_slippage_pct": "Fill slippage (%)"},
            )
            figure.update_layout(margin=dict(l=10, r=10, t=20, b=10))
            st.plotly_chart(figure, use_container_width=True)
        with chart_cols[1]:
            st.markdown("#### Seconds to fill")
            figure = px.histogram(
                plot_data,
                x="seconds_to_fill",
                color="side",
                barmode="overlay",
                labels={"seconds_to_fill": "Seconds"},
            )
            figure.update_layout(margin=dict(l=10, r=10, t=20, b=10))
            st.plotly_chart(figure, use_container_width=True)

        table = calibrations.copy()
        for column in ["submitted_at", "filled_at"]:
            table[column] = table[column].map(format_et)
        with st.expander("Calibration rows"):
            st.dataframe(table, use_container_width=True, hide_index=True)

    st.markdown("#### Limit-walk attempts")
    if walks.empty:
        st.caption("No completed fill has a limit-walk observation.")
    else:
        figure = px.bar(
            walks,
            x="limit_walk_steps",
            y="fills",
            color="side",
            barmode="group",
            labels={"limit_walk_steps": "Limit-walk step", "fills": "Fills"},
        )
        figure.update_layout(margin=dict(l=10, r=10, t=20, b=10))
        st.plotly_chart(figure, use_container_width=True)
