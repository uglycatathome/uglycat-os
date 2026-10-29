"""Tests for LLM Gateway — cost-control choke point for all LLM calls."""

import sys
import types
from unittest.mock import MagicMock

import pytest

from family_os.kernel.config import (
    AppConfig,
    ApprovalConfig,
    Config,
    DigestConfig,
    LLMConfig,
    NotifierConfig,
    PluginConfig,
    SchedulerConfig,
    Secrets,
)
from family_os.kernel.llm_gateway import LLMGateway
from family_os.kernel.store import Store


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def store(test_db_path):
    s = Store(test_db_path)
    s.initialize()
    return s


@pytest.fixture
def config():
    return Config(
        app=AppConfig(),
        llm=LLMConfig(),
        plugins=PluginConfig(),
        scheduler=SchedulerConfig(),
        notifier=NotifierConfig(),
        digest=DigestConfig(),
        approval=ApprovalConfig(),
        secrets=Secrets(),
    )


@pytest.fixture
def gateway(store, config):
    return LLMGateway(store, config)


@pytest.fixture
def fake_litellm():
    """Install a fake ``litellm`` module so ``ask()`` can import it.

    The fixture creates a ``types.ModuleType`` and registers it in
    ``sys.modules`` before each test that asks for it.  Tests can further
    customise attributes with ``monkeypatch`` or direct assignment on the
    returned module.
    """
    mod = types.ModuleType("litellm")
    mod.completion = MagicMock(
        return_value=MagicMock(
            choices=[MagicMock(message=MagicMock(content="Mocked reply"))],
            usage=MagicMock(prompt_tokens=10, completion_tokens=20),
        )
    )
    mod.cost_per_token = MagicMock(return_value={"total_cost": 0.001})
    sys.modules["litellm"] = mod
    return mod


# ── Initialisation ────────────────────────────────────────────────────────────


class TestGatewayInit:
    """Gateway construction and initial state."""

    def test_gateway_init(self, gateway):
        """Gateway should hold references to store and config."""
        assert gateway is not None
        assert gateway._store is not None
        assert gateway._config is not None

    def test_initial_cost_zero(self, gateway):
        """No calls made yet, so monthly cost is 0.0."""
        assert gateway.get_monthly_cost() == 0.0

    def test_budget_not_exceeded_initially(self, gateway):
        """With zero cost and a positive budget, budget is not exceeded."""
        assert gateway.is_budget_exceeded() is False


# ── Cost logging & budget ─────────────────────────────────────────────────────


class TestBudget:
    """Budget enforcement and cost tracking."""

    def test_log_call(self, gateway):
        """_log_call should persist a record and increment the call count."""
        gateway._log_call("test_plugin", "gpt-4o", 100, 50, 0.003)
        assert gateway._store.get_total_llm_calls() == 1

    def test_budget_exceeded(self, gateway):
        """At $10 cost with $10 budget, budget is exceeded (ratio >= 1.0)."""
        gateway._log_call("test", "gpt-4o", 1000, 500, 10.0)
        assert gateway.is_budget_exceeded() is True

    def test_budget_warning(self, gateway):
        """At $8 cost with $10 budget, the ratio should be >= 0.8."""
        gateway._log_call("test", "gpt-4o", 800, 400, 8.0)
        ratio = gateway.get_monthly_cost() / gateway._config.llm.monthly_budget_usd
        assert ratio >= 0.8

    def test_multiple_calls_sum_cost(self, gateway):
        """Multiple logged calls should accumulate cost."""
        gateway._log_call("a", "gpt-4o", 100, 50, 2.0)
        gateway._log_call("b", "gpt-4o", 100, 50, 3.0)
        assert gateway.get_monthly_cost() == 5.0


# ── Model routing ─────────────────────────────────────────────────────────────


