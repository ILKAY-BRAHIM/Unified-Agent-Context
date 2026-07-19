"""Subprocess drivers for Claude Code and Codex.

This app's OWN driver — a deliberate sibling to the VS Code extension's
`driver.ts`, not shared with it. The two apps are independent front ends; a user
picks one. They meet only at the shared uac core (memory, skills, hooks), never
in each other's code.

No model here. A turn is forwarded to the official CLI as a child process; the
model runs there under the user's OAuth login. No API key is ever involved.

    claude -p "<prompt>" --output-format stream-json --verbose [--resume <id>]
    codex exec [resume <id>] --json "<prompt>"

Flags and event shapes move — both CLIs ship often. Parsing is lenient: an
unrecognised event yields nothing rather than crashing the chat.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

CLAUDE = "claude-code"
CODEX = "codex"


class AgentError(RuntimeError):
    pass


@dataclass
class Event:
    """One thing that happened during a turn, normalised across both agents.

    kind: session | text | reasoning | shell | tool | file_change | result | error | other
    """

    kind: str
    text: str = ""
    raw: dict = field(default_factory=dict)


@dataclass
class ModelInfo:
    label: str
    description: str = ""
    efforts: list[str] = field(default_factory=list)
    default_effort: str = ""


@dataclass
class Capabilities:
    model: str = ""
    models: list[str] = field(default_factory=list)
    model_info: dict[str, ModelInfo] = field(default_factory=dict)
    commands: list[str] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    subagents: list[str] = field(default_factory=list)


@dataclass
class Control:
    id: str
    label: str
    values: list[str]
    blank: str
    editable: bool = False
    widget: str = "menu"  # "menu" | "dots"
    descriptions: dict[str, str] = field(default_factory=dict)
    labels: dict[str, str] = field(default_factory=dict)
    icon: str = ""
    default_value: str = ""


TurnOptions = dict


# --- tool calls -> readable lines -------------------------------------------


def _summarize_input(data: dict | None) -> str:
    if not isinstance(data, dict):
        return ""
    for key in ("query", "pattern", "command", "description", "url", "prompt", "name", "content"):
        v = data.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
    for v in data.values():
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def _rel(p) -> str:
    return "/".join(str(p or "").split("/")[-2:])


def describe_tool(name: str, data: dict | None) -> Event:
    data = data or {}
    if name == "Bash":
        return Event("shell", str(data.get("command", "")))
    if name == "Read":
        return Event("tool", f"Read {_rel(data.get('file_path'))}")
    if name == "Write":
        return Event("file_change", f"Write {_rel(data.get('file_path'))}")
    if name in ("Edit", "NotebookEdit"):
        return Event("file_change", f"Edit {_rel(data.get('file_path') or data.get('notebook_path'))}")
    if name == "Grep":
        return Event("tool", f"Grep {data.get('pattern', '')}")
    if name == "Glob":
        return Event("tool", f"Glob {data.get('pattern', '')}")
    if name == "WebSearch":
        return Event("tool", f"Search {data.get('query', '')}")
    if name == "WebFetch":
        return Event("tool", f"Fetch {data.get('url', '')}")
    if name == "Task":
        return Event("tool", f"Agent {data.get('description', '')}")
    if name == "TodoWrite":
        return Event("tool", "Updated the plan")
    mcp = re.match(r"^mcp__(\w+)__(\w+)$", name or "")
    label = f"{mcp.group(1)}: {mcp.group(2)}" if mcp else str(name or "tool")
    arg = _summarize_input(data)
    return Event("tool", f"{label} {arg}" if arg else label)


def _claude_blocks(message: dict) -> list[Event]:
    blocks = message.get("content")
    if isinstance(blocks, str):
        return [Event("text", blocks)] if blocks.strip() else []
    if not isinstance(blocks, list):
        return []
    out: list[Event] = []
    for b in blocks:
        if not isinstance(b, dict):
            continue
        t = b.get("type")
        if t == "text" and str(b.get("text", "")).strip():
            out.append(Event("text", b["text"], raw=b))
        elif t == "thinking" and str(b.get("thinking", "")).strip():
            out.append(Event("reasoning", b["thinking"], raw=b))
        elif t == "tool_use":
            e = describe_tool(b.get("name", ""), b.get("input"))
            e.raw = b
            out.append(e)
    return out


def short_model(model_id: str) -> str:
    if not model_id:
        return ""
    m = re.search(r"(opus|sonnet|haiku|fable)[\w.-]*(\[1m\])?", model_id, re.I)
    return f"{m.group(1).lower()}{m.group(2) or ''}" if m else model_id


CLAUDE_PERMISSION_MODES = ["acceptEdits", "plan", "manual", "dontAsk", "auto"]
CLAUDE_EFFORT = ["low", "medium", "high", "xhigh", "max"]
CODEX_EFFORT = ["none", "minimal", "low", "medium", "high", "xhigh"]
CODEX_SANDBOX = ["read-only", "workspace-write"]


class Driver:
    name = ""
    binary = ""

    def __init__(self, approvals: str = ""):
        self.approvals = approvals

    def command(self, prompt: str, session_id: str | None, opts: TurnOptions | None = None) -> list[str]:
        raise NotImplementedError

    def parse(self, obj: dict) -> list[Event]:
        raise NotImplementedError

    def controls(self, caps: Capabilities) -> list[Control]:
        raise NotImplementedError

    def capabilities(self, init_raw: dict) -> Capabilities:
        return Capabilities()

    def expand_prompt(self, text: str, project_skills: list[str]) -> str:
        return text

    def available(self) -> bool:
        return shutil.which(self.binary) is not None

    def run(self, prompt, session_id=None, cwd=None, opts=None, on_spawn=None) -> Iterator[Event]:
        if not self.available():
            raise AgentError(
                f"{self.binary!r} is not on PATH. Install the {self.name} CLI and log in — "
                f"uac never talks to a model API itself."
            )
        proc = subprocess.Popen(
            self.command(prompt, session_id, opts),
            stdin=subprocess.DEVNULL,  # else `codex exec` blocks reading stdin
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            cwd=str(cwd) if cwd else None,
        )
        if on_spawn:
            on_spawn(proc)
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                yield Event("other", line)
                continue
            yield from self.parse(obj)
        proc.wait()
        if proc.returncode not in (0, None) and not getattr(proc, "_uac_killed", False):
            stderr = proc.stderr.read().strip() if proc.stderr else ""
            yield Event("error", stderr or f"{self.binary} exited {proc.returncode}")


class ClaudeDriver(Driver):
    name = "Claude Code"
    binary = "claude"

    def command(self, prompt, session_id, opts=None) -> list[str]:
        opts = opts or {}
        cmd = [self.binary, "-p", prompt, "--output-format", "stream-json", "--verbose"]
        cmd += ["--allowed-tools", "mcp__uac"]  # works regardless of workspace trust
        cmd += ["--permission-mode", opts.get("permission") or self.approvals]
        if opts.get("model"):
            cmd += ["--model", opts["model"]]
        if opts.get("effort"):
            cmd += ["--effort", opts["effort"]]
        if opts.get("subagent"):
            cmd += ["--agent", opts["subagent"]]
        if session_id:
            cmd += ["--resume", session_id]
        return cmd

    def parse(self, obj) -> list[Event]:
        t = obj.get("type")
        if t == "system":
            return [Event("session", obj.get("session_id", ""), raw=obj)] if obj.get("subtype") == "init" else []
        if t == "assistant":
            return _claude_blocks(obj.get("message", {}))
        if t == "user":
            return []  # tool results; the call is already shown
        if t == "result":
            return [Event("result", obj.get("result", ""), raw=obj)]
        if t == "error":
            return [Event("error", obj.get("error") or obj.get("message", ""), raw=obj)]
        return []

    def capabilities(self, init_raw) -> Capabilities:
        skills = init_raw.get("skills", []) or []
        commands = init_raw.get("slash_commands", []) or []
        return Capabilities(
            model=init_raw.get("model", ""),
            commands=[c for c in commands if c not in skills],
            skills=skills,
            subagents=init_raw.get("agents", []) or [],
        )

    def controls(self, caps) -> list[Control]:
        return [
            Control("permission", "Mode", CLAUDE_PERMISSION_MODES, blank="Mode",
                    default_value="acceptEdits", icon="⚡",
                    labels={"manual": "Manual", "acceptEdits": "Edit automatically",
                            "plan": "Plan", "auto": "Auto", "dontAsk": "Don't ask"},
                    descriptions={"manual": "Ask for approval before each edit",
                                  "acceptEdits": "Edit the selection or whole file",
                                  "plan": "Explore and present a plan before editing",
                                  "auto": "Approve safe actions, pause for risky ones",
                                  "dontAsk": "Don't ask again about allowed edits"}),
            Control("model", "Model", caps.models, blank="Model", default_value=short_model(caps.model)),
            Control("effort", "Effort", CLAUDE_EFFORT, blank="Effort", widget="dots"),
            Control("subagent", "Subagent", caps.subagents, blank="Agent", default_value="default"),
        ]


class CodexDriver(Driver):
    name = "Codex"
    binary = "codex"

    ITEM_KINDS = {
        "agent_message": "text",
        "reasoning": "reasoning",
        "command_execution": "shell",
        "mcp_tool_call": "tool",
        "web_search": "tool",
        "file_change": "file_change",
    }

    def command(self, prompt, session_id, opts=None) -> list[str]:
        opts = opts or {}
        # `--json` and `-c` overrides are valid on both `codex exec` and
        # `codex exec resume`. `--sandbox`/`--model` are ONLY valid on the
        # initial `exec` — passing them to `resume` makes Codex reject the whole
        # command. On a resumed turn the sandbox is inherited from the session
        # and the model is set through `-c` instead.
        flags = ["--json"]
        if opts.get("effort"):
            flags += ["-c", f'model_reasoning_effort="{opts["effort"]}"']
        if session_id:
            if opts.get("model"):
                flags += ["-c", f'model="{opts["model"]}"']
            return [self.binary, "exec", "resume", session_id, *flags, prompt]
        flags += ["--sandbox", opts.get("sandbox") or self.approvals]
        if opts.get("model"):
            flags += ["--model", opts["model"]]
        return [self.binary, "exec", *flags, prompt]

    def parse(self, obj) -> list[Event]:
        t = obj.get("type", "")
        if t == "thread.started":
            return [Event("session", obj.get("thread_id", ""), raw=obj)]
        if t == "item.completed":
            item = obj.get("item", {})
            kind = self.ITEM_KINDS.get(item.get("type", ""))
            if not kind:
                return []
            if item.get("type") == "mcp_tool_call":
                label = f"{item.get('server', 'mcp')}: {item.get('tool', '')}"
            else:
                label = self._item_text(item)
            return [Event(kind, label, raw=obj)] if label else []
        if t == "turn.completed":
            return [Event("result", raw=obj)]
        if t in ("turn.failed", "error"):
            err = obj.get("error") or {}
            text = err.get("message") if isinstance(err, dict) else str(err)
            return [Event("error", text or obj.get("message", ""), raw=obj)]
        return []

    @staticmethod
    def _item_text(item) -> str:
        for key in ("text", "message", "command", "path", "summary"):
            if item.get(key):
                return str(item[key])
        return ""

    def controls(self, caps) -> list[Control]:
        info = caps.model_info
        efforts = info[caps.model].efforts if caps.model in info and info[caps.model].efforts else CODEX_EFFORT
        return [
            Control("sandbox", "Mode", CODEX_SANDBOX, blank="Mode",
                    default_value="workspace-write", icon="✋",
                    labels={"read-only": "Read only", "workspace-write": "Edit workspace"},
                    descriptions={"read-only": "Read the workspace, change nothing",
                                  "workspace-write": "Edit files here, nothing outside"}),
            Control("model", "Model", list(info.keys()), blank="Model", editable=True,
                    default_value=caps.model,
                    labels={s: m.label for s, m in info.items()},
                    descriptions={s: m.description for s, m in info.items() if m.description}),
            Control("effort", "Effort", efforts, blank="Effort", widget="dots"),
        ]

    def expand_prompt(self, text, project_skills) -> str:
        m = re.match(r"^/([a-z0-9][a-z0-9-]*)\s*([\s\S]*)$", text, re.I)
        if not m or m.group(1) not in project_skills:
            return text
        name, rest = m.group(1), m.group(2).strip()
        task = f"\n\nThen apply it to this task: {rest}" if rest else ""
        return (f'Use the skill_load tool to load the project skill "{name}", then follow '
                f"its instructions exactly.{task}")


DRIVERS = {CLAUDE: ClaudeDriver, CODEX: CodexDriver}
DEFAULT_APPROVALS = {CLAUDE: "acceptEdits", CODEX: "workspace-write"}


def get_driver(agent: str, approvals: str | None = None) -> Driver:
    try:
        cls = DRIVERS[agent]
    except KeyError:
        raise AgentError(f"Unknown agent {agent!r}. Use one of: {', '.join(DRIVERS)}.") from None
    return cls(approvals if approvals is not None else DEFAULT_APPROVALS[agent])


def read_codex_models(home: Path | None = None) -> Capabilities:
    home = home or Path(os.path.expanduser("~"))
    caps = Capabilities()
    try:
        parsed = json.loads((home / ".codex" / "models_cache.json").read_text())
    except (OSError, json.JSONDecodeError):
        return caps
    for m in parsed.get("models", []):
        slug = m.get("slug")
        if not slug:
            continue
        levels = m.get("supported_reasoning_levels") or []
        caps.model_info[slug] = ModelInfo(
            label=m.get("display_name", slug),
            description=m.get("description", ""),
            efforts=[l.get("effort") if isinstance(l, dict) else l for l in levels if l],
            default_effort=m.get("default_reasoning_level", ""),
        )
    caps.models = list(caps.model_info.keys())
    return caps


def discover_capabilities(binary: str, cwd: Path) -> Capabilities:
    """Free (`/model`, num_turns 0) probe that yields Claude's skills, commands,
    subagents, current model, and the model list in one call."""
    caps = Capabilities()
    try:
        out = subprocess.run(
            [binary, "-p", "/model", "--output-format", "stream-json", "--verbose"],
            cwd=str(cwd), capture_output=True, text=True, timeout=25,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return caps
    driver = ClaudeDriver()
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if obj.get("type") == "system" and obj.get("subtype") == "init":
            models = caps.models
            caps = driver.capabilities(obj)
            caps.models = models
        elif obj.get("type") == "result":
            m = re.search(r"Available:\s*([^.\n]+)", str(obj.get("result", "")))
            if m:
                caps.models = [x.strip() for x in m.group(1).split(",")
                               if x.strip() and not x.strip().startswith("or ")]
    return caps


class ChatSession:
    """Multi-turn conversation with one agent; the CLI keeps its own history via
    the resumed session id."""

    def __init__(self, agent: str, cwd: Path | None = None, approvals: str | None = None):
        self.agent = agent
        self.driver = get_driver(agent, approvals)
        self.cwd = cwd
        self.session_id: str | None = None
        self._proc: subprocess.Popen | None = None

    def send(self, prompt: str, opts: TurnOptions | None = None) -> Iterator[Event]:
        self._proc = None
        for event in self.driver.run(prompt, self.session_id, self.cwd, opts, on_spawn=self._keep):
            if event.kind == "session" and event.text:
                self.session_id = event.text
            yield event
        self._proc = None

    def _keep(self, proc):
        self._proc = proc

    def abort(self) -> bool:
        proc = self._proc
        if proc is None or proc.poll() is not None:
            return False
        proc._uac_killed = True
        proc.terminate()
        return True
