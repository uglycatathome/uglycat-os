"""Dependency injection container.

Context is the ONLY coupling point between plugins and kernel services.
Plugins receive a Context in their run() method and use it to access
Store, KnowledgeBase, and LLMGateway. Plugins never import kernel modules directly.
"""
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from family_os.kernel.store import Store
    from family_os.kernel.config import Config


class Context:
    """Injected into every plugin's run() method.

    Provides controlled access to kernel services. Optional services
    (knowledge_base, llm_gateway) may be None during early bootstrap
    or testing.
    """

    def __init__(
        self,
        store: "Store",
        config: "Config",
        knowledge_base=None,
        llm_gateway=None,
    ):
        self.store = store
        self.config = config
        self.knowledge_base = knowledge_base
        self.llm_gateway = llm_gateway
