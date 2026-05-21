from collections.abc import Iterator
from typing import Any

from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session, sessionmaker

from autoessay.config import get_settings


def _sqlite_connect_args(database_url: str) -> dict[str, Any]:
    if database_url.startswith("sqlite"):
        return {"check_same_thread": False}
    return {}


def make_engine(database_url: str | None = None) -> Engine:
    url = database_url or get_settings().database_url
    # SSE event streams hold a session per connected workspace tab, so the
    # default pool of 5 + 10 overflow runs out quickly under realistic
    # multi-tab use. Lift the ceiling and recycle stale connections.
    engine = create_engine(
        url,
        connect_args=_sqlite_connect_args(url),
        future=True,
        pool_pre_ping=True,
        pool_size=20,
        max_overflow=40,
        pool_recycle=1800,
    )
    if url.startswith("sqlite"):
        _install_sqlite_pragmas(engine)
    return engine


def _install_sqlite_pragmas(engine: Engine) -> None:
    @event.listens_for(engine, "connect")
    def _set_sqlite_pragmas(dbapi_connection: Any, _connection_record: Any) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.close()


engine = make_engine()
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False, future=True)


def get_session() -> Iterator[Session]:
    with SessionLocal() as session:
        yield session


def get_engine() -> Engine:
    """Dependency-injectable accessor for the SQLAlchemy engine.

    SSE endpoints can't take a SessionDependency without pinning a pool
    slot for the entire stream lifetime. They use this engine accessor
    to open short-lived connections per poll instead. Tests override
    this in app.dependency_overrides.
    """
    return engine


def check_database(connection: Connection | None = None) -> bool:
    if connection is not None:
        connection.execute(text("SELECT 1"))
        return True
    with engine.connect() as local_connection:
        local_connection.execute(text("SELECT 1"))
    return True
