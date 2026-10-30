"""Tests for the news_digest plugin.

Covers:
- storage: meeting_archive 落盘/读取/对比、status 写入
- analyzer: fallback 在 mock 失败时切换模型
- plugin: LLM unavailable → status=skipped 且双群空消息；主流程双群消息一致
"""

import json
from pathlib import Path

import pytest


def _d(days_ago: int = 0) -> str:
    """相对今天的日期（YYYY-MM-DD）——避免硬编码日期随窗口滚动而失效。"""
    from datetime import datetime, timedelta

    return (datetime.now() - timedelta(days=days_ago)).strftime("%Y-%m-%d")


def _mk_ctx(tmp_path, gw, extra_llm=None):
    from family_os.kernel.config import (
        AppConfig, ApprovalConfig, ChannelConfig, Config, DigestConfig,
        LLMConfig, NotifierConfig, PluginConfig, SchedulerConfig, Secrets,
    )
    from family_os.kernel.context import Context
    from family_os.kernel.store import Store

    store = Store(tmp_path / "test.db")
    store.initialize()
    llm_kwargs = {
        "news_digest_model": "Qwen/Qwen3-8B",
        "news_digest_fallback_models": ["Qwen/Qwen3.5-4B",
                                        "deepseek-ai/DeepSeek-R1-0528-Qwen3-8B"],
    }
    if extra_llm:
        llm_kwargs.update(extra_llm)
    config = Config(
        app=AppConfig(data_dir=str(tmp_path)),
        llm=LLMConfig(**llm_kwargs),
        plugins=PluginConfig(),
        scheduler=SchedulerConfig(),
        digest=DigestConfig(),
        approval=ApprovalConfig(),
        notifier=NotifierConfig(channels=[ChannelConfig(type="file", enabled=True)]),
        secrets=Secrets(),
    )
    return Context(store=store, config=config, llm_gateway=gw)


class TestMeetingArchive:
    def test_ingest_and_list(self, tmp_path):
        from family_os.plugins.news_digest import storage

        data_dir = tmp_path
        storage.ingest_meeting_text(data_dir, "zzjj", "2026-09-26", "公报A")
        dest = storage.ingest_meeting_text(data_dir, "zzjj", "2026-07-15", "公报B")
        assert dest.exists()

        items = storage.list_meeting_texts(data_dir, "zzjj")
        assert [i["date"] for i in items] == ["2026-09-26", "2026-07-15"]

        recent = storage.get_recent_meeting_texts(data_dir, "zzjj", 2)
        assert len(recent) == 2
        assert recent[0]["date"] == "2026-09-26"
        assert "公报A" in recent[0]["text"]

    def test_ingest_dedup_same_key_date(self, tmp_path):
        from family_os.plugins.news_digest import storage

        data_dir = tmp_path
        storage.ingest_meeting_text(data_dir, "zzjj", "2026-09-26", "v1")
        storage.ingest_meeting_text(data_dir, "zzjj", "2026-09-26", "v2")
        items = storage.list_meeting_texts(data_dir, "zzjj")
        assert len(items) == 1, "同一 key+date 不应重复备份"

    def test_get_recent_empty(self, tmp_path):
        from family_os.plugins.news_digest import storage

        assert storage.get_recent_meeting_texts(tmp_path, "zzjj", 2) == []

    def test_cleanup_keeps_archive(self, tmp_path):
        from family_os.plugins.news_digest import storage

        data_dir = tmp_path
        # 造几个老快照 + 一个档案
        for d in ["2026-09-25", "2026-09-24", "2026-09-23"]:
            (storage.run_dir_for(data_dir, d)).mkdir(parents=True)
            (storage.run_dir_for(data_dir, d) / "digest.md").write_text("x", encoding="utf-8")
        storage.ingest_meeting_text(data_dir, "zzjj", "2026-01-01", "旧公报")

        storage.cleanup(data_dir, keep_runs=1)
        assert (storage.archive_dir(data_dir) / "zzjj" / "2026-01-01.md").exists(), \
            "meeting_archive 永不被 cleanup 清理"
        assert not (storage.run_dir_for(data_dir, "2026-09-23")).exists(), "老快照被轮换"


class TestStatusWrite:
    def test_write_status(self, tmp_path):
        from family_os.plugins.news_digest import storage

        storage.write_status(tmp_path, "skipped", "网络问题")
        st = json.loads((tmp_path / "history" / "news_digest" / "status.json")
                        .read_text(encoding="utf-8"))
        assert st["status"] == "skipped"
        assert st["reason"]


class TestDigestJson:
    def test_build_digest_json_structure(self):
        """结构化 digest.json：精选条目字段与推送 markdown 同源（供 web 渲染）。"""
        from family_os.plugins.news_digest.plugin import NewsDigest

        d = NewsDigest._build_digest_json(
            "2026-10-01",
            [{"title": "a"}, {"title": "b"}],
            {"selected": [{"source_name": "中新网", "title": "买房国补上线",
                           "url": "http://x", "summary": "摘要内容",
                           "reason": "重要理由"}],
             "select_mode": "llm",
             "stage1": "官方解读", "stage2": "措辞变化", "stage3": "一句话建议"})
        assert d["date"] == "2026-10-01"
        assert d["total_fetched"] == 2 and d["selected_count"] == 1
        assert d["selected"][0] == {
            "source": "中新网", "title": "买房国补上线", "url": "http://x",
            "summary": "摘要内容", "reason": "重要理由"}
        assert d["stage3"] == "一句话建议"

    def test_build_digest_json_empty_selected_keeps_note(self):
        """精选为空时保留宁缺毋滥原因，供 web 显示。"""
        from family_os.plugins.news_digest.plugin import NewsDigest

        d = NewsDigest._build_digest_json("2026-10-01", [{"title": "a"}], {
            "select_mode": "empty", "selected_note": "均与近期重复",
            "selected": []})
        assert d["selected"] == [] and d["empty_note"] == "均与近期重复"

    def test_build_digest_json_strips_icons_from_stages(self):
        """digest.json 各 stage 与 markdown 同源：行首图例剔除（供 web 渲染一致）。"""
        from family_os.plugins.news_digest.plugin import NewsDigest

        d = NewsDigest._build_digest_json("2026-10-01", [{"title": "a"}], {
            "select_mode": "llm", "selected": [],
            "stage1": "解读：积极财政\n⚠️ 力度变化：加码",
            "stage2": "⚠️ 新提法：X\n✓ 延续：Y",
            "stage3": "💡建议一句话"})
        assert d["stage1"] == "解读：积极财政\n力度变化：加码"
        assert d["stage2"] == "新提法：X\n延续：Y"
        assert d["stage3"] == "建议一句话"
        assert "⚠️" not in d["stage2"] and "💡" not in d["stage3"]

    def test_strip_icons_removes_leading_icons_only(self):
        """_strip_icons 只剔除行首图例，不影响正文与标题图标。"""
        from family_os.plugins.news_digest.plugin import NewsDigest

        text = "⚠️ 新提法：X\n💡合理规划消费\n普通行 ✅ 仍在\n\n\n"
        out = NewsDigest._strip_icons(text)
        assert out == "新提法：X\n合理规划消费\n普通行 ✅ 仍在"


class TestLLMLeakGuards:
    """2026-10-09：修三类已发送内容里的质量问题（注泄漏/空占位/无措辞填充段）。"""

    def test_strip_notes_drops_wrapped_note_lines(self):
        """①「注：…」被括号/加粗/列表符包裹时也要剔除（此前以「（注：」开头漏网）。"""
        from family_os.plugins.news_digest.plugin import NewsDigest

        text = ("（注：历史公报未涉及医保资金等主题，故未设延续项）\n"
                "**注：**以下为模型说明\n"
                "- 注: 同上\n"
                "注：普通注行\n"
                "监控漏洞需警惕\n"          # 正文含"注"字但不是注行 → 保留
                "注意防范风险\n")           # 以"注"开头但非"注："→ 保留
        out = NewsDigest._strip_notes(text)
        assert "历史公报未涉及" not in out
        assert "以下为模型说明" not in out
        assert "同上" not in out
        assert out.splitlines() == ["监控漏洞需警惕", "注意防范风险"]

    def test_drop_stage2_placeholders(self):
        """②「无对应历史提法/无延续项」空占位句渲染层隐藏。"""
        from family_os.plugins.news_digest.plugin import NewsDigest

        text = ("新提法\n"
                '1. "精准滴灌" → 首次提出定向调控新路径\n'
                "力度变化\n"
                "1. 无对应历史提法，力度分级无法直接对比\n"
                "2. 无对应历史提法，力度分级无法直接对比\n"
                "延续\n"
                "1. 无延续项（今日主题均首次出现，历史无匹配）")
        out = NewsDigest._drop_stage2_placeholders(text)
        assert "无对应历史提法" not in out
        assert "无延续项" not in out
        assert "精准滴灌" in out

    def test_assemble_digest_omits_stage2_when_all_placeholders(self):
        """stage2 全为空占位时整段"历史措辞变化"不出现（宁缺毋滥）。"""
        from family_os.plugins.news_digest import plugin

        result = {
            "selected": [{"source_name": "中新网", "title": "T", "url": "u",
                          "summary": "s", "reason": "r"}],
            "select_mode": "llm", "stage1": "", "stage3": "",
            "stage2": ("力度变化\n1. 无对应历史提法，力度分级无法直接对比\n"
                       "延续\n1. 无延续项（今日主题均首次出现，历史无匹配）"),
        }
        digest = plugin.NewsDigest()._assemble_digest("2026-10-09", [{"title": "t"}], result)
        assert "历史措辞变化" not in digest
        assert "无对应历史提法" not in digest

    def test_stage1_skipped_when_no_official_wording(self, monkeypatch):
        """③ stage0 为空占位时 stage1 不调 LLM（此前会回显"未见新提法"填充段）。"""
        from family_os.plugins.news_digest import analyzer

        called = {"n": 0}

        def _spy(ctx, prompt):
            called["n"] += 1
            return "泛化填充段"

        monkeypatch.setattr(analyzer, "_ask_with_fallback", _spy)
        placeholder = "（今日未提取到明确的官方特征措辞：或新闻无显著政策表述，或提取失败）"
        assert analyzer._stage1_decode(None, "m", placeholder) == ""
        assert called["n"] == 0

    def test_stage1_called_when_wording_present(self, monkeypatch):
        """stage0 有真实措辞时仍照常解读。"""
        from family_os.plugins.news_digest import analyzer

        monkeypatch.setattr(analyzer, "_ask_with_fallback",
                            lambda ctx, prompt: "稳增长→托底经济")
        stage0 = '[{"key_phrases": ["稳增长"]}]'
        assert analyzer._stage1_decode(None, "m", stage0) == "稳增长→托底经济"

    def test_build_digest_json_drops_stage2_placeholders(self):
        """digest.json 的 stage2 与 markdown 同源：空占位句一并隐藏（web 一致）。"""
        from family_os.plugins.news_digest.plugin import NewsDigest

        d = NewsDigest._build_digest_json("2026-10-09", [{"title": "a"}], {
            "select_mode": "llm", "selected": [],
            "stage1": "（注：泄漏行）\n正常解读",
            "stage2": "新提法\n1. X\n力度变化\n1. 无对应历史提法，无法直接对比"})
        assert d["stage1"] == "正常解读"
        assert "无对应历史提法" not in d["stage2"]
        assert "新提法" in d["stage2"]


class TestLLMSkip:
    def test_skip_when_llm_network_error(self, tmp_path):
        from family_os.plugins.news_digest.plugin import NewsDigest

        class APIConnectionError(Exception):
            pass

        class NetGW:
            def ask(self, prompt, model_tier="default", plugin_name="unknown", **kw):
                raise APIConnectionError("connect timeout")

        ctx = _mk_ctx(tmp_path, NetGW())
        res = NewsDigest().run(ctx)
        assert res.status == "skipped"
        assert res.data.get("_messages") == {}
        assert "网络" in res.data["reason"]

    def test_skip_when_llm_budget(self, tmp_path):
        from family_os.plugins.news_digest.plugin import NewsDigest

        class BudgetGW:
            def ask(self, prompt, model_tier="default", plugin_name="unknown", **kw):
                raise RuntimeError("Monthly budget $10.00 exceeded")

        res = NewsDigest().run(_mk_ctx(tmp_path, BudgetGW()))
        assert res.status == "skipped"
        assert "预算" in res.data["reason"]


