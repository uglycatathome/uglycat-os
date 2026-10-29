"""analyzer.py —— news_digest 的 LLM 分析 pipeline。

4 阶段（全部走降级封装 _ask_with_fallback）：
  0 提取特征措辞 → 1 对照方法论解码 → 2 与历史文本对比 → 3 老百姓一句话建议
模型链配置于 config.llm.news_digest_model + fallback_models（免费模型）。
"""

import json
import logging
import re

from family_os.kernel.context import Context

from . import decode

logger = logging.getLogger("family_os.news_digest.analyzer")

PLUGIN = "news_digest"

# 党和国家重要会议登记表（2026-10-10 按用户提供的完整清单结构化）。
# 每项：(名称正则, 归档 key, 规范会议名)。
#   - 归档 key：会议概要与措辞按 key+日期 存入 meeting_archive，供后续同主题对比。
# 2026-10-10：移除"是否保底入选"标记——不再有任何规则保底强制入选机制，
# 会议是否入选完全由 LLM 精选（提示词已把重要会议列为必选关注重点）决定；
# 本表仅用于【归档】，命中即入库供后续同主题措辞对比。
# 正则要求命中会议全称/特征词，避免「全会」等宽泛子串误伤一般新闻。
MEETING_REGISTRY = [
    # ── 五年周期：党代会与中央全会序列 ──────────────────────────────────
    (r"中国共产党第[一二三四五六七八九十]+次全国代表大会|党的[一二三四五六七八九十]+大",
     "dangdaihui", "党的全国代表大会"),
    (r"[一二三四五六七八九十]+届中央委员会第[一二三四五六七八九十]+次全体会议|"
     r"[一二三四五六七八九十]+中全会",
     "quanhui", "中央委员会全体会议"),
    # ── 年度：中央纪委全会 ─────────────────────────────────────────────
    (r"中央纪律检查委员会第[一二三四五六七八九十]+次全体会议|"
     r"[一二三四五六七八九十]+届中央纪委第[一二三四五六七八九十]+次全会|"
     r"中央纪委[一二三四五六七八九十]+次全会",
     "jiwei", "中央纪委全体会议"),
    # ── 年度：全国两会 ─────────────────────────────────────────────────
    (r"全国人民代表大会|全国人大会议|全国政协全体会议|"
     r"全国政协[一二三四五六七八九十]{1,3}届[一二三四五六七八九十]{1,3}次会议|"
     r"全国两会|政府工作报告",
     "lianghui", "全国两会"),
    (r"地方两会", "difang_lianghui", "地方两会"),
    # ── 年度：经济与三农部署节点 ───────────────────────────────────────
    (r"中央经济工作会议", "jingji", "中央经济工作会议"),
    (r"中央农村工作会议", "nongcun", "中央农村工作会议"),
    # ── 多月：中央政治局序列 ───────────────────────────────────────────
    (r"中央政治局常务委员会会议|政治局常委会会议",
     "zzjj_cwh", "中央政治局常委会会议"),
    (r"中央政治局会议|中共中央政治局会议", "zzjj", "中央政治局会议"),
    (r"政治局.{0,4}集体学习", "zzjj_jiti", "中央政治局集体学习"),
    # ── 多月：中央/国务院其他例会 ─────────────────────────────────────
    (r"中央财经委员会", "caijingwei", "中央财经委员会会议"),
    (r"中央全面深化改革委员会", "gaige", "中央全面深化改革委员会会议"),
    (r"国务院常务会议", "guowuyuan_changwu", "国务院常务会议"),
    (r"委员长会议", "weiyuanzhang", "全国人大常委会委员长会议"),
    (r"全国人大常委会.{0,8}会议|全国人民代表大会常务委员会",
     "rdcwh", "全国人大常委会会议"),
    (r"全国政协主席会议", "zhuxi", "全国政协主席会议"),
    (r"全国政协常务委员会会议|全国政协常委会会议", "zxcwh", "全国政协常委会会议"),
    (r"双周协商座谈会", "shuangzhou", "全国政协双周协商座谈会"),
    # ── 事件型高规格会议（不定期） ────────────────────────────────────
    (r"中央外事工作会议", "waishi", "中央外事工作会议"),
    (r"中央城市工作会议", "chengshi", "中央城市工作会议"),
    (r"中央金融工作会议", "jinrong", "中央金融工作会议"),
    (r"中央人才工作会议", "rencai", "中央人才工作会议"),
    (r"中央民族工作会议", "minzu", "中央民族工作会议"),
]

# 代码级「近期同主题」粗筛阈值（2026-10-09 由 0.7 提到 0.9）。
# 代码去重已降级为【粗筛】：只拦近乎完全相同的标题；真正的主题级语义去重
# 由独立的 LLM 去重阶段完成（见 _stage_dedup）。
RECENT_SELECT_THRESHOLD = 0.9


def _meeting_match(text: str):
    """文本命中会议登记表 → (归档 key, 规范会议名)，否则 None。

    仅用于【归档】（命中即入库供同主题措辞对比），不再参与任何强制入选。
    """
    for pat, key, name in MEETING_REGISTRY:
        if re.search(pat, text or ""):
            return key, name
    return None


def _meeting_key_for(title: str) -> str | None:
    """标题命中会议登记表任一规则 → 返回归档 key，否则 None。"""
    hit = _meeting_match(title)
    return hit[0] if hit else None


def _purified_text(item: dict, stage1: str) -> str:
    """提纯精简文本：标题/来源/提纯摘要/重要性/措辞解读，供本地化入库对比。"""
    lines = [
        f"【标题】{(item.get('title') or '').strip()}",
        f"【来源】{(item.get('source_name') or '').strip()}",
    ]
    if item.get("summary"):
        lines.append(f"【提纯】{' '.join(item['summary'].split())}")
    if item.get("reason"):
        lines.append(f"【重要性】{' '.join(item['reason'].split())}")
    if stage1:
        lines.append(f"【措辞解读】{' '.join(stage1.split())[:400]}")
    return "\n".join(l for l in lines if l.strip())


def _auto_store_meetings(ctx: Context, run_date: str, articles: list[dict],
                         selected: list[dict], stage1: str) -> None:
    """会议概要与措辞归档（2026-10-10 扩展至全部登记会议）。

    凡标题命中 MEETING_REGISTRY 的会议新闻，均按 会议key+日期 提纯入库
    （概要必存；措辞解读仅当该会议被选入精选时随 stage1 一并存入）。
    同 key+日期 去重、永留存，供后续同主题措辞对比分析。
    """
    from . import storage

    selected_titles = {(it.get("title") or "").strip() for it in (selected or [])}
    for a in articles:
        hit = _meeting_match(a.get("title") or "")
        if not hit:
            continue
        key, name = hit
        item = {
            "title": a.get("title", ""),
            "source_name": a.get("source_name", ""),
            "summary": " ".join((a.get("summary") or a.get("body") or "").split())[:160],
            "reason": f"重要会议·{name}",
        }
        in_sel = (a.get("title") or "").strip() in selected_titles
        try:
            storage.ingest_meeting_text(
                ctx.config.app.data_dir, key, run_date,
                _purified_text(item, stage1 if in_sel else ""))
            logger.info("news_digest 会议归档 %s/%s（%s）", key, run_date,
                        "含措辞" if in_sel else "仅概要")
        except Exception as e:  # noqa: PERF203
            logger.warning("news_digest 会议归档失败 %s: %s", key, e)


