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


# ── Tests: 作业级硬截止 ───────────────────────────────────────────────────────

class HangingSkill(Skill):
    """一个永不返回的插件——模拟网络调用死等（2026-11-04/05 两次真实事故）。"""

    name: str = "hanging_skill"
    cost_tier: str = "free"

    def run(self, ctx: Context, stage: str | None = None) -> Result:  # noqa: ARG002
        import time
        time.sleep(30)
        return Result(status="ok")

    def register_cli(self, app) -> None:
        pass


class TestJobHardTimeout:
    """卡死的插件不能把 daemon 线程占死，当天消息必须照发（哪怕是 ❌ 超时）。"""

    @pytest.fixture
    def notifier_config(self, mocker):
        cfg = mocker.MagicMock()
        cfg.scheduler.timezone = "UTC"
        cfg.notifier.channels = []
        return cfg

    def test_hung_plugin_is_abandoned_and_error_notified(
            self, notifier_config, mock_ctx, mocker, monkeypatch):
        from family_os.kernel import scheduler as sched_mod

        monkeypatch.setattr(sched_mod, "_JOB_HARD_TIMEOUT_S", 0.5)
        mock_notifier = mocker.patch(
            "family_os.kernel.scheduler.Notifier", autospec=True)
        mock_ctx.config = notifier_config
        scheduler = Scheduler(config=notifier_config, plugins=[HangingSkill])

        import time
        t0 = time.monotonic()
        scheduler._run_and_notify("hanging_skill", mock_ctx)
        elapsed = time.monotonic() - t0

        assert elapsed < 3, f"硬截止没有生效（耗时 {elapsed:.1f}s）"
        instance = mock_notifier.return_value
        instance.send.assert_called_once()
        result_arg = instance.send.call_args[0][0]
        assert result_arg.status == "error"
        assert "秒未完成" in (result_arg.error or "")


# ── Tests: 同一插件两个触发时间（2026-11-06「半夜抓、早上发」）────────────────

_STAGE_SEEN: list = []


class StageAwareSkill(Skill):
    """记录 run() 收到的 stage，验证 fetch_schedule 会以 stage="fetch" 触发。"""

    name: str = "stage_skill"
    cost_tier: str = "free"

    def run(self, ctx: Context, stage: str | None = None) -> Result:
        _STAGE_SEEN.append(stage)
        return Result(status="ok")

    def register_cli(self, app) -> None:
        pass


class TestFetchSchedule:
    """market_pulse 把「抓取」和「发布」拆成两个 cron，调度器要能注册两个作业。"""

    def _config(self, mocker, fetch_schedule="0 0-6 * * *"):
        cfg = mocker.MagicMock()
        cfg.scheduler.timezone = "UTC"
        cfg.plugin_instances = {
            "stage_skill": PluginInstanceConfig(
                enabled=True, schedule="30 8 * * 0-4",
                fetch_schedule=fetch_schedule),
        }
        return cfg

    def test_registers_extra_job(self, mocker, mock_ctx):
        scheduler = Scheduler(config=self._config(mocker), plugins=[StageAwareSkill])
        scheduler.start_daemon(mock_ctx)
        try:
            ids = {j.id for j in scheduler._bg_scheduler.get_jobs()}
        finally:
            scheduler._bg_scheduler.shutdown(wait=False)
        assert ids == {"stage_skill", "stage_skill.fetch"}

    def test_no_extra_job_without_fetch_schedule(self, mocker, mock_ctx):
        scheduler = Scheduler(
            config=self._config(mocker, fetch_schedule=None),
            plugins=[StageAwareSkill])
        scheduler.start_daemon(mock_ctx)
        try:
            ids = {j.id for j in scheduler._bg_scheduler.get_jobs()}
        finally:
            scheduler._bg_scheduler.shutdown(wait=False)
        assert ids == {"stage_skill"}

    def test_run_plugin_passes_stage(self, mocker, mock_ctx):
        _STAGE_SEEN.clear()
        scheduler = Scheduler(config=self._config(mocker), plugins=[StageAwareSkill])
        scheduler.run_plugin("stage_skill", mock_ctx, stage="fetch")
        scheduler.run_plugin("stage_skill", mock_ctx)
        assert _STAGE_SEEN == ["fetch", None], (
            "普通插件必须仍以 run(ctx) 调用，只有配了 fetch_schedule 才传 stage")


