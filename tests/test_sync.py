import json

import tomlkit

from uac import sync


def test_registers_both_agents(cfg):
    sync.register_all(cfg)

    assert (cfg.root / ".mcp.json").exists()
    assert (cfg.root / ".claude" / "settings.json").exists()
    assert (cfg.root / ".codex" / "config.toml").exists()
    assert (cfg.root / ".codex" / "hooks.json").exists()


def test_mcp_registration_tags_each_agent_as_its_own_source(cfg):
    """Attribution depends on this: each agent spawns its own server process."""
    sync.register_claude_mcp(cfg)
    sync.register_codex_mcp(cfg)

    claude = json.loads((cfg.root / ".mcp.json").read_text())
    assert claude["mcpServers"]["uac"]["env"]["UAC_SOURCE"] == "claude-code"

    codex = tomlkit.parse((cfg.root / ".codex" / "config.toml").read_text())
    assert codex["mcp_servers"]["uac"]["env"]["UAC_SOURCE"] == "codex"


def test_hooks_registered_for_both_events(cfg):
    sync.register_claude_hooks(cfg)
    hooks = json.loads((cfg.root / ".claude" / "settings.json").read_text())["hooks"]

    assert "session-start" in hooks["SessionStart"][0]["hooks"][0]["command"]
    assert "--agent claude-code" in hooks["SessionStart"][0]["hooks"][0]["command"]
    assert "hook stop" in hooks["Stop"][0]["hooks"][0]["command"]
    # Stop takes no matcher in either agent.
    assert "matcher" not in hooks["Stop"][0]


def test_our_mcp_tools_are_pre_approved(cfg):
    """Without this the real CLI denies mcp__uac__memory_write and nothing is ever saved."""
    sync.register_claude_hooks(cfg)
    data = json.loads((cfg.root / ".claude" / "settings.json").read_text())
    assert "mcp__uac" in data["permissions"]["allow"]


def test_permission_allowlist_is_idempotent_and_keeps_user_rules(cfg):
    path = cfg.root / ".claude" / "settings.json"
    path.parent.mkdir()
    path.write_text(json.dumps({"permissions": {"allow": ["Bash(git *)"], "deny": ["Bash(rm *)"]}}))

    sync.register_claude_hooks(cfg)
    sync.register_claude_hooks(cfg)
    perms = json.loads(path.read_text())["permissions"]

    assert perms["allow"].count("mcp__uac") == 1
    assert "Bash(git *)" in perms["allow"]
    assert perms["deny"] == ["Bash(rm *)"]


def test_register_is_idempotent(cfg):
    sync.register_all(cfg)
    first = (cfg.root / ".claude" / "settings.json").read_text()
    sync.register_all(cfg)
    second = (cfg.root / ".claude" / "settings.json").read_text()

    assert first == second
    hooks = json.loads(second)["hooks"]
    assert len(hooks["SessionStart"]) == 1
    assert len(hooks["Stop"]) == 1


def test_registration_preserves_the_users_own_hooks(cfg):
    path = cfg.root / ".claude" / "settings.json"
    path.parent.mkdir()
    path.write_text(
        json.dumps(
            {
                "model": "opus",
                "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "make lint"}]}]},
            }
        )
    )

    sync.register_claude_hooks(cfg)
    data = json.loads(path.read_text())

    assert data["model"] == "opus"  # unrelated settings untouched
    commands = [h["command"] for e in data["hooks"]["Stop"] for h in e["hooks"]]
    assert "make lint" in commands
    assert any("hook stop" in c for c in commands)


def test_codex_registration_preserves_existing_toml(cfg):
    path = cfg.root / ".codex" / "config.toml"
    path.parent.mkdir()
    path.write_text('model = "gpt-5"\n\n[mcp_servers.other]\ncommand = "other-server"\n')

    sync.register_codex_mcp(cfg)
    doc = tomlkit.parse(path.read_text())

    assert doc["model"] == "gpt-5"
    assert doc["mcp_servers"]["other"]["command"] == "other-server"
    assert doc["mcp_servers"]["uac"]["env"]["UAC_SOURCE"] == "codex"


