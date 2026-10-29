"""Tests for kernel store service — SQLite storage layer (Store)."""

import sqlite3

import pytest

from family_os.kernel.store import Store


# ── Schema tests ──────────────────────────────────────────────────────────────

class TestStoreSchema:
    """Verify that initialize() creates the correct schema."""

    def test_creates_db_file(self, test_db_path):
        """The database file should exist after initialize()."""
        store = Store(test_db_path)
        store.initialize()
        assert test_db_path.exists(), "DB file was not created"

    def test_creates_all_tables(self, test_db_path):
        """All 8 tables should exist after initialize()."""
        store = Store(test_db_path)
        store.initialize()
        tables = store._list_tables()
        expected = {"entities", "facts", "events", "decisions",
                    "documents", "llm_calls", "llm_cache", "snapshots"}
        assert set(tables) == expected, f"Missing tables: {expected - set(tables)}"

    def test_idempotent_initialize(self, test_db_path):
        """Calling initialize() multiple times should not raise."""
        store = Store(test_db_path)
        store.initialize()
        store.initialize()  # second call
        tables = store._list_tables()
        assert len(tables) == 8


# ── Entities CRUD ─────────────────────────────────────────────────────────────

class TestEntities:
    """add_entity / get_entities."""

    def test_add_and_get_all(self, test_db_path):
        store = Store(test_db_path)
        store.initialize()

        eid1 = store.add_entity("person", "Alice")
        eid2 = store.add_entity("person", "Bob")
        eid3 = store.add_entity("pet", "Fluffy")

        all_entities = store.get_entities()
        assert len(all_entities) == 3

    def test_get_entities_type_filter(self, test_db_path):
        store = Store(test_db_path)
        store.initialize()

        store.add_entity("person", "Alice")
        store.add_entity("person", "Bob")
        store.add_entity("pet", "Fluffy")

        persons = store.get_entities(entity_type="person")
        assert len(persons) == 2
        assert all(e["type"] == "person" for e in persons)

    def test_get_entities_empty_type(self, test_db_path):
        store = Store(test_db_path)
        store.initialize()

        result = store.get_entities(entity_type="nonexistent")
        assert result == []

    def test_entity_extra_default(self, test_db_path):
        store = Store(test_db_path)
        store.initialize()

        eid = store.add_entity("person", "Charlie")
        entities = store.get_entities(entity_type="person")
        matching = [e for e in entities if e["id"] == eid]
        assert len(matching) == 1
        assert matching[0]["name"] == "Charlie"


# ── Facts CRUD + dedup ────────────────────────────────────────────────────────

class TestFacts:
    """add_fact / get_fact / query_facts with dedup and historical snapshots."""

    def test_add_and_query_by_type(self, test_db_path):
        store = Store(test_db_path)
        store.initialize()

        fid1 = store.add_fact("preference", "color", "blue", member="Alice")
        fid2 = store.add_fact("preference", "food", "pizza", member="Bob")

        fav_color = store.query_facts(type="preference", key="color")
        assert len(fav_color) == 1
        assert fav_color[0]["value"] == "blue"

    def test_dedup_same_type_key_value(self, test_db_path):
        """Same type+key+value should return the existing fact id (update)."""
        store = Store(test_db_path)
        store.initialize()

        fid1 = store.add_fact("preference", "color", "blue", member="Alice")
        fid2 = store.add_fact("preference", "color", "blue", member="Bob")

        assert fid1 == fid2, "Dedup failed: same fact should return same id"

    def test_different_value_differs(self, test_db_path):
        """Different value should create a new fact."""
        store = Store(test_db_path)
        store.initialize()

        fid1 = store.add_fact("preference", "color", "blue")
        fid2 = store.add_fact("preference", "color", "red")

        assert fid1 != fid2, "Different values should produce different fact ids"

    def test_historical_snapshot(self, test_db_path):
        """Two facts with same type+key but different valid_from/valid_to."""
        store = Store(test_db_path)
        store.initialize()

        # Old value
        fid_old = store.add_fact("residence", "city", "Beijing",
                                 member="Alice", valid_from="2020-01-01",
                                 valid_to="2023-12-31")
        # New value
        fid_new = store.add_fact("residence", "city", "Shanghai",
                                 member="Alice", valid_from="2024-01-01")

        assert fid_old != fid_new

        facts = store.query_facts(type="residence", key="city")
        assert len(facts) == 2

    def test_query_facts_no_filters(self, test_db_path):
        """query_facts with no filters should return all facts."""
        store = Store(test_db_path)
        store.initialize()

        store.add_fact("a", "k1", "v1")
        store.add_fact("b", "k2", "v2")
        all_facts = store.query_facts()
        assert len(all_facts) == 2

    def test_get_fact_by_id(self, test_db_path):
        store = Store(test_db_path)
        store.initialize()

        fid = store.add_fact("test", "key", "value")
        fact = store.get_fact(fid)
        assert fact is not None
        assert fact["key"] == "key"
        assert fact["value"] == "value"

    def test_get_fact_not_found(self, test_db_path):
        store = Store(test_db_path)
        store.initialize()

        fact = store.get_fact(999)
        assert fact is None


