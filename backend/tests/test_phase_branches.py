"""Tests for branch CRUD + branch-aware rerun (codex-AGREEd #2 stage 2.C).

Covers:
- GET /branches lists ``main`` after run creation; backfill works.
- POST /branches creates a fork, copies upstream heads, leaves
  forked phase + downstream empty.
- POST /branches with a non-visible base_pv 409s.
- POST /branches/active switches the run's workspace pointer.
- DELETE /branches refuses main, 204 for others; active resets.
- Two branches hold independent stale_from_phase markers.
- A rerun on branch B does NOT touch branch A's run_heads (the
  cross-branch leak codex round-1 round flagged).
- phase_version_inputs records the upstream lineage at begin time.
"""

from __future__ import annotations

from pathlib import Path

from httpx import ASGITransport, AsyncClient

from autoessay.main import app
from autoessay.models import (
    Branch,
    Domain,
    PhaseVersion,
    PhaseVersionInput,
    Project,
    Run,
    RunHead,
    User,
)
from autoessay.phase_version import run_with_versioning
from autoessay.run_writer import create_run_directory


def _seed_upstream_phase_artifacts(run_dir: Path) -> None:
    """See ``test_phase_rerun._seed_upstream_phase_artifacts``."""
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
    run_id: str = "run_br_test",
    *,
    state: str = "USER_DEEP_DIVE_REVIEW",
) -> Path:
    run_dir = create_run_directory(
        tmp_path / "runs",
        run_id,
        "proj_br",
        state=state,
        domain_id="financial_history",
    )
    _seed_upstream_phase_artifacts(run_dir)
    with app_session() as session:
        if session.get(User, "single-user") is None:
            session.add(User(id="single-user", display_name="Single User"))
        if session.get(Domain, "financial_history") is None:
            session.add(
                Domain(
                    id="financial_history",
                    display_name="Financial History",
                    version="0.1.0",
                    enabled=True,
                ),
            )
        session.flush()
        if session.get(Project, "proj_br") is None:
            session.add(
                Project(
                    id="proj_br",
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
                project_id="proj_br",
                domain_version="0.1.0",
                run_dir=str(run_dir),
                state=state,
                baseline_hash="x",
            ),
        )
        session.flush()
        # Test fixtures bypass the run-creation endpoint, so the main
        # branch must be created explicitly.
        from autoessay.branches import ensure_main_branch

        run = session.get(Run, run_id)
        assert run is not None
        ensure_main_branch(session, run)
        session.commit()
    return run_dir


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


async def test_list_branches_returns_main_after_run_seed(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path,
) -> None:
    _seed(app_session, tmp_path)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/runs/run_br_test/branches")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["branches"]) == 1
    assert body["branches"][0]["name"] == "main"
    assert body["branches"][0]["is_active"] is True
    assert body["active_branch_id"] == body["branches"][0]["id"]


async def test_create_branch_copies_upstream_heads(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path,
) -> None:
    """Forking from synthesizer-v1 should copy any UPSTREAM heads
    (curator, scout, etc.) but leave synthesizer + downstream empty."""
    run_dir = _seed(app_session, tmp_path, run_id="run_br_fork")
    syn_legacy = run_dir / "synthesis" / "claims.jsonl"
    with app_session() as session:
        run = session.get(Run, "run_br_fork")
        # Produce v1 of synthesizer on main branch.
        run_with_versioning(session, run, "synthesizer", lambda: _write(syn_legacy, "v1"))
        synth_v1 = session.scalar(
            __import__("sqlalchemy")
            .select(PhaseVersion)
            .where(PhaseVersion.run_id == "run_br_fork")
            .where(PhaseVersion.phase == "synthesizer"),
        )
        assert synth_v1 is not None
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/runs/run_br_fork/branches",
            json={"name": "alt", "base_pv_id": synth_v1.id},
        )
    assert resp.status_code == 201, resp.text
    new_branch = resp.json()
    assert new_branch["name"] == "alt"
    assert new_branch["forked_phase"] == "synthesizer"
    # Stage 2.C round-3 fix: the new branch's head for the forked
    # phase is base_pv itself (so the rerun endpoint can resolve "no
    # output yet"). Subsequent reruns produce divergent versions.
    with app_session() as session:
        head_pv_id = session.scalar(
            __import__("sqlalchemy")
            .select(RunHead.version_id)
            .where(RunHead.branch_id == new_branch["id"])
            .where(RunHead.phase == "synthesizer"),
        )
        assert head_pv_id == synth_v1.id


