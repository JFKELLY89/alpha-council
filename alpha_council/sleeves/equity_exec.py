"""
Alpha Council v2.7 - single-leg equity execution for the sleeves.

Deliberately simpler than the options walk: the sleeves trade liquid
ETFs and large-cap shares with marketable DAY limits (buy at the ask,
sell at the bid), poll briefly, and cancel on timeout. The cancel path
reuses the options desk's confirm-after-cancel discipline so a fill that
races the cancel is adopted, never double-submitted.

Every order - submitted, filled, canceled or rejected - lands in
sleeve_orders, because net_invested (the sleeve isolation identity in
accounting.py) is computed from these rows and an unrecorded fill would
silently leak sleeve P&L into the options desk's NAV.

Place at: alpha_council/sleeves/equity_exec.py
"""

from __future__ import annotations

import asyncio
from typing import Any

from alpha_council.db.engine import Database
from alpha_council.utils.ids import new_uuid, short_id
from alpha_council.utils.time import utc_now

TERMINAL_STATUSES = {"filled", "canceled", "expired", "rejected",
                     "done_for_day", "stopped"}


async def _record(db: Database, order_pk: str, **fields: Any) -> None:
    sets = ", ".join(f"{k}=?" for k in fields)
    await db.execute(
        f"UPDATE sleeve_orders SET {sets}, updated_at=? WHERE order_pk=?",
        (*fields.values(), utc_now().isoformat(), order_pk))


async def submit_marketable(orders: Any, db: Database, *, sleeve: str,
                            symbol: str, side: str, qty: int,
                            reference_price: float, reason: str,
                            decision_id: str | None = None,
                            timeout_seconds: float = 45.0,
                            poll_seconds: float = 2.0
                            ) -> dict[str, Any] | None:
    """One marketable-limit DAY order. Returns the broker order if FILLED.

    A partial fill at timeout is kept (the filled part is real and must be
    accounted), the remainder canceled, and None returned so the caller
    knows the intent did not complete.
    """
    if qty < 1:
        return None
    limit = round(float(reference_price), 2)
    if limit <= 0:
        return None
    order_pk = f"slvord_{new_uuid()[:12]}"
    cid = f"slv_{sleeve.lower()}_{short_id(6)}_{short_id(4)}"
    await db.execute(
        "INSERT INTO sleeve_orders(order_pk, sleeve, decision_id, "
        "client_order_id, symbol, side, qty, limit_price, status, "
        "submitted_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (order_pk, sleeve, decision_id, cid, symbol, side, qty, limit,
         "new", utc_now().isoformat(), utc_now().isoformat()))

    try:
        broker = await orders._post("/v2/orders", {
            "symbol": symbol, "qty": str(int(qty)), "side": side,
            "type": "limit", "limit_price": f"{limit:.2f}",
            "time_in_force": "day", "client_order_id": cid,
        })
    except Exception as exc:  # noqa: BLE001 - recorded, caller decides
        await _record(db, order_pk, status="rejected",
                      reason=f"{reason} | submit failed: {exc}"[:200])
        raise
    order_id = str(broker.get("id") or "")
    await _record(db, order_pk, alpaca_order_id=order_id,
                  status=str(broker.get("status") or "accepted"),
                  reason=reason[:200])

    async def settle(order: dict[str, Any]) -> dict[str, Any] | None:
        status = str(order.get("status") or "")
        filled_qty = float(order.get("filled_qty") or 0.0)
        fill_price = (float(order.get("filled_avg_price"))
                      if order.get("filled_avg_price") else None)
        notional = (round(filled_qty * fill_price, 2)
                    if fill_price is not None and filled_qty else None)
        await _record(db, order_pk, status=status, filled_qty=filled_qty,
                      fill_price=fill_price, notional=notional)
        return order if status == "filled" else None

    deadline = asyncio.get_event_loop().time() + timeout_seconds
    while asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(poll_seconds)
        order = await orders.get_order(order_id)
        if order is None:
            continue
        if str(order.get("status")) in TERMINAL_STATUSES:
            return await settle(order)

    # Timeout: cancel, then believe whatever the broker says happened.
    await orders.cancel(order_id)
    confirmed = await orders._confirm_after_cancel(order_id)
    if confirmed is not None:
        return await settle(confirmed)
    await _record(db, order_pk, status="canceled")
    return None
