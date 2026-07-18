"""Driver tests (Phase 4a).

These check the parsers against *recorded* event shapes. That catches
regressions, but it does NOT prove the shapes match what the real CLIs emit —
neither CLI was installed when this was written. Re-record against real output
before trusting `uac chat`.
"""

import json

import pytest

from uac.agents import ChatSession, ClaudeDriver, CodexDriver, Event, get_driver
from uac.agents import AgentError
from uac.hooks import CLAUDE, CODEX

# --- recorded streams --------------------------------------------------------

CLAUDE_STREAM = [
    {"type": "system", "subtype": "init", "session_id": "sess-claude-1", "tools": ["Read"]},
    {"type": "assistant", "message": {"content": [{"type": "text", "text": "Looking at it."}]}},
    {"type": "user", "message": {"content": [{"type": "tool_result", "content": "ok"}]}},
    {"type": "assistant", "message": {"content": [{"type": "text", "text": "Done."}]}},
    {"type": "result", "subtype": "success", "result": "Done.", "session_id": "sess-claude-1"},
]

CODEX_STREAM = [
    {"type": "thread.started", "thread_id": "thread-codex-1"},
    {"type": "turn.started"},
    {"type": "item.completed", "item": {"type": "reasoning", "text": "Thinking about it."}},
    {"type": "item.completed", "item": {"type": "command_execution", "command": "pytest -q"}},
    {"type": "item.completed", "item": {"type": "file_change", "path": "src/app.py"}},
    {"type": "item.completed", "item": {"type": "agent_message", "text": "All tests pass."}},
    {"type": "turn.completed", "usage": {"input_tokens": 10}},
]


def parse_all(driver, stream):
    return [driver.parse(obj) for obj in stream]


# --- Claude ------------------------------------------------------------------


def test_claude_command_shape():
    cmd = ClaudeDriver().command("do the thing", None)
    assert cmd[:2] == ["claude", "-p"]
    assert "do the thing" in cmd
    assert "--output-format" in cmd and "stream-json" in cmd
    assert "--resume" not in cmd


def test_claude_resumes_with_a_session_id():
    cmd = ClaudeDriver().command("next turn", "sess-abc")
    assert "--resume" in cmd
    assert cmd[cmd.index("--resume") + 1] == "sess-abc"


def test_claude_passes_the_permission_mode():
    cmd = get_driver(CLAUDE).command("go", None)
    assert cmd[cmd.index("--permission-mode") + 1] == "acceptEdits"


def test_claude_pre_approves_our_mcp_tools_on_the_command_line():
    """settings.json permissions are ignored until the workspace is trusted, so
    without this every memory_write is denied and Claude silently falls back to
    its own private memory — the exact split-brain this project prevents."""
    cmd = get_driver(CLAUDE).command("go", None)
    assert cmd[cmd.index("--allowed-tools") + 1] == "mcp__uac"


def test_codex_passes_the_sandbox_mode():
    cmd = get_driver(CODEX).command("go", None)
    assert cmd[cmd.index("--sandbox") + 1] == "workspace-write"


def test_approvals_can_be_overridden_or_left_to_the_cli():
    assert "--sandbox" not in get_driver(CODEX, approvals="").command("go", None)
    cmd = get_driver(CODEX, approvals="read-only").command("go", None)
    assert cmd[cmd.index("--sandbox") + 1] == "read-only"


def test_defaults_are_not_the_dangerous_modes():
    """acceptEdits/workspace-write let the agent work; bypass/full-access hand it the machine."""
    assert "bypassPermissions" not in get_driver(CLAUDE).command("go", None)
    assert "danger-full-access" not in get_driver(CODEX).command("go", None)


def test_claude_stream_parses_into_expected_kinds():
    events = parse_all(ClaudeDriver(), CLAUDE_STREAM)
    assert [e.kind for e in events] == ["session", "text", "other", "text", "result"]
    assert events[0].text == "sess-claude-1"
    assert events[1].text == "Looking at it."
    assert events[4].text == "Done."


def test_claude_concatenates_multiple_text_blocks():
    event = ClaudeDriver().parse(
        {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]},
        }
    )
    assert event.text == "ab"


def test_claude_ignores_non_text_blocks():
    event = ClaudeDriver().parse(
        {
            "type": "assistant",
            "message": {"content": [{"type": "tool_use", "name": "Read"}, {"type": "text", "text": "hi"}]},
        }
    )
    assert event.text == "hi"


# --- Codex -------------------------------------------------------------------


def test_codex_command_shape():
    cmd = CodexDriver().command("do the thing", None)
    assert cmd == ["codex", "exec", "--json", "do the thing"]


