"""Tests for ``GET /api/runs/{id}/phase-history`` (PR-A4.2).

The endpoint returns the per-phase modal payload with the
3-flag computed state (head_missing / prompt_dirty /
lineage_dirty) plus full upstream summary and version list.

State-flag rules (codex AGREE 2026-05-02 amendment 1):
- ``head_missing``: no RunHead row for (run, branch, phase).
- ``prompt_dirty``: at least one ``PhasePromptDraft`` row whose
  content_hash differs from the head pv's
  ``PhaseVersionPrompt.content_hash`` for the same prompt_key.
  If the head has no prompt snapshot for that key but a draft
  exists, also dirty.
- ``lineage_dirty``: head pv's recorded ``upstream_pv_id`` no
  longer matches the current upstream RunHead for any upstream
  phase.
"""

from __future__ import annotations

from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from autoessay.config import get_settings
from autoessay.main import app
from autoessay.models import (
    PhasePromptDraft,
    PhaseVersion,
    PhaseVersionPrompt,
    Run,
    RunHead,
)


async def _walk_to(client: AsyncClient, *target_phases: str) -> str:
    """Create a project + run and walk through ``target_phases``
    using the stub agents."""
    project_resp = await client.post(
        "/api/projects",
        json={"title": "phase-history endpoint test"},
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
    await client.post(f"/api/runs/{run_id}/proposal", json={})
    for phase in target_phases:
        if phase == "curator":
            await _approve_source_review(client, run_id, "skim_candidates")
        elif phase == "synthesizer":
            await _approve_source_review(client, run_id, "shortlist")
        resp = await client.post(f"/api/runs/{run_id}/{phase}", json={})
        assert resp.status_code == 202, f"{phase} POST: {resp.text}"
    return run_id


async def _approve_source_review(client: AsyncClient, run_id: str, source_key: str) -> None:
    response = await client.get(f"/api/runs/{run_id}/sources")
    assert response.status_code == 200, response.text
    source_ids = [
        str(row["source_id"])
        for row in response.json()[source_key]
        if isinstance(row, dict) and row.get("source_id")
    ]
    assert source_ids
    checkpoint_type = (
        "USER_SEARCH_REVIEW" if source_key == "skim_candidates" else "USER_DEEP_DIVE_REVIEW"
    )
    scope = "search_review" if source_key == "skim_candidates" else "deep_dive_review"
    checkpoint_resp = await client.post(
        f"/api/runs/{run_id}/checkpoints/{checkpoint_type}",
        json={
            "status": "ACCEPTED",
            "decision_payload": {
                "source_ids": source_ids,
                "approved_source_ids": source_ids,
                "review_scope": scope,
            },
        },
    )
    assert checkpoint_resp.status_code == 201, checkpoint_resp.text


def _enable_stubs(monkeypatch, *names: str) -> None:
    monkeypatch.setenv("AUTOESSAY_SYNC_WORKER", "1")
    monkeypatch.setenv("AUTOESSAY_PROPOSAL_STUB", "1")
    monkeypatch.setenv("AUTOESSAY_INCLUDE_UNVERIFIED_IN_CITATION_POOL", "1")
    for n in names:
        monkeypatch.setenv(f"AUTOESSAY_{n.upper()}_STUB", "1")
    get_settings.cache_clear()


async def test_phase_history_vanilla_run_has_clean_flags(
    app_session,  # type: ignore[no-untyped-def]
    monkeypatch,
) -> None:
    """A run that just walked scout → curator → synthesizer (no
    drafts, no upstream rerun) should report all flags False on
    each completed phase, and head_missing=True for un-run
    phases."""
    _enable_stubs(monkeypatch, "scout", "curator", "synthesizer")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        run_id = await _walk_to(client, "scout", "curator", "synthesizer")
        resp = await client.get(f"/api/runs/{run_id}/phase-history")

    assert resp.status_code == 200
    body = resp.json()
    by_phase = {p["phase"]: p for p in body["phases"]}
    for phase in ("scout", "curator", "synthesizer"):
        flags = by_phase[phase]["state_flags"]
        assert flags == {
            "head_missing": False,
            "prompt_dirty": False,
            "lineage_dirty": False,
        }
        assert by_phase[phase]["head_version_no"] == 1
    # Phases that haven't run yet — head_missing=True.
    for phase in ("ideator", "drafter", "stylist", "critic", "integrity", "exports"):
        flags = by_phase[phase]["state_flags"]
        assert flags["head_missing"] is True
        assert by_phase[phase]["head_version_no"] is None


async def test_phase_history_head_missing_after_upstream_advances(
    app_session,  # type: ignore[no-untyped-def]
    monkeypatch,
) -> None:
    """Walk to USER_FIELD_REVIEW (synthesizer done), then rerun
    scout. PR-A4.3 cascade now DELETES curator/synthesizer
    RunHeads (codex round-2 amendment): no downstream pv has
    lineage matching the new scout v2, so heads are removed.

    The 3-flag state therefore reports ``head_missing=True`` on
    curator and synthesizer (NOT ``lineage_dirty`` — that was
    the pre-cascade behavior). This matches rule 5: cascade
    on activate-or-rerun keeps heads consistent with current
    upstream lineage.
    """
    _enable_stubs(monkeypatch, "scout", "curator", "synthesizer")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        run_id = await _walk_to(client, "scout", "curator", "synthesizer")
        # Rerun scout → scout v2 + cascade clears curator/synthesizer.
        rerun_resp = await client.post(
            f"/api/runs/{run_id}/phases/scout/rerun",
            json={},
        )
        assert rerun_resp.status_code in (200, 202), rerun_resp.text

        resp = await client.get(f"/api/runs/{run_id}/phase-history")

    body = resp.json()
    by_phase = {p["phase"]: p for p in body["phases"]}

    # Scout itself: head moved to v2.
    assert by_phase["scout"]["head_version_no"] == 2
    assert by_phase["scout"]["state_flags"]["lineage_dirty"] is False

    # Curator + synthesizer: cascade deleted their RunHead rows
    # because no pv has lineage matching scout v2. They report
    # head_missing=True now.
    assert by_phase["curator"]["state_flags"]["head_missing"] is True
    assert by_phase["synthesizer"]["state_flags"]["head_missing"] is True

    # upstream_summary should still show scout's current head
    # (v2). matches_my_lineage is False because there is no
    # head pv to compare against.
    syn_upstream = {u["upstream_phase"]: u for u in by_phase["synthesizer"]["upstream_summary"]}
    assert syn_upstream["scout"]["head_version_no"] == 2  # current scout head


async def test_phase_history_prompt_dirty_when_draft_differs(
    app_session,  # type: ignore[no-untyped-def]
    monkeypatch,
) -> None:
    """Plant a PhasePromptDraft for synthesizer with content_hash
    that differs from the head pv's PhaseVersionPrompt snapshot.
    Endpoint should report prompt_dirty=True for synthesizer."""
    _enable_stubs(monkeypatch, "scout", "curator", "synthesizer")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        run_id = await _walk_to(client, "scout", "curator", "synthesizer")

        # Plant a draft with a fake hash that won't match anything.
        with app_session() as session:
            run = session.scalar(select(Run).where(Run.id == run_id))
            assert run is not None
            session.add(
                PhasePromptDraft(
                    run_id=run.id,
                    branch_id=run.active_branch_id,
                    phase="synthesizer",
                    prompt_key="main",
                    content="overridden content",
                    content_hash="sha-different-from-anything-the-agent-wrote",
                ),
            )
            session.commit()

        resp = await client.get(f"/api/runs/{run_id}/phase-history")

    body = resp.json()
    by_phase = {p["phase"]: p for p in body["phases"]}
    assert by_phase["synthesizer"]["state_flags"]["prompt_dirty"] is True
    # Other phases (no drafts) stay clean.
    assert by_phase["scout"]["state_flags"]["prompt_dirty"] is False
    assert by_phase["curator"]["state_flags"]["prompt_dirty"] is False


async def test_phase_history_versions_list_full_lineage(
    app_session,  # type: ignore[no-untyped-def]
    monkeypatch,
) -> None:
    """The endpoint's ``versions`` array should include every pv
    created on the active branch, sorted descending, with full
    lineage. Synthesizer with one rerun → 2 versions."""
    _enable_stubs(monkeypatch, "scout", "curator", "synthesizer")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        run_id = await _walk_to(client, "scout", "curator", "synthesizer")
        await client.post(
            f"/api/runs/{run_id}/phases/synthesizer/rerun",
            json={},
        )

        resp = await client.get(f"/api/runs/{run_id}/phase-history")

    body = resp.json()
    by_phase = {p["phase"]: p for p in body["phases"]}
    syn_versions = by_phase["synthesizer"]["versions"]
    # 2 versions: v1 (vanilla) and v2 (rerun), newest first.
    assert [v["version_no"] for v in syn_versions] == [2, 1]
    # is_head should be True only for v2.
    assert syn_versions[0]["is_head"] is True
    assert syn_versions[1]["is_head"] is False
    # Each version has lineage to scout + curator.
    for v in syn_versions:
        upstream_phases = {ln["upstream_phase"] for ln in v["upstream_lineage"]}
        assert "scout" in upstream_phases
        assert "curator" in upstream_phases


async def test_phase_history_dependent_summary_when_referenced(
    app_session,  # type: ignore[no-untyped-def]
    monkeypatch,
) -> None:
    """Scout v1 is referenced by curator v1 + synthesizer v1 etc.
    The endpoint should report ``has_downstream_dependents=True``
    on scout v1 with a sample dependent in dependent_summary."""
    _enable_stubs(monkeypatch, "scout", "curator", "synthesizer")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        run_id = await _walk_to(client, "scout", "curator", "synthesizer")
        resp = await client.get(f"/api/runs/{run_id}/phase-history")

    body = resp.json()
    by_phase = {p["phase"]: p for p in body["phases"]}
    scout_v1 = by_phase["scout"]["versions"][0]
    assert scout_v1["version_no"] == 1
    assert scout_v1["has_downstream_dependents"] is True
    assert scout_v1["dependent_summary"] is not None


async def test_user_edit_pv_captures_prompt_snapshot(
    app_session,  # type: ignore[no-untyped-def]
    monkeypatch,
) -> None:
    """PR-A4.2 codex amendment 1: ``apply_phase_user_edit`` must
    capture a prompt snapshot on the new user_edit pv so future
    prompt_dirty checks have a baseline to compare against."""
    _enable_stubs(monkeypatch, "scout", "curator", "synthesizer")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        run_id = await _walk_to(client, "scout", "curator", "synthesizer")
        # PR-A4.1b: synthesizer now has a tracked head pv from
        # the vanilla first run; user-edit must echo back its id.
        editable = await client.get(
            f"/api/runs/{run_id}/phases/synthesizer/editable",
        )
        base_version_id = editable.json()["base_version_id"]
        # Edit synthesizer artifacts → mode=new creates a user_edit pv.
        edit_resp = await client.put(
            f"/api/runs/{run_id}/phases/synthesizer/edit",
            json={
                "base_version_id": base_version_id,
                "mode": "new",
                "files": {
                    "synthesis/claims.jsonl": '{"claim":"user edit"}\n',
                    "synthesis/synthesizer_report.md": "# user-edited\n",
                },
            },
        )
        assert edit_resp.status_code == 200, edit_resp.text

    with app_session() as session:
        # The new user_edit pv should have PhaseVersionPrompt rows.
        head_pv_id = session.scalar(
            select(RunHead.version_id)
            .where(RunHead.run_id == run_id)
            .where(RunHead.phase == "synthesizer"),
        )
        assert head_pv_id is not None
        head_pv = session.get(PhaseVersion, head_pv_id)
        assert head_pv is not None and head_pv.source == "user_edit"
        prompts = session.scalars(
            select(PhaseVersionPrompt).where(
                PhaseVersionPrompt.phase_version_id == head_pv_id,
            ),
        ).all()
        # At least one prompt row captured (synthesizer's "main").
        assert len(prompts) >= 1
        assert any(p.prompt_key == "main" for p in prompts)


# =====================================================================
# PR-A4.4: new endpoints — activate-lineage-match + cancel drafts
# + extended payload (delete_blocked, runnable_now, status, etc.)
# =====================================================================


async def test_phase_history_includes_delete_blocked_per_version(
    app_session,  # type: ignore[no-untyped-def]
    monkeypatch,
) -> None:
    """Codex amendment 5 (2026-05-02): each version row exposes
    ``delete_blocked`` + ``delete_block_reason`` so the modal can
    gate the [删除] button without re-implementing the rule-4
    reverse-dep check."""
    _enable_stubs(monkeypatch, "scout", "curator")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        run_id = await _walk_to(client, "scout", "curator")
        resp = await client.get(f"/api/runs/{run_id}/phase-history")

    body = resp.json()
    by_phase = {p["phase"]: p for p in body["phases"]}
    # scout v1 is the active head → delete blocked with reason
    # "active_head".
    scout_v1 = by_phase["scout"]["versions"][0]
    assert scout_v1["delete_blocked"] is True
    assert scout_v1["delete_block_reason"] == "active_head"
    # Every version row also surfaces status now.
    assert scout_v1["status"] == "done"


async def test_phase_history_runnable_now_only_for_ready_phase(
    app_session,  # type: ignore[no-untyped-def]
    monkeypatch,
) -> None:
    """Codex amendment 4: ``runnable_now`` is True only for the
    phase the run can actually start from its current state."""
    _enable_stubs(monkeypatch, "scout")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        run_id = await _walk_to(client, "scout")  # state = USER_SEARCH_REVIEW
        resp = await client.get(f"/api/runs/{run_id}/phase-history")

    body = resp.json()
    by_phase = {p["phase"]: p for p in body["phases"]}
    # USER_SEARCH_REVIEW → curator is the next runnable phase.
    assert by_phase["curator"]["runnable_now"] is True
    # Other phases are not runnable from this state.
    for phase in ("scout", "synthesizer", "ideator", "drafter"):
        assert by_phase[phase]["runnable_now"] is False


async def test_activate_lineage_match_finds_compatible_pv(
    app_session,  # type: ignore[no-untyped-def]
    monkeypatch,
) -> None:
    """Walk + rerun scout twice (scout v1, v2, v3) so multiple
    candidates exist; activate scout v1 (cascade clears curator);
    then call POST /activate-lineage-match on curator → backend
    finds curator v1 (lineage = scout v1) and activates it."""
    _enable_stubs(monkeypatch, "scout", "curator", "synthesizer")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        run_id = await _walk_to(client, "scout", "curator", "synthesizer")
        # Rerun scout → scout v2 (cascade clears curator/synth heads).
        await client.post(f"/api/runs/{run_id}/phases/scout/rerun", json={})

        # Reactivate scout v1.
        with app_session() as session:
            scout_v1 = session.scalars(
                select(PhaseVersion)
                .where(PhaseVersion.run_id == run_id)
                .where(PhaseVersion.phase == "scout")
                .where(PhaseVersion.version_no == 1),
            ).one()
            scout_v1_id = scout_v1.id
        await client.post(
            f"/api/runs/{run_id}/phases/scout/versions/{scout_v1_id}/activate",
        )
        # cascade above should already restore curator v1 (its
        # lineage matches scout v1). Then the activate-lineage-match
        # endpoint on curator should be a no-op or idempotent.
        resp = await client.post(
            f"/api/runs/{run_id}/phases/curator/versions/activate-lineage-match",
        )

    assert resp.status_code == 200, resp.text


async def test_activate_lineage_match_404_when_no_candidate(
    app_session,  # type: ignore[no-untyped-def]
    monkeypatch,
) -> None:
    """When the current upstream vector has no compatible
    historical pv, the endpoint 404s so the modal can fall back
    to "rerun to generate"."""
    _enable_stubs(monkeypatch, "scout", "curator", "synthesizer")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        run_id = await _walk_to(client, "scout", "curator", "synthesizer")
        # Rerun scout → scout v2; cascade clears curator/synth.
        # No curator pv exists with lineage to scout v2.
        await client.post(f"/api/runs/{run_id}/phases/scout/rerun", json={})

        resp = await client.post(
            f"/api/runs/{run_id}/phases/curator/versions/activate-lineage-match",
        )

    assert resp.status_code == 404
    assert "no historical version matches" in resp.json()["detail"].lower()


async def test_activate_lineage_match_endpoint_itself_cascades_downstream(
    app_session,  # type: ignore[no-untyped-def]
    monkeypatch,
) -> None:
    """PR-A4.4 codex round 2 amendment 4 (suggestion): the existing
    happy-path test relies on a prior /activate(scout v1) call to
    cascade-restore curator before /activate-lineage-match runs,
    making the endpoint call idempotent. This test instead drives
    the endpoint into a state where curator has no head AND a
    matching candidate exists, then verifies the endpoint itself
    flips curator to head AND cascades synth.

    Setup: scout v1 + curator v1 + synth v1 (walk). Manually clear
    curator + synth heads via DB to simulate a state where the
    lineage-match endpoint is the first to discover curator v1 +
    synth v1 match the current scout head."""
    _enable_stubs(monkeypatch, "scout", "curator", "synthesizer")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        run_id = await _walk_to(client, "scout", "curator", "synthesizer")

        # Manually clear curator + synth heads. (In real prod this
        # state arises from a cascade where no candidate matched at
        # the moment of cascade; here we engineer it directly so
        # the endpoint has unambiguous work to do.)
        with app_session() as session:
            run = session.scalar(select(Run).where(Run.id == run_id))
            assert run is not None
            for phase in ("curator", "synthesizer"):
                head = session.scalar(
                    select(RunHead)
                    .where(RunHead.run_id == run_id)
                    .where(RunHead.branch_id == run.active_branch_id)
                    .where(RunHead.phase == phase),
                )
                if head is not None:
                    session.delete(head)
            session.commit()

        # POST endpoint on curator. Backend should find curator v1
        # (lineage = scout v1 = current scout head) and activate
        # with cascade → synth v1 (lineage = curator v1) restored.
        resp = await client.post(
            f"/api/runs/{run_id}/phases/curator/versions/activate-lineage-match",
        )
        assert resp.status_code == 200, resp.text

        with app_session() as session:
            run = session.scalar(select(Run).where(Run.id == run_id))
            assert run is not None
            curator_head = session.scalar(
                select(RunHead)
                .where(RunHead.run_id == run_id)
                .where(RunHead.branch_id == run.active_branch_id)
                .where(RunHead.phase == "curator"),
            )
            synth_head = session.scalar(
                select(RunHead)
                .where(RunHead.run_id == run_id)
                .where(RunHead.branch_id == run.active_branch_id)
                .where(RunHead.phase == "synthesizer"),
            )
            assert curator_head is not None, "endpoint failed to set curator head"
            assert synth_head is not None, "endpoint cascade failed to restore synth head"
            curator_pv = session.scalar(
                select(PhaseVersion).where(PhaseVersion.id == curator_head.version_id),
            )
            synth_pv = session.scalar(
                select(PhaseVersion).where(PhaseVersion.id == synth_head.version_id),
            )
            assert curator_pv is not None and curator_pv.version_no == 1
            assert synth_pv is not None and synth_pv.version_no == 1


async def test_cancel_phase_prompt_drafts_idempotent(
    app_session,  # type: ignore[no-untyped-def]
    monkeypatch,
) -> None:
    """Codex amendment 3: phase-wide cancel of prompt drafts.
    Plant a draft, cancel, verify gone. Re-issue → still gone,
    204."""
    _enable_stubs(monkeypatch, "scout", "curator", "synthesizer")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        run_id = await _walk_to(client, "scout", "curator", "synthesizer")
        # Plant a draft.
        with app_session() as session:
            run = session.scalar(select(Run).where(Run.id == run_id))
            assert run is not None
            session.add(
                PhasePromptDraft(
                    run_id=run.id,
                    branch_id=run.active_branch_id,
                    phase="synthesizer",
                    prompt_key="main",
                    content="user-edited",
                    content_hash="abc",
                ),
            )
            session.commit()

        # First cancel → 204 + draft gone.
        resp1 = await client.delete(
            f"/api/runs/{run_id}/phases/synthesizer/prompts/drafts",
        )
        assert resp1.status_code == 204
        # Second cancel → 204 + still no draft (idempotent).
        resp2 = await client.delete(
            f"/api/runs/{run_id}/phases/synthesizer/prompts/drafts",
        )
        assert resp2.status_code == 204

    with app_session() as session:
        rows = session.scalars(
            select(PhasePromptDraft).where(PhasePromptDraft.run_id == run_id),
        ).all()
        assert rows == []


def test_framework_lens_runnable_now_skipped_when_no_lens_inputs(
    tmp_path,  # type: ignore[no-untyped-def]
) -> None:
    # Round-1 audit #6: when paper_mode != theory_article and there
    # are zero theoretical_lens inputs, the start_framework_lens
    # endpoint would skip directly to IDEATOR_RUNNING. Phase-history
    # must therefore report runnable_now=False so the modal doesn't
    # surface a dead "run framework_lens now" CTA.
    import json
    from types import SimpleNamespace

    from autoessay.phase_history import _phase_runnable_now

    run_dir = tmp_path / "run_lens_skip"
    (run_dir / "sources").mkdir(parents=True)
    (run_dir / "sources" / "shortlist.json").write_text(
        json.dumps([{"source_id": "s1", "research_role": "primary_subject"}]),
        encoding="utf-8",
    )
    # No synthesis/synthesizer.json + zero theoretical_lens entries
    # → should_run_framework_lens(case_analysis, ...) returns False.
    run = SimpleNamespace(
        state="USER_FIELD_REVIEW",
        run_dir=str(run_dir),
        paper_mode="case_analysis",
    )
    assert _phase_runnable_now(run, "framework_lens") is False  # type: ignore[arg-type]


def test_framework_lens_runnable_now_true_for_theory_article_mode(
    tmp_path,  # type: ignore[no-untyped-def]
) -> None:
    # Round-1 audit #6 mirror: theory_article mode bypasses the
    # skip-when-empty check (the agent FAILs_FIXABLE with guidance
    # instead of skipping), so runnable_now must be True even with
    # no lens inputs — the user needs the affordance to trigger the
    # phase and see the guidance.
    import json
    from types import SimpleNamespace

    from autoessay.phase_history import _phase_runnable_now

    run_dir = tmp_path / "run_lens_theory"
    (run_dir / "sources").mkdir(parents=True)
    (run_dir / "sources" / "shortlist.json").write_text(
        json.dumps([{"source_id": "s1", "research_role": "primary_subject"}]),
        encoding="utf-8",
    )
    run = SimpleNamespace(
        state="USER_FIELD_REVIEW",
        run_dir=str(run_dir),
        paper_mode="theory_article",
    )
    assert _phase_runnable_now(run, "framework_lens") is True  # type: ignore[arg-type]


def test_framework_lens_runnable_now_true_with_lens_inputs(
    tmp_path,  # type: ignore[no-untyped-def]
) -> None:
    # And the positive case: a case_analysis run with at least one
    # theoretical_lens source IS runnable.
    import json
    from types import SimpleNamespace

    from autoessay.phase_history import _phase_runnable_now

    run_dir = tmp_path / "run_lens_positive"
    (run_dir / "sources").mkdir(parents=True)
    (run_dir / "sources" / "shortlist.json").write_text(
        json.dumps(
            [
                {"source_id": "s1", "research_role": "primary_subject"},
                {"source_id": "s2", "research_role": "theoretical_lens"},
            ]
        ),
        encoding="utf-8",
    )
    run = SimpleNamespace(
        state="USER_FIELD_REVIEW",
        run_dir=str(run_dir),
        paper_mode="case_analysis",
    )
    assert _phase_runnable_now(run, "framework_lens") is True  # type: ignore[arg-type]


def test_ideator_runnable_now_blocked_when_theory_article_at_field_review(
    tmp_path,  # type: ignore[no-untyped-def]
) -> None:
    # Codex round-4 #1 (2026-05-03): theory_article must traverse
    # framework_lens before ideator. Direct skip from
    # USER_FIELD_REVIEW → IDEATOR_RUNNING is rejected by
    # start_ideator; phase-history runnable_now must mirror so the
    # modal CTA isn't a dead button.
    from types import SimpleNamespace

    from autoessay.phase_history import _phase_runnable_now

    run = SimpleNamespace(
        state="USER_FIELD_REVIEW",
        run_dir=str(tmp_path),
        paper_mode="theory_article",
    )
    assert _phase_runnable_now(run, "ideator") is False  # type: ignore[arg-type]


def test_ideator_runnable_now_allowed_when_theory_article_at_lens_review(
    tmp_path,  # type: ignore[no-untyped-def]
) -> None:
    # Mirror: theory_article AT USER_LENS_REVIEW (post-lens path)
    # is the correct entry point for ideator and must be runnable.
    from types import SimpleNamespace

    from autoessay.phase_history import _phase_runnable_now

    run = SimpleNamespace(
        state="USER_LENS_REVIEW",
        run_dir=str(tmp_path),
        paper_mode="theory_article",
    )
    assert _phase_runnable_now(run, "ideator") is True  # type: ignore[arg-type]


def test_ideator_runnable_now_allowed_for_non_theory_at_field_review(
    tmp_path,  # type: ignore[no-untyped-def]
) -> None:
    # Non-theory paper modes can still skip lens directly to ideator.
    from types import SimpleNamespace

    from autoessay.phase_history import _phase_runnable_now

    run = SimpleNamespace(
        state="USER_FIELD_REVIEW",
        run_dir=str(tmp_path),
        paper_mode="case_analysis",
    )
    assert _phase_runnable_now(run, "ideator") is True  # type: ignore[arg-type]
