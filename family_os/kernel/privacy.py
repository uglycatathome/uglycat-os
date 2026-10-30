"""privacy.py —— 隐私分级的最小工具。

家庭事实（`facts` 表）带 `sensitivity` 字段（public / private / secret）。
约定：`secret` 级事实**不进入任何 LLM 调用**（只留在本地库里）。
"""

from __future__ import annotations

SECRET = "secret"


def is_secret_fact(fact) -> bool:
    """判断一条事实是否为 secret 级；兼容 sqlite3.Row / dict / 缺字段。"""
    try:
        return str(fact["sensitivity"] or "").strip().lower() == SECRET
    except (KeyError, IndexError, TypeError):
        return False
