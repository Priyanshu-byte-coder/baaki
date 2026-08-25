"""Model client for the tail stage.

Provider-agnostic in shape, Groq by default because that is what the key pool
is for. The client's only job is to return parsed JSON or raise -- it makes no
decisions about reconciliation, and nothing downstream trusts its output until
:mod:`baaki.match.guardrails` has checked it.

Two failure modes are handled explicitly rather than left to surface as parse
errors:

``TruncatedCompletion``
    The model hit its token ceiling mid-object. The JSON is invalid because it
    is incomplete, not because the model was confused, and retrying with the
    same budget will fail identically. Reasoning models bill chain-of-thought
    against ``max_tokens``, which is what makes this common.

``UngroundedOutput``
    Raised by the guardrails, not here, but worth naming together: a
    syntactically perfect object that references records which do not exist.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass

from .budget import KeyPool, estimate_tokens

log = logging.getLogger(__name__)


class TruncatedCompletion(RuntimeError):
    """The model stopped because it ran out of tokens, not because it finished."""


class ModelUnavailable(RuntimeError):
    """Every key in the pool failed. The caller must degrade, not guess."""


@dataclass(slots=True)
class Completion:
    text: str
    model: str
    prompt_tokens: int
    completion_tokens: int

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def extract_json(raw: str) -> str:
    """Pull a JSON object out of a model response.

    Models wrap objects in prose or code fences even when told not to. This
    strips a fence if present, otherwise takes the outermost balanced braces.
    """
    raw = (raw or "").strip()
    fenced = _FENCE.search(raw)
    if fenced:
        raw = fenced.group(1).strip()

    start = raw.find("{")
    if start == -1:
        return raw

    depth = 0
    in_string = False
    escaped = False
    for i, ch in enumerate(raw[start:], start):
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return raw[start : i + 1]
    # Unbalanced: the object never closed, which means it was cut off.
    raise TruncatedCompletion("response ended inside an unterminated JSON object")


class LLMClient:
    """Thin async client with key rotation, TPM budgeting and bounded retries."""

    def __init__(
        self,
        *,
        keys: list[str] | None = None,
        model: str | None = None,
        tpm_limit: int | None = None,
        max_retries: int | None = None,
    ) -> None:
        raw_keys = keys if keys is not None else _keys_from_env()
        self.model = model or os.getenv("BAAKI_TAIL_MODEL", "openai/gpt-oss-120b")
        self.tpm_limit = tpm_limit or int(os.getenv("BAAKI_TPM_LIMIT", "8000"))
        self.max_retries = max_retries or int(os.getenv("BAAKI_MAX_RETRIES", "4"))
        self.pool = KeyPool(raw_keys, self.tpm_limit) if raw_keys else None

    @property
    def available(self) -> bool:
        """Whether a model can be reached at all.

        Baaki must run to completion without one. When this is False the tail
        stage reports its residue as unresolved rather than inventing answers,
        and the run is still valid -- just less explained.
        """
        return self.pool is not None

    async def complete(
        self, system: str, user: str, *, max_tokens: int = 1200, temperature: float = 0.0
    ) -> Completion:
        if self.pool is None:
            raise ModelUnavailable("no API keys configured")

        from groq import AsyncGroq  # imported lazily so the offline path needs no SDK

        estimate = estimate_tokens(system) + estimate_tokens(user) + max_tokens
        last_error: Exception | None = None

        for attempt in range(self.max_retries):
            key, reservation = await self.pool.acquire(self.model, estimate)
            bucket = self.pool.bucket(key, self.model)
            try:
                client = AsyncGroq(api_key=key)
                response = await client.chat.completions.create(
                    model=self.model,
                    messages=[
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    max_tokens=max_tokens,
                    temperature=temperature,
                )
                choice = response.choices[0]
                usage = getattr(response, "usage", None)
                if usage is not None:
                    await bucket.reconcile(reservation, usage.total_tokens)

                if choice.finish_reason == "length":
                    raise TruncatedCompletion(
                        f"{self.model} hit the {max_tokens}-token ceiling before closing "
                        f"its response"
                    )

                return Completion(
                    text=choice.message.content or "",
                    model=self.model,
                    prompt_tokens=getattr(usage, "prompt_tokens", 0) if usage else 0,
                    completion_tokens=getattr(usage, "completion_tokens", 0) if usage else 0,
                )

            except TruncatedCompletion:
                # Deterministic under the same budget; retrying is pure cost.
                raise

            except Exception as exc:  # noqa: BLE001 - provider SDKs raise broadly
                last_error = exc
                if _is_rate_limit(exc):
                    # The server's accounting wins. Block this key so the next
                    # attempt routes elsewhere instead of straight back here.
                    await bucket.penalise(_retry_after_seconds(exc, attempt))
                else:
                    await bucket.release(reservation)
                    await asyncio.sleep(min(2.0**attempt, 8.0))
                log.warning(
                    "tail model call failed on attempt %d/%d: %s",
                    attempt + 1,
                    self.max_retries,
                    exc,
                )

        raise ModelUnavailable(f"all {self.max_retries} attempts failed: {last_error}")

    async def complete_json(self, system: str, user: str, *, max_tokens: int = 1200) -> dict:
        completion = await self.complete(system, user, max_tokens=max_tokens)
        return json.loads(extract_json(completion.text))


def _keys_from_env() -> list[str]:
    raw = os.getenv("BAAKI_GROQ_API_KEYS") or os.getenv("GROQ_API_KEYS") or ""
    keys = [k.strip() for k in re.split(r"[,\s]+", raw) if k.strip()]
    single = (os.getenv("BAAKI_GROQ_API_KEY") or os.getenv("GROQ_API_KEY") or "").strip()
    if single and single not in keys:
        keys.insert(0, single)
    return keys


def _is_rate_limit(exc: Exception) -> bool:
    status = getattr(exc, "status_code", None)
    if status == 429:
        return True
    text = str(exc).lower()
    return "rate limit" in text or "429" in text or "quota" in text


def _retry_after_seconds(exc: Exception, attempt: int) -> float:
    """Honour a Retry-After header when the provider sends one."""
    headers = getattr(getattr(exc, "response", None), "headers", None) or {}
    raw = headers.get("retry-after") or headers.get("Retry-After")
    if raw:
        try:
            return max(0.5, float(raw))
        except (TypeError, ValueError):
            pass
    match = re.search(r"try again in ([\d.]+)s", str(exc), re.IGNORECASE)
    if match:
        return max(0.5, float(match.group(1)))
    return min(2.0**attempt, 30.0)