async def test_two_branches_have_independent_heads(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path,
) -> None:
    """A rerun on branch B must NOT touch branch A's run_heads."""
    run_dir = _seed(app_session, tmp_path, run_id="run_br_iso")
    syn = run_dir / "synthesis" / "claims.jsonl"
    with app_session() as session:
        run = session.get(Run, "run_br_iso")
        run_with_versioning(session, run, "synthesizer", lambda: _write(syn, "main-v1"))
        v1 = session.scalar(
            __import__("sqlalchemy")
            .select(PhaseVersion)
            .where(PhaseVersion.phase == "synthesizer"),
        )
        assert v1 is not None
        v1_id = v1.id
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Fork from v1 onto branch alt, then switch to it.
        fork_resp = await client.post(
            "/api/runs/run_br_iso/branches",
            json={"name": "alt", "base_pv_id": v1_id},
        )
        alt_id = fork_resp.json()["id"]
        await client.post(
            "/api/runs/run_br_iso/branches/active",
            json={"branch_id": alt_id},
        )
    # Now run synthesizer again; branch B (alt) should produce v2 but
    # branch A (main) should still see v1 as its head.
    with app_session() as session:
        run = session.get(Run, "run_br_iso")
        run_with_versioning(session, run, "synthesizer", lambda: _write(syn, "alt-v2"))
        main_id = "br_main_run_br_iso"
        main_head = session.scalar(
            __import__("sqlalchemy")
            .select(RunHead.version_id)
            .where(RunHead.branch_id == main_id)
            .where(RunHead.phase == "synthesizer"),
        )
        alt_head = session.scalar(
            __import__("sqlalchemy")
            .select(RunHead.version_id)
            .where(RunHead.branch_id == alt_id)
            .where(RunHead.phase == "synthesizer"),
        )
        assert main_head == v1_id, "main branch's head must not move"
        assert alt_head is not None
        assert alt_head != v1_id, "alt branch must produce a new head"


async def test_phase_version_inputs_records_upstream_lineage(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path,
) -> None:
    """begin_phase_version must record the (run, branch)'s upstream
    heads explicitly (codex round-1 non-negotiable)."""
    run_dir = _seed(app_session, tmp_path, run_id="run_br_lineage")
    sources = run_dir / "sources" / "shortlist.json"
    syn = run_dir / "synthesis" / "claims.jsonl"
    with app_session() as session:
        run = session.get(Run, "run_br_lineage")
        run_with_versioning(session, run, "curator", lambda: _write(sources, "[]"))
        curator_pv = session.scalar(
            __import__("sqlalchemy").select(PhaseVersion).where(PhaseVersion.phase == "curator"),
        )
        assert curator_pv is not None
        run_with_versioning(session, run, "synthesizer", lambda: _write(syn, "{}"))
        synth_pv = session.scalar(
            __import__("sqlalchemy")
            .select(PhaseVersion)
            .where(PhaseVersion.phase == "synthesizer"),
        )
        assert synth_pv is not None
        inputs = session.scalars(
            __import__("sqlalchemy")
            .select(PhaseVersionInput)
            .where(PhaseVersionInput.phase_version_id == synth_pv.id),
        ).all()
        upstream_map = {row.upstream_phase: row.upstream_pv_id for row in inputs}
        assert upstream_map.get("curator") == curator_pv.id


async def test_delete_branch_refuses_main(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path,
) -> None:
    _seed(app_session, tmp_path, run_id="run_br_del")
    main_id = "br_main_run_br_del"
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.delete(f"/api/runs/run_br_del/branches/{main_id}")
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "main_branch_protected"


