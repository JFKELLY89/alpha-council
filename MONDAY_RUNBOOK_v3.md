# Monday, August 31 — Runbook v3

**Status change since v2:** the scheduler, the autonomous entry point, and
the live mark source are **built and verified**. The 11:30–15:00 build block
is gone. Monday is now measurement, one calibration trade, and starting a
system that already works.

**Sessions remaining after today:** 3 (Sep 1, 2, 3)
**Submission:** Friday Sep 4, 11:00 ET

---

## Contents

1. Before you start
2. 08:30 — Earnings calendar (the only research task)
3. 08:50 — Verify prices and health
4. 09:30 — Wait
5. 09:45 — Data reality probe
6. 10:05 — Write the measured config
7. 10:15 — Backfill and discovery
8. 10:30 — Vertical slice dry run
9. 10:45 — First calibration trade
10. 11:15 — Live council with real data
11. 11:45 — Full funnel dry run
12. 12:15 — Start the scheduler
13. 16:15 — Post-close review
14. Tuesday preview
15. Hard rules and abort conditions

---

## 1. Before you start

Open three terminals in `C:\Users\LAPTOP\Documents\alpha-council`:

| Terminal | Purpose |
|---|---|
| **A** | running commands |
| **B** | dashboard, left running all day |
| **C** | tailing the scheduler once it starts |

Six gates govern the day. Do not pass one until it is met.

| Gate | Condition |
|---|---|
| 1 | 440 tests pass, schema verifies |
| 2 | Probe emits a recommendation from ≥3 valid rounds |
| 3 | Vertical slice dry run returns 3–5 structures on SPY |
| 4 | One spread opened and closed, with an `execution_calibrations` row |
| 5 | Claude returns a valid `RedTeamReview` |
| 6 | One autonomous scan completes without an unhandled exception |

---

## 2. 08:30 — Earnings calendar

**The only task today that needs research rather than a command.** Last
night's startup printed `blackout windows: 0`, which means the Risk
Constitution will currently let the system trade straight into an earnings
print.

### 2.1 Verify the two unverified exclusions

Open `config/universe.yaml` and find:

```yaml
  - {symbol: FDX,   reason: "UNVERIFIED earnings inside competition window - confirm or restore"}
  - {symbol: LULU,  reason: "UNVERIFIED earnings inside competition window - confirm or restore"}
```

Check both against a source you trust. Then either:

- **Confirmed reporting Sep 1–4** → change the reason to cite the date:
  `reason: "earnings 2026-09-03 after close"`
- **Not reporting in the window** → delete that line entirely so the symbol
  rejoins the universe

### 2.2 Build the blackout calendar

Open `config/event_calendar.yaml`. It should currently contain only the two
macro entries. Add an `events:` entry for every Core symbol you can confirm
reports between Sep 1 and Sep 3:

```yaml
events:
  - name: US International Trade in Goods and Services
    source: BEA
    timestamp_et: "2026-09-03T08:30:00-04:00"
    pre_block_minutes: 15
    post_block_minutes: 5

  - name: Employment Situation - August 2026
    source: BLS
    timestamp_et: "2026-09-04T08:30:00-04:00"
    pre_block_minutes: 15
    post_block_minutes: 5

  - name: EXAMPLE Q3 earnings
    source: earnings
    timestamp_et: "2026-09-02T16:20:00-04:00"
    pre_block_minutes: 30
    post_block_minutes: 10
    symbols: [EXAMPLE]
```

Rules that matter:

- `symbols:` scopes the blackout to those tickers. Omit it and the window
  blocks **everything**, which is right for macro and wrong for earnings.
- Use `16:20` for after-close reports and `08:00` for before-open.
- `pre_block_minutes: 30` means no new entry in the 30 minutes before.

### 2.3 Verify it loads

```powershell
uv run python -c "import yaml; d=yaml.safe_load(open('config/event_calendar.yaml',encoding='utf-8')); print(len(d.get('events',[])), 'events')"
```

You want at least 2. If you cannot confirm any earnings dates, that is an
acceptable outcome — but know that earnings risk is then unmanaged, and
consider keeping position count low.

---

## 3. 08:50 — Verify prices and health

### 3.1 Model prices

Open the OpenAI and Anthropic consoles. Compare against
`config/scoring.yaml`:

```yaml
model_prices:
  gpt-5.6-luna:    {input: 0.20, output: 1.20}
  gpt-5.6-sol:     {input: 4.00, output: 20.00}
  claude-sonnet-5: {input: 2.00, output: 10.00}
```

Sunday's four-call council cost $0.0259 — roughly a quarter of my
projection. Either the analysts are cheaper than modelled or a price is
wrong. Find out which. Edit the YAML if any figure differs.

