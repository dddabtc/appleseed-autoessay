"""Blind judge prompt rendering and runner adapters for the ABC experiment."""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Protocol
from uuid import UUID

from autoessay.config import LLMProviderSpec, get_llm_providers
from autoessay.experiments.abc_judge_schema import (
    ABC_ARCHITECTURE_JUDGE_SCHEMA,
    JUDGE_IDS,
    JUDGE_SCHEMA_VERSION,
    LEDGER_ITEMS,
    validate_judge_output,
)
from autoessay.llm_client import LLMClient

JUDGE_MAX_TOKENS = 8000
JUDGE_TIMEOUT_SECONDS = 1800
JUDGE_PROVIDER_RETRIES = 2
JUDGE_VALIDATION_RETRIES = 2
CODEX_API_MODEL = "gpt-5.5"
CODEX_REASONING_EFFORT = "xhigh"
CODEX_JUDGE_SPECS: dict[str, tuple[str, str]] = {
    "codex-gpt-5.5-xhigh": ("gpt-5.5", "xhigh"),
    "codex-gpt-5.4": ("gpt-5.4", "medium"),
}
PROVIDER_JUDGE_SPECS: dict[str, tuple[str, str]] = {
    "apiport-gpt-5.4": ("apiport", "gpt-5.4"),
}

SYSTEM_PROMPT = """You are an independent blind reviewer for a Chinese academic manuscript
quality experiment. Score only the manuscript shown to you. Do not infer
which experimental arm produced it. Do not reward plausible intent that is
not visible in the text. Use the JSON schema exactly.

Evidence rule: score citation and evidence quality from manuscript-internal
claims, citation markers, and reference entries only. A claim is supported
only when the manuscript gives enough citation context for a reader to trace
the claim to a listed reference. Hallucinated, missing, or mismatched
references lower citation_alignment even if the prose is fluent."""

USER_PROMPT_TEMPLATE = """Judge ID: {judge_id}
Submission UUID: {submission_uuid}

Read the blinded manuscript below and return one JSON object matching
abc_architecture_judge_v1. Score compliance, novelty, and completeness on
1-10 scales. Then score all 13 ledger items. Use short manuscript anchors
as evidence. If the manuscript is unreadable or too incomplete to score, set
validity.can_score=false and explain why.

Output contract:
{output_contract}

<MANUSCRIPT>
{blinded_manuscript_markdown}
</MANUSCRIPT>"""


class JudgeAdapter(Protocol):
    def __call__(self, prompt: JudgePrompt) -> str | None: ...


@dataclass(frozen=True)
class JudgePrompt:
    submission_uuid: str
    system: str
    user: str

    @property
    def messages(self) -> list[dict[str, str]]:
        return [
            {"role": "system", "content": self.system},
            {"role": "user", "content": self.user},
        ]

    def as_markdown(self) -> str:
        return (
            "# ABC Blind Judge Input\n\n"
            "## System Prompt\n\n"
            "```text\n"
            f"{self.system}\n"
            "```\n\n"
            "## User Prompt\n\n"
            "```text\n"
            f"{self.user}\n"
            "```\n"
        )

    def as_exec_prompt(self) -> str:
        return f"SYSTEM:\n{self.system}\n\nUSER:\n{self.user}\n"


@dataclass(frozen=True)
class JudgeRunResult:
    judge_id: str
    submission_uuid: str
    submission_dir: Path
    output_path: Path
    status: str
    manual_input_path: Path | None = None
    schema_path: Path | None = None
    raw_output_path: Path | None = None


def build_judge_prompt(
    *,
    judge_id: str,
    submission_uuid: str,
    blinded_manuscript_markdown: str,
) -> JudgePrompt:
    """Render the protocol Section 5 blind-review prompt."""
    if judge_id not in JUDGE_IDS:
        raise ValueError(f"Unsupported judge_id {judge_id!r}")
    UUID(submission_uuid)
    return JudgePrompt(
        submission_uuid=submission_uuid,
        system=SYSTEM_PROMPT,
        user=USER_PROMPT_TEMPLATE.format(
            judge_id=judge_id,
            submission_uuid=submission_uuid,
            output_contract=_output_contract(judge_id),
            blinded_manuscript_markdown=blinded_manuscript_markdown.strip(),
        ),
    )


