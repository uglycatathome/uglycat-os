"""doctor.py —— 部署环境体检。

供用户与 AI 安装代理共用的唯一判据：`doctor --json` 输出 ``ok`` 字段，
代理只看这个字段决定"装完没有"，不解析人话日志。

检查项（前五类为硬性，失败即 ok=false）：
  1. Python 版本 >= 3.11
  2. 依赖可导入（不触发实际网络/重依赖初始化）
  3. config.yaml 可加载
  4. data_dir 可写、SQLite 可用
  5. LLM 凭据已配置（至少一个 provider）
软性项（只告警）：调度表达式可解析、Server酱 SendKey、通知渠道。
可选：--network 时逐个探测新闻源可达性（默认关闭，避免体检本身依赖外网）。
"""

from __future__ import annotations

import json
import sqlite3
import sys
from importlib.util import find_spec
from pathlib import Path

MIN_PYTHON = (3, 11)

# 核心依赖：缺任一即安装未完成
_REQUIRED_MODULES = (
    "typer", "pydantic", "pydantic_settings", "yaml", "dotenv",
    "httpx", "feedparser", "selectolax", "apscheduler", "litellm",
)

_LLM_KEY_FIELDS = (
    "deepseek_api_key", "openai_api_key", "anthropic_api_key",
    "siliconflow_api_key", "zhipu_api_key",
)


def _item(name: str, ok: bool, detail: str, level: str = "error") -> dict:
    """一条检查结果；level=warn 时不影响整体 ok。"""
    return {"name": name, "ok": bool(ok), "level": level, "detail": str(detail)}


def _check_python() -> dict:
    version = sys.version_info
    ok = (version.major, version.minor) >= MIN_PYTHON
    return _item("python", ok,
                 f"{version.major}.{version.minor}.{version.micro}"
                 + ("" if ok else f"（需要 >= {MIN_PYTHON[0]}.{MIN_PYTHON[1]}）"))


def _check_deps() -> list[dict]:
    out = []
    for mod in _REQUIRED_MODULES:
        present = find_spec(mod) is not None
        out.append(_item(f"dep:{mod}", present,
                         "已安装" if present else "缺失，请重新运行安装脚本"))
    return out


def _check_config(config_path: Path) -> tuple[dict | None, list[dict]]:
    """加载配置；返回 (config | None, 检查项列表)。"""
    checks: list[dict] = []
    if not config_path.exists():
        checks.append(_item("config", False, f"找不到 {config_path}"))
        return None, checks
    try:
        from family_os.kernel.config import Config

        config = Config.load(config_path)
    except Exception as exc:  # noqa: BLE001
        checks.append(_item("config", False, f"加载失败：{type(exc).__name__}: {exc}"))
        return None, checks
    checks.append(_item("config", True, f"已加载 {config_path}"))
    return config, checks


def _check_data_dir(config) -> list[dict]:
    """数据目录可写 + SQLite 可用 + 数据库可初始化。"""
    checks: list[dict] = []
    data_dir = Path(config.app.data_dir).expanduser()
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        probe = data_dir / ".write_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        checks.append(_item("data_dir", True, f"可写：{data_dir}"))
    except Exception as exc:  # noqa: BLE001
        checks.append(_item("data_dir", False, f"不可写：{data_dir}（{exc}）"))
        return checks

    try:
        conn = sqlite3.connect(data_dir / ".doctor.db")
        conn.execute("CREATE TABLE IF NOT EXISTS _probe (id INTEGER)")
        conn.close()
        (data_dir / ".doctor.db").unlink(missing_ok=True)
        checks.append(_item("sqlite", True, "可用"))
    except Exception as exc:  # noqa: BLE001
        checks.append(_item("sqlite", False, f"不可用：{exc}"))
    return checks


def _check_llm(config) -> list[dict]:
    """至少一个 LLM provider 的 Key 已配置（不联网校验）。"""
    secrets = config.secrets
    present = [f for f in _LLM_KEY_FIELDS if getattr(secrets, f, None)]
    if present:
        return [_item("llm_key", True,
                      "已配置：" + ", ".join(f.replace("_api_key", "").upper() for f in present))]
    return [_item("llm_key", False,
                  "未配置任何 provider 的 API Key，请在 .env 中设置 "
                  "ZHIPU_API_KEY / SILICONFLOW_API_KEY / DEEPSEEK_API_KEY 之一")]


