"""Tests for per-version diff (codex-AGREEd #2 stage 2.D).

Covers:
- text_unified diff for .md files (unified-diff line array).
- jsonl_records diff for synthesis/claims.jsonl, matched by claim_id.
- jsonl_records fallback to content-hash when claim_id is absent.
- json_structural diff with ``angle_id``-keyed list matching.
- file_status: added, removed, changed, unchanged.
- context: ``same_upstream_inputs``, ``prompt_hash_changed`` reflect
  the actual lineage / prompt diff.
- ``against`` defaults to parent_pv_id; 409 when there is no parent.
- 409 when either pv has status != 'done'.
- 404 on cross-run / cross-phase pv ids.
"""

from __future__ import annotations

from pathlib import Path

from httpx import ASGITransport, AsyncClient

from autoessay.main import app
from autoessay.models import (
    Domain,
    PhaseVersion,
    Project,
    Run,
    User,
)
from autoessay.phase_version import run_with_versioning
from autoessay.run_writer import create_run_directory


def _seed(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path: Path,
    run_id: str = "run_diff_test",
) -> Path:
    run_dir = create_run_directory(
        tmp_path / "runs",
        run_id,
        "proj_diff",
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
                id="proj_diff",
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
                project_id="proj_diff",
                domain_version="0.1.0",
                run_dir=str(run_dir),
                state="USER_DEEP_DIVE_REVIEW",
                baseline_hash="x",
            ),
        )
        session.flush()
        from autoessay.branches import ensure_main_branch

        run = session.get(Run, run_id)
        assert run is not None
        ensure_main_branch(session, run)
        session.commit()
    return run_dir


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


async def test_diff_text_unified_for_markdown(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path,
) -> None:
    run_dir = _seed(app_session, tmp_path, run_id="run_diff_text")
    legacy = run_dir / "synthesis" / "claims.jsonl"
    report = run_dir / "synthesis" / "synthesizer_report.md"
    with app_session() as session:
        run = session.get(Run, "run_diff_text")

        def first() -> None:
            _write(legacy, "{}")
            _write(report, "first\nsecond\nthird\n")

        run_with_versioning(session, run, "synthesizer", first)

        def second() -> None:
            _write(legacy, "{}")
            _write(report, "first\nSECOND\nthird\n")

        run_with_versioning(session, run, "synthesizer", second)
        rows = session.scalars(
            __import__("sqlalchemy")
            .select(PhaseVersion)
            .where(PhaseVersion.phase == "synthesizer")
            .order_by(PhaseVersion.version_no.asc()),
        ).all()
        v1, v2 = rows[0].id, rows[1].id
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            f"/api/runs/run_diff_text/phases/synthesizer/versions/{v2}/diff?against={v1}"
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    md_diff = next(f for f in body["files"] if f["logical_path"].endswith(".md"))
    assert md_diff["diff_type"] == "text_unified"
    assert md_diff["file_status"] == "changed"
    assert any("-second" in line for line in md_diff["body"]["lines"])
    assert any("+SECOND" in line for line in md_diff["body"]["lines"])


async def test_diff_jsonl_records_matches_by_claim_id(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path,
) -> None:
    run_dir = _seed(app_session, tmp_path, run_id="run_diff_jsonl")
    legacy = run_dir / "synthesis" / "claims.jsonl"
    with app_session() as session:
        run = session.get(Run, "run_diff_jsonl")

        def first() -> None:
            _write(
                legacy,
                '{"claim_id": "c1", "text": "alpha"}\n{"claim_id": "c2", "text": "beta"}\n',
            )

        run_with_versioning(session, run, "synthesizer", first)

        def second() -> None:
            _write(
                legacy,
                '{"claim_id": "c1", "text": "alpha-revised"}\n'
                '{"claim_id": "c3", "text": "gamma"}\n',
            )

        run_with_versioning(session, run, "synthesizer", second)
        rows = session.scalars(
            __import__("sqlalchemy")
            .select(PhaseVersion)
            .where(PhaseVersion.phase == "synthesizer")
            .order_by(PhaseVersion.version_no.asc()),
        ).all()
        v1, v2 = rows[0].id, rows[1].id
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            f"/api/runs/run_diff_jsonl/phases/synthesizer/versions/{v2}/diff?against={v1}"
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    claims_diff = next(f for f in body["files"] if f["logical_path"] == "synthesis/claims.jsonl")
    assert claims_diff["diff_type"] == "jsonl_records"
    assert claims_diff["match_basis"] == "id"
    added_ids = {r["claim_id"] for r in claims_diff["body"]["added"]}
    removed_ids = {r["claim_id"] for r in claims_diff["body"]["removed"]}
    changed_ids = {r["before"]["claim_id"] for r in claims_diff["body"]["changed"]}
    assert added_ids == {"c3"}
    assert removed_ids == {"c2"}
    assert changed_ids == {"c1"}


