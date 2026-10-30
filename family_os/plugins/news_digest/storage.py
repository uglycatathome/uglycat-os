"""storage.py —— news_digest 落盘。

- 每日运行快照：data/history/news_digest/<YYYY-MM-DD>/（raw + 各阶段输出 + digest）
- 措辞对比库：data/history/news_digest/meeting_archive/（永留存，cleanup 不删）
- 状态文件：data/history/news_digest/status.json（供本地排查与健康巡检）
"""

import json
import logging
import shutil
from datetime import datetime
from pathlib import Path

logger = logging.getLogger("family_os.news_digest.storage")

STATUS_FILE = "status.json"


# ── 目录定位 ─────────────────────────────────────────────────────────────

def history_dir(data_dir: str | Path) -> Path:
    return Path(data_dir) / "history" / "news_digest"


def run_dir_for(data_dir: str | Path, date: str) -> Path:
    return history_dir(data_dir) / date


def archive_dir(data_dir: str | Path) -> Path:
    return history_dir(data_dir) / "meeting_archive"


def seen_dir(data_dir: str | Path) -> Path:
    """跨天去重标题指纹目录：history/news_digest/seen/<date>.json"""
    return history_dir(data_dir) / "seen"


def selected_dir(data_dir: str | Path) -> Path:
    """近期已推送主题目录：history/news_digest/selected/<date>.json"""
    return history_dir(data_dir) / "selected"


# ── 跨天去重标题指纹 ─────────────────────────────────────────────────────