async def test_delete_branch_resets_active_to_main(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path,
) -> None:
    """Deleting the active branch must drop active_branch_id back to
    main so the workspace pointer doesn't dangle."""
    run_dir = _seed(app_session, tmp_path, run_id="run_br_del2")
    syn = run_dir / "synthesis" / "claims.jsonl"
    with app_session() as session:
        run = session.get(Run, "run_br_del2")
        run_with_versioning(session, run, "synthesizer", lambda: _write(syn, "v1"))
        v1 = session.scalar(
            __import__("sqlalchemy")
            .select(PhaseVersion)
            .where(PhaseVersion.phase == "synthesizer"),
        )
        assert v1 is not None
        v1_id = v1.id
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        fork = await client.post(
            "/api/runs/run_br_del2/branches",
            json={"name": "alt", "base_pv_id": v1_id},
        )
        alt_id = fork.json()["id"]
        await client.post(
            "/api/runs/run_br_del2/branches/active",
            json={"branch_id": alt_id},
        )
        del_resp = await client.delete(f"/api/runs/run_br_del2/branches/{alt_id}")
    assert del_resp.status_code == 204
    main_id = "br_main_run_br_del2"
    with app_session() as session:
        run = session.get(Run, "run_br_del2")
        assert run.active_branch_id == main_id
        b = session.get(Branch, alt_id)
        assert b is not None
        assert b.deleted_at is not None


async def test_create_branch_with_unknown_base_pv_returns_404(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path,
) -> None:
    _seed(app_session, tmp_path, run_id="run_br_404")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/runs/run_br_404/branches",
            json={"name": "alt", "base_pv_id": "pv_does_not_exist"},
        )
    assert resp.status_code == 404


async def test_two_branches_hold_independent_stale_markers(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path,
) -> None:
    """Branch A's stale_from_phase must be independent of branch B's."""
    _seed(app_session, tmp_path, run_id="run_br_stale")
    main_id = "br_main_run_br_stale"
    with app_session() as session:
        run = session.get(Run, "run_br_stale")
        # Make a fake second branch directly so we don't depend on
        # a fork base_pv being available for this isolated test.
        from autoessay.branches import set_branch_stale

        session.add(Branch(id="br_alt_stale", run_id="run_br_stale", name="alt"))
        session.commit()
        run = session.get(Run, "run_br_stale")
        set_branch_stale(session, run, "synthesizer", branch_id=main_id)
        set_branch_stale(session, run, "ideator", branch_id="br_alt_stale")
        session.commit()
    with app_session() as session:
        a = session.get(Branch, main_id)
        b = session.get(Branch, "br_alt_stale")
        assert a is not None and b is not None
        assert a.stale_from_phase == "synthesizer"
        assert b.stale_from_phase == "ideator"


async def test_switch_branch_materializes_legacy_files(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path,
) -> None:
    """Switching branches must restore the target branch's heads to
    disk, not leave the previous branch's files behind (codex round-2
    #2 stage 2.C non-negotiable)."""
    run_dir = _seed(app_session, tmp_path, run_id="run_br_swap")
    legacy = run_dir / "synthesis" / "claims.jsonl"
    with app_session() as session:
        run = session.get(Run, "run_br_swap")
        run_with_versioning(session, run, "synthesizer", lambda: _write(legacy, "main-content"))
        v1 = session.scalar(
            __import__("sqlalchemy").select(PhaseVersion).where(PhaseVersion.phase == "synthesizer")
        )
        assert v1 is not None
        v1_id = v1.id
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        fork = await client.post(
            "/api/runs/run_br_swap/branches",
            json={"name": "alt", "base_pv_id": v1_id},
        )
        alt_id = fork.json()["id"]
        await client.post(
            "/api/runs/run_br_swap/branches/active",
            json={"branch_id": alt_id},
        )
    with app_session() as session:
        run = session.get(Run, "run_br_swap")
        run_with_versioning(session, run, "synthesizer", lambda: _write(legacy, "alt-content"))
    assert legacy.read_text() == "alt-content"
    main_id = "br_main_run_br_swap"
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await client.post(
            "/api/runs/run_br_swap/branches/active",
            json={"branch_id": main_id},
        )
    # Switching back to main MUST restore main's content; without
    # materialize-on-switch the file would still hold "alt-content".
    assert legacy.read_text() == "main-content"


