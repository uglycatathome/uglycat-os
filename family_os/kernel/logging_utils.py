"""日志脱敏 —— 防止带密钥的 URL（Server酱 SendKey / 企业微信 webhook key）进入日志。

背景：httpx 的 `HTTPStatusError` 消息里带完整 URL（含 query 里的 key），
Notifier 在推送失败时以 WARNING 记录该异常；WARNING 及以上会经 logging.lastResort
直接落到 journald，于是密钥会被写进系统日志。

做法：在「日志记录创建层」装一个 `LogRecordFactory`，对每条记录的 msg 与 args
做 URL 脱敏。这样无论以后新增什么 handler、什么 logger，都不会漏。

覆盖范围：msg 里的完整 URL、字符串参数里的完整 URL、异常对象（str/repr 输出）。
已知边界：若一条 URL 被占位符切成两段（前缀在 msg、密钥在参数里），参数那半段
不含 "http" 因而不会被掩码——代码中不存在这种写法，所有日志传的都是完整 URL 或异常。
"""
from __future__ import annotations

import logging
import re

# 匹配 http(s) URL 直到空白/引号/右括号等分隔符。
# 刻意排除 '%'：否则会把日志里的 `%s` 占位符一起吃掉，导致 `msg % args` 抛
# TypeError（"not all arguments converted"）。
_URL_RE = re.compile(r"https?://[^\s'\"<>()\[\]{}，。；、%]+")

# 保留域名，路径与查询部分统一掩码，只留末尾 4 位便于人工核对是哪一个 key
_TAIL_KEEP = 4
_TAIL_MIN_HIDE = 8

_installed = False


class _RedactedArg:
    """包装任意对象（如异常），使其 str()/repr() 输出被脱敏。

    必须包装而不是 `str(value)`：异常消息里带完整 URL（httpx.HTTPStatusError
    形如 `Client error '404' for url 'https://...?key=xxx'`），直接透传会泄漏；
    而直接转字符串又会破坏 `%r` 等格式化。包装可保留格式化语义且输出已脱敏。
    """

    __slots__ = ("_v",)

    def __init__(self, value):
        self._v = value

    def __str__(self) -> str:
        return redact_text(str(self._v))

    def __repr__(self) -> str:
        return redact_text(repr(self._v))


def redact_url(url: str) -> str:
    """把 URL 的路径/查询串掩码成 `<域名>/…<末尾4位>`。

    例：
        https://sctapi.ftqq.com/SCT1234567890.send       -> https://sctapi.ftqq.com/…890.
        https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abcd-1234 -> https://qyapi.weixin.qq.com/…1234
        https://example.com/                             -> https://example.com
    """
    m = re.match(r"^(https?://[^/]+)(/.*)?$", url)
    if not m:
        return url
    host, tail = m.group(1), m.group(2) or ""
    if not tail or tail == "/":
        return host
    if len(tail) <= _TAIL_MIN_HIDE:
        return f"{host}/…"
    return f"{host}/…{tail[-_TAIL_KEEP:]}"


def redact_text(text: str) -> str:
    """把一段文本里出现的所有 URL 掩码。非 URL 文本原样返回。"""
    if not text or "http" not in text:
        return text
    return _URL_RE.sub(lambda m: redact_url(m.group(0)), text)


def _redact_arg(value):
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, tuple):
        return tuple(_redact_arg(v) for v in value)
    if isinstance(value, dict):
        return {k: _redact_arg(v) for k, v in value.items()}
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    # 其他对象（异常等）：包装后脱敏，保留 %s/%r 格式化行为
    return _RedactedArg(value)


def install_redaction() -> bool:
    """装入全局脱敏。幂等：重复调用不会叠加包装。返回本次是否真正安装。"""
    global _installed
    if _installed or getattr(logging.getLogRecordFactory(), "_family_os_redacting", False):
        _installed = True
        return False

    prev_factory = logging.getLogRecordFactory()

    def _factory(*args, **kwargs):
        record = prev_factory(*args, **kwargs)
        if isinstance(record.msg, str):
            record.msg = redact_text(record.msg)
        if record.args:
            record.args = _redact_arg(record.args)
        return record

    _factory._family_os_redacting = True  # type: ignore[attr-defined]
    logging.setLogRecordFactory(_factory)
    _installed = True
    return True
