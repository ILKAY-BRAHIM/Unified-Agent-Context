import pytest

from uac import skills


def test_parses_frontmatter_and_body(project, skill_file):
    skill_file("deploy-runbook", description="Use when deploying.", body="# Deploy\n\nRun make.")
    found = skills.discover(project)

    assert len(found) == 1
    assert found[0].name == "deploy-runbook"
    assert found[0].description == "Use when deploying."
    assert found[0].body == "# Deploy\n\nRun make."


def test_discover_is_sorted_and_finds_all(project, skill_file):
    skill_file("zebra")
    skill_file("alpha")
    assert [s.name for s in skills.discover(project)] == ["alpha", "zebra"]


def test_no_skills_dir_is_not_an_error(project):
    assert skills.discover(project) == []


def test_skill_list_never_returns_bodies(project, skill_file):
    """Progressive disclosure: the index must stay cheap."""
    skill_file("secret-heavy", body="A" * 5000)
    entry = skills.discover(project)[0].index_entry()

    assert set(entry) == {"name", "description"}
    assert "A" * 100 not in str(entry)


def test_load_returns_the_body(project, skill_file):
    skill_file("runbook", body="# Runbook\n\nStep one.")
    assert skills.load(project, "runbook").body == "# Runbook\n\nStep one."


def test_load_unknown_skill_lists_what_exists(project, skill_file):
    skill_file("real-skill")
    with pytest.raises(skills.SkillError) as exc:
        skills.load(project, "imaginary")
    assert "imaginary" in str(exc.value)
    assert "real-skill" in str(exc.value)  # tells you what you could have meant


# --- malformed skills must fail loudly, and always name the file -------------


def _bad(project, name, text):
    directory = project / ".agents" / "skills"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.md"
    path.write_text(text)
    return path


def test_missing_frontmatter_names_the_file(project):
    path = _bad(project, "broken", "# Just markdown, no frontmatter\n")
    with pytest.raises(skills.SkillError, match="frontmatter"):
        skills.discover(project)
    with pytest.raises(skills.SkillError) as exc:
        skills.parse_skill(path)
    assert "broken.md" in str(exc.value)


def test_invalid_yaml_names_the_file(project):
    _bad(project, "badyaml", "---\nname: [unclosed\n---\n\nbody\n")
    with pytest.raises(skills.SkillError) as exc:
        skills.discover(project)
    assert "badyaml.md" in str(exc.value)


def test_missing_description_explains_why_it_matters(project):
    _bad(project, "nodesc", "---\nname: nodesc\n---\n\nbody\n")
    with pytest.raises(skills.SkillError) as exc:
        skills.discover(project)
    assert "description" in str(exc.value)
    assert "nodesc.md" in str(exc.value)


def test_missing_name_fails(project):
    _bad(project, "noname", "---\ndescription: Something.\n---\n\nbody\n")
    with pytest.raises(skills.SkillError, match="name"):
        skills.discover(project)


def test_name_must_match_filename(project):
    """Otherwise skill_load(name) can't find the file."""
    _bad(project, "actual-file", "---\nname: different-name\ndescription: X.\n---\n\nbody\n")
    with pytest.raises(skills.SkillError, match="does not match the filename"):
        skills.discover(project)


def test_name_must_be_kebab_case(project):
    _bad(project, "Bad_Name", "---\nname: Bad_Name\ndescription: X.\n---\n\nbody\n")
    with pytest.raises(skills.SkillError, match="kebab-case"):
        skills.discover(project)


def test_frontmatter_must_be_a_mapping(project):
    _bad(project, "listy", "---\n- one\n- two\n---\n\nbody\n")
    with pytest.raises(skills.SkillError, match="mapping"):
        skills.discover(project)


def test_render_index_shows_when_to_use(project, skill_file):
    skill_file("deploy-runbook", description="Use when shipping to prod.")
    rendered = skills.render_index(skills.discover(project))
    assert rendered == "- deploy-runbook: Use when shipping to prod."


def test_render_index_with_no_skills(project):
    assert skills.render_index([]) == "(no skills defined)"


# --- importing Claude's own user skills -------------------------------------
# Only ~/.claude/skills/<name>/SKILL.md exists on disk. Claude's other skills
# (deep-research, dataviz, verify …) are compiled into its binary, so there is
# nothing to import — the CLI has to say so rather than look broken.


@pytest.fixture
def fake_claude_home(tmp_path, monkeypatch):
    """A stand-in ~/.claude/skills — never touch the developer's real one."""
    root = tmp_path / "claude-skills"
    monkeypatch.setattr(skills, "claude_skills_dir", lambda: root)

    def _add(name, description="Use when doing the thing.", body="# Body\n\nSteps."):
        d = root / name
        d.mkdir(parents=True, exist_ok=True)
        (d / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {description}\n---\n\n{body}\n")

    return _add


def test_discovers_claude_user_skills(fake_claude_home):
    fake_claude_home("grill-me", description="Stress-test a plan.")
    found = skills.discover_claude_skills()
    assert [s.name for s in found] == ["grill-me"]
    assert found[0].description == "Stress-test a plan."


def test_name_comes_from_the_directory_not_the_filename(fake_claude_home):
    """Claude's layout is <name>/SKILL.md, so the filename is always SKILL."""
    fake_claude_home("frontend-design")
    assert skills.discover_claude_skills()[0].name == "frontend-design"


def test_import_makes_claude_skills_usable_by_both_agents(project, fake_claude_home):
    fake_claude_home("grill-me", body="# Grill\n\nAsk hard questions.")
    imported, skipped = skills.import_from_claude(project)

    assert imported == ["grill-me"]
    assert skipped == []
    # It is now an ordinary project skill: Codex reaches it via skill_load.
    loaded = skills.load(project, "grill-me")
    assert "Ask hard questions." in loaded.body


def test_import_does_not_clobber_an_edited_copy(project, fake_claude_home, skill_file):
    fake_claude_home("grill-me", body="original from claude")
    skill_file("grill-me", body="my edited version")

    imported, skipped = skills.import_from_claude(project)
    assert imported == []
    assert skipped == ["grill-me"]
    assert "my edited version" in skills.load(project, "grill-me").body


def test_import_overwrite_replaces(project, fake_claude_home, skill_file):
    fake_claude_home("grill-me", body="fresh from claude")
    skill_file("grill-me", body="stale")

    imported, _ = skills.import_from_claude(project, overwrite=True)
    assert imported == ["grill-me"]
    assert "fresh from claude" in skills.load(project, "grill-me").body


def test_nothing_to_import_when_claude_has_no_user_skills(project, fake_claude_home):
    assert skills.discover_claude_skills() == []
    assert skills.import_from_claude(project) == ([], [])


def test_a_malformed_claude_skill_is_skipped_not_fatal(project, fake_claude_home):
    """Claude's format is close to ours but not guaranteed identical."""
    fake_claude_home("good")
    bad = skills.claude_skills_dir() / "bad"
    bad.mkdir(parents=True)
    (bad / "SKILL.md").write_text("no frontmatter here")

    imported, _ = skills.import_from_claude(project)
    assert imported == ["good"]