class TestAnalyzerFallback:
    def test_fallback_switches_model_on_failure(self, tmp_path):
        from family_os.plugins.news_digest import analyzer

        calls = []

        class FlakyGateway:
            def ask(self, prompt, model_tier="default", plugin_name="unknown", **kw):
                calls.append(model_tier)
                if model_tier == "Qwen/Qwen3-8B":
                    raise RuntimeError("main down")
                return "RESULT-" + model_tier

        ctx = _mk_ctx(tmp_path, FlakyGateway())
        out = analyzer._ask_with_fallback(ctx, "hi")
        assert out == "RESULT-Qwen/Qwen3.5-4B"
        assert calls == ["Qwen/Qwen3-8B", "Qwen/Qwen3.5-4B"]

    def test_fallback_raises_when_all_fail(self, tmp_path):
        from family_os.plugins.news_digest import analyzer

        class AllDown:
            def ask(self, prompt, model_tier="default", plugin_name="unknown", **kw):
                raise RuntimeError("down")

        with pytest.raises(RuntimeError):
            analyzer._ask_with_fallback(_mk_ctx(tmp_path, AllDown()), "hi")


class TestStage0Robustness:
    """stage0 提取健壮性：JSON 失败重试、非 JSON 兜底、空 items 走明确占位。"""

    def test_stage0_parse_valid(self):
        from family_os.plugins.news_digest.analyzer import _stage0_parse

        raw = '{"items":[{"thread":"财政","key_phrases":["加力扩大内需"]}]}'
        assert _stage0_parse(raw) == ["加力扩大内需"]

    def test_stage0_parse_empty_items_is_valid_empty(self):
        from family_os.plugins.news_digest.analyzer import _stage0_parse

        assert _stage0_parse('{"items":[]}') == []

    def test_stage0_parse_malformed_returns_none(self):
        from family_os.plugins.news_digest.analyzer import _stage0_parse

        assert _stage0_parse("我不是JSON") is None
        assert _stage0_parse('{"no_items": 1}') is None
        # 代码块包裹的有效 JSON 也能被截取出来
        assert _stage0_parse('```json\n{"items":[{"key_phrases":["X"]}]}\n```') == ["X"]

    def test_stage0_parse_truncated_json_recovers(self):
        """输出被截断（外层未闭合）时，仍能从完整 key_phrases 数组恢复措辞。"""
        from family_os.plugins.news_digest.analyzer import _stage0_parse

        truncated = (
            '{"items":[{"thread":"外贸","key_phrases":["监管互认新模式",'
            '"指定监管场地"],"source":"中国新闻网-财经","date":"2026-09-2'
        )
        assert _stage0_parse(truncated) == ["监管互认新模式", "指定监管场地"]

        # 多个 items 被截断，抓到最后一个完整数组即可
        multi = '{"items":[{"key_phrases":["A"]},{"key_phrases":["B","C"]},{"key_phrases":["D"'
        assert _stage0_parse(multi) == ["A", "B", "C"]

    def test_stage0_retries_on_malformed_then_succeeds(self, tmp_path):
        from family_os.plugins.news_digest import analyzer

        calls = {"n": 0}

        class Flaky:
            def ask(self, prompt, model_tier="default", plugin_name="unknown", **kw):
                calls["n"] += 1
                if calls["n"] == 1:
                    return "抱歉我输出了一段纯文本"
                return '{"items":[{"thread":"宏","key_phrases":["跨周期调节"]}]}'

        ctx = _mk_ctx(tmp_path, Flaky())
        out = analyzer._stage0_extract(ctx, "METHOD", [{"title": "x"}])
        assert calls["n"] == 2
        assert "跨周期调节" in out
        assert json.loads(out) == ["跨周期调节"]

    def test_stage0_placeholder_when_consistently_empty(self, tmp_path):
        from family_os.plugins.news_digest import analyzer

        class Empty:
            def ask(self, prompt, model_tier="default", plugin_name="unknown", **kw):
                return '{"items":[]}'

        ctx = _mk_ctx(tmp_path, Empty())
        out = analyzer._stage0_extract(ctx, "METHOD", [{"title": "x"}])
        assert "今日未提取到明确" in out, "空 items 应走占位而非返回 [] 触发 stage1 示例"

    def test_stage0_last_resort_model_on_empty_items(self, tmp_path):
        """链上两次都回空 items（GLM 限流常见）→ 用链末模型兜底，拿到措辞。"""
        from family_os.plugins.news_digest import analyzer

        calls = []

        class Gw:
            def ask(self, prompt, model_tier="default", plugin_name="unknown", **kw):
                calls.append(model_tier)
                if model_tier == "deepseek-ai/DeepSeek-R1-0528-Qwen3-8B":
                    return '{"items":[{"thread":"产业","key_phrases":["新质生产力"]}]}'
                return '{"items":[]}'

        ctx = _mk_ctx(tmp_path, Gw())
        out = analyzer._stage0_extract(ctx, "METHOD", [{"title": "x"}])
        assert "新质生产力" in out
        assert calls[-1] == "deepseek-ai/DeepSeek-R1-0528-Qwen3-8B"

    def test_stage0_placeholder_ignores_llm_freeform(self, tmp_path):
        from family_os.plugins.news_digest import analyzer

        class Freeform:
            def ask(self, prompt, model_tier="default", plugin_name="unknown", **kw):
                return "加力扩大内需很重要，建议大家关注消费和投资"

        ctx = _mk_ctx(tmp_path, Freeform())
        out = analyzer._stage0_extract(ctx, "METHOD", [{"title": "x"}])
        assert "今日未提取到明确" in out


class TestDualGroupMessage:
    def test_run_ok_sends_identical_group_messages(self, tmp_path, monkeypatch):
        from family_os.plugins.news_digest.plugin import NewsDigest

        class GoodGW:
            def ask(self, prompt, model_tier="default", plugin_name="unknown", **kw):
                # 精选 → stage0 → 其余阶段
                if "精选" in prompt:
                    return '{"items":[{"title":"国务院部署稳增长工作",' \
                           '"summary":"加力扩大内需，推动重大项目开工，政策落地提速、更积极。",' \
                           '"reason":"直接关系就业与消费。"}]}'
                if "输出 JSON" in prompt:
                    return '{"items":[{"thread":"财政","key_phrases":["加力扩大内需"]}]}'
                return "解读内容\n💡建议一句话"

        # 隔离 fetch（不联网），把 articles 打桩
        monkeypatch.setattr(
            "family_os.plugins.news_digest.plugin.fetcher",
            _FakeFetcher(),
        )

        ctx = _mk_ctx(tmp_path, GoodGW())
        # 预置历史公报，使 stage2「同主题措辞变化」走真实对比路径（而非无历史占位）
        from family_os.plugins.news_digest import storage
        storage.ingest_meeting_text(tmp_path, "zzjj", "2026-09-26", "历史公报文本")
        res = NewsDigest().run(ctx)
        assert res.status == "ok"
        msg = res.data.get("_messages", {})
        assert msg, "ok 时应构建双群消息"
        assert msg.get("family") == msg.get("invest"), "双群内容必须一致"
        assert "💡" in msg["family"]  # 标题「### 💡 老百姓一句话建议」保留图例
        assert "今日抓取 1 条 · 精选 1 条" in msg["family"]
        assert "📌 今日要闻" in msg["family"], "应先列出今日要闻（摘要+出处）"
        assert "**[国务院部署稳增长工作](http://x.com/1)**" in msg["family"]
        assert "来源：test" in msg["family"]
        assert msg["family"].count("http://x.com/1") == 1, "URL 只出现在链接目标里，不裸显"
        assert "加力扩大内需，推动重大项目开工" in msg["family"], "应含更丰富的 AI 摘要"
        assert "为什么重要：直接关系就业与消费" in msg["family"]
        assert "⚠️ 历史措辞变化" in msg["family"], "措辞变化解析需强调（标题图例保留）"
        assert "📝 加力扩大内需" not in msg["family"], "正文行首图例应剔除"


class _FakeFetcher:
    def fetch_articles(self, data_dir, registry):
        return [
            {"title": "国务院部署稳增长工作", "url": "http://x.com/1",
             "summary": "加力扩大内需，推动条件成熟重大项目开工",
             "body": "", "published": "2026-09-27", "source_name": "test"},
        ]


