"""Scheduler kernel service.

Provides the ``Scheduler`` class that manages plugin lifecycle:
periodic execution via APScheduler (``start_daemon``) and on-demand
invocation via ``run_plugin``.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Type

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from family_os.kernel.skill import Result, Skill
from family_os.kernel.notifier import Notifier

if TYPE_CHECKING:
    from family_os.kernel.config import Config
    from family_os.kernel.context import Context

log = logging.getLogger(__name__)

# 单个作业的硬截止（秒）。正常一轮 market_pulse 约 270~420s；超时说明有调用在
# 无限等待（历史事故 5h20m / 45min），必须截断否则当天消息发不出去。
_JOB_HARD_TIMEOUT_S = 900.0

__all__ = ["Scheduler"]


class Scheduler:
    """Orchestrates plugin execution — both scheduled and manual.

    Parameters
    ----------
    config : Config
        Application configuration (uses ``scheduler.timezone``).
    plugins : list[Type[Skill]]
        List of discovered plugin classes.
    """

    def __init__(self, config: Config, plugins: list[Type[Skill]]) -> None:
        self._config = config
        # Build a name → class lookup map
        self._plugin_map: dict[str, Type[Skill]] = {
            plugin_cls.name: plugin_cls for plugin_cls in plugins
        }
        # Internal APScheduler instance; created by start_daemon()
        self._bg_scheduler: BackgroundScheduler | None = None

    # ── list_jobs ──────────────────────────────────────────────────────────

    def list_jobs(self) -> list[dict]:
        """Return metadata for all plugins that have a configured schedule.

        Schedules are read from ``Config.plugin_instances`` (replacing the
        old ``Skill.schedule`` class attribute) so non-developers can adjust
        them without touching plugin code.

        Returns
        -------
        list[dict]
            Each dict contains ``plugin`` (name), ``schedule`` (cron string
            from the instance config), and ``cost_tier``.  Sorted
            alphabetically by plugin name.
        """
        jobs = []
        for name, plugin_cls in self._plugin_map.items():
            instance_cfg = self._config.plugin_instances.get(name)
            schedule = instance_cfg.schedule if instance_cfg else None
            if schedule:
                jobs.append({
                    "plugin": name,
                    "schedule": schedule,
                    "cost_tier": plugin_cls.cost_tier,
                })
        jobs.sort(key=lambda j: j["plugin"])
        return jobs

    # ── run_plugin ─────────────────────────────────────────────────────────

    def run_plugin(self, plugin_name: str, ctx: Context) -> Result:
        """Look up a plugin by name, instantiate it, and run it.

        Parameters
        ----------
        plugin_name : str
            The ``name`` attribute of the desired plugin.
        ctx : Context
            Dependency-injection container.

        Returns
        -------
        Result
            The plugin's result on success, or a ``Result(status="error")``
            if the plugin raised an exception.

        Raises
        ------
        ValueError
            If *plugin_name* is not found in the plugin map.
        """
        plugin_cls = self._plugin_map.get(plugin_name)
        if plugin_cls is None:
            raise ValueError(f"unknown plugin: {plugin_name}")

        try:
            plugin = plugin_cls()
            return plugin.run(ctx)
        except Exception as exc:
            log.exception("Plugin %s failed", plugin_name)
            return Result(status="error", error=str(exc))

    # ── _run_and_notify (daemon only) ──────────────────────────────────────

    def _run_and_notify(self, plugin_name: str, ctx: Context) -> None:
        """Run a plugin and dispatch its result through the Notifier.

        This is the daemon-only path.  Manual CLI invocations go through
        ``run_plugin()`` directly so the caller controls output formatting.
        """
        from concurrent.futures import ThreadPoolExecutor
        from concurrent.futures import TimeoutError as FutTimeout
        from datetime import datetime

        t0 = datetime.now()
        print(f"[{t0.strftime('%H:%M:%S')}] {plugin_name} job started", flush=True)

        # 作业级硬截止：插件内部的网络调用即使全都没有超时（历史上两次真实事故：
        # 2026-11-04 core 层分页接口死等、2026-11-05 ref 层 ssl.read 死等），
        # 这里也必须把控制权拿回来——否则整个 daemon 线程被占死，当天一条消息都发不出去。
        # 线程无法强杀，超时后放弃等待并回报 error，让 Notifier 发出「❌ 任务超时」。
        ex = ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"job-{plugin_name}")
        try:
            result = ex.submit(self.run_plugin, plugin_name, ctx).result(
                timeout=_JOB_HARD_TIMEOUT_S)
        except FutTimeout:
            log.critical(
                "Plugin %s exceeded hard deadline %.0fs — abandoned (worker thread "
                "still stuck in background)", plugin_name, _JOB_HARD_TIMEOUT_S,
            )
            print(f"[{datetime.now().strftime('%H:%M:%S')}] {plugin_name} HARD TIMEOUT "
                  f"after {_JOB_HARD_TIMEOUT_S:.0f}s — 已放弃等待", flush=True)
            result = Result(
                status="error",
                error=(f"{plugin_name} 任务超过 {_JOB_HARD_TIMEOUT_S:.0f} 秒未完成，"
                       f"已放弃等待（当天数据可能不完整）"),
            )
        finally:
            ex.shutdown(wait=False)

        dt = (datetime.now() - t0).total_seconds()
        print(f"[{datetime.now().strftime('%H:%M:%S')}] {plugin_name} done "
              f"({dt:.0f}s, status={result.status})", flush=True)
        try:
            notifier = Notifier(ctx.config)
            notifier.send(result, plugin_name)
        except Exception:
            log.exception(
                "Notifier failed for plugin %s (result status=%s)",
                plugin_name,
                result.status,
            )

    # ── start_daemon ───────────────────────────────────────────────────────

    def start_daemon(self, ctx: Context) -> None:
        """Start the APScheduler background scheduler.

        For every plugin instance that is enabled in config and has a
        schedule, a cron job is registered.  The scheduler is started once
        all jobs are added.

        Parameters
        ----------
        ctx : Context
            Dependency-injection container forwarded to each plugin run.
        """
        self._bg_scheduler = BackgroundScheduler(
            timezone=self._config.scheduler.timezone,
        )

        for name, plugin_cls in self._plugin_map.items():
            instance_cfg = self._config.plugin_instances.get(name)
            if instance_cfg and instance_cfg.enabled and instance_cfg.schedule:
                self._bg_scheduler.add_job(
                    lambda n=name: self._run_and_notify(n, ctx),
                    CronTrigger.from_crontab(instance_cfg.schedule),
                    id=name,
                    name=name,
                    # 卡住的上一轮未结束时不再叠加新实例；错过的触发合并成一次；
                    # 允许迟到 1 小时内补跑（daemon 重启/机器重启后仍能补发当天消息）。
                    max_instances=1,
                    coalesce=True,
                    misfire_grace_time=3600,
                )

        self._bg_scheduler.start()
        log.info(
            "Scheduler daemon started with %d job(s).",
            len(self._bg_scheduler.get_jobs()),
        )
