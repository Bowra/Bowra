"""原文件丢失对账：把「行在、原文件不在」的产物标为 missing，文件回来后恢复 ready。

设计见 docs/sprints/artifact-volume-2026-09-04/batch0-missing-tech-design.md。

只认**确定的不存在**（backend.head 返回 None），权限、网络、超时一律视为判断不了、不改状态。
落标记前两道闸：

1. 存储健康探针：最近创建的 ready 行应当读得到。卷没挂上时所有文件都表现为「不存在」，
   与真丢失无法区分；这时最近的文件也读不到，本轮一律不标。
2. 比例闸：本轮新判为不存在的行超过已检查行的 20% 且超过 50 条 ⇒ 视为存储问题而不是个别丢失，
   不自动落标记，须运维确认（``bowhead artifacts reconcile --apply --force-ratio``）。
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from bowhead_core.artifact_store import LocalBackend

if TYPE_CHECKING:
    from bowhead_core.artifact_store import ArtifactBackend, ArtifactStore

BlobState = Literal["present", "absent", "unknown"]

_TRUTHY = {"1", "true", "yes", "on"}

# Storage health probe: sample recently created ready rows; insufficient readable proportion means unhealthy storage.
HEALTH_SAMPLE = 20
HEALTH_MIN_PRESENT_RATIO = 0.8
# Ratio gate: newly absent rows exceed both thresholds ⇒ do not automatically mark.
RATIO_GATE = 0.20
RATIO_GATE_MIN_ABSENT = 50
BATCH_SIZE = 500


def reconcile_apply_enabled() -> bool:
    """``FEATURE_ARTIFACT_MISSING_RECONCILE``：关（默认）时每日作业只统计不落标记。"""
    return os.environ.get("FEATURE_ARTIFACT_MISSING_RECONCILE", "0").strip().lower() in _TRUTHY


async def probe_blob(backend: ArtifactBackend, path: str) -> BlobState:
    """原文件在不在。只有 head 明确返回 None 才算不在；任何异常都是判断不了。"""
    if not path:
        return "absent"
    try:
        if await backend.head(path) is not None:
            return "present"
        # Match KB worker's original-file fallback: retry local relative paths with .data/ prefix; readable means not lost.
        if isinstance(backend, LocalBackend) and not Path(path).is_absolute():
            if await backend.head(str(Path(".data") / path)) is not None:
                return "present"
    except Exception:  # noqa: BLE001 - Permissions/network/timeouts: cannot decide; leave state unchanged
        return "unknown"
    return "absent"


@dataclass
class ReconcileResult:
    checked: int = 0
    absent: int = 0  # Found absent this round and still currently ready
    restored_candidates: int = 0  # Currently missing but file has reappeared
    unknown: int = 0
    marked: int = 0
    restored: int = 0
    applied: bool = False
    # skipped_reason: storage_unhealthy / ratio_gate / dry_run / "" (marked or nothing to do)
    skipped_reason: str = ""
    health_present: int = 0
    health_checked: int = 0
    sample_absent: list[str] = field(default_factory=list)

    def summary(self) -> dict[str, object]:
        return {
            "checked": self.checked,
            "absent": self.absent,
            "restorable": self.restored_candidates,
            "unknown": self.unknown,
            "marked": self.marked,
            "restored": self.restored,
            "applied": self.applied,
            "skipped_reason": self.skipped_reason,
            "health": f"{self.health_present}/{self.health_checked}",
        }


async def storage_healthy(store: ArtifactStore) -> tuple[bool, int, int]:
    """最近创建的 ready 行读得到的比例是否达标。没有任何 ready 行时视为健康（无事可做）。"""
    recent = await store.list_recent_ready_paths(limit=HEALTH_SAMPLE)
    if not recent:
        return True, 0, 0
    states = [await probe_blob(store.backend, path) for _, path in recent]
    present = sum(1 for s in states if s == "present")
    return present / len(recent) >= HEALTH_MIN_PRESENT_RATIO, present, len(recent)


async def reconcile_missing_artifacts(
    store: ArtifactStore,
    *,
    apply: bool,
    force_ratio: bool = False,
    batch_size: int = BATCH_SIZE,
) -> ReconcileResult:
    """扫描全部 ready / missing 行并按规则切换状态。``apply=False`` 只统计。"""
    result = ReconcileResult()
    healthy, result.health_present, result.health_checked = await storage_healthy(store)
    if not healthy:
        result.skipped_reason = "storage_unhealthy"
        return result

    to_mark: list[str] = []
    to_restore: list[str] = []
    after = ""
    while True:
        rows = await store.list_reconcile_candidates(after_id=after, limit=batch_size)
        if not rows:
            break
        for aid, path, status in rows:
            state = await probe_blob(store.backend, path)
            result.checked += 1
            if state == "unknown":
                result.unknown += 1
            elif state == "absent" and status == "ready":
                to_mark.append(aid)
            elif state == "present" and status == store.MISSING_STATUS:
                to_restore.append(aid)
        after = rows[-1][0]
        await asyncio.sleep(0)  # Yield the event loop between batches
    result.absent = len(to_mark)
    result.restored_candidates = len(to_restore)
    result.sample_absent = to_mark[:20]

    if not apply:
        result.skipped_reason = "dry_run"
        return result
    # Restoration is always safe (file exists); new missing marks must pass the ratio gate.
    result.restored = await store.set_blob_missing(to_restore, missing=False)
    over_gate = result.absent > RATIO_GATE_MIN_ABSENT and result.absent / max(result.checked, 1) > RATIO_GATE
    if over_gate and not force_ratio:
        result.skipped_reason = "ratio_gate"
        result.applied = bool(result.restored)
        return result
    result.marked = await store.set_blob_missing(to_mark, missing=True)
    result.applied = True
    return result
