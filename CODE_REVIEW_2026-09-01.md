# Alpha Council — Whole-of-Model Code Review

**Date:** 2026-09-01 (mid-competition; sessions remaining: Sep 1–3)
**Scope:** v2.4 Implementation Specification, v2.5 Alpha Evolution Addendum, and the entire codebase (`alpha_council/`, `dashboard/`, `scripts/`, `config/`, `tests/` — ~25.5k lines) reviewed against both documents and for standalone correctness.
**Method:** every core module read line-by-line; every issue below verified against the actual call graph, not just the file it lives in; fixes applied in place and pinned by regression tests.
**Result:** 30 issues fixed in code, 9 deliberate deviations documented, and — in the evening session of Sep 1 — **all four missing spec features built** (Part 5) and **the operator checklist resolved** (Part 6), with the whole system preflighted end-to-end against live services twice. Test suite: **453 → 505 passing** (49 new/updated tests across two waves).

Severity legend:
- **P0** — would have caused a runtime crash, silent loss of a safety control, or wrong money math in live paper trading.
- **P1** — spec-conformance or correctness defect with material effect on results, attribution, or the demo claims.
- **P2** — hygiene, drift, or robustness.

---

## Part 1 — P0 issues (all fixed)

### 1. Position monitoring crashed on every poll — exits could never fire
**Where:** `alpha_council/alpaca/market_data.py` (`SymbolSnapshot`), called from `execution/position_monitor.py:poll()` and `journal/marks.py:_underlying_now()`.
**Problem:** both callers invoke `snap.signal_price(...)` on a `SymbolSnapshot`, but the class had no such method (it lives on the inner `QuoteObservation`). Every monitoring poll raised `AttributeError`, the scheduler's `_guarded` wrapper swallowed it into `system_events`, and **no exit rule — target, invalidation, time stop, flatten — could ever execute**. The test suite missed it because tests exercise the pure `evaluate_exit()` and stub the snapshot.
**Fix (applied):** added `SymbolSnapshot.signal_price()` delegating to `quote.signal_price()` with a minute-bar-close fallback.
**Step-by-step (as applied):**
1. Add `signal_price(prefer_last_above_spread_pct=0.010)` to `SymbolSnapshot`.
2. Return `quote.signal_price(...)`, falling back to `minute_bar_close` when the quote is unusable.
3. Regression tests: `test_symbol_snapshot_has_signal_price`, `test_signal_price_falls_back_to_minute_bar`.

### 2. Closing orders were submitted with opening (debit) semantics
**Where:** `execution/order_manager.py` (`build_intent`, `execute_with_walk`), `models/execution.py` (`ExecutionIntent.to_alpaca_payload`), `execution/position_monitor.py:close()`.
**Problem:** closing a debit vertical is a net **credit**; Alpaca's mleg convention is positive limit = debit, negative = credit. The close path reused the opening walk and submitted a **positive** limit — a marketable order straight through the market that could fill at any credit at all, including near zero. Exits could give away most of the position's value. Additionally, the close was priced from **entry-time** quotes, and no execution-calibration record was written for closes (spec §17.5 requires one for every submitted opening *and closing* spread).
**Fix (applied):**
1. `ExecutionIntent` gained `limit_is_credit`; a validator forces the flag to match the legs' close intents; the payload emits a negative limit for credits.
2. New `close_walk_prices(adjusted_mid, conservative, buffer)` — three **descending** credit targets from the current adjusted mid toward the conservative exit (`long_bid − short_ask`), floored at one tick, mirroring §17.3.
3. `execute_with_walk(..., closing=True, close_adjusted_mid=…, close_conservative=…)` uses the descending ladder; `PositionMonitor.close()` fetches **current** ADJUSTED_MID and CONSERVATIVE marks from the mark source and passes them (entry-time quotes only as a fallback).
4. `record_calibration(..., closing=True, …)` is now called on every close, with the conservative floor as the reference; `ExecutionCalibration`'s limit-vs-natural validator now enforces the correct inequality per side (open: limit ≤ natural; close: credit ≥ floor).
5. `_extract_fill_debit(..., closing=True)` nets sells-minus-buys so a closing fill reports the credit magnitude.
6. Regression tests: `test_close_walk_descends_toward_conservative_floor`, `test_close_walk_never_demands_below_one_tick`, `test_closing_payload_carries_negative_limit`, `test_fill_extraction_close_nets_credit`, `test_close_credit_may_not_fall_below_conservative_floor`.