def test_gitignore_covers_generated_state(cfg):
    sync.ensure_gitignore(cfg)
    body = (cfg.root / ".gitignore").read_text()
    for entry in sync.GITIGNORE_ENTRIES:
        assert entry in body


def test_gitignore_is_idempotent_and_keeps_user_entries(cfg):
    (cfg.root / ".gitignore").write_text("node_modules/\n")
    sync.ensure_gitignore(cfg)
    sync.ensure_gitignore(cfg)
    lines = (cfg.root / ".gitignore").read_text().splitlines()

    assert "node_modules/" in lines
    assert lines.count(".agents/memory.db") == 1


def test_generated_claude_md_points_at_agents_md(cfg):
    sync.generate_claude_md(cfg)
    body = cfg.claude_md.read_text()

    assert sync.GENERATED_HEADER in body
    assert "@AGENTS.md" in body


def test_claude_md_includes_claude_only_extras(cfg):
    cfg.claude_extra = "- Use /self-review before PRs"
    sync.generate_claude_md(cfg)
    body = cfg.claude_md.read_text()

    assert "## Claude-specific" in body
    assert "- Use /self-review before PRs" in body


def test_claude_md_generation_is_idempotent(cfg):
    sync.generate_claude_md(cfg)
    first = cfg.claude_md.read_text()
    sync.generate_claude_md(cfg)
    assert cfg.claude_md.read_text() == first


def test_refuses_to_clobber_a_hand_written_claude_md(cfg):
    cfg.claude_md.write_text("# My hand-written notes\n")
    try:
        sync.generate_claude_md(cfg)
    except SystemExit as exc:
        assert "Refusing to overwrite" in str(exc)
        assert cfg.claude_md.read_text() == "# My hand-written notes\n"
    else:
        raise AssertionError("expected uac to refuse to overwrite hand-written CLAUDE.md")


def test_mirrors_skills_natively_for_claude(cfg, skill_file):
    skill_file("deploy-runbook", description="Use when deploying.", body="# Deploy\n\nRun make.")
    sync.mirror_skills(cfg)

    mirrored = cfg.root / ".claude" / "skills" / "deploy-runbook" / "SKILL.md"
    body = mirrored.read_text()
    assert "name: deploy-runbook" in body
    assert "description: Use when deploying." in body
    assert "Run make." in body


def test_mirror_drops_skills_that_no_longer_exist(cfg, skill_file):
    path = skill_file("temporary")
    sync.mirror_skills(cfg)
    assert (cfg.root / ".claude" / "skills" / "temporary").exists()

    path.unlink()
    sync.mirror_skills(cfg)
    assert not (cfg.root / ".claude" / "skills" / "temporary").exists()


# --- sync --check: the pre-commit guard that keeps the mirror from rotting ----


def test_check_is_clean_right_after_sync(cfg, skill_file):
    skill_file("runbook")
    sync.sync_all(cfg)
    assert sync.stale_paths(cfg) == []


def test_check_flags_a_missing_claude_md(cfg):
    assert any("CLAUDE.md is missing" in p for p in sync.stale_paths(cfg))


def test_check_flags_an_edited_skill(cfg, skill_file):
    skill_file("runbook", body="original")
    sync.sync_all(cfg)
    skill_file("runbook", body="edited after sync")

    assert any("out of date" in p for p in sync.stale_paths(cfg))


def test_check_flags_an_orphaned_mirror(cfg, skill_file):
    path = skill_file("runbook")
    sync.sync_all(cfg)
    path.unlink()

    assert any("no matching skill" in p for p in sync.stale_paths(cfg))


def test_check_flags_stale_claude_md_after_config_change(cfg):
    sync.sync_all(cfg)
    cfg.claude_extra = "- new rule"
    assert any("CLAUDE.md is out of date" in p for p in sync.stale_paths(cfg))


def test_invalid_existing_json_fails_loudly(cfg):
    path = cfg.root / ".claude" / "settings.json"
    path.parent.mkdir()
    path.write_text("{not json")

    try:
        sync.register_claude_hooks(cfg)
    except SystemExit as exc:
        assert "not valid JSON" in str(exc)
    else:
        raise AssertionError("expected a loud failure on malformed settings.json")
