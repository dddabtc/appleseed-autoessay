"""Multi-provider chat-completion client with sequential fallback.

Each ``chat_completion`` call walks the configured provider chain
(see ``autoessay.config.get_llm_providers``) in order. The first
provider returning HTTP 200 wins; transient failures advance to the
next provider in the chain.

Failure classification:

- 5xx / 429 / connect-error / timeout / unparseable JSON body
  → transient; try next provider.
- 401 / 403
  → also transient because each provider has its own bearer token,
  so an auth failure on rightcode does not predict an auth failure
  on apiport. Only abort if every provider in the chain returns 401.
- 400 / 404 / 422
  → caller error (bad request shape / unknown model / schema
  violation). Aborts the entire chain immediately because the same
  request body would fail the same way on the next provider.

The caller-facing ``model`` argument becomes audit metadata only
(``requested_model``); the actual model sent to each provider is
the per-provider ``LLMProviderSpec.model``. This lets a single
caller (e.g. ``settings.one_api_model = 'gpt-5.4-mini'``) be served
by a heterogeneous chain (rightcode/apiport via OpenAI gpt-5.4-mini,
minimax via MiniMax-M2.7).

The ``retries`` argument is the number of *additional full-chain
rounds* to attempt beyond the first; ``retries=0`` (the default
used by the harness) means each provider is tried exactly once.
This deliberately differs from the legacy single-provider semantic
where ``retries`` was per-provider — same-provider retries are now
useless because the chain itself is the retry mechanism.
"""

import asyncio
import contextlib
import json
import re
from collections.abc import Sequence
from typing import Any

import httpx

from autoessay.config import LLMProviderSpec, get_llm_providers


class JSONStrictRetryable(Exception):
    """Raised internally when a 200 response carries malformed JSON
    in the ``content`` field while ``validate_json_content=True``.

    Treated by the provider chain as a transient error (advances to
    the next provider / round). If the chain exhausts without a
    valid-JSON response, the last instance bubbles up to the caller
    as a normal exception (no special handling).
    """

    def __init__(self, *, provider: str, content: str, cause: Exception) -> None:
        # Truncate content to 240 chars in the message to avoid
        # writing the entire malformed payload into logs while still
        # giving operators enough context to recognize the failure
        # mode (e.g. "model started with prose, then JSON").
        snippet = content[:240]
        super().__init__(
            f"provider {provider!r} returned 200 with content that "
            f"failed json.loads ({cause}). content[:240]={snippet!r}",
        )
        self.provider = provider
        self.content = content
        self.cause = cause


_NO_REASONING_SUFFIX = (
    "Do not include any <think>, <thinking>, or <thought> reasoning blocks in your response."
)
_REASONING_BLOCK_RE = re.compile(
    r"<(?P<tag>think|thinking|thought)\b[^>]*>(?P<reasoning>.*?)</(?P=tag)>",
    re.IGNORECASE | re.DOTALL,
)

# Status codes that indicate a per-provider problem (transient or
# auth) where falling through to the next provider may succeed.
_TRANSIENT_STATUSES = frozenset({401, 403, 408, 429})

# Status codes that indicate a caller-side problem (bad request,
# unknown model, etc) where every provider would fail the same way.
_ABORT_STATUSES = frozenset({400, 404, 422})