def _check_schedules(config) -> list[dict]:
    """已启用插件的 cron 表达式是否可解析（软性）。"""
    from apscheduler.triggers.cron import CronTrigger

    checks: list[dict] = []
    for name, inst in (config.plugin_instances or {}).items():
        if not inst.enabled or not inst.schedule:
            continue
        try:
            CronTrigger.from_crontab(inst.schedule)
        except Exception as exc:  # noqa: BLE001
            checks.append(_item(f"schedule:{name}", False,
                                f"{inst.schedule} 无法解析：{exc}", level="warn"))
    if not checks:
        checks.append(_item("schedule", True, "无启用的定时任务或表达式均可解析"))
    return checks


def _check_notifier(config) -> list[dict]:
    """通知渠道是否至少有一个可用（软性）。"""
    checks: list[dict] = []
    channels = [c for c in config.notifier.channels if c.enabled]
    if not channels:
        return [_item("notifier", False, "没有启用的通知渠道（可先用 file 渠道）",
                      level="warn")]
    for ch in channels:
        if ch.type == "serverchan":
            key = getattr(config.secrets, ch.sendkey_env.lower(), None)
            checks.append(_item(
                "notifier:serverchan", bool(key),
                "SendKey 已配置" if key else "未配置 SendKey，推送不会到达微信",
                level="warn"))
        else:
            checks.append(_item(f"notifier:{ch.type}", True, "已启用"))
    return checks


def _check_sources(config_path: Path, network: bool) -> list[dict]:
    """新闻源可达性（默认关闭，--network 才探测）。"""
    if not network:
        return [_item("sources", True, "跳过外网探测（加 --network 开启）", level="warn")]
    sources_file = config_path.parent / "sources.yaml"
    try:
        import yaml

        raw = yaml.safe_load(sources_file.read_text(encoding="utf-8")) or {}
        sources = [s for s in raw.get("sources", []) if "新闻" in (s.get("tags") or [])]
    except Exception as exc:  # noqa: BLE001
        return [_item("sources", False, f"读取 {sources_file} 失败：{exc}")]
    if not sources:
        return [_item("sources", False, "没有 tag 含「新闻」的源")]

    import httpx

    ok_count, failures = 0, []
    for src in sources:
        target = src.get("list_url") or src.get("url")
        try:
            httpx.get(target, timeout=8.0, follow_redirects=True,
                      headers={"User-Agent": "Mozilla/5.0"}).raise_for_status()
            ok_count += 1
        except Exception as exc:  # noqa: BLE001
            failures.append(f"{src.get('id')}({type(exc).__name__})")
    detail = f"可达 {ok_count}/{len(sources)}"
    if failures:
        detail += "；失败：" + ", ".join(failures[:5])
    return [_item("sources", ok_count > 0, detail)]


def run_doctor(config_path: str | Path = "family_os/config/config.yaml",
               network: bool = False) -> dict:
    """执行全部检查，返回 ``{"ok": bool, "checks": [...]}``。"""
    config_path = Path(config_path)
    checks: list[dict] = [_check_python(), *_check_deps()]

    config, config_checks = _check_config(config_path)
    checks.extend(config_checks)
    if config is not None:
        checks.extend(_check_data_dir(config))
        checks.extend(_check_llm(config))
        checks.extend(_check_schedules(config))
        checks.extend(_check_notifier(config))
        checks.extend(_check_sources(config_path, network))

    ok = all(c["ok"] for c in checks if c["level"] == "error")
    return {"ok": ok, "checks": checks}


def format_checks(result: dict) -> str:
    """人类可读输出（AI 应改用 --json）。"""
    lines = []
    for c in result["checks"]:
        mark = "OK  " if c["ok"] else ("WARN" if c["level"] == "warn" else "FAIL")
        lines.append(f"[{mark}] {c['name']}: {c['detail']}")
    lines.append("")
    lines.append("体检通过 ✓" if result["ok"] else "体检未通过 ✗（见上面 FAIL 项）")
    return "\n".join(lines)


def dumps(result: dict) -> str:
    return json.dumps(result, ensure_ascii=False, indent=2)
