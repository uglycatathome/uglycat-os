"""Tests for the Notifier kernel service."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import httpx
import pytest

from family_os.kernel.config import (
    AppConfig,
    ChannelConfig,
    Config,
    NotifierConfig,
    PluginInstanceConfig,
    Secrets,
    WebhookConfig,
    LLMConfig,
    PluginConfig,
    SchedulerConfig,
    DigestConfig,
    ApprovalConfig,
)
from family_os.kernel import outbox
from family_os.kernel.notifier import Notifier
from family_os.kernel.skill import Result


# ── Helpers ──────────────────────────────────────────────────────────────────

def _make_config(
    data_dir: str,
    channels: list[dict] | None = None,
    webhooks: dict[str, WebhookConfig] | None = None,
    plugin_instances: dict[str, PluginInstanceConfig] | None = None,
) -> Config:
    """Build a Config object with a custom data_dir and notifier channels."""
    if channels is None:
        channels = [{"type": "file", "enabled": True}]
    return Config(
        app=AppConfig(data_dir=data_dir),
        llm=LLMConfig(),
        plugins=PluginConfig(),
        scheduler=SchedulerConfig(),
        notifier=NotifierConfig(
            channels=[ChannelConfig(**c) for c in channels],
        ),
        digest=DigestConfig(),
        approval=ApprovalConfig(),
        # _env_file=None: don't read the repo's real .env (live webhook URLs)
        # so tests are deterministic in any checkout.
        secrets=Secrets(_env_file=None),
        webhooks=webhooks,
        plugin_instances=plugin_instances,
    )


def _dual_webhook_config() -> dict[str, WebhookConfig]:
    """Return the standard family + invest webhook map."""
    return {
        "family": WebhookConfig(
            name="family", label="家庭群", env_key="WECHAT_FAMILY_URL"
        ),
        "invest": WebhookConfig(
            name="invest", label="投资群", env_key="WECHAT_INVEST_URL"
        ),
    }


def _list_files(inbox: Path) -> list[Path]:
    """Return .md files in inbox, sorted by name."""
    return sorted(inbox.glob("*.md"))


# ── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture
def inbox(temp_dir):
    """Return the inbox subdirectory for a given temp_dir."""
    p = temp_dir / "inbox"
    p.mkdir(parents=True, exist_ok=True)
    return p


@pytest.fixture
def config_with_file_channel(temp_dir):
    """Config with file channel enabled, data_dir pointing to temp_dir."""
    return _make_config(data_dir=str(temp_dir))


@pytest.fixture
def notifier(config_with_file_channel):
    """Notifier instance backed by a file-only config."""
    return Notifier(config_with_file_channel)


# ── Tests: send_to_inbox ─────────────────────────────────────────────────────

class TestSendToInbox:
    """send_to_inbox must create a properly named Markdown file."""

    def test_creates_file_with_correct_name(self, notifier, temp_dir):
        """File should be named <YYYYMMDD_HHMMSS>_<plugin_name>.md."""
        result = Result(status="ok", data={"key": "value"})
        path = notifier.send_to_inbox(result, "test_plugin")

        assert path.exists()
        assert path.suffix == ".md"
        assert "test_plugin" in path.name
        # Name pattern: 8 digits _ 6 digits _ plugin.md
        parts = path.stem.split("_")
        assert len(parts[0]) == 8 and parts[0].isdigit()  # YYYYMMDD
        assert len(parts[1]) == 6 and parts[1].isdigit()  # HHMMSS

    def test_content_contains_plugin_name(self, notifier, temp_dir):
        """The markdown body should contain the plugin name."""
        result = Result(status="ok", data={"key": "value"})
        path = notifier.send_to_inbox(result, "test_plugin")

        content = path.read_text(encoding="utf-8")
        assert "test_plugin" in content
        assert "ok" in content

    def test_inbox_dir_created_if_not_exists(self, temp_dir):
        """send_to_inbox should create data/inbox/ if it doesn't exist."""
        cfg = _make_config(data_dir=str(temp_dir))
        n = Notifier(cfg)

        result = Result(status="ok")
        path = n.send_to_inbox(result, "test_plugin")

        assert path.exists()
        assert path.parent.name == "inbox"

    def test_result_with_suggestions_preserves_priority_icons(self, notifier, temp_dir):
        """Suggestions should render with red/yellow/green priority icons."""
        result = Result(
            status="ok",
            suggestions=[
                {"title": "Urgent fix", "priority": "high", "detail": "Do it now"},
                {"title": "Check later", "priority": "medium", "detail": "When free"},
                {"title": "Optional", "priority": "low", "detail": "Nice to have"},
            ],
        )
        path = notifier.send_to_inbox(result, "suggestion_test")
        content = path.read_text(encoding="utf-8")

        assert "🔴" in content  # high priority
        assert "🟡" in content  # medium priority
        assert "🟢" in content  # low priority
        assert "Urgent fix" in content
        assert "Check later" in content
        assert "Optional" in content

    def test_result_with_todos_formatted_correctly(self, notifier, temp_dir):
        """Todos should render as checklist items with deadline."""
        result = Result(
            status="ok",
            todos=[
                {"action": "Buy groceries", "deadline": "2025-01-15", "category": "shopping"},
                {"action": "Pay bills", "deadline": "2025-01-20", "category": "finance"},
            ],
        )
        path = notifier.send_to_inbox(result, "todo_test")
        content = path.read_text(encoding="utf-8")

        assert "- [ ] Buy groceries" in content
        assert "- [ ] Pay bills" in content
        assert "截止: 2025-01-15" in content or "deadline: 2025-01-15" in content
        assert "截止: 2025-01-20" in content or "deadline: 2025-01-20" in content

    def test_empty_result_writes_minimal_file(self, notifier, temp_dir):
        """A result with default values should produce a minimal file."""
        result = Result()
        path = notifier.send_to_inbox(result, "minimal")
        content = path.read_text(encoding="utf-8")

        assert "# " in content
        assert "minimal" in content
        assert "ok" in content
        # No suggestions or todos sections
        assert content.strip() != ""


