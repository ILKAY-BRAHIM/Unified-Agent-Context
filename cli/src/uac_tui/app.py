"""Full-screen terminal chat for Claude Code and Codex.

A sibling to the VS Code extension: same idea, independent app. The headline
feature is switching between the two agents in one place, with your shared
memory and skills alongside.
"""

from __future__ import annotations

from pathlib import Path

from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Collapsible, Input, Label, Markdown, OptionList, Static
from textual.widgets.option_list import Option

from . import files
from .driver import (
    CLAUDE,
    CODEX,
    AgentError,
    Capabilities,
    ChatSession,
    Control,
    Event,
    discover_capabilities,
    get_driver,
    read_codex_models,
)

# Shared uac core — memory and skills only, never UI. This is the one seam the
# two front ends meet at.
try:
    from uac import links, skills as uac_skills
    from uac.config import find_project_root, load_config
    from uac.memory import MemoryStore
    _CORE = True
except Exception:  # pragma: no cover - core should be installed alongside
    _CORE = False

AGENT_LABEL = {CLAUDE: "Claude Code", CODEX: "Codex"}
MARKERS = {"shell": "$", "tool": "▸", "file_change": "±", "reasoning": "…"}


def _at_token(value: str) -> str | None:
    """The word being typed after an `@`, or None. `@` must start the value or
    follow a space, so `me@example.com` doesn't trigger it."""
    import re

    m = re.search(r"(?:^|\s)@([^\s]*)$", value)
    return m.group(1) if m else None


def _replace_at(value: str, path: str) -> str:
    """Replace the `@token` under the cursor with the chosen path, keeping the
    rest of the sentence."""
    import re

    return re.sub(r"(^|\s)@[^\s]*$", lambda m: f"{m.group(1)}@{path} ", value)


# --- messages posted from the streaming worker thread -----------------------


class StreamEvent(Message):
    def __init__(self, event: Event) -> None:
        self.event = event
        super().__init__()


class StreamDone(Message):
    def __init__(self, error: str = "") -> None:
        self.error = error
        super().__init__()


# --- a modal list picker, reused for model / effort / mode / subagent -------


class PickScreen(ModalScreen[str | None]):
    """Choose one value for a Control. Returns the raw value, "" for default, or
    None if cancelled."""

    BINDINGS = [Binding("escape", "dismiss_none", "Cancel")]

    def __init__(self, control: Control, current: str) -> None:
        self.control = control
        self.current = current
        super().__init__()

    def compose(self) -> ComposeResult:
        c = self.control
        opts: list[Option] = [Option(f"Default ({c.labels.get(c.default_value, c.default_value)})"
                                     if c.default_value else "Default", id="")]
        for v in c.values:
            label = c.labels.get(v, v)
            desc = c.descriptions.get(v)
            text = f"{label}\n  {desc}" if desc else label
            opts.append(Option(text, id=v))
        with Vertical(id="picker"):
            yield Label(c.label, id="picker-title")
            ol = OptionList(*opts, id="picker-list")
            yield ol

    def on_mount(self) -> None:
        ol = self.query_one(OptionList)
        # Land on the current choice.
        for i, opt in enumerate(ol._options):
            if opt.id == (self.current or ""):
                ol.highlighted = i
                break

    @on(OptionList.OptionSelected)
    def _picked(self, ev: OptionList.OptionSelected) -> None:
        self.dismiss(ev.option.id or "")

    def action_dismiss_none(self) -> None:
        self.dismiss(None)


