"""Tests for Skill ABC, Result model, and discover_plugins()."""

from pathlib import Path
import pytest

from family_os.kernel.skill import Result, Skill


# ── Result model tests ────────────────────────────────────────────────────────

class TestResultModel:
    """Result is a Pydantic BaseModel with status, data, suggestions, todos, error."""

    def test_default_values(self):
        """Default Result has status='ok', empty data/suggestions/todos, error=None."""
        r = Result()
        assert r.status == "ok"
        assert r.data == {}
        assert r.suggestions == []
        assert r.todos == []
        assert r.error is None

    def test_with_status_and_data(self):
        """Result can carry a non-default status and arbitrary data."""
        r = Result(status="error", data={"reason": "timeout"})
        assert r.status == "error"
        assert r.data == {"reason": "timeout"}

    def test_with_suggestions(self):
        """Result can hold a list of suggestion dicts."""
        r = Result(
            suggestions=[
                {"title": "Check logs", "priority": "high", "detail": "Review /var/log"},
                {"title": "Restart service", "priority": "medium", "detail": "systemctl restart"},
            ]
        )
        assert len(r.suggestions) == 2
        assert r.suggestions[0]["title"] == "Check logs"
        assert r.suggestions[0]["priority"] == "high"

    def test_with_todos(self):
        """Result can hold a list of todo dicts."""
        r = Result(
            todos=[
                {"action": "Buy groceries", "deadline": "2026-08-01", "category": "shopping"},
            ]
        )
        assert len(r.todos) == 1
        assert r.todos[0]["action"] == "Buy groceries"

    def test_with_error(self):
        """Result can carry an error message."""
        r = Result(status="error", error="Something went wrong")
        assert r.error == "Something went wrong"


# ── Skill ABC tests ───────────────────────────────────────────────────────────

class TestSkillABC:
    """Skill is an abstract base class; subclasses must implement run and register_cli."""

    def test_cannot_instantiate_skill_directly(self):
        """Attempting to instantiate Skill directly should raise TypeError."""
        with pytest.raises(TypeError):
            Skill()  # type: ignore[abstract]

    def test_subclass_without_implementing_run_is_abstract(self):
        """A subclass that omits run() should still be abstract."""
        class IncompleteSkill(Skill):
            def register_cli(self, app):
                pass

        with pytest.raises(TypeError):
            IncompleteSkill()  # type: ignore[abstract]

    def test_subclass_without_implementing_register_cli_is_abstract(self):
        """A subclass that omits register_cli() should still be abstract."""
        class IncompleteSkill(Skill):
            def run(self, ctx):
                from family_os.kernel.skill import Result
                return Result()

        with pytest.raises(TypeError):
            IncompleteSkill()  # type: ignore[abstract]

    def test_concrete_subclass_can_be_instantiated(self):
        """A subclass implementing both abstract methods can be instantiated."""
        class ConcreteSkill(Skill):
            name = "test_skill"

            def run(self, ctx):
                return Result()

            def register_cli(self, app):
                pass

        skill = ConcreteSkill()
        assert skill.name == "test_skill"
        assert not hasattr(skill, "schedule") or skill.schedule is None
        assert skill.cost_tier == "free"
        assert isinstance(skill.run(None), Result)

    def test_class_attributes_are_inherited(self):
        """cost_tier default value should be available; schedule no longer a class attr."""
        assert not hasattr(Skill, "schedule") or Skill.schedule is None
        assert Skill.cost_tier == "free"


# ── discover_plugins tests ────────────────────────────────────────────────────