### 3. The limit walk was structurally disabled — attempts 2 and 3 could never run
**Where:** `orchestrator.py:evaluate_candidate()` step 3, `risk/position_sizing.py`, `risk/constitution.py`, `models/risk.py`.
**Problem:** the orchestrator computed the walk's dollar ceiling as `approved_max_loss / qty / 100`. But `approved_max_loss = qty × max_loss_per_spread` and `max_loss_per_spread = initial_limit_debit × 100`, so the ceiling **equals the first rung of the ladder**. `walk_prices()` clips every price to the ceiling and drops non-increasing entries — the "three-step walk" (§17.3) degenerated to a single attempt, silently, on every order. The spec's anti-no-fill mechanism did not exist in practice.
**Fix (applied):**
1. `SizingResult` gained `budget_dollars` — the binding cap in dollars **before** flooring to whole spreads (`floor($1250/$520) = 2` spreads leaves $210 of granted headroom).
2. `RiskConstitution._size()` bounds it by the live portfolio/sector room (`portfolio_risk_room()` helper added).
3. `RiskEvaluation` gained `approved_risk_budget` (validated ≥ `approved_max_loss` when set; zeroed on REJECT/HALT).
4. The orchestrator prices the walk at `budget/qty/100` (never below the initial limit, never above natural — `walk_prices` still caps at natural).
5. Regression test: `test_budget_leaves_walk_headroom` proves ≥ 2 rungs exist under the corrected ceiling.
**Note:** total worst-case loss stays within the granted budget (min of PM request, 2% hard cap, Claude cap, portfolio room), which is precisely the "Risk Constitution's dollar ceiling" §17.3 names.