# ── Tests: send ──────────────────────────────────────────────────────────────

class TestSend:
    """send() must dispatch to the correct channel implementations."""

    def test_file_channel_creates_inbox_file(self, notifier, temp_dir):
        """With a file channel enabled, send() should create an inbox file."""
        result = Result(status="ok", data={"message": "hello"})
        paths = notifier.send(result, "file_plugin")

        assert len(paths) == 1
        assert paths[0].exists()
        assert paths[0].suffix == ".md"

    def test_send_disabled_channels_skipped(self, temp_dir):
        """Disabled channels should not produce any files."""
        cfg = _make_config(
            data_dir=str(temp_dir),
            channels=[
                {"type": "file", "enabled": False},
            ],
        )
        n = Notifier(cfg)
        result = Result(status="ok")
        paths = n.send(result, "disabled_test")

        # No enabled channels, so no output
        inbox = Path(temp_dir) / "inbox"
        files = _list_files(inbox) if inbox.exists() else []
        assert len(files) == 0
        assert paths == []

    def test_email_channel_stub_does_not_raise(self, temp_dir):
        """Email channel should be a stub that logs and does not raise."""
        cfg = _make_config(
            data_dir=str(temp_dir),
            channels=[
                {"type": "file", "enabled": True},
                {"type": "email", "enabled": True},
            ],
        )
        n = Notifier(cfg)
        result = Result(status="ok", data={"test": True})
        # Should not raise any exception
        paths = n.send(result, "stub_test")
        # File channel still works
        assert len(paths) >= 1

    def test_wechat_skips_when_no_webhook_url(self, temp_dir, mocker):
        """WeChat channel should skip without error when webhook URL is missing."""
        mock_post = mocker.patch("family_os.kernel.notifier.httpx.post")
        cfg = _make_config(
            data_dir=str(temp_dir),
            channels=[
                {"type": "file", "enabled": True},
                {"type": "wechat", "enabled": True},
            ],
            webhooks=_dual_webhook_config(),
            plugin_instances={
                "wechat_no_url_test": PluginInstanceConfig(notify_on=["family"]),
            },
        )
        # secrets has no WECHAT_FAMILY_URL → _resolve_webhook_url returns None
        n = Notifier(cfg)
        result = Result(status="ok", data={"test": True})
        # Should not raise any exception and should not attempt a POST
        paths = n.send(result, "wechat_no_url_test")
        assert mock_post.call_count == 0
        # File channel still works
        assert len(paths) >= 1

    def test_wechat_sends_markdown_payload(self, temp_dir, mocker):
        """WeChat channel should POST a markdown message to the webhook URL."""
        fake_url = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=fake"
        mock_post = mocker.patch("family_os.kernel.notifier.httpx.post")

        cfg = _make_config(
            data_dir=str(temp_dir),
            channels=[
                {"type": "wechat", "enabled": True},
            ],
            webhooks=_dual_webhook_config(),
            plugin_instances={
                "test_plugin": PluginInstanceConfig(notify_on=["family"]),
            },
        )
        cfg._secrets = Secrets(wechat_family_url=fake_url)
        n = Notifier(cfg)

        result = Result(
            status="ok",
            suggestions=[{"title": "Test suggestion", "priority": "high"}],
            todos=[{"action": "Test todo", "deadline": "2026-08-01"}],
        )
        n.send(result, "test_plugin")

        # Verify httpx.post was called at least once
        assert mock_post.call_count >= 1, (
            f"Expected at least 1 httpx.post call, got {mock_post.call_count}"
        )

        # Check first call's URL and payload
        call_args = mock_post.call_args_list[0]

        # Check URL
        assert call_args[0][0] == fake_url

        # Check payload structure
        payload = call_args[1]["json"]
        assert payload["msgtype"] == "markdown"
        assert "test_plugin" in payload["markdown"]["content"]
        assert "Test suggestion" in payload["markdown"]["content"]
        assert "Test todo" in payload["markdown"]["content"]

    def test_wechat_handles_network_error(self, temp_dir, mocker):
        """Network errors from WeChat webhook must not propagate."""
        fake_url = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=fake"
        mocker.patch(
            "family_os.kernel.notifier.httpx.post",
            side_effect=httpx.ConnectError("connection refused"),
        )

        cfg = _make_config(
            data_dir=str(temp_dir),
            channels=[
                {"type": "wechat", "enabled": True},
            ],
            webhooks=_dual_webhook_config(),
            plugin_instances={
                "network_error_test": PluginInstanceConfig(notify_on=["family"]),
            },
        )
        cfg._secrets = Secrets(wechat_family_url=fake_url)
        n = Notifier(cfg)

        result = Result(status="ok")
        # Must not raise
        paths = n.send(result, "network_error_test")
        assert paths == []  # no file channel

    def test_multiple_channels(self, temp_dir):
        """Multiple enabled file channels should produce multiple files (though only one inbox file is created for file type)."""
        # Only one file channel is meaningful, but test that it works
        cfg = _make_config(
            data_dir=str(temp_dir),
            channels=[
                {"type": "file", "enabled": True},
                {"type": "file", "enabled": True},
            ],
        )
        n = Notifier(cfg)
        result = Result(status="ok", data={"num": 1})
        paths = n.send(result, "multi_test")
        # Each file channel call produces a Path
        assert len(paths) == 2
        for p in paths:
            assert p.exists()


