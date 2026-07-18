"""Cross-project memory links (Phase 3, D10).

Each project keeps its own memory.db. A machine-global registry maps project
name -> project root, and a project opts into reading others:

    [links]
    read = ["project-a"]

Links are explicit, read-only and one-way: B reading A never modifies A, and A
doesn't know B exists. To share both ways, both projects declare the link.

Every result carries `origin_project`, because a memory from another project may
come from a different stack — the agent needs to see where a fact came from so
it can discount it. Silent merging is how you make the other agent confidently
wrong.
"""

from __future__ import annotations

from pathlib import Path

import tomlkit

from .config import (
    CONFIG_RELPATH,
    MEMORY_RELPATH,
    Config,
    global_db_path,
    global_dir,
    registry_path,
)
from .memory import Memory, MemoryStore

GLOBAL_ORIGIN = "global"
CURRENT_ORIGIN = "current"


class LinkError(ValueError):
    pass


# --- registry ----------------------------------------------------------------


def load_registry() -> dict[str, Path]:
    path = registry_path()
    if not path.exists():
        return {}
    doc = tomlkit.parse(path.read_text())
    return {name: Path(str(root)) for name, root in doc.get("projects", {}).items()}


def register_project(name: str, root: Path) -> None:
    """Make a project linkable by name. Idempotent; re-registering moves the path."""
    global_dir().mkdir(parents=True, exist_ok=True)
    path = registry_path()
    doc = tomlkit.parse(path.read_text()) if path.exists() else tomlkit.document()
    projects = doc.get("projects")
    if projects is None:
        projects = tomlkit.table()
        doc["projects"] = projects
    projects[name] = str(Path(root).resolve())
    path.write_text(tomlkit.dumps(doc))


def unregister_project(name: str) -> bool:
    path = registry_path()
    if not path.exists():
        return False
    doc = tomlkit.parse(path.read_text())
    projects = doc.get("projects", {})
    if name not in projects:
        return False
    del projects[name]
    path.write_text(tomlkit.dumps(doc))
    return True


def resolve_db(name: str) -> Path:
    registry = load_registry()
    if name not in registry:
        known = ", ".join(sorted(registry)) or "none"
        raise LinkError(f"Project {name!r} isn't registered. Known projects: {known}. "
                        f"Run `uac init` in that project first.")
    db = registry[name] / MEMORY_RELPATH
    if not db.exists():
        raise LinkError(f"Project {name!r} is registered at {registry[name]} but has no memory.db.")
    return db


# --- link management ---------------------------------------------------------


def add_link(cfg: Config, name: str) -> None:
    if name == cfg.project_name:
        raise LinkError("A project can't link to itself.")
    resolve_db(name)  # fail now, not at search time

    path = cfg.root / CONFIG_RELPATH
    doc = tomlkit.parse(path.read_text()) if path.exists() else tomlkit.document()
    links = doc.get("links")
    if links is None:
        links = tomlkit.table()
        doc["links"] = links
    current = list(links.get("read", []))
    if name not in current:
        current.append(name)
    links["read"] = current
    path.write_text(tomlkit.dumps(doc))


def remove_link(cfg: Config, name: str) -> bool:
    path = cfg.root / CONFIG_RELPATH
    if not path.exists():
        return False
    doc = tomlkit.parse(path.read_text())
    current = list(doc.get("links", {}).get("read", []))
    if name not in current:
        return False
    doc["links"]["read"] = [n for n in current if n != name]
    path.write_text(tomlkit.dumps(doc))
    return True


# --- stores ------------------------------------------------------------------


def global_store() -> MemoryStore:
    global_dir().mkdir(parents=True, exist_ok=True)
    return MemoryStore(global_db_path())


def write_store(cfg: Config) -> MemoryStore:
    """Where this project's writes land.

    Only ever the current project or the global store — never a linked project.
    """
    return global_store() if cfg.scope == "global" else MemoryStore(cfg.db_path)


def read_stores(cfg: Config) -> list[tuple[MemoryStore, str]]:
    """Everything a search may read, each tagged with its origin label."""
    stores = [(MemoryStore(cfg.db_path), CURRENT_ORIGIN), (global_store(), GLOBAL_ORIGIN)]
    for name in cfg.links_read:
        try:
            stores.append((MemoryStore(resolve_db(name)), name))
        except LinkError:
            # A broken link shouldn't take down search for everything else.
            continue
    return stores


def federated_search(cfg: Config, query: str, limit: int = 5) -> list[Memory]:
    """Search current + global + linked projects, merged and labelled.

    Current-project hits outrank linked ones regardless of score (D10): a
    perfectly-worded memory from another repo is still less likely to be right
    here than a decent one from this repo.
    """
    scored: list[tuple[int, float, Memory, MemoryStore]] = []
    for store, origin in read_stores(cfg):
        for mem, score in store.search_scored(query, limit=limit, origin=origin):
            rank = 0 if origin == CURRENT_ORIGIN else 1
            scored.append((rank, score, mem, store))

    scored.sort(key=lambda row: (row[0], row[1]))
    winners = scored[:limit]

    by_store: dict[int, tuple[MemoryStore, list[str]]] = {}
    for _, _, mem, store in winners:
        entry = by_store.setdefault(id(store), (store, []))
        entry[1].append(mem.id)
    for store, ids in by_store.values():
        store.record_access(ids)

    return [mem for _, _, mem, _ in winners]
