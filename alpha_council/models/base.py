"""
Alpha Council v2.3 - shared model base.

Place at: alpha_council/models/base.py
"""

from __future__ import annotations

from datetime import datetime, timezone

from pydantic import BaseModel, ConfigDict


class StrictModel(BaseModel):
    """Base for every domain model.

    extra="forbid" is load-bearing: it is how an LLM returning an
    unexpected field becomes a NO TRADE instead of a silent success.
    """

    model_config = ConfigDict(
        extra="forbid",
        validate_assignment=True,
        str_strip_whitespace=True,
    )


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def clip(value: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, value))
