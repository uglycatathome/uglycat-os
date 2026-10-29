"""LLM Gateway — cost-control choke point for all LLM calls.

All LLM calls in the system go through this gateway. It enforces monthly
budgets, caches responses, sanitizes PII before prompts leave the machine,
and logs all usage for cost tracking.
"""

import hashlib
import re
import threading
import time

from family_os.kernel.config import Config
from family_os.kernel.store import Store

# ── 智谱（BigModel）免费档适配 ───────────────────────────────────────────────
# 1) 免费档并发数为 1：用进程级锁串行化，避免并发请求直接失败。
# 2) 免费模型 glm-4.7-flash 默认开启思考，会把 max_tokens 全耗在 reasoning 上、
#    content 返回空串（HTTP 200 的静默失败）→ 调用时强制 thinking disabled。
# 3) 高峰期会返回 1305「访问量过大」或 429 → 指数退避重试，仍失败则抛出，
#    由插件的模型链降级到硅基流动。
_ZHIPU_LOCK = threading.Lock()
_ZHIPU_MAX_ATTEMPTS = 2          # 只重试一次，避免限流时长时间占用
_ZHIPU_BACKOFF_S = (2,)
_ZHIPU_TIMEOUT_S = 90            # 免费档排队可能很久；超时即降级


def _is_rate_limit_error(exc: Exception) -> bool:
    """判断是否为智谱免费档的限流/过载（1305 / 429 / RateLimit）。"""
    text = f"{type(exc).__name__} {exc}"
    return any(k in text for k in ("1305", "429", "RateLimit", "访问量过大"))


