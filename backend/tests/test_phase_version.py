"""Tests for persistent phase version history (codex-AGREEd #2 stage 2.A).

Covers the lifecycle codex required:
- successful rerun creates new pv, archives blobs, supersedes prior,
  flips run_head
- failed rerun marks pv failed, restores legacy paths from active
- activate_version copies blobs back over legacy paths and flips head
- 409 on activating a non-done version
- input_snapshot_hash always populated; identical-input reruns yield
  the same hash
- first run creates v1 with parent_pv_id=NULL
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from autoessay.main import app
from autoessay.models import (
    Domain,
    PhaseArtifact,
    PhaseVersion,
    Project,
    Run,
    RunHead,
    User,
)
from autoessay.phase_version import (
    activate_version,
    begin_phase_version,
    commit_phase_version,
    fail_phase_version,
    run_with_versioning,
)
from autoessay.run_writer import create_run_directory


def _seed_upstream_phase_artifacts(run_dir: Path) -> None:
    """See ``test_phase_rerun._seed_upstream_phase_artifacts`` — same
    rationale: ``assert_can_rerun`` now invokes ``assert_phase_ready``
    so tests must seed enough upstream artifacts to satisfy each
    phase's deterministic readiness check.
    """
    for relpath, content in (
        ("discovery/skim_candidates.jsonl", '{"id":"x"}\n'),
        ("sources/shortlist.json", '[{"source_id":"x"}]\n'),
        ("synthesis/claims.jsonl", '{"claim_id":"c1","source_id":"x"}\n'),
        ("novelty/selected_thesis.json", '{"angle_id":"angle_001"}'),
    ):
        path = run_dir / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def _seed(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path: Path,
    run_id: str = "run_pv_test",
) -> Path:
    run_dir = create_run_directory(
        tmp_path / "runs",
        run_id,
        "proj_pv",
        state="USER_DEEP_DIVE_REVIEW",
        domain_id="financial_history",
    )
    with app_session() as session:
        session.add(User(id="single-user", display_name="Single User"))
        session.add(
            Domain(
                id="financial_history",
                display_name="Financial History",
                version="0.1.0",
                enabled=True,
            ),
        )
        session.flush()
        session.add(
            Project(
                id="proj_pv",
                user_id="single-user",
                title="t",
                domain_id="financial_history",
                domain_version="0.1.0",
                status="CREATED",
            ),
        )
        session.add(
            Run(
                id=run_id,
                project_id="proj_pv",
                domain_version="0.1.0",
                run_dir=str(run_dir),
                state="USER_DEEP_DIVE_REVIEW",
                baseline_hash="x",
            ),
        )
        session.commit()
    return run_dir


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


async def test_first_run_creates_v1_with_no_parent(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path,
) -> None:
    run_dir = _seed(app_session, tmp_path)
    legacy = run_dir / "synthesis" / "claims.jsonl"
    with app_session() as session:
        run = session.get(Run, "run_pv_test")
        # Simulate the synthesizer writing a single artifact during
        # the run (snapshot-diff attributes ownership to this pv).
        run_with_versioning(
            session,
            run,
            "synthesizer",
            lambda: _write(legacy, '{"k": 1}'),
            created_by="single-user",
        )
        head = session.scalar(
            __import__("sqlalchemy")
            .select(RunHead.version_id)
            .where(RunHead.run_id == "run_pv_test")
            .where(RunHead.phase == "synthesizer"),
        )
        pv = session.get(PhaseVersion, head)
        assert pv is not None
        assert pv.version_no == 1
        assert pv.parent_pv_id is None
        assert pv.status == "done"
        # Artifact archived under the per-pv dir.
        archived = session.scalars(
            __import__("sqlalchemy")
            .select(PhaseArtifact)
            .where(PhaseArtifact.phase_version_id == pv.id),
        ).all()
        assert len(archived) == 1
        assert archived[0].logical_path == "synthesis/claims.jsonl"
        assert archived[0].blob_path.startswith("phases/")


async def test_rerun_creates_v2_supersedes_v1(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path,
) -> None:
    run_dir = _seed(app_session, tmp_path)
    legacy = run_dir / "synthesis" / "claims.jsonl"
    with app_session() as session:
        run = session.get(Run, "run_pv_test")
        run_with_versioning(
            session,
            run,
            "synthesizer",
            lambda: _write(legacy, '{"v": 1}'),
        )
        # Re-run: agent overwrites legacy path.
        run_with_versioning(
            session,
            run,
            "synthesizer",
            lambda: _write(legacy, '{"v": 2}'),
        )
        rows = session.scalars(
            __import__("sqlalchemy")
            .select(PhaseVersion)
            .where(PhaseVersion.run_id == "run_pv_test")
            .where(PhaseVersion.phase == "synthesizer")
            .order_by(PhaseVersion.version_no.asc()),
        ).all()
        assert len(rows) == 2
        assert rows[0].version_no == 1
        # Stage 2.C dropped global supersede; status stays "done", activeness is per-branch.
        assert rows[0].status == "done"
        assert rows[1].version_no == 2
        assert rows[1].status == "done"
        assert rows[1].parent_pv_id == rows[0].id
        head = session.scalar(
            __import__("sqlalchemy")
            .select(RunHead.version_id)
            .where(RunHead.run_id == "run_pv_test")
            .where(RunHead.phase == "synthesizer"),
        )
        assert head == rows[1].id


async def test_failed_rerun_restores_legacy_from_prior_active(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path,
) -> None:
    run_dir = _seed(app_session, tmp_path)
    legacy = run_dir / "synthesis" / "claims.jsonl"
    with app_session() as session:
        run = session.get(Run, "run_pv_test")
        # First run produces v1 with content "good".
        run_with_versioning(
            session,
            run,
            "synthesizer",
            lambda: _write(legacy, '{"good": true}'),
        )

        # Re-run that crashes mid-write — the agent has already
        # written corrupt content to the legacy path before raising.
        def boom() -> None:
            _write(legacy, "CORRUPT")
            raise RuntimeError("simulated agent failure")

        with pytest.raises(RuntimeError):
            run_with_versioning(session, run, "synthesizer", boom)
        # Legacy file must be restored from v1's archived blob.
        assert legacy.read_text() == '{"good": true}'
        # v2 is recorded as failed; v1 remains the active head.
        rows = session.scalars(
            __import__("sqlalchemy")
            .select(PhaseVersion)
            .where(PhaseVersion.run_id == "run_pv_test")
            .where(PhaseVersion.phase == "synthesizer")
            .order_by(PhaseVersion.version_no.asc()),
        ).all()
        assert rows[0].status == "done"
        assert rows[1].status == "failed"
        head = session.scalar(
            __import__("sqlalchemy")
            .select(RunHead.version_id)
            .where(RunHead.run_id == "run_pv_test")
            .where(RunHead.phase == "synthesizer"),
        )
        assert head == rows[0].id


async def test_input_snapshot_hash_stable_for_same_upstream(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path,
) -> None:
    run_dir = _seed(app_session, tmp_path)
    legacy = run_dir / "synthesis" / "claims.jsonl"
    with app_session() as session:
        run = session.get(Run, "run_pv_test")
        run_with_versioning(session, run, "synthesizer", lambda: _write(legacy, "x"))
        # Re-run with identical upstream (no upstream phase pv head
        # changes). hash must match the prior pv's hash.
        run_with_versioning(session, run, "synthesizer", lambda: _write(legacy, "y"))
        rows = session.scalars(
            __import__("sqlalchemy")
            .select(PhaseVersion)
            .where(PhaseVersion.run_id == "run_pv_test")
            .where(PhaseVersion.phase == "synthesizer")
            .order_by(PhaseVersion.version_no.asc()),
        ).all()
        assert rows[0].input_snapshot_hash == rows[1].input_snapshot_hash


async def test_list_versions_endpoint_returns_active_marker(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path,
) -> None:
    run_dir = _seed(app_session, tmp_path)
    legacy = run_dir / "synthesis" / "claims.jsonl"
    with app_session() as session:
        run = session.get(Run, "run_pv_test")
        run_with_versioning(session, run, "synthesizer", lambda: _write(legacy, '{"v": 1}'))
        run_with_versioning(session, run, "synthesizer", lambda: _write(legacy, '{"v": 2}'))
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/runs/run_pv_test/phases/synthesizer/versions")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["phase"] == "synthesizer"
    assert len(body["versions"]) == 2
    assert body["active_version_id"] == body["versions"][0]["id"]
    assert body["versions"][0]["version_no"] == 2
    assert body["versions"][0]["is_active"] is True
    assert body["versions"][1]["is_active"] is False
    # Stage 3.E: phase that produced output reports
    # has_completed_output=True (regardless of whether the output came
    # from a versioned rerun or the initial vanilla run).
    assert body["has_completed_output"] is True


async def test_list_versions_completed_output_without_rerun(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path,
) -> None:
    """Stage 3.E: a phase that has produced output via the initial
    (non-versioned) run still reports has_completed_output=True so
    the UI can offer "Rerun phase" before any rerun has happened."""
    run_dir = _seed(app_session, tmp_path)
    # Initial vanilla run writes the legacy sentinel but does NOT go
    # through run_with_versioning, so phase_versions stays empty.
    legacy = run_dir / "synthesis" / "claims.jsonl"
    _write(legacy, '{"vanilla": true}')
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/runs/run_pv_test/phases/synthesizer/versions")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["versions"] == []
    assert body["has_completed_output"] is True


async def test_list_versions_no_output_reports_false(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path,
) -> None:
    _seed(app_session, tmp_path)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/runs/run_pv_test/phases/synthesizer/versions")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["versions"] == []
    assert body["has_completed_output"] is False


async def test_activate_older_version_flips_head_and_legacy(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path,
) -> None:
    run_dir = _seed(app_session, tmp_path)
    legacy = run_dir / "synthesis" / "claims.jsonl"
    with app_session() as session:
        run = session.get(Run, "run_pv_test")
        run_with_versioning(session, run, "synthesizer", lambda: _write(legacy, "v1-content"))
        v1_id = session.scalar(
            __import__("sqlalchemy")
            .select(PhaseVersion.id)
            .where(PhaseVersion.run_id == "run_pv_test")
            .where(PhaseVersion.phase == "synthesizer")
            .order_by(PhaseVersion.version_no.asc())
            .limit(1),
        )
        run_with_versioning(session, run, "synthesizer", lambda: _write(legacy, "v2-content"))
        # Currently legacy = v2 content.
        assert legacy.read_text() == "v2-content"
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            f"/api/runs/run_pv_test/phases/synthesizer/versions/{v1_id}/activate"
        )
    assert resp.status_code == 200, resp.text
    assert legacy.read_text() == "v1-content"
    with app_session() as session:
        head = session.scalar(
            __import__("sqlalchemy")
            .select(RunHead.version_id)
            .where(RunHead.run_id == "run_pv_test")
            .where(RunHead.phase == "synthesizer"),
        )
        assert head == v1_id


async def test_activate_failed_version_returns_409(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path,
) -> None:
    run_dir = _seed(app_session, tmp_path)
    legacy = run_dir / "synthesis" / "claims.jsonl"
    with app_session() as session:
        run = session.get(Run, "run_pv_test")
        run_with_versioning(session, run, "synthesizer", lambda: _write(legacy, "ok"))

        def boom() -> None:
            raise RuntimeError("x")

        with pytest.raises(RuntimeError):
            run_with_versioning(session, run, "synthesizer", boom)
        failed_pv_id = session.scalar(
            __import__("sqlalchemy")
            .select(PhaseVersion.id)
            .where(PhaseVersion.status == "failed")
            .limit(1),
        )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            f"/api/runs/run_pv_test/phases/synthesizer/versions/{failed_pv_id}/activate"
        )
    assert resp.status_code == 409


async def test_unit_begin_then_fail_then_commit_path(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path,
) -> None:
    """Direct unit-level path: begin -> fail must not advance head;
    a subsequent begin -> commit should still produce v2 (failed v
    counts toward version_no as codex agreed)."""
    run_dir = _seed(app_session, tmp_path, run_id="run_pv_unit")
    legacy = run_dir / "synthesis" / "claims.jsonl"
    with app_session() as session:
        run = session.get(Run, "run_pv_unit")
        # First successful run -> v1. Write inside the run so the
        # snapshot-diff attributes the file to pv1.
        pv1, prior1 = begin_phase_version(session, run, "synthesizer")
        session.commit()
        assert prior1 is None
        _write(legacy, "first")
        commit_phase_version(session, run, pv1)
        session.commit()
        # Begin a 2nd run, then fail it.
        pv2, prior2 = begin_phase_version(session, run, "synthesizer")
        session.commit()
        assert prior2 == pv1.id
        # Simulate the agent dirty-writing then failing.
        _write(legacy, "dirty")
        fail_phase_version(session, run, pv2, prior2)
        session.commit()
        assert legacy.read_text() == "first"
        assert pv2.status == "failed"
        # Begin a 3rd run that succeeds -> version_no=3, parent=v1.
        pv3, prior3 = begin_phase_version(session, run, "synthesizer")
        session.commit()
        assert prior3 == pv1.id  # still pointing at the active head v1
        _write(legacy, "third")
        commit_phase_version(session, run, pv3)
        session.commit()
        assert pv3.version_no == 3
        assert pv3.parent_pv_id == pv1.id


async def test_shared_drafts_dir_attribution(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path,
) -> None:
    """Drafter and stylist both write under ``drafts/`` but to disjoint
    files. A stylist rerun must NOT archive the drafter's manuscript."""
    run_dir = _seed(app_session, tmp_path, run_id="run_pv_share")
    drafter_file = run_dir / "drafts" / "v001" / "manuscript.md"
    stylist_file = run_dir / "drafts" / "v001" / "style" / "stop_slop_score.json"
    with app_session() as session:
        run = session.get(Run, "run_pv_share")
        # Drafter run produces manuscript.md.
        run_with_versioning(
            session,
            run,
            "drafter",
            lambda: _write(drafter_file, "manuscript text"),
        )
        # Stylist run produces only the style score file. Its archived
        # artifact set must contain ONLY style/stop_slop_score.json,
        # not the drafter's manuscript.md.
        run_with_versioning(
            session,
            run,
            "stylist",
            lambda: _write(stylist_file, '{"score": 0.8}'),
        )
        stylist_pv = session.scalar(
            __import__("sqlalchemy")
            .select(PhaseVersion)
            .where(PhaseVersion.phase == "stylist")
            .where(PhaseVersion.run_id == "run_pv_share"),
        )
        assert stylist_pv is not None
        archived_paths = sorted(
            a.logical_path
            for a in session.scalars(
                __import__("sqlalchemy")
                .select(PhaseArtifact)
                .where(PhaseArtifact.phase_version_id == stylist_pv.id),
            ).all()
        )
        assert archived_paths == ["drafts/v001/style/stop_slop_score.json"]