def _output_contract(judge_id: str) -> str:
    ledger_lines = "\n".join(
        f"- {item['id']}: dimension={item['dimension']}, max={item['max']}" for item in LEDGER_ITEMS
    )
    return (
        "Return only a JSON object, with no markdown fences or prose.\n"
        f"- schema_version must be {JUDGE_SCHEMA_VERSION!r}.\n"
        f"- judge_id must be exactly {judge_id!r}.\n"
        "- submission_uuid must be the exact UUID from the request.\n"
        "- validity must include can_score boolean and reason string or null.\n"
        "- Do not set validity.can_score=false for weak evidence, mismatched "
        "citations, generic prose, or poor quality. Those cases are scoreable "
        "and should receive low scores. Use can_score=false only for empty, "
        "corrupted, non-Chinese, or severely truncated submissions.\n"
        "- Even if can_score=false, all fields below are still required; use "
        "low scores and MISSING evidence anchors rather than omitting them.\n"
        "- overall_scores must include only compliance, novelty, completeness, "
        "each a finite number from 1 to 10.\n"
        "- ledger must contain exactly these 13 items, each once. Each item must "
        "include id, dimension, max, points, reason_code, evidence, and "
        "brief_reason. Points must be whole or half points from 0 to max. "
        "Evidence must be a non-empty array with 1-2 short manuscript anchors. "
        "brief_reason must be a non-empty short sentence and must not be omitted.\n"
        f"{ledger_lines}\n"
        "- residual_risks must be an array of at most 3 strings.\n"
        "- confidence must be present and must be exactly one of low, medium, high."
    )


def judge_submission(
    *,
    results_dir: str | Path,
    judge_id: str,
    submission_uuid: str,
    adapters: Mapping[str, JudgeAdapter] | None = None,
    allow_live: bool = True,
    timeout_seconds: int = JUDGE_TIMEOUT_SECONDS,
) -> JudgeRunResult:
    """Run or prepare judging for one blinded submission.

    The runner locates the blinded manuscript by UUID and never reads
    blind_map.json, preserving judge independence from arm labels.
    """
    if judge_id not in JUDGE_IDS:
        raise ValueError(
            f"Unsupported judge_id {judge_id!r}; expected one of {', '.join(JUDGE_IDS)}"
        )
    submission_dir = find_blinded_submission_dir(results_dir, submission_uuid)
    manuscript_path = submission_dir / "manuscript.md"
    output_path = submission_dir / f"judge-{judge_id}.json"
    if output_path.exists():
        payload = _load_json_object(output_path)
        ok, errors = validate_judge_output(payload)
        if not ok:
            raise ValueError(f"Existing judge output is invalid at {output_path}: {errors}")
        return JudgeRunResult(
            judge_id=judge_id,
            submission_uuid=submission_uuid,
            submission_dir=submission_dir,
            output_path=output_path,
            status="existing",
        )

    prompt = build_judge_prompt(
        judge_id=judge_id,
        submission_uuid=submission_uuid,
        blinded_manuscript_markdown=manuscript_path.read_text(encoding="utf-8"),
    )
    raw_output_path = submission_dir / f"judge-{judge_id}.raw.txt"
    raw_output: str | None = None
    validation_errors: list[str] = []
    for attempt in range(JUDGE_VALIDATION_RETRIES + 1):
        raw_output = _run_adapter(
            judge_id=judge_id,
            prompt=prompt,
            adapters=adapters,
            allow_live=allow_live,
            timeout_seconds=timeout_seconds,
        )
        if raw_output is None:
            manual_input_path, schema_path = write_manual_judge_inputs(
                submission_dir=submission_dir,
                judge_id=judge_id,
                prompt=prompt,
            )
            return JudgeRunResult(
                judge_id=judge_id,
                submission_uuid=submission_uuid,
                submission_dir=submission_dir,
                output_path=output_path,
                status="manual_required",
                manual_input_path=manual_input_path,
                schema_path=schema_path,
            )

        try:
            payload = _extract_json_object(raw_output)
        except ValueError:
            validation_errors = ["response must be one complete JSON object"]
        else:
            ok, validation_errors = validate_judge_output(payload)
            if ok and payload["judge_id"] != judge_id:
                validation_errors = [f"judge_id must be exactly {judge_id!r}"]
            if ok and payload["submission_uuid"] != submission_uuid:
                validation_errors = [f"submission_uuid must be exactly {submission_uuid!r}"]
            if not validation_errors:
                _write_json(output_path, payload)
                return JudgeRunResult(
                    judge_id=judge_id,
                    submission_uuid=submission_uuid,
                    submission_dir=submission_dir,
                    output_path=output_path,
                    status="scored",
                    raw_output_path=raw_output_path if raw_output_path.exists() else None,
                )

        if attempt < JUDGE_VALIDATION_RETRIES:
            prompt = _prompt_with_validation_errors(prompt, validation_errors)
            continue

    _write_text(raw_output_path, raw_output or "")
    write_manual_judge_inputs(submission_dir=submission_dir, judge_id=judge_id, prompt=prompt)
    raise ValueError(
        f"Judge {judge_id} returned invalid JSON after {JUDGE_VALIDATION_RETRIES + 1} attempts: "
        f"{validation_errors}; raw output at {raw_output_path}"
    )


