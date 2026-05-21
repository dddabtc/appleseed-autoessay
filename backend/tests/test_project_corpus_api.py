"""API tests for the per-project corpus endpoints introduced by
PR-B1 + audit-fixes (PR #114).

Endpoints under test:

- ``GET /api/projects/{id}/corpus`` — list project-scoped docs +
  every global corpus the user owns, with `is_selected` resolved
  from the explicit `project_corpus_selections` table.
- ``PUT /api/projects/{id}/corpus/selection`` — replace the set
  of global corpora this project includes.
- ``POST /api/projects/{id}/corpus/upload`` — upload a prior
  paper into the project-scoped corpus.

Codex audit 2026-05-01 flagged these endpoints as untested; this
file closes that gap.
"""

from io import BytesIO
from pathlib import Path

from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from autoessay.main import app
from autoessay.models import (
    Corpus,
    CorpusDocument,
    ProjectCorpusSelection,
)


async def _create_project(client: AsyncClient, title: str = "Corpus API test") -> str:
    response = await client.post(
        "/api/projects",
        json={
            "title": title,
            "domain_id": "financial_history",
            "language": "en",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _seed_global_corpus(
    app_session,  # type: ignore[no-untyped-def]
    user_id: str,
    *,
    name: str,
    corpus_id: str | None = None,
    enabled: bool = True,
) -> str:
    from autoessay.models import User

    with app_session() as session:
        if session.get(User, user_id) is None:
            session.add(User(id=user_id, display_name=user_id))
            session.flush()
        cid = corpus_id or f"corp_{name.replace(' ', '_')}"
        session.add(
            Corpus(
                id=cid,
                owner_user_id=user_id,
                user_id=user_id,
                project_id=None,
                name=name,
                enabled=enabled,
            ),
        )
        session.commit()
        return cid


async def test_get_project_corpus_lists_globals_with_selection(
    app_session,  # type: ignore[no-untyped-def]
) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Seed a global corpus BEFORE the project so create_project's
        # auto-include will pick it up.
        _seed_global_corpus(app_session, user_id="single-user", name="Global A")
        project_id = await _create_project(client)
        # Add a SECOND global AFTER the project — auto-include will
        # NOT have run, so the new global will be unselected for
        # this project until the user opts in.
        _seed_global_corpus(app_session, user_id="single-user", name="Global B")

        response = await client.get(f"/api/projects/{project_id}/corpus")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["project_id"] == project_id
    assert body["project_corpus_id"] is None  # no upload yet
    assert body["project_documents"] == []
    names = {entry["name"]: entry["is_selected"] for entry in body["global_corpora"]}
    assert names == {"Global A": True, "Global B": False}


async def test_put_project_corpus_selection_replaces_rows(
    app_session,  # type: ignore[no-untyped-def]
) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        _seed_global_corpus(app_session, user_id="single-user", name="A")
        b_id = _seed_global_corpus(app_session, user_id="single-user", name="B")
        project_id = await _create_project(client)
        # After auto-include, both A and B are selected.

        # Replace selection with only B.
        response = await client.put(
            f"/api/projects/{project_id}/corpus/selection",
            json={"global_corpus_ids": [b_id]},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["selected_global_corpus_ids"] == [b_id]

    # Verify in DB: only one selection row remains, and it points
    # at corpus B.
    with app_session() as session:
        rows = list(
            session.scalars(
                select(ProjectCorpusSelection).where(
                    ProjectCorpusSelection.project_id == project_id,
                ),
            ),
        )
    assert len(rows) == 1
    assert rows[0].corpus_id == b_id


async def test_put_selection_rejects_non_global_corpus(
    app_session,  # type: ignore[no-untyped-def]
) -> None:
    """Selection endpoint must reject any corpus_id that is not a
    GLOBAL corpus owned by the user. Project-scoped corpora and
    other-user corpora are equally invalid."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        project_id = await _create_project(client)
        # Manually create a project-scoped corpus on this project,
        # then try to "select" it as a global — must 400.
        with app_session() as session:
            session.add(
                Corpus(
                    id="corp_scoped",
                    owner_user_id="single-user",
                    user_id="single-user",
                    project_id=project_id,
                    name="Project-scoped",
                    enabled=True,
                ),
            )
            session.commit()

        response = await client.put(
            f"/api/projects/{project_id}/corpus/selection",
            json={"global_corpus_ids": ["corp_scoped"]},
        )

    assert response.status_code == 400
    assert "non-global" in response.json()["detail"]


async def test_post_project_corpus_upload_creates_project_scoped_doc(
    app_session,  # type: ignore[no-untyped-def]
    monkeypatch,  # type: ignore[no-untyped-def]
) -> None:
    """Upload a small text file into the per-project corpus.
    Exercises the full path: project-scoped Corpus is created (or
    reused), CorpusDocument written, file blob stored under
    `data_dir/runs/<user>/corpus/projects/<project>/originals/`."""
    monkeypatch.setenv("AUTOESSAY_SYNC_WORKER", "1")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        project_id = await _create_project(client)
        payload = (
            b"# Prior paper for the project corpus\n\n"
            b"This is some illustrative content that the project would draw on."
        )
        response = await client.post(
            f"/api/projects/{project_id}/corpus/upload",
            files={
                "file": ("prior-project.md", BytesIO(payload), "text/markdown"),
            },
        )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["task_id"] in {"sync", body["task_id"]}
    assert body["document"]["title"]
    assert body["document"]["original_size_bytes"] == len(payload)

    with app_session() as session:
        # Project-scoped corpus exists for THIS project.
        corpus = session.scalar(
            select(Corpus).where(
                Corpus.owner_user_id == "single-user",
                Corpus.project_id == project_id,
            ),
        )
        assert corpus is not None
        # CorpusDocument links to that corpus.
        document = session.scalar(
            select(CorpusDocument).where(CorpusDocument.id == body["document"]["id"]),
        )
        assert document is not None
        assert document.corpus_id == corpus.id
        # File blob landed under the per-project originals directory.
        source_path = Path(document.source_path)
        assert "projects" in source_path.parts
        assert project_id in source_path.parts
        assert source_path.is_file()


async def test_get_project_corpus_includes_uploaded_document(
    app_session,  # type: ignore[no-untyped-def]
    monkeypatch,  # type: ignore[no-untyped-def]
) -> None:
    """End-to-end: upload then GET shows the new document under
    `project_documents`."""
    monkeypatch.setenv("AUTOESSAY_SYNC_WORKER", "1")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        project_id = await _create_project(client)
        await client.post(
            f"/api/projects/{project_id}/corpus/upload",
            files={
                "file": (
                    "another.md",
                    BytesIO(b"# Project-scoped sample\n\nSecond document."),
                    "text/markdown",
                ),
            },
        )

        response = await client.get(f"/api/projects/{project_id}/corpus")

    assert response.status_code == 200
    body = response.json()
    assert body["project_corpus_id"] is not None
    assert len(body["project_documents"]) == 1
    assert body["project_documents"][0]["title"]


async def test_endpoints_404_for_unknown_project(
    app_session,  # type: ignore[no-untyped-def]
) -> None:
    """Authorization path: each project corpus endpoint must 404
    when the project id is unknown (or owned by another user — the
    server collapses both cases into 404 to avoid leaking project
    existence)."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        get_resp = await client.get("/api/projects/proj_does_not_exist/corpus")
        put_resp = await client.put(
            "/api/projects/proj_does_not_exist/corpus/selection",
            json={"global_corpus_ids": []},
        )
        upload_resp = await client.post(
            "/api/projects/proj_does_not_exist/corpus/upload",
            files={
                "file": ("x.md", BytesIO(b"# x\n"), "text/markdown"),
            },
        )

    assert get_resp.status_code == 404
    assert put_resp.status_code == 404
    assert upload_resp.status_code == 404
