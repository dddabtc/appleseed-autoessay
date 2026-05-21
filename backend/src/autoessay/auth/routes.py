"""PR-B: native username/password auth routes.

Replaces the prior Casdoor OIDC ``/login`` redirect + ``/callback``
exchange with a single ``POST /api/auth/login`` that takes
``{username, password}`` JSON, verifies the bcrypt hash on
``users.password_hash``, and issues the same ``autoessay_session``
cookie the OIDC path used to set. The downstream session +
middleware pipeline is unchanged so request authorisation is
identical to before.

Behaviour notes:

- Generic 401 ``"Invalid credentials"`` regardless of which leg of
  the check failed (unknown username, NULL hash, wrong password) so
  attackers cannot enumerate accounts. ``passwords.dummy_verify``
  burns the same bcrypt CPU on the unknown-user path so timing
  side-channels are closed.
- 10 failed attempts from one IP within an hour → 429 with
  ``Retry-After``. Successful login clears the counter. See
  ``rate_limit.py`` for the IP-source priority (CF-Connecting-IP →
  X-Forwarded-For → ``request.client.host``).
- Cookie ``Secure`` flag tracks ``request.url.scheme`` so local
  HTTP dev sessions don't get silently dropped by the browser
  (codex round-2 Q2 amendment).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field
from redis import Redis
from sqlalchemy import select
from sqlalchemy.orm import Session

from autoessay.auth import rate_limit
from autoessay.auth.middleware import SESSION_COOKIE_NAME, current_user
from autoessay.auth.passwords import dummy_verify, verify_password
from autoessay.auth.session import create_session, delete_session
from autoessay.config import Settings, get_settings
from autoessay.db import get_session
from autoessay.models import User, utcnow

router = APIRouter(prefix="/api/auth", tags=["auth"])
SESSION_MAX_AGE_SECONDS = 12 * 60 * 60
SettingsDependency = Annotated[Settings, Depends(get_settings)]
SessionDependency = Annotated[Session, Depends(get_session)]
CurrentUserDependency = Annotated[User, Depends(current_user)]


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)


class MeResponse(BaseModel):
    id: str
    username: str | None
    email: str | None
    display_name: str | None
    picture_url: str | None


class LoginResponse(BaseModel):
    user: MeResponse


def _redis_client(settings: Settings) -> Redis:
    return Redis.from_url(settings.redis_url)


@router.post("/login", response_model=LoginResponse)
def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    settings: SettingsDependency,
    db_session: SessionDependency,
) -> LoginResponse:
    ip = rate_limit.client_ip(request)
    redis = _redis_client(settings)
    decision = rate_limit.check(redis, ip)
    if decision.blocked:
        response.headers["Retry-After"] = str(decision.retry_after_seconds)
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many failed login attempts. Try again later.",
            headers={"Retry-After": str(decision.retry_after_seconds)},
        )

    user = db_session.scalar(select(User).where(User.username == payload.username))
    password_ok = verify_password(payload.password, user.password_hash) if user else False
    if user is None:
        # Spend bcrypt CPU on the unknown-user path so the response
        # time matches the success path exactly.
        dummy_verify(payload.password)
    if user is None or not password_ok or user.password_hash is None:
        rate_limit.record_failure(redis, ip)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
        )

    user.last_login_at = utcnow()
    db_session.flush()
    session_id = create_session(user.id, db_session=db_session)
    rate_limit.clear(redis, ip)

    response.set_cookie(
        SESSION_COOKIE_NAME,
        session_id,
        max_age=SESSION_MAX_AGE_SECONDS,
        httponly=True,
        secure=request.url.scheme == "https",
        samesite="lax",
    )
    return LoginResponse(
        user=MeResponse(
            id=user.id,
            username=user.username,
            email=user.email,
            display_name=user.display_name,
            picture_url=user.picture_url,
        ),
    )


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(
    request: Request,
    db_session: SessionDependency,
) -> Response:
    session_id = request.cookies.get(SESSION_COOKIE_NAME)
    if session_id:
        delete_session(session_id, db_session)
    logout_response = Response(status_code=status.HTTP_204_NO_CONTENT)
    logout_response.delete_cookie(SESSION_COOKIE_NAME)
    return logout_response


@router.get("/me", response_model=MeResponse)
def me(user: CurrentUserDependency) -> MeResponse:
    return MeResponse(
        id=user.id,
        username=user.username,
        email=user.email,
        display_name=user.display_name,
        picture_url=user.picture_url,
    )