def run_pipeline(ctx: Context, articles: list[dict],
                 meeting_archive: dict, run_dir,
                 recent_topics=None) -> dict:
    """执行 pipeline，返回 {selected, select_mode, stage0..3}。

    2026-10-10 流水线拆分（用户要求「筛选与 LLM 去重分开」）：
      ① 代码粗筛：剔除与近期已推送主题【近乎完全相同标题】的候选（兜底）；
      ② LLM 去重（_stage_dedup，独立调用）：主题级【语义】去重，剔除与近期
         已推送主题同主题的候选（含「同一政策不同写法」）；
      ③ 筛选（_stage_select，独立调用）：从去重后的候选中精选【政策性大新闻】
         ≤3 条，六类并列重点——中美货币政策、党和国家重要会议、中央/国务院
         顶层政策文件、部委级重大政策/监管文件、重要经济数据发布、权威研究机构
         报告（2026-10-23 新增第六类：社科院及其研究所/国研中心/宏观院/NIFD 等）；
         （2026-10-13 起加【只认官方发布】硬门槛：非官方发布的媒体评论/深度
         报道/调查报道，及行业专项「整治/治理/打击」行动一律不选）
      ④ stage0~3：措辞提取/解码/历史对比/一句话建议；
      ⑤ 会议概要与措辞归档（命中登记表即入库，供后续对比）。
    2026-10-10：**移除全部规则保底**（原利率/会议/顶层文件/部委文件/经济数据
    五类强制入选）与「最新 N 条」凑数兜底——是否入选完全由 LLM 精选决定，
    五类重点只体现在精选提示词中；解析失败/无合格信息即空选（宁缺毋滥），
    空选时跳过 ④，mode="empty"，上层据此跳过当日推送。
    """
    # ① 代码粗筛（近乎完全相同标题）→ ② LLM 语义去重
    candidates = _filter_recent_topics(articles, recent_topics)
    deduped = _stage_dedup(ctx, candidates, recent_topics)
    if deduped is not None:
        candidates = deduped
    if not candidates:
        note = "今日新闻均与近期已推送主题重复，无新增量主题"
        return _finalize(ctx, run_dir, articles, [], "empty", note,
                         meeting_archive)

    # ③ 筛选（纯筛选，不含去重指令；是否入选完全由 LLM 决定）
    selected, select_note = _stage_select(ctx, candidates)
    if selected is None:
        # 2026-10-10：不做任何凑数兜底——解析失败等同"无合格信息"（宁缺毋滥）
        logger.warning("news_digest 精选解析失败，宁缺毋滥：不做凑数兜底")
        selected, select_note = [], "精选解析失败（宁缺毋滥）"

    mode = "empty" if not selected else "llm"
    return _finalize(ctx, run_dir, articles, selected, mode, select_note,
                     meeting_archive)


def _finalize(ctx: Context, run_dir, articles: list[dict], selected: list[dict],
              mode: str, note: str, meeting_archive: dict) -> dict:
    """跑 4 阶段 + 会议归档 + 落盘，返回统一结果 dict。"""
    matched = _match_selected(articles, selected) if selected else []
    stage0 = stage1 = stage2 = stage3 = ""
    market_impact = ""
    if mode != "empty":
        stage0 = _stage0_extract(ctx, decode.stage0_block(), matched)
        stage1 = _stage1_decode(ctx, decode.stage1_block(), stage0, matched)
        stage2 = _stage2_compare(ctx, decode.stage2_block(), stage0, meeting_archive,
                                 focus=stage1)
        stage3 = _stage3_advice(ctx, stage1, stage2, matched)
        # 利率类新闻（中国/美国降息加息、降准、LPR、美联储）额外补黄金/股市影响
        if matched and _is_monetary_news(matched):
            market_impact = _stage_market_impact(ctx, matched)
    # 会议概要与措辞归档（命中登记表即入库，本地化存储供后续对比）
    _auto_store_meetings(ctx, run_dir.name, articles, matched, stage1)

    result = {
        "selected": matched, "select_mode": mode, "selected_note": note,
        "stage0": stage0, "stage1": stage1, "stage2": stage2, "stage3": stage3,
        "market_impact": market_impact,
    }
    persist_result = {
        k: (json.dumps(v, ensure_ascii=False) if isinstance(v, list) else v)
        for k, v in result.items()
    }
    _persist(run_dir, persist_result)
    return result


# ── 精选阶段（宁缺毋滥，≤3 条） ─────────────────────────────────────────

def _has_official_wording(stage0: str) -> bool:
    """stage0 是否有真实可用的官方特征措辞（空/占位/非 JSON 均视为无）。"""
    s = (stage0 or "").strip()
    if not s or s.startswith("（"):
        return False
    try:
        return bool(json.loads(s))
    except Exception:
        return False


def _recent_titles(recent_topics) -> list[str]:
    """从近期主题条目（dict 或旧版纯标题字符串）中取出标题列表。"""
    out: list[str] = []
    for t in (recent_topics or []):
        title = (t.get("title") if isinstance(t, dict) else t) or ""
        title = title.strip()
        if title:
            out.append(title)
    return out


def _filter_recent_topics(articles: list[dict],
                          recent_topics) -> list[dict]:
    """粗筛：剔除与近期已推送主题【标题近乎完全相同】的候选。

    仅做字符串粗筛（高阈值 0.9），真正的主题级语义去重交给 LLM 精选
    （prompt 中列出近期主题，由其判断「同一政策不同写法」的同主题）。
    """
    titles = _recent_titles(recent_topics)
    if not titles:
        return articles
    from . import dedup

    return [a for a in articles
            if not dedup.is_dup(a.get("title") or "", titles,
                                RECENT_SELECT_THRESHOLD)]


def _recent_block(recent_topics) -> str:
    """构建「近期已推送主题」提示块：topic_key + 标题 + 一句话摘要。"""
    lines = []
    for t in (recent_topics or [])[:20]:
        if isinstance(t, str):
            t = {"title": t}
        title = (t.get("title") or "").strip()
        if not title:
            continue
        key = (t.get("topic_key") or "").strip()
        summary = " ".join((t.get("summary") or "").split())[:60]
        tag = f"[{key}] " if key else ""
        lines.append(f"- {tag}{title}" + (f" —— {summary}" if summary else ""))
    if not lines:
        return ""
    return (
        "\n\n## 近期已推送主题（语义去重，勿重复精选）\n"
        "下面这些主题最近 7 天已经推送过。请**逐条语义比对**当日候选新闻："
        "凡与下列主题属于**同一件事/同一政策**的（哪怕标题写法完全不同，"
        "例如「房贷贴息」与「购房贷款贴息」「买房国补」是同一主题），"
        "一律【不要】再选；\n"
        "只有当出现【重大实质新进展】（政策正式落地/细则出台/金额与范围明确/"
        "重要数据公布等）时才可再选，并在 reason 末尾标注「（同主题·新进展）」，"
        "topic_key 沿用下方括号里的标签。\n"
        + "\n".join(lines)
    )


