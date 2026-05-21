"""Shared language directive helper for agent prompts.

Every agent that produces user-facing prose (Drafter, Stylist, Critic,
Proposal, Synthesizer, Ideator) appends ``language_directive(language)``
to its system prompt so the LLM responds in the project's language.

Agents that produce structured JSON metadata (Scout queries, Curator
rankings) keep this directive too — JSON field NAMES always stay in
English, but free-text fields (rationale, reasoning, notes) follow the
project language.
"""

from __future__ import annotations

from typing import Final

SUPPORTED: Final[tuple[str, ...]] = ("en", "zh", "ja")

_DIRECTIVES: Final[dict[str, str]] = {
    "en": (
        "Reply only in English (en). "
        "All prose and JSON string values must be English; "
        "JSON field names stay in English. "
        "Use academic conventions appropriate for English-language journals."
    ),
    "zh": (
        "Reply only in Simplified Chinese (zh-CN, 简体中文). "
        "所有正文与 JSON 字符串值都必须使用简体中文; "
        "JSON 字段名保持英文. "
        "使用适合中文学术期刊的写作规范 (例如 GB/T 7714-2015 引用格式)."
    ),
    "ja": (
        "Reply only in Japanese (ja-JP, 日本語). "
        "全ての本文及び JSON 文字列値は日本語で記述すること; "
        "JSON のフィールド名は英語のまま. "
        "学術論文向けの日本語表現を用いる (常体 — である調; 敬語は使わない)."
    ),
}


def language_directive(language: str | None) -> str:
    """Return the system-prompt suffix instructing the LLM to reply in
    ``language``. Falls back to English for unknown / None values to keep
    legacy behaviour.
    """
    code = (language or "en").strip().lower()
    return _DIRECTIVES.get(code, _DIRECTIVES["en"])