class TestSelected:
    """今日要闻精选（宁缺毋滥 ≤3 条）：渲染 + 组装三种模式。"""

    def _mk_plugin(self):
        from family_os.plugins.news_digest.plugin import NewsDigest
        return NewsDigest()

    def test_selected_renders_link_summary_reason(self):
        selected = [{
            "title": "国务院部署稳增长工作", "url": "http://x.com/1",
            "source_name": "测试",
            "summary": "加力扩大内需，推动重大项目开工，政策落地提速、更积极。",
            "reason": "直接关系就业与消费",
        }]
        out = self._mk_plugin()._format_selected(selected)
        assert out.startswith("1. **[国务院部署稳增长工作](http://x.com/1)**\n   来源：测试")
        assert "加力扩大内需，推动重大项目开工" in out
        assert "为什么重要：直接关系就业与消费" in out
        # 图例只在标题出现：条目正文不再带 📝/💡 前缀
        assert "📝" not in out and "💡" not in out

    def test_short_source_abbreviation(self):
        """来源用简短缩写，不显示「中国新闻网-财经」这类长全称。"""
        plugin = self._mk_plugin()
        assert plugin._short_source("中国新闻网-财经") == "中新网·财经"
        assert plugin._short_source("社科院工业经济研究所") == "社科院·工经所"
        assert plugin._short_source("英国广播公司中文网") == "BBC中文"
        assert plugin._short_source("") == ""
        # 未知长名称：去泛化后缀 + 截断
        assert len(plugin._short_source("某某某某某某某某某某新闻网")) <= 9

    def test_selected_preserves_input_order(self):
        selected = [
            {"title": "A", "url": "http://x.com/a", "source_name": "源A",
             "summary": "sA", "reason": "rA"},
            {"title": "B", "url": "http://x.com/b", "source_name": "源B",
             "summary": "sB", "reason": "rB"},
        ]
        out = self._mk_plugin()._format_selected(selected)
        assert out.index("A") < out.index("B")

    def test_selected_truncate_title_summary_reason(self):
        selected = [{
            "title": "国务院部署稳增长工作以进一步推动经济高质量发展若干政策措施落地见效并明确部门责任分工与时间表",
            "url": "http://x.com/1", "source_name": "源",
            "summary": "加力扩大内需，" + "持续发力。" * 40,
            "reason": "因为这是关系到每个人收入就业消费投资楼市的重要政策信号，" + "重要。" * 20,
        }]
        out = self._mk_plugin()._format_selected(selected)
        assert out.startswith("1. **[国务院部署稳增长工作以进一步推动经济高质量发展")
        assert out.count("…") == 3

    def test_selected_fallbacks_without_url_or_summary(self):
        selected = [
            {"title": "标题甲", "url": "", "source_name": "源甲",
             "summary": "有摘要", "reason": ""},
            {"title": "标题乙", "url": "http://x.com/b", "source_name": "",
             "summary": "", "reason": ""},
            {"title": "  ", "url": "http://x.com/c", "source_name": "源",
             "summary": "s", "reason": "r"},
        ]
        out = self._mk_plugin()._format_selected(selected)
        assert "**标题甲**" in out and "来源：源甲" in out and "有摘要" in out
        assert "**[标题乙](http://x.com/b)**" in out
        assert "x.com/c" not in out

    def test_selected_empty_returns_empty(self):
        assert self._mk_plugin()._format_selected([]) == ""

    def test_digest_llm_mode_order_and_emphasis(self):
        plugin = self._mk_plugin()
        result = {
            "selected": [{
                "title": "国务院部署稳增长工作", "url": "http://x.com/1",
                "source_name": "测试",
                "summary": "加力扩大内需，推动重大项目开工。",
                "reason": "直接关系就业与消费",
            }],
            "select_mode": "llm",
            "stage1": "解读：加力扩大内需",
            "stage2": "⚠️ 新提法：…\n⚠️ 力度变化：…",
            "stage3": "💡建议一句话",
        }
        digest = plugin._assemble_digest("2026-09-28", [{"title": "t"}], result)
        assert "今日抓取 1 条 · 精选 1 条" in digest
        # 顺序：要闻 → 措辞解读 → ⚠️措辞变化 → 建议（标题保留图例）
        assert digest.index("### 📌 今日要闻") < digest.index("### 🔍 官方措辞解读")
        assert digest.index("### 🔍 官方措辞解读") < digest.index("### ⚠️ 历史措辞变化")
        assert digest.index("### ⚠️ 历史措辞变化") < digest.index("### 💡 老百姓一句话建议")
        assert digest.index("### 💡 老百姓一句话建议") < digest.index("建议一句话")
        assert "**[国务院部署稳增长工作](http://x.com/1)**" in digest
        assert digest.count("http://x.com/1") == 1
        # 正文不再重复图例（⚠️/💡 只在标题出现）
        assert "新提法：…" in digest and "力度变化：…" in digest
        assert "⚠️ 新提法" not in digest and "💡建议一句话" not in digest

    def test_digest_includes_market_impact_for_rate_news(self):
        """利率类新闻要单独一节说明对黄金/股市的影响。"""
        plugin = self._mk_plugin()
        result = {
            "selected": [{"title": "央行降准0.25个百分点", "url": "http://x.com/1",
                          "source_name": "测试", "summary": "释放长期资金", "reason": "流动性"}],
            "select_mode": "llm",
            "market_impact": "黄金：通常偏强\n股市：流动性改善\n建议：分批定投，不加杠杆",
            "stage1": "【核心判断】…", "stage2": "", "stage3": "💡建议",
        }
        digest = plugin._assemble_digest("2026-10-25", [{"title": "t"}], result)
        assert "### 📈 利率变化对黄金与股市的影响" in digest
        assert digest.index("### 📌 今日要闻") < digest.index("### 📈 利率变化")
        assert "黄金：通常偏强" in digest and "分批定投" in digest

    def test_digest_empty_mode_no_analysis(self):
        plugin = self._mk_plugin()
        result = {"selected": [], "select_mode": "empty",
                  "stage1": "", "stage2": "", "stage3": ""}
        digest = plugin._assemble_digest("2026-09-28", [{"title": "t"}], result)
        assert "今日抓取 1 条 · 精选 0 条" in digest
        assert "今日无重要新闻" in digest
        assert "### 🔍" not in digest, "无重要新闻时不应推送解读"

    def test_digest_no_fallback_articles_when_empty(self):
        """2026-10-10：无合格信息时不再按最新时间凑数兜底（宁缺毋滥）。"""
        plugin = self._mk_plugin()
        result = {"selected": [], "select_mode": "empty",
                  "stage1": "", "stage2": "", "stage3": ""}
        articles = [{
            "title": "兜底新闻", "url": "http://x.com/f",
            "summary": "按时间最新", "body": "", "published": "2026-09-27",
            "source_name": "源",
        }]
        digest = plugin._assemble_digest("2026-09-28", articles, result)
        assert "兜底新闻" not in digest, "空选时不得用最新新闻凑数"
        assert "今日无重要新闻" in digest

    def test_digest_stage2_dedups_repeated_lines(self):
        plugin = self._mk_plugin()
        result = {"selected": [{
            "title": "上海楼市新政", "url": "http://x.com/1",
            "source_name": "测试", "summary": "s", "reason": "r"}],
            "select_mode": "llm",
            "stage1": "解读一句话",
            "stage2": ("⚠️ 新提法：加强预售管理\n⚠️ 新提法：加强预售管理\n"
                       "⚠️ 力度变化：今日主题首次出现，无历史可比\n"
                       "⚠️ 力度变化：今日主题首次出现，无历史可比"),
            "stage3": "💡优先买现房",
        }
        digest = plugin._assemble_digest("2026-09-28", [{"title": "t"}], result)
        assert digest.count("新提法：加强预售管理") == 1
        assert digest.count("无历史可比") == 1
        assert "⚠️ 新提法" not in digest, "正文行首图例应被剔除，只保留在标题"

    def test_digest_stage3_keeps_first_advice_only(self):
        plugin = self._mk_plugin()
        result = {"selected": [{
            "title": "上海楼市新政", "url": "http://x.com/1",
            "source_name": "测试", "summary": "s", "reason": "r"}],
            "select_mode": "llm",
            "stage1": "解读一句话",
            "stage2": "（无历史公报可对比，跳过同主题措辞变化对比。）",
            "stage3": "💡优先买现房\n💡关注基建创业方向",
        }
        digest = plugin._assemble_digest("2026-09-28", [{"title": "t"}], result)
        assert "优先买现房" in digest
        assert "基建创业方向" not in digest, "应只保留第一条建议"
        assert "💡优先买现房" not in digest, "建议正文行首 💡 应被剔除，只保留在标题"


