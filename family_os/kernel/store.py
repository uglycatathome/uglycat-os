"""Store service — SQLite-backed persistent storage for Family AI OS.

Provides a low-level data-access layer for all kernel services and plugins.
Manages 8 tables (entities, facts, events, decisions, documents, llm_calls,
llm_cache, snapshots) with WAL journaling and foreign-key enforcement.
"""

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any


class Store:
    """SQLite storage service.

    Parameters
    ----------
    db_path : str | Path
        Filesystem path to the SQLite database file.
    """

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = Path(db_path)

    # ── internal helpers ─────────────────────────────────────────────────────

    def _connect(self) -> sqlite3.Connection:
        """Open a new connection with sane defaults."""
        conn = sqlite3.connect(str(self._db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _list_tables(self) -> list[str]:
        """Return a list of application table names in the database (for testing).

        Internal SQLite tables (e.g. ``sqlite_sequence``) are excluded.
        """
        conn = self._connect()
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
        conn.close()
        return [r["name"] for r in rows]

    # ── schema management ────────────────────────────────────────────────────

    def initialize(self) -> None:
        """Create all tables and indexes (idempotent)."""
        conn = self._connect()

        conn.executescript("""

        CREATE TABLE IF NOT EXISTS entities (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            type        TEXT    NOT NULL,
            name        TEXT    NOT NULL,
            extra       TEXT    DEFAULT '{}',
            created_at  TEXT    DEFAULT (datetime('now')),
            updated_at  TEXT    DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS facts (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            type        TEXT    NOT NULL,
            key         TEXT    NOT NULL,
            value       TEXT    NOT NULL,
            member      TEXT    DEFAULT '全家',
            sensitivity TEXT    DEFAULT 'private',
            source      TEXT    DEFAULT 'manual_cmd',
            format      TEXT    DEFAULT 'structured',
            valid_from  TEXT,
            valid_to    TEXT,
            created_at  TEXT    DEFAULT (datetime('now')),
            updated_at  TEXT    DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS events (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            entity_id   INTEGER REFERENCES entities(id),
            event_type  TEXT    NOT NULL,
            description TEXT    NOT NULL,
            scheduled_at TEXT,
            completed_at TEXT,
            status      TEXT    DEFAULT 'pending',
            sensitivity TEXT    DEFAULT 'private',
            source      TEXT    DEFAULT 'system',
            created_at  TEXT    DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS decisions (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            entity_id       INTEGER REFERENCES entities(id),
            title           TEXT    NOT NULL,
            rationale       TEXT    NOT NULL,
            prediction      TEXT,
            outcome         TEXT,
            score           INTEGER,
            status          TEXT    DEFAULT 'pending',
            approved_by     TEXT,
            rejected_reason TEXT,
            created_at      TEXT    DEFAULT (datetime('now')),
            updated_at      TEXT    DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS documents (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            path        TEXT    NOT NULL UNIQUE,
            doc_type    TEXT    NOT NULL,
            entity_id   INTEGER REFERENCES entities(id),
            tags        TEXT    DEFAULT '[]',
            sensitivity TEXT    DEFAULT 'private',
            source      TEXT    DEFAULT 'manual',
            format      TEXT    DEFAULT 'document',
            created_at  TEXT    DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS llm_calls (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            plugin_name     TEXT    NOT NULL,
            model           TEXT    NOT NULL,
            prompt_tokens   INTEGER DEFAULT 0,
            completion_tokens INTEGER DEFAULT 0,
            cost            REAL    DEFAULT 0.0,
            cache_hit       INTEGER DEFAULT 0,
            created_at      TEXT    DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS llm_cache (
            cache_key   TEXT    PRIMARY KEY,
            response    TEXT    NOT NULL,
            model       TEXT    NOT NULL,
            created_at  TEXT    DEFAULT (datetime('now')),
            ttl_hours   INTEGER DEFAULT 24
        );

        CREATE INDEX IF NOT EXISTS idx_facts_type       ON facts(type);
        CREATE INDEX IF NOT EXISTS idx_facts_member     ON facts(member);
        CREATE INDEX IF NOT EXISTS idx_entities_type    ON entities(type);
        CREATE INDEX IF NOT EXISTS idx_events_status    ON events(status);
        CREATE INDEX IF NOT EXISTS idx_decisions_status ON decisions(status);
        CREATE INDEX IF NOT EXISTS idx_llm_calls_plugin ON llm_calls(plugin_name);
        CREATE INDEX IF NOT EXISTS idx_llm_calls_created ON llm_calls(created_at);

        CREATE TABLE IF NOT EXISTS snapshots (
            key         TEXT    PRIMARY KEY,
            payload     TEXT    NOT NULL,
            created_at  TEXT    DEFAULT (datetime('now'))
        );
        """)

        conn.commit()
        conn.close()

    # ── Entities ─────────────────────────────────────────────────────────────

    def add_entity(self, entity_type: str, name: str,
                   extra: dict | None = None) -> int:
        """Insert a new entity and return its id."""
        extra_json = json.dumps(extra or {}, ensure_ascii=False)
        conn = self._connect()
        cur = conn.execute(
            "INSERT INTO entities (type, name, extra) VALUES (?, ?, ?)",
            (entity_type, name, extra_json),
        )
        conn.commit()
        pk = cur.lastrowid
        conn.close()
        return pk

    def get_entities(self, entity_type: str | None = None) -> list[sqlite3.Row]:
        """Return entities, optionally filtered by type."""
        conn = self._connect()
        if entity_type:
            rows = conn.execute(
                "SELECT * FROM entities WHERE type = ? ORDER BY id",
                (entity_type,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM entities ORDER BY id"
            ).fetchall()
        conn.close()
        return rows

    # ── Facts (with dedup) ───────────────────────────────────────────────────

    def _find_fact(self, fact_type: str, key: str, value: str) -> int | None:
        """Return the id of an existing fact matching type+key+value, or None."""
        conn = self._connect()
        row = conn.execute(
            "SELECT id FROM facts WHERE type = ? AND key = ? AND value = ?",
            (fact_type, key, value),
        ).fetchone()
        conn.close()
        return row["id"] if row else None

    def _update_fact(self, fact_id: int, **kwargs: Any) -> None:
        """Update arbitrary columns on an existing fact."""
        set_clause = ", ".join(f"{k} = ?" for k in kwargs)
        if not set_clause:
            return
        values = list(kwargs.values()) + [fact_id]
        conn = self._connect()
        conn.execute(
            f"UPDATE facts SET {set_clause}, updated_at = datetime('now') "
            "WHERE id = ?",
            values,
        )
        conn.commit()
        conn.close()

    def add_fact(
        self,
        fact_type: str,
        key: str,
        value: str,
        member: str = "全家",
        sensitivity: str = "private",
        source: str = "manual_cmd",
        fmt: str = "structured",
        valid_from: str | None = None,
        valid_to: str | None = None,
        **kwargs: Any,
    ) -> int:
        """Insert a fact, or update an existing one with the same type+key+value.

        Returns the fact id (new or existing).
        """
        existing = self._find_fact(fact_type, key, value)
        if existing is not None:
            update_fields = {
                "member": member,
                "sensitivity": sensitivity,
                "source": source,
                "format": fmt,
            }
            if valid_from is not None:
                update_fields["valid_from"] = valid_from
            if valid_to is not None:
                update_fields["valid_to"] = valid_to
            update_fields.update(kwargs)
            self._update_fact(existing, **update_fields)
            return existing

        conn = self._connect()
        cur = conn.execute(
            """INSERT INTO facts (type, key, value, member, sensitivity,
                                  source, format, valid_from, valid_to)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (fact_type, key, value, member, sensitivity, source, fmt,
             valid_from, valid_to),
        )
        conn.commit()
        pk = cur.lastrowid
        conn.close()
        return pk

    def get_fact(self, fact_id: int) -> sqlite3.Row | None:
        """Fetch a single fact by id, or None."""
        conn = self._connect()
        row = conn.execute(
            "SELECT * FROM facts WHERE id = ?", (fact_id,)
        ).fetchone()
        conn.close()
        return row

    def query_facts(
        self,
        type: str | None = None,
        key: str | None = None,
        member: str | None = None,
        **extra_filters: Any,
    ) -> list[sqlite3.Row]:
        """Return facts matching the supplied filters.

        Filters are combined with AND.  Only equality filters are supported
        via the extra_filters dict.
        """
        conditions: list[str] = []
        params: list[Any] = []

        if type is not None:
            conditions.append("type = ?")
            params.append(type)
        if key is not None:
            conditions.append("key = ?")
            params.append(key)
        if member is not None:
            conditions.append("member = ?")
            params.append(member)

        for col, val in extra_filters.items():
            conditions.append(f"{col} = ?")
            params.append(val)

        where = ""
        if conditions:
            where = "WHERE " + " AND ".join(conditions)

        conn = self._connect()
        rows = conn.execute(
            f"SELECT * FROM facts {where} ORDER BY id", params
        ).fetchall()
        conn.close()
        return rows

    # ── Events ───────────────────────────────────────────────────────────────

    def add_event(
        self,
        entity_id: int,
        event_type: str,
        description: str,
        scheduled_at: str | None = None,
        completed_at: str | None = None,
        status: str = "pending",
        sensitivity: str = "private",
        source: str = "system",
    ) -> int:
        """Insert a new event and return its id."""
        conn = self._connect()
        cur = conn.execute(
            """INSERT INTO events (entity_id, event_type, description,
                                   scheduled_at, completed_at, status,
                                   sensitivity, source)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (entity_id, event_type, description, scheduled_at, completed_at,
             status, sensitivity, source),
        )
        conn.commit()
        pk = cur.lastrowid
        conn.close()
        return pk

    def get_pending_events(self) -> list[sqlite3.Row]:
        """Return all events with status='pending'."""
        conn = self._connect()
        rows = conn.execute(
            "SELECT * FROM events WHERE status = 'pending' ORDER BY id"
        ).fetchall()
        conn.close()
        return rows

    # ── Decisions ────────────────────────────────────────────────────────────

    def add_decision(
        self,
        entity_id: int,
        title: str,
        rationale: str,
        prediction: str | None = None,
        outcome: str | None = None,
        score: int | None = None,
        status: str = "pending",
    ) -> int:
        """Insert a new decision and return its id."""
        conn = self._connect()
        cur = conn.execute(
            """INSERT INTO decisions (entity_id, title, rationale, prediction,
                                      outcome, score, status)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (entity_id, title, rationale, prediction, outcome, score, status),
        )
        conn.commit()
        pk = cur.lastrowid
        conn.close()
        return pk

    def get_pending_decisions(self) -> list[sqlite3.Row]:
        """Return all decisions with status='pending'."""
        conn = self._connect()
        rows = conn.execute(
            "SELECT * FROM decisions WHERE status = 'pending' ORDER BY id"
        ).fetchall()
        conn.close()
        return rows

    def update_decision_status(
        self,
        decision_id: int,
        status: str,
        approved_by: str | None = None,
        rejected_reason: str | None = None,
    ) -> None:
        """Update a decision's status and optionally approval/rejection info."""
        conn = self._connect()
        conn.execute(
            """UPDATE decisions
               SET status = ?, approved_by = ?, rejected_reason = ?,
                   updated_at = datetime('now')
               WHERE id = ?""",
            (status, approved_by, rejected_reason, decision_id),
        )
        conn.commit()
        conn.close()

    # ── Documents ────────────────────────────────────────────────────────────

    def add_document(
        self,
        path: str,
        doc_type: str,
        entity_id: int | None = None,
        tags: list[str] | None = None,
        sensitivity: str = "private",
        source: str = "manual",
        fmt: str = "document",
    ) -> int:
        """Insert a document record and return its id.

        Raises
        ------
        sqlite3.IntegrityError
            If *path* already exists.
        """
        tags_json = json.dumps(tags or [], ensure_ascii=False)
        conn = self._connect()
        cur = conn.execute(
            """INSERT INTO documents (path, doc_type, entity_id, tags,
                                      sensitivity, source, format)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (path, doc_type, entity_id, tags_json, sensitivity, source, fmt),
        )
        conn.commit()
        pk = cur.lastrowid
        conn.close()
        return pk

    # ── Snapshots (generic latest-state key-value store) ──────────────────

    def save_snapshot(self, key: str, payload: dict) -> None:
        """Upsert a latest-state snapshot (idempotent by key)."""
        conn = self._connect()
        conn.execute(
            """INSERT INTO snapshots (key, payload) VALUES (?, ?)
               ON CONFLICT(key) DO UPDATE SET
                   payload = excluded.payload,
                   created_at = datetime('now')""",
            (key, json.dumps(payload, ensure_ascii=False)),
        )
        conn.commit()
        conn.close()

    def load_snapshot(self, key: str) -> dict | None:
        """Return the latest snapshot payload for *key*, or None."""
        conn = self._connect()
        row = conn.execute(
            "SELECT payload FROM snapshots WHERE key = ?", (key,)
        ).fetchone()
        conn.close()
        if row is None:
            return None
        try:
            return json.loads(row["payload"])
        except Exception:
            return None

    # ── LLM Calls & Cost tracking ───────────────────────────────────────────

    def log_llm_call(
        self,
        plugin_name: str,
        model: str,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        cost: float = 0.0,
        cache_hit: int = 0,
    ) -> int:
        """Record an LLM API call and return its id."""
        conn = self._connect()
        cur = conn.execute(
            """INSERT INTO llm_calls (plugin_name, model, prompt_tokens,
                                      completion_tokens, cost, cache_hit)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (plugin_name, model, prompt_tokens, completion_tokens, cost,
             cache_hit),
        )
        conn.commit()
        pk = cur.lastrowid
        conn.close()
        return pk

    def get_monthly_cost(self) -> float:
        """Return total LLM cost for the current month."""
        conn = self._connect()
        row = conn.execute(
            "SELECT COALESCE(SUM(cost), 0) AS total FROM llm_calls "
            "WHERE created_at >= datetime('now', 'start of month')"
        ).fetchone()
        conn.close()
        return float(row["total"])

    def get_total_llm_calls(self) -> int:
        """Return the total number of recorded LLM calls."""
        conn = self._connect()
        row = conn.execute("SELECT COUNT(*) AS cnt FROM llm_calls").fetchone()
        conn.close()
        return int(row["cnt"])
