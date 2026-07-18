"""FastMCP server exposing the shared context layer to both agents.

Phase 1 ships the memory tools + project_context. skill_list/skill_load land in
Phase 2.

Tool descriptions are the prompt: they are the only thing telling each model
when to reach for these mid-session. Write them as instructions to an agent, not
as API docs, and iterate on them based on whether the agents actually call them
unprompted.
"""

from __future__ import annotations

from fastmcp import FastMCP

from . import links, skills
from .config import Config, current_source, load_config
from .context import build_project_context, render_memories
from .memory import MemoryStore

mcp = FastMCP("unified-agent-context")

_cfg: Config | None = None


def _config() -> Config:
    global _cfg
    if _cfg is None:
        _cfg = load_config()
    return _cfg


def _state() -> tuple[Config, MemoryStore]:
    cfg = _config()
    return cfg, links.write_store(cfg)


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False})
def memory_write(content: str, kind: str = "fact", tags: list[str] | None = None) -> str:
    """Save a durable fact, decision, or gotcha about this project so future
    sessions — in this agent or another one — can recall it. Use when you learn
    something non-obvious that isn't already written in the codebase.

    kind must be one of: decision, fact, gotcha, preference.
    Keep each memory to one self-contained statement.
    """
    cfg, store = _state()
    mem_id = store.write(
        content=content, kind=kind, tags=tags, source=current_source(), scope=cfg.scope
    )
    return f"Saved memory {mem_id} ({kind}, source={current_source()})."


@mcp.tool(annotations={"readOnlyHint": True, "idempotentHint": True, "openWorldHint": False})
def memory_search(query: str, limit: int = 5) -> list[dict]:
    """Search memories saved by any agent working on this project. Call this
    early in a session, and whenever you're about to make an assumption about
    project conventions, architecture, or past decisions.

    Each result records which agent saved it (`source`) and which project it came
    from (`origin_project`). Results whose `origin_project` is not "current" came
    from a different project — weigh them accordingly, since that project may use
    a different stack or conventions.
    """
    cfg = _config()
    return [m.to_dict() for m in links.federated_search(cfg, query, limit=limit or cfg.max_results)]


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True})
def memory_forget(id: str) -> str:
    """Delete a memory that is wrong or obsolete."""
    _, store = _state()
    return f"Deleted memory {id}." if store.forget(id) else f"No memory with id {id}."


@mcp.tool(annotations={"readOnlyHint": True, "idempotentHint": True, "openWorldHint": False})
def skill_list() -> list[dict]:
    """List available project skills (name + when to use each). Call at session
    start to see what reusable knowledge exists. Returns names and descriptions
    only — use skill_load to read one.
    """
    return [s.index_entry() for s in skills.discover(_config().root)]


@mcp.tool(annotations={"readOnlyHint": True, "idempotentHint": True, "openWorldHint": False})
def skill_load(name: str) -> str:
    """Load the full text of a skill by name. Call this when a skill's
    description matches what you're about to do.
    """
    return skills.load(_config().root, name).body


@mcp.tool(annotations={"readOnlyHint": True, "idempotentHint": True, "openWorldHint": False})
def project_context() -> str:
    """Return the project's AGENTS.md, the available skills, and a summary of
    recent memories. Call this first in any new session.
    """
    cfg = _config()
    return build_project_context(cfg, links.write_store(cfg), limit=cfg.max_results * 2)


def serve(http: bool = False, port: int = 8765) -> None:
    if http:
        mcp.run(transport="http", port=port)
    else:
        mcp.run()


__all__ = ["mcp", "serve", "render_memories"]