# ── Tests: _resolve_webhook_url ────────────────────────────────────────────


class TestResolveWebhookUrl:
    """Direct tests for ``_resolve_webhook_url``."""

    def test_known_webhook_returns_configured_url(self, temp_dir):
        cfg = _make_config(
            data_dir=str(temp_dir),
            webhooks=_dual_webhook_config(),
        )
        cfg._secrets = Secrets(wechat_family_url="https://example.com/family")
        n = Notifier(cfg)
        assert n._resolve_webhook_url("family") == "https://example.com/family"

    def test_unknown_webhook_returns_none(self, temp_dir):
        n = Notifier(_make_config(data_dir=str(temp_dir)))
        assert n._resolve_webhook_url("missing") is None

    def test_unconfigured_url_returns_none(self, temp_dir):
        cfg = _make_config(
            data_dir=str(temp_dir),
            webhooks=_dual_webhook_config(),
        )
        # secrets has no WECHAT_FAMILY_URL value → URL not configured
        n = Notifier(cfg)
        assert n._resolve_webhook_url("family") is None


# ── Tests: _format_for_wechat ──────────────────────────────────────────────


class TestFormatForWechat:
    """Direct tests for ``_format_for_wechat`` and its helpers."""

    def test_returns_list(self):
        """``_format_for_wechat`` must return a list of strings."""
        result = Result(status="ok")
        messages = Notifier._format_for_wechat(result, "test_plugin")
        assert isinstance(messages, list)
        assert len(messages) >= 1
        assert all(isinstance(m, str) for m in messages)

    def test_all_messages_within_byte_limit(self):
        """Every returned message must be ≤ 4000 UTF-8 bytes."""
        result = Result(
            status="ok",
            suggestions=[
                {"title": "S" * 200, "priority": "high", "detail": "D" * 500}
                for _ in range(10)
            ],
            todos=[
                {"action": f"Action {i}", "deadline": "2026-12-31",
                 "category": "test"}
                for i in range(50)
            ],
        )
        messages = Notifier._format_for_wechat(result, "test_plugin")
        for msg in messages:
            assert len(msg.encode("utf-8")) <= 4000, (
                f"Message is {len(msg.encode('utf-8'))} bytes"
            )

    def test_dynamic_todos_all_fit(self):
        """When todos fit within byte budget, all should be included."""
        todos = [
            {"action": f"Task {i}", "deadline": "2026-12-31", "category": "test"}
            for i in range(14)
        ]
        result = Result(status="ok", todos=todos)
        messages = Notifier._format_for_wechat(result, "sop_reminder")
        primary = messages[0]

        # All 14 todos should be present (they're short enough to fit)
        for i in range(14):
            assert f"Task {i}" in primary, f"Task {i} missing from message"

        # No truncation note should appear
        assert "... 共" not in primary

    def test_todos_truncated_with_count_note(self):
        """When too many todos, show count note with ellipsis."""
        # Create many very long todos to force truncation
        todos = [
            {"action": f"Task {i}: " + "X" * 200,
             "deadline": "2026-12-31", "category": "test"}
            for i in range(50)
        ]
        result = Result(status="ok", todos=todos)
        messages = Notifier._format_for_wechat(result, "test_plugin")
        primary = messages[0]

        # Count note must appear when not all fit
        assert "共 50 项" in primary



    def test_safe_trim_preserves_utf8_boundaries(self):
        """``_safe_trim`` must not corrupt multi-byte UTF-8 characters."""
        # Build text where 4000-byte boundary falls mid-character
        # Each Chinese character is 3 bytes in UTF-8
        text = "中" * 2000  # 6000 bytes — well over limit
        trimmed = Notifier._safe_trim(text, max_bytes=4000)

        # Must be valid UTF-8
        trimmed.encode("utf-8")  # would raise if invalid

        # Must end with truncation marker
        assert trimmed.endswith("... (已截断)")

        # Must be ≤ max_bytes
        assert len(trimmed.encode("utf-8")) <= 4000

    def test_safe_trim_short_text_unchanged(self):
        """Text under the byte limit must not be modified."""
        text = "Hello, 世界!"
        trimmed = Notifier._safe_trim(text, max_bytes=4000)
        assert trimmed == text

    def test_safe_trim_cuts_at_newline(self):
        """When possible, ``_safe_trim`` should break at the last newline."""
        # Build text: many lines, where newline is at ~75% of the limit
        lines = [f"Line {i}: " + "x" * 80 for i in range(80)]
        text = "\n".join(lines)
        trimmed = Notifier._safe_trim(text, max_bytes=4000)

        # The cut should be at a newline if one exists near the boundary
        trimmed.encode("utf-8")  # valid UTF-8
        assert len(trimmed.encode("utf-8")) <= 4000


