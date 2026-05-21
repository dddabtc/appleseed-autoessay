"""PR-B: native username/password auth route tests."""

from __future__ import annotations

from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from autoessay.auth.middleware import SESSION_COOKIE_NAME
from autoessay.auth.passwords import hash_password
from autoessay.auth.session import create_session
from autoessay.config import get_settings
from autoessay.main import app
from autoessay.models import User


class _FakeRedis:
    """In-memory stand-in so we don't need a real Redis in unit tests.

    Implements only the surface ``rate_limit.py`` actually calls
    (``get`` / ``ttl`` / ``pipeline().incr.expire.execute`` /
    ``delete``). Anything else raises so we notice if the rate
    limiter starts using a new primitive.
    """

    def __init__(self) -> None:
        self.store: dict[bytes, int] = {}
        self.ttls: dict[bytes, int] = {}

    @staticmethod
    def _key(key: str | bytes) -> bytes:
        return key.encode("utf-8") if isinstance(key, str) else key

    def get(self, key: str) -> bytes | None:
        value = self.store.get(self._key(key))
        return str(value).encode("ascii") if value is not None else None

    def ttl(self, key: str) -> int:
        return self.ttls.get(self._key(key), -2)

    def pipeline(self) -> _FakeRedisPipeline:
        return _FakeRedisPipeline(self)

    def delete(self, key: str) -> None:
        bkey = self._key(key)
        self.store.pop(bkey, None)
        self.ttls.pop(bkey, None)


class _FakeRedisPipeline:
    def __init__(self, parent: _FakeRedis) -> None:
        self.parent = parent
        self.ops: list[tuple[str, str, int]] = []

    def incr(self, key: str) -> _FakeRedisPipeline:
        self.ops.append(("incr", key, 0))
        return self

    def expire(self, key: str, seconds: int) -> _FakeRedisPipeline:
        self.ops.append(("expire", key, seconds))
        return self

    def execute(self) -> list[Any]:
        results: list[Any] = []
        for op_name, key, payload in self.ops:
            bkey = self.parent._key(key)
            if op_name == "incr":
                self.parent.store[bkey] = self.parent.store.get(bkey, 0) + 1
                results.append(self.parent.store[bkey])
            elif op_name == "expire":
                self.parent.ttls[bkey] = payload
                results.append(True)
        return results


@pytest.fixture()
def fake_redis(monkeypatch: pytest.MonkeyPatch) -> _FakeRedis:
    redis = _FakeRedis()
    monkeypatch.setattr(
        "autoessay.auth.routes._redis_client",
        lambda _settings: redis,
    )
    return redis


LOGIN_USERNAME = "test-admin"
LOGIN_PASSWORD = "local-test-password"


def _seed_login_user(
    app_session,  # type: ignore[no-untyped-def]
    *,
    username: str = LOGIN_USERNAME,
    password: str = LOGIN_PASSWORD,
) -> str:
    user_id = f"user_{username}"
    with app_session() as session:
        session.add(
            User(
                id=user_id,
                username=username,
                password_hash=hash_password(password),
                display_name="Test User",
            ),
        )
        session.commit()
    return user_id


async def test_login_success_sets_cookie_and_returns_user(
    app_session,  # type: ignore[no-untyped-def]
    monkeypatch,
    fake_redis,
) -> None:
    monkeypatch.setenv("AUTOESSAY_AUTH_BYPASS", "0")
    get_settings.cache_clear()
    user_id = _seed_login_user(app_session)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="https://testserver") as client:
        response = await client.post(
            "/api/auth/login",
            json={"username": LOGIN_USERNAME, "password": LOGIN_PASSWORD},
        )
    assert response.status_code == 200
    body = response.json()
    assert body["user"]["id"] == user_id
    assert body["user"]["username"] == LOGIN_USERNAME
    cookie_header = response.headers.get("set-cookie") or ""
    assert SESSION_COOKIE_NAME in cookie_header


async def test_login_wrong_password_returns_401(
    app_session,  # type: ignore[no-untyped-def]
    monkeypatch,
    fake_redis,
) -> None:
    monkeypatch.setenv("AUTOESSAY_AUTH_BYPASS", "0")
    get_settings.cache_clear()
    _seed_login_user(app_session)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="https://testserver") as client:
        response = await client.post(
            "/api/auth/login",
            json={"username": LOGIN_USERNAME, "password": "wrong-password"},
        )
    assert response.status_code == 401
    assert response.json() == {"detail": "Invalid credentials"}


