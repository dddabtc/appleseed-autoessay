import json
from collections import Counter
from pathlib import Path

from conftest import seed_project
from sqlalchemy import select

from autoessay.agents.curator import run_curator
from autoessay.agents.scout import run_scout
from autoessay.agents.synthesizer import _source_ids_from_json, run_synthesizer
from autoessay.config import get_settings
from autoessay.models import Checkpoint, Run, RunEvent, utcnow
from autoessay.run_writer import create_run_directory


def test_run_synthesizer_stub_end_to_end_transitions_and_writes_artifacts(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("AUTOESSAY_SCOUT_STUB", "1")
    monkeypatch.setenv("AUTOESSAY_CURATOR_STUB", "1")
    monkeypatch.setenv("AUTOESSAY_INCLUDE_UNVERIFIED_IN_CITATION_POOL", "1")
    monkeypatch.setenv("AUTOESSAY_SYNTHESIZER_STUB", "1")
    get_settings.cache_clear()
    run_id = "run_synthesizer_success"
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
        selected_source_id = _first_shortlist_source_id(run_dir)
        session.add(
            Checkpoint(
                id="checkpoint_deep_dive_review",
                run_id=run_id,
                checkpoint_type="deep-dive-review",
                status="ACCEPTED",
                decision_payload=json.dumps({"source_ids": [selected_source_id]}),
                decided_at=utcnow(),
            ),
        )
        session.commit()

        summary = run_synthesizer(run_id, session)
        run = session.get(Run, run_id)
        events = list(
            session.scalars(
                select(RunEvent)
                .where(RunEvent.run_id == run_id)
                .order_by(RunEvent.created_at.asc()),
            ),
        )

    synthesis_dir = run_dir / "synthesis"
    claims = _read_jsonl(synthesis_dir / "claims.jsonl")
    report = (synthesis_dir / "synthesizer_report.md").read_text(encoding="utf-8")
    source_note_path = synthesis_dir / "source_notes" / f"{selected_source_id}.json"
    claim_counts = Counter(str(claim["claim_type"]) for claim in claims)

    assert run is not None
    assert run.state == "USER_FIELD_REVIEW"
    assert summary["state"] == "USER_FIELD_REVIEW"
    assert summary["sources_processed"] == 1
    assert claims
    assert source_note_path.exists()
    assert f"- Sources processed: {summary['sources_processed']}" in report
    assert f"- Claims total: {len(claims)}" in report
    for claim_type, count in claim_counts.items():
        assert f"- {claim_type}: {count}" in report
    assert "phase_started" in [event.event_type for event in events]
    assert "source_progress" in [event.event_type for event in events]
    assert events[-1].event_type == "phase_done"


def test_run_synthesizer_treats_empty_deep_dive_checkpoint_as_empty_selection(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("AUTOESSAY_SCOUT_STUB", "1")
    monkeypatch.setenv("AUTOESSAY_CURATOR_STUB", "1")
    monkeypatch.setenv("AUTOESSAY_INCLUDE_UNVERIFIED_IN_CITATION_POOL", "1")
    monkeypatch.setenv("AUTOESSAY_SYNTHESIZER_STUB", "1")
    get_settings.cache_clear()
    run_id = "run_synthesizer_empty_checkpoint"
    run_dir = create_run_directory(
        tmp_path / "runs",
        run_id,
        "proj_test",
        state="DOMAIN_LOADED",
        domain_id="financial_history",
    )

    with app_session() as session:
        project = seed_project(session)
        project.title = "empty deep dive selection"
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
        session.add(
            Checkpoint(
                id="checkpoint_deep_dive_empty",
                run_id=run_id,
                checkpoint_type="deep-dive-review",
                status="ACCEPTED",
                decision_payload=json.dumps({"source_ids": []}),
                decided_at=utcnow(),
            ),
        )
        session.commit()

        summary = run_synthesizer(run_id, session)
        run = session.get(Run, run_id)

    report = (run_dir / "synthesis" / "synthesizer_report.md").read_text(encoding="utf-8")
    claims = _read_jsonl(run_dir / "synthesis" / "claims.jsonl")
    assert run is not None
    assert run.state == "FAILED_FIXABLE"
    assert summary["sources_selected"] == 0
    assert summary["sources_processed"] == 0
    assert claims == []
    assert "No sources were selected for Synthesizer" in report


def test_synthesizer_source_id_parser_accepts_dict_and_list_payloads() -> None:
    assert _source_ids_from_json('{"source_ids": ["a", "b", "a", ""]}') == ["a", "b"]
    assert _source_ids_from_json('["a", "b", "a", ""]') == ["a", "b"]
    assert _source_ids_from_json('{"note": "accepted without source ids"}') is None


def test_synthesizer_writes_dual_track_artifact(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path: Path,
    monkeypatch,
) -> None:
    """PR-C1.a: synthesizer.json is written alongside the legacy
    claims.jsonl with all four research_role tracks present (empty
    arrays where no source matches)."""
    monkeypatch.setenv("AUTOESSAY_SCOUT_STUB", "1")
    monkeypatch.setenv("AUTOESSAY_CURATOR_STUB", "1")
    monkeypatch.setenv("AUTOESSAY_INCLUDE_UNVERIFIED_IN_CITATION_POOL", "1")
    monkeypatch.setenv("AUTOESSAY_SYNTHESIZER_STUB", "1")
    get_settings.cache_clear()
    run_id = "run_synthesizer_dual_track"
    run_dir = create_run_directory(
        tmp_path / "runs",
        run_id,
        "proj_test",
        state="DOMAIN_LOADED",
        domain_id="financial_history",
    )

    with app_session() as session:
        project = seed_project(session)
        project.title = "dual track test"
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
        selected_source_id = _first_shortlist_source_id(run_dir)
        session.add(
            Checkpoint(
                id="checkpoint_deep_dive_dual",
                run_id=run_id,
                checkpoint_type="deep-dive-review",
                status="ACCEPTED",
                decision_payload=json.dumps({"source_ids": [selected_source_id]}),
                decided_at=utcnow(),
            ),
        )
        session.commit()
        run_synthesizer(run_id, session)

    synthesis_dir = run_dir / "synthesis"
    dual_track_path = synthesis_dir / "synthesizer.json"
    assert dual_track_path.exists()
    payload = json.loads(dual_track_path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 1
    # All four track keys must be present (even if empty) so the
    # frontend renderer can rely on stable shape.
    for key in (
        "primary_track",
        "secondary_track",
        "theoretical_lens_track",
        "methodological_track",
    ):
        assert key in payload, f"missing track key {key}"
        assert isinstance(payload[key], list), f"track {key} not a list"
    # PR-C3 hook slot — present, null until that PR lands. PR-G1
    # moved the framework_lens downstream ref into lens-owned
    # framework_lens.json, so synthesizer no longer writes that field.
    assert payload["tension_summary_ref"] is None
    assert "framework_lens_summary_ref" not in payload

    # Legacy compatibility: claims.jsonl + source_notes/* still
    # written verbatim.
    assert (synthesis_dir / "claims.jsonl").exists()
    assert (synthesis_dir / "source_notes" / f"{selected_source_id}.json").exists()


def test_curator_classifies_research_role_into_shortlist(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path: Path,
    monkeypatch,
) -> None:
    """PR-C1.a: every shortlist.json entry carries a
    research_role field after the curator phase. Stub behaviour
    is deterministic: the curator stub uses synthetic source_ids
    that do not match any of the primary/theory/method prefixes,
    so all entries fall through to secondary_argument."""
    monkeypatch.setenv("AUTOESSAY_SCOUT_STUB", "1")
    monkeypatch.setenv("AUTOESSAY_CURATOR_STUB", "1")
    monkeypatch.setenv("AUTOESSAY_INCLUDE_UNVERIFIED_IN_CITATION_POOL", "1")
    get_settings.cache_clear()
    run_id = "run_curator_role_classification"
    run_dir = create_run_directory(
        tmp_path / "runs",
        run_id,
        "proj_test",
        state="DOMAIN_LOADED",
        domain_id="financial_history",
    )

    with app_session() as session:
        project = seed_project(session)
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

    shortlist = json.loads(
        (run_dir / "sources" / "shortlist.json").read_text(encoding="utf-8"),
    )
    assert shortlist, "shortlist must not be empty"
    for entry in shortlist:
        assert "research_role" in entry, "shortlist entry missing research_role"
        assert entry["research_role"] in {
            "primary_source",
            "secondary_argument",
            "theoretical_lens",
            "methodological_reference",
        }


def _first_shortlist_source_id(run_dir: Path) -> str:
    shortlist = json.loads((run_dir / "sources" / "shortlist.json").read_text(encoding="utf-8"))
    assert isinstance(shortlist, list)
    first = shortlist[0]
    assert isinstance(first, dict)
    source_id = first["source_id"]
    assert isinstance(source_id, str)
    return source_id


def _read_jsonl(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
