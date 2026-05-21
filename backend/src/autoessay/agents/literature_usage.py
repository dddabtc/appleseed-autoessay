"""Literature usage table — paper-quality-spec.md §五.11 + §三.

Builds a markdown table that maps each cited source to:
- 作者 / Authors
- 年份 / Year
- 题名 / Title
- 文献类型 / Type (article / book / thesis / report — heuristic from venue)
- 核心观点 / Core point (taken from the synthesizer source_note thesis if
  available; otherwise the source abstract; otherwise empty)
- 使用位置 / Where used (which sections / paragraph ids cited the source)
- 与本文关系 / Relation (支持 / 反驳 / 补充 / 背景 / 方法参考)

The Relation column is heuristic for now (defaults to "背景" when no
classification is available); Critic can promote it to one of the
five categories in a follow-up PR.

This is pure data assembly — no LLM call. Deterministic. Cheap.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from autoessay.clients.common import NormalizedSource

_RELATION_BACKGROUND = {
    "en": "background",
    "zh": "背景",
    "ja": "背景",
}
_RELATION_LABELS = {
    "support": {"en": "support", "zh": "支持", "ja": "支持"},
    "challenge": {"en": "challenge", "zh": "反驳", "ja": "反論"},
    "supplement": {"en": "supplement", "zh": "补充", "ja": "補足"},
    "background": _RELATION_BACKGROUND,
    "method": {"en": "method reference", "zh": "方法参考", "ja": "方法参照"},
}


@dataclass(frozen=True)
class LiteratureRow:
    source_id: str
    authors: str
    year: str
    title: str
    type: str
    core_point: str
    used_in: str
    relation: str


def build_literature_usage_table(
    *,
    cited_sources: Sequence[NormalizedSource],
    claim_map: Sequence[Mapping[str, object]],
    source_notes: Mapping[str, Mapping[str, object]] | None = None,
    project_language: str = "en",
    relation_map: Mapping[str, str] | None = None,
) -> str:
    """Return a markdown document with a single table summarising how
    every cited source is used. ``cited_sources`` already contains only
    sources that appear in claim_map. ``relation_map`` (optional) maps
    source_id -> relation label key from _RELATION_LABELS.
    """
    if not cited_sources:
        return ""
    code = (project_language or "en").lower()
    headers = _table_headers(code)
    rows: list[LiteratureRow] = []
    used_in_index = _index_used_in(claim_map)
    for source in cited_sources:
        relation_key = (relation_map or {}).get(source.source_id, "background")
        relation_pack = _RELATION_LABELS.get(relation_key, _RELATION_BACKGROUND)
        rel_label = relation_pack.get(code, _RELATION_BACKGROUND[code])
        used_in = used_in_index.get(source.source_id, [])
        rows.append(
            LiteratureRow(
                source_id=source.source_id,
                authors="; ".join(source.authors) if source.authors else "—",
                year=str(source.year) if source.year is not None else "—",
                title=source.title or "—",
                type=_classify_type(source),
                core_point=_pick_core_point(source, source_notes),
                used_in=", ".join(used_in) if used_in else "—",
                relation=rel_label,
            ),
        )
    title = _doc_title(code)
    body_lines: list[str] = [f"# {title}", ""]
    body_lines.append("| " + " | ".join(headers) + " |")
    body_lines.append("|" + "|".join(["---"] * len(headers)) + "|")
    for row in rows:
        body_lines.append(
            "| "
            + " | ".join(
                _escape_cell(cell)
                for cell in (
                    row.source_id,
                    row.authors,
                    row.year,
                    row.title,
                    row.type,
                    row.core_point,
                    row.used_in,
                    row.relation,
                )
            )
            + " |"
        )
    return "\n".join(body_lines).rstrip() + "\n"


def _index_used_in(claim_map: Sequence[Mapping[str, object]]) -> dict[str, list[str]]:
    index: dict[str, list[str]] = {}
    seen_pairs: set[tuple[str, str]] = set()
    for record in claim_map:
        section_id = str(record.get("section_id") or "").strip()
        paragraph_id = str(record.get("paragraph_id") or "").strip()
        location = paragraph_id or section_id
        if not location:
            continue
        raw_source_ids = record.get("source_ids")
        if not isinstance(raw_source_ids, list):
            continue
        for source_id in raw_source_ids:
            if not isinstance(source_id, str) or source_id == "[UNCITED]":
                continue
            key = (source_id, location)
            if key in seen_pairs:
                continue
            seen_pairs.add(key)
            index.setdefault(source_id, []).append(location)
    return index


def _classify_type(source: NormalizedSource) -> str:
    venue = (source.venue or "").lower()
    if any(token in venue for token in ("press", "publishing", "出版", "publisher")):
        return "book"
    if "thesis" in venue or "dissertation" in venue or "学位论文" in venue:
        return "thesis"
    if "report" in venue or "white paper" in venue:
        return "report"
    if source.doi or source.venue:
        return "article"
    return "metadata-only"


def _pick_core_point(
    source: NormalizedSource,
    source_notes: Mapping[str, Mapping[str, object]] | None,
) -> str:
    if source_notes:
        note = source_notes.get(source.source_id)
        if isinstance(note, Mapping):
            for key in ("thesis", "core_point", "summary"):
                value = note.get(key)
                if isinstance(value, str) and value.strip():
                    return _truncate(value.strip(), 220)
    if source.abstract and source.abstract.strip():
        return _truncate(source.abstract.strip(), 220)
    return "—"


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _escape_cell(value: str) -> str:
    return value.replace("\n", " ").replace("|", "\\|").strip() or "—"


def _table_headers(code: str) -> list[str]:
    if code == "zh":
        return [
            "source_id",
            "作者",
            "年份",
            "题名",
            "文献类型",
            "核心观点",
            "使用位置",
            "与本文关系",
        ]
    if code == "ja":
        return [
            "source_id",
            "著者",
            "年",
            "題名",
            "文献種別",
            "核心論点",
            "使用箇所",
            "本論との関係",
        ]
    return [
        "source_id",
        "authors",
        "year",
        "title",
        "type",
        "core point",
        "used in",
        "relation",
    ]


def _doc_title(code: str) -> str:
    if code == "zh":
        return "文献使用表"
    if code == "ja":
        return "文献使用表"
    return "Literature Usage Table"


def relation_label(relation_key: str, project_language: str) -> str:
    """Public helper for callers that want to render a single relation
    label outside the table builder."""
    code = (project_language or "en").lower()
    return _RELATION_LABELS.get(relation_key, _RELATION_BACKGROUND).get(
        code,
        _RELATION_BACKGROUND[code],
    )


__all__ = [
    "LiteratureRow",
    "build_literature_usage_table",
    "relation_label",
]
