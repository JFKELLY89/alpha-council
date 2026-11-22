# Desk Review — 2026-11-06 → 11-21 (Outage and Unattended Gap)

**Trigger:** operator report on 11-21 that the desk "hasn't been running for a few
weeks." **Finding in one line:** it traded unattended through 11-13, degraded
through 11-16, died into a **zombie on 11-17 at 13:04 ET**, and has done nothing
since — while the process-exists guard in the launcher told every scheduled
restart "already running, nothing to do."
**Review basis:** read-only forensics (DB, run logs, Windows power/boot records,
Task Scheduler state) plus a live broker probe at 20:5x ET 11-21. Nothing was
changed, killed, or restarted.

## Timeline

| When (ET) | What happened |
|---|---|
| 11-05 06:53 | Healthy day-4 launch (last reviewed session) |
| 11-06, 09, 10 | Traded unattended: 10 new positions, heavy losses |
| 11-07→09, 11-14→16 | Weekend machine suspends (~48h each) — MONITOR_GAP reported both on resume |
| 11-10 **15:20:00** | The 11-05 process **died silently** mid-afternoon — no error in the DB, only benign teardown noise in stderr; the machine did not reboot. Exactly-on-the-minute timing suggests external termination. Unexplained. |
| 11-11 09:36 | Relaunched at logon (the 08:20 weekday task couldn't run — machine asleep). Traded 11-11, 11-12; MCP healthy at start |
| 11-12→16 | Progressive degradation: **371 JOB_FAILED**, 10 SCAN_ABORTED, 16 EXIT_NOT_FILLED, 7 ORDER_SUBMIT_FAILED over the stretch; 11-13 and 11-16 sessions did zero councils; by 11-17 the 5-minute job was permanently wedged (every slot skipped) |
| 11-17 10:45–12:55 | Suspend storm **during market hours** (three MONITOR_GAP warnings); last position closed 11:51; last snapshot tick 11:40 |
| 11-17 13:02:10 | **Machine rebooted.** Auto-start relaunched the desk at 13:03 — before the network was up: account identity unreadable, PyPI unreachable (connection reset) so the MCP server could not install → REST-only degrade engaged correctly → **startup froze after the chain-priors banner line at 13:04:54** and never reached position restore, the scheduler, or SESSION_STARTED. Those processes are still alive today: a four-day zombie |
| 11-18→21 | The weekday 08:20 task fired on schedule (last: 11-20, result 0) but the launcher's guard is "a run_alpha_council process exists" — the zombie satisfies it, so **the self-healing that existed was neutralized**. Trading days fully dark: 11-17 pm, 11-18, 11-19, 11-20 |

## The damage

| Measure | Value |
|---|---|
| Equity 11-05 close → now | $94,325.62 → **$90,412.46** (**−$3,913, −4.1%**) |
| Options, realized 11-06→17 | **−$2,588 over 23 closed, 1 win** (TSLA −413, ARM −300, ASTS −264 and −100, NVDA −263, AAPL −202…) |
| ETF sleeve marks | $27,090.58 → $25,494.43 (**−$1,596**) — the market fell hard (QQQ −12%, VOO −10%, GLD −14% over the stretch); all 7 legs intact |
| Span cohort since 11-02 | **−$2,743 over 39 closed (4 wins)** |
| All-time ledger | 134 closed, −$7,038 |
| LLM spend over the gap | ~$18.37 across six session reports |
| Current book | **Options flat** (zero open at broker ✓ matches journal); ETF legs only |

Two mitigating facts. First, the options book was **flat before the desk died** —
the last position (ASTS) closed at 11:51 on 11-17, 73 minutes before the fatal
reboot, so nothing sat unmonitored into the 11-13/11-20 expiries. Second, most of
the equity damage is market beta through a passive sleeve, not desk malfunction —
though the band rebalancer stopped running after 11-13, so the sleeve never
rebalanced into the decline, and the 11-20 ETF cohort review never generated
(the desk was a zombie on its review date; the catch-up-safe job will write it at
the next post_close).

## What worked unattended

- **MONITOR_GAP (F11)** reported every blind window — the 48-hour weekend
  suspends and the mid-RTH suspend storm on 11-17 are all in the event log.
- **RULE_NORMALIZED (F13) fired five times** and was right each time — GOOGL's
  TIME threshold of 293.0 (a price, the exact MSFT defect) parsed to 3 days from
  the rule text; ASTS→10, IREN→7, NU→2, TSLA→5 recovered from None.
- **The MCP degrade paths (F11)**: the 11-17 relaunch correctly fell back to
  REST-only when PyPI was unreachable, and the prior session's MCP deadline kept
  calls failing fast rather than hanging councils.
- **Prior Brier scoring went live ~11-09** as predicted: 78 scores accumulated.
- The paper-only identity lock held through every degraded start.

## What failed — new findings

| # | Sev | Finding | Recommendation |
|---|---|---|---|
| **F14** | **High** | Startup can hang forever: the 11-17 relaunch froze between the chain-priors and budget banner lines (every step in that span is local — DB read, pure constructors — so the wedge is environmental post-boot) and sat four days without reaching SESSION_STARTED. No single-line fix is trustworthy from forensics alone | **Startup watchdog**: if SESSION_STARTED has not been logged within N minutes of process start (e.g. 10), log STARTUP_TIMEOUT and `sys.exit(1)` — a dead process is recoverable, a zombie is not |
| **F15** | **High** | `start_desk.ps1`'s no-duplicate guard is "a run_alpha_council process exists" — a zombie passes it forever, so the weekday task that should have healed the desk on 11-18/19/20 did nothing each morning | **Health-based guard**: treat the desk as running only if the DB shows a SESSION_STARTED (or any event) newer than ~90 minutes during market hours; otherwise kill the stale tree and launch. Paired with F14, every weekday 08:20 firing becomes a genuine self-heal |
| **F16** | Medium | **Phantom exits on bad quotes**: 6 exits over the gap graded INCONSISTENT by the audit — e.g. an invalidation "at 188.42" on a day whose bars span 190.40–194.52. Primary rules evaluate whatever price the snapshot returns, however stale or garbled, and the suspend/resume storms supplied garbage | Gate primary invalidation/target evaluation on quote freshness (reuse the existing `_equity_confidence` staleness machinery); a stale quote should log NO_UNDERLYING_QUOTE semantics, not fire an exit. Touches exit behavior → operator call, like F12 |
| F17 | Low | Partial/duplicate sleeve snapshots during account-read failures (an mv-5,088 row on 11-09, two rows for 11-11) pollute the ETF series | post_close should skip the sleeve snapshot when any leg read failed, rather than persist a partial |
| Obs | — | The 11-10 15:20:00 silent death remains unexplained (no reboot, no DB error, on-the-minute timing). If F14+F15 land, any recurrence self-heals the next weekday morning and becomes a logged event rather than an 18-hour outage |

Also carried forward: the machine's sleep behavior drove every blind window
(weekends, 11-17 RTH storm, and the reboot context). Power policy remains the
operator's decision — the desk can only report the gaps, which it now does.

## Standing items

- **ETF cohort review (was due 11-20)**: overdue; renders on demand via
  `scripts/etf_cohort_review.py` or auto-writes at the next post_close. The VUG
  cut question now has three weeks of priors data (78 Brier scores) behind it.
- **F12** (close-confirm parsing): the 6 INCONSISTENT exits in the gap are
  F16-shaped (bad data), not F12-shaped (confirmation semantics) — no change to
  its status; Nov-30 with F5/F10.
- F3/F4 open low. Options sizing review 11-30.

## Recommended remediation (awaiting the operator's word; nothing done yet)

1. Kill the zombie tree and relaunch via `Start-ScheduledTask AlphaCouncilDesk`
   (the F8 SOP) — the desk restores an empty options book and the ETF sleeve,
   and Monday 11-23 07:20/08:20 triggers resume normally.
2. Apply **F14 + F15** first (infrastructure only, no strategy surface): startup
   watchdog + health-based launcher guard, so this failure class self-heals.
3. **F16** on your word separately — it gates exits on data quality.
4. Generate the overdue ETF cohort review once the desk is back (or render it
   immediately, operator's preference).

*Review is read-only; the zombie was left running for forensics and no code,
config, or process state was changed.*
