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
# Delivered files named with their identifiers (docs/sprints/result-lookup-2026-11-15, FR20). A shortened
# result keeps lists of up to 30 items, so this many survive history hygiene.
_MAX_DELIVERED = 30
# Kinds the user gets as result files (the Results "deliverables" group, plus deck previews).
_DELIVERED_KINDS = frozenset({"deliverable", "canvas_export", "deck_preview"})

__all__ = [
    "DeliverableLedger",
    "build_deliverable_ledger",
    "collect_ok_tools",
    "count_memory_receipts",
    "produced_facts",
    "produced_files_note",
]


@dataclass(frozen=True)
class DeliverableLedger:
    """本轮在我们自己的库里留下了什么；计数须与 verified 标记一起解释。"""

    files: int = 0
    """本 turn 的用户可见稳定结果文件总数（交付物 + sandbox/data 输出）。"""

    file_names: tuple[str, ...] = ()
    """本轮产出交付物的文件名（上限 `_MAX_FILE_NAMES`）——喂给 Lead 的「事实」那一半。

    给计数不够、要给名字：dogfood 实证模型不是想撒谎，是它只看到 `files: 1`
    这个**为真但不够具体**的事实，就以为自己要的那种格式在里面。
    """

    delivered: tuple[tuple[str, str], ...] = ()
    """Identifier and name of each file this Turn delivered (up to ``_MAX_DELIVERED``).

    A name alone cannot be revised: ``revise_files`` takes identifiers. Later Turns read
    this pair from the helper result after history hygiene has shortened it.
    """

    undelivered: tuple[tuple[str, str], ...] = ()
    """Identifier and name of each file this Turn saved but did not deliver (work files, undelivered drafts)."""

    reused_files: int = 0
    reused_file_names: tuple[str, ...] = ()
    reused_files_verified: bool = True

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