### 3.2 Health check

```powershell
uv run pytest tests\ -q
uv run python -m alpha_council.settings
uv run python scripts\init_db.py --verify
git status --short
```

Expected: 440 passed, credentials shown as `present` (never a value),
schema checks pass, working tree clean.

**GATE 1 — stop here if anything fails.**

### 3.3 Start the dashboard (Terminal B)

```powershell
uv run streamlit run dashboard/app.py
```

Leave it running. Refresh after each step.

---

## 4. 09:30 — Wait fifteen minutes

**Do nothing.** The opening auction has the widest spreads and least stable
timestamps of the day. Measuring now would misconfigure the staleness
thresholds you are about to lock in for the week.

---

## 5. 09:45 — Data reality probe

```powershell
uv run python scripts\probe_data_reality.py
```

Nine minutes. Six rounds, 90 seconds apart. It refuses to emit a
recommendation from fewer than three valid in-session rounds.

### 5.1 Read Stage 3

| Field | What it should show |
|---|---|
| `valid rounds` | ≥ 3 |
| `frozen rounds` | 0 |
| `OPTION quote lag` | the number that decides everything |
| `EQUITY quote lag` | should be near zero |
| `greeks present` | high, roughly 85%+ |

### 5.2 Interpret the option lag

| p90 lag | Meaning | What to do |
|---|---|---|
| **< 90s** | Indicative is effectively live | Set `max_quote_lag_seconds` near 300. §5.4 delta adjustment becomes a rarely-used guard. |
| **90–400s** | Moderate delay | Take Stage 4's values as printed. |
| **> 400s** | Substantial delay confirmed | §5.4 is load-bearing. Underlying-driven exits are doing real work. |

**GATE 2 — if it prints "NO RECOMMENDATION ISSUED", re-run mid-session.
Never edit config from an invalid run.**

---

## 6. 10:05 — Write the measured config

### 6.1 Copy the Stage 4 block

Open `config/scoring.yaml`. Find the `options:` block and replace the four
values with the measured ones:

```yaml
options:
  fresh_quote_seconds: <from Stage 4>
  max_quote_lag_seconds: <from Stage 4>
  max_underlying_drift_pct: 0.010
```

Then the `equity:` block:

```yaml
equity:
  pre_submit_max_lag_seconds: <from Stage 4>
  high_confidence_max_spread_pct: <from Stage 4, or leave 0.005>
```

### 6.2 Bump the config version

```powershell
(Get-Content .env) -replace '^CONFIG_VERSION=.*','CONFIG_VERSION=v2.5-measured' | Set-Content .env -Encoding ascii
(Get-Content config\scoring.yaml) -replace '^config_version: v2\.5$','config_version: v2.5-measured' | Set-Content config\scoring.yaml -Encoding utf8
```

### 6.3 Verify

```powershell
uv run python -c "import yaml; d=yaml.safe_load(open('config/scoring.yaml',encoding='utf-8')); print(d['config_version']); print(d['options'])"
```

**Do not reset the database.** `ensure_config_version` creates the new row
on first use. The split between guessed and measured settings is exactly
what you want in the audit trail — it answers "how did you choose those
thresholds?" with "we measured them at 09:45 Monday."

---

## 7. 10:15 — Backfill and discovery

```powershell
uv run python scripts\backfill_bars.py
uv run python scripts\discover_once.py
```

### 7.1 What must be true

| Check | Why it matters |
|---|---|
| RVOL is no longer 40.0 for everything | 40.0 is the fallback when no clock window is found. If it persists, bucketing is broken and RVOL contributes nothing. |
| `DIR_AMBIGUOUS` well below Saturday's 37 | If most of the universe still sits between −0.15 and +0.15, that floor is your binding constraint, not the score floors. |
| Screener symbols reach Stage-0 with real scores | Confirms the on-demand backfill for dynamic symbols works. |
| `DISC_BLOCKED_CLASS` still catching leveraged ETFs | TQQQ, SOXL and similar should be excluded by name. |

If RVOL is still uniformly 40.0, stop and tell me. Everything downstream
scores wrong.

---

## 8. 10:30 — Vertical slice dry run

```powershell
uv run python scripts\vertical_slice.py
```

**This is the gate calibration moment — the first time Tier 1 meets a live
SPY chain.**

### 8.1 Read section 3's rejection histogram

| Dominant gate | Meaning | Response |
|---|---|---|
| `OPT_QUOTE_BLOCKED` in the hundreds | §6 config too tight, or the feed really is delayed | Re-check the probe output |
| `OPT_VOLUME_TOO_LOW` | Tier 1's `min_volume: 25` too tight this early | Note it; consider one change at 16:15 |
| `OPT_SPREAD_TOO_WIDE` | Tier 1's 15% leg cap too tight for this chain | Note it |
| `OPT_GREEKS_MISSING` ~15% | Normal — matches the probe | Ignore |