class TestModelRouting:
    """Model tier resolution."""

    def test_model_routing_default(self, gateway):
        """"default" tier should resolve to ``default_model``."""
        assert gateway._resolve_model("default") == gateway._config.llm.default_model

    def test_model_routing_reasoning(self, gateway):
        """"reasoning" tier should resolve to ``reasoning_model``."""
        assert (
            gateway._resolve_model("reasoning")
            == gateway._config.llm.reasoning_model
        )

    def test_model_routing_custom(self, gateway):
        """An unrecognised tier should be returned verbatim."""
        assert gateway._resolve_model("gpt-4-turbo") == "gpt-4-turbo"

    def test_provider_for_model_siliconflow_chains(self, store):
        """strategy_radar/news_digest 模型链上的模型（含 deepseek 子串的 R1）→ siliconflow。"""
        from family_os.kernel.llm_gateway import LLMGateway

        cfg = Config(
            app=AppConfig(), llm=LLMConfig(
                news_digest_model="Qwen/Qwen3-8B",
                news_digest_fallback_models=["Qwen/Qwen3.5-4B",
                                             "deepseek-ai/DeepSeek-R1-0528-Qwen3-8B"],
                strategy_radar_model="Qwen/Qwen3-8B",
                strategy_radar_fallback_models=["deepseek-ai/DeepSeek-R1-0528-Qwen3-8B",
                                                "Qwen/Qwen3.5-4B"],
            ),
            plugins=PluginConfig(), scheduler=SchedulerConfig(),
            notifier=NotifierConfig(), digest=DigestConfig(),
            approval=ApprovalConfig(), secrets=Secrets(),
        )
        gw = LLMGateway(store, cfg)

        # strategy_radar 链主模型 + 降级模型全部路由到 siliconflow
        assert gw._provider_for_model("Qwen/Qwen3-8B") == "siliconflow"
        assert gw._provider_for_model("Qwen/Qwen3.5-4B") == "siliconflow"
        # R1-0528 虽含 deepseek 子串，但由硅基流动托管 → siliconflow
        assert gw._provider_for_model("deepseek-ai/DeepSeek-R1-0528-Qwen3-8B") == "siliconflow"
        # 非链上 deepseek 模型 → DeepSeek 原生网关
        assert gw._provider_for_model("deepseek/deepseek-v4-flash") == "deepseek"
        # 无关模型 → None
        assert gw._provider_for_model("gpt-4o-mini") is None


# ── 智谱（BigModel）免费档适配 ────────────────────────────────────────────────


class TestZhipuProvider:
    """2026-10-24：glm-4.7-flash 作为 news_digest 主模型。

    免费档特性：并发 1（网关加锁）、必须 thinking disabled（否则空内容）、
    高峰 1305 需退避重试。全部用 fake litellm 验证，不打真实网络。
    """

    @staticmethod
    def _gw(store) -> LLMGateway:
        cfg = Config(
            app=AppConfig(),
            llm=LLMConfig(
                news_digest_model="glm-4.7-flash",
                news_digest_fallback_models=["glm-4-flash-250414", "Qwen/Qwen3-8B"],
                providers={"zhipu": {
                    "base_url": "https://open.bigmodel.cn/api/paas/v4",
                    "api_key_env": "ZHIPU_API_KEY",
                }},
            ),
            plugins=PluginConfig(), scheduler=SchedulerConfig(),
            notifier=NotifierConfig(), digest=DigestConfig(),
            approval=ApprovalConfig(),
            secrets=Secrets(_env_file=None, zhipu_api_key="sk-test"),
        )
        return LLMGateway(store, cfg)

    def test_glm_models_route_to_zhipu_even_if_in_chain(self, store):
        """glm-* 即使出现在 news_digest 链里，也必须走 zhipu 而不是 siliconflow。"""
        gw = self._gw(store)
        assert gw._provider_for_model("glm-4.7-flash") == "zhipu"
        assert gw._provider_for_model("glm-4-flash-250414") == "zhipu"
        assert gw._provider_for_model("Qwen/Qwen3-8B") == "siliconflow"

    def test_ask_uses_openai_route_and_disables_thinking(self, store, fake_litellm):
        """必须走 OpenAI 兼容路由，并强制 thinking disabled。"""
        gw = self._gw(store)
        gw.ask("hi", model_tier="glm-4.7-flash")

        kwargs = fake_litellm.completion.call_args.kwargs
        assert kwargs["custom_llm_provider"] == "openai"
        assert kwargs["api_base"] == "https://open.bigmodel.cn/api/paas/v4"
        assert kwargs["api_key"] == "sk-test"
        assert kwargs["extra_body"] == {"thinking": {"type": "disabled"}}
        assert kwargs["timeout"] == 90

    def test_empty_content_retries_then_raises(self, store, fake_litellm, monkeypatch):
        """HTTP 200 但 content 为空 = 失败（不能当成功返回）。"""
        gw = self._gw(store)
        fake_litellm.completion.return_value = MagicMock(
            choices=[MagicMock(message=MagicMock(content=""))],
            usage=MagicMock(prompt_tokens=1, completion_tokens=1),
        )
        monkeypatch.setattr("family_os.kernel.llm_gateway.time.sleep", lambda s: None)

        with pytest.raises(RuntimeError, match="智谱"):
            gw.ask("hi", model_tier="glm-4.7-flash")
        assert fake_litellm.completion.call_count == 2

    def test_rate_limit_fails_fast_without_retry(self, store, fake_litellm, monkeypatch):
        """1305/429 限流必须立即失败，交给上层降级（避免每次拖 60s+）。"""
        gw = self._gw(store)
        fake_litellm.completion.side_effect = RuntimeError("1305 访问量过大")
        monkeypatch.setattr("family_os.kernel.llm_gateway.time.sleep", lambda s: None)

        with pytest.raises(RuntimeError, match="智谱"):
            gw.ask("hi", model_tier="glm-4.7-flash")
        assert fake_litellm.completion.call_count == 1

    def test_retries_once_then_succeeds(self, store, fake_litellm, monkeypatch):
        """非限流类错误（如网络抖动）重试一次后成功。"""
        gw = self._gw(store)
        ok = MagicMock(
            choices=[MagicMock(message=MagicMock(content="正常输出"))],
            usage=MagicMock(prompt_tokens=1, completion_tokens=1),
        )
        fake_litellm.completion.side_effect = [RuntimeError("connection reset by peer"), ok]
        monkeypatch.setattr("family_os.kernel.llm_gateway.time.sleep", lambda s: None)

        assert gw.ask("hi", model_tier="glm-4.7-flash") == "正常输出"
        assert fake_litellm.completion.call_count == 2