def count_memory_receipts(invocations: Any) -> int:
    """本轮 `remember` 工具**拿到存储回执**的次数（纯函数）。

    ★★ 为什么需要它：账本原来只查 `memory_notes`（一个用户一行，看
    `source_turn_id == turn_id`），而 `remember` 写的是 **`memory_entries`**
    —— 写入侧早就搬家了，对账侧还留在旧表。后果是**每一次成功的 remember
    都被判成谎报**，防谎报守卫给用户追加一句「这条信息没有被写入长期记忆」，
    而同一轮的工具回执写着「已存为长期记忆」。116（1.2.59）真机复现：
    an observed task 的 `tool_result` 与 `final_answer` 自相矛盾。

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
    file_names: tuple[str, ...] = ()
    delivered: tuple[tuple[str, str], ...] = ()
    undelivered: tuple[tuple[str, str], ...] = ()
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
            delivered = tuple(
                (str(a.id), str(a.filename).strip())
                for a in delivs
                if getattr(a, "kind", None) in _DELIVERED_KINDS and getattr(a, "id", None) and str(getattr(a, "filename", "") or "").strip()
            )[:_MAX_DELIVERED]
            # Chat running state R2.8: what was saved but not delivered, so Lead can present a file it meant to hand
            # over. A fact only; nothing here decides what Lead delivers.
            undelivered = tuple(
                (str(a.id), str(a.filename).strip())
                for a in delivs
                if getattr(a, "kind", None) not in _DELIVERED_KINDS
                and getattr(a, "kind", None) != "deck_preview"
                and getattr(a, "id", None)
                and str(getattr(a, "filename", "") or "").strip()
            )[:_MAX_DELIVERED]
        except Exception as e:  # noqa: BLE001 — best-effort
            files_verified = False
            logger.debug("deliverable_ledger.files degraded turn=%s err=%s", turn_id, e)
        # Reuse lookup failure must not erase verified new-output evidence.
        if session_id and user_id:
            try:
                reused = {}
                presented = getattr(artifact_store, "list_presented_result_artifacts_for_turn", None)
                if callable(presented):
                    reused.update((a.id, a) for a in await presented(turn_id, session_id=session_id, user_id=user_id))
                # Plain model metadata cannot establish native presentation reuse.
                for artifact_id in _reused_sandbox_inputs(invocations):
                    artifact = await artifact_store.get_result_artifact_for_scope(artifact_id=artifact_id, session_id=session_id, user_id=user_id)
                    if artifact is not None:
                        reused[artifact_id] = artifact
                for artifact in reused.values():
                    if artifact.created_by_turn != turn_id and artifact.kind != "user_upload" and artifact.source != "upload" and artifact.filename:
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
                # canvas-update-turn batch B1 (116 dogfood R11 `an observed task`): continuation patches previous sections in place
                # without appending new ones. In-place patches are this turn's canvas output too
                # (`section_ids_touched_by_turn` = append ∪ patch).
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
        file_names=file_names,
        delivered=delivered,
        undelivered=undelivered,
        reused_files=len(reused_names),
        reused_file_names=tuple(reused_names[:_MAX_FILE_NAMES]),
        reused_files_verified=reused_verified,
        sections=sections,
        memories=memories,
        ok_tools=ok_tools,
        files_verified=files_verified,
        sections_verified=sections_verified,
        memories_verified=memories_verified,
    )


def produced_facts(ledger: DeliverableLedger) -> dict[str, Any]:
    """What this Turn saved, as the ``produced`` facts of a helper result for Lead.

    Unread items are null and named in ``verified``; an unknown count is never reported as 0.
    """
    facts: dict[str, Any] = {
        "files": ledger.files if ledger.files_verified else None,
        # Names, not only a count: with "files: 1" Lead took the requested format for granted (an observed task).
        "file_names": list(ledger.file_names) if ledger.files_verified else [],
        "reused_files": ledger.reused_files if ledger.reused_files_verified else None,
        "reused_file_names": list(ledger.reused_file_names) if ledger.reused_files_verified else [],
        "canvas_sections": ledger.sections if ledger.sections_verified else None,
        "memories": ledger.memories if ledger.memories_verified else None,
        "verified": {
            "files": ledger.files_verified,
            "reused_files": ledger.reused_files_verified,
            "canvas_sections": ledger.sections_verified,
            "memories": ledger.memories_verified,
        },
    }
    if ledger.files_verified:
        # Names alone cannot be revised later. The pair survives when history hygiene shortens this result,
        # where a bare identifier list did not say which file an identifier was. Present even when empty: on 116
        # a helper kept its deck as a work file, and Lead, seeing the deck among the registered names and no
        # delivered list, told the user the file was ready (pptx dogfood, labeling spike 2026-11-26).
        facts["delivered_files"] = [{"artifact_id": identity, "name": name} for identity, name in ledger.delivered]
    if ledger.files_verified and ledger.undelivered:
        facts["undelivered_files"] = [{"artifact_id": identity, "name": name} for identity, name in ledger.undelivered]
    return facts


# How files reach the user, stated with every helper result in the Turn's answer language. The note does not
# read the helper's text. Replayed on an observed execution with a false "PPTX 也已生成，可直接下载" added to the
# helper's answer, Lead repeated the false file in 5 of 8 calls with the ledger facts alone, in 0 of 20 with the
# Chinese note and in 1 of 12 with the English one (docs/sprints/delivery-check-without-patterns-2026-11-24).
# The note named produced.file_names, which also lists work and analysis files; it now names the delivered list.
# Replayed on the case where every saved deck was a work file, Lead said the file was not delivered in 4 of 6
# calls with this wording, against 1 of 6 with the old one.
_FILES_NOTE = {
    "en-US": (
        "Only the files in produced.delivered_files reach the user as result files. produced.undelivered_files were saved in this "
        "Turn as work or analysis files and are not shown to the user as result files. A file that the helper's answer above "
        "mentions but that is not in produced.delivered_files was not delivered as a result file, and the user cannot get it."
    ),
    "zh-CN": (
        "本轮作为结果文件交给用户的只有 produced.delivered_files 里的这些。produced.undelivered_files 里是本轮保存的工作文件或分析文件，"
        "不会作为结果文件显示给用户。上面 subagent 的话里提到、但不在 produced.delivered_files 里的文件，没有作为结果文件交付，用户拿不到。"
    ),
    "de-DE": (
        "Nur die Dateien in produced.delivered_files erreichen den Nutzer als Ergebnisdateien. produced.undelivered_files wurden "
        "in diesem Durchgang als Arbeits- oder Analysedateien gespeichert und werden dem Nutzer nicht als Ergebnisdateien angezeigt. "
        "Eine Datei, die die Antwort des Helfers oben erwähnt, die aber nicht in produced.delivered_files steht, wurde nicht als "
        "Ergebnisdatei geliefert, und der Nutzer kann sie nicht erhalten."
    ),
    "fr-FR": (
        "Seuls les fichiers de produced.delivered_files parviennent à l’utilisateur comme fichiers de résultat. "
        "produced.undelivered_files ont été enregistrés pendant ce tour comme fichiers de travail ou d’analyse et ne sont pas "
        "affichés à l’utilisateur comme fichiers de résultat. Un fichier que la réponse de l’assistant ci-dessus mentionne mais qui "
        "ne figure pas dans produced.delivered_files n’a pas été livré comme fichier de résultat, et l’utilisateur ne peut pas l’obtenir."
    ),
}
_REUSED_FILES_NOTE = {
    "en-US": "produced.reused_file_names lists existing files this Turn reused; they remain available and were not generated in this Turn.",
    "zh-CN": "produced.reused_file_names 里是本轮沿用的已有文件，它们仍可下载，不是本轮新生成的。",
    "de-DE": (
        "produced.reused_file_names listet vorhandene Dateien, die dieser Durchgang wiederverwendet hat; "
        "sie bleiben verfügbar und wurden nicht in diesem Durchgang erzeugt."
    ),
    "fr-FR": (
        "produced.reused_file_names liste les fichiers existants réutilisés pendant ce tour ; "
        "ils restent disponibles et n’ont pas été générés pendant ce tour."
    ),
}


def produced_files_note(ledger: DeliverableLedger, language: str) -> str:
    """The fixed note on how files reach the user, or "" when the file ledger could not be read."""
    if not ledger.files_verified:
        return ""
    note = _FILES_NOTE.get(language, _FILES_NOTE["en-US"])
    if ledger.reused_files_verified and ledger.reused_files:
        note += " " + _REUSED_FILES_NOTE.get(language, _REUSED_FILES_NOTE["en-US"])
    return note


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
