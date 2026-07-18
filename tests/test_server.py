"""Tool annotations are load-bearing — see project.md §9a.

`codex exec` auto-cancels every MCP tool call that isn't advertised as read-only
(upstream openai/codex#16685, #24135). Marking our read tools `readOnlyHint:
true` is what lets headless Codex use the shared memory at all. Dropping these
annotations silently breaks Codex; nothing else would catch it.
"""

import pytest

from uac.server import mcp

READ_ONLY = ["memory_search", "skill_list", "skill_load", "project_context"]
MUTATING = ["memory_write", "memory_forget"]


async def _tool(name):
    for tool in await mcp.list_tools():
        if tool.name == name:
            return tool
    raise AssertionError(f"{name} is not exposed by the server")


@pytest.mark.asyncio
@pytest.mark.parametrize("name", READ_ONLY)
async def test_read_tools_are_marked_read_only(name):
    """Without this, headless Codex cancels the call and reads nothing."""
    tool = await _tool(name)
    assert tool.annotations is not None, f"{name} has no annotations"
    assert tool.annotations.readOnlyHint is True


@pytest.mark.asyncio
@pytest.mark.parametrize("name", MUTATING)
async def test_mutating_tools_are_not_marked_read_only(name):
    """Honesty matters more than convenience: claiming a write is read-only
    would smuggle it past a host's approval gate."""
    tool = await _tool(name)
    assert tool.annotations.readOnlyHint is False


@pytest.mark.asyncio
async def test_forget_is_advertised_as_destructive():
    tool = await _tool("memory_forget")
    assert tool.annotations.destructiveHint is True


@pytest.mark.asyncio
async def test_write_is_not_advertised_as_destructive():
    """Adding a memory is additive; it destroys nothing."""
    tool = await _tool("memory_write")
    assert tool.annotations.destructiveHint is False


@pytest.mark.asyncio
async def test_all_six_tools_are_exposed():
    assert {t.name for t in await mcp.list_tools()} == set(READ_ONLY) | set(MUTATING)


@pytest.mark.asyncio
@pytest.mark.parametrize("name", READ_ONLY + MUTATING)
async def test_every_tool_has_a_description(name):
    """Tool descriptions are the prompt (§5.3) — an undescribed tool never gets called."""
    tool = await _tool(name)
    assert tool.description and len(tool.description) > 40
