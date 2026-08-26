from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path


def write_env_value(path: Path, key: str, value: str) -> None:
    """Atomically update one .env assignment while preserving other lines."""
    if "\r" in value or "\n" in value:
        raise ValueError(f"{key} 不能包含换行")
    original = path.read_text(encoding="utf-8") if path.exists() else ""
    newline = "\r\n" if "\r\n" in original else "\n"
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    replacement = f'{key}="{escaped}"{newline}'
    assignment = re.compile(rf"^\s*(?:export\s+)?{re.escape(key)}\s*=")
    lines = original.splitlines(keepends=True)
    rewritten: list[str] = []
    found = False
    for line in lines:
        if assignment.match(line.rstrip("\r\n")):
            rewritten.append(replacement)
            found = True
        else:
            rewritten.append(line)
    if not found:
        if rewritten and not rewritten[-1].endswith(("\n", "\r")):
            rewritten.append(newline)
        rewritten.append(replacement)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="", dir=path.parent,
            prefix=f".{path.name}.", suffix=".tmp", delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write("".join(rewritten))
        os.replace(temporary, path)
    except Exception:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        raise
    os.environ[key] = value