def write_manual_judge_inputs(
    *,
    submission_dir: Path,
    judge_id: str,
    prompt: JudgePrompt,
) -> tuple[Path, Path]:
    """Write manual judge input and schema files for unavailable adapters."""
    input_path = submission_dir / f"judge-input-{judge_id}.md"
    schema_path = submission_dir / "judge-schema.json"
    _write_text(input_path, prompt.as_markdown())
    _write_json(schema_path, ABC_ARCHITECTURE_JUDGE_SCHEMA)
    return input_path, schema_path


def _prompt_with_validation_errors(prompt: JudgePrompt, errors: list[str]) -> JudgePrompt:
    error_lines = "\n".join(f"- {error}" for error in errors[:20])
    return JudgePrompt(
        submission_uuid=prompt.submission_uuid,
        system=prompt.system,
        user=(
            f"{prompt.user}\n\n"
            "Your previous response failed validation. Return a fresh, complete JSON object "
            "only. Do not reuse the invalid output.\n"
            f"{error_lines}"
        ),
    )


def find_blinded_submission_dir(results_dir: str | Path, submission_uuid: str) -> Path:
    UUID(submission_uuid)
    root = Path(results_dir)
    matches = [
        path
        for path in root.glob(f"*/blinded/{submission_uuid}")
        if (path / "manuscript.md").is_file()
    ]
    if not matches:
        raise FileNotFoundError(f"No blinded manuscript found for submission {submission_uuid}")
    if len(matches) > 1:
        raise ValueError(f"Submission UUID {submission_uuid} appears in multiple blinded folders")
    return matches[0]


def list_blinded_submissions(results_dir: str | Path) -> list[str]:
    root = Path(results_dir)
    return sorted(
        path.parent.name
        for path in root.glob("*/blinded/*/manuscript.md")
        if _is_uuid(path.parent.name)
    )


