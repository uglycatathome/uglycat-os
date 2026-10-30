"""Configuration service for Family AI OS.

Provides typed Pydantic models for all config sections and a ``Config``
class that loads everything from a YAML file + environment variables.
"""

from pathlib import Path
from typing import List

import yaml
from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict


# ── YAML-backed configuration models ───────────────────────────────────────

class AppConfig(BaseModel):
    """Application-level paths and metadata."""

    name: str = "Family AI OS"
    data_dir: str = "./data"
    log_dir: str = "./logs"
    prompt_dir: str = "./prompts"


class LLMConfig(BaseModel):
    """LLM provider settings."""

    default_model: str = "gpt-4o-mini"
    reasoning_model: str = "gpt-4o"
    monthly_budget_usd: float = 10.0
    cache_enabled: bool = True
    cache_ttl_hours: int = 24
    budget_warn_threshold: float = 0.8
    budget_cutoff_threshold: float = 1.0
    model_pricing: dict = {}
    # 附加 LLM provider（如硅基流动），含 base_url / api_key_env
    providers: dict = {}
    # 每日新闻解读插件专用模型链（免费模型，配 model_pricing 0 价）
    news_digest_model: str = "Qwen/Qwen3-8B"
    news_digest_fallback_models: list[str] = []

class PluginConfig(BaseModel):
    """Plugin discovery and enablement settings."""

    enabled: list[str] = []
    auto_discover: bool = True
    plugin_dir: str = "./plugins"


class ChannelConfig(BaseModel):
    """A single notification channel.

    ``serverchan`` 渠道（2026-10-18 新增）把家庭群消息镜像到个人微信，
    其专用字段如下；其他渠道类型忽略这些字段。
    """

    type: str = "file"
    enabled: bool = True
    # serverchan 专用：镜像哪些群的 webhook 消息、SendKey 的环境变量名、
    # 标题最大字数、desp 最大字节数
    mirror_groups: list[str] = ["family"]
    sendkey_env: str = "SERVERCHAN_SENDKEY"
    title_max_chars: int = 32
    max_desp_bytes: int = 20000


class NotifierConfig(BaseModel):
    """Notification channel configuration."""

    channels: list[ChannelConfig] = []


class SchedulerConfig(BaseModel):
    """Background scheduler settings."""

    backend: str = "apscheduler"
    timezone: str = "UTC"


class DigestConfig(BaseModel):
    """Digest generation schedule and mode."""

    schedule: str = "0 8 * * *"
    mode: str = "daily"


class ApprovalConfig(BaseModel):
    """Approval workflow settings."""

    auto_approve_free: bool = True
    require_approval_for: list[str] = []


class WebhookConfig(BaseModel):
    """A named webhook target with label and env-key reference.

    The actual URL lives in ``.env`` under the key given by *env_key*.
    """

    name: str = ""  # populated from YAML dict key at load time
    label: str
    description: str = ""
    env_key: str


class PluginInstanceConfig(BaseModel):
    """Per-plugin scheduling and notification configuration.

    Replaces the old ``Skill.schedule`` class attribute with a
    config-driven approach so non-developers can adjust schedules and
    notification targets without touching Python code.
    """

    enabled: bool = True
    schedule: str | None = None
    notify_on: list[str] = []


# ── Environment-variable-backed secrets model ──────────────────────────────

