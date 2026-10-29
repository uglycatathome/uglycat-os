"""catch_up.py —— 启动时补跑当日漏掉的新闻任务。

本地部署场景（笔记本合盖、机器睡眠）下，定时任务可能被跳过。
启动（或开机调度触发）时若满足三个条件就补跑一次：
  1. 已过当日配置的推送时间；
  2. 当日 digest.json 不存在（说明没跑成）；
  3. 插件已启用。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import logging

logger = logging.getLogger("family_os.kernel.catch_up")

NEWS_PLUGIN = "news_digest"
DEFAULT_CRON = "0 8 * * *"


def _scheduled_hm(config) -> tuple[int, int] | None:
    """从 cron 表达式取出 (hour, minute)；非纯数字（如 */15）返回 None。"""
    inst = (config.plugin_instances or {}).get(NEWS_PLUGIN)
    cron = (inst.schedule if inst else None) or DEFAULT_CRON
    parts = cron.split()
    if len(parts) < 2:
        return None
    try:
        minute, hour = int(parts[0]), int(parts[1])
    except ValueError:
        return None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return hour, minute


def _digest_exists(data_dir: str | Path, date_str: str) -> bool:
    return (Path(data_dir) / "history" / "news_digest" / date_str / "digest.json").exists()


def maybe_catch_up(ctx, now: datetime | None = None, dry_run: bool = False) -> dict:
    """按需补跑；返回 ``{"ran": bool, "reason": str}``（不会抛异常）。"""
    now = now or datetime.now()
    data_dir = ctx.config.app.data_dir
    date_str = now.strftime("%Y-%m-%d")

    inst = (ctx.config.plugin_instances or {}).get(NEWS_PLUGIN)
    if inst is not None and not inst.enabled:
        return {"ran": False, "reason": "news_digest 未启用"}

    hm = _scheduled_hm(ctx.config)
    if hm is None:
        return {"ran": False, "reason": "调度时间无法解析，跳过补跑"}
    if (now.hour, now.minute) < hm:
        return {"ran": False, "reason": f"未到今日推送时间（{hm[0]:02d}:{hm[1]:02d}）"}
    if _digest_exists(data_dir, date_str):
        return {"ran": False, "reason": "今日产物已存在"}
    if dry_run:
        return {"ran": False, "reason": "dry-run：会补跑", "would_run": True}

    try:
        from family_os.kernel.discovery import discover_plugins
        from family_os.kernel.scheduler import Scheduler

        scheduler = Scheduler(ctx.config, discover_plugins(ctx.config.plugins.plugin_dir))
        result = scheduler.run_plugin(NEWS_PLUGIN, ctx)
    except Exception as exc:  # noqa: BLE001
        logger.warning("catch_up 补跑失败：%s", exc)
        return {"ran": False, "reason": f"补跑失败：{type(exc).__name__}: {exc}"}

    try:
        from family_os.kernel.notifier import Notifier

        Notifier(ctx.config).send(result, NEWS_PLUGIN)
    except Exception:  # noqa: BLE001
        logger.exception("catch_up 推送失败")

    return {"ran": True, "reason": "补跑完成", "status": result.status}
