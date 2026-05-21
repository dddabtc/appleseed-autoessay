"""PR-390 regression: PR-389 ``DELETE /api/projects/{id}/hard`` blew
up with ``sqlite3.IntegrityError: FOREIGN KEY constraint failed`` when
the project had a ``branches`` row referenced by self-FK
(``branches.parent_branch_id``) or by ``phase_versions.branch_id``
(use_alter cross-ref). The fix is ``PRAGMA foreign_keys=OFF`` for
the duration of the cascade, mirroring conftest's ``drop_all``
workaround.

Live trigger 2026-05-13: user bulk-selected 43 deleted runs, hit
"永久删除" → 500 "Internal Server Error".
"""

from __future__ import annotations

from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from autoessay.main import app
from autoessay.models import Branch, Project, Run


async def _make_project_with_branch_chain(client: AsyncClient) -> tuple[str, str]:
    proj = await client.post(
        "/api/projects",
        json={
            "title": "PR-390 fk cycle",
            "domain_id": "financial_history",
            "target_journal": None,
        },
    )
    project_id = proj.json()["id"]
    run_resp = await client.post(f"/api/projects/{project_id}/runs", json={})
    run_id = run_resp.json()["id"]
    return project_id, run_id


async def test_hard_delete_project_with_branch_self_fk(app_session) -> None:  # type: ignore[no-untyped-def]
    """Add a branch with ``parent_branch_id`` pointing to the auto-
    created main branch — reproduces the prod 500."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        project_id, run_id = await _make_project_with_branch_chain(client)

    # Seed a child branch
    with app_session() as session:
        main_branch = session.scalar(
            select(Branch).where(Branch.run_id == run_id, Branch.name == "main"),
        )
        assert main_branch is not None
        child = Branch(
            id="branch_pr390_child",
            run_id=run_id,
            name="experiment",
            parent_branch_id=main_branch.id,
        )
        session.add(child)
        session.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        soft = await client.delete(f"/api/projects/{project_id}")
        assert soft.status_code == 204
        hard = await client.delete(f"/api/projects/{project_id}/hard")
        assert hard.status_code == 204, hard.text

    with app_session() as session:
        assert session.scalar(select(Project).where(Project.id == project_id)) is None
        assert session.scalar(select(Run).where(Run.id == run_id)) is None
        # Both the main and the child branch rows should be gone.
        assert session.scalars(select(Branch).where(Branch.run_id == run_id)).all() == []


async def test_hard_delete_run_with_branch_chain(app_session) -> None:  # type: ignore[no-untyped-def]
    """Same regression at run scope (single-run hard-delete)."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        project_id, run_id = await _make_project_with_branch_chain(client)

    with app_session() as session:
        main_branch = session.scalar(
            select(Branch).where(Branch.run_id == run_id, Branch.name == "main"),
        )
        assert main_branch is not None
        for i in range(2):
            session.add(
                Branch(
                    id=f"branch_pr390_chain_{i}",
                    run_id=run_id,
                    name=f"chain_{i}",
                    parent_branch_id=main_branch.id,
                ),
            )
        session.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        soft = await client.delete(f"/api/runs/{run_id}")
        assert soft.status_code == 204
        hard = await client.delete(f"/api/runs/{run_id}/hard")
        assert hard.status_code == 204, hard.text

    with app_session() as session:
        assert session.scalar(select(Run).where(Run.id == run_id)) is None
        assert session.scalars(select(Branch).where(Branch.run_id == run_id)).all() == []
