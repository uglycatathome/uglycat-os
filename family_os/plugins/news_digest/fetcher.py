"""fetcher.py —— 每日新闻抓取。

复用 kernel.ingestion.SourceRegistry 按 tags 订阅 RSS 源，对每条抓标题 +
summary；当 summary 过短时尝试抓取正文全文。官方公报类文本若无法自动抓取，
通过 storage.ingest_meeting_text 人工灌入（见 plugin CLI）。
"""

import logging
import re

import feedparser
import httpx

from family_os.kernel.ingestion import SourceRegistry

logger = logging.getLogger("family_os.news_digest.fetcher")

# 订阅源 tag：chinanews 财经/时政已补「新闻」tag
FETCH_TAGS = ["新闻"]
MAX_AGE_DAYS = 2          # 每日推送，只看近 2 天新闻
SUMMARY_MIN_CHARS = 60    # summary 短于该值则尝试抓正文

# ── web_list：无 RSS 的机构站（社科院各研究所）────────────────────────────
# 2026-10-23：社科院/cass.cn、社科网/cssn.cn、国研中心、NIFD、CF40 等均无可用
# RSS（/rss /feed /atom.xml 全 404，RSSHub 镜像 /cass /gov/cass /drc 亦 404）。
# 社科院各所官网的栏目列表页可解析，且条目 URL 内嵌日期（tYYYYMMDD_xxxx.shtml），
# 故以 **URL 里的日期** 作为时效依据——页面上的「日期」常取自父容器，实测
# gjs.cssn.cn 一条 2026-10-20 的科研成果所在容器显示的是 2026年9月7，不可信。
_URL_DATE_RE = re.compile(r"t(20\d{2})(\d{2})(\d{2})_\d+")
# 事务性条目：研究所列表页混有大量行政信息（会议通知/征稿/招聘），抓进新闻池只会污染。
# 2026-11-01：裸「采购」会误杀统计局「采购经理指数（PMI）」——实装后 PMI 月度发布
# 直接被过滤掉，故收窄为采购公告/项目/意向等真正的采购事务。
_LIST_EXCLUDE = ("征稿", "招聘", "招标", "采购公告", "采购项目", "采购意向",
                 "讣告", "公示", "会议通知", "讲座", "研讨会", "报名", "启事",
                 "课题申报", "申报通知")
# 列表标题常带日期括注，如「…… [2026-10-09]」
_TITLE_DATE_TAIL = re.compile(r"\s*[\[（(]?\d{4}[-/.]\d{1,2}[-/.]\d{1,2}[\]）)]?\s*$")


def fetch_articles(data_dir, registry: SourceRegistry) -> list[dict]:
    """抓取当日新闻，返回标准化 article 列表。

    article dict 字段：title / url / summary / body / published / source_id /
    source_name。body 为可选的正文全文。
    """
    sources = registry.get_sources_by_tags(FETCH_TAGS)
    if not sources:
        logger.warning("No sources found for tags %s", FETCH_TAGS)
        return []

    # 去重：同一 URL 只抓一次（SourceRegistry 约定）
    seen = set()
    articles = []
    for source in sources:
        try:
            for a in _fetch_source(source):
                if a["url"] and a["url"] in seen:
                    continue
                seen.add(a["url"])
                articles.append(a)
        except Exception as e:  # 单源失败不阻塞整体
            logger.error("Failed to fetch source %s: %s", source["id"], e)
    return articles


def _fetch_source(source: dict) -> list[dict]:
    # 机构站（无 RSS）：走列表页解析分支
    if source.get("type") == "web_list":
        return _fetch_web_list(source)

    client = httpx.Client(timeout=15, follow_redirects=True)
    try:
        resp = client.get(source["url"], headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
    finally:
        client.close()

    feed = feedparser.parse(resp.text)
    # 2026-11-01：RSS 源也支持按源配置时效窗口（原先只有 web_list 支持）。
    # 部委/美联储这类「不定期发一次」的源需要 3~4 天窗才不至于永远抓空；
    # 未配置的源仍沿用 MAX_AGE_DAYS=2，行为不变。
    cutoff = _now_minus(days=int(source.get("max_age_days", MAX_AGE_DAYS)))

    out = []
    for entry in feed.entries[:50]:
        published = _parse_date(entry)
        if published and published < cutoff:
            continue
        summary = _clean_html(entry.get("summary", entry.get("description", "")))
        body = _fetch_body(entry.get("link")) if len(summary) < SUMMARY_MIN_CHARS else ""
        out.append({
            "title": entry.get("title", "").strip(),
            "url": entry.get("link", ""),
            "summary": summary,
            "body": body,
            "published": published.isoformat() if published else "",
            "source_id": source["id"],
            "source_name": source.get("name", source["id"]),
        })
    return out


def _fetch_web_list(source: dict) -> list[dict]:
    """从机构栏目列表页抓取条目（无 RSS 时的替代通道）。

    只认 URL 内嵌日期的链接（`link_pattern` 的第 1~3 组为年/月/日），
    按 `max_age_days`（默认 MAX_AGE_DAYS）过滤；排除微信外链与事务性条目。
    返回按发布时间倒序的前 `max_items` 条，并逐条抓正文供后续措辞提取。
    """
    from datetime import datetime, timezone
    from urllib.parse import urljoin

    list_url = source.get("list_url") or source["url"]
    resp = httpx.get(list_url, timeout=20, follow_redirects=True,
                     headers={"User-Agent": "Mozilla/5.0"})
    resp.raise_for_status()
    pattern = re.compile(source.get("link_pattern") or _URL_DATE_RE.pattern)

    try:
        from selectolax.parser import HTMLParser

        anchors = [(a.text() or "", a.attributes.get("href") or "")
                   for a in HTMLParser(resp.text).css("a[href]")]
    except ImportError:  # 与 _extract_body 一致：缺 selectolax 时退回正则
        anchors = [(m.group(2), m.group(1)) for m in re.finditer(
            r'<a[^>]+href="([^"]+)"[^>]*>([^<]+)</a>', resp.text)]

    cutoff = _now_minus(int(source.get("max_age_days", MAX_AGE_DAYS)))
    max_items = int(source.get("max_items", 5))
    # 标题白词：机构首页/综合栏目混有大量与本机构主题无关的转载（求是网首页 37 条
    # 绝大多数是习近平活动报道），只保留命中白词的条目
    title_include = source.get("title_include") or []

    out: list[dict] = []
    seen: set[str] = set()
    for raw_title, href in anchors:
        title = _TITLE_DATE_TAIL.sub("", " ".join(raw_title.split())).strip()
        if len(title) < 8 or not href or href.startswith(("#", "javascript")):
            continue
        if any(w in title for w in _LIST_EXCLUDE):
            continue
        if title_include and not any(w in title for w in title_include):
            continue
        url = urljoin(list_url, href)
        if "mp.weixin.qq.com" in url:
            continue
        m = pattern.search(url)
        if not m or m.lastindex < 3:
            continue
        try:
            published = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)),
                                 tzinfo=timezone.utc)
        except ValueError:
            continue
        if published < cutoff or url in seen:
            continue
        seen.add(url)
        out.append({
            "title": title,
            "url": url,
            "summary": "",
            "body": "",
            "published": published.isoformat(),
            "source_id": source["id"],
            "source_name": source.get("name", source["id"]),
        })

    out.sort(key=lambda a: a["published"], reverse=True)
    items = out[:max_items]
    for it in items:
        it["body"] = _fetch_body(it["url"])
    return items


