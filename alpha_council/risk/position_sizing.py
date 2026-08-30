"""
Alpha Council v2.4 - position sizing.

Three independent caps, all applied (§16.3):

    requested   what the PM asked for
    hard_cap    2% of equity, never relaxed by any tier or any model
    red_team    what Claude recommended, when it recommended less

The minimum wins. Requested and approved quantities are both recorded
because the attribution decomposition needs them: the sizing effect is
literally (qty_b - qty_a) * pnl_per_spread_b, which is unrecoverable if
only the final quantity survives.

Place at: alpha_council/risk/position_sizing.py
"""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(slots=True)
class SizingResult:
    requested_qty: int
    approved_qty: int
    requested_risk_dollars: float
    approved_risk_dollars: float
    hard_cap_dollars: float
    red_team_cap_dollars: float | None
    binding_cap: str
    max_loss_per_spread: float

    @property
    def was_resized(self) -> bool:
        return self.approved_qty < self.requested_qty

    @property
    def is_viable(self) -> bool:
        return self.approved_qty >= 1


def size_position(equity: float, desired_risk_pct: float,
                  max_loss_per_spread: float,
                  red_team_max_risk_pct: float | None = None,
                  hard_cap_pct: float = 2.0,
                  max_qty: int | None = None) -> SizingResult:
    """Convert a desired risk percentage into a spread count.

    max_loss_per_spread must come from the LIMIT debit actually submitted,
    never the mid. Sizing off the mid understates risk by exactly the
    slippage the limit walk is designed to pay.
    """
    if equity <= 0:
        raise ValueError(f"equity must be positive, got {equity}")
    if max_loss_per_spread <= 0:
        raise ValueError(
            f"max_loss_per_spread must be positive, got {max_loss_per_spread}")

    requested_dollars = equity * max(0.0, desired_risk_pct) / 100.0
    hard_cap_dollars = equity * hard_cap_pct / 100.0
    red_team_dollars = (equity * red_team_max_risk_pct / 100.0
                        if red_team_max_risk_pct is not None else None)

    caps: list[tuple[str, float]] = [
        ("requested", requested_dollars),
        ("hard_cap_2pct", hard_cap_dollars),
    ]
    if red_team_dollars is not None:
        caps.append(("red_team", red_team_dollars))

    binding, budget = min(caps, key=lambda kv: kv[1])

    requested_qty = int(math.floor(requested_dollars / max_loss_per_spread))
    approved_qty = int(math.floor(budget / max_loss_per_spread))
    if max_qty is not None:
        if approved_qty > max_qty:
            approved_qty, binding = max_qty, "operator_max_qty"
        requested_qty = min(requested_qty, max(requested_qty, max_qty))

    approved_qty = max(0, min(approved_qty, requested_qty))

    return SizingResult(
        requested_qty=requested_qty,
        approved_qty=approved_qty,
        requested_risk_dollars=round(requested_dollars, 2),
        approved_risk_dollars=round(approved_qty * max_loss_per_spread, 2),
        hard_cap_dollars=round(hard_cap_dollars, 2),
        red_team_cap_dollars=(round(red_team_dollars, 2)
                              if red_team_dollars is not None else None),
        binding_cap=binding,
        max_loss_per_spread=max_loss_per_spread,
    )


def max_qty_under_portfolio_limits(equity: float, max_loss_per_spread: float,
                                   current_open_risk: float,
                                   total_limit_pct: float,
                                   current_sector_risk: float,
                                   sector_limit_pct: float) -> int:
    """Largest quantity that keeps both portfolio caps satisfied."""
    total_room = equity * total_limit_pct / 100.0 - current_open_risk
    sector_room = equity * sector_limit_pct / 100.0 - current_sector_risk
    room = min(total_room, sector_room)
    if room <= 0:
        return 0
    return int(math.floor(room / max_loss_per_spread))