### 4. Cancel/fill race could double-submit a spread
**Where:** `execution/order_manager.py` (`execute_with_walk`, `get_order`).
**Problem:** when a walk step timed out, the code canceled and immediately submitted the next price. If the order **filled in the race between the last poll and the cancel** (Alpaca rejects the cancel of a filled order; the code ignored the cancel result), the next submission created a **second live spread for the same decision** — the exact "duplicate order" invariant §2.9 forbids. Compounding it, `get_order()` returned `None` for *every* error (`return None if exc.status == 404 else None`), so a transient 500 was indistinguishable from a missing order.
**Fix (applied):**
1. `get_order()` returns `None` only on 404 and raises otherwise; `_await_terminal` tolerates raised errors by continuing to poll until its deadline.
2. New `_confirm_after_cancel()`: after every cancel, re-fetch the order up to three times; a confirmed fill is adopted as the outcome; a confirmed cancel lets the walk continue; an **unknowable state aborts the walk** with `UNKNOWN_ORDER_STATE` and a loud `system_events` row — never submits another price on top of an order whose state is unknown.
3. Fill handling consolidated in `_record_fill()` (also fixes the fill path not updating the local order row's status).

### 5. The sector concentration cap was doubly dead
**Where:** `orchestrator.py` (wrong config file), `scheduler.py:_portfolio_state()` (never populated), `risk/constitution.py`.
**Problem:** (a) the orchestrator read the sector map from `universe.yaml`, which has no `sectors` key — the map lives in `risk_constitution.yaml` — so `sector_of()` returned `UNKNOWN` for every symbol; (b) the scheduler built `PortfolioState` without `sector_risk_dollars` at all, so accumulated sector exposure was always zero. Net effect: the 4% `max_sector_open_risk_pct` hard gate (§16.1) could never bind across positions — five concurrent 2% tech positions would all pass.
**Fix (applied):**
1. `RiskConstitution` now owns `self.sectors` from `risk_constitution.yaml`; the orchestrator reads the map from the constitution (with `universe.yaml` as fallback).
2. `_portfolio_state()` joins `trade_journal → decisions` for each open position's symbol and accumulates `sector_risk_dollars` by `sector_of(symbol, risk_config["sectors"])`.

### 6. Restart restored an arbitrary structure — exits keyed to wrong strikes
**Where:** `execution/position_monitor.py:restore()`.
**Problem:** the restore query joined `option_structures` on `decision_id` alone. A decision stores **up to five** structures; the join returned all of them and dict-insertion order decided which one the monitor tracked. Wrong short strike ⇒ wrong underlying-target exit, wrong max-profit basis, wrong breakeven — for a live position, after any restart.
**Fix (applied):** join through `risk_evaluations.structure_id` (the risk row records the approved structure; one per decision by construction), keep the old LEFT-JOIN behavior only as a defensive fallback for orphan rows, and restore the trade's `candidate_track` for correct close-side calibration labeling. Regression test: `test_restore_selects_risk_approved_structure`.

---

## Part 2 — P1 issues (all fixed)

### 7. Shadow book lost all state on restart — attribution silently froze
`ShadowBook._variants` was memory-only with no restore; the marking job reads `_variants`, so after any restart every existing decision marked nothing forever while its DB rows sat OPEN. **Fixed:** `ShadowBook.restore()` rebuilds `VariantState` from `shadow_trades` (OPEN and FLAT — vetoed variants must keep marking for the decomposition), wired into `run_alpha_council.py` startup. Test: `test_shadow_book_restore_rebuilds_variants`.

### 8. Executed variant kept "trading" after the real position closed
Nothing ever called `close_variant`/`close_all`; after a real exit the EXECUTED variant kept marking off market data, so attribution drifted away from the journal's realized P&L. **Fixed:** new `ShadowBook.close_decision(decision_id, executed_exit_debit, at)` freezes all variants at one timestamp — EXECUTED at the **actual exit credit**, counterfactuals at the market mark of the same moment — then computes and persists a final attribution. `PositionMonitor.close()` calls it after `journal.close_trade` (monitor now takes an optional `shadows` collaborator). Test: `test_close_decision_freezes_executed_at_actual_exit`.

### 9. VETO decomposition mislabeled sizing as selection
`compute()` forced `pnl_per_spread` to zero for qty-0 variants. Per the spec's own arithmetic (§7.5: `sizing_effect = (qty_B − qty_A) × pnl_per_spread_B`), pnl-per-spread is a property of the structure independent of quantity; zeroing it relabeled the entire VETO effect as *selection*. A VETO is a sizing-to-zero of the same structure. Also, the synthetic flat EXECUTED variant (no fill) now mirrors **Claude's** structure, not GPT's — the risk engine declined *Claude's* trade, and basing the flat variant on GPT's structure manufactured a phantom risk "selection effect". **Fixed** in `shadow_book.py:compute()`. Test: `test_veto_reads_as_sizing_effect_not_selection` (asserts selection ≈ 0, sizing = −qty×pnl).

### 10. Rejected-shadow instrumentation existed but was never wired — GateValue had nothing to measure
`RejectedShadowBook` was implemented and tested but **never instantiated in production**; no rejection ever created a `rejected_shadows` row and nothing marked them. §20.2's demo claim ("what each gate cost us on the trades we didn't take") was structurally empty. **Fixed:** `RejectionLog` accepts a `rejected_shadows` book and creates a row for every shadow-eligible rejection at flush (horizon = min(5 days, competition flatten)); the scheduler's `mark_shadows` job also runs `rejected_shadows.mark_open()`; `run_alpha_council.py` constructs and injects the book. Test: `test_rejection_log_creates_rejected_shadow`.

### 11. A PASS verdict silently resized trades through a channel attribution can't see
Three sites applied Claude's `recommended_max_risk_pct` even on PASS: `council.effective_risk_pct`, the orchestrator's `TradeRequest`, and a constitution ternary whose two branches were **identical** (`x if cond else x` — clearly a bug as written). But on PASS no CLAUDE_MODIFIED shadow variant exists (§19.1), so the size cut would surface in attribution as a *risk-engine* effect it didn't cause. **Fixed:** the cap now binds only on MODIFY, consistently in all three places. Tests updated/added: `test_red_team_cap_is_a_ceiling_not_a_floor` (MODIFY), `test_pass_verdict_does_not_cap_risk`, `test_pass_verdict_cap_is_ignored`, `test_resize_oversized_trade` (MODIFY).

### 12. Off-core news injection didn't exist (spec §10.2), and no market-wide sweep
`NewsIntelligence.collect()` dropped events for any symbol not already in the universe (`if symbol not in symbols: continue`), and the news query itself was filtered to universe symbols — so a fresh story about an off-universe name could never surface it. `DiscoveryService.refresh(news_symbols=…)` existed but nothing ever passed injections. **Fixed:** `collect()` gained `include_offcore` (score co-tagged off-universe symbols) and `market_wide` (one additional unfiltered recent-news query, deduped by article id); the scheduler's `_inject_offcore_news()` injects **material-catalyst** off-universe symbols into the universe via `Injection(source=ALPACA_NEWS)`. Every downstream gate (asset eligibility, options availability, data density, Stage-0) still applies — the injection only enters the pool, exactly as §10.2 requires.

### 13. `max_councils_per_day` was configured but never enforced
Tier configs carry per-day caps (30/16/18); nothing checked them — only `max_councils_per_scan`. **Fixed:** `_councils_remaining_today()` counts today's `decisions` rows (ET day, so a restart cannot reset the cap) and bounds each scan's council loop.

### 14. Funnel snapshots always recorded zero councils
`persist()` writes the snapshot before councils run and nothing updated it, so the Discovery Funnel tab's last stage read 0 forever. **Fixed:** `full_scan` updates `funnel_snapshots.councils_started` after the council loop.

### 15. A process left running overnight kept yesterday's tier, cutoff, and session
`TierManager.start_session()` was called once at process start; `cutoff_reached` was never reset; the session summary and budget day-window never rolled. Day 2 would open at yesterday's 14:15 Tier 3 with entries blocked. **Fixed:** `morning_reset()` (invoked from `refresh_discovery` and defensively from `full_scan`) resets the ladder, session summary, cutoff flag, and reloads budget day-windows when the ET date changes.

### 16. Scheduler cutoff hardcoded and divergent from the risk gate
`SchedulerConfig.new_trade_cutoff` defaulted to "15:35" and `from_config` never read it from anywhere, while the Risk Constitution reads `hard.new_trade_cutoff_et` from `risk_constitution.yaml` — two copies, already drifted. **Fixed:** `from_config(scoring, risk_cfg)` now derives the cutoff job from the same `hard.new_trade_cutoff_et` key the risk gate enforces. (See Part 4 on the 15:35-vs-spec-15:20 value itself.)

### 17. LLM-authored identity fields could corrupt scenario persistence
`ScenarioSet`'s schema forces the model to emit `scenario_set_id`, `decision_id`, `symbol`, `spot_at_generation`, `generated_at`; `persist_set()` stored them verbatim. A model echoing the same id twice would `INSERT OR REPLACE` over another decision's scenario set, and the model's idea of spot skewed every breakeven-move calculation in `PayoffSummary`. **Fixed:** `ScenarioGenerator.generate()` overwrites all five fields with server-side values before sanity checks and payoff computation — the same "the caller's value wins" rule `record_proposal` already applies to the PM's `decision_id`.

### 18. Adopted/recovered orders were never persisted
Both adoption paths in `submit_idempotent` returned without writing the `orders` row; later `_update_status` calls silently updated nothing — an audit hole against §2.10. **Fixed:** both paths persist the order.

### 19. Missing open interest passed every liquidity tier
`chain.py` skipped the OI check when the snapshot lacked the field (`if oi is not None and …`), so a contract with no OI at all sailed through a 250-minimum Tier 1 gate. **Fixed:** when the tier demands a floor, absence now rejects (`OPT_OI_MISSING`).

### 20. Advisory exits and VWAP invalidation were permanently inert
`poll()` hardcoded `spread_mark=None, option_confidence=BLOCKED, vwap=None`: §18's PROFIT_TARGET/PREMIUM_STOP triggers could never fire, PM-written VWAP invalidation rules were skipped, and the computed equity confidence was a dead variable. **Fixed:** the monitor (given the mark source) fetches the current ADJUSTED_MID spread mark (mark source's own lag/drift bounds make it at worst MEDIUM), computes session VWAP from stored RTH bars (current-session only), and records the equity confidence in each snapshot. Underlying-driven primaries remain the spine, exactly as spec §18 orders it.