class ChatApp(App):
    TITLE = "uac"
    SUB_TITLE = "unified agent context"

    CSS = """
    Screen { layout: vertical; }

    #body { height: 1fr; }
    #log { width: 1fr; padding: 1 2; }
    #memory { width: 42; border-left: solid $panel; padding: 0 1; display: none; }
    #memory.shown { display: block; }
    #memory-title { text-style: bold; color: $text-muted; padding: 1 0; }

    .you { color: $text-muted; margin-top: 1; }
    .who { text-style: bold; margin-top: 1; }
    /* uac brand: peach for Claude, sky for Codex — they read well on a dark terminal. */
    .who.claude-code { color: #ffbe91; }
    .who.codex { color: #cfebff; }
    .answer { margin: 0 0 1 0; }
    .activity { color: $text-muted; }
    .act-line { color: $text-muted; }
    .act-line.shell { color: $text; }
    .err { color: $error; margin: 1 0; }

    #controls { height: auto; padding: 0 2; color: $text-muted; }
    #controls .set { color: $accent; text-style: bold; }

    #composer { height: auto; border-top: solid $panel; padding: 1 2; }
    #prompt { border: round $panel; }
    #prompt:focus { border: round $accent; }

    #suggest { display: none; height: auto; max-height: 10; margin: 0 2; border: round $panel; }
    #suggest.shown { display: block; }

    #picker { width: 60; height: auto; max-height: 24; background: $panel; border: round $accent; padding: 1; }
    #picker-title { text-style: bold; padding: 0 0 1 0; }
    """

    BINDINGS = [
        Binding("ctrl+t", "switch_agent", "Switch agent"),
        Binding("ctrl+g", "pick('model')", "Model"),
        Binding("ctrl+e", "pick('effort')", "Effort"),
        Binding("ctrl+p", "pick('mode')", "Mode"),
        Binding("ctrl+b", "toggle_memory", "Memory"),
        Binding("ctrl+c", "stop_or_quit", "Stop / Quit", priority=True),
    ]

    def __init__(self, agent: str, cwd: Path) -> None:
        super().__init__()
        self.cwd = cwd
        self.agent = agent
        self.sessions: dict[str, ChatSession] = {}
        self.caps: dict[str, Capabilities] = {CLAUDE: Capabilities(), CODEX: Capabilities()}
        self.chosen: dict[str, dict[str, str]] = {CLAUDE: {}, CODEX: {}}
        self.project_skills: list[str] = []
        self.streaming = False
        self._activity: Collapsible | None = None
        self._turn_prose = False
        self._suggest_kind = ""  # "" | "/" | "@"

    # --- layout ---

    def compose(self) -> ComposeResult:
        with Horizontal(id="body"):
            yield VerticalScroll(id="log")
            with VerticalScroll(id="memory"):
                yield Label("Shared memory", id="memory-title")
        yield Static(id="controls")
        with Vertical(id="composer"):
            yield OptionList(id="suggest")
            yield Input(placeholder="Ask about this project…   (/ skills · @ files · ctrl+t switch)", id="prompt")

    def on_mount(self) -> None:
        self._greet()
        self.render_controls()
        self.query_one("#prompt", Input).focus()
        self.discover()

    def _greet(self) -> None:
        log = self.query_one("#log", VerticalScroll)
        # A little terminal brand mark: a two-tone chip (peach over sky, the two
        # agents) beside the wordmark.
        log.mount(Static(
            "[#ffbe91]▛▜[/]  [b]uac[/b]\n"
            "[#cfebff]▙▟[/]  [dim]unified agent context[/dim]\n",
            markup=True))
        log.mount(Static(
            "Ask Claude or Codex — both read the same project memory, so whatever one "
            "learns the other can use.\n", classes="greet"))

    # --- discovery (models, skills) runs off the UI thread ---

    @work(thread=True)
    def discover(self) -> None:
        codex = read_codex_models()
        if codex.models:
            self.call_from_thread(self._set_caps, CODEX, codex)
        if _CORE:
            try:
                self.project_skills = [s.name for s in uac_skills.discover(self.cwd)]
            except Exception:
                self.project_skills = []
        claude_bin = get_driver(CLAUDE).binary
        caps = discover_capabilities(claude_bin, self.cwd)
        if caps.skills or caps.models:
            self.call_from_thread(self._set_caps, CLAUDE, caps)

    def _set_caps(self, agent: str, caps: Capabilities) -> None:
        self.caps[agent] = caps
        self.render_controls()

    # --- controls bar ---

    def _controls(self) -> list[Control]:
        return get_driver(self.agent).controls(self.caps[self.agent])

    def render_controls(self) -> None:
        parts = [f"[b]{AGENT_LABEL[self.agent]}[/b]"]
        for c in self._controls():
            if not c.values and not c.editable:
                continue
            chosen = self.chosen[self.agent].get(c.id)
            if c.widget == "dots":
                scale = self._effort_scale(c)
                idx = scale.index(chosen) if chosen in scale else -1
                dots = "".join("●" if i <= idx else "○" for i in range(len(scale)))
                label = f"{c.label} {dots}"
            else:
                shown = c.labels.get(chosen or c.default_value, chosen or c.default_value or c.blank)
                label = f"{c.icon} {shown}".strip()
            parts.append(f"[{'b' if chosen else 'dim'}]{label}[/]")
        parts.append("[dim]^t agent  ^g model  ^e effort  ^p mode  ^b memory[/dim]")
        self.query_one("#controls", Static).update("   ".join(parts))

    def _effort_scale(self, control: Control) -> list[str]:
        """Effort follows the chosen model — codex scales differ per model."""
        if control.id != "effort":
            return control.values
        model = self.chosen[self.agent].get("model") or self.caps[self.agent].model
        info = self.caps[self.agent].model_info.get(model)
        return info.efforts if info and info.efforts else control.values

    # --- pickers ---

    def action_pick(self, which: str) -> None:
        cid = {"model": "model", "effort": "effort", "mode": self._mode_id()}[which]
        control = next((c for c in self._controls() if c.id == cid), None)
        if not control or (not control.values and not control.editable):
            self.notify(f"{which} isn't available for {AGENT_LABEL[self.agent]}")
            return
        if control.id == "effort":
            control = Control(**{**control.__dict__, "values": self._effort_scale(control)})
        current = self.chosen[self.agent].get(control.id, "")

        def done(value: str | None) -> None:
            if value is None:
                return
            if value:
                self.chosen[self.agent][control.id] = value
            else:
                self.chosen[self.agent].pop(control.id, None)
            self.render_controls()

        self.push_screen(PickScreen(control, current), done)

    def _mode_id(self) -> str:
        return "permission" if self.agent == CLAUDE else "sandbox"

    def action_switch_agent(self) -> None:
        if self.streaming:
            self.notify("Finish or stop the current turn first.")
            return
        self.agent = CODEX if self.agent == CLAUDE else CLAUDE
        self.render_controls()

    def action_toggle_memory(self) -> None:
        panel = self.query_one("#memory", VerticalScroll)
        panel.toggle_class("shown")
        if panel.has_class("shown"):
            self.load_memory()

    @work(thread=True)
    def load_memory(self) -> None:
        rows: list[str] = []
        if _CORE:
            try:
                cfg = load_config(self.cwd)
                for m in MemoryStore(cfg.db_path).recent(30):
                    tag = "" if m.origin_project == "current" else f" ({m.origin_project})"
                    rows.append(f"[dim]{m.kind}·{m.source}{tag}[/dim]\n{m.content}\n")
            except Exception as exc:
                rows = [f"[dim]could not read memory: {exc}[/dim]"]
        self.call_from_thread(self._show_memory, rows or ["[dim](no memories yet)[/dim]"])

    def _show_memory(self, rows: list[str]) -> None:
        panel = self.query_one("#memory", VerticalScroll)
        for old in panel.query(".mem"):
            old.remove()
        for r in rows:
            panel.mount(Static(r, classes="mem", markup=True))

    # --- the / and @ suggestion box ---

    @on(Input.Changed, "#prompt")
    def _on_typed(self, ev: Input.Changed) -> None:
        value = ev.value
        if value.startswith("/") and " " not in value:
            self._show_suggest("/", value[1:])
        else:
            m = _at_token(value)
            if m is not None:
                self._show_suggest("@", m)
            else:
                self._hide_suggest()

    def _show_suggest(self, kind: str, term: str) -> None:
        self._suggest_kind = kind
        ol = self.query_one("#suggest", OptionList)
        ol.clear_options()
        if kind == "/":
            rows = self._slash_matches(term)
            for name, hint in rows:
                ol.add_option(Option(f"/{name}   [dim]{hint}[/dim]", id=name))
        else:
            for name, path in files.search_files(self.cwd, term):
                ol.add_option(Option(f"{name}   [dim]{path}[/dim]", id=path))
        if ol.option_count:
            ol.add_class("shown")
            ol.highlighted = 0
        else:
            self._hide_suggest()

    def _slash_matches(self, term: str) -> list[tuple[str, str]]:
        caps = self.caps[self.agent]
        out = [(s, "project skill") for s in self.project_skills]
        out += [(s, "skill") for s in caps.skills if s not in self.project_skills]
        out += [(c, "command") for c in caps.commands]
        term = term.lower()
        return [r for r in out if term in r[0].lower()][:12]

    def _hide_suggest(self) -> None:
        self._suggest_kind = ""
        self.query_one("#suggest", OptionList).remove_class("shown")

    @on(OptionList.OptionSelected, "#suggest")
    def _suggest_chosen(self, ev: OptionList.OptionSelected) -> None:
        inp = self.query_one("#prompt", Input)
        if self._suggest_kind == "/":
            inp.value = f"/{ev.option.id} "
        else:
            inp.value = _replace_at(inp.value, str(ev.option.id))
        inp.cursor_position = len(inp.value)
        self._hide_suggest()
        inp.focus()

    # --- sending a turn ---

    @on(Input.Submitted, "#prompt")
    def _submit(self, ev: Input.Submitted) -> None:
        if self._suggest_kind:
            ol = self.query_one("#suggest", OptionList)
            if ol.highlighted is not None:
                self._suggest_chosen(OptionList.OptionSelected(ol, ol.highlighted,
                                                               ol.get_option_at_index(ol.highlighted)))
            return
        text = ev.value.strip()
        if not text or self.streaming:
            return
        self.query_one("#prompt", Input).value = ""
        self._add_user(text)
        self.run_turn(text)

    def _add_user(self, text: str) -> None:
        self.query_one("#log", VerticalScroll).mount(Static(f"› {text}", classes="you"))

    def session(self) -> ChatSession:
        if self.agent not in self.sessions:
            self.sessions[self.agent] = ChatSession(self.agent, cwd=self.cwd)
        return self.sessions[self.agent]

    @work(thread=True)
    def run_turn(self, text: str) -> None:
        driver = get_driver(self.agent)
        prompt = driver.expand_prompt(text, self.project_skills)
        opts = dict(self.chosen[self.agent])
        session = self.session()
        self.call_from_thread(self._turn_start)
        try:
            for event in session.send(prompt, opts):
                self.post_message(StreamEvent(event))
            self.post_message(StreamDone())
        except AgentError as exc:
            self.post_message(StreamDone(error=str(exc)))

    def _turn_start(self) -> None:
        self.streaming = True
        self._activity = None
        self._turn_prose = False
        log = self.query_one("#log", VerticalScroll)
        log.mount(Static(AGENT_LABEL[self.agent], classes=f"who {self.agent}"))
        log.scroll_end(animate=False)

    @on(StreamEvent)
    def _on_event(self, msg: StreamEvent) -> None:
        e = msg.event
        log = self.query_one("#log", VerticalScroll)
        if e.kind == "session" or not e.text:
            return
        if e.kind in ("text", "result"):
            # Claude repeats its reply in `result`; a slash command answers only
            # there. Show result only when the turn produced no prose of its own.
            if e.kind == "result" and self._turn_prose:
                return
            self._close_activity()
            self._turn_prose = True
            log.mount(Markdown(e.text, classes="answer"))
        elif e.kind == "error":
            self._close_activity()
            log.mount(Static(f"! {e.text}", classes="err"))
        else:
            self._activity_line(e)
        log.scroll_end(animate=False)

    def _close_activity(self) -> None:
        """Fold the working away once the answer (or an error) arrives."""
        if self._activity is not None:
            self._activity.collapsed = True
            self._activity = None

    def _activity_line(self, e: Event) -> None:
        log = self.query_one("#log", VerticalScroll)
        if self._activity is None:
            self._activity = Collapsible(title="working…", classes="activity", collapsed=False)
            log.mount(self._activity)
        marker = MARKERS.get(e.kind, " ")
        self._activity.mount(Static(f"{marker} {e.text}", classes=f"act-line {e.kind}"))
        n = len(self._activity.query(".act-line"))
        self._activity.title = f"{n} step{'s' if n != 1 else ''}"

    @on(StreamDone)
    def _on_done(self, msg: StreamDone) -> None:
        self.streaming = False
        self._close_activity()
        if msg.error:
            self.query_one("#log", VerticalScroll).mount(Static(f"! {msg.error}", classes="err"))
        self.query_one("#log", VerticalScroll).scroll_end(animate=False)

    def action_stop_or_quit(self) -> None:
        if self.streaming:
            self.session().abort()
            self.notify("Stopping…")
        else:
            self.exit()