async def test_first_run_failure_preserves_partial_output(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path,
) -> None:
    """PR-A4.1b (2026-05-02): when the very first run fails, the
    agent's partial writes are PRESERVED on disk so the user can
    inspect failure-evidence files (e.g. ``discovery/warnings.jsonl``).
    There is no prior version to restore from, so purge+restore
    would leave the working set empty and lose every diagnostic
    breadcrumb the agent wrote before crashing.

    The phase_versions row still records ``status='failed'`` and
    RunHead does not advance — so subsequent reruns can begin a
    fresh pv and overwrite this debris.
    """
    run_dir = _seed(app_session, tmp_path, run_id="run_pv_first_fail")
    legacy = run_dir / "synthesis" / "claims.jsonl"
    with app_session() as session:
        run = session.get(Run, "run_pv_first_fail")

        def boom() -> None:
            _write(legacy, "PARTIAL")
            raise RuntimeError("first-run crash")

        with pytest.raises(RuntimeError):
            run_with_versioning(session, run, "synthesizer", boom)
        # Partial output stays for debugging.
        assert legacy.exists()
        assert legacy.read_text() == "PARTIAL"
        # And the pv row is recorded as failed — RunHead absent.
        from sqlalchemy import select as _select

        failed = session.scalars(
            _select(PhaseVersion)
            .where(PhaseVersion.run_id == "run_pv_first_fail")
            .where(PhaseVersion.phase == "synthesizer")
            .where(PhaseVersion.status == "failed"),
        ).all()
        assert len(failed) == 1
        head = session.scalar(
            _select(RunHead.version_id)
            .where(RunHead.run_id == "run_pv_first_fail")
            .where(RunHead.phase == "synthesizer"),
        )
        assert head is None


