"""App-level tests driven through Textual's headless pilot against a fake CLI.

No real agent is spawned and no quota is spent: a tiny fake `claude` on PATH
emits a recorded stream-json turn, so the whole render pipeline (activity groups,
answer dedupe, collapse) is exercised for real.
"""

import os
import stat
from pathlib import Path

import pytest

from uac_tui.app import ChatApp

pytestmark = pytest.mark.asyncio

FAKE_CLAUDE = r'''#!/usr/bin/env python3
import json, sys, time
a = sys.argv[1:]
sid = a[a.index("--resume")+1] if "--resume" in a else "sess-1"
def emit(o): print(json.dumps(o), flush=True); time.sleep(0.01)
emit({"type":"system","subtype":"init","session_id":sid})
emit({"type":"assistant","message":{"content":[
  {"type":"thinking","thinking":"thinking about it"},
  {"type":"tool_use","name":"Read","input":{"file_path":"/w/app.py"}},
]}})
emit({"type":"assistant","message":{"content":[{"type":"text","text":"Done. Uses **pytest**."}]}})
emit({"type":"result","subtype":"success","result":"Done. Uses **pytest**."})
'''


@pytest.fixture
def fake_claude(tmp_path, monkeypatch):
    binp = tmp_path / "claude"
    binp.write_text(FAKE_CLAUDE)
    binp.chmod(binp.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    return tmp_path


async def _run_turn(app, pilot, prompt="hi"):
    app.query_one("#prompt").value = prompt
    await pilot.press("enter")
    for _ in range(200):
        await pilot.pause(0.01)
        if not app.streaming:
            return
    raise AssertionError("turn never finished")


async def test_a_turn_renders_answer_and_collapsed_activity(fake_claude, tmp_path):
    from textual.widgets import Collapsible, Markdown

    app = ChatApp(agent="claude-code", cwd=tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _run_turn(app, pilot, "what does this use?")

        answers = list(app.query(Markdown))
        groups = list(app.query(Collapsible))
        # One answer (the text block); `result` repeats it and must be deduped.
        assert len(answers) == 1
        # The working folds away once the answer lands.
        assert groups and all(g.collapsed for g in groups)


async def test_the_reply_is_not_duplicated(fake_claude, tmp_path):
    from textual.widgets import Markdown

    app = ChatApp(agent="claude-code", cwd=tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _run_turn(app, pilot)
        # assistant text + identical `result` => exactly one rendered answer.
        assert len(list(app.query(Markdown))) == 1


async def test_switching_agent_is_blocked_mid_turn(tmp_path):
    app = ChatApp(agent="claude-code", cwd=tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.streaming = True
        await pilot.press("ctrl+t")
        assert app.agent == "claude-code"  # refused while streaming


async def test_agent_switch_toggles_when_idle(tmp_path):
    app = ChatApp(agent="claude-code", cwd=tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("ctrl+t")
        assert app.agent == "codex"
        await pilot.press("ctrl+t")
        assert app.agent == "claude-code"


async def test_slash_shows_the_suggestion_box(tmp_path):
    from textual.widgets import OptionList

    app = ChatApp(agent="claude-code", cwd=tmp_path)
    async with app.run_test() as pilot:
        # discover() runs on mount as a worker and sets project_skills from the
        # real project; let it settle before we seed our own, or it clobbers us.
        await pilot.pause(0.3)
        app.project_skills = ["verify-web", "grill-me"]

        app.query_one("#prompt").focus()
        await pilot.press("/", "v", "e", "r")  # real keystrokes fire Input.Changed
        await pilot.pause()

        box = app.query_one("#suggest", OptionList)
        assert box.has_class("shown")
        assert box.option_count >= 1


async def test_memory_panel_toggles(tmp_path):
    app = ChatApp(agent="claude-code", cwd=tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert not app.query_one("#memory").has_class("shown")
        await pilot.press("ctrl+b")
        assert app.query_one("#memory").has_class("shown")
