"""V7 Knowledge Base capability — Bowhead Layer 3.

Components:
- document.py: KbDocument / KbChunk / ChunkMetadata + chunk_id helper
- errors.py: KbErrors enum
- schemas.py: Pydantic DTOs (KbFileDTO / KbSearchRequest / KbSearchResult)
- store.py: chromadb wrap (per-ws + per-team collection, P0-N1)
- files_store.py: kb_files SQL CRUD (P1-O10)
- permission.py: ScopeContext + can_read/write/delete/change_scope (P0-R1)
- retriever.py: 4-stage pipeline (embed → chromadb → rerank → post-process)
- embedder.py / reranker.py: dashscope thin async clients
- worker.py: background parse + index orchestration
- doc_type_store.py: KbDocType (extract_schema 文档表配置)
- extracted_record_store.py: KbExtractedRecord (PDF→fields_json 抽取结果)

See docs/V7-knowledge-base-tech-design.md v3.6 +
docs/kb-knowledge-tables-tech-design.md v1.0 for full design.
"""

from bowhead_core.kb.document import (
    ROLE_ENUM,
    ChunkMetadata,
    KbChunk,
    KbDocument,
    make_chunk_id,
)
from bowhead_core.kb.errors import (
    EmbedError,
    KbError,
    UnsupportedMimeError,
)
from bowhead_core.kb.schemas import (
    KbFileDTO,
    KbFileScopePatchRequest,
    KbSearchRequest,
    KbSearchResult,
    KbSearchResultChunk,
)

__all__ = [
    "ROLE_ENUM",
    "ChunkMetadata",
    "EmbedError",
    "KbChunk",
    "KbDocument",
    "KbError",
    "KbFileDTO",
    "KbFileScopePatchRequest",
    "KbSearchRequest",
    "KbSearchResult",
    "KbSearchResultChunk",
    "UnsupportedMimeError",
    "make_chunk_id",
]
