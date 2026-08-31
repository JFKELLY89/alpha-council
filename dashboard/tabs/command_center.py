"""Command Center tab."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import streamlit as st

from dashboard import queries
from alpha_council.utils.time import sessions_remaining, utc_now
from dashboard.formatting import (
    current_et_day_start_utc,
    empty_message,
    format_currency,
    format_et,
    format_integer,
    format_percent,
    parse_utc,
)


def _value(row: pd.Series, key: str):
    value = row.get(key)
    return None if value is None or pd.isna(value) else value


def render(database_path: str | Path) -> None:
    st.subheader("Command Center")
    st.caption("Account, exposure, spend, and the current Risk Constitution state.")

    metrics = queries.get_command_center_metrics(
        database_path, current_et_day_start_utc()
    )
    row = metrics.iloc[0]

    status = queries.get_session_status(
        database_path, current_et_day_start_utc()
    ).iloc[0]

    strip = st.columns(5)
    strip[0].metric("Sessions remaining", sessions_remaining())
    strip[1].metric("Scans today", format_integer(status["scans_today"]))
    strip[2].metric("Decisions today", format_integer(status["decisions_today"]))
    strip[3].metric("Rejections today", format_integer(status["rejections_today"]))

    last_scan = parse_utc(status["last_scan_at"])
    if last_scan is None:
        strip[4].metric("Last scan", "never")
    else:
        minutes = (utc_now() - last_scan).total_seconds() / 60
        strip[4].metric("Last scan", f"{minutes:,.0f} min ago")
        # Scheduled scans run at most 90 minutes apart, so a longer gap
        # during a session means the scheduler stopped.
        if minutes > 120:
            st.warning(
                f"No scan for {minutes:,.0f} minutes. If the market is open, "
                "the scheduler may have stopped."
            )
    st.divider()

    halt_count = int(row.get("halt_count") or 0)
    if halt_count > 0:
        st.error(
            "RISK CONSTITUTION HALT — new trading is stopped. "
            f"{halt_count} halt event(s), most recent "
            f"{format_et(_value(row, 'last_halt_at'))}."
        )

    top = st.columns(6)
    top[0].metric("Account equity", format_currency(_value(row, "account_equity")))
    top[1].metric("Competition P&L", format_currency(row["competition_pnl"]))
    top[2].metric("Day P&L", format_currency(row["day_pnl"]))
    top[3].metric("Peak equity", format_currency(_value(row, "peak_equity")))
    top[4].metric("Day drawdown", format_percent(_value(row, "daily_drawdown_pct")))
    top[5].metric(
        "Competition drawdown",
        format_percent(_value(row, "competition_drawdown_pct")),
    )

    bottom = st.columns(5)
    bottom[0].metric(
        "Open risk", format_percent(_value(row, "total_open_risk_pct_after"))
    )
    bottom[1].metric(
        "Sector exposure", format_percent(_value(row, "sector_risk_pct_after"))
    )
    bottom[2].metric("Active trades", format_integer(row["active_trade_count"]))
    bottom[3].metric(
        "Current tier",
        f"Tier {format_integer(_value(row, 'tier'))}"
        if _value(row, "tier") is not None
        else "—",
    )
    bottom[4].metric(
        "Risk state", _value(row, "risk_constitution_state") or "WAITING"
    )

    if _value(row, "latest_risk_evaluated_at") is None:
        st.info(empty_message("risk"))
    if float(row["competition_pnl"] or 0) == 0 and int(row["active_trade_count"] or 0) == 0:
        st.info(empty_message("trades"))

    positions_col, spend_col = st.columns([1.5, 1])
    with positions_col:
        st.markdown("#### Active positions")
        positions = queries.get_active_positions(database_path)
        if positions.empty:
            st.info(empty_message("positions"))
        else:
            display = positions.copy()
            display["captured_at"] = display["captured_at"].map(format_et)
            display["unrealized_plpc"] = display["unrealized_plpc"].map(
                lambda value: format_percent(value, fraction=True)
            )
            st.dataframe(
                display,
                use_container_width=True,
                hide_index=True,
                column_config={
                    "market_value": st.column_config.NumberColumn(format="$%.0f"),
                    "cost_basis": st.column_config.NumberColumn(format="$%.0f"),
                    "unrealized_pl": st.column_config.NumberColumn(format="$%.0f"),
                },
            )

    with spend_col:
        st.markdown("#### Provider spend")
        spend = queries.get_provider_spend(database_path)
        st.metric("Recorded spend", format_currency(row["provider_spend"], decimals=2))
        if spend.empty:
            st.info(empty_message("spend"))
        else:
            display = spend[["provider", "model", "requests", "cost_usd"]].copy()
            st.dataframe(
                display,
                use_container_width=True,
                hide_index=True,
                column_config={"cost_usd": st.column_config.NumberColumn(format="$%.2f")},
            )

    with st.expander("Tier audit detail"):
        st.write(
            {
                "config_version": _value(row, "config_version"),
                "activated_at_et": format_et(_value(row, "tier_activated_at")),
                "note": _value(row, "tier_note"),
                "historical_halts": int(row.get("halt_count") or 0),
                "last_halt_at_et": format_et(_value(row, "last_halt_at")),
            }
        )
