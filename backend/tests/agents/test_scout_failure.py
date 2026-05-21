import json
from pathlib import Path

import pytest
from conftest import seed_project

from autoessay.agents import scout
from autoessay.agents.scout import run_scout
from autoessay.clients._stubs import StubLitClient
from autoessay.clients.common import ClientSearchError, NormalizedSource
from autoessay.config import get_settings
from autoessay.models import Run, utcnow
from autoessay.run_writer import create_run_directory
from autoessay.state_machine import RunCancelled


class FailingClient(StubLitClient):
    async def search(
        self,
        query: str,
        year_window: int | tuple[int, int] | None,
        limit: int,
    ) -> list[NormalizedSource]:
        del year_window, limit
        raise ClientSearchError(self.source_id, query, "synthetic outage")


def test_run_scout_continues_with_partial_sources_and_warning_artifact(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("AUTOESSAY_SCOUT_STUB", "1")
    get_settings.cache_clear()
    monkeypatch.setattr(scout, "get_lit_client", _patched_client)
    run_id = "run_scout_partial"
    run_dir = create_run_directory(
        tmp_path / "runs",
        run_id,
        "proj_test",
        state="DOMAIN_LOADED",
        domain_id="financial_history",
    )

    with app_session() as session:
        project = seed_project(session)
        session.add(
            Run(
                id=run_id,
                project_id=project.id,
                domain_version="0.1.0",
                run_dir=str(run_dir),
                state="DOMAIN_LOADED",
                baseline_hash="test",
            ),
        )
        session.commit()

        summary = run_scout(run_id, session)
        run = session.get(Run, run_id)

    warnings = _read_jsonl(run_dir / "discovery" / "warnings.jsonl")
    candidates = _read_jsonl(run_dir / "discovery" / "skim_candidates.jsonl")

    assert run is not None
    assert run.state == "USER_SEARCH_REVIEW"
    assert summary["warnings"] > 0
    assert candidates
    assert any(warning["source_id"] == "semantic_scholar" for warning in warnings)


def test_run_scout_all_source_failure_enters_failed_vendor(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("AUTOESSAY_SCOUT_STUB", "1")
    get_settings.cache_clear()
    monkeypatch.setattr(
        scout,
        "get_lit_client",
        lambda source_id, source_config=None, domain_config=None: FailingClient(source_id),
    )
    run_id = "run_scout_failed_vendor"
    run_dir = create_run_directory(
        tmp_path / "runs",
        run_id,
        "proj_test",
        state="DOMAIN_LOADED",
        domain_id="financial_history",
    )

    with app_session() as session:
        project = seed_project(session)
        session.add(
            Run(
                id=run_id,
                project_id=project.id,
                domain_version="0.1.0",
                run_dir=str(run_dir),
                state="DOMAIN_LOADED",
                baseline_hash="test",
            ),
        )
        session.commit()

        summary = run_scout(run_id, session)
        run = session.get(Run, run_id)

    warnings = _read_jsonl(run_dir / "discovery" / "warnings.jsonl")

    assert run is not None
    assert run.state == "FAILED_VENDOR"
    assert summary["state"] == "FAILED_VENDOR"
    assert warnings


def test_run_scout_honors_cancel_before_progress_event_write(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("AUTOESSAY_SCOUT_STUB", "1")
    get_settings.cache_clear()
    run_id = "run_scout_cancel_mid_loop"
    run_dir = create_run_directory(
        tmp_path / "runs",
        run_id,
        "proj_test",
        state="DOMAIN_LOADED",
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
                state="DOMAIN_LOADED",
                baseline_hash="test",
            ),
        )
        session.commit()

        async def canceling_search_one(**kwargs):  # type: ignore[no-untyped-def]
            del kwargs
            run = session.get(Run, run_id)
            assert run is not None
            run.cancel_requested_at = utcnow()
            session.commit()
            return {
                "client": StubLitClient("openalex"),
                "source_id": "openalex",
                "query": "cancel",
                "sources": [],
                "error": None,
            }

        monkeypatch.setattr(scout, "_search_one", canceling_search_one)

        with pytest.raises(RunCancelled):
            run_scout(run_id, session)
        run = session.get(Run, run_id)

    assert run is not None
    assert run.state == "CANCELLED"
    assert not (run_dir / "discovery" / "skim_candidates.jsonl").exists()


def _patched_client(source_id: str, source_config=None, domain_config=None):  # type: ignore[no-untyped-def]
    del source_config, domain_config
    if source_id == "semantic_scholar":
        return FailingClient(source_id)
    return StubLitClient(source_id)


def _read_jsonl(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
