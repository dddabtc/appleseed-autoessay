"""PR-262 — shadow-baseline agent.

Generates a single-shot reference paper for the user's research
kernel using gpt-5.5 baseline-style prompting (the same prompt
shape we hand-validated in /tmp/codex_baseline_*.md). The
artifact is an INTERNAL anchor for downstream phases — never
shown to the user — and contains four parts:

1. ``manuscript_markdown`` — full ~6000-word zh CNKI-style
   reference paper (摘要 / 关键词 / 一-八 / 参考文献)
2. ``argument_map`` — per-section central claim + key evidence,
   compressed for the polish-loop's global-revision pass
3. ``reference_candidates`` — ≥10 author/year/title entries the
   model is confident exist; the source-enrichment phase
   (PR-263) verifies these via Crossref/OpenAlex/DOI/ISBN before
   merging into pipeline ``cited_sources``
4. ``section_plan`` — 8-section CNKI plan with target words +
   key argument per section

Why this exists: real-paper run data (4 选题 × 9 次 = 3 通过 6
失败, 5 of 6 failures = synthesizer 缺源) shows the pipeline
fails most often because the curator can't find ≥3 OA full-text
sources. The shadow baseline borrows the LLM's parametric
knowledge to bootstrap a candidate source list that PR-263
then verifies and feeds into the pipeline source pool. Codex
round-1 verdict (PR-262, AGREE Q5 + Q7 reorder): build the
runner first, then PR-263 enrichment, then PR-261 polish.

PR-262 v1 scope is INTENTIONALLY NARROW:
- LLM call + parse + persist artifact
- Manual trigger via API endpoint (PR-262 follow-up)
- Stub mode for tests
- NO automatic background trigger from proposal_accept
- NO consumption by other phases (that's PR-263)

This keeps the diff reviewable + lets us verify the LLM call
works before committing to async background plumbing.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, ValidationError, validator

from autoessay.agents._language import language_directive
from autoessay.agents._research_kernel_prompt import (
    KERNEL_INJECTION_GUARD,
    research_kernel_for_prompt,
)
from autoessay.clients.common import AccessStatus, NormalizedSource, VerificationStatus
from autoessay.config import get_settings
from autoessay.harness import (
    AuditWriter,
    HookContext,
    HookRegistry,
    LLMCallRequest,
    hash_text,
    run_llm_step,
)

_LOGGER = logging.getLogger(__name__)

SHADOW_BASELINE_SYSTEM_PROMPT = (
    "You are the SHADOW BASELINE agent. Your job is to generate a "
    "single-shot reference paper for the given research kernel + a "
    "structured plan, used as INTERNAL ANCHOR (never shown to user) "
    "for downstream polish + source-enrichment phases. "
    "Use your parametric knowledge of the field freely; do not "
    "fabricate citations — only include authors / works you are "
    "confident exist and that you would defend in an academic "
    "context. Treat this as a high-quality reference paper that "
    "another agent will benchmark against."
)

BASELINE_AS_EVIDENCE_SOURCE_ID = "shadow_baseline_v001"
BASELINE_AS_EVIDENCE_URL = "autoessay-shadow-baseline://v001"
_BASELINE_AS_EVIDENCE_SEGMENT_CHARS = 1200


SHADOW_BASELINE_OUTPUT_SCHEMA_HINT = {
    "manuscript_markdown": (
        "full ~6000-word zh CNKI-style paper with these blocks: "
        "摘要 (≥200 字) / 关键词 (5-8 个，分号分隔) / 一、引言 / "
        "二、文献综述 / 三、研究方法 / 四-六、案例分析 (一)(二)(三) / "
        "七、讨论 / 八、结论 / 参考文献 (≥10 条 GB/T 7714)。"
        "正文用 [N] 内联引用形式。"
    ),
    "argument_map": [
        {
            "section_id": "introduction",
            "central_claim": "本节中心论点（一句话）",
            "key_evidence": ["证据 1", "证据 2"],
        },
    ],
    "reference_candidates": [
        {
            "author": "作者姓 / Surname",
            "year": "2020",
            "title": "题名",
            "venue": "期刊名 / 出版社",
            "type": "article | book | chapter | archive",
            "doi_or_isbn": "10.x/y or null",
            "why_relevant": "30 字内说明它对本 kernel 的相关性",
        },
    ],
    "section_plan": [
        {
            "section_id": "introduction",
            "title": "一、引言",
            "target_words": 1200,
            "key_argument": "本节核心任务（一句话）",
        },
    ],
}


_SECTION_IDS = (
    "introduction",
    "historiography",
    "sources_method",
    "empirical_section_i",
    "empirical_section_ii",
    "empirical_section_iii",
    "discussion",
    "conclusion",
)


class ReferenceCandidate(BaseModel):
    """One candidate citation pulled from the model's parametric
    knowledge. PR-263 will verify each via Crossref / OpenAlex /
    DOI / ISBN before any of them enter pipeline ``cited_sources``."""

    author: str
    year: str
    title: str
    venue: str = ""
    type: str = "article"  # "article" | "book" | "chapter" | "archive"
    doi_or_isbn: str | None = None
    why_relevant: str = ""

    class Config:
        extra = "ignore"


class ArgumentMapEntry(BaseModel):
    section_id: str
    central_claim: str
    key_evidence: list[str] = Field(default_factory=list)

    class Config:
        extra = "ignore"


class SectionPlanEntry(BaseModel):
    section_id: str
    title: str
    target_words: int = 1200
    key_argument: str = ""

    class Config:
        extra = "ignore"


class ShadowBaselineOutput(BaseModel):
    manuscript_markdown: str
    argument_map: list[ArgumentMapEntry] = Field(default_factory=list)
    reference_candidates: list[ReferenceCandidate] = Field(default_factory=list)
    section_plan: list[SectionPlanEntry] = Field(default_factory=list)

    @validator("manuscript_markdown")
    def _manuscript_has_content(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("manuscript_markdown must be non-empty")
        return value

    class Config:
        extra = "ignore"


@dataclass
class ShadowBaselineResult:
    """In-memory return value from ``run_shadow_baseline``. The
    caller decides whether to persist; persistence helpers
    (``persist_shadow_baseline``, ``load_shadow_baseline``) live
    below for the API endpoint and downstream consumers."""

    output: ShadowBaselineOutput
    cached: bool


# ----- prompt builders --------------------------------------------


def _build_user_prompt(
    project_title: str,
    research_kernel: Mapping[str, Any] | None,
) -> str:
    """User-side prompt: the same kernel that the rest of the
    pipeline sees, plus an explicit reminder of the four output
    keys + the schema hint above. KERNEL_INJECTION_GUARD comes
    from the same shared helper drafter / ideator / critic /
    synthesizer all use."""
    kernel = research_kernel_for_prompt(research_kernel) if research_kernel else {}
    user_anchor = json.dumps(
        {
            "project_title": project_title,
            "research_kernel": dict(kernel) if kernel else {},
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return (
        f"User anchor: {user_anchor}. " + KERNEL_INJECTION_GUARD + " "
        f"Return strict JSON matching this schema: "
        f"{json.dumps(SHADOW_BASELINE_OUTPUT_SCHEMA_HINT, ensure_ascii=False, sort_keys=True)}. "
        "All four top-level keys are REQUIRED. ``argument_map`` must "
        "have one entry per body section_id (8 total: introduction, "
        "historiography, sources_method, empirical_section_i, "
        "empirical_section_ii, empirical_section_iii, discussion, "
        "conclusion). ``section_plan`` likewise. "
        "``reference_candidates`` must have ≥10 entries with real "
        "DOI / ISBN where you know them; null is acceptable when you "
        "don't, but the (author, year, title) triple must be a work "
        "you are confident exists."
    )


# ----- runner -----------------------------------------------------


def _stub_output() -> ShadowBaselineOutput:
    """Deterministic shadow baseline used by ``shadow_baseline_stub``
    in tests + CI. Matches the schema; content is minimal."""
    return ShadowBaselineOutput(
        manuscript_markdown=(
            "## 摘要\n\n本文为 stub-mode shadow baseline，仅用于测试。\n\n"
            "## 关键词\n\nstub；测试；shadow_baseline；占位\n\n"
            "## 一、引言\n\n占位正文。\n\n"
            "## 八、结论\n\n占位结论。\n\n"
            "## 参考文献\n\n[1] 张三. 某书. 北京:北京大学出版社, 2020.\n"
        ),
        argument_map=[
            ArgumentMapEntry(
                section_id=section_id,
                central_claim=f"stub central claim for {section_id}",
                key_evidence=["stub evidence"],
            )
            for section_id in _SECTION_IDS
        ],
        reference_candidates=[
            ReferenceCandidate(
                author="张三",
                year="2020",
                title="stub-mode 占位参考文献",
                venue="占位出版社",
                type="book",
                doi_or_isbn=None,
                why_relevant="stub",
            ),
        ],
        section_plan=[
            SectionPlanEntry(
                section_id=section_id,
                title=section_id.replace("_", " ").title(),
                target_words=1200,
                key_argument=f"stub key argument for {section_id}",
            )
            for section_id in _SECTION_IDS
        ],
    )


def _has_stub_true_field(value: object) -> bool:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if str(key).casefold() == "stub" and child is True:
                return True
            if _has_stub_true_field(child):
                return True
    if isinstance(value, list):
        return any(_has_stub_true_field(child) for child in value)
    return False


def _looks_like_stub_artifact(text: str) -> bool:
    """Return True for known pre-Slice-F shadow-baseline stubs.

    The real activation path must not reuse old stub files after the
    production default flips to model-backed generation. Keep the
    detector narrow around explicit stub markers and the pre-existing
    stub schema shape.
    """
    stripped = text.strip()
    if not stripped:
        return False
    lowered = stripped.casefold()
    if "baseline_v0_stub" in lowered:
        return True
    if "stub-mode shadow baseline" in lowered or "stub mode shadow baseline" in lowered:
        return True
    if "stub；测试；shadow_baseline；占位" in stripped:
        return True
    if "占位参考文献" in stripped and ("stub" in lowered or "shadow_baseline" in lowered):
        return True

    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError:
        return False
    if _has_stub_true_field(payload):
        return True
    if not isinstance(payload, Mapping):
        return False
    reference_candidates = payload.get("reference_candidates")
    if reference_candidates == []:
        return True
    payload_text = json.dumps(payload, ensure_ascii=False, sort_keys=True).casefold()
    return any(
        marker in payload_text
        for marker in (
            "baseline_v0_stub",
            "stub-mode",
            "stub central claim",
            "stub key argument",
            '"why_relevant": "stub"',
            "shadow baseline，仅用于测试",
            "shadow_baseline；占位",
        )
    )


def _cleanup_prior_stub_artifacts(run_dir: str | Path) -> None:
    """Remove stale stub artifacts before a real baseline is generated."""
    json_path, md_path = shadow_baseline_paths(run_dir)
    for path in (json_path, md_path):
        if not path.exists():
            continue
        try:
            existing = path.read_text(encoding="utf-8")
        except OSError as exc:
            _LOGGER.warning("Could not inspect shadow baseline artifact %s: %s", path, exc)
            continue
        if not _looks_like_stub_artifact(existing):
            continue
        try:
            path.unlink()
        except OSError as exc:
            _LOGGER.warning(
                "Could not remove stale shadow baseline stub artifact %s: %s",
                path,
                exc,
            )
        else:
            _LOGGER.warning("Removed stale shadow baseline stub artifact: %s", path)


def run_shadow_baseline(
    *,
    run_id: str,
    project_title: str,
    user_id: str | None,
    research_kernel: Mapping[str, Any] | None,
    audit: AuditWriter,
    hooks: HookRegistry | None = None,
    run_dir: str | Path | None = None,
) -> ShadowBaselineOutput | None:
    """Generate the shadow-baseline artifact in-memory. Returns
    ``None`` when the LLM response can't be parsed (callers fall
    back to the standard pipeline path with no anchor).

    ``audit`` is required so harness audit events flow into the
    same writer the calling phase uses (drafter / ideator / etc).
    ``hooks`` is optional — when omitted a fresh HookRegistry is
    created. The runner does NOT persist by itself; the API
    endpoint calls ``persist_shadow_baseline`` after a successful
    return so disk writes stay in the caller's transactional
    control.
    """
    settings = get_settings()
    if settings.shadow_baseline_stub:
        return _stub_output()
    if run_dir is not None:
        _cleanup_prior_stub_artifacts(run_dir)

    prompt = _build_user_prompt(project_title, research_kernel)
    messages = [
        {
            "role": "system",
            "content": SHADOW_BASELINE_SYSTEM_PROMPT + " " + language_directive("zh"),
        },
        {"role": "user", "content": prompt},
    ]
    request = LLMCallRequest(
        messages=messages,
        model=settings.one_api_model,
        temperature=0.4,
        # Bigger budget than per-section drafter (4500) because this
        # has to emit the whole 6000-word manuscript + the three
        # structured plan blocks in one shot. 16000 is what the
        # provider gateway accepts for gpt-5.5.
        max_tokens=16000,
        response_format={"type": "json_object"},
        request_id=f"shadow_baseline_{run_id}",
        prompt_template_id="shadow_baseline.v1",
    )
    context = HookContext(
        run_id=run_id,
        phase="shadow_baseline",
        step_id="shadow_baseline.compose",
        user_id=user_id,
        attempt=0,
        prompt_template_id=request.prompt_template_id,
        prompt_filled=prompt,
        prompt_hash=hash_text(prompt),
        project_title=project_title,
        run_metadata={"agent_phase": "shadow_baseline"},
    )
    hook_registry = hooks if hooks is not None else HookRegistry()
    response = asyncio.run(
        run_llm_step(
            request=request,
            hooks=hook_registry,
            context=context,
            output_schema=ShadowBaselineOutput,
            audit=audit,
            max_corrective_retries=settings.drafter_max_corrective_retries,
            llm_optional=False,
        ),
    )
    parsed = response.parsed
    if isinstance(parsed, ShadowBaselineOutput):
        return parsed
    if isinstance(parsed, Mapping):
        try:
            return ShadowBaselineOutput.parse_obj(parsed)
        except ValidationError:
            return None
    return None


# ----- persistence helpers ----------------------------------------


def shadow_baseline_dir(run_dir: str | Path) -> Path:
    return Path(run_dir) / "shadow_baseline"


def shadow_baseline_paths(run_dir: str | Path) -> tuple[Path, Path]:
    """Returns (json_path, markdown_path) under ``run_dir``."""
    base = shadow_baseline_dir(run_dir)
    return (base / "baseline_v001.json", base / "baseline_v001.md")


def persist_shadow_baseline(
    run_dir: str | Path,
    output: ShadowBaselineOutput,
) -> tuple[Path, Path]:
    """Write the artifact to disk. JSON gets the full structured
    output (used by PR-263 enrichment + PR-261 polish); markdown
    is the standalone manuscript so operators can eyeball it
    without parsing JSON."""
    json_path, md_path = shadow_baseline_paths(run_dir)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(
        json.dumps(output.dict(), ensure_ascii=False, sort_keys=True, indent=2),
        encoding="utf-8",
    )
    md_path.write_text(output.manuscript_markdown, encoding="utf-8")
    return json_path, md_path


def load_shadow_baseline(run_dir: str | Path) -> ShadowBaselineOutput | None:
    """Read a previously-persisted artifact. Returns ``None`` if
    no baseline has been generated yet OR if the file is corrupt
    (in which case downstream callers regenerate)."""
    json_path, _ = shadow_baseline_paths(run_dir)
    if not json_path.exists():
        return None
    try:
        raw = json_path.read_text(encoding="utf-8")
        if not get_settings().shadow_baseline_stub and _looks_like_stub_artifact(raw):
            _cleanup_prior_stub_artifacts(run_dir)
            return None
        payload = json.loads(raw)
        return ShadowBaselineOutput.parse_obj(payload)
    except (OSError, json.JSONDecodeError, ValidationError):
        return None


def maybe_inject_baseline_as_evidence_source(run_dir: str | Path) -> bool:
    """Expose the shadow baseline as a TEST-only approved source.

    The default production behavior remains unchanged. When
    ``AUTOESSAY_BASELINE_AS_EVIDENCE_TEST=1``, this helper upserts one
    synthetic source into ``sources/shortlist.json`` and writes a source
    note under ``synthesis/source_notes``. Downstream agents already
    trust those two artifacts for citation legality, so critic /
    final_rewrite do not need a separate exception path.
    """
    settings = get_settings()
    if not settings.baseline_as_evidence_test:
        return False

    base = Path(run_dir)
    output = load_shadow_baseline(base)
    if output is None or not output.manuscript_markdown.strip():
        return False

    segments = split_shadow_baseline_into_source_segments(output.manuscript_markdown)
    source = _baseline_as_evidence_source(output, segments)
    _upsert_source_json(base / "sources" / "shortlist.json", source)
    _write_baseline_as_evidence_note(
        base / "synthesis" / "source_notes" / f"{BASELINE_AS_EVIDENCE_SOURCE_ID}.json",
        segments,
    )
    return True


def split_shadow_baseline_into_source_segments(manuscript_markdown: str) -> list[dict[str, str]]:
    """Split the baseline manuscript into source-note-sized chunks.

    Paragraphs are preserved where possible, with ``参考文献`` omitted
    so the drafter grounds arguments in the baseline prose rather than
    treating the baseline's bibliography as independently verified.
    """
    body = manuscript_markdown.strip()
    if "\\n" in body and body.count("\n") < max(2, body.count("\\n") // 2):
        body = body.replace("\\n", "\n")
    ref_match = re.search(r"(?m)^\s*#{0,6}\s*参考文献\s*$", body)
    if ref_match:
        body = body[: ref_match.start()].strip()

    raw_blocks = [block.strip() for block in re.split(r"\n\s*\n+", body) if block.strip()]
    paragraphs: list[str] = []
    for block in raw_blocks:
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        if not lines:
            continue
        if len(lines) == 1 and re.match(
            r"^\s*#{0,6}\s*(摘要|关键词|[一二三四五六七八九十]、)",
            lines[0],
        ):
            continue
        paragraph = re.sub(r"\s+", " ", " ".join(lines)).strip()
        if len(paragraph) < 30:
            continue
        paragraphs.append(paragraph)

    if not paragraphs and body:
        paragraphs = [body[:_BASELINE_AS_EVIDENCE_SEGMENT_CHARS]]

    segments: list[dict[str, str]] = []
    current: list[str] = []
    current_len = 0
    for paragraph in paragraphs:
        projected = current_len + len(paragraph) + (2 if current else 0)
        if current and projected > _BASELINE_AS_EVIDENCE_SEGMENT_CHARS:
            segments.append(
                {
                    "segment_id": f"sb-p{len(segments) + 1:03d}",
                    "text": "\n\n".join(current),
                }
            )
            current = []
            current_len = 0
        if len(paragraph) > _BASELINE_AS_EVIDENCE_SEGMENT_CHARS:
            for index in range(0, len(paragraph), _BASELINE_AS_EVIDENCE_SEGMENT_CHARS):
                chunk = paragraph[index : index + _BASELINE_AS_EVIDENCE_SEGMENT_CHARS].strip()
                if chunk:
                    segments.append(
                        {
                            "segment_id": f"sb-p{len(segments) + 1:03d}",
                            "text": chunk,
                        }
                    )
            continue
        current.append(paragraph)
        current_len += len(paragraph) + (2 if current_len else 0)
    if current:
        segments.append(
            {
                "segment_id": f"sb-p{len(segments) + 1:03d}",
                "text": "\n\n".join(current),
            }
        )
    return segments


def _baseline_as_evidence_source(
    output: ShadowBaselineOutput,
    segments: list[dict[str, str]],
) -> NormalizedSource:
    abstract = " ".join(segment["text"] for segment in segments[:2]).strip()
    if not abstract:
        abstract = output.manuscript_markdown[:1000].strip()
    return NormalizedSource(
        source_id=BASELINE_AS_EVIDENCE_SOURCE_ID,
        title="Shadow Baseline Evidence Dossier v001",
        authors=["AutoEssay Shadow Baseline"],
        year=None,
        venue="AutoEssay baseline evidence dossier",
        doi=None,
        url=BASELINE_AS_EVIDENCE_URL,
        pdf_url=None,
        abstract=abstract[:2000],
        source_client="shadow_baseline",
        access_status=AccessStatus.OPEN,
        license="baseline-as-evidence-test",
        rank_score=9.9,
        risk_flags=["baseline_as_evidence_test_only"],
        research_role="core_evidence",
        provenance="shadow_baseline",
        canonical_bucket="frontier",
        canonical_rationale=(
            "TEST-only synthetic source from the run's shadow baseline manuscript; "
            "enabled only by AUTOESSAY_BASELINE_AS_EVIDENCE_TEST."
        ),
        verified_by="baseline_as_evidence_test",
        verification_status=VerificationStatus.VERIFIED,
        confidence=1.0,
    )


def _write_baseline_as_evidence_note(path: Path, segments: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "source_id": BASELINE_AS_EVIDENCE_SOURCE_ID,
        "title": "Shadow Baseline Evidence Dossier v001",
        "thesis": "TEST-only shadow baseline manuscript, segmented as an approved evidence source.",
        "evidence": (
            "Use these baseline prose segments to ground claims only when "
            "AUTOESSAY_BASELINE_AS_EVIDENCE_TEST is enabled; paraphrase and add analysis."
        ),
        "method": "Synthetic source-note projection of the persisted shadow_baseline artifact.",
        "limits": (
            "Test-only; not production evidence; do not copy sentences or paragraphs. "
            "The anti-plagiarism n-gram gate remains authoritative."
        ),
        "baseline_as_evidence_test": True,
        "segments": segments,
    }
    path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def _upsert_source_json(path: Path, source: NormalizedSource) -> None:
    records: list[object] = []
    if path.exists():
        try:
            decoded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(decoded, list):
                records = decoded
        except json.JSONDecodeError:
            records = []
    source_payload = dict(source.dict())
    updated: list[object] = []
    found = False
    for record in records:
        if isinstance(record, Mapping) and record.get("source_id") == source.source_id:
            updated.append(source_payload)
            found = True
        else:
            updated.append(record)
    if not found:
        updated.append(source_payload)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(updated, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