def _stage_dedup(ctx, articles,
                 recent_topics) -> list[dict] | None:
    """LLM 主题级【语义去重】（独立于筛选的单独 LLM 调用）。

    输入粗筛后的候选 + 近期已推送主题（topic_key/标题/摘要），让 LLM 语义判断
    哪些候选与近期主题属于同一件事/同一政策（哪怕标题写法完全不同，如「房贷
    贴息」与「购房贷款贴息」「买房国补」），返回应剔除的候选序号。保留「重大
    实质新进展」出口（由 LLM 判定后不剔除）。

    返回剔除后的候选列表；无近期主题或解析失败返回 None（上层保持原候选并仅
    依赖代码粗筛兜底）。
    """
    if not articles or not recent_topics:
        return None
    recent_block = _recent_block(recent_topics)
    if not recent_block:
        return None
    listing = "\n".join(
        f"{i}. [{a.get('source_name', '')}] {a.get('title', '')}\n"
        f"   {(a.get('summary') or a.get('body') or '')[:100]}"
        for i, a in enumerate(articles))
    prompt = (
        "任务：主题级去重。下面是「今日候选新闻」与「近期已推送主题」。请判断今日"
        "候选中有哪些与近期已推送主题属于【同一件事/同一政策】（哪怕标题写法完全"
        "不同，例如「房贷贴息」与「购房贷款贴息」「买房国补」是同一主题），把它们"
        "列为应剔除项。\n"
        "规则：\n"
        "①只剔与近期主题【同主题】的候选；今日候选彼此之间的重复不在这里处理。\n"
        "②若某候选虽属同主题，但包含【重大实质新进展】（政策正式落地/细则出台/"
        "金额与范围明确/重要数据公布等），则【保留】，不剔除。\n"
        "③宁缺毋滥：只有确实同主题才剔除，拿不准就保留。\n"
        "输出 JSON：{\"drop\":[{\"index\":3,\"topic_key\":\"房贷贴息\","
        "\"reason\":\"与近期《…》同主题且无新进展\"}]}；无需剔除则 {\"drop\":[]}。"
        "只输出 JSON，不要输出其他文字。\n"
        f"{recent_block}\n"
        f"## 今日候选新闻（index 从 0 起）\n{listing}"
    )
    try:
        raw = _ask_with_fallback(ctx, prompt)
        drop = _stage_dedup_parse(raw)
        if drop is None:
            logger.warning("news_digest 去重解析失败，重试一次: %s", (raw or "")[:80])
            raw = _ask_with_fallback(ctx, prompt)
            drop = _stage_dedup_parse(raw)
        if drop is None:
            return None
        kept = [a for i, a in enumerate(articles) if i not in drop]
        if len(kept) != len(articles):
            logger.info("news_digest LLM 语义去重剔除 %d 条同主题候选",
                        len(articles) - len(kept))
        return kept
    except Exception as e:  # noqa: PERF203
        logger.warning("news_digest 去重 LLM 调用失败: %s", str(e)[:120])
        return None


def _stage_dedup_parse(raw: str) -> set[int] | None:
    """解析去重 JSON → 应剔除的候选 index 集合；结构异常返回 None。"""
    base = _as_json(raw, "stage_dedup")
    if not base:
        return None
    try:
        obj = json.loads(base)
    except Exception:
        return None
    if not isinstance(obj, dict) or not isinstance(obj.get("drop"), list):
        return None
    out: set[int] = set()
    for it in obj["drop"]:
        idx = it.get("index") if isinstance(it, dict) else it
        try:
            out.add(int(idx))
        except (TypeError, ValueError):
            continue
    return out


# 精选输入窗口：只取「最新 N 条」会被国际冲突/体育/养生类软新闻挤满，
# 导致低频研究机构源（社科院/求是）永远进不了 LLM 的视野。
# 2026-10-24 实测：当日 194 篇中 13 篇来自社科院/求是，最新 20 条里 0 篇，
# 模型因此看不到它们而判「今日无合格新闻」。
# 因此：保留最新 SELECT_WINDOW 条，并额外补入优先源最新 PRIORITY_QUOTA 条。
PRIORITY_SOURCE_KEYWORDS = ("社科院", "求是")
SELECT_WINDOW = 20
PRIORITY_QUOTA = 6
# 2026-10-24 二次修复：只按「最新」取样，会把前一天的重要政策文件挤出视野——
# 实测 10-23 国办《关于…就业友好型发展方式的意见》被 10-24 的软新闻挤出最新 20 条，
# 模型因此看不到它。这里按【标题政策信号词】再补一批，不受时间排序限制。
POLICY_TITLE_KEYWORDS = (
    "中共中央", "国务院", "中办", "国办", "印发", "央行", "人民银行", "财政部",
    "发改委", "工信部", "商务部", "住建部", "证监会", "金融监管", "统计局",
    "海关总署", "CPI", "PPI", "GDP", "PMI", "社融", "存款准备金率", "降准",
    "降息", "加息", "LPR", "美联储", "FOMC", "议息", "联邦基金利率",
    "进出口", "出口", "失业率", "房价", "经济数据",
)
POLICY_QUOTA = 20


def _is_priority_source(article: dict) -> bool:
    name = article.get("source_name", "") or ""
    return any(k in name for k in PRIORITY_SOURCE_KEYWORDS)


def _has_policy_cue(article: dict) -> bool:
    title = article.get("title", "") or ""
    return any(k in title for k in POLICY_TITLE_KEYWORDS)


def _pick_select_candidates(articles: list[dict],
                            limit: int = SELECT_WINDOW,
                            quota: int = PRIORITY_QUOTA) -> list[dict]:
    """精选阶段的输入候选（按 URL 去重）：

    ① 最新 ``limit`` 条；② 优先源（社科院/求是）额外 ``quota`` 条；
    ③ 标题命中政策信号词的条目再补 ``POLICY_QUOTA`` 条（防前一天政策文件被挤出）。
    """
    ordered = sorted(articles, key=lambda x: x.get("published") or "", reverse=True)
    picked = list(ordered[:limit])
    seen = {a.get("url") for a in picked}
    extra = [a for a in ordered[limit:]
             if _is_priority_source(a) and a.get("url") not in seen][:quota]
    seen |= {a.get("url") for a in extra}
    policy = [a for a in ordered[limit:]
              if a.get("url") not in seen and _has_policy_cue(a)][:POLICY_QUOTA]
    return picked + extra + policy


