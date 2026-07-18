"""Shared project-context rendering, used by both the MCP tool and the hooks."""

from __future__ import annotations

from . import skills as skills_mod
from .config import Config
from .memory import Memory, MemoryStore

NO_MEMORIES = "(no memories saved yet)"


def render_memories(memories: list[Memory]) -> str:
    if not memories:
        return NO_MEMORIES
    lines = []
    for m in memories:
        tags = f" [{', '.join(m.tags)}]" if m.tags else ""
        origin = "" if m.origin_project == "current" else f" (from {m.origin_project})"
        lines.append(f"- ({m.kind}, via {m.source}{origin}){tags} {m.content}")
    return "\n".join(lines)


def build_project_context(cfg: Config, store: MemoryStore, limit: int = 10) -> str:
    """AGENTS.md + the skills index + recent memories — the session's starting context.

    The skills index is names and descriptions only. Bodies stay out of here on
    purpose (progressive disclosure): inlining them would defeat the whole point
    of the skills layer and bloat every single session.

    The standing instruction at the top is load-bearing. Observed in real use: a
    passive note ("use memory_search for anything not shown") at the *bottom* got
    ignored — asked about a fact sitting in the store, Codex grepped the
    filesystem and reported it found nothing. Agents reach for the tools they
    know (grep, read) unless told plainly, early, that a better source exists.
    """
    parts = [
        f"# Shared project context: {cfg.project_name}",
        "## How to use this context (standing instruction)\n"
        "This project has a **shared memory store** used by both Claude Code and Codex. "
        "It holds decisions, gotchas and conventions that are NOT written in the code.\n\n"
        "- **Before answering any question about this project — and before assuming any "
        "convention, decision, or piece of history — call `memory_search` first.** "
        "Do not grep the codebase for project knowledge that would live in memory: "
        "the store is the authority for *why* things are the way they are, and the code "
        "cannot tell you that.\n"
        "- The memories below are only the most recent few. `memory_search` sees all of them, "
        "plus anything shared from linked projects.\n"
        "- When you learn something durable and non-obvious, save it with `memory_write` so "
        "the other agent has it next session.",
    ]

    if cfg.agents_md.exists():
        parts.append("## AGENTS.md\n" + cfg.agents_md.read_text().strip())
    else:
        parts.append("## AGENTS.md\n(no AGENTS.md found at the project root)")

    try:
        found = skills_mod.discover(cfg.root)
    except skills_mod.SkillError as exc:
        # A broken skill file must be loud but must not cost you the whole session.
        parts.append(f"## Skills\n(could not load skills: {exc})")
    else:
        if found:
            parts.append(
                "## Skills available\n"
                + skills_mod.render_index(found)
                + "\n\nUse `skill_load(name)` to read one when its description matches your task."
            )

    recent = store.recent(limit)
    total = store.count()
    hidden = max(0, total - len(recent))
    tail = (
        f"\n\n{hidden} more memories are NOT shown here — call `memory_search` to reach them."
        if hidden
        else ""
    )
    parts.append(
        f"## Recent shared memories ({len(recent)} of {total} shown)\n"
        + render_memories(recent)
        + tail
    )
    return "\n\n".join(parts)
