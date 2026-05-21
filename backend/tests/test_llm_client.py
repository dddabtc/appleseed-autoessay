import json
import os

import httpx
import pytest

from autoessay.config import LLMProviderSpec
from autoessay.llm_client import LLMClient, _strip_reasoning_tags

# ---------------------------------------------------------------------------
# Reasoning-tag stripping — pure-function tests retained from before the
# multi-provider refactor.
# ---------------------------------------------------------------------------


def test_strip_reasoning_tags_basic() -> None:
    content, reasoning_text = _strip_reasoning_tags("<think>foo bar</think>\n\nOK")

    assert content == "OK"
    assert reasoning_text == "foo bar"


def test_strip_reasoning_tags_thinking_variant() -> None:
    content, reasoning_text = _strip_reasoning_tags("<THINKING>hidden\nnote</THINKING>OK")

    assert content == "OK"
    assert reasoning_text == "hidden\nnote"


def test_strip_reasoning_tags_thought_variant() -> None:
    content, reasoning_text = _strip_reasoning_tags("<thought>hidden note</thought>OK")

    assert content == "OK"
    assert reasoning_text == "hidden note"


def test_strip_reasoning_tags_multiple() -> None:
    content, reasoning_text = _strip_reasoning_tags(
        "<think>first</think>\nOK\n<think>second</think>",
    )

    assert content == "OK"
    assert reasoning_text == "first\n\nsecond"


def test_strip_reasoning_tags_unclosed() -> None:
    content, reasoning_text = _strip_reasoning_tags("<think>foo without close")

    assert content == "<think>foo without close"
    assert reasoning_text == ""


def test_strip_reasoning_tags_no_tags() -> None:
    content, reasoning_text = _strip_reasoning_tags("plain answer")

    assert content == "plain answer"
    assert reasoning_text == ""


# ---------------------------------------------------------------------------
# Single-provider behaviour (legacy-style construction kept for back-compat).
# ---------------------------------------------------------------------------


async def test_chat_completion_retries_500_and_returns_content() -> None:
    """Two full-chain rounds against a 1-provider chain == 2 attempts."""
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        assert request.headers["authorization"] == "Bearer test-token"
        if calls == 1:
            return httpx.Response(500, json={"error": "temporary"})
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "final answer"}}],
                "usage": {"total_tokens": 12},
            },
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
    ) as http_client:
        client = LLMClient(
            base_url="https://one-api.test",
            token="test-token",
            model="test-model",
            http_client=http_client,
            backoff_seconds=0,
        )

        response = await client.chat_completion(
            [{"role": "user", "content": "hello"}],
            "test-model",
            0.2,
            100,
            retries=1,
        )

    assert calls == 2
    assert response == {
        "content": "final answer",
        "reasoning_text": "",
        "usage": {"total_tokens": 12},
        "raw_content": "final answer",
        "finish_reason": None,
        "provider_used": "legacy",
        "provider_model": "test-model",
    }


async def test_chat_completion_strips_think() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "<think>x</think>OK"}}],
                "usage": {"total_tokens": 12},
            },
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
    ) as http_client:
        client = LLMClient(
            base_url="https://one-api.test",
            token="test-token",
            model="test-model",
            http_client=http_client,
        )

        response = await client.chat_completion(
            [{"role": "user", "content": "hello"}],
            "test-model",
            0.2,
            100,
        )

    assert response == {
        "content": "OK",
        "reasoning_text": "x",
        "usage": {"total_tokens": 12},
        "raw_content": "<think>x</think>OK",
        "finish_reason": None,
        "provider_used": "legacy",
        "provider_model": "test-model",
    }


async def test_default_max_tokens_4000() -> None:
    seen_payload: dict[str, object] | None = None

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal seen_payload
        seen_payload = json.loads(request.content.decode("utf-8"))
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "OK"}}]},
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
    ) as http_client:
        client = LLMClient(
            base_url="https://one-api.test",
            token="test-token",
            model="test-model",
            http_client=http_client,
        )

        await client.chat_completion(
            [{"role": "user", "content": "hello"}],
            "test-model",
            0.2,
        )

    assert seen_payload is not None
    assert seen_payload["max_tokens"] == 4000


