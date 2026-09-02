"""
Alpha Council v2.4 - AI budget manager.

Enforces the $50-per-provider caps and the per-session ceiling, and records
every call in api_usage with its decision_id.

The projection says spend lands near $6 / $2 against $50 caps, so the real
risk is not overspending — it is a runaway loop or a mispriced model
quietly burning the budget in an hour. The per-session ceiling exists for
that, not for the totals.

Place at: alpha_council/agents/budget.py
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from alpha_council.db.engine import Database
from alpha_council.utils.ids import new_uuid
from alpha_council.utils.time import iso_utc, to_et, utc_now


class BudgetMode(StrEnum):
    NORMAL = "NORMAL"
    RESERVE = "RESERVE"
    BLOCKED = "BLOCKED"


# Calls permitted once a provider enters reserve mode. The analysts are cut
# first because the PM decision and the Red Team objection are the two
# outputs the demo actually depends on.
RESERVE_ALLOWED = {
    "openai": {"portfolio_manager", "structure_selection", "pm_revision"},
    "anthropic": {"red_team"},
}


@dataclass(slots=True)
class UsageRecord:
    provider: str
    model: str
    purpose: str
    input_tokens: int
    output_tokens: int
    cached_tokens: int = 0
    cost_usd: float = 0.0
    decision_id: str | None = None
    request_id: str | None = None
    occurred_at: datetime = field(default_factory=utc_now)


@dataclass(slots=True)
class BudgetDecision:
    allowed: bool
    mode: BudgetMode
    reason: str
    spent_usd: float = 0.0
    remaining_usd: float = 0.0

    @property
    def gate_id(self) -> str:
        return "BUDGET_EXHAUSTED" if self.mode is BudgetMode.BLOCKED \
            else "BUDGET_RESERVE_MODE"


def compute_cost(model: str, input_tokens: int, output_tokens: int,
                 prices: dict[str, dict[str, float]],
                 cached_tokens: int = 0) -> float:
    """Reasoning and thinking tokens bill as output on all three models.

    An UNKNOWN model id is priced at the most expensive configured model
    rather than $0. A silently-free model would make every budget ceiling
    vacuous — the single failure mode this manager exists to prevent.
    The caller logs the mismatch; this function keeps enforcement alive.
    """
    p = prices.get(model)
    if not p:
        if not prices:
            return 0.0
        p = max(prices.values(),
                key=lambda x: x.get("input", 0.0) + x.get("output", 0.0))
    billable_input = max(0, input_tokens - cached_tokens)
    cached_cost = cached_tokens / 1e6 * p.get("input", 0.0) * 0.1
    return round(
        billable_input / 1e6 * p.get("input", 0.0)
        + output_tokens / 1e6 * p.get("output", 0.0)
        + cached_cost, 6)


def unpriced_models(config: dict[str, Any]) -> list[str]:
    """Model ids referenced in models.* with no model_prices entry.

    Run at startup: every name this returns would otherwise be billed at
    the worst-case fallback rate, which is safe but wrong — fix the config.
    """
    prices = config.get("model_prices", {})
    missing = []
    for purpose, spec in (config.get("models", {}) or {}).items():
        model = (spec or {}).get("model", "")
        if model and model not in prices:
            missing.append(f"{purpose}: {model}")
    return missing


class BudgetManager:
    def __init__(self, db: Database, config: dict[str, Any]):
        self.db = db
        self.config = config
        b = config.get("budget", {})
        self.prices: dict[str, dict[str, float]] = config.get("model_prices", {})
        self.session_ceiling = float(b.get("per_session_ceiling_usd", 0.75))
        self.daily_ceiling = float(b.get("daily_provider_ceiling_usd", 8.00))
        self.thresholds = {
            "openai": (float(b.get("openai_reserve_at_usd", 40.0)),
                       float(b.get("openai_block_at_usd", 48.0))),
            "anthropic": (float(b.get("anthropic_reserve_at_usd", 40.0)),
                          float(b.get("anthropic_block_at_usd", 48.0))),
        }
        self._session_spend: dict[str, float] = {}
        self._totals: dict[str, float] = {}
        self._daily: dict[str, float] = {}
        self.loaded = False

    # ---- state -----------------------------------------------------

    async def load(self) -> dict[str, float]:
        """Read committed spend from api_usage. Survives a restart."""
        rows = await self.db.fetchall(
            "SELECT provider, SUM(cost_usd) AS total FROM api_usage "
            "GROUP BY provider")
        self._totals = {r["provider"]: float(r["total"] or 0.0) for r in rows}

        today = str(to_et(utc_now()).date())
        rows = await self.db.fetchall(
            "SELECT provider, SUM(cost_usd) AS total FROM api_usage "
            "WHERE substr(occurred_at,1,10) >= ? GROUP BY provider", (today,))
        self._daily = {r["provider"]: float(r["total"] or 0.0) for r in rows}
        self.loaded = True
        return dict(self._totals)

    def total_spent(self, provider: str) -> float:
        return self._totals.get(provider, 0.0)

    def daily_spent(self, provider: str) -> float:
        return self._daily.get(provider, 0.0)

    def session_spent(self, session_id: str) -> float:
        return self._session_spend.get(session_id, 0.0)

    def mode(self, provider: str) -> BudgetMode:
        reserve_at, block_at = self.thresholds.get(provider, (40.0, 48.0))
        spent = self.total_spent(provider)
        if spent >= block_at:
            return BudgetMode.BLOCKED
        if spent >= reserve_at:
            return BudgetMode.RESERVE
        return BudgetMode.NORMAL

    def start_session(self, session_id: str) -> None:
        self._session_spend[session_id] = 0.0

    # ---- authorization ---------------------------------------------

    def allow_call(self, provider: str, purpose: str,
                   session_id: str | None = None,
                   estimated_cost: float = 0.0) -> BudgetDecision:
        _, block_at = self.thresholds.get(provider, (40.0, 48.0))
        spent = self.total_spent(provider)
        mode = self.mode(provider)
        remaining = max(0.0, block_at - spent)

        if mode is BudgetMode.BLOCKED:
            return BudgetDecision(
                False, mode,
                f"{provider} spend ${spent:.2f} at or beyond the "
                f"${block_at:.2f} block threshold",
                spent, remaining)

        if mode is BudgetMode.RESERVE:
            allowed = RESERVE_ALLOWED.get(provider, set())
            if purpose not in allowed:
                return BudgetDecision(
                    False, mode,
                    f"{provider} in reserve mode; only {sorted(allowed)} "
                    f"permitted, got {purpose!r}",
                    spent, remaining)

        daily = self.daily_spent(provider)
        if daily + estimated_cost > self.daily_ceiling:
            return BudgetDecision(
                False, BudgetMode.BLOCKED,
                f"{provider} daily spend ${daily:.2f} would exceed the "
                f"${self.daily_ceiling:.2f} ceiling",
                spent, remaining)

        if session_id is not None:
            session = self.session_spent(session_id)
            if session + estimated_cost > self.session_ceiling:
                return BudgetDecision(
                    False, BudgetMode.BLOCKED,
                    f"session spend ${session:.4f} would exceed the "
                    f"${self.session_ceiling:.2f} ceiling",
                    spent, remaining)

        return BudgetDecision(True, mode, "within budget", spent, remaining)

    # ---- recording --------------------------------------------------

    async def record(self, usage: UsageRecord, endpoint: str = "",
                     session_id: str | None = None) -> float:
        if usage.cost_usd <= 0:
            if usage.model not in self.prices and self.prices:
                await self.db.log_event(
                    "ERROR", "budget", "BUDGET_UNPRICED_MODEL",
                    f"{usage.model!r} has no model_prices entry; billing at "
                    "the most expensive configured rate",
                    {"model": usage.model, "purpose": usage.purpose})
            usage.cost_usd = compute_cost(
                usage.model, usage.input_tokens, usage.output_tokens,
                self.prices, usage.cached_tokens)

        await self.db.execute(
            "INSERT INTO api_usage(usage_id, provider, model, endpoint, "
            "occurred_at, decision_id, request_id, input_tokens, "
            "output_tokens, cached_tokens, cost_usd) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (new_uuid(), usage.provider, usage.model,
             endpoint or usage.purpose, iso_utc(usage.occurred_at),
             usage.decision_id, usage.request_id, usage.input_tokens,
             usage.output_tokens, usage.cached_tokens, usage.cost_usd))

        self._totals[usage.provider] = (
            self._totals.get(usage.provider, 0.0) + usage.cost_usd)
        self._daily[usage.provider] = (
            self._daily.get(usage.provider, 0.0) + usage.cost_usd)
        if session_id is not None:
            self._session_spend[session_id] = (
                self._session_spend.get(session_id, 0.0) + usage.cost_usd)

        mode = self.mode(usage.provider)
        if mode is not BudgetMode.NORMAL:
            await self.db.log_event(
                "WARN", "budget", f"BUDGET_{mode}",
                f"{usage.provider} entered {mode} at "
                f"${self._totals[usage.provider]:.2f}",
                {"provider": usage.provider,
                 "spent": self._totals[usage.provider]})
        return usage.cost_usd

    # ---- reporting --------------------------------------------------

    def summary(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for provider in ("openai", "anthropic"):
            reserve_at, block_at = self.thresholds.get(provider, (40.0, 48.0))
            spent = self.total_spent(provider)
            out[provider] = {
                "spent": round(spent, 4),
                "daily": round(self.daily_spent(provider), 4),
                "mode": str(self.mode(provider)),
                "reserve_at": reserve_at,
                "block_at": block_at,
                "remaining": round(max(0.0, block_at - spent), 4),
            }
        return out

    async def by_purpose(self) -> list[dict[str, Any]]:
        return await self.db.fetchall(
            "SELECT provider, model, endpoint AS purpose, COUNT(*) AS calls, "
            "SUM(input_tokens) AS in_tok, SUM(output_tokens) AS out_tok, "
            "ROUND(SUM(cost_usd), 4) AS cost FROM api_usage "
            "GROUP BY provider, model, endpoint ORDER BY cost DESC")
