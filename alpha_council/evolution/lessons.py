"""
Alpha Council v2.5 - post-trade lessons (Alpha Evolution Phase 2).

Reviews what the system actually did and produces hypotheses about why.

The division of labour is the whole design:

  SQL COMPUTES THE FACTS. Win rate, expectancy, gate histograms, funnel
  attrition, abstention reasons, fill bias — all aggregated in the
  database. A model asked to compute a win rate will get it wrong.

  THE MODEL INTERPRETS THEM. Given a factual brief it proposes
  explanations, states what would falsify each one, and names a test.
  That is a task models are genuinely good at.

  THE SAMPLE SIZE GOVERNS. Every lesson carries the count it was drawn
  from, and the model cannot recommend a change from a LOW-confidence
  reading. Over a five-day competition almost everything is LOW, and
  saying so is the honest output.

With few or no closed trades the richest available material is not
performance but ABSTENTION: why the council declines, which gates
eliminate the most candidates, and where the funnel loses breadth. That is
analysable from day one.

Place at: alpha_council/evolution/lessons.py
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from alpha_council.agents.evidence import EvidencePackage, estimate_tokens
from alpha_council.agents.llm import LLMClient
from alpha_council.db.engine import Database
from alpha_council.models.lessons import LessonSet, StrategyLesson
from alpha_council.utils.ids import new_uuid
from alpha_council.utils.time import iso_utc, utc_now

# ----------------------------------------------------------------------
# uniform-config spans (hackathon review idea #3, from Autobelay)
#
# Intraday tier escalations create versions named <base>-t<tier>-HHMMSS-
# xxxx (config_store.record_tier_change), so by late October the history
# held dozens of "config versions" and Alpha Evolution twice refused to
# analyse on "too many configurations in the sample". The confound that
# matters is the BASE configuration: a span is a maximal run of versions
# sharing one base, and evidence is windowed per span instead of being
# refused wholesale.
# ----------------------------------------------------------------------

_TIER_VERSION_RE = re.compile(r"^(?P<base>.+)-t[123]-\d{6}-[0-9a-f]{4}$")


def span_base(config_version: str | None) -> str:
    """Collapse a tier-escalation version onto the base it derives from."""
    if not config_version:
        return ""
    match = _TIER_VERSION_RE.match(config_version)
    return match.group("base") if match else config_version


async def config_spans(db: Database) -> list[dict[str, Any]]:
    """Whole-history spans, oldest first, with per-span outcome counts.

    Spans deliberately ignore the brief's lookback window: the current
    span may have started long before it, and cutting a span mid-way would
    reintroduce the very confound this exists to remove.
    """
    versions = await db.fetchall(
        "SELECT config_version, activated_at FROM config_versions "
        "ORDER BY activated_at")
    spans: list[dict[str, Any]] = []
    for row in versions:
        base = span_base(row["config_version"])
        if spans and spans[-1]["base_version"] == base:
            spans[-1]["versions"].append(row["config_version"])
        else:
            spans.append({"base_version": base,
                          "started_at": row["activated_at"],
                          "ended_at": None,
                          "versions": [row["config_version"]]})
    for prev, nxt in zip(spans, spans[1:]):
        prev["ended_at"] = nxt["started_at"]

    decision_counts = await db.fetchall(
        "SELECT config_version, COUNT(*) AS n FROM decisions "
        "WHERE COALESCE(candidate_track,'') != 'CALIBRATION' "
        "GROUP BY config_version")
    trade_rows = await db.fetchall(
        "SELECT d.config_version, COUNT(*) AS n, "
        "ROUND(SUM(t.realized_pnl), 2) AS pnl, "
        "SUM(CASE WHEN t.realized_pnl > 0 THEN 1 ELSE 0 END) AS wins "
        "FROM trade_journal t "
        "JOIN decisions d ON d.decision_id = t.decision_id "
        "WHERE t.status = 'CLOSED' GROUP BY d.config_version")
    decisions_by_version = {r["config_version"]: int(r["n"] or 0)
                            for r in decision_counts}
    trades_by_version = {r["config_version"]: r for r in trade_rows}

    for span in spans:
        decisions = closed = wins = 0
        pnl = 0.0
        for version in span["versions"]:
            decisions += decisions_by_version.get(version, 0)
            trade = trades_by_version.get(version)
            if trade:
                closed += int(trade["n"] or 0)
                wins += int(trade["wins"] or 0)
                pnl += float(trade["pnl"] or 0.0)
        span["decisions"] = decisions
        span["closed_trades"] = closed
        span["wins"] = wins
        span["pnl"] = round(pnl, 2)
        span["version_count"] = len(span["versions"])
    return spans


async def current_span_versions(db: Database) -> tuple[list[str],
                                                       dict[str, Any] | None]:
    """Versions of the span in force now, plus its metadata (or None)."""
    spans = await config_spans(db)
    if not spans:
        return [], None
    current = spans[-1]
    meta = {k: v for k, v in current.items() if k != "versions"}
    return list(current["versions"]), meta


@dataclass(slots=True)
class LessonBrief:
    """Deterministic facts. Nothing here is model-generated."""

    period_start: datetime
    period_end: datetime
    performance: dict[str, Any] = field(default_factory=dict)
    by_track: list[dict[str, Any]] = field(default_factory=list)
    decisions: list[dict[str, Any]] = field(default_factory=list)
    abstentions: list[dict[str, Any]] = field(default_factory=list)
    gates: list[dict[str, Any]] = field(default_factory=list)
    gate_value: list[dict[str, Any]] = field(default_factory=list)
    funnel: dict[str, Any] = field(default_factory=dict)
    execution: list[dict[str, Any]] = field(default_factory=list)
    red_team: list[dict[str, Any]] = field(default_factory=list)
    analysts: dict[str, Any] = field(default_factory=dict)
    intelligence: dict[str, Any] = field(default_factory=dict)
    config_versions: list[dict[str, Any]] = field(default_factory=list)
    config_spans: list[dict[str, Any]] = field(default_factory=list)
    current_span: dict[str, Any] = field(default_factory=dict)

    @property
    def closed_trades(self) -> int:
        return int(self.performance.get("closed_trades") or 0)

    @property
    def decision_count(self) -> int:
        return sum(int(d.get("n") or 0) for d in self.decisions)

    def to_sections(self) -> dict[str, Any]:
        return {
            "period": {"start": iso_utc(self.period_start),
                       "end": iso_utc(self.period_end)},
            "note": (
                "Every number below was computed in SQL from the system's "
                "own records. Do not recalculate them and do not infer "
                "figures that are absent."),
            "realized_performance": self.performance,
            "performance_by_track": self.by_track,
            "decision_outcomes": self.decisions,
            "abstention_reasons": self.abstentions,
            "gate_histogram": self.gates,
            "gate_value": self.gate_value,
            "funnel_attrition": self.funnel,
            "execution_quality": self.execution,
            "red_team_verdicts": self.red_team,
            "analyst_behaviour": self.analysts,
            "intelligence_coverage": self.intelligence,
            "config_versions_in_period": self.config_versions,
            "uniform_config_spans": self.config_spans,
            "current_span_performance": self.current_span,
            "span_note": (
                "A span is a maximal run of sessions under ONE base "
                "configuration; intraday tier escalations collapse onto "
                "their base. Trades from different spans are different "
                "experiments: draw performance conclusions from the "
                "current span, use earlier spans only to form hypotheses, "
                "and treat cross-span aggregates as confounded. Config "
                "churn is windowed for you - it is no longer a reason to "
                "refuse analysis; a thin CURRENT span is."),
        }


async def build_brief(db: Database, lookback_days: int = 7,
                      now: datetime | None = None) -> LessonBrief:
    """Aggregate everything the system recorded. Pure SQL."""
    now = now or utc_now()
    start = now - timedelta(days=lookback_days)
    since = iso_utc(start)

    brief = LessonBrief(period_start=start, period_end=now)

    row = await db.fetchone(
        "SELECT COUNT(*) AS closed_trades, "
        "ROUND(SUM(realized_pnl), 2) AS total_pnl, "
        "SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) AS wins, "
        "ROUND(AVG(CASE WHEN realized_pnl > 0 THEN realized_pnl END), 2) "
        "  AS average_win, "
        "ROUND(AVG(CASE WHEN realized_pnl <= 0 THEN realized_pnl END), 2) "
        "  AS average_loss, "
        "ROUND(AVG(realized_return_pct), 3) AS average_return_pct "
        "FROM trade_journal WHERE status='CLOSED' AND closed_at >= ?",
        (since,))
    performance = dict(row or {})
    closed = int(performance.get("closed_trades") or 0)
    performance["win_rate"] = (round(int(performance.get("wins") or 0) / closed, 3)
                               if closed else None)
    brief.performance = performance

    brief.by_track = await db.fetchall(
        "SELECT candidate_track, COUNT(*) AS n, "
        "ROUND(SUM(realized_pnl), 2) AS pnl, "
        "SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) AS wins "
        "FROM trade_journal WHERE status='CLOSED' AND closed_at >= ? "
        "GROUP BY candidate_track", (since,))

    brief.decisions = await db.fetchall(
        "SELECT state, COUNT(*) AS n FROM decisions "
        "WHERE created_at >= ? GROUP BY state ORDER BY n DESC", (since,))

    # Abstention text is the densest signal available when nothing trades.
    brief.abstentions = await db.fetchall(
        "SELECT symbol, revision, substr(abstain_reason, 1, 260) AS reason "
        "FROM trade_proposals WHERE trade = 0 AND abstain_reason IS NOT NULL "
        "AND created_at >= ? ORDER BY created_at DESC LIMIT 12", (since,))

    brief.gates = await db.fetchall(
        "SELECT stage, gate_id, tier, COUNT(*) AS rejections, "
        "COUNT(DISTINCT symbol) AS distinct_symbols FROM gate_rejections "
        "WHERE occurred_at >= ? GROUP BY stage, gate_id, tier "
        "ORDER BY rejections DESC LIMIT 20", (since,))

    brief.gate_value = await db.fetchall("SELECT * FROM v_gate_value LIMIT 10")

    funnel = await db.fetchone(
        "SELECT COUNT(*) AS scans, "
        "ROUND(AVG(discovery_count), 1) AS avg_discovered, "
        "ROUND(AVG(stage0_survivors), 1) AS avg_stage0, "
        "ROUND(AVG(prescore_survivors), 1) AS avg_prescore, "
        "ROUND(AVG(options_prescreened), 1) AS avg_options, "
        "ROUND(AVG(final_candidates), 2) AS avg_final, "
        "ROUND(AVG(councils_started), 2) AS avg_councils, "
        "SUM(event_track_count) AS event_track, "
        "SUM(momentum_track_count) AS momentum_track "
        "FROM funnel_snapshots WHERE as_of >= ?", (since,))
    brief.funnel = dict(funnel or {})

    brief.execution = await db.fetchall(
        "SELECT side, COUNT(*) AS n, "
        "ROUND(AVG(fill_bias_vs_adjusted), 4) AS mean_bias, "
        "ROUND(AVG(fill_slippage_pct), 5) AS mean_slippage, "
        "ROUND(AVG(seconds_to_fill), 1) AS mean_seconds, "
        "ROUND(AVG(limit_walk_steps), 2) AS mean_walk_steps "
        "FROM execution_calibrations WHERE actual_fill_debit IS NOT NULL "
        "AND submitted_at >= ? GROUP BY side", (since,))

    brief.red_team = await db.fetchall(
        "SELECT verdict, COUNT(*) AS n, ROUND(AVG(risk_score), 1) AS avg_risk, "
        "ROUND(AVG(recommended_max_risk_pct), 2) AS avg_max_risk "
        "FROM red_team_reviews WHERE created_at >= ? GROUP BY verdict",
        (since,))

    analysts = await db.fetchall(
        "SELECT agent_name, COUNT(*) AS calls, "
        "SUM(CASE WHEN status='OK' THEN 1 ELSE 0 END) AS ok, "
        "SUM(CASE WHEN status!='OK' THEN 1 ELSE 0 END) AS failed "
        "FROM agent_runs WHERE started_at >= ? GROUP BY agent_name",
        (since,))
    brief.analysts = {"runs": analysts}

    intel = await db.fetchone(
        "SELECT COUNT(*) AS events, COUNT(DISTINCT symbol) AS symbols, "
        "ROUND(AVG(catalyst_score), 1) AS avg_catalyst, "
        "SUM(CASE WHEN catalyst_score >= 55 THEN 1 ELSE 0 END) AS material "
        "FROM intelligence_events WHERE created_at >= ?", (since,))
    brief.intelligence = dict(intel or {})

    brief.config_versions = await db.fetchall(
        "SELECT config_version, tier, activated_at, note FROM config_versions "
        "WHERE activated_at >= ? ORDER BY activated_at", (since,))

    spans = await config_spans(db)
    brief.config_spans = [
        {k: v for k, v in span.items() if k != "versions"}
        for span in spans[-6:]]
    if spans:
        current = dict(brief.config_spans[-1])
        wins, closed = current.get("wins", 0), current.get("closed_trades", 0)
        current["win_rate"] = (round(wins / closed, 3) if closed else None)
        brief.current_span = current

    return brief


class LessonGenerator:
    """One LLM call turning a factual brief into tested hypotheses."""

    def __init__(self, client: LLMClient, db: Database,
                 config: dict[str, Any]):
        self.client = client
        self.db = db
        self.config = config
        self._prompt: str | None = None

    def prompt(self) -> str:
        if self._prompt is None:
            from alpha_council.settings import load_prompt

            self._prompt = load_prompt("lessons_system")
        return self._prompt

    async def generate(self, brief: LessonBrief,
                       session_id: str = "lessons") -> LessonSet | None:
        package = EvidencePackage(
            symbol="PORTFOLIO", as_of=brief.period_end, role="PM",
            sections=brief.to_sections())
        package.token_estimate = estimate_tokens(package.to_json())

        result = await self.client.call(
            "lessons", self.prompt(), package, LessonSet,
            session_id=session_id, estimated_cost=0.04)

        if result.failed or not isinstance(result.parsed, LessonSet):
            await self.db.log_event(
                "WARN", "lessons", "LESSONS_FAILED",
                result.error or "no valid lesson set")
            return None

        lessons = result.parsed
        await self.persist(lessons)
        await self.db.log_event(
            "INFO", "lessons", "LESSONS_GENERATED",
            f"{len(lessons.lessons)} lessons, "
            f"{len(lessons.actionable)} actionable",
            {"closed_trades": lessons.closed_trades,
             "insufficient_evidence": lessons.insufficient_evidence,
             "cost_usd": result.cost_usd})
        return lessons

    async def persist(self, lessons: LessonSet) -> int:
        rows = []
        for lesson in lessons.lessons:
            rows.append((
                f"les_{new_uuid()[:12]}", None, str(lesson.lesson_type),
                iso_utc(lessons.generated_at), lesson.observation,
                lesson.explanation_hypothesis,
                json.dumps(lesson.evidence_for),
                json.dumps(lesson.evidence_against),
                lesson.sample_size, str(lesson.confidence),
                lesson.proposed_test,
                1 if lesson.recommends_change else 0))
        if not rows:
            return 0
        await self.db.executemany(
            "INSERT INTO strategy_lessons(lesson_id, source_decision_id, "
            "lesson_type, created_at, observation, explanation_hypothesis, "
            "evidence_for_json, evidence_against_json, sample_size, "
            "confidence, proposed_test, recommends_change) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        return len(rows)


def format_lessons(lessons: LessonSet) -> str:
    """Readable rendering for the console and the dashboard."""
    lines = [
        f"Period: {lessons.period_start:%Y-%m-%d} to "
        f"{lessons.period_end:%Y-%m-%d}",
        f"Closed trades: {lessons.closed_trades}   "
        f"Decisions reviewed: {lessons.decisions_reviewed}",
        "",
        lessons.overall_assessment,
    ]
    if lessons.insufficient_evidence:
        lines.append("")
        lines.append("The generator judged the sample too thin for "
                     "performance conclusions.")

    for index, lesson in enumerate(lessons.lessons, start=1):
        lines.extend([
            "",
            f"{index}. [{lesson.lesson_type}] {lesson.confidence} "
            f"confidence, n={lesson.sample_size}",
            f"   observation : {lesson.observation}",
            f"   hypothesis  : {lesson.explanation_hypothesis}",
        ])
        for item in lesson.evidence_for[:3]:
            lines.append(f"   for         : {item}")
        for item in lesson.evidence_against[:3]:
            lines.append(f"   against     : {item}")
        lines.append(f"   test        : {lesson.proposed_test}")
        if lesson.recommends_change:
            lines.append(f"   CHANGE      : {lesson.proposed_change}")

    if not lessons.actionable:
        lines.append("")
        lines.append("No lesson reached the confidence needed to recommend "
                     "a change. Nothing in the configuration should move on "
                     "this evidence.")
    return "\n".join(lines)
