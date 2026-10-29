"""Tests for digest aggregator — inbox file collection, merging, and cleanup."""

import datetime
from pathlib import Path

import pytest

from family_os.kernel.digest import DigestAggregator
from family_os.kernel.config import Config, AppConfig, LLMConfig, PluginConfig, \
    SchedulerConfig, NotifierConfig, DigestConfig, ApprovalConfig, Secrets


# ── Helpers ──────────────────────────────────────────────────────────────────────

def _make_config(data_dir: str | Path) -> Config:
    """Create a minimal Config instance pointing at a given data directory."""
    return Config(
        app=AppConfig(data_dir=str(data_dir)),
        llm=LLMConfig(),
        plugins=PluginConfig(),
        scheduler=SchedulerConfig(),
        notifier=NotifierConfig(),
        digest=DigestConfig(mode="daily"),
        approval=ApprovalConfig(),
        secrets=Secrets(),
    )


def _make_inbox_file(
    inbox_dir: Path,
    plugin: str,
    dt: datetime.datetime,
    content: str,
) -> Path:
    """Write a sample inbox file with the expected naming convention."""
    timestamp = dt.strftime("%Y%m%d_%H%M%S")
    filename = f"{timestamp}_{plugin}.md"
    path = inbox_dir / filename
    path.write_text(content, encoding="utf-8")
    return path


# ── Collect ──────────────────────────────────────────────────────────────────────

class TestCollect:
    """DigestAggregator.collect() — scanning inbox dir and filtering by time."""

    def test_collect_reads_inbox_files(self, temp_dir):
        """Collect returns parsed items for all .md files in inbox."""
        inbox_dir = temp_dir / "data" / "inbox"
        inbox_dir.mkdir(parents=True)

        now = datetime.datetime.now()
        _make_inbox_file(inbox_dir, "weather", now, "Sunny day")
        _make_inbox_file(inbox_dir, "calendar", now, "Meeting at 3pm")

        config = _make_config(temp_dir / "data")
        agg = DigestAggregator(config)
        items = agg.collect(since_hours=24)

        assert len(items) == 2
        plugins = {i["plugin_name"] for i in items}
        assert plugins == {"weather", "calendar"}

    def test_collect_returns_correct_structure(self, temp_dir):
        """Each item dict has expected keys."""
        inbox_dir = temp_dir / "data" / "inbox"
        inbox_dir.mkdir(parents=True)

        now = datetime.datetime.now()
        _make_inbox_file(inbox_dir, "test_plugin", now, "Hello world")

        config = _make_config(temp_dir / "data")
        agg = DigestAggregator(config)
        items = agg.collect(since_hours=24)

        assert len(items) == 1
        item = items[0]
        assert "plugin_name" in item
        assert "timestamp" in item
        assert "content" in item
        assert "filepath" in item
        assert item["plugin_name"] == "test_plugin"
        assert item["content"] == "Hello world"

    def test_collect_filters_by_since_hours(self, temp_dir):
        """Items older than since_hours are excluded."""
        inbox_dir = temp_dir / "data" / "inbox"
        inbox_dir.mkdir(parents=True)

        now = datetime.datetime.now()
        recent = now - datetime.timedelta(hours=1)
        old = now - datetime.timedelta(hours=48)

        _make_inbox_file(inbox_dir, "recent_plugin", recent, "Recent content")
        _make_inbox_file(inbox_dir, "old_plugin", old, "Old content")

        config = _make_config(temp_dir / "data")
        agg = DigestAggregator(config)
        items = agg.collect(since_hours=24)

        assert len(items) == 1
        assert items[0]["plugin_name"] == "recent_plugin"

    def test_collect_empty_inbox_returns_empty_list(self, temp_dir):
        """Empty inbox directory returns []."""
        inbox_dir = temp_dir / "data" / "inbox"
        inbox_dir.mkdir(parents=True)

        config = _make_config(temp_dir / "data")
        agg = DigestAggregator(config)
        items = agg.collect(since_hours=24)

        assert items == []

    def test_collect_missing_inbox_dir_returns_empty_list(self, temp_dir):
        """Non-existent inbox directory returns [] without crashing."""
        config = _make_config(temp_dir / "data")  # no inbox dir created
        agg = DigestAggregator(config)
        items = agg.collect(since_hours=24)

        assert items == []

    def test_collect_ignores_non_md_files(self, temp_dir):
        """Non-.md files in inbox are ignored."""
        inbox_dir = temp_dir / "data" / "inbox"
        inbox_dir.mkdir(parents=True)

        now = datetime.datetime.now()
        _make_inbox_file(inbox_dir, "weather", now, "Sunny")
        # A non-md file
        (inbox_dir / "README.txt").write_text("ignore me", encoding="utf-8")

        config = _make_config(temp_dir / "data")
        agg = DigestAggregator(config)
        items = agg.collect(since_hours=24)

        assert len(items) == 1

    def test_collect_invalid_filename_skipped(self, temp_dir):
        """Files that don't match the naming pattern are silently skipped."""
        inbox_dir = temp_dir / "data" / "inbox"
        inbox_dir.mkdir(parents=True)

        now = datetime.datetime.now()
        _make_inbox_file(inbox_dir, "good", now, "Valid content")
        (inbox_dir / "bad_filename.md").write_text("bad", encoding="utf-8")
        (inbox_dir / "no_underscore.md").write_text("also bad", encoding="utf-8")

        config = _make_config(temp_dir / "data")
        agg = DigestAggregator(config)
        items = agg.collect(since_hours=24)

        assert len(items) == 1
        assert items[0]["plugin_name"] == "good"


