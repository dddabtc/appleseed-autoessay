import hashlib
import json
from pathlib import Path

from conftest import seed_styled_run

from autoessay.agents.critic import run_critic
from autoessay.agents.exporter import run_exports
from autoessay.config import get_settings
from autoessay.models import Checkpoint, Run, utcnow
from autoessay.state_machine import transition


def test_run_exports_writes_all_formats_and_manifest_hashes(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path: Path,
    monkeypatch,
) -> None:
    run_id, run_dir = _seed_final_acceptance(app_session, tmp_path, monkeypatch, "run_exports_ok")

    with app_session() as session:
        summary = run_exports(run_id, session)
        run = session.get(Run, run_id)

    exports_dir = run_dir / "exports"
    manifest = json.loads((exports_dir / "manifest.json").read_text(encoding="utf-8"))

    assert run is not None
    assert run.state == "EXPORTS_DONE"
    assert summary["state"] == "EXPORTS_DONE"
    assert (exports_dir / "manuscript.md").exists()
    assert (exports_dir / "manuscript.docx").exists()
    assert (exports_dir / "manuscript.html").exists()
    assert (exports_dir / "citations.bib").exists()
    assert (exports_dir / "citations.csl.json").exists()
    for payload in manifest["files"].values():
        path = run_dir / payload["path"]
        assert payload["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()


def _seed_final_acceptance(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path: Path,
    monkeypatch,
    run_id: str,
) -> tuple[str, Path]:
    run_id, run_dir = seed_styled_run(app_session, tmp_path, monkeypatch, run_id)
    monkeypatch.setenv("AUTOESSAY_CRITIC_STUB", "1")
    get_settings.cache_clear()
    with app_session() as session:
        run_critic(run_id, session)
        run = session.get(Run, run_id)
        assert run is not None
        transition(
            run,
            "USER_FINAL_ACCEPTANCE",
            session,
            reason="test final acceptance",
        )
        session.add(
            Checkpoint(
                id=f"checkpoint_final_{run_id}",
                run_id=run_id,
                checkpoint_type="USER_FINAL_ACCEPTANCE",
                status="ACCEPTED",
                decision_payload=json.dumps(
                    {
                        "accept": True,
                        "export_formats": ["markdown", "docx", "html", "bibtex", "csl_json"],
                    },
                    sort_keys=True,
                ),
                decided_at=utcnow(),
            ),
        )
        session.commit()
    return run_id, run_dir
