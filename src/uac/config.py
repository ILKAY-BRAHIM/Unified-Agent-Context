"""Project discovery and `.agents/config.toml` loading."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

CONFIG_RELPATH = Path(".agents/config.toml")
MEMORY_RELPATH = Path(".agents/memory.db")
STATE_RELDIR = Path(".agents/state")

# Markers that identify a project root, in priority order.
ROOT_MARKERS = (".agents", "AGENTS.md", ".git")

VALID_KINDS = ("decision", "fact", "gotcha", "preference")
VALID_SOURCES = ("claude-code", "codex", "human", "unknown")
VALID_SCOPES = ("project", "global")

def global_dir() -> Path:
    """Machine-global state (D10): the registry of known projects, and the store
    for memories that apply everywhere.

    Resolved per call, not at import, so UAC_HOME can redirect it — tests must
    never touch the user's real ~/.agents.
    """
    override = os.environ.get("UAC_HOME")
    return Path(override) if override else Path.home() / ".agents"


def registry_path() -> Path:
    return global_dir() / "registry.toml"


def global_db_path() -> Path:
    return global_dir() / "global.db"


@dataclass
class Config:
    """Resolved configuration for one project."""

    root: Path
    project_name: str
    max_results: int = 5
    scope: str = "project"  # default scope for this project's writes
    inject_context_on_start: bool = True
    flush_memory_on_end: bool = True
    claude_auto_memory: str = "ours"  # "ours" | "native"
    claude_extra: str = ""

    # Read-only, one-way links to other projects' memory (D10).
    links_read: list[str] = field(default_factory=list)

    @property
    def db_path(self) -> Path:
        return self.root / MEMORY_RELPATH

    @property
    def state_dir(self) -> Path:
        return self.root / STATE_RELDIR

    @property
    def agents_md(self) -> Path:
        return self.root / "AGENTS.md"

    @property
    def claude_md(self) -> Path:
        return self.root / "CLAUDE.md"


def find_project_root(start: Path | None = None) -> Path:
    """Walk up from `start` looking for a project marker.

    Falls back to `start` itself so the tool still works in a bare directory.
    """
    start = (start or Path.cwd()).resolve()
    for candidate in (start, *start.parents):
        for marker in ROOT_MARKERS:
            if (candidate / marker).exists():
                return candidate
    return start


def load_config(root: Path | None = None) -> Config:
    """Load `.agents/config.toml`, tolerating a missing file."""
    root = (root or find_project_root()).resolve()
    cfg = Config(root=root, project_name=root.name)

    path = root / CONFIG_RELPATH
    if not path.exists():
        return cfg

    with path.open("rb") as fh:
        data = tomllib.load(fh)

    project = data.get("project", {})
    cfg.project_name = project.get("name", cfg.project_name)

    memory = data.get("memory", {})
    cfg.max_results = int(memory.get("max_results", cfg.max_results))
    cfg.scope = memory.get("scope", cfg.scope)
    if cfg.scope not in VALID_SCOPES:
        raise ValueError(f"{path}: [memory] scope must be one of {VALID_SCOPES}; got {cfg.scope!r}")

    cfg.claude_extra = data.get("claude", {}).get("extra", "").strip()

    hooks = data.get("hooks", {})
    cfg.inject_context_on_start = bool(
        hooks.get("inject_context_on_start", cfg.inject_context_on_start)
    )
    cfg.flush_memory_on_end = bool(hooks.get("flush_memory_on_end", cfg.flush_memory_on_end))
    cfg.claude_auto_memory = hooks.get("claude_auto_memory", cfg.claude_auto_memory)

    cfg.links_read = list(data.get("links", {}).get("read", []))
    return cfg


def current_source() -> str:
    """Which agent is calling us.

    Each agent spawns its own stdio MCP server process, so registration sets
    UAC_SOURCE per agent (see sync.py). Falls back to "unknown" rather than
    guessing, so bad attribution is visible instead of silently wrong.
    """
    source = os.environ.get("UAC_SOURCE", "unknown").strip().lower()
    return source if source in VALID_SOURCES else "unknown"
