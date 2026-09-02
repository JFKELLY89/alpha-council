"""Counterfactual Lab tab."""

from __future__ import annotations

from html import escape
from pathlib import Path

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from dashboard import queries, theme
from dashboard.formatting import empty_message, format_currency, format_et, parse_json


def _effect_card(label: str, value, question: str) -> None:
    st.markdown(
        f"""
        <div class="ac-effect">
          <div class="ac-kicker">{escape(label)}</div>
          <div class="value">{escape(format_currency(value))}</div>
          <div class="ac-muted">{escape(question)}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def _selector(attributions: pd.DataFrame) -> str:
    labels = {
        row.decision_id: f"{row.symbol} · {format_et(row.as_of)} · {row.decision_id}"
        for row in attributions.itertuples()
    }
    return st.selectbox(
        "Attributed decision",
        attributions["decision_id"].tolist(),
        format_func=lambda value: labels[value],
        key="counterfactual_decision_id",
    )


def render(database_path: str | Path) -> None:
    st.subheader("Counterfactual Lab")
    st.caption("Did the Red Team and Risk Constitution add or destroy value?")

    totals = queries.get_attribution_totals(database_path)
    total_row = totals.iloc[0]
    if int(total_row["decisions"] or 0) == 0:
        st.info(empty_message("attribution"))
    else:
        st.markdown("#### Portfolio attribution")
        cols = st.columns(4)
        cols[0].metric("Decisions", int(total_row["decisions"]))
        cols[1].metric("Claude total", format_currency(total_row["claude_total"]))
        cols[2].metric("Risk total", format_currency(total_row["risk_total"]))
        cols[3].metric("Governance total", format_currency(total_row["governance_total"]))
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "layer": "Claude Red Team",
                        "selection": total_row["claude_selection"],
                        "sizing": total_row["claude_sizing"],
                        "reconciling_total": total_row["claude_total"],
                    },
                    {
                        "layer": "Risk Constitution",
                        "selection": total_row["risk_selection"],
                        "sizing": total_row["risk_sizing"],
                        "reconciling_total": total_row["risk_total"],
                    },
                ]
            ),
            width="stretch",
            hide_index=True,
            column_config={
                "selection": st.column_config.NumberColumn(format="$%.0f"),
                "sizing": st.column_config.NumberColumn(format="$%.0f"),
                "reconciling_total": st.column_config.NumberColumn(format="$%.0f"),
            },
        )

    attributions = queries.get_attribution_decisions(database_path)
    if attributions.empty:
        return

    decision_id = _selector(attributions)
    detail = queries.get_attribution_detail(database_path, decision_id).iloc[0]

    st.markdown("#### Three marked variants")
    variants = pd.DataFrame(
        [
            {
                "variant": "GPT Original",
                "quantity": detail["gpt_original_qty"],
                "pnl_per_spread": detail["gpt_original_pnl_per_spread"],
                "total_pnl": detail["gpt_original_pnl"],
                "interpretation": "Original PM choice",
            },
            {
                "variant": "Claude Modified",
                "quantity": detail["claude_modified_qty"],
                "pnl_per_spread": detail["claude_modified_pnl_per_spread"],
                "total_pnl": detail["claude_modified_pnl"],
                "interpretation": "Trade avoided" if detail["claude_modified_qty"] == 0 else "Red Team result",
            },
            {
                "variant": "Executed",
                "quantity": detail["executed_qty"],
                "pnl_per_spread": detail["executed_pnl_per_spread"],
                "total_pnl": detail["executed_pnl"],
                "interpretation": "Risk-approved execution",
            },
        ]
    )
    st.dataframe(
        variants,
        width="stretch",
        hide_index=True,
        column_config={
            "pnl_per_spread": st.column_config.NumberColumn(format="$%.0f"),
            "total_pnl": st.column_config.NumberColumn(format="$%.0f"),
        },
    )
    if detail["claude_modified_qty"] == 0:
        st.info(
            "Trade avoided: the Claude-modified quantity is zero. The GPT Original row shows what the avoided trade would have done."
        )

    st.markdown("#### From the PM's trade to the executed trade")
    steps = [
        detail["gpt_original_pnl"],
        detail["claude_selection_effect"],
        detail["claude_sizing_effect"],
        detail["risk_selection_effect"],
        detail["risk_sizing_effect"],
        detail["executed_pnl"],
    ]
    palette = theme.active_palette()
    waterfall = go.Figure(
        go.Waterfall(
            orientation="v",
            measure=["absolute", "relative", "relative", "relative",
                     "relative", "total"],
            x=["GPT original", "Claude selection", "Claude sizing",
               "Risk selection", "Risk sizing", "Executed"],
            y=steps,
            text=[format_currency(value) for value in steps],
            textposition="outside",
            connector={"line": {"width": 1, "color": palette.border}},
            # Financial semantics stay conventional in both modes: a step
            # that added money is green, one that cost money is red, and
            # the anchoring totals wear the gold.
            increasing={"marker": {"color": palette.gain}},
            decreasing={"marker": {"color": palette.loss}},
            totals={"marker": {"color": palette.gold}},
        )
    )
    waterfall.update_layout(
        margin=dict(l=10, r=10, t=25, b=10),
        yaxis_title="P&L ($)",
        showlegend=False,
    )
    theme.plot(waterfall, height=380)
    st.caption(
        "Each step is one governance layer's measured effect. The bars walk "
        "from what the PM proposed to what was actually executed."
    )

    st.markdown("#### Four effects")
    effects = st.columns(4)
    with effects[0]:
        _effect_card("Claude selection", detail["claude_selection_effect"], "Did the Red Team select a different expression?")
    with effects[1]:
        _effect_card("Claude sizing", detail["claude_sizing_effect"], "Did the Red Team change only the size?")
    with effects[2]:
        _effect_card("Risk selection", detail["risk_selection_effect"], "Did the risk layer change the expression?")
    with effects[3]:
        _effect_card("Risk sizing", detail["risk_sizing_effect"], "Did deterministic sizing help or hurt?")

        # Computed here rather than read from a column, so the identity is
    # demonstrated rather than asserted. selection + sizing must equal the
    # stored total; a visible tick is what a sceptical reader wants.
    claude_sum = detail["claude_selection_effect"] + detail["claude_sizing_effect"]
    risk_sum = detail["risk_selection_effect"] + detail["risk_sizing_effect"]

    reconciliation = pd.DataFrame(
        [
            {
                "layer": "Claude",
                "selection": detail["claude_selection_effect"],
                "sizing": detail["claude_sizing_effect"],
                "selection_plus_sizing": claude_sum,
                "stored_total": detail["claude_value_added"],
                "reconciles": abs(claude_sum - detail["claude_value_added"]) < 0.01,
            },
            {
                "layer": "Risk Constitution",
                "selection": detail["risk_selection_effect"],
                "sizing": detail["risk_sizing_effect"],
                "selection_plus_sizing": risk_sum,
                "stored_total": detail["risk_constitution_value_added"],
                "reconciles": abs(risk_sum - detail["risk_constitution_value_added"]) < 0.01,
            },
        ]
    )
    st.dataframe(
        reconciliation,
        width="stretch",
        hide_index=True,
        column_config={
            **{
                name: st.column_config.NumberColumn(format="$%.0f")
                for name in ["selection", "sizing", "selection_plus_sizing",
                             "stored_total"]
            },
            "reconciles": st.column_config.CheckboxColumn("Reconciles"),
        },
    )
    st.caption(
        "Selection effect + sizing effect = stored total. Negative values are "
        "retained as legitimate findings, not errors."
    )

    st.caption("Selection effect + sizing effect = stored total. Negative values are retained as legitimate findings.")

    notes = parse_json(detail["notes_json"], fallback={})
    if isinstance(notes, dict) and notes.get("narrative"):
        st.markdown(f"**Recorded narrative:** {notes['narrative']}")

    marks = queries.get_shadow_marks(database_path, decision_id)
    if not marks.empty:
        marks = marks.copy()
        marks["marked_at_et"] = pd.to_datetime(marks["marked_at"], utc=True).dt.tz_convert("America/New_York")
        figure = px.line(
            marks,
            x="marked_at_et",
            y="unrealized_pnl",
            color="variant",
            markers=True,
            # One identity per variant in every chart and both modes: the
            # PM's original is council blue, the Red Team's is Evolution
            # purple, and what actually executed wears the gold.
            color_discrete_map=theme.variant_colors(theme.active_palette()),
            labels={"marked_at_et": "Marked at (ET)", "unrealized_pnl": "Unrealized P&L ($)"},
        )
        figure.update_layout(margin=dict(l=10, r=10, t=25, b=10))
        theme.plot(figure)
