from uac.hooks import CLAUDE, CODEX, NUDGE, session_start_payload, stop_payload


# --- SessionStart (D5) -------------------------------------------------------


def test_session_start_injects_agents_md_and_memories(cfg, store):
    store.write("Deploys need VPN", kind="gotcha", source="codex")
    out = session_start_payload(CLAUDE, {}, cfg, store)

    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert out["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    assert "Use tabs, never spaces." in ctx  # from AGENTS.md
    assert "Deploys need VPN" in ctx  # from memory
    assert "via codex" in ctx  # source attribution is visible


def test_session_start_works_with_no_memories(cfg, store):
    ctx = session_start_payload(CODEX, {}, cfg, store)["hookSpecificOutput"]["additionalContext"]
    assert "no memories saved yet" in ctx


def test_session_start_can_be_disabled(cfg, store):
    cfg.inject_context_on_start = False
    assert session_start_payload(CLAUDE, {}, cfg, store) == {}


# --- Stop (D4) ---------------------------------------------------------------
# The agents consume Stop output differently. Claude shows `reason` to the user
# and only reads hookSpecificOutput.additionalContext; Codex turns `reason` into
# the continuation prompt. Getting this backwards silently breaks the nudge.


def test_stop_gives_claude_the_nudge_via_additional_context(cfg):
    out = stop_payload(CLAUDE, {"session_id": "s1"}, cfg)
    assert out["decision"] == "block"
    assert out["hookSpecificOutput"]["additionalContext"] == NUDGE
    assert out["hookSpecificOutput"]["hookEventName"] == "Stop"
    assert NUDGE not in out["reason"]  # Claude never sees `reason`


def test_stop_gives_codex_the_nudge_via_reason(cfg):
    out = stop_payload(CODEX, {"session_id": "s1"}, cfg)
    assert out["decision"] == "block"
    assert out["reason"] == NUDGE
    assert "hookSpecificOutput" not in out


def test_stop_nudges_only_once_per_session(cfg):
    """Stop fires on every turn; blocking each one would never terminate."""
    assert stop_payload(CODEX, {"session_id": "s1"}, cfg) != {}
    assert stop_payload(CODEX, {"session_id": "s1"}, cfg) == {}


def test_stop_nudges_each_session_separately(cfg):
    assert stop_payload(CODEX, {"session_id": "s1"}, cfg) != {}
    assert stop_payload(CODEX, {"session_id": "s2"}, cfg) != {}


def test_stop_respects_stop_hook_active(cfg):
    """Claude sets this while already continuing from a Stop hook."""
    assert stop_payload(CLAUDE, {"session_id": "s1", "stop_hook_active": True}, cfg) == {}


def test_stop_can_be_disabled(cfg):
    cfg.flush_memory_on_end = False
    assert stop_payload(CLAUDE, {"session_id": "s1"}, cfg) == {}


def test_stop_defers_to_native_claude_auto_memory(cfg):
    """§5.6 conflict: if the user picked native Auto Memory, we stay out of Claude's way."""
    cfg.claude_auto_memory = "native"
    assert stop_payload(CLAUDE, {"session_id": "s1"}, cfg) == {}
    assert stop_payload(CODEX, {"session_id": "s1"}, cfg) != {}  # Codex unaffected


def test_stop_handles_a_missing_session_id(cfg):
    assert stop_payload(CODEX, {}, cfg) != {}
    assert stop_payload(CODEX, {}, cfg) == {}


def test_session_id_cannot_escape_the_state_dir(cfg):
    """session_id reaches us from the agent; it must not become a path traversal."""
    stop_payload(CODEX, {"session_id": "../../etc/passwd"}, cfg)
    assert not (cfg.root.parent / "etc").exists()
    assert list(cfg.state_dir.iterdir())  # marker landed inside the state dir