### 8.2 Section 4 must return structures

3–5 is healthy. If zero:

```powershell
uv run python scripts\vertical_slice.py --tier 2
uv run python scripts\vertical_slice.py --tier 3
```

**Never loosen a liquidity gate to manufacture a structure.** If Tier 3 on
SPY produces nothing, something upstream is broken — that is the most
liquid options chain in existence.

### 8.3 Section 5

Risk Constitution should say APPROVE with `approved qty` of 1 at the 0.25%
default.

**GATE 3 — a valid structure on SPY at some tier.**

---

## 9. 10:45 — First calibration trade

```powershell
uv run python scripts\vertical_slice.py --live-paper --close-after 300
```

One lot, 0.25% risk, held five minutes, closed. **A lifecycle
demonstration, not an alpha bet.** Roughly six minutes.

### 9.1 What to watch

| Section | What it tells you |
|---|---|
| 7 | Which attempt filled. Attempt 1 = generous limit. Attempt 3 = the indicative reference sits well below the real market. |
| 8 | Fill debit and seconds to fill. |
| 9 | `bias vs adjusted` — **the first real measurement of what the free feed costs.** |
| 10 | Realized P&L on the round trip. |

### 9.2 If it does not fill

`NO_FILL` after three attempts is a valid recorded outcome. Re-run once. If
it fails twice, the walk is too conservative — paste me the three attempt
prices before changing anything.

### 9.3 Confirm the audit trail

```powershell
uv run python scripts\check_trade.py
```

You need **exactly one `execution_calibrations` row per submission**. If
that table is empty after a fill, the recording path is broken and no alpha
trade may proceed until it is fixed.

Open the dashboard's Execution Quality tab. Real data for the first time.

**GATE 4 — one spread opened and closed cleanly, with a calibration row.
Everything after this depends on it.**

---

## 10. 11:15 — Live council

```powershell
uv run python scripts\council_once.py --symbol <top candidate from step 7> --benchmark SPY
```

**No `--allow-stale`.** Live quotes, live volume, real candidate. Roughly
13 cents.

**This is the first time Claude will ever be called by this system.**

### 10.1 What to watch

- **Does the Red Team run at all?** Sunday stopped at the PM.
- **The verdict**, and whether any problem carries category `EXPRESSION` or
  `BREAKEVEN` — that means the mandatory trade-expression challenge fired,
  which is the v2.5 upgrade working.
- **The per-purpose cost table** against projection.

### 10.2 Then check

```powershell
uv run python scripts\check_params.py
uv run python scripts\check_agents.py
```

`check_params.py` must stay at **0**. Anything else means a model parameter
in `scoring.yaml` was rejected and the client fell back — fix the config
before the scheduler starts.

`check_agents.py` shows token counts. Compare the PM call against the
6,000-token cap; far under means a thin evidence pack.

### 10.3 If the PM abstains

That is informative, not broken. Try a second candidate. If three
consecutive live candidates abstain, tell me — the evidence pack likely
needs work.

**GATE 5 — Claude has returned a valid `RedTeamReview`.**

---

## 11. 11:45 — Full funnel dry run

```powershell
uv run python scripts\run_alpha_council.py --scan-now --dry-run
```

Runs discovery, the complete funnel, and councils on the survivors. Submits
nothing. A few cents.

**This is the rehearsal for autonomous operation.** It exercises exactly
what the 13:30 scheduled scan will do.

### 11.1 What to check

| Output | Expectation |
|---|---|
| `SCAN_COMPLETE` in the log | funnel completed |
| `candidates_evaluated` | > 0, ideally 3 |
| `councils_run` | > 0 |
| `stopped_by` | shows where candidates died |
| Dashboard → Discovery Funnel | a new scan row |
| Dashboard → Gate Lab | rejections recorded |

If `candidates_evaluated` is 0, the funnel is producing nothing at Tier 1
and the scheduler will do the same all afternoon. Diagnose before starting
it.

---

## 12. 12:15 — Start the scheduler

### 12.1 First run, capped at one trade (Terminal C)

```powershell
uv run python scripts\run_alpha_council.py --max-trades 1
```

Startup prints the service summary, restore, and all fifteen jobs with next
run times. Then it waits.

### 12.2 Confirm the timetable

| Job | Next fire |
|---|---|
| `scan_2` | 11:30 (past — will not run today) |
| `tier_0` | 12:30 |
| `scan_3` | 13:30 |
| `breadth_1` | 14:00 |
| `tier_1` | 14:15 |
| `scan_4` | 15:00 |
| `cutoff` | 15:20 |
| `position_monitor` | every 2 min |
| `shadow_marks` | every 5 min |

