import json
from pathlib import Path

from conftest import seed_styled_run

from autoessay.agents.critic import run_critic
from autoessay.agents.exporter import run_exports
from autoessay.config import get_settings
from autoessay.models import Run
from autoessay.state_machine import transition


def test_run_exports_blocks_unresolved_blocking_issues_without_outputs(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path: Path,
    monkeypatch,
) -> None:
    run_id, run_dir = seed_styled_run(app_session, tmp_path, monkeypatch, "run_exports_blocked")
    monkeypatch.setenv("AUTOESSAY_CRITIC_STUB", "1")
    get_settings.cache_clear()

    with app_session() as session:
        run_critic(run_id, session)
        run = session.get(Run, run_id)
        assert run is not None
        transition(run, "USER_FINAL_ACCEPTANCE", session, reason="test final acceptance")
        session.commit()

    blocking_path = run_dir / "reviews" / "blocking_issues.json"
    blocking_path.write_text(
        json.dumps(
            {
                "issues": [
                    {
                        "issue_id": "blocker_1",
                        "severity": "BLOCKER",
                        "description": "citation X has no DOI or URL",
                    },
                ],
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )

    with app_session() as session:
        summary = run_exports(run_id, session)
        run = session.get(Run, run_id)

    assert run is not None
    assert run.state == "FAILED_POLICY"
    assert summary["state"] == "FAILED_POLICY"
    assert not (run_dir / "exports" / "manifest.json").exists()
    assert not (run_dir / "exports" / "manuscript.md").exists()


def test_run_exports_auto_polish_retry_can_clear_blocking_issue(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path: Path,
    monkeypatch,
) -> None:
    run_id, run_dir = seed_styled_run(app_session, tmp_path, monkeypatch, "run_exports_retry")
    monkeypatch.setenv("AUTOESSAY_CRITIC_STUB", "1")
    get_settings.cache_clear()

    with app_session() as session:
        run_critic(run_id, session)
        run = session.get(Run, run_id)
        assert run is not None
        transition(run, "USER_FINAL_ACCEPTANCE", session, reason="test final acceptance")
        session.commit()

    blocking_path = run_dir / "reviews" / "blocking_issues.json"
    blocking_path.write_text(
        json.dumps(
            {
                "issues": [
                    {
                        "issue_id": "overclaim_1",
                        "severity": "BLOCKER",
                        "description": "Conclusion overclaims the available evidence.",
                    },
                ],
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )

    calls: list[dict[str, object]] = []

    def fake_retry(**kwargs: object) -> dict[str, object]:
        calls.append(dict(kwargs))
        return {"status": "rewritten", "rewrite_version": "v002"}

    monkeypatch.setattr("autoessay.agents.exporter.attempt_exports_policy_polish_retry", fake_retry)

    with app_session() as session:
        summary = run_exports(run_id, session)
        run = session.get(Run, run_id)

    assert run is not None
    assert run.state == "EXPORTS_DONE"
    assert summary["state"] == "EXPORTS_DONE"
    assert len(calls) == 1
    manifest = json.loads((run_dir / "exports" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["exports_policy_polish_retries"][0]["repair_result"]["status"] == "rewritten"
    blocking = json.loads(blocking_path.read_text(encoding="utf-8"))
    assert blocking["issues"][0]["resolved_by"] == "auto_polish_retry"


def test_run_exports_honors_user_force_approve_paragraph_resolution(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Regression: PR #91 force-approve marked blocking_issues entries
    as ``resolved_by="user_force_approve"``, but the exporter then
    re-ran ``run_citation_audit`` against claim_map.jsonl and
    re-generated the same audit_blockers, producing a FAILED_POLICY
    loop. Exporter must skip audit blockers whose paragraph_id was
    already user-resolved.
    """
    run_id, run_dir = seed_styled_run(
        app_session, tmp_path, monkeypatch, "run_exports_force_approved"
    )
    monkeypatch.setenv("AUTOESSAY_CRITIC_STUB", "1")
    get_settings.cache_clear()

    # Move run to USER_FINAL_ACCEPTANCE and seed claim_map with one
    # uncited claim (the audit will flag it). Mark that paragraph
    # in blocking_issues.json as user-resolved via force-approve.
    with app_session() as session:
        run_critic(run_id, session)
        run = session.get(Run, run_id)
        assert run is not None
        transition(run, "USER_FINAL_ACCEPTANCE", session, reason="test final acceptance")
        session.commit()

    # Inject an uncited claim that the audit would otherwise reject.
    draft_dir = run_dir / "drafts" / "v001"
    claim_map_path = draft_dir / "claim_map.jsonl"
    existing = claim_map_path.read_text(encoding="utf-8") if claim_map_path.exists() else ""
    claim_map_path.write_text(
        existing
        + json.dumps(
            {
                "draft_version": "v001",
                "section_id": "discussion",
                "section_title": "Discussion",
                "paragraph_id": "discussion-p001",
                "source_ids": ["[UNCITED]"],
                "uncited": True,
                "claim_text": "stub paragraph from drafter retry exhaustion",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    # Mark the BLOCKER as user-force-approved.
    blocking_path = run_dir / "reviews" / "blocking_issues.json"
    blocking_path.write_text(
        json.dumps(
            {
                "issues": [
                    {
                        "issue_id": "audit_discussion-p001_001",
                        "severity": "BLOCKER",
                        "paragraph_id": "discussion-p001",
                        "description": "claim has no source_ids",
                        "dimension": "evidence",
                        "source_ids": [],
                        "suggested_action": "VERIFY_CITATION",
                        "resolved": True,
                        "resolved_by": "user_force_approve",
                        "resolved_reason": "stub is acceptable for this draft",
                    },
                ],
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )

    with app_session() as session:
        summary = run_exports(run_id, session)
        run = session.get(Run, run_id)

    assert run is not None, "run must still exist"
    assert run.state == "EXPORTS_DONE", (
        f"force-approve must let exports complete, got state={run.state}"
    )
    assert summary["state"] == "EXPORTS_DONE"
    assert (run_dir / "exports" / "manifest.json").exists()