# ── 硅基流动：Qwen3 必须保留思考 ──────────────────────────────────────────────


class TestSiliconflowThinking:
    """2026-10-24：验证过「关思考」提速（415s→12s），但长列表去重会失准
    （职业 199 篇被剔 138/28 条，两轮差异极大），故网关不得给 Qwen3 关思考。"""

    @staticmethod
    def _gw(store) -> LLMGateway:
        cfg = Config(
            app=AppConfig(),
            llm=LLMConfig(
                news_digest_model="Qwen/Qwen3-8B",
                providers={"siliconflow": {
                    "base_url": "https://api.siliconflow.cn/v1",
                    "api_key_env": "SILICONFLOW_API_KEY",
                }},
            ),
            plugins=PluginConfig(), scheduler=SchedulerConfig(),
            notifier=NotifierConfig(), digest=DigestConfig(),
            approval=ApprovalConfig(),
            secrets=Secrets(_env_file=None, siliconflow_api_key="sk-sf"),
        )
        return LLMGateway(store, cfg)

    def test_qwen3_keeps_thinking_enabled(self, store, fake_litellm):
        gw = self._gw(store)
        gw.ask("hi", model_tier="Qwen/Qwen3-8B")
        kwargs = fake_litellm.completion.call_args.kwargs
        assert kwargs["custom_llm_provider"] == "openai"
        assert kwargs["api_base"] == "https://api.siliconflow.cn/v1"
        assert "extra_body" not in kwargs


# ── PII Sanitization ──────────────────────────────────────────────────────────


class TestSanitize:
    """Sanitization removes sensitive patterns from text."""

    def test_sanitize_removes_openai_keys(self, gateway):
        """OpenAI ``sk-...`` keys should be replaced with [REDACTED]."""
        text = (
            "API key: sk-abc12345defghijklmnopqrstuvwxyz123456789 "
            "and more text"
        )
        result = gateway._sanitize(text)
        assert "sk-abc12345defghijklmnopqrstuvwxyz123456789" not in result
        assert "[REDACTED]" in result

    def test_sanitize_removes_anthropic_keys(self, gateway):
        """Anthropic ``sk-ant-...`` keys should be replaced with [REDACTED]."""
        text = (
            "key=sk-ant-api03-abcdefghijklmnopqrstuvwxyz123456789"
            "-abcdefghijklmnopqrstuvwxyz1234"
        )
        result = gateway._sanitize(text)
        assert "sk-ant" not in result
        assert "[REDACTED]" in result

    def test_sanitize_preserves_normal_text(self, gateway):
        """Normal Chinese text should pass through unchanged."""
        text = "你好，今天天气真不错！"
        result = gateway._sanitize(text)
        assert result == text

    def test_sanitize_removes_chinese_id(self, gateway):
        """18-digit Chinese ID numbers should be replaced."""
        text = "身份证号是110101199001011234"
        result = gateway._sanitize(text)
        assert "110101199001011234" not in result
        assert "[REDACTED]" in result

    def test_sanitize_removes_chinese_id_with_x(self, gateway):
        """Chinese ID ending in 'X' should also be redacted."""
        text = "ID: 11010119900101123X"
        result = gateway._sanitize(text)
        assert "[REDACTED]" in result
        assert "11010119900101123X" not in result

    def test_sanitize_removes_bank_card(self, gateway):
        """16-19 digit bank card numbers should be replaced."""
        text = "银行卡号 6222021234567890123"
        result = gateway._sanitize(text)
        assert "6222021234567890123" not in result
        assert "[REDACTED]" in result

    def test_sanitize_short_numbers_preserved(self, gateway):
        """Numbers shorter than 16 digits should be preserved."""
        text = "电话 13800138000"
        result = gateway._sanitize(text)
        assert result == text


# ── Cache ─────────────────────────────────────────────────────────────────────


