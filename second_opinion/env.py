"""Load the project-root `.env` for locally launched processes.

A server or CLI started directly from the repository should behave the same as
one started with the variables exported into its shell. Without this, a key in
`.env` is invisible to the process, and live mode refuses at startup.

Rules:

* Only `KEY=VALUE` lines are read. `export KEY=VALUE` is accepted too.
* Comment lines (`#`) and blank lines are ignored; malformed lines are skipped.
* Values are optional double/single quoted; the quotes are stripped.
* An existing environment variable always wins, so a platform-provided value
  (Render, Docker `--env-file`, a shell export) is never overwritten.
* Values are never printed or logged.

Absent file: a no-op. The loader is called explicitly from entry points, never
at import time, so tests and library use are unaffected.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _parse_line(line: str):
    text = line.strip()
    if not text or text.startswith("#"):
        return None
    if text.startswith("export "):
        text = text[len("export "):].strip()
    if "=" not in text:
        return None
    key, _, value = text.partition("=")
    key = key.strip()
    value = value.strip()
    if not key or not key.replace("_", "").isalnum() or key[0].isdigit():
        return None
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        value = value[1:-1]
    return key, value


def load_env_file(path: Optional[Path] = None) -> int:
    """Load variables from `path` without overriding the real environment.

    Returns the number of variables actually set (0 when the file is absent or
    every key was already present). Values are not returned or logged.
    """
    env_path = Path(path) if path is not None else PROJECT_ROOT / ".env"
    try:
        lines = env_path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return 0

    applied = 0
    for line in lines:
        parsed = _parse_line(line)
        if parsed is None:
            continue
        key, value = parsed
        if key in os.environ:
            continue
        os.environ[key] = value
        applied += 1
    return applied