# ── Events ────────────────────────────────────────────────────────────────────

class TestEvents:
    """add_event / get_pending_events."""

    def test_add_and_get_pending(self, test_db_path):
        store = Store(test_db_path)
        store.initialize()

        eid = store.add_entity("person", "Alice")
        evt_id = store.add_event(eid, "reminder", "Buy milk",
                                 scheduled_at="2025-06-01 10:00")
        assert evt_id is not None

        pending = store.get_pending_events()
        assert len(pending) == 1
        assert pending[0]["description"] == "Buy milk"

    def test_get_pending_excludes_completed(self, test_db_path):
        store = Store(test_db_path)
        store.initialize()

        eid = store.add_entity("person", "Alice")
        store.add_event(eid, "reminder", "Pending task",
                        scheduled_at="2025-06-01 10:00")
        # Direct SQL to test filtering — event with status=completed
        conn = store._connect()
        conn.execute(
            "INSERT INTO events (entity_id, event_type, description, scheduled_at, status) "
            "VALUES (?, ?, ?, ?, 'completed')",
            (eid, "reminder", "Done task", "2025-06-01 10:00"),
        )
        conn.commit()
        conn.close()

        pending = store.get_pending_events()
        assert len(pending) == 1
        assert pending[0]["description"] == "Pending task"


# ── Decisions pipeline ────────────────────────────────────────────────────────

class TestDecisions:
    """add_decision / get_pending_decisions / update_decision_status."""

    def test_add_and_get_pending(self, test_db_path):
        store = Store(test_db_path)
        store.initialize()

        eid = store.add_entity("person", "Alice")
        dec_id = store.add_decision(
            eid, "Buy house", "Good investment",
            prediction="Prices will rise",
        )
        pending = store.get_pending_decisions()
        assert len(pending) == 1
        assert pending[0]["title"] == "Buy house"

    def test_update_decision_status(self, test_db_path):
        store = Store(test_db_path)
        store.initialize()

        eid = store.add_entity("person", "Alice")
        dec_id = store.add_decision(
            eid, "Buy car", "Need transport",
        )
        store.update_decision_status(dec_id, "approved", approved_by="Bob")
        pending = store.get_pending_decisions()
        assert len(pending) == 0

        # Also verify the decision now has approved_by set
        # Re-fetch via raw query
        conn = store._connect()
        row = conn.execute("SELECT * FROM decisions WHERE id = ?",
                           (dec_id,)).fetchone()
        conn.close()
        assert row["status"] == "approved"
        assert row["approved_by"] == "Bob"

    def test_update_decision_status_rejected(self, test_db_path):
        store = Store(test_db_path)
        store.initialize()

        eid = store.add_entity("person", "Alice")
        dec_id = store.add_decision(eid, "Invest", "Risky")
        store.update_decision_status(dec_id, "rejected", rejected_reason="Too risky")

        conn = store._connect()
        row = conn.execute("SELECT * FROM decisions WHERE id = ?",
                           (dec_id,)).fetchone()
        conn.close()
        assert row["status"] == "rejected"
        assert row["rejected_reason"] == "Too risky"