def load_seen_titles(data_dir: str | Path, window_days: int = 7) -> list[str]:
    """读取最近 window_days 天所有已处理标题（跨天去重指纹）。"""
    sdir = seen_dir(data_dir)
    if not sdir.exists():
        return []
    titles: list[str] = []
    cutoff = datetime.now().strftime("%Y-%m-%d")
    # seen 文件按日期命名，取最近 N 天
    dates = sorted(
        (f.stem for f in sdir.glob("*.json")
         if f.stem >= _date_n_days_ago(window_days)),
        reverse=True,
    )
    for d in dates:
        try:
            titles.extend(json.loads((sdir / f"{d}.json").read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError):
            continue
    return titles


def save_seen_titles(data_dir: str | Path, date: str, titles: list[str]) -> None:
    """把当日新增标题写入 seen/<date>.json（去重后不新增空文件）。"""
    if not titles:
        return
    sdir = seen_dir(data_dir)
    sdir.mkdir(parents=True, exist_ok=True)
    (sdir / f"{date}.json").write_text(
        json.dumps(sorted(set(titles)), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _date_n_days_ago(n: int) -> str:
    from datetime import timedelta

    return (datetime.now() - timedelta(days=n)).strftime("%Y-%m-%d")


# ── 近期已推送主题（供隔日 LLM 语义去重） ───────────────────────────────

def _normalize_topic(entry) -> dict:
    """统一主题条目：兼容旧版纯标题字符串与本版 {title,topic_key,summary}。"""
    if isinstance(entry, str):
        return {"title": entry.strip(), "topic_key": "", "summary": ""}
    if isinstance(entry, dict):
        return {
            "title": (entry.get("title") or "").strip(),
            "topic_key": (entry.get("topic_key") or "").strip(),
            "summary": " ".join((entry.get("summary") or "").split()),
        }
    return {"title": "", "topic_key": "", "summary": ""}


def save_selected_topics(data_dir: str | Path, date: str, topics: list) -> None:
    """记录当日实际精选推送的主题（title/topic_key/summary），供隔日 LLM 语义去重。

    兼容纯标题字符串与结构化 dict；无有效标题则不落文件。
    """
    entries: list[dict] = []
    seen: set[str] = set()
    for t in (topics or []):
        e = _normalize_topic(t)
        if not e["title"] or e["title"] in seen:
            continue
        seen.add(e["title"])
        entries.append(e)
    if not entries:
        return
    sdir = selected_dir(data_dir)
    sdir.mkdir(parents=True, exist_ok=True)
    (sdir / f"{date}.json").write_text(
        json.dumps(entries, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def load_recent_topics(data_dir: str | Path, window_days: int = 7) -> list[dict]:
    """读取最近 window_days 天已推送主题（新→旧），兼容旧版纯标题文件。

    返回 [{title, topic_key, summary}]，供精选阶段 LLM 做主题级语义去重。
    """
    sdir = selected_dir(data_dir)
    if not sdir.exists():
        return []
    cutoff = _date_n_days_ago(window_days)
    out: list[dict] = []
    for d in sorted((f.stem for f in sdir.glob("*.json") if f.stem >= cutoff),
                    reverse=True):
        try:
            data = json.loads((sdir / f"{d}.json").read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if not isinstance(data, list):
            continue
        out.extend(e for e in (_normalize_topic(x) for x in data) if e["title"])
    return out


# ── 每日快照 ─────────────────────────────────────────────────────────────

def save_raw_articles(run_dir: Path, articles: list[dict]) -> None:
    raw_dir = run_dir / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    for i, a in enumerate(articles, 1):
        content = (
            f"# {a.get('title','无标题')}\n\n"
            f"**来源**: {a.get('source_name','')}  **时间**: {a.get('published','')}\n\n"
            f"摘要：{a.get('summary','')}\n\n正文：{a.get('body','')}\n"
        )
        (raw_dir / f"{i:03d}.md").write_text(content, encoding="utf-8")


def save_output(run_dir: Path, name: str, content: str) -> Path:
    path = run_dir / f"{name}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def save_json(run_dir: Path, name: str, obj) -> Path:
    """落结构化 JSON（如 digest.json 供 web 渲染，避免解析 markdown）。"""
    path = run_dir / f"{name}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


# ── 措辞对比库（永留存） ─────────────────────────────────────────────────

def ingest_meeting_text(data_dir: str | Path, key: str, date: str,
                        text: str) -> Path:
    """把一份官方文本（如政治局公报全文）存入对比库。

    key 为主题名（如 zzjj = 政治局会议），date 为会议日期。
    同 key 同 date 已存在则跳过。
    """
    arch = archive_dir(data_dir)
    bucket = arch / _safe_key(key)
    bucket.mkdir(parents=True, exist_ok=True)
    dest = bucket / f"{date}.md"
    if dest.exists():
        logger.info("meeting text already exists: %s", dest)
        return dest
    dest.write_text(text, encoding="utf-8")
    _update_index(arch, key, date)
    logger.info("ingested meeting text: %s", dest)
    return dest


def list_meeting_texts(data_dir: str | Path, key: str) -> list[dict]:
    """列出某主题下历次文本（倒序），供对比：{date, file}。"""
    bucket = archive_dir(data_dir) / _safe_key(key)
    if not bucket.exists():
        return []
    out = []
    for f in sorted(bucket.glob("*.md"), reverse=True):
        out.append({"date": f.stem, "file": str(f)})
    return out


def get_recent_meeting_texts(data_dir: str | Path, key: str, n: int = 2) -> list[dict]:
    """取最近 n 份文本原文，供对比 prompt。"""
    out = []
    for item in list_meeting_texts(data_dir, key):
        path = Path(item["file"])
        if path.exists():
            out.append({"date": item["date"], "text": path.read_text(encoding="utf-8")})
        if len(out) >= n:
            break
    return out


def _update_index(arch: Path, key: str, date: str) -> None:
    index_path = arch / "index.json"
    index = {}
    if index_path.exists():
        try:
            index = json.loads(index_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            index = {}
    entries = index.get(key, [])
    if all(e.get("date") != date for e in entries):
        entries.append({"date": date})
    index[key] = entries
    index_path.write_text(json.dumps(index, ensure_ascii=False, indent=2))


def _safe_key(key: str) -> str:
    return "".join(c for c in key if c.isalnum() or c in "-_").strip() or "default"


# ── 状态 ─────────────────────────────────────────────────────────────────

def write_status(data_dir: str | Path, status: str, reason: str = "") -> None:
    path = history_dir(data_dir) / STATUS_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "date": datetime.now().strftime("%Y-%m-%d"),
        "status": status,
        "reason": reason,
    }, ensure_ascii=False, indent=2), encoding="utf-8")


# ── 清理 ─────────────────────────────────────────────────────────────────

def cleanup(data_dir: str | Path, keep_runs: int = 7) -> None:
    """轮换旧每日快照与 seen/selected 指纹，保留最近 keep_runs 天。meeting_archive 永不清理。"""
    hist = history_dir(data_dir)
    run_dirs = sorted(
        [d for d in hist.iterdir()
         if d.is_dir() and d.name not in ("meeting_archive", "seen", "selected")],
        reverse=True,
    )
    for old in run_dirs[keep_runs:]:
        shutil.rmtree(old)
        logger.info("Rotated old news run: %s", old)

    # 顺带轮换 seen/<date>.json 与 selected/<date>.json（与快照同窗口）
    cutoff = _date_n_days_ago(keep_runs)
    for sub in ("seen", "selected"):
        sdir = hist / sub
        if sdir.exists():
            for f in sdir.glob("*.json"):
                if f.stem < cutoff:
                    f.unlink(missing_ok=True)
                    logger.info("Rotated old %s titles: %s", sub, f)