class TestSelectStage:
    """精选阶段：解析健壮性、规则兜底、标题匹配、同主题措辞对比、空模式跳过。"""

    def test_select_parse_valid(self):
        from family_os.plugins.news_digest.analyzer import _stage_select_parse

        raw = '{"items":[{"title":"A","summary":"S","reason":"R"}]}'
        assert _stage_select_parse(raw) == [{"title": "A", "summary": "S", "reason": "R"}]

    def test_select_parse_empty_items_is_valid_empty(self):
        from family_os.plugins.news_digest.analyzer import _stage_select_parse

        assert _stage_select_parse('{"items":[]}') == []

    def test_select_parse_malformed_returns_none(self):
        from family_os.plugins.news_digest.analyzer import _stage_select_parse

        assert _stage_select_parse("我不是JSON") is None
        assert _stage_select_parse('{"no_items":1}') is None

    def test_select_parse_truncated_recovers(self):
        from family_os.plugins.news_digest.analyzer import _stage_select_parse

        truncated = ('{"items":[{"title":"政策加码","summary":"扩内需",'
                     '"reason":"重要"}],"no')
        assert _stage_select_parse(truncated) == [
            {"title": "政策加码", "summary": "扩内需", "reason": "重要"}]

    def test_select_note_extracted(self):
        from family_os.plugins.news_digest.analyzer import (
            _stage_select_parse, _stage_select_note,
        )

        raw = '{"items":[],"note":"今日均为常规报道，无重大政策信号"}'
        assert _stage_select_parse(raw) == []
        assert _stage_select_note(raw) == "今日均为常规报道，无重大政策信号"

    def test_select_note_malformed_empty(self):
        from family_os.plugins.news_digest.analyzer import _stage_select_note

        assert _stage_select_note("我不是JSON") == ""

    def test_select_no_articles_returns_empty(self, tmp_path):
        from family_os.plugins.news_digest import analyzer

        class Boom:
            def ask(self, prompt, model_tier="default", plugin_name="unknown", **kw):
                raise RuntimeError("不应被调用")

        assert analyzer._stage_select(_mk_ctx(tmp_path, Boom()), []) == ([], "")

    def test_select_genuine_empty(self, tmp_path):
        from family_os.plugins.news_digest import analyzer

        class Empty:
            def ask(self, prompt, model_tier="default", plugin_name="unknown", **kw):
                return '{"items":[],"note":"今日均为常规报道"}'

        out, note = analyzer._stage_select(_mk_ctx(tmp_path, Empty()),
                                           [{"title": "x"}])
        assert out == []
        assert note == "今日均为常规报道"

    def test_select_prompt_defaults_to_one_and_caps_three(self, tmp_path):
        """数量规则：默认只选 1 条，特别重要才扩到 2~3 条，硬上限 3。"""
        from family_os.plugins.news_digest import analyzer

        prompts = []

        class Rec:
            def ask(self, prompt, model_tier="default", plugin_name="unknown", **kw):
                prompts.append(prompt)
                return '{"items":[]}'

        analyzer._stage_select(_mk_ctx(tmp_path, Rec()), [{"title": "x"}])
        assert "大事可放 3 条" in prompts[0]
        assert "全部放出来" in prompts[0]
        assert "最多 3 条" in prompts[0]

    def test_select_output_capped_at_three(self, tmp_path):
        from family_os.plugins.news_digest import analyzer

        items = [{"title": f"国家发改委印发第{i}号管理办法", "summary": "x",
                  "reason": "y", "topic_key": "k"}
                 for i in range(1, 6)]

        class Many:
            def ask(self, prompt, model_tier="default", plugin_name="unknown", **kw):
                return json.dumps({"items": items}, ensure_ascii=False)

        out, _ = analyzer._stage_select(_mk_ctx(tmp_path, Many()), [{"title": "x"}])
        assert len(out) == 3, "无论模型返回多少条，最多保留 3 条"

    def test_qualified_news_gate(self):
        """活动类、媒体栏目、无官方出处的条目一律不够格（2026-10-26 真实误选回归）。"""
        from family_os.plugins.news_digest import analyzer as A

        assert A._is_qualified_news({
            "title": "护航对外投资高质量发展，新办法12月1日起施行",
            "summary": "国家发改委公布《对外投资管理办法》"}) is True
        # 栏目评论（媒体二次加工）
        assert A._is_qualified_news({
            "title": "中国经济面面观｜出口首破8万亿：“新”字挑大梁",
            "summary": "出口同比增长16.4%"}) is False
        # 活动类（电影节）
        assert A._is_qualified_news({
            "title": "从“工具”跃升为“生产力”， AI重构电影生产逻辑",
            "summary": "2026长三角国际青年电影周在浙江宁波举办"}) is False
        # 论坛/峰会
        assert A._is_qualified_news({"title": "某国际人工智能论坛在京召开"}) is False
        # 展会（广交会等交易会，标题带「出口/增长」数据词也算活动）
        assert A._is_qualified_news({"title": "从单品到成套供给 广交会家居外贸跑出增长新曲线"}) is False
        # 官方数据（标题含同比/出口）
        assert A._is_qualified_news({"title": "9月CPI同比上涨1.8%"}) is True
        # 无任何官方/数据痕迹
        assert A._is_qualified_news({"title": "某公司发布新款手机"}) is False

    def test_qualified_news_gate_today_real_cases(self):
        """2026-10-27 真实误选回归：技术性操作、省级数据、地方公告一律不够格。"""
        from family_os.plugins.news_digest import analyzer as A

        # 央行新设互换便利：技术性流动性操作，非利率决策
        assert A._is_qualified_news({
            "title": "中国央行设立债市非银大额赎回互换便利",
            "summary": "央行新设的这项工具旨在为债券市场非银机构提供流动性支持"}) is False
        # 省级 GDP：地方数据对全国读者无普遍意义
        assert A._is_qualified_news({
            "title": "浙江前三季度GDP达73224亿元 同比增长5.6%"}) is False
        # 地方人大公告
        assert A._is_qualified_news({
            "title": "河北省第十四届人民代表大会常务委员会公告"}) is False
        # 国务院令公布的条例：够格
        assert A._is_qualified_news({
            "title": "李强签署国务院令 公布《事业单位登记管理条例》"}) is True
        # 利率决策仍够格（降准不是技术性操作）
        assert A._is_qualified_news({"title": "央行宣布降准0.5个百分点"}) is True
        # 全国口径数据仍够格
        assert A._is_qualified_news({"title": "9月CPI同比上涨1.8%"}) is True

    def test_qualified_news_gate_keeps_top_meetings(self):
        """「召开/举办」是泛动词，不能把政治局会议等顶层会议误杀（2026-10-26 修正）。"""
        from family_os.plugins.news_digest import analyzer as A

        assert A._is_qualified_news({
            "title": "中共中央政治局召开会议 分析研究当前经济形势和经济工作"}) is True
        assert A._is_qualified_news({"title": "国务院常务会议部署稳就业稳外贸举措"}) is True
        assert A._is_qualified_news({"title": "全国两会审议政府工作报告"}) is True
        # 走访调研类活动仍应剔除
        assert A._is_qualified_news({"title": "习近平在江苏考察调研"}) is False
        # 活动通知（含正式文件词但属活动）仍应剔除
        assert A._is_qualified_news({"title": "关于举办2026世界人工智能大会高峰论坛的通知"}) is False

    def test_qualified_news_gate_rejects_party_and_quote_pieces(self):
        """2026-10-30 真实误选回归：党务主题、个人署名表态稿一律不够格。"""
        from family_os.plugins.news_digest import analyzer as A

        # 党务：即使标题含「决定」这类正式文件词也不选
        assert A._is_qualified_news({
            "title": "唐方裕：全会最重要的成果是审议通过了《中共中央关于持之以恒推进"
                     "全面从严治党若干重大问题的决定》"}) is False
        assert A._is_qualified_news({"title": "中央纪委国家监委部署党风廉政建设重点工作"}) is False
        # 个人署名表态稿（非机构前缀）
        assert A._is_qualified_news({"title": "张伟：下半年经济将保持稳中向好"}) is False
        # 机构+冒号仍属发布口径，保留
        assert A._is_qualified_news({"title": "国家统计局：10月CPI同比上涨1.8%"}) is True
        assert A._is_qualified_news({"title": "财政部：明年将实施更积极的财政政策"}) is True
        # 顶层会议不受影响
        assert A._is_qualified_news({
            "title": "中共中央政治局召开会议 分析研究2027年经济工作"}) is True

    def test_select_prompt_includes_user_rules(self, tmp_path, monkeypatch):
        """prompts/select.md 存在时会被追加到精选提示词（用户可自行调优口径）。"""
        from family_os.plugins.news_digest import analyzer

        (tmp_path / "prompts").mkdir()
        (tmp_path / "prompts" / "select.md").write_text(
            "- 标题含「电影周」的一律不选。", encoding="utf-8")
        monkeypatch.setenv("UGLYCAT_PROMPTS_DIR", str(tmp_path / "prompts"))
        assert "标题含「电影周」的一律不选" in analyzer._extra_rules("select")
        # 未提供的阶段不受影响
        assert analyzer._extra_rules("stage1") == ""
        monkeypatch.setenv("UGLYCAT_PROMPTS_DIR", str(tmp_path / "nonexistent"))
        assert analyzer._extra_rules("select") == ""

    def test_select_drops_unqualified_items(self, tmp_path):
        """LLM 选 3 条但 2 条不合格 → 只保留合格的那 1 条。"""
        from family_os.plugins.news_digest import analyzer

        class Mix:
            def ask(self, prompt, model_tier="default", plugin_name="unknown", **kw):
                return json.dumps({"items": [
                    {"title": "护航对外投资高质量发展，新办法12月1日起施行",
                     "summary": "国家发改委公布《对外投资管理办法》", "reason": "x", "topic_key": "对外投资"},
                    {"title": "中国经济面面观｜出口首破8万亿", "summary": "栏目原文",
                     "reason": "x", "topic_key": "出口"},
                    {"title": "AI重构电影生产逻辑", "summary": "电影周在宁波举办",
                     "reason": "x", "topic_key": "电影"},
                ]}, ensure_ascii=False)

        out, _ = analyzer._stage_select(_mk_ctx(tmp_path, Mix()), [{"title": "x"}])
        assert len(out) == 1
        assert "对外投资" in out[0]["title"]

    def test_select_llm_failure_returns_none(self, tmp_path):
        from family_os.plugins.news_digest import analyzer

        calls = {"n": 0}

        class Down:
            def ask(self, prompt, model_tier="default", plugin_name="unknown", **kw):
                calls["n"] += 1
                raise RuntimeError("down")

        out, note = analyzer._stage_select(_mk_ctx(tmp_path, Down()),
                                           [{"title": "x"}])
        assert out is None
        assert note == ""
        assert calls["n"] == 3, "主模型+两个备选模型各尝试一次后失败"

    def test_run_pipeline_no_fallback_when_select_unparsable(self, tmp_path):
        """2026-10-10：精选解析失败不再退「最新 N 条」兜底 → 空选（宁缺毋滥）。"""
        from family_os.plugins.news_digest import analyzer

        class Garbage:
            def ask(self, prompt, model_tier="default", plugin_name="unknown", **kw):
                return "我不是 JSON"

        res = analyzer.run_pipeline(_mk_ctx(tmp_path, Garbage()),
                                    [{"title": "无关紧要的日常新闻",
                                      "published": "2026-10-10"}],
                                    {}, tmp_path)
        assert res["select_mode"] == "empty"
        assert res["selected"] == []

    def test_match_selected_attaches_source_and_url(self):
        from family_os.plugins.news_digest import analyzer

        arts = [{"title": "国务院部署稳增长工作", "url": "http://x.com/1",
                 "source_name": "源", "body": "正文"}]
        out = analyzer._match_selected(arts, [{
            "title": "国务院部署稳增长工作", "summary": "S", "reason": "R"}])
        assert out[0]["url"] == "http://x.com/1"
        assert out[0]["source_name"] == "源"
        assert out[0]["summary"] == "S", "精选摘要优先，不被原文覆盖"

    def test_match_selected_unmatched_keeps_title(self):
        from family_os.plugins.news_digest import analyzer

        out = analyzer._match_selected([{"title": "其他"}], [{"title": "无关"}])
        assert out == [{"title": "无关"}]

    def test_stage2_prompt_compares_today_vs_history(self, tmp_path):
        from family_os.plugins.news_digest import analyzer

        prompts = []

        class Rec:
            def ask(self, prompt, model_tier="default", plugin_name="unknown", **kw):
                prompts.append(prompt)
                return "⚠️ 新提法：…"

        ctx = _mk_ctx(tmp_path, Rec())
        archive = {"zzjj": [{"date": "2026-09-26", "text": "公报A"}]}
        out = analyzer._stage2_compare(ctx, "METHOD", '["今日措辞"]', archive)
        assert out == "⚠️ 新提法：…"
        joined = "\n".join(prompts)
        assert "今日特征措辞" in joined, "应对比今日措辞而非只对比两份历史公报"
        assert "公报A" in joined

    def test_stage1_prompt_demands_core_judgement_and_max_three(self, tmp_path):
        """stage1 不再逐条翻译：先给【核心判断】，只挑 ≤3 条支撑措辞。"""
        from family_os.plugins.news_digest import analyzer

        prompts = []

        class Rec:
            def ask(self, prompt, model_tier="default", plugin_name="unknown", **kw):
                prompts.append(prompt)
                return "【核心判断】AI 创造高质量岗位、减缓替代"

        analyzer._stage1_decode(_mk_ctx(tmp_path, Rec()), "METHOD",
                                '{"items":[{"key_phrases":["高质量充分就业"]}]}',
                                [{"title": "国办印发意见", "summary": "支持AI就业"}])
        p = prompts[0]
        assert "【核心判断】" in p
        assert "≤3 条" in p and "不要逐条翻译" in p
        assert "今日要闻" in p and "给钱/减负" in p, "必须按政策动作类型归类且看到要闻"

    def test_stage2_focus_limits_to_two_changes(self, tmp_path):
        """stage2 只对比与核心判断相关、最多 2 条。"""
        from family_os.plugins.news_digest import analyzer

        prompts = []

        class Rec:
            def ask(self, prompt, model_tier="default", plugin_name="unknown", **kw):
                prompts.append(prompt)
                return "新提法：…"

        archive = {"zzjj": [{"date": "2026-09-26", "text": "公报A"}]}
        analyzer._stage2_compare(_mk_ctx(tmp_path, Rec()), "METHOD",
                                 '["今日措辞"]', archive,
                                 focus="【核心判断】AI 创造高质量岗位")
        p = prompts[0]
        assert "今日核心判断" in p
        assert "最多 2 条" in p
        assert "AI 创造高质量岗位" in p

    def test_stage2_no_archive_placeholder(self, tmp_path):
        from family_os.plugins.news_digest import analyzer

        class Boom:
            def ask(self, prompt, model_tier="default", plugin_name="unknown", **kw):
                raise RuntimeError("不应被调用")

        out = analyzer._stage2_compare(_mk_ctx(tmp_path, Boom()), "M", '["X"]', {})
        assert out.startswith("（无历史公报可对比")

    def test_stage3_grounded_on_selected_articles(self, tmp_path):
        """建议必须以今日精选为唯一事实来源，避免编造未发生的政策/目录。"""
        from family_os.plugins.news_digest import analyzer

        prompts = []

        class Rec:
            def ask(self, prompt, model_tier="default", plugin_name="unknown", **kw):
                prompts.append(prompt)
                return "💡 有失业风险的：查当地人社局最新通知"

        analyzer._stage3_advice(_mk_ctx(tmp_path, Rec()), "解读", "对比",
                                [{"title": "国办印发意见", "summary": "支持AI就业"}])
        p = prompts[0]
        assert "今日要闻（唯一事实来源）" in p
        assert "国办印发意见" in p
        assert "一律不许出现" in p
        assert "普通个人/家庭" in p
        assert "今日无直接可执行建议" in p

    def test_is_monetary_news_detection(self):
        """中国/美国的降息加息、降准、LPR、美联储议息都要触发黄金/股市说明。"""
        from family_os.plugins.news_digest import analyzer

        assert analyzer._is_monetary_news([{"title": "央行宣布降准0.25个百分点"}]) is True
        assert analyzer._is_monetary_news([{"title": "美联储宣布降息25个基点"}]) is True
        assert analyzer._is_monetary_news([{"title": "9月LPR报价出炉"}]) is True
        assert analyzer._is_monetary_news([{"title": "统计局：9月CPI同比上涨1.8%"}]) is False

    def test_run_pipeline_empty_mode_skips_stages(self, tmp_path):
        from family_os.plugins.news_digest import analyzer

        calls = {"n": 0}

        class OnlySelect:
            def ask(self, prompt, model_tier="default", plugin_name="unknown", **kw):
                calls["n"] += 1
                assert "精选" in prompt
                return '{"items":[]}'

        ctx = _mk_ctx(tmp_path, OnlySelect())
        res = analyzer.run_pipeline(ctx, [{"title": "x"}], {}, tmp_path)
        assert res["select_mode"] == "empty"
        assert res["stage1"] == ""
        assert calls["n"] == 1, "空模式应跳过 stage0-3，只调用精选一次"


