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
    # 官方结构化检索接口（无 RSS、栏目页又 JS 渲染）：走 JSON 取数分支
    if source.get("type") == "json_api":
        return _fetch_json_api(source)

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


def _json_published(row: dict, field: str):
    """解析 json_api 条目的发布时间；支持毫秒时间戳（gov.cn 的 pubtime）。

    返回 tz-aware datetime 或 None（无该字段/格式不认识）。
    """
    from datetime import datetime, timezone

    raw = row.get(field)
    if raw in (None, "", 0, "0"):
        return None
    try:
        ts = float(raw)
    except (TypeError, ValueError):
        return None
    if ts > 1e11:          # 毫秒时间戳
        ts /= 1000.0
    try:
        return datetime.fromtimestamp(ts, tz=timezone.utc)
    except (OSError, OverflowError, ValueError):
        return None


def _json_rows(payload, path: str) -> list[dict]:
    """按点号路径从响应体里取出条目列表（如 searchVO.listVO）。"""
    node = payload
    for key in path.split("."):
        node = node.get(key) if isinstance(node, dict) else None
        if node is None:
            return []
    return node if isinstance(node, list) else []


def _fetch_json_api(source: dict) -> list[dict]:
    """从官方结构化检索接口取条目（无 RSS、栏目页又 JS 渲染时的第三条通道）。

    2026-11-03 新增。首个接入方是中国政府网「政策文件库」检索接口
    （t=zhengcelibrary_gw 国务院文件 / zhengcelibrary_bm 部门文件），返回
    ``{"searchVO": {"listVO": [{title, url, summary, pubtime(ms), pcode, puborg}]}}``。

    为什么需要这条通道：部委官网栏目页实测普遍不可抓（2026-11-03 复测：商务部
    404、金融监管总局/人社部/工信部 空壳页、住建部 403、全国人大 454B），而这个
    接口一次覆盖国务院 + 各组成部门的全国性制度文件（部门库 12815 条）。

    **发布机构与文号并入摘要**：实测部门文件的标题/正文开头常只有文号、没有部委
    简称（如「关于印发《城乡义务教育补助经费管理办法》的通知」实际发文单位是
    财政部+教育部，只存在于 ``puborg`` 字段），不并入就会被选题范围（scope 依赖
    标题+摘要判断发布主体）误判为「非国家级发布」而整条丢弃。

    配置字段（除 url 外均可选，默认按 gov.cn 结构）：
      items_path / title_field / url_field / summary_field /
      published_ms_field / docno_field / puborg_field /
      headers / max_age_days / max_items / fetch_body
    """
    headers = {"User-Agent": "Mozilla/5.0"}
    headers.update(source.get("headers") or {})
    resp = httpx.get(source["url"], timeout=20, follow_redirects=True,
                     headers=headers)
    resp.raise_for_status()

    rows = _json_rows(resp.json(),
                      str(source.get("items_path") or "searchVO.listVO"))
    if not rows:
        logger.warning("json_api 源 %s 未取到条目（接口结构可能变化）",
                       source.get("id"))
        return []

    t_f = source.get("title_field", "title")
    u_f = source.get("url_field", "url")
    s_f = source.get("summary_field", "summary")
    p_f = source.get("published_ms_field", "pubtime")
    d_f = source.get("docno_field", "pcode")
    o_f = source.get("puborg_field", "puborg")

    cutoff = _now_minus(int(source.get("max_age_days", MAX_AGE_DAYS)))
    out: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        title = " ".join(str(row.get(t_f) or "").split())
        if len(title) < 8:              # 导航/按钮类残条
            continue
        published = _json_published(row, p_f)
        if published and published < cutoff:
            continue

        summary = " ".join(str(row.get(s_f) or "").split())
        # 机构 + 文号（如「财政部 教育部｜财教〔2026〕195号」）：既是政策文件分量的
        # 强标识，也是 scope 判定发布主体的唯一依据（标题里往往没有部委名）。
        meta = [x for x in (str(row.get(o_f) or "").strip(),
                            str(row.get(d_f) or "").strip())
                if x and x not in summary]
        if meta:
            summary = "｜".join(meta + [summary]) if summary else "｜".join(meta)

        out.append({
            "title": title,
            "url": str(row.get(u_f) or "").strip(),
            "summary": summary,
            "body": "",
            "published": published.isoformat() if published else "",
            "source_id": source["id"],
            "source_name": source.get("name", source["id"]),
        })

    out.sort(key=lambda a: a["published"], reverse=True)
    items = out[: int(source.get("max_items", 10))]
    # 政策文件正文较长且是「措辞提取」的材料来源；默认抓正文，可在源上关掉。
    if source.get("fetch_body", True):
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


