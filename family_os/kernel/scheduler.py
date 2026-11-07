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

# 单个作业的默认硬截止（秒）。正常一轮 market_pulse 约 270~420s；超时说明有调用在
# 无限等待（历史事故 5h20m / 45min），必须截断否则当天消息发不出去。
# **不是所有插件都适用**：每周六 strategy_radar 是 4 赛道 × 4 stage 的批量 LLM 任务，
# 实测 30~90 分钟，用 900s 会在周报生成前误判超时（2026-11-07 事故）。长跑插件通过
# config.yaml 的 plugin_instances.<name>.hard_timeout_s 单独放宽，见 _job_timeout_s()。
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
            fetch_schedule = instance_cfg.fetch_schedule if instance_cfg else None
            if schedule:
                jobs.append({
                    "plugin": name,
                    "schedule": schedule,
                    "fetch_schedule": fetch_schedule,
                    "cost_tier": plugin_cls.cost_tier,
                })
        jobs.sort(key=lambda j: j["plugin"])
        return jobs

    # ── run_plugin ─────────────────────────────────────────────────────────

    def run_plugin(self, plugin_name: str, ctx: Context,
                   stage: str | None = None) -> Result:
        """Look up a plugin by name, instantiate it, and run it.

        Parameters
        ----------
        plugin_name : str
            The ``name`` attribute of the desired plugin.
        ctx : Context
            Dependency-injection container.
        stage : str | None
            可选阶段名（``"fetch"`` / ``"publish"``）。仅当插件实例配了
            ``fetch_schedule`` 时才会传入；其它插件仍走 ``run(ctx)``。

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
            if stage is None:
                return plugin.run(ctx)
            return plugin.run(ctx, stage=stage)
        except Exception as exc:
            log.exception("Plugin %s failed", plugin_name)
            return Result(status="error", error=str(exc))

    # ── 作业硬截止 ────────────────────────────────────────────────────────

    def _job_timeout_s(self, plugin_name: str) -> float:
        """返回该插件的单作业硬截止：配置覆盖优先，否则用全局 900s。"""
        instances = getattr(self._config, "plugin_instances", None) or {}
        instance_cfg = instances.get(plugin_name) if hasattr(instances, "get") else None
        custom = getattr(instance_cfg, "hard_timeout_s", None)
        # 严格判数值：测试里 config 常是 MagicMock，Mock 参与 `> 0` 会抛 TypeError。
        if isinstance(custom, (int, float)) and not isinstance(custom, bool) and custom > 0:
            return float(custom)
        return _JOB_HARD_TIMEOUT_S

    # ── _run_and_notify (daemon only) ──────────────────────────────────────

    def _run_and_notify(self, plugin_name: str, ctx: Context,
                        stage: str | None = None) -> None:
        """Run a plugin and dispatch its result through the Notifier.

        This is the daemon-only path.  Manual CLI invocations go through
        ``run_plugin()`` directly so the caller controls output formatting.
        """
        from concurrent.futures import ThreadPoolExecutor
        from concurrent.futures import TimeoutError as FutTimeout
        from datetime import datetime

        label = plugin_name if stage is None else f"{plugin_name}[{stage}]"
        timeout_s = self._job_timeout_s(plugin_name)
        t0 = datetime.now()
        print(f"[{t0.strftime('%H:%M:%S')}] {label} job started", flush=True)

        # 作业级硬截止：插件内部的网络调用即使全都没有超时（历史上两次真实事故：
        # 2026-11-04 core 层分页接口死等、2026-11-05 ref 层 ssl.read 死等），
        # 这里也必须把控制权拿回来——否则整个 daemon 线程被占死，当天一条消息都发不出去。
        # 线程无法强杀，超时后放弃等待并回报 error，让 Notifier 发出「❌ 任务超时」。
        ex = ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"job-{label}")
        try:
            result = ex.submit(self.run_plugin, plugin_name, ctx, stage).result(
                timeout=timeout_s)
        except FutTimeout:
            log.critical(
                "Plugin %s exceeded hard deadline %.0fs — abandoned (worker thread "
                "still stuck in background)", label, timeout_s,
            )
            print(f"[{datetime.now().strftime('%H:%M:%S')}] {label} HARD TIMEOUT "
                  f"after {timeout_s:.0f}s — 已放弃等待", flush=True)
            # 夜间抓取阶段超时也不能推群：那时候家里人在睡觉，早上发布时再披露。
            if stage == "fetch":
                result = Result(status="skipped", data={
                    "mode": "fetch_hard_timeout",
                    "reason": f"抓取超过 {timeout_s:.0f} 秒未完成，已放弃等待",
                    "_messages": {},
                })
            else:
                result = Result(
                    status="error",
                    error=(f"{label} 任务超过 {timeout_s:.0f} 秒未完成，"
                           f"已放弃等待（当天数据可能不完整）"),
                )
        finally:
            ex.shutdown(wait=False)

        dt = (datetime.now() - t0).total_seconds()
        print(f"[{datetime.now().strftime('%H:%M:%S')}] {label} done "
              f"({dt:.0f}s, status={result.status})", flush=True)
        try:
            notifier = Notifier(ctx.config)
            notifier.send(result, plugin_name)
        except Exception:
            log.exception(
                "Notifier failed for plugin %s (result status=%s)",
                label,
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
            if not (instance_cfg and instance_cfg.enabled):
                continue
            # 主 schedule 走完整流程/发布；fetch_schedule（可选）走抓取阶段。
            # 目前只有 market_pulse 两条都配：夜里抓、早上发。
            for stage, cron in ((None, instance_cfg.schedule),
                                ("fetch", instance_cfg.fetch_schedule)):
                if not cron:
                    continue
                job_id = name if stage is None else f"{name}.{stage}"
                self._bg_scheduler.add_job(
                    lambda n=name, s=stage: self._run_and_notify(n, ctx, stage=s),
                    CronTrigger.from_crontab(cron),
                    id=job_id,
                    name=job_id,
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