class TestContentDedup:
    """跨天「主题/内容」级去重：按规范化标题相似度而非 URL。"""

    def test_normalize_title(self):
        from family_os.plugins.news_digest.dedup import normalize_title

        assert normalize_title("政治局开会 部署经济工作 发布") == "政治局开会部署经济工作"

    def test_is_dup_cross_source_same_topic(self):
        """同一事件被不同媒体以不同 URL 报道，标题近似 → 判重复。"""
        from family_os.plugins.news_digest.dedup import is_dup

        seen = ["英国广播公司：中国央行宣布降准支持实体经济"]
        new = "中国媒体报道：央行宣布降准支持实体经济一批"
        assert is_dup(new, seen, threshold=0.7)

    def test_is_dup_distinct_topic(self):
        from family_os.plugins.news_digest.dedup import is_dup

        seen = ["央行宣布降准"]
        assert not is_dup("新能源产业迎政策利好", seen)

    def test_filter_duplicates_keeps_new(self):
        from family_os.plugins.news_digest.dedup import filter_duplicates

        # 历史指纹已解读过 "央行全面降准"，今日该主题又以不同标题/URL 再出现 → 去重
        seen = ["央行宣布全面降准 支持实体经济"]
        articles = [
            {"title": "央行宣布全面降准支持实体经济", "url": "a"},       # 命中 → 去重
            {"title": "新能源产业政策加码", "url": "c"},                 # 新 → 保留
        ]
        kept, new_titles = filter_duplicates(articles, seen, threshold=0.8)
        assert {a["url"] for a in kept} == {"c"}
        assert new_titles == ["新能源产业政策加码"]  # 仅新增条目计入指纹
        assert kept[0]["title"] == "新能源产业政策加码"

    def test_filter_duplicates_seen_blank_title_not_written(self):
        from family_os.plugins.news_digest.dedup import filter_duplicates

        articles = [{"title": "   ", "url": "blank"},
                    {"title": "正常标题新闻", "url": "ok"}]
        kept, new_titles = filter_duplicates(articles, [])
        assert len(kept) == 2
        assert new_titles == ["正常标题新闻"], "空标题不写入指纹"

    def test_dedup_same_day_merges_multi_source_event(self):
        """内容去重：当日不同源报道同一事件，只保留最早一条。"""
        from family_os.plugins.news_digest.dedup import dedup_same_day

        arts = [
            {"title": "央行宣布全面降准0.5个百分点", "url": "u2",
             "published": "2026-10-10 09:00"},
            {"title": "央行宣布全面降准0.5个百分点 释放长期资金", "url": "u1",
             "published": "2026-10-10 08:00"},
            {"title": "新能源产业迎政策利好", "url": "u3",
             "published": "2026-10-10 07:00"},
        ]
        out = dedup_same_day(arts)
        urls = [a["url"] for a in out]
        assert "u3" in urls
        assert "u1" in urls, "同一事件保留发布时间最早的一条作代表"
        assert "u2" not in urls, "当日同事件的后续报道应被合并"

    def test_seen_persist_and_load(self, tmp_path):
        from family_os.plugins.news_digest import storage

        storage.save_seen_titles(tmp_path, _d(), ["A标题", "B标题"])
        loaded = storage.load_seen_titles(tmp_path, window_days=7)
        assert "A标题" in loaded and "B标题" in loaded

    def test_seen_load_empty_when_missing(self, tmp_path):
        from family_os.plugins.news_digest import storage

        assert storage.load_seen_titles(tmp_path) == []

    def test_seen_respects_window(self, tmp_path):
        from family_os.plugins.news_digest import storage
        from datetime import timedelta
        from datetime import datetime

        old = (datetime.now() - timedelta(days=9)).strftime("%Y-%m-%d")
        # 直接写超窗文件以断言 load 过滤
        sf = storage.seen_dir(tmp_path) / f"{old}.json"
        sf.parent.mkdir(parents=True, exist_ok=True)
        sf.write_text(json.dumps(["很旧标题"]), encoding="utf-8")
        assert storage.load_seen_titles(tmp_path, window_days=7) == []

    def test_run_applies_content_dedup(self, tmp_path, monkeypatch):
        """端到端：历史指纹命中同一主题 → 该条被剔除；无合格信息 → 跳过当日推送。"""
        from family_os.plugins.news_digest import storage
        from family_os.plugins.news_digest.plugin import NewsDigest

        # 预置历史指纹：昨天已解读过 "国务院部署稳增长工作"
        storage.save_seen_titles(tmp_path, _d(1), ["国务院部署稳增长工作"])

        class GoodGW:
            def ask(self, prompt, model_tier="default", plugin_name="unknown", **kw):
                if "输出 JSON" in prompt:
                    return '{"items":[{"thread":"宏观","key_phrases":["稳增长"]}]}'
                return "解读\n💡建议"

        monkeypatch.setattr(
            "family_os.plugins.news_digest.plugin.fetcher",
            _FakeFetcher(),  # 返回与历史同标题的文章 → 应被去重
        )
        ctx = _mk_ctx(tmp_path, GoodGW())
        res = NewsDigest().run(ctx)
        # 该文章被去重后无合格信息 → 跳过当日推送（静默、无消息）
        assert res.status == "skipped"
        assert res.data.get("_messages") == {}
        assert res.data.get("total_fetched") == 0
        st = json.loads((tmp_path / "history" / "news_digest" / "status.json")
                        .read_text(encoding="utf-8"))
        assert st["status"] == "no_news"


class TestKeyMeetings:
    """重要会议归档（2026-09-28）：命中登记表即提纯入库，供同主题措辞对比。

    2026-10-10：会议不再有任何强制入选保底，是否入选完全由 LLM 精选决定。
    """

    def test_meeting_key_mapping(self):
        from family_os.plugins.news_digest.analyzer import _meeting_key_for

        cases = {
            "中共中央政治局会议分析研究当前经济形势": "zzjj",
            "中央经济工作会议在北京举行": "jingji",
            "中共二十届四中全会在京举行": "quanhui",
            "全国两会即将召开": "lianghui",
            "政府工作报告：今年GDP目标5%左右": "lianghui",
            "全国政协十四届三次会议开幕": "lianghui",
            "地方两会陆续召开": "difang_lianghui",
            "某地开展安全生产检查": None,
            "": None,
        }
        for title, key in cases.items():
            assert _meeting_key_for(title) == key, f"{title!r} -> {_meeting_key_for(title)!r}"

    def test_auto_store_meetings_writes_archive(self, tmp_path):
        from family_os.plugins.news_digest import analyzer, storage

        articles = [{
            "title": "中央经济工作会议在北京举行", "url": "http://x.com/j",
            "source_name": "源", "summary": "总结今年经济工作，部署明年任务",
        }]
        selected = [{"title": "中央经济工作会议在北京举行"}]
        analyzer._auto_store_meetings(_mk_ctx(tmp_path, None), "2026-09-28",
                                      articles, selected, "stage1解读文本")
        recent = storage.get_recent_meeting_texts(tmp_path, "jingji", 2)
        assert len(recent) == 1
        assert recent[0]["date"] == "2026-09-28"
        text = recent[0]["text"]
        assert "【标题】中央经济工作会议在北京举行" in text
        assert "【来源】源" in text
        assert "【提纯】总结今年经济工作" in text
        assert "【重要性】重要会议·中央经济工作会议" in text
        assert "【措辞解读】stage1解读文本" in text

    def test_auto_store_meetings_archives_unselected_summary_only(self, tmp_path):
        """未入选的会议也归档概要（无措辞），保证对比库完整。"""
        from family_os.plugins.news_digest import analyzer, storage

        articles = [{
            "title": "国务院常务会议部署稳就业措施",
            "source_name": "源", "summary": "研究稳就业稳经济政策措施",
        }]
        analyzer._auto_store_meetings(_mk_ctx(tmp_path, None), "2026-10-10",
                                      articles, [], "")
        recent = storage.get_recent_meeting_texts(tmp_path,
                                                  "guowuyuan_changwu", 2)
        assert len(recent) == 1
        assert "【提纯】研究稳就业" in recent[0]["text"]
        assert "【措辞解读】" not in recent[0]["text"], "未入选会议无措辞"

    def test_run_pipeline_key_meeting_not_forced_but_still_archived(self, tmp_path):
        """2026-10-10：会议不再保底入选——LLM 精选为空即空选，但仍自动归档。

        移除全部规则保底后，LLM 判"今日无重要"（items=[]）时 mode="empty"，
        选中列表为空；会议新闻虽未入选，仍按登记表入库供后续措辞对比。
        """
        from family_os.plugins.news_digest import analyzer, storage

        class GoodGW:
            def ask(self, prompt, model_tier="default", plugin_name="unknown", **kw):
                if "精选" in prompt:
                    return '{"items":[],"note":"今日均为常规报道"}'
                if "输出 JSON" in prompt:  # stage0
                    return '{"items":[{"thread":"宏观","key_phrases":["稳中求进"]}]}'
                return "解读\n💡建议"

        articles = [
            {"title": "中共中央政治局会议分析研究当前经济形势", "url": "http://x.com/zz",
             "source_name": "源", "summary": "部署下一步经济工作", "published": "2026-09-28"},
            {"title": "某地社区举办趣味运动会", "url": "http://x.com/f",
             "source_name": "源", "summary": "居民健身", "published": "2026-09-28"},
        ]
        ctx = _mk_ctx(tmp_path, GoodGW())
        run_dir = storage.run_dir_for(tmp_path, "2026-09-28")
        run_dir.mkdir(parents=True, exist_ok=True)
        res = analyzer.run_pipeline(ctx, articles, {}, run_dir)
        assert res["select_mode"] == "empty", "无规则保底：LLM 空选即空模式"
        assert res["selected"] == []
        # 会议仍自动归档（key=zzjj，date=运行日），供后续同主题措辞对比
        recent = storage.get_recent_meeting_texts(tmp_path, "zzjj", 2)
        assert recent and recent[0]["date"] == "2026-09-28"
        assert "【标题】中共中央政治局会议分析研究当前经济形势" in recent[0]["text"]


