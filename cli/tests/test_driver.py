"""Driver tests for the CLI app.

This is a deliberate second copy of the driver (see driver.py's docstring), so it
gets its own tests rather than leaning on the extension's. Shapes here are the
ones recorded from the real CLIs.
"""

import json
import subprocess
from pathlib import Path

import pytest

from uac_tui import driver, files


def claude():
    return driver.ClaudeDriver()


def codex():
    return driver.CodexDriver()


# --- multi-event parsing (the activity display depends on it) ---------------


def test_one_message_yields_text_and_the_tool_it_called():
    events = claude().parse(
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "text", "text": "I'll read it."},
                    {"type": "tool_use", "name": "Read", "input": {"file_path": "/a/b/x.py"}},
                ]
            },
        }
    )
    assert [(e.kind, e.text) for e in events] == [("text", "I'll read it."), ("tool", "Read b/x.py")]


def test_thinking_is_reasoning():
    events = claude().parse(
        {"type": "assistant", "message": {"content": [{"type": "thinking", "thinking": "hmm"}]}}
    )
    assert events[0].kind == "reasoning" and events[0].text == "hmm"


def test_tool_calls_read_as_actions():
    cases = [
        ({"name": "Bash", "input": {"command": "pytest -q"}}, "shell", "pytest -q"),
        ({"name": "Edit", "input": {"file_path": "/w/src/app.ts"}}, "file_change", "Edit src/app.ts"),
        ({"name": "Grep", "input": {"pattern": "TODO"}}, "tool", "Grep TODO"),
        ({"name": "mcp__uac__memory_search", "input": {"query": "status"}}, "tool", "uac: memory_search status"),
    ]
    for block, kind, text in cases:
        (e,) = claude().parse({"type": "assistant", "message": {"content": [{"type": "tool_use", **block}]}})
        assert (e.kind, e.text) == (kind, text)


def test_session_id_is_its_own_event():
    (e,) = claude().parse({"type": "system", "subtype": "init", "session_id": "abc"})
    assert e.kind == "session" and e.text == "abc"


def test_tool_results_are_not_echoed():
    assert claude().parse({"type": "user", "message": {"content": [{"type": "tool_result"}]}}) == []


def test_two_searches_are_distinguishable_by_argument():
    def q(query):
        (e,) = claude().parse(
            {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "ToolSearch", "input": {"query": query}}]}}
        )
        return e.text

    assert q("select:Read") != q("notebook jupyter")


# --- turn options -> flags --------------------------------------------------


def test_claude_flags():
    cmd = claude().command("hi", None, {"model": "opus", "effort": "high", "permission": "plan"})
    assert cmd[cmd.index("--model") + 1] == "opus"
    assert cmd[cmd.index("--effort") + 1] == "high"
    assert cmd[cmd.index("--permission-mode") + 1] == "plan"
    assert cmd[cmd.index("--allowed-tools") + 1] == "mcp__uac"


def test_codex_effort_is_a_config_override():
    cmd = codex().command("hi", None, {"effort": "high"})
    assert cmd[cmd.index("-c") + 1] == 'model_reasoning_effort="high"'


def test_codex_resumes_with_the_session_id():
    cmd = codex().command("next", "thread-1", {})
    assert cmd[:4] == ["codex", "exec", "resume", "thread-1"]


# --- codex model cache ------------------------------------------------------


def _fake_codex_home(tmp_path, models):
    (tmp_path / ".codex").mkdir()
    (tmp_path / ".codex" / "models_cache.json").write_text(json.dumps({"models": models}))
    return tmp_path


def test_codex_models_and_per_model_effort_from_cache(tmp_path):
    home = _fake_codex_home(tmp_path, [
        {"slug": "terra", "display_name": "Terra", "supported_reasoning_levels": [{"effort": "low"}, {"effort": "ultra"}]},
        {"slug": "mini", "display_name": "Mini", "supported_reasoning_levels": [{"effort": "low"}, {"effort": "high"}]},
    ])
    caps = driver.read_codex_models(home)
    assert caps.models == ["terra", "mini"]
    assert caps.model_info["terra"].efforts == ["low", "ultra"]
    assert "ultra" not in caps.model_info["mini"].efforts


def test_missing_codex_cache_is_not_fatal(tmp_path):
    assert driver.read_codex_models(tmp_path).models == []


# --- expand_prompt: project skills to both agents ---------------------------


def test_claude_gets_a_project_skill_as_is():
    assert claude().expand_prompt("/verify-web fix", ["verify-web"]) == "/verify-web fix"


def test_codex_gets_a_skill_load_instruction():
    out = codex().expand_prompt("/verify-web fix the build", ["verify-web"])
    assert "skill_load" in out and '"verify-web"' in out and "fix the build" in out


def test_a_command_we_dont_own_is_left_alone():
    assert codex().expand_prompt("/context", ["verify-web"]) == "/context"


# --- @ file search ----------------------------------------------------------


def test_file_search_prefers_shallow_paths(tmp_path):
    (tmp_path / "a.py").write_text("")
    (tmp_path / "deep").mkdir()
    (tmp_path / "deep" / "a.py").write_text("")
    hits = files.search_files(tmp_path, "a.py")
    assert hits[0] == ("a.py", "a.py")  # shallow one first


def test_file_search_excludes_junk(tmp_path):
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "junk.py").write_text("")
    (tmp_path / "real.py").write_text("")
    names = [p for _, p in files.search_files(tmp_path, "py")]
    assert "real.py" in names
    assert not any("node_modules" in p for p in names)
