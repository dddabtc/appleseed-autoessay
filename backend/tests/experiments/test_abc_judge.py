from __future__ import annotations

import json
import subprocess
from pathlib import Path

import autoessay.experiments.abc_judge as abc_judge
from autoessay.config import LLMProviderSpec
from autoessay.experiments.abc_judge import build_judge_prompt, judge_submission
from autoessay.experiments.abc_judge_schema import JUDGE_SCHEMA_VERSION, LEDGER_ITEMS

SUBMISSION_UUID = "00000000-0000-0000-0000-000000000123"


def _valid_judge_payload(judge_id: str = "codex-gpt-5.5-xhigh") -> dict[str, object]:
    return {
        "schema_version": JUDGE_SCHEMA_VERSION,
        "judge_id": judge_id,
        "submission_uuid": SUBMISSION_UUID,
        "validity": {"can_score": True, "reason": None},
        "overall_scores": {
            "compliance": 7.0,
            "novelty": 6.0,
            "completeness": 8.0,
        },
        "ledger": [
            {
                "id": item["id"],
                "dimension": item["dimension"],
                "max": item["max"],
                "points": item["max"],
                "reason_code": "SUPPORTED",
                "evidence": ["anchor"],
                "brief_reason": "Brief reason.",
            }
            for item in LEDGER_ITEMS
        ],
        "residual_risks": [],
        "confidence": "high",
    }


def test_build_judge_prompt_renders_protocol_template_without_arm_hint() -> None:
    prompt = build_judge_prompt(
        judge_id="codex-gpt-5.5-xhigh",
        submission_uuid=SUBMISSION_UUID,
        blinded_manuscript_markdown="# 标题\n\n正文和参考文献。\n",
    )

    assert "independent blind reviewer" in prompt.system
    assert "Judge ID: codex-gpt-5.5-xhigh" in prompt.user
    assert f"Submission UUID: {SUBMISSION_UUID}" in prompt.user
    assert "abc_architecture_judge_v1" in prompt.user
    assert "<MANUSCRIPT>\n# 标题" in prompt.user
    assert "Arm B" not in prompt.as_exec_prompt()
    assert "B_prime" not in prompt.as_exec_prompt()


def test_judge_submission_falls_back_to_manual_input_files(tmp_path: Path) -> None:
    manuscript_path = (
        tmp_path / "results" / "hist-01" / "blinded" / SUBMISSION_UUID / "manuscript.md"
    )
    manuscript_path.parent.mkdir(parents=True)
    manuscript_path.write_text("# 标题\n\n正文。\n", encoding="utf-8")

    result = judge_submission(
        results_dir=tmp_path / "results",
        judge_id="codex-gpt-5.4",
        submission_uuid=SUBMISSION_UUID,
        allow_live=False,
    )

    assert result.status == "manual_required"
    assert result.manual_input_path is not None
    assert result.schema_path is not None
    assert result.output_path == manuscript_path.parent / "judge-codex-gpt-5.4.json"
    assert "Submission UUID" in result.manual_input_path.read_text(encoding="utf-8")
    schema = json.loads(result.schema_path.read_text(encoding="utf-8"))
    assert schema["properties"]["schema_version"]["const"] == JUDGE_SCHEMA_VERSION


def test_judge_submission_writes_valid_adapter_output(tmp_path: Path) -> None:
    manuscript_path = (
        tmp_path / "results" / "hist-01" / "blinded" / SUBMISSION_UUID / "manuscript.md"
    )
    manuscript_path.parent.mkdir(parents=True)
    manuscript_path.write_text("# 标题\n\n正文。\n", encoding="utf-8")

    def fake_adapter(_prompt: object) -> str:
        return json.dumps(_valid_judge_payload(), ensure_ascii=False)

    result = judge_submission(
        results_dir=tmp_path / "results",
        judge_id="codex-gpt-5.5-xhigh",
        submission_uuid=SUBMISSION_UUID,
        adapters={"codex-gpt-5.5-xhigh": fake_adapter},
    )

    assert result.status == "scored"
    payload = json.loads(result.output_path.read_text(encoding="utf-8"))
    assert payload["judge_id"] == "codex-gpt-5.5-xhigh"
    assert payload["submission_uuid"] == SUBMISSION_UUID


