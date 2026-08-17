from __future__ import annotations

from collections.abc import Generator

from sqlalchemy import create_engine, event, inspect
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.core.config import get_settings


class Base(DeclarativeBase):
    pass


settings = get_settings()
engine = create_engine(
    f"sqlite:///{settings.database_path.as_posix()}",
    connect_args={"check_same_thread": False},
)


@event.listens_for(Engine, "connect")
def configure_sqlite(dbapi_connection, _connection_record) -> None:  # type: ignore[no-untyped-def]
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.close()


SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def get_db() -> Generator[Session]:
    with SessionLocal() as session:
        yield session


def initialize_database() -> None:
    from app.db import models  # noqa: F401

    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.exec_driver_sql(
            """
            CREATE VIRTUAL TABLE IF NOT EXISTS chunk_fts USING fts5(
                chunk_id UNINDEXED,
                knowledge_base_id UNINDEXED,
                tokens,
                content
            )
            """
        )


def verify_schema() -> None:
    required = {
        "conversations",
        "messages",
        "knowledge_bases",
        "documents",
        "chunks",
        "memories",
        "confirmations",
        "jobs",
        "tool_runs",
        "processed_events",
        "extension_packages",
        "personas",
        "persona_assignments",
        "admin_identities",
        "artifacts",
        "app_settings",
    }
    missing = required.difference(inspect(engine).get_table_names())
    if missing:
        raise RuntimeError(f"数据库缺少必要表，迁移未完成: {', '.join(sorted(missing))}")