def _stage_select(ctx, articles) -> tuple[list[dict] | None, str]:
    """LLM 从【已去重】的当日候选中精选真正的【政策性大新闻】（≤3 条，宁缺毋滥）。

    2026-10-10：去重已拆为独立的 _stage_dedup 阶段，本阶段只做【筛选】。
    2026-10-13：新增【只认官方发布】硬门槛——只认中央/国务院/部委等官方机构
    正式发布的文件/公告/公报/数据，并由 LLM 先核对「发布主体 + 文种」；媒体评论、
    深度报道、调查报道、专栏及行业专项「整治/治理/打击」行动一律不选（此前
    『整治付费实习』类媒体深度报道被误当政策大新闻入选）。
    筛选六类并列重点（2026-10-23 起新增第六类「权威研究机构报告」）：
      ① 中国/美国的加息降息与货币政策（降息/降准/LPR/MLF/逆回购/美联储议息…）；
      ② 党和国家重要会议及其公报（两会/政治局会议/中央经济工作会议/全会…）；
      ③ 中共中央/国务院顶层政策文件（意见/决定/规划/纲要/条例/方案/通知…）；
      ④ 部委级重大政策/监管文件（证监会/财政部/发改委/央行…的办法/细则/规定）；
      ⑤ 重要经济数据发布（CPI/PPI/GDP/PMI、社融/M2、进出口、70 城房价、失业率）；
      ⑥ 国家级研究机构（社科院及其研究所/国研中心/宏观院/NIFD/国经中心等）正式
         发布的报告、蓝皮书、年度预测、指数。
    明确排除媒体评论/深度报道与地方民生琐事、软新闻、行业专项治理行动。

    返回 (items, note)：
      - items 为 list 表示解析成功（空 list=今日确无重要政策新闻，note 给理由）；
      - items 为 None 表示解析失败，上层视为空选（不做凑数兜底，宁缺毋滥）。
    """
    if not articles:
        return [], ""
    limited = "\n\n".join(
        f"[{a.get('source_name', '')}] {a.get('title', '')}\n"
        f"{(a.get('summary') or a.get('body') or '')[:150]}"
        for a in _pick_select_candidates(articles)
    )
    prompt = (
        "任务：从下列当日新闻中，精选出真正的【政策性大新闻】，宁缺毋滥。\n"
        "【只认官方发布】入选前必须先确认这条是【官方机构正式发布】，而非媒体自采：\n"
        "  ①能指出明确发布主体（中共中央/国务院/中办国办/某部委/国家统计局/央行/"
        "海关总署等）；②文种明确（意见/决定/规划/纲要/条例/办法/规定/方案/通知/"
        "公告/公报/统计发布等）。\n"
        "  媒体评论、深度报道、调查报道、记者观察、专栏文章、媒体转载评论，即便标题"
        "带「整治/治理/打击/规范」等官方口吻词，也【一律不选】——那只是媒体解读，"
        "不是官方发布。\n"
        "  【例外·权威研究机构】国家级研究机构的正式研究成果也视为有效发布：中国社科院"
        "（含各研究所）、国务院发展研究中心、中国宏观经济研究院、国家金融与发展实验室、"
        "中国国际经济交流中心等；文种为研究报告/蓝皮书/年度报告/指数/预测/白皮书。"
        "媒体对这些报告的转述、评论不单独入选，除非能确认系机构自身发布。\n"
        "【行业专项治理不选】单纯的行业专项「整治/治理/清理/打击/规范」行动（如整治"
        "『付费实习』灰色产业链、打击某类乱象、专项执法行动），除非是中共中央/国务院"
        "直接部署的全国性重大行动，否则【不选】；不得因标题出现「整治/打击」等词就当"
        "作政策大新闻。\n"
        "【重点一·中美货币政策】中国的降息/降准/加息、LPR、存款准备金率、"
        "货币政策取向；美国的加息/降息、美联储议息会议、联邦基金利率、点阵图等。\n"
        "【重点二·党和国家重要会议】全国两会、中央政治局会议、中央经济工作会议、"
        "中央农村工作会议、中央全会与党的全国代表大会、中央纪委全会、中央财经"
        "委员会会议、国务院常务会议等重大会议及其公报/决定。\n"
        "【重点三·中共中央/国务院顶层政策文件】中共中央、国务院（含中办/国办）"
        "印发的意见、决定、规划、纲要、条例、方案、行动计划、通知等重大文件，"
        "对经济有全局指向作用。如《中共中央 国务院关于发展新质生产力的意见》"
        "《关于促进民营经济发展壮大的意见》、中央一号文件等。**此类文件标题常较"
        "平淡、摘要简短，但级别极高，务必优先入选，不要漏掉。**\n"
        "【重点四·部委级重大政策/监管文件】证监会、财政部、发改委、工信部、"
        "商务部、住建部、央行、金融监管总局、市场监管总局、国资委等部委正式印发的"
        "【制度性/全局性重大文件】（管理办法、条例、规定、细则、实施方案等），"
        "对行业与市场有直接指向作用。**此类文件标题平淡但级别很高，务必优先入选；"
        "但一般性工作部署、风险提示、日常执法与专项治理通知不算重大，不选。**\n"
        "【重点五·重要经济数据发布】CPI/PPI/GDP/PMI、社融/M2/新增人民币贷款、"
        "进出口、70 城房价、失业率、工业增加值、固定资产投资、社会消费品零售等"
        "由国家统计局/央行/海关总署等发布的月度/季度【全国口径】数据。**这类新闻"
        "标题朴素、常被漏选，但直接关系市场温度与利率预期，务必优先入选。**\n"
        "【重点六·权威研究机构报告】中国社科院及其研究所（经济所/工经所/数技经所等）、"
        "国务院发展研究中心、中国宏观经济研究院、国家金融与发展实验室、"
        "中国国际经济交流中心等国家级智库正式发布的报告、蓝皮书、年度预测、指数。"
        "**这类内容标题朴素、常被媒体转述，务必核对是否为机构自身发布；确认是机构"
        "发布的，即便属研究报告而非政策文件，也应优先入选。**\n"
        "以上六类为【必选关注重点】：只要出现就优先入选（仍须先满足「只认官方发布」）。\n"
        "【其他·政策大新闻】中央/国务院/部委顶层部署的全局性重大政策（财政/楼市/"
        "外贸/产业/就业/消费/社保等）。民生领域一般性监管、治理、执法、服务提醒"
        "报道不算，不选。\n"
        "【明确排除】媒体评论/深度报道/调查报道，以及地方民生琐事与软新闻："
        "治安/诈骗案件、交通/停车、天气灾害、旅游打卡、社区活动、文体娱乐、企业"
        "一般经营动态、单纯人物报道、生活服务提醒等，一律【不选】。\n"
        "【明确排除·非政策类】单纯基建工程节点（开工/通车/铺轨/竣工/贯通/试运行/"
        "验收）、纪念活动及纪念出版物/邮票/纪念币/文艺作品发行、体育赛事、文艺演出、"
        "科技装置的工程进度（功率/工期/装机量等）——除非同一条同时包含新的国家政策"
        "或重要经济数据，否则【不选】。\n"
        "【党建/理论学习不选】习近平重要文章、党建思想、全面从严治党、党内政治生活、"
        "主题教育/理论学习/宣讲/研讨交流，以及机构学者的署名理论文章（如『机理建构』"
        "『范式转换』这类学术阐释）——除非同一条同时包含新的政策行动、正式文件或"
        "重要经济数据，否则【不选】。\n"
        "【活动/会议/展会不选】论坛、峰会、博览会、展会、电影周/电影节、艺术节、"
        "颁奖、启动仪式、座谈会、调研考察、赛事、发布会（非政策发布）——一律【不选】，"
        "无论规格多高、主题是否涉及 AI 或产业。"
        "（例外：中央政治局会议、国务院常务会议、全国两会等党和国家重要会议属于必看"
        "大事，不受此条限制。）\n"
        "【媒体栏目/评论不选】标题带「面面观/观察家/深度观察/述评/评论员/专访/图解/"
        "数说/盘点/记者手记」等栏目或体裁字样的，是媒体二次加工，非官方发布，一律【不选】。\n"
        "【只认国家级】省级/城市级的地方数据、地方政策、地方会议、地方法规一律【不选】"
        "（如「浙江前三季度GDP」「某市楼市新政」「某省人大常委会公告」），因为对全国"
        "读者没有普遍指导意义；只有中共中央/国务院/国家部委的全国性发布才算。\n"
        "【技术性操作不选】央行「新设/开展某项工具或操作」类新闻（互换便利、逆回购、"
        "MLF、SLF、再贷款、再贴现、公开市场操作、央票等），若未同时包含降息/降准/"
        "加息/LPR 等利率决策，属于技术性流动性安排，普通人不关心，一律【不选】。\n"
        "【每条都必须独立达标】选 2~3 条时，每一条都要各自满足「正式文件 或 权威发布"
        "主体 或 重要经济数据」；只要有一条达不到，就只放达标的那几条，宁少不多。\n"
        "【同一事件只选一条】当日若有多条新闻讲的其实是同一件事（不同媒体重复"
        "报道），只选其中一条。\n"
        "【数量·大事可放 3 条】先判断每条是否属于【同级大事】：中央/国务院及部委正式"
        "文件、中美货币政策（降息/加息/降准/LPR/美联储议息）、关键经济数据（CPI/PPI/GDP/"
        "PMI/出口/社融）、重大监管或楼市税收政策、重大产业政策。\n"
        "  · 当日只有 1 条够格 → 只放 1 条；\n"
        "  · 有 2~3 条【互不重复】且都够格 → 全部放出来（2~3 条），不要压成 1 条；\n"
        "  · 超出 3 条时按重要性取前 3；日常琐事、活动安排、工作部署一律不占名额。\n"
        "严禁为凑数把非同级的软新闻塞进来，最多 3 条；"
        "若全是软新闻/日常琐事，则 items 为空数组，并用 note 字段一句话说明理由。\n"
        "每条给出：title（与原文标题一致）、summary（2~3 句说人话的摘要，说明对"
        "老百姓意味着什么）、reason（一句话：为什么今天它最重要）、topic_key"
        "（3~8 字简短主题标签，如「房贷贴息」「降准」「美联储降息」，用于跨天去重）。"
        "若无法确认某条是官方正式发布（看不出发布主体或文种），则不要选它。\n"
        "输出 JSON：{\"items\":[{\"title\":\"...\",\"summary\":\"...\","
        "\"reason\":\"...\",\"topic_key\":\"...\"}]}。"
        "只输出 JSON，不要输出其他文字。\n"
        f"## 当日新闻\n{limited}"
    )
    try:
        raw = _ask_with_fallback(ctx, prompt)
        items, note = _stage_select_parse(raw), _stage_select_note(raw)
        if items is None:
            # 解析失败重试一次（与 stage0 同策略）
            logger.warning("news_digest 精选解析失败，重试一次: %s", (raw or "")[:80])
            raw = _ask_with_fallback(ctx, prompt)
            items, note = _stage_select_parse(raw), _stage_select_note(raw)
        # 硬门槛：剔除活动/栏目评论/无官方出处的条目（LLM 会自我合理化选入）
        if items:
            qualified = [it for it in items if _is_qualified_news(it)]
            if len(qualified) < len(items):
                logger.warning("news_digest 精选剔除不合格条目 %d/%d",
                               len(items) - len(qualified), len(items))
            items = qualified
        # 硬上限：无论模型输出多少，最多保留 3 条（默认 1 条由提示词约束）
        if items is not None and len(items) > 3:
            items = items[:3]
        return items, note
    except Exception as e:  # noqa: PERF203
        logger.warning("news_digest 精选 LLM 调用失败: %s", str(e)[:120])
        return None, ""


