"""plugin.py —— 每日新闻解读插件主入口。

流程：LLM 可用性探针 → 抓取当日新闻 → 4 阶段分析（黑话解码 + 措辞对比）→
构建双群一致的 digest → 返回 Result 触发推送。LLM 不可用时写 status=skipped
并静默不推送（原因写入 status.json 供排查）。

CLI：
  news run            立即执行
  news probe          逐模型测可用性
  news archive ingest 灌入官方文本（如政治局公报全文）供措辞对比
  news archive list   列出已灌入的文本
"""

import logging
import re
from datetime import datetime
from pathlib import Path

from family_os.kernel.context import Context
from family_os.kernel.skill import Result, Skill

from . import analyzer, dedup, fetcher, storage

logger = logging.getLogger("family_os.news_digest.plugin")

LLM_PROBE_PROMPT = "reply with the single word ok"

# 会参与措辞对比的主题 key：覆盖 analyzer.MEETING_REGISTRY 全部归档 key，
# 另加 finance（财经类，仅经 CLI `news archive ingest` 手工灌入）。
# 当日运行若在某 key 命中归档则自动做同主题措辞对比。
COMPARE_TOPICS = ["finance",
                  *[key for _p, key, _n in analyzer.MEETING_REGISTRY]]

# 正文行首的重复图例（LLM 按 prompt 给每行加标记/精选条目硬编码前缀）。
# 图例只保留在分节标题上（📰/📌/🔍/⚠️/💡），正文一律剔除避免分散注意力。
_ICON_PREFIXES = ("⚠️", "✓", "💡", "📝", "❗", "❌")

# 「注：…」说明行可能被模型包在这些前缀里（如「（注：」「**注：」「- 注：」），
# 判断泄漏前先剥离，避免漏网（2026-10-09）。
_NOTE_WRAPPERS = "（(【[「『*_#>-—· \t"

# stage2「无对比」空占位句的行首标记（宁缺毋滥，渲染层直接隐藏，2026-10-09）。
_STAGE2_EMPTY_PREFIXES = ("无对应历史提法", "无对应历史", "无延续项", "无历史可比",
                          "无对应延续", "历史无匹配", "无历史公报",
                          "无相关历史变化", "（无相关历史变化")
# stage2 三个类别标题（占位行被剔除后，无内容的孤儿标题一并隐藏）。
_STAGE2_HEADERS = ("新提法", "力度变化", "延续")

# 推送消息里的来源缩写（2026-10-27：微信端来源全称太长，只留简短标识）。
_SOURCE_SHORT = {
    "中国新闻网-财经": "中新网·财经",
    "中国新闻网-时政": "中新网·时政",
    "中国新闻网-国际": "中新网·国际",
    "中国新闻网": "中新网",
    "社科院经济研究所": "社科院·经济所",
    "社科院工业经济研究所": "社科院·工经所",
    "社科院数技经所": "社科院·数技经所",
    "英国广播公司中文网": "BBC中文",
    "人人都是产品经理": "人人产品",
    "品玩英文": "品玩",
    "雪球热门": "雪球",
    "CGTN-Business": "CGTN",
    "CNBC-Asia": "CNBC",
}