# ── Tests: _send_wechat multi-message ──────────────────────────────────────


class TestSendWechatMultiMessage:
    """Tests for config-driven multi-webhook WeChat sending."""

    def test_sends_multiple_messages(self, temp_dir, mocker):
        """``_send_wechat`` must POST once per configured webhook."""
        fake_family = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=family"
        fake_invest = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=invest"
        mock_post = mocker.patch("family_os.kernel.notifier.httpx.post")

        cfg = _make_config(
            data_dir=str(temp_dir),
            channels=[{"type": "wechat", "enabled": True}],
            webhooks=_dual_webhook_config(),
            plugin_instances={
                "market_pulse": PluginInstanceConfig(notify_on=["invest", "family"]),
            },
        )
        cfg._secrets = Secrets(
            wechat_family_url=fake_family,
            wechat_invest_url=fake_invest,
        )
        n = Notifier(cfg)

        result = Result(
            status="ok",
            data={
                "_messages": {
                    "invest": "## 📈 投资群消息",
                    "family": "## 🏠 家庭群消息",
                },
            },
        )
        n._send_wechat(result, "market_pulse")

        # One POST per webhook, in notify_on order
        assert mock_post.call_count == 2, (
            f"Expected 2 POST calls, got {mock_post.call_count}"
        )
        urls = [call[0][0] for call in mock_post.call_args_list]
        assert urls == [fake_invest, fake_family]
        # Each webhook received its own pre-built message
        contents = [
            call[1]["json"]["markdown"]["content"]
            for call in mock_post.call_args_list
        ]
        assert "投资群消息" in contents[0]
        assert "家庭群消息" in contents[1]

    def test_continues_on_single_message_failure(self, temp_dir, mocker):
        """If one webhook fails, remaining webhooks must still be sent."""
        fake_family = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=family"
        fake_invest = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=invest"

        # invest webhook fails, family webhook succeeds
        mock_post = mocker.patch(
            "family_os.kernel.notifier.httpx.post",
            side_effect=[
                httpx.ConnectError("connection refused"),  # invest fails
                mocker.MagicMock(status_code=200),          # family succeeds
            ],
        )

        cfg = _make_config(
            data_dir=str(temp_dir),
            channels=[{"type": "wechat", "enabled": True}],
            webhooks=_dual_webhook_config(),
            plugin_instances={
                "market_pulse": PluginInstanceConfig(notify_on=["invest", "family"]),
            },
        )
        cfg._secrets = Secrets(
            wechat_family_url=fake_family,
            wechat_invest_url=fake_invest,
        )
        n = Notifier(cfg)

        result = Result(
            status="ok",
            data={"_messages": {"invest": "invest", "family": "family"}},
        )
        # Must not raise
        n._send_wechat(result, "market_pulse")

        # Both webhooks attempted despite the first failing
        assert mock_post.call_count == 2


