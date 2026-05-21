import json
from pathlib import Path

from conftest import seed_project
from sqlalchemy import select

from autoessay.agents.curator import run_curator
from autoessay.agents.drafter import DEFAULT_SECTION_TITLES, run_drafter
from autoessay.agents.ideator import run_ideator, select_thesis_for_run
from autoessay.agents.scout import run_scout
from autoessay.agents.synthesizer import run_synthesizer
from autoessay.config import get_settings
from autoessay.models import Run, RunEvent
from autoessay.run_writer import create_run_directory


def test_run_drafter_stub_writes_manuscript_claim_map_and_bib(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path: Path,
    monkeypatch,
) -> None:
    run_id, run_dir = _seed_ready_for_drafting(app_session, tmp_path, monkeypatch)

    with app_session() as session:
        summary = run_drafter(run_id, session)
        run = session.get(Run, run_id)
        events = list(
            session.scalars(
                select(RunEvent)
                .where(RunEvent.run_id == run_id)
                .order_by(RunEvent.created_at.asc()),
            ),
        )

    draft_dir = run_dir / "drafts" / "v001"
    manuscript = (draft_dir / "manuscript.md").read_text(encoding="utf-8")
    claim_map = _read_jsonl(draft_dir / "claim_map.jsonl")
    bib = (draft_dir / "citations.bib").read_text(encoding="utf-8")
    shortlist_ids = _shortlist_ids(run_dir)

    assert run is not None
    assert run.state == "DRAFTER_RUNNING"
    assert summary["state"] == "DRAFTER_RUNNING"
    for title in DEFAULT_SECTION_TITLES:
        assert f"## {title}" in manuscript
    assert claim_map
    for claim in claim_map:
        source_ids = claim["source_ids"]
        assert isinstance(source_ids, list)
        assert all(
            source_id in shortlist_ids or source_id == "[UNCITED]" for source_id in source_ids
        )
    cited_ids = {
        source_id
        for claim in claim_map
        for source_id in claim["source_ids"]
        if source_id != "[UNCITED]"
    }
    for source_id in cited_ids:
        assert f"@article{{{source_id}," in bib or f"@misc{{{source_id}," in bib
    assert "section_progress" in [event.event_type for event in events]
    assert events[-1].event_type == "phase_done"


def test_run_drafter_rerun_increments_draft_version(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path: Path,
    monkeypatch,
) -> None:
    run_id, run_dir = _seed_ready_for_drafting(app_session, tmp_path, monkeypatch)

    with app_session() as session:
        first = run_drafter(run_id, session)
        second = run_drafter(run_id, session)

    assert first["draft_version"] == "v001"
    assert second["draft_version"] == "v002"
    assert (run_dir / "drafts" / "v001" / "manuscript.md").exists()
    assert (run_dir / "drafts" / "v002" / "manuscript.md").exists()


def _seed_ready_for_drafting(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path: Path,
    monkeypatch,
) -> tuple[str, Path]:
    monkeypatch.setenv("AUTOESSAY_SCOUT_STUB", "1")
    monkeypatch.setenv("AUTOESSAY_CURATOR_STUB", "1")
    monkeypatch.setenv("AUTOESSAY_INCLUDE_UNVERIFIED_IN_CITATION_POOL", "1")
    monkeypatch.setenv("AUTOESSAY_SYNTHESIZER_STUB", "1")
    monkeypatch.setenv("AUTOESSAY_IDEATOR_STUB", "1")
    monkeypatch.setenv("AUTOESSAY_DRAFTER_STUB", "1")
    get_settings.cache_clear()
    run_id = "run_drafter_success"
    run_dir = create_run_directory(
        tmp_path / "runs",
        run_id,
        "proj_test",
        state="DOMAIN_LOADED",
        domain_id="financial_history",
    )

    with app_session() as session:
        project = seed_project(session)
        project.title = "banking crises in the Great Depression"
        session.add(
            Run(
                id=run_id,
                project_id=project.id,
                domain_version="0.1.0",
                run_dir=str(run_dir),
                state="DOMAIN_LOADED",
                baseline_hash="test",
            ),
        )
        session.commit()

        run_scout(run_id, session)
        run_curator(run_id, session)
        run_synthesizer(run_id, session)
        run_ideator(run_id, session)
        run = session.get(Run, run_id)
        assert run is not None
        select_thesis_for_run(run, "angle_001")
        session.commit()

    return run_id, run_dir


