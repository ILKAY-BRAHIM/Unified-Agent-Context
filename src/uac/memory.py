"""SQLite + FTS5 memory store (D3: explicit write, never silent capture)."""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from .config import VALID_KINDS

SCHEMA = """
CREATE TABLE IF NOT EXISTS memories (
    id           TEXT PRIMARY KEY,
    content      TEXT NOT NULL,
    kind         TEXT NOT NULL,
    scope        TEXT NOT NULL DEFAULT 'project',
    source       TEXT NOT NULL,
    tags         TEXT,
    created_at   TEXT NOT NULL,
    accessed_at  TEXT,
    access_count INTEGER DEFAULT 0
);

CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
    content, tags, content='memories', content_rowid='rowid'
);

CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
    INSERT INTO memories_fts(rowid, content, tags) VALUES (new.rowid, new.content, new.tags);
END;

CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, content, tags)
    VALUES ('delete', old.rowid, old.content, old.tags);
END;

-- Scoped to content/tags so bookkeeping writes (accessed_at, access_count)
-- don't churn the FTS index.
CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE OF content, tags ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, content, tags)
    VALUES ('delete', old.rowid, old.content, old.tags);
    INSERT INTO memories_fts(rowid, content, tags) VALUES (new.rowid, new.content, new.tags);
END;
"""


@dataclass
class Memory:
    id: str
    content: str
    kind: str
    scope: str
    source: str
    tags: list[str]
    created_at: str
    accessed_at: str | None = None
    access_count: int = 0
    # Set at query time. Phase 1 is always "current"; Phase 3 (D10) fills in
    # linked project names so the agent can discount foreign memories.
    origin_project: str = "current"

    def to_dict(self) -> dict:
        return asdict(self)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _fts_query(raw: str) -> str:
    """Turn a natural-language query into a safe FTS5 MATCH expression.

    Every term is quoted, so FTS5 operators a model might emit ("-", "*", "NEAR")
    are treated as literals and can't blow up the query. Terms are OR-ed for
    recall; bm25 ranking sorts out precision.
    """
    terms = [t.replace('"', '""') for t in raw.split() if t.strip()]
    if not terms:
        raise ValueError("search query is empty")
    return " OR ".join(f'"{t}"' for t in terms)


def _row_to_memory(row: sqlite3.Row, origin: str = "current") -> Memory:
    return Memory(
        id=row["id"],
        content=row["content"],
        kind=row["kind"],
        scope=row["scope"],
        source=row["source"],
        tags=json.loads(row["tags"]) if row["tags"] else [],
        created_at=row["created_at"],
        accessed_at=row["accessed_at"],
        access_count=row["access_count"],
        origin_project=origin,
    )


class MemoryStore:
    """One SQLite file per project. Phase 1 handles the current project only;
    global routing and cross-project federation land in Phase 3 (D10)."""

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def write(
        self,
        content: str,
        kind: str = "fact",
        tags: list[str] | None = None,
        source: str = "unknown",
        scope: str = "project",
    ) -> str:
        content = (content or "").strip()
        if not content:
            raise ValueError("cannot save an empty memory")
        if kind not in VALID_KINDS:
            raise ValueError(f"kind must be one of {', '.join(VALID_KINDS)}; got {kind!r}")

        mem_id = str(uuid.uuid4())
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO memories (id, content, kind, scope, source, tags, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    mem_id,
                    content,
                    kind,
                    scope,
                    source,
                    json.dumps(tags or []),
                    _now(),
                ),
            )
        return mem_id

    def search_scored(
        self, query: str, limit: int = 5, origin: str = "current"
    ) -> list[tuple[Memory, float]]:
        """Ranked hits with their bm25 score (lower is better).

        Doesn't record access — federated search (D10) merges across stores and
        only some hits survive the final limit, so the caller decides what
        counts as "accessed".
        """
        match = _fts_query(query)
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT m.*, bm25(memories_fts) AS score FROM memories_fts f
                   JOIN memories m ON m.rowid = f.rowid
                   WHERE memories_fts MATCH ?
                   ORDER BY score, m.created_at DESC
                   LIMIT ?""",
                (match, limit),
            ).fetchall()
        return [(_row_to_memory(r, origin), r["score"]) for r in rows]

    def record_access(self, ids: list[str]) -> None:
        if not ids:
            return
        with self._connect() as conn:
            conn.executemany(
                "UPDATE memories SET accessed_at = ?, access_count = access_count + 1 WHERE id = ?",
                [(_now(), i) for i in ids],
            )

    def search(self, query: str, limit: int = 5) -> list[Memory]:
        found = [m for m, _ in self.search_scored(query, limit)]
        self.record_access([m.id for m in found])
        return found

    def forget(self, mem_id: str) -> bool:
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM memories WHERE id = ?", (mem_id,))
        return cur.rowcount > 0

    def get(self, mem_id: str) -> Memory | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM memories WHERE id = ?", (mem_id,)).fetchone()
        return _row_to_memory(row) if row else None

    def recent(self, limit: int = 10) -> list[Memory]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM memories ORDER BY created_at DESC, rowid DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [_row_to_memory(r) for r in rows]

    def count(self) -> int:
        with self._connect() as conn:
            return conn.execute("SELECT COUNT(*) AS n FROM memories").fetchone()["n"]
