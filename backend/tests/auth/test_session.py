from autoessay.auth.session import (
    cleanup_expired_sessions,
    create_session,
    delete_session,
    read_session,
)
from autoessay.models import User


def test_create_read_delete_session(app_session) -> None:  # type: ignore[no-untyped-def]
    with app_session() as session:
        session.add(User(id="user_session", display_name="Session User"))
        session.commit()
        session_id = create_session("user_session", db_session=session)

        session_record = read_session(session_id, db_session=session)
        assert session_record is not None
        assert session_record.session_id == session_id
        assert session_record.user_id == "user_session"
        assert session_record.csrf_token

        delete_session(session_id, db_session=session)
        assert read_session(session_id, db_session=session) is None


def test_expired_session_is_not_returned(app_session) -> None:  # type: ignore[no-untyped-def]
    with app_session() as session:
        session.add(User(id="user_expired", display_name="Expired User"))
        session.commit()
        session_id = create_session("user_expired", ttl_hours=-1, db_session=session)

        assert read_session(session_id, db_session=session) is None


def test_cleanup_expired_sessions_keeps_live_sessions(app_session) -> None:  # type: ignore[no-untyped-def]
    with app_session() as session:
        session.add(User(id="user_cleanup", display_name="Cleanup User"))
        session.commit()
        expired_id = create_session("user_cleanup", ttl_hours=-1, db_session=session)
        live_id = create_session("user_cleanup", ttl_hours=1, db_session=session)

        assert cleanup_expired_sessions(session) == 1
        assert read_session(expired_id, db_session=session) is None
        assert read_session(live_id, db_session=session) is not None