class LLMClient:
    def __init__(
        self,
        *,
        providers: Sequence[LLMProviderSpec] | None = None,
        # Legacy single-provider kwargs (kept for unit-test
        # convenience and any out-of-tree caller that constructed
        # LLMClient directly with a base URL + token before the
        # multi-provider chain landed). When ``providers`` is given
        # these are ignored. When ``providers`` is None and at least
        # one of these is set, a one-element chain is synthesized.
        base_url: str | None = None,
        token: str | None = None,
        model: str | None = None,
        http_client: httpx.AsyncClient | None = None,
        backoff_seconds: float = 0.25,
        # PR-369 P2-4 (codex review): expose a per-request timeout
        # override. Streaming Stage B (gpt-5.5 round-0 holistic
        # rewrite) needs 900s — longer than the default 180s used by
        # non-streaming critic / drafter calls. Without this kwarg
        # the caller's ``httpx.AsyncClient(timeout=900)`` was silently
        # overridden by ``self._timeout_seconds`` at every ``stream(
        # timeout=...)`` and ``post(timeout=...)`` call site.
        timeout_seconds: float | None = None,
    ) -> None:
        from autoessay.config import get_settings

        settings = get_settings()
        if providers is None and (base_url is not None or token is not None):
            providers = [
                LLMProviderSpec(
                    name="legacy",
                    base_url=base_url if base_url is not None else settings.one_api_base_url,
                    api_key=token if token is not None else settings.one_api_token,
                    model=model if model is not None else settings.one_api_model,
                ),
            ]
        # Snapshot the provider list at construction so a mid-call
        # config reload cannot reshuffle the chain partway through.
        # If no providers configured, fall back to env via
        # get_llm_providers().
        self._providers = list(providers) if providers is not None else list(get_llm_providers())
        if not self._providers:
            raise RuntimeError(
                "No LLM providers configured. Set AUTOESSAY_LLM_PROVIDERS or ONE_API_* env vars.",
            )
        self._owns_client = http_client is None
        # No base_url on the shared client — each request targets a
        # different provider URL.
        self._timeout_seconds = (
            float(timeout_seconds)
            if timeout_seconds is not None
            else float(settings.llm_request_timeout_seconds)
        )
        self._client = http_client or httpx.AsyncClient(timeout=self._timeout_seconds)
        self._backoff_seconds = backoff_seconds

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def chat_completion(
        self,
        messages: Sequence[dict[str, str]],
        model: str,
        temperature: float,
        max_tokens: int = 4000,
        retries: int = 0,
        response_format: dict[str, object] | None = None,
        force_no_reasoning: bool = False,
        validate_json_content: bool = False,
        stream: bool = False,
    ) -> dict[str, Any]:
        """Walk the provider chain until one succeeds.

        ``validate_json_content``: when true, after extracting the
        cleaned content from a 200 response, also attempt
        ``json.loads(content)``. A JSON parse failure is treated as a
        transient error (sleep_backoff + continue to next provider /
        round) rather than a successful return. This closes the gap
        where a provider returns 200 with malformed JSON for strict-
        JSON agents (proposal/critic/integrity) — without this flag
        the chain would return that bad content and the caller would
        need its own retry loop, which would re-hit the same provider
        first. Default false preserves behavior for non-strict-JSON
        callers (drafter / stylist / etc.).

        ``stream``: when true, send ``stream=True`` in the request body
        and parse the SSE response (``text/event-stream``). First
        byte arrives in <5s so Cloudflare edge timeouts (which fire
        on 100-125s of upstream silence) do not 524 the connection on
        long gpt-5.5 reasoning tasks. Return shape is identical to
        non-stream calls; per-chunk ``delta.content`` is accumulated
        into ``content`` and the final chunk's ``usage`` (if present)
        is preserved. Mid-stream drops are treated as transient and
        advance to the next provider; partial output is discarded.
        2026-05-12 round-0 v2 D+ work introduced this path.
        """
        prepared_messages = (
            _messages_with_no_reasoning_suffix(messages) if force_no_reasoning else list(messages)
        )
        prepared_messages = _ensure_json_keyword_in_user_message(
            prepared_messages,
            response_format,
        )
        rounds = max(1, retries + 1)

        last_response: httpx.Response | None = None
        last_exception: Exception | None = None

        for round_idx in range(rounds):
            for provider_idx, provider in enumerate(self._providers):
                payload: dict[str, object] = {
                    "model": provider.model,
                    "messages": prepared_messages,
                    "temperature": temperature,
                    "max_tokens": max_tokens,
                }
                if response_format is not None:
                    payload["response_format"] = response_format
                if stream:
                    payload["stream"] = True

                url = _build_chat_url(provider.base_url)
                headers = {"Authorization": f"Bearer {provider.api_key}"}
                if stream:
                    headers["Accept"] = "text/event-stream"

                if stream:
                    stream_result = await self._stream_one_provider(
                        url=url,
                        headers=headers,
                        payload=payload,
                        provider=provider,
                        validate_json_content=validate_json_content,
                    )
                    if isinstance(stream_result, dict):
                        return stream_result
                    # _stream_one_provider returns a (None, exception) tuple
                    # when the provider fails transiently; advance to next.
                    last_exception = stream_result
                    await self._sleep_backoff(round_idx, provider_idx)
                    continue

                try:
                    response = await self._client.post(
                        url,
                        headers=headers,
                        json=payload,
                        timeout=self._timeout_seconds,
                    )
                except httpx.TimeoutException as exc:
                    last_exception = exc
                    await self._sleep_backoff(round_idx, provider_idx)
                    continue
                except (httpx.ConnectError, httpx.NetworkError) as exc:
                    last_exception = exc
                    await self._sleep_backoff(round_idx, provider_idx)
                    continue

                status = response.status_code
                last_response = response

                if status == 200:
                    try:
                        data = response.json()
                    except json.JSONDecodeError as exc:
                        # Treat unparseable body as transient.
                        last_exception = exc
                        await self._sleep_backoff(round_idx, provider_idx)
                        continue
                    raw_content = _extract_content(data)
                    clean_content, reasoning_text = _strip_reasoning_tags(raw_content)
                    if validate_json_content:
                        try:
                            json.loads(clean_content)
                        except json.JSONDecodeError as exc:
                            # Provider responded 200 but content is not
                            # parseable JSON. Strict-JSON callers
                            # (proposal/critic/integrity) opt into this
                            # via ``validate_json_content=True``; treat
                            # like a transient transport error so the
                            # chain advances to the next provider.
                            last_exception = JSONStrictRetryable(
                                provider=provider.name,
                                content=clean_content,
                                cause=exc,
                            )
                            await self._sleep_backoff(round_idx, provider_idx)
                            continue
                    return {
                        "content": clean_content,
                        "reasoning_text": reasoning_text,
                        "usage": data.get("usage", {}),
                        "raw_content": raw_content,
                        "finish_reason": _extract_finish_reason(data),
                        "provider_used": provider.name,
                        "provider_model": provider.model,
                    }

                if status in _ABORT_STATUSES:
                    # Caller-side error — same request body fails the
                    # same way on every provider, so no point trying.
                    response.raise_for_status()

                if status >= 500 or status in _TRANSIENT_STATUSES:
                    last_exception = httpx.HTTPStatusError(
                        f"provider {provider.name!r} returned {status}",
                        request=response.request,
                        response=response,
                    )
                    await self._sleep_backoff(round_idx, provider_idx)
                    continue

                # Anything else (300-399, unusual 4xx like 405/406/410):
                # treat conservatively as caller-side and abort.
                response.raise_for_status()

        # Chain exhausted without success.
        if last_response is not None:
            last_response.raise_for_status()
        if last_exception is not None:
            raise last_exception
        raise RuntimeError("LLM chain exhausted without making any request")

    async def _stream_one_provider(
        self,
        *,
        url: str,
        headers: dict[str, str],
        payload: dict[str, object],
        provider: LLMProviderSpec,
        validate_json_content: bool,
    ) -> dict[str, Any] | Exception:
        """Send a single streaming request to one provider; accumulate
        delta chunks; return the same dict shape as the non-stream path
        on success, or an Exception on transient failure (caller
        advances to next provider).

        Streaming response format (OpenAI-compatible SSE):

            data: {"choices":[{"delta":{"content":"abc"}}], ...}
            data: {"choices":[{"delta":{"content":"def"}}], "usage": {...}}
            data: [DONE]

        Mid-stream drops or non-200 status are treated as transient
        and reported back to the chain. Partial output is discarded
        per codex AGREE-WITH-AMENDMENTS 2026-05-12 D+ ("completed-only
        commit") — never apply half a response to downstream agents.
        """
        try:
            async with self._client.stream(
                "POST",
                url,
                headers=headers,
                json=payload,
                timeout=self._timeout_seconds,
            ) as resp:
                if resp.status_code in _ABORT_STATUSES:
                    body = b""
                    with contextlib.suppress(Exception):
                        body = await resp.aread()
                    resp.raise_for_status()  # raises HTTPStatusError
                    return RuntimeError(  # unreachable
                        f"abort status {resp.status_code}: {body[:200]!r}"
                    )
                if resp.status_code != 200:
                    # Read body for diagnostic + treat as transient.
                    body = b""
                    with contextlib.suppress(Exception):
                        body = await resp.aread()
                    return httpx.HTTPStatusError(
                        (
                            f"provider {provider.name!r} stream returned "
                            f"{resp.status_code}: {body[:200]!r}"
                        ),
                        request=resp.request,
                        response=resp,
                    )

                content_parts: list[str] = []
                reasoning_parts: list[str] = []
                usage: dict[str, Any] = {}
                raw_chunks: list[str] = []
                finish_reason: str | None = None
                done = False
                async for raw_line in resp.aiter_lines():
                    if not raw_line:
                        continue
                    if not raw_line.startswith("data: "):
                        continue
                    payload_str = raw_line[len("data: ") :].strip()
                    if payload_str == "[DONE]":
                        done = True
                        break
                    raw_chunks.append(payload_str)
                    try:
                        chunk = json.loads(payload_str)
                    except json.JSONDecodeError:
                        continue
                    choices = chunk.get("choices") or []
                    if choices:
                        choice = choices[0]
                        if isinstance(choice, dict):
                            raw_finish_reason = choice.get("finish_reason")
                            if isinstance(raw_finish_reason, str):
                                finish_reason = raw_finish_reason
                            raw_delta = choice.get("delta") or {}
                            delta = raw_delta if isinstance(raw_delta, dict) else {}
                        else:
                            delta = {}
                        piece = delta.get("content") or ""
                        if piece:
                            content_parts.append(str(piece))
                        # Some providers stream reasoning content
                        # alongside visible content; collect it but do
                        # not include in the user-facing ``content``.
                        reasoning_piece = delta.get("reasoning_content") or ""
                        if reasoning_piece:
                            reasoning_parts.append(str(reasoning_piece))
                    chunk_usage = chunk.get("usage")
                    if isinstance(chunk_usage, dict):
                        usage = chunk_usage
        except httpx.TimeoutException as exc:
            return exc
        except (httpx.ConnectError, httpx.NetworkError, httpx.RemoteProtocolError) as exc:
            return exc

        if not done:
            # Stream ended without [DONE] — server closed connection
            # before completing the response. Treat as transient.
            return httpx.RemoteProtocolError(
                f"provider {provider.name!r} stream closed before [DONE]"
            )

        raw_content = "".join(content_parts)
        clean_content, stripped_reasoning = _strip_reasoning_tags(raw_content)
        reasoning_text = stripped_reasoning or "".join(reasoning_parts)
        if validate_json_content:
            try:
                json.loads(clean_content)
            except json.JSONDecodeError as exc:
                return JSONStrictRetryable(
                    provider=provider.name,
                    content=clean_content,
                    cause=exc,
                )
        return {
            "content": clean_content,
            "reasoning_text": reasoning_text,
            "usage": usage,
            "raw_content": raw_content,
            "finish_reason": finish_reason,
            "provider_used": provider.name,
            "provider_model": provider.model,
        }

    async def _sleep_backoff(self, round_idx: int, provider_idx: int) -> None:
        # Tiny escalating sleep to avoid hammering a flapping
        # gateway. Round 0 / provider 0 sleeps zero so the common
        # case stays fast; later attempts add small backoff. Capped
        # at ~2s so the harness doesn't time out waiting on the
        # whole chain.
        scale = round_idx * len(self._providers) + provider_idx
        if scale == 0:
            return
        delay = min(self._backoff_seconds * (2 ** min(scale - 1, 3)), 2.0)
        await asyncio.sleep(delay)


