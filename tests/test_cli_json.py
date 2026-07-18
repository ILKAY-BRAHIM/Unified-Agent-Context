"""`uac memory list --json` is a contract, not a convenience.

The VS Code extension parses this output instead of opening memory.db, so
memory.py stays the only owner of the schema. Renaming or dropping a field here
breaks the sidebar silently — it would just render an empty list. These tests
are the only thing that would catch it.
"""

import json

import pytest

from uac.cli import main

# What extension/src/memory/view.ts reads off each row.
EXTENSION_FIELDS = ["id", "content", "kind", "source", "tags", "created_at", "origin_project"]


@pytest.fixture
def in_project(project, monkeypatch):
    monkeypatch.chdir(project)
    return project


def test_memory_list_json_has_every_field_the_extension_reads(in_project, capsys):
    main(["memory", "add", "Deploys need VPN", "--kind", "gotcha", "--tags", "deploy,ops"])
    capsys.readouterr()

    main(["memory", "list", "--json"])
    rows = json.loads(capsys.readouterr().out)

    assert len(rows) == 1
    for field in EXTENSION_FIELDS:
        assert field in rows[0], f"extension reads {field!r} but it is missing"
    assert rows[0]["tags"] == ["deploy", "ops"]
    assert rows[0]["kind"] == "gotcha"
    assert rows[0]["source"] == "human"
    assert rows[0]["origin_project"] == "current"


def test_memory_list_json_is_empty_array_not_prose(in_project, capsys):
    """An empty store must still parse — the sidebar would throw on '(no memories…)'."""
    main(["memory", "list", "--json"])
    assert json.loads(capsys.readouterr().out) == []


def test_memory_search_json_is_parseable_when_nothing_matches(in_project, capsys):
    main(["memory", "add", "Something unrelated"])
    capsys.readouterr()

    main(["memory", "search", "zzz-no-such-thing", "--json"])
    assert json.loads(capsys.readouterr().out) == []


def test_memory_list_respects_limit(in_project, capsys):
    for i in range(5):
        main(["memory", "add", f"Fact number {i}"])
    capsys.readouterr()

    main(["memory", "list", "--json", "--limit", "2"])
    assert len(json.loads(capsys.readouterr().out)) == 2


def test_human_output_is_still_the_default(in_project, capsys):
    """--json must be opt-in; the bare command stays readable in a terminal."""
    main(["memory", "add", "Deploys need VPN", "--kind", "gotcha"])
    capsys.readouterr()

    main(["memory", "list"])
    out = capsys.readouterr().out
    assert "Deploys need VPN" in out
    with pytest.raises(json.JSONDecodeError):
        json.loads(out)