async def test_activate_blocks_pv_from_other_branch(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path,
) -> None:
    """A pv created on branch B must NOT be activatable onto branch
    A — that would be cherry-picking, which codex round-1 ruled out."""
    run_dir = _seed(app_session, tmp_path, run_id="run_br_visibility")
    legacy = run_dir / "synthesis" / "claims.jsonl"
    with app_session() as session:
        run = session.get(Run, "run_br_visibility")
        run_with_versioning(session, run, "synthesizer", lambda: _write(legacy, "main-v1"))
        v1 = session.scalar(
            __import__("sqlalchemy").select(PhaseVersion).where(PhaseVersion.phase == "synthesizer")
        )
        assert v1 is not None
        v1_id = v1.id
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        fork = await client.post(
            "/api/runs/run_br_visibility/branches",
            json={"name": "alt", "base_pv_id": v1_id},
        )
        alt_id = fork.json()["id"]
        await client.post(
            "/api/runs/run_br_visibility/branches/active",
            json={"branch_id": alt_id},
        )
    with app_session() as session:
        run = session.get(Run, "run_br_visibility")
        run_with_versioning(session, run, "synthesizer", lambda: _write(legacy, "alt-v2"))
        alt_v2 = session.scalar(
            __import__("sqlalchemy")
            .select(PhaseVersion)
            .where(PhaseVersion.phase == "synthesizer")
            .where(PhaseVersion.created_on_branch_id == alt_id)
        )
        assert alt_v2 is not None
        alt_v2_id = alt_v2.id
    # Switch back to main, then try to activate alt's v2 onto main.
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        main_id = "br_main_run_br_visibility"
        await client.post(
            "/api/runs/run_br_visibility/branches/active",
            json={"branch_id": main_id},
        )
        resp = await client.post(
            f"/api/runs/run_br_visibility/phases/synthesizer/versions/{alt_v2_id}/activate"
        )
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "pv_not_visible"


async def test_fork_uses_pv_recorded_lineage_not_current_heads(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path,
) -> None:
    """Forking from synthesizer-v1 must inherit v1's recorded
    upstream (curator-v1), NOT the base branch's CURRENT curator
    head (curator-v2 if it was rerun later). Otherwise the fork's
    metadata says 'forked from v1' but its data points elsewhere."""
    run_dir = _seed(app_session, tmp_path, run_id="run_br_lineage_fork")
    sources = run_dir / "sources" / "shortlist.json"
    syn = run_dir / "synthesis" / "claims.jsonl"
    with app_session() as session:
        run = session.get(Run, "run_br_lineage_fork")
        run_with_versioning(session, run, "curator", lambda: _write(sources, "[1]"))
        cur_v1 = session.scalar(
            __import__("sqlalchemy").select(PhaseVersion).where(PhaseVersion.phase == "curator")
        )
        assert cur_v1 is not None
        cur_v1_id = cur_v1.id
        run_with_versioning(session, run, "synthesizer", lambda: _write(syn, "syn-v1"))
        syn_v1 = session.scalar(
            __import__("sqlalchemy").select(PhaseVersion).where(PhaseVersion.phase == "synthesizer")
        )
        assert syn_v1 is not None
        syn_v1_id = syn_v1.id
        # Now rerun curator on main. This produces curator-v2; main's
        # head moves but synthesizer-v1's recorded upstream still
        # points at curator-v1.
        run_with_versioning(session, run, "curator", lambda: _write(sources, "[2]"))
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        fork = await client.post(
            "/api/runs/run_br_lineage_fork/branches",
            json={"name": "alt", "base_pv_id": syn_v1_id},
        )
    assert fork.status_code == 201, fork.text
    new_branch_id = fork.json()["id"]
    with app_session() as session:
        head_curator_on_alt = session.scalar(
            __import__("sqlalchemy")
            .select(RunHead.version_id)
            .where(RunHead.branch_id == new_branch_id)
            .where(RunHead.phase == "curator")
        )
        # Critical: alt branch's curator head must be cur_v1, the
        # version recorded as syn_v1's upstream — NOT cur_v2 (main's
        # current curator head).
        assert head_curator_on_alt == cur_v1_id