class TestSendWechatSkipped:
    """status=skipped 的结果（如 strategy_radar 因 LLM 欠费不可用）应完全静默，
    不推送空内容标题消息（原因由 health_check 周报从 status.json 汇报）。"""

    def test_skipped_does_not_send_any_message(self, temp_dir, mocker):
        fake_url = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=fake"
        mock_post = mocker.patch("family_os.kernel.notifier.httpx.post")
        cfg = _make_config(
            data_dir=str(temp_dir),
            channels=[{"type": "wechat", "enabled": True}],
            webhooks=_dual_webhook_config(),
            plugin_instances={
                "strategy_radar": PluginInstanceConfig(notify_on=["family"]),
            },
        )
        cfg._secrets = Secrets(wechat_family_url=fake_url)
        n = Notifier(cfg)
        result = Result(status="skipped", data={"_messages": {}})
        n._send_wechat(result, "strategy_radar")
        assert mock_post.call_count == 0, "skipped 结果不应推送任何（含空标题）消息"


# ── Tests: serverchan channel（家庭群消息镜像到个人微信，2026-10-18）────────


SENDKEY = "SCT_test_sendkey"
SC_URL = f"https://sctapi.ftqq.com/{SENDKEY}.send"

FAMILY_MSG = "## 🌡️ A股市场温度v2\n> 10-18 18:30\n\n**A股市场温度：56.4°C**\n"


def _sc_config(temp_dir, channels=None, notify_on=("family", "invest"),
               with_sendkey=True):
    """Config：只开 serverchan 渠道（可选带 file），secrets 含测试 SendKey。"""
    cfg = _make_config(
        data_dir=str(temp_dir),
        channels=channels or [{"type": "serverchan", "enabled": True}],
        webhooks=_dual_webhook_config(),
        plugin_instances={
            "market_pulse": PluginInstanceConfig(notify_on=list(notify_on)),
        },
    )
    cfg._secrets = Secrets(
        _env_file=None,
        serverchan_sendkey=SENDKEY if with_sendkey else None,
    )
    return cfg


def _sc_ok(mocker):
    """httpx.post mock：返回 Server酱 成功响应 code=0。"""
    return mocker.patch(
        "family_os.kernel.notifier.httpx.post",
        return_value=mocker.Mock(status_code=200, json=lambda: {"code": 0}),
    )