class Secrets(BaseSettings):
    """Sensitive credentials loaded from ``.env`` file.

    File is resolved relative to the current working directory.
    Extra environment variables are silently ignored.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    deepseek_api_key: str | None = None
    openai_api_key: str | None = None
    anthropic_api_key: str | None = None
    siliconflow_api_key: str | None = None
    # 智谱 BigModel（glm-4.7-flash / glm-4-flash-250414 为 0 价免费模型）
    zhipu_api_key: str | None = None
    wechat_family_url: str | None = None
    wechat_invest_url: str | None = None
    # Server酱（ServerChan Turbo）SendKey：家庭群消息镜像到个人微信
    serverchan_sendkey: str | None = None
    smtp_host: str | None = None
    smtp_port: int | None = None
    smtp_user: str | None = None
    smtp_password: str | None = None


# ── Unified Config loader ──────────────────────────────────────────────────

class Config:
    """Aggregate configuration, combining YAML sections with secrets from env."""

    def __init__(
        self,
        app: AppConfig,
        llm: LLMConfig,
        plugins: PluginConfig,
        scheduler: SchedulerConfig,
        notifier: NotifierConfig,
        digest: DigestConfig,
        approval: ApprovalConfig,
        secrets: Secrets,
        webhooks: dict[str, WebhookConfig] | None = None,
        plugin_instances: dict[str, PluginInstanceConfig] | None = None,
    ) -> None:
        self._app = app
        self._llm = llm
        self._plugins = plugins
        self._scheduler = scheduler
        self._notifier = notifier
        self._digest = digest
        self._approval = approval
        self._secrets = secrets
        self._webhooks = webhooks or {}
        self._plugin_instances = plugin_instances or {}

    # -- properties ----------------------------------------------------------

    @property
    def app(self) -> AppConfig:
        return self._app

    @property
    def llm(self) -> LLMConfig:
        return self._llm

    @property
    def plugins(self) -> PluginConfig:
        return self._plugins

    @property
    def scheduler(self) -> SchedulerConfig:
        return self._scheduler

    @property
    def notifier(self) -> NotifierConfig:
        return self._notifier

    @property
    def digest(self) -> DigestConfig:
        return self._digest

    @property
    def approval(self) -> ApprovalConfig:
        return self._approval

    @property
    def secrets(self) -> Secrets:
        return self._secrets

    @property
    def webhooks(self) -> dict[str, WebhookConfig]:
        return self._webhooks

    @property
    def plugin_instances(self) -> dict[str, PluginInstanceConfig]:
        return self._plugin_instances

    # -- factory -------------------------------------------------------------

    @classmethod
    def load(cls, config_path: str | Path) -> "Config":
        """Load configuration from a YAML file plus environment variables.

        Parameters
        ----------
        config_path : str | Path
            Path to the YAML configuration file.

        Returns
        -------
        Config
            Fully populated configuration object.

        Raises
        ------
        FileNotFoundError
            If *config_path* does not exist.
        """
        path = Path(config_path)
        if not path.exists():
            raise FileNotFoundError(f"Config file not found: {path}")

        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)

        app = AppConfig(**data.get("app", {}))
        llm = LLMConfig(**data.get("llm", {}))
        plugins = PluginConfig(**data.get("plugins", {}))

        scheduler_data = data.get("scheduler", {})
        scheduler = SchedulerConfig(**scheduler_data)

        notifier_data = data.get("notifier", {})
        channels = [ChannelConfig(**c) for c in notifier_data.get("channels", [])]
        notifier_obj = NotifierConfig(channels=channels)

        digest = DigestConfig(**data.get("digest", {}))
        approval = ApprovalConfig(**data.get("approval", {}))
        secrets = Secrets()

        # ── webhooks section ──────────────────────────────────────────────
        webhooks_data = data.get("webhooks", {}) or {}
        webhooks: dict[str, WebhookConfig] = {}
        for name, wh_data in webhooks_data.items():
            wh_data["name"] = name
            webhooks[name] = WebhookConfig(**wh_data)

        # ── plugin instances section ──────────────────────────────────────
        instances_data = data.get("plugins", {}).get("instances", {}) or {}
        plugin_instances: dict[str, PluginInstanceConfig] = {}
        for name, inst_data in instances_data.items():
            plugin_instances[name] = PluginInstanceConfig(**inst_data)

        return cls(
            app=app,
            llm=llm,
            plugins=plugins,
            scheduler=scheduler,
            notifier=notifier_obj,
            digest=digest,
            approval=approval,
            secrets=secrets,
            webhooks=webhooks,
            plugin_instances=plugin_instances,
        )
