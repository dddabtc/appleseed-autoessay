from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from secrets import token_urlsafe

from sqlalchemy import delete, select
from sqlalchemy.orm import Session as SQLAlchemySession

from autoessay.db import SessionLocal
from autoessay.models import AuthSession, utcnow


@dataclass(frozen=True)
class SessionRecord:
    session_id: str
    user_id: str
    expires_at: datetime
    csrf_token: str


def create_session(
    user_id: str,
    ttl_hours: int = 12,
    db_session: SQLAlchemySession | None = None,
) -> str:
    session = db_session or SessionLocal()
    should_close = db_session is None
    session_id = token_urlsafe(48)
    csrf_token = token_urlsafe(32)
    try:
        session.add(
            AuthSession(
                session_id=session_id,
                user_id=user_id,
                expires_at=utcnow() + timedelta(hours=ttl_hours),
                csrf_token=csrf_token,
            ),
        )
        session.commit()
        return session_id
    finally:
        if should_close:
            session.close()


def read_session(
    session_id: str,
    db_session: SQLAlchemySession | None = None,
) -> SessionRecord | None:
    session = db_session or SessionLocal()
    should_close = db_session is None
    try:
        auth_session = session.get(AuthSession, session_id)
        if auth_session is None:
            return None
        if _to_aware_utc(auth_session.expires_at) <= utcnow():
            session.delete(auth_session)
            session.commit()
            return None
        return SessionRecord(
            session_id=auth_session.session_id,
            user_id=auth_session.user_id,
            expires_at=auth_session.expires_at,
            csrf_token=auth_session.csrf_token,
        )
    finally:
        if should_close:
            session.close()


def delete_session(
    session_id: str,
    db_session: SQLAlchemySession | None = None,
) -> None:
    session = db_session or SessionLocal()
    should_close = db_session is None
    try:
        auth_session = session.get(AuthSession, session_id)
        if auth_session is not None:
            session.delete(auth_session)
            session.commit()
    finally:
        if should_close:
            session.close()


def cleanup_expired_sessions(db_session: SQLAlchemySession | None = None) -> int:
    session = db_session or SessionLocal()
    should_close = db_session is None
    try:
        expired_ids = list(
            session.scalars(
                select(AuthSession.session_id).where(AuthSession.expires_at <= utcnow()),
            ),
        )
        if not expired_ids:
            return 0
        session.execute(delete(AuthSession).where(AuthSession.session_id.in_(expired_ids)))
        session.commit()
        return len(expired_ids)
    finally:
        if should_close:
            session.close()


def cleanup_expired_sessions_job() -> dict[str, int]:
    return {"deleted": cleanup_expired_sessions()}


def cleanup_and_reschedule(delay_seconds: int = 3600) -> dict[str, int]:
    """RQ recurring job: clean up expired sessions, then re-enqueue self.

    Lives in this module (not in worker.py which Python loads as __main__
    when the worker process is started via `python -m autoessay.worker`).
    RQ rejects functions whose module is __main__ because workers cannot
    re-import them.
    """
    from datetime import timedelta as _td

    from redis import Redis
    from rq import Queue

    from autoessay.config import get_settings

    deleted = cleanup_expired_sessions()
    settings = get_settings()
    redis_connection = Redis.from_url(settings.redis_url)
    queue = Queue(settings.rq_queue_name, connection=redis_connection)
    queue.enqueue_in(_td(seconds=delay_seconds), cleanup_and_reschedule)
    return {"deleted": deleted}


def _to_aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
