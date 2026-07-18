"""`uac` command entrypoint."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__, links, skills
from .config import (
    VALID_KINDS,
    VALID_SCOPES,
    Config,
    find_project_root,
    load_config,
    registry_path,
)
from .context import render_memories
from .hooks import CLAUDE, CODEX, session_start_payload, stop_payload
from .memory import MemoryStore

AGENTS_MD_TEMPLATE = """# {name}

> Source of truth for both Claude Code and Codex. Hand-written — edit this file,
> not CLAUDE.md.

## What this project is

TODO: one paragraph.

## Conventions

TODO: how code is written here.

## Commands

TODO: build / test / run.
"""

CONFIG_TEMPLATE = """[project]
name = "{name}"

[memory]
scope = "project"        # "project" | "global"
max_results = 5

[links]
read = []                # Phase 3 (D10): read-only, one-way cross-project memory

[hooks]
inject_context_on_start = true
flush_memory_on_end     = true
claude_auto_memory      = "ours"   # "ours" | "native" — see §5.6

[mcp]
transport = "stdio"      # "stdio" | "http"
port = 8765
"""


def _store(cfg: Config) -> MemoryStore:
    return MemoryStore(cfg.db_path)


# --- commands ----------------------------------------------------------------


def cmd_init(args) -> int:
    from . import sync

    root = Path(args.path).resolve() if args.path else find_project_root()
    root.mkdir(parents=True, exist_ok=True)
    (root / ".agents").mkdir(exist_ok=True)
    skills.skills_dir(root).mkdir(parents=True, exist_ok=True)

    cfg_path = root / ".agents" / "config.toml"
    if not cfg_path.exists():
        cfg_path.write_text(CONFIG_TEMPLATE.format(name=root.name))
        print(f"created {cfg_path}")

    agents_md = root / "AGENTS.md"
    if not agents_md.exists():
        agents_md.write_text(AGENTS_MD_TEMPLATE.format(name=root.name))
        print(f"created {agents_md}  <- fill this in, it is the source of truth")

    cfg = load_config(root)
    _store(cfg)
    print(f"created {cfg.db_path}")

    for path in sync.sync_all(cfg):
        print(f"wrote {path}")

    links.register_project(cfg.project_name, cfg.root)
    print(f"registered {cfg.project_name!r} in {registry_path()} (linkable by name)")

    print(
        "\n"
        "IMPORTANT — manual steps this command CANNOT do for you.\n"
        "Until you do these, both agents silently ignore everything written above.\n"
        "They are security decisions, so only you can make them.\n"
        "\n"
        "  Claude Code:\n"
        "    1. Run `claude` here once and accept the trust prompt.\n"
        "       Otherwise it drops the permissions.allow entry ('this workspace has not\n"
        "       been trusted') and silently refuses every memory_write.\n"
        "\n"
        "  Codex:\n"
        "    2. Run `codex` here once and accept the directory trust prompt.\n"
        "    3. Inside codex, run `/hooks` and TRUST the uac hooks.\n"
        "       Codex ignores wrapper-installed hooks until you approve them there\n"
        "       (openai/codex#21615). Without this the session-start hook never runs,\n"
        "       so Codex never learns the shared memory exists — it will grep your files\n"
        "       and tell you it found nothing.\n"
        "\nThen:\n"
        "  4. Fill in AGENTS.md.\n"
        "  5. Add skills to .agents/skills/ and run `uac sync`.\n"
        "  6. Cross-agent test: have one agent save a memory, then ask the other to recall it\n"
        "     (`uac memory list` shows what landed)."
    )
    return 0


def cmd_sync(args) -> int:
    from . import sync

    cfg = load_config()
    if args.check:
        problems = sync.stale_paths(cfg)
        if problems:
            print("uac sync --check failed:", file=sys.stderr)
            for p in problems:
                print(f"  - {p}", file=sys.stderr)
            print("\nRun `uac sync`.", file=sys.stderr)
            return 1
        print("generated files are up to date")
        return 0

    for path in sync.sync_all(cfg):
        print(f"wrote {path}")
    return 0


def cmd_skills_list(args) -> int:
    cfg = load_config()
    try:
        found = skills.discover(cfg.root)
    except skills.SkillError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    if args.json:
        # Machine-readable for the VS Code extension's / palette. Index entries
        # only — never bodies (progressive disclosure, §5.2).
        print(json.dumps([s.index_entry() for s in found], indent=2))
        return 0
    print(skills.render_index(found))
    return 0


def cmd_skills_import(args) -> int:
    """Bring Claude Code's own user skills into the shared layer."""
    from . import sync

    cfg = load_config()
    available = skills.discover_claude_skills()
    if not available:
        print(
            f"No importable skills in {skills.claude_skills_dir()}.\n"
            "Only Claude's *user* skills live on disk; the ones that ship inside the claude\n"
            "binary (deep-research, dataviz, verify, code-review …) can't be copied.",
            file=sys.stderr,
        )
        return 1

    imported, skipped = skills.import_from_claude(cfg.root, overwrite=args.overwrite)
    for name in imported:
        print(f"imported {name}")
    for name in skipped:
        print(f"skipped  {name} (already exists — use --overwrite to replace)")

    if imported:
        for path in sync.mirror_skills(cfg):
            print(f"wrote    {path.relative_to(cfg.root)}")
        print(
            f"\n{len(imported)} skill(s) now shared: Claude keeps using them natively, and Codex\n"
            "can load them with skill_load. Edit them in .agents/skills/ — they're the\n"
            "project's copy now, not your personal Claude ones."
        )
    return 0


