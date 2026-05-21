from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from autoessay.agents.final_rewrite import _holistic_rewrite_user_prompt
from autoessay.agents.phase_context import (
    PACK_HEADER,
    build_global_context_pack,
    phase_context_prompt_block,
)
from autoessay.config import Settings, get_settings


def test_phase_context_pack_prioritizes_verified_core_sources(tmp_path: Path) -> None:
    _seed_context_artifacts(tmp_path)

    pack = build_global_context_pack(
        tmp_path,
        "ideator",
        settings=Settings(phase_context_accumulation=True, phase_context_budget_chars=5000),
    )

    assert pack.startswith(PACK_HEADER)
    assert "not evidence" in pack
    assert "source_core" in pack
    assert "source_weak" in pack
    assert pack.index("source_core") < pack.index("source_weak")
    persisted = tmp_path / "phase_context" / "global_context_pack_ideator.md"
    assert persisted.read_text(encoding="utf-8") == pack


def test_phase_context_flag_off_returns_empty_without_writing(tmp_path: Path) -> None:
    _seed_context_artifacts(tmp_path)

    pack = build_global_context_pack(
        tmp_path,
        "critic",
        settings=Settings(phase_context_accumulation=False),
    )

    assert pack == ""
    assert not (tmp_path / "phase_context").exists()


def test_phase_context_budget_truncates_by_priority(tmp_path: Path) -> None:
    _seed_context_artifacts(tmp_path, long_notes=True)

    pack = build_global_context_pack(
        tmp_path,
        "ideator",
        settings=Settings(phase_context_accumulation=True, phase_context_budget_chars=3000),
    )

    assert len(pack) <= 3000
    assert "Source Shortlist Priority" in pack
    assert "truncated" in pack


def test_material_diagnostic_context_omits_unprocessed_shortlist_sources(
    tmp_path: Path,
) -> None:
    _seed_context_artifacts(tmp_path)

    pack = build_global_context_pack(
        tmp_path,
        "material_diagnostic",
        settings=Settings(phase_context_accumulation=True, phase_context_budget_chars=5000),
    )

    assert "Source Shortlist Priority" not in pack
    assert "source_core" in pack
    assert "source_weak" not in pack


def test_drafter_prompt_context_omits_duplicated_source_notes(tmp_path: Path) -> None:
    _seed_context_artifacts(tmp_path)

    context = phase_context_prompt_block(
        tmp_path,
        "drafter",
        settings=Settings(phase_context_accumulation=True, phase_context_budget_chars=12000),
    )

    assert len(context) < 3600
    assert "Material Diagnostic" in context
    assert "Selected Thesis" in context
    assert "Source Notes" not in context
    assert "Synthesis Claims" not in context
    assert "source_weak" not in context


def test_phase_context_prompt_block_is_available_to_holistic_rewrite(tmp_path: Path) -> None:
    _seed_context_artifacts(tmp_path)
    context = phase_context_prompt_block(
        tmp_path,
        "final_rewrite",
        settings=Settings(phase_context_accumulation=True, phase_context_budget_chars=5000),
    )

    prompt = _holistic_rewrite_user_prompt(
        manuscript="第一段 [1]\n\n第二段 [1]",
        claim_map=[{"paragraph_id": "p1", "source_ids": ["source_core"]}],
        project=SimpleNamespace(title="Test title", language="zh"),
        accumulated_context=context,
    )

    assert "global_context_pack_non_citable" in prompt
    assert "Global Context Pack" in prompt
    assert "Only approved_sources" in prompt


def test_phase_context_settings_default_on(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.delenv("AUTOESSAY_PHASE_CONTEXT_ACCUMULATION", raising=False)
    get_settings.cache_clear()
    try:
        assert get_settings().phase_context_accumulation is True
    finally:
        get_settings.cache_clear()


def _seed_context_artifacts(root: Path, *, long_notes: bool = False) -> None:
    _write_json(
        root / "sources" / "shortlist.json",
        [
            {
                "source_id": "source_weak",
                "title": "Weak metadata-only source",
                "authors": ["B"],
                "year": 2020,
                "access_status": "metadata_only",
                "verification_status": "unverified",
                "research_role": "background",
                "confidence": 0.2,
                "rank_score": 0.1,
                "risk_flags": ["metadata_only_no_full_text"],
            },
            {
                "source_id": "source_core",
                "title": "Verified core source",
                "authors": ["A"],
                "year": 2019,
                "access_status": "open",
                "verification_status": "verified",
                "verified_by": "crossref",
                "research_role": "core_evidence",
                "confidence": 0.95,
                "rank_score": 0.9,
                "risk_flags": [],
            },
        ],
    )
    _write_json(
        root / "synthesis" / "material_diagnostic.json",
        {
            "sufficient": False,
            "missing_materials": ["archive chain"],
            "risks": ["candidate-only evidence"],
        },
    )
    _write_text(
        root / "synthesis" / "claims.jsonl",
        json.dumps(
            {
                "claim_id": "c1",
                "text": "The material supports a bounded route claim.",
                "source_ids": ["source_core"],
                "claim_type": "finding",
            },
            ensure_ascii=False,
        )
        + "\n",
    )
    note_text = "core evidence " * (300 if long_notes else 4)
    _write_json(
        root / "synthesis" / "source_notes" / "source_core.json",
        {
            "source_id": "source_core",
            "title": "Verified core source",
            "thesis": note_text,
            "method": "archive reading",
            "evidence": note_text,
            "limits": "does not prove a unique date",
            "claims": [{"claim_id": "c1", "text": note_text, "claim_type": "finding"}],
        },
    )
    _write_json(
        root / "novelty" / "selected_thesis.json",
        {
            "angle_id": "angle_001",
            "thesis_one_sentence": "A scoped thesis that keeps material limits visible.",
        },
    )


def _write_json(path: Path, payload: object) -> None:
    _write_text(path, json.dumps(payload, ensure_ascii=False, sort_keys=True))


def _write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")