# ── 精选硬门槛（2026-10-26）────────────────────────────────────────────
# 实测 LLM 会自我合理化，把「栏目评论」「电影节活动」当政策新闻选进来。
# 这里用确定性规则兜底：活动类一律剔除；标题若是媒体栏目/评论体裁则剔除；
# 余下必须能被证明是「官方文件 / 权威发布主体 / 重要经济数据」。
# 活动/事件名词：出现即视为活动类新闻（论坛、展会、电影节、发布会等）。
# 注意不要放「召开/举办」等泛动词——「中共中央政治局召开会议」是顶层会议，
# 属于必看新闻，泛动词会把这类标题误杀（2026-10-26 修正）。
_EVENT_RE = re.compile(
    r"论坛|峰会|展会|交易会|博览会|广交会|进博会|服贸会|高交会|消博会|"
    r"电影周|电影节|艺术节|颁奖|启动仪式|发布会|"
    r"赛事|开赛|展演|巡展|研讨班|培训班|开幕式|闭幕式"
)
# 走访调研类活动（无正式文件词时剔除）
_ACTIVITY_RE = re.compile(r"调研|考察|走访|座谈")
# 央行技术性流动性操作（非利率决策）——普通人不关心，不算重大政策
_MONETARY_OP_RE = re.compile(
    r"互换便利|逆回购|中期借贷便利|MLF|SLF|再贷款|再贴现|常备借贷|国库现金|"
    r"公开市场操作|央票|正回购|流动性投放|流动性支持|流动性工具|流动性安排"
)
_MAJOR_MONETARY_RE = re.compile(r"降息|加息|降准|存款准备金率|LPR|利率下调|利率上调")
# 地方/省级主体（配合数据词使用：地方数据对全国读者无普遍意义）
_LOCAL_RE = re.compile(
    r"北京|天津|上海|重庆|河北|山西|辽宁|吉林|黑龙江|江苏|浙江|安徽|福建|江西|"
    r"山东|河南|湖北|湖南|广东|海南|四川|贵州|云南|陕西|甘肃|青海|台湾|"
    r"内蒙古|广西|西藏|宁夏|新疆|香港|澳门|深圳|广州|杭州|成都|武汉|南京|苏州"
)
# 全国性主体：出现即认为地方名只是修饰（如「全国人大」「国家统计局」）
_NATIONAL_ACTOR_RE = re.compile(r"中共中央|国务院|国家|全国|中央")
# 全国口径的重要经济数据（地方/行业自报数据不算）
_NATIONAL_DATA_RE = re.compile(
    r"CPI|PPI|GDP|PMI|社融|M2|失业率|房价|工业增加值|社会消费品零售|进出口|"
    r"居民消费价格|采购经理|人民币贷款|外汇储备|财政收入"
)
_MEDIA_COLUMN_RE = re.compile(
    r"面面观|观察家|观察｜|观察\||观察：|深观察|观察之|述评|评论员|评论｜|专访|"
    r"图解|数说|视点|一线见闻|盘点|热点问答|记者手记|外媒"
)
_DOC_WORDS = (
    "意见", "通知", "办法", "条例", "规定", "决定", "规划", "方案", "公告",
    "批复", "细则", "若干措施", "规则", "纲要", "立法", "法（", "税法",
)
_OFFICIAL_BODIES = (
    "中共中央", "国务院", "办公厅", "发改委", "央行", "人民银行", "财政部",
    "统计局", "海关总署", "工信部", "商务部", "住建部", "人社部", "证监会",
    "金融监管总局", "市场监管总局", "税务总局", "网信办", "教育部", "卫健委",
    "国资委", "能源局", "医保局", "交通运输部", "水利部", "农业农村部",
    # 外交部例行表态属外交辞令，不作为「重大政策」依据，故不列入
)