def test_codex_resumes_with_a_thread_id():
    assert CodexDriver().command("next", "thread-1") == [
        "codex", "exec", "resume", "thread-1", "--json", "next",
    ]


def test_codex_stream_parses_into_expected_kinds():
    events = parse_all(CodexDriver(), CODEX_STREAM)
    assert [e.kind for e in events] == [
        "session", "other", "reasoning", "tool", "file_change", "text", "result",
    ]
    assert events[0].text == "thread-codex-1"
    assert events[3].text == "pytest -q"
    assert events[4].text == "src/app.py"
    assert events[5].text == "All tests pass."


def test_codex_turn_failed_is_an_error():
    event = CodexDriver().parse({"type": "turn.failed", "error": {"message": "rate limited"}})
    assert event.kind == "error"
    assert event.text == "rate limited"


# --- shared ------------------------------------------------------------------


@pytest.mark.parametrize("driver", [ClaudeDriver(), CodexDriver()])
def test_unknown_events_degrade_to_other_rather_than_crash(driver):
    """Both CLIs ship new event types constantly; that must not break the chat."""
    event = driver.parse({"type": "something.invented.tomorrow", "payload": {"x": 1}})
    assert event.kind == "other"
    assert event.raw["type"] == "something.invented.tomorrow"


@pytest.mark.parametrize("driver", [ClaudeDriver(), CodexDriver()])
def test_empty_event_does_not_crash(driver):
    assert driver.parse({}).kind == "other"


def test_get_driver_rejects_unknown_agents():
    with pytest.raises(AgentError, match="Unknown agent"):
        get_driver("gemini")


def test_session_captures_and_reuses_the_session_id(monkeypatch):
    """Turn 1 has no session id; turn 2 must resume with the one turn 1 returned."""
    session = ChatSession(CODEX)
    commands = []

    def fake_run(prompt, session_id=None, cwd=None):
        commands.append(session_id)
        yield Event(kind="session", text="thread-xyz")
        yield Event(kind="text", text="hello")

    monkeypatch.setattr(session.driver, "run", fake_run)

    list(session.send("first"))
    assert session.session_id == "thread-xyz"
    list(session.send("second"))

    assert commands == [None, "thread-xyz"]


def test_send_text_collects_the_final_answer(monkeypatch):
    session = ChatSession(CLAUDE)
    monkeypatch.setattr(
        session.driver,
        "run",
        lambda *a, **k: iter(
            [Event(kind="text", text="thinking"), Event(kind="result", text="final answer")]
        ),
    )
    assert session.send_text("go") == "final answer"


def test_send_text_raises_on_a_failed_turn(monkeypatch):
    session = ChatSession(CLAUDE)
    monkeypatch.setattr(
        session.driver, "run", lambda *a, **k: iter([Event(kind="error", text="not logged in")])
    )
    with pytest.raises(AgentError, match="not logged in"):
        session.send_text("go")


def test_missing_cli_explains_itself(monkeypatch):
    driver = ClaudeDriver()
    monkeypatch.setattr("uac.agents.shutil.which", lambda _: None)
    with pytest.raises(AgentError, match="not on PATH"):
        list(driver.run("hi"))


def test_stdin_is_closed_for_the_child(monkeypatch):
    """`codex exec` reads stdin when it isn't a TTY and appends it to the prompt.
    An open pipe hangs every turn forever — this must never regress."""
    import subprocess as sp

    captured = {}

    class FakeProc:
        stdout = iter([])
        stderr = None
        returncode = 0

        def wait(self):
            return 0

    def fake_popen(cmd, **kwargs):
        captured.update(kwargs)
        return FakeProc()

    monkeypatch.setattr("uac.agents.shutil.which", lambda _: "/usr/bin/codex")
    monkeypatch.setattr("uac.agents.subprocess.Popen", fake_popen)
    list(CodexDriver().run("hi"))

    assert captured["stdin"] is sp.DEVNULL


def test_real_claude_init_event_is_recognised():
    """Recorded from a real `claude -p --output-format stream-json` run."""
    event = ClaudeDriver().parse(
        {"type": "system", "subtype": "init", "session_id": "212ba2d0-ae5a", "apiKeySource": "none"}
    )
    assert event.kind == "session"
    assert event.text == "212ba2d0-ae5a"


def test_real_claude_rate_limit_event_does_not_crash():
    """Real streams carry event types the docs never mention."""
    assert ClaudeDriver().parse({"type": "rate_limit_event", "rate_limit_info": {}}).kind == "other"


def test_real_codex_error_uses_a_top_level_message():
    """Recorded from a real `codex exec --json` run: no nested `error` object."""
    event = CodexDriver().parse({"type": "error", "message": "Reconnecting... 2/5 (401)"})
    assert event.kind == "error"
    assert "Reconnecting" in event.text