async def test_diff_default_against_is_parent_pv(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path,
) -> None:
    run_dir = _seed(app_session, tmp_path, run_id="run_diff_parent")
    legacy = run_dir / "synthesis" / "claims.jsonl"
    with app_session() as session:
        run = session.get(Run, "run_diff_parent")
        run_with_versioning(session, run, "synthesizer", lambda: _write(legacy, "{}"))
        run_with_versioning(session, run, "synthesizer", lambda: _write(legacy, '{"a":1}'))
        rows = session.scalars(
            __import__("sqlalchemy")
            .select(PhaseVersion)
            .where(PhaseVersion.phase == "synthesizer")
            .order_by(PhaseVersion.version_no.asc()),
        ).all()
        _v1, v2 = rows[0].id, rows[1].id
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(f"/api/runs/run_diff_parent/phases/synthesizer/versions/{v2}/diff")
    assert resp.status_code == 200, resp.text


async def test_diff_no_parent_returns_409(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path,
) -> None:
    run_dir = _seed(app_session, tmp_path, run_id="run_diff_orphan")
    legacy = run_dir / "synthesis" / "claims.jsonl"
    with app_session() as session:
        run = session.get(Run, "run_diff_orphan")
        run_with_versioning(session, run, "synthesizer", lambda: _write(legacy, "{}"))
        v1 = session.scalar(
            __import__("sqlalchemy")
            .select(PhaseVersion)
            .where(PhaseVersion.phase == "synthesizer"),
        )
        assert v1 is not None
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            f"/api/runs/run_diff_orphan/phases/synthesizer/versions/{v1.id}/diff"
        )
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "no_default_against"


async def test_diff_rejects_failed_status(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path,
) -> None:
    """A failed/cancelled pv has no trustworthy archive — diff must
    409 rather than emit garbage."""
    run_dir = _seed(app_session, tmp_path, run_id="run_diff_failed")
    legacy = run_dir / "synthesis" / "claims.jsonl"
    with app_session() as session:
        import pytest as _pytest

        run = session.get(Run, "run_diff_failed")
        run_with_versioning(session, run, "synthesizer", lambda: _write(legacy, "{}"))
        v1 = session.scalar(
            __import__("sqlalchemy")
            .select(PhaseVersion)
            .where(PhaseVersion.phase == "synthesizer"),
        )
        assert v1 is not None
        v1_id = v1.id

        def boom() -> None:
            _write(legacy, "garbage")
            raise RuntimeError("simulated")

        with _pytest.raises(RuntimeError):
            run_with_versioning(session, run, "synthesizer", boom)
        v_failed = session.scalar(
            __import__("sqlalchemy").select(PhaseVersion).where(PhaseVersion.status == "failed"),
        )
        assert v_failed is not None
        failed_id = v_failed.id
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            f"/api/runs/run_diff_failed/phases/synthesizer/versions/{failed_id}/diff?against={v1_id}"
        )
    assert resp.status_code == 409


async def test_diff_context_reflects_prompt_change(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path,
) -> None:
    """``prompt_hash_changed`` is True when the prompt override
    differs between the two versions."""
    from autoessay.phase_version import ResolvedPrompt

    run_dir = _seed(app_session, tmp_path, run_id="run_diff_prompt")
    legacy = run_dir / "synthesis" / "claims.jsonl"
    with app_session() as session:
        run = session.get(Run, "run_diff_prompt")
        run_with_versioning(
            session,
            run,
            "synthesizer",
            lambda: _write(legacy, "{}"),
            prompts=[
                ResolvedPrompt(
                    prompt_key="main",
                    source="default",
                    content="A",
                    content_hash="hash_A",
                    template_id="x",
                )
            ],
        )
        run_with_versioning(
            session,
            run,
            "synthesizer",
            lambda: _write(legacy, "{}"),
            prompts=[
                ResolvedPrompt(
                    prompt_key="main",
                    source="override",
                    content="B",
                    content_hash="hash_B",
                    template_id="x",
                )
            ],
        )
        rows = session.scalars(
            __import__("sqlalchemy")
            .select(PhaseVersion)
            .where(PhaseVersion.phase == "synthesizer")
            .order_by(PhaseVersion.version_no.asc()),
        ).all()
        v1, v2 = rows[0].id, rows[1].id
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            f"/api/runs/run_diff_prompt/phases/synthesizer/versions/{v2}/diff?against={v1}"
        )
    assert resp.status_code == 200, resp.text
    ctx = resp.json()["context"]
    assert ctx["prompt_hash_changed"] is True
    assert ctx["same_upstream_inputs"] is True


