"""
Alpha Council v2.3 - Alpaca REST transport layer.

Handles auth, rate limiting, retry, and pagination for the high-throughput
data path. MCP V2 remains the control/execution plane; this is the batch
scanning plane (spec Section 3.1).

Design notes:
  * Token bucket at 150 req/min against a 200 req/min Basic-plan ceiling.
    The headroom absorbs the MCP client and any manual scripts.
  * Every paginated endpoint is drained through _paginate(). v1 of the
    probe silently truncated the contracts endpoint at its 200 limit.
  * Retries cover 429 and 5xx only. A 4xx other than 429 is a bug in our
    request and must surface immediately, not be retried into a rate limit.
  * Quote ages are clamped at zero. Negative values are local clock skew.

Place at: alpha_council/alpaca/rest_client.py
"""

from __future__ import annotations

import asyncio
import random
import time
from datetime import datetime, timezone
from typing import Any, Iterable, Iterator

import httpx

from alpha_council.settings import Settings, get_settings, load_yaml

# --------------------------------------------------------------------------
# timestamp helpers (shared with the probe; will move to utils/time.py)
# --------------------------------------------------------------------------


def parse_ts(raw: str | None) -> datetime | None:
    """Parse Alpaca RFC3339 timestamps, which carry nanosecond precision."""
    if not raw:
        return None
    s = raw.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    if "." in s:
        head, rest = s.split(".", 1)
        if "+" in rest:
            frac, tz = rest.split("+", 1)
            tz = "+" + tz
        elif "-" in rest:
            frac, tz = rest.split("-", 1)
            tz = "-" + tz
        else:
            frac, tz = rest, ""
        frac = (frac + "000000")[:6]
        s = f"{head}.{frac}{tz}"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def quote_age_seconds(raw: str | None,
                      now: datetime | None = None) -> float | None:
    """Age of a timestamp in seconds, clamped at zero."""
    ts = parse_ts(raw)
    if ts is None:
        return None
    ref = now or datetime.now(timezone.utc)
    return round(max(0.0, (ref - ts).total_seconds()), 2)