# ── Merge ────────────────────────────────────────────────────────────────────────

class TestMerge:
    """DigestAggregator.merge() — producing the Markdown digest."""

    def test_merge_empty_items(self, temp_dir):
        """merge with no items returns 'no output' message."""
        config = _make_config(temp_dir / "data")
        agg = DigestAggregator(config)
        result = agg.merge([])

        assert "本周期无新产出。" in result

    def test_merge_produces_structured_briefing(self, temp_dir):
        """merge produces a Markdown briefing with plugin sections."""
        items = [
            {
                "plugin_name": "weather",
                "timestamp": "2025-07-21 08:00",
                "content": "Today will be sunny.",
            },
            {
                "plugin_name": "calendar",
                "timestamp": "2025-07-21 09:00",
                "content": "Family dinner at 6pm.",
            },
        ]
        config = _make_config(temp_dir / "data")
        agg = DigestAggregator(config)
        result = agg.merge(items)

        # Title should mention daily
        assert "每日简报" in result
        # Should have both plugin names
        assert "weather" in result
        assert "calendar" in result
        # Should have the content
        assert "Today will be sunny." in result
        assert "Family dinner at 6pm." in result
        # Should have section markers
        assert "##" in result

    def test_merge_weekly_mode(self, temp_dir):
        """merge uses weekly title when config mode is weekly."""
        config = _make_config(temp_dir / "data")
        config._digest = DigestConfig(mode="weekly")
        agg = DigestAggregator(config)
        result = agg.merge([
            {"plugin_name": "test", "timestamp": "now", "content": "hi"},
        ])
        assert "每周简报" in result
        assert "每日简报" not in result

    def test_merge_high_priority_with_red_emoji(self, temp_dir):
        """Items containing 🔴 emoji are placed in high-priority section."""
        items = [
            {
                "plugin_name": "alert",
                "timestamp": "2025-07-21 08:00",
                "content": "🔴 Server down!",
            },
            {
                "plugin_name": "weather",
                "timestamp": "2025-07-21 09:00",
                "content": "Today will be sunny.",
            },
        ]
        config = _make_config(temp_dir / "data")
        agg = DigestAggregator(config)
        result = agg.merge(items)

        assert "🔴 高优先级" in result or "高优先级" in result
        assert "Server down!" in result
        assert "Today will be sunny." in result

    def test_merge_high_priority_with_urgent_keyword(self, temp_dir):
        """Items containing '紧急' keyword are placed in high-priority section."""
        items = [
            {
                "plugin_name": "alert",
                "timestamp": "2025-07-21 08:00",
                "content": "紧急：需要立即处理",
            },
            {
                "plugin_name": "weather",
                "timestamp": "2025-07-21 09:00",
                "content": "Today will be sunny.",
            },
        ]
        config = _make_config(temp_dir / "data")
        agg = DigestAggregator(config)
        result = agg.merge(items)

        assert "高优先级" in result
        assert "紧急：需要立即处理" in result
        assert "Today will be sunny." in result

    def test_merge_all_high_priority(self, temp_dir):
        """When all items are high priority, only the priority section is shown."""
        items = [
            {
                "plugin_name": "alert1",
                "timestamp": "2025-07-21 08:00",
                "content": "🔴 Critical issue A",
            },
            {
                "plugin_name": "alert2",
                "timestamp": "2025-07-21 09:00",
                "content": "🔴 Critical issue B",
            },
        ]
        config = _make_config(temp_dir / "data")
        agg = DigestAggregator(config)
        result = agg.merge(items)

        assert "高优先级" in result
        assert "一般信息" not in result


