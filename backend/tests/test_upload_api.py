import json
from pathlib import Path

from conftest import seed_project
from httpx import ASGITransport, AsyncClient

from autoessay.main import app
from autoessay.models import Run
from autoessay.phase_version import _purge_owned_files
from autoessay.run_writer import create_run_directory


async def test_upload_pdf_new_source_updates_manifest_and_shortlist(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path: Path,
) -> None:
    run_id = "run_upload_pdf"
    run_dir = create_run_directory(
        tmp_path / "runs",
        run_id,
        "proj_test",
        state="USER_DEEP_DIVE_REVIEW",
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
                state="USER_DEEP_DIVE_REVIEW",
                baseline_hash="test",
            ),
        )
        session.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        upload_response = await client.post(
            f"/api/runs/{run_id}/sources/upload",
            data={
                "source_id": "new",
                "title": "Uploaded Paper",
                "authors": "Ada Author; Bert Writer",
                "year": "2024",
                "doi": "10.1234/uploaded",
                "url": "https://example.test/uploaded",
            },
            files={"pdf": ("uploaded.pdf", b"%PDF-1.4 upload", "application/pdf")},
        )
        sources_response = await client.get(f"/api/runs/{run_id}/sources")

    assert upload_response.status_code == 201
    payload = upload_response.json()
    source_id = payload["source_id"]
    assert source_id.startswith("user_")
    assert sources_response.status_code == 200
    sources = sources_response.json()
    assert source_id in sources["fulltext_manifest"]
    assert any(item["source_id"] == source_id for item in sources["shortlist"])
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        pdf_response = await client.get(f"/api/runs/{run_id}/sources/{source_id}/pdf")

    manifest = json.loads(
        (run_dir / "sources" / "fulltext_manifest.json").read_text(encoding="utf-8"),
    )
    shortlist = json.loads((run_dir / "sources" / "shortlist.json").read_text(encoding="utf-8"))
    assert source_id in manifest
    assert any(item["source_client"] == "user_upload" for item in shortlist)
    assert pdf_response.status_code == 200
    assert pdf_response.headers["content-type"].startswith("application/pdf")


async def test_uploaded_pdf_survives_curator_owned_file_purge(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path: Path,
) -> None:
    run_id = "run_upload_pdf_purge"
    run_dir = create_run_directory(
        tmp_path / "runs",
        run_id,
        "proj_test",
        state="USER_DEEP_DIVE_REVIEW",
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
                state="USER_DEEP_DIVE_REVIEW",
                baseline_hash="test",
            ),
        )
        session.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        upload_response = await client.post(
            f"/api/runs/{run_id}/sources/upload",
            data={
                "source_id": "new",
                "title": "Persistent Upload",
                "authors": "Ada Author",
            },
            files={"pdf": ("uploaded.pdf", b"%PDF-1.4 upload", "application/pdf")},
        )
    assert upload_response.status_code == 201
    source_id = upload_response.json()["source_id"]
    upload_path = run_dir / "sources" / "uploads" / f"{source_id}.pdf"
    assert upload_path.is_file()

    _purge_owned_files(run_dir, "curator")

    assert upload_path.is_file()
    assert (run_dir / "sources" / "user_upload_manifest.json").is_file()
    assert (run_dir / "sources" / "user_upload_sources.json").is_file()
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        sources_response = await client.get(f"/api/runs/{run_id}/sources")
        pdf_response = await client.get(f"/api/runs/{run_id}/sources/{source_id}/pdf")

    assert sources_response.status_code == 200
    sources = sources_response.json()
    assert source_id in sources["fulltext_manifest"]
    assert any(item["source_id"] == source_id for item in sources["shortlist"])
    assert pdf_response.status_code == 200
    assert pdf_response.headers["content-type"].startswith("application/pdf")


async def test_upload_pdf_rejects_non_pdf(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path: Path,
) -> None:
    run_id = "run_upload_reject"
    run_dir = create_run_directory(
        tmp_path / "runs",
        run_id,
        "proj_test",
        state="USER_DEEP_DIVE_REVIEW",
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
                state="USER_DEEP_DIVE_REVIEW",
                baseline_hash="test",
            ),
        )
        session.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            f"/api/runs/{run_id}/sources/upload",
            data={"source_id": "new", "title": "Bad Upload"},
            files={"pdf": ("bad.txt", b"not a pdf", "text/plain")},
        )

    assert response.status_code == 400


async def test_upload_pdf_rejected_during_running_state(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path: Path,
) -> None:
    # Round-1 audit #21: source upload mid-flight would race the
    # running curator/synthesizer's snapshot read of the corpus.
    run_id = "run_upload_run_guard"
    run_dir = create_run_directory(
        tmp_path / "runs",
        run_id,
        "proj_test",
        state="CURATOR_RUNNING",
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
                state="CURATOR_RUNNING",
                baseline_hash="test",
            ),
        )
        session.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            f"/api/runs/{run_id}/sources/upload",
            data={"source_id": "new", "title": "Mid-flight upload"},
            files={"pdf": ("uploaded.pdf", b"%PDF-1.4 upload", "application/pdf")},
        )

    assert response.status_code == 409
    assert "currently running" in response.json()["detail"]
