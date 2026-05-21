"""Tests for the PUT /api/runs/{id}/phases/{phase}/edit endpoint
and the underlying ``apply_phase_user_edit`` helper.

Covers every codex amendment from the 2026-05-01 design review of
issue 1:

- amendment 1: branch-scoped stale (set_branch_stale, not Run.stale)
- amendment 2: gate on quiescent + has_completed_output, not on
  USER_*_REVIEW exact state
- amendment 3: optimistic concurrency via base_version_id
- amendment 4: archive layout reuses begin/commit_phase_version
- amendment 5/6: source='user_edit' is recorded and surfaced via
  PhaseVersionsResponse (covered by PR-A1's tests; we re-verify
  the path here for the user-edit invocation)
- amendment 7: drafter manuscript + claim_map must be paired
"""

from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from autoessay.main import app
from autoessay.models import PhaseVersion, Run, RunHead


async def _create_run(client: AsyncClient) -> str:
    project_response = await client.post(
        "/api/projects",
        json={
            "title": "Phase user edit test",
            "domain_id": "financial_history",
            "language": "en",
        },
    )
    assert project_response.status_code == 201
    run_response = await client.post(
        f"/api/projects/{project_response.json()['id']}/runs",
    )
    return run_response.json()["id"]


def _seed_phase_completed(
    app_session,  # type: ignore[no-untyped-def]
    run_id: str,
    *,
    phase: str,
    files: dict[str, str],
) -> str:
    """Plant on-disk artifacts for ``phase`` so has_completed_output
    returns True, plus a phase_version + run_head row pointing at it
    so the active branch has a real head to base optimistic
    concurrency on. Returns the seeded version id."""
    from autoessay.branches import ensure_main_branch

    with app_session() as session:
        run = session.scalar(select(Run).where(Run.id == run_id))
        assert run is not None
        if run.active_branch_id is None:
            ensure_main_branch(session, run)
            session.commit()
        branch_id = run.active_branch_id
        run_dir = Path(run.run_dir)
        for path, content in files.items():
            target = run_dir / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        pv = PhaseVersion(
            id=f"pv_seed_{phase}",
            run_id=run.id,
            phase=phase,
            version_no=1,
            parent_pv_id=None,
            status="done",
            artifacts_dir=f"phases/pv_seed_{phase}",
            input_snapshot_hash=None,
            prompt_hash=None,
            created_on_branch_id=branch_id,
            created_by=None,
            source="agent",
        )
        session.add(pv)
        session.add(
            RunHead(
                run_id=run.id,
                branch_id=branch_id,
                phase=phase,
                version_id=pv.id,
            ),
        )
        session.commit()
        return pv.id


async def test_synthesizer_user_edit_creates_user_edit_version(
    app_session,  # type: ignore[no-untyped-def]
) -> None:
    """Happy path: edit synthesizer artifacts at FAILED state-irrelevant
    points; new phase_version row records source=user_edit."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        run_id = await _create_run(client)
        seeded_pv = _seed_phase_completed(
            app_session,
            run_id,
            phase="synthesizer",
            files={
                "synthesis/claims.jsonl": '{"claim":"original"}\n',
                "synthesis/synthesizer_report.md": "# original report\n",
            },
        )

        response = await client.put(
            f"/api/runs/{run_id}/phases/synthesizer/edit",
            json={
                "base_version_id": seeded_pv,
                "files": {
                    "synthesis/claims.jsonl": '{"claim":"edited by user"}\n',
                    "synthesis/synthesizer_report.md": "# user-edited report\n",
                },
            },
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["source"] == "user_edit"
    assert body["version_no"] == 2

    with app_session() as session:
        pv = session.scalar(
            select(PhaseVersion).where(PhaseVersion.id == body["phase_version_id"]),
        )
        assert pv is not None
        assert pv.source == "user_edit"
        assert pv.status == "done"


async def test_drafter_edit_requires_manuscript_and_claim_map_together(
    app_session,  # type: ignore[no-untyped-def]
) -> None:
    """Codex amendment 7: editing manuscript without claim_map (or
    vice versa) is rejected — the two artifacts cross-reference and
    must be re-saved together."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        run_id = await _create_run(client)
        seeded_pv = _seed_phase_completed(
            app_session,
            run_id,
            phase="drafter",
            files={
                "drafts/v001/manuscript.md": "# original manuscript\n",
                "drafts/v001/claim_map.jsonl": '{"claim_id":"c1","paragraph_id":"p1"}\n',
            },
        )

        response = await client.put(
            f"/api/runs/{run_id}/phases/drafter/edit",
            json={
                "base_version_id": seeded_pv,
                "files": {
                    "drafts/v001/manuscript.md": "# user-edited\n",
                },
            },
        )

    assert response.status_code == 400
    assert "claim_map" in response.json()["detail"].lower() or "claim_map.jsonl" in response.text