# ── 正文容器（2026-11-07 生产事故）──────────────────────────────────────
# 事故：中新网文章页把「阅读推荐 / 首页热榜」也写成 <p>，旧实现直接拼接**整页**
# 所有 <p>，于是 11-07 美国非农那条的 body 尾部混进几十条无关标题（C罗、敦煌、
# 「资本市场政策加力稳预期」…），污染了措辞提取与 LLM 解读（stage2 出现与两条
# 新闻都无关的「长期向好的基本面没有变」）。
# 正文在语义容器里（中新网是 div#left_zw），命中后只取容器内 <p>；选择器按
# 精确→通用排列，避免 `#content` 这类过宽选择器把侧栏一起圈进来。
_ARTICLE_SELECTORS = (
    "#left_zw", ".left_zw",                          # 中新网 / 中新社
    "#artibody", "#articleContent", "#article_content", "#zoom",
    "[itemprop=\"articleBody\"]",
    ".article-content", ".article_content", ".article-body", ".article_body",
    ".detail-content", ".news-content", ".TRS_Editor",
    ".content_maincontent_content", "article",
)

def _is_link_heading(p, text: str) -> bool:
    """推荐位/热榜标题：整段挂在 <a> 祖先里、且短。

    中新网这类页面的相关推荐是 `<a><div><p>标题</p></div></a>`，标题本身就是
    一个链接，长度 10~40 字；正文段落不会整体包在 <a> 里。
    **不用「有没有句末标点」判标题**：真实热榜里有「散瞳验光会伤孩子眼睛？」
    「…来一次敦煌！」这类带标点结尾的标题，靠标点反而会漏。
    """
    if len(text) > 60:
        return False
    node = p.parent
    for _ in range(4):
        if node is None:
            return False
        if node.tag == "a":
            return True
        node = node.parent
    return False


def _paragraph_texts(node, min_chars: int = 10) -> list[str]:
    """取节点内合格 <p> 文本：压空白、丢过短片段、丢推荐位标题。"""
    out = []
    for p in node.css("p"):
        t = " ".join((p.text(deep=True, strip=True) or "").split())
        if len(t) >= min_chars and not _is_link_heading(p, t):
            out.append(t)
    return out


def _scoped_paragraphs(tree) -> list[str] | None:
    """优先返回正文容器内的 <p>；都未命中返回 None（由调用方退回全页）。"""
    for sel in _ARTICLE_SELECTORS:
        for node in tree.css(sel):
            texts = _paragraph_texts(node)
            if len(texts) >= 2 and sum(len(t) for t in texts) >= 120:
                return texts
    return None


def _extract_body(html: str, max_chars: int = 3000) -> str:
    """用 selectolax 提取正文纯文本（优先 <p> 段落，剔除 style/script/nav 噪音）。

    2026-10-13：修复原实现——它在循环里对**每次新建的解析器**做 decompose，
    真正用于取正文的 tree 从未被清理，导致 <style> 里的 CSS/脚本原文原样泄漏
    进正文（中新网-滚动等源曾整篇正文都是 CSS）。现改为单解析器复用；并改为
    **优先拼接 <p> 段落**——纯 body 文本会先取到站点导航菜单/版权行等链接型
    chrome，而正文段落几乎都在 <p> 里；<p> 太少再退回整个 body 文本。数据缺失
    或疑似 CSS/JS 垃圾则返回空串（降级用 summary），避免把噪音喂给 LLM 造成误判。

    2026-11-07：再加两层——① 命中正文容器时只取容器内 <p>，不再看侧栏热榜；
    ② 无论哪条路径都剔除「挂在 <a> 里、短、无句末标点」的推荐位标题。定位到
    容器后**不启用**整页 body 兜底，否则又会把侧栏带回来。
    """
    try:
        from selectolax.parser import HTMLParser

        tree = HTMLParser(html)
        for sel in ("script", "style", "noscript", "nav", "header", "footer",
                    "aside"):
            for node in tree.css(sel):
                node.decompose()
        scoped = _scoped_paragraphs(tree)
        if scoped is not None:
            text = "\n".join(scoped)
        else:
            texts = _paragraph_texts(tree)
            if texts:
                # 哪怕只有一段短正文也不再退回整页文本：那个 fallback 会把侧栏
                # 热榜重新带回来（2026-11-07）。宁可短，也不要喂给 LLM 噪音。
                text = "\n".join(texts)
            elif tree.body:
                # 站点正文完全不在 <p> 里（如纯 <div> 稿件）才用整页文本。
                text = "\n".join(l.strip() for l in tree.body.text(strip=False).splitlines()
                                 if l.strip())
            else:
                text = ""
        return "" if _is_junk_body(text) else text[:max_chars]
    except Exception:
        import re

        text = _strip_style_script(html)
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        return "" if _is_junk_body(text) else text[:max_chars]


def _paragraph_text(tree, min_chars: int = 10) -> str:
    """拼接 <p> 段落文本（合并空白），过滤过短片段与推荐位标题。"""
    return "\n".join(_paragraph_texts(tree, min_chars))


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