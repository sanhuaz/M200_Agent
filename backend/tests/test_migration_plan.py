from __future__ import annotations

import sqlite3
from pathlib import Path

from scripts.migration_plan import build_plan

HEAD = ("0008_companion_reply_integrity",)


def _database(path: Path, *, legacy: bool = False, revision: str | None = None) -> None:
    with sqlite3.connect(path) as connection:
        if legacy:
            connection.execute("CREATE TABLE conversations (id TEXT PRIMARY KEY)")
        if revision is not None:
            connection.execute("CREATE TABLE alembic_version (version_num TEXT PRIMARY KEY)")
            connection.execute("INSERT INTO alembic_version VALUES (?)", (revision,))


def test_empty_database_upgrades_without_backup(tmp_path: Path) -> None:
    plan = build_plan(tmp_path / "empty.db", HEAD)
    assert plan.database_state == "empty"
    assert plan.needs_upgrade is True
    assert plan.needs_backup is False
    assert plan.needs_stamp is False


def test_legacy_database_is_backed_up_and_stamped(tmp_path: Path) -> None:
    database = tmp_path / "legacy.db"
    _database(database, legacy=True)
    plan = build_plan(database, HEAD)
    assert plan.database_state == "legacy"
    assert plan.needs_upgrade is True
    assert plan.needs_backup is True
    assert plan.needs_stamp is True


def test_database_one_revision_behind_is_backed_up(tmp_path: Path) -> None:
    database = tmp_path / "behind.db"
    _database(database, revision="0007_companion_safety_mode")
    plan = build_plan(database, HEAD)
    assert plan.current_revisions == ("0007_companion_safety_mode",)
    assert plan.needs_upgrade is True
    assert plan.needs_backup is True
    assert plan.needs_stamp is False


def test_database_at_head_does_not_create_backup(tmp_path: Path) -> None:
    database = tmp_path / "head.db"
    _database(database, revision=HEAD[0])
    plan = build_plan(database, HEAD)
    assert plan.current_revisions == HEAD
    assert plan.needs_upgrade is False
    assert plan.needs_backup is False
    assert plan.needs_stamp is False
