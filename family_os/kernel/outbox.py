"""outbox.py —— Server酱（个人微信）失败消息的待补推队列（2026-11-11）。

背景：VM 在境外，到 ``sctapi.ftqq.com`` 偶发 TLS 握手超时；旧实现「失败只记
warning、不重试、不落盘」→ 消息永久静默丢失，只能靠用户发现手机没响。

安全口径（**补推前必须分清**）：

- ``status="pending"``：**确定未送达**（连接/握手阶段失败、服务端明确拒绝），
  可自动补推。
- ``status="uncertain"``：**可能已送达**（读超时 / 非 200 / 响应非 JSON /
  未知异常），绝不自动补推 —— Server酱免费版每天只有 5 条，重复推送既打扰
  用户又浪费额度，只能人工确认后手动补推。
- ``status="sending"``：补推已发出但没写回结果（进程崩溃残留）。下次扫描
  一律降级为 ``uncertain``，不重发。
- ``status="exhausted"`` / 过期：不再自动补推，保留文件供周报与人工处理。

只做「存/取/状态流转」，不碰网络；发送函数由 Notifier 注入，便于单测。
"""
from __future__ import annotations

import json
import logging
import os
import re
import uuid
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

OUTBOX_SUBDIR = Path("outbox") / "serverchan"
_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9_-]")


# ── 目录与原子读写 ───────────────────────────────────────────────────────

def outbox_dir(data_dir: str | Path) -> Path:
    return Path(data_dir) / OUTBOX_SUBDIR


def _write(path: Path, entry: dict) -> None:
    """原子写：先写 .tmp 再 replace，避免补推过程中断电留下半截 JSON。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(entry, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    os.replace(tmp, path)


def _safe(name: str) -> str:
    return _SAFE_NAME_RE.sub("_", name or "unknown")[:40]


# ── 入队 / 遍历 ──────────────────────────────────────────────────────────

def enqueue(data_dir: str | Path, *, plugin: str, group: str, title: str,
            desp: str, reason: str, uncertain: bool = False,
            entry_id: str | None = None, attempts: int = 0) -> Path:
    """把一条发送失败的消息落盘，返回文件路径。"""
    now = datetime.now()
    eid = entry_id or uuid.uuid4().hex[:12]
    entry = {
        "id": eid,
        "created_at": now.isoformat(timespec="seconds"),
        "updated_at": now.isoformat(timespec="seconds"),
        "plugin": plugin,
        "group": group,
        "title": title,
        "desp": desp,
        "reason": reason,
        "status": "uncertain" if uncertain else "pending",
        "attempts": attempts,
    }
    path = outbox_dir(data_dir) / (
        f"{now.strftime('%Y%m%d_%H%M%S')}_{_safe(plugin)}_{_safe(group)}_{eid}.json")
    _write(path, entry)
    return path


def iter_entries(data_dir: str | Path) -> list[tuple[Path, dict]]:
    """读取全部条目；损坏的 JSON 跳过并记录 warning，不影响其它条目。"""
    d = outbox_dir(data_dir)
    if not d.exists():
        return []
    out: list[tuple[Path, dict]] = []
    for path in sorted(d.glob("*.json")):
        try:
            entry = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.warning("[outbox] 条目损坏，已跳过 %s: %s", path.name, exc)
            continue
        if isinstance(entry, dict):
            out.append((path, entry))
    return out


def is_expired(entry: dict, max_age_hours: int) -> bool:
    """超过 max_age_hours 的条目不再自动补推（隔夜新闻已无意义）。"""
    try:
        created = datetime.fromisoformat(entry.get("created_at", ""))
    except ValueError:
        return False
    return datetime.now() - created > timedelta(hours=max_age_hours)


def update(path: Path, entry: dict) -> None:
    entry["updated_at"] = datetime.now().isoformat(timespec="seconds")
    _write(path, entry)


def remove(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


# ── 自动补推 ─────────────────────────────────────────────────────────────

def flush(data_dir: str | Path, send_fn, *, max_age_hours: int = 24,
          max_attempts: int = 3) -> dict:
    """尝试补推所有可安全补推的条目。

    ``send_fn(title, desp) -> (sent: bool, uncertain: bool, reason: str)``
    由 Notifier 注入；``uncertain=True`` 表示请求可能已被服务端接收。

    返回统计字典，供日志 / CLI 输出。
    """
    stats = {"sent": 0, "failed": 0, "skipped_uncertain": 0,
             "skipped_expired": 0, "exhausted": 0}
    for path, entry in iter_entries(data_dir):
        status = entry.get("status")
        if status == "sending":
            # 上一次补推发出后没写回结果 → 无法判断是否送达，永不自动重发。
            entry["status"] = "uncertain"
            entry["reason"] = "补推中断（sending 残留，可能已送达）"
            update(path, entry)
            stats["skipped_uncertain"] += 1
            continue
        if status == "uncertain":
            stats["skipped_uncertain"] += 1
            continue
        if status != "pending":
            continue
        if is_expired(entry, max_age_hours):
            stats["skipped_expired"] += 1
            continue
        attempts = int(entry.get("attempts") or 0)
        if attempts >= max_attempts:
            entry["status"] = "exhausted"
            update(path, entry)
            stats["exhausted"] += 1
            continue

        # 先落 sending 再发：即使进程在此刻崩溃，也不会下次重复推送。
        entry["status"] = "sending"
        update(path, entry)
        try:
            sent, uncertain, reason = send_fn(entry.get("title", ""),
                                              entry.get("desp", ""))
        except Exception as exc:  # noqa: BLE001 — 发送回调异常一律按「可能已送达」处理
            sent, uncertain, reason = False, True, f"{type(exc).__name__}: {exc}"
        if sent:
            remove(path)
            stats["sent"] += 1
            continue
        entry["status"] = "uncertain" if uncertain else "pending"
        entry["attempts"] = attempts + 1
        entry["reason"] = reason
        entry["updated_at"] = datetime.now().isoformat(timespec="seconds")
        update(path, entry)
        stats["failed"] += 1
    return stats


# ── 健康周报视图 ─────────────────────────────────────────────────────────

def summarize(data_dir: str | Path, max_age_hours: int = 24) -> dict:
    """返回 health_check 周报约定的 ``{status, ok, notes, issues}``。"""
    entries = [e for _, e in iter_entries(data_dir)]
    counts = Counter(e.get("status", "?") for e in entries)
    expired = sum(1 for e in entries
                  if e.get("status") == "pending"
                  and is_expired(e, max_age_hours))
    ok, notes, issues = [], [], []
    if not entries:
        ok.append("个人微信推送队列为空（无漏推）")
    if counts["pending"]:
        issues.append(f"Server酱待自动补推 {counts['pending']} 条，请确认手机是否收到")
    if counts["uncertain"]:
        issues.append(f"{counts['uncertain']} 条状态不明（可能已送达），需人工确认后手动补推")
    if counts["sending"]:
        issues.append(f"{counts['sending']} 条补推中断残留，需人工确认")
    if counts["exhausted"]:
        issues.append(f"{counts['exhausted']} 条补推已耗尽重试上限，需人工处理")
    if expired:
        notes.append(f"{expired} 条待补推消息已超过 {max_age_hours}h（不再自动补推）")
    return {"status": "issue" if issues else "ok",
            "ok": ok, "notes": notes, "issues": issues}
