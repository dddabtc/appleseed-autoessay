import json
from pathlib import Path

from conftest import seed_project
from sqlalchemy import select

from autoessay.agents import stylist
from autoessay.agents.stylist import run_stylist
from autoessay.config import get_settings
from autoessay.models import Project, ProviderCall, Run
from autoessay.run_writer import create_run_directory
from autoessay.style_profile import StyleProfile


class DroppedClaimStylistLLM:
    async def chat_completion(
        self,
        messages,  # type: ignore[no-untyped-def]
        model: str,
        temperature: float,
        max_tokens: int,
        retries: int = 0,
        response_format: dict[str, object] | None = None,
        **_kwargs: object,
    ) -> dict[str, object]:
        del messages, model, temperature, max_tokens, retries, response_format
        content = json.dumps(
            {
                "revised_prose": "The revised paragraph still cites `source_1`.",
                "edit_summary": ["Dropped claim preservation."],
                "preserved_claim_ids": [],
            },
        )
        return {"content": content, "raw_content": content, "usage": {"total_tokens": 21}}

    async def aclose(self) -> None:
        return None


class DroppedCitationStylistLLM:
    async def chat_completion(
        self,
        messages,  # type: ignore[no-untyped-def]
        model: str,
        temperature: float,
        max_tokens: int,
        retries: int = 0,
        response_format: dict[str, object] | None = None,
        **_kwargs: object,
    ) -> dict[str, object]:
        del messages, model, temperature, max_tokens, retries, response_format
        content = json.dumps(
            {
                "revised_prose": "The revised paragraph removes the citation key.",
                "edit_summary": ["Dropped a citation."],
                "preserved_claim_ids": ["claim_1"],
            },
        )
        return {"content": content, "raw_content": content, "usage": {"total_tokens": 22}}

    async def aclose(self) -> None:
        return None


class OverlapStylistLLM:
    async def chat_completion(
        self,
        messages,  # type: ignore[no-untyped-def]
        model: str,
        temperature: float,
        max_tokens: int,
        retries: int = 0,
        response_format: dict[str, object] | None = None,
        **_kwargs: object,
    ) -> dict[str, object]:
        del messages, model, temperature, max_tokens, retries, response_format
        content = json.dumps(
            {
                "revised_prose": (
                    "alpha beta gamma delta epsilon appears in revised prose with `source_1`."
                ),
                "edit_summary": ["Copied a local example."],
                "preserved_claim_ids": ["claim_1"],
            },
        )
        return {"content": content, "raw_content": content, "usage": {"total_tokens": 23}}

    async def aclose(self) -> None:
        return None


def test_stylist_harness_claim_preservation_hook_fails_fixable(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path: Path,
    monkeypatch,
) -> None:
    _seed_single_section_stylist_run(app_session, tmp_path, run_id="run_stylist_harness_claim")
    _enable_harness(monkeypatch)
    monkeypatch.setattr("autoessay.harness.runner.LLMClient", DroppedClaimStylistLLM)

    with app_session() as session:
        summary = run_stylist("run_stylist_harness_claim", session)
        run = session.get(Run, "run_stylist_harness_claim")

    assert run is not None
    assert run.state == "FAILED_FIXABLE"
    assert summary["state"] == "FAILED_FIXABLE"
    assert "claim_1" in str(summary["guidance"])


def test_stylist_harness_citation_preservation_hook_fails_fixable(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path: Path,
    monkeypatch,
) -> None:
    _seed_single_section_stylist_run(app_session, tmp_path, run_id="run_stylist_harness_citation")
    _enable_harness(monkeypatch)
    monkeypatch.setattr("autoessay.harness.runner.LLMClient", DroppedCitationStylistLLM)

    with app_session() as session:
        summary = run_stylist("run_stylist_harness_citation", session)
        run = session.get(Run, "run_stylist_harness_citation")

    assert run is not None
    assert run.state == "FAILED_FIXABLE"
    assert summary["state"] == "FAILED_FIXABLE"
    assert "source_1" in str(summary["guidance"])


def test_stylist_harness_ngram_guard_hook_rejects_and_reverts(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path: Path,
    monkeypatch,
) -> None:
    run_dir = _seed_single_section_stylist_run(
        app_session,
        tmp_path,
        run_id="run_stylist_harness_ngram",
    )
    _enable_harness(monkeypatch)
    monkeypatch.setattr("autoessay.harness.runner.LLMClient", OverlapStylistLLM)

    def fake_profile(*_args, **_kwargs) -> StyleProfile:  # type: ignore[no-untyped-def]
        return StyleProfile(short_local_examples=["alpha beta gamma delta epsilon zeta"])

    monkeypatch.setattr(stylist, "build_style_profile", fake_profile)

    with app_session() as session:
        summary = run_stylist("run_stylist_harness_ngram", session)
        provider_calls = list(
            session.scalars(
                select(ProviderCall).where(ProviderCall.run_id == "run_stylist_harness_ngram"),
            ),
        )

    style_dir = run_dir / "drafts" / "v001" / "style"
    styled = (style_dir / "paper_styled.md").read_text(encoding="utf-8")
    violations = json.loads((style_dir / "n_gram_violations.json").read_text(encoding="utf-8"))

    assert summary["state"] == "USER_REVISION_REVIEW"
    assert provider_calls[0].status == "rejected_fallback_used"
    assert "alpha beta gamma delta epsilon" not in styled
    assert "The claim links deposit insurance" in styled
    assert violations[0]["overlaps"] == ["alpha beta gamma delta epsilon"]


def _enable_harness(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("AUTOESSAY_STYLIST_STUB", "0")
    monkeypatch.setenv("AUTOESSAY_STOP_SLOP_LLM_ENABLED", "0")
    get_settings.cache_clear()


def _seed_single_section_stylist_run(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path: Path,
    *,
    run_id: str,
) -> Path:
    run_dir = create_run_directory(
        tmp_path / "runs",
        run_id,
        "proj_test",
        state="DRAFTER_RUNNING",
        domain_id="financial_history",
    )
    draft_dir = run_dir / "drafts" / "v001"
    draft_dir.mkdir(parents=True)
    (draft_dir / "manuscript.md").write_text(
        '<a id="introduction"></a>\n'
        "## Introduction\n\n"
        "The claim links deposit insurance to bank behavior through `source_1`.\n",
        encoding="utf-8",
    )
    (draft_dir / "claim_map.jsonl").write_text(
        json.dumps(
            {
                "draft_version": "v001",
                "section_id": "introduction",
                "section_title": "Introduction",
                "claim_id": "claim_1",
                "paragraph_id": "introduction-p001",
                "claim_text": "Deposit insurance changed bank behavior.",
                "source_ids": ["source_1"],
                "uncited": False,
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    (draft_dir / "citations.bib").write_text(
        "@article{source_1,\n  title={Source One},\n}\n",
        encoding="utf-8",
    )
    with app_session() as session:
        project = session.get(Project, "proj_test")
        if project is None:
            project = seed_project(session)
        session.add(project)
        session.add(
            Run(
                id=run_id,
                project_id=project.id,
                domain_version="0.1.0",
                run_dir=str(run_dir),
                state="DRAFTER_RUNNING",
                baseline_hash="test",
            ),
        )
        session.commit()
    return run_dir
