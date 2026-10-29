"""启动补跑：错过 08:00 且当日无产物时才补跑。"""

from datetime import datetime
from types import SimpleNamespace

import pytest


def _ctx(tmp_path, schedule="0 8 * * *", enabled=True, plugin_dir="./plugins"):
    from family_os.kernel.config import (
        AppConfig, ApprovalConfig, Config, DigestConfig, LLMConfig,
        NotifierConfig, PluginConfig, PluginInstanceConfig, SchedulerConfig, Secrets,
    )
    from family_os.kernel.context import Context

    instances = {"news_digest": PluginInstanceConfig(
        enabled=enabled, schedule=schedule, notify_on=["family"])}
    config = Config(
        app=AppConfig(data_dir=str(tmp_path)),
        llm=LLMConfig(),
        plugins=PluginConfig(enabled=["news_digest"], plugin_dir=plugin_dir),
        scheduler=SchedulerConfig(),
        digest=DigestConfig(),
        approval=ApprovalConfig(),
        notifier=NotifierConfig(channels=[]),
        secrets=Secrets(),
        plugin_instances=instances,
    )
    return Context(store=None, config=config, llm_gateway=None)


def _write_digest(tmp_path, date_str):
    from pathlib import Path

    d = Path(tmp_path) / "history" / "news_digest" / date_str
    d.mkdir(parents=True, exist_ok=True)
    (d / "digest.json").write_text("{}", encoding="utf-8")


def test_skip_before_scheduled_time(tmp_path):
    from family_os.kernel.catch_up import maybe_catch_up

    ctx = _ctx(tmp_path)
    res = maybe_catch_up(ctx, now=datetime(2026, 10, 30, 7, 0))
    assert res["ran"] is False
    assert "未到" in res["reason"]


def test_would_run_after_time_without_digest(tmp_path):
    from family_os.kernel.catch_up import maybe_catch_up

    ctx = _ctx(tmp_path)
    res = maybe_catch_up(ctx, now=datetime(2026, 10, 30, 9, 0), dry_run=True)
    assert res["ran"] is False
    assert res.get("would_run") is True


def test_skip_when_digest_exists(tmp_path):
    from family_os.kernel.catch_up import maybe_catch_up

    ctx = _ctx(tmp_path)
    _write_digest(tmp_path, "2026-10-30")
    res = maybe_catch_up(ctx, now=datetime(2026, 10, 30, 9, 0), dry_run=True)
    assert res["ran"] is False
    assert "已存在" in res["reason"]


def test_skip_when_plugin_disabled(tmp_path):
    from family_os.kernel.catch_up import maybe_catch_up

    ctx = _ctx(tmp_path, enabled=False)
    res = maybe_catch_up(ctx, now=datetime(2026, 10, 30, 9, 0), dry_run=True)
    assert res["ran"] is False
    assert "未启用" in res["reason"]


def test_unparsable_schedule_skips(tmp_path):
    from family_os.kernel.catch_up import maybe_catch_up

    ctx = _ctx(tmp_path, schedule="*/15 8 * * *")
    res = maybe_catch_up(ctx, now=datetime(2026, 10, 30, 9, 0), dry_run=True)
    assert res["ran"] is False
    assert "无法解析" in res["reason"]