async def test_drafter_edit_accepts_paired_files(
    app_session,  # type: ignore[no-untyped-def]
) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        run_id = await _create_run(client)
        seeded_pv = _seed_phase_completed(
            app_session,
            run_id,
            phase="drafter",
            files={
                "drafts/v001/manuscript.md": "# original\n",
                "drafts/v001/claim_map.jsonl": '{"claim_id":"c1"}\n',
            },
        )

        response = await client.put(
            f"/api/runs/{run_id}/phases/drafter/edit",
            json={
                "base_version_id": seeded_pv,
                "files": {
                    "drafts/v001/manuscript.md": "# user edit paired\n",
                    "drafts/v001/claim_map.jsonl": '{"claim_id":"c1","note":"user"}\n',
                },
            },
        )

    assert response.status_code == 200, response.text


async def test_optimistic_concurrency_409_when_head_moved(
    app_session,  # type: ignore[no-untyped-def]
) -> None:
    """Caller passed a stale base_version_id — must 409."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        run_id = await _create_run(client)
        _seed_phase_completed(
            app_session,
            run_id,
            phase="ideator",
            files={
                "novelty/angle_cards.json": '[{"angle_id":"a1"}]',
            },
        )

        response = await client.put(
            f"/api/runs/{run_id}/phases/ideator/edit",
            json={
                "base_version_id": "pv_unknown_or_old",
                "files": {
                    "novelty/angle_cards.json": '[{"angle_id":"a1","edited":true}]',
                },
            },
        )

    assert response.status_code == 409
    assert "another version" in response.json()["detail"].lower()


async def test_rejects_paths_outside_registry(
    app_session,  # type: ignore[no-untyped-def]
) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        run_id = await _create_run(client)
        seeded_pv = _seed_phase_completed(
            app_session,
            run_id,
            phase="ideator",
            files={"novelty/angle_cards.json": "[]"},
        )

        # Try to overwrite an arbitrary path that isn't in the
        # ideator editable registry.
        response = await client.put(
            f"/api/runs/{run_id}/phases/ideator/edit",
            json={
                "base_version_id": seeded_pv,
                "files": {"synthesis/claims.jsonl": '{"claim":"sneaky"}'},
            },
        )

    assert response.status_code == 409
    assert "cannot edit" in response.json()["detail"]


async def test_rejects_invalid_json(
    app_session,  # type: ignore[no-untyped-def]
) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        run_id = await _create_run(client)
        seeded_pv = _seed_phase_completed(
            app_session,
            run_id,
            phase="ideator",
            files={"novelty/angle_cards.json": "[]"},
        )

        response = await client.put(
            f"/api/runs/{run_id}/phases/ideator/edit",
            json={
                "base_version_id": seeded_pv,
                "files": {
                    "novelty/angle_cards.json": "not-valid-json",
                },
            },
        )

    assert response.status_code == 400
    assert "invalid json" in response.json()["detail"].lower()


async def test_rejects_when_phase_has_no_output_yet(
    app_session,  # type: ignore[no-untyped-def]
) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        run_id = await _create_run(client)
        # Don't seed any artifacts.
        response = await client.put(
            f"/api/runs/{run_id}/phases/synthesizer/edit",
            json={
                "base_version_id": None,
                "files": {"synthesis/claims.jsonl": '{"claim":"x"}'},
            },
        )

    assert response.status_code == 409
    assert (
        "no output yet" in response.json()["detail"].lower()
        or "has not produced" in response.json()["detail"].lower()
    )


async def test_rejects_when_run_is_running(
    app_session,  # type: ignore[no-untyped-def]
) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        run_id = await _create_run(client)
        seeded_pv = _seed_phase_completed(
            app_session,
            run_id,
            phase="ideator",
            files={"novelty/angle_cards.json": "[]"},
        )

        with app_session() as session:
            run = session.scalar(select(Run).where(Run.id == run_id))
            assert run is not None
            run.state = "DRAFTER_RUNNING"
            session.commit()

        response = await client.put(
            f"/api/runs/{run_id}/phases/ideator/edit",
            json={
                "base_version_id": seeded_pv,
                "files": {"novelty/angle_cards.json": "[]"},
            },
        )

    assert response.status_code == 409
    assert "currently running" in response.json()["detail"].lower()


async def test_phase_versions_response_exposes_user_edit_source(
    app_session,  # type: ignore[no-untyped-def]
) -> None:
    """End-to-end: after a user-edit, GET /versions reflects
    source='user_edit' (PR-A1 plumbing × PR-A2 caller)."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        run_id = await _create_run(client)
        seeded_pv = _seed_phase_completed(
            app_session,
            run_id,
            phase="synthesizer",
            files={
                "synthesis/claims.jsonl": "{}\n",
                "synthesis/synthesizer_report.md": "# x\n",
            },
        )

        edit_resp = await client.put(
            f"/api/runs/{run_id}/phases/synthesizer/edit",
            json={
                "base_version_id": seeded_pv,
                "files": {
                    "synthesis/claims.jsonl": "{}\n",
                    "synthesis/synthesizer_report.md": "# y\n",
                },
            },
        )
        assert edit_resp.status_code == 200, edit_resp.text

        versions_resp = await client.get(
            f"/api/runs/{run_id}/phases/synthesizer/versions",
        )

    assert versions_resp.status_code == 200
    versions = versions_resp.json()["versions"]
    sources = {v["version_no"]: v["source"] for v in versions}
    assert sources[1] == "agent"
    assert sources[2] == "user_edit"