class TestCache:
    """SQLite-backed response caching."""

    def test_cache_store_and_retrieve(self, gateway):
        """A cached response should be retrievable by cache key."""
        key = gateway._cache_key("hello", "default")
        gateway._store_cache(key, "world", "gpt-4o-mini")
        result = gateway._check_cache(key)
        assert result == "world"

    def test_cache_expired(self, gateway):
        """An entry stored with ttl_hours=0 should not be retrievable."""
        key = gateway._cache_key("expired_test", "default")
        # Insert with zero TTL so it expires immediately
        conn = gateway._store._connect()
        conn.execute(
            "INSERT OR REPLACE INTO llm_cache "
            "(cache_key, response, model, created_at, ttl_hours) "
            "VALUES (?, ?, ?, datetime('now'), 0)",
            (key, "expired_response", "gpt-4o-mini"),
        )
        conn.commit()
        conn.close()

        result = gateway._check_cache(key)
        assert result is None

    def test_cache_miss(self, gateway):
        """A non-existent key should return None."""
        result = gateway._check_cache("nonexistent_key")
        assert result is None

    def test_cache_overwrite(self, gateway):
        """Storing the same key again should replace the old value."""
        key = gateway._cache_key("overwrite_test", "default")
        gateway._store_cache(key, "old_value", "gpt-4o-mini")
        gateway._store_cache(key, "new_value", "gpt-4o-mini")
        result = gateway._check_cache(key)
        assert result == "new_value"

    def test_cache_key_consistency(self, gateway):
        """The same prompt+tier should always produce the same key."""
        k1 = gateway._cache_key("Hello world", "default")
        k2 = gateway._cache_key("Hello world", "default")
        assert k1 == k2

    def test_cache_key_different_tier(self, gateway):
        """Different tiers should produce different keys for the same prompt."""
        k1 = gateway._cache_key("Hello world", "default")
        k2 = gateway._cache_key("Hello world", "reasoning")
        assert k1 != k2

    def test_cleanup_expired_cache(self, gateway):
        """_cleanup_expired_cache should remove entries past their TTL."""
        key = gateway._cache_key("cleanup_test", "default")
        conn = gateway._store._connect()

        # Insert an already-expired entry (created 2 hours ago, TTL 1 hour)
        conn.execute(
            "INSERT OR REPLACE INTO llm_cache "
            "(cache_key, response, model, created_at, ttl_hours) "
            "VALUES (?, ?, ?, datetime('now', '-2 hours'), 1)",
            (key, "stale", "gpt-4o-mini"),
        )
        # Sanity: _check_cache returns None because it filters by expiry
        conn.commit()
        conn.close()

        assert gateway._check_cache(key) is None, (
            "Expired entry should not be returned by _check_cache"
        )

        # After cleanup the row should be gone from the DB entirely
        gateway._cleanup_expired_cache()
        conn = gateway._store._connect()
        row = conn.execute(
            "SELECT COUNT(*) AS cnt FROM llm_cache WHERE cache_key = ?",
            (key,),
        ).fetchone()
        conn.close()
        assert row["cnt"] == 0


# ── ask() — integration via fake_litellm fixture ──────────────────────────────


class TestAsk:
    """ask() method — tested with a fake litellm module."""

    def test_ask_raises_when_budget_exceeded(self, gateway):
        """ask() should raise RuntimeError if the monthly budget is exceeded."""
        # Force cost to exceed the $10 budget
        gateway._log_call("test", "gpt-4o", 10000, 5000, 10.0)

        with pytest.raises(RuntimeError, match="budget"):
            gateway.ask("Hello")

    def test_ask_returns_cached_response(self, gateway, fake_litellm):
        """ask() should return a cached response without calling the LLM."""
        # Pre-populate cache for the prompt
        key = gateway._cache_key("Hello", "default")
        gateway._store_cache(key, "Cached response", "gpt-4o-mini")

        result = gateway.ask("Hello")
        assert result == "Cached response"
        # LLM should NOT have been called
        fake_litellm.completion.assert_not_called()

    def test_ask_includes_context(self, gateway, fake_litellm):
        """ask() should prepend context to the prompt."""
        # Customise the fake response for this test
        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = "LLM reply"
        mock_response.usage.prompt_tokens = 50
        mock_response.usage.completion_tokens = 30
        fake_litellm.completion.return_value = mock_response

        result = gateway.ask(
            prompt="Say hi",
            context="Context info",
            plugin_name="test_plugin",
        )
        assert result == "LLM reply"

        # Verify the call was logged (non-cache path)
        assert gateway._store.get_total_llm_calls() >= 1

    def test_ask_cache_hit_avoids_logging(self, gateway, fake_litellm):
        """A cache hit should NOT log a new LLM call."""
        key = gateway._cache_key("Cache me", "default")
        gateway._store_cache(key, "Cached value", "gpt-4o-mini")

        result = gateway.ask("Cache me")
        assert result == "Cached value"
        assert gateway._store.get_total_llm_calls() == 0
