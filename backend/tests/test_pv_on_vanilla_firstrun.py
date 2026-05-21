"""Tests for PR-A4.1b: vanilla first runs through start_* now
create phase_versions + run_heads + lineage rows.

Before PR-A4.1b, the start_* endpoints called the agent runner
directly. Only the /rerun_phase endpoint wrapped runs in
``run_with_versioning``. As a result, vanilla first runs left the
``phase_versions`` table empty for that (run, phase), and the
phase-history modal showed no version chips.

PR-A4.1b inserts ``maybe_run_with_versioning`` inside each
agent's ``run_<phase>`` entry function, so both code paths
(start_* sync, RQ async, /rerun_phase explicit-wrap) end up
creating pv rows. ``maybe_*`` is a no-op when /rerun_phase
already wrapped — it detects an in-flight pv with status='running'
and runs inline instead of double-wrapping.
"""

from __future__ import annotations

from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from autoessay.config import get_settings
from autoessay.main import app
from autoessay.models import Branch, PhaseVersion, PhaseVersionInput, RunHead


async def _create_run_and_walk_to_proposal_review(
    client: AsyncClient,
) -> str:
    project_resp = await client.post(
        "/api/projects",
        json={"title": "PV vanilla first-run test"},
    )
    assert project_resp.status_code == 201
    run_resp = await client.post(
        f"/api/projects/{project_resp.json()['id']}/runs",
    )
    assert run_resp.status_code == 201
    run_id = run_resp.json()["id"]
    await client.post(
        f"/api/runs/{run_id}/transitions",
        json={"to_state": "DOMAIN_LOADED", "reason": "test"},
    )
    await client.post(
        f"/api/runs/{run_id}/proposal",
        json={},
    )
    return run_id


async def _approve_skim_candidates(client: AsyncClient, run_id: str) -> None:
    sources_resp = await client.get(f"/api/runs/{run_id}/sources")
    assert sources_resp.status_code == 200, sources_resp.text
    source_ids = [
        str(row["source_id"])
        for row in sources_resp.json()["skim_candidates"]
        if isinstance(row, dict) and row.get("source_id")
    ]
    assert source_ids
    checkpoint_resp = await client.post(
        f"/api/runs/{run_id}/checkpoints/USER_SEARCH_REVIEW",
        json={
            "status": "ACCEPTED",
            "decision_payload": {
                "source_ids": source_ids,
                "approved_source_ids": source_ids,
                "review_scope": "search_review",
            },
        },
    )
    assert checkpoint_resp.status_code == 201, checkpoint_resp.text


async def test_scout_first_run_creates_pv_row(
    app_session,  # type: ignore[no-untyped-def]
    monkeypatch,
) -> None:
    """``start_scout`` from a vanilla USER_PROPOSAL_REVIEW state
    creates a v1 phase_versions row + run_head + main branch."""
    monkeypatch.setenv("AUTOESSAY_SYNC_WORKER", "1")
    monkeypatch.setenv("AUTOESSAY_PROPOSAL_STUB", "1")
    monkeypatch.setenv("AUTOESSAY_SCOUT_STUB", "1")
    get_settings.cache_clear()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        run_id = await _create_run_and_walk_to_proposal_review(client)
        scout_resp = await client.post(f"/api/runs/{run_id}/scout", json={})

    assert scout_resp.status_code == 202

    with app_session() as session:
        # Scout pv row exists with v1 + agent + done.
        scout_pvs = session.scalars(
            select(PhaseVersion)
            .where(PhaseVersion.run_id == run_id)
            .where(PhaseVersion.phase == "scout"),
        ).all()
        assert len(scout_pvs) == 1
        assert scout_pvs[0].version_no == 1
        assert scout_pvs[0].source == "agent"
        assert scout_pvs[0].status == "done"

        # main branch was created, RunHead points at the new pv.
        branch = session.scalars(
            select(Branch).where(Branch.run_id == run_id),
        ).one()
        assert branch.name == "main"
        head = session.scalar(
            select(RunHead.version_id)
            .where(RunHead.run_id == run_id)
            .where(RunHead.phase == "scout"),
        )
        assert head == scout_pvs[0].id


async def test_curator_first_run_records_lineage(
    app_session,  # type: ignore[no-untyped-def]
    monkeypatch,
) -> None:
    """``start_curator`` after start_scout records that curator's
    pv was based on scout's v1 — so future activate-version
    cascades and phase-history rendering can use the lineage."""
    for stub in ("PROPOSAL", "SCOUT", "CURATOR"):
        monkeypatch.setenv(f"AUTOESSAY_{stub}_STUB", "1")
    monkeypatch.setenv("AUTOESSAY_SYNC_WORKER", "1")
    get_settings.cache_clear()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        run_id = await _create_run_and_walk_to_proposal_review(client)
        await client.post(f"/api/runs/{run_id}/scout", json={})
        await _approve_skim_candidates(client, run_id)
        await client.post(f"/api/runs/{run_id}/curator", json={})

    with app_session() as session:
        curator_pv = session.scalars(
            select(PhaseVersion)
            .where(PhaseVersion.run_id == run_id)
            .where(PhaseVersion.phase == "curator"),
        ).one()
        scout_pv = session.scalars(
            select(PhaseVersion)
            .where(PhaseVersion.run_id == run_id)
            .where(PhaseVersion.phase == "scout"),
        ).one()

        # Lineage row records curator's upstream as scout's v1 pv.
        lineage = session.scalars(
            select(PhaseVersionInput).where(
                PhaseVersionInput.phase_version_id == curator_pv.id,
            ),
        ).all()
        scout_upstream = [row for row in lineage if row.upstream_phase == "scout"]
        assert len(scout_upstream) == 1
        assert scout_upstream[0].upstream_pv_id == scout_pv.id


async def test_no_double_wrap_on_rerun(
    app_session,  # type: ignore[no-untyped-def]
    monkeypatch,
) -> None:
    """A rerun via /rerun_phase wraps in ``run_with_versioning``
    explicitly. The agent's own ``maybe_run_with_versioning``
    must detect the in-flight pv and run inline — otherwise we'd
    create two pvs per rerun.
    """
    for stub in ("PROPOSAL", "SCOUT"):
        monkeypatch.setenv(f"AUTOESSAY_{stub}_STUB", "1")
    monkeypatch.setenv("AUTOESSAY_SYNC_WORKER", "1")
    get_settings.cache_clear()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        run_id = await _create_run_and_walk_to_proposal_review(client)
        await client.post(f"/api/runs/{run_id}/scout", json={})  # → v1
        await client.post(
            f"/api/runs/{run_id}/phases/scout/rerun",
            json={},
        )  # → v2

    with app_session() as session:
        scout_pvs = session.scalars(
            select(PhaseVersion)
            .where(PhaseVersion.run_id == run_id)
            .where(PhaseVersion.phase == "scout")
            .order_by(PhaseVersion.version_no.asc()),
        ).all()
        # Exactly two pvs: the original v1 from start_scout, and
        # the v2 from /rerun. NOT three (which would mean the
        # agent's wrap re-fired inside the rerun's wrap).
        assert [pv.version_no for pv in scout_pvs] == [1, 2]