async def test_stale_marker_only_inspects_branch_heads(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path,
) -> None:
    """Branch A has no synthesizer head; main has both syn + ideator
    heads. After a curator rerun on A, A's stale_from_phase must NOT
    advance to synthesizer (which is "completed" only via main's
    files on disk)."""
    run_dir = _seed(app_session, tmp_path, run_id="run_br_stale_iso")
    sources = run_dir / "sources" / "shortlist.json"
    syn = run_dir / "synthesis" / "claims.jsonl"
    with app_session() as session:
        run = session.get(Run, "run_br_stale_iso")
        run_with_versioning(session, run, "curator", lambda: _write(sources, "[]"))
        run_with_versioning(session, run, "synthesizer", lambda: _write(syn, "{}"))
        cur_v1 = session.scalar(
            __import__("sqlalchemy").select(PhaseVersion).where(PhaseVersion.phase == "curator")
        )
        assert cur_v1 is not None
    # Fork from curator-v1 onto branch alt; alt has curator head but
    # no synthesizer head.
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        fork = await client.post(
            "/api/runs/run_br_stale_iso/branches",
            json={"name": "alt", "base_pv_id": cur_v1.id},
        )
        alt_id = fork.json()["id"]
        await client.post(
            "/api/runs/run_br_stale_iso/branches/active",
            json={"branch_id": alt_id},
        )
    # Now rerun curator on alt. alt's stale_from_phase should NOT be
    # "synthesizer" — alt has no synthesizer head, even though main's
    # synthesis/claims.jsonl is on disk after materialization.
    with app_session() as session:
        run = session.get(Run, "run_br_stale_iso")
        run_with_versioning(session, run, "curator", lambda: _write(sources, "[2]"))
        from autoessay.branches import get_branch_stale

        assert get_branch_stale(session, run, branch_id=alt_id) is None


async def test_fork_then_rerun_via_api_succeeds(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path,
) -> None:
    """After forking, the rerun endpoint must accept the forked phase.
    Without setting base_pv as the new branch's initial head, the
    rerun would 409 'no output yet' (codex round-3 #2 stage 2.C)."""
    run_dir = _seed(app_session, tmp_path, run_id="run_br_rerun_after_fork")
    legacy = run_dir / "synthesis" / "claims.jsonl"
    with app_session() as session:
        run = session.get(Run, "run_br_rerun_after_fork")
        run_with_versioning(session, run, "synthesizer", lambda: _write(legacy, "main-v1"))
        v1 = session.scalar(
            __import__("sqlalchemy").select(PhaseVersion).where(PhaseVersion.phase == "synthesizer")
        )
        assert v1 is not None
        v1_id = v1.id
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        fork = await client.post(
            "/api/runs/run_br_rerun_after_fork/branches",
            json={"name": "alt", "base_pv_id": v1_id},
        )
        alt_id = fork.json()["id"]
        await client.post(
            "/api/runs/run_br_rerun_after_fork/branches/active",
            json={"branch_id": alt_id},
        )
        # Stub the runner so the test doesn't need real LLM inputs.
        from autoessay import main as main_mod

        def fake_synth(run_id, session=None, **kwargs):
            _write(legacy, "alt-v2")

        main_mod._PHASE_RUNNERS["synthesizer"] = fake_synth
        # Fork+switch purges owned files; re-seed the upstream artifact
        # synthesizer_ready expects. (Stage 3.E follow-up: rerun now
        # enforces phase_readiness preconditions.)
        _write(run_dir / "sources" / "shortlist.json", '[{"source_id":"x"}]')
        resp = await client.post("/api/runs/run_br_rerun_after_fork/phases/synthesizer/rerun")
    assert resp.status_code == 202, resp.text


async def test_list_versions_includes_inherited_head(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path,
) -> None:
    """Forking from synth-v1 sets alt's synth head to v1 (created on
    main). list_versions for alt must include v1 even though
    created_on_branch_id=main, otherwise active_version_id points
    nowhere (codex round-3 #2 stage 2.C)."""
    run_dir = _seed(app_session, tmp_path, run_id="run_br_list_inherit")
    legacy = run_dir / "synthesis" / "claims.jsonl"
    with app_session() as session:
        run = session.get(Run, "run_br_list_inherit")
        run_with_versioning(session, run, "synthesizer", lambda: _write(legacy, "main-v1"))
        v1 = session.scalar(
            __import__("sqlalchemy").select(PhaseVersion).where(PhaseVersion.phase == "synthesizer")
        )
        assert v1 is not None
        v1_id = v1.id
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        fork = await client.post(
            "/api/runs/run_br_list_inherit/branches",
            json={"name": "alt", "base_pv_id": v1_id},
        )
        alt_id = fork.json()["id"]
        await client.post(
            "/api/runs/run_br_list_inherit/branches/active",
            json={"branch_id": alt_id},
        )
        resp = await client.get("/api/runs/run_br_list_inherit/phases/synthesizer/versions")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["active_version_id"] == v1_id
    listed_ids = [entry["id"] for entry in body["versions"]]
    assert v1_id in listed_ids


