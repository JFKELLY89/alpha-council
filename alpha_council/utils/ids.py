"""
Alpha Council v2.4 - identifier generation.

The client order ID format is load-bearing for idempotency: after any
timeout the order manager queries by client ID before considering a retry,
so the ID must be deterministic in shape and unique in fact.

Place at: alpha_council/utils/ids.py
"""

from __future__ import annotations

import hashlib
import re
import uuid
from datetime import datetime

from alpha_council.utils.time import iso_utc

CLIENT_ORDER_ID_MAX = 48
CLIENT_ORDER_ID_RE = re.compile(r"^ac_[0-9a-f]{8}_r[01]_[0-9a-f]{8}$")


def short_id(n: int = 8) -> str:
    return uuid.uuid4().hex[:n]


def new_uuid() -> str:
    return uuid.uuid4().hex


def decision_id() -> str:
    return f"dec_{short_id(12)}"


def scan_id(started_at: datetime | None = None) -> str:
    """Sortable by time, which makes the audit trail readable in raw SQL."""
    stamp = iso_utc(started_at)[:19].replace("-", "").replace(":", "").replace("T", "")
    return f"scan_{stamp}_{short_id(4)}"


def candidate_id(scan: str, symbol: str) -> str:
    return f"cand_{scan[-8:]}_{symbol.upper()}_{short_id(4)}"


def structure_id(symbol: str, rank: int) -> str:
    return f"st_{symbol.upper()}_{rank}_{short_id(6)}"


def discovery_id(scan: str, symbol: str, source: str) -> str:
    return f"disc_{scan[-8:]}_{symbol.upper()}_{source[:4].lower()}_{short_id(4)}"


def rejection_id() -> str:
    return f"rej_{short_id(12)}"


def shadow_id(decision: str, variant: str) -> str:
    return f"shd_{decision[-8:]}_{variant[:3].lower()}_{short_id(4)}"


def calibration_id(decision: str, side: str) -> str:
    return f"cal_{decision[-8:]}_{side[:1].lower()}_{short_id(6)}"


def client_order_id(decision: str, revision: int = 0) -> str:
    """ac_<8hex>_r<0|1>_<8hex>, always under Alpaca's 48-character limit.

    The decision fragment is hashed rather than sliced so that a decision ID
    containing non-hex characters still produces a valid, stable prefix.
    """
    if revision not in (0, 1):
        raise ValueError(f"revision must be 0 or 1, got {revision}")
    digest = hashlib.sha256(decision.encode("utf-8")).hexdigest()[:8]
    cid = f"ac_{digest}_r{revision}_{short_id(8)}"
    if len(cid) > CLIENT_ORDER_ID_MAX:  # pragma: no cover - shape is fixed
        raise ValueError(f"client_order_id too long: {cid}")
    return cid


def is_valid_client_order_id(cid: str) -> bool:
    return bool(CLIENT_ORDER_ID_RE.match(cid)) and len(cid) <= CLIENT_ORDER_ID_MAX


def decision_fragment(client_order_id_value: str) -> str | None:
    """Recover the decision hash from a client order ID, for reconciliation."""
    m = CLIENT_ORDER_ID_RE.match(client_order_id_value)
    return client_order_id_value[3:11] if m else None


def content_hash(*parts: str) -> str:
    """Stable hash for intelligence deduplication.

    Normalizes whitespace and case so that trivial formatting differences
    between two copies of the same wire story collapse to one hash.
    """
    normalized = " ".join(" ".join(p.split()).lower() for p in parts if p)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def input_hash(payload: str) -> str:
    """Hash of an LLM prompt, so identical inputs are provably identical."""
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def occ_key(underlying: str, expiration: str, option_type: str,
            strike: float) -> str:
    """Canonical contract identity, independent of provider display format."""
    return f"{underlying.upper()}|{expiration}|{option_type.upper()}|{strike:.3f}"