async def test_force_no_reasoning_adds_system_suffix() -> None:
    seen_payload: dict[str, object] | None = None

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal seen_payload
        seen_payload = json.loads(request.content.decode("utf-8"))
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "OK"}}]},
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
    ) as http_client:
        client = LLMClient(
            base_url="https://one-api.test",
            token="test-token",
            model="test-model",
            http_client=http_client,
        )

        await client.chat_completion(
            [{"role": "user", "content": "hello"}],
            "test-model",
            0.2,
            force_no_reasoning=True,
        )

    assert seen_payload is not None
    sent_messages = seen_payload["messages"]
    assert isinstance(sent_messages, list)
    assert sent_messages[0] == {
        "role": "system",
        "content": (
            "Do not include any <think>, <thinking>, or <thought> reasoning blocks in your "
            "response."
        ),
    }


async def test_chat_completion_uses_configured_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    from autoessay.config import get_settings

    monkeypatch.setenv("AUTOESSAY_LLM_REQUEST_TIMEOUT_SECONDS", "180")
    get_settings.cache_clear()
    seen_timeout: dict[str, float] | None = None

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal seen_timeout
        seen_timeout = request.extensions["timeout"]
        raise httpx.ReadTimeout("timed out", request=request)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
    ) as http_client:
        client = LLMClient(
            base_url="https://one-api.test",
            token="test-token",
            model="test-model",
            http_client=http_client,
        )

        with pytest.raises(httpx.ReadTimeout):
            await client.chat_completion(
                [{"role": "user", "content": "hello"}],
                "test-model",
                0.2,
                100,
            )

    assert seen_timeout is not None
    assert seen_timeout["connect"] == 180.0
    assert seen_timeout["read"] == 180.0
    assert seen_timeout["write"] == 180.0
    assert seen_timeout["pool"] == 180.0


# ---------------------------------------------------------------------------
# Multi-provider chain — fallback semantics. Each test wires N providers via
# the explicit ``providers=`` constructor and inspects the URL of each
# request to identify which provider served it.
# ---------------------------------------------------------------------------


def _three_providers() -> list[LLMProviderSpec]:
    return [
        LLMProviderSpec(
            name="primary",
            base_url="https://primary.test",
            api_key="key-primary",
            model="primary-model",
        ),
        LLMProviderSpec(
            name="secondary",
            base_url="https://secondary.test",
            api_key="key-secondary",
            model="secondary-model",
        ),
        LLMProviderSpec(
            name="tertiary",
            base_url="https://tertiary.test",
            api_key="key-tertiary",
            model="tertiary-model",
        ),
    ]


def _ok_response(content: str = "answer") -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "choices": [{"message": {"content": content}}],
            "usage": {"total_tokens": 7},
        },
    )


async def _run_with_handler(
    handler,
    providers: list[LLMProviderSpec] | None = None,
    *,
    retries: int = 0,
    response_format: dict[str, object] | None = None,
    validate_json_content: bool = False,
):
    """Helper to construct a LLMClient with a MockTransport handler."""
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = LLMClient(
            providers=providers or _three_providers(),
            http_client=http_client,
            backoff_seconds=0,
        )
        return await client.chat_completion(
            [{"role": "user", "content": "hello"}],
            "logical-model",
            0.2,
            100,
            retries=retries,
            response_format=response_format,
            validate_json_content=validate_json_content,
        )


async def test_first_provider_serves_when_healthy() -> None:
    seen: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return _ok_response("primary OK")

    response = await _run_with_handler(handler)

    assert seen == ["https://primary.test/v1/chat/completions"]
    assert response["provider_used"] == "primary"
    assert response["provider_model"] == "primary-model"
    assert response["content"] == "primary OK"