class TestServerChanChannel:
    """serverchan 渠道：只镜像 family 群，内容与企业微信群那条完全一致。"""

    def test_mirrors_family_message_verbatim(self, temp_dir, mocker):
        """family 群消息 → 个人微信：URL 含 SendKey，desp 即群里那条 markdown。"""
        mock_post = _sc_ok(mocker)
        n = Notifier(_sc_config(temp_dir, channels=[
            {"type": "serverchan", "enabled": True},
        ]))
        result = Result(
            status="ok",
            data={"_messages": {"family": FAMILY_MSG, "invest": "## 📈 投资群"}},
        )
        n.send(result, "market_pulse")

        # 只镜像 family（invest 不镜像）→ 恰好一次调用
        assert mock_post.call_count == 1, mock_post.call_args_list
        call = mock_post.call_args
        assert call[0][0] == SC_URL
        assert call[1]["data"]["desp"] == FAMILY_MSG
        assert call[1]["data"]["title"] == "🌡️ A股市场温度v2"

    def test_invest_only_send_is_not_mirrored(self, temp_dir, mocker):
        """notify_groups 限定 invest（如 CLI --notify-invest）时不镜像。"""
        mock_post = _sc_ok(mocker)
        n = Notifier(_sc_config(temp_dir))
        result = Result(status="ok",
                        data={"_messages": {"family": FAMILY_MSG, "invest": "x"}})
        n.send(result, "market_pulse", notify_groups=["invest"])
        assert mock_post.call_count == 0

    def test_skipped_is_silent(self, temp_dir, mocker):
        """status=skipped（LLM 不可用/非交易日）→ 静默，与企微一致。"""
        mock_post = _sc_ok(mocker)
        n = Notifier(_sc_config(temp_dir))
        n.send(Result(status="skipped", data={"_messages": {}}), "market_pulse")
        assert mock_post.call_count == 0

    def test_missing_sendkey_skips_without_error(self, temp_dir, mocker):
        """SendKey 未配置 → 不发请求、不抛错，其他渠道不受影响。"""
        mock_post = _sc_ok(mocker)
        n = Notifier(_sc_config(
            temp_dir,
            channels=[{"type": "file", "enabled": True},
                      {"type": "serverchan", "enabled": True}],
            with_sendkey=False,
        ))
        paths = n.send(
            Result(status="ok", data={"_messages": {"family": FAMILY_MSG}}),
            "market_pulse",
        )
        assert mock_post.call_count == 0
        assert len(paths) == 1  # file 渠道照常工作

    def test_quota_error_is_logged_not_raised(self, temp_dir, mocker, caplog):
        """免费版 5 条/天用尽（code!=0）→ 只记 warning，file 渠道照常成功。"""
        mocker.patch(
            "family_os.kernel.notifier.httpx.post",
            return_value=mocker.Mock(
                status_code=200,
                json=lambda: {"code": 40001, "message": "今日发送条数已达上限",
                              "data": {"error": "ERR_LIMIT"}},
            ),
        )
        n = Notifier(_sc_config(
            temp_dir,
            channels=[{"type": "file", "enabled": True},
                      {"type": "serverchan", "enabled": True}],
        ))
        with caplog.at_level("WARNING"):
            paths = n.send(
                Result(status="ok", data={"_messages": {"family": FAMILY_MSG}}),
                "market_pulse",
            )
        assert len(paths) == 1
        assert any("serverchan" in r.message and "推送失败" in r.message
                   for r in caplog.records)

    def test_network_error_is_logged_not_raised(self, temp_dir, mocker):
        mocker.patch(
            "family_os.kernel.notifier.httpx.post",
            side_effect=httpx.ConnectError("connection refused"),
        )
        n = Notifier(_sc_config(temp_dir))
        # 不抛异常即通过
        n.send(Result(status="ok", data={"_messages": {"family": FAMILY_MSG}}),
               "market_pulse")

    def test_long_message_is_trimmed(self, temp_dir, mocker):
        """desp 超 max_desp_bytes → 截断并标注（不会超 Server酱 限制）。"""
        mock_post = _sc_ok(mocker)
        n = Notifier(_sc_config(
            temp_dir,
            channels=[{"type": "serverchan", "enabled": True,
                       "max_desp_bytes": 200}],
        ))
        long_msg = "## 长消息标题\n\n" + "内容" * 300
        n.send(Result(status="ok", data={"_messages": {"family": long_msg}}),
               "market_pulse")
        desp = mock_post.call_args[1]["data"]["desp"]
        assert len(desp.encode("utf-8")) <= 200
        assert "已截断" in desp


class TestServerChanTitle:
    """title 取消息首行（去 markdown 符号），≤32 字，空则退回插件名。"""

    def test_strips_markdown_prefix(self):
        assert Notifier._serverchan_title("## 🌡️ A股市场温度v2\n\n正文") \
            == "🌡️ A股市场温度v2"
        assert Notifier._serverchan_title("> 引用行") == "引用行"
        assert Notifier._serverchan_title("**加粗首行**\n") == "加粗首行"

    def test_truncates_to_32_chars(self):
        title = Notifier._serverchan_title("## " + "超" * 60)
        assert len(title) == 32
        assert title.endswith("…")

    def test_falls_back_to_plugin_name(self):
        assert Notifier._serverchan_title("\n\n", fallback="health_check") \
            == "health_check"


# ── Tests: Server酱加固（2026-11-11）────────────────────────────────────────
# 事故：当日 08:04 境外 VM 到 sctapi.ftqq.com 的 TLS 握手超时，旧实现 10s 超时
# + 「失败只记 warning 不重试」→ 个人微信消息永久静默丢失。