async def test_graceful_failure_state_records_failed_version(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path,
) -> None:
    """If the runner returns normally but transitioned the run to
    ``FAILED_FIXABLE``, the pv must be recorded as failed and run_head
    must NOT advance to it."""
    run_dir = _seed(app_session, tmp_path, run_id="run_pv_fixable")
    legacy = run_dir / "synthesis" / "claims.jsonl"
    with app_session() as session:
        run = session.get(Run, "run_pv_fixable")
        # First, a clean v1 so we can verify run_head sticks on it.
        run_with_versioning(session, run, "synthesizer", lambda: _write(legacy, "v1"))

        # Second, a runner that "fails fixably": writes a half-baked
        # output and sets state to FAILED_FIXABLE without raising.
        def graceful_fail() -> None:
            _write(legacy, "half-baked")
            run.state = "FAILED_FIXABLE"

        run_with_versioning(session, run, "synthesizer", graceful_fail)
        rows = session.scalars(
            __import__("sqlalchemy")
            .select(PhaseVersion)
            .where(PhaseVersion.run_id == "run_pv_fixable")
            .where(PhaseVersion.phase == "synthesizer")
            .order_by(PhaseVersion.version_no.asc()),
        ).all()
        assert rows[1].status == "failed"
        head = session.scalar(
            __import__("sqlalchemy")
            .select(RunHead.version_id)
            .where(RunHead.run_id == "run_pv_fixable")
            .where(RunHead.phase == "synthesizer"),
        )
        assert head == rows[0].id
        # And legacy is restored from v1.
        assert legacy.read_text() == "v1"