def rfc3339(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def chunked(items: Iterable[str], size: int) -> Iterator[list[str]]:
    batch: list[str] = []
    for item in items:
        batch.append(item)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch


# --------------------------------------------------------------------------
# errors
# --------------------------------------------------------------------------


class AlpacaError(RuntimeError):
    def __init__(self, status: int, url: str, body: str):
        self.status = status
        self.url = url
        self.body = body[:500]
        super().__init__(f"HTTP {status} {url}: {self.body}")


class AlpacaRateLimited(AlpacaError):
    pass


class AlpacaDataUnavailable(AlpacaError):
    """Feed entitlement rejection - e.g. requesting SIP on the Basic plan."""


# --------------------------------------------------------------------------
# rate limiter
# --------------------------------------------------------------------------


class TokenBucket:
    """Async token bucket. Shared across every request from this process."""

    def __init__(self, rate_per_minute: int, burst: int):
        self.rate = rate_per_minute / 60.0
        self.capacity = float(burst)
        self._tokens = float(burst)
        self._updated = time.monotonic()
        self._lock = asyncio.Lock()
        self.total_waits = 0
        self.total_wait_seconds = 0.0

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()
                self._tokens = min(
                    self.capacity, self._tokens + (now - self._updated) * self.rate
                )
                self._updated = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                wait = (1.0 - self._tokens) / self.rate
                self.total_waits += 1
                self.total_wait_seconds += wait
                await asyncio.sleep(wait)

    def stats(self) -> dict[str, float | int]:
        return {
            "waits": self.total_waits,
            "wait_seconds": round(self.total_wait_seconds, 2),
            "tokens_available": round(self._tokens, 2),
        }


# --------------------------------------------------------------------------
# client
# --------------------------------------------------------------------------


class AlpacaRestClient:
    """Async REST client for the Alpaca data and paper-trading APIs."""

    MAX_SNAPSHOT_SYMBOLS = 100
    MAX_OPTION_SYMBOLS = 100

    def __init__(self, settings: Settings | None = None,
                 scoring: dict[str, Any] | None = None):
        self.settings = settings or get_settings()
        self.settings.assert_paper_only()

        cfg = (scoring or load_yaml("scoring")).get("rate_limits", {})
        self.max_retries = int(cfg.get("max_retries", 4))
        self.backoff_base = float(cfg.get("backoff_base_seconds", 1.0))
        self.backoff_max = float(cfg.get("backoff_max_seconds", 30.0))
        self.bucket = TokenBucket(
            int(cfg.get("alpaca_requests_per_minute", 150)),
            int(cfg.get("alpaca_burst", 20)),
        )

        self.stock_feed = self.settings.alpaca_data_feed
        self.option_feed = self.settings.alpaca_option_feed
        self.data_base = self.settings.data_base_url
        self.trade_base = self.settings.trading_base_url

        self._client: httpx.AsyncClient | None = None
        self.request_count = 0
        self.retry_count = 0

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    async def __aenter__(self) -> "AlpacaRestClient":
        await self.connect()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    async def connect(self) -> None:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(30.0, connect=10.0),
                headers=self.settings.alpaca_headers,
                limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
            )

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError("AlpacaRestClient.connect() has not been awaited.")
        return self._client

    # ------------------------------------------------------------------
    # transport
    # ------------------------------------------------------------------

    async def _get(self, url: str,
                   params: dict[str, Any] | None = None) -> dict[str, Any]:
        clean = {k: v for k, v in (params or {}).items() if v is not None}
        attempt = 0
        while True:
            await self.bucket.acquire()
            self.request_count += 1
            try:
                r = await self.client.get(url, params=clean)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                if attempt >= self.max_retries:
                    raise AlpacaError(0, url, f"transport failure: {exc}") from exc
                await self._backoff(attempt)
                attempt += 1
                self.retry_count += 1
                continue

            if r.status_code == 200:
                return r.json()

            if r.status_code == 429:
                if attempt >= self.max_retries:
                    raise AlpacaRateLimited(429, url, r.text)
                retry_after = r.headers.get("Retry-After")
                delay = float(retry_after) if retry_after else None
                await self._backoff(attempt, delay)
                attempt += 1
                self.retry_count += 1
                continue

            if 500 <= r.status_code < 600:
                if attempt >= self.max_retries:
                    raise AlpacaError(r.status_code, url, r.text)
                await self._backoff(attempt)
                attempt += 1
                self.retry_count += 1
                continue

            # 4xx other than 429: our request is wrong. Surface it now.
            if r.status_code in (401, 403) and "feed" in str(clean):
                raise AlpacaDataUnavailable(r.status_code, url, r.text)
            raise AlpacaError(r.status_code, url, r.text)

    async def _backoff(self, attempt: int, explicit: float | None = None) -> None:
        if explicit is not None:
            await asyncio.sleep(min(explicit, self.backoff_max))
            return
        delay = min(self.backoff_base * (2 ** attempt), self.backoff_max)
        await asyncio.sleep(delay * (0.5 + random.random() * 0.5))  # jitter

    async def _paginate(self, url: str, params: dict[str, Any],
                        collect_key: str, token_key: str = "next_page_token",
                        max_pages: int = 50) -> dict[str, Any]:
        """Drain a paginated endpoint.

        collect_key may address a dict (symbol -> list) or a list payload.
        Returns the merged collection.
        """
        merged: Any = None
        token: str | None = None
        for _ in range(max_pages):
            page_params = dict(params)
            if token:
                page_params["page_token"] = token
            payload = await self._get(url, page_params)
            chunk = payload.get(collect_key)

            if chunk is None:
                pass
            elif merged is None:
                merged = chunk if isinstance(chunk, list) else dict(chunk)
            elif isinstance(merged, list):
                merged.extend(chunk)
            else:
                for sym, rows in chunk.items():
                    if isinstance(rows, list):
                        merged.setdefault(sym, []).extend(rows)
                    else:
                        merged[sym] = rows

            token = payload.get(token_key)
            if not token:
                break
        return merged if merged is not None else {}

    # ------------------------------------------------------------------
    # trading API
    # ------------------------------------------------------------------

    async def get_clock(self) -> dict[str, Any]:
        return await self._get(f"{self.trade_base}/v2/clock")

    async def get_account(self) -> dict[str, Any]:
        return await self._get(f"{self.trade_base}/v2/account")

    async def get_calendar(self, start: str, end: str) -> list[dict[str, Any]]:
        payload = await self._get(f"{self.trade_base}/v2/calendar",
                                  {"start": start, "end": end})
        return payload if isinstance(payload, list) else []

    async def get_assets(self, symbols: list[str]) -> list[dict[str, Any]]:
        """Per-symbol asset lookup. Used once at startup for eligibility."""
        out = []
        for sym in symbols:
            try:
                out.append(await self._get(f"{self.trade_base}/v2/assets/{sym}"))
            except AlpacaError as exc:
                if exc.status == 404:
                    continue
                raise
        return out

    async def get_option_contracts(
        self, underlying: str, *,
        expiration_gte: str, expiration_lte: str,
        strike_gte: float | None = None, strike_lte: float | None = None,
        contract_type: str | None = None, limit: int = 200,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "underlying_symbols": underlying,
            "status": "active",
            "expiration_date_gte": expiration_gte,
            "expiration_date_lte": expiration_lte,
            "limit": limit,
        }
        if strike_gte is not None:
            params["strike_price_gte"] = f"{strike_gte:.2f}"
        if strike_lte is not None:
            params["strike_price_lte"] = f"{strike_lte:.2f}"
        if contract_type:
            params["type"] = contract_type
        result = await self._paginate(
            f"{self.trade_base}/v2/options/contracts", params, "option_contracts"
        )
        return result if isinstance(result, list) else []

    # ------------------------------------------------------------------
    # market data API - equities
    # ------------------------------------------------------------------

    async def get_stock_snapshots(self, symbols: list[str]) -> dict[str, Any]:
        """Batch snapshots. Chunked at 100 symbols per request."""
        merged: dict[str, Any] = {}
        for batch in chunked(symbols, self.MAX_SNAPSHOT_SYMBOLS):
            payload = await self._get(
                f"{self.data_base}/v2/stocks/snapshots",
                {"symbols": ",".join(batch), "feed": self.stock_feed},
            )
            merged.update(payload.get("snapshots", payload))
        return merged

    async def get_latest_quotes(self, symbols: list[str]) -> dict[str, Any]:
        merged: dict[str, Any] = {}
        for batch in chunked(symbols, self.MAX_SNAPSHOT_SYMBOLS):
            payload = await self._get(
                f"{self.data_base}/v2/stocks/quotes/latest",
                {"symbols": ",".join(batch), "feed": self.stock_feed},
            )
            merged.update(payload.get("quotes", {}))
        return merged

    async def get_stock_bars(self, symbols: list[str], timeframe: str,
                             start: datetime, end: datetime,
                             limit: int = 10000) -> dict[str, list[dict[str, Any]]]:
        """Historical bars, fully paginated. Chunked at 100 symbols."""
        merged: dict[str, list[dict[str, Any]]] = {}
        for batch in chunked(symbols, self.MAX_SNAPSHOT_SYMBOLS):
            result = await self._paginate(
                f"{self.data_base}/v2/stocks/bars",
                {
                    "symbols": ",".join(batch),
                    "timeframe": timeframe,
                    "start": rfc3339(start),
                    "end": rfc3339(end),
                    "limit": limit,
                    "sort": "asc",
                    "adjustment": "raw",
                    "feed": self.stock_feed,
                },
                "bars",
            )
            if isinstance(result, dict):
                for sym, rows in result.items():
                    merged.setdefault(sym, []).extend(rows or [])
        return merged

    async def get_news(self, symbols: list[str], start: datetime,
                       end: datetime, limit: int = 50,
                       include_content: bool = True) -> list[dict[str, Any]]:
        result = await self._paginate(
            f"{self.data_base}/v1beta1/news",
            {
                "symbols": ",".join(symbols),
                "start": rfc3339(start),
                "end": rfc3339(end),
                "limit": limit,
                "sort": "asc",
                "include_content": str(include_content).lower(),
            },
            "news",
        )
        return result if isinstance(result, list) else []

    # ------------------------------------------------------------------
    # market data API - options
    # ------------------------------------------------------------------

    async def get_option_chain(self, underlying: str,
                               expiration_gte: str | None = None,
                               expiration_lte: str | None = None,
                               limit: int = 1000) -> dict[str, Any]:
        """Full chain snapshot for one underlying, paginated."""
        result = await self._paginate(
            f"{self.data_base}/v1beta1/options/snapshots/{underlying}",
            {
                "feed": self.option_feed,
                "limit": limit,
                "expiration_date_gte": expiration_gte,
                "expiration_date_lte": expiration_lte,
            },
            "snapshots",
        )
        return result if isinstance(result, dict) else {}

    async def get_option_snapshots(self, occ_symbols: list[str]) -> dict[str, Any]:
        """Snapshots for specific contracts. Chunked at 100."""
        merged: dict[str, Any] = {}
        for batch in chunked(occ_symbols, self.MAX_OPTION_SYMBOLS):
            payload = await self._get(
                f"{self.data_base}/v1beta1/options/snapshots",
                {"symbols": ",".join(batch), "feed": self.option_feed},
            )
            merged.update(payload.get("snapshots", {}))
        return merged

    # ------------------------------------------------------------------
    # diagnostics
    # ------------------------------------------------------------------

    def stats(self) -> dict[str, Any]:
        return {
            "requests": self.request_count,
            "retries": self.retry_count,
            "stock_feed": self.stock_feed,
            "option_feed": self.option_feed,
            **self.bucket.stats(),
        }
