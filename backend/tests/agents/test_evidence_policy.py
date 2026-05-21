from __future__ import annotations

import pytest

from autoessay.agents._evidence_policy import EvidencePolicies
from autoessay.config import Settings

_ENV_KEYS = (
    "AUTOESSAY_VERIFY_BY_SOURCE_DRAFTING_SOURCE_BOUND",
    "AUTOESSAY_VERIFY_BY_SOURCE_DRAFTING_ANALYTIC",
    "AUTOESSAY_VERIFY_BY_SOURCE_FINAL",
    "AUTOESSAY_EVIDENCE_WHITELIST_DRAFTING",
    "AUTOESSAY_EVIDENCE_WHITELIST_FINAL",
)


def _settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    return Settings()  # type: ignore[call-arg]


def test_from_settings_drafting_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    policies = EvidencePolicies.from_settings("drafting", _settings(monkeypatch))

    assert policies.verify_source_bound == "strict"
    assert policies.verify_analytic == "soft"
    assert policies.whitelist == "soft"


def test_from_settings_final_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    policies = EvidencePolicies.from_settings("final", _settings(monkeypatch))

    assert policies.verify_source_bound == "strict"
    assert policies.verify_analytic == "strict"
    assert policies.whitelist == "strict"


def test_from_settings_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("AUTOESSAY_VERIFY_BY_SOURCE_DRAFTING_ANALYTIC", "off")

    policies = EvidencePolicies.from_settings("drafting", Settings())  # type: ignore[call-arg]

    assert policies.verify_analytic == "off"


def test_strict_whitelist_directive_contains_pr301_keywords(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policies = EvidencePolicies.from_settings("final", _settings(monkeypatch))

    assert "不得首次引入新的年份" in policies.whitelist_directive
    assert "正文已展示证据 (Supported claims digest)" in policies.whitelist_directive
    assert "唯一可援引" in policies.whitelist_directive


def test_soft_whitelist_directive_uses_metadata_not_hard_reject(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policies = EvidencePolicies(
        phase="drafting",
        verify_source_bound="strict",
        verify_analytic="soft",
        whitelist="soft",
    )

    assert "evidence_status=model_backed" in policies.whitelist_directive
    assert "不要在 prose" in policies.whitelist_directive
    assert "唯一可援引" not in policies.whitelist_directive
    assert "删除" not in policies.whitelist_directive


def test_strict_coherence_rule_9_contains_original_ban(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policies = EvidencePolicies.from_settings("final", _settings(monkeypatch))

    assert "最后一个 `##`" in policies.coherence_rule_9
    assert "阶段划分" in policies.coherence_rule_9
    assert "一律不可" in policies.coherence_rule_9


def test_soft_coherence_rule_9_is_warning_prompt() -> None:
    policies = EvidencePolicies(
        phase="drafting",
        verify_source_bound="strict",
        verify_analytic="soft",
        whitelist="soft",
    )

    assert "软保护区" in policies.coherence_rule_9
    assert "warning event" in policies.coherence_rule_9
    assert "一律不可" not in policies.coherence_rule_9


def test_supported_claims_block_strict_uses_unique_range() -> None:
    policies = EvidencePolicies(
        phase="final",
        verify_source_bound="strict",
        verify_analytic="strict",
        whitelist="strict",
    )

    block = policies.supported_claims_block("body claim digest")

    assert "正文已展示证据 (Supported claims digest" in block
    assert "唯一" in block
    assert "删除" in block


def test_supported_claims_block_soft_uses_model_backed_metadata() -> None:
    policies = EvidencePolicies(
        phase="drafting",
        verify_source_bound="strict",
        verify_analytic="soft",
        whitelist="soft",
    )

    block = policies.supported_claims_block("body claim digest")

    assert "优先参考" in block
    assert "evidence_status=model_backed" in block
    assert "(model_backed) 标记" in block


def test_section_directive_prefix_declares_both_claim_policies() -> None:
    policies = EvidencePolicies(
        phase="drafting",
        verify_source_bound="strict",
        verify_analytic="soft",
        whitelist="soft",
    )

    prefix = policies.section_directive_prefix()

    assert "source_bound" in prefix
    assert "analytic" in prefix
    assert "`strict`" in prefix
    assert "`soft`" in prefix
