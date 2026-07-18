import pytest

from uac import links
from uac.config import load_config
from uac.memory import MemoryStore


def _link(cfg, name):
    links.add_link(cfg, name)
    return load_config(cfg.root)  # re-read so links_read is populated


# --- registry ----------------------------------------------------------------


def test_register_and_resolve(project):
    links.register_project("myproj", project)
    MemoryStore(load_config(project).db_path)
    assert links.resolve_db("myproj") == project / ".agents" / "memory.db"


def test_register_is_idempotent(project):
    links.register_project("myproj", project)
    links.register_project("myproj", project)
    assert list(links.load_registry()) == ["myproj"]


def test_resolving_an_unknown_project_lists_known_ones(other_project):
    with pytest.raises(links.LinkError) as exc:
        links.resolve_db("nope")
    assert "nope" in str(exc.value)
    assert "other" in str(exc.value)


def test_unregister(project):
    links.register_project("myproj", project)
    assert links.unregister_project("myproj") is True
    assert links.unregister_project("myproj") is False


# --- link management ---------------------------------------------------------


def test_add_link_persists_to_config(cfg, other_project):
    reloaded = _link(cfg, "other")
    assert reloaded.links_read == ["other"]


def test_add_link_is_idempotent(cfg, other_project):
    links.add_link(cfg, "other")
    links.add_link(cfg, "other")
    assert load_config(cfg.root).links_read == ["other"]


def test_cannot_link_to_an_unregistered_project(cfg):
    with pytest.raises(links.LinkError):
        links.add_link(cfg, "ghost")


def test_cannot_link_to_self(cfg):
    with pytest.raises(links.LinkError, match="itself"):
        links.add_link(cfg, cfg.project_name)


def test_remove_link(cfg, other_project):
    reloaded = _link(cfg, "other")
    assert links.remove_link(reloaded, "other") is True
    assert load_config(cfg.root).links_read == []
    assert links.remove_link(reloaded, "other") is False


def test_adding_a_link_preserves_other_config(cfg, other_project):
    _link(cfg, "other")
    reloaded = load_config(cfg.root)
    assert reloaded.project_name == "testproj"
    assert reloaded.max_results == 5


# --- federated search (the point of D10) -------------------------------------


def test_finds_a_memory_from_a_linked_project_and_labels_it(cfg, other_project):
    MemoryStore(other_project.db_path).write("Kafka retention is 7 days", source="codex")
    reloaded = _link(cfg, "other")

    found = links.federated_search(reloaded, "kafka retention")

    assert len(found) == 1
    assert found[0].content == "Kafka retention is 7 days"
    assert found[0].origin_project == "other"  # so the agent can discount it


def test_without_a_link_the_other_project_is_invisible(cfg, other_project):
    MemoryStore(other_project.db_path).write("Kafka retention is 7 days")
    assert links.federated_search(cfg, "kafka retention") == []


def test_current_project_outranks_linked_projects(cfg, other_project):
    """A better-worded memory from elsewhere is still less likely to be right here."""
    MemoryStore(other_project.db_path).write("Kafka retention retention retention")
    MemoryStore(cfg.db_path).write("Kafka retention here")
    reloaded = _link(cfg, "other")

    found = links.federated_search(reloaded, "kafka retention")

    assert [m.origin_project for m in found] == ["current", "other"]


def test_global_memories_are_always_visible(cfg):
    links.global_store().write("I prefer tabs everywhere", kind="preference", scope="global")
    found = links.federated_search(cfg, "tabs")

    assert len(found) == 1
    assert found[0].origin_project == "global"


def test_links_are_read_only(cfg, other_project):
    """B reading A must never write to A — that's the whole safety property."""
    reloaded = _link(cfg, "other")
    links.federated_search(reloaded, "anything")
    links.write_store(reloaded).write("A memory in B")

    assert MemoryStore(other_project.db_path).count() == 0
    assert MemoryStore(reloaded.db_path).count() == 1


def test_writes_go_global_when_scope_is_global(cfg):
    cfg.scope = "global"
    links.write_store(cfg).write("A global fact", scope="global")

    assert links.global_store().count() == 1
    assert MemoryStore(cfg.db_path).count() == 0


def test_a_broken_link_does_not_break_search(cfg, other_project, monkeypatch):
    MemoryStore(cfg.db_path).write("Local memory about kafka")
    reloaded = _link(cfg, "other")
    links.unregister_project("other")  # project vanished from the registry

    found = links.federated_search(reloaded, "kafka")
    assert [m.content for m in found] == ["Local memory about kafka"]


def test_search_respects_the_limit_across_stores(cfg, other_project):
    for i in range(5):
        MemoryStore(other_project.db_path).write(f"kafka note {i}")
        MemoryStore(cfg.db_path).write(f"kafka local {i}")
    reloaded = _link(cfg, "other")

    assert len(links.federated_search(reloaded, "kafka", limit=3)) == 3


def test_access_is_recorded_on_the_owning_store(cfg, other_project):
    other_store = MemoryStore(other_project.db_path)
    other_store.write("Kafka retention is 7 days")
    reloaded = _link(cfg, "other")

    links.federated_search(reloaded, "kafka")
    assert other_store.recent()[0].access_count == 1
