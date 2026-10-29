"""dedup.py —— news_digest 代码级「粗筛」去重（两个阶段）。

2026-10-10 拆分（用户要求「先日期去重和内容去重，然后进筛选 stage」）：
  ① 日期去重 `filter_duplicates`：跨天，剔除与近 N 天已处理标题【近乎完全相同】
     的重复投递（同一篇稿子被反复抓到）；
  ② 内容去重 `dedup_same_day`：当日，合并同一天内不同源对【同一事件】的报道，
     同一事件只留一条（近似标题聚类，保留最早一条作代表）。

⚠️ 两者都只是**粗筛**，不承担主题级语义去重——实测「同一政策不同写法」标题
相似度极低（如「从房贷贴息看住房支持政策新探索」vs「经济日报：购房贷款贴息
重在精准滴灌」仅 0.26），字符串去重结构性拦不住；主题级语义去重由独立的
LLM 去重阶段完成（analyzer._stage_dedup）。
"""

import logging
import re
from difflib import SequenceMatcher

logger = logging.getLogger("family_os.news_digest.dedup")

# 粗筛阈值：只拦「近乎完全相同」的标题（同源重复投递/仅差后缀词）。
# 主题级语义去重不在此列，交给 LLM。
DEFAULT_THRESHOLD = 0.9

# 当日内容去重阈值（比跨天略低）：同一事件不同媒体标题常高度相似。
SAME_DAY_THRESHOLD = 0.75

# 常见的媒体名后缀/无意义收尾词，规范时去掉以减少噪音
_TRAILING_WORDS = ("发布", "报道", "播", "说", "表示", "称", "：", ":")

_PUNCT = re.compile(r"[\s\u3000，。；：、（）()【】\[\]《》\"'“”‘’!！?？·\-—_…,.;:！]+")


def normalize_title(title: str) -> str:
    """规范化标题：统一空白/标点/全角，得到稳定指纹。"""
    t = title or ""
    # 去媒体常见收尾词（如 "新华社发布" 中的 发布/说/表示）
    lowered = t
    for w in _TRAILING_WORDS:
        if lowered.endswith(w):
            lowered = lowered[: -len(w)]
    t = _PUNCT.sub("", lowered)
    return t.strip()


def is_dup(new_title: str, seen_titles: list[str],
           threshold: float = DEFAULT_THRESHOLD) -> bool:
    """粗筛：与历史指纹模糊比对，任一相似度 ≥ threshold 视为近乎同一标题。"""
    norm = normalize_title(new_title)
    if not norm:
        return False
    for seen in seen_titles:
        s = normalize_title(seen)
        if not s:
            continue
        ratio = SequenceMatcher(None, norm, s).ratio()
        if ratio >= threshold:
            return True
    return False


def filter_duplicates(articles: list[dict], seen_titles: list[str],
                      threshold: float = DEFAULT_THRESHOLD
                      ) -> tuple[list[dict], list[str]]:
    """【日期去重】跨天粗筛。返回 (保留的文章, 本日新增指纹)。

    - 与历史 seen_titles 近乎完全相同的文章被剔除（仅粗筛，不做主题语义判断）；
    - 保留文章中的新增指纹被收集起来供写回。
    """
    kept: list[dict] = []
    new_titles: list[str] = []
    for a in articles:
        title = a.get("title", "") or ""
        if is_dup(title, seen_titles, threshold):
            logger.info("日期去重跳过(近乎完全相同标题): %s", title[:40])
            continue
        norm = normalize_title(title)
        if norm:  # 空标题不写入指纹（无法代表主题）
            new_titles.append(title)
        kept.append(a)
    return kept, new_titles


def dedup_same_day(articles: list[dict],
                   threshold: float = SAME_DAY_THRESHOLD) -> list[dict]:
    """【内容去重】当日粗筛：同一事件的多源报道只保留一条。

    以规范化标题做近似聚类（同源/异源同事件标题相似度通常 ≥ 阈值），
    每组保留发布时间最早的一条作为代表，其余视为重复报道剔除。
    仅粗筛：写法差异大的同事件仍交给后续 LLM 去重。
    """
    kept: list[dict] = []
    reps: list[str] = []        # 已保留代表的规范化标题
    for a in sorted(articles, key=lambda x: x.get("published") or ""):
        title = a.get("title", "") or ""
        norm = normalize_title(title)
        if not norm:
            kept.append(a)
            continue
        if any(SequenceMatcher(None, norm, r).ratio() >= threshold
               for r in reps):
            logger.info("内容去重跳过(当日同事件): %s", title[:40])
            continue
        reps.append(norm)
        kept.append(a)
    return kept