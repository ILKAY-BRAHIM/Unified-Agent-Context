"""Subprocess drivers for Claude Code and Codex (Phase 4, D8).

The chat front-end has no model. A user turn is forwarded to the official CLI
spawned as a child process; the model runs there under the user's own OAuth
login. No API key is ever involved (§2).

Both CLIs expose a machine-readable event stream, so we parse that rather than
scraping the interactive TUI:

    claude -p "<prompt>" --output-format stream-json --verbose [--resume <id>]
    codex exec [resume <id>] --json "<prompt>"

WARNING: these flags and event shapes move — both tools ship frequently. Verify
against each CLI's `--help` before trusting this. Parsing is deliberately
lenient: anything unrecognised becomes an `other` event carrying its raw JSON,
so a new event type degrades to noise instead of a crash.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

from .hooks import CLAUDE, CODEX


class AgentError(RuntimeError):
    pass


@dataclass
class Event:
    """One thing that happened during a turn, normalised across both agents."""

    kind: str  # session | text | reasoning | tool | file_change | result | error | other
    text: str = ""
    raw: dict = field(default_factory=dict)


def _text_from_content(message: dict) -> str:
    """Claude puts assistant text in a content-block list."""
    blocks = message.get("content") or []
    if isinstance(blocks, str):
        return blocks
    return "".join(b.get("text", "") for b in blocks if isinstance(b, dict) and b.get("type") == "text")


class Driver:
    name: str = ""
    binary: str = ""

    def __init__(self, approvals: str = ""):
        # D9: how much the agent may do without asking. Empty means "whatever
        # the CLI defaults to". Values are agent-specific and validated by the
        # CLI itself, so a new mode doesn't need a change here.
        self.approvals = approvals

    def command(self, prompt: str, session_id: str | None) -> list[str]:
        raise NotImplementedError

    def parse(self, obj: dict) -> Event:
        raise NotImplementedError

    def available(self) -> bool:
        return shutil.which(self.binary) is not None

    def run(self, prompt: str, session_id: str | None = None, cwd: Path | None = None) -> Iterator[Event]:
        if not self.available():
            raise AgentError(
                f"{self.binary!r} is not on PATH. Install the {self.name} CLI and log in with "
                f"your subscription — uac never talks to a model API itself."
            )

        proc = subprocess.Popen(
            self.command(prompt, session_id),
            # `codex exec` reads stdin whenever it isn't a TTY and appends it to
            # the prompt ("Reading additional input from stdin..."). An inherited
            # or piped stdin therefore hangs the turn forever, or silently
            # corrupts the prompt. Close it: the prompt is passed as an argument.
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            cwd=str(cwd) if cwd else None,
        )
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                # Not every line is JSON (banners, warnings). Surface, don't die.
                yield Event(kind="other", text=line)
                continue
            yield self.parse(obj)

        proc.wait()
        if proc.returncode != 0:
            stderr = proc.stderr.read().strip() if proc.stderr else ""
            yield Event(kind="error", text=stderr or f"{self.binary} exited {proc.returncode}")


class ClaudeDriver(Driver):
    name = "Claude Code"
    binary = "claude"

    def command(self, prompt: str, session_id: str | None) -> list[str]:
        cmd = [self.binary, "-p", prompt, "--output-format", "stream-json", "--verbose"]
        # Our own MCP tools, pre-approved on the command line.
        #
        # `permissions.allow` in .claude/settings.json is IGNORED until the user
        # accepts the workspace trust dialog — so in a fresh project every
        # memory_write is silently denied, and Claude quietly falls back to its
        # own private memory, which is exactly the split-brain this project
        # exists to prevent. --allowed-tools works regardless of trust.
        cmd += ["--allowed-tools", "mcp__uac"]
        if self.approvals:
            cmd += ["--permission-mode", self.approvals]
        if session_id:
            cmd += ["--resume", session_id]
        return cmd

    def parse(self, obj: dict) -> Event:
        kind = obj.get("type")
        if kind == "system" and obj.get("subtype") == "init":
            return Event(kind="session", text=obj.get("session_id", ""), raw=obj)
        if kind == "assistant":
            return Event(kind="text", text=_text_from_content(obj.get("message", {})), raw=obj)
        if kind == "result":
            return Event(kind="result", text=obj.get("result", ""), raw=obj)
        if kind == "error":
            return Event(kind="error", text=obj.get("error", "") or obj.get("message", ""), raw=obj)
        return Event(kind="other", raw=obj)


class CodexDriver(Driver):
    name = "Codex"
    binary = "codex"

    # item.type -> our event kind
    ITEM_KINDS = {
        "agent_message": "text",
        "reasoning": "reasoning",
        "command_execution": "tool",
        "mcp_tool_call": "tool",
        "web_search": "tool",
        "file_change": "file_change",
        "todo_list": "other",
    }

    def command(self, prompt: str, session_id: str | None) -> list[str]:
        flags = ["--json"]
        if self.approvals:
            flags += ["--sandbox", self.approvals]
        if session_id:
            return [self.binary, "exec", "resume", session_id, *flags, prompt]
        return [self.binary, "exec", *flags, prompt]

    def parse(self, obj: dict) -> Event:
        kind = obj.get("type", "")
        if kind == "thread.started":
            return Event(kind="session", text=obj.get("thread_id", ""), raw=obj)
        if kind == "item.completed":
            item = obj.get("item", {})
            mapped = self.ITEM_KINDS.get(item.get("type", ""), "other")
            return Event(kind=mapped, text=self._item_text(item), raw=obj)
        if kind == "turn.completed":
            return Event(kind="result", raw=obj)
        if kind in ("turn.failed", "error"):
            error = obj.get("error") or {}
            text = error.get("message") if isinstance(error, dict) else str(error)
            return Event(kind="error", text=text or obj.get("message", ""), raw=obj)
        return Event(kind="other", raw=obj)

    @staticmethod
    def _item_text(item: dict) -> str:
        for key in ("text", "message", "command", "path", "summary"):
            if item.get(key):
                return str(item[key])
        return ""


DRIVERS: dict[str, type[Driver]] = {CLAUDE: ClaudeDriver, CODEX: CodexDriver}

# D9 defaults: let the agent work in the workspace without prompting on every
# edit, but don't hand it the machine. Deliberately not bypassPermissions /
# danger-full-access.
DEFAULT_APPROVALS = {CLAUDE: "acceptEdits", CODEX: "workspace-write"}


def get_driver(agent: str, approvals: str | None = None) -> Driver:
    try:
        cls = DRIVERS[agent]
    except KeyError:
        raise AgentError(f"Unknown agent {agent!r}. Use one of: {', '.join(DRIVERS)}.") from None
    return cls(approvals if approvals is not None else DEFAULT_APPROVALS[agent])


class ChatSession:
    """Multi-turn conversation with one agent.

    The session id from the first turn is reused on every later turn, so the CLI
    keeps its own conversation state — we never replay history ourselves.
    """

    def __init__(self, agent: str, cwd: Path | None = None, approvals: str | None = None):
        self.agent = agent
        self.driver = get_driver(agent, approvals)
        self.cwd = cwd
        self.session_id: str | None = None

    def send(self, prompt: str) -> Iterator[Event]:
        for event in self.driver.run(prompt, session_id=self.session_id, cwd=self.cwd):
            if event.kind == "session" and event.text:
                self.session_id = event.text
            yield event

    def send_text(self, prompt: str) -> str:
        """Collect a whole turn as plain text (used by `uac review`)."""
        chunks, errors = [], []
        for event in self.send(prompt):
            if event.kind in ("text", "result") and event.text:
                chunks.append(event.text)
            elif event.kind == "error" and event.text:
                errors.append(event.text)
        if errors and not chunks:
            raise AgentError("; ".join(errors))
        # Claude emits assistant text and then repeats it in `result`; keep the last.
        return chunks[-1] if chunks else ""