---

## Part 3 — P2 issues (all fixed)

| # | Where | Issue | Fix |
|---|---|---|---|
| 21 | `risk/position_sizing.py` | `requested_qty = min(requested_qty, max(requested_qty, max_qty))` — a no-op that read like a clamp | Removed; comment states requested stays unclamped for attribution |
| 22 | `models/calibration.py` | `is_usable_for_learning` was `A and B and C or (A and B and not-C)` ≡ `A and B` — obfuscated | Rewritten as what it means |
| 23 | `intelligence/news.py` | `str(list).replace("'", '"')` for facts/urls — invalid JSON on any apostrophe (e.g. *Barron's*) | `json.dumps` |
| 24 | `quant/scanner.py` | same `str().replace` pattern for `source_counts_json` | `json.dumps` |
| 25 | `alpaca/market_data.py` | RVOL baseline: constant-key `sorted()` depended silently on dict insertion order | Explicit sort by session date |
| 26 | `journal/trade_journal.py` | `close_trade` read `row["symbol"]` from a table with no symbol column | Join `decisions` for the symbol |
| 27 | `risk/constitution.py` | `BlackoutWindow.blocks()` interpreted a naive calendar timestamp in machine-local time | Naive timestamps now pinned to ET |
| 28 | `options_engine/spreads.py` | `_diversify` deduped strike pairs ignoring expiration — same strikes a week apart are different trades; thin chains starved the PM of choices | Expiration added to the pair key |
| 29 | `execution/order_manager.py` | Persist-failure banner said "ORDER FILLED" for an order merely submitted | Reworded |
| 30 | `execution/order_manager.py` | Fill path never updated the local order row's status | `_record_fill` persists "filled" |

---

## Part 4 — Deliberate deviations from the specs (documented, not "fixed")

These are working as coded on purpose, or are operator decisions already annotated in config. They are listed so the divergence is a decision, not an accident.

1. **Scenario taxonomy:** code uses CONTINUATION / STALL / REVERSAL (min 2, max 4, no MACRO_OVERRIDE) versus v2.5 §5.2's CONTINUATION / BASE / FAILURE (exactly 3, optional 4th). STALL is a sharper trade-expression probe than BASE and the sanity checker enforces coherence; keep, but say so in the demo script.
2. **Scenario bands are absolute prices, not % returns** (v2.5 §5.3 wanted the LLM to emit percentages and Python to map them). The `sanity_check` guards against drift from spot, and issue 17's fix pins spot server-side. Acceptable; documented.
3. **`new_trade_cutoff_et: "15:35"`** in `risk_constitution.yaml` versus spec §16.1's 15:20, with a matching 15:20 scan in the schedule. Operator widened the runway; both the scheduler job and the risk gate now read the same key so they cannot diverge again. If you want spec-strict behavior, change the one YAML value (the 15:20 scan then becomes analysis-only spend — consider dropping it at the same time).
4. **Tier 1 floors lowered** (pre 62→58, final 68→62) and `options_prescreen_top_n` 12→20 — annotated in `scoring.yaml` with two sessions of evidence ("no candidate scored above 63"). Quality opinions, not safety gates; hard gates untouched. Keep, disclose in the write-up.
5. **`max_councils_per_day: 30` at Tier 1** versus spec's 12 — consistent with the 9-scan schedule; now actually enforced (issue 13).
6. **MCP degrade-to-REST** versus §9.1's "fail startup if execution-critical tools missing". The code warns loudly and continues; the hackathon requires demonstrating MCP, and the run banner already shouts when it is absent. Keep, but verify MCP connects on demo day.
7. **Scenario generator on Luna** (config) versus v2.5 §16's Sol — a cost decision recorded in `scoring.yaml`.
8. **Reward/risk floor is expressed through cost/width** (`RR = (1−c/w)/(c/w)`) rather than a second explicit gate — mathematically equivalent; §12.6's table is honored through `max_cost_to_width`.
9. **Council after cutoff/dry-run still runs** (evaluate-but-don't-execute): intentional — shadow variants and gate rows keep accruing evidence. LLM spend past the cutoff is bounded by the last scan time.

---

## Part 5 — Spec features: BUILT 2026-09-01 evening (all four)

All four gaps were closed the evening of Sep 1 and verified end-to-end against live services (two `--scan-now --dry-run` preflights on the production database). Suite: **505 passing** (+35 new tests). What follows is what shipped, superseding the build plans that stood here.

### A. SEC EDGAR collector — BUILT (`alpha_council/intelligence/sec.py`)
Active-universe polling per §10.1: CIK map from `company_tickers.json` (24h-cached in `system_state`), submissions JSON per symbol on a **rotating 40-symbol window** with a 600s per-symbol cooldown and 5 rps spacing under the validated `SEC_USER_AGENT`. Priority forms (8-K by item code, 10-K/Q, 6-K, 4, 13D/G ± amendments, S-3, 424B*) become TIER_1_PRIMARY `IntelligenceItem`s + scored events through the same tables news uses; direction comes from the tape only (`resolve_direction` with NEUTRAL wording); accession-number dedup makes sweeps idempotent; every failure path returns `{}` and logs degradation. Wired into `_gather_intelligence` beside news; `classify_track` already promotes SEC-evidenced symbols to the EVENT track. Config: `sec:` block in `scoring.yaml`. The global current-filings feed remains deliberately unbuilt (config ships `sec_global_injection_enabled: false`; the spec licenses this).

**Live-fire calibration finding (worth the demo slide):** the first real sweep (22:22 ET, 518 filings) showed banks' routine 424B2 structured-note supplements averaging catalyst 57 — above the 55 material floor — because the §10.3 formula's additive terms (reliability 100, freshness, neutral surprise) floor any *fresh, well-sourced* item near 55 regardless of content. Two fixes: (1) routine-form bands lowered (424B* → 15–35, Form 4 → 15–40, Reg-FD-only 8-Ks → bottom of band) with a 5-per-symbol 424B cap; (2) `IntelSummary.has_material_catalyst` now also requires **materiality ≥ 50** — "material" has to mean material, not merely fresh. This correctly tightens the news pipeline too. The 518 events scored under the old bands were purged (items retained, so dedup prevents re-scoring); the change is pinned by `test_routine_filings_stay_below_the_material_floor`.

### B. Pre-Market Strategist — BUILT (`alpha_council/agents/premarket.py`)
`PreMarketBrief` model (v2.5 §14) in `models/evolution.py`; deterministic context pack (overnight events, benchmark gaps, open positions, prior lessons, blackout windows, champion id) → one `briefing` call → persisted to `premarket_briefs`, **idempotent per session date** (restart reuses the stored brief). Scheduler job at `schedule.briefing_et` (08:45); the brief's `as_context()` text flows into `EvidenceBuilder(session_briefing=…)`, which already routes it to PM/Catalyst/Red-Team packages. Absent brief = normal scan (v2.5 §28). Prompt: `config/prompts/premarket_system.txt`.

### C. Champion/Challenger + promotion engine — BUILT (`alpha_council/evolution/`, shadow-only)
- `change_validator.py` — the safety boundary, built first and tested hardest: immutable-path prefixes (whole `hard.` block, paper lock, liquidity floors, budget, execution), per-tier liquidity leaves untouchable, whitelisted mutable prefixes only, ≤3 changes, ±10% hysteresis bound, and the model's `champion_value` claim must match the actual config. `PROMPT_EMPHASIS` is advisory prose, never auto-applied.
- `champion.py` — `ChampionRegistry`: exactly-one-champion invariant (`alpha_v2_5_c0` created at startup from the live config), one active challenger, rejected proposals persisted for audit, and `promote()` **raises without `operator_approved=True`** — no scheduled code path calls it.
- `agents/alpha_evolution.py` — post-close review with a **deterministic evidence pre-gate** (below `min_observations_to_propose` the LLM is never called); `EvolutionDecision` schema makes NO CHANGE a first-class output; identity fields overwritten server-side; validator has the last word before storage.
- `shadow_runner.py` — deterministic re-scoring of stored `candidate_scores` under challenger weights/floors with track-quota selection; idempotent per (strategy, source); measurable vs unmeasurable observations labeled in each rationale.
- `performance.py` + `promotion.py` — common-set comparison (champion-traded decisions share outcomes; challenger-only trades measured through marked rejected shadows or **counted as unmeasured, never invented**); promotion rules per §12: insufficient evidence → CONTINUE_SHADOW, failed rule can never be argued past (schema-enforced), unmeasured share > 25% blocks by itself, operator approval always required.
- `service.py` — the 16:15–16:35 chain (facts → lessons → proposal → shadow → performance → recommendation), every step individually fenced, wired into the scheduler's `post_close`.
- Dashboard: **Alpha Evolution tab** (`dashboard/tabs/evolution.py` + 7 read-only queries) — champion vs challenger, proposals with their change tables, shadow counts, the promotion checklist with failed rules in red, the lessons feed, and today's brief.
- Config: `alpha_evolution:` block + `alpha_evolution` model entry in `scoring.yaml`; prompt `config/prompts/alpha_evolution_system.txt`.

### D. §17.4 pre-submit refresh — BUILT (`alpha_council/execution/presubmit.py`)
Immediately before submission: fresh underlying quote (≤5s or `EXEC_STALE_PRESUBMIT`), both legs re-fetched, per-leg spread gate re-applied (this path bypasses the chain fetch where that gate normally lives — a leg that widened past the tier ceiling is `EXEC_REPRICE_FAILED`, caught by its own test), the spread rebuilt through the same `SpreadBuilder._try_pair` with **identity preserved** (structure_id/rank survive so no foreign key orphans), then the **Risk Constitution re-run against the repriced limit** — "no stale approval may be reused" — with the re-evaluation journaled and the audit row updated to the submitted prices. The walk ceiling and calibration record use the fresh underlying. Optional collaborator on the Orchestrator, so replay and tests without a market connection are unaffected.

---

## Part 6 — Operator items: RESOLVED 2026-09-01 evening (4 of 5; one true decision left)

1. **Model prices — hardened in code.** `compute_cost` now bills an unknown model id at the **most expensive configured rate** instead of $0 (a silently-free model would void every ceiling — the one failure mode the manager exists to prevent), `BudgetManager.record` logs `BUDGET_UNPRICED_MODEL`, and startup prints any `models.*` entry missing a price via `unpriced_models()` (current config: none missing). Console verification of the three prices remains worthwhile but no longer a silent failure mode.
2. **`v2.5-measured-2` config row — verified present and active** in the production database (queried directly; it is the only row with `deactivated_at IS NULL`).
3. **Earnings calendar — verified and corrected.** The calendar already carried DELL/PANW (Sep 1), AVGO/SNOW (Sep 2), LULU (Sep 3); AVGO was independently re-verified against Broadcom's own announcement (Q3 FY2026 after close Sep 2, call 17:00 ET) and CRM confirmed already-reported (Aug 26). One real defect found: every earnings window used `post_block_minutes: 10`, but §16.4 blocks "through the first 10 minutes of the **following session**" — as written the system could buy DELL's gap open at 09:35 on Sep 2. All five windows now use `post_block_minutes: 1040` (16:20 + 1040m = 09:40 next day), pinned by `test_earnings_blackout_covers_the_gap_open`.
4. **`.gitignore` — verified**: `.env`, `.env.*`, `*.db`, `data/` are all covered.
5. **The 15:20 scan / 15:35 cutoff pairing — left standing as the operator's choice.** The pair is internally coherent (the scan's councils can still execute before 15:35) and both now read the same config key, so they cannot drift apart. Reverting to the spec's 15:20 is a one-line YAML change that should then also drop the 15:20 scan.

---

## Part 7 — Verification

- Full suite: **505 passed** (`uv run pytest tests/`), up from 453; no skips, no xfails.
- `tests/test_review_fixes.py` (14 tests) pins the Part 1–3 fixes: `signal_price` presence and fallback; descending close walk and tick floor; negative closing limit payload; closing fill netting; walk-budget headroom; constitution sector-map ownership; shadow restore; VETO-as-sizing decomposition; close-time attribution freeze; rejected-shadow creation and marking; risk-approved-structure restore with track.
- `tests/test_evolution_engine.py` (19 tests) pins Part 5C: every named v2.5 §27 validator case (risk-limit / paper-mode / liquidity-floor / tier-leaf rejections, bounded-change acceptance, >10% bound, misstated baseline, unknown path, >3 changes), registry invariants (single champion, single challenger, **promotion raises without operator approval**, stored config immutability), promotion rules (insufficient sample, drawdown blocks, unmeasured share blocks, schema refuses PROMOTE with failed rules), and the shadow runner's floor-flip + idempotency + common-set performance with unmeasured counting.
- `tests/test_presubmit_sec_premarket.py` (16 tests) pins Parts 5A/B/D and 6.3: stale-underlying block, identity-preserving reprice, crossed-quote and widened-spread rejection, SEC form classification, 8-K item ranking, live-sweep-calibrated routine-filing floor, accession dedup, fail-open collection, earnings blackout spanning the gap open, brief context rendering.
- Four existing tests updated to corrected semantics (PASS-cap ×2, close-side calibration floor, unknown-model billing), each with an explanatory comment.
- **Two live preflights** (`--scan-now --dry-run`, 22:22 and 22:26 ET Sep 1) against the production database: MCP connected (16/54 tools resolved, account read served over MCP), 14,279-asset catalog, champion `alpha_v2_5_c0` created idempotently, SEC sweep collected 518 filings, news injected 7 off-universe symbols, funnel ran 123 → 30 → 20 chains → 0 councils (after-hours quotes correctly `OPT_CHAIN_UNUSABLE`), $0.00 LLM spend, clean shutdown both times.

## Part 8 — Session-morning checklist (Sep 2)

Start the autonomous session before 08:45 ET so the scheduler owns the whole day:

```
uv run python scripts/run_alpha_council.py --max-trades 3
```

What the day now looks like: 08:45 pre-market brief → 09:35 discovery + morning reset → 09:40–15:20 scans (with SEC + news + off-core injection, §17.4 refresh before every submission, walk with real headroom, credit-side closes) → 15:35 cutoff → 15:45 flatten check → 16:15 report + lessons → 16:30 at-most-one challenger proposal → shadow scoring + promotion recommendation. AVGO/SNOW are entry-blocked from 15:50 through Thursday 09:40; DELL/PANW are blocked until 09:40 tomorrow morning.

Watch for on day one of the new pieces: the 08:45 `PREMARKET_BRIEF` event in system_events (a failure logs `PREMARKET_FAILED` and scanning continues), `SEC_COLLECTED` counts on each scan, and — if ≥8 decisions have accumulated by the close — either a `CHALLENGER_STORED` or an explicit `NO_CHANGE` from the evolution cycle. Promotion can only ever be advisory: `ChampionRegistry.promote()` raises without `operator_approved=True` and nothing scheduled calls it.

## Change inventory (files touched)

| File | Changes |
|---|---|
| `alpha_council/alpaca/market_data.py` | `SymbolSnapshot.signal_price` (P0-1); RVOL sort clarity (25) |
| `alpha_council/models/execution.py` | `limit_is_credit` + validator + signed payload (P0-2) |
| `alpha_council/models/calibration.py` | side-aware limit/floor validator (P0-2); `is_usable_for_learning` (22) |
| `alpha_council/execution/order_manager.py` | close walk, cancel-race verification, `get_order` semantics, adopted-order persistence, close calibration, fill extraction, `_record_fill` (P0-2/4, 18, 29, 30) |
| `alpha_council/risk/position_sizing.py` | `budget_dollars`, `portfolio_risk_room`, no-op removal (P0-3, 21) |
| `alpha_council/models/risk.py` | `approved_risk_budget` + coherence validator (P0-3) |
| `alpha_council/risk/constitution.py` | sector map ownership, PASS-cap ternary, budget bounding, blackout tz guard (P0-3/5, 11, 27) |
| `alpha_council/orchestrator.py` | walk ceiling from budget, sector source, PASS cap, track on MonitoredPosition (P0-3/5, 11) |
| `alpha_council/agents/council.py` | `effective_risk_pct` MODIFY-only (11) |
| `alpha_council/execution/position_monitor.py` | risk-approved restore + track, advisory marks, VWAP, close flow with current marks + calibration + shadow freeze (P0-1/2/6, 20) |
| `alpha_council/journal/shadow_book.py` | `restore`, `close_decision`, decomposition fix, flat-executed basis (7, 8, 9) |
| `alpha_council/journal/trade_journal.py` | rejected-shadow creation on flush, close_trade symbol join (10, 26) |
| `alpha_council/scheduler.py` | morning reset, daily council cap, funnel councils update, rejected-shadow marking, off-core injection, sector risk, cutoff from risk config (12–16) |
| `alpha_council/intelligence/news.py` | off-core + market-wide collection, JSON persistence (12, 23) |
| `alpha_council/evolution/scenarios.py` | server-side identity override (17) |
| `alpha_council/options_engine/chain.py` | missing-OI rejection (19) |
| `alpha_council/options_engine/spreads.py` | expiration in diversity key (28) |
| `alpha_council/quant/scanner.py` | JSON source counts (24) |
| `scripts/run_alpha_council.py` | wiring: marks/shadows into monitor, RejectedShadowBook, shadow restore, scheduler risk config; evening wave: presubmit, SEC, premarket, champion registry, evolution service, unpriced-model warning |
| `tests/test_review_fixes.py` | new — 14 regression tests |
| `tests/test_council.py`, `tests/test_risk.py`, `tests/test_v24_models.py`, `tests/test_agents.py` | updated to corrected semantics + new cases |

**Evening wave (Parts 5–6 build), new files:**

| File | Contents |
|---|---|
| `alpha_council/execution/presubmit.py` | §17.4 pre-submit refresh (stale block, identity-preserving reprice, leg-quality gate) |
| `alpha_council/intelligence/sec.py` | SEC EDGAR collector (rotating window, accession dedup, live-calibrated bands, fail-open) |
| `alpha_council/agents/premarket.py` | Pre-Market Strategist (idempotent per session date) |
| `alpha_council/agents/alpha_evolution.py` | evolution agent (deterministic pre-gate, validator-gated storage) |
| `alpha_council/models/evolution.py` | PreMarketBrief, ParameterChange, ChallengerProposal, EvolutionDecision, StrategyPerformance, PromotionRecommendation |
| `alpha_council/evolution/champion.py` | ChampionRegistry (operator-gated promote) |
| `alpha_council/evolution/change_validator.py` | the safety boundary (immutable paths, bounds, baseline check) |
| `alpha_council/evolution/shadow_runner.py` | deterministic challenger re-scoring |
| `alpha_council/evolution/performance.py` | common-set metrics with unmeasured counting |
| `alpha_council/evolution/promotion.py` | deterministic promotion rules |
| `alpha_council/evolution/service.py` | fenced post-close cycle |
| `dashboard/tabs/evolution.py` | Alpha Evolution tab |
| `config/prompts/premarket_system.txt`, `config/prompts/alpha_evolution_system.txt` | new prompts |
| `tests/test_evolution_engine.py`, `tests/test_presubmit_sec_premarket.py` | 35 new tests |

**Evening wave, modified:** `scheduler.py` (premarket job, SEC merge, evolution post-close, session briefing into evidence), `agents/budget.py` (worst-case billing for unpriced models + `unpriced_models()`), `quant/scoring.py` (`EVENT_MATERIALITY_FLOOR`, `IntelSummary.materiality_score`), `intelligence/news.py` (unchanged this wave), `config/scoring.yaml` (`sec:`, `alpha_evolution:`, `alpha_evolution` model), `config/event_calendar.yaml` (§16.4 overnight windows), `dashboard/app.py` + `dashboard/queries.py` (evolution tab + 7 queries).
