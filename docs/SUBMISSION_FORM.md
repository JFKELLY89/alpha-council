# Submission form — prepared answers

*Alpaca AI Trading Agents Hackathon · deadline 2026-09-04 15:00 UTC (11:00 ET). Paste-ready; trim to each field's length limit.*

## Identity

- **Project name:** Alpha Council
- **Team / handle:** JFKELLY89
- **Repository:** https://github.com/JFKELLY89/alpha-council (public)
- **Demo video:** *(paste hosted link)*
- **Contact:** jfkelly89@pm.me

## One-liner (≤ 140 chars)

An autonomous options desk that debates its trades, measures whether each of its own decision layers added value, and refuses to trust itself without evidence.

## Short description (≤ 100 words)

Alpha Council is an autonomous, options-native paper-trading desk with a governance chain modeled on a real trading floor. A deterministic quant funnel finds candidates; GPT analysts and a GPT portfolio manager debate and propose against Python-priced scenario payoff tables; a Claude Red Team attacks the proposal; a deterministic Risk Constitution has the final, un-overridable word. Every decision is stored three ways — what GPT proposed, what Claude changed, what executed — so the P&L contribution of each layer is a queryable number, and every rejected candidate gets a shadow record so the gates are audited too. Paper trading only, enforced in code.

## Long description (≤ 500 words)

**What it does.** Over the trading day one process runs a premarket brief, nine scheduled scans, sequential councils, a position monitor, a 15:35 entry cutoff, a 15:45 flatten, and a post-close lessons + evolution cycle. Each scan narrows ~130–170 discovered symbols (core universe + movers + most-actives + symbols injected from material news and SEC filings) to ≤5 finalists on EVENT and MOMENTUM tracks. Each finalist gets a council: Bull/Bear/Catalyst analysts, a scenario generator, a Python payoff engine pricing five real two-leg debit verticals under every scenario, a GPT Portfolio Manager that proposes or abstains with underlying-price invalidation rules, a Claude Red Team returning PASS/MODIFY/VETO, and one revision that may resize, switch structure, or withdraw but never increase risk. The Risk Constitution then decides APPROVE/RESIZE/REJECT/HALT deterministically; approved orders walk limit rungs on Alpaca paper, and every fill is calibrated against the indicative-adjusted mid.

**What actually happened (Sep 2–3, 2026).** 55 councils, 24 PM trade proposals, 19 completed Red Team reviews (18 MODIFY, 1 VETO), 11 Constitution approvals, five journaled lifecycles (one calibration, four council-approved alpha trades: BMNR, MSTR, CLSK, JNJ), realized −$251, $7.74 of the $100 GenAI budget. Day one produced zero alpha trades on a catalyst-thin tape — every abstain reasoned, the one survivor rejected 0.01 under the confidence floor. Day two traded after two operator decisions made on the record: continuation-conviction setups may proceed at reduced size, and a reduction floors at one spread instead of rounding to zero. Both post-close evolution cycles declined to propose a Challenger, the second naming the confound (too many configuration versions active). The dashboard shows all of it on real rows.

**Alpaca usage.** Alpaca's MCP server is the control plane — account state, the market clock, and positions — with the measured MCP share of control-plane calls reported in the Audit tab. Order execution deliberately uses Alpaca's REST API directly, because the idempotent submit-verify-recover semantics around each `client_order_id` are load-bearing safety behavior we were not willing to route through a second transport during competition week. Market data: IEX equities (same-feed, same-clock-window RVOL) and the Indicative options feed (timestamped legs, staleness buffers, presubmit re-quote, open interest from the contracts endpoint because Indicative snapshots carry none).

**What is original.** Counterfactual Decision Attribution (three variants per decision, selection vs sizing per layer), Gate Attribution (shadow records for every refused candidate — what conservatism saved or cost), and Champion/Challenger evolution that is shadow-only with operator-gated promotion; the code raises without a human.

## Judging-criteria mapping

| Criterion | Where to look |
|---|---|
| P&L performance | README "What actually happened"; Command Center; Counterfactual Lab (governance total −$1,634 hypothetical vs PM originals on 21 decisions — reported, not hidden) |
| Technology implementation | 526 tests; structured-output LLM calls metered per model; WAL SQLite journal; `close_all.py` standalone flatten; presubmit re-quote; restart-safe position monitor |
| Creativity & originality | Counterfactual Lab, Gate Lab, Alpha Evolution tabs |
| Presentation & execution | Dashboard (9 tabs, `?tab=` deep links), README screenshots, this write-up, the video |

## Tech stack

Python 3.11 · uv · Alpaca Trading API (paper) + Market Data (IEX, Indicative options) + Alpaca MCP server · OpenAI (gpt-5.6-sol / gpt-5.6-luna) · Anthropic (claude-sonnet-5) · SQLite (WAL) · APScheduler · Streamlit + Plotly · Pydantic

## Safety statement

Paper-only, asserted at every entry point; hard risk gates (2% per trade, 10% open, 4% sector, drawdown halts, liquidity floors, DTE bounds, 15:35 cutoff) live in config and never move at runtime; the Red Team can only reduce risk; evolution promotion raises without explicit operator approval; secrets stay in `.env` (untracked), the database is untracked.
