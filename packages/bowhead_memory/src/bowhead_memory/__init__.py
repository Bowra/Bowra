"""Bowhead Memory 子系统.

M2.4 已实现：
    - :class:`MemoryCandidateStore` — Dreaming Light 候选队列
    - :class:`DreamingLight`         — LLM 观察 turn → 提议 0-3 条候选
    - :class:`DreamingEngine`        — REM (高频 pattern) + Deep (运维统计)

M3 后续：MemoryRetriever / ConflictResolver / 长期向量检索
"""

from bowhead_memory.candidates import (
    DreamingLight,
    DreamObservation,
    MemoryCandidate,
    MemoryCandidateStore,
)
from bowhead_memory.dreaming import (
    DeepDigest,
    DreamingEngine,
    RemDigest,
)
from bowhead_memory.vector_client import (
    EmbedProxyClientFactory,
    VectorClient,
    VectorDBError,
    collection_names,
    parse_schema_collection,
    resolve_tenant_from_collections,
    schema_collection,
)

__all__ = [
    "DeepDigest",
    "DreamObservation",
    "DreamingEngine",
    "DreamingLight",
    "EmbedProxyClientFactory",
    "MemoryCandidate",
    "MemoryCandidateStore",
    "RemDigest",
    "VectorClient",
    "VectorDBError",
    "collection_names",
    "parse_schema_collection",
    "resolve_tenant_from_collections",
    "schema_collection",
]

__version__ = "0.3.0"
