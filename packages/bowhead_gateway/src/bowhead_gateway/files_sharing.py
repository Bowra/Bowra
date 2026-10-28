"""file-management-v2 P3 —— 文件的空间/团队作用域（分享）。

设计见 `docs/file-management-p3-space-scope-design-2026-07-25.md`。要点：

  · **接授权引擎，不给 artifacts 加列**——平台已有一套 grant 真相
    （`resource_kind="artifact"`），文件没有理由自成体系。加列会制造第二处
    权限真相，与 discovery §3.6「物理一件产品两件」同款陷阱。
  · **只读共享**：被分享方能看能下载，不能删/改分类/加知识库。
  · **运行数据与知识库文件不参与**：前者是 agent 副产物（共享只有噪音），
    后者有自己的 scope 体系（`kb_files.scope`），两套语义不能纠缠。
  · **无 feature flag**：本系统用户基数小（单机 prod），灰度放量无从谈起；
    本实现无 schema 变更，镜像 revert 即完全回退，flag 的边际价值不抵其分叉成本。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from bowhead_core.artifact_store import Artifact

log = logging.getLogger(__name__)

#: Resource kind in grants, alongside kb_file / team_skill / tool.
ARTIFACT_RESOURCE_KIND = "artifact"

ShareSubject = Literal["workspace", "team"]

#: Only assets can be shared. Runtime data is an agent-work byproduct; sharing it adds noise.
SHAREABLE_KINDS: frozenset[str] = frozenset({"user_upload", "deliverable", "canvas_export"})


class ShareRejected(Exception):
    """不允许分享（规则，不是故障）。"""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def assert_shareable(artifact: Artifact, *, in_kb: bool) -> None:
    """分享前的规则检查。防线在服务层而非 UI —— UI 只是不显示入口。

    Raises:
        ShareRejected: 运行数据 / 知识库文件 / 已删除。
    """
    if artifact.deleted_at:
        raise ShareRejected("deleted")
    kind = getattr(artifact, "kind", None) or "tool_data"
    if kind not in SHAREABLE_KINDS:
        # Same asset/runtime-data distinction as D1.
        raise ShareRejected(f"kind_not_shareable:{kind}")
    if in_kb:
        # KB-file sharing is controlled by kb_files.scope; parallel sharing semantics make
        # file visibility impossible to explain clearly.
        raise ShareRejected("in_kb_use_knowledge_base_scope")


async def shared_artifact_ids(
    access_store: Any,
    *,
    team_id: str,
    user_id: str,
    workspace_id: str | None,
    group_ids: tuple[str, ...] = (),
) -> frozenset[str]:
    """别人分享给我的 artifact id 集。

    单次批量查询（`granted_resource_ids` 一条 SQL），不做 per-row 判定——
    列表是热路径。缺 store / 缺上下文时返回空集（零开销）。
    """
    if access_store is None or not team_id or not user_id:
        return frozenset()
    try:
        return await access_store.granted_resource_ids(
            team_id,
            user_id=user_id,
            group_ids=group_ids,
            workspace_id=workspace_id,
            kind=ARTIFACT_RESOURCE_KIND,
        )
    except Exception as e:  # noqa: BLE001 - Failed grant lookup means no sharing; never admit.
        log.warning("files_sharing.granted_ids_failed user=%s error=%s", user_id, e)
        return frozenset()


async def can_read_shared(
    access_store: Any,
    *,
    artifact_id: str,
    team_id: str,
    user_id: str,
    workspace_id: str | None,
    group_ids: tuple[str, ...] = (),
) -> bool:
    """下载/预览路径的 grant 检查——owner check 失败后的第二次机会。

    **每次实查，不缓存**：撤销必须立即生效（design §2.3-4）。
    """
    ids = await shared_artifact_ids(
        access_store,
        team_id=team_id,
        user_id=user_id,
        workspace_id=workspace_id,
        group_ids=group_ids,
    )
    return artifact_id in ids
