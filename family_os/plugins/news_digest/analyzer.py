"""analyzer.py —— news_digest 的 LLM 分析 pipeline。

服务目标（2026-11-01 调整）：为【创业选择 / 职业选择 / 投资决策】提供信息支持。
因此选题范围收窄到四类信号（判定逻辑在 scope.py）：
    重要文件发布 / 重要会议 / 重要经济指标 / 全国性产业大会（展示型，非政策）
党务党建、纪检反腐、地方信息、四类之外的活动、媒体评论、单条出口额等一律不入选。

流程：
  代码粗筛（近乎相同标题）→ LLM 主题级去重 → scope 范围取样 → LLM 精选
  → 0 提取特征措辞 → 1 对照方法论解码 → 2 与历史文本对比 → 3 一句话建议
模型链配置于 config.llm.news_digest_model + fallback_models（免费模型）。
"""

import json
import logging
import os
import re
from pathlib import Path

from family_os.kernel.context import Context

from . import decode, scope

logger = logging.getLogger("family_os.news_digest.analyzer")

PLUGIN = "news_digest"


def _extra_rules(name: str) -> str:
    """读取用户自定义提示词补充规则（`prompts/<name>.md`）。

    使用者（或使用者的 AI）只改这些 markdown 文件、不动代码，就能持续优化
    评选口径与黑话解读；文件不存在或为空时返回空串，行为与默认完全一致。
    路径可用环境变量 `UGLYCAT_PROMPTS_DIR` 覆盖，默认相对当前目录的 `prompts/`。
    """
    p = Path(os.environ.get("UGLYCAT_PROMPTS_DIR", "prompts")) / f"{name}.md"
    try:
        if not p.is_file():
            return ""
        txt = p.read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    if not txt:
        return ""
    return ("\n\n## 用户自定义规则（优先遵循）\n"
            "以下是使用者针对本阶段的补充要求。若与上文的输出格式要求冲突，"
            "仍以 JSON 输出格式为准。\n"
            f"{txt}")

# 党和国家重要会议登记表（2026-11-01 迁移到 scope.py，本处仅做别名以便
# 插件/测试沿用 analyzer.MEETING_REGISTRY）。
# 用途不变：**仅用于归档**（会议概要与措辞按 key+日期入库，供同主题措辞对比），
# 不参与任何强制入选、也不等同于选题范围（范围判定见 scope.classify）。
MEETING_REGISTRY = scope.MEETING_REGISTRY

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

    2026-11-01 重构（用户口径：解读只为创业/职业/投资服务）：
      ① 代码粗筛：剔除与近期已推送主题【近乎完全相同标题】的候选（兜底）；
      ② LLM 去重（_stage_dedup，独立调用）：主题级【语义】去重；
      ③ 候选窗口（_pick_select_candidates）：只取命中 scope 四类信号的候选，
         不受「最新 N 条」限制（前一天的重要文件不会被今日软新闻挤出视野）；
      ④ 筛选（_stage_select，独立调用）：LLM 从候选里选 ≤3 条，
         且每条必须**同时**通过 scope.classify 的确定性硬门槛
         （重要文件 / 重要会议 / 重要经济指标 / 产业大会），否则剔除；
      ⑤ stage0~3：措辞提取/解码/历史对比/一句话建议（口径改为创业·职业·投资）；
      ⑥ 会议概要与措辞归档（命中登记表即入库，供后续对比）。

    宁缺毋滥：解析失败/无合格信息即空选（不做任何凑数兜底），
    空选时跳过 ⑤，mode="empty"，上层据此跳过当日推送。
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
        + _extra_rules("dedup")
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


# 精选输入窗口（2026-11-01 重构）：旧实现按「最新 N 条 + 优先源 + 标题信号词」
# 取样，软新闻照样会挤进 LLM 视野。现在先用 scope.classify 做确定性范围取样：
# 只有命中四类信号（文件/会议/指标/产业大会）的候选进入精选提示词，且不受发布时间
# 限制——前一天的重要文件不会被今天的软新闻挤出视野。
# 范围取样为空时退回最新 SELECT_WINDOW 条：让 LLM 有内容可看、能给出
# 「今日无合格新闻」的理由；最终是否入选仍由 scope 硬门槛把关。
SELECT_WINDOW = 20
SCOPE_QUOTA = 30


