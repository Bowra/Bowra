"""Bowhead LLM Router.

Provider-Neutral 适配器框架。核心契约见 ``types.LLMAdapter``；所有厂商
（原生 / OpenAI-compatible / custom_http）都实现该 Protocol，对上层 Agent
完全透明（TD §4.3.6）。

主要入口：
    - :class:`Router` — 按 agent × profile 选 provider，处理 fallback / 熔断
    - :class:`LLMAdapter` — Adapter 契约
    - :class:`MockAdapter` — 默认 Dev Mock
"""

from bowhead_router.adapter import MockAdapter, RefusalAdapter, TeamModelUnconfiguredError
from bowhead_router.adapters import OpenAICompatibleAdapter
from bowhead_router.config_loader import (
    REQUIRED_AGENT_TYPES,
    LoadedRoutingConfig,
    RoutingConfigError,
    build_router_from_config,
    load_routing_config,
)
from bowhead_router.errors import (
    InvocationOutcomeUnknownError,
    InvocationTargetsExhaustedError,
    LLMTargetFailure,
    ProviderCallError,
    ProviderFailureKind,
    RetryPolicy,
    RouteTargetsExhaustedError,
    StreamingInterruptedError,
    StreamingTargetsExhaustedError,
)
from bowhead_router.invocation import (
    PhysicalInvocationAttempt,
    PhysicalInvocationHook,
    PhysicalInvocationSpec,
    physical_invocation_required,
    physical_invocation_scope,
)
from bowhead_router.probe import (
    CompatibleProbeRegistry,
    NotTestable,
    ProbeRequestPlan,
    ProbeResult,
    not_testable_result,
)
from bowhead_router.registry import ProviderConfig, ProviderRegistry
from bowhead_router.router import AgentBinding, Router
from bowhead_router.transport import (
    CanonicalProviderTransportPlan,
    SecretComponent,
    build_provider_transport_plan,
    materialize_transport_headers,
    public_transport_preview,
)
from bowhead_router.types import (
    ImageRef,
    LLMAdapter,
    LLMChunk,
    LLMRequest,
    LLMResponse,
    Message,
    TokenUsage,
    ToolCall,
    ToolSchema,
)
from bowhead_router.usage import (
    UNATTRIBUTED,
    LLMUsageRecord,
    UsageContext,
    current_usage_context,
    record_refusal,
    record_usage,
    set_refusal_sink,
    set_usage_sink,
    usage_context,
)
from bowhead_router.verify import (
    VerifyResult,
    discover_provider_model_candidates,
    discover_provider_models,
    verify_provider,
    verify_provider_capabilities,
)

__all__ = [
    "REQUIRED_AGENT_TYPES",
    "UNATTRIBUTED",
    "AgentBinding",
    "CanonicalProviderTransportPlan",
    "CompatibleProbeRegistry",
    "ImageRef",
    "InvocationTargetsExhaustedError",
    "InvocationOutcomeUnknownError",
    "LLMAdapter",
    "LLMChunk",
    "LLMRequest",
    "LLMResponse",
    "LLMUsageRecord",
    "LLMTargetFailure",
    "LoadedRoutingConfig",
    "Message",
    "MockAdapter",
    "NotTestable",
    "OpenAICompatibleAdapter",
    "PhysicalInvocationAttempt",
    "PhysicalInvocationHook",
    "PhysicalInvocationSpec",
    "physical_invocation_required",
    "ProviderConfig",
    "ProviderCallError",
    "ProviderFailureKind",
    "ProviderRegistry",
    "RefusalAdapter",
    "ProbeRequestPlan",
    "ProbeResult",
    "Router",
    "RetryPolicy",
    "RouteTargetsExhaustedError",
    "RoutingConfigError",
    "TeamModelUnconfiguredError",
    "SecretComponent",
    "TokenUsage",
    "ToolCall",
    "ToolSchema",
    "UsageContext",
    "StreamingInterruptedError",
    "StreamingTargetsExhaustedError",
    "VerifyResult",
    "build_router_from_config",
    "build_provider_transport_plan",
    "current_usage_context",
    "discover_provider_models",
    "discover_provider_model_candidates",
    "load_routing_config",
    "record_refusal",
    "record_usage",
    "set_refusal_sink",
    "materialize_transport_headers",
    "not_testable_result",
    "public_transport_preview",
    "physical_invocation_scope",
    "set_usage_sink",
    "usage_context",
    "verify_provider",
    "verify_provider_capabilities",
]

__version__ = "0.1.0"
