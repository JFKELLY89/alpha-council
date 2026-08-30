"""
Alpha Council v2.4 - time and market-session utilities.

All storage is UTC. All display and all session logic is America/New_York.
The RVOL baseline depends on stable clock-window bucketing, so the
same-clock-window helper lives here rather than in the scanner.

Place at: alpha_council/utils/time.py
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

RTH_OPEN = time(9, 30)
RTH_CLOSE = time(16, 0)
EARLY_CLOSE = time(13, 0)

# Competition-week anchors (spec §1, §16.1).
COMPETITION_START = date(2026, 8, 28)
COMPETITION_LAST_SESSION = date(2026, 9, 3)
COMPETITION_FLATTEN_ET = datetime(2026, 9, 3, 15, 45, tzinfo=ET)
SUBMISSION_DEADLINE_ET = datetime(2026, 9, 4, 11, 0, tzinfo=ET)

# 2026 US market holidays relevant to the build window.
MARKET_HOLIDAYS_2026 = {
    date(2026, 1, 1), date(2026, 1, 19), date(2026, 2, 16),
    date(2026, 4, 3), date(2026, 5, 25), date(2026, 6, 19),
    date(2026, 7, 3), date(2026, 9, 7), date(2026, 11, 26),
    date(2026, 12, 25),
}
EARLY_CLOSE_DAYS_2026 = {date(2026, 11, 27), date(2026, 12, 24)}


# ----------------------------------------------------------------------
# conversion
# ----------------------------------------------------------------------

def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def et_now() -> datetime:
    return datetime.now(ET)


def to_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def to_et(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(ET)


def iso_utc(dt: datetime | None = None) -> str:
    """Canonical format for every TEXT timestamp column."""
    return to_utc(dt or utc_now()).isoformat(timespec="microseconds")


def parse_alpaca_ts(raw: str | None) -> datetime | None:
    """Parse RFC3339 with nanosecond precision, which fromisoformat rejects."""
    if not raw:
        return None
    s = raw.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    if "." in s:
        head, rest = s.split(".", 1)
        if "+" in rest:
            frac, tz = rest.split("+", 1)
            tz = "+" + tz
        elif "-" in rest:
            frac, tz = rest.split("-", 1)
            tz = "-" + tz
        else:
            frac, tz = rest, ""
        s = f"{head}.{(frac + '000000')[:6]}{tz}"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(
        timezone.utc)


def rfc3339(dt: datetime) -> str:
    return to_utc(dt).isoformat(timespec="seconds").replace("+00:00", "Z")


def age_seconds(ts: datetime | None, now: datetime | None = None) -> float | None:
    """Age clamped at zero.

    A negative age means the local clock trails the exchange, which is skew,
    not a quote from the future. Observed at -0.08s against Alpaca on
    2026-08-28. It must never reach the staleness logic.
    """
    if ts is None:
        return None
    return round(max(0.0, (to_utc(now or utc_now()) - to_utc(ts)).total_seconds()), 3)


# ----------------------------------------------------------------------
# sessions
# ----------------------------------------------------------------------

def is_trading_day(d: date) -> bool:
    return d.weekday() < 5 and d not in MARKET_HOLIDAYS_2026


def session_close_time(d: date) -> time:
    return EARLY_CLOSE if d in EARLY_CLOSE_DAYS_2026 else RTH_CLOSE


def session_bounds(d: date) -> tuple[datetime, datetime] | None:
    """RTH open and close for a date, in ET. None on a non-trading day."""
    if not is_trading_day(d):
        return None
    return (
        datetime.combine(d, RTH_OPEN, tzinfo=ET),
        datetime.combine(d, session_close_time(d), tzinfo=ET),
    )


def is_rth(dt: datetime) -> bool:
    """True only inside regular trading hours.

    This is the filter that keeps extended-hours bars out of the RVOL
    baseline. Alpaca returned bars stamped 20:50Z on 2026-08-28 against a
    20:00Z close; including those makes the same-clock-window denominator
    unstable across sessions.
    """
    et = to_et(dt)
    bounds = session_bounds(et.date())
    if bounds is None:
        return False
    open_dt, close_dt = bounds
    return open_dt <= et < close_dt


def previous_trading_days(n: int, before: date | None = None) -> list[date]:
    """The n most recent trading days strictly before `before`, oldest first."""
    cursor = (before or et_now().date()) - timedelta(days=1)
    out: list[date] = []
    while len(out) < n:
        if is_trading_day(cursor):
            out.append(cursor)
        cursor -= timedelta(days=1)
    return list(reversed(out))


def trading_days_between(start: date, end: date) -> list[date]:
    days, cursor = [], start
    while cursor <= end:
        if is_trading_day(cursor):
            days.append(cursor)
        cursor += timedelta(days=1)
    return days


def minutes_since_open(dt: datetime) -> int | None:
    et = to_et(dt)
    bounds = session_bounds(et.date())
    if bounds is None:
        return None
    return int((et - bounds[0]).total_seconds() // 60)


def minutes_to_close(dt: datetime) -> int | None:
    et = to_et(dt)
    bounds = session_bounds(et.date())
    if bounds is None:
        return None
    return int((bounds[1] - et).total_seconds() // 60)


# ----------------------------------------------------------------------
# clock-window bucketing for RVOL
# ----------------------------------------------------------------------

def clock_window_index(dt: datetime, window_minutes: int = 15) -> int | None:
    """Which intraday window a timestamp falls in, counted from the open.

    RVOL compares the current window's volume against the median of the
    SAME window across prior sessions (spec §12.4). Bucketing from the open
    rather than from the wall clock keeps early-close days aligned.
    """
    mins = minutes_since_open(dt)
    if mins is None or mins < 0:
        return None
    et = to_et(dt)
    bounds = session_bounds(et.date())
    if bounds is None or et >= bounds[1]:
        return None
    return mins // window_minutes


def clock_window_label(index: int, window_minutes: int = 15) -> str:
    """Human label for a window index, e.g. 3 -> '10:15-10:30'."""
    start = datetime.combine(date(2000, 1, 1), RTH_OPEN) + timedelta(
        minutes=index * window_minutes)
    end = start + timedelta(minutes=window_minutes)
    return f"{start:%H:%M}-{end:%H:%M}"


def windows_per_session(d: date, window_minutes: int = 15) -> int:
    bounds = session_bounds(d)
    if bounds is None:
        return 0
    span = (bounds[1] - bounds[0]).total_seconds() / 60
    return int(span // window_minutes)


# ----------------------------------------------------------------------
# competition helpers
# ----------------------------------------------------------------------

def is_past_new_trade_cutoff(dt: datetime | None = None,
                             cutoff: time = time(15, 20)) -> bool:
    return to_et(dt or utc_now()).time() >= cutoff


def is_competition_flatten_time(dt: datetime | None = None) -> bool:
    return to_et(dt or utc_now()) >= COMPETITION_FLATTEN_ET


def sessions_remaining(dt: datetime | None = None) -> int:
    today = to_et(dt or utc_now()).date()
    if today > COMPETITION_LAST_SESSION:
        return 0
    return len(trading_days_between(max(today, COMPETITION_START),
                                    COMPETITION_LAST_SESSION))


def parse_et_time(hhmm: str) -> time:
    """Parse a config value like '12:30' into a time."""
    hour, minute = hhmm.strip().split(":")
    return time(int(hour), int(minute))


def et_time_reached(hhmm: str, dt: datetime | None = None) -> bool:
    """Used by the breadth/tier ladder to test schedule thresholds."""
    return to_et(dt or utc_now()).time() >= parse_et_time(hhmm)
