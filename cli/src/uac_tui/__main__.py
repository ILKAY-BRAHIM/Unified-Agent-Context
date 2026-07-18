"""`uac-chat` entry point."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .driver import CLAUDE, CODEX, get_driver


def _project_root() -> Path:
    """The project to run in — the uac core knows how to find it; fall back to
    cwd so the app still runs in a bare directory."""
    try:
        from uac.config import find_project_root

        return find_project_root()
    except Exception:
        return Path.cwd()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="uac-chat",
        description="Full-screen terminal chat for Claude Code and Codex over one shared memory layer.",
    )
    parser.add_argument("--agent", default=CLAUDE, choices=[CLAUDE, CODEX], help="which agent to start with")
    parser.add_argument("--cwd", type=Path, default=None, help="project directory (default: discovered)")
    args = parser.parse_args(argv)

    # Fail early and plainly if neither CLI is installed — the whole app drives
    # them, so there's nothing to do without at least one.
    if not any(get_driver(a).available() for a in (CLAUDE, CODEX)):
        print(
            "Neither `claude` nor `codex` is on your PATH.\n"
            "Install at least one and log in with your subscription — uac never calls a model API.",
            file=sys.stderr,
        )
        return 1

    from .app import ChatApp

    ChatApp(agent=args.agent, cwd=(args.cwd or _project_root()).resolve()).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
