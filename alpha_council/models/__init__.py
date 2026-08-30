"""
Alpha Council v2.4 - domain models.

Place at: alpha_council/models/__init__.py
"""

from alpha_council.models.base import StrictModel, clip, utc_now
from alpha_council.models.calibration import ExecutionCalibration, FillBiasEstimate
from alpha_council.models.candidate import AnalystAssessment, CandidateFeatures
from alpha_council.models.discovery import (
    DiscoveryCandidate,
    DiscoverySourceStatus,
    FunnelSnapshot,
)
from alpha_council.models.enums import (
    CandidateTrack,
    DataConfidence,
    DecisionState,
    Direction,
    DiscoveryDisableReason,
    DiscoverySource,
    ExitReason,
    GateStage,
    MarkMethod,
    OrderSide,
    RiskDecision,
    Severity,
    ShadowVariant,
    SourceTier,
    StrategyType,
    Verdict,
)
from alpha_council.models.execution import (
    AttributionSnapshot,
    ExecutionIntent,
    OrderReceipt,
    ShadowMark,
    ShadowTradeDefinition,
)
from alpha_council.models.intelligence import IntelligenceEvent, IntelligenceItem
from alpha_council.models.market import Bar, DataQualityResult, QuoteObservation
from alpha_council.models.risk import GateRejection, RiskEvaluation, RiskViolation
from alpha_council.models.trading import (
    InvalidationRule,
    OptionLeg,
    OptionStructure,
    PortfolioProposal,
    RedTeamProblem,
    RedTeamReview,
)

__all__ = [
    "AnalystAssessment", "AttributionSnapshot", "Bar", "CandidateFeatures",
    "CandidateTrack", "DataConfidence", "DataQualityResult", "DecisionState",
    "Direction", "DiscoveryCandidate", "DiscoveryDisableReason",
    "DiscoverySource", "DiscoverySourceStatus", "ExecutionCalibration",
    "ExecutionIntent", "ExitReason", "FillBiasEstimate", "FunnelSnapshot",
    "GateRejection", "GateStage", "IntelligenceEvent", "IntelligenceItem",
    "InvalidationRule", "MarkMethod", "OptionLeg", "OptionStructure",
    "OrderReceipt", "OrderSide", "PortfolioProposal", "QuoteObservation",
    "RedTeamProblem", "RedTeamReview", "RiskDecision", "RiskEvaluation",
    "RiskViolation", "Severity", "ShadowMark", "ShadowTradeDefinition",
    "ShadowVariant", "SourceTier", "StrategyType", "StrictModel", "Verdict",
    "clip", "utc_now",
]