`blackout windows` should now show your entries from step 2. If it still
says 0, the calendar did not load.

### 12.3 Sit in front of the 13:30 scan

Watch a full autonomous cycle. In the dashboard, Discovery Funnel gains a
scan, Scanner shows candidates, Gate Lab gains rejections, and if a trade
fires, Command Center updates.

**GATE 6 — one autonomous scan completes without an unhandled exception.**

### 12.4 If a trade fires

The ceiling stops further entries. Watch the position monitor poll it every
two minutes. Confirm in the dashboard that `shadow_trades` has three
variants and `shadow_marks` is accumulating.

### 12.5 If the scheduler raises

It is designed to survive job failures — each job is isolated and logs to
`system_events`. But an unhandled exception at the top level means
something is wrong. Ctrl-C, check `system_events`, paste it to me.

---

## 13. 16:15 — Post-close review

The `post_close` job writes a session report automatically. Then:

```powershell
uv run python scripts\check_trade.py
uv run python scripts\check_agents.py
uv run python scripts\discover_once.py
```

### 13.1 Review in the dashboard

| Tab | Question |
|---|---|
| Command Center | spend against projection, drawdown, positions |
| Discovery Funnel | did screener symbols reach councils? |
| Gate Lab | which gate eliminated the most candidates? |
| Execution Quality | indicated-to-fill bias from today's fills |
| Counterfactual Lab | if a trade fired, are three variants marking? |

### 13.2 Change at most one quality gate

Per §20.3, **one** change, with a stated reason. If `OPT_VOLUME_TOO_LOW`
dominated all day, that is the candidate. Edit `config/scoring.yaml`, bump
to `v2.5-measured-2`, and note why.

Never touch `risk_constitution.yaml`'s `hard:` block.

### 13.3 Reconcile the budget

Compare measured spend against $0.096 OpenAI and $0.036 Anthropic per
council session. Adjust caps once, tonight, then leave them.

### 13.4 Commit

```powershell
git add -A
git commit -m "Monday: measured config, first calibration trade, first autonomous session"
git push
```

---

## 14. Tuesday preview

**Morning:** start the scheduler at 09:25 with no trade ceiling, watch the
09:40 scan, then check hourly.

```powershell
uv run python scripts\run_alpha_council.py
```

**Evening — Alpha Evolution Phase 1**, the Scenario Generator. The
deterministic payoff engine and all nine tables are already built; what is
missing is the LLM call producing CONTINUATION / STALL / REVERSAL bands.
That gives the PM and the Red Team breakeven and stall numbers to reason
about, and it shows well in the Council tab.

**Wednesday is the cutoff for new features.** Thursday is trading and
submission assets only, with the flatten at 15:45 and the video recorded
that evening.

---

## 15. Hard rules

- **No alpha trades until the calibration spread has opened and closed.**
- **Never edit `risk_constitution.yaml`'s `hard:` block.** Those are capital
  preservation, not opinions.
- **Never lower a liquidity floor to create activity.** No trade is an
  acceptable outcome; a bad trade is not.
- **Nothing goes live without a passing test suite.**
- **Change at most one quality gate per day**, with a `config_versions` row
  and a stated reason.
- **New-trade cutoff is 15:20 ET**, enforced in code.

## Abort conditions

Stop and diagnose rather than working around:

| Condition | Likely cause |
|---|---|
| Probe reports NO RECOMMENDATION | ran too close to a session boundary |
| RVOL still 40.0 everywhere | clock-window bucketing not finding the session |
| Zero structures on SPY at Tier 3 | bug in chain normalization or filters |
| `check_params.py` returns > 0 | a model parameter was rejected; fix config |
| Fill above `natural_debit` | limit walk bug — **halt immediately** |
| Two orders live for one `decision_id` | idempotency failure — **halt immediately** |
| `execution_calibrations` empty after a fill | recording path broken |
| Scheduler raises unhandled | do not leave it running unattended |

## Open questions to resolve today

1. **Option chain pagination.** AMD returned exactly 1,000 contracts, SPY
   2,022, LLY 572. The round number suggests AMD truncated at a page
   boundary. Watch `contracts seen` on a mid-size chain — another exact
   1,000 confirms a bug in `get_option_chain`.
2. **Direction ambiguity floor.** SPY, NVDA and several Core names sat
   below 0.15 on Friday's close. If that persists intraday, it caps your
   candidate count more than any score floor does.
3. **Evidence pack size.** Sunday's PM call cost far less than projected.
   Check `agent_runs.input_tokens` against the 6,000-token cap.