def test_judge_submission_retries_invalid_schema_output(tmp_path: Path) -> None:
    manuscript_path = (
        tmp_path / "results" / "hist-01" / "blinded" / SUBMISSION_UUID / "manuscript.md"
    )
    manuscript_path.parent.mkdir(parents=True)
    manuscript_path.write_text("# 标题\n\n正文。\n", encoding="utf-8")
    invalid = _valid_judge_payload()
    invalid["ledger"] = [
        {key: value for key, value in entry.items() if key != "brief_reason"}
        for entry in invalid["ledger"]
    ]
    valid = _valid_judge_payload()
    calls = 0

    def fake_adapter(prompt: object) -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            return json.dumps(invalid, ensure_ascii=False)
        assert isinstance(prompt, abc_judge.JudgePrompt)
        assert "previous response failed validation" in prompt.user
        assert "brief_reason" in prompt.user
        return json.dumps(valid, ensure_ascii=False)

    result = judge_submission(
        results_dir=tmp_path / "results",
        judge_id="codex-gpt-5.5-xhigh",
        submission_uuid=SUBMISSION_UUID,
        adapters={"codex-gpt-5.5-xhigh": fake_adapter},
    )

    assert result.status == "scored"
    assert calls == 2
    payload = json.loads(result.output_path.read_text(encoding="utf-8"))
    assert payload["ledger"][0]["brief_reason"] == "Brief reason."


def test_run_provider_judge_uses_named_provider(monkeypatch) -> None:
    prompt = build_judge_prompt(
        judge_id="codex-gpt-5.4",
        submission_uuid=SUBMISSION_UUID,
        blinded_manuscript_markdown="# 标题\n\n正文。\n",
    )
    captured: dict[str, object] = {}

    class FakeLLMClient:
        def __init__(self, *, providers: list[LLMProviderSpec], timeout_seconds: float) -> None:
            captured["providers"] = providers
            captured["timeout_seconds"] = timeout_seconds

        async def chat_completion(self, *args: object, **kwargs: object) -> dict[str, object]:
            captured["args"] = args
            captured["kwargs"] = kwargs
            return {"content": '{"ok": true}'}

        async def aclose(self) -> None:
            captured["closed"] = True

    providers = [
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
    monkeypatch.setattr(abc_judge, "get_llm_providers", lambda: providers)
    monkeypatch.setattr(abc_judge, "LLMClient", FakeLLMClient)

    raw_output = abc_judge._run_provider_judge(
        judge_id="apiport-gpt-5.4",
        prompt=prompt,
        timeout_seconds=12,
    )

    assert raw_output == '{"ok": true}'
    captured_providers = captured["providers"]
    assert isinstance(captured_providers, list)
    assert len(captured_providers) == 1
    assert captured_providers[0].name == "apiport"
    assert captured_providers[0].model == "gpt-5.4"
    assert captured["timeout_seconds"] == 12.0
    assert captured["closed"] is True
    kwargs = captured["kwargs"]
    assert isinstance(kwargs, dict)
    assert kwargs["response_format"] == {"type": "json_object"}
    assert kwargs["validate_json_content"] is True
    assert kwargs["retries"] == abc_judge.JUDGE_PROVIDER_RETRIES


def test_run_codex_exec_pins_model_and_reasoning(monkeypatch) -> None:
    prompt = build_judge_prompt(
        judge_id="codex-gpt-5.5-xhigh",
        submission_uuid=SUBMISSION_UUID,
        blinded_manuscript_markdown="# 标题\n\n正文。\n",
    )
    captured: dict[str, object] = {}

    def fake_which(name: str) -> str | None:
        assert name == "codex"
        return "/usr/bin/codex"

    def fake_run(
        args: list[str],
        *,
        check: bool,
        capture_output: bool,
        text: bool,
        timeout: int,
    ) -> subprocess.CompletedProcess[str]:
        captured["args"] = args
        captured["check"] = check
        captured["capture_output"] = capture_output
        captured["text"] = text
        captured["timeout"] = timeout
        return subprocess.CompletedProcess(args=args, returncode=0, stdout='{"ok": true}\n')

    monkeypatch.setattr(abc_judge.shutil, "which", fake_which)
    monkeypatch.setattr(abc_judge.subprocess, "run", fake_run)

    raw_output = abc_judge._run_codex_exec(prompt=prompt, timeout_seconds=34)

    assert raw_output == '{"ok": true}'
    args = captured["args"]
    assert isinstance(args, list)
    assert args[:6] == [
        "/usr/bin/codex",
        "exec",
        "--ephemeral",
        "--ignore-rules",
        "--sandbox",
        "read-only",
    ]
    assert "--model" in args
    assert args[args.index("--model") + 1] == "gpt-5.5"
    assert "-c" in args
    assert args[args.index("-c") + 1] == "model_reasoning_effort=xhigh"
    assert args[-1] == prompt.as_exec_prompt()
    assert captured["timeout"] == 34
