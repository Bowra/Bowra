"""上传产物的空间归属决策 —— 全 gateway 唯一实现。

**为什么收口**：同一个表达式 `ctx.workspace_id if scope != "team" else ""` 在 `routes/`
下有 **6 份手抄**——`kb.py` 的单文件上传 / bundle 上传 / promotion，与 `kb_presign.py` 的
单文件 presign / bundle presign / bundle confirm（promotion 只算一处；初版写成「6 处 + promotion」
是重复计数，2026-09-19 review 更正）。它产出的值有**两类消费方、空串语义相反**：

| 消费方             | 空串的含义                                             |
| ------------------ | ------------------------------------------------------ |
| `artifacts` 列     | 空串要翻成显式哨兵 `__team__`（否则切点后被 fail-close 拦） |
| `kb_files` 列      | 空串**本身就是**团队档的合法值（冻结语义，KB scope 过滤依赖它） |

⇒ 本模块同时产出两个值，调用方各取所需。**绝不能把哨兵喂给 `kb_files`**——那会改变
KB 的 scope 过滤行为。这正是本次收口必须一次做对的地方。

**收口那批的承诺是「零行为变化」**：`decide_for_kb_scope()` 的返回值与被替换的 6 处表达式**逐字等价**，
本模块只是把它们收到一处并**加上可观测性**。要不要改变 `ATTRIBUTION_NONE` 那一档的取值
（今天它落 `__team__`／`NULL`），是**尚未拍板**的产品问题，不在本批。

**为什么需要可观测性**：2026-09-19 真机实测，`POST /api/v1/files/upload` 在前端真实形状下
（无空间头、无 `session_id`）归属落 **NULL**，而 C4 此前一直是 `0.0000%` —— 那个 0 只说明
上传族零样本，不说明写入点健康。要拍板「没有归属输入时该落什么」，先得知道它**多高频**。
⇒ 各调用点用 `reason` 打点，先积累真实分布，再拍值。

**打点机制的取舍**：

① 日志行用 stdlib `log.info` 而非 structlog，是**有意偏离** `docs/coding-standards.md` §5.1 那条禁令：
   四个路由模块今天全部用 stdlib logging，单独引入 structlog 会让同一文件里并存两套日志系统。
   ⚠ 顺带记一条被实测证伪的直觉：**不要照抄 KB 那套 `emit_audit`**——它用 `extra=` 传参，
   而本仓当前的 logging 配置（`main.py` 只调 `logging.basicConfig`，没接 JSON formatter）
   **从不渲染 `extra`**，于是 audit 行的 event_type 与 payload 实际上整个丢失、不可查。

② **`MetricsLog.track` 落库挂在默认关的 `FEATURE_ARTIFACT_ATTRIBUTION_METRICS` 后**
   （`track_attribution` / `attribution_metrics_enabled`）。动机：docker logs 是 per-container、重建即清空，
   而这批数据要支撑一个**尚未拍板**的产品决定，不该下次部署就蒸发。
   ⚠ **灰度操作（怎么读、怎么开、验收、回滚、退出条件）只在 `docs/灰度功能清单.md`「产物归属打点灰度」维护**，
   本文件不重复——CLAUDE.md 禁止在别处复制灰度操作说明。

③ ★★ **写库脱离请求路径，但不是零成本**（2026-09-21 三轮 review；别改回内联 `await`，也别再写「零影响」）：
   `MetricsLog` 与产物库是**同一个 sqlite 文件**（`main.py` 里都取 `db_path`），`track()` 每次自开连接并 commit，
   默认 `busy_timeout=5000ms`。
   - **内联 `await` 是错的**：锁被占时上传要多等最多 5 秒。⚠ 措辞按第三轮复核更正：**「会改变结果」只在早先把打点放在
     『决策之时』的位置上实测成立**（锁被占 7 秒：flag 关 500、flag 开 200）；现在打点在成功之后，内联 `await`
     在同样的锁下不再翻转结果（500/500、200/200），剩下的只是**纯增加延迟**——这一条足以否决内联。
     仓里早有同族先例：`lead_orchestrator.py` 的 `response_kind_scanner_decision` 打点原先也是内联 `await`，
     因「阻塞流式回调（TTFT 回归）」改成了 detached task。本模块沿用 `files.py` 自己的 `_classify_bg_tasks`
     写法（`create_task` + `app.state` 上留引用集合 + 完成时 discard）。
   - **后台任务也不是零成本**：它仍是同一个单写者文件上**多出来的一个写者**。实测（真 `files_presign` 路由 + 真 WAL 文件）
     的**方向**在几位评审那里一致，**幅度随压测方式差异很大**：连续顺序请求 p50 约 1ms → 约 2.7ms；
     写压饱和时 flag 开更差（p50/p99 与总耗时上升、边缘请求被推向 5s 超时、并发过高时大量打点行被上限丢弃）；
     但「在多少并发下开始出现 5xx」各次测量并不一致（进程内直驱 600 并发曾见 6–7%，另一次 300–600 并发两边都是 0%，
     900 并发时关 0.2–0.5%、开 0.6–1.2%）——**别引用任何一个绝对数字**。116 曾有单日 1022 条产物（09-05，所有来源、不只上传），
     离这个量级差好几个数量级，所以**不为这个临时测量另造批量写队列**；使用限制见灰度清单。
   - 停机时排队中的写会让进程多等最多一个 `busy_timeout`（5s），且那几行会丢（best-effort）。
   - 待处理任务数有上限（`_MAX_PENDING_METRIC_TASKS`），防 I/O 卡住时无界堆积。

④ ★ **打点位置＝「成功之后」，不是「决策之时」**：调用点放在各路由**成功 `return` 之前**，
   于是被 403 / 413 / 415 / 429 / 503 拒掉的请求、决策之后才失败的请求、确认重试，**结构上**不会被计数，
   计数口径统一为「一次成功完成的上传步骤」。原日志行仍在决策点（未动）。
   `files_upload` / `files_presign` 的 `reason` 读的是**落库后的真实值**而不是请求意图
   （见 `reason_for_resolved_attribution`）。
   ⚠ **8 个打点路由里，KB 的 7 个上传类入口的 `reason=none` 在生产里不可达**：真实 RBAC 下上传 / presign / confirm
   缺空间头直接 400 `workspace_required`（`access_context.py`）；只有测试夹具里那个不带 RBAC 的假 `UserStore` 才走得到。
   所以这 7 个路由在生产里只会记 `space` / `team`，真出现 `none` 反而是「这道 400 被绕开了」的告警。
   （例外：`POST /kb/files/from-artifact` 晋升路径**有意** `allow_missing_workspace=True`——`/files` 页是个人资产库、
   不发空间头，严格版上线后曾让「加入知识库」100% 失败——所以 personal 档的 `none` 在那里可达；
   但它不创建 artifact，也不打点。）

同族先例：`artifact_origin.py`（source/kind → origin 的唯一实现，同样因手抄漂移才收口）。
守卫见 `test_artifact_attribution_guard.py`（禁止第 7 份手抄）。
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import TYPE_CHECKING, Any, Final, NamedTuple

from bowhead_core.artifact_store import TEAM_SCOPE_WORKSPACE_ID

if TYPE_CHECKING:
    from fastapi import Request

logger = logging.getLogger(__name__)

#: Attribution-source categories, **for metrics only**, never authorization decisions.
#: Names are shared with log events; changing these changes production metric definitions.
ATTRIBUTION_SPACE: Final = "space"
"""调用方带着真实空间上下文（`ws_*`）。"""

ATTRIBUTION_TEAM: Final = "team"
"""显式的团队档上传（`scope="team"`）——本来就不属于任何单个空间。"""

ATTRIBUTION_SESSION: Final = "session"
"""没有显式空间，但有 session/turn，归属由推导梯子解析出了值。"""

ATTRIBUTION_NONE: Final = "none"
"""★ 要量的那一档：**既没有空间上下文，也没有 session** ⇒ 归属推不出来。

