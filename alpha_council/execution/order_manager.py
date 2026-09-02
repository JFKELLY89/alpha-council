"""
Alpha Council v2.4 - order construction, submission, and the limit walk.

Two properties matter more than anything else here.

IDEMPOTENCY. Every submission carries a unique client_order_id. After any
timeout or ambiguous failure the manager queries Alpaca by that ID before
even considering a retry. A duplicate spread is worse than a missed one:
it doubles risk silently and corrupts the attribution ledger.

CONSERVATIVE PRICING. Indicative quotes are derived, not OPRA NBBO, so the
walk starts near the adjusted mid and steps toward natural in three moves,
never past it and never past the Risk Constitution's dollar ceiling. Every
submission writes an execution-calibration record so §17.5 can measure what
the indicative reference actually cost against real fills.

Place at: alpha_council/execution/order_manager.py
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import httpx

from alpha_council.alpaca.rest_client import AlpacaError, AlpacaRestClient
from alpha_council.db.engine import Database
from alpha_council.models.calibration import ExecutionCalibration
from alpha_council.models.enums import CandidateTrack, Direction, OrderSide
from alpha_council.models.execution import ExecutionIntent, OrderReceipt
from alpha_council.models.trading import OptionLeg, OptionStructure
from alpha_council.utils.ids import calibration_id, client_order_id, new_uuid
from alpha_council.utils.math import safe_div
from alpha_council.utils.time import age_seconds, iso_utc, parse_alpaca_ts, utc_now

TERMINAL_STATUSES = {"filled", "canceled", "expired", "rejected", "done_for_day"}
WORKING_STATUSES = {"new", "accepted", "pending_new", "partially_filled",
                    "accepted_for_bidding", "held"}


@dataclass(slots=True)
class WalkStep:
    attempt: int
    limit_debit: float
    submitted_at: datetime
    status: str = "pending"
    order_id: str | None = None
    client_order_id: str | None = None
    filled_at: datetime | None = None
    fill_price: float | None = None


@dataclass(slots=True)
class ExecutionOutcome:
    decision_id: str
    filled: bool
    qty: int
    fill_debit: float | None = None
    filled_at: datetime | None = None
    order_id: str | None = None
    client_order_id: str | None = None
    steps: list[WalkStep] = field(default_factory=list)
    final_status: str = "NO_FILL"
    adopted_existing: bool = False
    error: str | None = None

    @property
    def walk_steps(self) -> int:
        return len(self.steps)

    @property
    def seconds_to_fill(self) -> float | None:
        if not (self.filled and self.steps and self.filled_at):
            return None
        return (self.filled_at - self.steps[0].submitted_at).total_seconds()


def build_intent(structure: OptionStructure, decision_id: str, qty: int,
                 limit_debit: float, revision: int = 0,
                 attempt: int = 1, closing: bool = False) -> ExecutionIntent:
    """Turn an approved structure into a submittable multi-leg intent.

    For a closing order, `limit_debit` is the net CREDIT demanded; the
    payload carries it with a negative sign per the mleg convention.
    """
    legs: list[OptionLeg] = []
    for leg in structure.legs:
        if closing:
            side = "SELL" if leg.side == "BUY" else "BUY"
            intent = "sell_to_close" if leg.side == "BUY" else "buy_to_close"
        else:
            side, intent = leg.side, leg.position_intent
        legs.append(leg.model_copy(update={"side": side,
                                           "position_intent": intent}))

    return ExecutionIntent(
        decision_id=decision_id,
        client_order_id=client_order_id(decision_id, revision),
        structure_id=structure.structure_id,
        qty=qty,
        limit_debit=round(limit_debit, 2),
        limit_is_credit=closing,
        attempt=attempt,
        legs=legs,
    )


def walk_prices(adjusted_mid: float, natural: float, buffer: float,
                max_allowed: float) -> list[float]:
    """Three prices, monotonically increasing, never past natural or the cap.

    §17.3. Wider first step than a live-quote system would use, because the
    reference price itself is an estimate.
    """
    ladder = [
        adjusted_mid + 0.25 * (natural - adjusted_mid) + buffer,
        adjusted_mid + 0.60 * (natural - adjusted_mid) + buffer,
        natural,
    ]
    ceiling = min(natural, max_allowed)
    out: list[float] = []
    for price in ladder:
        p = round(min(price, ceiling), 2)
        if p > 0 and (not out or p > out[-1]):
            out.append(p)
    return out or [round(ceiling, 2)]


def close_walk_prices(adjusted_mid: float, conservative: float,
                      buffer: float, tick: float = 0.01) -> list[float]:
    """Closing credits: three CREDIT targets, monotonically DEcreasing.

    Mirror of walk_prices. Start near the adjusted-mid credit, concede
    toward the conservative exit (sell the long at bid, buy the short back
    at ask), never demand less than the greater of that floor and one tick.
    A closing order priced as an opening debit is a marketable order
    straight through the market and can fill at any credit at all.
    """
    floor = max(round(conservative, 2), tick)
    mid = max(adjusted_mid, floor)
    ladder = [
        mid - 0.25 * (mid - floor) - buffer,
        mid - 0.60 * (mid - floor) - buffer,
        floor,
    ]
    out: list[float] = []
    for price in ladder:
        p = round(max(price, floor), 2)
        if p >= tick and (not out or p < out[-1]):
            out.append(p)
    return out or [floor]


class OrderManager:
    """Submits and manages multi-leg paper orders."""

    def __init__(self, api: AlpacaRestClient, db: Database,
                 wait_seconds: float = 30.0, poll_seconds: float = 3.0):
        self.api = api
        self.db = db
        self.wait_seconds = wait_seconds
        self.poll_seconds = poll_seconds

    # ---- transport --------------------------------------------------

    async def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        await self.api.bucket.acquire()
        self.api.request_count += 1
        r = await self.api.client.post(f"{self.api.trade_base}{path}",
                                       json=payload)
        if r.status_code >= 400:
            raise AlpacaError(r.status_code, path, r.text)
        return r.json()

    async def _delete(self, path: str) -> bool:
        await self.api.bucket.acquire()
        self.api.request_count += 1
        r = await self.api.client.delete(f"{self.api.trade_base}{path}")
        return r.status_code in (200, 204)

    async def get_by_client_id(self, cid: str) -> dict[str, Any] | None:
        """The idempotency primitive. None means the order does not exist."""
        try:
            await self.api.bucket.acquire()
            self.api.request_count += 1
            r = await self.api.client.get(
                f"{self.api.trade_base}/v2/orders:by_client_order_id",
                params={"client_order_id": cid})
            if r.status_code == 404:
                return None
            if r.status_code >= 400:
                raise AlpacaError(r.status_code, "by_client_order_id", r.text)
            return r.json()
        except (httpx.TimeoutException, httpx.TransportError):
            return None

    async def get_order(self, order_id: str) -> dict[str, Any] | None:
        """None means the order does not exist. Anything else raises.

        The previous version returned None for every error, which made a
        transient 500 indistinguishable from a missing order — and the walk
        would then cancel-and-reprice an order whose true state it never
        saw. Callers that can tolerate uncertainty catch AlpacaError and
        decide; nothing gets to mistake an outage for an absence.
        """
        try:
            return await self.api._get(f"{self.api.trade_base}/v2/orders/{order_id}")
        except AlpacaError as exc:
            if exc.status == 404:
                return None
            raise

    async def cancel(self, order_id: str) -> bool:
        return await self._delete(f"/v2/orders/{order_id}")

    # ---- submission -------------------------------------------------

    async def submit_idempotent(self, intent: ExecutionIntent) -> OrderReceipt:
        """Submit once. On any ambiguity, adopt rather than resubmit.

        Adopted and recovered orders are persisted exactly like fresh ones:
        an order that exists at the broker with no local row is an audit
        hole, and _update_status against a missing row is a silent no-op.
        """
        existing = await self.get_by_client_id(intent.client_order_id)
        if existing:
            receipt = OrderReceipt(
                decision_id=intent.decision_id,
                client_order_id=intent.client_order_id,
                alpaca_order_id=existing.get("id", ""),
                status=existing.get("status", "unknown"),
                submitted_at=parse_alpaca_ts(existing.get("submitted_at")) or utc_now(),
                adopted=True, raw=existing,
            )
            await self._persist_order(intent, receipt)
            return receipt

        payload = intent.to_alpaca_payload()
        try:
            resp = await self._post("/v2/orders", payload)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            # The request may or may not have been accepted. Ask, never guess.
            await asyncio.sleep(2.0)
            recovered = await self.get_by_client_id(intent.client_order_id)
            if recovered:
                await self.db.log_event(
                    "WARN", "order_manager", "ORDER_RECOVERED_AFTER_TIMEOUT",
                    "adopted an order found by client ID after a transport error",
                    {"client_order_id": intent.client_order_id})
                receipt = OrderReceipt(
                    decision_id=intent.decision_id,
                    client_order_id=intent.client_order_id,
                    alpaca_order_id=recovered.get("id", ""),
                    status=recovered.get("status", "unknown"),
                    submitted_at=utc_now(), adopted=True, raw=recovered,
                )
                await self._persist_order(intent, receipt)
                return receipt
            raise AlpacaError(0, "/v2/orders", f"transport failure: {exc}")

        receipt = OrderReceipt(
            decision_id=intent.decision_id,
            client_order_id=intent.client_order_id,
            alpaca_order_id=resp.get("id", ""),
            status=resp.get("status", "unknown"),
            submitted_at=parse_alpaca_ts(resp.get("submitted_at")) or utc_now(),
            raw=resp,
        )
        await self._persist_order(intent, receipt)
        return receipt

    async def _persist_order(self, intent: ExecutionIntent,
                             receipt: OrderReceipt) -> None:
        """Record the order. A persistence failure after a live submission
        is loud: the position exists at the broker either way, and a silent
        failure means a real position with no local record."""
        try:
            await self._write_order(intent, receipt)
        except Exception as exc:  # noqa: BLE001
            await self.db.log_event(
                "ERROR", "order_manager", "ORDER_NOT_PERSISTED",
                f"{intent.client_order_id}: {type(exc).__name__}: {exc}"[:400],
                {"client_order_id": intent.client_order_id,
                 "alpaca_order_id": receipt.alpaca_order_id,
                 "qty": intent.qty, "limit": intent.limit_debit})
            print(f"[order_manager] ORDER LIVE AT BROKER BUT NOT RECORDED: "
                  f"{receipt.alpaca_order_id} — {exc}", flush=True)

    async def _write_order(self, intent: ExecutionIntent,
                           receipt: OrderReceipt) -> None:
        await self.db.execute(
            "INSERT OR REPLACE INTO orders("
            "order_pk, decision_id, structure_id, client_order_id, "
            "alpaca_order_id, intent, status, qty, limit_price, attempt, "
            "limit_walk_step, submitted_at, updated_at, raw_json) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (new_uuid(), intent.decision_id, intent.structure_id,
             intent.client_order_id, receipt.alpaca_order_id,
             "CLOSE" if intent.legs[0].position_intent.endswith("close") else "OPEN",
             receipt.status, intent.qty, intent.limit_debit, intent.attempt,
             intent.attempt, iso_utc(receipt.submitted_at), iso_utc(),
             json.dumps(receipt.raw, default=str)[:20000]),
        )

    async def _await_terminal(self, order_id: str,
                              timeout: float) -> dict[str, Any] | None:
        deadline = utc_now().timestamp() + timeout
        last: dict[str, Any] | None = None
        while utc_now().timestamp() < deadline:
            try:
                last = await self.get_order(order_id)
            except AlpacaError:
                # A transient read failure is not a state; keep polling
                # until the deadline rather than guessing.
                await asyncio.sleep(self.poll_seconds)
                continue
            if last and last.get("status") in TERMINAL_STATUSES:
                return last
            await asyncio.sleep(self.poll_seconds)
        return last

    async def _confirm_after_cancel(self, order_id: str
                                    ) -> dict[str, Any] | None:
        """State of an order after a cancel request, or None if unknowable.

        The race this guards: the order fills between the last poll and the
        cancel. Alpaca rejects the cancel, and repricing without checking
        would submit a SECOND spread for the same decision. Ask, then act.
        """
        await asyncio.sleep(1.0)
        for _ in range(3):
            try:
                order = await self.get_order(order_id)
            except AlpacaError:
                await asyncio.sleep(self.poll_seconds)
                continue
            if order is None:
                return None
            if order.get("status") in TERMINAL_STATUSES:
                return order
            await asyncio.sleep(self.poll_seconds)
        return None

    # ---- the walk ---------------------------------------------------

    async def execute_with_walk(self, structure: OptionStructure,
                                decision_id: str, qty: int,
                                max_allowed_debit: float,
                                revision: int = 0,
                                closing: bool = False,
                                fill_bias_buffer: float = 0.0,
                                close_adjusted_mid: float | None = None,
                                close_conservative: float | None = None,
                                ) -> ExecutionOutcome:
        """Open: walk the debit UP toward natural, capped by risk.
        Close: walk the demanded credit DOWN toward the conservative exit.

        For closes, pass the CURRENT marks when available; entry-time
        quotes are the fallback, not the preference.
        """
        buffer = structure.staleness_buffer + max(0.0, fill_bias_buffer)
        if closing:
            mid = (close_adjusted_mid if close_adjusted_mid is not None
                   else structure.adjusted_mid_debit)
            conservative = (close_conservative if close_conservative is not None
                            else structure.long_leg.bid - structure.short_leg.ask)
            prices = close_walk_prices(mid, conservative, buffer)
        else:
            prices = walk_prices(
                adjusted_mid=structure.adjusted_mid_debit,
                natural=structure.natural_debit,
                buffer=buffer,
                max_allowed=max_allowed_debit,
            )
        return await self._run_walk(structure, decision_id, qty, prices,
                                    revision=revision, closing=closing)

    async def _run_walk(self, structure: OptionStructure, decision_id: str,
                        qty: int, prices: list[float], revision: int,
                        closing: bool) -> ExecutionOutcome:
        outcome = ExecutionOutcome(decision_id=decision_id, filled=False, qty=qty)

        for attempt, limit in enumerate(prices, start=1):
            intent = build_intent(structure, decision_id, qty, limit,
                                  revision=revision, attempt=attempt,
                                  closing=closing)
            step = WalkStep(attempt=attempt, limit_debit=limit,
                            submitted_at=utc_now(),
                            client_order_id=intent.client_order_id)
            outcome.steps.append(step)

            try:
                receipt = await self.submit_idempotent(intent)
            except AlpacaError as exc:
                step.status = f"error {exc.status}"
                outcome.error = str(exc)[:300]
                await self.db.log_event(
                    "ERROR", "order_manager", "ORDER_SUBMIT_FAILED",
                    f"attempt {attempt} failed", {"error": str(exc)[:300]})
                break

            step.order_id = receipt.alpaca_order_id
            outcome.adopted_existing = outcome.adopted_existing or receipt.adopted

            final = await self._await_terminal(receipt.alpaca_order_id,
                                               self.wait_seconds)
            status = (final or {}).get("status", "unknown")
            step.status = status

            if status == "filled":
                return await self._record_fill(outcome, step, receipt,
                                               final or {}, limit, closing)

            if status in TERMINAL_STATUSES:
                await self._update_status(receipt.client_order_id, status)
                continue

            # Still working after the wait: cancel before repricing, so two
            # live orders for one decision can never coexist. Then VERIFY:
            # the order may have filled in the race between the last poll
            # and the cancel, and repricing on top of a fill would double
            # the position.
            await self.cancel(receipt.alpaca_order_id)
            confirmed = await self._confirm_after_cancel(
                receipt.alpaca_order_id)
            confirmed_status = (confirmed or {}).get("status", "unknown")

            if confirmed_status == "filled":
                step.status = "filled"
                return await self._record_fill(outcome, step, receipt,
                                               confirmed or {}, limit,
                                               closing)

            if confirmed is None or confirmed_status not in TERMINAL_STATUSES:
                # State unknowable. Submitting another price now risks two
                # live spreads for one decision; stop and surface it.
                step.status = "unknown_after_cancel"
                outcome.error = ("order state unknowable after cancel; walk "
                                 "stopped to avoid a duplicate submission")
                outcome.final_status = "UNKNOWN_ORDER_STATE"
                await self.db.log_event(
                    "ERROR", "order_manager", "ORDER_STATE_UNKNOWN",
                    f"{receipt.alpaca_order_id} not terminal after cancel; "
                    "walk aborted",
                    {"decision_id": decision_id,
                     "order_id": receipt.alpaca_order_id})
                return outcome

            step.status = confirmed_status
            await self._update_status(receipt.client_order_id,
                                      confirmed_status)

        outcome.final_status = "NO_FILL" if not outcome.error else "ERROR"
        return outcome

    async def _record_fill(self, outcome: ExecutionOutcome, step: WalkStep,
                           receipt: OrderReceipt, final: dict[str, Any],
                           limit: float, closing: bool) -> ExecutionOutcome:
        fill_price = _extract_fill_debit(final, limit, closing=closing)
        step.fill_price = fill_price
        step.filled_at = parse_alpaca_ts(final.get("filled_at"))
        outcome.filled = True
        outcome.fill_debit = fill_price
        outcome.filled_at = step.filled_at or utc_now()
        outcome.order_id = receipt.alpaca_order_id
        outcome.client_order_id = receipt.client_order_id
        outcome.final_status = "FILLED"
        await self._update_status(receipt.client_order_id, "filled")
        return outcome

    async def _update_status(self, cid: str, status: str) -> None:
        await self.db.execute(
            "UPDATE orders SET status=?, updated_at=? WHERE client_order_id=?",
            (status, iso_utc(), cid))

    # ---- calibration -------------------------------------------------

    async def record_calibration(self, outcome: ExecutionOutcome,
                                 structure: OptionStructure,
                                 track: CandidateTrack, direction: Direction,
                                 underlying_at_submit: float,
                                 underlying_at_fill: float | None = None,
                                 closing: bool = False,
                                 close_adjusted_mid: float | None = None,
                                 close_conservative: float | None = None,
                                 decision_id: str | None = None,
                                 ) -> ExecutionCalibration | None:
        """Measure indicative-reference-to-fill bias (§17.5).

        Every submitted opening AND closing spread creates a record. For a
        close, the reference is the current adjusted mid credit and the
        floor is the conservative exit; pass them when known, entry-time
        values are only a fallback.
        """
        if not outcome.steps:
            return None

        side = OrderSide.CLOSE if closing else OrderSide.OPEN
        underlying_at_quote = (structure.underlying_price
                               or underlying_at_submit)
        base_decision = decision_id or outcome.decision_id
        if closing:
            adjusted_ref = max(0.01, close_adjusted_mid
                               if close_adjusted_mid is not None
                               else structure.adjusted_mid_debit)
            # Rounded to the tick exactly as the ladder floor is, so the
            # final submitted credit compares cleanly against it.
            conservative = (close_conservative
                            if close_conservative is not None
                            else structure.long_leg.bid
                            - structure.short_leg.ask)
            natural_ref = max(0.01, round(conservative, 2))
        else:
            adjusted_ref = structure.adjusted_mid_debit
            natural_ref = structure.natural_debit

        record = ExecutionCalibration.with_derived(
            calibration_id=calibration_id(base_decision, str(side)),
            decision_id=base_decision,
            symbol=structure.symbol,
            side=side,
            candidate_track=track,
            direction=direction,
            submitted_at=outcome.steps[0].submitted_at,
            filled_at=outcome.filled_at,
            indicative_raw_mid=max(0.01, structure.raw_mid_debit),
            indicative_adjusted_mid=adjusted_ref,
            natural_debit_estimate=natural_ref,
            initial_limit_debit=outcome.steps[0].limit_debit,
            final_submitted_limit=outcome.steps[-1].limit_debit,
            actual_fill_debit=outcome.fill_debit,
            seconds_to_fill=outcome.seconds_to_fill,
            limit_walk_steps=outcome.walk_steps,
            quote_lag_seconds=structure.max_quote_lag_seconds,
            underlying_at_quote=underlying_at_quote,
            underlying_at_submit=underlying_at_submit,
            underlying_at_fill=underlying_at_fill,
        )

        await self.db.execute(
            "INSERT OR REPLACE INTO execution_calibrations("
            "calibration_id, decision_id, symbol, side, candidate_track, "
            "direction, submitted_at, filled_at, indicative_raw_mid, "
            "indicative_adjusted_mid, natural_debit_estimate, "
            "initial_limit_debit, final_submitted_limit, actual_fill_debit, "
            "seconds_to_fill, limit_walk_steps, quote_lag_seconds, "
            "underlying_at_quote, underlying_at_submit, underlying_at_fill, "
            "fill_bias_vs_adjusted, fill_bias_vs_limit, fill_slippage_pct) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (record.calibration_id, record.decision_id, record.symbol,
             str(record.side), str(record.candidate_track),
             str(record.direction), iso_utc(record.submitted_at),
             iso_utc(record.filled_at) if record.filled_at else None,
             record.indicative_raw_mid, record.indicative_adjusted_mid,
             record.natural_debit_estimate, record.initial_limit_debit,
             record.final_submitted_limit, record.actual_fill_debit,
             record.seconds_to_fill, record.limit_walk_steps,
             record.quote_lag_seconds, record.underlying_at_quote,
             record.underlying_at_submit, record.underlying_at_fill,
             record.fill_bias_vs_adjusted, record.fill_bias_vs_limit,
             record.fill_slippage_pct),
        )
        return record

    # ---- positions ---------------------------------------------------

    async def open_option_positions(self) -> list[dict[str, Any]]:
        positions = await self.api._get(f"{self.api.trade_base}/v2/positions")
        rows = positions if isinstance(positions, list) else []
        return [p for p in rows if p.get("asset_class") == "us_option"]

    async def working_orders(self) -> list[dict[str, Any]]:
        orders = await self.api._get(f"{self.api.trade_base}/v2/orders",
                                     {"status": "open", "limit": 100})
        return orders if isinstance(orders, list) else []


def _extract_fill_debit(order: dict[str, Any], fallback: float,
                        closing: bool = False) -> float:
    """Net amount actually filled, as a positive magnitude.

    Opening: the net debit paid (buys minus sells). Closing: the net credit
    received (sells minus buys). Either way the caller gets a positive
    number in the same units as the submitted limit.
    """
    avg = order.get("filled_avg_price")
    if avg:
        try:
            return abs(float(avg))
        except (TypeError, ValueError):
            pass

    legs = order.get("legs") or []
    if legs:
        net = 0.0
        seen = False
        for leg in legs:
            price = leg.get("filled_avg_price")
            if not price:
                continue
            seen = True
            qty = float(leg.get("ratio_qty") or 1)
            buy = str(leg.get("side", "")).lower() == "buy"
            if closing:
                sign = -1.0 if buy else 1.0     # credit: sells add, buys cost
            else:
                sign = 1.0 if buy else -1.0     # debit: buys add, sells offset
            net += sign * abs(float(price)) * qty
        if seen and net > 0:
            return round(net, 4)
    return fallback
