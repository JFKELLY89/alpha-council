"""
Alpha Council v2.4 - domain enums.

Every string that crosses a module boundary or lands in a CHECK-constrained
database column is defined here, so the schema and the code cannot drift.

v2.4 additions: DiscoverySource, CandidateTrack, DiscoveryDisableReason.

Place at: alpha_council/models/enums.py
"""

from __future__ import annotations

from enum import StrEnum


class Direction(StrEnum):
    BULLISH = "BULLISH"
    BEARISH = "BEARISH"
    NEUTRAL = "NEUTRAL"

    @property
    def sign(self) -> int:
        """d = +1 bullish, -1 bearish, 0 neutral."""
        return {"BULLISH": 1, "BEARISH": -1, "NEUTRAL": 0}[self.value]


class DataConfidence(StrEnum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    DEGRADED = "DEGRADED"
    BLOCKED = "BLOCKED"

    @property
    def tradable(self) -> bool:
        return self is not DataConfidence.BLOCKED


class Verdict(StrEnum):
    PASS = "PASS"
    MODIFY = "MODIFY"
    VETO = "VETO"


class RiskDecision(StrEnum):
    APPROVE = "APPROVE"
    RESIZE = "RESIZE"
    REJECT = "REJECT"
    HALT = "HALT"

    @property
    def blocks_trade(self) -> bool:
        return self in (RiskDecision.REJECT, RiskDecision.HALT)


class StrategyType(StrEnum):
    BULL_CALL_DEBIT = "BULL_CALL_DEBIT"
    BEAR_PUT_DEBIT = "BEAR_PUT_DEBIT"

    @property
    def option_type(self) -> str:
        return "CALL" if self is StrategyType.BULL_CALL_DEBIT else "PUT"

    @property
    def direction(self) -> Direction:
        return (Direction.BULLISH if self is StrategyType.BULL_CALL_DEBIT
                else Direction.BEARISH)


class SourceTier(StrEnum):
    TIER_1_PRIMARY = "TIER_1_PRIMARY"
    TIER_2_MAJOR_NEWS = "TIER_2_MAJOR_NEWS"
    TIER_3_SPECIALIST = "TIER_3_SPECIALIST"
    TIER_4_SOCIAL = "TIER_4_SOCIAL"
    UNKNOWN = "UNKNOWN"


# ======================================================================
# v2.4: discovery
# ======================================================================

class DiscoverySource(StrEnum):
    """How a symbol entered the evaluation pool. Recorded on every
    candidate and decision so the demo can answer 'why did Alpha Council
    notice this symbol?'"""

    CORE = "CORE"
    ALPACA_NEWS = "ALPACA_NEWS"
    SEC_EVENT = "SEC_EVENT"
    MOST_ACTIVE = "MOST_ACTIVE"
    MOVER = "MOVER"
    OTHER_DYNAMIC = "OTHER_DYNAMIC"

    @property
    def is_optional(self) -> bool:
        """Optional sources fail open. A 403 disables the source for the
        session and never fails a scan."""
        return self in (DiscoverySource.MOST_ACTIVE, DiscoverySource.MOVER,
                        DiscoverySource.SEC_EVENT)

    @property
    def is_event_bearing(self) -> bool:
        """Sources that carry a catalyst and can seed the EVENT track."""
        return self in (DiscoverySource.ALPACA_NEWS, DiscoverySource.SEC_EVENT)

    @property
    def expires(self) -> bool:
        return self is not DiscoverySource.CORE


class CandidateTrack(StrEnum):
    """EVENT and MOMENTUM score on different scales, so they are ranked
    separately with quotas rather than merged into one list."""

    EVENT = "EVENT"
    MOMENTUM = "MOMENTUM"
    CALIBRATION = "CALIBRATION"

    @property
    def requires_catalyst(self) -> bool:
        return self is CandidateTrack.EVENT

    @property
    def is_alpha(self) -> bool:
        """CALIBRATION trades demonstrate the lifecycle. They are not alpha
        bets and must not count toward the alpha-trade tally."""
        return self is not CandidateTrack.CALIBRATION


class DiscoveryDisableReason(StrEnum):
    FORBIDDEN_403 = "FORBIDDEN_403"
    NOT_CONFIGURED = "NOT_CONFIGURED"
    REPEATED_ERROR = "REPEATED_ERROR"
    OPERATOR_DISABLED = "OPERATOR_DISABLED"


# ======================================================================
# execution and journaling
# ======================================================================

class ShadowVariant(StrEnum):
    GPT_ORIGINAL = "GPT_ORIGINAL"
    CLAUDE_MODIFIED = "CLAUDE_MODIFIED"
    EXECUTED = "EXECUTED"


class MarkMethod(StrEnum):
    """Must be identical across every variant at a given timestamp, or the
    attribution arithmetic is meaningless."""

    ADJUSTED_MID = "ADJUSTED_MID"
    CONSERVATIVE = "CONSERVATIVE"


class OrderSide(StrEnum):
    OPEN = "OPEN"
    CLOSE = "CLOSE"


class DecisionState(StrEnum):
    CANDIDATE = "CANDIDATE"
    COUNCIL_STARTED = "COUNCIL_STARTED"
    PM_PROPOSED = "PM_PROPOSED"
    STRUCTURES_GENERATED = "STRUCTURES_GENERATED"
    STRUCTURE_SELECTED = "STRUCTURE_SELECTED"
    RED_TEAMED = "RED_TEAMED"
    REVISED = "REVISED"
    RISK_APPROVED = "RISK_APPROVED"
    RISK_REJECTED = "RISK_REJECTED"
    ORDER_SUBMITTED = "ORDER_SUBMITTED"
    ORDER_WORKING = "ORDER_WORKING"
    FILLED = "FILLED"
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"
    NO_FILL = "NO_FILL"
    POSITION_OPEN = "POSITION_OPEN"
    POSITION_CLOSED = "POSITION_CLOSED"
    ATTRIBUTED = "ATTRIBUTED"


class GateStage(StrEnum):
    UNIVERSE = "UNIVERSE"
    DISCOVERY = "DISCOVERY"          # v2.4: eligibility / cap / TTL rejections
    STAGE0 = "STAGE0"                # v2.4: FastScore cut
    DATA_QUALITY = "DATA_QUALITY"
    PRESCORE = "PRESCORE"
    OPTIONS_CHAIN = "OPTIONS_CHAIN"
    OPTIONS_STRUCTURE = "OPTIONS_STRUCTURE"
    OPPORTUNITY_SCORE = "OPPORTUNITY_SCORE"
    BUDGET = "BUDGET"
    PM_ABSTAIN = "PM_ABSTAIN"
    RED_TEAM = "RED_TEAM"
    RISK = "RISK"
    EXECUTION = "EXECUTION"

    @property
    def shadow_eligible(self) -> bool:
        """Stages where a fully priced structure existed at rejection time
        and can therefore be shadow-marked."""
        return self in (
            GateStage.OPPORTUNITY_SCORE,
            GateStage.PM_ABSTAIN,
            GateStage.RED_TEAM,
            GateStage.RISK,
            GateStage.EXECUTION,
        )

    @property
    def is_cheap_stage(self) -> bool:
        """Stages that must never trigger a chain fetch or an LLM call."""
        return self in (GateStage.UNIVERSE, GateStage.DISCOVERY, GateStage.STAGE0)


class ExitReason(StrEnum):
    UNDERLYING_TARGET = "UNDERLYING_TARGET"
    UNDERLYING_INVALIDATION = "UNDERLYING_INVALIDATION"
    PROFIT_TARGET = "PROFIT_TARGET"
    PREMIUM_STOP = "PREMIUM_STOP"
    TIME_STOP = "TIME_STOP"
    COMPETITION_FLATTEN = "COMPETITION_FLATTEN"
    RISK_HALT = "RISK_HALT"
    MANUAL = "MANUAL"


class Severity(StrEnum):
    INFO = "INFO"
    WARN = "WARN"
    BLOCK = "BLOCK"
    HALT = "HALT"
