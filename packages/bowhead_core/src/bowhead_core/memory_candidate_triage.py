"""记忆重做 批2 —— FR6 存量候选处置的**判据**（tech-design §5.3）。

判据放在库里而不是脚本里，理由有两条：
① 脚本要能被单测钉住（`scripts/` 下的东西没人跑测试）；
② **同一套判据同时驱动「实时写入闸」与「存量清理」** —— 语言族 / 一次性族直接复用
   `memory_write_gate`，判重复用 `memory_write_policy`。两边各写一份的话，
   清理脚本判成「一次性」的那条，实时闸可能照样放行（「一个概念 N 处实现」）。

四分类（依据 discovery §8 已经读完 58 条得出的结论）：

| 桶            | 量   | 处置                                              |
| ------------- | ---- | ------------------------------------------------- |
| `settings`    | 8    | reject，reason=`moved_to_settings`                |
| `garbage`     | 3    | reject，reason=`low_quality`                      |
| `one_off`     | ~12  | reject，reason=`one_off_request`                  |
| `keep`        | ~12  | **保持 pending** —— approve 是用户的权利，脚本不代批 |

外加两种**组内**处置（同一主张的多条记录）：
- 重复组：只留最新一条 pending，其余 reject `duplicate_of:<cand_id>`；
- 矛盾组：只留时间最新的一条，旧的 reject `superseded`。

⚠ `reject` 会写 `memory_candidate_suppressions`（30 天）。被压制的主张 30 天后
可能重新冒出来 —— 这是既有设计，不改；但报表必须标出来，别让人以为「永远清干净了」。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from bowhead_core.memory_write_gate import (
    REASON_LANGUAGE_IS_A_SETTING,
    REASON_ONE_OFF,
    _key_is_language_setting,
    looks_like_language_preference,
    one_off_markers,
)
from bowhead_core.memory_write_policy import is_near_duplicate, is_verbatim_duplicate

BUCKET_SETTINGS = "settings"
BUCKET_GARBAGE = "garbage"
BUCKET_ONE_OFF = "one_off"
BUCKET_KEEP = "keep"

#: ⚠ Never include bare `主题` / `配色` / `色系`: Chinese `主题` usually means
#: topic/title, not UI theme ("报告**主题**按季度拆" / "分析**主题**固定为营收"),
#: otherwise content preferences become appearance settings and get rejected; reproduced in review.
#: Nonlanguage members of the "belongs in settings" family. discovery §8 names:
#: dark+compact, soft palette, week starts Monday; all already have deterministic appearance/space setting fields,
#: so memory conflicts with settings (user changes settings, memory retains old value).
_SETTINGS_MARKERS: tuple[str, ...] = (
    "暗色",
    "深色",
    "dark mode",
    "紧凑",
    "compact",
    "柔和色系",
    "周起始",
    "周一为一周",
    # key is snake_case (`week_start_day`), value natural language; recognize both,
    # or neither matches `combined_text` "week_start_day Monday".
    "week start",
    "week_start",
    "start of week",
)

#: Garbage criteria. discovery §8 names three: `"1"` (value is numeric 1),
#: `most_recent_working_day` (subjectless fragment), `个人测试工作区` (tool_context, not preference).
_GARBAGE_MARKERS: tuple[str, ...] = (
    "个人测试工作区",
    "测试工作区",
)


@dataclass(frozen=True)
class TriagedCandidate:
    """一条候选的分类结果。`decision` 是 `--apply` 真正要执行的动作。"""

    candidate_id: str
    kind: str
    key: str
    value_text: str
    created_at: str
    bucket: str
    reason: str
    #: Retained member of duplicate/conflict group (nonempty when this entry is superseded by it).
    superseded_by_candidate_id: str | None = None
    #: ★ **Same timestamp as keeper** ⇒ "take latest" has no discriminating evidence for this pair.
    #: Report must show it for human review, never imply a chronological choice.
    tie_with_keeper: bool = False


@dataclass(frozen=True)
class TriageReport:
    triaged: list[TriagedCandidate] = field(default_factory=list)
    #: claim_key → retained candidate_id for the group.
    groups: dict[str, str] = field(default_factory=dict)

    def ties(self) -> list[TriagedCandidate]:
        """时间戳打平、靠 `order_hint`/id 兜底决出的那些对。**必须人工过一眼。**"""
        return [t for t in self.triaged if t.tie_with_keeper]

    def by_bucket(self) -> dict[str, list[TriagedCandidate]]:
        out: dict[str, list[TriagedCandidate]] = {}
        for item in self.triaged:
            out.setdefault(item.bucket, []).append(item)
        return out

    def rejects(self) -> list[TriagedCandidate]:
        """真正要 reject 的：三个坏桶 + 组内被取代的。`keep` 且未被取代的不动。"""
        return [t for t in self.triaged if t.bucket != BUCKET_KEEP or t.superseded_by_candidate_id]


def _looks_like_garbage(value_text: str, key: str = "") -> bool:
    text = value_text.strip()
    if not text:
        return True
    # ① Named markers are **content** criteria, independent of key (`个人测试工作区` is
    # leftover tool_context, not preference; any key still makes it garbage).
    if any(marker in text for marker in _GARBAGE_MARKERS):
        return True
    # ② Shape criterion (pure number) holds **only without key**. With a meaningful key,
    # numeric value is legitimate: `{key:'kpi_top_n', value:'5'}` /
    # `{key:'money_unit_scale', value:'1000000'}` are standard structured preferences.
    # Same reason this module removed length/identifier-shape thresholds:
    # **shape judgments need distinguishing context**; without it, do not judge.
    if str(key or "").strip():
        return False
    # ★ **Do not reject identifier-shaped text**: `most_recent_working_day` (§8 fragment)
    # and `revenue_in_millions` (legitimate short preference) have **identical shapes**; cannot separate without a model.
    # If inseparable, defer to report human review (§5.3 already requires intermediate review).
    stripped = text.replace("_", "").replace("-", "").replace(".", "").replace(",", "")
    return bool(stripped.isdigit())


def classify_one(*, value_text: str, key: str = "") -> tuple[str, str]:
    """单条分类 → `(bucket, reason)`。**不看组**，组内处置见 `triage`。

    ★ 分类看的是 **key + value**（与实时闸同一个 `combined_text`）：结构化候选的语义
    常常全在 key 上 —— `{key:'answer_language', value:'English'}` 单看 value 只是个
    普通英文词，会被判成「真价值、交用户裁」，而它恰恰属于本 sprint 要清的最大一桶。
    ⚠ **判重仍然只比 value**（见 `triage`）：key 不同但说的是同一件事，才是该合并的那种。
    """
    from bowhead_core.memory_write_gate import combined_text

    text = combined_text(key=key, value_text=value_text)
    # ★ **Garbage criteria must run last**: coarsest of four (shape, no semantics),
    # otherwise they seize entries with clear destinations: `用中文回答` should go to settings,
    # `毛利率优先` to keep; initially both became low_quality with 30-day suppression.
    # Same discipline as sibling module: **judgment order is correctness** (language before one-off there).
    if _key_is_language_setting(key) or looks_like_language_preference(value_text):
        return BUCKET_SETTINGS, REASON_LANGUAGE_IS_A_SETTING
    low = text.lower()
    if any(marker in low for marker in _SETTINGS_MARKERS):
        return BUCKET_SETTINGS, "moved_to_settings"
    if one_off_markers(text):
        return BUCKET_ONE_OFF, REASON_ONE_OFF
    if _looks_like_garbage(value_text, key):
        return BUCKET_GARBAGE, "low_quality"
    return BUCKET_KEEP, "user_decides"


def _sortable_created_at(raw: object) -> str:
    """★ `memory_candidates.created_at` 里**同时存着两种分隔符**：ISO 的 `T` 与
    sqlite 的空格（`candidates.py` 自己的注释就记着这条，别处的查询全用 `datetime()` 包）。
    按原始串排序时 `' '(0x20) < 'T'(0x54)`，于是 `2026-08-10 09:00` 会排在
    `2026-08-10T08:00` **前面** ⇒「取最新」又一次选反方向，而且 `tie_with_keeper`
    比的也是原始串，连人审提示都不会给。统一成 `T` 再比。"""
    return str(raw or "").replace(" ", "T", 1)


def triage(rows: list[dict[str, Any]]) -> TriageReport:
    """把 pending 候选整批分类 + 归并。

    `rows` 每条要有 `id` / `kind` / `key` / `value_text` / `created_at`，
    可选 `order_hint`（插入序，sqlite 传 `rowid`）。按什么顺序传都行（内部会排）。

    归并只在 **`keep` 桶内**做：坏桶里的条目本来就要 reject，再分组没有意义，
    而且会让报表把「因为重复被丢」和「因为是垃圾被丢」混在一起，人审时看不清。
    """
    # ★ Sorting `order_hint` is necessary: second-resolution `created_at` means same-batch candidates
    # **tie**, silently turning "latest" into random UUID selection; report still says
    # "latest", hiding reversal. First real run encountered Top 10 / Top 5
    # resolved to older Top 10. Callers should pass insertion order, such as SQLite `rowid`.
    ordered = sorted(
        rows,
        key=lambda r: (
            _sortable_created_at(r.get("created_at")),
            int(r.get("order_hint") or 0),
            str(r.get("id") or ""),
        ),
    )
    created_at_by_id = {str(r.get("id") or ""): _sortable_created_at(r.get("created_at")) for r in rows}
    triaged: list[TriagedCandidate] = []
    groups: dict[str, str] = {}
    # ★ Representatives must **partition by (user, space, kind)**; a global table marks user A's
    # candidate duplicate of user B's and rejects it; script defaults to all users (no `--user`),
    # directly deleting cross-tenant data on 116 production. Partition key → [(value_text, candidate_id)].
    representatives: dict[tuple[str, str, str], list[tuple[str, str]]] = {}
    superseded: dict[str, str] = {}

    for row in ordered:
        value_text = str(row.get("value_text") or "")
        bucket, reason = classify_one(value_text=value_text, key=str(row.get("key") or ""))
        cid = str(row.get("id") or "")
        if bucket == BUCKET_KEEP:
            partition = (
                str(row.get("user_id") or ""),
                str(row.get("workspace_id") or ""),
                str(row.get("kind") or ""),
            )
            bucket_reps = representatives.setdefault(partition, [])
            hit = next(
                (
                    rep_id
                    for rep_text, rep_id in bucket_reps
                    if is_verbatim_duplicate(value_text, rep_text) or is_near_duplicate(value_text, rep_text)
                ),
                None,
            )
            if hit is not None:
                # Later update ⇒ retain later, mark prior superseded (§5.3: latest).
                # ★ With ≥3 near-duplicates, **redirect all earlier pointers to hit toward the new keeper**,
                # or a→b, b→c leaves dangling pointers: report says "a superseded by b" while b
                # is also rejected, showing reviewers a soon-to-be-deleted target.
                # DISCOVERY_ROWS has only two per group, so missed this chain initially (net margin
                # was actually proposed 5 times).
                for old_id, keeper in list(superseded.items()):
                    if keeper == hit:
                        superseded[old_id] = cid
                superseded[hit] = cid
                bucket_reps[:] = [(t, i) for t, i in bucket_reps if i != hit]
            bucket_reps.append((value_text, cid))
        triaged.append(
            TriagedCandidate(
                candidate_id=cid,
                kind=str(row.get("kind") or ""),
                key=str(row.get("key") or ""),
                value_text=value_text,
                created_at=str(row.get("created_at") or ""),
                bucket=bucket,
                reason=reason,
            )
        )

    if superseded:
        triaged = [
            (
                t
                if t.candidate_id not in superseded
                else TriagedCandidate(
                    **{
                        **t.__dict__,
                        "reason": f"duplicate_of:{superseded[t.candidate_id]}",
                        "superseded_by_candidate_id": superseded[t.candidate_id],
                        "tie_with_keeper": (created_at_by_id.get(t.candidate_id, "") == created_at_by_id.get(superseded[t.candidate_id], "")),
                    }
                )
            )
            for t in triaged
        ]
    for bucket_reps in representatives.values():
        for text, cid in bucket_reps:
            groups[text] = cid
    return TriageReport(triaged=triaged, groups=groups)


__all__ = [
    "BUCKET_GARBAGE",
    "BUCKET_KEEP",
    "BUCKET_ONE_OFF",
    "BUCKET_SETTINGS",
    "TriageReport",
    "TriagedCandidate",
    "classify_one",
    "triage",
]
