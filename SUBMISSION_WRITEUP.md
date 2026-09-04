# Alpha Council — Submission Write-up

*Alpaca AI Trading Agents Hackathon · Team JFKELLY89 · September 2026*

**Demo video:** https://youtu.be/z-F0h_6jOp0 · **Code:** https://github.com/JFKELLY89/alpha-council

Alpha Council is an autonomous, options-native paper-trading desk with a governance chain modeled on a real trading floor: a deterministic quant funnel finds candidates, GPT analysts and a GPT portfolio manager debate and propose, a Claude red team attacks the proposal, a deterministic Risk Constitution has the final, un-overridable word — and a measurement layer records what every one of those decisions was worth. This document maps the build to the four judging criteria.

---

## 1. P&L Performance

Options give a paper account something stocks cannot: **defined-risk directional expression**. Every trade is a two-leg debit vertical, so maximum loss is known to the dollar before submission and the Risk Constitution sizes against that number, never a stop-loss hope.

What drives entries:

- **Dynamic discovery** — a per-scan funnel (~130 discovered → 30 stage-0 → ~20 pre-scored → ≤5 final) built from a core universe plus movers, most-actives, and symbols injected from material news and SEC filings the same morning they break. Candidates carry an EVENT or MOMENTUM track label, scored by track-specific weights.
- **Scenario reasoning** — before the PM sees a candidate, a scenario generator produces three bounded price paths and a Python payoff engine prices five real spreads under each. The PM picks a structure (or abstains) against actual payoff tables, not adjectives.
- **Trade-expression analysis** — the Red Team's most common live critique is precisely trade expression ("your own scenario table loses in STALL"), and revisions may switch structures or withdraw but may never increase risk.
- **Post-trade learning** — every close feeds fill calibration (measured open-side bias +$0.13, close-side +$0.005 on a ~$5.30 spread) and post-trade lessons; every rejected candidate gets a shadow record so gate costs are measurable too.

**What the two live sessions produced (Sep 2–3, real numbers):** 55 councils, 24 Portfolio Manager trade proposals, 19 completed Claude Red Team reviews (18 MODIFY, 1 VETO), 11 Risk Constitution approvals, and five journaled trade lifecycles — one calibration lifecycle and four council-approved alpha trades (BMNR, MSTR, CLSK, JNJ), every one of them opened and closed through the full open-walk / monitor / close path. Realized P&L: **−$251** (BMNR +$12, MSTR +$10, CLSK −$72, JNJ −$192, calibration −$9), with a meaningful share of the two losses being spread-crossing cost from the forced 15:45 competition flatten rather than adverse movement.

Day one produced zero alpha trades on a catalyst-thin tape: the PM abstained or withdrew on revision because a debit vertical's own scenario table lost under STALL, and the Constitution rejected the one survivor at 0.51 confidence against a 0.52 floor. We consider that discipline a P&L feature. Day two turned it into trades with two operator decisions made on the record that morning: continuation-conviction setups may proceed at *reduced* size instead of dying to stall exposure, and a reduction floors at **one spread** instead of rounding to zero — two fully approved trades had been vetoed by granularity alone. The same afternoon the Red Team issued its first VETO, the seat allocator reserved its first event-track hearing, and the Constitution rejected a fifth MSTR add-on against an already crypto-heavy book. Small sample, honest sign, every decision queryable.

## 2. Technology Implementation