async def test_delete_active_branch_materializes_main(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path,
) -> None:
    """Deleting the active branch must materialize main onto disk so
    the legacy bundle endpoints don't keep showing the deleted
    branch's content (codex round-3 #2 stage 2.C)."""
    run_dir = _seed(app_session, tmp_path, run_id="run_br_del_mat")
    legacy = run_dir / "synthesis" / "claims.jsonl"
    with app_session() as session:
        run = session.get(Run, "run_br_del_mat")
        run_with_versioning(session, run, "synthesizer", lambda: _write(legacy, "main-only"))
        v1 = session.scalar(
            __import__("sqlalchemy").select(PhaseVersion).where(PhaseVersion.phase == "synthesizer")
        )
        assert v1 is not None
        v1_id = v1.id
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        fork = await client.post(
            "/api/runs/run_br_del_mat/branches",
            json={"name": "alt", "base_pv_id": v1_id},
        )
        alt_id = fork.json()["id"]
        await client.post(
            "/api/runs/run_br_del_mat/branches/active",
            json={"branch_id": alt_id},
        )
    # Diverge alt's content on disk.
    with app_session() as session:
        run = session.get(Run, "run_br_del_mat")
        run_with_versioning(session, run, "synthesizer", lambda: _write(legacy, "alt-divergent"))
    # Delete alt while it's active.
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        del_resp = await client.delete(f"/api/runs/run_br_del_mat/branches/{alt_id}")
    assert del_resp.status_code == 204
    # Disk must now show main's content, not alt's.
    assert legacy.read_text() == "main-only"


async def test_fork_base_pv_must_be_done_status(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path,
) -> None:
    """Fork rejects 409 when base_pv has status != 'done' (codex
    round-4 #2 stage 2.C: a failed/cancelled pv has no restorable
    archive, so installing it as a branch head would create a
    non-restorable branch)."""
    run_dir = _seed(app_session, tmp_path, run_id="run_br_status_check")
    legacy = run_dir / "synthesis" / "claims.jsonl"
    with app_session() as session:
        run = session.get(Run, "run_br_status_check")
        # Force a failed pv on synthesizer.
        import pytest

        def boom() -> None:
            _write(legacy, "garbage")
            raise RuntimeError("simulated")

        with pytest.raises(RuntimeError):
            run_with_versioning(session, run, "synthesizer", boom)
        failed = session.scalar(
            __import__("sqlalchemy").select(PhaseVersion).where(PhaseVersion.status == "failed"),
        )
        assert failed is not None
        failed_id = failed.id
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/runs/run_br_status_check/branches",
            json={"name": "alt", "base_pv_id": failed_id},
        )
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "base_pv_not_done"


async def test_list_versions_reaches_back_through_parent_chain(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path,
) -> None:
    """Repro from codex round-4 #2 stage 2.C: main->v1, fork alt at
    v1, rerun synth on alt → v2. v1 is no longer alt's head and was
    created on main, but it's v2's parent and the fork point. The
    history modal must still list v1 (so the user can fork from it
    again or activate it)."""
    run_dir = _seed(app_session, tmp_path, run_id="run_br_chain")
    legacy = run_dir / "synthesis" / "claims.jsonl"
    with app_session() as session:
        run = session.get(Run, "run_br_chain")
        run_with_versioning(session, run, "synthesizer", lambda: _write(legacy, "main-v1"))
        v1 = session.scalar(
            __import__("sqlalchemy").select(PhaseVersion).where(PhaseVersion.phase == "synthesizer")
        )
        assert v1 is not None
        v1_id = v1.id
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        fork = await client.post(
            "/api/runs/run_br_chain/branches",
            json={"name": "alt", "base_pv_id": v1_id},
        )
        alt_id = fork.json()["id"]
        await client.post(
            "/api/runs/run_br_chain/branches/active",
            json={"branch_id": alt_id},
        )
    with app_session() as session:
        run = session.get(Run, "run_br_chain")
        run_with_versioning(session, run, "synthesizer", lambda: _write(legacy, "alt-v2"))
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/runs/run_br_chain/phases/synthesizer/versions")
    assert resp.status_code == 200, resp.text
    listed = [v["id"] for v in resp.json()["versions"]]
    assert v1_id in listed, "v1 must remain visible as ancestor of alt's v2"
    # And v2 should also be listed as the active head.
    assert resp.json()["active_version_id"] != v1_id
    assert resp.json()["active_version_id"] in listed


