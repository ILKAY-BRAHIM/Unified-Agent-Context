"""`@`-mention file search.

Only the front end can see the workspace, so this is ours to do. `git ls-files`
gives us the tracked files for free — already honouring `.gitignore` — which is
almost always the set you'd want to point an agent at. Outside a git repo we
fall back to a bounded walk.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

_EXCLUDE = {"node_modules", ".git", ".next", "dist", "out", "__pycache__", ".venv", "venv"}


def _git_files(root: Path) -> list[str] | None:
    try:
        res = subprocess.run(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
            cwd=str(root), capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if res.returncode != 0:
        return None
    return [line for line in res.stdout.splitlines() if line.strip()]


def _walk_files(root: Path, limit: int = 4000) -> list[str]:
    out: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _EXCLUDE and not d.startswith(".")]
        for name in filenames:
            rel = os.path.relpath(os.path.join(dirpath, name), root)
            out.append(rel)
            if len(out) >= limit:
                return out
    return out


def search_files(root: Path, query: str, limit: int = 12) -> list[tuple[str, str]]:
    """Return (name, path) pairs matching `query`, shallowest first.

    Matching is a simple case-insensitive substring on the path — enough for a
    mention box, and predictable. An empty query lists the shallowest files, so
    typing a bare `@` still shows something useful.
    """
    files = _git_files(root)
    if files is None:
        files = _walk_files(root)

    q = query.lower()
    hits = [f for f in files if q in f.lower()] if q else files
    # Shallowest path first — the file you mean is rarely six levels down — then
    # alphabetical for a stable order.
    hits.sort(key=lambda f: (f.count("/"), f.lower()))
    return [(os.path.basename(f), f) for f in hits[:limit]]