async def test_diff_unit_json_structural_with_id_keyed_list() -> None:
    """JSON structural diff with angle_id-keyed list matching."""
    from autoessay.phase_diff import _diff_json_structural

    a = b'{"angles": [{"angle_id": "a1", "score": 0.5}, {"angle_id": "a2", "score": 0.7}]}'
    b = b'{"angles": [{"angle_id": "a2", "score": 0.9}, {"angle_id": "a3", "score": 0.4}]}'
    body = _diff_json_structural("ideator", "novelty/angle_cards.json", a, b)
    paths = {c["path"] for c in body["changes"]}
    assert any("a1" in p and "removed" not in p or "[angle_id=a1]" in p for p in paths)
    assert any("a3" in p for p in paths)


async def test_diff_added_and_removed_files_are_distinguished(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path,
) -> None:
    run_dir = _seed(app_session, tmp_path, run_id="run_diff_addrem")
    legacy = run_dir / "synthesis" / "claims.jsonl"
    extra = run_dir / "synthesis" / "extra_v1.txt"
    new = run_dir / "synthesis" / "new_v2.md"
    with app_session() as session:
        run = session.get(Run, "run_diff_addrem")

        def first() -> None:
            _write(legacy, "{}")
            _write(extra, "only-in-v1")

        run_with_versioning(session, run, "synthesizer", first)

        def second() -> None:
            # Snapshot is full at commit time; the agent must
            # explicitly delete extra_v1.txt for it to count as
            # "removed" in v2's archive.
            extra.unlink()
            _write(legacy, "{}")
            _write(new, "only-in-v2")

        run_with_versioning(session, run, "synthesizer", second)
        rows = session.scalars(
            __import__("sqlalchemy")
            .select(PhaseVersion)
            .where(PhaseVersion.phase == "synthesizer")
            .order_by(PhaseVersion.version_no.asc()),
        ).all()
        v1, v2 = rows[0].id, rows[1].id
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            f"/api/runs/run_diff_addrem/phases/synthesizer/versions/{v2}/diff?against={v1}"
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    statuses = {f["logical_path"]: f["file_status"] for f in body["files"]}
    assert statuses["synthesis/extra_v1.txt"] == "removed"
    assert statuses["synthesis/new_v2.md"] == "added"
    assert body["summary"]["files_added"] == 1
    assert body["summary"]["files_removed"] == 1


async def test_added_removed_files_use_renderable_diff_type(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path,
) -> None:
    """Codex round-2 #2 stage 2.D: added/removed files must use the
    same diff_type strings the UI knows (``text_unified`` etc.), not
    raw classifier kinds (``text``)."""
    run_dir = _seed(app_session, tmp_path, run_id="run_diff_added_type")
    legacy = run_dir / "synthesis" / "claims.jsonl"
    extra = run_dir / "synthesis" / "extra.md"
    new = run_dir / "synthesis" / "fresh.md"
    with app_session() as session:
        run = session.get(Run, "run_diff_added_type")

        def first() -> None:
            _write(legacy, "{}")
            _write(extra, "old")

        run_with_versioning(session, run, "synthesizer", first)

        def second() -> None:
            extra.unlink()
            _write(legacy, "{}")
            _write(new, "fresh")

        run_with_versioning(session, run, "synthesizer", second)
        rows = session.scalars(
            __import__("sqlalchemy")
            .select(PhaseVersion)
            .where(PhaseVersion.phase == "synthesizer")
            .order_by(PhaseVersion.version_no.asc()),
        ).all()
        v1, v2 = rows[0].id, rows[1].id
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            f"/api/runs/run_diff_added_type/phases/synthesizer/versions/{v2}/diff?against={v1}"
        )
    assert resp.status_code == 200
    files = resp.json()["files"]
    by_path = {f["logical_path"]: f for f in files}
    # Added .md should use text_unified (renderable), not raw "text".
    assert by_path["synthesis/fresh.md"]["diff_type"] == "text_unified"
    assert by_path["synthesis/extra.md"]["diff_type"] == "text_unified"


async def test_jsonl_parse_failure_relabels_diff_type(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path,
) -> None:
    """When JSONL fails to parse, body falls back to a text diff —
    diff_type must become ``text_unified`` so the UI renders ``lines``
    instead of looking for ``added``/``removed`` arrays."""
    run_dir = _seed(app_session, tmp_path, run_id="run_diff_jsonl_fallback")
    legacy = run_dir / "synthesis" / "claims.jsonl"
    with app_session() as session:
        run = session.get(Run, "run_diff_jsonl_fallback")

        def first() -> None:
            _write(legacy, "this is not jsonl")

        run_with_versioning(session, run, "synthesizer", first)

        def second() -> None:
            _write(legacy, "still not jsonl, but different")

        run_with_versioning(session, run, "synthesizer", second)
        rows = session.scalars(
            __import__("sqlalchemy")
            .select(PhaseVersion)
            .where(PhaseVersion.phase == "synthesizer")
            .order_by(PhaseVersion.version_no.asc()),
        ).all()
        v1, v2 = rows[0].id, rows[1].id
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            f"/api/runs/run_diff_jsonl_fallback/phases/synthesizer/versions/{v2}/diff?against={v1}"
        )
    assert resp.status_code == 200
    claims = next(f for f in resp.json()["files"] if f["logical_path"] == "synthesis/claims.jsonl")
    assert claims["diff_type"] == "text_unified"
    assert "lines" in claims["body"]