async def test_login_unknown_user_returns_401_and_burns_bcrypt(
    app_session,  # type: ignore[no-untyped-def]
    monkeypatch,
    fake_redis,
) -> None:
    monkeypatch.setenv("AUTOESSAY_AUTH_BYPASS", "0")
    get_settings.cache_clear()
    # No login user seeded — table is empty.

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="https://testserver") as client:
        response = await client.post(
            "/api/auth/login",
            json={"username": "ghost", "password": "anything"},
        )
    assert response.status_code == 401
    assert response.json() == {"detail": "Invalid credentials"}


async def test_login_null_password_hash_user_returns_401(
    app_session,  # type: ignore[no-untyped-def]
    monkeypatch,
    fake_redis,
) -> None:
    """Historical OIDC-only rows (password_hash IS NULL) cannot log in."""

    monkeypatch.setenv("AUTOESSAY_AUTH_BYPASS", "0")
    get_settings.cache_clear()
    with app_session() as session:
        session.add(
            User(
                id="user_legacy",
                username="legacy",
                password_hash=None,
                oidc_subject="historical-subject",
                display_name="Legacy",
            ),
        )
        session.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="https://testserver") as client:
        response = await client.post(
            "/api/auth/login",
            json={"username": "legacy", "password": "doesnt-matter"},
        )
    assert response.status_code == 401


async def test_rate_limit_blocks_after_threshold(
    app_session,  # type: ignore[no-untyped-def]
    monkeypatch,
    fake_redis,
) -> None:
    monkeypatch.setenv("AUTOESSAY_AUTH_BYPASS", "0")
    get_settings.cache_clear()
    _seed_login_user(app_session)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="https://testserver") as client:
        for _ in range(10):
            response = await client.post(
                "/api/auth/login",
                json={"username": LOGIN_USERNAME, "password": "wrong"},
            )
            assert response.status_code == 401
        # 11th attempt — even with the correct password — must be blocked.
        blocked = await client.post(
            "/api/auth/login",
            json={"username": LOGIN_USERNAME, "password": LOGIN_PASSWORD},
        )
    assert blocked.status_code == 429
    assert blocked.headers.get("Retry-After")


async def test_rate_limit_clears_on_success(
    app_session,  # type: ignore[no-untyped-def]
    monkeypatch,
    fake_redis,
) -> None:
    monkeypatch.setenv("AUTOESSAY_AUTH_BYPASS", "0")
    get_settings.cache_clear()
    _seed_login_user(app_session)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="https://testserver") as client:
        # 9 fails — under the threshold of 10.
        for _ in range(9):
            await client.post(
                "/api/auth/login",
                json={"username": LOGIN_USERNAME, "password": "wrong"},
            )
        # Successful login — the counter should reset.
        success = await client.post(
            "/api/auth/login",
            json={"username": LOGIN_USERNAME, "password": LOGIN_PASSWORD},
        )
        assert success.status_code == 200
        # New round of fails should not immediately trip 429.
        for _ in range(5):
            response = await client.post(
                "/api/auth/login",
                json={"username": LOGIN_USERNAME, "password": "wrong"},
            )
            assert response.status_code == 401


async def test_logout_deletes_session_cookie(
    app_session,  # type: ignore[no-untyped-def]
    monkeypatch,
    fake_redis,
) -> None:
    monkeypatch.setenv("AUTOESSAY_AUTH_BYPASS", "0")
    get_settings.cache_clear()
    user_id = _seed_login_user(app_session)
    with app_session() as session:
        session_id = create_session(user_id, db_session=session)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="https://testserver") as client:
        response = await client.post(
            "/api/auth/logout",
            headers={"Cookie": f"{SESSION_COOKIE_NAME}={session_id}"},
        )
    assert response.status_code == 204
    cookie_header = response.headers.get("set-cookie") or ""
    assert SESSION_COOKIE_NAME in cookie_header


async def test_me_returns_current_user(
    app_session,  # type: ignore[no-untyped-def]
    monkeypatch,
    fake_redis,
) -> None:
    monkeypatch.setenv("AUTOESSAY_AUTH_BYPASS", "0")
    get_settings.cache_clear()
    user_id = _seed_login_user(app_session)
    with app_session() as session:
        session_id = create_session(user_id, db_session=session)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="https://testserver") as client:
        response = await client.get(
            "/api/auth/me",
            headers={"Cookie": f"{SESSION_COOKIE_NAME}={session_id}"},
        )
    assert response.status_code == 200
    body = response.json()
    assert body["id"] == user_id
    assert body["username"] == LOGIN_USERNAME
