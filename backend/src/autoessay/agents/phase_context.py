"""Accumulated context packs for cross-phase LLM prompts."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from pathlib import Path

from autoessay.config import Settings, get_settings

DEFAULT_PHASE_CONTEXT_BUDGET_CHARS = 12000
PACK_HEADER = "## Global Context Pack"
_NON_CITABLE_POLICY = (
    "Use this pack only for continuity, framing, source confidence, and material limits. "
    "It is not evidence. Only approved_sources / shortlist entries and claim_map source_ids "
    "are citable, and all normal verified-only citation gates still apply."
)
_ROLE_WEIGHT = {
    "core_evidence": 0,
    "primary_source": 0,
    "theoretical_lens": 1,
    "secondary_argument": 2,
    "background": 3,
    "out_of_scope": 5,
}
_ACCESS_WEIGHT = {
    "open": 0,
    "user_upload": 0,
    "metadata_only": 1,
    "unavailable": 2,
    "blocked": 3,
}
_VERIFICATION_WEIGHT = {
    "verified": 0,
    "pending": 1,
    "unverified": 2,
    "disputed": 3,
}
_PROMPT_PHASE_BUDGETS = {
    "material_diagnostic": 5000,
    "ideator": 7000,
    "drafter": 3000,
    "final_rewrite": 5000,
    "critic": 5000,
}


def phase_context_prompt_block(
    run_dir: str | Path,
    phase: str,
    *,
    settings: Settings | None = None,
    budget_chars: int | None = None,
) -> str:
    """Return a prompt-ready accumulated context block for ``phase``.

    The block is empty when the feature flag is off or no upstream
    artifacts exist. When non-empty, the same text is persisted under
    ``phase_context/global_context_pack_<phase>.md`` for run auditability.
    """

    settings = settings or get_settings()
    effective_budget = budget_chars
    if effective_budget is None:
        effective_budget = min(
            int(settings.phase_context_budget_chars),
            _PROMPT_PHASE_BUDGETS.get(phase, DEFAULT_PHASE_CONTEXT_BUDGET_CHARS),
        )
    pack = build_global_context_pack(
        run_dir,
        phase,
        settings=settings,
        budget_chars=effective_budget,
    )
    if not pack:
        return ""
    return f"\n\n{pack}\n\n"


def build_global_context_pack(
    run_dir: str | Path,
    phase: str,
    *,
    settings: Settings | None = None,
    budget_chars: int | None = None,
) -> str:
    settings = settings or get_settings()
    if not settings.phase_context_accumulation:
        return ""
    root = Path(run_dir)
    budget = max(3000, int(budget_chars or settings.phase_context_budget_chars))
    sections = _collect_sections(root, phase)
    if not sections:
        return ""
    pack = _render_budgeted_pack(phase=phase, sections=sections, budget_chars=budget)
    _persist_pack(root, phase, pack)
    return pack


def _collect_sections(root: Path, phase: str) -> list[tuple[str, str]]:
    sections: list[tuple[str, str]] = []
    if phase in {"ideator"}:
        sources = _source_shortlist_section(root / "sources" / "shortlist.json")
        if sources:
            sections.append(("Source Shortlist Priority", sources))
    diagnostic = _json_section(
        "Material Diagnostic",
        root / "synthesis" / "material_diagnostic.json",
        limit=2400,
    )
    if diagnostic:
        sections.append(("Material Diagnostic", diagnostic))
    claims = _claims_section(root / "synthesis" / "claims.jsonl")
    if claims and phase in {"material_diagnostic", "ideator"}:
        sections.append(("Synthesis Claims", claims))
    notes = _source_notes_section(root / "synthesis" / "source_notes")
    if notes and phase in {"material_diagnostic", "ideator"}:
        sections.append(("Source Notes", notes))
    lens = _json_section(
        "Framework Lens",
        root / "synthesis" / "framework_lens.json",
        limit=2600,
    )
    if lens:
        sections.append(("Framework Lens", lens))
    thesis = _json_section(
        "Selected Thesis",
        root / "novelty" / "selected_thesis.json",
        limit=2600,
    )
    if thesis:
        sections.append(("Selected Thesis", thesis))
    angles = _angle_cards_section(root / "novelty" / "angle_cards.json")
    if angles and phase in {"drafter", "final_rewrite", "critic"}:
        sections.append(("Angle Cards", angles))
    draft = _latest_draft_section(root, phase)
    if draft:
        sections.append(("Latest Draft Context", draft))
    return sections


def _render_budgeted_pack(
    *,
    phase: str,
    sections: Sequence[tuple[str, str]],
    budget_chars: int,
) -> str:
    header = (
        f"{PACK_HEADER}\n"
        f"- phase: {phase}\n"
        f"- budget_chars: {budget_chars}\n"
        f"- policy: {_NON_CITABLE_POLICY}\n"
    )
    out = header
    remaining = budget_chars - len(out)
    for title, content in sections:
        if remaining <= 160:
            out += "\n[global_context_pack truncated before remaining lower-priority sections]\n"
            break
        block = f"\n### {title}\n{content.strip()}\n"
        if len(block) > remaining:
            block = (
                block[: max(0, remaining - 96)].rstrip()
                + "\n[global_context_pack section truncated by priority budget]\n"
            )
            out += block
            break
        out += block
        remaining = budget_chars - len(out)
    return out.rstrip()


def _persist_pack(root: Path, phase: str, pack: str) -> None:
    safe_phase = re.sub(r"[^A-Za-z0-9_.-]+", "_", phase).strip("_") or "phase"
    out_dir = root / "phase_context"
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        target = out_dir / f"global_context_pack_{safe_phase}.md"
        temporary = target.with_suffix(target.suffix + ".tmp")
        temporary.write_text(pack, encoding="utf-8")
        temporary.replace(target)
    except OSError:
        return


def _source_shortlist_section(path: Path) -> str:
    raw = _load_json(path)
    if not isinstance(raw, list):
        return ""
    entries = [entry for entry in raw if isinstance(entry, Mapping)]
    if not entries:
        return ""
    rows = [_compact_source(entry) for entry in sorted(entries, key=_source_priority_key)[:18]]
    return _json_dump(rows)


def _compact_source(entry: Mapping[str, object]) -> dict[str, object]:
    keys = (
        "source_id",
        "title",
        "authors",
        "year",
        "venue",
        "access_status",
        "verification_status",
        "verified_by",
        "research_role",
        "provenance",
        "canonical_bucket",
        "confidence",
        "rank_score",
        "risk_flags",
        "topic_relevance",
    )
    out: dict[str, object] = {}
    for key in keys:
        value = entry.get(key)
        if value not in (None, "", [], {}):
            out[key] = _truncate_value(value, 300)
    return out


def _source_priority_key(entry: Mapping[str, object]) -> tuple[float, ...]:
    role = str(entry.get("research_role") or "")
    access = str(entry.get("access_status") or "")
    status = str(entry.get("verification_status") or "")
    verified_by = str(entry.get("verified_by") or "")
    risk_flags = entry.get("risk_flags")
    confidence = _float_value(entry.get("confidence"), default=0.0)
    rank_score = _float_value(entry.get("rank_score"), default=0.0)
    risk_count = len(risk_flags) if isinstance(risk_flags, list) else 0
    verified_weight = 0 if verified_by else _VERIFICATION_WEIGHT.get(status, 2)
    return (
        float(_ROLE_WEIGHT.get(role, 4)),
        float(verified_weight),
        float(_ACCESS_WEIGHT.get(access, 2)),
        float(risk_count),
        -confidence,
        -rank_score,
    )


def _json_section(_title: str, path: Path, *, limit: int) -> str:
    payload = _load_json(path)
    if not isinstance(payload, (Mapping, list)):
        return ""
    return _truncate_text(_json_dump(payload), limit)


def _claims_section(path: Path) -> str:
    claims = _load_jsonl(path)
    compact: list[dict[str, object]] = []
    for claim in claims[:40]:
        if not isinstance(claim, Mapping):
            continue
        compact.append(
            {
                key: _truncate_value(claim.get(key), 420)
                for key in (
                    "claim_id",
                    "text",
                    "claim_text",
                    "claim_type",
                    "source_id",
                    "source_ids",
                    "evidence_status",
                    "confidence",
                )
                if claim.get(key) not in (None, "", [], {})
            },
        )
    if not compact:
        return ""
    return _truncate_text(_json_dump(compact), 3600)


def _source_notes_section(path: Path) -> str:
    if not path.exists() or not path.is_dir():
        return ""
    rows: list[dict[str, object]] = []
    for note_path in sorted(path.glob("*.json")):
        note = _load_json(note_path)
        if not isinstance(note, Mapping):
            continue
        source_id = str(note.get("source_id") or note_path.stem)
        rows.append(
            {
                "source_id": source_id,
                "title": _truncate_value(note.get("title"), 180),
                "thesis": _truncate_value(note.get("thesis"), 420),
                "method": _truncate_value(note.get("method"), 260),
                "evidence": _truncate_value(note.get("evidence"), 420),
                "limits": _truncate_value(note.get("limits"), 260),
                "claims": _compact_note_claims(note.get("claims")),
            },
        )
    if not rows:
        return ""
    return _truncate_text(_json_dump(rows[:18]), 5200)


def _compact_note_claims(value: object) -> list[object]:
    if not isinstance(value, list):
        return []
    claims: list[object] = []
    for item in value[:3]:
        if isinstance(item, Mapping):
            claims.append(
                {
                    "claim_id": _truncate_value(item.get("claim_id"), 120),
                    "text": _truncate_value(item.get("text"), 280),
                    "claim_type": _truncate_value(item.get("claim_type"), 80),
                },
            )
        elif isinstance(item, str):
            claims.append(_truncate_text(item, 280))
    return claims


def _angle_cards_section(path: Path) -> str:
    payload = _load_json(path)
    cards = payload.get("angle_cards") if isinstance(payload, Mapping) else payload
    if not isinstance(cards, list):
        return ""
    compact = []
    for card in cards[:8]:
        if not isinstance(card, Mapping):
            continue
        compact.append(
            {
                key: _truncate_value(card.get(key), 420)
                for key in (
                    "angle_id",
                    "working_title",
                    "thesis_one_sentence",
                    "key_claim_ids",
                    "why_novel",
                    "evidence_so_far",
                    "missing_evidence",
                    "journal_fit_note",
                    "risks",
                    "framework_lens",
                    "methodological_choice",
                )
                if card.get(key) not in (None, "", [], {})
            },
        )
    if not compact:
        return ""
    return _truncate_text(_json_dump(compact), 3600)


def _latest_draft_section(root: Path, phase: str) -> str:
    if phase not in {"stylist", "final_rewrite", "critic", "exporter"}:
        return ""
    draft_dir = _latest_version_dir(root / "drafts")
    if draft_dir is None:
        return ""
    parts: list[str] = []
    rationale = _read_text(draft_dir / "draft_rationale.md")
    if rationale:
        parts.append("draft_rationale:\n" + _truncate_text(rationale, 1600))
    manuscript = _read_text(draft_dir / "manuscript.md")
    if manuscript:
        parts.append("manuscript_outline:\n" + _manuscript_outline(manuscript))
    styled = _read_text(root / "stylist" / "paper_styled.md")
    if styled and phase in {"final_rewrite", "critic", "exporter"}:
        parts.append("stylist_outline:\n" + _manuscript_outline(styled))
    return "\n\n".join(parts)


def _manuscript_outline(text: str) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    headings = [line for line in lines if line.startswith("#")][:24]
    if headings:
        return "\n".join(headings)
    return _truncate_text(" ".join(lines), 1600)


def _latest_version_dir(root: Path) -> Path | None:
    if not root.exists():
        return None
    candidates = [
        path for path in root.iterdir() if path.is_dir() and re.fullmatch(r"v\d+", path.name)
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda path: int(path.name[1:]))


def _load_json(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _load_jsonl(path: Path) -> list[object]:
    rows: list[object] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    except OSError:
        return []
    return rows


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _json_dump(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _truncate_value(value: object, limit: int) -> object:
    if isinstance(value, str):
        return _truncate_text(value, limit)
    if isinstance(value, list):
        return [_truncate_value(item, limit) for item in value[:12]]
    if isinstance(value, Mapping):
        return {str(key): _truncate_value(item, limit) for key, item in list(value.items())[:12]}
    return value


def _truncate_text(value: str, limit: int) -> str:
    cleaned = " ".join(str(value).split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: max(0, limit - 28)].rstrip() + " [truncated]"


def _float_value(value: object, *, default: float) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return default
    return default


__all__ = [
    "PACK_HEADER",
    "build_global_context_pack",
    "phase_context_prompt_block",
]
