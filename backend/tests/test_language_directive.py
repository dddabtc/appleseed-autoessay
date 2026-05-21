"""Unit tests for the per-language system-prompt directive used by all agents."""

from __future__ import annotations

import pytest

from autoessay.agents._language import language_directive


def test_english_directive_mentions_english() -> None:
    text = language_directive("en")
    assert "English" in text
    assert "JSON" in text  # JSON field-name guidance is preserved


@pytest.mark.parametrize("alias", ["zh", "ZH", " zh ", None])
def test_chinese_directive_returns_chinese_text(alias: str | None) -> None:
    # None falls back to English; zh/aliases produce the Chinese directive.
    text = language_directive(alias)
    if alias is None:
        assert "English" in text
    else:
        assert "Simplified Chinese" in text or "简体中文" in text
        assert "GB/T" in text  # Chinese citation-style hint is present


def test_japanese_directive_returns_japanese_text() -> None:
    text = language_directive("ja")
    assert "Japanese" in text
    assert "日本語" in text
    assert "である" in text  # academic-style Japanese guidance is present


def test_unknown_language_falls_back_to_english() -> None:
    text = language_directive("klingon")
    assert "English" in text