今天的落值**随入口而异**：KB 族的 `artifacts` 列拿 `__team__`、`kb_files` 列拿空串；
`files_upload` / `files_presign` 的 `artifacts` 列拿 `NULL`。
⚠ **在 8 个打点路由里，生产真正会走到这一档的只有 `files_upload` / `files_presign`**——KB 的 7 个上传类入口缺空间头
会先被 400 `workspace_required` 拒掉（见模块 docstring ④），它们的 `none` 只在测试夹具里可达；
（`from-artifact` 晋升路径是例外，它有意放行缺失空间，但不创建 artifact、也不打点。）
这一档该落什么值**尚未拍板**。在拍板前，本模块**不改变**任何取值，只负责把它数出来。
"""

VALID_REASONS: Final = frozenset({ATTRIBUTION_SPACE, ATTRIBUTION_TEAM, ATTRIBUTION_SESSION, ATTRIBUTION_NONE})

#: Metric event prefix. Routes use their own logger for `log.info(ATTRIBUTION_EVENT, ...)`,
#: allowing one prefix to retrieve attribution distributions across all upload surfaces.
ATTRIBUTION_EVENT: Final = "artifact.attribution"

#: Allowed metric route names. **Runtime validation**: a typo silently creates an isolated rollup bucket.
ATTRIBUTION_ROUTES: Final = frozenset(
    {
        "kb_single_upload",
        "kb_bundle_upload",
        "kb_admin_import",
        "kb_google_import",
        "kb_presign_single",
        "kb_presign_bundle",
        "kb_presign_bundle_confirm",
        "files_upload",
        "files_presign",
    }
)


class UploadAttribution(NamedTuple):
    """一次上传的归属决策结果。

    ⚠ `scope_workspace_id` 与 `artifact_workspace_id` **不可互换**（见模块 docstring 的表）。
    """

    scope_workspace_id: str
    """给 `kb_files.workspace_id` 用：空串＝团队/无空间档（冻结语义，勿改）。"""

    artifact_workspace_id: str
    """给 `artifacts.workspace_id` 用：空串已翻成显式哨兵 `__team__`。"""

    reason: str
    """`VALID_REASONS` 之一，仅用于打点。"""


def decide_for_kb_scope(scope: str, ctx_workspace_id: str | None) -> UploadAttribution:
    """KB 上传族（`kb.py` / `kb_presign.py`）的归属决策。

    与被替换的 6 处表达式逐字等价：
        ``workspace_id = ctx.workspace_id if scope != "team" else ""``
        ``artifacts 列 = workspace_id or TEAM_SCOPE_WORKSPACE_ID``

    ★ `scope` 双认 `"space"` / `"workspace"`（批3/c1 的读双认口径），此处不做归一化——
      它只影响「是不是 team」这一个判断，而两种拼写都不是 `"team"`。
    """
    raw = "" if scope == "team" else (ctx_workspace_id or "")
    if scope == "team":
        reason = ATTRIBUTION_TEAM
    elif raw:
        reason = ATTRIBUTION_SPACE
    else:
        # personal (or space scope without a space header): use the sentinel for now, pending a decision.
        # ⚠ This branch is **unreachable in production** for the 8 metric routes: real RBAC first returns 400 `workspace_required`.
        # Only fixture fake `UserStore`s without RBAC hooks reach it. The **sole production caller** is `from-artifact` promotion
        # (intentionally `allow_missing_workspace=True`, reads only `scope_workspace_id`, no metrics). Preserve it for zero behavior changes.
        reason = ATTRIBUTION_NONE
    return UploadAttribution(
        scope_workspace_id=raw,
        artifact_workspace_id=raw or TEAM_SCOPE_WORKSPACE_ID,
        reason=reason,
    )


_TRUTHY: Final = frozenset({"1", "true", "yes", "on"})
_ATTRIBUTION_METRICS_ENV: Final = "FEATURE_ARTIFACT_ATTRIBUTION_METRICS"

#: Persisted event name. **Must be unique**: `/admin/metrics/rollup` filters `name` exactly;
#: one query sees one name, so put the route in `attrs`, never in the name.
ATTRIBUTION_METRIC_NAME: Final = "artifact.attribution"

#: Allowed `attrs` keys. **Low-cardinality fields only**: rollup sorts and joins every attrs key/value pair into
#: **one string** as its grouping key. High-cardinality workspace_id / artifact_id / filenames
#: explode `by_attrs` into singleton keys, obscuring the distribution (and exposing IDs to the admin surface).
#: `source` is only for `files_upload`, constrained by files.py's `_ALLOWED_UPLOAD_SOURCES` allowlist (4 values).
_ALLOWED_METRIC_ATTRS: Final = frozenset({"route", "reason", "scope", "source"})

#: Background-task set on `app.state` (prevents GC, like files.py's `_classify_bg_tasks`).
_METRIC_TASKS_ATTR: Final = "_attribution_metric_tasks"

#: Concurrent metric-task cap. Tasks normally finish in milliseconds, keeping this almost empty; bound accumulation if writes stall.
_MAX_PENDING_METRIC_TASKS: Final = 256

#: Normalize legacy `scope` spelling (batch 3/c1 dual-read semantics). Only metric buckets change, never stored values.
_SCOPE_ALIASES: Final = {"workspace": "space"}

#: Valid `scope` values after normalization. Request parsing already bounds them with `Literal`; validate **values** again
#: because `_ALLOWED_METRIC_ATTRS` checks only keys. Passing `scope_id` as `scope` at a caller creates high-cardinality buckets.
_VALID_SCOPES: Final = frozenset({"personal", "space", "team"})


def attribution_metrics_enabled() -> bool:
    """``FEATURE_ARTIFACT_ATTRIBUTION_METRICS`` —— 默认 **关**。

    - **关** = 上传的行为、响应、写入值与今天完全一致：不排任何后台任务、不多一次 DB 写。
      ⚠ 唯一差别在**观测**且与本 flag 无关、始终生效：`artifact.attribution.files_presign` 日志行的 `reason`
      改按落库结果分类（更正了一个把 `none` 记成 `session` / `space` 的口径错误），旧口径下 09-20 攒的日志分布
      对这个路由不可与新口径直接比较。
    - **开** = 每次**成功完成**的上传步骤，在请求路径**之外**排一个后台任务往 ``metrics_event_log`` 落一行。

    只认 `1` / `true` / `yes` / `on`（大小写不敏感、去首尾空白）；`0` / `false` / `off` / 空串 / 其他任何值都是**关**。

    开启后每次上传多一次 sqlite connect + commit，是同一个单写者文件上多出来的写者（模块 docstring ③ 有实测数字与
    使用限制），所以按 CLAUDE.md 默认关、灰度后再翻。怎么读 / 开 / 验收 / 回滚 / 退出：
    见 `docs/灰度功能清单.md`「产物归属打点灰度」。
    """
    return (os.environ.get(_ATTRIBUTION_METRICS_ENV) or "").strip().lower() in _TRUTHY


async def _write_metric(metrics_log: Any, attrs: dict[str, str]) -> None:
    """后台任务体：真正写库。`MetricsLog.track` 自己吞 DB 异常，这里再兜一层防任务异常无人取。"""
    try:
        await metrics_log.track(ATTRIBUTION_METRIC_NAME, attrs=attrs)
    except Exception as e:  # noqa: BLE001 — Metric failures must never escape.
        logger.warning("%s.metric_track_failed route=%s err=%s", ATTRIBUTION_METRIC_NAME, attrs.get("route"), e)


async def track_attribution(
    request: Request,
    *,
    route: str,
    reason: str,
    scope: str | None = None,
    source: str | None = None,
) -> None:
    """把一次**已成功完成**的上传步骤的归属分类排进 ``metrics_event_log``（flag 关时直接返回）。

    ★★ **绝不让上传失败或改变响应**：本函数**只排一个后台任务就返回**（`async` 只是为了让调用点写法统一，
    它**不挂起**），真正的 sqlite 写在请求路径之外，所以请求**不会等它**。
    ⚠ 但它**不是零成本**——仍是同一个单写者文件上多出来的一个写者，写饱和时可能把边缘请求推过
    5s 超时（模块 docstring ③ 有实测数字）；别把它读成「绝对无影响」。
    全部逻辑在一个 try 里（包括读 `request.app.state`）：任何异常只 `logger.warning`，绝不冒泡。
    `except Exception` 不吞 `CancelledError`（Python 3.12 里它是 `BaseException`）。

    ★ `attrs` 只放 `_ALLOWED_METRIC_ATTRS` 里的低基数键；`route` / `reason` 拼错或不在
      `ATTRIBUTION_ROUTES` / `VALID_REASONS` 里时**直接丢弃并告警**，不写脏桶。
    ★ `scope` 的旧拼写 `workspace` 归一成 `space`：`Form` 路由收到的是原始拼写，JSON 路由经 pydantic 已归一，
      不归一会把同一个 `space` 桶劈成两个。
    ★ **不落 user_id**：rollup 不读它，白存 90 天的「按人上传时间戳」没有消费方，
      而且 `metrics_event_log` 不会随用户删除而清理。
    ★ 待处理任务数上限 `_MAX_PENDING_METRIC_TASKS`：写卡住（磁盘 I/O 停顿）时不无界堆积任务与 aiosqlite 线程。
    """
    if not attribution_metrics_enabled():
        return
    try:
        if route not in ATTRIBUTION_ROUTES or reason not in VALID_REASONS:
            logger.warning("%s.metric_rejected route=%s reason=%s", ATTRIBUTION_METRIC_NAME, route, reason)
            return
        app = request.app
        metrics_log = getattr(app.state, "metrics_log", None)
        if metrics_log is None:
            return
        attrs: dict[str, str] = {"route": route, "reason": reason}
        if scope is not None:
            normalised_scope = _SCOPE_ALIASES.get(scope, scope)
            if normalised_scope not in _VALID_SCOPES:  # Reject high-cardinality scope_id values such as `ws_*` passed as scope.
                logger.warning("%s.metric_rejected_scope route=%s scope=%r", ATTRIBUTION_METRIC_NAME, route, scope)
                return
            attrs["scope"] = normalised_scope
        if source is not None:
            attrs["source"] = source
        if not attrs.keys() <= _ALLOWED_METRIC_ATTRS:  # Guard future high-cardinality attrs keys.
            logger.warning("%s.metric_rejected_attrs keys=%s", ATTRIBUTION_METRIC_NAME, sorted(attrs))
            return
        tasks: set[asyncio.Task[None]] | None = getattr(app.state, _METRIC_TASKS_ATTR, None)
        if tasks is None:
            tasks = set()
            setattr(app.state, _METRIC_TASKS_ATTR, tasks)
        if len(tasks) >= _MAX_PENDING_METRIC_TASKS:
            logger.warning("%s.metric_dropped_backlog route=%s pending=%d", ATTRIBUTION_METRIC_NAME, route, len(tasks))
            return
        coro = _write_metric(metrics_log, attrs)
        try:
            task = asyncio.create_task(coro, name="artifact-attribution-metric")
        except Exception:
            coro.close()  # Failed create_task leaves this unawaited; close to avoid `never awaited` RuntimeWarning.
            raise
        tasks.add(task)
        task.add_done_callback(tasks.discard)
    except Exception as e:  # noqa: BLE001 — Metric failures must never bubble into uploads.
        logger.warning("%s.metric_schedule_failed route=%s err=%s", ATTRIBUTION_METRIC_NAME, route, e)


async def drain_attribution_metrics(app: Any) -> None:
    """等本进程里已排队的打点任务全部写完。

    测试用（在 `TestClient` 的 portal 里调，见 conftest 的 `attribution_metrics` 夹具）；
    也是将来需要「优雅停机不丢队列」时可以直接挂上的钩子。
    """
    tasks = list(getattr(app.state, _METRIC_TASKS_ATTR, None) or ())
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


def reason_for_resolved_attribution(resolved_workspace_id: str | None, *, via_explicit_space: bool = False) -> str:
    """按**落库结果**分类（而不是按调用方意图）——给成功之后的打点用。

    ★★ 为什么按结果、不按意图（2026-09-19 review，2026-09-21 又在 `files_presign` 上逮到同一个坑）：
      `files.py` 的 `/upload` 归属由 `_resolve_write_workspace_id` **事后推导**，它在 session 行不存在、
      `sessions.workspace_id` 为空、甚至吞掉 `aiosqlite.OperationalError` 时**都返回 None**；
      `files_presign` 的 `_require_upload_space` 在 **session 分支**返回 session 自己的空间（可以是空串），
      并且**完全无视**请求头。所以「请求带了 `session_id` / 空间头」≠「真的落了归属」。
      按意图计数会让 `ATTRIBUTION_NONE` 被低估，而这个偏差方向恰好是**不安全**的那一侧——
      它会让人以为「推不出归属」很罕见、可以翻闸。这批打点的全部目的就是量准 NONE。

    分类：
      - 没落值（`None` / 空串）⇒ `ATTRIBUTION_NONE`；
      - 落了值、且**值来自显式空间头**（`via_explicit_space=True`）⇒ `ATTRIBUTION_SPACE`；
      - 落了值、值来自 session / turn / canvas 梯子 ⇒ `ATTRIBUTION_SESSION`。
    ★ 由**调用方**说明值是哪一支给的，而不是在这里靠「落值是否等于请求头」去猜：
      头与 session 恰好指向同一个空间时，值相等猜法会把 session 分支记成 `space`（2026-09-21 B 评审）。
    `files.py` 的 `/upload` 不接受空间头，调用时不传 `via_explicit_space`，落了值即 `SESSION`。
    """
    if not resolved_workspace_id:
        return ATTRIBUTION_NONE
    return ATTRIBUTION_SPACE if via_explicit_space else ATTRIBUTION_SESSION
