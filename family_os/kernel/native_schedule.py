"""native_schedule.py —— 用操作系统原生调度器定时跑一次新闻任务。

为什么不用常驻进程：本地部署（家庭电脑/笔记本）不保证开机常驻，
``family-os serve`` 一旦退出就漏推。原生调度器按「到点触发一次」工作，
配合 ``catch_up``（开机后补跑）即可覆盖绝大多数场景。

三个平台：
  * Windows  → 任务计划（schtasks）
  * macOS    → launchd（LaunchAgents plist）
  * Linux    → 用户 crontab
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

MARKER = "uglycat-os:news"
TASK_NAME = "uglycat-os-news"
LAUNCHD_LABEL = "com.uglycat.news"

DEFAULT_COMMAND = "run news_digest --notify"


def detect_platform() -> str:
    if sys.platform.startswith("win"):
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    return "linux"


def _parse_time(time_str: str) -> tuple[int, int]:
    parts = (time_str or "").strip().split(":")
    if len(parts) != 2:
        raise ValueError(f"时间格式应为 HH:MM，收到：{time_str!r}")
    hour, minute = int(parts[0]), int(parts[1])
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"时间超出范围：{time_str!r}")
    return hour, minute


def build_plan(platform: str, time_str: str = "08:00",
               repo_root: str | Path = ".",
               python: str | None = None,
               command: str = DEFAULT_COMMAND) -> dict:
    """生成要落盘的文件与要执行的命令（不执行）。

    返回 ``{"platform", "files": [(path, content)], "commands": [...],
    "uninstall_commands": [...], "marker"}``。
    """
    hour, minute = _parse_time(time_str)
    repo = Path(repo_root).resolve()
    py = python or sys.executable
    args = f'"{py}" run.py {command}'

    if platform == "windows":
        tr = f'cmd /c cd /d "{repo}" && {args}'
        return {
            "platform": platform,
            "files": [],
            "commands": [["schtasks", "/Create", "/F", "/SC", "DAILY",
                          "/ST", f"{hour:02d}:{minute:02d}",
                          "/TN", TASK_NAME, "/TR", tr]],
            "uninstall_commands": [["schtasks", "/Delete", "/F", "/TN", TASK_NAME]],
            "marker": TASK_NAME,
        }

    if platform == "macos":
        plist_dir = Path.home() / "Library" / "LaunchAgents"
        plist_path = plist_dir / f"{LAUNCHD_LABEL}.plist"
        plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{LAUNCHD_LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>{py}</string>
    <string>run.py</string>
    {''.join(f'<string>{a}</string>' for a in command.split())}
  </array>
  <key>WorkingDirectory</key><string>{repo}</string>
  <key>StartCalendarInterval</key>
  <dict><key>Hour</key><integer>{hour}</integer><key>Minute</key><integer>{minute}</integer></dict>
  <key>StandardOutPath</key><string>{repo}/logs/cron.log</string>
  <key>StandardErrorPath</key><string>{repo}/logs/cron.log</string>
  <key>RunAtLoad</key><false/>
</dict>
</plist>
"""
        return {
            "platform": platform,
            "files": [(plist_path, plist)],
            "commands": [["launchctl", "unload", str(plist_path)],
                         ["launchctl", "load", str(plist_path)]],
            "uninstall_commands": [["launchctl", "unload", str(plist_path)]],
            "marker": LAUNCHD_LABEL,
        }

    # linux
    cron_line = (f"{minute} {hour} * * * cd {repo} && {args} "
                 f">> {repo}/logs/cron.log 2>&1  # {MARKER}")
    return {
        "platform": "linux",
        "files": [],
        "commands": [["crontab", "install"]],   # 特殊动作，见 apply_plan
        "uninstall_commands": [["crontab", "uninstall"]],
        "marker": MARKER,
        "cron_line": cron_line,
    }


def _read_crontab() -> str:
    try:
        proc = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    except FileNotFoundError:
        return ""
    return proc.stdout if proc.returncode == 0 else ""


def _write_crontab(content: str) -> None:
    subprocess.run(["crontab", "-"], input=content, text=True, check=True)


def _install_cron(cron_line: str) -> None:
    lines = [l for l in _read_crontab().splitlines() if MARKER not in l]
    lines.append(cron_line)
    _write_crontab("\n".join(lines).strip() + "\n")


def _uninstall_cron() -> None:
    lines = [l for l in _read_crontab().splitlines() if MARKER not in l]
    _write_crontab("\n".join(lines).strip() + "\n")


def apply_plan(plan: dict, dry_run: bool = False, uninstall: bool = False) -> list[str]:
    """落盘并执行计划；返回人类可读的执行记录。"""
    log: list[str] = []
    if uninstall:
        if plan["platform"] == "linux":
            if dry_run:
                return ["将移除 crontab 中带标记的行"]
            _uninstall_cron()
            return ["已从 crontab 移除"]
        if plan["platform"] == "macos":
            for path, _content in plan["files"]:
                if dry_run:
                    log.append(f"将删除 {path}")
                    continue
                path.unlink(missing_ok=True)
                log.append(f"已删除 {path}")
        for cmd in plan["uninstall_commands"]:
            if dry_run:
                log.append("将执行：" + " ".join(cmd))
                continue
            subprocess.run(cmd, capture_output=True, text=True)
            log.append("已执行：" + " ".join(cmd))
        return log

    for path, content in plan["files"]:
        if dry_run:
            log.append(f"将写入 {path}")
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        log.append(f"已写入 {path}")

    if plan["platform"] == "linux":
        if dry_run:
            log.append("将写入 crontab：" + plan["cron_line"])
        else:
            _install_cron(plan["cron_line"])
            log.append("已写入 crontab：" + plan["cron_line"])
        return log

    for cmd in plan["commands"]:
        if dry_run:
            log.append("将执行：" + " ".join(cmd))
            continue
        proc = subprocess.run(cmd, capture_output=True, text=True)
        log.append(("已执行：" if proc.returncode == 0 else "失败：")
                   + " ".join(cmd) + (f"（{proc.stderr.strip()[:80]}）" if proc.returncode else ""))
    return log


def build_uninstall_plan(platform: str, repo_root: str | Path = ".") -> dict:
    """卸载计划（复用 build_plan 的路径信息）。"""
    plan = build_plan(platform, "08:00", repo_root)
    plan["uninstall_commands"] = plan.get("uninstall_commands", [])
    return plan
