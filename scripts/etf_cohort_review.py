"""
Alpha Council v2.6 - render the ETF cohort review on demand.

The scheduler writes this review automatically once, on/after
priors.cohort_review_date (2026-11-20). This script renders the same
deterministic report any day, so progress can be checked mid-probation
without disturbing the run-once flag.

Usage:
    uv run python scripts/etf_cohort_review.py            # print
    uv run python scripts/etf_cohort_review.py --write    # also save to reports/
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from alpha_council.db.engine import Database  # noqa: E402
from alpha_council.evolution.cohort_review import (  # noqa: E402
    build_cohort_review,
    write_cohort_review,
)
from alpha_council.settings import get_settings, load_yaml  # noqa: E402


async def run(write: bool) -> int:
    settings = get_settings()
    scoring = load_yaml("scoring")
    async with Database(settings.database_path) as db:
        text = await build_cohort_review(db, scoring)
        print(text)
        if write:
            path = await write_cohort_review(db, scoring)
            print(f"\nsaved: {path}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--write", action="store_true",
                    help="also save the report under reports/")
    return asyncio.run(run(ap.parse_args().write))


if __name__ == "__main__":
    sys.exit(main())
