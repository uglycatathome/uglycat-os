"""Tests for kernel config service — Config models and Config.load()."""

from pathlib import Path
import pytest

from family_os.kernel.config import (
    AppConfig,
    LLMConfig,
    PluginConfig,
    ChannelConfig,
    NotifierConfig,
    SchedulerConfig,
    DigestConfig,
    ApprovalConfig,
    Secrets,
    Config,
    WebhookConfig,
    PluginInstanceConfig,
)


# ── Model default-value tests ──────────────────────────────────────────────

class TestAppConfigDefaults:
    def test_defaults(self):
        cfg = AppConfig()
        assert cfg.name == "Family AI OS"
        assert cfg.data_dir == "./data"
        assert cfg.log_dir == "./logs"
        assert cfg.prompt_dir == "./prompts"


class TestLLMConfigDefaults:
    def test_defaults(self):
        cfg = LLMConfig()
        assert cfg.default_model == "gpt-4o-mini"
        assert cfg.reasoning_model == "gpt-4o"
        assert cfg.monthly_budget_usd == 10.0
        assert cfg.cache_enabled is True
        assert cfg.cache_ttl_hours == 24
        assert cfg.budget_warn_threshold == 0.8
        assert cfg.budget_cutoff_threshold == 1.0


class TestPluginConfigDefaults:
    def test_defaults(self):
        cfg = PluginConfig()
        assert cfg.enabled == []
        assert cfg.auto_discover is True
        assert cfg.plugin_dir == "./plugins"


class TestChannelConfigDefaults:
    def test_defaults(self):
        cfg = ChannelConfig()
        assert cfg.type == "file"
        assert cfg.enabled is True


class TestSchedulerConfigDefaults:
    def test_defaults(self):
        cfg = SchedulerConfig()
        assert cfg.backend == "apscheduler"
        assert cfg.timezone == "UTC"


class TestDigestConfigDefaults:
    def test_defaults(self):
        cfg = DigestConfig()
        assert cfg.schedule == "0 8 * * *"
        assert cfg.mode == "daily"


class TestApprovalConfigDefaults:
    def test_defaults(self):
        cfg = ApprovalConfig()
        assert cfg.auto_approve_free is True
        assert cfg.require_approval_for == []


class TestWebhookConfig:
    def test_defaults(self):
        cfg = WebhookConfig(label="Test", env_key="TEST_URL")
        assert cfg.label == "Test"
        assert cfg.description == ""
        assert cfg.env_key == "TEST_URL"


class TestPluginInstanceConfig:
    def test_defaults(self):
        cfg = PluginInstanceConfig()
        assert cfg.enabled is True
        assert cfg.schedule is None
        assert cfg.notify_on == []

    def test_disabled_with_schedule(self):
        cfg = PluginInstanceConfig(
            enabled=False, schedule="0 9 * * *", notify_on=["family"],
        )
        assert cfg.enabled is False
        assert cfg.schedule == "0 9 * * *"
        assert cfg.notify_on == ["family"]


# ── Config.load() tests ────────────────────────────────────────────────────

