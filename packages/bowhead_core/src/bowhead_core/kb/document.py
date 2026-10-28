"""KB document / chunk dataclasses + chunk_id helper.

Tech design v3.6 §2.1.

P0-A (round 1): chunk_id 用 hash content-addressable, 重 parse 同 chunk 同 id, chromadb upsert 自动 dedup.
P1-O8 (round 3): 24 hex char (96 bit), 100M chunk birthday paradox 冲突 < 10⁻⁶.
P0-O3 (round 3): chromadb where filter 不接受 None, 改 is_active bool + deleted_at_iso.
P0-N1 + P1-O6: ChunkMetadata 加 scope_owner / workspace_id / team_id.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Literal

ROLE_ENUM = Literal["intro", "data", "analysis", "appendix", "glossary", "sop"]


# Placeholder prefixes — the loaders write a stub chunk when a file fails to parse
# (scanned PDF with no vision L2, corrupt file, etc.) or a spreadsheet has no data.
# Sources:
#   pdf_loader._placeholder_result   → "(PDF parse failed — …"
#   image_loader._placeholder_result → "(图片解析失败: …"
#   xlsx_loader / csv_loader         → "(xlsx parse failed: …" / "(csv parse failed: …"
#                                      / "(xlsx empty — 没找到数据 island)" (empty sheet, same CSV text)
#   docx_loader                      → "(DOCX parse failed)"
# These are ingest artifacts, never real content: the worker drops them after
# splitting (never indexed) and retrieval filters them ALWAYS-ON (legacy chunks
# indexed before that), so a stub is never returned to the LLM and cited as the
# document's content.
PLACEHOLDER_PREFIXES = (
    "(PDF parse failed",
    "(csv parse failed",
    "(图片解析失败",
    "(xlsx parse failed",
    "(DOCX parse failed",
    "(xlsx empty",
)


def is_parse_failure_placeholder(content: str) -> bool:
    """True if the chunk is a loader placeholder stub (see PLACEHOLDER_PREFIXES)."""
    if not content:
        return False
    return content.lstrip().startswith(PLACEHOLDER_PREFIXES)


@dataclass
class KbDocument:
    """Loader 输出 → splitter 输入. langchain_core.documents.Document shape 兼容."""

    page_content: str
    metadata: dict[str, Any] = field(default_factory=dict)
    id: str | None = None  # chunk_id (post-split)


@dataclass
class ChunkMetadata:
    """chromadb 入库 metadata. flat (chromadb 0.5+ 也支持 nested 但 flat 更稳).

    P0-N1: per-ws collection + metadata filter scope/scope_owner.
    P0-O3: is_active bool + deleted_at_iso 替 deleted_at None.
    """

    file_id: str
    file_name: str
    # Batch3/c1 (D3): canonical=space (legacy workspace migrated by one-shot UPDATE;
    # transitional reads accept both in store/files_store; API schemas validators accept both).
    scope: Literal["personal", "space", "team"]
    scope_owner: str  # personal=user_id / workspace=ws_id / team=team_id
    workspace_id: str  # Physical space collection owner (team scope uses empty string).
    team_id: str
    chunk_index: int  # 1-based within file.
    role: ROLE_ENUM = "intro"
    page: int | None = None
    section_title: str | None = None
    char_offset: int | None = None
    lang: Literal["zh", "en", "mixed"] = "zh"
    version: int = 1
    uploaded_by: str = ""
    uploaded_at: str = ""
    is_active: bool = True
    deleted_at_iso: str = ""  # ISO timestamp=soft-deleted, empty string=active.
    generation_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dict for chromadb metadata.

        chromadb 接受 str / int / float / bool / None — 这里全 str/int/bool, 不 None.
        """
        return {
            "file_id": self.file_id,
            "file_name": self.file_name,
            "scope": self.scope,
            "scope_owner": self.scope_owner,
            "workspace_id": self.workspace_id,
            "team_id": self.team_id,
            "chunk_index": self.chunk_index,
            "role": self.role,
            "page": self.page if self.page is not None else -1,  # -1 = no page
            "section_title": self.section_title or "",
            "char_offset": self.char_offset if self.char_offset is not None else -1,
            "lang": self.lang,
            "version": self.version,
            "uploaded_by": self.uploaded_by,
            "uploaded_at": self.uploaded_at,
            "is_active": self.is_active,
            "deleted_at_iso": self.deleted_at_iso,
            "generation_id": self.generation_id,
        }


@dataclass
class KbChunk:
    """chromadb 入库后的 chunk 记录."""

    chunk_id: str  # P0-A hash content-addressable
    file_id: str
    content: str
    embedding: list[float] | None
    metadata: ChunkMetadata


def make_chunk_id(file_id: str, chunk_index: int, content: str) -> str:
    """P0-A content-addressable chunk_id, 重 parse 同 chunk 同 id, chromadb upsert 自动 dedup.

    P1-O8: 24 hex char (96 bit), 100M chunk 冲突概率 < 10⁻⁶.
    输入: file_id + chunk_index + content prefix 200 char (内容变 → id 变, version+1 自动).
    """
    h = hashlib.sha256(f"{file_id}:{chunk_index}:{content[:200]}".encode()).hexdigest()
    return f"chunk_{h[:24]}"