async def test_activate_succeeds_even_when_branch_stale_from_phase_set(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path,
) -> None:
    """PR-A4.3 (codex 2026-05-02 review): the activate endpoint
    used to call ``assert_can_rerun`` which 409s when
    ``branch.stale_from_phase`` is set. That gate is too broad
    for activation: the cascade itself is the recovery action,
    and the new lineage-vector match decides which downstream
    heads survive. The narrower guard now only rejects on
    cancelled / RUNNING_STATES.

    Verifies activation now SUCCEEDS in the same fixture that
    previously returned 409.
    """
    run_dir = _seed(app_session, tmp_path, run_id="run_pv_stale")
    syn = run_dir / "synthesis" / "claims.jsonl"
    nov = run_dir / "novelty" / "angle_cards.json"
    with app_session() as session:
        run = session.get(Run, "run_pv_stale")
        run_with_versioning(session, run, "synthesizer", lambda: _write(syn, "syn-v1"))
        run_with_versioning(session, run, "ideator", lambda: _write(nov, "nov-v1"))
        # A second ideator run for an older version to activate later.
        run_with_versioning(session, run, "ideator", lambda: _write(nov, "nov-v2"))
        ideator_v1_id = session.scalar(
            __import__("sqlalchemy")
            .select(PhaseVersion.id)
            .where(PhaseVersion.run_id == "run_pv_stale")
            .where(PhaseVersion.phase == "ideator")
            .order_by(PhaseVersion.version_no.asc())
            .limit(1),
        )
        # Plant a branch.stale_from_phase pointer that under the
        # OLD design would have blocked activate via
        # assert_can_rerun. Under the new design it's just a
        # legacy cache field; the per-pv computed state in
        # PR-A4.2's phase-history endpoint is the source of
        # truth.
        from autoessay.branches import ensure_main_branch, set_branch_stale

        ensure_main_branch(session, run)
        set_branch_stale(session, run, "synthesizer")
        session.commit()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            f"/api/runs/run_pv_stale/phases/ideator/versions/{ideator_v1_id}/activate"
        )
    assert resp.status_code == 200, resp.text


