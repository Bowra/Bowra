"""Report-scoped source attribution — the single ownership judge.

source-accessibility tech-design 2026-08-27 批 1（`docs/source-accessibility-tech-design-2026-08-27.md`）。

回答且只回答一个问题：**「这个被引用的对象属不属于这份报告」** —— 按 kind 去问
拥有该对象的那张表（artifacts / canvas_sections / kb_extracted_rows），
而不是扫 payload 找字段名、也不是看 id 前缀猜。

与既有两份实现的分工（设计 §3.1）：

- ``attributed``（本模块）→ 展示面：label / 锚点 / tooltip 释放与否；
- ``rendered``（``results_report_relations.project_report_relations``）→ 出口面：
  ``/artifacts/{id}`` 给不给字节。字节出口 ⟺ attributed ∧ rendered ∧ artifact 行存在。

安全不变量：

- **fail-closed**：store 缺失、查询异常、kind 未知 → 一律非 OWNED（宁可少给不可多给）；
- **FOREIGN 全剥**：存在但不属于这份报告的对象不释放任何字段（display 恒 None）——
  这是 recvsPEv79H2zB 那轮建立、成对测试钉住的安全面；
- 每个判定都带稳定的 ``reason_code``，静默降级不允许存在（设计 §0.4）。

接线现状（review F4 订正，2026-08-27）：c1 起由 canvas detail 的 ``turnCitations``
sidecar 消费（``routes/canvas.py::_turn_citations_sidecar``，gated by
``FEATURE_SOURCE_ATTRIBUTION_V2``, default on since 2026-10-14）；批 2 将由
``bowhead_gateway/source_access_projection.py`` 的事件投影消费（未接）。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol, get_args

from bowhead_core.citations import CitationKind

logger = logging.getLogger(__name__)

# (kind, ref_id): reference awaiting attribution; kind from CitationKind (or historical DB strings).
CitedRef = tuple[str, str]


class Attribution(StrEnum):
    """归属四态（设计 §3.2 / §3.5）。二态 bool 是本 bug 的病根之一：
    「判不出来」与「真的没权限」在用户面必须可区分。"""

    OWNED = "owned"  # Owning store confirms this belongs to the report.
    FOREIGN = "foreign"  # Exists outside report → restricted; strip all.
    MISSING = "missing"  # Not found / attribution failed → unavailable (fail-closed result).
    UNSUPPORTED = "unsupported"  # No resolver for kind → our gap, not user fault.


@dataclass(frozen=True, slots=True)
class DisplayHandle:
    """OWNED 时可释放的展示要素。label 通常来自引用自己（turn_citations 落库的
    那条），解析器只在权威库有更好的值时才填。"""

    anchor_id: str | None = None  # Canvas DOM anchor (sec_*) for FE scroll-to.
    label: str | None = None
    # kb_chunk: viewable KB location (kb_file_id / kb_chunk_id / kb_scope / kb_space_id).
    kb_ref: Mapping[str, str] | None = None


@dataclass(frozen=True, slots=True)
class Verdict:
    attribution: Attribution
    reason_code: str  # Stable snake_case for logs / metrics / FE copy keys.
    display: DisplayHandle | None = None  # Non-None only for OWNED.
    # Potential byte-exit artifact ID: only OWNED and actually stored in artifacts;
    # exit surface also requires rendered (design §3.1); this is a candidate, not authorization.
    # In-flight (unpersisted) matches always None: this field enforces "bytes require persistence" (design §3.4-2).
    exit_candidate: str | None = None


@dataclass(frozen=True, slots=True)
class ReportScope:
    """一次投影的报告作用域。membership 集合由调用方从服务端数据推导
    （event projector 手上本来就有），解析器自己只查「被判对象」所在的库。"""

    workspace_id: str
    session_id: str
    canvas_id: str | None = None
    turn_id: str | None = None
    session_turn_ids: frozenset[str] = frozenset()  # All session turn IDs (server-side).
    source_ref_artifact_ids: frozenset[str] = frozenset()  # Session source_refs artifact IDs.
    rendered_artifact_ids: frozenset[str] = frozenset()  # project_report_relations closure.


@dataclass(frozen=True, slots=True)
class InFlightIndex:
    """流式生成中、尚未落库的画布区块 id 索引（设计 §3.4）。

    ⚠ 只允许装**服务端自己铸的 id**（canvas.section_added/streaming/done 事件载荷里的
    区块 id，由 ``canvas_store._nid("sec")`` / StreamingDecomposer 铸造）——
    绝不从模型写的字段收，否则这里就成了绕开归属判定的注入口。
    """

    section_ids: frozenset[str] = frozenset()


class Resolver(Protocol):
    """每种 kind 一个解析器：按归属问拥有它的库（设计 §3.3）。"""

    async def attribute(
        self,
        ref_ids: Sequence[str],
        scope: ReportScope,
        in_flight: InFlightIndex,
    ) -> dict[str, Verdict]: ...


@dataclass(slots=True)
class AttributionStores:
    """解析器需要的权威库句柄。任何一个缺失 → 对应 kind fail-closed 成 MISSING。"""

    artifact_store: Any | None = None  # ArtifactStore（get_many）
    canvas_store: Any | None = None  # CanvasStore（get_section_canvas_id）
    kb_rows_store: Any | None = None  # KbExtractedRowsStore（get）
    # KB-file viewer read-access check, already bound to viewer (gateway-injected; see KbFileAccess).
    kb_file_access: Callable[[str], Awaitable[KbFileAccess]] | None = None


@dataclass(frozen=True, slots=True)
class KbFileAccess:
    """看的人对一份知识库文件的读权限结论（与知识库页面同一判定，由网关算）。"""

    state: str  # "readable" | "forbidden" | "missing"
    file_name: str | None = None
    scope: str | None = None
    space_id: str | None = None  # File's space when scope=space.


def _owned(reason: str, *, anchor_id: str | None = None, exit_candidate: str | None = None) -> Verdict:
    return Verdict(
        Attribution.OWNED,
        reason,
        display=DisplayHandle(anchor_id=anchor_id),
        exit_candidate=exit_candidate,
    )


@dataclass(slots=True)
class _ArtifactFamilyResolver:
    """data_ref / tool_output：对象住在 artifacts 表（id 形如 art_*）。

    归属谓词（任一命中即 OWNED，设计 §3.3）：
      created_by_turn ∈ 本会话 turn 集 ∨ canvas_id = 本会话画布
      ∨ id ∈ 会话 source_refs ∨ id ∈ rendered closure。
    ⚠ 不是 ``created_by_turn == 正在读的 turn`` —— 会话内跨轮引用是合法形态
    （116 实测存在，设计 §1.5）。
    """

    artifact_store: Any | None

    async def attribute(
        self,
        ref_ids: Sequence[str],
        scope: ReportScope,
        in_flight: InFlightIndex,
    ) -> dict[str, Verdict]:
        out: dict[str, Verdict] = {}
        get_many = getattr(self.artifact_store, "get_many", None)
        if not callable(get_many):
            # Old stores/test doubles without bulk API → fail-closed (same rule as _turn_owned_artifact_ids).
            return dict.fromkeys(ref_ids, Verdict(Attribution.MISSING, "store_unavailable"))
        try:
            artifacts = await get_many(list(dict.fromkeys(ref_ids)))
        except Exception:
            logger.warning("source_attribution.artifact_lookup_failed n=%d", len(ref_ids))
            return dict.fromkeys(ref_ids, Verdict(Attribution.MISSING, "resolver_error"))
        by_id = {art.id: art for art in artifacts if getattr(art, "id", None)}
        for ref_id in ref_ids:
            art = by_id.get(ref_id)
            if art is None:
                out[ref_id] = Verdict(Attribution.MISSING, "artifact_missing")
            elif getattr(art, "status", None) == "missing":
                # Row remains but blob missing (reconciliation marks missing): source unavailable to readers.
                out[ref_id] = Verdict(Attribution.MISSING, "artifact_blob_missing")
            elif getattr(art, "created_by_turn", None) in scope.session_turn_ids:
                out[ref_id] = _owned("owned_by_session_turn", exit_candidate=ref_id)
            elif scope.canvas_id and getattr(art, "canvas_id", None) == scope.canvas_id:
                out[ref_id] = _owned("owned_by_canvas", exit_candidate=ref_id)
            elif ref_id in scope.source_ref_artifact_ids:
                out[ref_id] = _owned("owned_by_source_ref", exit_candidate=ref_id)
            elif ref_id in scope.rendered_artifact_ids:
                out[ref_id] = _owned("owned_by_rendered", exit_candidate=ref_id)
            else:
                out[ref_id] = Verdict(Attribution.FOREIGN, "foreign_artifact")
        return out


@dataclass(slots=True)
class _ChartSectionResolver:
    """chart_section：对象住在 canvas_sections 表（id 形如 sec_*）。

    归属按 **canvas** 判，不按 turn 判（跨轮引用合法，116 实测 1/24）；
    **有意不看** superseded / hidden_for_chat_mode —— 那是显示裁决不是授权裁决
    （116 实测 16/24 指向 hidden 段落，拿它当授权条件会误杀三分之二，设计 §3.3-1）。
    区块没有字节出口：exit_candidate 恒 None。
    """

    canvas_store: Any | None

    async def attribute(
        self,
        ref_ids: Sequence[str],
        scope: ReportScope,
        in_flight: InFlightIndex,
    ) -> dict[str, Verdict]:
        lookup = getattr(self.canvas_store, "get_section_canvas_id", None)
        # Review F7: individual awaits cause N serial connections (detail is a hot read endpoint);
        # gather + Semaphore, like _KbRowResolver.
        semaphore = asyncio.Semaphore(8)

        async def resolve(ref_id: str) -> Verdict:
            persisted_canvas: str | None = None
            lookup_failed = False
            if callable(lookup):
                async with semaphore:
                    try:
                        persisted_canvas = await lookup(ref_id)
                    except Exception:
                        logger.warning("source_attribution.section_lookup_failed id=%s", ref_id)
                        lookup_failed = True
            if persisted_canvas is not None and not lookup_failed:
                if not scope.canvas_id:
                    # Review F9: "scope has no canvas" ≠ "section belongs to another canvas". FOREIGN reason
                    # is also a security signal; false matches contaminate observations. Unprovable attribution → MISSING.
                    return Verdict(Attribution.MISSING, "scope_canvas_unavailable")
                if persisted_canvas == scope.canvas_id:
                    return _owned("owned_by_canvas", anchor_id=ref_id)
                return Verdict(Attribution.FOREIGN, "foreign_section")
            if lookup_failed:
                # Lookup exception fails closed: no in-flight fallback (ownership unprovable).
                return Verdict(Attribution.MISSING, "resolver_error")
            if ref_id in in_flight.section_ids:
                # In-flight section: server minted this turn's ID ⇒ trusted ownership; no byte exit (design §3.4-2).
                return _owned("owned_in_flight", anchor_id=ref_id)
            return Verdict(
                Attribution.MISSING,
                "section_missing" if callable(lookup) else "store_unavailable",
            )

        unique = list(dict.fromkeys(ref_ids))
        verdicts = await asyncio.gather(*(resolve(ref_id) for ref_id in unique))
        return dict(zip(unique, verdicts, strict=True))


@dataclass(slots=True)
class _KbRowResolver:
    """kb_row：对象住在 kb_extracted_rows 表（id 形如 row_*）。

    归属按 workspace 判（该表没有会话归属；§8-1 拍板：本期只给标签不给出口，
    将来若要开数据出口必须先收紧判据）。store.get 自带 soft-delete 过滤，
    查不到统一 MISSING。⚠ 116 现零样本 —— 上线前必须造真实样本验过（§8-1）。
    """

    kb_rows_store: Any | None

    async def attribute(
        self,
        ref_ids: Sequence[str],
        scope: ReportScope,
        in_flight: InFlightIndex,
    ) -> dict[str, Verdict]:
        get_one = getattr(self.kb_rows_store, "get", None)
        if not callable(get_one):
            return dict.fromkeys(ref_ids, Verdict(Attribution.MISSING, "store_unavailable"))
        semaphore = asyncio.Semaphore(8)

        async def resolve(ref_id: str) -> Verdict:
            async with semaphore:
                try:
                    row = await get_one(ref_id)
                except Exception:
                    logger.warning("source_attribution.kb_row_lookup_failed id=%s", ref_id)
                    return Verdict(Attribution.MISSING, "resolver_error")
            if row is None:
                return Verdict(Attribution.MISSING, "row_missing")
            if getattr(row, "workspace_id", None) != scope.workspace_id:
                return Verdict(Attribution.FOREIGN, "foreign_row")
            return _owned("owned_by_workspace")

        unique = list(dict.fromkeys(ref_ids))
        verdicts = await asyncio.gather(*(resolve(ref_id) for ref_id in unique))
        return dict(zip(unique, verdicts, strict=True))


@dataclass(slots=True)
class _KbChunkResolver:
    """kb_chunk：引用身份 ``kb:<file_id>:<chunk_id>``（知识库回答引用技术设计 §3）。

    归属按**看的人**判，不按会话判：引用只是指向，能不能看由知识库读权限决定
    （分享给没有该文件权限的人 ⇒ 受限）。片段本身是否仍在不在这里判——位置与原文
    由前端按同一权限读取，片段没了显示「原文已更新」。
    """

    kb_file_access: Callable[[str], Awaitable[KbFileAccess]] | None

    async def attribute(
        self,
        ref_ids: Sequence[str],
        scope: ReportScope,
        in_flight: InFlightIndex,
    ) -> dict[str, Verdict]:
        from bowhead_core.kb_citations import parse_kb_citation_identity

        check = self.kb_file_access
        semaphore = asyncio.Semaphore(8)
        by_file: dict[str, asyncio.Task[KbFileAccess]] = {}

        async def file_access(file_id: str) -> KbFileAccess:
            assert check is not None
            async with semaphore:
                return await check(file_id)

        async def resolve(ref_id: str) -> Verdict:
            parsed = parse_kb_citation_identity(ref_id)
            if parsed is None:
                return Verdict(Attribution.MISSING, "kb_identity_invalid")
            if check is None:
                # No viewer (or gateway injection) ⇒ cannot determine viewability ⇒ restricted, not unavailable.
                return Verdict(Attribution.FOREIGN, "kb_viewer_unavailable")
            file_id, chunk_id = parsed
            task = by_file.get(file_id)
            if task is None:
                task = by_file[file_id] = asyncio.ensure_future(file_access(file_id))
            try:
                access = await task
            except Exception:
                logger.warning("source_attribution.kb_file_access_failed file=%s", file_id)
                return Verdict(Attribution.FOREIGN, "resolver_error")
            if access.state == "missing":
                return Verdict(Attribution.MISSING, "kb_file_missing")
            if access.state != "readable":
                return Verdict(Attribution.FOREIGN, "kb_file_forbidden")
            kb_ref = {"kb_file_id": file_id, "kb_chunk_id": chunk_id}
            if access.scope:
                kb_ref["kb_scope"] = access.scope
            if access.space_id:
                kb_ref["kb_space_id"] = access.space_id
            return Verdict(
                Attribution.OWNED,
                "kb_file_readable",
                display=DisplayHandle(label=access.file_name or None, kb_ref=kb_ref),
            )

        unique = list(dict.fromkeys(ref_ids))
        verdicts = await asyncio.gather(*(resolve(ref_id) for ref_id in unique))
        return dict(zip(unique, verdicts, strict=True))


@dataclass(slots=True)
class _UnsupportedResolver:
    """web_clip：全仓没有生产注册点（116 数据零样本）。
    显式 UNSUPPORTED 而不是静默判否 —— 若哪天有人开始注册它，
    闸 C 的打点与 §6-D 巡检会立刻冒头，而不是在生产静默剥 label（设计 §3.5）。"""

    kind: str

    async def attribute(
        self,
        ref_ids: Sequence[str],
        scope: ReportScope,
        in_flight: InFlightIndex,
    ) -> dict[str, Verdict]:
        if ref_ids:
            logger.warning(
                "source_attribution.unsupported_kind kind=%s n=%d session=%s",
                self.kind,
                len(ref_ids),
                scope.session_id,
            )
        return dict.fromkeys(ref_ids, Verdict(Attribution.UNSUPPORTED, "unsupported_kind"))


# Gate A: every CitationKind member needs a factory here (tests enumerate get_args).
# New kind without resolver ⇒ CI failure, never silent production rejection (design §6-A).
RESOLVER_FACTORIES: Mapping[str, Callable[[AttributionStores], Resolver]] = {
    "data_ref": lambda stores: _ArtifactFamilyResolver(stores.artifact_store),
    "tool_output": lambda stores: _ArtifactFamilyResolver(stores.artifact_store),
    "chart_section": lambda stores: _ChartSectionResolver(stores.canvas_store),
    "kb_row": lambda stores: _KbRowResolver(stores.kb_rows_store),
    "kb_chunk": lambda stores: _KbChunkResolver(stores.kb_file_access),
    "web_clip": lambda stores: _UnsupportedResolver("web_clip"),
}

assert set(RESOLVER_FACTORIES) == set(get_args(CitationKind)), "RESOLVER_FACTORIES 必须与 CitationKind 一一对应（闸 A）"


def build_resolvers(stores: AttributionStores) -> dict[str, Resolver]:
    return {kind: factory(stores) for kind, factory in RESOLVER_FACTORIES.items()}


async def attribute_refs(
    refs: Sequence[CitedRef],
    *,
    scope: ReportScope,
    stores: AttributionStores,
    in_flight: InFlightIndex | None = None,
) -> dict[CitedRef, Verdict]:
    """批量归属判定入口。按 kind 分组、每 kind 一次批量解析（设计 §3.2：live SSE
    逐事件重建 projector，逐条查库会吃掉首响）。

    kind 串来自事件载荷 / turn_citations 落库值，可能是任何历史串：
    未知 kind → UNSUPPORTED（fail-closed + 可观测），绝不猜。
    """
    index = in_flight if in_flight is not None else InFlightIndex()
    resolvers = build_resolvers(stores)
    by_kind: dict[str, list[str]] = {}
    for kind, ref_id in refs:
        if isinstance(ref_id, str) and ref_id:
            bucket = by_kind.setdefault(kind, [])
            if ref_id not in bucket:
                bucket.append(ref_id)
    out: dict[CitedRef, Verdict] = {}
    for kind, ref_ids in by_kind.items():
        resolver = resolvers.get(kind)
        if resolver is None:
            logger.warning("source_attribution.unknown_kind kind=%s n=%d", kind, len(ref_ids))
            for ref_id in ref_ids:
                out[(kind, ref_id)] = Verdict(Attribution.UNSUPPORTED, "unknown_kind")
            continue
        try:
            verdicts = await resolver.attribute(ref_ids, scope, index)
        except Exception:
            logger.warning("source_attribution.resolver_crashed kind=%s n=%d", kind, len(ref_ids))
            verdicts = dict.fromkeys(ref_ids, Verdict(Attribution.MISSING, "resolver_error"))
        for ref_id in ref_ids:
            # Missing resolver verdict also fails closed; never silently allow.
            out[(kind, ref_id)] = verdicts.get(ref_id, Verdict(Attribution.MISSING, "resolver_error"))
    return out


__all__ = [
    "RESOLVER_FACTORIES",
    "Attribution",
    "AttributionStores",
    "CitedRef",
    "DisplayHandle",
    "InFlightIndex",
    "KbFileAccess",
    "ReportScope",
    "Resolver",
    "Verdict",
    "attribute_refs",
    "build_resolvers",
]