async def test_activate_ancestor_succeeds_after_branch_rerun(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path,
) -> None:
    """Same setup as test_list_versions_reaches_back_through_parent_chain
    but the user wants to ACTIVATE v1 on alt after producing v2.
    Activation must succeed because v1 is reachable via parent chain."""
    run_dir = _seed(app_session, tmp_path, run_id="run_br_act_anc")
    legacy = run_dir / "synthesis" / "claims.jsonl"
    with app_session() as session:
        run = session.get(Run, "run_br_act_anc")
        run_with_versioning(session, run, "synthesizer", lambda: _write(legacy, "main-v1"))
        v1 = session.scalar(
            __import__("sqlalchemy").select(PhaseVersion).where(PhaseVersion.phase == "synthesizer")
        )
        assert v1 is not None
        v1_id = v1.id
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        fork = await client.post(
            "/api/runs/run_br_act_anc/branches",
            json={"name": "alt", "base_pv_id": v1_id},
        )
        alt_id = fork.json()["id"]
        await client.post(
            "/api/runs/run_br_act_anc/branches/active",
            json={"branch_id": alt_id},
        )
    with app_session() as session:
        run = session.get(Run, "run_br_act_anc")
        run_with_versioning(session, run, "synthesizer", lambda: _write(legacy, "alt-v2"))
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            f"/api/runs/run_br_act_anc/phases/synthesizer/versions/{v1_id}/activate"
        )
    assert resp.status_code == 200, resp.text
    assert legacy.read_text() == "main-v1"


async def test_create_branch_rejected_during_running_state(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path,
) -> None:
    # Round-1 audit #19: branch creation must not race a running phase.
    run_dir = _seed(
        app_session,
        tmp_path,
        run_id="run_br_create_run_guard",
        state="USER_DEEP_DIVE_REVIEW",
    )
    legacy = run_dir / "synthesis" / "claims.jsonl"
    with app_session() as session:
        run = session.get(Run, "run_br_create_run_guard")
        run_with_versioning(session, run, "synthesizer", lambda: _write(legacy, "v1"))
        v1 = session.scalar(
            __import__("sqlalchemy").select(PhaseVersion).where(PhaseVersion.phase == "synthesizer")
        )
        assert v1 is not None
        # Now flip to RUNNING — simulating an in-progress phase.
        run.state = "DRAFTER_RUNNING"
        session.commit()
        v1_id = v1.id
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/runs/run_br_create_run_guard/branches",
            json={"name": "alt", "base_pv_id": v1_id},
        )
    assert resp.status_code == 409
    assert "currently running" in resp.json()["detail"]


async def test_switch_branch_rejected_during_running_state(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path,
) -> None:
    # Round-1 audit #18: branch switch materializes legacy paths;
    # mid-flight switch would race the running agent's writes.
    _seed(
        app_session,
        tmp_path,
        run_id="run_br_switch_run_guard",
        state="DRAFTER_RUNNING",
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Use any branch_id; the guard fires before validation.
        resp = await client.post(
            "/api/runs/run_br_switch_run_guard/branches/active",
            json={"branch_id": "main"},
        )
    assert resp.status_code == 409
    assert "currently running" in resp.json()["detail"]


async def test_delete_branch_rejected_during_running_state(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path,
) -> None:
    # Round-1 audit #19: branch delete falls back + remateriliazes —
    # same race window as switch.
    _seed(
        app_session,
        tmp_path,
        run_id="run_br_delete_run_guard",
        state="STYLIST_RUNNING",
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.delete(
            "/api/runs/run_br_delete_run_guard/branches/some-branch-id",
        )
    assert resp.status_code == 409
    assert "currently running" in resp.json()["detail"]