def _fetch_body(url: str) -> str:
    """抓取文章正文全文。失败返回空串（降级用 summary）。"""
    if not url:
        return ""
    try:
        resp = httpx.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
        return _extract_body(resp.text)
    except Exception as e:
        logger.debug("body fetch failed %s: %s", url, str(e)[:80])
        return ""


def _extract_body(html: str, max_chars: int = 3000) -> str:
    """用 selectolax 提取正文纯文本（优先 <p> 段落，剔除 style/script/nav 噪音）。

    2026-10-13：修复原实现——它在循环里对**每次新建的解析器**做 decompose，
    真正用于取正文的 tree 从未被清理，导致 <style> 里的 CSS/脚本原文原样泄漏
    进正文（中新网-滚动等源曾整篇正文都是 CSS）。现改为单解析器复用；并改为
    **优先拼接 <p> 段落**——纯 body 文本会先取到站点导航菜单/版权行等链接型
    chrome，而正文段落几乎都在 <p> 里；<p> 太少再退回整个 body 文本。数据缺失
    或疑似 CSS/JS 垃圾则返回空串（降级用 summary），避免把噪音喂给 LLM 造成误判。
    """
    try:
        from selectolax.parser import HTMLParser

        tree = HTMLParser(html)
        for sel in ("script", "style", "noscript", "nav", "header", "footer",
                    "aside"):
            for node in tree.css(sel):
                node.decompose()
        text = _paragraph_text(tree)
        if len(text) < 120 and tree.body:
            body = "\n".join(l.strip() for l in tree.body.text(strip=False).splitlines()
                             if l.strip())
            if len(body) > len(text):
                text = body
        return "" if _is_junk_body(text) else text[:max_chars]
    except Exception:
        import re

        text = _strip_style_script(html)
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        return "" if _is_junk_body(text) else text[:max_chars]


def _paragraph_text(tree, min_chars: int = 10) -> str:
    """拼接 <p> 段落文本（合并空白），过滤过短片段（导航/按钮等）。"""
    out = []
    for p in tree.css("p"):
        t = " ".join((p.text(deep=True, strip=True) or "").split())
        if len(t) >= min_chars:
            out.append(t)
    return "\n".join(out)


def _strip_style_script(html: str) -> str:
    """正则整段剔除 <style>/<script>/<noscript>（含其中的文本内容）。"""
    import re

    for tag in ("style", "script", "noscript"):
        html = re.sub(rf"<{tag}\b[^>]*>.*?</{tag}>", " ", html,
                      flags=re.IGNORECASE | re.DOTALL)
    return html


def _is_junk_body(text: str) -> bool:
    """疑似 CSS/JS 噪音（无实际正文）→ 不可用，返回空串降级用 summary。"""
    sample = (text or "")[:2000]
    if not sample.strip():
        return True
    if sample.count("{") + sample.count("}") >= 5:
        return True
    return "@media" in sample or "!important" in sample



def _clean_html(text: str) -> str:
    try:
        from selectolax.parser import HTMLParser

        return HTMLParser(text).text(strip=True)
    except ImportError:
        import re

        return re.sub(r"<[^>]+>", "", text).strip()


def _parse_date(entry):
    from calendar import timegm
    from datetime import datetime, timezone

    if hasattr(entry, "published_parsed") and entry.published_parsed:
        return datetime.fromtimestamp(timegm(entry.published_parsed), tz=timezone.utc)
    if hasattr(entry, "updated_parsed") and entry.updated_parsed:
        return datetime.fromtimestamp(timegm(entry.updated_parsed), tz=timezone.utc)
    return None


def _now_minus(days: int):
    from datetime import datetime, timedelta, timezone

    return datetime.now(timezone.utc) - timedelta(days=days)