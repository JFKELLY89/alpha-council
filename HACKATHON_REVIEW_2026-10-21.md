# Alpaca AI Trading Agents Hackathon — Winners Review & Incorporation Decisions

**Date:** 2026-10-21. Sources: lablab.ai winners listing and project pages, lablab's X announcement, and the winners' repositories (cloned and read; the 1st-place repo is a third-party archive — the original was deleted — so treat details as high-but-not-perfect confidence).

## Standings

| Place | Project | Team | Published result |
|---|---|---|---|
| 1st (+Featherless Prize) | **Autobelay — "Long Premium, Short Leash"** | RazorsEdge (W.P. & W.C. McCormick) | **+5.10%** ($100k → $105,095.51) |
| 2nd | **Should-AI Buy?** | SquadBlessingMiracle | no P&L published |
| 3rd | **Killswitch Capital** | Team Quantus | "Claude proposes, fifteen deterministic gates dispose" |

Finalists included Deflow and PRISM. ~3,600 participants, 425 shipped projects. **Alpha Council did not place and received no public mention.**

**The only public judge commentary** is the organizers' announcement line: *"The best ones had one rule: the AI never pulls the trigger."* Judging criteria (no weights published): P&L Performance, Technology Implementation, Creativity & Originality, Presentation & Execution.

## The uncomfortable, useful conclusion

**The entire podium is Alpha Council's architecture** — LLM proposes, deterministic code disposes; the organizers made that the headline. Alpha Council was in the winning lane and lost on the scoreboard, not the drawing board: +5.1% over 15 round trips beat −$251 over 5 lifecycles with a zero-trade day one, and "P&L Performance" led the criteria. Secondary factors: Autobelay's pure-MCP compliance and radical self-honesty; Should-AI-Buy's product-grade demo surface (its "AI council" is mostly if/else with an LLM veto, and it never actually places an option order — presentation carried it).

## What stood out in the winners' code

**Autobelay (1st)** — single-leg long options premium (2–45 DTE) on 5 liquid names, 10-minute cycles, open-source models via Featherless, everything through Alpaca's MCP server. Its real edge is **operational epistemics**:
- Market-implied priors (Kalshi daily index ranges + the chain's own implied distribution) fed to the model as two independent "crowds," usability-gated, and **Brier-scored nightly** against what the model was actually handed.
- **Rhetoric audits**: every percentage the model cites is matched to the prior it was given (`unsupported`/`misattributed`); exit *reasons* are graded against account facts. Caught the model inventing "forced expiry" pressure that no code imposed.
- **No self-review**: the nightly critique model is chosen from the journal as "first model that did not trade today"; violating pins are refused.
- **Identity guard** keyed on the broker-*reported* account number, asymmetric (test runs fail closed; the judged account warns-and-proceeds on unreadable identity).
- Live 3-account champion/challenger with per-account halts; runtime overrides expire at the close; config hash journaled per cycle. 941 credential-free tests; docstrings cite incident numbers.
- Honest weaknesses: no spreads (naked theta/IV exposure), regex-JSON decisions, market orders everywhere, 4 days of evidence, and its Kimi challenger lost −7.04% on the identical harness.

**Should-AI Buy? (2nd)** — Next.js platform, 7-stage "council," thesis-health monitoring daemon, polished judge-access dashboard. Under the hood: deterministic if/else "agents" with template prose; the only runtime LLM is a veto-only synthesis stage that can downgrade but never create a BUY (malformed output fails closed — genuinely nice). Options are advisory only — production orders are market buys of the underlying. The "897/897 tests" never import a production module. Lesson: presentation weighed heavily; fail-closed LLM veto is a sound pattern.

## Incorporation decisions (pros / cons / disposition)

| # | Idea (source) | Pros | Cons | Disposition |
|---|---|---|---|---|
| 1 | **Broker-identity guard** (Autobelay) | Cheap insurance against the mis-keyed-credentials catastrophe class; asymmetric fail-closed/warn semantics; the paper lock doesn't cover *which* paper account | None material (a rotated account requires updating one env var — that is the guard working) | **INCORPORATED 2026-10-21**: `AlpacaRestClient.assert_account_identity`, wired into `run_alpha_council` + `close_all`, armed via `ALPACA_EXPECTED_ACCOUNT_LAST4`, tested |
| 2 | **Recent-outcomes block in the PM prompt** + gate-rejection repeat counters (Autobelay) | Stops intra-session re-proposals of refused trades; PM currently gets zero outcome feedback; saves council spend | Any PM-prompt change shifts behavior unpredictably — on a PM that already over-abstains, a litany of losses could suppress the few winners too | Recommend; stage behind one supervised session |
| 3 | **Config fingerprint windowing for evolution** + overrides that expire at the close (Autobelay) | Directly removes the "too many config versions" confound that has twice made Alpha Evolution refuse to propose; every session starts from committed config | Per-config-span windows shrink sample sizes; override layer is a new mutation surface | Recommend: teach evolution to window on uniform-config spans first (no override layer yet) |
| 4 | **Rhetoric audits** — citation matching + exit-claim grading (Autobelay) | Reporting-only; catches hallucinated numbers exactly where Alpha Council treats prose as load-bearing (PM invalidation rules) | Matching has false positives (derived/rephrased numbers) → report noise erodes trust; medium build | Recommend — high value, needs careful matcher design |
| 5 | **Market-implied priors, Brier-scored** (Autobelay) | Free non-LLM directional second opinion; calibration yardstick strengthens the attribution story | New external dependency (Kalshi); index-level only while the desk trades single names; medium build | Recommend for SPY/QQQ candidates first |
| 6 | **Live second-paper-account challenger** (Autobelay) | Promotion evidence becomes measured (fills, slippage) instead of modeled | Large build; second credential set; doubles ops surface; more spend | Defer until a challenger exists at all |
| 7 | **Thesis-health state machine** (Should-AI-Buy daemon + Autobelay's then-vs-now framing) | Unifies VWAP/PM invalidation into one auditable HEALTHY/DEGRADED/INVALIDATED state; feeds attribution | Overlaps existing invalidation logic (redundancy risk); exit-behavior change on a strategy already losing to exits | Recommend after the entry-geometry question (see performance review) is settled |

Trivial mentions: spread-cost framing line in the PM prompt (one sentence, behavior-shifting → bundle with #2); webhook alerts on halts/exits (needs user's endpoint); fail-closed LLM veto pattern (Alpha Council's red team already fails closed — confirmed, no change).

## Context: same-day performance review

See the 2026-10-21 performance review (session record): one month, 61 closed alpha trades, 8 winners, −$3,339; invalidation exits 48/−$3,979 vs target exits 8/+$977; execution clean (123 fills, −$0.05 mean bias). Root operational causes fixed same day: lifetime-scoped budget thresholds (12-day analyst/learning lockout) → 30-day rolling window; 56 discarded catalyst assessments (signed-score schema mismatch) → prompt scale + recovery validator; busy_timeout 5s→15s; duplicate desk instances killed; identity guard added (above).
