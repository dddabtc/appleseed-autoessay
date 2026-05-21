import json
import re
from pathlib import Path

from conftest import seed_project
from sqlalchemy import select

from autoessay.agents.curator import run_curator
from autoessay.agents.drafter import run_drafter
from autoessay.agents.ideator import run_ideator, select_thesis_for_run
from autoessay.agents.scout import run_scout
from autoessay.agents.stylist import ManuscriptSection, _compose_sections, run_stylist
from autoessay.agents.synthesizer import run_synthesizer
from autoessay.config import get_settings
from autoessay.models import Run, RunEvent
from autoessay.run_writer import create_run_directory


def test_run_stylist_stub_writes_style_artifacts_and_preserves_load_bearing_ids(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path: Path,
    monkeypatch,
) -> None:
    run_id, run_dir = _seed_ready_for_styling(app_session, tmp_path, monkeypatch)

    with app_session() as session:
        summary = run_stylist(run_id, session)
        run = session.get(Run, run_id)
        events = list(
            session.scalars(
                select(RunEvent)
                .where(RunEvent.run_id == run_id)
                .order_by(RunEvent.created_at.asc()),
            ),
        )

    style_dir = run_dir / "drafts" / "v001" / "style"
    original_claim_map = _read_jsonl(run_dir / "drafts" / "v001" / "claim_map.jsonl")
    styled = (style_dir / "paper_styled.md").read_text(encoding="utf-8")
    score = json.loads((style_dir / "stop_slop_score.json").read_text(encoding="utf-8"))
    bib = (run_dir / "drafts" / "v001" / "citations.bib").read_text(encoding="utf-8")

    assert run is not None
    assert run.state == "USER_REVISION_REVIEW"
    assert summary["state"] == "USER_REVISION_REVIEW"
    assert (style_dir / "style_delta.md").exists()
    assert original_claim_map
    assert score["initial"]["total"] >= 0
    assert score["final"]["total"] >= 0
    for key in _bib_keys(bib):
        if key in (run_dir / "drafts" / "v001" / "manuscript.md").read_text(encoding="utf-8"):
            assert key in styled
    assert "section_progress" in [event.event_type for event in events]
    assert events[-1].event_type == "phase_done"


def test_compose_sections_omits_html_anchors() -> None:
    rendered = _compose_sections(
        [ManuscriptSection(section_id="introduction", title="一、引言", prose="正文。")]
    )

    assert '<a id="introduction"></a>' not in rendered
    assert "## 一、引言" in rendered


def _seed_ready_for_styling(
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
    monkeypatch.setenv("AUTOESSAY_STYLIST_STUB", "1")
    monkeypatch.setenv("AUTOESSAY_STOP_SLOP_LLM_ENABLED", "0")
    get_settings.cache_clear()
    run_id = "run_stylist_success"
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
        run_drafter(run_id, session)

    return run_id, run_dir


def _read_jsonl(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _bib_keys(citations_bib: str) -> set[str]:
    return set(re.findall(r"@\w+\{([^,\s]+),", citations_bib))
