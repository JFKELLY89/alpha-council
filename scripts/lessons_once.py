"""
Alpha Council v2.5 - post-trade lessons runner.

Writes the lessons prompt on first run, aggregates the deterministic brief,
and calls the generator. Roughly $0.04 per run.

Place at: scripts/lessons_once.py

Usage:
    uv run python scripts/lessons_once.py
    uv run python scripts/lessons_once.py --days 3 --brief-only
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from alpha_council.agents.budget import BudgetManager  # noqa: E402
from alpha_council.agents.llm import OpenAIClient  # noqa: E402
from alpha_council.db.engine import Database  # noqa: E402
from alpha_council.evolution.lessons import (  # noqa: E402
    LessonGenerator,
    build_brief,
    format_lessons,
)
from alpha_council.settings import (  # noqa: E402
    PROMPTS_DIR,
    ensure_directories,
    get_settings,
    load_yaml,
)

LESSONS_SYSTEM = """
You are Alpha Council's Post-Trade Lesson Analyst.

You are given a factual brief describing what the system actually did over
a period: realized trades, decisions, abstention reasons, gate rejections,
funnel attrition, execution quality, and intelligence coverage. Every
number in it was computed from the system's own records.

Produce hypotheses about system behaviour. You are not producing
conclusions, and you are not authorised to change anything.

Rules:

- DO NOT RECALCULATE ANY FIGURE. The arithmetic is already done. If you
  need a number that is not in the brief, say it is unavailable.
- Every lesson must carry the sample size it was drawn from. Count the
  actual observations, not the population they came from.
- Confidence is capped by that sample. Under 5 observations is LOW, under
  15 is at most MEDIUM. A LOW confidence lesson may not recommend a
  change; state the test instead and let evidence accumulate.
- Every lesson requires evidence_against: the strongest reason your
  hypothesis might be wrong, or what this sample cannot rule out. A
  hypothesis with nothing against it has not been thought about.
- Every lesson requires a proposed_test: something specific that would
  confirm or refute it with more data.
- Prefer few good lessons to many weak ones. Three is usually better than
  six.

On thin samples:

If there are fewer than three closed trades, you cannot draw performance
conclusions and you should set insufficient_evidence to true. That is not
a failure to analyse; it is the correct reading. Say so plainly in
overall_assessment.

When trades are scarce the richest material is why trades did NOT happen:
abstention reasons, which gates eliminated the most candidates, where the
funnel lost breadth, and whether the intelligence layer supplied usable
catalysts. Those are analysable from the first session and are where you
should concentrate.

Be specific. "The PM is too conservative" is not a lesson. "Six of eight
abstentions cited a missing invalidation level, which suggests the
evidence pack lacks support and resistance data" is a lesson.

Output only the structured LessonSet object.
""".strip()


def say(msg: str = "") -> None:
    print(msg, flush=True)


def rule(title: str) -> None:
    say("")
    say("=" * 74)
    say(title)
    say("=" * 74)


async def run(args: argparse.Namespace) -> int:
    settings = get_settings()
    settings.assert_paper_only()
    scoring = load_yaml("scoring")

    ensure_directories()
    prompt_path = PROMPTS_DIR / "lessons_system.txt"
    if not prompt_path.exists() or args.rewrite_prompt:
        prompt_path.write_text(LESSONS_SYSTEM + "\n", encoding="utf-8")
        say(f"  wrote {prompt_path.name}")

    async with Database(settings.database_path) as db:
        rule("1. DETERMINISTIC BRIEF")
        brief = await build_brief(db, lookback_days=args.days)

        say(f"  period          : {brief.period_start:%Y-%m-%d} to "
            f"{brief.period_end:%Y-%m-%d}")
        say(f"  closed trades   : {brief.closed_trades}")
        say(f"  decisions       : {brief.decision_count}")
        say(f"  abstentions     : {len(brief.abstentions)}")
        say(f"  gates recorded  : {len(brief.gates)}")
        say(f"  intelligence    : {brief.intelligence.get('events', 0)} events, "
            f"{brief.intelligence.get('material', 0)} material")
        say("")
        say("  funnel averages per scan:")
        for key in ("avg_discovered", "avg_stage0", "avg_prescore",
                    "avg_options", "avg_final", "avg_councils"):
            say(f"    {key:<18}{brief.funnel.get(key)}")

        if brief.gates:
            say("")
            say("  top gates:")
            for gate in brief.gates[:6]:
                say(f"    {gate['gate_id']:<30}{gate['rejections']:>6}")

        if brief.abstentions:
            say("")
            say("  recent abstentions:")
            for row in brief.abstentions[:4]:
                say(f"    {row['symbol']:<7}{str(row['reason'])[:80]}")

        if args.brief_only:
            rule("BRIEF ONLY")
            say("  No model was called.")
            if args.dump:
                say(json.dumps(brief.to_sections(), indent=2, default=str))
            return 0

        if not settings.has_openai():
            say("  OPENAI_API_KEY is not set.")
            return 1

        rule("2. GENERATING LESSONS")
        budget = BudgetManager(db, scoring)
        await budget.load()
        client = OpenAIClient(db, budget, scoring,
                              settings.openai_api_key.get_secret_value())
        generator = LessonGenerator(client, db, scoring)

        lessons = await generator.generate(brief)
        if lessons is None:
            say("  The generator returned no valid lesson set.")
            return 1

        rule("3. LESSONS")
        say(format_lessons(lessons))

        rule("SUMMARY")
        say(f"  lessons written : {len(lessons.lessons)}")
        say(f"  actionable      : {len(lessons.actionable)}")
        say(f"  budget          : {budget.summary()['openai']}")

    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--brief-only", action="store_true",
                    help="aggregate and print without calling a model")
    ap.add_argument("--dump", action="store_true",
                    help="print the full brief as JSON")
    ap.add_argument("--rewrite-prompt", action="store_true")
    return asyncio.run(run(ap.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
