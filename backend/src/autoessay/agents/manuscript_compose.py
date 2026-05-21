"""Compose the final manuscript by wrapping the body with title /
authors / abstract / keywords / references — the standard structure of
an academic paper.

The Drafter / Stylist pipeline produces only the body sections. This
module fills in the front matter (title, authors, abstract, keywords)
and the back matter (numbered references list), so the manuscript
exported to .md / .docx / .html actually looks like a paper.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel, ValidationError, validator

from autoessay.agents._humanizer import humanizer_directive
from autoessay.agents._language import language_directive
from autoessay.clients.common import NormalizedSource
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

# How much of the body to include when asking the LLM to summarise. The
# Drafter output for a 5-section Chinese paper is usually 3-6k chars.
# Cap at 12k to stay well under the model's prompt budget.
FRONT_MATTER_BODY_CHAR_LIMIT = 12000


@dataclass(frozen=True)
class FrontMatter:
    """Title / abstract / keywords for one paper.

    ``title`` is the refined paper title (may differ from
    ``project.title`` which is the user-typed topic). ``abstract`` is
    a short paragraph (about 150-300 chars in zh, 80-200 words in en).
    ``keywords`` is 3-6 short noun phrases. Empty fields are tolerated;
    callers fall back to project.title and skip the abstract/keywords
    block.
    """

    title: str
    abstract: str
    keywords: list[str]


class FrontMatterOutput(BaseModel):
    """LLM output schema."""

    title: str
    abstract: str
    keywords: list[str]

    @validator("title", "abstract")
    def _trim(cls, value: str) -> str:
        return value.strip()

    @validator("keywords")
    def _clean_keywords(cls, value: list[str]) -> list[str]:
        cleaned: list[str] = []
        for item in value:
            text = " ".join(str(item).split())
            if text:
                cleaned.append(text)
        return cleaned[:6]

    class Config:
        extra = "ignore"


def compose_manuscript(
    *,
    run: Run,
    session: Session,
    body_markdown: str,
    project_title: str,
    project_language: str,
    authors: Sequence[str],
    cited_sources: Sequence[NormalizedSource],
    selected_thesis: Mapping[str, object] | None = None,
) -> str:
    """Return the full manuscript markdown with front matter + body +
    references. ``body_markdown`` is the existing styled / drafted body
    output. Callers pass the cited shortlist (in any order) — this
    function numbers them deterministically by appearance order in the
    body if possible, otherwise alphabetically by source_id.

    PR-D2.1 (2026-05-03): the front-matter LLM call now goes through
    ``harness.run_llm_step``. Caller (exporter) must thread ``run``
    and ``session`` so the standalone ``AuditWriter`` can be created.
    """
    body_markdown = strip_existing_paper_matter(body_markdown, project_language)
    front = _build_front_matter(
        run=run,
        session=session,
        body_markdown=body_markdown,
        project_title=project_title,
        project_language=project_language,
        selected_thesis=selected_thesis,
    )
    front_block = _render_front_block(
        front=front,
        authors=authors,
        project_language=project_language,
    )
    references_block = _render_references_block(
        cited_sources=cited_sources,
        body_markdown=body_markdown,
        project_language=project_language,
    )
    parts: list[str] = []
    if front_block:
        parts.append(front_block)
        parts.append("---")
    parts.append(body_markdown.rstrip())
    if references_block:
        parts.append("---")
        parts.append(references_block)
    return "\n\n".join(parts).rstrip() + "\n"


def strip_existing_paper_matter(body_markdown: str, project_language: str) -> str:
    """Return body-only markdown before export-time composition.

    Drafter historically wrapped Chinese/Japanese bodies with
    ``摘要``/``关键词``/``参考文献`` so intermediate manuscripts were
    readable. Exporter composition also adds front/back matter. This
    helper makes the export boundary idempotent: if a styled/rewrite
    manuscript already carries paper matter, strip those generated
    blocks and leave only body sections for the final wrapper.
    """
    text = body_markdown.strip()
    if not text:
        return ""
    code = (project_language or "en").strip().lower()
    text = _strip_existing_back_matter(text, code)
    text = _strip_existing_front_matter(text, code)
    return text.strip() + "\n"


def _strip_existing_front_matter(text: str, code: str) -> str:
    if code not in {"zh", "ja"}:
        return text
    lines = text.splitlines()
    body_start_re = re.compile(
        r"^\s*#{1,6}\s*(?:[一二三四五六七八九十]+[、.．]|"
        r"(?:一|二|三|四|五|六|七|八|九|十)、)"
    )
    for index, line in enumerate(lines):
        if body_start_re.match(line):
            return "\n".join(lines[index:]).lstrip()
    return text


def _strip_existing_back_matter(text: str, code: str) -> str:
    labels = {"参考文献"} if code in {"zh", "ja"} else {"References"}
    pattern = re.compile(
        r"(?m)^#{1,6}\s*(?:" + "|".join(re.escape(label) for label in labels) + r")\s*$"
    )
    matches = list(pattern.finditer(text))
    if not matches:
        return text
    return text[: matches[-1].start()].rstrip()


def _build_front_matter(
    *,
    run: Run,
    session: Session,
    body_markdown: str,
    project_title: str,
    project_language: str,
    selected_thesis: Mapping[str, object] | None,
) -> FrontMatter:
    """Try the LLM-generated front matter first; fall back to safe
    defaults so an exporter never crashes on a bad LLM response."""
    settings = get_settings()
    if getattr(settings, "front_matter_stub", False):
        # Test/CI mode: skip the LLM call entirely. Use the project
        # title as the paper title and leave abstract/keywords empty.
        title = _refined_title_default(project_title, selected_thesis)
        return FrontMatter(title=title, abstract="", keywords=[])
    audit = AuditWriter(
        session=session,
        run_dir=Path(run.run_dir),
        agent_name="ManuscriptCompose",
    )
    try:
        result = asyncio.run(
            _generate_front_matter_via_llm(
                run=run,
                audit=audit,
                body_markdown=body_markdown[:FRONT_MATTER_BODY_CHAR_LIMIT],
                project_title=project_title,
                project_language=project_language,
                selected_thesis=selected_thesis,
            ),
        )
    except Exception:  # noqa: BLE001 - exporter must not crash on LLM hiccup
        result = None
    if result is None:
        title = _refined_title_default(project_title, selected_thesis)
        return FrontMatter(title=title, abstract="", keywords=[])
    return result


async def _generate_front_matter_via_llm(
    *,
    run: Run,
    audit: AuditWriter,
    body_markdown: str,
    project_title: str,
    project_language: str,
    selected_thesis: Mapping[str, object] | None,
) -> FrontMatter | None:
    settings = get_settings()
    model = getattr(settings, "one_api_model", None) or "gpt-5.4-mini"
    schema = {
        "title": "refined paper title (may differ from the user's typed topic)",
        "abstract": "150-300 character abstract summarising thesis, evidence, and limitations",
        "keywords": ["3-6 short noun phrases"],
    }
    thesis_summary = ""
    if selected_thesis:
        for key in ("thesis_one_sentence", "working_title", "why_novel"):
            value = selected_thesis.get(key)
            if isinstance(value, str) and value.strip():
                thesis_summary = value.strip()
                break
    system = (
        "You are a manuscript front-matter summariser. Produce a paper "
        "title, an abstract, and 3-6 keywords for the manuscript body "
        "the user provides. Do not invent claims or data not present in "
        "the body. Do not include placeholder text or [UNCITED] tokens. "
        "Return strict JSON only. "
        + language_directive(project_language)
        + "\n\n"
        + humanizer_directive(project_language)
    )
    user = (
        f"User-supplied topic: {project_title}\n"
        f"Selected thesis (if any): {thesis_summary}\n\n"
        f"Manuscript body (truncated):\n{body_markdown}\n\n"
        f"Return strict JSON matching this schema: "
        f"{json.dumps(schema, sort_keys=True)}"
    )
    request = LLMCallRequest(
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        model=model,
        temperature=0.2,
        max_tokens=900,
        response_format={"type": "json_object"},
        request_id="front_matter",
        prompt_template_id="manuscript_compose.front_matter.v1",
    )
    context = HookContext(
        run_id=run.id,
        phase="exports",
        step_id="front_matter",
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
            output_schema=FrontMatterOutput,
            audit=audit,
        )
    except SchemaViolationError:
        return None
    parsed = response.parsed
    if not isinstance(parsed, FrontMatterOutput):
        try:
            parsed = FrontMatterOutput.parse_obj(parsed if isinstance(parsed, dict) else {})
        except ValidationError:
            return None
    return FrontMatter(
        title=parsed.title or _refined_title_default(project_title, selected_thesis),
        abstract=parsed.abstract,
        keywords=list(parsed.keywords),
    )


def _refined_title_default(
    project_title: str,
    selected_thesis: Mapping[str, object] | None,
) -> str:
    if selected_thesis:
        candidate = selected_thesis.get("working_title")
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return project_title.strip()


def _render_front_block(
    *,
    front: FrontMatter,
    authors: Sequence[str],
    project_language: str,
) -> str:
    labels = _front_labels(project_language)
    code = (project_language or "en").lower()
    label_sep = "：" if code in {"zh", "ja"} else ": "
    keyword_sep = "；" if code == "zh" else "; "
    lines: list[str] = [f"# {front.title}"]
    real_authors = [a.strip() for a in authors if a and a.strip() and not _is_placeholder_author(a)]
    if real_authors:
        lines.append("")
        lines.append(f"**{labels['authors']}**{label_sep}{', '.join(real_authors)}")
    if front.abstract.strip():
        lines.append("")
        lines.append(f"**{labels['abstract']}**{label_sep}{front.abstract.strip()}")
    if front.keywords:
        lines.append("")
        lines.append(f"**{labels['keywords']}**{label_sep}{keyword_sep.join(front.keywords)}")
    return "\n".join(lines).rstrip()


def _is_placeholder_author(value: str) -> bool:
    cleaned = " ".join(value.split()).strip()
    if cleaned in {"Single User", "Admin", "Test User"}:
        return True
    return bool(re.fullmatch(r"(?:user|single-user|test-user)[_-]?[0-9A-Za-z]*", cleaned))


def _render_references_block(
    *,
    cited_sources: Sequence[NormalizedSource],
    body_markdown: str,
    project_language: str,
) -> str:
    if not cited_sources:
        return ""
    ordered = _ordered_cited_sources(cited_sources, body_markdown)
    if not ordered:
        return ""
    labels = _front_labels(project_language)
    lines: list[str] = [f"## {labels['references']}", ""]
    for index, source in enumerate(ordered, start=1):
        lines.append(_format_reference(index, source, project_language))
    return "\n".join(lines).rstrip()


def _ordered_cited_sources(
    cited_sources: Sequence[NormalizedSource],
    body_markdown: str,
) -> list[NormalizedSource]:
    """Order sources by first appearance of their source_id in the body
    markdown. Sources never mentioned by ID fall to the end (alphabetical).
    """
    if re.search(r"\[\d{1,3}\]", body_markdown):
        return list(cited_sources)
    by_id = {source.source_id: source for source in cited_sources}
    seen: list[str] = []
    seen_set: set[str] = set()
    for match in re.finditer(r"[\w:_./-]+", body_markdown):
        token = match.group(0)
        if token in by_id and token not in seen_set:
            seen.append(token)
            seen_set.add(token)
    leftover = sorted(set(by_id) - seen_set)
    return [by_id[sid] for sid in seen + leftover]


def _format_reference(index: int, source: NormalizedSource, project_language: str) -> str:
    authors = "; ".join(source.authors) if source.authors else ""
    parts: list[str] = [f"[{index}]"]
    if authors:
        parts.append(f"{authors}.")
    if source.title:
        parts.append(f"{source.title}.")
    bibinfo: list[str] = []
    if source.venue:
        bibinfo.append(source.venue)
    if source.year is not None:
        bibinfo.append(str(source.year))
    if bibinfo:
        parts.append(", ".join(bibinfo) + ".")
    if source.doi:
        parts.append(f"DOI: {source.doi}.")
    elif source.url:
        parts.append(f"URL: {source.url}.")
    return " ".join(parts).rstrip()


def _front_labels(project_language: str) -> dict[str, str]:
    code = (project_language or "en").lower()
    if code == "zh":
        return {
            "authors": "作者",
            "abstract": "摘要",
            "keywords": "关键词",
            "references": "参考文献",
        }
    if code == "ja":
        return {
            "authors": "著者",
            "abstract": "要旨",
            "keywords": "キーワード",
            "references": "参考文献",
        }
    return {
        "authors": "Authors",
        "abstract": "Abstract",
        "keywords": "Keywords",
        "references": "References",
    }


__all__ = [
    "FrontMatter",
    "FrontMatterOutput",
    "compose_manuscript",
    "strip_existing_paper_matter",
]