async def chat_completion(
    messages: Sequence[dict[str, str]],
    model: str,
    temperature: float,
    max_tokens: int = 4000,
    retries: int = 0,
    response_format: dict[str, object] | None = None,
    force_no_reasoning: bool = False,
    validate_json_content: bool = False,
) -> dict[str, Any]:
    client = LLMClient()
    try:
        return await client.chat_completion(
            messages,
            model,
            temperature,
            max_tokens,
            retries,
            response_format=response_format,
            force_no_reasoning=force_no_reasoning,
            validate_json_content=validate_json_content,
        )
    finally:
        await client.aclose()


def _build_chat_url(base_url: str) -> str:
    """Append ``/v1/chat/completions`` to a provider base URL.

    Tolerates trailing slashes and URLs that already include ``/v1``.
    """
    trimmed = base_url.rstrip("/")
    if trimmed.endswith("/v1"):
        return f"{trimmed}/chat/completions"
    return f"{trimmed}/v1/chat/completions"


def _extract_content(data: dict[str, Any]) -> str:
    choices = data.get("choices", [])
    if not choices:
        return ""
    first = choices[0]
    if not isinstance(first, dict):
        return ""
    message = first.get("message", {})
    if not isinstance(message, dict):
        return ""
    content = message.get("content", "")
    return content if isinstance(content, str) else ""