def cmd_skills_show(args) -> int:
    cfg = load_config()
    try:
        print(skills.load(cfg.root, args.name).body)
    except skills.SkillError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


def cmd_link_add(args) -> int:
    cfg = load_config()
    try:
        links.add_link(cfg, args.project)
    except links.LinkError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"{cfg.project_name} now reads memories from {args.project!r} (read-only, one-way)")
    return 0


def cmd_link_ls(args) -> int:
    cfg = load_config()
    registry = links.load_registry()
    print(f"{cfg.project_name} reads: {', '.join(cfg.links_read) or '(nothing)'}")
    print("\nregistered projects:")
    for name, root in sorted(registry.items()):
        marker = " <- this project" if name == cfg.project_name else ""
        print(f"  {name}: {root}{marker}")
    return 0


def cmd_link_rm(args) -> int:
    cfg = load_config()
    if links.remove_link(cfg, args.project):
        print(f"{cfg.project_name} no longer reads {args.project!r}")
        return 0
    print(f"{cfg.project_name} was not reading {args.project!r}", file=sys.stderr)
    return 1


def cmd_serve(args) -> int:
    from .server import serve

    serve(http=args.http, port=args.port)
    return 0


def cmd_memory_add(args) -> int:
    cfg = load_config()
    if args.scope:
        cfg.scope = args.scope
    tags = [t.strip() for t in (args.tags or "").split(",") if t.strip()]
    mem_id = links.write_store(cfg).write(
        content=args.content, kind=args.kind, tags=tags, source="human", scope=cfg.scope
    )
    print(mem_id)
    return 0


def cmd_memory_list(args) -> int:
    cfg = load_config()
    found = _store(cfg).recent(args.limit)
    if args.json:
        # Machine-readable for the VS Code extension. Keep this stable — the
        # extension parses it rather than opening memory.db, so the schema stays
        # owned by memory.py alone.
        print(json.dumps([m.to_dict() for m in found], indent=2))
        return 0
    print(render_memories(found))
    return 0


def cmd_memory_search(args) -> int:
    """Searches this project + global + linked projects (D10)."""
    cfg = load_config()
    found = links.federated_search(cfg, args.query, limit=args.limit or cfg.max_results)
    if args.json:
        print(json.dumps([m.to_dict() for m in found], indent=2))
        return 0
    if not found:
        print(f"(nothing matching {args.query!r})")
        return 0
    print(render_memories(found))
    return 0


def cmd_memory_forget(args) -> int:
    cfg = load_config()
    if _store(cfg).forget(args.id):
        print(f"deleted {args.id}")
        return 0
    print(f"no memory with id {args.id}", file=sys.stderr)
    return 1


def cmd_chat(args) -> int:
    from .chat import chat

    cfg = load_config()
    return chat(args.agent, cwd=cfg.root)


def cmd_run(args) -> int:
    from .chat import run_once

    cfg = load_config()
    return run_once(args.agent, args.prompt, cwd=cfg.root)


def cmd_review(args) -> int:
    from .chat import review

    cfg = load_config()
    return review(args.task, cwd=cfg.root)


def cmd_hook(args) -> int:
    """Hook entrypoint: read the agent's JSON on stdin, emit JSON on stdout.

    Fails open on purpose. A hook that raises would block the user's session, so
    any error degrades to "no hook output" and the agent carries on.
    """
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
        cfg = load_config(Path(payload["cwd"]) if payload.get("cwd") else None)

        if args.event == "session-start":
            out = session_start_payload(args.agent, payload, cfg, _store(cfg))
        else:
            out = stop_payload(args.agent, payload, cfg)

        if out:
            print(json.dumps(out))
    except Exception as exc:  # noqa: BLE001 - never break the agent's session
        print(f"uac hook error (ignored): {exc}", file=sys.stderr)
    return 0


