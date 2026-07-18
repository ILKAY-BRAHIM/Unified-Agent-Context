"""Terminal chat front-end (Phase 4a, D8) and the review workflow (Phase 5).

`uac chat` is a thin REPL: your prompt goes to the agent's CLI subprocess, its
event stream comes back. The same drivers back the VS Code extension, so the
plumbing is proven here before any UI work.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from .agents import AgentError, ChatSession, Event, get_driver
from .hooks import CLAUDE, CODEX

# Kept dim/plain rather than styled: this is a debugging surface for the
# extension, not the product.
PREFIX = {
    "text": "",
    "reasoning": "  … ",
    "tool": "  $ ",
    "file_change": "  ± ",
    "error": "  ! ",
}


def render(event: Event) -> str | None:
    if event.kind in ("session", "other", "result"):
        return None
    if not event.text:
        return None
    return f"{PREFIX.get(event.kind, '  ')}{event.text}"


def chat(agent: str, cwd: Path | None = None) -> int:
    session = ChatSession(agent, cwd=cwd)
    driver = session.driver

    if not driver.available():
        print(
            f"{driver.binary!r} is not on PATH. Install the {driver.name} CLI and log in.\n"
            f"uac never talks to a model API — it only drives the CLI you already pay for.",
            file=sys.stderr,
        )
        return 1

    print(f"chatting with {driver.name} (ctrl-d to quit)")
    print("your shared memory + skills are injected by the session-start hook.\n")

    while True:
        try:
            prompt = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not prompt:
            continue

        try:
            for event in session.send(prompt):
                line = render(event)
                if line is not None:
                    print(line)
        except AgentError as exc:
            print(f"error: {exc}", file=sys.stderr)
        print()


def run_once(agent: str, prompt: str, cwd: Path | None = None) -> int:
    try:
        print(ChatSession(agent, cwd=cwd).send_text(prompt))
    except AgentError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


def _git_diff(cwd: Path) -> str:
    result = subprocess.run(
        ["git", "diff", "HEAD"], cwd=str(cwd), capture_output=True, text=True
    )
    if result.returncode != 0:
        raise AgentError(f"git diff failed: {result.stderr.strip()}")
    return result.stdout


def review(task: str, cwd: Path | None = None) -> int:
    """Claude writes, Codex reviews — the pattern that justifies paying for both.

    Runs in the working tree, not a worktree. If you want isolation, run this
    inside `git worktree add` yourself (or use Nimbalyst, which already does).
    """
    cwd = cwd or Path.cwd()
    for agent in (CLAUDE, CODEX):
        driver = get_driver(agent)
        if not driver.available():
            print(f"{driver.binary!r} is not on PATH — `uac review` needs both CLIs.", file=sys.stderr)
            return 1

    print(f"=== {CLAUDE} is working on: {task}\n")
    try:
        wrote = ChatSession(CLAUDE, cwd=cwd).send_text(task)
        print(wrote)

        diff = _git_diff(cwd)
        if not diff.strip():
            print("\nno changes to review.")
            return 0

        print(f"\n=== {CODEX} is reviewing the diff ({len(diff.splitlines())} lines)\n")
        verdict = ChatSession(CODEX, cwd=cwd).send_text(
            "Review this diff for bugs, security issues, and missing tests. "
            "Be specific and concise.\n\n" + diff
        )
        print(verdict)
    except AgentError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0