def _extract_finish_reason(data: dict[str, Any]) -> str | None:
    choices = data.get("choices", [])
    if not choices:
        return None
    first = choices[0]
    if not isinstance(first, dict):
        return None
    finish_reason = first.get("finish_reason")
    return finish_reason if isinstance(finish_reason, str) else None


def _strip_reasoning_tags(content: str) -> tuple[str, str]:
    """Return content with complete reasoning-tag blocks removed.

    OpenAI reasoning models keep reasoning hidden and report it through
    usage.completion_tokens_details.reasoning_tokens; production checks showed
    rightcode/gpt-5.5 returning clean "OK" content while MiniMax-M2.7 returned
    a <think> block inside assistant content. Stripping the OpenAI-compatible
    <think>/<thinking>/<thought> convention at this client boundary keeps JSON
    parsing stable for agents, preserves raw_content for audit, and avoids
    leaking provider-specific quirks into callers. Unclosed tags are left
    untouched so a literal "<think>" string does not corrupt the response.
    """
    reasoning_blocks: list[str] = []

    def replace(match: re.Match[str]) -> str:
        reasoning_blocks.append(match.group("reasoning").strip())
        return ""

    clean_content = _REASONING_BLOCK_RE.sub(replace, content).strip()
    return clean_content, "\n\n".join(reasoning_blocks)


