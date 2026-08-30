"""
Alpha Council v2.3 - Section 5.2 Day-0 Data Reality Probe, v2 (clock-aware).

Changes from v1, all driven by the 2026-08-28 run that straddled the closing bell:
  * checks the market clock EVERY round and refuses to measure a closed market
  * detects a frozen feed by comparing quote timestamps across rounds
  * clamps negative lag (local clock skew) to zero instead of propagating it
  * records equity spread percentiles for the Section 11.1 confidence calibration
  * paginates the option contracts endpoint instead of silently hitting the limit
  * emits NO config recommendation unless it has >= 3 valid in-session rounds

Place at: scripts/probe_data_reality.py
Run at:   Monday 09:45 ET (well inside regular trading hours)

Usage:
    uv run python scripts/probe_data_reality.py
    uv run python scripts/probe_data_reality.py --rounds 6 --interval 90
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv

DATA_BASE = "https://data.alpaca.markets"
TRADE_BASE = "https://paper-api.alpaca.markets"
OUT_PATH = Path("data/probe_results.json")

PROBE_UNDERLYING = "SPY"
EQUITY_SYMBOLS = ["SPY", "QQQ", "NVDA", "AAPL", "JPM", "XOM"]
N_CONTRACTS = 10
MIN_VALID_ROUNDS = 3

# Stop taking new rounds this many seconds before the close, so we never
# repeat the v1 mistake of measuring a frozen post-close feed.
CLOSE_GUARD_SECONDS = 600


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def parse_ts(raw: str | None) -> datetime | None:
    """Parse Alpaca RFC3339 timestamps, which may carry nanosecond precision."""
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
        frac = (frac + "000000")[:6]
        s = f"{head}.{frac}{tz}"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def lag_seconds(ts: datetime | None, now: datetime) -> float | None:
    """Age of a quote in seconds, clamped at zero.

    Negative values mean the local clock trails Alpaca's. That is skew, not a
    quote from the future, and it must never reach the staleness logic.
    """
    if ts is None:
        return None
    return round(max(0.0, (now - ts).total_seconds()), 2)


def pct(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    idx = min(len(s) - 1, int(round(q * (len(s) - 1))))
    return round(s[idx], 4)


def stats(values: list[float]) -> dict[str, float | int]:
    clean = [v for v in values if v is not None]
    if not clean:
        return {"n": 0}
    return {
        "n": len(clean),
        "min": round(min(clean), 2),
        "median": round(statistics.median(clean), 2),
        "p90": pct(clean, 0.90),
        "p99": pct(clean, 0.99),
        "max": round(max(clean), 2),
    }


def say(msg: str = "") -> None:
    print(msg, flush=True)


def rule(title: str) -> None:
    say("")
    say("=" * 72)
    say(title)
    say("=" * 72)


# --------------------------------------------------------------------------
# api client
# --------------------------------------------------------------------------

class Alpaca:
    def __init__(self, key: str, secret: str, option_feed: str, stock_feed: str):
        self.option_feed = option_feed
        self.stock_feed = stock_feed
        self.client = httpx.Client(
            timeout=20.0,
            headers={
                "APCA-API-KEY-ID": key,
                "APCA-API-SECRET-KEY": secret,
                "Accept-Encoding": "gzip, deflate",
            },
        )

    def get(self, url: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        r = self.client.get(url, params=params)
        if r.status_code >= 400:
            raise RuntimeError(f"{r.status_code} {url}\n{r.text[:500]}")
        return r.json()

    def clock(self) -> dict[str, Any]:
        return self.get(f"{TRADE_BASE}/v2/clock")

    def account(self) -> dict[str, Any]:
        return self.get(f"{TRADE_BASE}/v2/account")

    def stock_snapshots(self, symbols: list[str]) -> dict[str, Any]:
        return self.get(
            f"{DATA_BASE}/v2/stocks/snapshots",
            {"symbols": ",".join(symbols), "feed": self.stock_feed},
        )

    def option_contracts_paged(self, underlying: str, lo: float, hi: float,
                               d_from: date, d_to: date,
                               max_pages: int = 8) -> list[dict[str, Any]]:
        """Follow next_page_token. v1 silently truncated at the 200 limit."""
        out: list[dict[str, Any]] = []
        token: str | None = None
        for _ in range(max_pages):
            params: dict[str, Any] = {
                "underlying_symbols": underlying,
                "status": "active",
                "type": "call",
                "strike_price_gte": f"{lo:.2f}",
                "strike_price_lte": f"{hi:.2f}",
                "expiration_date_gte": d_from.isoformat(),
                "expiration_date_lte": d_to.isoformat(),
                "limit": 200,
            }
            if token:
                params["page_token"] = token
            payload = self.get(f"{TRADE_BASE}/v2/options/contracts", params)
            out.extend(payload.get("option_contracts", []))
            token = payload.get("next_page_token")
            if not token:
                break
        return out

    def option_snapshots(self, occ_symbols: list[str]) -> dict[str, Any]:
        return self.get(
            f"{DATA_BASE}/v1beta1/options/snapshots",
            {"symbols": ",".join(occ_symbols), "feed": self.option_feed},
        ).get("snapshots", {})


# --------------------------------------------------------------------------
# stages
# --------------------------------------------------------------------------

def stage_account(api: Alpaca, results: dict[str, Any]) -> bool:
    rule("STAGE 1 - ACCOUNT AND CLOCK")
    acct = api.account()
    clk = api.clock()

    equity = float(acct.get("equity", 0))
    is_open = bool(clk.get("is_open"))
    next_close = parse_ts(clk.get("next_close"))
    now = parse_ts(clk.get("timestamp")) or datetime.now(timezone.utc)
    to_close = (next_close - now).total_seconds() if (is_open and next_close) else None

    results["account"] = {
        "account_number": acct.get("account_number"),
        "equity": equity,
        "options_approved_level": acct.get("options_approved_level"),
        "options_trading_level": acct.get("options_trading_level"),
        "status": acct.get("status"),
    }
    results["clock"] = {
        "is_open": is_open,
        "timestamp": clk.get("timestamp"),
        "next_open": clk.get("next_open"),
        "next_close": clk.get("next_close"),
        "seconds_to_close": to_close,
    }

    say(f"  account        : {acct.get('account_number')}  ({acct.get('status')})")
    say(f"  equity         : ${equity:,.2f}")
    say(f"  options level  : {acct.get('options_trading_level')} "
        f"(approved {acct.get('options_approved_level')})")
    say(f"  market open    : {is_open}")
    if to_close is not None:
        say(f"  time to close  : {to_close / 60:.1f} min")

    if not is_open:
        say("")
        say("  !! MARKET CLOSED. Aborting.")
        say("  !! A closed feed returns the last pre-close print, so measured")
        say("  !! lag grows by exactly the sleep interval each round and means")
        say("  !! nothing. Re-run during regular trading hours.")
        return False

    if to_close is not None and to_close < CLOSE_GUARD_SECONDS:
        say("")
        say(f"  !! Only {to_close / 60:.1f} min to the close. Aborting: rounds")
        say("  !! would straddle the bell. Run earlier in the session.")
        return False

    if equity < 99000 or equity > 101000:
        say("  !! WARNING: equity is not ~$100,000. Confirm the account.")
    try:
        if int(acct.get("options_trading_level") or 0) < 3:
            say("  !! BLOCKER: options level < 3. Debit verticals need level 3.")
    except (TypeError, ValueError):
        pass
    return True


def stage_contracts(api: Alpaca, results: dict[str, Any], spot: float) -> list[str]:
    rule("STAGE 2 - OPTION CONTRACTS (open interest availability)")
    today = datetime.now(timezone.utc).date()
    contracts = api.option_contracts_paged(
        PROBE_UNDERLYING, spot * 0.94, spot * 1.06,
        today + timedelta(days=7), today + timedelta(days=21),
    )
    if not contracts:
        raise RuntimeError("No option contracts returned for the 7-21 DTE window.")

    def oi(c: dict[str, Any]) -> int:
        try:
            return int(c.get("open_interest") or 0)
        except (TypeError, ValueError):
            return 0

    contracts.sort(key=oi, reverse=True)
    chosen = contracts[:N_CONTRACTS]
    has_oi = sum(1 for c in contracts if c.get("open_interest") is not None)
    oi_dates = sorted({c.get("open_interest_date") for c in contracts
                       if c.get("open_interest_date")})

    results["contracts"] = {
        "returned": len(contracts),
        "with_open_interest": has_oi,
        "open_interest_dates": oi_dates,
        "selected": [
            {"symbol": c.get("symbol"), "strike": c.get("strike_price"),
             "expiration": c.get("expiration_date"),
             "open_interest": c.get("open_interest"),
             "open_interest_date": c.get("open_interest_date")}
            for c in chosen
        ],
    }

    say(f"  contracts returned (paged) : {len(contracts)}")
    say(f"  with open_interest field   : {has_oi}")
    say(f"  open_interest as-of        : {oi_dates or 'NONE'}")
    for c in chosen:
        say(f"    {str(c.get('symbol')):<22} K={str(c.get('strike_price')):<8} "
            f"exp={c.get('expiration_date')} OI={c.get('open_interest')}")

    if oi_dates:
        try:
            staleness = (today - date.fromisoformat(oi_dates[-1])).days
            say(f"  open interest is {staleness} calendar day(s) stale -> treat as a")
            say("  coarse proxy only; session volume and spread carry the gate.")
        except ValueError:
            pass
    return [c["symbol"] for c in chosen]


def stage_rounds(api: Alpaca, results: dict[str, Any], occ: list[str],
                 rounds: int, interval: int) -> None:
    rule(f"STAGE 3 - FEED BEHAVIOR ({rounds} rounds, {interval}s apart)")

    opt_lags: list[float] = []
    eq_lags: list[float] = []
    eq_spreads: dict[str, list[float]] = {s: [] for s in EQUITY_SYMBOLS}
    greeks = iv_ok = seen = 0
    prev_opt_ts: dict[str, str] = {}
    prev_eq_ts: dict[str, str] = {}
    valid_rounds = 0
    frozen_rounds = 0
    samples: list[dict[str, Any]] = []
    spot = 0.0

    for i in range(1, rounds + 1):
        clk = api.clock()
        if not clk.get("is_open"):
            say(f"  round {i}: market closed mid-probe. Stopping.")
            break

        now = datetime.now(timezone.utc)
        eq_snaps = api.stock_snapshots(EQUITY_SYMBOLS)
        opt_snaps = api.option_snapshots(occ)

        # ---- equity ----
        eq_frozen = 0
        round_eq_lags: list[float] = []
        for sym in EQUITY_SYMBOLS:
            s = eq_snaps.get(sym) or {}
            q = s.get("latestQuote") or {}
            t = s.get("latestTrade") or {}
            bid, ask, raw_ts = q.get("bp"), q.get("ap"), q.get("t")
            lag = lag_seconds(parse_ts(raw_ts), now)
            if lag is not None:
                round_eq_lags.append(lag)
                eq_lags.append(lag)
            if bid and ask and ask >= bid:
                mid = (bid + ask) / 2
                if sym == PROBE_UNDERLYING:
                    spot = mid
                if mid > 0:
                    eq_spreads[sym].append((ask - bid) / mid)
            if raw_ts and prev_eq_ts.get(sym) == raw_ts:
                eq_frozen += 1
            if raw_ts:
                prev_eq_ts[sym] = raw_ts
            samples.append({
                "round": i, "kind": "equity", "symbol": sym,
                "bid": bid, "ask": ask, "last": t.get("p"),
                "quote_lag_s": lag, "quote_ts": raw_ts,
            })

        # ---- options ----
        opt_frozen = 0
        round_opt_lags: list[float] = []
        for sym, snap in opt_snaps.items():
            q = snap.get("latestQuote") or {}
            g = snap.get("greeks") or {}
            iv = snap.get("impliedVolatility")
            raw_ts = q.get("t")
            lag = lag_seconds(parse_ts(raw_ts), now)
            if lag is not None:
                round_opt_lags.append(lag)
                opt_lags.append(lag)
            seen += 1
            if g.get("delta") is not None:
                greeks += 1
            if iv is not None:
                iv_ok += 1
            if raw_ts and prev_opt_ts.get(sym) == raw_ts:
                opt_frozen += 1
            if raw_ts:
                prev_opt_ts[sym] = raw_ts
            samples.append({
                "round": i, "kind": "option", "symbol": sym,
                "bid": q.get("bp"), "ask": q.get("ap"),
                "quote_lag_s": lag, "quote_ts": raw_ts,
                "delta": g.get("delta"), "gamma": g.get("gamma"),
                "theta": g.get("theta"), "vega": g.get("vega"), "iv": iv,
            })

        o = stats(round_opt_lags)
        e = stats(round_eq_lags)
        frozen = i > 1 and len(opt_snaps) > 0 and opt_frozen == len(opt_snaps)
        if frozen:
            frozen_rounds += 1
        else:
            valid_rounds += 1

        flag = "  <-- FROZEN FEED, round discarded" if frozen else ""
        say(f"  round {i}/{rounds} {now.strftime('%H:%M:%SZ')}  "
            f"opt median={o.get('median')}s max={o.get('max')}s  "
            f"eq median={e.get('median')}s  "
            f"unchanged: opt {opt_frozen}/{len(opt_snaps)} "
            f"eq {eq_frozen}/{len(EQUITY_SYMBOLS)}{flag}")

        if i < rounds:
            time.sleep(interval)

    results["valid_rounds"] = valid_rounds
    results["frozen_rounds"] = frozen_rounds
    results["option_lag_seconds"] = stats(opt_lags)
    results["equity_lag_seconds"] = stats(eq_lags)
    results["greeks_coverage"] = {"quotes_seen": seen, "with_greeks": greeks,
                                  "with_iv": iv_ok}
    results["equity_spread_pct"] = {
        sym: {"n": len(v), "median": pct(v, 0.50), "p90": pct(v, 0.90),
              "max": round(max(v), 4) if v else None}
        for sym, v in eq_spreads.items()
    }
    results["spy_spot"] = spot
    results["samples"] = samples

    say("")
    say(f"  valid rounds     : {valid_rounds} (frozen/discarded: {frozen_rounds})")
    say(f"  OPTION quote lag : {results['option_lag_seconds']}")
    say(f"  EQUITY quote lag : {results['equity_lag_seconds']}")
    say(f"  greeks present   : {greeks}/{seen}    IV present: {iv_ok}/{seen}")
    say("")
    say("  equity quoted spread as % of mid (Section 11.1 calibration):")
    for sym, s in results["equity_spread_pct"].items():
        if s["n"]:
            say(f"    {sym:<5} median={s['median']:.4%}  p90={s['p90']:.4%}")
        else:
            say(f"    {sym:<5} no data")


def stage_recommend(results: dict[str, Any]) -> None:
    rule("STAGE 4 - RECOMMENDED CONFIG")

    if results.get("valid_rounds", 0) < MIN_VALID_ROUNDS:
        say(f"  Only {results.get('valid_rounds', 0)} valid rounds "
            f"(need {MIN_VALID_ROUNDS}). NO RECOMMENDATION ISSUED.")
        say("  Re-run mid-session. Do not edit config from this run.")
        return

    opt = results["option_lag_seconds"]
    eq = results["equity_lag_seconds"]
    p90 = float(opt["p90"])

    fresh = 60 if p90 <= 120 else max(60, int(round(p90 * 1.2 / 10) * 10))
    max_lag = min(max(int(round(max(p90 * 1.5, fresh * 2) / 60) * 60), 300), 1800)
    eq_p90 = float(eq["p90"]) if eq.get("n") else 0.0
    eq_fresh = max(5, int(round(eq_p90 * 2)))

    say("  # config/scoring.yaml")
    say("  options:")
    say(f"    fresh_quote_seconds: {fresh}")
    say(f"    max_quote_lag_seconds: {max_lag}")
    say("    max_underlying_drift_pct: 0.010")
    say("  equity:")
    say(f"    pre_submit_max_lag_seconds: {eq_fresh}")

    spreads = [v["p90"] for v in results.get("equity_spread_pct", {}).values()
               if v.get("n")]
    if spreads:
        worst = max(spreads)
        say(f"    high_confidence_max_spread_pct: {max(0.005, round(worst * 1.2, 4))}")
        if worst > 0.005:
            say("")
            say(f"    !! Worst p90 quoted spread is {worst:.3%}, above the 0.5%")
            say("    !! HIGH-confidence threshold in Section 11.1. On wide-quote")
            say("    !! names prefer last trade over quote mid as signal_price.")

    say("")
    if p90 < 90:
        say("  VERDICT: option quotes are effectively live. Section 5.4 delta")
        say("  adjustment stays as a guard but will rarely engage.")
    elif p90 < 400:
        say("  VERDICT: moderate delay. Delta adjustment engages sometimes.")
        say("  Keep the staleness buffer in the limit debit.")
    else:
        say("  VERDICT: substantial delay. Section 5.4 is LOAD-BEARING and")
        say("  underlying-driven exits (Section 18) are mandatory.")


# --------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rounds", type=int, default=6)
    ap.add_argument("--interval", type=int, default=90)
    args = ap.parse_args()

    load_dotenv()
    key = os.getenv("ALPACA_API_KEY", "").strip()
    secret = os.getenv("ALPACA_SECRET_KEY", "").strip()
    if not key or not secret:
        say("ERROR: ALPACA_API_KEY / ALPACA_SECRET_KEY missing from .env")
        return 1
    if os.getenv("ALPACA_PAPER_TRADE", "true").lower() != "true":
        say("ERROR: ALPACA_PAPER_TRADE must be true. Refusing to run.")
        return 1

    api = Alpaca(key, secret,
                 option_feed=os.getenv("ALPACA_OPTION_FEED", "indicative"),
                 stock_feed=os.getenv("ALPACA_DATA_FEED", "iex"))

    results: dict[str, Any] = {
        "probe_version": 2,
        "probe_run_at": datetime.now(timezone.utc).isoformat(),
        "stock_feed": api.stock_feed,
        "option_feed": api.option_feed,
        "rounds_requested": args.rounds,
        "interval_seconds": args.interval,
    }

    try:
        if not stage_account(api, results):
            OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
            results["aborted"] = "market_closed_or_near_close"
            OUT_PATH.write_text(json.dumps(results, indent=2), encoding="utf-8")
            return 3

        snaps = api.stock_snapshots([PROBE_UNDERLYING])
        q = (snaps.get(PROBE_UNDERLYING) or {}).get("latestQuote") or {}
        bid, ask = q.get("bp"), q.get("ap")
        spot = (bid + ask) / 2 if bid and ask else 0.0
        if not spot:
            raise RuntimeError("No usable SPY midpoint.")
        say(f"  SPY reference midpoint: {spot:.2f}")

        occ = stage_contracts(api, results, spot)
        stage_rounds(api, results, occ, args.rounds, args.interval)
        stage_recommend(results)
    except Exception as exc:  # noqa: BLE001
        say("")
        say(f"PROBE FAILED: {exc}")
        OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        results["error"] = str(exc)
        OUT_PATH.write_text(json.dumps(results, indent=2), encoding="utf-8")
        return 2

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(results, indent=2), encoding="utf-8")
    rule("DONE")
    say(f"  full results written to {OUT_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
