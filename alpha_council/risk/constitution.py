"""
Alpha Council v2.4 - the Risk Constitution.

This is the layer no model can talk its way past. Claude can recommend a
smaller size; it cannot authorize a larger one. GPT can propose a trade; it
cannot approve one. Every check here is code, every failure is journaled,
and the tier ladder never touches any of it.

Evaluation collects ALL violations rather than short-circuiting, because
"this trade failed on sector concentration" is far less useful for
calibration than "this trade failed on sector concentration and reward/risk
and was 40 minutes past the cutoff."

Place at: alpha_council/risk/constitution.py
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time
from typing import Any, Sequence

from alpha_council.models.enums import (
    DataConfidence,
    Direction,
    RiskDecision,
    Severity,
    StrategyType,
    Verdict,
)
from alpha_council.models.risk import RiskEvaluation, RiskViolation
from alpha_council.models.trading import OptionStructure
from alpha_council.risk.position_sizing import (
    SizingResult,
    max_qty_under_portfolio_limits,
    portfolio_risk_room,
    size_position,
)
from alpha_council.utils.time import parse_et_time, to_et, utc_now

ALLOWED_STRATEGIES = {StrategyType.BULL_CALL_DEBIT, StrategyType.BEAR_PUT_DEBIT}


@dataclass(slots=True)
class BlackoutWindow:
    name: str
    source: str
    timestamp_et: datetime
    pre_block_minutes: int = 15
    post_block_minutes: int = 5
    symbols: list[str] = field(default_factory=list)

    def blocks(self, when: datetime, symbol: str | None = None) -> bool:
        if self.symbols and symbol and symbol.upper() not in self.symbols:
            return False
        anchor = self.timestamp_et
        if anchor.tzinfo is None:
            # A calendar entry without an offset means ET, per the file's
            # naming. .timestamp() on a naive datetime would interpret it
            # in the machine's local zone instead.
            from alpha_council.utils.time import ET

            anchor = anchor.replace(tzinfo=ET)
        et = to_et(when)
        start = anchor.timestamp() - self.pre_block_minutes * 60
        end = anchor.timestamp() + self.post_block_minutes * 60
        return start <= et.timestamp() <= end


def load_blackouts(event_calendar: dict[str, Any]) -> list[BlackoutWindow]:
    out = []
    for ev in event_calendar.get("events", []) or []:
        ts = ev.get("timestamp_et")
        if not ts:
            continue
        try:
            when = datetime.fromisoformat(ts)
        except ValueError:
            continue
        out.append(BlackoutWindow(
            name=ev.get("name", "unnamed"), source=ev.get("source", ""),
            timestamp_et=when,
            pre_block_minutes=int(ev.get("pre_block_minutes", 15)),
            post_block_minutes=int(ev.get("post_block_minutes", 5)),
            symbols=[s.upper() for s in ev.get("symbols", []) or []],
        ))
    return out


@dataclass(slots=True)
class PortfolioState:
    equity: float
    day_start_equity: float
    peak_equity: float
    open_risk_dollars: float = 0.0
    sector_risk_dollars: dict[str, float] = field(default_factory=dict)
    open_position_count: int = 0
    open_decision_ids: set[str] = field(default_factory=set)

    @property
    def daily_drawdown_pct(self) -> float:
        if self.day_start_equity <= 0:
            return 0.0
        return max(0.0, (self.day_start_equity - self.equity)
                   / self.day_start_equity * 100.0)

    @property
    def competition_drawdown_pct(self) -> float:
        if self.peak_equity <= 0:
            return 0.0
        return max(0.0, (self.peak_equity - self.equity)
                   / self.peak_equity * 100.0)

    def sector_risk(self, sector: str) -> float:
        return self.sector_risk_dollars.get(sector, 0.0)


@dataclass(slots=True)
class TradeRequest:
    decision_id: str
    symbol: str
    sector: str
    direction: Direction
    structure: OptionStructure
    desired_risk_pct: float
    pm_confidence: float
    red_team_verdict: Verdict
    red_team_max_risk_pct: float | None
    equity_data_confidence: DataConfidence
    option_data_confidence: DataConfidence
    final_opportunity_score: float
    market_open: bool = True
    is_calibration_trade: bool = False


class RiskConstitution:
    """Deterministic APPROVE / RESIZE / REJECT / HALT."""

    def __init__(self, risk_cfg: dict[str, Any], scoring_cfg: dict[str, Any],
                 blackouts: Sequence[BlackoutWindow] = ()):
        self.hard = risk_cfg.get("hard", {})
        self.quality = risk_cfg.get("quality", {})
        self.scoring = scoring_cfg
        self.blackouts = list(blackouts)
        self.paper_only = bool(risk_cfg.get("paper_only", True))
        # The sector map lives in risk_constitution.yaml because it feeds
        # the 4% sector cap. The orchestrator previously looked for it in
        # universe.yaml, which has no such key, so every symbol mapped to
        # UNKNOWN and the sector cap never distinguished anything.
        self.sectors: dict[str, list[str]] = risk_cfg.get("sectors", {})

    def tier_cfg(self, tier: int) -> dict[str, Any]:
        return self.scoring.get("tiers", {}).get(tier, {})

    # ------------------------------------------------------------------

    def evaluate(self, request: TradeRequest, portfolio: PortfolioState,
                 tier: int = 1, config_version: str = "v2.4",
                 paper_mode: bool = True,
                 now: datetime | None = None) -> RiskEvaluation:
        now = now or utc_now()
        v: list[RiskViolation] = []
        s = request.structure
        tier_cfg = self.tier_cfg(tier)

        halt = self._check_halts(request, portfolio, paper_mode, v)
        self._check_eligibility(request, tier_cfg, now, v)
        self._check_structure(s, tier_cfg, v)
        self._check_duplicates(request, portfolio, v)

        sizing = self._size(request, portfolio, s, v)

        decision = self._decide(halt, v, sizing)

        return RiskEvaluation(
            decision_id=request.decision_id,
            evaluated_at=now,
            decision=decision,
            config_version=config_version,
            tier=tier,
            account_equity=portfolio.equity,
            requested_qty=sizing.requested_qty,
            approved_qty=(sizing.approved_qty
                          if decision in (RiskDecision.APPROVE,
                                          RiskDecision.RESIZE) else 0),
            requested_max_loss=round(
                sizing.requested_qty * s.max_loss_per_spread, 2),
            approved_max_loss=(sizing.approved_risk_dollars
                               if decision in (RiskDecision.APPROVE,
                                               RiskDecision.RESIZE) else 0.0),
            approved_risk_budget=(sizing.budget_dollars
                                  if decision in (RiskDecision.APPROVE,
                                                  RiskDecision.RESIZE)
                                  else 0.0),
            total_open_risk_pct_after=self._pct(
                portfolio.open_risk_dollars
                + sizing.approved_risk_dollars, portfolio.equity),
            sector_risk_pct_after=self._pct(
                portfolio.sector_risk(request.sector)
                + sizing.approved_risk_dollars, portfolio.equity),
            daily_drawdown_pct=round(portfolio.daily_drawdown_pct, 4),
            competition_drawdown_pct=round(
                portfolio.competition_drawdown_pct, 4),
            violations=v,
        )

    # ---- check groups ----------------------------------------------

    def _check_halts(self, request: TradeRequest, portfolio: PortfolioState,
                     paper_mode: bool, v: list[RiskViolation]) -> bool:
        halted = False

        if self.paper_only and not paper_mode:
            v.append(RiskViolation(
                rule_id="RISK_PAPER_MODE", severity=Severity.HALT,
                message="live trading mode detected; Alpha Council is paper-only",
                observed_value="live", allowed_value="paper"))
            halted = True

        daily_limit = float(self.hard.get("max_daily_drawdown_pct", 5.0))
        if portfolio.daily_drawdown_pct >= daily_limit:
            v.append(RiskViolation(
                rule_id="RISK_DAILY_DRAWDOWN", severity=Severity.HALT,
                message=f"daily drawdown {portfolio.daily_drawdown_pct:.2f}% "
                        f"at or beyond the {daily_limit}% limit",
                observed_value=round(portfolio.daily_drawdown_pct, 2),
                allowed_value=daily_limit))
            halted = True

        comp_limit = float(self.hard.get("max_competition_peak_drawdown_pct", 12.0))
        if portfolio.competition_drawdown_pct >= comp_limit:
            v.append(RiskViolation(
                rule_id="RISK_COMPETITION_DRAWDOWN", severity=Severity.HALT,
                message=f"peak-to-trough drawdown "
                        f"{portfolio.competition_drawdown_pct:.2f}% at or "
                        f"beyond the {comp_limit}% limit",
                observed_value=round(portfolio.competition_drawdown_pct, 2),
                allowed_value=comp_limit))
            halted = True

        return halted

    def _check_eligibility(self, request: TradeRequest, tier_cfg: dict[str, Any],
                           now: datetime, v: list[RiskViolation]) -> None:
        if not request.market_open:
            v.append(RiskViolation(
                rule_id="RISK_MARKET_CLOSED", severity=Severity.BLOCK,
                message="market is closed"))

        cutoff = self.hard.get("new_trade_cutoff_et", "15:20")
        if to_et(now).time() >= parse_et_time(cutoff):
            v.append(RiskViolation(
                rule_id="RISK_AFTER_CUTOFF", severity=Severity.BLOCK,
                message=f"past the {cutoff} ET new-trade cutoff",
                observed_value=to_et(now).strftime("%H:%M"),
                allowed_value=cutoff))

        for label, conf in (("equity", request.equity_data_confidence),
                            ("option", request.option_data_confidence)):
            if conf is DataConfidence.BLOCKED:
                v.append(RiskViolation(
                    rule_id="RISK_DATA_BLOCKED", severity=Severity.BLOCK,
                    message=f"{label} data quality is BLOCKED",
                    observed_value=str(conf), allowed_value="not BLOCKED"))

        if request.red_team_verdict is Verdict.VETO:
            v.append(RiskViolation(
                rule_id="RISK_RED_TEAM_VETO", severity=Severity.BLOCK,
                message="Red Team returned VETO; it cannot be overridden",
                observed_value="VETO", allowed_value="PASS or MODIFY"))

        for window in self.blackouts:
            if window.blocks(now, request.symbol):
                v.append(RiskViolation(
                    rule_id="RISK_EVENT_BLACKOUT", severity=Severity.BLOCK,
                    message=f"inside the blackout window for {window.name}",
                    observed_value=window.name))

        floor = float(tier_cfg.get("pm_confidence_floor", 0.60))
        if request.pm_confidence < floor:
            v.append(RiskViolation(
                rule_id="RISK_PM_CONFIDENCE", severity=Severity.BLOCK,
                message=f"PM confidence {request.pm_confidence:.2f} below "
                        f"the tier floor {floor:.2f}",
                observed_value=request.pm_confidence, allowed_value=floor))

        score_floor = float(tier_cfg.get("final_score_floor", 68.0))
        if request.final_opportunity_score < score_floor:
            v.append(RiskViolation(
                rule_id="RISK_SCORE_FLOOR", severity=Severity.BLOCK,
                message=f"opportunity score "
                        f"{request.final_opportunity_score:.1f} below the "
                        f"tier floor {score_floor:.1f}",
                observed_value=round(request.final_opportunity_score, 1),
                allowed_value=score_floor))

    def _check_structure(self, s: OptionStructure, tier_cfg: dict[str, Any],
                         v: list[RiskViolation]) -> None:
        if s.strategy not in ALLOWED_STRATEGIES:
            v.append(RiskViolation(
                rule_id="RISK_STRATEGY_NOT_ALLOWED", severity=Severity.BLOCK,
                message=f"{s.strategy} is not an allowed defined-risk vertical",
                observed_value=str(s.strategy)))

        if len(s.legs) != 2:
            v.append(RiskViolation(
                rule_id="RISK_LEG_COUNT", severity=Severity.BLOCK,
                message=f"expected exactly 2 legs, got {len(s.legs)}",
                observed_value=len(s.legs), allowed_value=2))

        min_dte = int(self.hard.get("min_dte", 3))
        if s.dte < min_dte:
            v.append(RiskViolation(
                rule_id="RISK_DTE_OUT_OF_BOUNDS", severity=Severity.BLOCK,
                message=f"DTE {s.dte} below the hard minimum {min_dte}",
                observed_value=s.dte, allowed_value=min_dte))
        if self.hard.get("no_0dte", True) and s.dte < 1:
            v.append(RiskViolation(
                rule_id="RISK_0DTE", severity=Severity.BLOCK,
                message="0DTE is prohibited", observed_value=s.dte))

        max_cw = float(tier_cfg.get("max_cost_to_width", 0.55))
        if s.cost_to_width_ratio > max_cw:
            v.append(RiskViolation(
                rule_id="RISK_COST_TO_WIDTH", severity=Severity.BLOCK,
                message=f"cost/width {s.cost_to_width_ratio:.3f} above the "
                        f"tier ceiling {max_cw:.3f}",
                observed_value=round(s.cost_to_width_ratio, 3),
                allowed_value=max_cw))

        floor = self.scoring.get("liquidity_floor", {})
        for leg in s.legs:
            min_oi = int(floor.get("min_open_interest", 75))
            if leg.open_interest is not None and leg.open_interest < min_oi:
                v.append(RiskViolation(
                    rule_id="RISK_LEG_OPEN_INTEREST", severity=Severity.BLOCK,
                    message=f"{leg.symbol} open interest {leg.open_interest} "
                            f"below the absolute floor {min_oi}",
                    observed_value=leg.open_interest, allowed_value=min_oi))
            max_sp = float(floor.get("max_leg_spread_pct", 0.22))
            if leg.spread_pct > max_sp:
                v.append(RiskViolation(
                    rule_id="RISK_LEG_SPREAD", severity=Severity.BLOCK,
                    message=f"{leg.symbol} spread {leg.spread_pct:.3f} above "
                            f"the absolute floor {max_sp:.3f}",
                    observed_value=round(leg.spread_pct, 3),
                    allowed_value=max_sp))

        if s.initial_limit_debit > s.natural_debit + 1e-9:
            v.append(RiskViolation(
                rule_id="RISK_LIMIT_ABOVE_NATURAL", severity=Severity.BLOCK,
                message="limit debit exceeds the natural debit",
                observed_value=s.initial_limit_debit,
                allowed_value=s.natural_debit))

    def _check_duplicates(self, request: TradeRequest,
                          portfolio: PortfolioState,
                          v: list[RiskViolation]) -> None:
        if request.decision_id in portfolio.open_decision_ids:
            v.append(RiskViolation(
                rule_id="RISK_DUPLICATE_ORDER", severity=Severity.BLOCK,
                message=f"an order already exists for {request.decision_id}",
                observed_value=request.decision_id))

        max_positions = int(self.hard.get("max_concurrent_positions", 5))
        if portfolio.open_position_count >= max_positions:
            v.append(RiskViolation(
                rule_id="RISK_MAX_POSITIONS", severity=Severity.BLOCK,
                message=f"{portfolio.open_position_count} open positions at "
                        f"the limit of {max_positions}",
                observed_value=portfolio.open_position_count,
                allowed_value=max_positions))

    def _size(self, request: TradeRequest, portfolio: PortfolioState,
              s: OptionStructure, v: list[RiskViolation]) -> SizingResult:
        total_limit = float(
            self.hard.get("max_total_open_option_risk_pct", 10.0))
        sector_limit = float(self.hard.get("max_sector_open_risk_pct", 4.0))
        portfolio_max = max_qty_under_portfolio_limits(
            equity=portfolio.equity,
            max_loss_per_spread=s.max_loss_per_spread,
            current_open_risk=portfolio.open_risk_dollars,
            total_limit_pct=total_limit,
            current_sector_risk=portfolio.sector_risk(request.sector),
            sector_limit_pct=sector_limit,
        )

        # Claude's recommendation caps sizing only when Claude asked for a
        # change (MODIFY). On PASS nothing changed and no CLAUDE_MODIFIED
        # shadow variant exists, so honouring the number anyway would let a
        # cap the attribution cannot see silently reshape the executed
        # size. (The previous ternary here had identical branches.)
        red_team_pct = (request.red_team_max_risk_pct
                        if request.red_team_verdict is Verdict.MODIFY
                        else None)

        sizing = size_position(
            equity=portfolio.equity,
            desired_risk_pct=request.desired_risk_pct,
            max_loss_per_spread=s.max_loss_per_spread,
            red_team_max_risk_pct=red_team_pct,
            hard_cap_pct=float(self.hard.get("max_risk_per_trade_pct", 2.0)),
            max_qty=portfolio_max,
        )
        # The walk's dollar ceiling: the binding cap, further bounded by the
        # room left under the portfolio and sector limits.
        room = portfolio_risk_room(
            portfolio.equity, portfolio.open_risk_dollars, total_limit,
            portfolio.sector_risk(request.sector), sector_limit)
        sizing.budget_dollars = round(min(sizing.budget_dollars, room), 2)

        if portfolio_max == 0:
            v.append(RiskViolation(
                rule_id="RISK_PORTFOLIO_FULL", severity=Severity.BLOCK,
                message="no room under the total or sector risk caps",
                observed_value=round(portfolio.open_risk_dollars, 2)))
        elif sizing.approved_qty < sizing.requested_qty:
            v.append(RiskViolation(
                rule_id="RISK_RESIZED", severity=Severity.WARN,
                message=f"quantity reduced {sizing.requested_qty} -> "
                        f"{sizing.approved_qty} by {sizing.binding_cap}",
                observed_value=sizing.requested_qty,
                allowed_value=sizing.approved_qty))

        if sizing.approved_qty < 1:
            v.append(RiskViolation(
                rule_id="RISK_QTY_ZERO", severity=Severity.BLOCK,
                message=f"risk budget affords no spreads at "
                        f"${s.max_loss_per_spread:.2f} each",
                observed_value=0, allowed_value=1))
        return sizing

    # ---- decision --------------------------------------------------

    @staticmethod
    def _decide(halted: bool, violations: list[RiskViolation],
                sizing: SizingResult) -> RiskDecision:
        if halted:
            return RiskDecision.HALT
        if any(x.severity is Severity.BLOCK for x in violations):
            return RiskDecision.REJECT
        if sizing.approved_qty < sizing.requested_qty and sizing.approved_qty >= 1:
            return RiskDecision.RESIZE
        return RiskDecision.APPROVE

    @staticmethod
    def _pct(dollars: float, equity: float) -> float:
        return round(max(0.0, dollars / equity * 100.0), 4) if equity > 0 else 0.0


def sector_of(symbol: str, sector_map: dict[str, list[str]],
              default: str = "UNKNOWN") -> str:
    """Dynamic symbols with no mapping share one bucket, so an unmapped
    cluster cannot evade the 4% sector cap by being unclassified."""
    sym = symbol.upper()
    for sector, members in sector_map.items():
        if sym in members:
            return sector
    return default