class TestRecentSelectedTopics:
    """隔日选题去重（2026-10-01）：近期已推送主题不再重复精选 + 注泄漏剔除。"""

    # ── storage：selected/ 落盘 ────────────────────────────────────────

    def test_selected_persist_and_load(self, tmp_path):
        from family_os.plugins.news_digest import storage

        storage.save_selected_topics(tmp_path, _d(1), [
            {"title": "房贷贴息新政", "topic_key": "房贷贴息", "summary": "首套房贴息"},
            {"title": "A"}])
        storage.save_selected_topics(tmp_path, _d(), [{"title": "B"}])
        loaded = storage.load_recent_topics(tmp_path, window_days=7)
        by_title = {t["title"]: t for t in loaded}
        assert "房贷贴息新政" in by_title and "B" in by_title
        assert by_title["房贷贴息新政"]["topic_key"] == "房贷贴息"
        assert by_title["房贷贴息新政"]["summary"] == "首套房贴息"

    def test_selected_loads_legacy_string_format(self, tmp_path):
        """兼容旧版 selected/<date>.json（纯标题字符串列表）。"""
        from family_os.plugins.news_digest import storage

        sf = storage.selected_dir(tmp_path) / f"{_d()}.json"
        sf.parent.mkdir(parents=True, exist_ok=True)
        sf.write_text(json.dumps(["旧格式主题"]), encoding="utf-8")
        loaded = storage.load_recent_topics(tmp_path)
        assert [t["title"] for t in loaded] == ["旧格式主题"]
        assert loaded[0]["topic_key"] == ""

    def test_selected_load_empty_when_missing(self, tmp_path):
        from family_os.plugins.news_digest import storage

        assert storage.load_recent_topics(tmp_path) == []

    def test_selected_ignores_blank_titles(self, tmp_path):
        from family_os.plugins.news_digest import storage

        storage.save_selected_topics(tmp_path, _d(),
                                     [{"title": " "}, {"title": "有效标题"}])
        assert [t["title"] for t in storage.load_recent_topics(tmp_path)] == ["有效标题"]

    def test_cleanup_rotates_selected(self, tmp_path):
        from family_os.plugins.news_digest import storage
        from datetime import timedelta, datetime

        old = (datetime.now() - timedelta(days=9)).strftime("%Y-%m-%d")
        sf = storage.selected_dir(tmp_path) / f"{old}.json"
        sf.parent.mkdir(parents=True, exist_ok=True)
        sf.write_text(json.dumps(["很旧主题"]), encoding="utf-8")
        storage.save_selected_topics(tmp_path, _d(), [{"title": "今日主题"}])
        storage.cleanup(tmp_path, keep_runs=7)
        assert not sf.exists(), "selected 超窗指纹应被轮换"
        assert [t["title"] for t in storage.load_recent_topics(tmp_path)] == ["今日主题"]

    # ── 筛选阶段：政策性大新闻 + 中美利率/会议优先（不含去重指令） ────

    def test_select_prompt_focuses_policy_rates_meetings(self, tmp_path):
        from family_os.plugins.news_digest import analyzer

        prompts = []

        class Rec:
            def ask(self, prompt, model_tier="default", plugin_name="unknown", **kw):
                prompts.append(prompt)
                return ('{"items":[{"title":"新能源政策加码","summary":"s",'
                        '"reason":"r","topic_key":"新能源"}]}')

        ctx = _mk_ctx(tmp_path, Rec())
        analyzer._stage_select(ctx,
                               [{"title": "新能源政策加码", "published": "2026-10-01"}])
        joined = "\n".join(prompts)
        assert "政策性大新闻" in joined, "入选标准应收紧为政策性大新闻"
        assert "加息" in joined and "降息" in joined, "应重点关注中美国加息降息"
        assert "美联储" in joined, "应关心中美货币政策"
        assert "中央经济工作会议" in joined, "应重点关注重要会议"
        assert "topic_key" in joined, "应要求 LLM 回传 topic_key 供落盘去重"
        assert "近期已推送主题" not in joined, "去重指令应移出筛选阶段"

    # ── LLM 去重阶段：主题级语义去重（独立调用） ──────────────────────

    def test_dedup_prompt_lists_recent_topics_with_key(self, tmp_path):
        from family_os.plugins.news_digest import analyzer

        prompts = []

        class Rec:
            def ask(self, prompt, model_tier="default", plugin_name="unknown", **kw):
                prompts.append(prompt)
                return '{"drop":[]}'

        ctx = _mk_ctx(tmp_path, Rec())
        out = analyzer._stage_dedup(
            ctx,
            [{"title": "经济日报：购房贷款贴息重在精准滴灌", "published": "2026-10-01"}],
            [{"title": "从房贷贴息看住房支持政策新探索", "topic_key": "房贷贴息",
              "summary": "首套房贷款贴息"}])
        assert out is not None and len(out) == 1, "无剔除时原样返回候选"
        joined = "\n".join(prompts)
        assert "近期已推送主题" in joined
        assert "[房贷贴息]" in joined, "近期主题应带 topic_key 标签供语义比对"
        assert "同一件事" in joined or "同一主题" in joined
        assert "index" in joined, "应要求 LLM 回传应剔除候选的 index"

    def test_dedup_drops_llm_flagged_same_topic(self, tmp_path):
        from family_os.plugins.news_digest import analyzer

        class Rec:
            def ask(self, prompt, model_tier="default", plugin_name="unknown", **kw):
                return '{"drop":[{"index":0,"topic_key":"房贷贴息","reason":"同主题"}]}'

        ctx = _mk_ctx(tmp_path, Rec())
        out = analyzer._stage_dedup(
            ctx,
            [{"title": "经济日报：购房贷款贴息重在精准滴灌"},
             {"title": "新能源龙头扩产"}],
            [{"title": "从房贷贴息看住房支持政策新探索", "topic_key": "房贷贴息"}])
        assert [a["title"] for a in out] == ["新能源龙头扩产"], \
            "LLM 判定同主题的候选应被剔除"

    def test_dedup_no_recent_topics_skips_llm(self, tmp_path):
        from family_os.plugins.news_digest import analyzer

        class Boom:
            def ask(self, prompt, model_tier="default", plugin_name="unknown", **kw):
                raise RuntimeError("不应被调用")

        out = analyzer._stage_dedup(_mk_ctx(tmp_path, Boom()),
                                    [{"title": "x"}], [])
        assert out is None, "无近期主题时跳过去重（不调 LLM）"

    def test_dedup_parse_variants(self):
        from family_os.plugins.news_digest.analyzer import _stage_dedup_parse

        assert _stage_dedup_parse("我不是JSON") is None
        assert _stage_dedup_parse('{"no_drop":1}') is None
        assert _stage_dedup_parse('{"drop":[]}') == set()
        assert _stage_dedup_parse(
            '{"drop":[{"index":2},{"index":"3"},{"x":1}]}') == {2, 3}

    def test_coarse_filter_only_drops_near_identical(self):
        """粗筛只拦近乎完全相同标题；同政策不同写法不被代码误删。"""
        from family_os.plugins.news_digest import analyzer

        arts = [
            {"title": "买房国补上线 最高节省利息近5万元"},
            {"title": "经济日报：购房贷款贴息重在精准滴灌"},
        ]
        kept = analyzer._filter_recent_topics(
            arts, [{"title": "买房国补上线 最高节省利息近5万元",
                    "topic_key": "房贷贴息"}])
        titles = [a["title"] for a in kept]
        assert "买房国补上线 最高节省利息近5万元" not in titles, \
            "近乎完全相同的候选应被粗筛剔除"
        assert "经济日报：购房贷款贴息重在精准滴灌" in titles, \
            "同主题不同写法相似度低，应留给 LLM 语义去重"

    def test_select_prompt_lists_policy_doc_priority(self, tmp_path):
        """筛选提示词：五类并列重点（2026-10-10 由「三类」扩展而来）。

        2026-10-10：保底机制整体移除后，五类重点完全由提示词覆盖承载。
        """
        from family_os.plugins.news_digest import analyzer

        prompts = []

        class Rec:
            def ask(self, prompt, model_tier="default", plugin_name="unknown", **kw):
                prompts.append(prompt)
                return '{"items":[]}'

        analyzer._stage_select(_mk_ctx(tmp_path, Rec()),
                               [{"title": "x", "published": "2026-10-10"}])
        joined = "\n".join(prompts)
        assert "顶层政策文件" in joined
        assert "中共中央" in joined and "国务院" in joined
        assert "发展新质生产力的意见" in joined, "应给出示例文件避免 LLM 漏选"
        assert "必选关注重点" in joined
        assert "部委级重大政策" in joined and "证监会" in joined
        assert "重要经济数据发布" in joined and "CPI" in joined

    def test_no_force_include_helpers_left(self):
        """2026-10-10：全部规则保底已移除，五类重点只由提示词承载。"""
        from family_os.plugins.news_digest import analyzer

        for name in ("_ensure_rate_news", "_ensure_key_meetings",
                     "_ensure_policy_docs", "_ensure_ministry_docs",
                     "_ensure_econ_data", "_rate_hit", "_policy_doc_hit",
                     "_ministry_doc_hit", "_econ_data_hit", "_policy_subject"):
            assert not hasattr(analyzer, name), f"{name} 应已移除"

    def test_llm_selected_meeting_is_the_only_way_in(self, tmp_path):
        """无保底：会议/利率/数据类新闻只有被 LLM 精选才会进入 selected。"""
        from family_os.plugins.news_digest import analyzer

        articles = [
            {"title": "中共中央政治局会议分析研究当前经济形势",
             "source_name": "源", "summary": "部署经济工作", "published": "2026-10-10"},
            {"title": "美联储宣布降息25个基点",
             "source_name": "源", "summary": "联邦基金利率下调", "published": "2026-10-10"},
            {"title": "统计局：9月份CPI同比上涨0.4%",
             "source_name": "源", "summary": "居民消费价格温和上涨", "published": "2026-10-10"},
        ]

        class PickNone:
            def ask(self, prompt, model_tier="default", plugin_name="unknown", **kw):
                assert "精选" in prompt
                return '{"items":[],"note":"均与近期重复"}'

        res = analyzer.run_pipeline(_mk_ctx(tmp_path, PickNone()), articles, {},
                                    tmp_path)
        assert res["selected"] == [], "无保底：LLM 空选时不得有任何强制补入"
        assert res["select_mode"] == "empty"

        class PickOne:
            def ask(self, prompt, model_tier="default", plugin_name="unknown", **kw):
                if "精选" in prompt:
                    return ('{"items":[{"title":"美联储宣布降息25个基点",'
                            '"summary":"s","reason":"r","topic_key":"美联储降息"}]}')
                if "输出 JSON" in prompt:
                    return '{"items":[{"thread":"宏观","key_phrases":["降息"]}]}'
                return "解读\n💡建议"

        res2 = analyzer.run_pipeline(_mk_ctx(tmp_path, PickOne()), articles, {},
                                     tmp_path)
        assert [it["title"] for it in res2["selected"]] == ["美联储宣布降息25个基点"], \
            "仅 LLM 精选的一条入选，其余两条不因\"重要\"被自动补入"

    def test_meeting_registry_is_three_tuple_for_archive_only(self):
        """会议登记表降为三元组（正则/key/名），仅用于归档。"""
        from family_os.plugins.news_digest import analyzer

        assert all(len(row) == 3 for row in analyzer.MEETING_REGISTRY)
        assert analyzer._meeting_match("中央财经委员会第六次会议") == (
            "caijingwei", "中央财经委员会会议")

    def test_pipeline_all_duplicates_returns_empty(self, tmp_path):
        from family_os.plugins.news_digest import analyzer

        class Rec:
            def ask(self, prompt, model_tier="default", plugin_name="unknown", **kw):
                return '{"drop":[{"index":0}]}'

        arts = [{"title": "经济日报：购房贷款贴息重在精准滴灌",
                 "published": "2026-10-01"}]
        res = analyzer.run_pipeline(
            _mk_ctx(tmp_path, Rec()), arts, {}, tmp_path,
            [{"title": "从房贷贴息看住房支持政策新探索", "topic_key": "房贷贴息"}])
        assert res["select_mode"] == "empty"
        assert "重复" in res["selected_note"]

    # ── stage2：无官方措辞不调 LLM ─────────────────────────────────────

    def test_stage2_skips_when_no_official_wording(self, tmp_path):
        from family_os.plugins.news_digest import analyzer

        class Boom:
            def ask(self, prompt, model_tier="default", plugin_name="unknown", **kw):
                raise RuntimeError("不应被调用")

        placeholder = ("（今日未提取到明确的官方特征措辞：或新闻无显著政策表述，"
                       "或提取失败）")
        archive = {"zzjj": [{"date": "2026-09-26", "text": "公报A"}]}
        out = analyzer._stage2_compare(_mk_ctx(tmp_path, Boom()), "M",
                                       placeholder, archive)
        assert out == "", "无官方措辞时不应调 LLM 对比（避免任务说明泄漏）"

    # ── 渲染层：剔除「注：」泄漏行 ─────────────────────────────────────

    def test_digest_strips_note_lines(self):
        from family_os.plugins.news_digest.plugin import NewsDigest

        plugin = NewsDigest()
        result = {
            "selected": [{"title": "t", "url": "http://x.com/1", "source_name": "源",
                          "summary": "sm", "reason": "r"}],
            "select_mode": "llm",
            "stage1": "解读一句话",
            "stage2": "✓ 延续：无\n注：根据用户提供的任务说明，无法对比分析。",
            "stage3": "💡建议一句话",
        }
        digest = plugin._assemble_digest("2026-10-01", [], result)
        assert "注：" not in digest, "LLM 泄漏的「注：」说明行不应出现在推送里"
        assert "延续：无" in digest
        assert "✓ 延续：无" not in digest, "正文行首图例应剔除，只保留在标题"

    # ── 端到端：昨日已推送主题今日不再入选 ─────────────────────────────

    def test_run_avoids_recently_selected_topic(self, tmp_path, monkeypatch):
        from family_os.plugins.news_digest import storage
        from family_os.plugins.news_digest.plugin import NewsDigest

        # 昨日已精选推送该主题
        storage.save_selected_topics(tmp_path, _d(1),
                                     [{"title": "买房国补上线 最高节省利息近5万元",
                                       "topic_key": "房贷贴息"}])

        class ProbeOk:
            """探针应答 ok；同主题候选剔除后不应再被调用。"""
            def ask(self, prompt, model_tier="default", plugin_name="unknown", **kw):
                if "single word ok" in prompt:
                    return "ok"
                raise RuntimeError("不应被调用：同主题候选已剔除，无需 LLM")

        class FakeFetcher:
            def fetch_articles(self, data_dir, registry):
                return [{
                    "title": "买房国补上线！最高节省利息近5万元",
                    "url": "http://x.com/gb", "source_name": "test",
                    "summary": "国家首次对个人房贷提供贴息", "body": "",
                    "published": "2026-10-01",
                }]

        monkeypatch.setattr("family_os.plugins.news_digest.plugin.fetcher",
                            FakeFetcher())
        ctx = _mk_ctx(tmp_path, ProbeOk())
        res = NewsDigest().run(ctx)
        # 同主题已被剔除 → 无合格信息 → 跳过当日推送
        assert res.status == "skipped"
        assert res.data.get("_messages") == {}
        digest = res.data.get("digest", "")
        assert "今日无重要新闻" in digest
        assert "国补" not in digest, "同主题新闻不应再次入选推送"
        # 当日没有新增量主题 → 不落盘 selected（避免空文件）
        assert [t["title"] for t in
                storage.load_recent_topics(tmp_path, window_days=7)] == [
            "买房国补上线 最高节省利息近5万元"]

    def test_run_saves_selected_for_next_day(self, tmp_path, monkeypatch):
        from family_os.plugins.news_digest import storage
        from family_os.plugins.news_digest.plugin import NewsDigest

        class GoodGW:
            def ask(self, prompt, model_tier="default", plugin_name="unknown", **kw):
                if "精选" in prompt:
                    return '{"items":[{"title":"国家能源局印发新能源产业高质量发展通知",' \
                           '"summary":"新能源产业加速发展",' \
                           '"reason":"直接关系相关行业就业与投资机会。"}]}'
                if "输出 JSON" in prompt:
                    return '{"items":[{"thread":"产业","key_phrases":["新能源扩产"]}]}'
                return "解读内容\n💡建议一句话"

        class FakeFetcher:
            def fetch_articles(self, data_dir, registry):
                return [{
                    "title": "国家能源局印发新能源产业高质量发展通知", "url": "http://x.com/xny",
                    "source_name": "test", "summary": "s", "body": "",
                    "published": "2026-10-01",
                }]

        monkeypatch.setattr("family_os.plugins.news_digest.plugin.fetcher",
                            FakeFetcher())
        ctx = _mk_ctx(tmp_path, GoodGW())
        res = NewsDigest().run(ctx)
        assert res.status == "ok"
        saved = [t["title"] for t in
                 storage.load_recent_topics(tmp_path, window_days=7)]
        assert "国家能源局印发新能源产业高质量发展通知" in saved, "当日精选主题应落盘供隔日去重"


