from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from autoessay.config import LLMProviderSpec
from autoessay.experiments import abc_generator
from autoessay.experiments.abc_architecture import GENERATION_MODEL_ID
from autoessay.experiments.abc_extract import KernelMetadata, dump_front_half_package
from autoessay.experiments.abc_generator import (
    TokenBudgetExceededError,
    generate_b,
    generate_b_prime,
    generate_c,
    generate_g,
)


class FakeGateway:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.responses = [
            "# B\n\n摘要\n\n正文【1】{{TODO}}\n\n参考文献\n1. A\n",
            "# B Prime\n\n摘要\n\n正文【1】\n\n参考文献\n1. A\n",
            "# C\n\n摘要\n\n正文{{TODO}}（1）\n\n参考文献\n1. Model Known Source\n",
        ]

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
        self.calls.append(
            {
                "messages": list(messages),
                "model": model,
                "temperature": temperature,
                "max_tokens": max_tokens,
                "retries": retries,
                "response_format": response_format,
                "force_no_reasoning": force_no_reasoning,
                "validate_json_content": validate_json_content,
                "stream": stream,
            }
        )
        content = self.responses.pop(0)
        return {
            "content": content,
            "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
            "provider_used": "fake-provider",
            "provider_model": "fake-model",
        }


async def test_generator_stub_b_b_prime_c_full_flow(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    (run_dir / "discovery").mkdir(parents=True)
    (run_dir / "sources").mkdir()
    (run_dir / "synthesis").mkdir()
    (run_dir / "discovery" / "scout_report.md").write_text("scout", encoding="utf-8")
    (run_dir / "sources" / "shortlist.json").write_text(
        '[{"source_id":"S1","title":"A"}]\n',
        encoding="utf-8",
    )
    (run_dir / "synthesis" / "claims.jsonl").write_text('{"claim":"x"}\n', encoding="utf-8")
    results_dir = tmp_path / "results"
    dump_front_half_package(
        run_dir=run_dir,
        results_dir=results_dir,
        kernel_id="econ-01",
        a_run_id="run-a",
        metadata=KernelMetadata(
            title="公共物品理论在中国财政学中的本土化路径",
            research_kernel={"tentative_question": "如何本土化？"},
            target_journal="《经济研究》",
        ),
    )
    gateway = FakeGateway()

    b = await generate_b(kernel_id="econ-01", results_dir=results_dir, gateway=gateway)
    b_prime = await generate_b_prime(kernel_id="econ-01", results_dir=results_dir, gateway=gateway)
    c = await generate_c(kernel_id="econ-01", results_dir=results_dir, gateway=gateway)

    assert b.manuscript_path.read_text(encoding="utf-8") == (
        "# B\n\n## 摘要\n\n正文[1]\n\n## 参考文献\n[1] A\n"
    )
    assert "{{TODO}}" in c.manuscript_path.read_text(encoding="utf-8")
    b_prov = json.loads(b.provenance_path.read_text(encoding="utf-8"))
    bp_prov = json.loads(b_prime.provenance_path.read_text(encoding="utf-8"))
    c_prov = json.loads(c.provenance_path.read_text(encoding="utf-8"))
    assert b_prov["arm"] == "B"
    assert b_prov["model_id"] == GENERATION_MODEL_ID
    assert b_prov["provider"] == "fake-provider"
    assert b_prov["provider_model"] == "fake-model"
    assert b_prov["provider_fallback_allowed"] is True
    assert b_prov["source_package_sha256"]
    assert b_prov["compliance_repair"]["attempted"] is True
    assert bp_prov["arm"] == "B_prime"
    assert bp_prov["base_b_manuscript_sha256"]
    assert bp_prov["self_critique_prompt_sha256"] == bp_prov["prompt_sha256"]
    assert bp_prov["self_critique"]["attempted"] is True
    assert bp_prov["self_critique"]["output_equal_to_base"] is False
    assert bp_prov["self_critique"]["anti_echo_retry_attempted"] is False
    assert bp_prov["self_critique"]["attempt_count"] == 1
    assert b_prime.manuscript_path.read_text(encoding="utf-8") != b.manuscript_path.read_text(
        encoding="utf-8"
    )
    assert c_prov["arm"] == "C"
    assert c_prov["source_package_sha256"] is None
    assert c_prov["compliance_repair"]["attempted"] is False
    assert len(gateway.calls) == 3
    assert all(call["force_no_reasoning"] is False for call in gateway.calls)
    assert all(call["stream"] is False for call in gateway.calls)
    assert "EVIDENCE_PACKAGE" not in gateway.calls[2]["messages"][1]["content"]


async def test_generate_b_prime_retries_byte_identical_echo_once(tmp_path: Path) -> None:
    results_dir = tmp_path / "results"
    kernel_dir = results_dir / "econ-01"
    (kernel_dir / "front_half").mkdir(parents=True)
    (kernel_dir / "front_half" / "package.md").write_text(
        "source_id: S1\nclaim\n",
        encoding="utf-8",
    )
    base_b = "# B\n\n## 摘要\n\n正文[1]\n\n## 参考文献\n[1] A\n"
    revised = "# B\n\n## 摘要\n\n修订后的正文[1]\n\n## 参考文献\n[1] A\n"
    (kernel_dir / "B").mkdir()
    (kernel_dir / "B" / "manuscript.md").write_text(base_b, encoding="utf-8")
    gateway = FakeGateway()
    gateway.responses = [base_b, revised]

    result = await generate_b_prime(kernel_id="econ-01", results_dir=results_dir, gateway=gateway)

    manuscript = result.manuscript_path.read_text(encoding="utf-8")
    provenance = json.loads(result.provenance_path.read_text(encoding="utf-8"))
    assert manuscript == revised
    assert len(gateway.calls) == 2
    assert "上一轮输出与 B 稿逐字节相同" in gateway.calls[1]["messages"][1]["content"]
    assert provenance["token_usage"]["total_tokens"] == 60
    assert provenance["self_critique"]["anti_echo_retry_attempted"] is True
    assert provenance["self_critique"]["attempt_count"] == 2
    assert len(provenance["self_critique"]["attempt_prompt_sha256s"]) == 2


async def test_generate_b_prime_rejects_byte_identical_echo(tmp_path: Path) -> None:
    results_dir = tmp_path / "results"
    kernel_dir = results_dir / "econ-01"
    (kernel_dir / "front_half").mkdir(parents=True)
    (kernel_dir / "front_half" / "package.md").write_text(
        "source_id: S1\nclaim\n",
        encoding="utf-8",
    )
    base_b = "# B\n\n## 摘要\n\n正文[1]\n\n## 参考文献\n[1] A\n"
    (kernel_dir / "B").mkdir()
    (kernel_dir / "B" / "manuscript.md").write_text(base_b, encoding="utf-8")
    gateway = FakeGateway()
    gateway.responses = [base_b, base_b]

    with pytest.raises(RuntimeError, match="after anti-echo retry"):
        await generate_b_prime(kernel_id="econ-01", results_dir=results_dir, gateway=gateway)

    assert len(gateway.calls) == 2
    assert not (kernel_dir / "B_prime" / "manuscript.md").exists()


async def test_generate_g_uses_front_half_and_records_cut_audit(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    run_dir = tmp_path / "run"
    (run_dir / "discovery").mkdir(parents=True)
    (run_dir / "sources").mkdir()
    (run_dir / "synthesis").mkdir()
    (run_dir / "discovery" / "scout_report.md").write_text("scout", encoding="utf-8")
    (run_dir / "sources" / "shortlist.json").write_text(
        '[{"source_id":"S1","title":"A"}]\n',
        encoding="utf-8",
    )
    results_dir = tmp_path / "results"
    dump_front_half_package(
        run_dir=run_dir,
        results_dir=results_dir,
        kernel_id="econ-01",
        a_run_id="run-a",
        metadata=KernelMetadata(
            title="公共物品理论在中国财政学中的本土化路径",
            research_kernel={"tentative_question": "如何本土化？"},
            target_journal="《经济研究》",
        ),
    )
    (results_dir / "econ-01" / "E").mkdir()
    (results_dir / "econ-01" / "E" / "manuscript.md").write_text(
        "# E\n\n正文\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        abc_generator,
        "_load_ars_full_mode_prompt",
        lambda _path: {
            "prompt": "ARS full mode",
            "ars_skill_sha": "ars-sha",
            "ars_skill_file_sha256": "ars-file-sha",
            "manifest": [],
        },
    )
    gateway = FakeGateway()
    gateway.responses = ["# G\n\n摘要\n\n正文【1】\n\n参考文献\n1. A\n"]

    result = await generate_g(kernel_id="econ-01", results_dir=results_dir, gateway=gateway)

    manuscript = result.manuscript_path.read_text(encoding="utf-8")
    provenance = json.loads(result.provenance_path.read_text(encoding="utf-8"))
    state = json.loads((results_dir / "econ-01" / "G" / "state.json").read_text(encoding="utf-8"))
    assert manuscript == "# G\n\n## 摘要\n\n正文[1]\n\n## 参考文献\n[1] A\n"
    assert provenance["arm"] == "G"
    assert provenance["source_package_sha256"]
    assert provenance["compliance_repair"]["attempted"] is True
    assert provenance["ars_single_call"]["phase_count"] == 1
    assert provenance["ars_single_call"]["llm_call_count"] == 1
    assert provenance["ars_single_call"]["revision_loop_count"] == 0
    assert provenance["ars_single_call"]["peer_review_structure"]["enabled"] is False
    assert provenance["ars_single_call"]["claim_audit"]["enabled"] is True
    assert provenance["ars_single_call"]["md5_distinctness"]["comparisons"]["E"]["distinct"] is True
    assert state["cut_point"] == "after_appleseed_front_half"
    assert state["audit_points"]["phase_count"] == 1
    assert "EVIDENCE_PACKAGE" in gateway.calls[0]["messages"][1]["content"]
    assert "ARS full mode" in gateway.calls[0]["messages"][1]["content"]
    assert gateway.calls[0]["stream"] is True


async def test_generate_b_hard_fails_when_token_cap_exceeded(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    run_dir = tmp_path / "run"
    (run_dir / "discovery").mkdir(parents=True)
    (run_dir / "sources").mkdir()
    (run_dir / "synthesis").mkdir()
    (run_dir / "discovery" / "scout_report.md").write_text("scout", encoding="utf-8")
    (run_dir / "sources" / "shortlist.json").write_text(
        '[{"source_id":"S1","title":"A"}]\n',
        encoding="utf-8",
    )
    results_dir = tmp_path / "results"
    dump_front_half_package(
        run_dir=run_dir,
        results_dir=results_dir,
        kernel_id="econ-01",
        a_run_id="run-a",
        metadata=KernelMetadata(
            title="公共物品理论在中国财政学中的本土化路径",
            research_kernel={"tentative_question": "如何本土化？"},
            target_journal="《经济研究》",
        ),
    )
    monkeypatch.setenv("AUTOESSAY_EXPERIMENT_ABC_TOKEN_CAP", "1")

    with pytest.raises(TokenBudgetExceededError, match="exceeding cap 1"):
        await generate_b(kernel_id="econ-01", results_dir=results_dir, gateway=FakeGateway())

    assert not (results_dir / "econ-01" / "B" / "manuscript.md").exists()


async def test_generate_b_rejects_missing_token_usage(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    (run_dir / "discovery").mkdir(parents=True)
    (run_dir / "sources").mkdir()
    (run_dir / "synthesis").mkdir()
    (run_dir / "discovery" / "scout_report.md").write_text("scout", encoding="utf-8")
    (run_dir / "sources" / "shortlist.json").write_text(
        '[{"source_id":"S1","title":"A"}]\n',
        encoding="utf-8",
    )
    results_dir = tmp_path / "results"
    dump_front_half_package(
        run_dir=run_dir,
        results_dir=results_dir,
        kernel_id="econ-01",
        a_run_id="run-a",
        metadata=KernelMetadata(
            title="公共物品理论在中国财政学中的本土化路径",
            research_kernel={"tentative_question": "如何本土化？"},
            target_journal="《经济研究》",
        ),
    )
    gateway = FakeGateway()
    original_completion = gateway.chat_completion

    async def missing_usage_completion(*args: object, **kwargs: object) -> dict[str, Any]:
        await original_completion(*args, **kwargs)
        return {
            "content": "# B\n\n摘要\n\n正文\n",
            "usage": {},
            "provider_used": "apiport",
            "provider_model": "gpt-5.4-mini",
        }

    gateway.chat_completion = missing_usage_completion  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="missing token usage"):
        await generate_b(kernel_id="econ-01", results_dir=results_dir, gateway=gateway)

    assert not (results_dir / "econ-01" / "B" / "manuscript.md").exists()


def test_pinned_llm_client_uses_full_provider_chain(monkeypatch: Any) -> None:
    providers = [
        LLMProviderSpec(
            name="rightcode",
            base_url="https://rightcode.example",
            api_key="rightcode-key",
            model="gpt-5.4-mini",
        ),
        LLMProviderSpec(
            name="apiport",
            base_url="https://apiport.example",
            api_key="apiport-key",
            model="gpt-5.4-mini",
        ),
        LLMProviderSpec(
            name="minimax",
            base_url="https://minimax.example",
            api_key="minimax-key",
            model="MiniMax-M2.7",
        ),
    ]
    captured: dict[str, Any] = {}

    class CapturingLLMClient:
        def __init__(
            self,
            *,
            providers: Sequence[LLMProviderSpec],
            timeout_seconds: float,
        ) -> None:
            captured["providers"] = list(providers)
            captured["timeout_seconds"] = timeout_seconds

    monkeypatch.setattr(abc_generator, "get_llm_providers", lambda: providers)
    monkeypatch.setattr(abc_generator, "LLMClient", CapturingLLMClient)

    abc_generator._make_pinned_llm_client(GENERATION_MODEL_ID)

    assert captured["providers"] == providers
    assert [provider.name for provider in captured["providers"]] == [
        "rightcode",
        "apiport",
        "minimax",
    ]
    assert [provider.model for provider in captured["providers"]] == [
        "gpt-5.4-mini",
        "gpt-5.4-mini",
        "MiniMax-M2.7",
    ]
    assert captured["timeout_seconds"] == 900.0
