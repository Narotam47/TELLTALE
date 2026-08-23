from __future__ import annotations

import structlog
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from telltale.config import settings

log = structlog.get_logger()

engine = create_engine(settings.database_url, echo=False)


@event.listens_for(Engine, "connect")
def _set_sqlite_pragma(dbapi_connection, connection_record):
    import sqlite3

    if isinstance(dbapi_connection, sqlite3.Connection):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


SessionLocal = sessionmaker(bind=engine)


def get_session() -> Session:
    return SessionLocal()


def checkpoint_wal(bind: Engine | Session | None = None) -> bool:
    """Fold the -wal sidecar back into the main .db file.

    SQLite in WAL mode keeps committed data in <db>-wal until a checkpoint runs,
    so copying the .db alone yields an empty database. Running TRUNCATE after the
    final commit leaves the file self-contained. No-op on non-SQLite backends.
    Returns True if a checkpoint ran.
    """
    target = bind if bind is not None else engine
    if isinstance(target, Session):
        target = target.get_bind()

    if target.dialect.name != "sqlite":
        return False
    if target.url.database in (None, "", ":memory:"):
        return False

    # AUTOCOMMIT: a checkpoint cannot truncate while a read transaction is open.
    with target.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        row = conn.execute(text("PRAGMA wal_checkpoint(TRUNCATE)")).fetchone()

    blocked = bool(row and row[0])
    if blocked:
        log.warning("wal.checkpoint_blocked", result=tuple(row))
    else:
        log.info("wal.checkpoint_done")
    return not blocked
