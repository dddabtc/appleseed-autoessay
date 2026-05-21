"""Detailed outline — paper-quality-spec.md three-stage workflow §2.

Runs after the Ideator has produced angle cards. For every angle card
the user is about to choose between, generates a per-section outline
with six attributes per section:

- function (the role this section plays in the argument)
- argument (the sub-claim the section will defend)
- literature (which sources support the argument here)
- materials (data / case / archive the section relies on)
- relation_to_thesis (how this section advances the central thesis)
- weakness (the most plausible objection or evidence gap)

The artifact lands in ``novelty/detailed_outlines.{md,json}`` so the
USER_NOVELTY_REVIEW screen can show the user not just *which angle*
they are picking but *what paper* that angle commits them to writing.
The Drafter consumes this in a follow-up — for now it is informational
review material.

Stub mode (``AUTOESSAY_DETAILED_OUTLINE_STUB=1``) returns a
deterministic 5-section skeleton per angle so tests don't need a
live LLM. Any LLM / parse error fails-open to the same stub
skeleton — the ideator must never crash because the outline LLM
hiccupped, and an empty outline would silently lose the artifact.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel, ValidationError

from autoessay.agents._language import language_directive
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

# Default 5-section skeleton used by the stub and as a fallback when
# the LLM omits an angle. Section ids match Drafter conventions where
# they overlap.
_STUB_SECTION_TEMPLATE: tuple[tuple[str, dict[str, str]], ...] = (
    (
        "introduction",
        {
            "zh": "引言",
            "ja": "序論",
            "en": "Introduction",
        },
    ),
    (
        "literature_review",
        {
            "zh": "文献综述",
            "ja": "先行研究",
            "en": "Literature Review",
        },
    ),
    (
        "framework",
        {
            "zh": "理论框架",
            "ja": "理論枠組",
            "en": "Theoretical Framework",
        },
    ),
    (
        "empirical",
        {
            "zh": "正文核心论证",
            "ja": "本論",
            "en": "Empirical Section",
        },
    ),
    (
        "conclusion",
        {
            "zh": "结论",
            "ja": "結論",
            "en": "Conclusion",
        },
    ),
)


@dataclass(frozen=True)
class OutlineSection:
    section_id: str
    title: str
    function: str
    argument: str
    literature: str
    materials: str
    relation_to_thesis: str
    weakness: str


@dataclass(frozen=True)
class AngleOutline:
    angle_id: str
    working_title: str
    sections: tuple[OutlineSection, ...]


class _LLMOutlineSection(BaseModel):
    section_id: str = ""
    title: str = ""
    function: str = ""
    argument: str = ""
    literature: str = ""
    materials: str = ""
    relation_to_thesis: str = ""
    weakness: str = ""

    class Config:
        extra = "ignore"


class _LLMAngleOutline(BaseModel):
    angle_id: str
    working_title: str = ""
    sections: list[_LLMOutlineSection] = []

    class Config:
        extra = "ignore"


class _LLMOutput(BaseModel):
    outlines: list[_LLMAngleOutline] = []

    class Config:
        extra = "ignore"


def build_detailed_outlines(
    *,
    run: Run,
    session: Session,
    angle_cards: Sequence[Mapping[str, object]],
    claims: Sequence[Mapping[str, object]],
    source_notes: Mapping[str, object],
    project_title: str,
    project_language: str,
) -> tuple[AngleOutline, ...]:
    """Return a per-angle detailed outline tuple.

    PR-D2.1 (2026-05-03): the LLM call now goes through
    ``harness.run_llm_step``. Caller (ideator) must thread ``run`` and
    ``session`` so the standalone ``AuditWriter`` can be created.
    """
    if not angle_cards:
        return ()
    settings = get_settings()
    if getattr(settings, "detailed_outline_stub", False):
        return _stub_outlines(angle_cards, project_language)
    audit = AuditWriter(
        session=session,
        run_dir=Path(run.run_dir),
        agent_name="DetailedOutline",
    )
    try:
        result = asyncio.run(
            _run_outlines_via_llm(
                run=run,
                audit=audit,
                angle_cards=angle_cards,
                claims=claims,
                source_notes=source_notes,
                project_title=project_title,
                project_language=project_language,
            ),
        )
    except Exception:  # noqa: BLE001 — ideator must not crash on hiccup
        return _stub_outlines(angle_cards, project_language)
    if not result:
        return _stub_outlines(angle_cards, project_language)
    return result


def _stub_outlines(
    angle_cards: Sequence[Mapping[str, object]],
    project_language: str,
) -> tuple[AngleOutline, ...]:
    code = (project_language or "en").lower()
    out: list[AngleOutline] = []
    for card in angle_cards:
        angle_id = str(card.get("angle_id") or "").strip()
        if not angle_id:
            continue
        working_title = str(card.get("working_title") or "").strip()
        sections = tuple(
            OutlineSection(
                section_id=section_id,
                title=titles.get(code, titles["en"]),
                function="",
                argument="",
                literature="",
                materials="",
                relation_to_thesis="",
                weakness="",
            )
            for section_id, titles in _STUB_SECTION_TEMPLATE
        )
        out.append(
            AngleOutline(
                angle_id=angle_id,
                working_title=working_title,
                sections=sections,
            ),
        )
    return tuple(out)


async def _run_outlines_via_llm(
    *,
    run: Run,
    audit: AuditWriter,
    angle_cards: Sequence[Mapping[str, object]],
    claims: Sequence[Mapping[str, object]],
    source_notes: Mapping[str, object],
    project_title: str,
    project_language: str,
) -> tuple[AngleOutline, ...]:
    settings = get_settings()
    model = getattr(settings, "one_api_model", None) or "gpt-5.4-mini"
    angle_digest = [
        {
            "angle_id": card.get("angle_id"),
            "working_title": card.get("working_title"),
            "thesis": card.get("thesis_one_sentence"),
            "key_claim_ids": card.get("key_claim_ids"),
            "evidence_so_far": card.get("evidence_so_far"),
            "missing_evidence": card.get("missing_evidence"),
        }
        for card in angle_cards
    ]
    source_digest = _compact_source_notes(source_notes)
    schema = {
        "outlines": [
            {
                "angle_id": "from input verbatim",
                "working_title": "the angle's working title",
                "sections": [
                    {
                        "section_id": "introduction | literature_review | framework | "
                        "empirical | empirical_i | empirical_ii | conclusion",
                        "title": "section heading in the project language",
                        "function": "what this section does in the argument",
                        "argument": "the sub-claim defended here",
                        "literature": "which sources are used and how",
                        "materials": "data / archive / case the section uses",
                        "relation_to_thesis": "how this section advances the thesis",
                        "weakness": "most plausible objection or evidence gap",
                    },
                ],
            },
        ],
    }
    system = (
        "You are a paper architect. For each candidate angle, design a "
        "5-7 section outline that the Drafter can follow. Every section "
        "must reference real sources from the supplied digest by id. "
        "Do not invent sources or evidence. Return strict JSON only. "
        + language_directive(project_language)
    )
    user = (
        f"Project title: {project_title}\n\n"
        f"Candidate angles: {json.dumps(angle_digest, ensure_ascii=False)[:8000]}\n\n"
        "Source digest (id -> thesis / method / evidence / limits):\n"
        f"{json.dumps(source_digest, ensure_ascii=False)[:14000]}\n\n"
        f"Claims total: {len(claims)}\n\n"
        f"Return strict JSON matching this schema: "
        f"{json.dumps(schema, ensure_ascii=False)}"
    )
    request = LLMCallRequest(
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        model=model,
        temperature=0.2,
        max_tokens=3200,
        response_format={"type": "json_object"},
        request_id="detailed_outline",
        prompt_template_id="detailed_outline.v1",
    )
    context = HookContext(
        run_id=run.id,
        phase="ideator",
        step_id="detailed_outline",
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
        return ()
    parsed = response.parsed
    if not isinstance(parsed, _LLMOutput):
        try:
            parsed = _LLMOutput.parse_obj(parsed if isinstance(parsed, dict) else {})
        except ValidationError:
            return ()
    by_angle_id: dict[str, _LLMAngleOutline] = {}
    for entry in parsed.outlines:
        if entry.angle_id and entry.angle_id not in by_angle_id:
            by_angle_id[entry.angle_id] = entry
    out: list[AngleOutline] = []
    for card in angle_cards:
        angle_id = str(card.get("angle_id") or "").strip()
        if not angle_id:
            continue
        working_title = str(card.get("working_title") or "").strip()
        match = by_angle_id.get(angle_id)
        if match is None or not match.sections:
            continue
        sections = tuple(
            OutlineSection(
                section_id=_clean(section.section_id) or f"section_{idx + 1}",
                title=_clean(section.title),
                function=_clean(section.function),
                argument=_clean(section.argument),
                literature=_clean(section.literature),
                materials=_clean(section.materials),
                relation_to_thesis=_clean(section.relation_to_thesis),
                weakness=_clean(section.weakness),
            )
            for idx, section in enumerate(match.sections)
        )
        out.append(
            AngleOutline(
                angle_id=angle_id,
                working_title=working_title or _clean(match.working_title),
                sections=sections,
            ),
        )
    return tuple(out)


def _clean(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())


def _compact_source_notes(source_notes: Mapping[str, object]) -> dict[str, dict[str, str]]:
    digest: dict[str, dict[str, str]] = {}
    for source_id, note in source_notes.items():
        if not isinstance(note, Mapping):
            continue
        digest[source_id] = {
            "thesis": _truncate(note.get("thesis"), 200),
            "method": _truncate(note.get("method"), 140),
            "evidence": _truncate(note.get("evidence"), 200),
            "limits": _truncate(note.get("limits"), 140),
        }
    return digest


def _truncate(value: object, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    cleaned = " ".join(value.split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 1].rstrip() + "…"


_TITLE_BY_CODE: dict[str, str] = {
    "zh": "详细大纲",
    "ja": "詳細アウトライン",
    "en": "Detailed Outlines",
}
_LABELS_BY_CODE: dict[str, dict[str, str]] = {
    "zh": {
        "title": "题名",
        "function": "本节作用",
        "argument": "核心论点",
        "literature": "文献使用",
        "materials": "材料",
        "relation_to_thesis": "与中心论点关系",
        "weakness": "潜在弱点",
        "empty": "（待补）",
    },
    "ja": {
        "title": "題名",
        "function": "節の役割",
        "argument": "中心論点",
        "literature": "文献の使い方",
        "materials": "資料",
        "relation_to_thesis": "中心命題との関係",
        "weakness": "弱点",
        "empty": "（未記入）",
    },
    "en": {
        "title": "Title",
        "function": "Function",
        "argument": "Argument",
        "literature": "Literature",
        "materials": "Materials",
        "relation_to_thesis": "Relation to thesis",
        "weakness": "Weakness",
        "empty": "(to be filled)",
    },
}


def render_outlines_markdown(outlines: Sequence[AngleOutline], project_language: str) -> str:
    if not outlines:
        return ""
    code = (project_language or "en").lower()
    labels = _LABELS_BY_CODE.get(code, _LABELS_BY_CODE["en"])
    title = _TITLE_BY_CODE.get(code, "Detailed Outlines")
    empty = labels["empty"]
    lines: list[str] = [f"# {title}", ""]
    for outline in outlines:
        header = f"{outline.working_title} (`{outline.angle_id}`)"
        lines.append(f"## {header}")
        lines.append("")
        for section in outline.sections:
            section_label = section.title or section.section_id
            lines.append(f"### {section_label}")
            lines.append("")
            lines.append(f"- **{labels['function']}**: {section.function or empty}")
            lines.append(f"- **{labels['argument']}**: {section.argument or empty}")
            lines.append(f"- **{labels['literature']}**: {section.literature or empty}")
            lines.append(f"- **{labels['materials']}**: {section.materials or empty}")
            lines.append(
                f"- **{labels['relation_to_thesis']}**: {section.relation_to_thesis or empty}"
            )
            lines.append(f"- **{labels['weakness']}**: {section.weakness or empty}")
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def outlines_to_dict(outlines: Sequence[AngleOutline]) -> dict[str, object]:
    return {
        "outlines": [
            {
                "angle_id": outline.angle_id,
                "working_title": outline.working_title,
                "sections": [
                    {
                        "section_id": section.section_id,
                        "title": section.title,
                        "function": section.function,
                        "argument": section.argument,
                        "literature": section.literature,
                        "materials": section.materials,
                        "relation_to_thesis": section.relation_to_thesis,
                        "weakness": section.weakness,
                    }
                    for section in outline.sections
                ],
            }
            for outline in outlines
        ],
    }


__all__ = [
    "AngleOutline",
    "OutlineSection",
    "build_detailed_outlines",
    "outlines_to_dict",
    "render_outlines_markdown",
]
