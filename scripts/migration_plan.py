from __future__ import annotations

import argparse
import json
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory


@dataclass(frozen=True)
class MigrationPlan:
    database_state: str
    current_revisions: tuple[str, ...]
    head_revisions: tuple[str, ...]
    needs_backup: bool
    needs_stamp: bool
    needs_upgrade: bool


def database_state(database_path: Path) -> tuple[str, tuple[str, ...]]:
    if not database_path.exists() or database_path.stat().st_size == 0:
        return "empty", ()
    with sqlite3.connect(database_path) as connection:
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        if not tables:
            return "empty", ()
        if "alembic_version" not in tables:
            return "legacy", ()
        revisions = tuple(
            sorted(str(row[0]) for row in connection.execute("SELECT version_num FROM alembic_version"))
        )
        return "versioned", revisions


def build_plan(database_path: Path, head_revisions: tuple[str, ...]) -> MigrationPlan:
    state, current_revisions = database_state(database_path)
    at_head = state == "versioned" and current_revisions == tuple(sorted(head_revisions))
    return MigrationPlan(
        database_state=state,
        current_revisions=current_revisions,
        head_revisions=tuple(sorted(head_revisions)),
        needs_backup=state == "legacy" or (state == "versioned" and not at_head),
        needs_stamp=state == "legacy",
        needs_upgrade=not at_head,
    )


def alembic_heads(config_path: Path) -> tuple[str, ...]:
    config = Config(str(config_path))
    scripts = ScriptDirectory.from_config(config)
    return tuple(sorted(scripts.get_heads()))


def main() -> None:
    parser = argparse.ArgumentParser(description="Inspect the PersonalAgent migration state")
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    plan = build_plan(args.database, alembic_heads(args.config))
    print(json.dumps(asdict(plan), ensure_ascii=False))


if __name__ == "__main__":
    main()
