"""知识库片段成为对话引用（docs/design/知识库回答引用-Tech-Design-2026-09-25.md）。

链路两段，落点不同（per-run 编号表只在 run 出口还在）：

1. **run 出口**（`subagent._legacy_run`，仅 Lead）：用本 run 自己的 ν1.5 表把指向 ``doc`` 的
   ``[ref:N]`` 翻成 ``[ref:<chunk_id>]``。别的 ``[ref:N]`` 不动（下游照旧剥掉）。
   ⚠ 不能挪到下游统一翻：Lead 与 peer 各有一份表、同一编号指向不同片段。
2. **终答收口**（`lead_orchestrator._resolve_citations`）：``[ref:<chunk_id>]`` 若是本轮
   ``kb_search``（含 peer 嵌套调用）真实返回的片段，登记为 ``kb_chunk`` 引用并改写成
   ``[cite:N]``；否则视为编造，留给既有 strip 删除。

引用身份 ``kb:<file_id>:<chunk_id>`` 写入 ``turn_citations.artifact_id``（列名是历史名），
label 为文件名；原文与位置不随引用保存，查看时按当前权限读取。
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

KB_CHUNK_KIND = "kb_chunk"
_IDENTITY_PREFIX = "kb:"
_REF_SIMPLE_RE = re.compile(r"\[ref:(\d{1,4})\]")
_REF_CHUNK_RE = re.compile(r"\[ref:(chunk_[A-Za-z0-9]+)\]")


def kb_citation_identity(file_id: str, chunk_id: str) -> str:
    return f"{_IDENTITY_PREFIX}{file_id}:{chunk_id}"


def parse_kb_citation_identity(identity: str | None) -> tuple[str, str] | None:
    """``kb:<file_id>:<chunk_id>`` → (file_id, chunk_id)；格式不对返回 None。"""
    if not isinstance(identity, str) or not identity.startswith(_IDENTITY_PREFIX):
        return None
    file_id, sep, chunk_id = identity[len(_IDENTITY_PREFIX) :].partition(":")
    if not sep or not file_id or not chunk_id:
        return None
    return file_id, chunk_id


def translate_doc_refs(text: str, allowlist: Any) -> str:
    """run 出口：本 run 的 ``[ref:N]``（N 指向 ``doc``）→ ``[ref:<chunk_id>]``；其余原样。"""
    if not text or allowlist is None or "[ref:" not in text:
        return text

    def _repl(m: re.Match[str]) -> str:
        simple = int(m.group(1))
        try:
            if allowlist.kind_of(simple) != "doc":
                return m.group(0)
            real = allowlist.resolve(m.group(1))
        except Exception:  # noqa: BLE001 - unresolved stays unchanged, downstream strips as before
            return m.group(0)
        return f"[ref:{real}]" if isinstance(real, str) and real.startswith("chunk_") else m.group(0)

    return _REF_SIMPLE_RE.sub(_repl, text)


def _flatten(invocations: Iterable[Any] | None) -> list[Any]:
    out: list[Any] = []
    for inv in invocations or []:
        out.append(inv)
        nested = getattr(inv, "nested", None)
        if isinstance(nested, list):
            out.extend(_flatten(nested))
    return out


def kb_chunk_index(invocations: Iterable[Any] | None) -> dict[str, tuple[str, str]]:
    """本轮（含 peer 嵌套）``kb_search`` 真实返回的片段：chunk_id → (file_id, file_name)。"""
    index: dict[str, tuple[str, str]] = {}
    for inv in _flatten(invocations):
        if getattr(inv, "tool_name", "") != "kb_search":
            continue
        result = getattr(inv, "result", None)
        chunks = result.get("chunks") if isinstance(result, dict) else None
        if not isinstance(chunks, list):
            continue
        for c in chunks:
            if not isinstance(c, dict):
                continue
            cid, fid = c.get("chunk_id"), c.get("file_id")
            if isinstance(cid, str) and cid and isinstance(fid, str) and fid and cid not in index:
                index[cid] = (fid, str(c.get("file_name") or ""))
    return index


def register_kb_refs(text: str, registry: Any, invocations: Iterable[Any] | None) -> str:
    """终答收口：本轮检索到的 ``[ref:<chunk_id>]`` → 登记 ``kb_chunk`` 并改写成 ``[cite:N]``。

    同一片段重复引用复用编号（registry 按身份去重）；不在本轮检索结果里的片段 id 原样
    留下，由既有 strip 删除——与「孤儿 ``[cite:N]`` 删除」同一原则。
    """
    if not text or "[ref:chunk_" not in text:
        return text
    index = kb_chunk_index(invocations)
    if not index:
        return text

    def _repl(m: re.Match[str]) -> str:
        chunk_id = m.group(1)
        hit = index.get(chunk_id)
        if hit is None:
            return m.group(0)
        file_id, file_name = hit
        seq = registry.register(kb_citation_identity(file_id, chunk_id), label=file_name or None, kind=KB_CHUNK_KIND)
        return f"[cite:{seq}]"

    return _REF_CHUNK_RE.sub(_repl, text)
