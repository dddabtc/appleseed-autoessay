"""Tests for user-forced approval (Stage 3.E follow-up).

Codex AGREE-with-amendments. Coverage:
- FAILED_POLICY → USER_FINAL_ACCEPTANCE with blocker resolution
- FAILED_VENDOR → USER_FINAL_ACCEPTANCE
- FAILED_FIXABLE → USER_REVISION_REVIEW (drafter / stylist)
- 409 when no minimum artifact
- 409 from CANCELLED
- audit event records reason + cleared blocker snapshot
"""

from __future__ import annotations

import json
from pathlib import Path

from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from autoessay.config import get_settings
from autoessay.main import app
from autoessay.models import Run, RunEvent


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


async def test_force_approve_failed_policy_resolves_blockers(  # type: ignore[no-untyped-def]
    app_session, monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv("AUTOESSAY_SYNC_WORKER", "1")
    get_settings.cache_clear()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        project_response = await client.post(
            "/api/projects", json={"title": "force-approve policy"}
        )
        run_response = await client.post(f"/api/projects/{project_response.json()['id']}/runs")
        run_id = run_response.json()["id"]

        # Seed FAILED_POLICY with two BLOCKER issues on disk.
        with app_session() as session:
            run = session.get(Run, run_id)
            assert run is not None
            run.state = "FAILED_POLICY"
            session.add(
                RunEvent(
                    id="evt_fail_exports_policy",
                    run_id=run_id,
                    event_type="phase_failed",
                    payload=json.dumps({"phase": "exports", "failure_class": "failed_policy"}),
                )
            )
            session.commit()
            _write(
                Path(run.run_dir) / "reviews" / "blocking_issues.json",
                json.dumps(
                    {
                        "issues": [
                            {
                                "issue_id": "audit_a",
                                "severity": "BLOCKER",
                                "paragraph_id": "discussion-p001",
                                "description": "missing source_ids",
                                "dimension": "evidence",
                                "source_ids": [],
                                "suggested_action": "VERIFY_CITATION",
                            },
                            {
                                "issue_id": "critic_b",
                                "severity": "BLOCKER",
                                "paragraph_id": "discussion-p001",
                                "description": "stub paragraph",
                                "dimension": "structure",
                                "source_ids": ["[UNCITED]"],
                                "suggested_action": "REWRITE",
                            },
                        ]
                    }
                ),
            )

        force_resp = await client.post(
            f"/api/runs/{run_id}/force-approve",
            json={"reason": "stub paragraph is acceptable for this draft"},
        )

    assert force_resp.status_code == 200, force_resp.text
    body = force_resp.json()
    assert body["state"] == "USER_FINAL_ACCEPTANCE"
    # Force-approve no longer applies after the transition.
    assert body["force_approve"] is None or body["force_approve"]["applicable"] is False

    # blocking_issues.json now has resolved=true on both issues.
    with app_session() as session:
        run = session.get(Run, run_id)
        assert run is not None
        with open(Path(run.run_dir) / "reviews" / "blocking_issues.json") as f:
            data = json.load(f)
        assert all(issue["resolved"] is True for issue in data["issues"])
        assert all(issue["resolved_by"] == "user_force_approve" for issue in data["issues"])

        # Audit event was recorded with the reason and cleared snapshot.
        audit = session.scalar(
            select(RunEvent)
            .where(RunEvent.run_id == run_id)
            .where(RunEvent.event_type == "force_approve")
            .order_by(RunEvent.created_at.desc())
            .limit(1),
        )
        assert audit is not None
        payload = json.loads(audit.payload)
        assert payload["prior_state"] == "FAILED_POLICY"
        assert payload["new_state"] == "USER_FINAL_ACCEPTANCE"
        assert "stub paragraph is acceptable" in payload["reason"]
        assert len(payload["cleared_blockers"]) == 2
        assert payload["blocking_issues_sha256_pre"]


async def test_force_approve_failed_policy_routes_by_failed_phase(  # type: ignore[no-untyped-def]
    app_session, monkeypatch
) -> None:
    """FAILED_POLICY outside exports must return to that phase's review
    gate instead of always jumping to final acceptance."""
    monkeypatch.setenv("AUTOESSAY_SYNC_WORKER", "1")
    get_settings.cache_clear()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        project_response = await client.post(
            "/api/projects", json={"title": "force-approve scout policy"}
        )
        run_response = await client.post(f"/api/projects/{project_response.json()['id']}/runs")
        run_id = run_response.json()["id"]

        with app_session() as session:
            run = session.get(Run, run_id)
            assert run is not None
            run.state = "FAILED_POLICY"
            _write(Path(run.run_dir) / "discovery" / "scout_report.md", "# Scout report\n")
            _write(
                Path(run.run_dir) / "reviews" / "blocking_issues.json",
                json.dumps(
                    {
                        "issues": [
                            {
                                "issue_id": "scout_policy",
                                "severity": "BLOCKER",
                                "description": "search policy needs user review",
                                "resolved": False,
                            }
                        ]
                    }
                ),
            )
            session.add(
                RunEvent(
                    id="evt_fail_scout_policy",
                    run_id=run_id,
                    event_type="phase_failed",
                    payload=json.dumps({"phase": "scout", "failure_class": "failed_policy"}),
                )
            )
            session.commit()

        get_resp = await client.get(f"/api/runs/{run_id}")
        force_resp = await client.post(
            f"/api/runs/{run_id}/force-approve",
            json={"reason": "reviewed scout policy findings manually"},
        )

    assert get_resp.status_code == 200, get_resp.text
    hint = get_resp.json()["force_approve"]
    assert hint["applicable"] is True
    assert hint["target_state"] == "USER_SEARCH_REVIEW"

    assert force_resp.status_code == 200, force_resp.text
    body = force_resp.json()
    assert body["state"] == "USER_SEARCH_REVIEW"

    with app_session() as session:
        run = session.get(Run, run_id)
        assert run is not None
        with open(Path(run.run_dir) / "reviews" / "blocking_issues.json") as f:
            data = json.load(f)
        assert data["issues"][0]["resolved"] is True


async def test_generic_transition_rejects_failed_state_review_back_edge(  # type: ignore[no-untyped-def]
    app_session, monkeypatch
) -> None:
    """FAILED_* → USER_* recovery must go through force-approve so it
    records phase-aware audit context instead of bypassing the contract."""
    monkeypatch.setenv("AUTOESSAY_SYNC_WORKER", "1")
    get_settings.cache_clear()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        project_response = await client.post(
            "/api/projects", json={"title": "generic failed backedge"}
        )
        run_response = await client.post(f"/api/projects/{project_response.json()['id']}/runs")
        run_id = run_response.json()["id"]

        with app_session() as session:
            run = session.get(Run, run_id)
            assert run is not None
            run.state = "FAILED_POLICY"
            _write(Path(run.run_dir) / "discovery" / "scout_report.md", "# Scout report\n")
            session.add(
                RunEvent(
                    id="evt_generic_backedge_fail",
                    run_id=run_id,
                    event_type="phase_failed",
                    payload=json.dumps({"phase": "scout", "failure_class": "failed_policy"}),
                )
            )
            session.commit()

        transition_resp = await client.post(
            f"/api/runs/{run_id}/transitions",
            json={"to_state": "USER_SEARCH_REVIEW", "reason": "manual bypass"},
        )

    assert transition_resp.status_code == 409
    assert "force-approve" in transition_resp.text


async def test_force_approve_framework_lens_routes_to_lens_review(  # type: ignore[no-untyped-def]
    app_session, monkeypatch
) -> None:
    monkeypatch.setenv("AUTOESSAY_SYNC_WORKER", "1")
    get_settings.cache_clear()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        project_response = await client.post(
            "/api/projects", json={"title": "framework lens force approve"}
        )
        run_response = await client.post(f"/api/projects/{project_response.json()['id']}/runs")
        run_id = run_response.json()["id"]

        with app_session() as session:
            run = session.get(Run, run_id)
            assert run is not None
            run.state = "FAILED_FIXABLE"
            _write(Path(run.run_dir) / "synthesis" / "framework_lens.json", "{}\n")
            session.add(
                RunEvent(
                    id="evt_fail_framework_lens",
                    run_id=run_id,
                    event_type="phase_failed",
                    payload=json.dumps(
                        {"phase": "framework_lens", "failure_class": "failed_fixable"}
                    ),
                )
            )
            session.commit()

        get_resp = await client.get(f"/api/runs/{run_id}")
        force_resp = await client.post(
            f"/api/runs/{run_id}/force-approve",
            json={"reason": "accept the partial framework lens"},
        )

    assert get_resp.status_code == 200, get_resp.text
    hint = get_resp.json()["force_approve"]
    assert hint["applicable"] is True
    assert hint["target_state"] == "USER_LENS_REVIEW"
    assert force_resp.status_code == 200, force_resp.text
    assert force_resp.json()["state"] == "USER_LENS_REVIEW"


async def test_force_approve_proposal_ignores_legacy_checkpoint_sentinel(  # type: ignore[no-untyped-def]
    app_session, monkeypatch
) -> None:
    monkeypatch.setenv("AUTOESSAY_SYNC_WORKER", "1")
    get_settings.cache_clear()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        project_response = await client.post("/api/projects", json={"title": "proposal sentinel"})
        run_response = await client.post(f"/api/projects/{project_response.json()['id']}/runs")
        run_id = run_response.json()["id"]

        with app_session() as session:
            run = session.get(Run, run_id)
            assert run is not None
            run.state = "FAILED_FIXABLE"
            _write(Path(run.run_dir) / "proposal" / "checkpoint.json", "{}\n")
            session.add(
                RunEvent(
                    id="evt_fail_proposal_legacy_checkpoint",
                    run_id=run_id,
                    event_type="phase_failed",
                    payload=json.dumps({"phase": "proposal", "failure_class": "failed_fixable"}),
                )
            )
            session.commit()

        force_resp = await client.post(
            f"/api/runs/{run_id}/force-approve",
            json={"reason": "legacy checkpoint should not count"},
        )

    assert force_resp.status_code == 409
    assert "not applicable" in force_resp.text or "blank" in force_resp.text


async def test_force_approve_legacy_failed_policy_without_phase_stays_409(  # type: ignore[no-untyped-def]
    app_session, monkeypatch
) -> None:
    monkeypatch.setenv("AUTOESSAY_SYNC_WORKER", "1")
    get_settings.cache_clear()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        project_response = await client.post(
            "/api/projects", json={"title": "legacy failed policy"}
        )
        run_response = await client.post(f"/api/projects/{project_response.json()['id']}/runs")
        run_id = run_response.json()["id"]

        with app_session() as session:
            run = session.get(Run, run_id)
            assert run is not None
            run.state = "FAILED_POLICY"
            session.commit()

        get_resp = await client.get(f"/api/runs/{run_id}")
        force_resp = await client.post(
            f"/api/runs/{run_id}/force-approve",
            json={"reason": "legacy failure has no phase context"},
        )

    assert get_resp.status_code == 200, get_resp.text
    hint = get_resp.json()["force_approve"]
    assert hint["applicable"] is False
    assert force_resp.status_code == 409


async def test_force_approve_failed_fixable_drafter_to_revision(  # type: ignore[no-untyped-def]
    app_session, monkeypatch
) -> None:
    """FAILED_FIXABLE on drafter → USER_REVISION_REVIEW so user can
    accept partial output (and rerun later if desired)."""
    monkeypatch.setenv("AUTOESSAY_SYNC_WORKER", "1")
    get_settings.cache_clear()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        project_response = await client.post(
            "/api/projects", json={"title": "force-approve drafter"}
        )
        run_response = await client.post(f"/api/projects/{project_response.json()['id']}/runs")
        run_id = run_response.json()["id"]

        # Seed FAILED_FIXABLE + drafter has produced a manuscript.
        with app_session() as session:
            run = session.get(Run, run_id)
            assert run is not None
            run.state = "FAILED_FIXABLE"
            session.commit()
            _write(Path(run.run_dir) / "drafts" / "v001" / "manuscript.md", "# stub\n")
            session.add(
                RunEvent(
                    id="evt_fail_drafter",
                    run_id=run_id,
                    event_type="phase_failed",
                    payload=json.dumps({"phase": "drafter", "failure_class": "failed_fixable"}),
                )
            )
            session.commit()

        force_resp = await client.post(
            f"/api/runs/{run_id}/force-approve",
            json={"reason": "the partial draft is good enough to review"},
        )

    assert force_resp.status_code == 200, force_resp.text
    assert force_resp.json()["state"] == "USER_REVISION_REVIEW"


async def test_force_approve_409_when_no_phase_artifact(  # type: ignore[no-untyped-def]
    app_session, monkeypatch
) -> None:
    """Codex amendment: reject if target review state has no minimum
    artifact to show — force-approve mustn't dump the user on an empty
    review screen.
    """
    monkeypatch.setenv("AUTOESSAY_SYNC_WORKER", "1")
    get_settings.cache_clear()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        project_response = await client.post("/api/projects", json={"title": "no artifact 409"})
        run_response = await client.post(f"/api/projects/{project_response.json()['id']}/runs")
        run_id = run_response.json()["id"]

        with app_session() as session:
            run = session.get(Run, run_id)
            assert run is not None
            run.state = "FAILED_FIXABLE"
            session.commit()
            session.add(
                RunEvent(
                    id="evt_fail_drafter_empty",
                    run_id=run_id,
                    event_type="phase_failed",
                    payload=json.dumps({"phase": "drafter", "failure_class": "failed_fixable"}),
                )
            )
            session.commit()

        force_resp = await client.post(
            f"/api/runs/{run_id}/force-approve",
            json={"reason": "trying to override anyway"},
        )

    assert force_resp.status_code == 409
    detail = force_resp.json()["detail"].lower()
    assert "not applicable" in detail or "blank" in detail


async def test_force_approve_cancelled_returns_409(  # type: ignore[no-untyped-def]
    app_session,
) -> None:
    """CANCELLED is terminal by design; force-approve never applies."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        project_response = await client.post("/api/projects", json={"title": "cancelled 409"})
        run_response = await client.post(f"/api/projects/{project_response.json()['id']}/runs")
        run_id = run_response.json()["id"]
        with app_session() as session:
            run = session.get(Run, run_id)
            assert run is not None
            run.state = "CANCELLED"
            session.commit()

        force_resp = await client.post(
            f"/api/runs/{run_id}/force-approve",
            json={"reason": "trying anyway"},
        )

    assert force_resp.status_code == 409


async def test_force_approve_rejects_short_reason(  # type: ignore[no-untyped-def]
    app_session,
) -> None:
    """Reason must be ≥ 5 chars after trimming (codex amendment)."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        project_response = await client.post("/api/projects", json={"title": "short reason"})
        run_response = await client.post(f"/api/projects/{project_response.json()['id']}/runs")
        run_id = run_response.json()["id"]
        with app_session() as session:
            run = session.get(Run, run_id)
            assert run is not None
            run.state = "FAILED_VENDOR"
            session.commit()

        force_resp = await client.post(
            f"/api/runs/{run_id}/force-approve",
            json={"reason": "ok"},
        )

    # Pydantic min_length validation returns 422.
    assert force_resp.status_code in {400, 422}


async def test_run_response_force_approve_field_when_failed_vendor(  # type: ignore[no-untyped-def]
    app_session,
) -> None:
    """RunResponse.force_approve must precompute target/consequence
    so frontend doesn't replicate the mapping (codex amendment)."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        project_response = await client.post("/api/projects", json={"title": "force_approve field"})
        run_response = await client.post(f"/api/projects/{project_response.json()['id']}/runs")
        run_id = run_response.json()["id"]
        with app_session() as session:
            run = session.get(Run, run_id)
            assert run is not None
            run.state = "FAILED_VENDOR"
            session.commit()

        get_resp = await client.get(f"/api/runs/{run_id}")

    body = get_resp.json()
    assert body["force_approve"] is not None
    assert body["force_approve"]["applicable"] is True
    assert body["force_approve"]["target_state"] == "USER_FINAL_ACCEPTANCE"
    assert "skips integrity" in body["force_approve"]["consequence"].lower()