def _is_qualified_news(item: dict) -> bool:
    """判断精选条目是否够格（活动/栏目评论/来源不明的软新闻一律不够格）。

    判定顺序（前者优先）：
      ① 媒体栏目/评论体裁 → 剔除；
      ② 论坛/峰会/展会/电影节/发布会等活动名词 → 剔除；
      ③ 党和国家重要会议（登记表命中）→ 保留（含「政治局召开会议」等标题）；
      ④ 标题带正式文件词 → 保留；
      ⑤ 央行技术性流动性操作（无降息/降准/加息/LPR）→ 剔除；
      ⑥ 地方/省级数据（浙江GDP 等）→ 剔除；
      ⑦ 调研/考察/走访/座谈等走访类活动 → 剔除；
      ⑧ 权威发布主体只在标题中才算数 / 全国性经济数据 → 保留；其余剔除。
    """
    title = (item.get("title") or "").strip()
    summary = (item.get("summary") or "").strip()
    if not title:
        return False
    text = f"{title} {summary}"
    if _MEDIA_COLUMN_RE.search(title):
        return False
    if _EVENT_RE.search(text):
        return False
    if _meeting_match(title):
        return True
    # 地方主体：地方数据/政策/公告/规划对全国读者无普遍意义（除非全国性主体发文）
    if _LOCAL_RE.search(title) and not _NATIONAL_ACTOR_RE.search(title):
        return False
    if any(w in title for w in _DOC_WORDS):
        return True
    if _MONETARY_OP_RE.search(title) and not _MAJOR_MONETARY_RE.search(text):
        return False
    if _ACTIVITY_RE.search(text):
        return False
    # 权威主体只认「标题里写明的发布方」——摘要里提到「央行/发改委」只是转述，
    # 不足以证明是原始发布（今日「央行新设互换便利」即此类媒体转述）。
    if any(b in title for b in _OFFICIAL_BODIES):
        return True
    if _MAJOR_MONETARY_RE.search(title):
        return True
    return bool(_NATIONAL_DATA_RE.search(title))


def _stage_select_note(raw: str) -> str:
    """提取顶层 note（空选时的理由）；结构异常返回空串。"""
    base = _as_json(raw, "stage_select_note")
    if base:
        try:
            obj = json.loads(base)
            note = obj.get("note") if isinstance(obj, dict) else ""
            return str(note).strip()[:80] if isinstance(note, str) else ""
        except Exception:
            pass
    return ""


def _stage_select_parse(raw: str) -> list[dict] | None:
    """解析精选 JSON；结构异常返回 None，空 items 返回 []。"""
    base = _as_json(raw, "stage_select")
    if base:
        try:
            obj = json.loads(base)
            if isinstance(obj, dict) and isinstance(obj.get("items"), list):
                return [it for it in obj["items"]
                        if isinstance(it, dict) and (it.get("title") or "").strip()]
        except Exception:
            pass  # 落入截断恢复
    import re

    items: list[dict] = []
    for m in re.finditer(r"\{[^{}]*\"title\"[^{}]*\}", raw or ""):
        try:
            obj = json.loads(m.group(0))
        except Exception:
            continue
        if isinstance(obj, dict) and (obj.get("title") or "").strip():
            items.append({k: v for k, v in obj.items()
                          if k in ("title", "summary", "reason", "topic_key")})
    return items or None


def _match_selected(articles: list[dict], selected: list[dict]) -> list[dict]:
    """把精选 title 匹配回原始文章，附加 url/source_name/body 等字段。"""
    def _score(a: dict, t: str) -> int:
        at = (a.get("title") or "").strip()
        if at == t:
            return 3
        if at and (at in t or t in at):
            return 2
        if at[:10] and (at.startswith(t[:10]) or t.startswith(at[:10])):
            return 1
        return 0

    matched = []
    for it in selected:
        t = (it.get("title") or "").strip()
        best = max(articles, key=lambda a: _score(a, t), default=None)
        if best is not None and _score(best, t) > 0:
            m = dict(it)
            for k in ("url", "source_name", "body", "summary", "published"):
                if best.get(k):
                    m.setdefault(k, best[k])
            matched.append(m)
        else:
            matched.append(dict(it))
    return matched


# ── 阶段实现 ─────────────────────────────────────────────────────────────