class TestNoNewsStatus:
    """2026-10-10：当日无合格信息 → 跳过推送（status=no_news），健康周报记 note。"""

    def test_health_check_no_news_is_note_not_issue(self, tmp_path):
        pytest.importorskip("family_os.plugins.health_check")
        from family_os.plugins.health_check import plugin as hc
        from family_os.plugins.news_digest import storage

        storage.write_status(tmp_path, "no_news",
                             "今日无合格新闻（宁缺毋滥），已跳过当日推送")
        out = hc._news_digest_check(tmp_path)
        assert out["issues"] == [], "无合格新闻属正常结果，不应升级为 issue"
        assert any("无合格新闻" in n for n in out["notes"])
        assert any("每日新闻解读" in n for n in out["notes"])


class TestFetchBody:
    """2026-10-13：修复正文提取——CSS/脚本噪音不得泄漏进正文。"""

    def test_extract_body_strips_css_and_script(self):
        from family_os.plugins.news_digest import fetcher

        html = (
            "<html><head>"
            "<style>.logo{float:left;margin:31px 25px;width:177px}"
            "#navbar{color:#cd131c;background:#fff}</style>"
            "<script>var cfg={a:1,b:2,c:3};</script>"
            "</head><body>"
            "<div>中新社北京10月12日电 国务院常务会议部署稳增长工作。</div>"
            "</body></html>"
        )
        out = fetcher._extract_body(html)
        assert "国务院常务会议" in out, "应提取到真实正文"
        assert "float" not in out and "navbar" not in out and "margin" not in out, \
            "CSS 不得泄漏进正文（原实现因解析器未复用而泄漏）"

    def test_extract_body_css_only_returns_empty(self):
        """正文只有 CSS/JS 噪音（无真实文本）→ 返回空串，降级用 summary。"""
        from family_os.plugins.news_digest import fetcher

        html = ("<html><head><style>.a{float:left}.b{margin:0}"
                ".c{padding:1px}</style></head><body></body></html>")
        assert fetcher._extract_body(html) == ""

    def test_extract_body_prefers_paragraphs_over_site_chrome(self):
        """正文优先取 <p> 段落，避开只含链接的导航菜单文本。"""
        from family_os.plugins.news_digest import fetcher

        # selectolax 是 pyproject 声明的必需依赖。缺失（＝用了非项目解释器）时
        # _extract_body 会静默走正则兜底分支、本用例失去意义，故直接报错，
        # 避免给出「不应取到导航菜单链接」这类误导性 diff。
        try:
            import selectolax  # noqa: F401
        except ImportError as e:
            pytest.fail(f"请在项目 venv 中运行门禁（app/.venv 或 uv run），缺 selectolax：{e}")

        para1 = ("新华社北京10月13日电，国家主席习近平13日下午在人民大会堂"
                 "会见来华进行国事访问的博茨瓦纳总统博科，就推动中非矿业合作"
                 "提出4点主张，双方将进一步深化能源、机械等领域务实合作。")
        para2 = ("与会各方表示，将以此为契机拓展在基础设施、农业加工、人才"
                 "培养等方面的合作空间，推动中非合作论坛成果落地见效。")
        html = (
            "<html><body>"
            '<div id="navbar"><a href="#">首页</a><a href="#">时政</a>'
            '<a href="#">财经</a><a href="#">国际</a></div>'
            f'<div class="left_zw"><p>{para1}</p><p>{para2}</p></div>'
            "</body></html>"
        )
        out = fetcher._extract_body(html)
        assert "中非矿业合作" in out, "应取到正文段落"
        assert "首页" not in out and "navbar" not in out, "不应取到导航菜单链接"

    def test_extract_body_junk_detector(self):
        from family_os.plugins.news_digest import fetcher

        assert fetcher._is_junk_body("") is True
        assert fetcher._is_junk_body(".a{color:red}.b{margin:0}.c{padding:1px}") is True
        assert fetcher._is_junk_body("@media screen and (max-width:600px)") is True
        assert fetcher._is_junk_body(
            "国务院常务会议部署稳增长工作，加力扩大内需。") is False


class TestSelectOnlyOfficialRelease:
    """2026-10-13：精选提示词加【只认官方发布】门槛。

    此前『整治"付费实习"灰色产业链』类**媒体深度报道**被误当政策大新闻入选，
    现要求 LLM 先核对发布主体+文种，非官方发布/行业专项治理一律不选。
    """

    def test_select_prompt_requires_official_release(self, tmp_path):
        from family_os.plugins.news_digest import analyzer

        prompts = []

        class Rec:
            def ask(self, prompt, model_tier="default", plugin_name="unknown", **kw):
                prompts.append(prompt)
                return '{"items":[]}'

        analyzer._stage_select(_mk_ctx(tmp_path, Rec()),
                               [{"title": "整治付费实习灰色产业链",
                                 "published": "2026-10-13"}])
        joined = "\n".join(prompts)
        assert "只认官方发布" in joined, "应加官方发布门槛"
        assert "发布主体" in joined and "文种" in joined, "应先核对发布主体与文种"
        assert "媒体评论" in joined and "深度报道" in joined and "调查报道" in joined, \
            "应明确排除媒体评论/深度报道/调查报道"
        assert "整治" in joined and "付费实习" in joined, "应点名行业专项整治类不选"
        assert "行业专项治理" in joined or "行业专项" in joined

    def test_select_prompt_keeps_six_priorities(self, tmp_path):
        """加门槛后六类重点（2026-10-23 新增权威研究机构报告）不退化。"""
        from family_os.plugins.news_digest import analyzer

        prompts = []

        class Rec:
            def ask(self, prompt, model_tier="default", plugin_name="unknown", **kw):
                prompts.append(prompt)
                return '{"items":[]}'

        analyzer._stage_select(_mk_ctx(tmp_path, Rec()),
                               [{"title": "x", "published": "2026-10-13"}])
        joined = "\n".join(prompts)
        assert "政策性大新闻" in joined
        assert "加息" in joined and "降息" in joined and "美联储" in joined
        assert "中央经济工作会议" in joined
        assert "顶层政策文件" in joined and "部委级重大政策" in joined
        assert "重要经济数据发布" in joined and "CPI" in joined
        assert "重点六" in joined and "权威研究机构报告" in joined
        assert "社科院" in joined and "国务院发展研究中心" in joined
        assert "topic_key" in joined
        assert "近期已推送主题" not in joined, "去重指令仍应留在独立阶段"

    def test_official_gate_accepts_research_institutes(self, tmp_path):
        """只认官方发布不得把研究机构报告一并排除（2026-10-23）。"""
        from family_os.plugins.news_digest import analyzer

        prompts = []

        class Rec:
            def ask(self, prompt, model_tier="default", plugin_name="unknown", **kw):
                prompts.append(prompt)
                return '{"items":[]}'

        analyzer._stage_select(_mk_ctx(tmp_path, Rec()),
                               [{"title": "x", "published": "2026-10-23"}])
        joined = "\n".join(prompts)
        assert "例外·权威研究机构" in joined
        assert "蓝皮书" in joined and "年度报告" in joined
        assert "转述、评论不单独入选" in joined, "媒体转述不得被当成机构发布"


class TestMethodologyWiring:
    """方法论必须按阶段注入：去重/筛选不注入，stage0/1/2 各取对应块，stage3 不注入。"""

    def _captured(self, monkeypatch):
        from family_os.plugins.news_digest import analyzer

        prompts: list[str] = []

        def fake_ask(_ctx, prompt):
            prompts.append(prompt)
            return '{"items": []}'

        monkeypatch.setattr(analyzer, "_ask_with_fallback", fake_ask)
        return prompts

    def test_dedup_and_select_get_no_methodology(self, tmp_path, monkeypatch):
        from family_os.plugins.news_digest import analyzer

        prompts = self._captured(monkeypatch)
        ctx = _mk_ctx(tmp_path, None)
        analyzer._stage_dedup(ctx, [{"title": "标题A", "source_name": "新华社"}],
                              [{"title": "近期标题", "topic_key": "标签"}])
        analyzer._stage_select(ctx, [{"title": "标题A", "source_name": "新华社"}])
        joined = "\n".join(prompts)
        assert "对冲规律" not in joined
        assert "高频词分级" not in joined

    def test_stage0_gets_grading_only(self, tmp_path, monkeypatch):
        from family_os.plugins.news_digest import analyzer, decode

        prompts = self._captured(monkeypatch)
        analyzer._stage0_extract(
            _mk_ctx(tmp_path, None), decode.stage0_block(),
            [{"title": "标题A", "source_name": "新华社", "summary": "摘要"}])
        assert "高频词分级" in prompts[0]
        assert "经济类专题表" not in prompts[0]

    def test_stage1_gets_topic_tables(self, tmp_path, monkeypatch):
        from family_os.plugins.news_digest import analyzer, decode

        prompts = self._captured(monkeypatch)
        analyzer._stage1_decode(
            _mk_ctx(tmp_path, None), decode.stage1_block(),
            '{"items":[{"thread":"货币","key_phrases":["降准降息仍有空间"]}]}')
        assert "经济类专题表" in prompts[0]

    def test_stage2_gets_formula_not_tables(self, tmp_path, monkeypatch):
        from family_os.plugins.news_digest import analyzer, decode

        prompts = self._captured(monkeypatch)
        archive = {"zzjj": [{"date": "2026-09-26", "text": "公报A"}]}
        analyzer._stage2_compare(
            _mk_ctx(tmp_path, None), decode.stage2_block(),
            '{"items":[{"thread":"货币","key_phrases":["降准降息仍有空间"]}]}', archive)
        assert "对冲规律" in prompts[0]
        assert "经济类专题表" not in prompts[0]

    def test_stage3_gets_no_methodology(self, tmp_path, monkeypatch):
        from family_os.plugins.news_digest import analyzer

        prompts = self._captured(monkeypatch)
        analyzer._stage3_advice(_mk_ctx(tmp_path, None), "解读内容", "对比内容")
        assert "对冲规律" not in prompts[0]
        assert "高频词分级" not in prompts[0]


