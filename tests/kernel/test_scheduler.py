"""Tests for the Scheduler kernel service."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from family_os.kernel.scheduler import Scheduler
from family_os.kernel.skill import Result, Skill
from family_os.kernel.config import PluginInstanceConfig

if TYPE_CHECKING:
    from family_os.kernel.config import Config
    from family_os.kernel.context import Context


# ── Inline test Skill subclasses ──────────────────────────────────────────────

class ScheduledSkill(Skill):
    """A plugin with a cron schedule configured via plugin_instances."""
    name: str = "scheduled_skill"
    cost_tier: str = "free"

    def run(self, ctx: Context) -> Result:
        return Result(status="ok", data={"message": "scheduled run"})

    def register_cli(self, app) -> None:
        pass


class ManualSkill(Skill):
    """A manual-only plugin (no configured schedule)."""
    name: str = "manual_skill"
    cost_tier: str = "on_demand_llm"

    def run(self, ctx: Context) -> Result:
        return Result(status="ok", data={"message": "manual run"})

    def register_cli(self, app) -> None:
        pass


class FailingSkill(Skill):
    """A plugin whose run() raises an exception."""
    name: str = "failing_skill"
    cost_tier: str = "free"

    def run(self, ctx: Context) -> Result:
        raise RuntimeError("something went wrong")

    def register_cli(self, app) -> None:
        pass


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def mock_config(mocker):
    """Return a Config mock with a scheduler.timezone and plugin_instances."""
    cfg = mocker.MagicMock()
    cfg.scheduler.timezone = "UTC"
    cfg.plugin_instances = {
        "scheduled_skill": PluginInstanceConfig(
            enabled=True, schedule="0 8 * * *"
        ),
        "manual_skill": PluginInstanceConfig(enabled=True, schedule=None),
        "failing_skill": PluginInstanceConfig(
            enabled=True, schedule="30 6 * * *"
        ),
    }
    return cfg


@pytest.fixture
def mixed_plugins():
    """Return a list containing a scheduled and a manual plugin class."""
    return [ScheduledSkill, ManualSkill]


@pytest.fixture
def mock_ctx(mocker):
    """Return a minimal Context mock."""
    return mocker.MagicMock()


# ── Tests: list_jobs ─────────────────────────────────────────────────────────

class TestListJobs:
    """list_jobs() must only return plugins with a non-None schedule."""

    def test_includes_scheduled_plugins(self, mock_config, mixed_plugins):
        scheduler = Scheduler(config=mock_config, plugins=mixed_plugins)
        jobs = scheduler.list_jobs()
        names = [j["plugin"] for j in jobs]
        assert "scheduled_skill" in names

    def test_excludes_manual_plugins(self, mock_config, mixed_plugins):
        scheduler = Scheduler(config=mock_config, plugins=mixed_plugins)
        jobs = scheduler.list_jobs()
        names = [j["plugin"] for j in jobs]
        assert "manual_skill" not in names

    def test_returns_expected_fields(self, mock_config, mixed_plugins):
        scheduler = Scheduler(config=mock_config, plugins=mixed_plugins)
        jobs = scheduler.list_jobs()
        for job in jobs:
            assert "plugin" in job
            assert "schedule" in job
            assert "cost_tier" in job

    def test_sorted_by_name(self, mock_config):
        class ASkill(Skill):
            name = "a_skill"
            cost_tier = "free"
            def run(self, ctx): return Result()
            def register_cli(self, app): pass

        class ZSkill(Skill):
            name = "z_skill"
            cost_tier = "low_freq_llm"
            def run(self, ctx): return Result()
            def register_cli(self, app): pass

        mock_config.plugin_instances = {
            "a_skill": PluginInstanceConfig(enabled=True, schedule="0 8 * * *"),
            "z_skill": PluginInstanceConfig(enabled=True, schedule="0 9 * * *"),
        }
        scheduler = Scheduler(config=mock_config, plugins=[ZSkill, ASkill])
        jobs = scheduler.list_jobs()
        names = [j["plugin"] for j in jobs]
        assert names == sorted(names)

    def test_empty_plugins_list(self, mock_config):
        scheduler = Scheduler(config=mock_config, plugins=[])
        assert scheduler.list_jobs() == []


# ── Tests: run_plugin ────────────────────────────────────────────────────────

class TestRunPlugin:
    """run_plugin() must execute a named plugin and return a Result."""

    def test_executes_and_returns_result(self, mock_config, mixed_plugins, mock_ctx):
        scheduler = Scheduler(config=mock_config, plugins=mixed_plugins)
        result = scheduler.run_plugin("scheduled_skill", mock_ctx)
        assert isinstance(result, Result)
        assert result.status == "ok"

    def test_raises_value_error_for_unknown(self, mock_config, mixed_plugins, mock_ctx):
        scheduler = Scheduler(config=mock_config, plugins=mixed_plugins)
        with pytest.raises(ValueError, match="unknown"):
            scheduler.run_plugin("nonexistent", mock_ctx)

    def test_returns_error_result_on_failure(self, mock_config, mock_ctx):
        scheduler = Scheduler(config=mock_config, plugins=[FailingSkill])
        result = scheduler.run_plugin("failing_skill", mock_ctx)
        assert isinstance(result, Result)
        assert result.status == "error"
        assert "something went wrong" in (result.error or "")


# ── Tests: start_daemon ──────────────────────────────────────────────────────

class TestStartDaemon:
    """start_daemon() must start the APScheduler BackgroundScheduler."""

    def test_starts_scheduler(self, mock_config, mixed_plugins, mock_ctx):
        scheduler = Scheduler(config=mock_config, plugins=mixed_plugins)
        scheduler.start_daemon(mock_ctx)
        assert scheduler._bg_scheduler.running is True
        scheduler._bg_scheduler.shutdown(wait=False)

    def test_start_daemon_with_empty_plugins(self, mock_config, mock_ctx):
        scheduler = Scheduler(config=mock_config, plugins=[])
        scheduler.start_daemon(mock_ctx)
        assert scheduler._bg_scheduler.running is True
        scheduler._bg_scheduler.shutdown(wait=False)


# ── Tests: _run_and_notify ─────────────────────────────────────────────────

class TestRunAndNotify:
    """_run_and_notify() must run the plugin and dispatch through Notifier."""

    @pytest.fixture
    def notifier_config(self, mocker):
        """Config mock with the notifier.channels attribute Notifier needs."""
        cfg = mocker.MagicMock()
        cfg.scheduler.timezone = "UTC"
        cfg.notifier.channels = []
        return cfg

    def test_calls_notifier_send_after_run(self, notifier_config, mock_ctx, mocker):
        """A successful plugin run must forward the Result to Notifier.send."""
        mock_notifier = mocker.patch(
            "family_os.kernel.scheduler.Notifier", autospec=True
        )
        mock_ctx.config = notifier_config
        scheduler = Scheduler(config=notifier_config, plugins=[ScheduledSkill])

        scheduler._run_and_notify("scheduled_skill", mock_ctx)

        # Notifier was instantiated with the context's config
        mock_notifier.assert_called_once_with(notifier_config)
        # send() was called
        instance = mock_notifier.return_value
        instance.send.assert_called_once()
        call_args = instance.send.call_args
        result_arg = call_args[0][0]
        plugin_arg = call_args[0][1]
        assert isinstance(result_arg, Result)
        assert result_arg.status == "ok"
        assert plugin_arg == "scheduled_skill"

    def test_notifies_even_on_plugin_error(self, notifier_config, mock_ctx, mocker):
        """When a plugin fails, the error Result must still be dispatched."""
        mock_notifier = mocker.patch(
            "family_os.kernel.scheduler.Notifier", autospec=True
        )
        mock_ctx.config = notifier_config
        scheduler = Scheduler(config=notifier_config, plugins=[FailingSkill])

        scheduler._run_and_notify("failing_skill", mock_ctx)

        instance = mock_notifier.return_value
        instance.send.assert_called_once()
        result_arg = instance.send.call_args[0][0]
        assert result_arg.status == "error"
        assert "something went wrong" in (result_arg.error or "")

    def test_notifier_exception_is_suppressed(self, notifier_config, mock_ctx, mocker):
        """If Notifier.send raises, the exception must not propagate."""
        mock_notifier = mocker.patch(
            "family_os.kernel.scheduler.Notifier", autospec=True
        )
        mock_notifier.return_value.send.side_effect = RuntimeError("channel down")
        mock_ctx.config = notifier_config
        scheduler = Scheduler(config=notifier_config, plugins=[ScheduledSkill])

        # Must not raise
        scheduler._run_and_notify("scheduled_skill", mock_ctx)

        mock_notifier.return_value.send.assert_called_once()
