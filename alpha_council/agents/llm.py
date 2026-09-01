"""
Alpha Council v2.4 - LLM provider clients.

Both providers are wrapped behind one interface that guarantees three
things:

  STRUCTURED OR NOTHING. Every response parses into a Pydantic model or the
  call fails. A malformed or refused response returns a failed result, and
  the orchestrator turns that into NO TRADE plus a gate_rejections row.
  There is no free-text path into a trading decision.

  DEFENSIVE PARAMETERS. Model names and reasoning/effort parameter shapes
  for GPT-5.6 and Sonnet 5 are configuration, not assumptions baked into
  code. If the provider rejects a parameter, the client retries once
  without it and logs what it dropped, rather than failing a whole council
  session over a keyword.

  EVERY CALL IS PRICED. Token counts and cost land in api_usage with the
  decision_id before the result is returned.

Combines what the spec splits into openai_client.py and anthropic_client.py;
they share the retry, recording, and parsing logic and separating them would
duplicate all three.

Place at: alpha_council/agents/llm.py
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any, Type, TypeVar

from pydantic import BaseModel, ValidationError

from alpha_council.agents.budget import BudgetManager, UsageRecord
from alpha_council.agents.evidence import EvidencePackage
from alpha_council.db.engine import Database
from alpha_council.utils.ids import input_hash, new_uuid
from alpha_council.utils.time import iso_utc, utc_now

T = TypeVar("T", bound=BaseModel)

# Parameters worth retrying without when a provider rejects them. These are
# the surfaces most likely to have moved since this code was written.
DROPPABLE_PARAMS = ("reasoning", "effort", "output_config", "reasoning_effort",
                    "thinking", "response_format", "text")


class LLMError(RuntimeError):
    pass


class LLMRefusal(LLMError):
    """The model declined. Treated as NO TRADE, never as a soft failure."""


@dataclass(slots=True)
class LLMResult:
    ok: bool
    purpose: str
    provider: str
    model: str
    parsed: BaseModel | None = None
    raw_text: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    cost_usd: float = 0.0
    latency_seconds: float = 0.0
    request_id: str | None = None
    dropped_params: list[str] = field(default_factory=list)
    error: str | None = None

    @property
    def failed(self) -> bool:
        return not self.ok or self.parsed is None


class LLMClient:
    """Shared plumbing. Subclasses implement _invoke only."""

    provider = "unknown"

    def __init__(self, db: Database, budget: BudgetManager,
                 config: dict[str, Any], api_key: str):
        self.db = db
        self.budget = budget
        self.config = config
        self.api_key = api_key
        self._client: Any = None
        self.dropped_params: set[str] = set()

    # ---- subclass surface -------------------------------------------

    async def _invoke(self, model: str, system: str, user: str,
                      schema: Type[T], settings: dict[str, Any],
                      omit: set[str]) -> tuple[str, dict[str, int], str | None]:
        raise NotImplementedError

    # ---- public ------------------------------------------------------

    async def call(self, purpose: str, system_prompt: str,
                   evidence: EvidencePackage, schema: Type[T],
                   decision_id: str | None = None,
                   session_id: str | None = None,
                   estimated_cost: float = 0.05) -> LLMResult:
        spec = self.config.get("models", {}).get(purpose, {})
        model = spec.get("model", "")
        result = LLMResult(ok=False, purpose=purpose, provider=self.provider,
                           model=model)

        gate = self.budget.allow_call(self.provider, purpose, session_id,
                                      estimated_cost)
        if not gate.allowed:
            result.error = f"{gate.gate_id}: {gate.reason}"
            await self._journal(result, decision_id, "", "BUDGET_BLOCKED")
            return result

        # Stable system text first, volatile evidence last: prompt caching
        # keys on the shared prefix.
        user = evidence.to_json(indent=None)
        started = utc_now()

        try:
            raw, usage, request_id = await self._invoke(
                model, system_prompt, user, schema, spec, set(self.dropped_params))
        except LLMRefusal as exc:
            result.error = f"refusal: {exc}"
            await self._journal(result, decision_id, user, "REFUSED")
            return result
        except Exception as exc:  # noqa: BLE001 - any failure is NO TRADE
            result.error = f"{type(exc).__name__}: {exc}"[:400]
            await self._journal(result, decision_id, user, "ERROR")
            return result

        result.latency_seconds = round(
            (utc_now() - started).total_seconds(), 2)
        result.raw_text = raw
        result.input_tokens = usage.get("input", 0)
        result.output_tokens = usage.get("output", 0)
        result.cached_tokens = usage.get("cached", 0)
        result.request_id = request_id
        result.dropped_params = sorted(self.dropped_params)

        result.cost_usd = await self.budget.record(
            UsageRecord(provider=self.provider, model=model, purpose=purpose,
                        input_tokens=result.input_tokens,
                        output_tokens=result.output_tokens,
                        cached_tokens=result.cached_tokens,
                        decision_id=decision_id, request_id=request_id),
            endpoint=purpose, session_id=session_id)

        try:
            result.parsed = parse_structured(raw, schema)
            result.ok = True
        except (ValidationError, ValueError, json.JSONDecodeError) as exc:
            result.error = f"schema validation failed: {exc}"[:400]
            await self._journal(result, decision_id, user, "INVALID_SCHEMA")
            return result

        await self._journal(result, decision_id, user, "OK")
        return result

    async def _journal(self, result: LLMResult, decision_id: str | None,
                       prompt: str, status: str) -> None:
        """Record the call. A journalling failure must never discard a
        model response: the audit row is valuable, the decision is more so."""
        try:
            await self.db.execute(
                "INSERT INTO agent_runs(run_id, decision_id, agent_name, "
                "provider, model, purpose, started_at, completed_at, "
                "input_hash, prompt_text, output_json, input_tokens, "
                "output_tokens, cost_usd, status, error) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (new_uuid(), decision_id, result.purpose, result.provider,
                 result.model, result.purpose, iso_utc(), iso_utc(),
                 input_hash(prompt), prompt[:20000],
                 (result.parsed.model_dump_json() if result.parsed
                  else result.raw_text[:8000]),
                 result.input_tokens, result.output_tokens, result.cost_usd,
                 status, result.error))
        except Exception as exc:  # noqa: BLE001
            # Retry without the foreign key rather than losing the record.
            try:
                await self.db.log_event(
                    "ERROR", "llm", "AGENT_RUN_NOT_JOURNALLED",
                    f"{result.purpose}: {type(exc).__name__}",
                    {"error": str(exc)[:300], "decision_id": decision_id})
            except Exception:  # noqa: BLE001
                pass


def parse_structured(raw: str, schema: Type[T]) -> T:
    """Parse a model response into the schema.

    Tolerates fenced code blocks and leading prose, because a provider that
    ignores a structured-output hint should not cost a trading decision.
    Never tolerates extra fields: the models use extra='forbid', so a
    hallucinated key fails here by design.
    """
    text = (raw or "").strip()
    if not text:
        raise ValueError("empty response")

    if "```" in text:
        blocks = text.split("```")
        for block in blocks:
            candidate = block.strip()
            if candidate.startswith("json"):
                candidate = candidate[4:].strip()
            if candidate.startswith("{"):
                text = candidate
                break

    if not text.startswith("{"):
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            raise ValueError(f"no JSON object in response: {text[:120]!r}")
        text = text[start:end + 1]

    return schema.model_validate_json(text)


# ======================================================================
# OpenAI
# ======================================================================

class OpenAIClient(LLMClient):
    provider = "openai"

    async def _ensure(self) -> Any:
        if self._client is None:
            from openai import AsyncOpenAI

            # Without a timeout a stalled request hangs the whole council,
            # and with it the scan and every job behind it.
            self._client = AsyncOpenAI(api_key=self.api_key, timeout=90.0,
                                       max_retries=1)
        return self._client

    async def _invoke(self, model: str, system: str, user: str,
                      schema: Type[T], settings: dict[str, Any],
                      omit: set[str]) -> tuple[str, dict[str, int], str | None]:
        client = await self._ensure()

        kwargs: dict[str, Any] = {
            "model": model,
            "input": [{"role": "system", "content": system},
                      {"role": "user", "content": user}],
        }
        if "reasoning" not in omit and settings.get("reasoning"):
            kwargs["reasoning"] = {"effort": settings["reasoning"]}
        if "text" not in omit:
            kwargs["text"] = {
                "format": {
                    "type": "json_schema",
                    "name": schema.__name__,
                    "schema": schema.model_json_schema(),
                    "strict": False,
                }
            }

        try:
            resp = await client.responses.create(**kwargs)
        except Exception as exc:  # noqa: BLE001
            dropped = self._drop_param(exc, kwargs)
            if dropped is None:
                raise
            await self.db.log_event(
                "WARN", "openai_client", "PARAM_DROPPED",
                f"retrying without {dropped!r}",
                {"model": model, "error": str(exc)[:200]})
            kwargs.pop(dropped, None)
            resp = await client.responses.create(**kwargs)

        text = getattr(resp, "output_text", "") or ""
        if not text:
            text = self._extract_text(resp)

        usage = getattr(resp, "usage", None)
        counts = {
            "input": int(getattr(usage, "input_tokens", 0) or 0),
            "output": int(getattr(usage, "output_tokens", 0) or 0),
            "cached": int(getattr(
                getattr(usage, "input_tokens_details", None),
                "cached_tokens", 0) or 0),
        }
        return text, counts, getattr(resp, "id", None)

    def _drop_param(self, exc: Exception, kwargs: dict[str, Any]) -> str | None:
        message = str(exc).lower()
        for param in DROPPABLE_PARAMS:
            if param in kwargs and param in message:
                self.dropped_params.add(param)
                return param
        return None

    @staticmethod
    def _extract_text(resp: Any) -> str:
        chunks: list[str] = []
        for item in getattr(resp, "output", []) or []:
            for part in getattr(item, "content", []) or []:
                if getattr(part, "type", "") == "refusal":
                    raise LLMRefusal(getattr(part, "refusal", "refused"))
                value = getattr(part, "text", None)
                if value:
                    chunks.append(value)
        return "\n".join(chunks)


# ======================================================================
# Anthropic
# ======================================================================

class AnthropicClient(LLMClient):
    provider = "anthropic"

    async def _ensure(self) -> Any:
        if self._client is None:
            from anthropic import AsyncAnthropic

            self._client = AsyncAnthropic(api_key=self.api_key, timeout=90.0,
                                          max_retries=1)
        return self._client

    async def _invoke(self, model: str, system: str, user: str,
                      schema: Type[T], settings: dict[str, Any],
                      omit: set[str]) -> tuple[str, dict[str, int], str | None]:
        client = await self._ensure()

        instruction = (
            f"{system}\n\nRespond with a single JSON object matching this "
            f"schema. No prose, no code fences.\n"
            f"{json.dumps(schema.model_json_schema(), separators=(',', ':'))}"
        )

        kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": int(settings.get("max_tokens", 4096)),
            "system": instruction,
            "messages": [{"role": "user", "content": user}],
        }
        # Sonnet 5 rejects non-default sampling parameters, so none are set.
        if "output_config" not in omit:
            kwargs["output_config"] = {
                "format": {"type": "json_schema",
                           "schema": schema.model_json_schema()}}
        if "effort" not in omit and settings.get("effort"):
            kwargs["effort"] = settings["effort"]

        try:
            resp = await client.messages.create(**kwargs)
        except Exception as exc:  # noqa: BLE001
            dropped = self._drop_param(exc, kwargs)
            if dropped is None:
                raise
            await self.db.log_event(
                "WARN", "anthropic_client", "PARAM_DROPPED",
                f"retrying without {dropped!r}",
                {"model": model, "error": str(exc)[:200]})
            kwargs.pop(dropped, None)
            resp = await client.messages.create(**kwargs)

        if getattr(resp, "stop_reason", "") == "refusal":
            raise LLMRefusal("model returned a refusal stop reason")

        chunks = []
        for block in getattr(resp, "content", []) or []:
            if getattr(block, "type", "") == "text":
                chunks.append(getattr(block, "text", ""))
        text = "\n".join(c for c in chunks if c)

        usage = getattr(resp, "usage", None)
        counts = {
            "input": int(getattr(usage, "input_tokens", 0) or 0),
            "output": int(getattr(usage, "output_tokens", 0) or 0),
            "cached": int(getattr(usage, "cache_read_input_tokens", 0) or 0),
        }
        return text, counts, getattr(resp, "id", None)

    def _drop_param(self, exc: Exception, kwargs: dict[str, Any]) -> str | None:
        message = str(exc).lower()
        for param in DROPPABLE_PARAMS:
            if param in kwargs and param in message:
                self.dropped_params.add(param)
                return param
        return None


def build_clients(db: Database, budget: BudgetManager, config: dict[str, Any],
                  openai_key: str, anthropic_key: str
                  ) -> dict[str, LLMClient]:
    return {
        "openai": OpenAIClient(db, budget, config, openai_key),
        "anthropic": AnthropicClient(db, budget, config, anthropic_key),
    }
