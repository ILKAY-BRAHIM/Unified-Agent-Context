import pytest

from uac.config import load_config
from uac.memory import MemoryStore


@pytest.fixture(autouse=True)
def isolated_home(tmp_path_factory, monkeypatch):
    """Redirect the machine-global registry + global.db away from the real ~/.agents.

    Autouse on purpose: a test that forgets this would silently write into the
    developer's actual home directory.
    """
    monkeypatch.setenv("UAC_HOME", str(tmp_path_factory.mktemp("uac-home")))


@pytest.fixture
def project(tmp_path):
    """A scaffolded project root with config + AGENTS.md."""
    (tmp_path / ".agents").mkdir()
    (tmp_path / ".agents" / "config.toml").write_text(
        '[project]\nname = "testproj"\n\n[memory]\nmax_results = 5\n'
    )
    (tmp_path / "AGENTS.md").write_text("# testproj\n\nUse tabs, never spaces.\n")
    return tmp_path


@pytest.fixture
def cfg(project):
    return load_config(project)


@pytest.fixture
def store(cfg):
    return MemoryStore(cfg.db_path)


@pytest.fixture
def skill_file(project):
    """Write a skill into .agents/skills/."""

    def _write(name, description="When you need to do the thing.", body="# Body\n\nSteps here."):
        directory = project / ".agents" / "skills"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{name}.md"
        path.write_text(f"---\nname: {name}\ndescription: {description}\n---\n\n{body}\n")
        return path

    return _write


@pytest.fixture
def other_project(tmp_path_factory):
    """A second, independently registered project to link against."""
    from uac import links

    root = tmp_path_factory.mktemp("other")
    (root / ".agents").mkdir()
    (root / ".agents" / "config.toml").write_text('[project]\nname = "other"\n')
    other_cfg = load_config(root)
    MemoryStore(other_cfg.db_path)
    links.register_project("other", root)
    return other_cfg
