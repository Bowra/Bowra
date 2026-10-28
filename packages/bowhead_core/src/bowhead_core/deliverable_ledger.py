"""said-not-done Seam B / WP-1 —— 交付物账单（读侧，无状态）。

**这一轮到底把什么东西留在了库里？** 只回答这一个问题，不做任何判断、不写任何表。

为什么需要它（discovery §2）：116 prod 全库 23 次「用户明说要一个文件」只有 4 次真出了
文件（83% 漏做），而其中 **9 次答案还写着「PPT 已生成」「PDF 已经导出好了，可以直接
下载使用」** —— 用户从答案里根本看不出来没做。要拦住这种谎报，先得有一个**与产出路径
无关**的事实来源；本模块就是那个事实来源。

设计约束（tech-design-seam-b.md §2/§4.4）：

- **不新增表、不写库** —— 纯读。回滚只需关 flag，没有迁移，也没有"DB 在前代码在后"的
  不可回滚风险。
- **观测物挂在我们自己的持久化上，不挂在 tool 上。** teammcp / `tool_creator` 建的工具 /
  外部 skill 都不是我们写的，要求每个工具各自登记"完成证据"是一厢情愿。
- **best-effort** —— 任何一项取数失败都标记为未验证，绝不把“未知”伪装成 0，
  也不让收尾路径挂掉（与既有 guard 的 fail-soft 策略一致）。

能核到什么、核不到什么（**三档，必须诚实标注，别让后人误以为它无所不知**）：

| 档 | 覆盖 | 判据 | prod 覆盖率 |
| --- | --- | --- | --- |
| 1 我们自己的持久化 | 文件 / 报告段 / 记忆 | 对应计数 > 0 | **19 次漏做全在这一档** |
| 2 外部副作用 | teammcp 建 ticket 之类 | 只能核 `ok_tools` 非空 | 9 次谎报里 6 次是**零工具调用**，这一档抓得住 |
| 3 真核不了 | 外部系统里究竟有没有那行数据 | —— | 明确承认，不假装能核 |
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# Filename count returned to Lead: only to recognize whether the requested file exists, not to provide an inventory.
_MAX_FILE_NAMES = 10
_EXTENSION_ALIASES = {
    ".htm": ".html",
    ".xhtml": ".html",
    ".jpeg": ".jpg",
    ".xls": ".xlsx",
    ".doc": ".docx",
    ".markdown": ".md",
}


def _canonical_extension(ext: str) -> str:
    return _EXTENSION_ALIASES.get(ext.lower(), ext.lower())


__all__ = [
    "DeliverableLedger",
    "build_deliverable_ledger",
    "collect_ok_tools",
    "count_memory_receipts",
]


@dataclass(frozen=True)
class DeliverableLedger:
    """本轮在我们自己的库里留下了什么；计数须与 verified 标记一起解释。"""

    files: int = 0
    """本 turn 的用户可见稳定结果文件总数（交付物 + sandbox/data 输出）。

    ⚠ 只回答"有几个"，回答不了"有没有你要的那种"。判「用户点名要的格式产出没有」
    必须走 `count_for_ext()`（真机 t_1c11f59a7ba3：用户要 .docx、peer 拿 PDF 兜底，
    `files == 1` 让双条件失效，谎报直接漏网）。
    """

    file_exts: frozenset[str] = frozenset()
    """本轮产出交付物的扩展名集合（小写含点，如 `.pdf`）。"""

    file_names: tuple[str, ...] = ()
    """本轮产出交付物的文件名（上限 `_MAX_FILE_NAMES`）——喂给 Lead 的「事实」那一半。

    给计数不够、要给名字：dogfood 实证模型不是想撒谎，是它只看到 `files: 1`
    这个**为真但不够具体**的事实，就以为自己要的那种格式在里面。
    """

    reused_files: int = 0
    reused_file_names: tuple[str, ...] = ()
    reused_file_exts: frozenset[str] = frozenset()
    reused_files_verified: bool = True

    def count_available_for_ext(self, ext: str) -> int:
        """Availability includes scoped reused files; generation still does not."""
        produced = self.count_for_ext(ext)
        if produced < 0 or not self.reused_files_verified:
            return -1
        return produced + sum(1 for value in self.reused_file_exts if _canonical_extension(value) == _canonical_extension(ext))

    def count_available_files(self) -> int:
        return self.files + self.reused_files if self.files_verified and self.reused_files_verified else -1

    sections: int = 0
    """本轮落到画布上的报告段数：append 的段数；一个都没 append 时取本轮就地 patch 过的段数（续轮修订）。"""

    memories: int = 0
    """本轮是否写入了长期记忆（0/1 —— memory_notes 是一用户一行，按最后写入方判定）。"""

    ok_tools: frozenset[str] = field(default_factory=frozenset)
    """本轮**成功返回**的工具名集合（含 peer 的 nested 调用）。档 2 唯一能核的东西。"""

    files_verified: bool = True
    """文件账单是否成功读取。False 表示未知，不等于 0 个。"""

    sections_verified: bool = True
    """画布段账单是否成功读取。False 表示未知。"""

    memories_verified: bool = True
    """长期记忆账单是否成功读取。False 表示未知。"""

    @property
    def produced_anything(self) -> bool:
        """本轮有没有留下任何持久产物。"""
        return bool(self.files or self.sections or self.memories)

    @property
    def worked_but_produced_nothing(self) -> bool:
        """**派了活却什么都没留下** —— 原 sprint 说的 C 形态。

        ⚠ 不能只用 `not produced_anything` 当判据：**普通聊天轮本来就不产持久物**
        （问个概念、查个数、澄清一句），无条件计数等于把每一轮闲聊都算进来，
        指标会被稀释到没有意义。所以必须叠加「确实派了 peer」这个条件 ——
        那才是"干了活"的信号。
        """
        return (
            "subagent_spawn" in self.ok_tools
            and self.files_verified
            and self.sections_verified
            and self.memories_verified
            and not self.produced_anything
        )

    def count_for_ext(self, ext: str) -> int:
        """本轮产出了几个该扩展名（小写含点，如 `.docx`）的交付物。

        **`-1` = 无法对账 ⇒ 调用方必须放行**，与 `count_for()` 同一约定。
        触发条件：账单里有文件、却一个文件名都没读到（取数降级 / store 不返 filename）。

        ⚠ 这里**绝不能返回 0**：那会把"读不到名字"误判成"没有该格式"，
        给真出了文件的轮次扣上"没出"的帽子。误判比漏判更伤用户，是本模块的第一原则。
        """
        if not self.files_verified or (self.files > 0 and not self.file_exts):
            return -1
        wanted = _canonical_extension(ext)
        return sum(1 for value in self.file_exts if _canonical_extension(value) == wanted)

    def count_for(self, claim_kind: str) -> int:
        """按"声明类型"取对应的账单项。未知类型返回 -1（= 无法对账，调用方须放行）。

        -1 而不是 0 是刻意的：0 会被判成"没产出"从而误触发闸，而未知声明类型的正确
        行为是**不判**。误判（给真出了文件的轮次扣"没出"的帽子）比漏判更伤用户。
        """
        values = {
            "file": self.files if self.files_verified else -1,
            "report": self.sections if self.sections_verified else -1,
            "memory": self.memories if self.memories_verified else -1,
        }
        return values.get(claim_kind, -1)


def count_memory_receipts(invocations: Any) -> int:
    """本轮 `remember` 工具**拿到存储回执**的次数（纯函数）。

    ★★ 为什么需要它：账本原来只查 `memory_notes`（一个用户一行，看
    `source_turn_id == turn_id`），而 `remember` 写的是 **`memory_entries`**
    —— 写入侧早就搬家了，对账侧还留在旧表。后果是**每一次成功的 remember
    都被判成谎报**，防谎报守卫给用户追加一句「这条信息没有被写入长期记忆」，
    而同一轮的工具回执写着「已存为长期记忆」。116（1.2.59）真机复现：
    turn t_6a73a7a107dd 的 `tool_result` 与 `final_answer` 自相矛盾。

    ★ 判据取**存储自己回的 id**，不是「工具跑过」：`ok_tools` 把非 dict 结果
    保守算成功（对「有没有动过手」是对的），但这个守卫的误报方向相反 ——
    错判成「写了」会让真正的谎报溜过去。有 `id` 才算数。

    ⚠ 与 `collect_ok_tools` 一样**必须走 `nested`**：委派轮里 peer 的真实调用
    挂在那儿，只读顶层会在最需要它的轮次上全瞎。
    """
    seen = 0

    def _walk(items: Any) -> None:
        nonlocal seen
        for inv in items or []:
            if str(getattr(inv, "tool_name", "") or "") == "remember":
                result = getattr(inv, "result", None)
                if isinstance(result, dict) and result.get("ok") is not False and result.get("id"):
                    seen += 1
            _walk(getattr(inv, "nested", None))

    _walk(invocations)
    return seen


def collect_ok_tools(invocations: Any) -> frozenset[str]:
    """本轮成功返回的工具名（纯函数）。

    ⚠ **必须走 `inv.nested`**：委派轮里 peer 的真实工具调用挂在 `invocation.nested` 上
    （`tool_registry` 在 spawn 结束时把 `ctx._last_nested_invocations` 搬过去并 delattr
    掉 ctx 上的字段）。只读顶层会让这个函数在**恰恰最需要它的委派轮上全瞎** ——
    `chat_fabrication_guard` 踩过同一个坑并在源码里记了账（trace t_0e818cb5）。
    """
    out: set[str] = set()

    def _walk(items: Any) -> None:
        for inv in items or []:
            name = str(getattr(inv, "tool_name", "") or "")
            result = getattr(inv, "result", None)
            ok = result.get("ok") if isinstance(result, dict) else None
            # Missing ok (non-dict result / legacy shape) conservatively counts as success: this set detects attempted work;
            # underreporting is riskier than overreporting (overreporting = assuming an uncalled tool ran = gate permits false claims).
            if name and ok is not False:
                out.add(name)
            _walk(getattr(inv, "nested", None))

    _walk(invocations)
    return frozenset(out)


async def build_deliverable_ledger(
    *,
    turn_id: str | None,
    user_id: str | None = None,
    session_id: str | None = None,
    artifact_store: Any | None = None,
    canvas_store: Any | None = None,
    memory_notes_store: Any | None = None,
    invocations: Any = None,
) -> DeliverableLedger:
    """把本轮的产出实况读成一份账单；单项失败标记为未验证。"""
    ok_tools = collect_ok_tools(invocations)
    if not turn_id:
        return DeliverableLedger(
            ok_tools=ok_tools,
            files_verified=False,
            sections_verified=False,
            memories_verified=False,
        )

    files = 0
    file_exts: frozenset[str] = frozenset()
    file_names: tuple[str, ...] = ()
    reused_names: list[str] = []
    reused_verified = True
    files_verified = artifact_store is not None
    if artifact_store is not None:
        try:
            # Fetch complete `Artifact` records (filename/mime): a mere len() formerly discarded
            # format information, leaving the ledger only able to count. The data was already available.
            list_results = getattr(artifact_store, "list_result_artifacts_for_turn", None)
            if callable(list_results):
                delivs = await list_results(turn_id) or []
            else:
                # Compatibility for older stores/test doubles during rollout.
                delivs = await artifact_store.list_deliverables_for_turn(turn_id) or []
            files = len(delivs)
            names = [str(getattr(a, "filename", "") or "").strip() for a in delivs]
            file_names = tuple(n for n in names if n)[:_MAX_FILE_NAMES]
            file_exts = frozenset("." + n.rsplit(".", 1)[-1].lower() for n in names if "." in n)
        except Exception as e:  # noqa: BLE001 — best-effort
            files_verified = False
            logger.debug("deliverable_ledger.files degraded turn=%s err=%s", turn_id, e)
        # Reuse lookup failure must not erase verified new-output evidence.
        if session_id and user_id:
            try:
                # Only successful current-task inputs can establish reuse.
                for artifact_id in _reused_sandbox_inputs(invocations):
                    artifact = await artifact_store.get_result_artifact_for_scope(artifact_id=artifact_id, session_id=session_id, user_id=user_id)
                    if (
                        artifact is not None
                        and artifact.created_by_turn != turn_id
                        and artifact.kind != "user_upload"
                        and artifact.source != "upload"
                        and artifact.filename
                    ):
                        reused_names.append(artifact.filename)
            except Exception as e:  # noqa: BLE001 — best-effort
                reused_names = []
                reused_verified = False
                logger.debug("deliverable_ledger.reused_files degraded turn=%s error_type=%s", turn_id, type(e).__name__)

    sections = 0
    sections_verified = canvas_store is not None
    if canvas_store is not None:
        try:
            sections = int(await canvas_store.count_sections_for_turn(turn_id) or 0)
            if sections == 0 and hasattr(canvas_store, "section_ids_touched_by_turn"):
                # canvas-update-turn batch B1 (116 dogfood R11 `t_bf8659b468c0`): continuation patches previous sections in place
                # without appending new ones; append-only counting yields 0 and adds "本轮没有内容落到画布上" despite actual changes.
                # In-place patches are this turn's canvas output too (`section_ids_touched_by_turn` = append ∪ patch).
                sections = len(await canvas_store.section_ids_touched_by_turn(turn_id) or ())
        except Exception as e:  # noqa: BLE001
            sections_verified = False
            logger.debug("deliverable_ledger.sections degraded turn=%s err=%s", turn_id, e)

    # ★ Union two sources: `memory_entries` (remember's primary path, using storage receipts)
    # and `memory_notes` (notes path, one row per user).
    # Querying only the latter labels all personal-memory remember writes as false claims.
    memories = count_memory_receipts(invocations)
    memories_verified = bool(invocations is not None) or (memory_notes_store is not None and bool(user_id))
    if memory_notes_store is not None and user_id:
        try:
            # memory_notes has **one row per user** (upsert), so detecting a memory write this turn
            # requires checking that row's last writer is this turn, not counting rows.
            note = await memory_notes_store.get(user_id=user_id)
            if note is not None and getattr(note, "source_turn_id", None) == turn_id:
                memories += 1
        except Exception as e:  # noqa: BLE001
            memories_verified = False
            logger.debug("deliverable_ledger.memories degraded turn=%s err=%s", turn_id, e)

    return DeliverableLedger(
        files=files,
        file_exts=file_exts,
        file_names=file_names,
        reused_files=len(reused_names),
        reused_file_names=tuple(reused_names[:_MAX_FILE_NAMES]),
        reused_file_exts=frozenset("." + name.rsplit(".", 1)[-1].lower() for name in reused_names if "." in name),
        reused_files_verified=reused_verified,
        sections=sections,
        memories=memories,
        ok_tools=ok_tools,
        files_verified=files_verified,
        sections_verified=sections_verified,
        memories_verified=memories_verified,
    )


def _reused_sandbox_inputs(invocations: Any) -> tuple[str, ...]:
    from bowhead_core.sandbox_inputs_normalize import FLAT_SANDBOX_TOOL, SANDBOX_SKILL_NAME, SKILL_INVOKE_TOOL, salvage_misnested_sandbox_args

    ids: dict[str, None] = {}

    def walk(items: Any) -> None:
        for inv in items or ():
            result = getattr(inv, "result", None)
            args = getattr(inv, "arguments", None)
            name = getattr(inv, "tool_name", "")
            if isinstance(args, dict) and isinstance(result, dict) and result.get("ok") is True:
                if name == SKILL_INVOKE_TOOL and args.get("name") == SANDBOX_SKILL_NAME:
                    args = args.get("input")
                    name = SANDBOX_SKILL_NAME
                if name in {FLAT_SANDBOX_TOOL, SANDBOX_SKILL_NAME} and isinstance(args, dict):
                    args, _ = salvage_misnested_sandbox_args(args)
                if name in {FLAT_SANDBOX_TOOL, SANDBOX_SKILL_NAME} and isinstance(args, dict) and isinstance(args.get("inputs"), dict):
                    for value in args["inputs"].values():
                        artifact_id = value.get("artifact_id") if isinstance(value, dict) else value
                        if isinstance(artifact_id, str) and 0 < len(artifact_id) <= 128 and len(ids) < 16:
                            ids[artifact_id] = None
            walk(getattr(inv, "nested", None))

    walk(invocations)
    return tuple(ids)
