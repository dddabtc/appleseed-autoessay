import json
from pathlib import Path

from conftest import seed_project

from autoessay.agents import stylist
from autoessay.agents.stylist import run_stylist
from autoessay.config import get_settings
from autoessay.models import Run
from autoessay.run_writer import create_run_directory
from autoessay.style_profile import StyleProfile


def test_stylist_ngram_guard_rejects_prior_paper_overlap(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path: Path,
    monkeypatch,
) -> None:
    run_id, run_dir = _seed_single_section_draft(app_session, tmp_path)
    monkeypatch.setenv("AUTOESSAY_STOP_SLOP_LLM_ENABLED", "0")
    get_settings.cache_clear()
    example = "alpha beta gamma delta epsilon zeta"

    def fake_profile(*_args, **_kwargs) -> StyleProfile:  # type: ignore[no-untyped-def]
        return StyleProfile(short_local_examples=[example])

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
                        "alpha beta gamma delta epsilon appears in the revised prose "
                        "with `source_1`."
                    ),
                    "edit_summary": ["Copied local rhythm."],
                    "preserved_claim_ids": ["claim_1"],
                },
            )
            return {"content": content, "raw_content": content, "usage": {"total_tokens": 24}}

        async def aclose(self) -> None:
            return None

    monkeypatch.setattr(stylist, "build_style_profile", fake_profile)
    monkeypatch.setattr("autoessay.harness.runner.LLMClient", OverlapStylistLLM)

    with app_session() as session:
        summary = run_stylist(run_id, session)
        run = session.get(Run, run_id)

    style_dir = run_dir / "drafts" / "v001" / "style"
    styled = (style_dir / "paper_styled.md").read_text(encoding="utf-8")
    violations = (style_dir / "n_gram_violations.json").read_text(encoding="utf-8")

    assert run is not None
    assert run.state == "USER_REVISION_REVIEW"
    assert summary["state"] == "USER_REVISION_REVIEW"
    assert "alpha beta gamma delta epsilon" not in styled
    assert "The claim links deposit insurance" in styled
    assert "alpha beta gamma delta epsilon" in violations


def _seed_single_section_draft(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path: Path,
) -> tuple[str, Path]:
    run_id = "run_stylist_ngram_guard"
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
        )
        + "\n",
        encoding="utf-8",
    )
    (draft_dir / "citations.bib").write_text(
        "@article{source_1,\n  title={Source One},\n}\n",
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
                baseline_hash="test",
            ),
        )
        session.commit()
    return run_id, run_dir