# --- parser ------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="uac", description="Unified agent context layer")
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="cmd", required=True)

    init = sub.add_parser("init", help="scaffold and register in both CLIs")
    init.add_argument("path", nargs="?", help="project root (default: discovered)")
    init.set_defaults(func=cmd_init)

    serve = sub.add_parser("serve", help="run the MCP server")
    serve.add_argument("--http", action="store_true", help="streamable HTTP instead of stdio")
    serve.add_argument("--port", type=int, default=8765)
    serve.set_defaults(func=cmd_serve)

    syncp = sub.add_parser("sync", help="regenerate CLAUDE.md + .claude/skills/ and re-register")
    syncp.add_argument(
        "--check", action="store_true", help="exit non-zero if generated files are stale"
    )
    syncp.set_defaults(func=cmd_sync)

    sk = sub.add_parser("skills", help="inspect project skills")
    sksub = sk.add_subparsers(dest="skillcmd", required=True)
    skl = sksub.add_parser("list", help="name + when-to-use for each skill")
    skl.add_argument("--json", action="store_true", help="machine-readable output")
    skl.set_defaults(func=cmd_skills_list)
    sks = sksub.add_parser("show", help="print a skill body")
    sks.add_argument("name")
    sks.set_defaults(func=cmd_skills_show)

    ski = sksub.add_parser(
        "import", help="copy Claude Code's user skills into .agents/skills/ so Codex gets them too"
    )
    ski.add_argument("--overwrite", action="store_true", help="replace skills that already exist")
    ski.set_defaults(func=cmd_skills_import)

    lk = sub.add_parser("link", help="cross-project memory links (read-only, one-way)")
    lksub = lk.add_subparsers(dest="linkcmd", required=True)
    lka = lksub.add_parser("add", help="read another project's memories")
    lka.add_argument("project")
    lka.set_defaults(func=cmd_link_add)
    lkl = lksub.add_parser("ls", help="show links and registered projects")
    lkl.set_defaults(func=cmd_link_ls)
    lkr = lksub.add_parser("rm", help="stop reading a project")
    lkr.add_argument("project")
    lkr.set_defaults(func=cmd_link_rm)

    mem = sub.add_parser("memory", help="inspect the memory store")
    memsub = mem.add_subparsers(dest="memcmd", required=True)

    add = memsub.add_parser("add", help="save a memory by hand")
    add.add_argument("content")
    add.add_argument("--kind", default="fact", choices=VALID_KINDS)
    add.add_argument("--tags", help="comma-separated")
    add.add_argument("--scope", choices=VALID_SCOPES, help="override the configured scope")
    add.set_defaults(func=cmd_memory_add)

    lst = memsub.add_parser("list", help="most recent memories")
    lst.add_argument("--limit", type=int, default=20)
    lst.add_argument("--json", action="store_true", help="machine-readable output")
    lst.set_defaults(func=cmd_memory_list)

    sea = memsub.add_parser("search")
    sea.add_argument("query")
    sea.add_argument("--limit", type=int)
    sea.add_argument("--json", action="store_true", help="machine-readable output")
    sea.set_defaults(func=cmd_memory_search)

    fgt = memsub.add_parser("forget")
    fgt.add_argument("id")
    fgt.set_defaults(func=cmd_memory_forget)

    ch = sub.add_parser("chat", help="chat with an agent through its CLI (no API key)")
    ch.add_argument("--agent", default=CLAUDE, choices=[CLAUDE, CODEX])
    ch.set_defaults(func=cmd_chat)

    run = sub.add_parser("run", help="one-shot headless prompt")
    run.add_argument("agent", choices=[CLAUDE, CODEX])
    run.add_argument("prompt")
    run.set_defaults(func=cmd_run)

    rev = sub.add_parser("review", help="Claude writes the change, Codex reviews the diff")
    rev.add_argument("task")
    rev.set_defaults(func=cmd_review)

    hook = sub.add_parser("hook", help="internal: invoked by the agents' hooks")
    hook.add_argument("event", choices=["session-start", "stop"])
    hook.add_argument("--agent", required=True, choices=[CLAUDE, CODEX])
    hook.set_defaults(func=cmd_hook)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)
