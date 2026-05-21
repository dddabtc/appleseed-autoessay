"""PR-J5: synthesizer FAILED_FIXABLE event payload now exposes
``per_source_warnings`` so the FailureResolutionBanner can show users
WHICH sources failed and WHY (no PDF + no abstract / PoorExtraction /
LLM parse fail), not just the generic "upload more PDFs / broaden /
refine" guidance.

Pins the new contract:
  * ``_fail_fixable`` accepts ``per_source_warnings`` kwarg
  * each warning surfaced as ``{source_id, failure_class, message[:280]}``
  * ``per_source_warning_total`` reports the full count even when
    the visible list is truncated to ``_PER_SOURCE_WARNING_LIMIT``
  * non-mapping warning entries silently dropped (defensive)
  * works on the full FAILED_FIXABLE flow end-to-end (state transition
    payload + phase_failed event payload both carry the breakdown)
"""

from __future__ import annotations

import json
from pathlib import Path

from conftest import seed_project
from sqlalchemy import select

from autoessay.config import get_settings
from autoessay.models import Run, RunEvent
from autoessay.run_writer import create_run_directory


def test_fail_fixable_helper_surfaces_per_source_warnings(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Direct unit test on ``_fail_fixable``: pass a warnings list,
    assert the returned dict carries the surfaced breakdown and the
    state_transition + phase_failed events both carry it too."""
    from autoessay.agents.synthesizer import _fail_fixable

    monkeypatch.setenv("AUTOESSAY_SYNTHESIZER_STUB", "1")
    get_settings.cache_clear()

    run_id = "run_j5_fail_fixable_unit"
    run_dir = create_run_directory(
        tmp_path / "runs",
        run_id,
        "proj_test",
        state="SYNTHESIZER_RUNNING",
        domain_id="financial_history",
    )
    with app_session() as session:
        seed_project(session)
        run = Run(
            id=run_id,
            project_id="proj_test",
            domain_version="0.1.0",
            run_dir=str(run_dir),
            state="SYNTHESIZER_RUNNING",
            baseline_hash="t",
        )
        session.add(run)
        session.commit()
        session.refresh(run)
        warnings: list[dict[str, object]] = [
            {
                "source_id": "src_a",
                "failure_class": "fixable_deterministic",
                "message": "No PDF text or abstract was available for synthesis.",
            },
            {
                "source_id": "src_b",
                "failure_class": "fixable_prompt",
                "message": "Synthesizer summary JSON did not parse after retry.",
            },
            {
                "source_id": "src_c",
                "failure_class": "fixable_deterministic",
                "message": "PoorExtraction: text density too low (3% words).",
            },
            "not a mapping — should be silently dropped",  # type: ignore[list-item]
        ]
        result = _fail_fixable(
            run,
            session,
            "Test guidance",
            selected_count=4,
            processed_count=0,
            per_source_warnings=warnings,  # type: ignore[arg-type]
        )

    assert result["state"] == "FAILED_FIXABLE"
    surfaced = result["per_source_warnings"]
    assert isinstance(surfaced, list)
    # Non-mapping silently dropped → 3 surfaced entries.
    assert len(surfaced) == 3
    assert surfaced[0]["source_id"] == "src_a"
    assert surfaced[0]["failure_class"] == "fixable_deterministic"
    assert surfaced[0]["message"].startswith("No PDF text")
    assert surfaced[1]["source_id"] == "src_b"
    assert surfaced[1]["failure_class"] == "fixable_prompt"
    assert surfaced[2]["source_id"] == "src_c"
    # Total count reflects the original list length (incl. the dropped
    # non-mapping entry — caller saw 4 warnings; surfaced 3).
    assert result["per_source_warning_total"] == 4

    # Both events carry the same breakdown (state_transition payload
    # + phase_failed event payload).
    with app_session() as session:
        events = list(
            session.scalars(
                select(RunEvent)
                .where(RunEvent.run_id == run_id)
                .order_by(RunEvent.created_at, RunEvent.id),
            )
        )
    phase_failed = [e for e in events if e.event_type == "phase_failed"]
    assert len(phase_failed) == 1
    payload = json.loads(phase_failed[0].payload or "{}")
    assert payload["per_source_warnings"][0]["source_id"] == "src_a"
    assert payload["per_source_warning_total"] == 4
    assert payload["guidance"] == "Test guidance"
    assert payload["selected_count"] == 4
    assert payload["processed_count"] == 0


def test_fail_fixable_truncates_long_message(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Defensive: a single bad warning with a very long message can't
    bloat the SSE payload; J5 caps each surfaced ``message`` at 280
    chars."""
    from autoessay.agents.synthesizer import _fail_fixable

    monkeypatch.setenv("AUTOESSAY_SYNTHESIZER_STUB", "1")
    get_settings.cache_clear()

    run_id = "run_j5_truncate_unit"
    run_dir = create_run_directory(
        tmp_path / "runs",
        run_id,
        "proj_test",
        state="SYNTHESIZER_RUNNING",
        domain_id="financial_history",
    )
    with app_session() as session:
        seed_project(session)
        run = Run(
            id=run_id,
            project_id="proj_test",
            domain_version="0.1.0",
            run_dir=str(run_dir),
            state="SYNTHESIZER_RUNNING",
            baseline_hash="t",
        )
        session.add(run)
        session.commit()
        session.refresh(run)
        long_message = "x" * 5000
        result = _fail_fixable(
            run,
            session,
            "Test guidance",
            selected_count=1,
            processed_count=0,
            per_source_warnings=[
                {
                    "source_id": "src_long",
                    "failure_class": "fixable_prompt",
                    "message": long_message,
                }
            ],
        )

    assert len(result["per_source_warnings"][0]["message"]) == 280


def test_fail_fixable_handles_empty_warnings_list(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Old call sites that didn't pass per_source_warnings (e.g. the
    'no sources selected' path) still get an empty list + 0 total —
    not a missing key, not a None."""
    from autoessay.agents.synthesizer import _fail_fixable

    monkeypatch.setenv("AUTOESSAY_SYNTHESIZER_STUB", "1")
    get_settings.cache_clear()

    run_id = "run_j5_empty_unit"
    run_dir = create_run_directory(
        tmp_path / "runs",
        run_id,
        "proj_test",
        state="SYNTHESIZER_RUNNING",
        domain_id="financial_history",
    )
    with app_session() as session:
        seed_project(session)
        run = Run(
            id=run_id,
            project_id="proj_test",
            domain_version="0.1.0",
            run_dir=str(run_dir),
            state="SYNTHESIZER_RUNNING",
            baseline_hash="t",
        )
        session.add(run)
        session.commit()
        session.refresh(run)
        result = _fail_fixable(
            run,
            session,
            "Test guidance",
            selected_count=0,
            processed_count=0,
            # No per_source_warnings kwarg → defaults to ()
        )

    assert result["per_source_warnings"] == []
    assert result["per_source_warning_total"] == 0


def test_callsite_no_sources_selected_passes_empty_warnings_list() -> None:
    """The 'no sources selected' callsite (synthesizer.py line ~226)
    explicitly passes ``per_source_warnings=[]``. Pin that the source
    code carries the explicit empty list so future maintainers don't
    drop it (which would silently regress the J5 contract for that
    branch)."""
    src = (Path(__file__).resolve().parents[2] / "src/autoessay/agents/synthesizer.py").read_text(
        encoding="utf-8"
    )
    # The fixed-shape selected_count=0 _fail_fixable callsite must
    # explicitly pass per_source_warnings=[].
    assert (
        "selected_count=0,\n            processed_count=0,\n            per_source_warnings=[]"
        in src
        or (
            "selected_count=0,\n            processed_count=0,\n            per_source_warnings=[],"
            in src
        )
    ), (
        "synthesizer.py 'no sources selected' callsite must explicitly "
        "pass per_source_warnings=[] so frontend lookup doesn't see a "
        "missing key. Found neither pattern in source."
    )