async def test_editable_endpoint_lists_artifacts_with_current_content(
    app_session,  # type: ignore[no-untyped-def]
) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        run_id = await _create_run(client)
        _seed_phase_completed(
            app_session,
            run_id,
            phase="synthesizer",
            files={
                "synthesis/claims.jsonl": '{"claim":"seed"}',
                "synthesis/synthesizer_report.md": "seeded",
            },
        )

        resp = await client.get(f"/api/runs/{run_id}/phases/synthesizer/editable")

    assert resp.status_code == 200
    body = resp.json()
    paths = {entry["path"] for entry in body["entries"]}
    assert paths == {"synthesis/claims.jsonl", "synthesis/synthesizer_report.md"}
    contents = {entry["path"]: entry["current_content"] for entry in body["entries"]}
    assert contents["synthesis/claims.jsonl"] == '{"claim":"seed"}'
    assert contents["synthesis/synthesizer_report.md"] == "seeded"
    assert body["base_version_id"] is not None


def test_unknown_phase_rejected_at_helper_level() -> None:
    from autoessay.phase_user_edit import PhaseUserEditError, apply_phase_user_edit

    with pytest.raises(PhaseUserEditError):
        apply_phase_user_edit(
            session=None,  # type: ignore[arg-type]
            run=None,  # type: ignore[arg-type]
            phase="not-a-real-phase",
            base_version_id=None,
            files={},
            user_id=None,
        )


# ---------------------------------------------------------------
# PR-A3 replace-vs-new mode tests (codex AGREE 2026-05-01).
# ---------------------------------------------------------------