async def test_archive_is_full_snapshot_not_delta(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path,
) -> None:
    """A pv must contain every owned file at commit time, not just
    files that changed since the previous run. Restoring an older pv
    that didn't touch file X must still reproduce X."""
    run_dir = _seed(app_session, tmp_path, run_id="run_pv_full_snap")
    # Use the canonical sentinel filename so assert_can_rerun's
    # has_completed_output predicate sees the phase as rerunnable.
    a = run_dir / "synthesis" / "claims.jsonl"
    b = run_dir / "synthesis" / "extra.json"
    with app_session() as session:
        run = session.get(Run, "run_pv_full_snap")

        def first() -> None:
            _write(a, "a-v1")
            _write(b, "b-v1")

        run_with_versioning(session, run, "synthesizer", first)
        # Second run only touches claims.jsonl. extra.json unchanged.
        run_with_versioning(session, run, "synthesizer", lambda: _write(a, "a-v2"))
        v1_id = session.scalar(
            __import__("sqlalchemy")
            .select(PhaseVersion.id)
            .where(PhaseVersion.run_id == "run_pv_full_snap")
            .where(PhaseVersion.phase == "synthesizer")
            .order_by(PhaseVersion.version_no.asc())
            .limit(1),
        )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            f"/api/runs/run_pv_full_snap/phases/synthesizer/versions/{v1_id}/activate"
        )
    assert resp.status_code == 200, resp.text
    # Both files must come back to their v1 content — even b.json,
    # which the v2 run did not write.
    assert a.read_text() == "a-v1"
    assert b.read_text() == "b-v1"