def _stage0_extract(ctx, block, articles) -> str:
    limited = "\n\n".join(
        f"[{a.get('source_name','')}] {a.get('title','')}\n{a.get('body') or a.get('summary','')[:200]}"
        for a in articles[:15]
    ) or "（今日无新闻）"
    prompt = (
        f"{block}\n\n"
        "任务：从下列当日新闻中提取『官方特征措辞』与『黑话』，聚焦标题、政策表述、"
        "强调词、措辞强度。输出 JSON："
        "{\"items\":[{\"thread\":\"主题归类，如财政/货币/外贸/民生/楼市/产业\","
        "\"key_phrases\":[\"...\",\"...\"],\"source\":\"来源\",\"date\":\"日期\"}]}。"
        "若无官方措辞则 items 为空数组。只输出 JSON，不要输出任何其他文字。\n\n"
        f"## 当日新闻\n{limited}"
    )
    raw = _ask_with_fallback(ctx, prompt)
    parsed = _stage0_parse(raw)

    # 提取失败（无法得到合法 JSON、或 key_phrases 全空但模型给了一段文本）→ 重试一次
    if not parsed:
        logger.warning("stage0 提取结果异常，重试一次: %s", (raw or "")[:80])
        raw = _ask_with_fallback(ctx, prompt)
        parsed = _stage0_parse(raw)

    # 2026-10-24：GLM 主/降级模型遇 1305 限流时会回「{"items":[]}」之类的合法空结果，
    # _ask_with_fallback 只在抛异常时降级、不会因「空结果」换模型 → 直接用链末模型兜底。
    if not parsed:
        fallbacks = list(ctx.config.llm.news_digest_fallback_models or [])
        if fallbacks:
            try:
                logger.warning("stage0 连续两次为空 → 用链末模型 %s 兜底", fallbacks[-1])
                raw = ctx.llm_gateway.ask(
                    prompt=prompt, model_tier=fallbacks[-1], plugin_name=PLUGIN)
                parsed = _stage0_parse(raw)
            except Exception as e:
                logger.warning("stage0 链末兜底失败: %s", str(e)[:100])

    # 有可用措辞（key_phrases 非空）→ 原样返回列表 JSON 供 stage1 消费
    if parsed:
        return json.dumps(parsed, ensure_ascii=False)
    # 确无措辞或重试后仍失败：返回明确占位，避免 stage1 被误导去“补充示例”
    return "（今日未提取到明确的官方特征措辞：或新闻无显著政策表述，或提取失败）"


def _stage0_parse(raw: str) -> list | None:
    """解析 stage0 输出为 key_phrases 列表；失败/结构异常返回 None。

    除常规完整 JSON 外，还容忍**输出被截断**的 JSON（免费长文本模型偶发触
    达输出上限）：即便外层对象未闭合，只要出现完整的 `"key_phrases":[...]`
    数组也能恢复出措辞，避免一次截断导致整轮提取失败。
    """
    base = _as_json(raw, "stage0")
    if base:
        try:
            obj = json.loads(base)
            if isinstance(obj, dict):
                items = obj.get("items")
                if isinstance(items, list):
                    phrases = [p for it in items if isinstance(it, dict)
                               for p in it.get("key_phrases", [])
                               if isinstance(p, str) and p.strip()]
                    return phrases
        except Exception:
            pass  # 落入下方截断恢复分支
    # 截断恢复：从原文抓所有完整闭合的 key_phrases 字符串数组
    import re

    phrases: list[str] = []
    for m in re.finditer(r'"key_phrases"\s*:\s*\[([^\]]*)\]', raw or ""):
        try:
            arr = json.loads("[" + m.group(1) + "]")
        except Exception:
            continue
        phrases.extend(p for p in arr if isinstance(p, str) and p.strip())
    return phrases or None


def _stage1_decode(ctx, block, stage0, articles=None) -> str:
    """核心判断 + ≤3 条支撑措辞（通用框架，适配所有政策类型）。

    2026-10-09：stage0 为空占位时直接返回空串、不调 LLM。
    2026-10-25：加入今日要闻，避免只凭拆散的措辞推主线；按「政策动作类型」归类。
    """
    if not _has_official_wording(stage0):
        return ""
    news = "\n".join(
        f"- {a.get('title', '')}：{(a.get('summary') or '')[:120]}"
        for a in (articles or [])[:3]
    ) or "（无）"
    prompt = (
        f"{block}\n\n"
        "任务：不要逐条翻译所有措辞。先判断这条新闻属于哪类政策动作："
        "①给钱/减负（补贴、减税、降费）；②收紧/限制（收费、监管、准入）；"
        "③定调/规划（会议、文件、纲要）；④数据发布（CPI/PMI/出口等）；⑤机构研究/报告。\n"
        "然后提炼【核心判断】：一句话说清政策要解决什么问题、主要影响谁、大概多久见效"
        "（≤60 字，以「【核心判断】」开头）。\n"
        "再只挑 **≤3 条最能体现『力度/范围/时间』**的措辞（如全面/逐步/试点/限期/严禁/"
        "加力），每条说明它对普通人的实际后果（≤40 字）。与主线无关、只是修辞的措辞一律不提。\n"
        "若今日有多条要闻，**以第 1 条为主线**；其余条目的措辞只有在直接支撑该主线时"
        "才保留（不同主题不要硬凑进同一条核心判断）。\n"
        "格式（纯文本，不要把模板文字原样抄出来）：\n"
        "【核心判断】……\n"
        "1. 原措辞A → 对普通人的实际后果\n"
        "2. 原措辞B → 对普通人的实际后果\n"
        "3. 原措辞C → 对普通人的实际后果\n"
        "写法示例（体现力度/范围/时间，不要照抄）："
        "『全面推开 → 全国都会执行，别只看试点城市』；『限期整改 → 相关行业要动真格，别抱侥幸』\n"
        "若这些措辞并非真实政策表述、无解读价值，则不要输出任何内容（宁可留空）。\n\n"
        f"## 今日要闻\n{news}\n\n## 特征措辞\n{stage0}"
    )
    return _ask_with_fallback(ctx, prompt)


def _stage2_compare(ctx, block, stage0, meeting_archive, focus: str = "") -> str:
    """今日特征措辞 vs 备份历史公报：强调同主题措辞变化。无历史则注明跳过。

    2026-10-01：今日无官方措辞（stage0 为空占位）时直接返回空串、不调 LLM，
    避免模型把任务说明回显进正文（「没有就没有」）；渲染层据此整段省略。
    """
    if not _has_official_wording(stage0):
        return ""
    if not meeting_archive:
        return "（无历史公报可对比，本次主题首次出现或未灌入公报，跳过同主题措辞变化对比。）"
    parts = []
    for topic, items in meeting_archive.items():
        if not items:
            continue
        latest = items[0]
        parts.append(f"# 最新公报《{latest['date']}》\n{latest['text'][:1500]}")
        if len(items) > 1:
            prev = items[1]
            parts.append(f"# 上次公报《{prev['date']}》\n{prev['text'][:1500]}")
    if not parts:
        return "（对比库为空，跳过措辞横向对比。）"
    corpus = "\n\n".join(parts)
    focus_block = f"\n## 今日核心判断（对比必须围绕它展开）\n{focus.strip()}\n" if focus.strip() else ""
    prompt = (
        f"{block}\n\n"
        "任务：只对比与【今日核心判断】直接相关的同主题措辞变化，最多 2 条，宁缺毋滥。"
        "每条一句话（≤50 字）：说清今天的说法相对历史公报是【新提法】【力度变化】还是"
        "【延续】。与核心判断无关的措辞不要写；没有相关变化就只输出「（无相关历史变化）」。\n\n"
        f"{focus_block}\n## 今日特征措辞\n{stage0}\n\n## 历史公报文本\n{corpus}"
    )
    return _ask_with_fallback(ctx, prompt)


# ── 利率类新闻：黄金/股市影响（条件触发，2026-10-25）────────────────────

