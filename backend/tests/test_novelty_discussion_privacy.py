from pathlib import Path

from httpx import ASGITransport, AsyncClient

from autoessay.auth.middleware import SESSION_COOKIE_NAME
from autoessay.auth.session import create_session
from autoessay.config import get_settings
from autoessay.main import app
from autoessay.models import Domain, NoveltyDiscussion, Project, Run, User
from autoessay.run_writer import create_run_directory


async def test_novelty_discussion_messages_are_scoped_to_run_owner(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("AUTOESSAY_AUTH_BYPASS", "0")
    get_settings.cache_clear()
    owner_cookie, other_cookie, run_id = _seed_owned_discussion(app_session, tmp_path)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="https://test") as client:
        owner_response = await client.get(
            f"/api/runs/{run_id}/novelty/discussion",
            headers={"Cookie": owner_cookie},
        )
        other_response = await client.get(
            f"/api/runs/{run_id}/novelty/discussion",
            headers={"Cookie": other_cookie},
        )
        other_post = await client.post(
            f"/api/runs/{run_id}/novelty/discuss",
            headers={"Cookie": other_cookie},
            json={"user_message": "Try a different angle."},
        )

    assert owner_response.status_code == 200
    assert len(owner_response.json()) == 1
    assert other_response.status_code == 404
    assert other_post.status_code == 404


def _seed_owned_discussion(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path: Path,
) -> tuple[str, str, str]:
    run_id = "run_private_discussion"
    run_dir = create_run_directory(
        tmp_path / "runs",
        run_id,
        "proj_private",
        state="USER_NOVELTY_REVIEW",
        domain_id="financial_history",
    )
    with app_session() as session:
        owner = User(
            id="user_discussion_owner",
            oidc_subject="subject-discussion-owner",
            oidc_issuer="https://auth.example.test/casdoor",
            email="owner@example.test",
            display_name="Owner",
        )
        other = User(
            id="user_discussion_other",
            oidc_subject="subject-discussion-other",
            oidc_issuer="https://auth.example.test/casdoor",
            email="other@example.test",
            display_name="Other",
        )
        session.add(owner)
        session.add(other)
        session.add(
            Domain(
                id="financial_history",
                display_name="Financial History",
                version="0.1.0",
                enabled=True,
            ),
        )
        session.flush()
        session.add(
            Project(
                id="proj_private",
                user_id=owner.id,
                title="Private novelty run",
                domain_id="financial_history",
                domain_version="0.1.0",
                status="CREATED",
            ),
        )
        session.add(
            Run(
                id=run_id,
                project_id="proj_private",
                domain_version="0.1.0",
                run_dir=str(run_dir),
                state="USER_NOVELTY_REVIEW",
                baseline_hash="test",
            ),
        )
        session.flush()
        session.add(
            NoveltyDiscussion(
                id="discussion_private_1",
                run_id=run_id,
                role="user",
                content="Private feedback",
                generation_token=1,
            ),
        )
        session.commit()
        owner_session = create_session(owner.id, db_session=session)
        other_session = create_session(other.id, db_session=session)
    return (
        f"{SESSION_COOKIE_NAME}={owner_session}",
        f"{SESSION_COOKIE_NAME}={other_session}",
        run_id,
    )
