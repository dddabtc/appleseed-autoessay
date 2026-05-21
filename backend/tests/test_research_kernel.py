"""Tests for PR-C0.b1 research_kernel module + edit endpoint."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import seed_project
from httpx import ASGITransport, AsyncClient

from autoessay.branches import ensure_main_branch, get_branch_stale
from autoessay.main import app
from autoessay.models import Branch, PhaseVersion, Run, RunHead
from autoessay.research_kernel import (
    compute_kernel_hash,
    has_any_pipeline_completion,
    kernel_snapshot_path,
    normalize_kernel_for_hash,
    stale_marks_after_kernel_edit,
    write_kernel_snapshot,
)

# ---------------------------------------------------------------------------
# Pure-function tests
# ---------------------------------------------------------------------------


def test_normalize_kernel_for_hash_dict_order_invariant() -> None:
    a = {"x": 1, "y": 2}
    b = {"y": 2, "x": 1}
    assert normalize_kernel_for_hash(a) == normalize_kernel_for_hash(b)


def test_normalize_kernel_for_hash_preserves_string_whitespace() -> None:
    """Strings with internal whitespace are NOT collapsed; user
    intent preserved (codex round-3 answer 1)."""
    a = {"q": "hello world"}
    b = {"q": "hello  world"}
    assert normalize_kernel_for_hash(a) != normalize_kernel_for_hash(b)


def test_compute_kernel_hash_deterministic() -> None:
    h1 = compute_kernel_hash("case_analysis", {"q": "x"})
    h2 = compute_kernel_hash("case_analysis", {"q": "x"})
    assert h1 == h2
    assert len(h1) == 64  # sha256 hex


def test_compute_kernel_hash_changes_on_mode_change() -> None:
    h1 = compute_kernel_hash("case_analysis", {"q": "x"})
    h2 = compute_kernel_hash("empirical", {"q": "x"})
    assert h1 != h2


def test_compute_kernel_hash_changes_on_kernel_change() -> None:
    h1 = compute_kernel_hash("case_analysis", {"q": "x"})
    h2 = compute_kernel_hash("case_analysis", {"q": "y"})
    assert h1 != h2


def test_kernel_snapshot_path() -> None:
    p = kernel_snapshot_path(Path("/r"), 7)
    assert p == Path("/r/proposal/research_kernel_v007.json")


def test_write_kernel_snapshot_atomic(tmp_path: Path) -> None:
    target = write_kernel_snapshot(
        run_dir=tmp_path,
        proposal_version=2,
        paper_mode="case_analysis",
        kernel={"observed_puzzle": "test", "kernel_schema_version": 1},
        timestamp_utc="2026-05-02T20:00:00Z",
    )
    assert target.exists()
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["proposal_version"] == 2
    assert payload["paper_mode"] == "case_analysis"
    assert payload["kernel_schema_version"] == 1
    assert payload["timestamp_utc"] == "2026-05-02T20:00:00Z"
    assert payload["kernel"]["observed_puzzle"] == "test"


def test_write_kernel_snapshot_idempotent(tmp_path: Path) -> None:
    """Re-writing same version overwrites in place (replace mode)."""
    write_kernel_snapshot(
        run_dir=tmp_path,
        proposal_version=1,
        paper_mode="case_analysis",
        kernel={"observed_puzzle": "v1"},
        timestamp_utc="2026-05-02T20:00:00Z",
    )
    target = write_kernel_snapshot(
        run_dir=tmp_path,
        proposal_version=1,
        paper_mode="case_analysis",
        kernel={"observed_puzzle": "v2"},
        timestamp_utc="2026-05-02T20:01:00Z",
    )
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["kernel"]["observed_puzzle"] == "v2"
    # Only one file in the dir.
    assert len(list((tmp_path / "proposal").glob("research_kernel_v*.json"))) == 1


def test_write_kernel_snapshot_creates_dir(tmp_path: Path) -> None:
    """Parent ``proposal/`` dir created if missing."""
    assert not (tmp_path / "proposal").exists()
    write_kernel_snapshot(
        run_dir=tmp_path,
        proposal_version=1,
        paper_mode="case_analysis",
        kernel={},
        timestamp_utc="2026-05-02T20:00:00Z",
    )
    assert (tmp_path / "proposal" / "research_kernel_v001.json").exists()


# ---------------------------------------------------------------------------
# Stale-propagation helpers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_has_any_pipeline_completion_false_for_empty_run(
    app_session,  # type: ignore[no-untyped-def]
) -> None:
    """A run that's done proposal but no pipeline phases yet:
    has_any_pipeline_completion returns False, so the kernel-edit
    endpoint takes the no-downstream-completed branch."""
    with app_session() as session:
        seed_project(session)
        run = Run(
            id="run_no_pipeline",
            project_id="proj_test",
            domain_version="0.1.0",
            run_dir="/tmp/test",
            state="USER_PROPOSAL_REVIEW",
            baseline_hash="x",
        )
        session.add(run)
        session.flush()
        # Add an active branch but no pipeline RunHead rows.
        from datetime import datetime, timezone

        session.add(
            Branch(
                id="branch_main",
                run_id="run_no_pipeline",
                name="main",
                created_at=datetime.now(timezone.utc),
            ),
        )
        session.commit()

        assert has_any_pipeline_completion(session, "run_no_pipeline") is False
        assert stale_marks_after_kernel_edit(session, "run_no_pipeline") == []


@pytest.mark.asyncio
async def test_has_any_pipeline_completion_true_when_scout_done(
    app_session,  # type: ignore[no-untyped-def]
) -> None:
    """After scout completes, kernel edit triggers stale-on-scout."""
    with app_session() as session:
        seed_project(session)
        run = Run(
            id="run_scout_done",
            project_id="proj_test",
            domain_version="0.1.0",
            run_dir="/tmp/test",
            state="USER_SEARCH_REVIEW",
            baseline_hash="x",
        )
        session.add(run)
        session.flush()
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)
        session.add(
            Branch(
                id="branch_main2",
                run_id="run_scout_done",
                name="main",
                created_at=now,
            ),
        )
        # Add a done scout pv + RunHead pointing at it.
        session.add(
            PhaseVersion(
                id="pv_scout_v1",
                run_id="run_scout_done",
                phase="scout",
                version_no=1,
                source="agent",
                status="done",
                artifacts_dir="phases/scout/v1",
                created_on_branch_id="branch_main2",
                created_at=now,
                completed_at=now,
            ),
        )
        session.add(
            RunHead(
                run_id="run_scout_done",
                branch_id="branch_main2",
                phase="scout",
                version_id="pv_scout_v1",
            ),
        )
        session.commit()

        assert has_any_pipeline_completion(session, "run_scout_done") is True
        marks = stale_marks_after_kernel_edit(session, "run_scout_done")
        assert marks == [("branch_main2", "scout")]


# ---------------------------------------------------------------------------
# PUT /api/runs/{id}/research_kernel — endpoint integration tests
# ---------------------------------------------------------------------------


def _kernel_edit_body(
    paper_mode: str,
    kernel: dict,
    base_proposal_version: int,
    base_kernel_hash: str,
    *,
    accept_developer_preview: bool = False,
) -> dict:
    return {
        "paper_mode": paper_mode,
        "kernel": kernel,
        "base_proposal_version": base_proposal_version,
        "base_kernel_hash": base_kernel_hash,
        "accept_developer_preview": accept_developer_preview,
    }


@pytest.mark.asyncio
async def test_kernel_edit_pre_proposal_db_only(
    app_session,  # type: ignore[no-untyped-def]
    monkeypatch,
    tmp_path: Path,
) -> None:
    """Pre-proposal: DB updated, NO snapshot written, no version
    bump."""
    monkeypatch.setenv("AUTOESSAY_AUTH_BYPASS", "1")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    with app_session() as session:
        seed_project(session)
        run = Run(
            id="run_pre_proposal",
            project_id="proj_test",
            domain_version="0.1.0",
            run_dir=str(run_dir),
            state="DOMAIN_LOADED",
            baseline_hash="x",
            paper_mode="case_analysis",
            research_kernel_json={"kernel_schema_version": 1},
        )
        session.add(run)
        session.commit()

    base_hash = compute_kernel_hash("case_analysis", {"kernel_schema_version": 1})

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.put(
            "/api/runs/run_pre_proposal/research_kernel",
            json=_kernel_edit_body(
                "case_analysis",
                {"kernel_schema_version": 1, "observed_puzzle": "first edit"},
                0,
                base_hash,
            ),
        )

    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["proposal_version"] == 0
    assert payload["kernel"]["observed_puzzle"] == "first edit"
    assert (run_dir / "proposal").exists() is False  # no file written


@pytest.mark.asyncio
async def test_kernel_edit_pre_proposal_marks_scout_stale_when_pipeline_completed(
    app_session,  # type: ignore[no-untyped-def]
    monkeypatch,
    tmp_path: Path,
) -> None:
    """Proposal-less kernel edits keep DB-only semantics, but completed
    source work must be marked stale from scout."""
    from datetime import datetime, timezone

    monkeypatch.setenv("AUTOESSAY_AUTH_BYPASS", "1")
    run_dir = tmp_path / "run_proposal_less_done"
    run_dir.mkdir()
    initial_kernel = {"kernel_schema_version": 1, "observed_puzzle": "old"}
    with app_session() as session:
        seed_project(session)
        run = Run(
            id="run_proposal_less_done",
            project_id="proj_test",
            domain_version="0.1.0",
            run_dir=str(run_dir),
            state="USER_SEARCH_REVIEW",
            baseline_hash="x",
            paper_mode="case_analysis",
            research_kernel_json=initial_kernel,
            proposal_version=0,
        )
        session.add(run)
        session.flush()
        branch = ensure_main_branch(session, run)
        now = datetime.now(timezone.utc)
        session.add(
            PhaseVersion(
                id="pv_proposal_less_scout_v1",
                run_id=run.id,
                phase="scout",
                version_no=1,
                source="agent",
                status="done",
                artifacts_dir="phases/scout/v1",
                created_on_branch_id=branch.id,
                created_at=now,
                completed_at=now,
            ),
        )
        session.add(
            RunHead(
                run_id=run.id,
                branch_id=branch.id,
                phase="scout",
                version_id="pv_proposal_less_scout_v1",
            ),
        )
        session.commit()

    base_hash = compute_kernel_hash("case_analysis", initial_kernel)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.put(
            "/api/runs/run_proposal_less_done/research_kernel",
            json=_kernel_edit_body(
                "case_analysis",
                {"kernel_schema_version": 1, "observed_puzzle": "edited"},
                0,
                base_hash,
            ),
        )

    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["proposal_version"] == 0
    assert payload["stale_from_phase"] == "scout"
    assert (run_dir / "proposal").exists() is False
    with app_session() as session:
        run = session.get(Run, "run_proposal_less_done")
        assert run is not None
        assert run.proposal_version == 0
        assert run.research_kernel_json["observed_puzzle"] == "edited"
        assert get_branch_stale(session, run) == "scout"


@pytest.mark.asyncio
async def test_kernel_edit_rejects_unknown_mode(
    app_session,  # type: ignore[no-untyped-def]
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("AUTOESSAY_AUTH_BYPASS", "1")
    run_dir = tmp_path / "run_x"
    run_dir.mkdir()
    with app_session() as session:
        seed_project(session)
        session.add(
            Run(
                id="run_unknown_mode",
                project_id="proj_test",
                domain_version="0.1.0",
                run_dir=str(run_dir),
                state="DOMAIN_LOADED",
                baseline_hash="x",
                paper_mode="case_analysis",
                research_kernel_json={"kernel_schema_version": 1},
            ),
        )
        session.commit()

    base_hash = compute_kernel_hash("case_analysis", {"kernel_schema_version": 1})
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.put(
            "/api/runs/run_unknown_mode/research_kernel",
            json=_kernel_edit_body(
                "not_a_real_mode",
                {"kernel_schema_version": 1},
                0,
                base_hash,
            ),
        )
    assert resp.status_code == 400
    assert "unknown" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_kernel_edit_rejects_developer_preview_without_ack(
    app_session,  # type: ignore[no-untyped-def]
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("AUTOESSAY_AUTH_BYPASS", "1")
    run_dir = tmp_path / "run_y"
    run_dir.mkdir()
    with app_session() as session:
        seed_project(session)
        session.add(
            Run(
                id="run_preview_unack",
                project_id="proj_test",
                domain_version="0.1.0",
                run_dir=str(run_dir),
                state="DOMAIN_LOADED",
                baseline_hash="x",
                paper_mode="case_analysis",
                research_kernel_json={"kernel_schema_version": 1},
            ),
        )
        session.commit()

    base_hash = compute_kernel_hash("case_analysis", {"kernel_schema_version": 1})
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.put(
            "/api/runs/run_preview_unack/research_kernel",
            json=_kernel_edit_body(
                "empirical",
                {"kernel_schema_version": 1},
                0,
                base_hash,
                accept_developer_preview=False,
            ),
        )
    assert resp.status_code == 400
    assert "developer_preview" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_kernel_edit_accepts_developer_preview_with_ack(
    app_session,  # type: ignore[no-untyped-def]
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("AUTOESSAY_AUTH_BYPASS", "1")
    run_dir = tmp_path / "run_z"
    run_dir.mkdir()
    with app_session() as session:
        seed_project(session)
        session.add(
            Run(
                id="run_preview_ack",
                project_id="proj_test",
                domain_version="0.1.0",
                run_dir=str(run_dir),
                state="DOMAIN_LOADED",
                baseline_hash="x",
                paper_mode="case_analysis",
                research_kernel_json={"kernel_schema_version": 1},
            ),
        )
        session.commit()

    base_hash = compute_kernel_hash("case_analysis", {"kernel_schema_version": 1})
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.put(
            "/api/runs/run_preview_ack/research_kernel",
            json=_kernel_edit_body(
                "empirical",
                {"kernel_schema_version": 1},
                0,
                base_hash,
                accept_developer_preview=True,
            ),
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["paper_mode"] == "empirical"


@pytest.mark.asyncio
async def test_kernel_edit_rejects_proposal_version_mismatch(
    app_session,  # type: ignore[no-untyped-def]
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("AUTOESSAY_AUTH_BYPASS", "1")
    run_dir = tmp_path / "run_v"
    run_dir.mkdir()
    with app_session() as session:
        seed_project(session)
        session.add(
            Run(
                id="run_pv_mismatch",
                project_id="proj_test",
                domain_version="0.1.0",
                run_dir=str(run_dir),
                state="DOMAIN_LOADED",
                baseline_hash="x",
                paper_mode="case_analysis",
                research_kernel_json={"kernel_schema_version": 1},
                proposal_version=3,
            ),
        )
        session.commit()

    base_hash = compute_kernel_hash("case_analysis", {"kernel_schema_version": 1})
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.put(
            "/api/runs/run_pv_mismatch/research_kernel",
            json=_kernel_edit_body(
                "case_analysis",
                {"kernel_schema_version": 1, "x": 1},
                999,  # stale
                base_hash,
            ),
        )
    assert resp.status_code == 409
    assert "base_proposal_version" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_kernel_edit_rejects_kernel_hash_mismatch(
    app_session,  # type: ignore[no-untyped-def]
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("AUTOESSAY_AUTH_BYPASS", "1")
    run_dir = tmp_path / "run_h"
    run_dir.mkdir()
    with app_session() as session:
        seed_project(session)
        session.add(
            Run(
                id="run_hash_mismatch",
                project_id="proj_test",
                domain_version="0.1.0",
                run_dir=str(run_dir),
                state="DOMAIN_LOADED",
                baseline_hash="x",
                paper_mode="case_analysis",
                research_kernel_json={"kernel_schema_version": 1},
            ),
        )
        session.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.put(
            "/api/runs/run_hash_mismatch/research_kernel",
            json=_kernel_edit_body(
                "case_analysis",
                {"kernel_schema_version": 1, "x": 1},
                0,
                "stale-hash-from-yesterday",
            ),
        )
    assert resp.status_code == 409
    assert "base_kernel_hash" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_kernel_edit_rejects_when_running(
    app_session,  # type: ignore[no-untyped-def]
    monkeypatch,
    tmp_path: Path,
) -> None:
    """Run currently in *_RUNNING state → 409."""
    monkeypatch.setenv("AUTOESSAY_AUTH_BYPASS", "1")
    run_dir = tmp_path / "run_r"
    run_dir.mkdir()
    with app_session() as session:
        seed_project(session)
        session.add(
            Run(
                id="run_running",
                project_id="proj_test",
                domain_version="0.1.0",
                run_dir=str(run_dir),
                state="SCOUT_RUNNING",
                baseline_hash="x",
                paper_mode="case_analysis",
                research_kernel_json={"kernel_schema_version": 1},
            ),
        )
        session.commit()

    base_hash = compute_kernel_hash("case_analysis", {"kernel_schema_version": 1})
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.put(
            "/api/runs/run_running/research_kernel",
            json=_kernel_edit_body(
                "case_analysis",
                {"kernel_schema_version": 1, "x": 1},
                0,
                base_hash,
            ),
        )
    assert resp.status_code == 409
    assert "another phase" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_kernel_edit_no_op_when_unchanged(
    app_session,  # type: ignore[no-untyped-def]
    monkeypatch,
    tmp_path: Path,
) -> None:
    """Submitting identical kernel + mode is a no-op (200 + same
    state)."""
    monkeypatch.setenv("AUTOESSAY_AUTH_BYPASS", "1")
    run_dir = tmp_path / "run_no_op"
    run_dir.mkdir()
    initial_kernel = {"kernel_schema_version": 1, "observed_puzzle": "stable"}
    with app_session() as session:
        seed_project(session)
        session.add(
            Run(
                id="run_noop",
                project_id="proj_test",
                domain_version="0.1.0",
                run_dir=str(run_dir),
                state="DOMAIN_LOADED",
                baseline_hash="x",
                paper_mode="case_analysis",
                research_kernel_json=initial_kernel,
            ),
        )
        session.commit()

    base_hash = compute_kernel_hash("case_analysis", initial_kernel)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.put(
            "/api/runs/run_noop/research_kernel",
            json=_kernel_edit_body(
                "case_analysis",
                initial_kernel,
                0,
                base_hash,
            ),
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["research_kernel_hash"] == base_hash


@pytest.mark.asyncio
async def test_kernel_edit_rejects_mode_change_after_proposal(
    app_session,  # type: ignore[no-untyped-def]
    monkeypatch,
    tmp_path: Path,
) -> None:
    """Codex round-2 amendment 1: paper_mode immutable once
    proposal_version >= 1. A curl/SDK caller trying to flip
    modes after proposal exists must get 400."""
    monkeypatch.setenv("AUTOESSAY_AUTH_BYPASS", "1")
    run_dir = tmp_path / "run_modechange"
    run_dir.mkdir()
    initial_kernel = {"kernel_schema_version": 1}
    with app_session() as session:
        seed_project(session)
        session.add(
            Run(
                id="run_modechange",
                project_id="proj_test",
                domain_version="0.1.0",
                run_dir=str(run_dir),
                state="USER_PROPOSAL_REVIEW",
                baseline_hash="x",
                paper_mode="empirical",
                research_kernel_json=initial_kernel,
                proposal_version=1,  # proposal exists
            ),
        )
        session.commit()

    base_hash = compute_kernel_hash("empirical", initial_kernel)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.put(
            "/api/runs/run_modechange/research_kernel",
            json=_kernel_edit_body(
                "case_analysis",  # different mode!
                {"kernel_schema_version": 1, "x": 1},
                1,
                base_hash,
                accept_developer_preview=True,
            ),
        )
    assert resp.status_code == 400, resp.text
    assert "paper_mode cannot be changed" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_kernel_edit_preserves_existing_preview_mode_without_ack(
    app_session,  # type: ignore[no-untyped-def]
    monkeypatch,
    tmp_path: Path,
) -> None:
    """Codex round-2 amendment 2: preview ack required only on
    mode TRANSITION into a preview mode, not on preservation.
    An existing empirical run can edit the kernel without
    re-acking developer_preview."""
    monkeypatch.setenv("AUTOESSAY_AUTH_BYPASS", "1")
    run_dir = tmp_path / "run_preserveprev"
    run_dir.mkdir()
    initial_kernel = {"kernel_schema_version": 1}
    with app_session() as session:
        seed_project(session)
        session.add(
            Run(
                id="run_preserveprev",
                project_id="proj_test",
                domain_version="0.1.0",
                run_dir=str(run_dir),
                state="DOMAIN_LOADED",
                baseline_hash="x",
                paper_mode="empirical",
                research_kernel_json=initial_kernel,
            ),
        )
        session.commit()

    base_hash = compute_kernel_hash("empirical", initial_kernel)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.put(
            "/api/runs/run_preserveprev/research_kernel",
            json=_kernel_edit_body(
                "empirical",  # same mode
                {"kernel_schema_version": 1, "observed_puzzle": "edit"},
                0,
                base_hash,
                accept_developer_preview=False,  # NO ack on preserve
            ),
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["paper_mode"] == "empirical"