async def test_falls_through_to_second_provider_on_5xx() -> None:
    seen: list[tuple[str, str]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append((str(request.url), request.headers["authorization"]))
        if "primary.test" in str(request.url):
            return httpx.Response(503, json={"error": "down"})
        return _ok_response("from secondary")

    response = await _run_with_handler(handler)

    assert len(seen) == 2
    assert seen[0][0].startswith("https://primary.test/")
    assert seen[0][1] == "Bearer key-primary"
    assert seen[1][0].startswith("https://secondary.test/")
    assert seen[1][1] == "Bearer key-secondary"
    assert response["provider_used"] == "secondary"
    assert response["provider_model"] == "secondary-model"
    assert response["content"] == "from secondary"


async def test_falls_through_on_429_rate_limit() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if "primary.test" in str(request.url):
            return httpx.Response(429, json={"error": "rate-limited"})
        return _ok_response("from secondary")

    response = await _run_with_handler(handler)

    assert response["provider_used"] == "secondary"


async def test_falls_through_on_per_provider_401() -> None:
    """401 from one provider does NOT predict 401 from the next.

    Each provider has its own bearer token. A 401 means *that
    provider's* key is bad/expired/revoked; the chain may still
    succeed via a sibling with a different key. This intentionally
    diverges from a naive 'no fallback on 4xx' rule.
    """

    async def handler(request: httpx.Request) -> httpx.Response:
        if "primary.test" in str(request.url):
            return httpx.Response(401, json={"error": "unauthorized"})
        return _ok_response("from secondary")

    response = await _run_with_handler(handler)
    assert response["provider_used"] == "secondary"


async def test_falls_through_on_connect_error() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if "primary.test" in str(request.url):
            raise httpx.ConnectError("refused", request=request)
        return _ok_response("from secondary")

    response = await _run_with_handler(handler)
    assert response["provider_used"] == "secondary"


async def test_falls_through_on_timeout() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if "primary.test" in str(request.url):
            raise httpx.ReadTimeout("slow", request=request)
        return _ok_response("from secondary")

    response = await _run_with_handler(handler)
    assert response["provider_used"] == "secondary"


async def test_aborts_on_400_bad_request_no_fallback() -> None:
    """400 means the request body is malformed; the same body would
    fail the same way on every provider, so no fallback."""
    seen: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(400, json={"error": "bad request"})

    with pytest.raises(httpx.HTTPStatusError):
        await _run_with_handler(handler)
    assert len(seen) == 1
    assert seen[0].startswith("https://primary.test/")


async def test_aborts_on_404_unknown_model_no_fallback() -> None:
    seen: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(404, json={"error": "not found"})

    with pytest.raises(httpx.HTTPStatusError):
        await _run_with_handler(handler)
    assert len(seen) == 1


async def test_aborts_on_422_no_fallback() -> None:
    seen: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(422, json={"error": "unprocessable"})

    with pytest.raises(httpx.HTTPStatusError):
        await _run_with_handler(handler)
    assert len(seen) == 1


async def test_all_providers_5xx_raises_last_error() -> None:
    """When the entire chain is unhealthy, surface the final HTTPStatusError."""
    seen: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(503, json={"error": "down"})

    with pytest.raises(httpx.HTTPStatusError):
        await _run_with_handler(handler)
    # All three providers should have been tried before giving up.
    assert len(seen) == 3
    hosts = [str(s).split("/")[2] for s in seen]
    assert hosts == ["primary.test", "secondary.test", "tertiary.test"]


async def test_request_uses_per_provider_model_not_caller_model() -> None:
    """Caller-facing ``model`` arg is audit metadata only; the wire
    payload uses the per-provider ``LLMProviderSpec.model``."""
    seen_payloads: list[dict[str, object]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        seen_payloads.append(body)
        if "primary.test" in str(request.url):
            return httpx.Response(503, json={"error": "down"})
        return _ok_response()

    await _run_with_handler(handler)

    assert seen_payloads[0]["model"] == "primary-model"
    assert seen_payloads[1]["model"] == "secondary-model"


async def test_response_format_relayed_to_provider() -> None:
    seen_payloads: list[dict[str, object]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen_payloads.append(json.loads(request.content.decode("utf-8")))
        return _ok_response()

    await _run_with_handler(
        handler,
        providers=[_three_providers()[0]],
        response_format={"type": "json_object"},
    )

    assert seen_payloads[0]["response_format"] == {"type": "json_object"}


async def test_retries_argument_means_full_chain_rounds() -> None:
    """``retries=N`` issues ``N + 1`` rounds against the full chain.

    With 2 providers and ``retries=1`` we should see at most
    2 * 2 = 4 attempts when every provider 5xxs.
    """
    seen: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(503, json={"error": "down"})

    providers = _three_providers()[:2]

    with pytest.raises(httpx.HTTPStatusError):
        await _run_with_handler(handler, providers=providers, retries=1)

    assert len(seen) == 4
    assert [str(u).split("/")[2] for u in seen] == [
        "primary.test",
        "secondary.test",
        "primary.test",
        "secondary.test",
    ]


async def test_url_normalization_handles_v1_suffix() -> None:
    seen: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return _ok_response()

    providers = [
        LLMProviderSpec(
            name="with-v1",
            base_url="https://example.test/v1",
            api_key="k",
            model="m",
        ),
    ]

    await _run_with_handler(handler, providers=providers)
    assert seen == ["https://example.test/v1/chat/completions"]


async def test_url_normalization_handles_trailing_slash() -> None:
    seen: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return _ok_response()

    providers = [
        LLMProviderSpec(
            name="trailing",
            base_url="https://example.test/",
            api_key="k",
            model="m",
        ),
    ]

    await _run_with_handler(handler, providers=providers)
    assert seen == ["https://example.test/v1/chat/completions"]


# ---------------------------------------------------------------------------
# Config parsing — get_llm_providers JSON / backward-compat behaviour.
# ---------------------------------------------------------------------------


def _reset_settings_cache() -> None:
    from autoessay.config import get_settings

    get_settings.cache_clear()


def test_get_llm_providers_synthesizes_one_from_legacy_env(monkeypatch) -> None:
    monkeypatch.delenv("AUTOESSAY_LLM_PROVIDERS", raising=False)
    monkeypatch.setenv("ONE_API_BASE_URL", "https://one.test")
    monkeypatch.setenv("ONE_API_TOKEN", "tok")
    monkeypatch.setenv("ONE_API_MODEL", "model-X")
    _reset_settings_cache()
    from autoessay.config import get_llm_providers

    chain = get_llm_providers()
    assert len(chain) == 1
    assert chain[0].name == "default"
    assert chain[0].base_url == "https://one.test"
    assert chain[0].api_key == "tok"
    assert chain[0].model == "model-X"


def test_get_llm_providers_parses_json_list(monkeypatch) -> None:
    payload = [
        {
            "name": "rightcode",
            "base_url": "https://www.right.codes/codex",
            "api_key": "sk-rc",
            "model": "gpt-5.4-mini",
        },
        {
            "name": "minimax",
            "base_url": "https://api.minimax.io",
            "api_key": "sk-mm",
            "model": "MiniMax-M2.7",
        },
    ]
    monkeypatch.setenv("AUTOESSAY_LLM_PROVIDERS", json.dumps(payload))
    _reset_settings_cache()
    from autoessay.config import get_llm_providers

    chain = get_llm_providers()
    assert [p.name for p in chain] == ["rightcode", "minimax"]
    assert chain[0].api_key == "sk-rc"
    assert chain[1].model == "MiniMax-M2.7"


def test_get_llm_providers_rejects_invalid_json(monkeypatch) -> None:
    monkeypatch.setenv("AUTOESSAY_LLM_PROVIDERS", "not-json[")
    _reset_settings_cache()
    from autoessay.config import get_llm_providers

    with pytest.raises(ValueError, match="not valid JSON"):
        get_llm_providers()


def test_get_llm_providers_rejects_empty_list(monkeypatch) -> None:
    monkeypatch.setenv("AUTOESSAY_LLM_PROVIDERS", "[]")
    _reset_settings_cache()
    from autoessay.config import get_llm_providers

    with pytest.raises(ValueError, match="non-empty"):
        get_llm_providers()


# ---------------------------------------------------------------------------
# validate_json_content — strict-JSON callers (proposal/critic/integrity etc.)
# opt in so that a provider returning 200 with malformed JSON triggers
# the multi-provider fallback. Without this flag, the chain would
# return the bad content (default behavior preserved for non-JSON callers).
# ---------------------------------------------------------------------------


async def test_validate_json_content_disabled_keeps_bad_json() -> None:
    """When validate_json_content=False (default), 200 OK + bad JSON
    body returns the bad content as-is (no fallback)."""
    seen: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return _ok_response("not json — just prose")

    response = await _run_with_handler(handler)
    assert seen == ["https://primary.test/v1/chat/completions"]
    assert response["content"] == "not json — just prose"
    assert response["provider_used"] == "primary"


async def test_validate_json_content_enabled_falls_through_on_bad_json() -> None:
    """validate_json_content=True: provider 1 returns 200 with malformed
    JSON content → chain advances to provider 2 (which returns valid
    JSON). Without this fix the bad content would be returned."""
    seen: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if "primary" in str(request.url):
            return _ok_response("```json\n{broken")  # parses NOT
        return _ok_response('{"ok": true}')

    response = await _run_with_handler(handler, validate_json_content=True)
    assert len(seen) == 2
    assert "primary" in seen[0]
    assert "secondary" in seen[1]
    assert response["provider_used"] == "secondary"
    assert response["content"] == '{"ok": true}'


async def test_validate_json_content_strips_think_then_validates() -> None:
    """The validator runs against the cleaned content (post
    _strip_reasoning_tags), not the raw payload. A response that's
    valid JSON wrapped in <think>...</think> noise should pass."""
    seen: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return _ok_response('<think>scratch</think>{"ok":1}')

    response = await _run_with_handler(handler, validate_json_content=True)
    assert len(seen) == 1
    assert response["content"] == '{"ok":1}'


async def test_validate_json_content_all_providers_bad_raises() -> None:
    """If every provider returns malformed JSON, the chain raises
    JSONStrictRetryable from the last attempt."""
    seen: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return _ok_response("not json")

    from autoessay.llm_client import JSONStrictRetryable

    with pytest.raises(JSONStrictRetryable):
        await _run_with_handler(handler, validate_json_content=True)
    assert len(seen) == 3  # all 3 providers tried


async def test_validate_json_content_does_not_block_500_fallback() -> None:
    """Pre-existing 5xx fallback path still works alongside the new
    JSON-content validation. Provider 1: 503; provider 2: 200 + JSON."""

    async def handler(request: httpx.Request) -> httpx.Response:
        if "primary" in str(request.url):
            return httpx.Response(503, json={"error": "down"})
        return _ok_response('{"ok": true}')

    response = await _run_with_handler(handler, validate_json_content=True)
    assert response["provider_used"] == "secondary"
    assert response["content"] == '{"ok": true}'


def test_get_llm_providers_rejects_missing_required_field(monkeypatch) -> None:
    monkeypatch.setenv(
        "AUTOESSAY_LLM_PROVIDERS",
        json.dumps([{"name": "x", "base_url": "u", "api_key": "k"}]),  # no "model"
    )
    _reset_settings_cache()
    from autoessay.config import get_llm_providers

    with pytest.raises(ValueError, match="model"):
        get_llm_providers()


@pytest.fixture(autouse=True)
def _restore_settings_cache():
    yield
    _reset_settings_cache()
    # Wipe any test-injected env that might leak between tests.
    for key in ("AUTOESSAY_LLM_PROVIDERS",):
        os.environ.pop(key, None)


# ---------------------------------------------------------------------------
# Streaming (SSE) path — added 2026-05-12 D+ to unblock gpt-5.5 stage B
# rewrites that exceed Cloudflare edge timeouts (100-125s) in non-streaming
# mode. Streaming starts the response within seconds, keeping the
# connection alive throughout multi-minute reasoning runs.
# ---------------------------------------------------------------------------


def _sse_response(chunks: list[str], *, usage: dict[str, int] | None = None) -> httpx.Response:
    """Build an SSE response body that mimics an OpenAI-compatible
    streamed chat completion. ``chunks`` is the sequence of delta
    content strings the model emits; ``usage`` (if given) is attached
    to the final delta chunk so callers can verify it propagates."""
    lines: list[str] = []
    for idx, piece in enumerate(chunks):
        chunk: dict[str, object] = {
            "choices": [{"delta": {"content": piece}, "index": 0}],
        }
        if idx == len(chunks) - 1 and usage is not None:
            chunk["usage"] = usage
        lines.append(f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n")
    lines.append("data: [DONE]\n\n")
    body = "".join(lines).encode("utf-8")
    return httpx.Response(
        200,
        content=body,
        headers={"Content-Type": "text/event-stream"},
    )


async def _run_stream(
    handler,
    providers: list[LLMProviderSpec] | None = None,
    *,
    retries: int = 0,
    response_format: dict[str, object] | None = None,
    validate_json_content: bool = False,
):
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = LLMClient(
            providers=providers or _three_providers(),
            http_client=http_client,
            backoff_seconds=0,
        )
        return await client.chat_completion(
            [{"role": "user", "content": "hello"}],
            "logical-model",
            0.2,
            100,
            retries=retries,
            response_format=response_format,
            validate_json_content=validate_json_content,
            stream=True,
        )


async def test_streaming_accumulates_chunks_and_preserves_usage() -> None:
    saw_stream_in_payload: list[bool] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        saw_stream_in_payload.append(body.get("stream") is True)
        assert request.headers.get("accept") == "text/event-stream"
        return _sse_response(
            ["Hel", "lo, ", "world", "!"],
            usage={"prompt_tokens": 4, "completion_tokens": 4, "total_tokens": 8},
        )

    response = await _run_stream(handler)
    assert saw_stream_in_payload == [True]
    assert response["content"] == "Hello, world!"
    assert response["raw_content"] == "Hello, world!"
    assert response["usage"] == {"prompt_tokens": 4, "completion_tokens": 4, "total_tokens": 8}
    assert response["provider_used"] == "primary"
    assert response["provider_model"] == "primary-model"


async def test_streaming_strips_reasoning_tags() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return _sse_response(
            ["<think>", "internal note", "</think>", "visible answer"],
        )

    response = await _run_stream(handler)
    # Reasoning tags are stripped from content
    assert response["content"] == "visible answer"
    # But raw_content still contains them
    assert "<think>" in response["raw_content"]
    assert "internal note" in response["raw_content"]


async def test_streaming_advances_to_next_provider_on_5xx() -> None:
    seen: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host
        seen.append(host)
        if host == "primary.test":
            return httpx.Response(503, json={"error": "down"})
        return _sse_response(["fallback"], usage={"total_tokens": 1})

    response = await _run_stream(handler)
    assert seen[0] == "primary.test"
    assert response["provider_used"] == "secondary"
    assert response["content"] == "fallback"


async def test_streaming_treats_mid_stream_drop_as_transient() -> None:
    """If a provider returns 200 but the stream closes without ``[DONE]``,
    the chain must advance to the next provider rather than returning a
    partial response. Codex AGREE-WITH-AMENDMENTS 2026-05-12 D+:
    completed-only commit."""
    seen: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host
        seen.append(host)
        if host == "primary.test":
            # Stream ends mid-response, no [DONE] marker.
            body = b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n'
            return httpx.Response(200, content=body, headers={"Content-Type": "text/event-stream"})
        return _sse_response(["complete"], usage={"total_tokens": 1})

    response = await _run_stream(handler)
    assert seen == ["primary.test", "secondary.test"]
    assert response["content"] == "complete"
    assert response["provider_used"] == "secondary"


async def test_streaming_validate_json_content_advances_on_bad_json() -> None:
    seen: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host
        seen.append(host)
        if host == "primary.test":
            return _sse_response(["not json at all"])
        return _sse_response(['{"ok": true}'])

    response = await _run_stream(handler, validate_json_content=True)
    assert seen == ["primary.test", "secondary.test"]
    assert response["content"] == '{"ok": true}'
    assert response["provider_used"] == "secondary"


async def test_streaming_handles_empty_lines_and_non_data_lines() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        # Mix in empty lines and a non-data comment, both should be
        # silently ignored by the SSE parser.
        body = (
            b": comment line\n"
            b"\n"
            b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
            b"\n"
            b'data: {"choices":[{"delta":{"content":" there"}}]}\n\n'
            b"data: [DONE]\n\n"
        )
        return httpx.Response(200, content=body, headers={"Content-Type": "text/event-stream"})

    response = await _run_stream(handler)
    assert response["content"] == "hi there"


async def test_streaming_accept_header_set() -> None:
    captured: list[dict[str, str]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.append(dict(request.headers))
        return _sse_response(["ok"])

    await _run_stream(handler)
    # Streaming requests advertise text/event-stream so the gateway
    # knows to keep the connection in SSE mode rather than buffering.
    assert captured[0].get("accept") == "text/event-stream"
