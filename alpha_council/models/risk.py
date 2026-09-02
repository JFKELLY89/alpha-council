"""
Alpha Council v2.3 - risk and gate models.

Place at: alpha_council/models/risk.py
"""

from __future__ import annotations

from datetime import datetime

from pydantic import Field, model_validator

from alpha_council.models.base import StrictModel
from alpha_council.models.enums import Direction, GateStage, RiskDecision, Severity


class RiskViolation(StrictModel):
    rule_id: str
    severity: Severity
    message: str
    observed_value: float | str | None = None
    allowed_value: float | str | None = None


class RiskEvaluation(StrictModel):
    decision_id: str
    evaluated_at: datetime
    decision: RiskDecision
    config_version: str = "v1"
    tier: int = Field(default=1, ge=1, le=3)

    account_equity: float = Field(gt=0)
    requested_qty: int = Field(ge=0)
    approved_qty: int = Field(ge=0)
    requested_max_loss: float = Field(ge=0)
    approved_max_loss: float = Field(ge=0)
    # Dollars of risk the binding cap actually granted, before flooring to
    # whole spreads. The limit walk prices against budget/qty; against
    # approved_max_loss/qty the ceiling equals the first price and the walk
    # degenerates to one attempt.
    approved_risk_budget: float = Field(default=0.0, ge=0)

    total_open_risk_pct_after: float = Field(ge=0)
    sector_risk_pct_after: float = Field(ge=0)
    daily_drawdown_pct: float = Field(ge=0)
    competition_drawdown_pct: float = Field(ge=0)

    violations: list[RiskViolation] = Field(default_factory=list)

    @model_validator(mode="after")
    def _decision_coherent(self) -> "RiskEvaluation":
        if self.approved_qty > self.requested_qty:
            raise ValueError("approved quantity cannot exceed the request")

        if self.decision.blocks_trade and self.approved_qty != 0:
            raise ValueError(f"{self.decision} requires approved_qty=0")
        if self.decision is RiskDecision.APPROVE:
            if self.approved_qty != self.requested_qty:
                raise ValueError("APPROVE means the full request was granted; "
                                 "use RESIZE otherwise")
            if any(v.severity in (Severity.BLOCK, Severity.HALT)
                   for v in self.violations):
                raise ValueError("APPROVE cannot carry a BLOCK or HALT violation")
        if self.decision is RiskDecision.RESIZE:
            if self.approved_qty < 1:
                raise ValueError("RESIZE must approve at least one spread")
            if self.approved_qty >= self.requested_qty:
                raise ValueError("RESIZE must reduce the quantity")
        if self.decision is RiskDecision.HALT:
            if not any(v.severity is Severity.HALT for v in self.violations):
                raise ValueError("HALT requires a HALT-severity violation")
        if (self.approved_risk_budget > 0
                and self.decision in (RiskDecision.APPROVE,
                                      RiskDecision.RESIZE)
                and self.approved_risk_budget + 1e-6 < self.approved_max_loss):
            raise ValueError(
                f"approved_risk_budget {self.approved_risk_budget} is below "
                f"approved_max_loss {self.approved_max_loss}; the budget is "
                "the cap the quantity was floored from and cannot be smaller")
        return self

    @property
    def blocking_violations(self) -> list[RiskViolation]:
        return [v for v in self.violations
                if v.severity in (Severity.BLOCK, Severity.HALT)]


class GateRejection(StrictModel):
    """Spec Section 20. One row per gate that stops a candidate.

    This is not error logging. It is the measurement instrument that lets
    the system state what each gate cost or saved.
    """

    rejection_id: str
    occurred_at: datetime
    config_version: str
    scan_id: str | None = None
    decision_id: str | None = None
    symbol: str
    direction: Direction
    stage: GateStage
    gate_id: str
    observed_value: float | str | None = None
    threshold_value: float | str | None = None
    tier: int = Field(ge=1, le=3)
    hard_gate: bool
    shadow_eligible: bool = False
    shadow_structure_json: str | None = None
    note: str = ""

    @model_validator(mode="after")
    def _shadow_coherent(self) -> "GateRejection":
        if self.shadow_eligible and not self.stage.shadow_eligible:
            raise ValueError(
                f"{self.stage} occurs before a priced structure exists "
                "and cannot be shadow-marked"
            )
        if self.shadow_eligible and not self.shadow_structure_json:
            raise ValueError("shadow_eligible requires the structure payload")
        return self

    @model_validator(mode="after")
    def _gate_id_named(self) -> "GateRejection":
        if not self.gate_id.strip():
            raise ValueError("gate_id is required; it is the calibration key")
        return self