class TestWebListSource:
    """2026-10-23：社科院各研究所无 RSS，新增 type: web_list 抓栏目列表页。

    时效以 **URL 内嵌日期**（tYYYYMMDD_xxxx.shtml）为准——实测页面文本日期可能
    来自父容器（gjs.cssn.cn 一条 10-20 的成果，容器显示「2026年9月7」），不可信。
    """

    @staticmethod
    def _days_ago(n: int) -> str:
        from datetime import datetime, timedelta, timezone

        return (datetime.now(timezone.utc) - timedelta(days=n)).strftime("%Y%m%d")

    def _href(self, days_ago: int, n: int = 6000000) -> str:
        d = self._days_ago(days_ago)
        return f"./xwzx/{d[:6]}/t{d}_{n}.shtml"

    def _html(self, items) -> str:
        return ("<html><body>"
                + "".join(f'<a href="{h}">{t}</a>' for t, h in items)
                + "</body></html>")

    def _patch(self, monkeypatch, html, bodies=None):
        from family_os.plugins.news_digest import fetcher

        class _Resp:
            text = html
            status_code = 200

            def raise_for_status(self):
                return None

        monkeypatch.setattr(fetcher.httpx, "get", lambda *a, **k: _Resp())
        monkeypatch.setattr(fetcher, "_fetch_body",
                            lambda url: (bodies or {}).get(url, ""))
        return fetcher

    @staticmethod
    def _src(**kw) -> dict:
        base = {
            "id": "cass_ie", "name": "社科院经济研究所", "type": "web_list",
            "list_url": "http://ie.cass.cn/",
            "link_pattern": r"t(20\d{2})(\d{2})(\d{2})_\d+\.shtml",
            "max_age_days": 7, "max_items": 5,
        }
        base.update(kw)
        return base

    def test_parses_dated_articles_and_resolves_urls(self, monkeypatch):
        from urllib.parse import urljoin

        href = self._href(1)
        fetcher = self._patch(monkeypatch, self._html([
            ("科研成果｜现代化产业体系建设 [2026-10-20]", href),
        ]))
        items = fetcher._fetch_web_list(self._src())

        assert len(items) == 1
        it = items[0]
        assert it["url"] == urljoin("http://ie.cass.cn/", href)
        assert it["title"] == "科研成果｜现代化产业体系建设", "标题尾部日期括注应清除"
        assert it["published"].startswith(self._days_ago(1)[:4])
        assert it["source_name"] == "社科院经济研究所"
        assert it["source_id"] == "cass_ie"

    def test_skips_stale_beyond_max_age(self, monkeypatch):
        fetcher = self._patch(monkeypatch, self._html([
            ("十天前的旧成果标题足够长", self._href(10)),
        ]))
        assert fetcher._fetch_web_list(self._src(max_age_days=7)) == []

    def test_excludes_admin_items(self, monkeypatch):
        """研究所列表混有行政信息，不能进新闻池。"""
        fetcher = self._patch(monkeypatch, self._html([
            ("关于召开学术研讨会的通知", self._href(1, 1)),
            ("2026 年度课题申报启事", self._href(1, 2)),
            ("我所公开招聘研究人员公告", self._href(1, 3)),
            ("征稿：本刊专题栏目", self._href(1, 4)),
            ("科研成果｜真实研究文章标题", self._href(1, 5)),
        ]))
        items = fetcher._fetch_web_list(self._src())
        assert [i["title"] for i in items] == ["科研成果｜真实研究文章标题"]

    def test_respects_max_items_and_sorts_newest_first(self, monkeypatch):
        fetcher = self._patch(monkeypatch, self._html([
            ("第三新的文章标题够长", self._href(3, 1)),
            ("最新的文章标题够长", self._href(1, 2)),
            ("第二新的文章标题够长", self._href(2, 3)),
        ]))
        items = fetcher._fetch_web_list(self._src(max_items=2))
        assert len(items) == 2
        assert items[0]["title"] == "最新的文章标题够长"
        assert items[1]["title"] == "第二新的文章标题够长"

    def test_skips_undated_and_wechat_links(self, monkeypatch):
        fetcher = self._patch(monkeypatch, self._html([
            ("没有日期的链接标题足够长", "/no/date/here.html"),
            ("微信公众号文章标题足够长", "https://mp.weixin.qq.com/s/abc123"),
        ]))
        assert fetcher._fetch_web_list(self._src()) == []

    def test_body_is_fetched_for_selected_items(self, monkeypatch):
        from urllib.parse import urljoin

        href = self._href(1)
        url = urljoin("http://ie.cass.cn/", href)
        fetcher = self._patch(monkeypatch, self._html([
            ("科研成果｜带正文的文章", href),
        ]), bodies={url: "正文内容：研究报告摘要。"})
        items = fetcher._fetch_web_list(self._src())
        assert items[0]["body"] == "正文内容：研究报告摘要。"

    def test_duplicate_urls_collapsed(self, monkeypatch):
        href = self._href(1)
        fetcher = self._patch(monkeypatch, self._html([
            ("同一篇文章重复出现", href),
            ("同一篇文章重复出现", href),
        ]))
        assert len(fetcher._fetch_web_list(self._src())) == 1

    def test_title_include_keeps_only_matching_items(self, monkeypatch):
        """求是网首页混入大量习近平活动报道，只保留标题含「求是」的条目。"""
        fetcher = self._patch(monkeypatch, self._html([
            ("习近平向2026全球治理论坛致信", self._href(1, 1)),
            ("2026年第20期《求是》杂志发表习近平总书记重要文章", self._href(5, 2)),
            ("中共中央政治局召开会议审议巡视报告", self._href(1, 3)),
        ]))
        items = fetcher._fetch_web_list(self._src(title_include=["求是"],
                                                  max_age_days=20))
        assert [i["title"] for i in items] == [
            "2026年第20期《求是》杂志发表习近平总书记重要文章"]

    def test_no_title_include_keeps_all(self, monkeypatch):
        """未配置白词时不做标题过滤（社科院各所走栏目页，无需白词）。"""
        fetcher = self._patch(monkeypatch, self._html([
            ("研究文章甲标题足够长", self._href(1, 1)),
            ("研究文章乙标题足够长", self._href(1, 2)),
        ]))
        assert len(fetcher._fetch_web_list(self._src())) == 2


class TestResearchSourceConfig:
    """sources.yaml 的社科院各所源配置必须完整，否则线上会静默抓空。"""

    def _web_list_sources(self) -> list[dict]:
        import yaml
        from pathlib import Path

        p = Path(__file__).resolve().parents[2] / "family_os" / "config" / "sources.yaml"
        data = yaml.safe_load(p.read_text(encoding="utf-8"))
        return [s for s in data["sources"] if s.get("type") == "web_list"]

    def test_three_cass_institutes_configured(self):
        ids = {s["id"] for s in self._web_list_sources()}
        assert ids == {"cass_ie", "cass_gjs", "cass_iqte", "qstheory", "xinhua_politics"}

    def test_qstheory_uses_title_whitelist(self):
        """求是网首页绝大多数是习近平活动报道，必须靠白词只看《求是》相关。"""
        qs = [s for s in self._web_list_sources() if s["id"] == "qstheory"]
        assert qs, "求是源未配置"
        assert "求是" in qs[0].get("title_include", []), "缺少 title_include"
        assert "qstheory.cn" in qs[0]["list_url"]

    def test_required_fields_present(self):
        for s in self._web_list_sources():
            assert s.get("list_url"), f"{s['id']} 缺 list_url"
            assert s.get("link_pattern"), f"{s['id']} 缺 link_pattern"
            assert "新闻" in s.get("tags", []), f"{s['id']} 未挂 新闻 tag，不会被抓取"
            assert s.get("max_age_days", 0) >= 2, f"{s['id']} 时效窗口过窄"

    def test_link_pattern_has_year_month_day_groups(self):
        """抓取层用第 1~3 组当日期，组数不足会全部静默跳过。"""
        import re
        for s in self._web_list_sources():
            p = re.compile(s["link_pattern"])
            assert p.groups >= 3, f"{s['id']} 的 link_pattern 缺少年月日捕获组"


class TestSelectCandidateWindow:
    """2026-10-24：精选输入窗口不能只有「最新 20 条」。

    实测当日 194 篇，社科院/求是 13 篇全部落在最新 20 条之外 → 模型根本没看到，
    误判「今日无合格新闻」。改为「最新 20 条 + 优先源最新 6 条」。
    """

    def _soft(self, n: int, hour: int = 20) -> dict:
        return {"title": f"软新闻{n}", "source_name": "新华社",
                "url": f"https://x/{n}", "published": f"2026-10-24T{hour:02d}:{n:02d}:00"}

    def _priority(self, n: int, day: int = 20) -> dict:
        return {"title": f"社科院研究成果{n}", "source_name": "社科院经济研究所",
                "url": f"https://cass/{n}", "published": f"2026-10-{day:02d}T00:00:00"}

    def test_priority_item_outside_newest_window_is_included(self):
        from family_os.plugins.news_digest import analyzer

        articles = [self._soft(n) for n in range(25)] + [self._priority(1)]
        picked = analyzer._pick_select_candidates(articles)
        titles = [a["title"] for a in picked]
        assert "社科院研究成果1" in titles, "优先源文章必须进入精选视野"
        assert len(picked) == analyzer.SELECT_WINDOW + 1

    def test_priority_quota_is_capped(self):
        from family_os.plugins.news_digest import analyzer

        articles = [self._soft(n) for n in range(25)] + [self._priority(n) for n in range(10)]
        picked = analyzer._pick_select_candidates(articles)
        assert len(picked) == analyzer.SELECT_WINDOW + analyzer.PRIORITY_QUOTA

    def _policy(self, n: int, day: int = 23) -> dict:
        return {"title": f"国务院印发意见{n}", "source_name": "新华社",
                "url": f"https://p/{n}", "published": f"2026-10-{day:02d}T00:00:00"}

    def test_policy_cue_item_outside_window_is_included(self):
        """前一天的重要政策文件被今天软新闻挤出最新 20 条时，仍要靠信号词补进来。"""
        from family_os.plugins.news_digest import analyzer

        articles = [self._soft(n) for n in range(25)] + [self._policy(1)]
        picked = analyzer._pick_select_candidates(articles)
        assert "国务院印发意见1" in [a["title"] for a in picked]

    def test_policy_cue_ignores_party_building_titles(self):
        """党建/理论标题不得被政策信号词桶拉进候选（「意见」等泛词已移除）。"""
        from family_os.plugins.news_digest import analyzer

        assert analyzer._has_policy_cue(
            {"title": "习近平党建思想对解决大党独有难题的理论贡献与实践指引"}) is False
        assert analyzer._has_policy_cue(
            {"title": "国务院办公厅印发《关于适应人工智能技术发展助力构建就业友好型"
                      "发展方式的意见》"}) is True

    def test_old_non_priority_items_still_excluded(self):
        from family_os.plugins.news_digest import analyzer

        old_soft = {"title": "十天前的旧软新闻", "source_name": "中国新闻网",
                    "url": "https://x/old", "published": "2026-10-14T00:00:00"}
        articles = [self._soft(n) for n in range(25)] + [old_soft]
        picked = analyzer._pick_select_candidates(articles)
        assert "十天前的旧软新闻" not in [a["title"] for a in picked]

    def test_select_prompt_receives_priority_item(self, tmp_path):
        from family_os.plugins.news_digest import analyzer

        prompts = []

        class Rec:
            def ask(self, prompt, model_tier="default", plugin_name="unknown", **kw):
                prompts.append(prompt)
                return '{"items":[]}'

        articles = [self._soft(n) for n in range(25)] + [self._priority(1)]
        analyzer._stage_select(_mk_ctx(tmp_path, Rec()), articles)
        assert "社科院研究成果1" in prompts[0]


class TestSourceDisplayNames:
    """sources.yaml 的 name 是「展示出处」，不得带抓取层技术标记。

    历史问题：`财新·最新(RSSHub镜像)`、`中国新闻网-滚动` 这类名字直接进了
    微信推送和网页，读者看到的是管道细节而非新闻出处。已在配置层改净。
    """

    def _names(self) -> list[str]:
        import yaml
        from pathlib import Path

        p = Path(__file__).resolve().parents[2] / "family_os" / "config" / "sources.yaml"
        data = yaml.safe_load(p.read_text(encoding="utf-8"))
        return [s["name"] for s in data["sources"]]

    def _ids(self) -> set[str]:
        import yaml
        from pathlib import Path

        p = Path(__file__).resolve().parents[2] / "family_os" / "config" / "sources.yaml"
        data = yaml.safe_load(p.read_text(encoding="utf-8"))
        return {s["id"] for s in data["sources"]}

    def test_no_tech_markers_in_names(self):
        bad = [n for n in self._names() if "RSSHub" in n or "镜像" in n or "(英文)" in n]
        assert bad == [], f"来源名仍含技术标记：{bad}"

    def test_no_feed_level_suffix(self):
        """-滚动 / ·最新 属 feed 类型，不是媒体名。"""
        bad = [n for n in self._names() if n.endswith("-滚动") or n.endswith("·最新")]
        assert bad == [], f"来源名仍含 feed 级后缀：{bad}"

    def test_known_outlets_resolve_to_clean_name(self):
        names = self._names()
        for want in ("新华社", "观察者网", "工信部", "中国新闻网"):
            assert want in names, f"缺少干净的出处名：{want}"
        # 财新是订阅制媒体，公开版会剔除；仅当该源存在时才校验名称
        if "rsshub_caixin" in self._ids():
            assert "财新" in names, "缺少干净的出处名：财新"

    def test_editorial_sections_preserved(self):
        """编辑栏目（-财经/-时政/-国际）是出处的一部分，不能被一起抹掉。"""
        names = self._names()
        for keep in ("中国新闻网-财经", "中国新闻网-时政", "中国新闻网-国际"):
            assert keep in names, f"编辑栏目被误删：{keep}"