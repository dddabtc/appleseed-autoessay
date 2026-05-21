"""PR-C2.a: framework_lens helpers + agent unit + integration tests."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from conftest import seed_project
from sqlalchemy import select

from autoessay.agents.framework_lens import run_framework_lens
from autoessay.framework_lens import (
    FRAMEWORK_LENS_ARTIFACT_PATH,
    LensSignal,
    build_synthesizer_input_ref,
    compose_framework_lens,
    framework_lens_skip_failure_guidance,
    has_theoretical_lens_inputs,
    lens_names_from_payload,
    read_framework_lens,
    resolve_framework_lens_summary_ref,
    should_run_framework_lens,
    write_framework_lens,
)
from autoessay.models import Run
from autoessay.run_writer import create_run_directory

# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_has_theoretical_lens_inputs_dual_track() -> None:
    dual = {"theoretical_lens_track": [{"text": "anything"}]}
    assert has_theoretical_lens_inputs(dual_track=dual, shortlist=[])


def test_has_theoretical_lens_inputs_shortlist_tag() -> None:
    shortlist = [
        {"source_id": "s1", "research_role": "secondary_argument"},
        {"source_id": "s2", "research_role": "theoretical_lens"},
    ]
    assert has_theoretical_lens_inputs(dual_track=None, shortlist=shortlist)


def test_has_theoretical_lens_inputs_returns_false_when_neither() -> None:
    shortlist = [
        {"source_id": "s1", "research_role": "secondary_argument"},
    ]
    assert not has_theoretical_lens_inputs(
        dual_track={"theoretical_lens_track": []},
        shortlist=shortlist,
    )


def test_should_run_skips_when_no_lens_inputs_and_not_theory_article() -> None:
    assert not should_run_framework_lens(
        paper_mode="case_analysis",
        dual_track={"theoretical_lens_track": []},
        shortlist=[],
    )
    assert not should_run_framework_lens(
        paper_mode="empirical",
        dual_track=None,
        shortlist=[{"source_id": "s1", "research_role": "secondary_argument"}],
    )


def test_should_run_runs_when_lens_track_has_claims() -> None:
    assert should_run_framework_lens(
        paper_mode="case_analysis",
        dual_track={"theoretical_lens_track": [{"text": "x"}]},
        shortlist=[],
    )


def test_should_run_runs_when_shortlist_has_lens_tag() -> None:
    assert should_run_framework_lens(
        paper_mode="case_analysis",
        dual_track={"theoretical_lens_track": []},
        shortlist=[
            {"source_id": "s_lens", "research_role": "theoretical_lens"},
        ],
    )


def test_should_run_theory_article_always_returns_true() -> None:
    """Codex amendment 2: theory_article cannot silently skip."""
    assert should_run_framework_lens(
        paper_mode="theory_article",
        dual_track=None,
        shortlist=[],
    )


def test_skip_failure_guidance_for_theory_article_is_localized() -> None:
    msg = framework_lens_skip_failure_guidance("theory_article")
    assert "theoretical_lens" in msg
    # zh + en bilingual
    assert "理论论文模式" in msg
    assert "theory_article" in msg.lower() or "theoretical_lens" in msg


def test_skip_failure_guidance_empty_for_other_modes() -> None:
    assert framework_lens_skip_failure_guidance("case_analysis") == ""
    assert framework_lens_skip_failure_guidance("empirical") == ""


def test_compose_framework_lens_stub_emits_signal_per_lens_source() -> None:
    shortlist = [
        {
            "source_id": "lens_bourdieu",
            "title": "Bourdieu: Outline of a Theory of Practice",
            "venue": "Cambridge UP",
            "research_role": "theoretical_lens",
        },
        {
            "source_id": "secondary_xyz",
            "title": "Some article",
            "venue": "Journal X",
            "research_role": "secondary_argument",
        },
    ]
    payload = compose_framework_lens(
        shortlist=shortlist,
        dual_track=None,
        paper_mode="case_analysis",
        stub=True,
    )
    assert payload["schema_version"] == 2
    assert payload["paper_mode"] == "case_analysis"
    assert payload["synthesizer_input_ref"] == {}
    signals = payload["signals"]
    assert isinstance(signals, list)
    assert len(signals) == 1
    sig = signals[0]
    assert sig["source_id"] == "lens_bourdieu"
    assert "key_concepts" in sig
    assert isinstance(sig["key_concepts"], list)


def test_compose_framework_lens_stub_is_deterministic() -> None:
    shortlist = [
        {
            "source_id": "lens_a",
            "title": "Alpha",
            "research_role": "theoretical_lens",
        }
    ]
    a = compose_framework_lens(
        shortlist=shortlist, dual_track=None, paper_mode="case_analysis", stub=True
    )
    b = compose_framework_lens(
        shortlist=shortlist, dual_track=None, paper_mode="case_analysis", stub=True
    )
    assert a == b


def test_lens_names_from_payload_returns_set() -> None:
    payload = {
        "signals": [
            {"lens_name": "Habitus theory"},
            {"lens_name": "Field theory"},
            {"lens_name": "Habitus theory"},  # dup
        ]
    }
    names = lens_names_from_payload(payload)
    assert names == {"Habitus theory", "Field theory"}


def test_lens_names_from_payload_returns_empty_for_none() -> None:
    assert lens_names_from_payload(None) == set()
    assert lens_names_from_payload({}) == set()


def test_write_and_read_framework_lens_roundtrip(tmp_path: Path) -> None:
    payload = {
        "schema_version": 2,
        "paper_mode": "theory_article",
        "synthesizer_input_ref": {
            "synthesizer_pv_id": "pv_synth",
            "synthesizer_artifact_hash": "abc123",
        },
        "signals": [
            {
                "lens_name": "lens",
                "key_concepts": ["c1"],
                "source_id": "s1",
                "applicability_to_kernel": "x",
            }
        ],
    }
    run_dir = tmp_path / "run_x"
    run_dir.mkdir()
    target = write_framework_lens(run_dir, payload)
    assert target.exists()
    loaded = read_framework_lens(run_dir)
    assert loaded == payload


def test_build_synthesizer_input_ref_hashes_artifact(tmp_path: Path) -> None:
    run_dir = tmp_path / "run_a"
    (run_dir / "synthesis").mkdir(parents=True)
    synth_payload = '{"schema_version": 1, "primary_track": []}\n'
    (run_dir / "synthesis" / "synthesizer.json").write_text(
        synth_payload,
        encoding="utf-8",
    )
    ref = build_synthesizer_input_ref(run_dir, synthesizer_pv_id="pv_synth_1")
    assert ref == {
        "synthesizer_pv_id": "pv_synth_1",
        "synthesizer_artifact_hash": hashlib.sha256(synth_payload.encode()).hexdigest(),
    }


def test_resolve_framework_lens_summary_ref_prefers_lens_artifact(tmp_path: Path) -> None:
    run_dir = tmp_path / "run_ref"
    (run_dir / "synthesis").mkdir(parents=True)
    (run_dir / FRAMEWORK_LENS_ARTIFACT_PATH).write_text(
        json.dumps({"schema_version": 2, "signals": []}),
        encoding="utf-8",
    )
    ref = resolve_framework_lens_summary_ref(
        run_dir,
        synthesizer_payload={"framework_lens_summary_ref": "legacy/wrong.json"},
    )
    assert ref == FRAMEWORK_LENS_ARTIFACT_PATH


def test_resolve_framework_lens_summary_ref_falls_back_to_legacy_hook(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "run_legacy_ref"
    run_dir.mkdir()
    ref = resolve_framework_lens_summary_ref(
        run_dir,
        synthesizer_payload={"framework_lens_summary_ref": "synthesis/framework_lens.json"},
    )
    assert ref == FRAMEWORK_LENS_ARTIFACT_PATH


def test_lens_signal_dataclass_immutable() -> None:
    s = LensSignal(
        lens_name="x",
        key_concepts=("a", "b"),
        source_id="src",
        applicability_to_kernel="...",
    )
    # frozen=True dataclass raises FrozenInstanceError on attribute set.
    with pytest.raises(AttributeError):
        s.lens_name = "y"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Agent integration test (state machine + artifact)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_framework_lens_writes_schema_v2_artifact_without_synthesizer_hook(
    app_session,  # type: ignore[no-untyped-def]
    monkeypatch,
    tmp_path: Path,
) -> None:
    """End-to-end: from USER_FIELD_REVIEW the agent walks to
    USER_LENS_REVIEW, writes synthesis/framework_lens.json, and does
    not mutate synthesizer.json."""
    monkeypatch.setenv("AUTOESSAY_FRAMEWORK_LENS_STUB", "1")
    # PR-C2c: is_stub_enabled() now reads from cached Settings; clear
    # the cache after monkeypatch so the agent picks up the new env.
    from autoessay.config import get_settings as _get_settings

    _get_settings.cache_clear()
    run_id = "run_lens_smoke"
    run_dir = create_run_directory(
        tmp_path / "runs",
        run_id,
        "proj_test",
        state="USER_FIELD_REVIEW",
        domain_id="financial_history",
    )
    # Seed a shortlist with a theoretical_lens-tagged source.
    (run_dir / "sources").mkdir(parents=True, exist_ok=True)
    (run_dir / "sources" / "shortlist.json").write_text(
        json.dumps(
            [
                {
                    "source_id": "lens_bourdieu",
                    "title": "Bourdieu: Outline",
                    "research_role": "theoretical_lens",
                }
            ]
        ),
        encoding="utf-8",
    )
    # Seed an existing synthesizer.json. The lens phase may read/hash it
    # but must not write a downstream hook into it.
    (run_dir / "synthesis").mkdir(parents=True, exist_ok=True)
    synth_path = run_dir / "synthesis" / "synthesizer.json"
    synth_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "primary_track": [],
                "secondary_track": [],
                "theoretical_lens_track": [],
                "methodological_track": [],
                "tension_summary_ref": None,
            }
        ),
        encoding="utf-8",
    )
    synth_before = synth_path.read_text(encoding="utf-8")

    with app_session() as session:
        seed_project(session)
        session.add(
            Run(
                id=run_id,
                project_id="proj_test",
                domain_version="0.1.0",
                run_dir=str(run_dir),
                state="USER_FIELD_REVIEW",
                baseline_hash="t",
                paper_mode="theory_article",
                research_kernel_json={"kernel_schema_version": 1},
            )
        )
        session.commit()
        result = run_framework_lens(run_id, session)
        run = session.scalar(select(Run).where(Run.id == run_id))

    assert run is not None
    assert run.state == "USER_LENS_REVIEW"
    assert result["signals"] == 1

    # Artifact exists and is well-formed.
    flens = json.loads((run_dir / "synthesis" / "framework_lens.json").read_text(encoding="utf-8"))
    assert flens["schema_version"] == 2
    assert flens["paper_mode"] == "theory_article"
    assert len(flens["signals"]) == 1
    assert flens["synthesizer_input_ref"]["synthesizer_pv_id"] is None
    assert (
        flens["synthesizer_input_ref"]["synthesizer_artifact_hash"]
        == hashlib.sha256(synth_before.encode()).hexdigest()
    )

    # synthesizer.json is unchanged by the framework_lens phase.
    assert synth_path.read_text(encoding="utf-8") == synth_before


# ---------------------------------------------------------------------------
# GET /api/runs/{id}/framework_lens (PR-C2.b Tier 4: lens-tab data fetch)
# ---------------------------------------------------------------------------


def test_get_framework_lens_returns_signals_when_artifact_present(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path: Path,
) -> None:
    from autoessay.main import get_framework_lens

    run_id = "run_lens_get_present"
    run_dir = create_run_directory(
        tmp_path / "runs",
        run_id,
        "proj_test",
        state="USER_LENS_REVIEW",
        domain_id="financial_history",
    )
    (run_dir / "synthesis").mkdir(parents=True, exist_ok=True)
    (run_dir / "synthesis" / "framework_lens.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "paper_mode": "theory_article",
                "synthesizer_input_ref": {
                    "synthesizer_pv_id": "pv_synth_1",
                    "synthesizer_artifact_hash": "abc123",
                },
                "signals": [
                    {
                        "lens_name": "Bourdieu's habitus",
                        "key_concepts": ["habitus", "capital"],
                        "source_id": "openalex_W1",
                        "applicability_to_kernel": "Maps practice patterns onto field constraints.",
                    },
                    {
                        "lens_name": "Polanyi's embeddedness",
                        "key_concepts": ["embeddedness"],
                        "source_id": "openalex_W2",
                        "applicability_to_kernel": "Frames market action as socially embedded.",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    with app_session() as session:
        seed_project(session)
        session.add(
            Run(
                id=run_id,
                project_id="proj_test",
                domain_version="0.1.0",
                run_dir=str(run_dir),
                state="USER_LENS_REVIEW",
                baseline_hash="t",
            ),
        )
        session.commit()

        body = get_framework_lens(run_id, session).dict()
    assert body["artifact_present"] is True
    assert body["schema_version"] == 2
    assert body["synthesizer_input_ref"] == {
        "synthesizer_pv_id": "pv_synth_1",
        "synthesizer_artifact_hash": "abc123",
    }
    assert len(body["signals"]) == 2
    assert body["signals"][0]["lens_name"] == "Bourdieu's habitus"
    assert body["signals"][0]["key_concepts"] == ["habitus", "capital"]
    assert body["signals"][1]["source_id"] == "openalex_W2"


def test_get_framework_lens_returns_empty_when_artifact_missing(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path: Path,
) -> None:
    from autoessay.main import get_framework_lens

    run_id = "run_lens_get_missing"
    run_dir = create_run_directory(
        tmp_path / "runs",
        run_id,
        "proj_test",
        state="USER_FIELD_REVIEW",
        domain_id="financial_history",
    )
    with app_session() as session:
        seed_project(session)
        session.add(
            Run(
                id=run_id,
                project_id="proj_test",
                domain_version="0.1.0",
                run_dir=str(run_dir),
                state="USER_FIELD_REVIEW",
                baseline_hash="t",
            ),
        )
        session.commit()

        body = get_framework_lens(run_id, session).dict()
    assert body["artifact_present"] is False
    assert body["signals"] == []
    assert body["schema_version"] is None


def test_get_synthesis_derives_lens_summary_ref_from_lens_artifact(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path: Path,
) -> None:
    from autoessay.main import get_synthesis

    run_id = "run_synthesis_lens_ref"
    run_dir = create_run_directory(
        tmp_path / "runs",
        run_id,
        "proj_test",
        state="USER_LENS_REVIEW",
        domain_id="financial_history",
    )
    (run_dir / "synthesis").mkdir(parents=True, exist_ok=True)
    (run_dir / "synthesis" / "synthesizer.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "primary_track": [],
                "secondary_track": [],
                "theoretical_lens_track": [],
                "methodological_track": [],
                "tension_summary_ref": None,
            }
        ),
        encoding="utf-8",
    )
    (run_dir / "synthesis" / "framework_lens.json").write_text(
        json.dumps({"schema_version": 2, "synthesizer_input_ref": {}, "signals": []}),
        encoding="utf-8",
    )
    with app_session() as session:
        seed_project(session)
        session.add(
            Run(
                id=run_id,
                project_id="proj_test",
                domain_version="0.1.0",
                run_dir=str(run_dir),
                state="USER_LENS_REVIEW",
                baseline_hash="t",
            ),
        )
        session.commit()

        body = get_synthesis(run_id, session).dict()
    assert body["dual_track"]["framework_lens_summary_ref"] == FRAMEWORK_LENS_ARTIFACT_PATH