class LLMGateway:
    """Central gateway for all LLM API calls.

    Parameters
    ----------
    store : Store
        Persistent storage for cost logging and response caching.
    config : Config
        Application configuration including LLM settings and secrets.
    """

    def __init__(self, store: Store, config: Config) -> None:
        self._store = store
        self._config = config

    # ── Public API ───────────────────────────────────────────────────────────

    def ask(self, prompt: str, context: str = "",
            model_tier: str = "default",
            plugin_name: str = "unknown") -> str:
        """Send a prompt to the LLM and return the response.

        Parameters
        ----------
        prompt : str
            The user/plugin prompt.
        context : str, optional
            Additional context to include before the prompt.
        model_tier : str, optional
            Model tier to use (``"default"`` or ``"reasoning"``).
        plugin_name : str, optional
            Name of the calling plugin for cost attribution.

        Returns
        -------
        str
            The LLM response text.

        Raises
        ------
        RuntimeError
            If the monthly budget has been exceeded.
        """
        # 1. Budget check
        if self.is_budget_exceeded():
            raise RuntimeError(
                f"Monthly budget ${self._config.llm.monthly_budget_usd:.2f} exceeded"
            )

        # 2. Sanitize inputs
        sanitized_context = self._sanitize(context) if context else ""
        sanitized_prompt = self._sanitize(prompt)

        # 3. Build full prompt
        full_prompt = (
            f"{sanitized_context}\n\n{sanitized_prompt}"
            if sanitized_context
            else sanitized_prompt
        )

        # 4. Check cache
        cache_key = self._cache_key(full_prompt, model_tier)
        cached = self._check_cache(cache_key)
        if cached is not None:
            return cached

        # 5. Resolve model
        model = self._resolve_model(model_tier)

        # 6. Import litellm (lazy — avoids requiring it for tests that
        #    raise before reaching this point)
        import litellm  # noqa: F811

        # 7. Resolve API key and call LLM
        api_key = self._get_api_key(model)
        # 判断 provider：当前网关把 deepseek → 内置 deepseek 分支，
        # 其余 providers（如 siliconflow）→ 走自定义 base_url + openai 兼容。
        provider = self._provider_for_model(model)
        is_deepseek = provider == "deepseek"

        completion_kwargs: dict = {
            "model": model,
            "messages": [{"role": "user", "content": full_prompt}],
        }
        if api_key:
            completion_kwargs["api_key"] = api_key
        if is_deepseek:
            completion_kwargs["api_base"] = "https://api.deepseek.com"
            # Enable reasoning for deepseek-v4-pro (reasoning tier)
            if model_tier == "reasoning":
                completion_kwargs["extra_body"] = {"thinking": {"type": "enabled"}}
        elif provider == "siliconflow":
            # 硅基流动走 litellm 的 openai 兼容 provider
            cfg = self._config.llm.providers.get("siliconflow", {})
            completion_kwargs["custom_llm_provider"] = "openai"
            completion_kwargs["api_base"] = cfg.get("base_url")
            completion_kwargs["api_key"] = api_key or self._config.secrets.siliconflow_api_key
            # 注意：不要给 Qwen3 系加 enable_thinking:false。实测（2026-10-24）关思考后
            # 长列表去重任务失准：职业 199 篇被剔除 138/28 条（两轮差异极大），而开思考
            # 时稳定只剩 3 条。速度提升（415s→12s）不值得这个准确性代价。
        elif provider == "zhipu":
            # 智谱 BigModel：OpenAI 兼容端点；必须关闭 thinking（否则 content 为空）
            cfg = self._config.llm.providers.get("zhipu", {})
            completion_kwargs["custom_llm_provider"] = "openai"
            completion_kwargs["api_base"] = cfg.get("base_url")
            completion_kwargs["api_key"] = api_key or self._config.secrets.zhipu_api_key
            completion_kwargs["extra_body"] = {"thinking": {"type": "disabled"}}
            completion_kwargs["timeout"] = _ZHIPU_TIMEOUT_S

        response = self._complete(completion_kwargs, provider)

        text = response.choices[0].message.content

        # 8. Calculate cost and log
        prompt_tokens = response.usage.prompt_tokens
        completion_tokens = response.usage.completion_tokens

        try:
            cost_info = litellm.cost_per_token(
                model=model,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
            )
            total_cost = cost_info.get("total_cost", 0.0)
        except Exception:
            total_cost = 0.0

        # Fall back to local pricing if litellm doesn't know this model
        if total_cost == 0.0:
            total_cost = self._calculate_cost(model, prompt_tokens, completion_tokens)

        self._store.log_llm_call(
            plugin_name=plugin_name,
            model=model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cost=total_cost,
        )

        # 8. Cache the response
        self._store_cache(cache_key, text, model)

        # 9. Periodic cleanup of expired cache entries
        self._cleanup_expired_cache()

        return text

    def get_monthly_cost(self) -> float:
        """Return total LLM cost for the current month.

        Delegates to the store, which queries the ``llm_calls`` table
        filtered to the current calendar month.
        """
        return self._store.get_monthly_cost()

    def is_budget_exceeded(self) -> bool:
        """Check whether the monthly budget has been exceeded.

        Returns ``True`` when ``cost / monthly_budget >= budget_cutoff_threshold``.
        A zero or negative budget is treated as unlimited (always returns ``False``).
        """
        cost = self.get_monthly_cost()
        budget = self._config.llm.monthly_budget_usd
        if budget <= 0:
            return False
        return (cost / budget) >= self._config.llm.budget_cutoff_threshold

    # ── Cost logging helper (testing hook) ──────────────────────────────────

    def _log_call(self, plugin_name: str, model: str,
                  prompt_tokens: int = 0, completion_tokens: int = 0,
                  cost: float = 0.0) -> int:
        """Record an LLM API call in the store. (Public for testing.)"""
        return self._store.log_llm_call(
            plugin_name=plugin_name,
            model=model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cost=cost,
        )

    # ── Cost calculation ────────────────────────────────────────────────────

    def _calculate_cost(self, model: str, prompt_tokens: int,
                        completion_tokens: int) -> float:
        """Calculate cost from local model pricing config.

        Falls back when litellm doesn't know pricing for custom models.
        """
        pricing = self._config.llm.model_pricing.get(model)
        if not pricing:
            return 0.0
        prompt_cost = (prompt_tokens / 1_000_000) * pricing.get("prompt_per_1m", 0)
        completion_cost = (completion_tokens / 1_000_000) * pricing.get("completion_per_1m", 0)
        return prompt_cost + completion_cost

    def get_balance(self) -> dict:
        """Query DeepSeek account balance via API.

        Returns {"balance": float, "currency": str} or {"error": str}.
        """
        import httpx
        api_key = self._config.secrets.deepseek_api_key
        if not api_key:
            return {"error": "No DeepSeek API key configured"}
        try:
            resp = httpx.get(
                "https://api.deepseek.com/user/balance",
                headers={"Accept": "application/json",
                         "Authorization": f"Bearer {api_key}"},
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
            # DeepSeek returns {"is_available": true, "balance_infos": [...]}
            balances = data.get("balance_infos", [])
            if balances:
                return {
                    "balance": float(balances[0].get("total_balance", 0)),
                    "currency": balances[0].get("currency", "CNY"),
                }
            return {"balance": 0.0, "currency": "CNY"}
        except Exception as e:
            return {"error": str(e)}

    # ── Provider call with free-tier guards ─────────────────────────────────

    def _complete(self, completion_kwargs: dict, provider: str | None):
        """调用 litellm；智谱免费档额外处理并发 1、空内容与限流重试。"""
        import litellm

        if provider != "zhipu":
            return litellm.completion(**completion_kwargs)

        last_exc: Exception | None = None
        for attempt in range(_ZHIPU_MAX_ATTEMPTS):
            try:
                # 免费档并发数为 1：串行化，避免直接触发限流
                with _ZHIPU_LOCK:
                    resp = litellm.completion(**completion_kwargs)
                text = (resp.choices[0].message.content or "").strip()
                if not text:
                    # HTTP 200 但无正文：thinking 未关闭或输出被截断，按失败处理
                    raise RuntimeError(
                        "智谱返回空内容（thinking 未关闭或 max_tokens 被 reasoning 耗尽）"
                    )
                return resp
            except Exception as exc:  # noqa: BLE001 — 统一退避重试
                last_exc = exc
                # 限流/过载不重试：立即抛出，让上层模型链快速降级到 glm-4-flash
                # （实测 1305 时单次排队可达 60s+，重试 3 次会拖掉 200s）
                if _is_rate_limit_error(exc):
                    break
                if attempt < _ZHIPU_MAX_ATTEMPTS - 1:
                    time.sleep(_ZHIPU_BACKOFF_S[attempt])
        raise RuntimeError(
            f"智谱调用失败（尝试 {_ZHIPU_MAX_ATTEMPTS} 次）：{last_exc}"
        )

    # ── Model routing ───────────────────────────────────────────────────────

    def _resolve_model(self, model_tier: str) -> str:
        """Resolve a model tier name to an actual model identifier.

        * ``"default"`` → ``config.llm.default_model``
        * ``"reasoning"`` → ``config.llm.reasoning_model``
        * anything else is returned verbatim (direct model id).
        """
        if model_tier == "default":
            return self._config.llm.default_model
        elif model_tier == "reasoning":
            return self._config.llm.reasoning_model
        return model_tier

    # ── API key resolution ──────────────────────────────────────────────────

    def _provider_for_model(self, model: str) -> str | None:
        """Return the *provider* group a model belongs to.

        * ``zhipu`` → ``glm-*``（智谱 BigModel，OpenAI 兼容端点）
        * ``siliconflow`` → models listed in the news_digest model chain
          (哪怕模型 id 含 ``deepseek`` 子串，也按 siliconflow 路由，因为
           它由硅基流动托管、走其 openai 兼容 base_url)
        * ``deepseek`` → models containing ``deepseek`` or ``deepseek/`` prefix
          (仅指 DeepSeek 原生网关模型)
        * otherwise ``None``
        """
        # 智谱优先判定：glm-* 即使出现在 news_digest/radar 链里也走 zhipu
        if model.startswith("glm-"):
            return "zhipu"
        llm = self._config.llm
        chain = [llm.news_digest_model, *(llm.news_digest_fallback_models or [])]
        radar_chain = [llm.strategy_radar_model, *(llm.strategy_radar_fallback_models or [])]
        if model in chain or model in radar_chain:
            return "siliconflow"
        if "deepseek" in model:
            return "deepseek"
        return None

    def _get_api_key(self, model: str) -> str | None:
        """Return the API key required for *model*, or ``None``.

        * Models containing ``deepseek`` → DeepSeek key
        * Models starting with ``gpt-``, ``o1``, or ``o3`` → OpenAI key
        * Models containing ``claude`` → Anthropic key
        * Models in the siliconflow chain → SiliconFlow key
        * Everything else → ``None`` (relies on environment / litellm defaults)
        """
        if self._provider_for_model(model) == "zhipu":
            return self._config.secrets.zhipu_api_key
        if self._provider_for_model(model) == "siliconflow":
            return self._config.secrets.siliconflow_api_key
        if "deepseek" in model:
            return self._config.secrets.deepseek_api_key
        if model.startswith(("gpt-", "o1", "o3")):
            return self._config.secrets.openai_api_key
        elif "claude" in model:
            return self._config.secrets.anthropic_api_key
        return None

    # ── PII Sanitization ────────────────────────────────────────────────────

    @staticmethod
    def _sanitize(text: str) -> str:
        """Sanitize sensitive information from *text* using regex patterns.

        Redacts:
        * OpenAI API keys (``sk-...``)
        * Anthropic API keys (``sk-ant-...``)
        * 18-digit Chinese ID numbers
        * 16-19 digit bank account / card numbers
        """
        text = re.sub(r'sk-[a-zA-Z0-9]{20,}', '[REDACTED]', text)
        text = re.sub(r'sk-ant-[a-zA-Z0-9_-]{20,}', '[REDACTED]', text)
        # NOTE: re.ASCII is required so \b treats Chinese characters as
        # non-word characters; otherwise Python's default Unicode \w
        # includes CJK ideographs and \b would not match next to them.
        text = re.sub(r'\b\d{17}[\dXx]\b', '[REDACTED]', text, flags=re.ASCII)   # Chinese ID
        text = re.sub(r'\b\d{16,19}\b', '[REDACTED]', text, flags=re.ASCII)       # Bank card
        return text

    # ── SQLite-backed cache ─────────────────────────────────────────────────

    @staticmethod
    def _cache_key(prompt: str, model_tier: str) -> str:
        """Generate a SHA-256 hex digest cache key from prompt + model tier."""
        raw = f"{prompt}||{model_tier}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _check_cache(self, cache_key: str) -> str | None:
        """Return cached response for *cache_key*, or ``None``.

        Only returns entries whose TTL has not yet expired:
        ``created_at + ttl_hours > now``.
        """
        conn = self._store._connect()
        try:
            row = conn.execute(
                "SELECT response FROM llm_cache "
                "WHERE cache_key = ? "
                "AND datetime(created_at, '+' || ttl_hours || ' hours') "
                "  > datetime('now')",
                (cache_key,),
            ).fetchone()
            return row["response"] if row else None
        finally:
            conn.close()

    def _store_cache(self, cache_key: str, response: str, model: str) -> None:
        """Insert or replace a cache entry."""
        conn = self._store._connect()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO llm_cache "
                "(cache_key, response, model, created_at, ttl_hours) "
                "VALUES (?, ?, ?, datetime('now'), ?)",
                (cache_key, response, model,
                 self._config.llm.cache_ttl_hours),
            )
            conn.commit()
        finally:
            conn.close()

    def _cleanup_expired_cache(self) -> None:
        """Remove all entries whose TTL has expired."""
        conn = self._store._connect()
        try:
            conn.execute(
                "DELETE FROM llm_cache "
                "WHERE datetime(created_at, '+' || ttl_hours || ' hours') "
                "  <= datetime('now')"
            )
            conn.commit()
        finally:
            conn.close()