class TestDiscoverPlugins:
    """Plugin discovery scans directories for plugin.py with Skill subclasses."""

    def test_discovers_skill_subclass(self, tmp_path):
        """A temp dir with a valid plugin.py should yield the Skill subclass."""
        plugin_dir = tmp_path / "plugins"
        plugin_dir.mkdir()
        skill_dir = plugin_dir / "my_skill"
        skill_dir.mkdir()
        (skill_dir / "plugin.py").write_text("""
from family_os.kernel.skill import Skill, Result

class MyCoolSkill(Skill):
    name = "my_cool_skill"

    def run(self, ctx):
        return Result()

    def register_cli(self, app):
        pass
""")
        from family_os.kernel.discovery import discover_plugins
        plugins = discover_plugins(str(plugin_dir))
        assert len(plugins) == 1
        assert plugins[0].__name__ == "MyCoolSkill"
        # The class attribute should be accessible
        assert plugins[0].name == "my_cool_skill"

    def test_empty_dir_returns_empty_list(self, tmp_path):
        """A directory with no subdirectories should return an empty list."""
        d = tmp_path / "empty_plugins"
        d.mkdir()
        from family_os.kernel.discovery import discover_plugins
        assert discover_plugins(str(d)) == []

    def test_non_existent_dir_returns_empty_list(self, tmp_path):
        """A directory that does not exist should return an empty list."""
        from family_os.kernel.discovery import discover_plugins
        assert discover_plugins(str(tmp_path / "does_not_exist")) == []

    def test_folder_without_plugin_file_is_skipped(self, tmp_path):
        """A subdirectory without plugin.py should be skipped gracefully."""
        plugin_dir = tmp_path / "plugins"
        plugin_dir.mkdir()
        empty_skill_dir = plugin_dir / "no_plugin_here"
        empty_skill_dir.mkdir()
        # Add an __init__.py to ensure it's still skipped
        (empty_skill_dir / "__init__.py").write_text("")
        from family_os.kernel.discovery import discover_plugins
        assert discover_plugins(str(plugin_dir)) == []

    def test_underscore_prefix_folder_is_skipped(self, tmp_path):
        """Folders starting with _ should be skipped."""
        plugin_dir = tmp_path / "plugins"
        plugin_dir.mkdir()
        skipped_dir = plugin_dir / "_private"
        skipped_dir.mkdir()
        (skipped_dir / "plugin.py").write_text("""
from family_os.kernel.skill import Skill, Result

class HiddenSkill(Skill):
    name = "hidden"

    def run(self, ctx):
        return Result()

    def register_cli(self, app):
        pass
""")
        from family_os.kernel.discovery import discover_plugins
        assert discover_plugins(str(plugin_dir)) == []

    def test_dot_prefix_folder_is_skipped(self, tmp_path):
        """Folders starting with . should be skipped."""
        plugin_dir = tmp_path / "plugins"
        plugin_dir.mkdir()
        skipped_dir = plugin_dir / ".hidden"
        skipped_dir.mkdir()
        (skipped_dir / "plugin.py").write_text("""
from family_os.kernel.skill import Skill, Result

class HiddenDotSkill(Skill):
    name = "hidden_dot"

    def run(self, ctx):
        return Result()

    def register_cli(self, app):
        pass
""")
        from family_os.kernel.discovery import discover_plugins
        assert discover_plugins(str(plugin_dir)) == []

    def test_faulty_plugin_does_not_break_discovery(self, tmp_path):
        """A plugin that raises on import should be skipped; others still found."""
        plugin_dir = tmp_path / "plugins"
        plugin_dir.mkdir()

        # Faulty plugin
        bad_dir = plugin_dir / "bad_plugin"
        bad_dir.mkdir()
        (bad_dir / "plugin.py").write_text("""
raise ImportError("This plugin is broken on purpose")
""")

        # Good plugin
        good_dir = plugin_dir / "good_plugin"
        good_dir.mkdir()
        (good_dir / "plugin.py").write_text("""
from family_os.kernel.skill import Skill, Result

class GoodSkill(Skill):
    name = "good_skill"

    def run(self, ctx):
        return Result()

    def register_cli(self, app):
        pass
""")
        from family_os.kernel.discovery import discover_plugins
        plugins = discover_plugins(str(plugin_dir))
        assert len(plugins) == 1
        assert plugins[0].__name__ == "GoodSkill"