async def test_first_rerun_failure_preserves_failure_evidence(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path,
) -> None:
    """PR-A4.1b (2026-05-02): when the first rerun runs with no
    prior tracked pv (the migration didn't backfill this run, or
    it's a fresh new agent invocation pre-migration), and the
    rerun crashes, ``fail_phase_version`` no longer purge+restores
    because there is no good prior state to point to.

    Instead, the agent's partial writes survive so the user can
    inspect what went wrong. This is the trade-off codex AGREEd
    to in 2026-05-02 PR-A4 design review: failure evidence beats
    restoring pre-existing legacy.

    For runs that DO have a prior tracked pv, the prior content is
    restored — see :func:`test_graceful_failure_state_records_failed_version`.
    """
    run_dir = _seed(app_session, tmp_path, run_id="run_pv_legacy_safe")
    legacy = run_dir / "synthesis" / "claims.jsonl"
    # Existing unversioned legacy output (simulates a normal first
    # run that ran before any rerun, on a run that pre-dates
    # migration 017 and so has no pv row tracking yet).
    _write(legacy, "pre-existing")
    with app_session() as session:
        run = session.get(Run, "run_pv_legacy_safe")

        def boom() -> None:
            _write(legacy, "GARBAGE FROM CRASHED AGENT")
            raise RuntimeError("first-rerun crash")

        with pytest.raises(RuntimeError):
            run_with_versioning(session, run, "synthesizer", boom)
        # Agent's partial writes survive (lossy for pre-existing
        # legacy on this code path, but the trade-off is debugging
        # information). For runs that have already been backfilled
        # by migration 017, prior is no longer None and the
        # restore path runs.
        assert legacy.read_text() == "GARBAGE FROM CRASHED AGENT"


async def test_failed_vendor_state_records_failed_version(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path,
) -> None:
    """FAILED_VENDOR (scout/integrity) is also a graceful failure
    state — runner returns normally but state is not 'done'."""
    run_dir = _seed(app_session, tmp_path, run_id="run_pv_vendor")
    legacy = run_dir / "discovery" / "scout_report.md"
    with app_session() as session:
        run = session.get(Run, "run_pv_vendor")
        run_with_versioning(session, run, "scout", lambda: _write(legacy, "good"))

        def vendor_fail() -> None:
            _write(legacy, "garbage")
            run.state = "FAILED_VENDOR"

        run_with_versioning(session, run, "scout", vendor_fail)
        rows = session.scalars(
            __import__("sqlalchemy")
            .select(PhaseVersion)
            .where(PhaseVersion.run_id == "run_pv_vendor")
            .where(PhaseVersion.phase == "scout")
            .order_by(PhaseVersion.version_no.asc()),
        ).all()
        assert rows[1].status == "failed"
        # And legacy is restored from the prerun backup.
        assert legacy.read_text() == "good"


