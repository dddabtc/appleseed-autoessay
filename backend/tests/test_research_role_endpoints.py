"""PR-C1.b: tests for the new research_role + evidence_ledger
HTTP endpoints."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import seed_project
from httpx import ASGITransport, AsyncClient

from autoessay.main import app
from autoessay.models import Branch, Run


def _create_run_with_shortlist(
    session,  # type: ignore[no-untyped-def]
    tmp_path: Path,
    *,
    run_id: str,
    shortlist: list[dict[str, object]],
    has_synthesizer_json: bool = False,
    state: str = "USER_SEARCH_REVIEW",
) -> Path:
    seed_project(session)
    run_dir = tmp_path / run_id
    (run_dir / "sources").mkdir(parents=True)
    (run_dir / "sources" / "shortlist.json").write_text(
        json.dumps(shortlist, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    if has_synthesizer_json:
        (run_dir / "synthesis").mkdir(parents=True)
        (run_dir / "synthesis" / "synthesizer.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "primary_track": [],
                    "secondary_track": [],
                    "theoretical_lens_track": [],
                    "methodological_track": [],
                    "tension_summary_ref": None,
                    "framework_lens_summary_ref": None,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    session.add(
        Run(
            id=run_id,
            project_id="proj_test",
            domain_version="0.1.0",
            run_dir=str(run_dir),
            state=state,
            baseline_hash="t",
            paper_mode="case_analysis",
            research_kernel_json={"kernel_schema_version": 1},
        ),
    )
    session.commit()
    return run_dir


@pytest.mark.asyncio
async def test_update_research_role_writes_shortlist_and_returns_value(
    app_session,  # type: ignore[no-untyped-def]
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("AUTOESSAY_AUTH_BYPASS", "1")
    with app_session() as session:
        run_dir = _create_run_with_shortlist(
            session,
            tmp_path,
            run_id="run_role_basic",
            shortlist=[
                {
                    "source_id": "openalex_W1",
                    "title": "Some study",
                    "research_role": "secondary_argument",
                },
            ],
        )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.put(
            "/api/runs/run_role_basic/sources/openalex_W1/research_role",
            json={"research_role": "primary_source"},
        )
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["source_id"] == "openalex_W1"
    assert payload["research_role"] == "primary_source"
    assert payload["synthesis_marked_stale"] is False

    shortlist = json.loads((run_dir / "sources" / "shortlist.json").read_text())
    assert shortlist[0]["research_role"] == "primary_source"


@pytest.mark.asyncio
async def test_update_research_role_marks_synthesis_stale_when_artifact_exists(
    app_session,  # type: ignore[no-untyped-def]
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("AUTOESSAY_AUTH_BYPASS", "1")
    with app_session() as session:
        _create_run_with_shortlist(
            session,
            tmp_path,
            run_id="run_role_stale",
            shortlist=[
                {
                    "source_id": "openalex_W1",
                    "title": "Some study",
                    "research_role": "secondary_argument",
                },
            ],
            has_synthesizer_json=True,
        )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.put(
            "/api/runs/run_role_stale/sources/openalex_W1/research_role",
            json={"research_role": "theoretical_lens"},
        )
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["synthesis_marked_stale"] is True

    with app_session() as session:
        branch = session.scalar(
            __import__("sqlalchemy").select(Branch).where(Branch.run_id == "run_role_stale"),
        )
        assert branch is not None
        assert branch.stale_from_phase == "synthesizer"


@pytest.mark.asyncio
async def test_update_research_role_rejects_invalid_tier(
    app_session,  # type: ignore[no-untyped-def]
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("AUTOESSAY_AUTH_BYPASS", "1")
    with app_session() as session:
        _create_run_with_shortlist(
            session,
            tmp_path,
            run_id="run_role_bad",
            shortlist=[{"source_id": "openalex_W1", "title": "x"}],
        )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.put(
            "/api/runs/run_role_bad/sources/openalex_W1/research_role",
            json={"research_role": "not_a_real_role"},
        )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_update_research_role_404_for_unknown_source(
    app_session,  # type: ignore[no-untyped-def]
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("AUTOESSAY_AUTH_BYPASS", "1")
    with app_session() as session:
        _create_run_with_shortlist(
            session,
            tmp_path,
            run_id="run_role_unknown",
            shortlist=[{"source_id": "openalex_W1", "title": "x"}],
        )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.put(
            "/api/runs/run_role_unknown/sources/does_not_exist/research_role",
            json={"research_role": "primary_source"},
        )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_evidence_ledger_get_empty_when_no_artifact(
    app_session,  # type: ignore[no-untyped-def]
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("AUTOESSAY_AUTH_BYPASS", "1")
    with app_session() as session:
        _create_run_with_shortlist(
            session,
            tmp_path,
            run_id="run_ledger_empty",
            shortlist=[{"source_id": "openalex_W1", "title": "x"}],
        )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/runs/run_ledger_empty/evidence_ledger")
    assert resp.status_code == 200
    payload = resp.json()
    # No synthesis dir at all → artifact_present=False (legacy run).
    assert payload["artifact_present"] is False
    assert payload["entries"] == []


@pytest.mark.asyncio
async def test_evidence_ledger_override_appends_and_folds(
    app_session,  # type: ignore[no-untyped-def]
    monkeypatch,
    tmp_path: Path,
) -> None:
    """End-to-end: append a claim row directly, POST an override,
    GET reflects the folded effective override on that claim."""
    monkeypatch.setenv("AUTOESSAY_AUTH_BYPASS", "1")
    with app_session() as session:
        run_dir = _create_run_with_shortlist(
            session,
            tmp_path,
            run_id="run_ledger_fold",
            shortlist=[{"source_id": "openalex_W1", "title": "x"}],
        )

    # Seed a claim via the evidence_ledger module directly so the
    # endpoint has something to fold against (mirrors what the
    # synthesizer would write).
    from autoessay.evidence_ledger import append_rows, claim_row

    cr = claim_row(
        source_id="src_alpha",
        claim_text="The reform of 1898 reorganized the salt monopoly.",
        citation_target="archive_qing_001",
        confidence=0.85,
    )
    append_rows(run_dir, [cr])

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # POST per-claim override.
        post = await client.post(
            "/api/runs/run_ledger_fold/evidence_ledger/overrides",
            json={
                "source_id": "src_alpha",
                "claim_id": cr["claim_id"],
                "action": "attribute_to_user",
                "user": "zhaodali78",
            },
        )
        assert post.status_code == 200
        assert post.json()["appended"] is True

        # GET folds the override into the entry.
        get = await client.get("/api/runs/run_ledger_fold/evidence_ledger")
    assert get.status_code == 200
    payload = get.json()
    assert payload["artifact_present"] is True
    assert len(payload["entries"]) == 1
    entry = payload["entries"][0]
    assert entry["override_action"] == "attribute_to_user"
    assert entry["override_user"] == "zhaodali78"


@pytest.mark.asyncio
async def test_evidence_ledger_override_cite_normally_cancels_attribute(
    app_session,  # type: ignore[no-untyped-def]
    monkeypatch,
    tmp_path: Path,
) -> None:
    """Codex amendment: ``cite_normally`` is an EXPLICIT
    cancellation override. A later cite_normally entry beats an
    earlier attribute_to_user — but the override is still
    recorded (``override_action == 'cite_normally'``), not absent.
    """
    monkeypatch.setenv("AUTOESSAY_AUTH_BYPASS", "1")
    with app_session() as session:
        run_dir = _create_run_with_shortlist(
            session,
            tmp_path,
            run_id="run_ledger_cancel",
            shortlist=[{"source_id": "openalex_W1", "title": "x"}],
        )

    from autoessay.evidence_ledger import append_rows, claim_row

    cr = claim_row(
        source_id="src_a",
        claim_text="alpha",
        citation_target="t",
        confidence=0.5,
    )
    append_rows(run_dir, [cr])

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await client.post(
            "/api/runs/run_ledger_cancel/evidence_ledger/overrides",
            json={
                "source_id": "src_a",
                "claim_id": cr["claim_id"],
                "action": "attribute_to_user",
                "user": "u",
            },
        )
        await client.post(
            "/api/runs/run_ledger_cancel/evidence_ledger/overrides",
            json={
                "source_id": "src_a",
                "claim_id": cr["claim_id"],
                "action": "cite_normally",
                "user": "u",
            },
        )
        get = await client.get("/api/runs/run_ledger_cancel/evidence_ledger")
    payload = get.json()
    entry = payload["entries"][0]
    assert entry["override_action"] == "cite_normally"


@pytest.mark.asyncio
async def test_evidence_ledger_source_wide_override_applies_to_all_claims(
    app_session,  # type: ignore[no-untyped-def]
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("AUTOESSAY_AUTH_BYPASS", "1")
    with app_session() as session:
        run_dir = _create_run_with_shortlist(
            session,
            tmp_path,
            run_id="run_ledger_wide",
            shortlist=[{"source_id": "openalex_W1", "title": "x"}],
        )

    from autoessay.evidence_ledger import append_rows, claim_row

    rows = [
        claim_row(source_id="src_x", claim_text=t, citation_target="a", confidence=0.5)
        for t in ("alpha", "beta")
    ]
    append_rows(run_dir, rows)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await client.post(
            "/api/runs/run_ledger_wide/evidence_ledger/overrides",
            json={
                "source_id": "src_x",
                "claim_id": None,
                "action": "attribute_to_user",
                "user": "u",
            },
        )
        get = await client.get("/api/runs/run_ledger_wide/evidence_ledger")

    payload = get.json()
    assert len(payload["entries"]) == 2
    # Both entries pick up the source-wide override.
    for e in payload["entries"]:
        assert e["override_action"] == "attribute_to_user"


@pytest.mark.asyncio
async def test_evidence_ledger_override_rejects_invalid_action(
    app_session,  # type: ignore[no-untyped-def]
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("AUTOESSAY_AUTH_BYPASS", "1")
    with app_session() as session:
        _create_run_with_shortlist(
            session,
            tmp_path,
            run_id="run_ledger_bad_action",
            shortlist=[{"source_id": "openalex_W1", "title": "x"}],
        )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/runs/run_ledger_bad_action/evidence_ledger/overrides",
            json={
                "source_id": "src_x",
                "claim_id": None,
                "action": "block_citation",  # not in C1.b vocabulary
                "user": "u",
            },
        )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_synthesis_endpoint_includes_dual_track_when_artifact_present(
    app_session,  # type: ignore[no-untyped-def]
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("AUTOESSAY_AUTH_BYPASS", "1")
    with app_session() as session:
        _create_run_with_shortlist(
            session,
            tmp_path,
            run_id="run_synth_dual",
            shortlist=[{"source_id": "openalex_W1", "title": "x"}],
            has_synthesizer_json=True,
        )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/runs/run_synth_dual/synthesis")
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["dual_track"] is not None
    assert payload["dual_track"]["schema_version"] == 1
    for k in (
        "primary_track",
        "secondary_track",
        "theoretical_lens_track",
        "methodological_track",
    ):
        assert k in payload["dual_track"]


@pytest.mark.asyncio
async def test_synthesis_endpoint_dual_track_null_for_legacy_run(
    app_session,  # type: ignore[no-untyped-def]
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("AUTOESSAY_AUTH_BYPASS", "1")
    with app_session() as session:
        _create_run_with_shortlist(
            session,
            tmp_path,
            run_id="run_synth_legacy",
            shortlist=[{"source_id": "openalex_W1", "title": "x"}],
            has_synthesizer_json=False,
        )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/runs/run_synth_legacy/synthesis")
    assert resp.status_code == 200
    assert resp.json()["dual_track"] is None


@pytest.mark.asyncio
async def test_research_role_update_rejected_during_running_state(
    app_session,  # type: ignore[no-untyped-def]
    monkeypatch,
    tmp_path: Path,
) -> None:
    # Round-1 audit #22: research_role mutation must not race a
    # running phase. Mid-flight role change would silently flip the
    # synthesizer's input snapshot.
    monkeypatch.setenv("AUTOESSAY_AUTH_BYPASS", "1")
    with app_session() as session:
        _create_run_with_shortlist(
            session,
            tmp_path,
            run_id="run_role_run_guard",
            shortlist=[{"source_id": "openalex_W1", "title": "x"}],
            state="SYNTHESIZER_RUNNING",
        )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.put(
            "/api/runs/run_role_run_guard/sources/openalex_W1/research_role",
            json={"research_role": "secondary_argument"},
        )
    assert resp.status_code == 409
    assert "currently running" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_evidence_ledger_override_rejected_during_running_state(
    app_session,  # type: ignore[no-untyped-def]
    monkeypatch,
    tmp_path: Path,
) -> None:
    # Round-1 audit #23: ledger override mutates synthesis stale
    # state and feeds downstream agents. Reject mid-flight.
    monkeypatch.setenv("AUTOESSAY_AUTH_BYPASS", "1")
    with app_session() as session:
        _create_run_with_shortlist(
            session,
            tmp_path,
            run_id="run_ledger_run_guard",
            shortlist=[{"source_id": "openalex_W1", "title": "x"}],
            state="SYNTHESIZER_RUNNING",
        )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/runs/run_ledger_run_guard/evidence_ledger/overrides",
            json={
                "source_id": "openalex_W1",
                "claim_id": "abc",
                "action": "attribute_to_user",
                "user": "zhaodali78",
            },
        )
    assert resp.status_code == 409
    assert "currently running" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_research_role_stale_propagation_preserves_earlier_marker(
    app_session,  # type: ignore[no-untyped-def]
    monkeypatch,
    tmp_path: Path,
) -> None:
    # Codex DISAGREE #3 (2026-05-03): if the branch is already stale
    # at curator (an earlier phase), updating research_role must NOT
    # overwrite that marker with "synthesizer" — doing so would let
    # the user skip the true earliest stale phase.
    monkeypatch.setenv("AUTOESSAY_AUTH_BYPASS", "1")
    with app_session() as session:
        _create_run_with_shortlist(
            session,
            tmp_path,
            run_id="run_stale_earlier",
            shortlist=[
                {"source_id": "openalex_W1", "title": "x", "research_role": "primary_subject"},
            ],
            has_synthesizer_json=True,
        )
        from autoessay.branches import ensure_main_branch, set_branch_stale
        from autoessay.models import Run as _Run

        run = session.get(_Run, "run_stale_earlier")
        assert run is not None
        ensure_main_branch(session, run)
        set_branch_stale(session, run, "curator")
        session.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.put(
            "/api/runs/run_stale_earlier/sources/openalex_W1/research_role",
            json={"research_role": "secondary_argument"},
        )
    assert resp.status_code == 200, resp.text

    with app_session() as session:
        from autoessay.branches import get_branch_stale
        from autoessay.models import Run as _Run

        run = session.get(_Run, "run_stale_earlier")
        assert run is not None
        assert get_branch_stale(session, run) == "curator"


# ---------------------------------------------------------------------------
# PR-C2c regression — DOI-shaped source_ids with `:` and `/` must reach the
# handler. Without ``{source_id:path}`` Starlette decodes ``%2F`` to ``/``
# before route matching, returning a hard ``404 {"detail": "Not Found"}``
# that no application code ever sees. This silently broke every real-LLM
# lens promotion in prod since PR-C1.b (#144). Surfaced by the PR-C2c
# real-paper acceptance run on 2026-05-03.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_research_role_accepts_doi_shaped_source_id(
    app_session,  # type: ignore[no-untyped-def]
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("AUTOESSAY_AUTH_BYPASS", "1")
    doi_sid = "crossref:10.1108/ijbm-02-2025-0095"
    with app_session() as session:
        run_dir = _create_run_with_shortlist(
            session,
            tmp_path,
            run_id="run_role_doi",
            shortlist=[
                {
                    "source_id": doi_sid,
                    "title": "Doi-shaped source",
                    "research_role": "secondary_argument",
                },
            ],
        )

    transport = ASGITransport(app=app)
    from urllib.parse import quote

    encoded = quote(doi_sid, safe="")
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.put(
            f"/api/runs/run_role_doi/sources/{encoded}/research_role",
            json={"research_role": "theoretical_lens"},
        )
    # Pre-fix this returned 404 ``Not Found`` from FastAPI's router (route
    # never matched); post-fix the handler runs, finds the source, writes
    # the role, returns 200.
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["source_id"] == doi_sid
    assert payload["research_role"] == "theoretical_lens"

    shortlist = json.loads((run_dir / "sources" / "shortlist.json").read_text())
    assert shortlist[0]["research_role"] == "theoretical_lens"


@pytest.mark.asyncio
async def test_get_source_pdf_accepts_doi_shaped_source_id(
    app_session,  # type: ignore[no-untyped-def]
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("AUTOESSAY_AUTH_BYPASS", "1")
    doi_sid = "crossref:10.1108/ijbm-02-2025-0095"
    with app_session() as session:
        run_dir = _create_run_with_shortlist(
            session,
            tmp_path,
            run_id="run_pdf_doi",
            shortlist=[
                {
                    "source_id": doi_sid,
                    "title": "Doi-shaped source",
                },
            ],
        )
        # Seed a fake pdf in the manifest + on disk so find_local_pdf_path
        # resolves to a real file (otherwise the handler returns 404 from
        # its application code, which would mask the routing concern).
        (run_dir / "sources" / "fulltext").mkdir(parents=True, exist_ok=True)
        pdf_path = run_dir / "sources" / "fulltext" / "doi.pdf"
        pdf_path.write_bytes(b"%PDF-1.4 fake")
        (run_dir / "sources" / "fulltext_manifest.json").write_text(
            json.dumps(
                {doi_sid: {"pdf_path": "sources/fulltext/doi.pdf"}},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    transport = ASGITransport(app=app)
    from urllib.parse import quote

    encoded = quote(doi_sid, safe="")
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(f"/api/runs/run_pdf_doi/sources/{encoded}/pdf")
    # Accept either 200 (resolved + streamed) or 200-with-stream.
    # Pre-fix this was a hard 404 from the router.
    assert resp.status_code == 200, resp.text
    assert resp.headers.get("content-type", "").startswith("application/pdf")