def _run_adapter(
    *,
    judge_id: str,
    prompt: JudgePrompt,
    adapters: Mapping[str, JudgeAdapter] | None,
    allow_live: bool,
    timeout_seconds: int,
) -> str | None:
    if adapters is not None:
        adapter = adapters.get(judge_id)
        return adapter(prompt) if adapter is not None else None
    if not allow_live:
        return None
    if judge_id in CODEX_JUDGE_SPECS:
        model, reasoning_effort = CODEX_JUDGE_SPECS[judge_id]
        return _run_codex_exec(
            prompt=prompt,
            timeout_seconds=timeout_seconds,
            model=model,
            reasoning_effort=reasoning_effort,
        )
    if judge_id in PROVIDER_JUDGE_SPECS:
        return _run_provider_judge(
            judge_id=judge_id,
            prompt=prompt,
            timeout_seconds=timeout_seconds,
        )
    raise ValueError(f"Unsupported judge_id {judge_id!r}")


def _run_codex_exec(
    *,
    prompt: JudgePrompt,
    timeout_seconds: int,
    model: str = CODEX_API_MODEL,
    reasoning_effort: str = CODEX_REASONING_EFFORT,
) -> str | None:
    codex = shutil.which("codex")
    if codex is None:
        raise RuntimeError("codex executable is required for judge codex-gpt-5.5-xhigh")
    try:
        completed = subprocess.run(
            [
                codex,
                "exec",
                "--ephemeral",
                "--ignore-rules",
                "--sandbox",
                "read-only",
                "--model",
                model,
                "-c",
                f"model_reasoning_effort={reasoning_effort}",
                prompt.as_exec_prompt(),
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError("codex judge execution failed") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        raise RuntimeError(
            "codex judge execution failed"
            + (f": {detail[:500]}" if detail else f" with exit code {completed.returncode}")
        )
    return completed.stdout.strip() or None


def _run_provider_judge(
    *,
    judge_id: str,
    prompt: JudgePrompt,
    timeout_seconds: int,
) -> str:
    provider_name, provider_model = PROVIDER_JUDGE_SPECS[judge_id]
    providers = [provider for provider in get_llm_providers() if provider.name == provider_name]
    if not providers:
        raise RuntimeError(
            f"AUTOESSAY_LLM_PROVIDERS must include provider {provider_name!r} for judge {judge_id}"
        )
    provider = replace(providers[0], model=provider_model)
    return asyncio.run(
        _run_provider_judge_async(
            provider=provider,
            prompt=prompt,
            timeout_seconds=timeout_seconds,
        )
    )


async def _run_provider_judge_async(
    *,
    provider: LLMProviderSpec,
    prompt: JudgePrompt,
    timeout_seconds: int,
) -> str:
    client = LLMClient(providers=[provider], timeout_seconds=float(timeout_seconds))
    try:
        response = await client.chat_completion(
            prompt.messages,
            provider.model,
            0,
            max_tokens=JUDGE_MAX_TOKENS,
            retries=JUDGE_PROVIDER_RETRIES,
            response_format={"type": "json_object"},
            force_no_reasoning=True,
            validate_json_content=True,
            stream=False,
        )
    finally:
        await client.aclose()
    content = str(response.get("content") or "").strip()
    if not content:
        raise RuntimeError(f"Provider judge {provider.name!r} returned empty content")
    return content


def _extract_json_object(raw_output: str) -> dict[str, object]:
    decoder = json.JSONDecoder()
    stripped = raw_output.strip()
    try:
        value = json.loads(stripped)
    except json.JSONDecodeError:
        value = None
    if isinstance(value, dict):
        return value

    for start in (index for index, char in enumerate(raw_output) if char == "{"):
        try:
            value, _end = decoder.raw_decode(raw_output[start:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    raise ValueError("No JSON object found in judge output")


def _load_json_object(path: Path) -> dict[str, object]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"Expected JSON object at {path}")
    return data


def _write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(value, encoding="utf-8")
    temporary.replace(path)


def _write_json(path: Path, payload: Mapping[str, object]) -> None:
    _write_text(path, json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def _is_uuid(value: str) -> bool:
    try:
        UUID(value)
    except ValueError:
        return False
    return True
