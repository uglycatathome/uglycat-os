"""Tests for kernel ingestion layer — Normalizer and SourceRegistry."""

from pathlib import Path

import yaml
import pytest

from family_os.kernel.store import Store
from family_os.kernel.ingestion import Normalizer, SourceRegistry


# ── Normalizer ──────────────────────────────────────────────────────────────────


class TestNormalizer:
    """Normalizer — wraps store with tagging and dedup."""

    def test_ingest_fact_with_all_fields(self, test_db_path):
        """ingest_fact with all tags specified — verify sensitivity/source/format
        in stored fact."""
        store = Store(test_db_path)
        store.initialize()
        normalizer = Normalizer(store)

        raw = {
            "type": "preference",
            "key": "color",
            "value": "blue",
            "member": "Alice",
            "sensitivity": "public",
            "source": "conversation",
            "format": "structured",
        }
        fid = normalizer.ingest_fact(raw)
        fact = store.get_fact(fid)
        assert fact is not None
        assert fact["type"] == "preference"
        assert fact["key"] == "color"
        assert fact["value"] == "blue"
        assert fact["member"] == "Alice"
        assert fact["sensitivity"] == "public"
        assert fact["source"] == "conversation"
        assert fact["format"] == "structured"

    def test_ingest_fact_minimal_defaults(self, test_db_path):
        """ingest_fact with minimal raw — verify defaults applied."""
        store = Store(test_db_path)
        store.initialize()
        normalizer = Normalizer(store)

        raw = {"type": "preference", "key": "color", "value": "red"}
        fid = normalizer.ingest_fact(raw)
        fact = store.get_fact(fid)
        assert fact is not None
        assert fact["sensitivity"] == "private"
        assert fact["source"] == "manual_note"
        assert fact["format"] == "structured"
        assert fact["member"] == "全家"

    def test_dedup_same_fact_returns_same_id(self, test_db_path):
        """Same type+key+value returns same id."""
        store = Store(test_db_path)
        store.initialize()
        normalizer = Normalizer(store)

        raw = {"type": "preference", "key": "color", "value": "green"}
        fid1 = normalizer.ingest_fact(raw)
        fid2 = normalizer.ingest_fact(raw)
        assert fid1 == fid2

    def test_ingest_document(self, test_db_path):
        """ingest_document creates document record."""
        store = Store(test_db_path)
        store.initialize()
        normalizer = Normalizer(store)

        doc_id = normalizer.ingest_document(
            path="/docs/test.txt",
            doc_type="note",
            entity_id=None,
            tags=["test"],
        )
        assert doc_id is not None

        conn = store._connect()
        row = conn.execute(
            "SELECT * FROM documents WHERE id = ?", (doc_id,)
        ).fetchone()
        conn.close()
        assert row["path"] == "/docs/test.txt"
        assert row["doc_type"] == "note"

    def test_ingest_batch(self, test_db_path):
        """ingest_batch processes multiple facts."""
        store = Store(test_db_path)
        store.initialize()
        normalizer = Normalizer(store)

        facts = [
            {"type": "preference", "key": "color", "value": "blue"},
            {"type": "preference", "key": "food", "value": "pizza"},
            {"type": "preference", "key": "drink", "value": "water"},
        ]
        ids = normalizer.ingest_batch(facts)
        assert len(ids) == 3
        assert all(isinstance(fid, int) for fid in ids)
        assert len(set(ids)) == 3  # all distinct


# ── SourceRegistry ──────────────────────────────────────────────────────────────