# ── Run (full cycle) ─────────────────────────────────────────────────────────────

class TestRun:
    """DigestAggregator.run() — full collect → merge → save → cleanup cycle."""

    def test_run_collects_merges_saves_and_cleans(self, temp_dir):
        """run() collects inbox files, saves digest to history, and deletes inbox files."""
        inbox_dir = temp_dir / "data" / "inbox"
        inbox_dir.mkdir(parents=True)

        now = datetime.datetime.now()
        _make_inbox_file(inbox_dir, "weather", now, "Sunny day")
        _make_inbox_file(inbox_dir, "calendar", now, "Meeting at 3pm")

        config = _make_config(temp_dir / "data")
        agg = DigestAggregator(config)
        result = agg.run()

        # Should return merged text
        assert isinstance(result, str)
        assert "Sunny day" in result
        assert "Meeting at 3pm" in result

        # Digest should be saved to history
        history_dir = temp_dir / "data" / "history" / "digests"
        assert history_dir.exists()
        saved_files = list(history_dir.glob("digest_*.md"))
        assert len(saved_files) == 1

        # Saved content should match returned text
        saved_content = saved_files[0].read_text(encoding="utf-8")
        assert saved_content == result

        # Inbox files should be deleted
        remaining = list(inbox_dir.glob("*.md"))
        assert len(remaining) == 0

    def test_run_empty_inbox(self, temp_dir):
        """run() with empty inbox returns 'no output' message and creates no history."""
        inbox_dir = temp_dir / "data" / "inbox"
        inbox_dir.mkdir(parents=True)

        config = _make_config(temp_dir / "data")
        agg = DigestAggregator(config)
        result = agg.run()

        assert "本周期无新产出。" in result

        history_dir = temp_dir / "data" / "history" / "digests"
        if history_dir.exists():
            saved_files = list(history_dir.glob("digest_*.md"))
            assert len(saved_files) == 0

    def test_run_with_missing_inbox(self, temp_dir):
        """run() with missing inbox dir gracefully returns 'no output'."""
        config = _make_config(temp_dir / "data")  # no inbox created
        agg = DigestAggregator(config)
        result = agg.run()

        assert "本周期无新产出。" in result

    def test_run_preserves_non_md_files_in_inbox(self, temp_dir):
        """run() only deletes .md inbox files, leaving other files intact."""
        inbox_dir = temp_dir / "data" / "inbox"
        inbox_dir.mkdir(parents=True)

        now = datetime.datetime.now()
        _make_inbox_file(inbox_dir, "weather", now, "Sunny")
        (inbox_dir / "notes.txt").write_text("keep me", encoding="utf-8")
        (inbox_dir / ".gitkeep").write_text("", encoding="utf-8")

        config = _make_config(temp_dir / "data")
        agg = DigestAggregator(config)
        agg.run()

        remaining = list(inbox_dir.iterdir())
        assert len(remaining) == 2  # txt + .gitkeep
        assert (inbox_dir / "notes.txt").exists()
        assert (inbox_dir / ".gitkeep").exists()
