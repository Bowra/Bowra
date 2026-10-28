"""Team-scoped KB model runtime.

The database stores remain shared; this registry swaps model clients and the
Chroma namespace for an team whose v2 LLM config defines `kb.models/active`.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any

from bowhead_router.model_capabilities import provider_has_team_secret
from bowhead_router.transport import build_provider_transport_plan

from bowhead_core.kb.config_loader import (
    PROTOCOLS,
    KbModelConfigError,
    KbModelRegistry,
    ModelSpec,
)
from bowhead_core.kb.doc_type_classifier import DocTypeClassifier
from bowhead_core.kb.embedder import Embedder
from bowhead_core.kb.reranker import Reranker
from bowhead_core.kb.retriever import KbRetriever
from bowhead_core.kb.store import KbStore
from bowhead_core.kb.structured_extractor import StructuredExtractor
from bowhead_core.kb.vision_llm import VisionLLMClient
from bowhead_core.kb.worker import KbWorker
from bowhead_core.team_llm_config import is_v2_config

logger = logging.getLogger(__name__)

_IDLE_TIMEOUT_SEC = 3600
_MAX_CACHE = 100
_SUPPORTED_PROTOCOLS = frozenset(PROTOCOLS)
_KB_CAPABILITIES = ("embed", "rerank", "vision", "row_extractor")


@dataclass(frozen=True, slots=True)
class KbCapabilityDiagnostics:
    configured: bool
    valid: bool
    reason: str


@dataclass(frozen=True, slots=True)
class KbRuntimeDiagnostics:
    capabilities: dict[str, KbCapabilityDiagnostics]
    index_version: str | None
    pending_state: str | None
    pending_embedding: KbCapabilityDiagnostics | None
    has_active_embedding: bool


def diagnose_kb_runtime_config(llm_config: dict[str, Any]) -> KbRuntimeDiagnostics:
    """Validate local KB runtime structure without constructing network clients."""
    providers = llm_config.get("providers") or {}
    kb = llm_config.get("kb") or {}
    models = kb.get("models") if isinstance(kb, dict) else {}
    active = kb.get("active") if isinstance(kb, dict) else {}
    models = models if isinstance(models, dict) else {}
    active = active if isinstance(active, dict) else {}

    capabilities: dict[str, KbCapabilityDiagnostics] = {}
    for capability in _KB_CAPABILITIES:
        model_name = active.get(capability)
        capabilities[capability] = _diagnose_kb_model_ref(
            providers,
            models,
            model_name,
            capability,
        )

    pending = kb.get("pending_embed") if isinstance(kb, dict) else None
    pending_state = None
    pending_embedding = None
    if isinstance(pending, dict):
        pending_state = str(pending.get("state") or "pending")
        pending_embedding = _diagnose_kb_model_ref(
            providers,
            models,
            pending.get("model"),
            "embed",
        )
        if not all(isinstance(pending.get(field), str) and bool(pending[field].strip()) for field in ("model", "target_index_version", "task_id")):
            pending_embedding = KbCapabilityDiagnostics(
                configured=True,
                valid=False,
                reason="pending_structure_invalid",
            )
    embedding = capabilities["embed"]
    return KbRuntimeDiagnostics(
        capabilities=capabilities,
        index_version=str(kb.get("index_version")) if isinstance(kb, dict) and kb.get("index_version") else None,
        pending_state=pending_state,
        pending_embedding=pending_embedding,
        has_active_embedding=embedding.configured and embedding.valid,
    )


def _diagnose_kb_model_ref(
    providers: Any,
    models: Any,
    model_name: Any,
    capability: str,
) -> KbCapabilityDiagnostics:
    if not isinstance(model_name, str) or not model_name:
        return KbCapabilityDiagnostics(
            configured=False,
            valid=False,
            reason="missing",
        )
    spec = models.get(model_name) if isinstance(models, dict) else None
    if not isinstance(spec, dict):
        return KbCapabilityDiagnostics(
            configured=True,
            valid=False,
            reason="model_reference_invalid",
        )
    provider_name = spec.get("provider")
    provider = providers.get(provider_name) if isinstance(providers, dict) else None
    provider_url = (
        provider.get("endpoint")
        if isinstance(provider, dict) and provider.get("type") == "azure_openai"
        else provider.get("base_url")
        if isinstance(provider, dict)
        else None
    )
    auth = provider.get("auth") if isinstance(provider, dict) else None
    keyless = isinstance(auth, dict) and auth.get("type") == "none"
    if isinstance(provider, dict) and provider.get("type") == "custom_http" and isinstance(spec.get("real_id") or spec.get("model"), str):
        try:
            provider_url = build_provider_transport_plan(
                provider,
                purpose="runtime_embed",
                model_id=str(spec.get("real_id") or spec.get("model")),
                capability="embed",
            ).request_url
        except ValueError:
            provider_url = None
    valid = (
        isinstance(provider_name, str)
        and isinstance(provider, dict)
        and isinstance(provider_url, str)
        and bool(provider_url.strip())
        and (keyless or provider_has_team_secret(providers, provider_name))
        and spec.get("capability") == capability
        and isinstance(spec.get("real_id") or spec.get("model"), str)
        and bool(str(spec.get("real_id") or spec.get("model")).strip())
        and spec.get("protocol", "openai_compat") in _SUPPORTED_PROTOCOLS
    )
    if valid and capability == "embed":
        try:
            valid = int(spec.get("dim")) > 0
        except (TypeError, ValueError):
            valid = False
    return KbCapabilityDiagnostics(
        configured=True,
        valid=valid,
        reason="ready" if valid else "config_invalid",
    )


@dataclass
class TeamKbRuntime:
    version: int
    index_version: str | None
    kb_store: KbStore
    retriever: KbRetriever | None
    worker: KbWorker | None
    registry: KbModelRegistry
    last_used: float
    # kb-matrix M2 §8⑤: matrix-engine anchors: reuse extractor in _execute_call; resolve router
    # (effective_router) for health short-circuiting/cache keys on the **same instance** (team v2 config
    # binds "lead" differently from the global router; the wrong instance corrupts short-circuit counts/cache-key models).
    # Both are None on the embedder=None early-return path (no extraction capability there; engine uses defaults).
    structured_extractor: StructuredExtractor | None = None
    router: Any = None


class TeamKbRuntimeRegistry:
    def __init__(
        self,
        *,
        persist_dir: str,
        files_store: Any,
        artifact_store: Any,
        router: Any,
        team_router_registry: Any,
        doc_type_store: Any,
        extracted_record_store: Any,
        extracted_rows_store: Any,
        per_row_jobs_store: Any,
        row_extractor: Any,
        active_per_row_tasks_ref: Any,
        ingestion_gate: Any = None,
    ) -> None:
        self._persist_dir = persist_dir
        self._files_store = files_store
        self._artifact_store = artifact_store
        self._router = router
        self._team_router_registry = team_router_registry
        self._doc_type_store = doc_type_store
        self._extracted_record_store = extracted_record_store
        self._extracted_rows_store = extracted_rows_store
        self._per_row_jobs_store = per_row_jobs_store
        self._row_extractor = row_extractor
        self._active_per_row_tasks_ref = active_per_row_tasks_ref
        self._ingestion_gate = ingestion_gate
        self._cache: dict[str, TeamKbRuntime] = {}
        self._lock = asyncio.Lock()
        self._reaper_task: asyncio.Task[None] | None = None

    async def start_background(self) -> None:
        if self._reaper_task is None or self._reaper_task.done():
            self._reaper_task = asyncio.create_task(self._reaper())

    async def stop(self) -> None:
        if self._reaper_task:
            self._reaper_task.cancel()

    def invalidate(self, team_id: str) -> None:
        self._cache.pop(team_id, None)

    def observation_store(self, team_id: str, version: int) -> KbStore | None:
        """Read an already-open current store without constructing model clients."""
        entry = self._cache.get(team_id)
        return entry.kb_store if entry is not None and entry.version == version else None

    async def _reaper(self) -> None:
        try:
            while True:
                await asyncio.sleep(300)
                now = time.time()
                async with self._lock:
                    for team_id, runtime in list(self._cache.items()):
                        if now - runtime.last_used > _IDLE_TIMEOUT_SEC:
                            del self._cache[team_id]
                            logger.info("team_kb_runtime.idle_evict team=%s", team_id)
        except asyncio.CancelledError:
            pass

    async def get(
        self,
        team_id: str,
        llm_config: dict[str, Any],
        version: int,
    ) -> TeamKbRuntime | None:
        if not is_v2_config(llm_config):
            return None
        kb = llm_config.get("kb") or {}
        if not isinstance(kb, dict) or not kb.get("models") or not kb.get("active"):
            return None
        router = await self._router_for_team(team_id, llm_config, version)
        async with self._lock:
            entry = self._cache.get(team_id)
            if entry is not None and entry.version == version:
                entry.last_used = time.time()
                return entry
            runtime = self._build_runtime(llm_config, version, router=router)
            if runtime is None:
                return None
            if len(self._cache) >= _MAX_CACHE:
                oldest = min(self._cache.items(), key=lambda kv: kv[1].last_used)
                del self._cache[oldest[0]]
            self._cache[team_id] = runtime
            return runtime

    def build_runtime_for_config(
        self,
        llm_config: dict[str, Any],
        version: int,
        *,
        router: Any = None,
    ) -> TeamKbRuntime | None:
        """Build an uncached runtime for one-off jobs such as embed reindex."""
        return self._build_runtime(llm_config, version, router=router)

    async def _router_for_team(
        self,
        team_id: str,
        llm_config: dict[str, Any],
        version: int,
    ) -> Any:
        get_router = getattr(self._team_router_registry, "get", None)
        if callable(get_router):
            try:
                router = await get_router(team_id, llm_config, version)
                if router is not None:
                    return router
            except Exception as e:
                logger.warning("team_kb_runtime.router_unavailable team=%s err=%s", team_id, e)
        return self._router

    def _build_runtime(
        self,
        llm_config: dict[str, Any],
        version: int,
        *,
        router: Any = None,
    ) -> TeamKbRuntime | None:
        try:
            registry = _build_kb_registry(llm_config)
            kb = llm_config.get("kb") or {}
            index_version = kb.get("index_version")
            kb_store = KbStore(self._persist_dir, index_version=index_version)
            embedder = _build_embedder(registry)
            reranker = _build_reranker(registry)
            vision_client = _build_vision_client(registry)
            if embedder is None:
                return TeamKbRuntime(
                    version=version,
                    index_version=index_version,
                    kb_store=kb_store,
                    retriever=None,
                    worker=None,
                    registry=registry,
                    last_used=time.time(),
                )
            from bowhead_core.kb.retrieval_policy import retrieval_top_k

            retriever = KbRetriever(
                embedder,
                kb_store,
                reranker,
                files_store=self._files_store,
                artifact_store=self._artifact_store,
                default_top_k=retrieval_top_k(kb),
            )
            effective_router = router or self._router
            structured_extractor = StructuredExtractor(router=effective_router)
            doc_type_classifier = DocTypeClassifier(
                router=effective_router,
                doc_type_store=self._doc_type_store,
            )
            worker = KbWorker(
                files_store=self._files_store,
                store=kb_store,
                embedder=embedder,
                framework_inferrer=None,
                artifact_fetcher=self._artifact_store,
                vision_client=vision_client,
                doc_type_store=self._doc_type_store,
                extracted_record_store=self._extracted_record_store,
                structured_extractor=structured_extractor,
                doc_type_classifier=doc_type_classifier,
                extracted_rows_store=self._extracted_rows_store,
                per_row_jobs_store=self._per_row_jobs_store,
                row_extractor=self._row_extractor,
                kb_model_registry=registry,
                active_per_row_tasks_ref=self._active_per_row_tasks_ref,
                ingestion_gate=self._ingestion_gate,
            )
            return TeamKbRuntime(
                version=version,
                index_version=index_version,
                kb_store=kb_store,
                retriever=retriever,
                worker=worker,
                registry=registry,
                last_used=time.time(),
                structured_extractor=structured_extractor,
                router=effective_router,
            )
        except Exception as e:
            logger.warning("team_kb_runtime.build_failed err=%s", e)
            return None


def _build_kb_registry(llm_config: dict[str, Any]) -> KbModelRegistry:
    providers = llm_config.get("providers") or {}
    raw_kb = llm_config.get("kb") or {}
    by_name: dict[str, ModelSpec] = {}
    for name, spec in (raw_kb.get("models") or {}).items():
        if not isinstance(spec, dict):
            continue
        provider_name = spec.get("provider")
        provider = providers.get(provider_name) if isinstance(providers, dict) else None
        if not isinstance(provider, dict):
            raise KbModelConfigError(f"kb model {name!r} references unknown provider")
        auth = provider.get("auth")
        provider_secret = provider.get("api_key") or (auth.get("secret") if isinstance(auth, dict) else None)
        by_name[str(name)] = ModelSpec(
            name=str(name),
            capability=spec.get("capability"),
            provider=str(provider_name),
            real_id=str(spec.get("real_id") or name),
            base_url=str(provider.get("endpoint") or provider.get("base_url") or ""),
            api_key=str(provider_secret or ""),
            timeout_sec=int(provider.get("timeout_sec", 120) or 120),
            dim=int(spec["dim"]) if spec.get("dim") is not None else None,
            ssrf_guard=True,
            # de-edu 2026-06-05: pass through request protocol (dashscope_native = native rerank path).
            # Values outside the allowlist fall back to openai_compat (lenient team-config semantics).
            protocol=(spec.get("protocol") if spec.get("protocol") in ("openai_compat", "dashscope_native") else "openai_compat"),
            provider_type=str(provider.get("type") or "openai_compatible"),
            provider_options={
                **{
                    key: value
                    for key, value in provider.items()
                    if key
                    not in {
                        "api_key",
                        "base_url",
                        "endpoint",
                        "type",
                    }
                },
                "endpoint": provider.get("endpoint"),
                "models": provider.get("models") or [],
            },
        )
    return KbModelRegistry(by_name=by_name, active=dict(raw_kb.get("active") or {}))


def _build_embedder(registry: KbModelRegistry) -> Embedder | None:
    try:
        spec = registry.get_active("embed")
        auth = spec.provider_options.get("auth")
        keyless = isinstance(auth, dict) and auth.get("type") == "none"
        return Embedder(spec) if spec.api_key or keyless else None
    except Exception as e:
        logger.warning("team_kb_runtime.embed_unavailable err=%s", e)
        return None


def _build_reranker(registry: KbModelRegistry) -> Reranker | None:
    try:
        spec = registry.get_active("rerank")
        return Reranker(spec) if spec.api_key else None
    except Exception as e:
        logger.warning("team_kb_runtime.rerank_unavailable err=%s", e)
        return None


def _build_vision_client(registry: KbModelRegistry) -> VisionLLMClient | None:
    try:
        spec = registry.get_active("vision")
        if not spec.api_key:
            return None
        return VisionLLMClient(
            api_key=spec.api_key,
            model=spec.real_id,
            base_url=spec.base_url,
            timeout_sec=spec.timeout_sec or 120,
            ssrf_guard=spec.ssrf_guard,
        )
    except Exception as e:
        logger.warning("team_kb_runtime.vision_unavailable err=%s", e)
        return None