async def test_activate_rmdir_empty_versioned_subdirs(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path,
) -> None:
    """Activating an older drafter pv must remove the now-empty
    ``drafts/v002/`` directory, otherwise drafter readers that pick
    the highest-numbered draft dir would still resolve to v002 even
    though it contains no files."""
    run_dir = _seed(app_session, tmp_path, run_id="run_pv_rmdir")
    v1_manuscript = run_dir / "drafts" / "v001" / "manuscript.md"
    v2_manuscript = run_dir / "drafts" / "v002" / "manuscript.md"
    with app_session() as session:
        run = session.get(Run, "run_pv_rmdir")
        run_with_versioning(session, run, "drafter", lambda: _write(v1_manuscript, "v1"))
        # Second drafter run writes to v002/.
        run_with_versioning(session, run, "drafter", lambda: _write(v2_manuscript, "v2"))
        v1_id = session.scalar(
            __import__("sqlalchemy")
            .select(PhaseVersion.id)
            .where(PhaseVersion.run_id == "run_pv_rmdir")
            .where(PhaseVersion.phase == "drafter")
            .order_by(PhaseVersion.version_no.asc())
            .limit(1),
        )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(f"/api/runs/run_pv_rmdir/phases/drafter/versions/{v1_id}/activate")
    assert resp.status_code == 200, resp.text
    assert v1_manuscript.exists()
    # The v002/ directory must be gone now that its contents were purged.
    assert not (run_dir / "drafts" / "v002").exists()


async def test_drafter_activate_does_not_clobber_stylist_files(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path,
) -> None:
    """Activating a drafter pv purges only drafter-owned files. The
    stylist's drafts/<v>/style/* files must survive untouched."""
    run_dir = _seed(app_session, tmp_path, run_id="run_pv_disjoint")
    manuscript = run_dir / "drafts" / "v001" / "manuscript.md"
    style_score = run_dir / "drafts" / "v001" / "style" / "score.json"
    with app_session() as session:
        run = session.get(Run, "run_pv_disjoint")
        run_with_versioning(
            session,
            run,
            "drafter",
            lambda: _write(manuscript, "manuscript-v1"),
        )
        run_with_versioning(
            session,
            run,
            "stylist",
            lambda: _write(style_score, '{"score": 1}'),
        )
        # New drafter run with different content -> v2.
        run_with_versioning(
            session,
            run,
            "drafter",
            lambda: _write(manuscript, "manuscript-v2"),
        )
        drafter_v1_id = session.scalar(
            __import__("sqlalchemy")
            .select(PhaseVersion.id)
            .where(PhaseVersion.run_id == "run_pv_disjoint")
            .where(PhaseVersion.phase == "drafter")
            .order_by(PhaseVersion.version_no.asc())
            .limit(1),
        )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            f"/api/runs/run_pv_disjoint/phases/drafter/versions/{drafter_v1_id}/activate"
        )
    assert resp.status_code == 200, resp.text
    assert manuscript.read_text() == "manuscript-v1"
    # Stylist's score file must be untouched by the drafter activation.
    assert style_score.exists()
    assert style_score.read_text() == '{"score": 1}'


