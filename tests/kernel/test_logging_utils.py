"""日志脱敏单测（kernel/logging_utils.py）。

覆盖真实泄漏场景：httpx.HTTPStatusError 的消息里带完整 URL（含 query 里的 key），
Notifier 以 WARNING 记录该异常 → WARNING 会经 logging.lastResort 落 journald。
"""
from __future__ import annotations

import logging

import pytest

from family_os.kernel import logging_utils
from family_os.kernel.logging_utils import (
    install_redaction,
    redact_text,
    redact_url,
)

# 假 Key：不得使用真实 SendKey（曾误把真实值写进测试，2026-10-30 修正）
SERVERCHAN_KEY = "SCT_TEST_KEY_DUMMY"
WEBHOOK_URL = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abcd-1234-5678-9012"


@pytest.fixture
def redaction():
    """在当前测试内装入脱敏，结束后还原全局工厂与模块标记。"""
    prev = logging.getLogRecordFactory()
    prev_flag = logging_utils._installed
    logging_utils._installed = False
    install_redaction()
    yield
    logging.setLogRecordFactory(prev)
    logging_utils._installed = prev_flag


class TestRedactUrl:
    def test_keeps_host_masks_path_and_query(self):
        """保留域名，路径与 query 掩码，只留末尾 4 位。"""
        out = redact_url(WEBHOOK_URL)
        assert out.startswith("https://qyapi.weixin.qq.com/")
        assert "abcd-1234-5678-9012" not in out
        assert out.endswith("9012")

    def test_bare_host_untouched(self):
        """只有域名（无路径）时原样返回。"""
        assert redact_url("https://example.com") == "https://example.com"
        assert redact_url("https://example.com/") == "https://example.com"

    def test_short_tail_is_fully_masked(self):
        """尾部过短（≤8）时整条掩掉，避免把短 key 直接露出来。"""
        assert redact_url("https://example.com/ab") == "https://example.com/…"

    def test_non_url_untouched(self):
        """不是 URL 的字符串原样返回。"""
        assert redact_url("not a url") == "not a url"


class TestRedactText:
    def test_masks_every_url_in_text(self):
        """一段文本里出现的多个 URL 全部掩码，普通文字不动。"""
        text = f"发送失败 {WEBHOOK_URL} 与 https://sctapi.ftqq.com/{SERVERCHAN_KEY}.send"
        out = redact_text(text)
        assert "abcd-1234-5678-9012" not in out
        assert SERVERCHAN_KEY not in out
        assert "发送失败" in out
        assert "qyapi.weixin.qq.com" in out

    def test_plain_text_fast_path(self):
        """不含 http 的文本直接原样返回。"""
        assert redact_text("no links here") == "no links here"

    def test_empty_text(self):
        assert redact_text("") == ""


class TestGlobalRedaction:
    def test_warning_with_url_arg_is_redacted(self, redaction, caplog):
        """真实场景：把 httpx 异常（消息含完整 URL）作为 %s 参数记录，密钥不得出现。"""
        exc = RuntimeError(
            f"Client error '404 Not Found' for url '{WEBHOOK_URL}'"
        )
        logger = logging.getLogger("family_os.kernel.notifier")
        with caplog.at_level(logging.WARNING):
            logger.warning("[wechat] webhook post failed: %s", exc)

        assert "abcd-1234-5678-9012" not in caplog.text
        assert "qyapi.weixin.qq.com" in caplog.text

    def test_url_in_arg_is_redacted(self, redaction, caplog):
        """密钥写在参数里（真实写法：整条 URL 作为 %s 参数）时被掩码。"""
        logger = logging.getLogger("family_os.test.logging")
        with caplog.at_level(logging.WARNING):
            logger.warning("[serverchan] 推送失败 %s",
                           f"https://sctapi.ftqq.com/{SERVERCHAN_KEY}.send")

        assert SERVERCHAN_KEY not in caplog.text
        assert "sctapi.ftqq.com" in caplog.text

    def test_percent_placeholder_does_not_break_formatting(self, redaction, caplog):
        """回归：URL 里带 %s 占位符时，脱敏不得吃掉占位符导致格式化报错。

        注意（已知边界）：若 URL 被占位符切成两段——前缀在 msg、密钥在参数里
        （如 msg="POST https://host/%s" + args=("<KEY>.send",)）——参数那半段不含
        "http" 因而不会被掩码。代码里不存在这种写法：所有日志传的都是完整 URL
        或异常对象，二者均已覆盖。此处只守护「不崩」这一条。
        """
        logger = logging.getLogger("family_os.test.logging")
        with caplog.at_level(logging.WARNING):
            logger.warning("POST https://sctapi.ftqq.com/%s.send 失败",
                           f"{SERVERCHAN_KEY}.send")

        assert "失败" in caplog.text      # 格式化成功（未抛 TypeError）
        assert ".send" in caplog.text     # 占位符确实被参数替换

    def test_non_string_args_preserved(self, redaction, caplog):
        """非字符串参数（数字）不被破坏。"""
        logger = logging.getLogger("family_os.test.logging")
        with caplog.at_level(logging.WARNING):
            logger.warning("尝试 %d 次，%s", 3, "ok")
        assert "尝试 3 次" in caplog.text

    def test_install_is_idempotent(self, redaction):
        """重复安装不叠加包装。"""
        assert install_redaction() is False