async def test_synthesizer_replace_mode_overwrites_in_place(
    app_session,  # type: ignore[no-untyped-def]
) -> None:
    """``mode=replace`` does NOT bump version_no, returns the same
    phase_version_id, and updates the head pv's archive in place.
    Source flips from agent → user_edit.
    """
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        run_id = await _create_run(client)
        seeded_pv = _seed_phase_completed(
            app_session,
            run_id,
            phase="synthesizer",
            files={
                "synthesis/claims.jsonl": '{"claim":"original"}\n',
                "synthesis/synthesizer_report.md": "# original\n",
            },
        )

        response = await client.put(
            f"/api/runs/{run_id}/phases/synthesizer/edit",
            json={
                "base_version_id": seeded_pv,
                "mode": "replace",
                "files": {
                    "synthesis/claims.jsonl": '{"claim":"replaced"}\n',
                    "synthesis/synthesizer_report.md": "# replaced\n",
                },
            },
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["mode"] == "replace"
    assert body["phase_version_id"] == seeded_pv  # SAME id
    assert body["version_no"] == 1  # NOT bumped
    assert body["stale_from_phase"] is None

    with app_session() as session:
        pv = session.get(PhaseVersion, seeded_pv)
        assert pv is not None
        # Source bumped from 'agent' to 'user_edit'.
        assert pv.source == "user_edit"


async def test_replace_rejected_when_downstream_completed(
    app_session,  # type: ignore[no-untyped-def]
) -> None:
    """``mode=replace`` 409s when ``ideator`` (a downstream of
    synthesizer) has produced output on disk. Codex amendment 5."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        run_id = await _create_run(client)
        seeded_pv = _seed_phase_completed(
            app_session,
            run_id,
            phase="synthesizer",
            files={
                "synthesis/claims.jsonl": '{"claim":"original"}\n',
                "synthesis/synthesizer_report.md": "# original\n",
            },
        )
        # Plant ideator output so first_completed_downstream
        # returns 'ideator'.
        with app_session() as session:
            run = session.scalar(select(Run).where(Run.id == run_id))
            assert run is not None
            (Path(run.run_dir) / "novelty").mkdir(parents=True, exist_ok=True)
            (Path(run.run_dir) / "novelty" / "angle_cards.json").write_text(
                "[]\n",
                encoding="utf-8",
            )

        response = await client.put(
            f"/api/runs/{run_id}/phases/synthesizer/edit",
            json={
                "base_version_id": seeded_pv,
                "mode": "replace",
                "files": {
                    "synthesis/claims.jsonl": '{"claim":"new"}\n',
                    "synthesis/synthesizer_report.md": "# new\n",
                },
            },
        )

    assert response.status_code == 409
    detail = response.json()["detail"].lower()
    assert "replace" in detail and "ideator" in detail


async def test_editable_endpoint_replace_eligible_false_when_phase_running(
    app_session,  # type: ignore[no-untyped-def]
) -> None:
    """2026-05-03 follow-up: even when no downstream has produced
    output yet, replace_eligible MUST be False if the run is in
    a RUNNING state — the PUT save would 409 otherwise. UI
    affordance must match authority."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        run_id = await _create_run(client)
        _seed_phase_completed(
            app_session,
            run_id,
            phase="curator",
            files={
                "sources/shortlist.json": "[]",
            },
        )

        # Force the run into a RUNNING state (synthesizer is
        # consuming curator's output).
        with app_session() as session:
            run = session.scalar(select(Run).where(Run.id == run_id))
            assert run is not None
            run.state = "SYNTHESIZER_RUNNING"
            session.commit()

        resp = await client.get(f"/api/runs/{run_id}/phases/curator/editable")
        assert resp.status_code == 200
        # No completed downstream output exists yet, but a RUNNING
        # state still bars replace.
        assert resp.json()["replace_eligible"] is False


async def test_editable_endpoint_replace_eligible_false_when_phase_lock_held(
    app_session,  # type: ignore[no-untyped-def]
) -> None:
    """2026-05-03 follow-up: same gate via active_phase_lock —
    even if state is quiescent, a held phase lock means an agent
    is mid-flight (TOCTOU race covered by codex round-2 amendment 5)."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        run_id = await _create_run(client)
        _seed_phase_completed(
            app_session,
            run_id,
            phase="curator",
            files={"sources/shortlist.json": "[]"},
        )
        with app_session() as session:
            run = session.scalar(select(Run).where(Run.id == run_id))
            assert run is not None
            run.active_phase_lock = "synthesizer"
            session.commit()

        resp = await client.get(f"/api/runs/{run_id}/phases/curator/editable")
        assert resp.status_code == 200
        assert resp.json()["replace_eligible"] is False


async def test_editable_endpoint_reports_replace_eligibility(
    app_session,  # type: ignore[no-untyped-def]
) -> None:
    """GET /editable's ``replace_eligible`` flag tracks whether the
    UI should offer the radio. True when no downstream completed
    AND head is exclusive; False once any downstream produces
    output."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        run_id = await _create_run(client)
        _seed_phase_completed(
            app_session,
            run_id,
            phase="synthesizer",
            files={
                "synthesis/claims.jsonl": '{"claim":"seed"}',
                "synthesis/synthesizer_report.md": "seeded",
            },
        )

        before = await client.get(f"/api/runs/{run_id}/phases/synthesizer/editable")
        # Plant downstream output AFTER the first GET.
        with app_session() as session:
            run = session.scalar(select(Run).where(Run.id == run_id))
            assert run is not None
            (Path(run.run_dir) / "novelty").mkdir(parents=True, exist_ok=True)
            (Path(run.run_dir) / "novelty" / "angle_cards.json").write_text(
                "[]\n",
                encoding="utf-8",
            )
        after = await client.get(f"/api/runs/{run_id}/phases/synthesizer/editable")

    assert before.status_code == 200
    assert before.json()["replace_eligible"] is True
    assert after.status_code == 200
    assert after.json()["replace_eligible"] is False