def test_framework_lens_activate_restores_self_contained_input_ref(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path,
) -> None:
    """PR-G1: framework_lens owns its version hook inside
    framework_lens.json. Activating an older lens version must restore
    the matching schema-v2 input ref without mutating synthesizer.json.
    """
    run_dir = _seed(app_session, tmp_path, run_id="run_pv_lens_ref_restore")
    synth = run_dir / "synthesis" / "synthesizer.json"
    lens = run_dir / "synthesis" / "framework_lens.json"
    with app_session() as session:
        run = session.get(Run, "run_pv_lens_ref_restore")
        run_with_versioning(
            session,
            run,
            "synthesizer",
            lambda: _write(synth, '{"schema_version": 1, "primary_track": [{"v": 1}]}'),
        )
        synth_v1_id = session.scalar(
            __import__("sqlalchemy")
            .select(RunHead.version_id)
            .where(RunHead.run_id == "run_pv_lens_ref_restore")
            .where(RunHead.phase == "synthesizer"),
        )

        def lens_v1() -> None:
            _write(
                lens,
                json.dumps(
                    {
                        "schema_version": 2,
                        "paper_mode": "theory_article",
                        "synthesizer_input_ref": {
                            "synthesizer_pv_id": synth_v1_id,
                            "synthesizer_artifact_hash": "hash-v1",
                        },
                        "signals": [],
                    }
                ),
            )

        run_with_versioning(session, run, "framework_lens", lens_v1)
        lens_v1_id = session.scalar(
            __import__("sqlalchemy")
            .select(PhaseVersion.id)
            .where(PhaseVersion.run_id == "run_pv_lens_ref_restore")
            .where(PhaseVersion.phase == "framework_lens")
            .order_by(PhaseVersion.version_no.asc())
            .limit(1),
        )

        def lens_v2() -> None:
            _write(
                lens,
                json.dumps(
                    {
                        "schema_version": 2,
                        "paper_mode": "theory_article",
                        "synthesizer_input_ref": {
                            "synthesizer_pv_id": synth_v1_id,
                            "synthesizer_artifact_hash": "hash-v2",
                        },
                        "signals": [],
                    }
                ),
            )

        run_with_versioning(session, run, "framework_lens", lens_v2)

    assert (
        json.loads(lens.read_text(encoding="utf-8"))["synthesizer_input_ref"][
            "synthesizer_artifact_hash"
        ]
        == "hash-v2"
    )
    with app_session() as session:
        run = session.get(Run, "run_pv_lens_ref_restore")
        assert run is not None
        assert lens_v1_id is not None
        activate_version(session, run, "framework_lens", lens_v1_id)
        session.commit()
    restored = json.loads(lens.read_text(encoding="utf-8"))
    assert restored["synthesizer_input_ref"] == {
        "synthesizer_pv_id": synth_v1_id,
        "synthesizer_artifact_hash": "hash-v1",
    }
    assert "framework_lens_summary_ref" not in json.loads(synth.read_text(encoding="utf-8"))


def test_synthesizer_rerun_cascade_purges_lens_without_synth_hook(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path,
) -> None:
    """PR-G1: when a new synthesizer head invalidates framework_lens,
    cascade purges only the lens-owned artifact. No hook field is
    restored into synthesizer.json.
    """
    run_dir = _seed(app_session, tmp_path, run_id="run_pv_lens_cascade")
    synth = run_dir / "synthesis" / "synthesizer.json"
    lens = run_dir / "synthesis" / "framework_lens.json"
    with app_session() as session:
        run = session.get(Run, "run_pv_lens_cascade")
        run_with_versioning(
            session,
            run,
            "synthesizer",
            lambda: _write(synth, '{"schema_version": 1, "primary_track": [{"v": 1}]}'),
        )
        synth_v1_id = session.scalar(
            __import__("sqlalchemy")
            .select(RunHead.version_id)
            .where(RunHead.run_id == "run_pv_lens_cascade")
            .where(RunHead.phase == "synthesizer"),
        )
        run_with_versioning(
            session,
            run,
            "framework_lens",
            lambda: _write(
                lens,
                json.dumps(
                    {
                        "schema_version": 2,
                        "synthesizer_input_ref": {
                            "synthesizer_pv_id": synth_v1_id,
                            "synthesizer_artifact_hash": "hash-v1",
                        },
                        "signals": [],
                    }
                ),
            ),
        )
        assert lens.exists()
        run_with_versioning(
            session,
            run,
            "synthesizer",
            lambda: _write(synth, '{"schema_version": 1, "primary_track": [{"v": 2}]}'),
        )

    assert not lens.exists()
    assert "framework_lens_summary_ref" not in json.loads(synth.read_text(encoding="utf-8"))
