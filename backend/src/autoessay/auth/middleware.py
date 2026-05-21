from collections.abc import Iterator
from contextlib import contextmanager
from typing import Annotated, Any

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session as SQLAlchemySession
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import JSONResponse, Response

from autoessay.auth.session import delete_session, read_session
from autoessay.config import get_settings
from autoessay.db import SessionLocal, get_session
from autoessay.models import User

SESSION_COOKIE_NAME = "autoessay_session"
DBSessionDependency = Annotated[SQLAlchemySession, Depends(get_session)]


class AuthGateMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        settings = get_settings()
        if _is_public_path(request):
            return await call_next(request)
        if not request.url.path.startswith("/api/"):
            return await call_next(request)
        if settings.auth_bypass:
            request.state.current_user = _synthetic_single_user()
            return await call_next(request)
        session_id = request.cookies.get(SESSION_COOKIE_NAME)
        if not session_id:
            return _unauthorized()
        with _request_db_session(request.app) as db_session:
            user = _resolve_user_from_session_id(session_id, db_session)
            if user is None:
                return _unauthorized()
            request.state.current_user = user
        return await call_next(request)


def current_user(
    request: Request,
    db_session: DBSessionDependency,
) -> User:
    settings = get_settings()
    if settings.auth_bypass:
        return _synthetic_single_user()
    state_user = getattr(request.state, "current_user", None)
    if isinstance(state_user, User):
        return state_user
    session_id = request.cookies.get(SESSION_COOKIE_NAME)
    if not session_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="not authenticated")
    user = _resolve_user_from_session_id(session_id, db_session)
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="not authenticated")
    return user


def validate_auth_boot_settings() -> None:
    get_settings()


def _is_public_path(request: Request) -> bool:
    path = request.url.path
    method = request.method.upper()
    if path.startswith("/api/auth/"):
        return True
    return (method, path) in {
        ("GET", "/"),
        ("GET", "/healthz"),
        ("GET", "/readyz"),
        ("GET", "/version"),
    }


def _resolve_user_from_session_id(session_id: str, db_session: SQLAlchemySession) -> User | None:
    session_record = read_session(session_id, db_session)
    if session_record is None:
        return None
    user = db_session.get(User, session_record.user_id)
    if user is None:
        delete_session(session_id, db_session)
        return None
    return user


def _synthetic_single_user() -> User:
    return User(id="single-user", display_name="Single User")


def _unauthorized() -> JSONResponse:
    return JSONResponse(
        {"detail": "not authenticated"},
        status_code=status.HTTP_401_UNAUTHORIZED,
    )


@contextmanager
def _request_db_session(app: Any) -> Iterator[SQLAlchemySession]:
    override = getattr(app, "dependency_overrides", {}).get(get_session)
    if override is None:
        with SessionLocal() as db_session:
            yield db_session
        return
    iterator = override()
    db_session = next(iterator)
    try:
        yield db_session
    finally:
        close = getattr(iterator, "close", None)
        if close is not None:
            close()