- **Alpaca REST + MCP, used for what each is best at.** Alpaca's MCP server is Alpha Council's control plane: account state, the market clock, and positions are served over MCP, with the measured MCP share of control-plane calls reported in the Audit tab. Order execution deliberately uses Alpaca's REST API directly, because the idempotent submit-verify-recover semantics around each `client_order_id` are load-bearing safety behavior we were not willing to route through a second transport during competition week.
- **A fully autonomous decision loop.** One process runs the trading day: premarket brief, scheduled scans, sequential councils, a breadth/quality tier ladder that widens the search before it ever relaxes standards, a position monitor with advisory marks, a 15:35 new-trade cutoff, a 15:45 flatten, and a post-close lessons + evolution cycle. Every job is isolated; a failed scan cannot take down the day.
- **Structured GenAI throughout.** Every LLM call returns schema-validated JSON (OpenAI structured outputs; Anthropic `output_config` with a range-constraint-stripping adapter), is budget-metered per model with unknown models billed at the worst-case rate, and is recorded in `agent_runs` with tokens, cost, and errors. LLMs propose; deterministic code decides.
- **Feed honesty as engineering.** Equities on IEX with same-feed, same-clock-window RVOL; options on the Indicative feed with staleness buffers, spread-adjusted mids, and a presubmit re-quote of both legs; open interest fetched from the contracts endpoint because we measured that Indicative snapshots simply do not carry it (12,746 contracts, zero OI fields). When a data source fails, its gate stands down loudly instead of fabricating a neutral value.
- **Resilience details that came from live fire:** multi-leg limit walks (ascending debit opens, descending credit closes) with a fill-race guard after every cancel, order adoption on restart, WAL-mode SQLite shared safely with operational scripts, and a standalone `close_all.py` flatten that exits non-zero if either book is not flat.

## 3. Creativity & Originality

Three mechanisms we have not seen together in a trading agent:

1. **Counterfactual Decision Attribution.** Every decision persists three variants — what GPT originally proposed, what Claude's review changed it to, and what executed. P&L differences decompose into selection effect and sizing effect per layer, so "did the Red Team add value?" and "what did the Risk Constitution's resize cost?" are queryable numbers. A VETO is recorded as sizing-to-zero, so refusals are measured too.
2. **Gate Attribution.** Candidates rejected by quantitative gates get shadow structures marked forward. The Gate Lab can therefore show what each gate family *saved or cost*, with sample sizes — the system audits its own conservatism.
3. **Champion/Challenger evolution with enforced humility.** A post-close agent reads the day's measured outcomes and lessons and may propose a Challenger — a bounded mutation of whitelisted quality parameters only, validated against immutable paths, run shadow-only against the Champion. Promotion requires explicit operator approval, and the code raises without it. On its first live day it reviewed 50 decisions and declined to propose anything: "insufficient outcome evidence — one closed CALIBRATION trade." After the second day it reviewed 77 decisions and five closed trades and declined again, this time naming the confound: "numerous configuration/tier versions were active during the period." A learning loop that refuses to overfit its first two days — and can tell a small sample from a contaminated one — is the design working, not a limitation.

## 4. Presentation & Execution

The dashboard (Streamlit, dark/light brand theme) is built to answer the four questions judges actually ask, on real rows:

- **Why was this trade found?** Discovery tab: the funnel narrowing live, per-gate rejection counts, track labels, injected-news candidates.
- **Why was it modified or rejected?** Council tab: analyst evidence, scenario payoff tables, the PM's thesis and invalidation rules, the Red Team's verdict with its strongest counterargument, the revision diff, and the Constitution's violation list (e.g. the live one: *PM confidence 0.51 below the tier floor 0.52*).
- **Did governance add value?** Counterfactual Lab: per-layer selection/sizing attribution; Gate Lab for the refused trades.
- **What did the system learn?** Evolution tab: lessons with evidence for *and against*, the Champion's config, Challenger status, and the promotion gate that stays closed without evidence.

Execution quality is its own tab: indicative-adjusted reference vs actual fills, limit-walk behavior rung by rung, and both-side calibration bias from the journaled lifecycle.

**Efficiency note:** the entire competition — 22 full scans, 55 councils, 19 Red Team reviews, two post-close lessons + evolution cycles — cost **$7.74 of the $100 GenAI budget** (392 OpenAI calls, 21 Anthropic calls). The architecture spends models where they change decisions and Python everywhere else: 15,982 gate rejections across 948 symbols were decided by arithmetic, and 13,720 intelligence events were scored before a single model was asked anything.

---

## Honest framing

The calibration and evolution loop is instrumentation that would enable learning at scale; the competition run is its first, deliberately small sample. With tens of decisions, refitting weights would be noise-chasing — so Alpha Council records counterfactuals, writes falsifiable lessons, and makes the Challenger wait for a sample worth acting on.

**Alpha Council does not just explain its trades. It measures whether its own reasoning helped, learns from those measurements, and tests better versions of itself before trusting them with capital.**
