"""
Alpha Council v2.5 §10.3/§13 - challenger change validation.

THE safety boundary of Alpha Evolution. Every proposed ParameterChange
passes through here BEFORE a challenger can be stored, and a single
violation rejects the whole proposal. The rules are constitutional, not
stylistic:

  IMMUTABLE PATHS ARE IMMUTABLE. Risk limits, the paper lock, strategy
  bans, liquidity floors, idempotency, execution safety — a challenger
  naming any of them is rejected outright, whatever the rationale says.

  CHANGES ARE SMALL. Numeric moves are bounded (default ±10% per
  version), at most three changes per challenger, and the model's claim
  about the champion's current value must MATCH the actual configuration.
  A model that misstates the baseline is proposing against a strategy
  that does not exist.

  PROMPT_EMPHASIS IS ADVISORY. It names no config path, changes no number,
  and is carried for the operator to read - never auto-applied.

Deterministic, synchronous, no I/O. If it is not rejected here, it is a
storable shadow hypothesis; nothing here ever touches the live config.

Place at: alpha_council/evolution/change_validator.py
"""

from __future__ import annotations

from typing import Any, Sequence

from alpha_council.models.evolution import (
    ChallengerProposal,
    ChangeCategory,
    ParameterChange,
)

# Paths (and path prefixes) no challenger may name. Matched against the
# start of the dotted parameter_path, so "hard.max_risk_per_trade_pct"
# and everything under "hard." is caught by one entry.
DEFAULT_IMMUTABLE_PREFIXES: tuple[str, ...] = (
    "hard.",                    # the entire Risk Constitution hard block
    "paper_only",
    "risk.",                    # spec-styled aliases
    "execution.",
    "liquidity.",
    "liquidity_floor.",         # the tier ladder's absolute floor
    "budget.",                  # AI spend ceilings are operator-owned
    "rate_limits.",
    "activity_target.",
)

# Whitelisted mutable prefixes per §10.2. A path matching neither list is
# rejected: unknown territory is not a place for an optimizer.
DEFAULT_MUTABLE_PREFIXES: tuple[str, ...] = (
    "tiers.",                   # quality floors/targets (per-tier)
    "pre_score_weights_",
    "opportunity_weights_",
    "fast_score_weights.",
    "structure_weights.",
    "leg_liquidity_weights.",
    "tracks.",
    "discovery.",
    "catalyst_weights.",
)

# Tier keys that look mutable by prefix but are hard-liquidity in nature:
# the ladder may not go below the floor, and a challenger may not move the
# per-tier liquidity values at all (§10.3 "minimum hard liquidity floor").
TIER_IMMUTABLE_LEAVES = ("min_open_interest", "min_volume",
                         "max_leg_spread_pct", "dte")


def resolve_path(config: dict[str, Any], dotted: str) -> Any:
    """Value at a dotted path; int-like segments try dict[int] too."""
    node: Any = config
    for part in dotted.split("."):
        if not isinstance(node, dict):
            return None
        if part in node:
            node = node[part]
            continue
        try:
            key = int(part)
        except ValueError:
            return None
        if key in node:
            node = node[key]
        else:
            return None
    return node


def validate_changes(proposal: ChallengerProposal,
                     champion_config: dict[str, Any],
                     evolution_cfg: dict[str, Any] | None = None
                     ) -> list[str]:
    """Every reason the proposal is not storable. Empty list = storable."""
    cfg = evolution_cfg or {}
    problems: list[str] = []

    immutable = tuple(cfg.get("immutable_paths",
                              DEFAULT_IMMUTABLE_PREFIXES))
    max_changes = int(cfg.get("max_changes_per_challenger", 3))
    max_pct = float(cfg.get("hysteresis", {}).get(
        "max_threshold_change_per_version_pct", 10.0))

    if len(proposal.changes) > max_changes:
        problems.append(
            f"{len(proposal.changes)} changes exceed the maximum "
            f"{max_changes}")

    for change in proposal.changes:
        problems.extend(_validate_one(change, champion_config, immutable,
                                      max_pct))
    return problems


def _validate_one(change: ParameterChange, champion_config: dict[str, Any],
                  immutable: Sequence[str], max_pct: float) -> list[str]:
    path = change.parameter_path.strip()
    problems: list[str] = []

    # 1. constitutional paths are untouchable, whatever the category says
    for prefix in immutable:
        if path == prefix.rstrip(".") or path.startswith(prefix):
            problems.append(f"{path}: immutable constitutional path")
            return problems

    # 2. tier liquidity leaves are hard-floor territory
    if path.startswith("tiers."):
        leaf = path.rsplit(".", 1)[-1]
        if leaf in TIER_IMMUTABLE_LEAVES:
            problems.append(
                f"{path}: per-tier liquidity values are part of the hard "
                "floor and may not be proposed")
            return problems

    # 3. PROMPT_EMPHASIS is advisory prose, not a config edit
    if change.category is ChangeCategory.PROMPT_EMPHASIS:
        return problems

    # 4. the path must exist and be whitelisted
    if not any(path.startswith(p) for p in DEFAULT_MUTABLE_PREFIXES):
        problems.append(f"{path}: not a whitelisted mutable parameter")
        return problems
    current = resolve_path(champion_config, path)
    if current is None:
        problems.append(f"{path}: no such parameter in the champion config")
        return problems

    # 5. the model's baseline claim must match reality
    if isinstance(current, (int, float)) and isinstance(
            change.champion_value, (int, float)):
        if abs(float(current) - float(change.champion_value)) > 1e-9:
            problems.append(
                f"{path}: claimed champion value {change.champion_value} "
                f"but the config holds {current}")
    elif str(current) != str(change.champion_value):
        problems.append(
            f"{path}: claimed champion value {change.champion_value!r} "
            f"but the config holds {current!r}")

    # 6. numeric moves stay inside the hysteresis bound
    if isinstance(current, (int, float)) and isinstance(
            change.challenger_value, (int, float)):
        base = abs(float(current))
        if base > 1e-9:
            move_pct = abs(float(change.challenger_value) - float(current)) \
                / base * 100.0
            if move_pct > max_pct + 1e-9:
                problems.append(
                    f"{path}: {move_pct:.1f}% move exceeds the "
                    f"{max_pct:.0f}% per-version bound")
    elif not isinstance(change.challenger_value, type(current)):
        problems.append(
            f"{path}: challenger value type {type(change.challenger_value).__name__} "
            f"does not match config type {type(current).__name__}")

    return problems


def apply_changes(base_config: dict[str, Any],
                  changes: Sequence[ParameterChange]) -> dict[str, Any]:
    """Deep-copied config with the challenger's values applied.

    Only call on changes that already passed validate_changes; this
    function trusts its input and exists for the shadow runner.
    PROMPT_EMPHASIS entries are skipped (advisory).
    """
    import copy

    out = copy.deepcopy(base_config)
    for change in changes:
        if change.category is ChangeCategory.PROMPT_EMPHASIS:
            continue
        parts = change.parameter_path.split(".")
        node: Any = out
        for part in parts[:-1]:
            if isinstance(node, dict) and part in node:
                node = node[part]
                continue
            try:
                key = int(part)
            except ValueError:
                node = None
                break
            node = node.get(key) if isinstance(node, dict) else None
            if node is None:
                break
        if not isinstance(node, dict):
            continue
        leaf = parts[-1]
        if leaf in node:
            node[leaf] = change.challenger_value
        else:
            try:
                node[int(leaf)] = change.challenger_value
            except (ValueError, KeyError):
                continue
    return out
