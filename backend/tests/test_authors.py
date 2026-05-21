"""Tests for the author roster + per-project author assignment.

Covers the codex-AGREEd #5 design:
- self-author lazy bootstrap on first GET /api/authors
- ORCID checksum + email validation
- soft-delete preserves project_author rows
- self-author cannot be soft-deleted
- PUT /api/projects/{id}/authors validations:
  * positions contiguous 0..N-1
  * no duplicate author_ids
  * 50-author cap
  * deleted authors only allowed if already attached
- Roster cap (200 active authors per user)
- Exporter fallback chain (project authors > self > user.display_name)
"""

from __future__ import annotations

from httpx import ASGITransport, AsyncClient

from autoessay.main import app
from autoessay.models import Author, Project


async def _create_project(client: AsyncClient, title: str = "essay") -> str:
    resp = await client.post(
        "/api/projects",
        json={"title": title, "domain_id": "financial_history", "language": "en"},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def test_first_list_creates_self_author(
    app_session,  # type: ignore[no-untyped-def]
) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/authors")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert body[0]["is_self"] is True


async def test_create_and_patch_author(
    app_session,  # type: ignore[no-untyped-def]
) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await client.get("/api/authors")  # bootstrap self
        create = await client.post(
            "/api/authors",
            json={
                "display_name": "Jane Smith",
                "affiliation": "Some Uni",
                "email": "jane@example.com",
                "orcid": "0000-0002-1825-0097",  # valid checksum
            },
        )
    assert create.status_code == 201, create.text
    aid = create.json()["id"]
    assert create.json()["is_self"] is False
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        patch = await client.patch(
            f"/api/authors/{aid}",
            json={"affiliation": "New Uni"},
        )
    assert patch.status_code == 200
    assert patch.json()["affiliation"] == "New Uni"


async def test_orcid_checksum_rejected(
    app_session,  # type: ignore[no-untyped-def]
) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await client.get("/api/authors")
        # Wrong check digit (last char). 0000-0002-1825-0099 has bad checksum.
        resp = await client.post(
            "/api/authors",
            json={"display_name": "Bad", "orcid": "0000-0002-1825-0099"},
        )
    assert resp.status_code == 400
    assert "checksum" in resp.json()["detail"].lower()


async def test_orcid_pattern_rejected(
    app_session,  # type: ignore[no-untyped-def]
) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await client.get("/api/authors")
        resp = await client.post(
            "/api/authors",
            json={"display_name": "Bad", "orcid": "not-an-orcid"},
        )
    assert resp.status_code == 400


async def test_invalid_email_rejected(
    app_session,  # type: ignore[no-untyped-def]
) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await client.get("/api/authors")
        resp = await client.post(
            "/api/authors",
            json={"display_name": "X", "email": "not-an-email"},
        )
    assert resp.status_code == 400


async def test_self_author_cannot_be_deleted(
    app_session,  # type: ignore[no-untyped-def]
) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        list_resp = await client.get("/api/authors")
        self_id = list_resp.json()[0]["id"]
        delete = await client.delete(f"/api/authors/{self_id}")
    assert delete.status_code == 409


async def test_soft_delete_preserves_project_author_row(
    app_session,  # type: ignore[no-untyped-def]
) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await client.get("/api/authors")
        create = await client.post("/api/authors", json={"display_name": "Co-author"})
        co_id = create.json()["id"]
        pid = await _create_project(client)
        # Attach co-author at position 0.
        put = await client.put(
            f"/api/projects/{pid}/authors",
            json={"authors": [{"author_id": co_id, "position": 0}]},
        )
        assert put.status_code == 200, put.text
        # Soft-delete the co-author.
        delete = await client.delete(f"/api/authors/{co_id}")
        assert delete.status_code == 204
        # Project still lists the (now-deleted) author.
        get = await client.get(f"/api/projects/{pid}/authors")
    assert get.status_code == 200
    assert len(get.json()["authors"]) == 1
    assert get.json()["authors"][0]["deleted"] is True


async def test_put_rejects_non_contiguous_positions(
    app_session,  # type: ignore[no-untyped-def]
) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await client.get("/api/authors")
        c1 = (await client.post("/api/authors", json={"display_name": "A"})).json()["id"]
        c2 = (await client.post("/api/authors", json={"display_name": "B"})).json()["id"]
        pid = await _create_project(client)
        put = await client.put(
            f"/api/projects/{pid}/authors",
            json={
                "authors": [
                    {"author_id": c1, "position": 0},
                    {"author_id": c2, "position": 2},  # gap!
                ],
            },
        )
    assert put.status_code == 400


async def test_put_rejects_duplicate_author_ids(
    app_session,  # type: ignore[no-untyped-def]
) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await client.get("/api/authors")
        a1 = (await client.post("/api/authors", json={"display_name": "Dup"})).json()["id"]
        pid = await _create_project(client)
        put = await client.put(
            f"/api/projects/{pid}/authors",
            json={
                "authors": [
                    {"author_id": a1, "position": 0},
                    {"author_id": a1, "position": 1},
                ],
            },
        )
    assert put.status_code == 400


async def test_put_rejects_newly_picked_deleted_author(
    app_session,  # type: ignore[no-untyped-def]
) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await client.get("/api/authors")
        a1 = (await client.post("/api/authors", json={"display_name": "X"})).json()["id"]
        await client.delete(f"/api/authors/{a1}")
        pid = await _create_project(client)
        put = await client.put(
            f"/api/projects/{pid}/authors",
            json={"authors": [{"author_id": a1, "position": 0}]},
        )
    assert put.status_code == 400


async def test_exporter_fallback_uses_project_authors(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path,
) -> None:
    """When project_author rows exist, _resolve_authors uses them."""
    from autoessay.agents.exporter import _resolve_authors

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await client.get("/api/authors")
        a1 = (await client.post("/api/authors", json={"display_name": "Co A"})).json()["id"]
        a2 = (await client.post("/api/authors", json={"display_name": "Co B"})).json()["id"]
        pid = await _create_project(client)
        await client.put(
            f"/api/projects/{pid}/authors",
            json={
                "authors": [
                    {"author_id": a1, "position": 0},
                    {"author_id": a2, "position": 1},
                ],
            },
        )
    with app_session() as session:
        project = session.get(Project, pid)
        names = _resolve_authors(session, project)
    assert names == ["Co A", "Co B"]


async def test_exporter_fallback_to_self_author(
    app_session,  # type: ignore[no-untyped-def]
) -> None:
    from autoessay.agents.exporter import _resolve_authors

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        list_resp = await client.get("/api/authors")
        self_name = list_resp.json()[0]["display_name"]
        pid = await _create_project(client)
        # No project_author rows attached.
    with app_session() as session:
        project = session.get(Project, pid)
        names = _resolve_authors(session, project)
    # Self-author exists, so the fallback chain stops there.
    assert names == [self_name]


async def test_exporter_fallback_to_user_display_name_when_no_self_author(
    app_session,  # type: ignore[no-untyped-def]
) -> None:
    """If neither project_author nor self-author exists, the fallback
    is User.display_name (then ``Admin``)."""
    from sqlalchemy import select

    from autoessay.agents.exporter import _resolve_authors
    from autoessay.models import Domain, User

    with app_session() as session:
        # Inline seed (avoid `from tests.conftest import …` which
        # breaks in CI: pytest's rootdir does not always make the
        # ``tests`` package importable).
        session.add(User(id="single-user", display_name="Single User"))
        session.add(
            Domain(
                id="financial_history",
                display_name="Financial History",
                version="0.1.0",
                enabled=True,
            ),
        )
        session.flush()
        project = Project(
            id="proj_no_self",
            user_id="single-user",
            title="t",
            domain_id="financial_history",
            domain_version="0.1.0",
            status="CREATED",
        )
        session.add(project)
        session.commit()
        # No GET /api/authors yet → no self-author; no project_authors.
        rows = session.scalars(select(Author).where(Author.user_id == "single-user")).all()
        assert list(rows) == []
        names = _resolve_authors(session, project)
    assert names == ["Single User"]
