"""
Alpha Council v2.5 - post-trade lesson models.

A lesson is a hypothesis about system behaviour, tied to a stated sample
size and an explicit test. It is not a conclusion and it is not permission
to change anything.

The validators here enforce the one property that makes lessons safe on a
competition-week sample: a hypothesis drawn from four observations cannot
recommend a configuration change, however confident the wording. With
three trading sessions of data, every honest lesson is LOW confidence, and
the model is not allowed to talk itself out of that.

Place at: alpha_council/models/lessons.py
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import Field, model_validator

from alpha_council.models.base import StrictModel

# Below this, a pattern is an anecdote. The threshold is deliberately low
# for a five-day competition and would be far higher in production.
MIN_SAMPLE_FOR_MEDIUM = 5
MIN_SAMPLE_FOR_HIGH = 15


class LessonType(StrEnum):
    """What part of the system a lesson is about."""

    SELECTION = "SELECTION"       # which candidates get chosen
    EXPRESSION = "EXPRESSION"     # which structure expresses the thesis
    SIZING = "SIZING"             # how much risk is taken
    TIMING = "TIMING"             # when entries and exits happen
    EXECUTION = "EXECUTION"       # fills, slippage, limit walks
    GATE = "GATE"                 # a specific gate's behaviour
    INTELLIGENCE = "INTELLIGENCE" # catalyst scoring and news handling
    ABSTENTION = "ABSTENTION"     # why the council declines
    DATA = "DATA"                 # feed quality and coverage


class Confidence(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class StrategyLesson(StrictModel):
    lesson_type: LessonType
    observation: str = Field(min_length=20)
    explanation_hypothesis: str = Field(min_length=20)
    evidence_for: list[str] = Field(default_factory=list, max_length=6)
    evidence_against: list[str] = Field(default_factory=list, max_length=6)
    sample_size: int = Field(ge=0)
    confidence: Confidence
    proposed_test: str = Field(min_length=10)
    recommends_change: bool = False
    proposed_change: str | None = None

    @model_validator(mode="after")
    def _confidence_matches_sample(self) -> "StrategyLesson":
        """Confidence is capped by how much was actually observed.

        A model will happily write HIGH confidence over three data points.
        The cap is arithmetic, not editorial.
        """
        if (self.confidence is Confidence.HIGH
                and self.sample_size < MIN_SAMPLE_FOR_HIGH):
            raise ValueError(
                f"HIGH confidence requires at least {MIN_SAMPLE_FOR_HIGH} "
                f"observations, got {self.sample_size}")
        if (self.confidence is Confidence.MEDIUM
                and self.sample_size < MIN_SAMPLE_FOR_MEDIUM):
            raise ValueError(
                f"MEDIUM confidence requires at least "
                f"{MIN_SAMPLE_FOR_MEDIUM} observations, got "
                f"{self.sample_size}")
        return self

    @model_validator(mode="after")
    def _low_confidence_cannot_recommend(self) -> "StrategyLesson":
        """A LOW-confidence lesson may not propose a change.

        This is the guard that keeps a five-day sample from rewriting the
        configuration. Lessons at this confidence are observations to
        carry forward, not instructions.
        """
        if self.recommends_change and self.confidence is Confidence.LOW:
            raise ValueError(
                "a LOW confidence lesson cannot recommend a change; state "
                "the proposed_test instead and let evidence accumulate")
        return self

    @model_validator(mode="after")
    def _change_needs_a_description(self) -> "StrategyLesson":
        if self.recommends_change and not self.proposed_change:
            raise ValueError(
                "recommends_change is true but proposed_change is empty")
        return self

    @model_validator(mode="after")
    def _contrary_evidence_is_required(self) -> "StrategyLesson":
        """A hypothesis with nothing against it has not been tested.

        The prompt asks for the strongest case against each lesson. An
        empty list means the model skipped that step.
        """
        if not self.evidence_against:
            raise ValueError(
                "evidence_against is empty; state what would make this "
                "hypothesis wrong, or what the sample cannot rule out")
        return self


class LessonSet(StrictModel):
    """One analysis pass over a period of system behaviour."""

    generated_at: datetime
    period_start: datetime
    period_end: datetime
    closed_trades: int = Field(ge=0)
    decisions_reviewed: int = Field(ge=0)
    overall_assessment: str = Field(min_length=20)
    lessons: list[StrategyLesson] = Field(default_factory=list, max_length=6)
    insufficient_evidence: bool = False

    @model_validator(mode="after")
    def _thin_samples_are_declared(self) -> "LessonSet":
        """With almost no closed trades, saying so is the finding.

        A lesson set that confidently draws performance conclusions from
        two trades is worse than one that reports the sample is too thin.
        """
        if self.closed_trades < 3 and not self.insufficient_evidence:
            performance_types = {LessonType.SELECTION, LessonType.EXPRESSION,
                                 LessonType.SIZING, LessonType.TIMING}
            offending = [l for l in self.lessons
                         if l.lesson_type in performance_types
                         and l.confidence is not Confidence.LOW]
            if offending:
                raise ValueError(
                    f"{self.closed_trades} closed trades cannot support a "
                    f"{offending[0].confidence} performance lesson; set "
                    "insufficient_evidence or lower the confidence")
        return self

    @property
    def actionable(self) -> list[StrategyLesson]:
        return [l for l in self.lessons if l.recommends_change]

    def by_type(self, lesson_type: LessonType) -> list[StrategyLesson]:
        return [l for l in self.lessons if l.lesson_type is lesson_type]