class NewsDigest(Skill):
    name = "news_digest"
    cost_tier = "low_freq_llm"

    def run(self, ctx: Context) -> Result:
        """执行每日新闻解读（探针失败则 skipped 静默不推送）。"""
        ok, reason = self._llm_available(ctx)
        if not ok:
            storage.write_status(ctx.config.app.data_dir, "skipped", reason)
            logger.warning("news_digest 跳过：LLM 不可用（%s）", reason)
            return Result(
                status="skipped",
                data={"mode": "skipped_llm", "reason": reason,
                      "llm_issue": reason, "_messages": {}},
            )
        try:
            return self._do_run(ctx)
        except Exception as e:
            logger.exception("news_digest run failed")
            return Result(status="error", error=str(e))

    def _llm_available(self, ctx: Context) -> tuple[bool, str]:
        try:
            # 沿模型链探测（主模型优先，失败自动降级），任一可用即视为可用
            analyzer._ask_with_fallback(ctx, LLM_PROBE_PROMPT)
            return True, ""
        except Exception as e:
            return False, self._classify_llm_error(e)

    @staticmethod
    def _classify_llm_error(e: Exception) -> str:
        name = type(e).__name__
        msg = str(e)
        if isinstance(e, RuntimeError) or "budget" in msg.lower():
            return "LLM 月度预算已用尽（budget cutoff）"
        if "Authentication" in name or "401" in msg:
            return "LLM API Key 无效或鉴权失败"
        if "InsufficientQuota" in name or "Quota" in name or "402" in msg or "insufficient balance" in msg.lower():
            return "LLM 账户余额/额度不足（欠费）"
        if ("APIConnection" in name or "Timeout" in name or "Connection" in name
                or "connect" in name.lower() or "timeout" in msg.lower()):
            return "LLM 网络连接/超时问题"
        return f"LLM 调用失败：{name} {msg[:60]}"

    def _do_run(self, ctx: Context) -> Result:
        data_dir = Path(ctx.config.app.data_dir)
        run_date = datetime.now().strftime("%Y-%m-%d")
        run_dir = storage.run_dir_for(data_dir, run_date)
        run_dir.mkdir(parents=True, exist_ok=True)

        # ── 抓取新闻 ──────────────────────────────────────────────────
        from family_os.kernel.ingestion import SourceRegistry
        registry = SourceRegistry(Path("family_os/config/sources.yaml"))
        articles = fetcher.fetch_articles(data_dir, registry)

        # ── ① 日期去重（跨天，代码粗筛） ─────────────────────────────
        # 剔除与近 window 天已处理标题【近乎完全相同】的重复投递。
        DEDUP_WINDOW_DAYS = 7  # 与快照保留一致
        seen_titles = storage.load_seen_titles(data_dir, DEDUP_WINDOW_DAYS)
        articles, new_titles = dedup.filter_duplicates(articles, seen_titles)
        if new_titles:
            storage.save_seen_titles(data_dir, run_date, new_titles)

        # ── ② 内容去重（当日，代码粗筛） ─────────────────────────────
        # 合并同一天内不同源对同一事件的重复报道，同一事件只留一条。
        articles = dedup.dedup_same_day(articles)

        # 近期已推送主题（title/topic_key/summary）：供 LLM 语义去重阶段使用
        recent_topics = storage.load_recent_topics(data_dir, DEDUP_WINDOW_DAYS)

        storage.save_raw_articles(run_dir, articles)

        # ── 措辞对比库：命中主题则取最近两次作对比 ───────────────────
        meeting_archive = {}
        for topic in COMPARE_TOPICS:
            items = storage.get_recent_meeting_texts(data_dir, topic, 2)
            if items:
                meeting_archive[topic] = items

        # ── ③ LLM 语义去重 → ④ 筛选 → 4 阶段（见 analyzer.run_pipeline） ──
        result = analyzer.run_pipeline(ctx, articles, meeting_archive, run_dir,
                                       recent_topics)
        # 记录当日实际精选主题（含 topic_key/摘要），供后续日期 LLM 语义去重
        storage.save_selected_topics(data_dir, run_date,
                                     result.get("selected") or [])

        # ── 组装 digest（双群一致） ───────────────────────────────────
        digest = self._assemble_digest(run_date, articles, result)
        storage.save_output(run_dir, "digest", digest)
        # 2026-10-01：结构化副本供 web 数据健康/新闻解读页渲染（免解析 markdown）
        storage.save_json(run_dir, "digest",
                          self._build_digest_json(run_date, articles, result))

        # 2026-10-10：宁缺毋滥——当日无合格信息（LLM 空选）时跳过当日推送
        # （不推占位消息）。status="skipped" 让 Notifier 保持静默，原因写入
        # status.json 由健康周报汇报（status="no_news" 与 LLM 故障区分）。
        if not result.get("selected"):
            storage.write_status(data_dir, "no_news",
                                 "今日无合格新闻（宁缺毋滥），已跳过当日推送")
            logger.info("news_digest 今日无合格新闻 → 跳过推送")
            return Result(
                status="skipped",
                data={"mode": "no_news", "date": run_date,
                      "total_fetched": len(articles),
                      "digest": digest, "_messages": {}},
            )

        storage.write_status(data_dir, "ok")
        storage.cleanup(data_dir)

        return Result(
            status="ok",
            data={
                "date": run_date,
                "total_fetched": len(articles),
                "digest": digest,
                "_messages": {"family": digest, "invest": digest},  # 两群一致
            },
            suggestions=[{
                "title": f"每日新闻解读 {len(articles)} 条",
                "priority": "medium",
                "detail": result["stage3"][:180] or "今日无可用解读",
            }],
            todos=[],
        )

    def _assemble_digest(self, run_date: str, articles: list[dict],
                         result: dict) -> str:
        selected = result.get("selected") or []
        lines = [
            f"📰 每日新闻解读 · {run_date}",
            f"今日抓取 {len(articles)} 条 · 精选 {len(selected)} 条\n---",
        ]
        # 2026-09-28：先列「今日要闻」（宁缺毋滥，≤3 条精选：来源+可点开
        # 标题+丰富摘要+重要性理由），再进入措辞解读/同主题措辞变化。
        # 2026-10-10：移除「最新 N 条」凑数兜底——无合格信息时只留一行说明
        # （该说明仅落盘供 web 查看，当日不再推送）。
        if selected:
            lines.append("### 📌 今日要闻\n" + self._format_selected(selected))
        else:
            note = result.get("selected_note") or ""
            if note:
                lines.append(f"今日无重要新闻（宁缺毋滥）：{note}")
            else:
                lines.append("今日无重要新闻（宁缺毋滥，今日不推送详细解读）。")
        if result.get("market_impact", "").strip():
            mi = self._strip_icons(self._strip_notes(result["market_impact"]))
            if mi.strip():
                lines.append("\n### 📈 利率变化对黄金与股市的影响\n" + mi.strip())
        if result.get("stage1", "").strip():
            stage1 = self._strip_icons(self._strip_notes(result["stage1"]))
            if stage1.strip():
                lines.append("\n### 🔍 官方措辞解读\n" + stage1.strip())
        stage2 = self._strip_icons(self._strip_notes(result.get("stage2", "")))
        stage2 = self._drop_stage2_placeholders(stage2)
        if stage2.strip() and not stage2.startswith("（无历史"):
            # 2026-09-29：合并连续重复行（LLM 偶发按项重复输出占位/新提法）
            lines.append("\n### ⚠️ 历史措辞变化（同主题对比）\n"
                         + self._dedup_lines(stage2))
        if result.get("stage3", "").strip():
            # 2026-09-29：只保留第一条 💡 建议，确保「一句话建议」只有一条
            lines.append("\n### 💡 老百姓一句话建议\n"
                         + self._first_advice(result["stage3"]))
        return "\n".join(lines)

    @staticmethod
    def _dedup_lines(text: str) -> str:
        """合并连续重复行，去除 LLM 偶发按每项重复输出的占位行。"""
        out, last = [], ""
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            if stripped != last:
                out.append(stripped)
                last = stripped
        return "\n".join(out)

    @staticmethod
    def _strip_notes(text: str) -> str:
        """剔除 LLM 泄漏的「注：…」说明行（模型偶发把任务说明回显进正文）。

        2026-10-01：stage2 无措辞时不调 LLM（analyzer 层已兜住），这里做
        渲染层保险——没有的内容不输出（宁缺毋滥）。
        2026-10-09：模型常把注包在括号/加粗/列表符里（如「（注：历史公报未
        涉及…）」），故先剥离行首包裹符再判断，避免以「（注：」开头时漏网。
        """
        return "\n".join(
            l for l in (text or "").splitlines()
            if l.strip() and not l.strip().lstrip(_NOTE_WRAPPERS).startswith(("注：", "注:")))

    @staticmethod
    def _drop_stage2_placeholders(text: str) -> str:
        """隐藏 stage2 中「无对应历史提法/无延续项…」等空占位句（宁缺毋滥）。

        2026-10-09：模型在无历史可比时仍输出整段"无对应历史提法，力度分级
        无法直接对比"占位；剔除这些空话后，**后面没有内容的孤儿类别标题
        （新提法/力度变化/延续）也一并隐藏**，避免只剩裸标题。
        """
        entries = []   # ("header"|"item", text)
        for raw in (text or "").splitlines():
            s = raw.strip()
            if not s:
                continue
            probe = re.sub(r"^[0-9]+[.、)）]\s*", "", s)   # 去掉 "1. " 序号前缀
            if probe.startswith(_STAGE2_EMPTY_PREFIXES) or "无法直接对比" in probe:
                continue    # 空占位句 → 剔除
            if s in _STAGE2_HEADERS or s in (f"{h}：" for h in _STAGE2_HEADERS) \
                    or s in (f"{h}:" for h in _STAGE2_HEADERS):
                entries.append(("header", s))   # 裸类别标题（无内联内容）
            else:
                entries.append(("item", s))
        out, pending = [], None
        for kind, s in entries:
            if kind == "header":
                pending = s          # 暂存：等有内容行再落笔
                continue
            if pending is not None:
                out.append(pending)
                pending = None
            out.append(s)
        return "\n".join(out)        # 结尾悬挂的孤儿标题（pending）直接丢弃

    @staticmethod
    def _strip_icons(text: str) -> str:
        """剔除行首重复图例（⚠️/✓/💡/📝…），图例只保留在分节标题上。"""
        lines = []
        for raw in (text or "").splitlines():
            l = raw.strip()
            while any(l.startswith(ic) for ic in _ICON_PREFIXES):
                for ic in _ICON_PREFIXES:
                    if l.startswith(ic):
                        l = l[len(ic):].strip()
                        break
            if l:
                lines.append(l)
        return "\n".join(lines)

    @staticmethod
    def _first_advice(text: str) -> str:
        """只保留第一条 💡 建议（LLM 偶发输出多条），并剔除行首图例。"""
        lines = [l.strip() for l in text.splitlines() if l.strip()]
        for line in lines:
            if "💡" in line:
                return NewsDigest._strip_icons(line)
        return NewsDigest._strip_icons(lines[0]) if lines else ""

    @staticmethod
    def _build_digest_json(run_date: str, articles: list[dict],
                           result: dict) -> dict:
        """结构化 digest（web 新闻解读页直接消费，字段与推送 markdown 同源）。"""
        selected = result.get("selected") or []
        items = []
        for it in selected:
            title = (it.get("title") or "").strip()
            if not title:
                continue
            items.append({
                "source": (it.get("source_name") or "").strip(),
                "title": title,
                "url": (it.get("url") or "").strip(),
                "summary": " ".join((it.get("summary") or "").split()),
                "reason": " ".join((it.get("reason") or "").split()),
            })
        return {
            "date": run_date,
            "total_fetched": len(articles),
            "selected_count": len(items),
            "select_mode": result.get("select_mode", "llm"),
            "empty_note": (result.get("selected_note") or "").strip()
            if result.get("select_mode") == "empty" else "",
            "selected": items,
            "stage1": NewsDigest._strip_icons(
                NewsDigest._strip_notes(result.get("stage1") or "")),
            "market_impact": NewsDigest._strip_icons(
                NewsDigest._strip_notes(result.get("market_impact") or "")),
            "stage2": NewsDigest._drop_stage2_placeholders(
                NewsDigest._strip_icons(
                    NewsDigest._strip_notes(result.get("stage2") or ""))),
            "stage3": NewsDigest._strip_icons(
                NewsDigest._strip_notes(result.get("stage3") or "")),
        }

    @staticmethod
    def _format_selected(selected: list[dict]) -> str:
        """渲染精选要闻：可点开的标题（主角）+ 来源缩写 + 摘要 + 为什么重要。

        2026-10-27：微信端「[来源] [标题](链接)」会被 Markdown 误判为引用式
        链接，导致标题丢失、裸 URL 暴露。改为标题单独成链接，来源另起一行缩写。
        """
        items = []
        for i, it in enumerate(selected, 1):
            title = (it.get("title") or "").strip()
            if not title:
                continue
            src = NewsDigest._short_source(it.get("source_name") or "")
            url = (it.get("url") or "").strip()
            summary = " ".join((it.get("summary") or "").split())
            reason = " ".join((it.get("reason") or "").split())
            if len(summary) > 120:
                summary = summary[:120] + "…"
            if len(reason) > 60:
                reason = reason[:60] + "…"
            title = title if len(title) <= 40 else title[:40] + "…"

            head = f"**[{title}]({url})**" if url else f"**{title}**"
            block = f"{i}. {head}"
            if src:
                block += f"\n   来源：{src}"
            if summary:
                block += f"\n   {summary}"
            if reason:
                block += f"\n   为什么重要：{reason}"
            items.append(block)
        return "\n\n".join(items)

    @staticmethod
    def _short_source(name: str) -> str:
        """来源缩写：已知来源查表，未知来源去掉泛化后缀并限制长度。"""
        s = (name or "").strip()
        if not s:
            return ""
        if s in _SOURCE_SHORT:
            return _SOURCE_SHORT[s]
        s = s.replace("中国社会科学院", "社科院")
        for suffix in ("中文网", "新闻网", "新闻", "网站", "网"):
            if s.endswith(suffix) and len(s) > len(suffix) + 2:
                s = s[: -len(suffix)]
                break
        return s if len(s) <= 8 else s[:8] + "…"

    # ── CLI ────────────────────────────────────────────────────────────

    def register_cli(self, app) -> None:
        import typer
        from family_os.kernel.config import Config
        from family_os.kernel.llm_gateway import LLMGateway
        from family_os.kernel.store import Store

        news_cli = typer.Typer(help="每日新闻解读", name="news")

        def _build_ctx() -> Context:
            config = Config.load("family_os/config/config.yaml")
            db_path = Path(config.app.data_dir) / "family.db"
            store = Store(db_path)
            store.initialize()
            gateway = LLMGateway(store=store, config=config)
            return Context(store=store, config=config, llm_gateway=gateway)

        @news_cli.command(name="run")
        def news_run():
            """立即执行每日新闻解读。"""
            import typer as t
            result = self.run(_build_ctx())
            t.echo(f"status={result.status}")
            if result.status == "ok":
                t.echo(result.data.get("digest", ""))

        @news_cli.command(name="probe")
        def news_probe():
            """逐模型测连通性。"""
            import typer as t
            for item in analyzer.probe(_build_ctx()):
                mark = "✅" if item["ok"] else "❌"
                t.echo(f"{mark} {item['model']}" + (f"  {item['reason']}" if not item["ok"] else ""))

        archive_cli = typer.Typer(help="措辞对比库管理", name="archive")

        @archive_cli.command(name="ingest")
        def archive_ingest(key: str, file: str = typer.Argument(...)):
            """灌入官方文本（key+date 从文件名/参数）。示例: news archive ingest --key zzjj file.md"""
            from family_os.kernel.ingestion import SourceRegistry  # noqa: F401
            import re
            p = Path(file)
            text = p.read_text(encoding="utf-8")
            m = re.search(r"(\d{4}-\d{2}-\d{2})", p.name)
            date = m.group(1) if m else datetime.now().strftime("%Y-%m-%d")
            ctx = _build_ctx()
            dest = storage.ingest_meeting_text(ctx.config.app.data_dir, key, date, text)
            typer.echo(f"灌入成功: {dest}")

        @archive_cli.command(name="list")
        def archive_list(key: str):
            """列出某主题已灌入的历次文本。"""
            ctx = _build_ctx()
            items = storage.list_meeting_texts(ctx.config.app.data_dir, key)
            if not items:
                typer.echo(f"{key}: 无记录（目录为空或不存在）")
                return
            for it in items:
                typer.echo(f"  {it['date']}  {it['file']}")

        news_cli.add_typer(archive_cli)
        app.add_typer(news_cli)