def _messages_with_no_reasoning_suffix(
    messages: Sequence[dict[str, str]],
) -> list[dict[str, str]]:
    copied_messages = [dict(message) for message in messages]
    for message in copied_messages:
        if message.get("role") == "system":
            content = message.get("content", "")
            message["content"] = f"{content.rstrip()}\n\n{_NO_REASONING_SUFFIX}".strip()
            return copied_messages
    return [{"role": "system", "content": _NO_REASONING_SUFFIX}, *copied_messages]


_JSON_KEYWORD_RE = re.compile(r"json", re.IGNORECASE)
_JSON_SUFFIX = "(Respond with strict JSON.)"


def _ensure_json_keyword_in_user_message(
    messages: Sequence[dict[str, str]],
    response_format: dict[str, object] | None,
) -> list[dict[str, str]]:
    # OpenAI-compat upstreams enforce that when ``response_format`` is
    # ``json_object``, at least one **user** message content must
    # mention "json" — system prompts don't count on some gateways
    # (e.g. rightcode 2026-05-13). Inject a tiny suffix when missing so
    # all 20+ agent call sites stay compliant without per-prompt edits.
    if response_format is None:
        return [dict(m) for m in messages]
    if response_format.get("type") != "json_object":
        return [dict(m) for m in messages]
    copied = [dict(m) for m in messages]
    for message in copied:
        if message.get("role") != "user":
            continue
        content = message.get("content", "")
        if isinstance(content, str) and _JSON_KEYWORD_RE.search(content):
            return copied
    for message in reversed(copied):
        if message.get("role") == "user":
            content = message.get("content", "")
            if isinstance(content, str):
                message["content"] = f"{content.rstrip()}\n\n{_JSON_SUFFIX}".strip()
            return copied
    copied.append({"role": "user", "content": _JSON_SUFFIX})
    return copied