class TestServerChanHardening:
    """超时放宽 / 分类重试 / outbox 补推 / 可见告警。"""

    @staticmethod
    def _result(msg=FAMILY_MSG):
        return Result(status="ok", data={"_messages": {"family": msg}})

    @staticmethod
    def _sc_no_retry(**extra):
        ch = {"type": "serverchan", "enabled": True,
              "connect_retries": 0, "retry_delay_s": 0.0}
        ch.update(extra)
        return [ch]

    def test_channel_defaults(self):
        ch = ChannelConfig(type="serverchan")
        assert ch.timeout_s == 25.0
        assert ch.connect_retries == 1
        assert ch.outbox_max_age_hours == 24
        assert ch.outbox_max_attempts == 3
        assert ch.alert_enabled is True

    def test_timeout_is_configurable_and_default_25s(self, temp_dir, mocker):
        mock_post = _sc_ok(mocker)
        Notifier(_sc_config(temp_dir)).send(self._result(), "market_pulse")
        assert mock_post.call_args[1]["timeout"] == 25.0

        mock_post.reset_mock()
        n2 = Notifier(_sc_config(temp_dir, channels=[
            {"type": "serverchan", "enabled": True, "timeout_s": 30.0}]))
        n2.send(self._result(), "market_pulse")
        assert mock_post.call_args[1]["timeout"] == 30.0

    @pytest.mark.parametrize("exc", [
        httpx.ConnectTimeout("connect timed out"),
        httpx.ConnectError("_ssl.c:983: The handshake operation timed out"),
        httpx.ReadTimeout("_ssl.c:983: The handshake operation timed out"),
        httpx.PoolTimeout("pool exhausted"),
    ])
    def test_connect_phase_error_is_retried_and_recovers(
            self, temp_dir, mocker, exc):
        """连接/握手阶段失败 → 重试一次成功；SSL 握手超时被包成 ReadTimeout 也要重试。"""
        resp = mocker.Mock(status_code=200, json=lambda: {"code": 0})
        mock_post = mocker.patch("family_os.kernel.notifier.httpx.post",
                                 side_effect=[exc, resp])
        n = Notifier(_sc_config(temp_dir, channels=[
            {"type": "serverchan", "enabled": True, "retry_delay_s": 0.0}]))
        n.send(self._result(), "market_pulse")
        assert mock_post.call_count == 2
        assert not outbox.iter_entries(temp_dir), "重试成功不应留下 outbox 条目"

    @pytest.mark.parametrize("exc", [
        httpx.ReadTimeout("read timed out"),
        httpx.WriteTimeout("write timed out"),
    ])
    def test_read_write_timeout_is_never_retried_and_marked_uncertain(
            self, temp_dir, mocker, exc):
        """读/写超时＝服务端可能已收单：绝不重试，落 uncertain 不自动补推。"""
        mock_post = mocker.patch("family_os.kernel.notifier.httpx.post",
                                 side_effect=exc)
        n = Notifier(_sc_config(temp_dir, channels=self._sc_no_retry()))
        n.send(self._result(), "market_pulse")
        assert mock_post.call_count == 1
        entries = outbox.iter_entries(temp_dir)
        assert len(entries) == 1
        assert entries[0][1]["status"] == "uncertain"

    def test_quota_error_is_pending_not_uncertain(self, temp_dir, mocker):
        """服务端明确拒绝（code!=0）＝确定未送达，可安全自动补推。"""
        mocker.patch(
            "family_os.kernel.notifier.httpx.post",
            return_value=mocker.Mock(
                status_code=200,
                json=lambda: {"code": 40001, "message": "今日发送条数已达上限",
                              "data": {"error": "ERR_LIMIT"}}))
        n = Notifier(_sc_config(temp_dir, channels=self._sc_no_retry()))
        n.send(self._result(), "market_pulse")
        assert outbox.iter_entries(temp_dir)[0][1]["status"] == "pending"

    def test_pending_entry_is_auto_flushed_on_next_send(self, temp_dir, mocker):
        """下次推送前自动补推「确定未送达」的旧消息，成功后删除条目。"""
        resp = mocker.Mock(status_code=200, json=lambda: {"code": 0})
        mock_post = mocker.patch(
            "family_os.kernel.notifier.httpx.post",
            side_effect=[httpx.ConnectError("down"), resp, resp])
        n = Notifier(_sc_config(temp_dir, channels=self._sc_no_retry()))
        n.send(self._result(), "market_pulse")
        assert len(outbox.iter_entries(temp_dir)) == 1
        n.send(self._result(), "market_pulse")
        assert mock_post.call_count == 3, "1 次失败 + 1 次补推 + 1 次当次推送"
        assert not outbox.iter_entries(temp_dir)

    def test_uncertain_entry_is_never_auto_flushed(self, temp_dir, mocker):
        """uncertain 条目即使后续推送成功也不补发，避免重复推送。"""
        resp = mocker.Mock(status_code=200, json=lambda: {"code": 0})
        mock_post = mocker.patch(
            "family_os.kernel.notifier.httpx.post",
            side_effect=[httpx.ReadTimeout("read timed out"), resp])
        n = Notifier(_sc_config(temp_dir, channels=self._sc_no_retry()))
        n.send(self._result(), "market_pulse")
        n.send(self._result(), "market_pulse")
        assert mock_post.call_count == 2, "不应为 uncertain 条目自动补推"
        assert outbox.iter_entries(temp_dir)[0][1]["status"] == "uncertain"

    def test_failure_alert_is_sent_once_per_day_per_plugin(self, temp_dir, mocker):
        """失败升级为可见告警，同一天同一插件只发一次（防刷屏）。"""
        calls = []

        def fake_post(url, **kwargs):
            calls.append((url, kwargs))
            if "sctapi.ftqq.com" in url:
                raise httpx.ConnectError("down")
            return mocker.Mock(status_code=200)

        mocker.patch("family_os.kernel.notifier.httpx.post", side_effect=fake_post)
        cfg = _sc_config(temp_dir, channels=self._sc_no_retry())
        cfg._secrets = Secrets(
            _env_file=None,
            serverchan_sendkey=SENDKEY,
            wechat_family_url="https://qyapi.weixin.qq.com/test?key=ALERTKEY",
        )
        n = Notifier(cfg)
        n.send(self._result(), "market_pulse")
        n.send(self._result(), "market_pulse")
        alerts = [c for c in calls if "qyapi.weixin.qq.com" in c[0]]
        assert len(alerts) == 1
        assert "个人微信推送失败" in alerts[0][1]["json"]["markdown"]["content"]

    def test_sendkey_never_leaks_into_outbox(self, temp_dir, mocker):
        """httpx 异常里的完整 URL 含 SendKey，落盘前必须脱敏。"""
        mocker.patch(
            "family_os.kernel.notifier.httpx.post",
            side_effect=httpx.ConnectError(
                f"connection failed for url 'https://sctapi.ftqq.com/{SENDKEY}.send'"))
        n = Notifier(_sc_config(temp_dir, channels=self._sc_no_retry()))
        n.send(self._result(), "market_pulse")
        raw = json.dumps(outbox.iter_entries(temp_dir)[0][1], ensure_ascii=False)
        assert SENDKEY not in raw
        assert "sctapi.ftqq.com" in raw  # 域名保留，便于排查

    def test_flush_degrades_sending_residue_to_uncertain(self, temp_dir, mocker):
        """补推中途崩溃残留的 sending 条目一律降级，不再自动重发。"""
        sender = mocker.Mock()
        p = outbox.enqueue(temp_dir, plugin="p", group="family", title="t",
                           desp="d", reason="r")
        e = json.loads(p.read_text(encoding="utf-8"))
        e["status"] = "sending"
        outbox.update(p, e)
        stats = outbox.flush(temp_dir, sender)
        assert stats["skipped_uncertain"] == 1
        sender.assert_not_called()
        assert json.loads(p.read_text(encoding="utf-8"))["status"] == "uncertain"

    def test_flush_skips_expired_and_exhausted(self, temp_dir, mocker):
        sender = mocker.Mock(return_value=(True, False, ""))
        old = outbox.enqueue(temp_dir, plugin="p", group="family", title="old",
                             desp="d", reason="r")
        e = json.loads(old.read_text(encoding="utf-8"))
        e["created_at"] = (datetime.now() - timedelta(hours=48)).isoformat()
        outbox.update(old, e)
        outbox.enqueue(temp_dir, plugin="p", group="family", title="done",
                       desp="d", reason="r", attempts=3)
        stats = outbox.flush(temp_dir, sender, max_age_hours=24, max_attempts=3)
        assert stats["skipped_expired"] == 1
        assert stats["exhausted"] == 1
        sender.assert_not_called()

    def test_summarize_contract_for_health_check(self, temp_dir):
        clean = outbox.summarize(temp_dir)
        assert clean["status"] == "ok" and clean["ok"] and not clean["issues"]
        outbox.enqueue(temp_dir, plugin="news_digest", group="family",
                       title="t", desp="d", reason="r")
        outbox.enqueue(temp_dir, plugin="news_digest", group="family",
                       title="u", desp="d", reason="r", uncertain=True)
        dirty = outbox.summarize(temp_dir)
        assert dirty["status"] == "issue"
        assert any("待自动补推" in x for x in dirty["issues"])
        assert any("状态不明" in x for x in dirty["issues"])

    def test_connect_phase_classification(self):
        f = Notifier._is_connect_phase_error
        assert f(httpx.ConnectTimeout("x"))
        assert f(httpx.ConnectError("x"))
        assert f(httpx.PoolTimeout("x"))
        assert f(httpx.ReadTimeout("_ssl.c:983: The handshake operation timed out"))
        assert not f(httpx.ReadTimeout("read timed out"))
        assert not f(httpx.WriteTimeout("write timed out"))