_MONETARY_KEYWORDS = (
    "降息", "加息", "降准", "存款准备金率", "LPR", "贷款市场报价利率",
    "美联储", "联邦基金利率", "FOMC", "议息会议", "利率决议",
    "逆回购", "MLF", "中期借贷便利",
)


def _is_monetary_news(articles: list[dict]) -> bool:
    """标题/摘要命中利率类关键词 → 需要补「对黄金与股市的影响」。"""
    text = " ".join(f"{a.get('title', '')} {a.get('summary', '')}" for a in articles)
    return any(k in text for k in _MONETARY_KEYWORDS)


def _stage_market_impact(ctx, articles) -> str:
    """利率类新闻专用：说明对黄金与股市的通常影响 + 1 条普通家庭建议。

    只讲方向与机制，不预测点位/涨跌幅；单一事件不改长期配置。
    """
    news = "\n".join(
        f"- {a.get('title', '')}：{(a.get('summary') or '')[:160]}"
        for a in articles[:3]
    )
    prompt = (
        "任务：今天的要闻涉及利率（中国或美国的降息/加息/降准/LPR/美联储议息）。"
        "请用普通人能懂的话，说明它对【黄金】和【股市】的通常影响，并给 1 条可执行建议。\n"
        "规则：\n"
        "① 先分清方向：降息/降准通常利多黄金与风险资产（流动性宽松、持有黄金机会成本下降）；"
        "加息/收紧通常相反。美国利率还要看美元（美元走强→黄金承压）。\n"
        "② 只讲方向与机制，必须用「通常/历史规律」措辞；不得预测具体涨跌幅、点位或时间。\n"
        "③ 黄金、股市各一行（≤45 字）：方向 + 一句机制 + 需要观察的变量。\n"
        "④ 建议一行（≤50 字）：面向普通家庭，动作具体（分批定投/不加杠杆/先观望），"
        "提醒单一事件不改长期配置。\n"
        "⑤ 事实只能来自下方新闻，不编造数据；禁止「必涨/抄底」这类断言。\n"
        "输出格式（纯文本）：\n"
        "黄金：……\n"
        "股市：……\n"
        "建议：……\n\n"
        f"## 今日要闻\n{news}"
    )
    return _ask_with_fallback(ctx, prompt)


def _stage3_advice(ctx, stage1, stage2, articles=None) -> str:
    """唯一 1 条可执行建议。articles 为今日精选（唯一事实来源），避免模型凭空编造。"""
    news = "\n".join(
        f"- {a.get('title', '')}：{(a.get('summary') or '')[:120]}"
        for a in (articles or [])[:3]
    ) or "（无）"
    prompt = (
        "任务：结合核心判断与今日新闻，从『普通老百姓』视角给出**唯一 1 条**可执行建议（≤70 字）。\n"
        "硬要求：\n"
        "① 事实只能来自下方【今日要闻】，里面没写到的部门/文件/目录/数字一律不许出现；\n"
        "② 读者只能是【普通个人/家庭】，不能把平台/企业/机构当成读者；"
        "不要只写给某个小众人群（在校大学生、某城市、某职业、某收入段都算窄口径）；\n"
        "③ 先按新闻类型选传导路径，再落到个人动作："
        "给钱/减负→看自己是否符合条件、去哪申领；收紧/限制→看自己是否在范围内、先停/改再观望；"
        "定调/规划→记住落地节点、等细则；数据发布→用数据校准自己的收支/房贷/就业判断；\n"
        "④ 动作要具体（查询/申报/暂缓/调整/预约），并给出时间点或判断标准；"
        "禁止「关注/了解/重视/保持/提升」这类空动作；\n"
        "⑤ 渠道优先用【今日要闻】里出现的发布部门官网；新闻没写发布部门时，"
        "才用同类官方渠道（人社局/公积金中心/央行/统计局/税务等），不得自造项目名；\n"
        "⑥ 只给一个动作，不喊口号、不贩卖焦虑。**若这条政策主要约束平台/企业，"
        "对普通个人没有直接可执行动作，就输出「💡 今日无直接可执行建议」**，不要硬编一个。\n"
        "正例（不同类型，领会思路，别照抄）：\n"
        "  就业类：💡 本月登录当地人社局官网查申报入口，符合条件就直接提交。\n"
        "  货币类：💡 等下次 LPR 公布后再决定是否提前还贷，别在这个月锁死。\n"
        "  楼市类：💡 本周先查本地公积金中心公告，看贷款额度是否上调再定买房节奏。\n"
        "  外贸类：💡 本周查海关/商务部最新目录，看自己的生意是否在退税或补贴范围。\n"
        "前置『💡』，只输出这一条，不要解释。\n\n"
        f"## 今日要闻（唯一事实来源）\n{news}\n\n"
        f"## 措辞解读\n{stage1}\n\n## 历史对比\n{stage2}"
    )
    return _ask_with_fallback(ctx, prompt)


# ── 降级封装 ─────────────────────────────────────────────────────────────

def _ask_with_fallback(ctx, prompt: str) -> str:
    """按 [主模型, *fallback] 逐个调用，异常降级下一模型。"""
    llm = ctx.config.llm
    chain = [llm.news_digest_model, *(llm.news_digest_fallback_models or [])]
    last_err = None
    for model in chain:
        try:
            return ctx.llm_gateway.ask(
                prompt=prompt, model_tier=model, plugin_name=PLUGIN)
        except Exception as e:  # noqa: PERF203
            logger.warning("news_digest model %s failed: %s → 降级", model, str(e)[:120])
            last_err = e
    # 保留原始异常（含其类型）供上层精确定位失败原因，而非包装成 RuntimeError
    raise last_err


def _as_json(raw: str, tag: str) -> str:
    """尝试把 LLM 输出规整成 JSON 字符串；失败原样返回。"""
    try:
        start, end = raw.index("{"), raw.rindex("}")
        return raw[start:end + 1]
    except Exception:
        return (raw or "").strip()


# ── 落盘 ─────────────────────────────────────────────────────────────────

def _persist(run_dir, result: dict) -> None:
    from . import storage

    try:
        for stage, content in result.items():
            if content:
                storage.save_output(run_dir, stage, content)
    except Exception as e:  # 落盘失败不阻塞主流程
        logger.warning("news_digest persist failed: %s", e)


# ── 探针 ─────────────────────────────────────────────────────────────────

def probe(ctx: Context) -> list[dict]:
    """逐模型测可用性，返回 [{model, ok, reason}]。"""
    llm = ctx.config.llm
    chain = [llm.news_digest_model, *(llm.news_digest_fallback_models or [])]
    out = []
    for model in chain:
        try:
            ctx.llm_gateway.ask(prompt="say ok", model_tier=model, plugin_name=PLUGIN)
            out.append({"model": model, "ok": True, "reason": ""})
        except Exception as e:  # noqa: PERF203
            out.append({"model": model, "ok": False, "reason": str(e)[:120]})
    return out