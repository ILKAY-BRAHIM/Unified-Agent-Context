import pytest

from uac.memory import MemoryStore


def test_write_search_forget_roundtrip(store):
    mem_id = store.write("The staging deploy needs VPN access", kind="gotcha")

    found = store.search("staging deploy")
    assert [m.id for m in found] == [mem_id]
    assert found[0].content == "The staging deploy needs VPN access"
    assert found[0].kind == "gotcha"

    assert store.forget(mem_id) is True
    assert store.search("staging deploy") == []
    assert store.forget(mem_id) is False


def test_forget_removes_from_fts_index(store):
    """The FTS delete trigger must fire, or forgotten memories keep surfacing."""
    mem_id = store.write("Redis is used for the rate limiter")
    store.forget(mem_id)
    assert store.search("Redis") == []


def test_updating_content_reindexes(store):
    mem_id = store.write("Deploys go through Jenkins")
    with store._connect() as conn:
        conn.execute("UPDATE memories SET content = ? WHERE id = ?", ("Deploys go through GitHub Actions", mem_id))
    assert store.search("Jenkins") == []
    assert [m.id for m in store.search("GitHub Actions")] == [mem_id]


def test_search_ranks_more_relevant_first(store):
    store.write("Unrelated note about the CSS build step")
    target = store.write("Postgres connection pooling is capped at 20 in staging")
    store.write("Another note mentioning staging once")

    found = store.search("postgres pooling")
    assert found[0].id == target


def test_source_attribution_is_preserved(store):
    claude_id = store.write("Claude learned this", source="claude-code")
    codex_id = store.write("Codex learned this", source="codex")

    by_id = {m.id: m.source for m in store.recent()}
    assert by_id[claude_id] == "claude-code"
    assert by_id[codex_id] == "codex"


def test_tags_roundtrip(store):
    store.write("Tagged memory", tags=["deploy", "ci"])
    assert store.recent()[0].tags == ["deploy", "ci"]


def test_search_records_access(store):
    store.write("Access tracking works")
    store.search("access tracking")
    found = store.recent()[0]
    assert found.access_count == 1
    assert found.accessed_at is not None


@pytest.mark.parametrize("query", ['NEAR("a" "b")', "foo*", "-bar", 'quote" inside', "AND OR NOT", "()"])
def test_fts_operators_in_a_query_do_not_crash(store, query):
    """A model can emit anything as a query; FTS5 syntax must never raise."""
    store.write("Some memory content")
    store.search(query)  # must not raise


def test_empty_query_raises(store):
    with pytest.raises(ValueError):
        store.search("   ")


def test_rejects_empty_content(store):
    with pytest.raises(ValueError):
        store.write("   ")


def test_rejects_unknown_kind(store):
    with pytest.raises(ValueError, match="kind must be one of"):
        store.write("content", kind="nonsense")


def test_defaults_to_project_scope(store):
    store.write("A fact")
    assert store.recent()[0].scope == "project"
    assert store.recent()[0].origin_project == "current"


def test_store_creates_db_and_parent_dirs(tmp_path):
    db = tmp_path / "nested" / "memory.db"
    MemoryStore(db)
    assert db.exists()


def test_reopening_an_existing_db_preserves_rows(cfg):
    MemoryStore(cfg.db_path).write("Persisted across opens")
    assert MemoryStore(cfg.db_path).count() == 1