# ── Documents ─────────────────────────────────────────────────────────────────

class TestDocuments:
    """add_document."""

    def test_add_document(self, test_db_path):
        store = Store(test_db_path)
        store.initialize()

        doc_id = store.add_document("/docs/note.txt", "note",
                                    tags=["personal", "todo"])
        assert doc_id is not None

        conn = store._connect()
        row = conn.execute("SELECT * FROM documents WHERE id = ?",
                           (doc_id,)).fetchone()
        conn.close()
        assert row["path"] == "/docs/note.txt"
        assert row["doc_type"] == "note"

    def test_document_unique_path(self, test_db_path):
        store = Store(test_db_path)
        store.initialize()

        store.add_document("/docs/unique.txt", "note")
        with pytest.raises(sqlite3.IntegrityError):
            store.add_document("/docs/unique.txt", "note")


# ── LLM Calls + Cost ──────────────────────────────────────────────────────────

class TestLLMCalls:
    """log_llm_call / get_monthly_cost / get_total_llm_calls."""

    def test_log_and_count(self, test_db_path):
        store = Store(test_db_path)
        store.initialize()

        cid1 = store.log_llm_call("test_plugin", "gpt-4o",
                                  prompt_tokens=100, completion_tokens=50,
                                  cost=0.003)
        cid2 = store.log_llm_call("test_plugin", "gpt-4o",
                                  prompt_tokens=200, completion_tokens=100,
                                  cost=0.006)

        total = store.get_total_llm_calls()
        assert total == 2

    def test_monthly_cost(self, test_db_path):
        store = Store(test_db_path)
        store.initialize()

        store.log_llm_call("plugin_a", "gpt-4o",
                           prompt_tokens=100, completion_tokens=50,
                           cost=1.0)
        store.log_llm_call("plugin_b", "gpt-4o-mini",
                           prompt_tokens=50, completion_tokens=25,
                           cost=0.5)

        cost = store.get_monthly_cost()
        assert cost == 1.5

    def test_monthly_cost_no_calls(self, test_db_path):
        store = Store(test_db_path)
        store.initialize()
        cost = store.get_monthly_cost()
        assert cost == 0.0

    def test_log_llm_call_with_cache_hit(self, test_db_path):
        store = Store(test_db_path)
        store.initialize()

        cid = store.log_llm_call("plugin", "gpt-4o",
                                 prompt_tokens=100, completion_tokens=50,
                                 cost=0.003, cache_hit=1)
        conn = store._connect()
        row = conn.execute("SELECT * FROM llm_calls WHERE id = ?",
                           (cid,)).fetchone()
        conn.close()
        assert row["cache_hit"] == 1


class TestSnapshots:
    """Generic latest-state snapshot table tests."""

    def test_save_and_load(self, test_db_path):
        from family_os.kernel.store import Store
        store = Store(test_db_path)
        store.initialize()
        store.save_snapshot("market_pulse", {"T": 64.9, "zone": "极热"})
        snap = store.load_snapshot("market_pulse")
        assert snap == {"T": 64.9, "zone": "极热"}

    def test_upsert_overwrites_by_key(self, test_db_path):
        from family_os.kernel.store import Store
        store = Store(test_db_path)
        store.initialize()
        store.save_snapshot("k", {"v": 1})
        store.save_snapshot("k", {"v": 2})
        assert store.load_snapshot("k") == {"v": 2}

    def test_load_missing_key_returns_none(self, test_db_path):
        from family_os.kernel.store import Store
        store = Store(test_db_path)
        store.initialize()
        assert store.load_snapshot("nope") is None

    def test_load_corrupt_payload_returns_none(self, test_db_path):
        from family_os.kernel.store import Store
        store = Store(test_db_path)
        store.initialize()
        conn = store._connect()
        conn.execute("INSERT INTO snapshots (key, payload) VALUES ('bad', 'not-json{{{')")
        conn.commit()
        conn.close()
        assert store.load_snapshot("bad") is None