def test_fetch_stage_hard_timeout_stays_silent(mock_ctx, mocker, monkeypatch):
    """夜间抓取超时也不能推群——那一刻家里人在睡觉，早上发布时再披露。"""
    from family_os.kernel import scheduler as sched_mod

    cfg = mocker.MagicMock()
    cfg.scheduler.timezone = "UTC"
    cfg.notifier.channels = []
    monkeypatch.setattr(sched_mod, "_JOB_HARD_TIMEOUT_S", 0.5)
    mock_notifier = mocker.patch("family_os.kernel.scheduler.Notifier", autospec=True)
    mock_ctx.config = cfg
    scheduler = Scheduler(config=cfg, plugins=[HangingSkill])

    scheduler._run_and_notify("hanging_skill", mock_ctx, stage="fetch")

    result_arg = mock_notifier.return_value.send.call_args[0][0]
    assert result_arg.status == "skipped"
    assert result_arg.data["mode"] == "fetch_hard_timeout"


class TestPerPluginHardTimeout:
    """2026-11-07 事故：900s 全局硬截止误伤每周六的战略雷达。

    11-05 为防 market_pulse 抓取卡死加了 900s 作业级硬截止，但 strategy_radar
    是 4 赛道 × 4 stage 的批量 LLM 任务，实测 30~90 分钟。第一个周六（11-07）
    09:15 就被判超时发了「❌ 任务超时」，09:31 生成好的正常周报被整个丢弃。
    长跑插件必须能单独放宽，其余插件仍保持 900s 防卡死。
    """

    def test_default_timeout_when_not_configured(self, mock_config, mixed_plugins):
        from family_os.kernel import scheduler as sched_mod

        scheduler = Scheduler(config=mock_config, plugins=mixed_plugins)
        assert scheduler._job_timeout_s("scheduled_skill") == sched_mod._JOB_HARD_TIMEOUT_S
        assert scheduler._job_timeout_s("unknown_plugin") == sched_mod._JOB_HARD_TIMEOUT_S

    def test_custom_timeout_takes_precedence(self, mock_config, mixed_plugins):
        mock_config.plugin_instances["scheduled_skill"] = PluginInstanceConfig(
            enabled=True, schedule="0 9 * * 5", hard_timeout_s=7200)
        scheduler = Scheduler(config=mock_config, plugins=mixed_plugins)
        assert scheduler._job_timeout_s("scheduled_skill") == 7200.0
        assert scheduler._job_timeout_s("manual_skill") == 900.0

    def test_bad_values_fall_back_to_default(self, mock_config, mixed_plugins):
        from family_os.kernel import scheduler as sched_mod

        mock_config.plugin_instances["scheduled_skill"] = PluginInstanceConfig(
            enabled=True, hard_timeout_s=0)
        scheduler = Scheduler(config=mock_config, plugins=mixed_plugins)
        assert scheduler._job_timeout_s("scheduled_skill") == sched_mod._JOB_HARD_TIMEOUT_S
        # MagicMock 配置（其它测试常用）不能让比较运算抛 TypeError
        mock_config.plugin_instances = None
        assert scheduler._job_timeout_s("scheduled_skill") == sched_mod._JOB_HARD_TIMEOUT_S

    def test_run_and_notify_passes_custom_timeout(self, mock_ctx, mocker, monkeypatch):
        """真正 submit 时用的必须是插件自己的截止，不是全局常量。"""
        import concurrent.futures as cf

        seen = {}

        class FakeFuture:
            def result(self, timeout=None):
                seen["timeout"] = timeout
                return Result(status="ok", data={"_messages": {}})

        class FakeExecutor:
            def __init__(self, *a, **kw):
                pass

            def submit(self, fn, *a, **kw):
                return FakeFuture()

            def shutdown(self, wait=True):
                pass

        monkeypatch.setattr(cf, "ThreadPoolExecutor", FakeExecutor)
        mocker.patch("family_os.kernel.scheduler.Notifier", autospec=True)
        cfg = mocker.MagicMock()
        cfg.scheduler.timezone = "UTC"
        cfg.plugin_instances = {
            "scheduled_skill": PluginInstanceConfig(
                enabled=True, schedule="0 9 * * 5", hard_timeout_s=7200),
        }
        mock_ctx.config = cfg
        Scheduler(config=cfg, plugins=[ScheduledSkill])._run_and_notify(
            "scheduled_skill", mock_ctx)
        assert seen["timeout"] == 7200.0