class TestSourceRegistry:
    """SourceRegistry — loads sources.yaml, plugins subscribe by tag."""

    @pytest.fixture
    def sources_yaml(self, temp_dir):
        """Create a temp sources.yaml with sample data."""
        data = {
            "sources": [
                {"id": "wechat", "name": "WeChat", "tags": ["social", "message"]},
                {"id": "email", "name": "Email", "tags": ["communication", "work"]},
                {"id": "calendar", "name": "Calendar", "tags": ["schedule", "work"]},
                {"id": "weather", "name": "Weather API", "tags": ["utility"]},
            ]
        }
        path = temp_dir / "sources.yaml"
        with open(path, "w", encoding="utf-8") as f:
            yaml.dump(data, f, default_flow_style=False)
        return path

    def test_list_all(self, sources_yaml):
        """list_all returns correct count."""
        registry = SourceRegistry(sources_yaml)
        sources = registry.list_all()
        assert len(sources) == 4

    def test_get_sources_by_tags_single(self, sources_yaml):
        """get_sources_by_tags filters correctly for a single tag."""
        registry = SourceRegistry(sources_yaml)
        results = registry.get_sources_by_tags(["work"])
        assert len(results) == 2
        ids = {s["id"] for s in results}
        assert ids == {"email", "calendar"}

    def test_get_sources_by_tags_multiple(self, sources_yaml):
        """get_sources_by_tags with multiple tags returns sources matching any."""
        registry = SourceRegistry(sources_yaml)
        results = registry.get_sources_by_tags(["social", "utility"])
        assert len(results) == 2
        ids = {s["id"] for s in results}
        assert ids == {"wechat", "weather"}

    def test_get_sources_by_tags_no_match(self, sources_yaml):
        """get_sources_by_tags with no match returns empty list."""
        registry = SourceRegistry(sources_yaml)
        results = registry.get_sources_by_tags(["nonexistent"])
        assert results == []

    def test_get_source_by_id_found(self, sources_yaml):
        """get_source_by_id finds a source by id."""
        registry = SourceRegistry(sources_yaml)
        source = registry.get_source_by_id("wechat")
        assert source is not None
        assert source["id"] == "wechat"
        assert source["name"] == "WeChat"

    def test_get_source_by_id_not_found(self, sources_yaml):
        """get_source_by_id returns None for missing id."""
        registry = SourceRegistry(sources_yaml)
        source = registry.get_source_by_id("nonexistent")
        assert source is None

    def test_missing_file(self, temp_dir):
        """Missing file returns empty list and doesn't crash."""
        path = temp_dir / "nonexistent.yaml"
        registry = SourceRegistry(path)
        sources = registry.list_all()
        assert sources == []


class TestLocalOverride:
    """sources.local.yaml 覆写层（用户私加源，不被 git pull 覆盖）。"""

    def _write(self, tmp_path, body):
        (tmp_path / "sources.yaml").write_text(body, encoding="utf-8")

    def test_local_adds_and_overrides(self, tmp_path):
        from family_os.kernel.ingestion import SourceRegistry

        self._write(tmp_path, "sources:\n  - name: A\n    type: rss\n    url: http://a\n    enabled: true\n")
        (tmp_path / "sources.local.yaml").write_text(
            "sources:\n  - name: A\n    enabled: false\n  - name: B\n    type: rss\n    url: http://b\n", encoding="utf-8")
        reg = SourceRegistry(tmp_path / "sources.yaml")
        by_name = {s["name"]: s for s in reg.list_all()}
        assert by_name["A"]["enabled"] is False, "同名条目应被覆写"
        assert "B" in by_name, "新条目应追加"

    def test_no_local_file_keeps_defaults(self, tmp_path):
        from family_os.kernel.ingestion import SourceRegistry

        self._write(tmp_path, "sources:\n  - name: A\n    type: rss\n    url: http://a\n    enabled: true\n")
        reg = SourceRegistry(tmp_path / "sources.yaml")
        assert [s["name"] for s in reg.list_all()] == ["A"]

    def test_broken_local_is_ignored(self, tmp_path):
        from family_os.kernel.ingestion import SourceRegistry

        self._write(tmp_path, "sources:\n  - name: A\n    type: rss\n    url: http://a\n")
        (tmp_path / "sources.local.yaml").write_text("sources: [oops", encoding="utf-8")
        reg = SourceRegistry(tmp_path / "sources.yaml")
        assert len(reg.list_all()) == 1