def _pick_select_candidates(articles: list[dict],
                            limit: int = SELECT_WINDOW,
                            quota: int = SCOPE_QUOTA) -> list[dict]:
    """精选阶段的输入候选：

    ① 命中 scope 四类信号的全部候选（时间倒序，最多 ``quota`` 条）；
    ② 一条都没有时，退回最新 ``limit`` 条，供 LLM 判定「今日无合格新闻」。
    """
    ordered = sorted(articles, key=lambda x: x.get("published") or "", reverse=True)
    in_scope = [a for a in ordered
                if scope.classify(a.get("title") or "",
                                  a.get("summary") or a.get("body") or "")]
    if in_scope:
        return in_scope[:quota]
    return ordered[:limit]


def _stage_select(ctx, articles) -> tuple[list[dict] | None, str]:
    """LLM 从【已去重】的候选中精选四类信号（≤3 条，宁缺毋滥）。

    2026-11-01 按用户口径重写：入选范围只有四类，且目的必须明确——
    为【创业选择 / 职业选择 / 投资决策】提供信息支持：
      A 国家级重要文件发布 / B 国家级重要会议 / C 重要经济指标
      D 全国性产业大会（展示型，不是政策信号，只认会上的成交/订单/趋势信息）。
    明确不选：党建党务与纪检反腐（即使带「决定/条例」）、个人署名表态稿、
    地方信息、D 之外的活动、媒体评论、央行技术性流动性操作，以及**单条出口额、
    进出口金额、贸易额这类数量新闻**（用户点名不够格）。
    LLM 输出后还要再过一道 scope.classify 确定性硬门槛，并回填「分类」标签。

    返回 (items, note)：
      - items 为 list 表示解析成功（空 list=今日确无合格信号，note 给理由）；
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
        "任务：下面是当日候选新闻。请只精选能为【创业方向 / 职业选择 / 投资决策】"
        "提供信息支持的【国家级信号与产业信号】，最多 3 条，宁缺毋滥。\n"
        "判断标准只有一条：这条新闻会不会改变一个人对「做什么生意、选什么行业/岗位、"
        "钱往哪配」的判断？不能的，一律不选。\n\n"
        "只有以下四类够格，每条都必须能明确归入其中一类：\n"
        "【A·国家级重要文件发布】\n"
        "  · 中共中央、国务院（含中办/国办）印发的意见/决定/规划/纲要/条例/方案/"
        "行动计划/通知等；\n"
        "  · 国务院部门（发改委/财政部/央行/证监会/工信部/商务部/住建部/人社部/"
        "统计局/金融监管总局/市场监管总局/国资委等）正式印发的全国性制度性文件"
        "（办法/条例/规定/细则/实施方案）；\n"
        "  · 判定关键：能指出发布主体 + 正式文种 + 全国适用。一般性工作部署、风险"
        "提示、日常执法通知不算。标题平淡但级别高的文件（如中央一号文件、"
        "《关于促进民营经济发展壮大的意见》）务必优先入选。\n"
        "【B·国家级重要会议】\n"
        "  · 中央政治局会议、中央政治局常委会会议、中央经济工作会议、中央农村工作"
        "会议、中央全会、全国两会、国务院常务会议、中央财经委员会会议、中央全面"
        "深化改革委员会会议、中央金融工作会议、中央城市工作会议等，及其公报。\n"
        "【C·重要经济指标】\n"
        "  · 利率决策：中国降息/降准/加息、LPR、存款准备金率；美国美联储议息、"
        "加息降息、联邦基金利率；\n"
        "  · 物价与增长：CPI、PPI、GDP、PMI、工业增加值、社会消费品零售、固定资产投资；\n"
        "  · 金融与就业：社融、M2、新增人民币贷款、70 城房价、失业率、外汇储备；\n"
        "  · 只认全国口径、由官方机构发布的原始数据。\n"
        "【D·产业大会（展示型，不是政策信号）】\n"
        "  · 进博会、广交会、服贸会、高交会、消博会、世界人工智能大会、"
        "世界互联网大会、博鳌亚洲论坛、中国发展高层论坛、夏季达沃斯等"
        "全国性/国际性年度头部大会；\n"
        "  · 只有当它们给出可用行业信号时才选：成交额/订单/签约、参会企业与结构、"
        "新品与技术首发、会上发布的政策或数据。**开幕仪式、展台花絮、招商口号、"
        "会前预告（「关于举办…」「将于…举办」）不选**；\n"
        "  · summary 必须写清这组数据能说明什么行业趋势，不能只复述数字。\n\n"
        "【一律不选】\n"
        "  · 党建党务、纪检反腐、巡视、主题教育、理论学习、宣讲；**即使标题含"
        "『决定/条例』等正式文件字样也不选**；\n"
        "  · 个人署名表态稿（「某某：…」「某某表示/称/强调…」），无论谈得多宏观；\n"
        "  · 地方/省级/城市级的政策、数据、会议、公告；\n"
        "  · D 之外的论坛、峰会、展会、交易会、电影节、发布会、启动仪式、"
        "调研考察、座谈会等活动（地方小展会、行业小型论坛一律不选）；\n"
        "  · 媒体评论、深度报道、调查报道，以及「面面观/观察家/述评/评论员/专访/"
        "图解/数说/盘点」类栏目；\n"
        "  · 央行技术性流动性操作（逆回购/MLF/再贷款/互换便利/公开市场操作），"
        "未同时包含降息/降准/加息/LPR 决策的；\n"
        "  · **单条出口额、进出口金额、贸易额、出口增速这类数量新闻**——只有数量、"
        "没有政策或价格信号，对创业/职业/投资判断没有增量，不够格；\n"
        "  · 单笔订单、单个企业或项目动态、基建工程节点、纪念活动、体育文娱、"
        "地方民生琐事。\n\n"
        "【同一事件只选一条】多源重复报道只留一条代表。\n"
        "【数量】1 条够格就只放 1 条；2~3 条互不重复且都够格才全放；超过 3 条取"
        "最重要的 3 条。一条都不够格时 items 为空数组，并用 note 一句话说明理由。\n"
        "每条给出：\n"
        "  title（与原文标题一致）\n"
        "  category（四选一：文件 / 会议 / 指标 / 产业大会）\n"
        "  summary（2~3 句说人话，说明它对创业、职业或投资意味着什么）\n"
        "  reason（一句话：为什么它能改变创业/职业/投资的判断）\n"
        "  topic_key（3~8 字主题标签，如「降准」「美联储降息」「民营经济」，用于跨天去重）\n"
        "输出 JSON：{\"items\":[{\"title\":\"...\",\"category\":\"文件\","
        "\"summary\":\"...\",\"reason\":\"...\",\"topic_key\":\"...\"}],"
        "\"note\":\"...\"}。只输出 JSON，不要输出其他文字。\n"
        f"## 当日新闻\n{limited}"
        + _extra_rules("select")
    )
    try:
        raw = _ask_with_fallback(ctx, prompt)
        items, note = _stage_select_parse(raw), _stage_select_note(raw)
        if items is None:
            # 解析失败重试一次（与 stage0 同策略）
            logger.warning("news_digest 精选解析失败，重试一次: %s", (raw or "")[:80])
            raw = _ask_with_fallback(ctx, prompt)
            items, note = _stage_select_parse(raw), _stage_select_note(raw)
        # 硬门槛：LLM 会自我合理化，必须再过 scope 的确定性判定，并回填分类标签
        if items:
            qualified = []
            for it in items:
                cat = scope.classify(it.get("title") or "",
                                     it.get("summary") or "")
                if cat:
                    qualified.append({**it, "category": scope.label(cat)})
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


def _is_qualified_news(item: dict) -> bool:
    """精选条目的最终硬门槛：必须命中 scope 的四类信号之一。

    2026-11-01：判定规则统一收口到 scope.py（范围只认「重要文件 / 重要会议 /
    重要经济指标 / 产业大会」），本函数仅作为调用点保留——LLM 会自我合理化把
    栏目评论、地方活动、地方数据、出口额当要闻选进来，这里用确定性规则兜底。
    """
    return scope.classify(item.get("title") or "",
                          item.get("summary") or "") is not None


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
        + _extra_rules("stage0")
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
    """核心判断 + ≤3 条支撑措辞（口径：对创业/职业/投资的含义）。

    2026-10-09：stage0 为空占位时直接返回空串、不调 LLM。
    2026-10-25：加入今日要闻，避免只凭拆散的措辞推主线；按「政策动作类型」归类。
    2026-11-01：解读落点从「对普通人意味着什么」改为「对创业/职业/投资意味着什么」。
    """
    if not _has_official_wording(stage0):
        return ""
    news = "\n".join(
        f"- {a.get('title', '')}：{(a.get('summary') or '')[:120]}"
        for a in (articles or [])[:3]
    ) or "（无）"
    prompt = (
        f"{block}\n\n"
        "任务：不要逐条翻译所有措辞。先判断这条新闻属于哪类信号："
        "①给钱/减负（补贴、减税、降费）；②收紧/限制（收费、监管、准入）；"
        "③定调/规划（会议、文件、纲要）；④数据发布（CPI/PPI/PMI/利率等）；"
        "⑤产业大会（成交额、订单、参会结构、技术首发）。\n"
        "然后提炼【核心判断】：一句话说清政策要解决什么问题、主要影响哪个行业或哪类人、"
        "大概多久见效（≤60 字，以「【核心判断】」开头）。\n"
        "再只挑 **≤3 条最能体现『力度/范围/时间』**的措辞（如全面/逐步/试点/限期/严禁/"
        "加力），每条说明它对【创业、职业或投资】的实际含义（≤40 字，例如准入放宽→可做"
        "的方向变多；限期整改→相关行业成本抬升；利率下行→杠杆与资产配置怎么调）。"
        "与主线无关、只是修辞的措辞一律不提。\n"
        "若今日有多条要闻，**以第 1 条为主线**；其余条目的措辞只有在直接支撑该主线时"
        "才保留（不同主题不要硬凑进同一条核心判断）。\n"
        "格式（纯文本，不要把模板文字原样抄出来）：\n"
        "【核心判断】……\n"
        "1. 原措辞A → 对创业/职业/投资的含义\n"
        "2. 原措辞B → 对创业/职业/投资的含义\n"
        "3. 原措辞C → 对创业/职业/投资的含义\n"
        "写法示例（体现力度/范围/时间，不要照抄）："
        "『全面推开 → 全国都执行，别只盯试点城市的生意』；"
        "『限期整改 → 相关行业合规成本上升，进场前先算这笔账』\n"
        "若这些措辞并非真实政策表述、无解读价值，则不要输出任何内容（宁可留空）。\n\n"
        f"## 今日要闻\n{news}\n\n## 特征措辞\n{stage0}"
        + _extra_rules("stage1")
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
        + _extra_rules("stage2")
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
        "④ 建议一行（≤50 字）：面向正在做投资/创业决策的人，动作具体（分批定投/"
        "不加杠杆/先观望），提醒单一事件不改长期配置。\n"
        "⑤ 事实只能来自下方新闻，不编造数据；禁止「必涨/抄底」这类断言。\n"
        "输出格式（纯文本）：\n"
        "黄金：……\n"
        "股市：……\n"
        "建议：……\n\n"
        f"## 今日要闻\n{news}"
        + _extra_rules("market_impact")
    )
    return _ask_with_fallback(ctx, prompt)


def _family_block(ctx) -> str:
    """把用户录入的家庭/个人信息整理成 Prompt 片段；未配置时返回空串。

    `sensitivity=secret` 的事实不进入任何 LLM 调用（与 market_pulse /
    strategy_radar 口径一致）。读取失败只记 warning，不影响当日推送。
    """
    try:
        from family_os.kernel.privacy import is_secret_fact

        facts = ctx.store.query_facts() or []
    except Exception as e:  # noqa: BLE001
        logger.warning("news_digest 读取家庭信息失败: %s", e)
        return ""
    items = []
    for f in facts:
        if is_secret_fact(f):
            continue
        try:
            items.append((f["member"] or "全家", f["type"], f["key"], f["value"]))
        except (KeyError, IndexError, TypeError):
            continue
    if not items:
        return ""
    lines = ["## 我的家庭/个人情况（本地录入，仅用于本次建议）"]
    for member, ftype, key, value in items[:15]:
        lines.append(f"- {member}｜{ftype}｜{key}：{value}")
    return "\n".join(lines)


def _stage3_advice(ctx, stage1, stage2, articles=None) -> str:
    """唯一 1 条可执行建议（口径：创业 / 职业 / 投资 三条落点之一）。

    2026-11-01：读者不再是「普通老百姓」，而是正在做创业、职业或投资决策的人。
    articles 为今日精选（唯一事实来源），避免模型凭空编造。
    若用户在本地录入过家庭/个人信息，建议要落到该家庭的具体处境。
    """
    news = "\n".join(
        f"- {a.get('title', '')}：{(a.get('summary') or '')[:120]}"
        for a in (articles or [])[:3]
    ) or "（无）"
    family = _family_block(ctx)
    if family:
        family_rule = (
            "⑦ 下方提供了我（读者本人）的家庭/事业情况：建议要结合这些具体情况，落到我能做的"
            "动作与判断标准上；不要复述其中的姓名、金额等原始信息，也不要提与本条新闻无关的项。\n"
            "⑧ 结合不上时仍按普通创业/就业/投资口径给建议，但不得编造家庭信息。\n"
        )
    else:
        family_rule = (
            "⑦ 未提供个人情况：按一般创业/就业/投资口径给建议，不要假设读者有特定行业、"
            "房贷或孩子。\n"
        )
    prompt = (
        "任务：结合核心判断与今日新闻，从【创业 / 职业 / 投资】视角给出**唯一 1 条**"
        "可执行动作（≤70 字）。\n"
        "硬要求：\n"
        "① 事实只能来自下方【今日要闻】，里面没写到的部门/文件/目录/数字一律不许出现；\n"
        "② 先判断这条信号落在哪条线上，再落到动作："
        "文件（准入/补贴/监管）→ 查申报条件与合规要求、算成本；"
        "会议（定调/规划）→ 记住落地节点、等细则、调整中期布局；"
        "指标（利率/物价/增长）→ 用它校准融资成本、资产配置与行业景气判断；"
        "产业大会（成交/订单/技术趋势）→ 用它校准所在行业的订单与选型判断；\n"
        "③ 动作要具体（查询/申报/暂缓/加仓/降杠杆/调整报价），并给出时间点或判断标准；"
        "禁止「关注/了解/重视/保持/提升」这类空动作；\n"
        "④ 渠道优先用【今日要闻】里出现的发布部门官网；新闻没写发布部门时，"
        "才用同类官方渠道（央行/统计局/发改委/证监会/人社局等），不得自造项目名；\n"
        "⑤ 只给一个动作，不喊口号、不贩卖焦虑。**若这条信号确实落不到个人可执行动作，"
        "就输出「💡 今日无直接可用的创业/职业/投资动作」**，不要硬编一个。\n"
        + family_rule +
        "正例（领会思路，别照抄）：\n"
        "  文件类：💡 本周去发改委官网查这份办法的申报门槛，算清自家生意是否够格。\n"
        "  会议类：💡 把细则落地时间记进日历，明年一季度再定扩不扩产。\n"
        "  利率类：💡 等下次 LPR 公布后再定融资/还贷节奏，别在这个月锁死成本。\n"
        "  数据类：💡 用这组 CPI/PMI 数据重算一遍客单价与库存周转，再决定是否补货。\n"
        "  大会类：💡 对照这份订单结构，挑一个正在放量的细分品类做这季度的主推。\n"
        "前置『💡』，只输出这一条，不要解释。\n\n"
        f"## 今日要闻（唯一事实来源）\n{news}\n\n"
        f"## 措辞解读\n{stage1}\n\n## 历史对比\n{stage2}"
        + (f"\n\n{family}" if family else "")
        + _extra_rules("stage3")
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