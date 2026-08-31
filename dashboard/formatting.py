"""Formatting and defensive parsing helpers for the dashboard UI."""

from __future__ import annotations

import json
from datetime import datetime, time, timezone
from typing import Any

import pandas as pd

from alpha_council.utils.time import ET, et_now, parse_alpaca_ts, to_et


EMPTY_STATES = {
    "risk": "No risk evaluation exists yet. Account and exposure metrics appear after the first council reaches the Risk Constitution.",
    "trades": "No trades have closed yet. Realized P&L appears after the first live session on Monday, August 31.",
    "positions": "No active positions. Position snapshots appear after an approved order fills.",
    "spend": "No provider usage has been recorded for this session.",
    "funnel": "No funnel snapshot exists yet. The scanner writes one after a discovery cycle completes.",
    "discovery": "No symbols have been discovered yet. Discovery reasons appear when the first scan runs.",
    "source_status": "No discovery-source probe has run yet. Availability is recorded at session startup.",
    "scanner": "No candidate scores exist yet. Scores appear after the first discovery pool is screened.",
    "decisions": "No council has started yet. End-to-end decision evidence appears after a candidate reaches council.",
    "attribution": "No attribution exists yet. Counterfactual effects require marked shadow variants for a completed decision.",
    "gates": "No gate rejections have been recorded yet. Gate evidence appears as candidates are filtered.",
    "gate_value": "No rejected shadow has reached a final mark yet. GateValue requires a hypothetical P&L outcome.",
    "execution": "No fills have been calibrated yet. Execution quality appears after the first order fills.",
    "audit": "No matching audit events have been recorded yet.",
}


def empty_message(key: str) -> str:
    return EMPTY_STATES.get(key, "No data is available for this panel yet.")


def _number(value: Any) -> float | None:
    if value is None or pd.isna(value):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def format_currency(value: Any, decimals: int = 0) -> str:
    number = _number(value)
    if number is None:
        return "—"
    sign = "−" if number < 0 else ""
    return f"{sign}${abs(number):,.{decimals}f}"


def format_percent(
    value: Any, decimals: int = 1, *, fraction: bool = False
) -> str:
    number = _number(value)
    if number is None:
        return "—"
    if fraction:
        number *= 100
    return f"{number:,.{decimals}f}%"


def format_number(value: Any, decimals: int = 1) -> str:
    number = _number(value)
    return "—" if number is None else f"{number:,.{decimals}f}"


def format_integer(value: Any) -> str:
    number = _number(value)
    return "—" if number is None else f"{number:,.0f}"


def parse_json(value: Any, fallback: Any = None) -> Any:
    """Parse a JSON cell without allowing partial rows to break the UI."""
    if value is None or (not isinstance(value, (dict, list)) and pd.isna(value)):
        return [] if fallback is None else fallback
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return [] if fallback is None else fallback


def pretty_json(value: Any) -> str:
    parsed = parse_json(value, fallback=value if value is not None else {})
    if isinstance(parsed, str):
        return parsed
    return json.dumps(parsed, indent=2, ensure_ascii=False, default=str)


def parse_utc(value: Any) -> datetime | None:
    if value is None or pd.isna(value):
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value
    return parse_alpaca_ts(str(value))


def format_et(value: Any, include_seconds: bool = False) -> str:
    parsed = parse_utc(value)
    if parsed is None:
        return "—"
    et = to_et(parsed)
    clock = et.strftime("%I:%M:%S %p" if include_seconds else "%I:%M %p").lstrip("0")
    return f"{et:%b} {et.day}, {et.year} {clock} ET"


def timestamp_series_et(series: pd.Series) -> pd.Series:
    return series.map(format_et)


def current_et_day_start_utc() -> str:
    start_et = datetime.combine(et_now().date(), time.min, tzinfo=ET)
    return start_et.astimezone(timezone.utc).isoformat()
