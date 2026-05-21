"""Material diagnostic — paper-quality-spec.md three-stage workflow §1.

Runs after Synthesizer has finished summarising the deep-dive sources.
Produces a short artifact that:

- Decides whether the assembled material is *sufficient* to defend a
  research argument on the current project topic.
- Proposes three candidate research questions / paper titles that the
  literature can actually support, ranked by feasibility.
- Lists specific missing materials so the user knows what to add if
  they want to widen the argument.
- Flags risks (recency gap, language coverage, selection bias).

The artifact is written to ``synthesis/material_diagnostic.{md,json}``
just before the USER_FIELD_REVIEW transition. It is informational —
the existing ``synthesizer_min_processed_sources`` threshold still
hard-fails when there is too little material; the diagnostic is for
the more nuanced "yes-the-LLM-ran but the topic is too ambitious"
case where the user needs to pick a narrower angle.

Stub mode (``AUTOESSAY_MATERIAL_DIAGNOSTIC_STUB=1``) returns a
placeholder that always says "incomplete" so tests don't need a
live LLM. Any LLM / parse error fails-open to the same incomplete
report — the synthesizer must never crash because the diagnostic
LLM hiccupped.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ValidationError, validator

from autoessay.agents._language import language_directive
from autoessay.agents.phase_context import phase_context_prompt_block
from autoessay.config import get_settings
from autoessay.harness import (
    AuditWriter,
    HookContext,
    HookRegistry,
    LLMCallRequest,
    hash_text,
    run_llm_step,
)
from autoessay.harness.runner import SchemaViolationError

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from autoessay.models import Run

RecommendedAction = Literal["proceed", "iterate", "incomplete"]


@dataclass(frozen=True)
class MaterialDiagnostic:
    sufficient: bool
    candidate_titles: tuple[str, ...]
    missing_materials: tuple[str, ...]
    risks: tuple[str, ...]
    recommended_action: RecommendedAction
    rationale: str


class _LLMOutput(BaseModel):
    sufficient: bool = False
    candidate_titles: list[str] = []
    missing_materials: list[str] = []
    risks: list[str] = []
    recommended_action: str = "incomplete"
    rationale: str = ""

    @validator("recommended_action")
    def _check_action(cls, value: str) -> str:
        cleaned = value.strip().lower()
        if cleaned not in {"proceed", "iterate", "incomplete"}:
            return "incomplete"
        return cleaned

    class Config:
        extra = "ignore"


def run_material_diagnostic(
    *,
    run: Run,
    session: Session,
    project_title: str,
    project_language: str,
    source_notes: Mapping[str, Mapping[str, object]],
    claims: Sequence[Mapping[str, object]],
    proposal: Mapping[str, object] | None = None,
) -> MaterialDiagnostic:
    """Return a MaterialDiagnostic for the assembled synthesis bundle.

    PR-D2.1 (2026-05-03): the LLM call now goes through
    ``harness.run_llm_step`` so audit + sentinel + corrective-retry
    are unified with the rest of the pipeline. Caller (synthesizer)
    must thread ``run`` and ``session`` so the standalone
    ``AuditWriter`` can be created with a real ``run_dir``.
    """
    settings = get_settings()
    if getattr(settings, "material_diagnostic_stub", False):
        return _stub_diagnostic()
    if not source_notes:
        return _incomplete_diagnostic("no synthesizer source notes available")
    audit = AuditWriter(
        session=session,
        run_dir=Path(run.run_dir),
        agent_name="MaterialDiagnostic",
    )
    try:
        result = asyncio.run(
            _run_diagnostic_via_llm(
                run=run,
                audit=audit,
                project_title=project_title,
                project_language=project_language,
                source_notes=source_notes,
                claims=claims,
                proposal=proposal,
            ),
        )
    except Exception:  # noqa: BLE001 — synthesizer must not crash on hiccup
        return _incomplete_diagnostic("material diagnostic LLM call raised an exception")
    if result is None:
        return _incomplete_diagnostic("material diagnostic LLM returned an unparseable response")
    return result


def _stub_diagnostic() -> MaterialDiagnostic:
    return MaterialDiagnostic(
        sufficient=False,
        candidate_titles=(),
        missing_materials=(),
        risks=(),
        recommended_action="incomplete",
        rationale="stub mode",
    )


def _incomplete_diagnostic(reason: str) -> MaterialDiagnostic:
    return MaterialDiagnostic(
        sufficient=False,
        candidate_titles=(),
        missing_materials=(),
        risks=(),
        recommended_action="incomplete",
        rationale=reason,
    )


async def _run_diagnostic_via_llm(
    *,
    run: Run,
    audit: AuditWriter,
    project_title: str,
    project_language: str,
    source_notes: Mapping[str, Mapping[str, object]],
    claims: Sequence[Mapping[str, object]],
    proposal: Mapping[str, object] | None,
) -> MaterialDiagnostic | None:
    settings = get_settings()
    model = getattr(settings, "one_api_model", None) or "gpt-5.4-mini"
    digest = _source_digest(source_notes, claims)
    schema = {
        "sufficient": "true | false — whether the gathered material can support a defensible paper",
        "candidate_titles": [
            "exactly 3 narrow research questions or paper titles the "
            "literature can actually support",
        ],
        "missing_materials": [
            "concrete materials still missing if the user wants to broaden the argument",
        ],
        "risks": ["recency gap, selection bias, language coverage, etc."],
        "recommended_action": "proceed | iterate",
        "rationale": "one-sentence justification grounded in the source digest",
    }
    proposal_summary = {
        "research_question": (proposal or {}).get("research_question"),
        "scope": (proposal or {}).get("scope"),
    }
    accumulated_context = phase_context_prompt_block(run.run_dir, "material_diagnostic")
    system = (
        "You are a strict review editor. Decide whether the literature "
        "the Synthesizer assembled is enough to defend a paper on the "
        "given project topic. Be candid: if the sources are off-topic, "
        "thin, or skewed, say so and recommend 'iterate'. Never "
        "fabricate sources or claims. Return strict JSON only. "
        + language_directive(project_language)
    )
    user = (
        f"Project title: {project_title}\n"
        f"Proposal summary: {json.dumps(proposal_summary, ensure_ascii=False)}\n\n"
        f"{accumulated_context}"
        "Source digest (one entry per processed source):\n"
        f"{json.dumps(digest, ensure_ascii=False)[:18000]}\n\n"
        f"Return strict JSON matching this schema: "
        f"{json.dumps(schema, ensure_ascii=False)}"
    )
    request = LLMCallRequest(
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        model=model,
        temperature=0.1,
        max_tokens=1400,
        response_format={"type": "json_object"},
        request_id="material_diagnostic",
        prompt_template_id="material_diagnostic.v1",
    )
    context = HookContext(
        run_id=run.id,
        phase="synthesizer",
        step_id="material_diagnostic",
        user_id=None,
        attempt=1,
        prompt_template_id=request.prompt_template_id,
        prompt_filled=user,
        prompt_hash=hash_text(user),
        project_title=project_title,
    )
    audit.start_invocation(context)
    try:
        response = await run_llm_step(
            request=request,
            hooks=HookRegistry(),
            context=context,
            output_schema=_LLMOutput,
            audit=audit,
        )
    except SchemaViolationError:
        return None
    parsed = response.parsed
    if not isinstance(parsed, _LLMOutput):
        try:
            parsed = _LLMOutput.parse_obj(parsed if isinstance(parsed, dict) else {})
        except ValidationError:
            return None
    candidate_titles = tuple(_clean_strings(parsed.candidate_titles))[:3]
    missing = tuple(_clean_strings(parsed.missing_materials))
    risks = tuple(_clean_strings(parsed.risks))
    return MaterialDiagnostic(
        sufficient=bool(parsed.sufficient),
        candidate_titles=candidate_titles,
        missing_materials=missing,
        risks=risks,
        recommended_action=parsed.recommended_action,  # type: ignore[arg-type]
        rationale=parsed.rationale.strip(),
    )


def _clean_strings(values: Sequence[object]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, str):
            continue
        cleaned = " ".join(value.split())
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        out.append(cleaned)
    return out


def _source_digest(
    source_notes: Mapping[str, Mapping[str, object]],
    claims: Sequence[Mapping[str, object]],
) -> list[Mapping[str, object]]:
    """One compact entry per source: id / thesis / method / evidence / limits.

    Truncates each field to ~280 chars so the prompt stays tractable
    even with 10+ sources.
    """
    digest: list[Mapping[str, object]] = []
    for source_id, note in source_notes.items():
        if not isinstance(note, Mapping):
            continue
        digest.append(
            {
                "source_id": source_id,
                "thesis": _truncate(note.get("thesis"), 280),
                "method": _truncate(note.get("method"), 200),
                "evidence": _truncate(note.get("evidence"), 280),
                "limits": _truncate(note.get("limits"), 200),
            },
        )
    if claims:
        digest.append({"claims_total": len(claims)})
    return digest


def _truncate(value: object, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    cleaned = " ".join(value.split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 1].rstrip() + "…"


_TITLE_BY_CODE: dict[str, str] = {
    "zh": "资料诊断",
    "ja": "資料診断",
    "en": "Material Diagnostic",
}
_LABELS_BY_CODE: dict[str, dict[str, str]] = {
    "zh": {
        "sufficient": "资料是否充分",
        "recommended_action": "建议下一步",
        "candidate_titles": "三个可行的研究问题 / 题名",
        "missing_materials": "尚缺资料",
        "risks": "风险提示",
        "rationale": "判断理由",
        "true": "是",
        "false": "否",
        "proceed": "可继续生成大纲",
        "iterate": "建议先调整选题或补充资料",
        "incomplete": "诊断未完成",
        "none": "（无）",
    },
    "ja": {
        "sufficient": "資料は十分か",
        "recommended_action": "推奨される次の手",
        "candidate_titles": "実行可能な3つの研究課題 / 題名",
        "missing_materials": "不足している資料",
        "risks": "リスク",
        "rationale": "判断理由",
        "true": "はい",
        "false": "いいえ",
        "proceed": "アウトラインへ進む",
        "iterate": "テーマ調整または資料補充を推奨",
        "incomplete": "診断未完了",
        "none": "（なし）",
    },
    "en": {
        "sufficient": "Material is sufficient",
        "recommended_action": "Recommended next step",
        "candidate_titles": "Three feasible research questions / titles",
        "missing_materials": "Missing materials",
        "risks": "Risks",
        "rationale": "Rationale",
        "true": "Yes",
        "false": "No",
        "proceed": "Proceed to outline",
        "iterate": "Iterate on the topic or add sources before continuing",
        "incomplete": "Diagnostic incomplete",
        "none": "(none)",
    },
}


def render_material_diagnostic_markdown(
    diagnostic: MaterialDiagnostic, project_language: str
) -> str:
    code = (project_language or "en").lower()
    labels = _LABELS_BY_CODE.get(code, _LABELS_BY_CODE["en"])
    title = _TITLE_BY_CODE.get(code, "Material Diagnostic")
    sufficient_label = labels["true"] if diagnostic.sufficient else labels["false"]
    action_label = labels.get(diagnostic.recommended_action, diagnostic.recommended_action)
    lines: list[str] = [
        f"# {title}",
        "",
        f"- **{labels['sufficient']}**: {sufficient_label}",
        f"- **{labels['recommended_action']}**: {action_label}",
        "",
        f"## {labels['candidate_titles']}",
        "",
    ]
    if diagnostic.candidate_titles:
        for idx, title_text in enumerate(diagnostic.candidate_titles, start=1):
            lines.append(f"{idx}. {title_text}")
    else:
        lines.append(labels["none"])
    lines.extend(["", f"## {labels['missing_materials']}", ""])
    if diagnostic.missing_materials:
        for item in diagnostic.missing_materials:
            lines.append(f"- {item}")
    else:
        lines.append(labels["none"])
    lines.extend(["", f"## {labels['risks']}", ""])
    if diagnostic.risks:
        for item in diagnostic.risks:
            lines.append(f"- {item}")
    else:
        lines.append(labels["none"])
    lines.extend(
        [
            "",
            f"## {labels['rationale']}",
            "",
            diagnostic.rationale or labels["none"],
            "",
        ],
    )
    return "\n".join(lines).rstrip() + "\n"


def diagnostic_to_dict(diagnostic: MaterialDiagnostic) -> dict[str, object]:
    return {
        "sufficient": diagnostic.sufficient,
        "candidate_titles": list(diagnostic.candidate_titles),
        "missing_materials": list(diagnostic.missing_materials),
        "risks": list(diagnostic.risks),
        "recommended_action": diagnostic.recommended_action,
        "rationale": diagnostic.rationale,
    }


__all__ = [
    "MaterialDiagnostic",
    "diagnostic_to_dict",
    "render_material_diagnostic_markdown",
    "run_material_diagnostic",
]
