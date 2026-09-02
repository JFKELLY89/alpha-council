"""
Alpha Council v2.5 - shadow book and counterfactual attribution.

The differentiator. Three variants of every decision are marked side by
side on the same schedule with the same method, and the P&L difference is
decomposed into selection and sizing effects:

    selection_effect(A->B) = (pnl_per_spread_B - pnl_per_spread_A) * qty_A
    sizing_effect(A->B)    = (qty_B - qty_A) * pnl_per_spread_B
    total_effect(A->B)     = selection + sizing = pnl_B - pnl_A

That decomposition answers the question a single number cannot: did the
Red Team pick a worse trade, or just a smaller one?

Two rules make the arithmetic mean anything:

  SAME METHOD, SAME MOMENT. Every variant is marked with one mark_method
  at one timestamp. Marking the executed variant at the bid and a shadow
  at the mid would manufacture an edge out of nothing.

  MARKS COME FROM DATA. A MarkSource returns a real spread mark. No model
  ever produces one.

Place at: alpha_council/journal/shadow_book.py
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol, Sequence

from alpha_council.db.engine import Database
from alpha_council.models.enums import MarkMethod, ShadowVariant
from alpha_council.models.execution import AttributionSnapshot
from alpha_council.models.trading import OptionStructure
from alpha_council.utils.ids import new_uuid, shadow_id
from alpha_council.utils.time import iso_utc, utc_now


class MarkSource(Protocol):
    """Supplies a current spread mark. Implemented against live quotes in
    production and stubbed in tests; never implemented by a model."""

    async def spread_mark(self, structure: OptionStructure,
                          method: MarkMethod) -> float | None: ...


@dataclass(slots=True)
class VariantState:
    variant: ShadowVariant
    structure: OptionStructure
    qty: int
    entry_debit: float
    entry_timestamp: datetime
    last_mark: float | None = None
    last_marked_at: datetime | None = None
    closed: bool = False
    final_mark: float | None = None

    def pnl_per_spread(self, mark: float | None = None) -> float:
        """Per-spread P&L in dollars. A debit spread gains as its value rises."""
        value = mark if mark is not None else (self.final_mark or self.last_mark)
        if value is None:
            return 0.0
        return round((value - self.entry_debit) * 100.0, 2)

    def total_pnl(self, mark: float | None = None) -> float:
        return round(self.pnl_per_spread(mark) * self.qty, 2)


@dataclass(slots=True)
class AttributionResult:
    decision_id: str
    as_of: datetime
    snapshot: AttributionSnapshot
    narrative: str = ""

    @property
    def claude_helped(self) -> bool:
        return self.snapshot.claude_value_added > 0

    @property
    def risk_helped(self) -> bool:
        return self.snapshot.risk_constitution_value_added > 0


class ShadowBook:
    """Creates, marks, and attributes counterfactual variants."""

    def __init__(self, db: Database, marks: MarkSource,
                 method: MarkMethod = MarkMethod.ADJUSTED_MID):
        self.db = db
        self.marks = marks
        self.method = method
        self._variants: dict[str, dict[ShadowVariant, VariantState]] = {}

    # ---- creation ---------------------------------------------------

    async def create(self, decision_id: str, variant: ShadowVariant,
                     structure: OptionStructure, qty: int,
                     entry_debit: float | None = None,
                     entry_timestamp: datetime | None = None,
                     close_policy: dict[str, Any] | None = None
                     ) -> VariantState:
        """Register a variant.

        A VETO produces CLAUDE_MODIFIED with qty 0. That is deliberate: the
        value of a trade avoided is measurable against the GPT original, and
        dropping the variant would silently discard the Red Team's best
        outcome.
        """
        state = VariantState(
            variant=variant, structure=structure, qty=max(0, qty),
            entry_debit=entry_debit if entry_debit is not None
            else structure.initial_limit_debit,
            entry_timestamp=entry_timestamp or utc_now())

        self._variants.setdefault(decision_id, {})[variant] = state

        await self.db.execute(
            "INSERT OR REPLACE INTO shadow_trades(shadow_id, decision_id, "
            "variant, structure_json, qty, entry_timestamp, "
            "entry_reference_debit, close_policy_json, status) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (shadow_id(decision_id, str(variant)), decision_id, str(variant),
             structure.model_dump_json()[:20000], state.qty,
             iso_utc(state.entry_timestamp), state.entry_debit,
             json.dumps(close_policy or {}),
             "FLAT" if state.qty == 0 else "OPEN"))
        return state

    def variants(self, decision_id: str) -> dict[ShadowVariant, VariantState]:
        return self._variants.get(decision_id, {})

    async def restore(self) -> int:
        """Rebuild in-memory variants from shadow_trades after a restart.

        The marking loop reads _variants; without this, every decision from
        a previous process marked nothing, silently, forever — the shadow
        rows sat OPEN in the database while attribution stopped moving.
        """
        rows = await self.db.fetchall(
            "SELECT * FROM shadow_trades WHERE status IN ('OPEN','FLAT')")
        restored = 0
        for row in rows:
            try:
                structure = OptionStructure.model_validate_json(
                    row["structure_json"])
            except Exception:  # noqa: BLE001 - one bad row must not stop it
                await self.db.log_event(
                    "ERROR", "shadow_book", "SHADOW_RESTORE_FAILED",
                    f"could not rebuild {row['shadow_id']}",
                    decision_id=row["decision_id"])
                continue
            from alpha_council.utils.time import parse_alpaca_ts

            state = VariantState(
                variant=ShadowVariant(row["variant"]),
                structure=structure,
                qty=int(row["qty"] or 0),
                entry_debit=float(row["entry_reference_debit"] or 0.0),
                entry_timestamp=parse_alpaca_ts(row["entry_timestamp"])
                or utc_now())
            self._variants.setdefault(
                row["decision_id"], {})[state.variant] = state
            restored += 1
        return restored

    # ---- marking ----------------------------------------------------

    async def mark_all(self, decision_id: str,
                       at: datetime | None = None) -> dict[ShadowVariant, float]:
        """Mark every variant at one timestamp with one method.

        Marking variants at different moments or by different methods would
        manufacture a difference that has nothing to do with the decisions
        being compared.
        """
        at = at or utc_now()
        states = self._variants.get(decision_id, {})
        out: dict[ShadowVariant, float] = {}

        for variant, state in states.items():
            if state.closed:
                continue
            mark = await self.marks.spread_mark(state.structure, self.method)
            if mark is None:
                continue
            state.last_mark = mark
            state.last_marked_at = at
            out[variant] = mark

            await self.db.execute(
                "INSERT INTO shadow_marks(shadow_mark_id, shadow_id, "
                "marked_at, mark_debit, unrealized_pnl, mark_method, "
                "quote_lag_seconds, source, raw_json) "
                "VALUES(?,?,?,?,?,?,?,?,'{}')",
                (new_uuid(), shadow_id(decision_id, str(variant)),
                 iso_utc(at), mark, state.total_pnl(mark), str(self.method),
                 state.structure.max_quote_lag_seconds, "shadow_book"))
        return out

    async def close_variant(self, decision_id: str, variant: ShadowVariant,
                            final_mark: float,
                            at: datetime | None = None) -> None:
        state = self._variants.get(decision_id, {}).get(variant)
        if state is None:
            return
        state.final_mark = final_mark
        state.closed = True
        state.last_marked_at = at or utc_now()
        await self.db.execute(
            "UPDATE shadow_trades SET status='CLOSED' "
            "WHERE decision_id=? AND variant=?", (decision_id, str(variant)))

    async def close_all(self, decision_id: str,
                        at: datetime | None = None) -> None:
        marks = await self.mark_all(decision_id, at)
        for variant, mark in marks.items():
            await self.close_variant(decision_id, variant, mark, at)

    async def close_decision(self, decision_id: str,
                             executed_exit_debit: float | None,
                             at: datetime | None = None
                             ) -> AttributionResult | None:
        """Freeze every variant when the real position closes.

        The EXECUTED variant is frozen at the ACTUAL exit value; the
        counterfactual variants at the market mark of the same moment, by
        the same method. Without this the executed variant kept marking off
        market data after the position was realized, and the attribution
        drifted away from the P&L the journal recorded.
        """
        at = at or utc_now()
        marks = await self.mark_all(decision_id, at)
        states = self._variants.get(decision_id, {})
        for variant, state in states.items():
            if state.closed:
                continue
            if (variant is ShadowVariant.EXECUTED
                    and executed_exit_debit is not None):
                final = executed_exit_debit
            else:
                final = marks.get(variant, state.last_mark)
            if final is None:
                # No mark obtainable this cycle; leave it open so the next
                # marking pass can close it rather than freezing a zero.
                continue
            await self.close_variant(decision_id, variant, final, at)

        result = self.compute(decision_id, at)
        if result is not None:
            await self.persist(result)
        return result

    # ---- attribution -------------------------------------------------

    def compute(self, decision_id: str,
                as_of: datetime | None = None) -> AttributionResult | None:
        """Four-way decomposition. Requires all three variants."""
        states = self._variants.get(decision_id, {})
        gpt = states.get(ShadowVariant.GPT_ORIGINAL)
        claude = states.get(ShadowVariant.CLAUDE_MODIFIED)
        executed = states.get(ShadowVariant.EXECUTED)
        if gpt is None:
            return None

        # No MODIFY means Claude's variant is the original, unchanged.
        claude = claude or gpt
        # No fill means the executed variant is flat — a sizing-to-zero of
        # the structure the risk engine actually evaluated, which is
        # Claude's. Basing it on GPT's structure would manufacture a risk
        # "selection effect" out of a trade that never happened.
        if executed is None:
            executed = VariantState(
                variant=ShadowVariant.EXECUTED, structure=claude.structure,
                qty=0, entry_debit=claude.entry_debit,
                entry_timestamp=claude.entry_timestamp,
                last_mark=claude.last_mark, final_mark=claude.final_mark)

        # pnl_per_spread is a property of the STRUCTURE, independent of
        # quantity. Forcing it to zero for qty-0 variants (as this used to)
        # relabelled a VETO — a sizing-to-zero of the same structure — as a
        # selection effect, contradicting the decomposition the spec
        # defines: sizing_effect = (qty_B - qty_A) * pnl_per_spread_B.
        gpt_ps = gpt.pnl_per_spread()
        claude_ps = claude.pnl_per_spread()
        exec_ps = executed.pnl_per_spread()

        c_sel, c_siz = AttributionSnapshot.decompose(
            gpt_ps, gpt.qty, claude_ps, claude.qty)
        r_sel, r_siz = AttributionSnapshot.decompose(
            claude_ps, claude.qty, exec_ps, executed.qty)

        snapshot = AttributionSnapshot(
            decision_id=decision_id, as_of=as_of or utc_now(),
            mark_method=self.method,
            gpt_original_pnl=round(gpt_ps * gpt.qty, 2),
            claude_modified_pnl=round(claude_ps * claude.qty, 2),
            executed_pnl=round(exec_ps * executed.qty, 2),
            gpt_original_pnl_per_spread=gpt_ps,
            claude_modified_pnl_per_spread=claude_ps,
            executed_pnl_per_spread=exec_ps,
            gpt_original_qty=gpt.qty, claude_modified_qty=claude.qty,
            executed_qty=executed.qty,
            claude_selection_effect=round(c_sel, 2),
            claude_sizing_effect=round(c_siz, 2),
            risk_selection_effect=round(r_sel, 2),
            risk_sizing_effect=round(r_siz, 2),
            claude_value_added=round(c_sel + c_siz, 2),
            risk_constitution_value_added=round(r_sel + r_siz, 2),
        )
        return AttributionResult(decision_id=decision_id,
                                 as_of=snapshot.as_of, snapshot=snapshot,
                                 narrative=describe(snapshot))

    async def persist(self, result: AttributionResult) -> None:
        s = result.snapshot
        await self.db.execute(
            "INSERT OR REPLACE INTO decision_attribution(attribution_id, "
            "decision_id, as_of, gpt_original_pnl, claude_modified_pnl, "
            "executed_pnl, gpt_original_pnl_per_spread, "
            "claude_modified_pnl_per_spread, executed_pnl_per_spread, "
            "gpt_original_qty, claude_modified_qty, executed_qty, "
            "claude_selection_effect, claude_sizing_effect, "
            "risk_selection_effect, risk_sizing_effect, claude_value_added, "
            "risk_constitution_value_added, mark_method, notes_json) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (f"attr_{s.decision_id[-8:]}", s.decision_id, iso_utc(s.as_of),
             s.gpt_original_pnl, s.claude_modified_pnl, s.executed_pnl,
             s.gpt_original_pnl_per_spread, s.claude_modified_pnl_per_spread,
             s.executed_pnl_per_spread, s.gpt_original_qty,
             s.claude_modified_qty, s.executed_qty,
             s.claude_selection_effect, s.claude_sizing_effect,
             s.risk_selection_effect, s.risk_sizing_effect,
             s.claude_value_added, s.risk_constitution_value_added,
             str(s.mark_method),
             json.dumps({"narrative": result.narrative})))

    async def portfolio_attribution(self) -> dict[str, Any]:
        row = await self.db.fetchone(
            "SELECT COUNT(*) AS n, "
            "ROUND(SUM(claude_value_added), 2) AS claude_total, "
            "ROUND(SUM(claude_selection_effect), 2) AS claude_selection, "
            "ROUND(SUM(claude_sizing_effect), 2) AS claude_sizing, "
            "ROUND(SUM(risk_constitution_value_added), 2) AS risk_total, "
            "ROUND(SUM(risk_selection_effect), 2) AS risk_selection, "
            "ROUND(SUM(risk_sizing_effect), 2) AS risk_sizing, "
            "ROUND(SUM(executed_pnl - gpt_original_pnl), 2) AS governance "
            "FROM decision_attribution")
        return dict(row or {})


def describe(s: AttributionSnapshot) -> str:
    """Plain-language attribution. Never suppresses a negative result.

    'Our red team cost us $180 in selection but saved $410 in sizing' is a
    more credible finding than a uniformly positive one.
    """
    parts: list[str] = []

    if s.claude_modified_qty == 0 and s.gpt_original_qty > 0:
        avoided = -s.gpt_original_pnl
        verb = "avoided a loss of" if avoided > 0 else "gave up a gain of"
        parts.append(f"Red Team VETO {verb} ${abs(avoided):,.2f}.")
    else:
        if abs(s.claude_selection_effect) >= 0.01:
            verb = "improved" if s.claude_selection_effect > 0 else "cost"
            parts.append(f"Red Team structure change {verb} "
                         f"${abs(s.claude_selection_effect):,.2f}.")
        if abs(s.claude_sizing_effect) >= 0.01:
            verb = "added" if s.claude_sizing_effect > 0 else "cost"
            parts.append(f"Red Team sizing change {verb} "
                         f"${abs(s.claude_sizing_effect):,.2f}.")
        if not parts:
            parts.append("Red Team made no measurable change.")

    if abs(s.risk_sizing_effect) >= 0.01:
        verb = "saved" if s.risk_sizing_effect > 0 else "cost"
        parts.append(f"Risk Constitution sizing {verb} "
                     f"${abs(s.risk_sizing_effect):,.2f}.")
    if abs(s.risk_selection_effect) >= 0.01:
        verb = "added" if s.risk_selection_effect > 0 else "cost"
        parts.append(f"Risk Constitution structure change {verb} "
                     f"${abs(s.risk_selection_effect):,.2f}.")

    total = s.total_governance_value_added
    verb = "added" if total > 0 else "cost"
    parts.append(f"Governance overall {verb} ${abs(total):,.2f}.")
    return " ".join(parts)


class RejectedShadowBook:
    """Marks candidates that a gate blocked, so GateValue is measurable."""

    def __init__(self, db: Database, marks: MarkSource,
                 method: MarkMethod = MarkMethod.ADJUSTED_MID):
        self.db = db
        self.marks = marks
        self.method = method

    async def create(self, rejection_id_value: str, symbol: str,
                     structure: OptionStructure, horizon_end: datetime,
                     entry_timestamp: datetime | None = None) -> str:
        shadow = f"rsh_{rejection_id_value[-10:]}"
        await self.db.execute(
            "INSERT OR REPLACE INTO rejected_shadows(rejected_shadow_id, "
            "rejection_id, symbol, structure_json, entry_timestamp, "
            "entry_reference_debit, horizon_end, status, mark_method) "
            "VALUES(?,?,?,?,?,?,?,'OPEN',?)",
            (shadow, rejection_id_value, symbol,
             structure.model_dump_json()[:20000],
             iso_utc(entry_timestamp or utc_now()),
             structure.initial_limit_debit, iso_utc(horizon_end),
             str(self.method)))
        return shadow

    async def mark_open(self, now: datetime | None = None) -> int:
        """Mark every open rejected shadow; close those past their horizon."""
        now = now or utc_now()
        rows = await self.db.fetchall(
            "SELECT * FROM rejected_shadows WHERE status='OPEN'")
        marked = 0

        for row in rows:
            try:
                structure = OptionStructure.model_validate_json(
                    row["structure_json"])
            except Exception:  # noqa: BLE001 - a bad row must not stop the loop
                continue

            mark = await self.marks.spread_mark(structure, self.method)
            if mark is None:
                continue

            entry = float(row["entry_reference_debit"])
            pnl_per_spread = round((mark - entry) * 100.0, 2)
            expired = iso_utc(now) >= row["horizon_end"]

            await self.db.execute(
                "UPDATE rejected_shadows SET last_mark_debit=?, "
                "last_marked_at=?, status=?, final_pnl_per_spread=? "
                "WHERE rejected_shadow_id=?",
                (mark, iso_utc(now), "CLOSED" if expired else "OPEN",
                 pnl_per_spread if expired else None,
                 row["rejected_shadow_id"]))
            marked += 1
        return marked
