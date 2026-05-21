"""PR-385 regression test: when ``response_format={"type":"json_object"}``
is set, the upstream OpenAI-compat gateway rejects requests whose user
messages don't contain the literal word "json". The LLMClient must inject
a tiny "Respond with strict JSON." suffix into the last user message
when the keyword is missing. Codex amendment: the keyword must be in a
**user** message specifically, not just anywhere — system prompts don't
count on some gateways (rightcode 2026-05-13)."""

from __future__ import annotations

from autoessay.llm_client import _ensure_json_keyword_in_user_message


def test_no_response_format_passthrough() -> None:
    msgs = [{"role": "user", "content": "hello world"}]
    out = _ensure_json_keyword_in_user_message(msgs, None)
    assert out == [{"role": "user", "content": "hello world"}]
    assert out is not msgs


def test_text_response_format_passthrough() -> None:
    msgs = [{"role": "user", "content": "hello"}]
    out = _ensure_json_keyword_in_user_message(msgs, {"type": "text"})
    assert out[0]["content"] == "hello"


def test_user_already_mentions_json_no_injection() -> None:
    msgs = [
        {"role": "system", "content": "You are a classifier."},
        {"role": "user", "content": "Respond as JSON object: hello"},
    ]
    out = _ensure_json_keyword_in_user_message(msgs, {"type": "json_object"})
    assert out[1]["content"] == "Respond as JSON object: hello"


def test_user_mentions_json_case_insensitive() -> None:
    msgs = [{"role": "user", "content": "give me Json plz"}]
    out = _ensure_json_keyword_in_user_message(msgs, {"type": "json_object"})
    assert "(Respond with strict JSON.)" not in out[0]["content"]


def test_keyword_in_system_only_still_injects() -> None:
    # Safety gate's real shape: system prompt contains "JSON object",
    # user template does NOT. Gateway only checks user content, so we
    # must inject even though "json" appears in system message.
    msgs = [
        {"role": "system", "content": "Reply with EXACTLY one JSON object."},
        {"role": "user", "content": "Context: title\n<USER_INPUT>hello</USER_INPUT>"},
    ]
    out = _ensure_json_keyword_in_user_message(msgs, {"type": "json_object"})
    assert out[0]["content"] == "Reply with EXACTLY one JSON object."
    assert "(Respond with strict JSON.)" in out[1]["content"]
    assert out[1]["content"].startswith("Context: title")


def test_injection_appends_to_last_user_when_multi_turn() -> None:
    msgs = [
        {"role": "user", "content": "first turn"},
        {"role": "assistant", "content": "ok"},
        {"role": "user", "content": "second turn"},
    ]
    out = _ensure_json_keyword_in_user_message(msgs, {"type": "json_object"})
    assert out[0]["content"] == "first turn"
    assert out[2]["content"].endswith("(Respond with strict JSON.)")


def test_no_user_message_appends_one() -> None:
    msgs: list[dict[str, str]] = [{"role": "system", "content": "You are a bot."}]
    out = _ensure_json_keyword_in_user_message(msgs, {"type": "json_object"})
    assert len(out) == 2
    assert out[-1] == {"role": "user", "content": "(Respond with strict JSON.)"}


def test_does_not_mutate_originals() -> None:
    msgs = [{"role": "user", "content": "hello"}]
    _ensure_json_keyword_in_user_message(msgs, {"type": "json_object"})
    assert msgs[0]["content"] == "hello"
