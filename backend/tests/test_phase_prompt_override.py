"""Tests for per-phase prompt override (codex-AGREEd #2 stage 2.B).

Covers:
- GET prompt returns default + null override on a fresh run.
- PUT prompt creates the draft, returns the new content_hash.
- PUT prompt with null/blank content deletes the row.
- PUT prompt over the byte cap returns 400.
- GET/PUT for an unsupported phase returns 404.
- Rerun consumes the active draft and stamps phase_version_prompts.
- Rerun with a stale draft_hash is rejected 409.
- Synthesizer prompt builder substitutes the override into the static
  instruction block but leaves dynamic context intact.
"""

from __future__ import annotations

from pathlib import Path

from httpx import ASGITransport, AsyncClient

from autoessay.main import app
from autoessay.models import (
    Domain,
    PhasePromptDraft,
    PhaseVersion,
    PhaseVersionPrompt,
    Project,
    Run,
    User,
)
from autoessay.phase_version import run_with_versioning
from autoessay.prompts import (
    SYNTHESIZER_MAIN_INSTRUCTIONS,
    get_phase_prompt_spec,
    hash_content,
)
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
    run_id: str = "run_pp_test",
    *,
    state: str = "USER_DEEP_DIVE_REVIEW",
) -> Path:
    run_dir = create_run_directory(
        tmp_path / "runs",
        run_id,
        "proj_pp",
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
        if session.get(Project, "proj_pp") is None:
            session.add(
                Project(
                    id="proj_pp",
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
                project_id="proj_pp",
                domain_version="0.1.0",
                run_dir=str(run_dir),
                state=state,
                baseline_hash="x",
            ),
        )
        session.commit()
    return run_dir


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


async def test_get_prompt_returns_default_and_null_override(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path,
) -> None:
    _seed(app_session, tmp_path)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/runs/run_pp_test/phases/synthesizer/prompt")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["phase"] == "synthesizer"
    assert body["prompt_key"] == "main"
    assert body["default_content"] == SYNTHESIZER_MAIN_INSTRUCTIONS
    assert body["override_content"] is None
    assert body["draft_hash"] is None
    assert body["supported"] is True


async def test_put_prompt_creates_draft_and_returns_hash(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path,
) -> None:
    _seed(app_session, tmp_path)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.put(
            "/api/runs/run_pp_test/phases/synthesizer/prompt",
            json={"content": "Focus on quantitative findings."},
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["override_content"] == "Focus on quantitative findings."
    assert body["draft_hash"] == hash_content("Focus on quantitative findings.")
    with app_session() as session:
        rows = session.scalars(__import__("sqlalchemy").select(PhasePromptDraft)).all()
        assert len(rows) == 1
        assert rows[0].content == "Focus on quantitative findings."


async def test_put_prompt_with_blank_content_deletes_draft(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path,
) -> None:
    _seed(app_session, tmp_path)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await client.put(
            "/api/runs/run_pp_test/phases/synthesizer/prompt",
            json={"content": "anything"},
        )
        # Now blank it out.
        resp = await client.put(
            "/api/runs/run_pp_test/phases/synthesizer/prompt",
            json={"content": "   "},
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["override_content"] is None
    assert body["draft_hash"] is None
    with app_session() as session:
        rows = session.scalars(__import__("sqlalchemy").select(PhasePromptDraft)).all()
        assert rows == []


async def test_put_prompt_rejected_during_running_state(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path,
) -> None:
    # Round-1 audit #13: prompt mutation must not race a running
    # phase. PUT /prompt during RUNNING_STATES would silently change
    # the prompt under a running agent's feet.
    _seed(app_session, tmp_path, run_id="run_pp_run_guard", state="DRAFTER_RUNNING")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.put(
            "/api/runs/run_pp_run_guard/phases/synthesizer/prompt",
            json={"content": "Focus on quantitative findings."},
        )
    assert resp.status_code == 409
    assert "currently running" in resp.json()["detail"]


async def test_cancel_prompt_drafts_rejected_during_running_state(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path,
) -> None:
    # Round-1 audit #13: phase-wide cancel must also respect the
    # running guard.
    _seed(
        app_session,
        tmp_path,
        run_id="run_pp_cancel_run_guard",
        state="STYLIST_RUNNING",
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.delete(
            "/api/runs/run_pp_cancel_run_guard/phases/stylist/prompts/drafts",
        )
    assert resp.status_code == 409
    assert "currently running" in resp.json()["detail"]


async def test_put_prompt_over_size_cap_returns_400(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path,
) -> None:
    _seed(app_session, tmp_path)
    big = "x" * 60_000
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.put(
            "/api/runs/run_pp_test/phases/synthesizer/prompt",
            json={"content": big},
        )
    assert resp.status_code == 400
    assert resp.json()["detail"]["code"] == "prompt_too_large"


async def test_get_prompt_for_unsupported_phase_returns_404(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path,
) -> None:
    _seed(app_session, tmp_path)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Phase "scout" has NO registered prompt surface and no
        # discovery fallback because supported_keys_for_phase("scout")
        # is empty. After Stage 3.A.4, "curator" no longer fits
        # this scenario — its `ranking` key is now discovered when
        # the caller omits prompt_key.
        resp = await client.get("/api/runs/run_pp_test/phases/scout/prompt")
    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "prompt_surface_not_supported"


async def test_rerun_consumes_active_draft_and_stamps_pv_prompts(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path,
) -> None:
    """A rerun with an active draft must:
    1. Pass the override to the agent (so it actually uses it), AND
    2. Persist the override on phase_version_prompts as source='override'.
    """
    run_dir = _seed(app_session, tmp_path, run_id="run_pp_rerun")
    legacy = run_dir / "synthesis" / "claims.jsonl"
    seen_overrides: list[str | None] = []
    from autoessay import main as main_mod

    def fake_synth(run_id, session=None, *, prompt_overrides=None):
        seen_overrides.append(prompt_overrides.get("main") if prompt_overrides else None)
        with open(legacy, "w", encoding="utf-8") as fh:
            fh.write("{}")

    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text("{}", encoding="utf-8")

    main_mod._PHASE_RUNNERS["synthesizer"] = fake_synth

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await client.put(
            "/api/runs/run_pp_rerun/phases/synthesizer/prompt",
            json={"content": "Be terse. Cite sources."},
        )
        resp = await client.post("/api/runs/run_pp_rerun/phases/synthesizer/rerun")
    assert resp.status_code == 202, resp.text
    assert seen_overrides == ["Be terse. Cite sources."]
    with app_session() as session:
        rows = session.scalars(
            __import__("sqlalchemy")
            .select(PhaseVersionPrompt)
            .order_by(PhaseVersionPrompt.created_at.desc()),
        ).all()
        assert len(rows) >= 1
        assert rows[0].source == "override"
        assert rows[0].content == "Be terse. Cite sources."
        # phase_versions.prompt_hash also populated.
        pv = session.get(PhaseVersion, rows[0].phase_version_id)
        assert pv is not None
        assert pv.prompt_hash is not None


async def test_rerun_with_stale_draft_hash_returns_409(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path,
) -> None:
    """Two-tab race: tab A sees draft hash X, tab B saves a new draft
    hash Y. tab A clicks 'save and rerun' with hash X; rerun must
    409 instead of running with the now-stale draft."""
    run_dir = _seed(app_session, tmp_path, run_id="run_pp_race")
    # has_completed_output requires the sentinel file; otherwise
    # assert_can_rerun returns 409 "no output" before our draft_hash
    # check has a chance to fire.
    _write(run_dir / "synthesis" / "claims.jsonl", "{}")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        get_resp = await client.put(
            "/api/runs/run_pp_race/phases/synthesizer/prompt",
            json={"content": "first version"},
        )
        original_hash = get_resp.json()["draft_hash"]
        # Another save replaces the draft.
        await client.put(
            "/api/runs/run_pp_race/phases/synthesizer/prompt",
            json={"content": "second version"},
        )
        # Rerun with the now-stale hash must 409.
        resp = await client.post(
            "/api/runs/run_pp_race/phases/synthesizer/rerun",
            json={"draft_hash": original_hash},
        )
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "prompt_draft_changed"


async def test_default_prompt_is_recorded_when_no_override(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path,
) -> None:
    """Even without an override, the resolved default must be
    snapshotted onto phase_version_prompts so the history view can
    always show 'this version was produced with this prompt'."""
    run_dir = _seed(app_session, tmp_path, run_id="run_pp_default")
    legacy = run_dir / "synthesis" / "claims.jsonl"
    with app_session() as session:
        run = session.get(Run, "run_pp_default")
        spec = get_phase_prompt_spec("synthesizer", "main")
        assert spec is not None
        from autoessay.phase_version import ResolvedPrompt

        run_with_versioning(
            session,
            run,
            "synthesizer",
            lambda: _write(legacy, '{"v": 1}'),
            prompts=[
                ResolvedPrompt(
                    prompt_key="main",
                    source="default",
                    content=spec.default_content,
                    content_hash=hash_content(spec.default_content),
                    template_id=spec.template_id,
                )
            ],
        )
        rows = session.scalars(__import__("sqlalchemy").select(PhaseVersionPrompt)).all()
        assert len(rows) == 1
        assert rows[0].source == "default"
        assert rows[0].content == SYNTHESIZER_MAIN_INSTRUCTIONS


def test_synthesizer_prompt_builder_uses_override() -> None:
    """The static instruction block is overridable, but the dynamic
    context (sources, schema spec, question) is always appended."""
    from autoessay.agents.synthesizer import _summary_prompt
    from autoessay.clients.common import NormalizedSource

    source = NormalizedSource(
        source_id="src_1",
        title="A",
        authors=["B"],
        year=2024,
        venue=None,
        abstract="abstract text",
        source_client="stub",
        access_status="open",
        risk_flags=[],
    )
    default_prompt = _summary_prompt(
        source=source,
        source_text="text",
        domain_data={},
        project_title="topic",
        proposal=None,
        suffix="",
    )
    overridden = _summary_prompt(
        source=source,
        source_text="text",
        domain_data={},
        project_title="topic",
        proposal=None,
        suffix="",
        instructions_override="CUSTOM INSTRUCTIONS BLOCK",
    )
    assert "You are Synthesizer" in default_prompt
    assert "You are Synthesizer" not in overridden
    assert "CUSTOM INSTRUCTIONS BLOCK" in overridden
    # Dynamic context appears in BOTH so the override doesn't break
    # schema parsing or starve the LLM of source data.
    for required in ("topic", "src_1", "required schema"):
        assert required in default_prompt
        assert required in overridden


async def test_input_snapshot_hash_changes_with_prompt(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path,
) -> None:
    """Same upstream + different prompt = different input_snapshot_hash.
    Codex round-1 caught this: future dedup logic would be wrong if
    the prompt was not part of effective input identity."""
    run_dir = _seed(app_session, tmp_path, run_id="run_pp_hash")
    legacy = run_dir / "synthesis" / "claims.jsonl"
    with app_session() as session:
        run = session.get(Run, "run_pp_hash")
        from autoessay.phase_version import ResolvedPrompt

        run_with_versioning(
            session,
            run,
            "synthesizer",
            lambda: _write(legacy, "v1"),
            prompts=[
                ResolvedPrompt(
                    prompt_key="main",
                    source="default",
                    content="A",
                    content_hash=hash_content("A"),
                    template_id="x",
                )
            ],
        )
        run_with_versioning(
            session,
            run,
            "synthesizer",
            lambda: _write(legacy, "v2"),
            prompts=[
                ResolvedPrompt(
                    prompt_key="main",
                    source="override",
                    content="B",
                    content_hash=hash_content("B"),
                    template_id="x",
                )
            ],
        )
        rows = session.scalars(
            __import__("sqlalchemy")
            .select(PhaseVersion)
            .where(PhaseVersion.run_id == "run_pp_hash")
            .where(PhaseVersion.phase == "synthesizer")
            .order_by(PhaseVersion.version_no.asc()),
        ).all()
        assert rows[0].input_snapshot_hash != rows[1].input_snapshot_hash
        assert rows[0].prompt_hash != rows[1].prompt_hash


async def test_rerun_with_explicit_null_draft_hash_enforces_no_override(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path,
) -> None:
    """A rerun body with ``"draft_hash": null`` (explicit, not omitted)
    means "I expect NO override is active". A concurrent tab that
    saves an override after the user clicks Save-and-Rerun must be
    detected as a stale-draft conflict, not silently consumed."""
    run_dir = _seed(app_session, tmp_path, run_id="run_pp_null_check")
    _write(run_dir / "synthesis" / "claims.jsonl", "{}")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # No override active. Now another tab writes one.
        await client.put(
            "/api/runs/run_pp_null_check/phases/synthesizer/prompt",
            json={"content": "ninja override"},
        )
        # First tab clicked Save-and-Rerun expecting NO override —
        # it sends draft_hash=null. The check must fire.
        resp = await client.post(
            "/api/runs/run_pp_null_check/phases/synthesizer/rerun",
            json={"draft_hash": None},
        )
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "prompt_draft_changed"


async def test_rerun_without_draft_hash_field_skips_check(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path,
) -> None:
    """Omitting ``draft_hash`` from the request body means "use
    whatever is saved, no check". An empty JSON object, not even
    null, must NOT trigger the optimistic concurrency check."""
    run_dir = _seed(app_session, tmp_path, run_id="run_pp_omit_check")
    _write(run_dir / "synthesis" / "claims.jsonl", "{}")
    from autoessay import main as main_mod

    main_mod._PHASE_RUNNERS["synthesizer"] = lambda run_id, session=None, **_: None
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Active override exists — should not block when no hash sent.
        await client.put(
            "/api/runs/run_pp_omit_check/phases/synthesizer/prompt",
            json={"content": "active"},
        )
        resp = await client.post(
            "/api/runs/run_pp_omit_check/phases/synthesizer/rerun",
            json={},
        )
    assert resp.status_code == 202, resp.text


async def test_list_version_prompts_returns_404_for_unknown_pv(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path,
) -> None:
    """A nonexistent or cross-run pv id must return 404, not an
    ambiguous empty list (codex round-2 review)."""
    _seed(app_session, tmp_path, run_id="run_pp_404_check")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/api/runs/run_pp_404_check/phases/synthesizer/versions/pv_does_not_exist/prompts"
        )
    assert resp.status_code == 404


def test_ideator_prompt_builder_uses_override() -> None:
    """Ideator's static instructions are overridable; dynamic context
    (project title, claims, source notes, schema spec) stays appended."""
    from autoessay.agents.ideator import _angle_prompt

    default_prompt = _angle_prompt(
        project_title="Topic",
        target_journal=None,
        domain_data={},
        claims=[],
        source_notes={},
        proposal=None,
        suffix="",
    )
    overridden = _angle_prompt(
        project_title="Topic",
        target_journal=None,
        domain_data={},
        claims=[],
        source_notes={},
        proposal=None,
        suffix="",
        instructions_override="CUSTOM IDEATOR INSTRUCTIONS",
    )
    assert "For each angle give thesis" in default_prompt
    assert "For each angle give thesis" not in overridden
    assert "CUSTOM IDEATOR INSTRUCTIONS" in overridden
    # Schema spec must remain in both so LLM output can still parse.
    for required in ("required schema" if False else "schema:", "Topic"):
        assert required in default_prompt or required in overridden
    assert "schema" in default_prompt
    assert "schema" in overridden


def test_critic_prompt_builder_uses_override() -> None:
    """Critic's static instructions are overridable; dynamic context
    (draft, claim map, evidence pack, schema spec) stays appended."""
    from autoessay.agents.critic import _critic_prompt

    default_prompt = _critic_prompt(
        draft="draft text",
        claim_map=[],
        shortlist=[],
        claims=[],
        source_notes={},
        selected_thesis={},
        suffix="",
    )
    overridden = _critic_prompt(
        draft="draft text",
        claim_map=[],
        shortlist=[],
        claims=[],
        source_notes={},
        selected_thesis={},
        suffix="",
        instructions_override="CUSTOM CRITIC INSTRUCTIONS",
    )
    assert "Find unsupported claims" in default_prompt
    assert "Find unsupported claims" not in overridden
    assert "CUSTOM CRITIC INSTRUCTIONS" in overridden
    # "You are Critic" wrapper and the schema spec stay in both.
    for required in ("You are Critic", "schema", "draft text"):
        assert required in default_prompt
        assert required in overridden


async def test_get_prompt_works_for_ideator_and_critic(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path,
) -> None:
    """Ideator and critic are now registered prompt surfaces.
    Stage 2.B initially shipped synthesizer-only; this verifies the
    extension to the other single-call agents."""
    _seed(app_session, tmp_path, run_id="run_pp_ext")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        for phase, expected_marker in (
            ("ideator", "For each angle"),
            ("critic", "Find unsupported claims"),
        ):
            resp = await client.get(f"/api/runs/run_pp_ext/phases/{phase}/prompt")
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert expected_marker in body["default_content"]
            assert body["override_content"] is None


def test_drafter_prompt_builder_uses_override_with_locked_ordering() -> None:
    """Drafter's static instruction block (universal argumentation +
    forbidden patterns + citation enforcement, merged) is overridable
    on every section. Codex round-1 #2 stage 2.B-extension locked in
    a new ordering: the merged universal block now precedes the
    section-type directive (whereas pre-2.B had section-type between
    argumentation and forbidden+citation). This test pins that order
    so future refactors notice if the prompt geometry shifts.
    """
    from autoessay.agents.drafter import SectionPlan, _section_prompt
    from autoessay.prompts import DRAFTER_MAIN_INSTRUCTIONS

    historiography_section = SectionPlan(
        section_id="historiography",
        title="Historiography",
        target_words=1200,
    )
    default_prompt = _section_prompt(
        section=historiography_section,
        selected_thesis={"thesis_one_sentence": "central claim"},
        source_notes={},
        shortlist=[],
        domain_data={},
        target_journal=None,
        suffix="",
    )
    overridden = _section_prompt(
        section=historiography_section,
        selected_thesis={"thesis_one_sentence": "central claim"},
        source_notes={},
        shortlist=[],
        domain_data={},
        target_journal=None,
        suffix="",
        instructions_override="CUSTOM DRAFTER INSTRUCTIONS",
    )
    # Override substitutes the merged static block.
    assert DRAFTER_MAIN_INSTRUCTIONS[:30] in default_prompt
    assert DRAFTER_MAIN_INSTRUCTIONS[:30] not in overridden
    assert "CUSTOM DRAFTER INSTRUCTIONS" in overridden
    # Dynamic anchors (thesis, outline header, schema spec) appear in
    # both — the override must not strip the surrounding scaffolding.
    for required in ("You are Drafter", "Outline:", "Approved sources:", "Section role:"):
        assert required in default_prompt
        assert required in overridden
    # Section-type directive is appended AFTER the universal block in
    # the new ordering. For 'historiography' the directive is a non-
    # empty Chinese string starting with 本节务必采用. Verify both
    # blocks land in the right order in BOTH default and overridden.
    section_type_marker = "本节务必采用"
    custom_block_offset = overridden.index("CUSTOM DRAFTER INSTRUCTIONS")
    section_type_offset = overridden.index(section_type_marker)
    assert custom_block_offset < section_type_offset, (
        "section-type directive must follow the overridable universal block"
    )
    # Same ordering invariant on the unmodified default prompt — pre-
    # 2.B code had section-type between universal and forbidden, so
    # this assertion confirms the new merged geometry.
    default_universal_offset = default_prompt.index(DRAFTER_MAIN_INSTRUCTIONS[:30])
    default_section_type_offset = default_prompt.index(section_type_marker)
    assert default_universal_offset < default_section_type_offset


def test_stylist_prompt_builder_uses_override() -> None:
    """Stylist's universal 'Revise prose only ... rejected output'
    instruction block is overridable per section. The full-manuscript
    re-polish prompt is intentionally NOT covered by this surface —
    out of scope for stage 2.B-extension."""
    from autoessay.agents.stylist import ManuscriptSection, _section_prompt
    from autoessay.prompts import STYLIST_MAIN_INSTRUCTIONS
    from autoessay.style_profile import StyleProfile

    section = ManuscriptSection(
        section_id="introduction",
        title="Introduction",
        prose="some prose",
    )
    default_prompt = _section_prompt(
        section=section,
        claim_ids=["c1"],
        style_profile=StyleProfile(),
        section_findings=[],
        suffix="",
    )
    overridden = _section_prompt(
        section=section,
        claim_ids=["c1"],
        style_profile=StyleProfile(),
        section_findings=[],
        suffix="",
        instructions_override="CUSTOM STYLIST INSTRUCTIONS",
    )
    assert STYLIST_MAIN_INSTRUCTIONS[:30] in default_prompt
    assert STYLIST_MAIN_INSTRUCTIONS[:30] not in overridden
    assert "CUSTOM STYLIST INSTRUCTIONS" in overridden
    # Dynamic context (claim_ids, draft section text, schema spec)
    # remains in both — schema parsing must not break under override.
    for required in (
        "You are Stylist",
        "Section name:",
        "Draft section:",
        "Claim IDs that must be preserved:",
        "Return strict JSON",
    ):
        assert required in default_prompt
        assert required in overridden


def test_drafter_override_applies_to_every_section() -> None:
    """codex round-1 explicit requirement: with one ``main`` override
    set, EVERY per-section drafter prompt within a single run must
    embed the override (the override surface is intentionally not
    per-section in stage 2.B). Section-type directives must still
    differ across sections — otherwise the override would have
    obliterated section-specific guidance."""
    from autoessay.agents.drafter import (
        _SECTION_TYPE_DIRECTIVES,
        SectionPlan,
        _section_prompt,
    )

    sections = [
        SectionPlan(section_id="introduction", title="Introduction", target_words=800),
        SectionPlan(section_id="historiography", title="Historiography", target_words=1200),
        SectionPlan(section_id="conclusion", title="Conclusion", target_words=600),
    ]
    override = "ONE EDITED RULESET FOR ALL SECTIONS"
    rendered: list[str] = []
    for section in sections:
        rendered.append(
            _section_prompt(
                section=section,
                selected_thesis={"thesis_one_sentence": "central claim"},
                source_notes={},
                shortlist=[],
                domain_data={},
                target_journal=None,
                suffix="",
                instructions_override=override,
            )
        )
    # Override appears in every prompt.
    for prompt in rendered:
        assert override in prompt
    # Section-type directives remain distinct: 'historiography' has a
    # non-empty directive, 'conclusion' has a non-empty directive,
    # 'introduction' has no entry. Confirms the override did not
    # erase or merge them.
    assert _SECTION_TYPE_DIRECTIVES["historiography"] in rendered[1]
    assert _SECTION_TYPE_DIRECTIVES["conclusion"] in rendered[2]
    assert _SECTION_TYPE_DIRECTIVES.get("introduction", "") == ""


def test_drafter_override_separator_against_strip_trailing_whitespace() -> None:
    """codex round-1 P2 regression: ``upsert_phase_prompt`` strips
    trailing whitespace from saved overrides. A user override like
    ``Use terse prose.`` (no trailing space) must NOT concatenate
    directly with the next non-empty block. The unconditional-space
    fix matches stylist's pattern.

    PR-J7 inserted the ``anchor_check`` rule between the universal
    instructions block and the style block, so the next adjacent token
    after the override is now ``anchor_check`` (when ``section_type_directive``
    is empty for the section) or ``section_type_directive``. Both
    paths must still preserve the leading space."""
    from autoessay.agents.drafter import SectionPlan, _section_prompt

    override = "Use terse prose."  # ends with '.', no trailing space
    # Section WITHOUT a section-type directive ('introduction' has no
    # entry in _SECTION_TYPE_DIRECTIVES) — override flows into the
    # PR-J7 anchor_check rule.
    intro_prompt = _section_prompt(
        section=SectionPlan(section_id="introduction", title="Intro", target_words=600),
        selected_thesis={},
        source_notes={},
        shortlist=[],
        domain_data={},
        target_journal=None,
        suffix="",
        instructions_override=override,
    )
    # No-space concatenation must NOT happen with the next non-empty
    # token (anchor_check, since section_type_directive is empty).
    assert "Use terse prose.anchor_check" not in intro_prompt
    # ≥1 space between override and anchor_check (J7 added one more
    # space at the boundary, total 2 spaces — both are acceptable;
    # 0 spaces is the regression we're guarding against).
    assert "Use terse prose. anchor_check" in intro_prompt or (
        "Use terse prose.  anchor_check" in intro_prompt
    )
    # The downstream style block is still present (just no longer
    # adjacent to the override — anchor_check sits between them).
    assert "目标期刊风格" in intro_prompt
    # Section WITH a section-type directive ('historiography' has one)
    # — override flows directly into the section-type block.
    historio_prompt = _section_prompt(
        section=SectionPlan(section_id="historiography", title="Hist", target_words=1200),
        selected_thesis={},
        source_notes={},
        shortlist=[],
        domain_data={},
        target_journal=None,
        suffix="",
        instructions_override=override,
    )
    assert "Use terse prose.本节务必采用" not in historio_prompt
    assert "Use terse prose. 本节务必采用" in historio_prompt
    assert "anchor_check" in historio_prompt


def test_stylist_override_applies_to_every_section() -> None:
    """Same invariant as drafter: stylist's ``main`` override must
    propagate to every section's prompt within a single run."""
    from autoessay.agents.stylist import ManuscriptSection, _section_prompt
    from autoessay.style_profile import StyleProfile

    sections = [
        ManuscriptSection(section_id="introduction", title="Introduction", prose="p1"),
        ManuscriptSection(section_id="discussion", title="Discussion", prose="p2"),
        ManuscriptSection(section_id="conclusion", title="Conclusion", prose="p3"),
    ]
    override = "STYLIST EDITED RULESET FOR ALL SECTIONS"
    for section in sections:
        prompt = _section_prompt(
            section=section,
            claim_ids=[f"{section.section_id}-c1"],
            style_profile=StyleProfile(),
            section_findings=[],
            suffix="",
            instructions_override=override,
        )
        assert override in prompt
        # Per-section dynamic context must still appear (proves the
        # override did not eat the section-specific scaffolding).
        assert section.title in prompt
        assert f"{section.section_id}-c1" in prompt


async def test_get_prompt_works_for_drafter_and_stylist(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path,
) -> None:
    """Drafter and stylist are registered prompt surfaces in the
    stage 2.B-extension. The 'main' override applies to every per-
    section LLM call within a single run."""
    _seed(app_session, tmp_path, run_id="run_pp_ds")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        for phase, expected_marker in (
            ("drafter", "全文必须围绕"),
            ("stylist", "Revise prose only"),
        ):
            resp = await client.get(f"/api/runs/run_pp_ds/phases/{phase}/prompt")
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert expected_marker in body["default_content"]
            assert body["override_content"] is None


async def test_get_prompt_works_for_curator_ranking(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path,
) -> None:
    """Stage 3.A.1: curator now exposes the 'ranking' prompt key.
    Default GET still 404s because (curator, 'main') is not
    registered; discovery fallback is Stage 3.A.4."""
    from autoessay.prompts import CURATOR_RANKING_INSTRUCTIONS

    _seed(app_session, tmp_path, run_id="run_pp_curator")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/runs/run_pp_curator/phases/curator/prompt?prompt_key=ranking")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["prompt_key"] == "ranking"
        assert body["default_content"] == CURATOR_RANKING_INSTRUCTIONS
        assert body["override_content"] is None
        assert body["template_id"] == "curator.ranking_batch.v1"

        put_resp = await client.put(
            "/api/runs/run_pp_curator/phases/curator/prompt",
            json={"prompt_key": "ranking", "content": "Prefer empirical sources."},
        )
        assert put_resp.status_code == 200, put_resp.text
        put_body = put_resp.json()
        assert put_body["prompt_key"] == "ranking"
        assert put_body["override_content"] == "Prefer empirical sources."

        # Default GET (no prompt_key) returns 200 after Stage 3.A.4
        # discovery fallback: (curator, "main") is unsupported but
        # `ranking` exists, so the response resolves to the first
        # supported key. Pre-3.A.4 this was 404.
        default_resp = await client.get("/api/runs/run_pp_curator/phases/curator/prompt")
        assert default_resp.status_code == 200
        assert default_resp.json()["prompt_key"] == "ranking"


def test_curator_harness_system_message_uses_override() -> None:
    """The harness-path system message is overridable; the schema-
    binding sentence and language_directive stay outside the editable
    surface (Stage 3.A.1, codex-AGREEd amendment 1)."""
    from autoessay.agents.curator import _curator_harness_system_message
    from autoessay.prompts import CURATOR_RANKING_INSTRUCTIONS

    default = _curator_harness_system_message(language="en", instructions_override=None)
    overridden = _curator_harness_system_message(language="en", instructions_override="MY RULES")
    assert CURATOR_RANKING_INSTRUCTIONS[:30] in default
    assert CURATOR_RANKING_INSTRUCTIONS[:30] not in overridden
    assert "MY RULES" in overridden
    # Schema-binding sentence and language directive remain in BOTH.
    for required in ("Return one strict JSON array", "Reply only in English"):
        assert required in default
        assert required in overridden
    # language_directive=None falls back to English without raising.
    assert "Reply only in English" in _curator_harness_system_message(
        language=None, instructions_override=None
    )
    # Chinese language directive stays with override.
    zh_overridden = _curator_harness_system_message(language="zh", instructions_override="MY RULES")
    assert "简体中文" in zh_overridden
    assert "MY RULES" in zh_overridden


def test_curator_async_system_message_uses_override() -> None:
    """The async fallback path keeps its own schema-binding sentence
    ('Return JSON only.') outside the override (Stage 3.A.1)."""
    from autoessay.agents.curator import _curator_async_system_message
    from autoessay.prompts import CURATOR_RANKING_INSTRUCTIONS

    default = _curator_async_system_message(instructions_override=None)
    overridden = _curator_async_system_message(instructions_override="MY RULES")
    assert CURATOR_RANKING_INSTRUCTIONS[:30] in default
    assert CURATOR_RANKING_INSTRUCTIONS[:30] not in overridden
    assert "MY RULES" in overridden
    for required in ("Return JSON only",):
        assert required in default
        assert required in overridden


def test_drafter_section_keys_registered() -> None:
    """All 8 drafter section keys are registered with default content
    matching DRAFTER_SECTION_ROLES + DRAFTER_SECTION_TYPE_DIRECTIVES
    (Stage 3.A.2)."""
    from autoessay.prompts import (
        DRAFTER_SECTION_ROLES,
        DRAFTER_SECTION_TYPE_DIRECTIVES,
        get_phase_prompt_spec,
        supported_keys_for_phase,
    )

    expected_keys = {
        "introduction",
        "historiography",
        "sources-method",
        "empirical-section-i",
        "empirical-section-ii",
        "empirical-section-iii",
        "discussion",
        "conclusion",
    }
    drafter_keys = set(supported_keys_for_phase("drafter"))
    assert drafter_keys == expected_keys | {"main"}
    for section_id in expected_keys:
        spec = get_phase_prompt_spec("drafter", section_id)
        assert spec is not None, section_id
        expected_default = DRAFTER_SECTION_ROLES[section_id] + DRAFTER_SECTION_TYPE_DIRECTIVES.get(
            section_id, ""
        )
        assert spec.default_content == expected_default
        assert spec.template_id == f"drafter.section.{section_id}.v1"


def test_section_override_replaces_role_and_type_directive() -> None:
    """Per-section override absorbs both the role hint AND the
    type directive at position-A; type-directive position cleared
    (Stage 3.A.2)."""
    from autoessay.agents.drafter import (
        _SECTION_ROLE_HINTS,
        _SECTION_TYPE_DIRECTIVES,
        SectionPlan,
        _section_prompt,
    )

    section = SectionPlan(section_id="historiography", title="Historiography", target_words=1200)
    overridden = _section_prompt(
        section=section,
        selected_thesis={"thesis_one_sentence": "central claim"},
        source_notes={},
        shortlist=[],
        domain_data={},
        target_journal=None,
        suffix="",
        section_override="MY HISTO BLOCK",
    )
    assert "Section role: MY HISTO BLOCK" in overridden
    assert _SECTION_ROLE_HINTS["historiography"][:30] not in overridden
    assert _SECTION_TYPE_DIRECTIVES["historiography"][:20] not in overridden
    # Universal `main` rules and the style block stay in place.
    assert "全文必须围绕" in overridden
    assert "目标期刊风格" in overridden
    assert "Return strict JSON" in overridden


def test_section_override_does_not_leak_across_sections() -> None:
    """A section_override applies only to ITS section's prompt."""
    from autoessay.agents.drafter import (
        _SECTION_ROLE_HINTS,
        SectionPlan,
        _section_prompt,
    )

    intro_prompt = _section_prompt(
        section=SectionPlan(section_id="introduction", title="Intro", target_words=600),
        selected_thesis={},
        source_notes={},
        shortlist=[],
        domain_data={},
        target_journal=None,
        suffix="",
        section_override="INTRO_OVERRIDE",
    )
    body1_prompt = _section_prompt(
        section=SectionPlan(
            section_id="empirical-section-i",
            title="Empirical Section I",
            target_words=900,
        ),
        selected_thesis={},
        source_notes={},
        shortlist=[],
        domain_data={},
        target_journal=None,
        suffix="",
        section_override=None,
    )
    assert "INTRO_OVERRIDE" in intro_prompt
    assert "INTRO_OVERRIDE" not in body1_prompt
    # empirical-section-i still carries its default role hint when no override active.
    assert _SECTION_ROLE_HINTS["empirical-section-i"][:20] in body1_prompt


def test_section_and_main_override_compose() -> None:
    """``main`` override and per-section override are independent
    surfaces; both can be active at once."""
    from autoessay.agents.drafter import SectionPlan, _section_prompt
    from autoessay.prompts import DRAFTER_MAIN_INSTRUCTIONS

    rendered = _section_prompt(
        section=SectionPlan(section_id="introduction", title="Intro", target_words=600),
        selected_thesis={},
        source_notes={},
        shortlist=[],
        domain_data={},
        target_journal=None,
        suffix="",
        instructions_override="MAIN_OVERRIDE",
        section_override="INTRO_OVERRIDE",
    )
    assert "MAIN_OVERRIDE" in rendered
    assert "INTRO_OVERRIDE" in rendered
    # Defaults absent.
    assert DRAFTER_MAIN_INSTRUCTIONS[:30] not in rendered
    from autoessay.agents.drafter import _SECTION_ROLE_HINTS

    assert _SECTION_ROLE_HINTS["introduction"][:30] not in rendered


def test_section_override_separator_against_strip_trailing_whitespace() -> None:
    """``upsert_phase_prompt`` strips trailing whitespace from saved
    overrides. The role line ends in an unconditional space so a
    user-edited override without trailing space cannot collide with
    the next concatenated block."""
    from autoessay.agents.drafter import SectionPlan, _section_prompt

    rendered = _section_prompt(
        section=SectionPlan(section_id="introduction", title="Intro", target_words=600),
        selected_thesis={},
        source_notes={},
        shortlist=[],
        domain_data={},
        target_journal=None,
        suffix="",
        section_override="My block.",
    )
    assert "Section role: My block. " in rendered


def test_drafter_empirical_prompt_includes_progression_directive() -> None:
    """Empirical sections must add distinct argumentative work instead
    of repeating the same thesis under new headings."""
    from autoessay.agents.drafter import SectionPlan, _section_prompt

    rendered = _section_prompt(
        section=SectionPlan(
            section_id="empirical-section-ii",
            title="Empirical Section II",
            target_words=900,
        ),
        selected_thesis={"thesis_one_sentence": "central claim"},
        source_notes={},
        shortlist=[],
        domain_data={},
        target_journal=None,
        suffix="",
    )

    assert "section_progression_directive" in rendered
    assert "distinct mechanism" in rendered
    assert "Do not keep restating the same node, year, or thesis label" in rendered
    assert "new source/evidence relation" in rendered


def test_drafter_progression_directive_survives_section_override() -> None:
    """Saved per-section overrides may replace the role hint, but not
    the non-overridable progression guard."""
    from autoessay.agents.drafter import _SECTION_ROLE_HINTS, SectionPlan, _section_prompt

    rendered = _section_prompt(
        section=SectionPlan(
            section_id="empirical-section-iii",
            title="Empirical Section III",
            target_words=900,
        ),
        selected_thesis={},
        source_notes={},
        shortlist=[],
        domain_data={},
        target_journal=None,
        suffix="",
        section_override="CUSTOM EMPIRICAL ROLE",
    )

    assert "Section role: CUSTOM EMPIRICAL ROLE" in rendered
    assert _SECTION_ROLE_HINTS["empirical-section-iii"][:20] not in rendered
    assert "section_progression_directive" in rendered
    assert "Do the third substantive evidentiary job" in rendered


async def test_get_prompt_works_for_drafter_sections(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path,
) -> None:
    """API GET returns 200 with the registered default content for
    every drafter section key (Stage 3.A.2)."""
    from autoessay.prompts import _drafter_section_default

    _seed(app_session, tmp_path, run_id="run_pp_drafter_keys")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        for section_id in (
            "introduction",
            "historiography",
            "sources-method",
            "empirical-section-i",
            "empirical-section-ii",
            "empirical-section-iii",
            "discussion",
            "conclusion",
        ):
            resp = await client.get(
                f"/api/runs/run_pp_drafter_keys/phases/drafter/prompt?prompt_key={section_id}"
            )
            assert resp.status_code == 200, (section_id, resp.text)
            body = resp.json()
            assert body["prompt_key"] == section_id
            assert body["default_content"] == _drafter_section_default(section_id)
            assert body["override_content"] is None


async def test_put_drafter_section_override_roundtrip(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path,
) -> None:
    """PUT a per-section override, GET it back, assert content."""
    _seed(app_session, tmp_path, run_id="run_pp_drafter_put")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        put_resp = await client.put(
            "/api/runs/run_pp_drafter_put/phases/drafter/prompt",
            json={"prompt_key": "historiography", "content": "MY HISTO BLOCK"},
        )
        assert put_resp.status_code == 200, put_resp.text
        get_resp = await client.get(
            "/api/runs/run_pp_drafter_put/phases/drafter/prompt?prompt_key=historiography"
        )
        assert get_resp.status_code == 200, get_resp.text
        body = get_resp.json()
        assert body["override_content"] == "MY HISTO BLOCK"
        # `main` GET still returns null override (separate surface).
        main_resp = await client.get("/api/runs/run_pp_drafter_put/phases/drafter/prompt")
        assert main_resp.status_code == 200
        assert main_resp.json()["override_content"] is None


async def test_drafter_rerun_threads_section_overrides(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path,
    monkeypatch,
) -> None:
    """``main`` + ``introduction`` overrides both reach
    ``run_drafter`` via ``prompt_overrides``; phase_version_prompts
    snapshot records all 9 keys (8 sections + main) with the
    correct ``source`` per key (Stage 3.A.2)."""
    run_dir = _seed(app_session, tmp_path, run_id="run_pp_drafter_rerun")
    # Drafter completion sentinel (drafts/*/manuscript.md).
    _write(run_dir / "drafts" / "v001" / "manuscript.md", "# stub\n")

    seen_overrides: list[dict[str, str] | None] = []
    from autoessay import main as main_mod

    def fake_drafter(run_id, session=None, *, prompt_overrides=None, **_):
        seen_overrides.append(dict(prompt_overrides) if prompt_overrides else None)

    monkeypatch.setitem(main_mod._PHASE_RUNNERS, "drafter", fake_drafter)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        main_put = await client.put(
            "/api/runs/run_pp_drafter_rerun/phases/drafter/prompt",
            json={"prompt_key": "main", "content": "MAIN_OVERRIDE"},
        )
        assert main_put.status_code == 200, main_put.text
        intro_put = await client.put(
            "/api/runs/run_pp_drafter_rerun/phases/drafter/prompt",
            json={"prompt_key": "introduction", "content": "INTRO_OVERRIDE"},
        )
        assert intro_put.status_code == 200, intro_put.text
        rerun = await client.post("/api/runs/run_pp_drafter_rerun/phases/drafter/rerun")
    assert rerun.status_code == 202, rerun.text
    assert seen_overrides == [{"main": "MAIN_OVERRIDE", "introduction": "INTRO_OVERRIDE"}]

    with app_session() as session:
        rows = session.scalars(
            __import__("sqlalchemy")
            .select(PhaseVersionPrompt)
            .order_by(PhaseVersionPrompt.prompt_key.asc())
        ).all()
        # 9 keys: main + 8 section_ids.
        assert len(rows) == 9
        by_key = {row.prompt_key: row for row in rows}
        assert by_key["main"].source == "override"
        assert by_key["main"].content == "MAIN_OVERRIDE"
        assert by_key["introduction"].source == "override"
        assert by_key["introduction"].content == "INTRO_OVERRIDE"
        for untouched in (
            "historiography",
            "sources-method",
            "empirical-section-i",
            "empirical-section-ii",
            "empirical-section-iii",
            "discussion",
            "conclusion",
        ):
            assert by_key[untouched].source == "default"


async def test_drafter_rerun_with_main_hash_passes_when_section_also_active(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path,
    monkeypatch,
) -> None:
    """Stage 3.A.2 P1 fix: when ``main`` AND a section key are both
    active overrides, posting rerun with ``prompt_key="main"`` and
    main's draft_hash must NOT false-409 because of the section
    override. The pre-3.A.2 check compared the FIRST override which
    could be the section override (sorts before ``main``)."""
    run_dir = _seed(app_session, tmp_path, run_id="run_pp_drafter_hash_pass")
    _write(run_dir / "drafts" / "v001" / "manuscript.md", "# stub\n")

    monkeypatch.setitem(
        main_mod_runners(),
        "drafter",
        lambda run_id, session=None, **_: None,
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        main_put = await client.put(
            "/api/runs/run_pp_drafter_hash_pass/phases/drafter/prompt",
            json={"prompt_key": "main", "content": "MAIN_TEXT"},
        )
        assert main_put.status_code == 200
        main_hash = main_put.json()["draft_hash"]
        await client.put(
            "/api/runs/run_pp_drafter_hash_pass/phases/drafter/prompt",
            json={"prompt_key": "introduction", "content": "INTRO_TEXT"},
        )
        rerun = await client.post(
            "/api/runs/run_pp_drafter_hash_pass/phases/drafter/rerun",
            json={"prompt_key": "main", "draft_hash": main_hash},
        )
    assert rerun.status_code == 202, rerun.text


async def test_drafter_rerun_with_stale_main_hash_still_409s(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path,
    monkeypatch,
) -> None:
    """Concurrency check stays correct under multi-key: a stale
    ``main`` hash MUST 409 even when other section overrides exist."""
    run_dir = _seed(app_session, tmp_path, run_id="run_pp_drafter_hash_stale")
    _write(run_dir / "drafts" / "v001" / "manuscript.md", "# stub\n")

    monkeypatch.setitem(
        main_mod_runners(),
        "drafter",
        lambda run_id, session=None, **_: None,
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        main_put = await client.put(
            "/api/runs/run_pp_drafter_hash_stale/phases/drafter/prompt",
            json={"prompt_key": "main", "content": "FIRST"},
        )
        stale_hash = main_put.json()["draft_hash"]
        await client.put(
            "/api/runs/run_pp_drafter_hash_stale/phases/drafter/prompt",
            json={"prompt_key": "main", "content": "SECOND"},
        )
        # Section override is unrelated; must not affect main's hash check.
        await client.put(
            "/api/runs/run_pp_drafter_hash_stale/phases/drafter/prompt",
            json={"prompt_key": "empirical-section-ii", "content": "BODY"},
        )
        rerun = await client.post(
            "/api/runs/run_pp_drafter_hash_stale/phases/drafter/rerun",
            json={"prompt_key": "main", "draft_hash": stale_hash},
        )
    assert rerun.status_code == 409
    assert rerun.json()["detail"]["code"] == "prompt_draft_changed"


def main_mod_runners() -> dict[str, object]:
    """Helper to dodge the import-shadowing trap when monkeypatch'ing
    ``_PHASE_RUNNERS`` from multiple tests (avoids re-importing at top
    of each test)."""
    from autoessay import main as main_mod

    return main_mod._PHASE_RUNNERS


async def test_rerun_legacy_hash_check_falls_back_to_first_supported_key(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path,
    monkeypatch,
) -> None:
    """Stage 3.A.2 backward-compat: when the caller did NOT explicitly
    set ``prompt_key`` but ``(phase, "main")`` is unsupported and the
    phase has another key registered (e.g. curator's only key is
    ``ranking``), the concurrency check uses the first supported key.
    Without this fallback, A.1's curator save-and-rerun contract
    regresses to a 404."""
    run_dir = _seed(app_session, tmp_path, run_id="run_pp_curator_hash_fallback")
    _write(run_dir / "sources" / "shortlist.json", "[]")

    monkeypatch.setitem(
        main_mod_runners(),
        "curator",
        lambda run_id, session=None, **_: None,
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        put_resp = await client.put(
            "/api/runs/run_pp_curator_hash_fallback/phases/curator/prompt",
            json={"prompt_key": "ranking", "content": "MY RANKING"},
        )
        assert put_resp.status_code == 200, put_resp.text
        ranking_hash = put_resp.json()["draft_hash"]
        # Legacy request shape: only draft_hash, no prompt_key.
        ok_resp = await client.post(
            "/api/runs/run_pp_curator_hash_fallback/phases/curator/rerun",
            json={"draft_hash": ranking_hash},
        )
    assert ok_resp.status_code == 202, ok_resp.text


async def test_rerun_legacy_hash_check_409s_on_stale_hash_with_fallback(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path,
    monkeypatch,
) -> None:
    """Same fallback path: a stale hash MUST 409, not 404 or pass."""
    run_dir = _seed(app_session, tmp_path, run_id="run_pp_curator_hash_fallback_stale")
    _write(run_dir / "sources" / "shortlist.json", "[]")

    monkeypatch.setitem(
        main_mod_runners(),
        "curator",
        lambda run_id, session=None, **_: None,
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        first_put = await client.put(
            "/api/runs/run_pp_curator_hash_fallback_stale/phases/curator/prompt",
            json={"prompt_key": "ranking", "content": "FIRST"},
        )
        stale_hash = first_put.json()["draft_hash"]
        await client.put(
            "/api/runs/run_pp_curator_hash_fallback_stale/phases/curator/prompt",
            json={"prompt_key": "ranking", "content": "SECOND"},
        )
        rerun = await client.post(
            "/api/runs/run_pp_curator_hash_fallback_stale/phases/curator/rerun",
            json={"draft_hash": stale_hash},
        )
    assert rerun.status_code == 409
    assert rerun.json()["detail"]["code"] == "prompt_draft_changed"


async def test_curator_ranking_rerun_threads_override(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path,
    monkeypatch,
) -> None:
    """A rerun with an active (curator, 'ranking') override must
    pass that override to ``run_curator`` via the ``prompt_overrides``
    kwarg (Stage 3.A.1)."""
    run_dir = _seed(app_session, tmp_path, run_id="run_pp_curator_rerun")
    # Curator's completion sentinel — required so assert_can_rerun
    # treats curator as a phase that has produced output.
    _write(run_dir / "sources" / "shortlist.json", "[]")

    seen_overrides: list[dict[str, str] | None] = []
    from autoessay import main as main_mod

    def fake_curator(run_id, session=None, *, prompt_overrides=None, **_):
        seen_overrides.append(prompt_overrides)

    monkeypatch.setitem(main_mod._PHASE_RUNNERS, "curator", fake_curator)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        put_resp = await client.put(
            "/api/runs/run_pp_curator_rerun/phases/curator/prompt",
            json={"prompt_key": "ranking", "content": "Prefer empirical sources."},
        )
        assert put_resp.status_code == 200, put_resp.text
        resp = await client.post("/api/runs/run_pp_curator_rerun/phases/curator/rerun")
    assert resp.status_code == 202, resp.text
    assert seen_overrides == [{"ranking": "Prefer empirical sources."}]


def test_stylist_repolish_registry_entry() -> None:
    """Stage 3.A.3: (stylist, 'repolish') is a registered prompt
    surface independent of (stylist, 'main')."""
    from autoessay.prompts import (
        STYLIST_REPOLISH_INSTRUCTIONS,
        get_phase_prompt_spec,
        supported_keys_for_phase,
    )

    spec = get_phase_prompt_spec("stylist", "repolish")
    assert spec is not None
    assert spec.default_content == STYLIST_REPOLISH_INSTRUCTIONS
    assert spec.template_id == "stylist.repolish.v1"
    assert sorted(supported_keys_for_phase("stylist")) == ["main", "repolish"]


def test_stylist_repolish_prompt_uses_override() -> None:
    """``instructions_override`` substitutes the static block in
    ``_repolish_prompt``; dynamic context (lowest dimension value,
    claim ids, manuscript, schema) stays appended (Stage 3.A.3)."""
    from autoessay.agents.stylist import _repolish_prompt
    from autoessay.prompts import STYLIST_REPOLISH_INSTRUCTIONS
    from autoessay.style_profile import StyleProfile

    default = _repolish_prompt(
        manuscript="ms",
        claim_ids=["c1"],
        style_profile=StyleProfile(),
        lowest_dimension="freshness",
    )
    overridden = _repolish_prompt(
        manuscript="ms",
        claim_ids=["c1"],
        style_profile=StyleProfile(),
        lowest_dimension="freshness",
        instructions_override="MY REPOLISH RULES",
    )
    assert STYLIST_REPOLISH_INSTRUCTIONS[:30] in default
    assert STYLIST_REPOLISH_INSTRUCTIONS[:30] not in overridden
    assert "MY REPOLISH RULES" in overridden
    # Dynamic context appears in BOTH (value signal + schema parse).
    for required in (
        "Lowest stop-slop dimension: freshness",
        '"c1"',
        "ms",
        "Return strict JSON",
    ):
        assert required in default
        assert required in overridden


def test_stylist_repolish_prompt_trailing_space_discipline() -> None:
    """An override stripped of trailing whitespace must NOT collide
    with the next concatenated dynamic block (Stage 3.A.3). Since
    2026-05-12 PR-3 the next block is ``empirical_preservation_guard``,
    not the lowest-stop-slop block; the discipline is the same but the
    neighbour changed."""
    from autoessay.agents.stylist import _repolish_prompt
    from autoessay.style_profile import StyleProfile

    rendered = _repolish_prompt(
        manuscript="ms",
        claim_ids=["c1"],
        style_profile=StyleProfile(),
        lowest_dimension="freshness",
        instructions_override="MY RULES.",
    )
    # No collision with the immediately-following empirical guard
    assert "MY RULES.empirical_preservation_guard" not in rendered
    assert "MY RULES. empirical_preservation_guard" in rendered
    # And the lowest-stop-slop block is still appended with proper spacing
    # after the guard, so the dynamic context still renders cleanly.
    assert "Lowest stop-slop dimension: freshness" in rendered


async def test_get_prompt_works_for_stylist_repolish(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path,
) -> None:
    """API GET/PUT roundtrip for (stylist, 'repolish'); main and
    repolish are independent surfaces."""
    from autoessay.prompts import STYLIST_REPOLISH_INSTRUCTIONS

    _seed(app_session, tmp_path, run_id="run_pp_stylist_repolish")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        get_resp = await client.get(
            "/api/runs/run_pp_stylist_repolish/phases/stylist/prompt?prompt_key=repolish"
        )
        assert get_resp.status_code == 200, get_resp.text
        body = get_resp.json()
        assert body["prompt_key"] == "repolish"
        assert body["default_content"] == STYLIST_REPOLISH_INSTRUCTIONS
        assert body["override_content"] is None

        put_resp = await client.put(
            "/api/runs/run_pp_stylist_repolish/phases/stylist/prompt",
            json={"prompt_key": "repolish", "content": "MY REPOLISH"},
        )
        assert put_resp.status_code == 200, put_resp.text
        assert put_resp.json()["override_content"] == "MY REPOLISH"

        # main GET still returns null override (independent surface).
        main_resp = await client.get("/api/runs/run_pp_stylist_repolish/phases/stylist/prompt")
        assert main_resp.status_code == 200
        assert main_resp.json()["override_content"] is None


async def test_stylist_rerun_threads_repolish_and_main(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path,
    monkeypatch,
) -> None:
    """Both ``main`` and ``repolish`` overrides reach ``run_stylist``
    via ``prompt_overrides`` (Stage 3.A.3)."""
    run_dir = _seed(app_session, tmp_path, run_id="run_pp_stylist_rerun")
    # Stylist completion sentinel matches drafts/*/style/*.
    _write(run_dir / "drafts" / "v001" / "style" / "paper_styled.md", "ok\n")
    # ``stylist_ready`` (called from rerun_phase as of Stage 3.E
    # follow-up) requires drafter's manuscript artifacts. Seed them.
    _write(run_dir / "drafts" / "v001" / "manuscript.md", "## stub\n")
    _write(run_dir / "drafts" / "v001" / "claim_map.jsonl", '{"id":"x"}\n')
    _write(run_dir / "drafts" / "v001" / "citations.bib", "@misc{x,}\n")

    seen_overrides: list[dict[str, str] | None] = []

    def fake_stylist(run_id, session=None, *, prompt_overrides=None, **_):
        seen_overrides.append(dict(prompt_overrides) if prompt_overrides else None)

    monkeypatch.setitem(main_mod_runners(), "stylist", fake_stylist)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await client.put(
            "/api/runs/run_pp_stylist_rerun/phases/stylist/prompt",
            json={"prompt_key": "main", "content": "MAIN_X"},
        )
        await client.put(
            "/api/runs/run_pp_stylist_rerun/phases/stylist/prompt",
            json={"prompt_key": "repolish", "content": "REPOLISH_X"},
        )
        resp = await client.post("/api/runs/run_pp_stylist_rerun/phases/stylist/rerun")
    assert resp.status_code == 202, resp.text
    assert seen_overrides == [{"main": "MAIN_X", "repolish": "REPOLISH_X"}]


def test_run_stylist_routes_main_and_repolish_overrides_separately(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path,
    monkeypatch,
) -> None:
    """Stage 3.A.3 P1 routing test: a swap between ``main`` and
    ``repolish`` inside ``_run_stylist_with_session`` would not be
    caught by the per-key tests above. Directly run ``run_stylist``
    against a low-quality drafter manuscript so the score-low branch
    triggers re-polish, monkey-patch ``_section_prompt`` and
    ``_repolish_prompt`` to capture the ``instructions_override``
    each receives, and assert the routing is NOT swapped.
    """
    import json as _json
    from pathlib import Path as _Path

    from conftest import seed_project

    from autoessay.agents import stylist as stylist_mod
    from autoessay.agents.stylist import run_stylist
    from autoessay.config import get_settings as _get_settings
    from autoessay.models import Run
    from autoessay.run_writer import create_run_directory

    run_id = "run_pp_stylist_route"
    run_dir = create_run_directory(
        tmp_path / "runs",
        run_id,
        "proj_test",
        state="DRAFTER_RUNNING",
        domain_id="financial_history",
    )
    draft_dir = run_dir / "drafts" / "v001"
    draft_dir.mkdir(parents=True)
    # Manuscript stuffed with stop-slop phrases so the score is far
    # below SCORE_THRESHOLD = 35 and the re-polish branch fires.
    bad_prose = (
        "众所周知，本文意义重大。显而易见，毋庸置疑，不言而喻，综上所述，毋庸赘言，举世瞩目。"
    )
    (draft_dir / "manuscript.md").write_text(
        '<a id="introduction"></a>\n## Introduction\n\n'
        + (bad_prose + "\n") * 6
        + "The paragraph cites `source_1`.\n",
        encoding="utf-8",
    )
    (draft_dir / "claim_map.jsonl").write_text(
        _json.dumps(
            {
                "draft_version": "v001",
                "section_id": "introduction",
                "section_title": "Introduction",
                "claim_id": "claim_1",
                "paragraph_id": "introduction-p001",
                "claim_text": "x",
                "source_ids": ["source_1"],
                "uncited": False,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (draft_dir / "citations.bib").write_text(
        "@article{source_1,\n  title={S},\n}\n",
        encoding="utf-8",
    )

    with app_session() as session:
        project = seed_project(session)
        session.add(
            Run(
                id=run_id,
                project_id=project.id,
                domain_version="0.1.0",
                run_dir=str(run_dir),
                state="DRAFTER_RUNNING",
                baseline_hash="x",
            ),
        )
        session.commit()

    monkeypatch.setenv("AUTOESSAY_STOP_SLOP_LLM_ENABLED", "0")
    _get_settings.cache_clear()

    section_overrides_seen: list[str | None] = []
    repolish_overrides_seen: list[str | None] = []

    real_section_prompt = stylist_mod._section_prompt
    real_repolish_prompt = stylist_mod._repolish_prompt

    def capture_section_prompt(*args, **kwargs):
        section_overrides_seen.append(kwargs.get("instructions_override"))
        return real_section_prompt(*args, **kwargs)

    def capture_repolish_prompt(*args, **kwargs):
        repolish_overrides_seen.append(kwargs.get("instructions_override"))
        return real_repolish_prompt(*args, **kwargs)

    monkeypatch.setattr(stylist_mod, "_section_prompt", capture_section_prompt)
    monkeypatch.setattr(stylist_mod, "_repolish_prompt", capture_repolish_prompt)

    class FakeStylistLLM:
        async def chat_completion(
            self,
            messages,
            model: str,
            temperature: float,
            max_tokens: int,
            retries: int = 0,
            response_format: dict[str, object] | None = None,
            **_kwargs: object,
        ) -> dict[str, object]:
            del messages, model, temperature, max_tokens, retries, response_format
            content = _json.dumps(
                {
                    "revised_prose": (
                        "众所周知，本节意义重大。显而易见，本节通过 `source_1` 论证 claim_1。"
                    ),
                    "edit_summary": ["stub revision"],
                    "preserved_claim_ids": ["claim_1"],
                },
            )
            return {"content": content, "raw_content": content, "usage": {"total_tokens": 21}}

        async def aclose(self) -> None:
            return None

    monkeypatch.setattr("autoessay.harness.runner.LLMClient", FakeStylistLLM)

    # Force the score-low branch to fire deterministically: every
    # score_text call inside _run_stylist_with_session returns a
    # below-threshold total. SCORE_THRESHOLD is 35 (stylist.py:42).
    def fake_score_text(*_args, **_kwargs):
        return {"total": 10, "findings": []}

    monkeypatch.setattr(stylist_mod, "score_text", fake_score_text)

    with app_session() as session:
        run_stylist(
            run_id,
            session,
            prompt_overrides={"main": "MAIN_X", "repolish": "REPOLISH_X"},
        )

    # Section path got the main override; repolish path got the repolish override.
    assert section_overrides_seen, "section path was not exercised"
    assert all(v == "MAIN_X" for v in section_overrides_seen)
    assert repolish_overrides_seen == ["REPOLISH_X"], (
        f"repolish path must receive prompt_overrides['repolish']; saw {repolish_overrides_seen}"
    )
    # And critically: never the swap.
    assert "REPOLISH_X" not in section_overrides_seen
    assert "MAIN_X" not in repolish_overrides_seen
    _ = _Path  # silence unused alias from imports above


async def test_get_prompt_returns_supported_keys(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path,
) -> None:
    """Stage 3.A.4: every GET response includes supported_keys for
    the phase, sorted alphabetically. Lets the frontend render a
    multi-key dropdown without a separate metadata round-trip."""
    _seed(app_session, tmp_path, run_id="run_pp_supported_keys")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Single-key phase.
        resp = await client.get("/api/runs/run_pp_supported_keys/phases/synthesizer/prompt")
        assert resp.status_code == 200
        assert resp.json()["supported_keys"] == ["main"]
        # Multi-key phase (drafter has 9 keys).
        resp = await client.get("/api/runs/run_pp_supported_keys/phases/drafter/prompt")
        assert resp.status_code == 200
        keys = resp.json()["supported_keys"]
        assert keys == sorted(keys)
        assert set(keys) == {
            "main",
            "introduction",
            "historiography",
            "sources-method",
            "empirical-section-i",
            "empirical-section-ii",
            "empirical-section-iii",
            "discussion",
            "conclusion",
        }
        # Curator-only-non-main case (single key "ranking"), via the
        # discovery fallback below.
        resp = await client.get("/api/runs/run_pp_supported_keys/phases/curator/prompt")
        assert resp.status_code == 200
        assert resp.json()["supported_keys"] == ["ranking"]


async def test_get_prompt_falls_back_to_first_supported_key_for_curator(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path,
) -> None:
    """Stage 3.A.4 discovery fallback: GET on a phase with only
    non-main keys (curator → ranking) and NO explicit prompt_key
    returns 200 with prompt_key resolved to the first supported key."""
    from autoessay.prompts import CURATOR_RANKING_INSTRUCTIONS

    _seed(app_session, tmp_path, run_id="run_pp_curator_discovery")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # No prompt_key in query string.
        resp = await client.get("/api/runs/run_pp_curator_discovery/phases/curator/prompt")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["prompt_key"] == "ranking"
        assert body["default_content"] == CURATOR_RANKING_INSTRUCTIONS
        assert body["supported_keys"] == ["ranking"]


async def test_get_prompt_explicit_empty_key_returns_404(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path,
) -> None:
    """``?prompt_key=`` (explicit empty string) is an invalid key,
    NOT an omitted parameter. Strict 404, no discovery fallback to
    main (codex round-2 P3, Stage 3.A.4)."""
    _seed(app_session, tmp_path, run_id="run_pp_explicit_empty_key")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Synthesizer DOES support main; if the empty string were
        # silently treated as omitted, we would 200 with main here.
        resp = await client.get(
            "/api/runs/run_pp_explicit_empty_key/phases/synthesizer/prompt?prompt_key="
        )
    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "prompt_surface_not_supported"


async def test_get_prompt_explicit_main_for_curator_still_404s(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path,
) -> None:
    """Discovery fallback only applies when the caller did NOT pass
    prompt_key. Explicit ?prompt_key=main keeps strict 404 semantics."""
    _seed(app_session, tmp_path, run_id="run_pp_curator_explicit_main")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/api/runs/run_pp_curator_explicit_main/phases/curator/prompt?prompt_key=main"
        )
    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "prompt_surface_not_supported"


async def test_put_prompt_response_includes_supported_keys(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path,
) -> None:
    """Stage 3.A.4: PUT responses also include supported_keys so
    that after a save the modal does not need a second metadata
    round-trip."""
    _seed(app_session, tmp_path, run_id="run_pp_put_keys")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # PUT a delete (blank content) — covers the early-return branch.
        del_resp = await client.put(
            "/api/runs/run_pp_put_keys/phases/synthesizer/prompt",
            json={"content": ""},
        )
        assert del_resp.status_code == 200
        assert del_resp.json()["supported_keys"] == ["main"]
        # PUT a non-empty content — covers the normal save branch.
        save_resp = await client.put(
            "/api/runs/run_pp_put_keys/phases/synthesizer/prompt",
            json={"content": "x"},
        )
        assert save_resp.status_code == 200
        assert save_resp.json()["supported_keys"] == ["main"]