def _read_jsonl(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _shortlist_ids(run_dir: Path) -> set[str]:
    shortlist = json.loads((run_dir / "sources" / "shortlist.json").read_text(encoding="utf-8"))
    return {str(item["source_id"]) for item in shortlist if isinstance(item, dict)}


def test_run_drafter_partial_stub_emits_phase_done_with_amber_severity(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Stage 3.E follow-up (codex AGREE): a partial stub set is
    degraded-but-usable output, not a phase failure. Drafter must
    emit ``phase_done`` (not ``phase_failed``) when 1 ≤ stubbed <
    total, and tag the summary with ``severity`` so the UI can render
    an amber "Placeholder section, needs review" badge.
    """
    import autoessay.agents.drafter as drafter_module
    from autoessay.agents.drafter import (
        _drafted_section_from_output,  # noqa: F401  (touch internal symbols
    )

    run_id, run_dir = _seed_ready_for_drafting(app_session, tmp_path, monkeypatch)

    real_draft_section = drafter_module._draft_section
    section_calls = {"index": 0}

    def fake_draft_section(*args, **kwargs):  # noqa: ANN001, ANN002
        # First two real calls succeed via the stub-mode path. Third
        # call fakes a schema failure so drafter falls back to
        # _stub_section.
        section_calls["index"] += 1
        if section_calls["index"] == 3:
            return None
        return real_draft_section(*args, **kwargs)

    monkeypatch.setattr(drafter_module, "_draft_section", fake_draft_section)

    with app_session() as session:
        summary = run_drafter(run_id, session)
        run = session.get(Run, run_id)
        events = list(
            session.scalars(
                select(RunEvent)
                .where(RunEvent.run_id == run_id)
                .order_by(RunEvent.created_at.asc()),
            ),
        )

    assert run is not None
    assert run.state == "DRAFTER_RUNNING", "partial stubs must NOT enter FAILED_FIXABLE"
    assert summary["state"] == "DRAFTER_RUNNING"
    assert summary["stubbed_sections"] == 1
    assert summary["sections"] > 1
    assert summary["severity"] in {"amber_minor", "amber_major"}
    assert isinstance(summary["stubbed_section_ids"], list)
    assert len(summary["stubbed_section_ids"]) == 1
    assert events[-1].event_type == "phase_done", "partial stubs emit phase_done, not phase_failed"

    # Per-section status visible in draft metadata for the UI.
    metadata = json.loads(
        (run_dir / "drafts" / "v001" / "draft_metadata.json").read_text(encoding="utf-8")
    )
    assert metadata["stubbed_sections"] == 1
    assert isinstance(metadata["section_statuses"], list)
    assert any(item["is_stubbed"] for item in metadata["section_statuses"])


def test_run_drafter_all_sections_stubbed_remains_failed_fixable(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Sanity: when every section LLM call fails, drafter still
    transitions to FAILED_FIXABLE. The all-stubbed case is the only
    real phase failure under the new policy.
    """
    import autoessay.agents.drafter as drafter_module

    run_id, run_dir = _seed_ready_for_drafting(app_session, tmp_path, monkeypatch)
    monkeypatch.setattr(drafter_module, "_draft_section", lambda *a, **k: None)

    with app_session() as session:
        summary = run_drafter(run_id, session)
        run = session.get(Run, run_id)
        events = list(
            session.scalars(
                select(RunEvent)
                .where(RunEvent.run_id == run_id)
                .order_by(RunEvent.created_at.asc()),
            ),
        )

    assert run is not None
    assert run.state == "FAILED_FIXABLE"
    assert summary["state"] == "FAILED_FIXABLE"
    assert summary["severity"] == "fail_all_stubbed"
    assert summary["stubbed_sections"] == summary["sections"]
    assert events[-1].event_type == "phase_failed"