class TestConfigLoad:
    def test_load_returns_correct_values(self, sample_config_yaml):
        """Config.load() should parse YAML and return a Config with correct values."""
        config = Config.load(sample_config_yaml)

        # app section
        assert config.app.name == "Family AI OS"
        assert config.app.data_dir == "./data"
        assert config.app.log_dir == "./logs"
        assert config.app.prompt_dir == "./prompts"

        # plugins section
        assert config.plugins.enabled == ["sop_reminder"]
        assert config.plugins.auto_discover is True
        assert config.plugins.plugin_dir == "./plugins"

        # llm section
        assert config.llm.default_model == "gpt-4o-mini"
        assert config.llm.reasoning_model == "gpt-4o"
        assert config.llm.monthly_budget_usd == 10.0
        assert config.llm.cache_enabled is True
        assert config.llm.cache_ttl_hours == 24
        assert config.llm.budget_warn_threshold == 0.8
        assert config.llm.budget_cutoff_threshold == 1.0
        # 模型链字段：sample 未配置 → 应用默认值
        assert config.llm.news_digest_model == "Qwen/Qwen3-8B"

        # scheduler section
        assert config.scheduler.backend == "apscheduler"
        assert config.scheduler.timezone == "Asia/Shanghai"

        # notifier section
        assert len(config.notifier.channels) == 3
        assert config.notifier.channels[0].type == "file"
        assert config.notifier.channels[0].enabled is True
        assert config.notifier.channels[1].type == "email"
        assert config.notifier.channels[1].enabled is False
        assert config.notifier.channels[2].type == "wechat"
        assert config.notifier.channels[2].enabled is False

        # digest + approval: not in sample config → verify defaults apply
        assert config.digest.schedule == "0 8 * * *"
        assert config.approval.auto_approve_free is True
        assert config.approval.require_approval_for == []

        # secrets are a Secrets instance
        assert isinstance(config.secrets, Secrets)

    def test_load_missing_file_raises_error(self, temp_dir):
        """Loading a non-existent config file should raise FileNotFoundError."""
        missing = temp_dir / "does_not_exist.yaml"
        with pytest.raises(FileNotFoundError):
            Config.load(missing)

    def test_load_with_string_path(self, sample_config_yaml):
        """Config.load() should accept a plain str path."""
        config = Config.load(str(sample_config_yaml))
        assert config.app.name == "Family AI OS"

    def test_webhooks_parsed(self, sample_config_yaml):
        config = Config.load(sample_config_yaml)
        assert "family" in config.webhooks
        assert config.webhooks["family"].env_key == "WECHAT_FAMILY_URL"
        assert config.webhooks["family"].label == "🏠 家庭群"

    def test_plugin_instances_parsed(self, sample_config_yaml):
        config = Config.load(sample_config_yaml)
        sr = config.plugin_instances["sop_reminder"]
        assert sr.enabled is False
        assert sr.schedule is None
        assert sr.notify_on == ["family"]

    def test_empty_webhooks_when_missing(self, temp_dir):
        import yaml
        config_data = {"app": {"name": "test"}, "plugins": {"enabled": [], "auto_discover": True, "plugin_dir": "."}}
        cfg_path = temp_dir / "minimal.yaml"
        with open(cfg_path, "w") as f:
            yaml.dump(config_data, f)
        config = Config.load(cfg_path)
        assert config.webhooks == {}
        assert config.plugin_instances == {}


# ── Secrets tests ──────────────────────────────────────────────────────────

class TestSecrets:
    def test_reads_from_env(self, monkeypatch):
        """Secrets should read values set via environment variables."""
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test123")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test456")
        monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
        monkeypatch.setenv("SMTP_PORT", "587")
        monkeypatch.setenv("SMTP_USER", "user@example.com")
        monkeypatch.setenv("SMTP_PASSWORD", "secret")
        monkeypatch.setenv("WECHAT_FAMILY_URL", "https://qyapi.weixin.qq.com/webhook/send?key=xxx")

        secrets = Secrets()
        assert secrets.openai_api_key == "sk-test123"
        assert secrets.anthropic_api_key == "sk-ant-test456"
        assert secrets.smtp_host == "smtp.example.com"
        assert secrets.smtp_port == 587
        assert secrets.smtp_user == "user@example.com"
        assert secrets.smtp_password == "secret"
        assert secrets.wechat_family_url == "https://qyapi.weixin.qq.com/webhook/send?key=xxx"

    def test_defaults_are_none(self):
        """Unset env vars should result in None values.

        ``_env_file=None`` ignores the repo's real ``.env`` (which contains
        live webhook/API keys) so the test is deterministic in any checkout.
        """
        secrets = Secrets(_env_file=None)
        assert secrets.openai_api_key is None
        assert secrets.anthropic_api_key is None
        assert secrets.smtp_host is None
        assert secrets.smtp_port is None
        assert secrets.smtp_user is None
        assert secrets.smtp_password is None
        assert secrets.wechat_family_url is None

    def test_extra_env_vars_ignored(self, monkeypatch):
        """Extra env vars not declared in Secrets should be ignored."""
        monkeypatch.setenv("OPENAI_API_KEY", "sk-valid")
        monkeypatch.setenv("UNEXPECTED_VAR", "should_not_cause_error")
        secrets = Secrets()
        assert secrets.openai_api_key == "sk-valid"
