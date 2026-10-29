from family_os.kernel.context import Context
from family_os.kernel.store import Store
from family_os.kernel.config import (
    Config, AppConfig, LLMConfig, PluginConfig,
    SchedulerConfig, NotifierConfig, ChannelConfig, DigestConfig,
    ApprovalConfig, Secrets,
)


def _make_config():
    return Config(
        app=AppConfig(),
        llm=LLMConfig(),
        plugins=PluginConfig(),
        scheduler=SchedulerConfig(),
        notifier=NotifierConfig(channels=[ChannelConfig(type="file", enabled=True)]),
        digest=DigestConfig(),
        approval=ApprovalConfig(),
        secrets=Secrets(),
    )


class TestContext:
    def test_holds_store_and_config(self, test_db_path):
        store = Store(test_db_path)
        config = _make_config()
        ctx = Context(store=store, config=config)
        assert ctx.store is store
        assert ctx.config is config

    def test_optional_services_default_to_none(self, test_db_path):
        store = Store(test_db_path)
        config = _make_config()
        ctx = Context(store=store, config=config)
        assert ctx.knowledge_base is None
        assert ctx.llm_gateway is None

    def test_optional_services_can_be_set(self, test_db_path):
        store = Store(test_db_path)
        config = _make_config()
        ctx = Context(store=store, config=config, knowledge_base="kb", llm_gateway="gw")
        assert ctx.knowledge_base == "kb"
        assert ctx.llm_gateway == "gw"
