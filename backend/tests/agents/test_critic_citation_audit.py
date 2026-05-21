import json
from pathlib import Path

from conftest import seed_styled_run

from autoessay.agents.critic import run_critic
from autoessay.config import get_settings


def test_critic_citation_audit_blocks_source_without_stable_reference(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path: Path,
    monkeypatch,
) -> None:
    run_id, run_dir = seed_styled_run(app_session, tmp_path, monkeypatch, "run_critic_audit")
    monkeypatch.setenv("AUTOESSAY_CRITIC_STUB", "1")
    get_settings.cache_clear()

    shortlist_path = run_dir / "sources" / "shortlist.json"
    shortlist = json.loads(shortlist_path.read_text(encoding="utf-8"))
    first_source_id = shortlist[0]["source_id"]
    shortlist[0]["doi"] = None
    shortlist[0]["url"] = None
    shortlist[0]["source_client"] = "crossref"
    shortlist[0]["access_status"] = "metadata_only"
    shortlist[0]["license"] = None
    shortlist[0]["risk_flags"] = []
    shortlist_path.write_text(
        json.dumps(shortlist, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    claim_map_path = run_dir / "drafts" / "v001" / "claim_map.jsonl"
    claims = [json.loads(line) for line in claim_map_path.read_text(encoding="utf-8").splitlines()]
    claims[0]["source_ids"] = [first_source_id]
    claim_map_path.write_text(
        "".join(json.dumps(claim, sort_keys=True) + "\n" for claim in claims),
        encoding="utf-8",
    )

    with app_session() as session:
        run_critic(run_id, session)

    audit_rows = [
        json.loads(line)
        for line in (run_dir / "reviews" / "claim_audit.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
        if line
    ]
    blocking = json.loads(
        (run_dir / "reviews" / "blocking_issues.json").read_text(encoding="utf-8")
    )

    blocked = [row for row in audit_rows if row["paragraph_id"] == claims[0]["paragraph_id"]]
    assert blocked
    assert blocked[0]["status"] == "BLOCKER"
    assert any(issue["suggested_action"] == "VERIFY_CITATION" for issue in blocking["issues"])