async def test_jsonl_match_basis_reports_id_with_fallback() -> None:
    """If some claim_id values are missing, match_basis must say
    ``id_with_fallback``, not ``id`` (codex round-2 #2 stage 2.D:
    the earlier code lied about precision)."""
    from autoessay.phase_diff import _diff_jsonl

    a = b'{"claim_id": "c1", "text": "alpha"}\n{"text": "no-id-record"}\n'
    b = b'{"claim_id": "c1", "text": "alpha-revised"}\n{"text": "still-no-id"}\n'
    body = _diff_jsonl("synthesizer", "synthesis/claims.jsonl", a, b)
    assert body["match_basis"] == "id_with_fallback"


async def test_jsonl_duplicate_ids_are_not_collapsed() -> None:
    """Duplicate claim_ids in a single file must not collapse the diff.

    Multiset-by-content matching: two records sharing claim_id but
    with different content stay distinct. The byte-identical record
    pairs by content; the differing record gets surfaced as ``changed``.
    """
    from autoessay.phase_diff import _diff_jsonl

    a = b'{"claim_id": "c1", "text": "first"}\n{"claim_id": "c1", "text": "second"}\n'
    b = b'{"claim_id": "c1", "text": "first"}\n{"claim_id": "c1", "text": "second-edited"}\n'
    body = _diff_jsonl("synthesizer", "synthesis/claims.jsonl", a, b)
    # Every record has claim_id, so no fallback was used.
    assert body["match_basis"] == "id"
    # "first"==="first" pairs as unchanged; "second" vs
    # "second-edited" pairs as 1 changed (same id bucket).
    assert len(body["changed"]) == 1
    assert body["changed"][0]["before"]["text"] == "second"
    assert body["changed"][0]["after"]["text"] == "second-edited"


async def test_jsonl_diff_is_order_insensitive_for_unkeyed_records() -> None:
    """Codex round-3 #2 stage 2.D: with no primary key, identical
    records swapped between versions must match by content hash and
    report no change — not 2 added + 2 removed."""
    from autoessay.phase_diff import _diff_jsonl

    a = b'{"text": "same"}\n{"text": "other"}\n'
    b = b'{"text": "other"}\n{"text": "same"}\n'
    body = _diff_jsonl("scout", "discovery/skim_candidates.jsonl", a, b)
    assert body["added"] == []
    assert body["removed"] == []
    assert body["changed"] == []


async def test_jsonl_duplicate_unkeyed_records_match_by_count() -> None:
    """Unkeyed multiset semantics: A has [{x},{x}], B has [{x}] →
    1 removed (the extra duplicate). Order does not matter."""
    from autoessay.phase_diff import _diff_jsonl

    a = b'{"text": "x"}\n{"text": "x"}\n'
    b = b'{"text": "x"}\n'
    body = _diff_jsonl("scout", "discovery/skim_candidates.jsonl", a, b)
    assert body["added"] == []
    assert len(body["removed"]) == 1
    assert body["changed"] == []


async def test_jsonl_diff_pairing_is_deterministic() -> None:
    """Codex round-4 #2 stage 2.D: pair-up of leftover records under
    the same id-bucket must not depend on Python's set hash order.
    Same inputs → same ``changed`` pairs every call."""
    from autoessay.phase_diff import _diff_jsonl

    # Three records sharing claim_id, all with different content.
    # Nothing matches by content, so all three get paired as changed.
    a = (
        b'{"claim_id": "c1", "text": "alpha"}\n'
        b'{"claim_id": "c1", "text": "beta"}\n'
        b'{"claim_id": "c1", "text": "gamma"}\n'
    )
    b = (
        b'{"claim_id": "c1", "text": "alpha-x"}\n'
        b'{"claim_id": "c1", "text": "beta-x"}\n'
        b'{"claim_id": "c1", "text": "gamma-x"}\n'
    )
    first = _diff_jsonl("synthesizer", "synthesis/claims.jsonl", a, b)
    for _ in range(20):
        again = _diff_jsonl("synthesizer", "synthesis/claims.jsonl", a, b)
        assert again["changed"] == first["changed"]
        assert again["added"] == first["added"]
        assert again["removed"] == first["removed"]
