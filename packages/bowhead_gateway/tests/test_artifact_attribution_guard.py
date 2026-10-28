"""上传归属决策的唯一实现守卫（空间隔离）。

历史：`workspace_id = ctx.workspace_id if scope != "team" else ""` 这个表达式在 `routes/` 下
曾有 **6 份手抄**（`kb.py` 的单文件上传 / bundle 上传 / promotion，与 `kb_presign.py` 的
单文件 presign / bundle presign / bundle confirm；promotion 只算一处），而它产出的值有
**两类消费方、空串语义相反**——`artifacts` 列要把空串翻成哨兵 `__team__`，`kb_files` 列的空串
**本身**就是团队档的合法值。手抄越多，两类越容易被混用。
现唯一实现在 `bowhead_gateway/artifact_attribution.py`（同族先例：`artifact_origin.py`）。

本守卫钉六件事：

1. **真值表**：`decide_for_kb_scope` 与被替换的表达式**逐字等价**（收口那批承诺零行为变化）
2. **两个输出不可互换**——把哨兵喂给 `kb_files` 会改变 KB 的 scope 过滤
3. `reason` 只取 `VALID_REASONS`，且 `ATTRIBUTION_NONE` 那一档能被单独数出来；
   files 族的 `reason` 按**落库结果**分类而不是按请求意图
4. `src/bowhead_gateway/` 下不得再出现**第 7 份**手抄
5. **打点**：flag 取值解析、默认关、脱离请求路径、失败绝不外溢、attrs 低基数且不带用户标识
6. **route 注册表**：调用点里出现的 route 名必须恰好等于 `ATTRIBUTION_ROUTES`

⚠ **关于 `_legacy_expression` 这个冻结基线的退休方式**：它存在是为了钉住「收口没顺手改语义」。
将来「没有归属输入时该落什么」那个**产品决定**落地时，正确做法是**整条删除本基线与相关用例**
（连同一条说明为什么），**不是**把基线改成与新实现匹配——后者会让这条守卫从此什么都不证明。
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import logging
from pathlib import Path
from types import SimpleNamespace

import bowhead_gateway.artifact_attribution as attribution_module
import pytest
from bowhead_core.artifact_store import TEAM_SCOPE_WORKSPACE_ID
from bowhead_gateway.artifact_attribution import (
    _ALLOWED_METRIC_ATTRS,
    _MAX_PENDING_METRIC_TASKS,
    ATTRIBUTION_METRIC_NAME,
    ATTRIBUTION_NONE,
    ATTRIBUTION_ROUTES,
    ATTRIBUTION_SESSION,
    ATTRIBUTION_SPACE,
    ATTRIBUTION_TEAM,
    VALID_REASONS,
    attribution_metrics_enabled,
    decide_for_kb_scope,
    drain_attribution_metrics,
    reason_for_resolved_attribution,
    track_attribution,
)

pytestmark = pytest.mark.unit

_FLAG = "FEATURE_ARTIFACT_ATTRIBUTION_METRICS"


#: Replaced original expression, retained verbatim as **differential baseline** (retirement policy in module docstring).
def _legacy_expression(scope: str, ctx_workspace_id: str) -> tuple[str, str]:
    workspace_id = ctx_workspace_id if scope != "team" else ""
    return workspace_id, (workspace_id or TEAM_SCOPE_WORKSPACE_ID)


@pytest.mark.parametrize(
    ("scope", "ctx_ws"),
    [
        ("team", "ws_42"),
        ("team", ""),
        ("space", "ws_42"),
        ("workspace", "ws_42"),  # Batch 3/c1 dual reads: neither spelling is team.
        ("space", ""),  # Space scope, but no space header.
        ("personal", "ws_42"),
        ("personal", ""),  # ★ Category awaiting a decision.
    ],
)
def test_decide_matches_the_legacy_expression_verbatim(scope: str, ctx_ws: str) -> None:
    """★ 收口那批承诺「零行为变化」——这条就是那句话的可执行形式。"""
    expected_scope_ws, expected_artifact_ws = _legacy_expression(scope, ctx_ws)
    got = decide_for_kb_scope(scope, ctx_ws)
    assert got.scope_workspace_id == expected_scope_ws
    assert got.artifact_workspace_id == expected_artifact_ws


def test_none_ctx_workspace_is_a_deliberate_widening() -> None:
    """★ seam 接受 `str | None`，而原表达式只吃 `str` —— 这是**有意扩域**，在此记录在案。

    故意**不**并进上面的真值表：legacy 传 `None` 会得到 `(None, '__team__')`，
    与 seam 的 `('', '__team__')` 不同，混进去会让那张表红得莫名其妙。

    今天不可达（`ScopeContext.workspace_id` 标注为 `str`，两条构造路径都给 `""` 兜底），
    方向也安全：legacy 的 `None` 落到 `kb_files.workspace_id`（NOT NULL）会 IntegrityError → 500，
    而 seam 归一成空串、与团队档同形。写下来是为了让「扩域」是个决定，不是意外。
    """
    got = decide_for_kb_scope("personal", None)
    assert got.scope_workspace_id == ""
    assert got.artifact_workspace_id == TEAM_SCOPE_WORKSPACE_ID


def test_two_outputs_are_not_interchangeable() -> None:
    """★★ 最要紧的一条：两个输出在 team 档上**必须不同**。

    `kb_files.workspace_id` 吃空串（团队档的冻结语义，KB scope 过滤依赖它）；
    `artifacts.workspace_id` 吃哨兵（否则切点后被 fail-close 拦掉）。
    """
    team = decide_for_kb_scope("team", "ws_42")
    assert team.scope_workspace_id == ""
    assert team.artifact_workspace_id == TEAM_SCOPE_WORKSPACE_ID
    assert team.scope_workspace_id != team.artifact_workspace_id

    space = decide_for_kb_scope("space", "ws_42")
    assert space.scope_workspace_id == space.artifact_workspace_id == "ws_42"


def test_reasons_are_valid_and_none_bucket_is_countable() -> None:
    """`ATTRIBUTION_NONE` 是「没有归属输入」的计数口径，必须能被单独数出来。

    ★ 逐档钉死（不只是「在 VALID_REASONS 里」）：`personal` 档**带**空间上下文时是 `space`、
      **不带**时是 `none`——2026-09-21 review 指出「personal + 空间头 ⇒ none」这种改错能存活整个套件。
    """
    assert decide_for_kb_scope("team", "ws_42").reason == ATTRIBUTION_TEAM
    assert decide_for_kb_scope("team", "").reason == ATTRIBUTION_TEAM
    assert decide_for_kb_scope("space", "ws_42").reason == ATTRIBUTION_SPACE
    assert decide_for_kb_scope("workspace", "ws_42").reason == ATTRIBUTION_SPACE
    assert decide_for_kb_scope("personal", "ws_42").reason == ATTRIBUTION_SPACE
    assert decide_for_kb_scope("personal", "").reason == ATTRIBUTION_NONE
    assert decide_for_kb_scope("space", "").reason == ATTRIBUTION_NONE
    for scope, ctx_ws in (("team", ""), ("space", ""), ("personal", "ws_42")):
        assert decide_for_kb_scope(scope, ctx_ws).reason in VALID_REASONS


@pytest.mark.parametrize(
    ("resolved", "via_explicit_space", "expected"),
    [
        (None, False, ATTRIBUTION_NONE),
        ("", False, ATTRIBUTION_NONE),  # Empty string/None both NONE: empty/NULL columns share fail-closed semantics.
        (None, True, ATTRIBUTION_NONE),  # ★ Explicit space-header branch ran but saved no value (intent ≠ result).
        ("", True, ATTRIBUTION_NONE),  # ★ Empty session space ⇒ NULL, even with session/space header.
        ("ws_hdr", True, ATTRIBUTION_SPACE),  # Value from explicit space header.
        ("ws_sess", False, ATTRIBUTION_SESSION),  # Value from session/turn/canvas ladder.
    ],
)
def test_resolved_attribution_measures_outcome_not_intent(resolved: str | None, via_explicit_space: bool, expected: str) -> None:
    """★★ `reason_for_resolved_attribution` 按**落库结果**分类，专治「测意图」这个偏差。

    这个偏差在本模块里**犯过两次**：`files.py` 的 `/upload`（2026-09-19，带了 `session_id` 也可能解析不出）
    与 `files_presign`（2026-09-21，session 分支返回 session 自己的空间且无视请求头）。
    两次的方向都一样：让 `NONE` 被低估，而这恰好会让人以为「推不出归属」很罕见、可以放心翻闸。

    ★ 「值是哪一支给的」由**调用方**传 `via_explicit_space`，本函数不靠「落值是否等于请求头」去猜：
      头与 session 恰好指向同一个空间时，值相等的猜法会把 session 分支记成 `space`。
    """
    assert reason_for_resolved_attribution(resolved, via_explicit_space=via_explicit_space) == expected


def test_resolved_attribution_default_is_the_session_ladder() -> None:
    """`/files/upload` 不接受空间头，不传 `via_explicit_space`：落了值就是梯子给的（`session`）。"""
    assert reason_for_resolved_attribution("ws_x") == ATTRIBUTION_SESSION


class _RecordingMetricsLog:
    """`MetricsLog` 的桩。`gate` 非空时 `track` 会一直阻塞到 gate 被 set——用来证明「不在请求路径上」。"""

    def __init__(self, *, boom: bool = False, gate: asyncio.Event | None = None) -> None:
        self.calls: list[dict] = []
        self._boom = boom
        self._gate = gate

    async def track(self, name: str, *, user_id: str | None = None, attrs: dict | None = None) -> None:
        if self._gate is not None:
            await self._gate.wait()
        if self._boom:
            raise RuntimeError("metrics backend down")
        self.calls.append({"name": name, "user_id": user_id, "attrs": dict(attrs or {})})


def _fake_request(metrics_log: object | None) -> SimpleNamespace:
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(metrics_log=metrics_log)))


async def _emit_and_drain(request: SimpleNamespace, **kwargs) -> None:
    await track_attribution(request, **kwargs)
    await drain_attribution_metrics(request.app)


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "True", "yes", "YES", "on", "On", " on ", " TRUE "])
def test_flag_accepts_the_documented_truthy_spellings(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv(_FLAG, value)
    assert attribution_metrics_enabled() is True


@pytest.mark.parametrize("value", ["0", "false", "FALSE", "no", "off", "OFF", "", " ", "2", "enabled", "maybe", "y"])
def test_flag_treats_everything_else_as_off(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    """★ B 评审逮到：把解析改成 `bool(os.environ.get(...))` 会让 `=0` **开启**打点，而文档写的默认值就是 `=0`。

    之前所有反向用例都只用 `delenv`、正向用例都只用 `"1"`，没有一个传 `"0"` / `"false"` / `" TRUE "`。
    """
    monkeypatch.setenv(_FLAG, value)
    assert attribution_metrics_enabled() is False


def test_flag_unset_is_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(_FLAG, raising=False)
    assert attribution_metrics_enabled() is False


def test_metrics_flag_defaults_off_and_writes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """★ 默认关 = **逐字节今天的行为**：不排任何后台任务、不多一次 DB 写。

    `_emit_and_drain` 之后再断言：任务若被排了，drain 会等它写完，`calls` 就会非空——
    所以这条对照能杀掉「闸被拆掉」，而不是「读得太早所以是 0」。
    """
    monkeypatch.delenv(_FLAG, raising=False)
    sink = _RecordingMetricsLog()
    request = _fake_request(sink)
    asyncio.run(_emit_and_drain(request, route="kb_single_upload", reason=ATTRIBUTION_NONE))
    assert sink.calls == [], "flag 关时**不得**落库"
    assert getattr(request.app.state, "_attribution_metric_tasks", None) is None, "flag 关时连任务集合都不该创建"


def test_metrics_attrs_stay_low_cardinality_and_carry_no_user_identifier(monkeypatch: pytest.MonkeyPatch) -> None:
    """★★ `attrs` 只许放低基数键——rollup 把**全部键值对**排序拼成一个分组键。

    混进 `workspace_id` / `artifact_id` / 文件名这类高基数值，`by_attrs` 会炸成一堆
    只出现一次的键、分布读不出来，还会把 id 泄进管理端。

    ★ 也不落 `user_id`：rollup 不读它，白存 90 天的「按人上传时间戳」没有消费方，
      而 `metrics_event_log` 不随用户删除清理。签名里就没有这个形参，落库列也必须是空。
    """
    monkeypatch.setenv(_FLAG, "1")
    sink = _RecordingMetricsLog()
    asyncio.run(_emit_and_drain(_fake_request(sink), route="files_upload", reason=ATTRIBUTION_SESSION, scope="personal", source="python_sandbox"))

    assert len(sink.calls) == 1
    call = sink.calls[0]
    assert call["name"] == ATTRIBUTION_METRIC_NAME, "name 必须单一——rollup 按 name 精确过滤"
    assert call["user_id"] is None, "不得落任何用户标识"
    assert set(call["attrs"]) <= _ALLOWED_METRIC_ATTRS, f"attrs 混进了高基数键：{sorted(set(call['attrs']) - _ALLOWED_METRIC_ATTRS)}"
    assert call["attrs"] == {"route": "files_upload", "reason": ATTRIBUTION_SESSION, "scope": "personal", "source": "python_sandbox"}
    assert "user_id" not in inspect.signature(track_attribution).parameters


def test_metric_is_scheduled_off_the_request_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """★★★ 这条钉住 2026-09-21 A 评审的 MEDIUM：**写库不能在请求路径上**。

    `MetricsLog` 与产物库是**同一个 sqlite 文件**，内联 `await track()` 遇锁竞争会让上传多等最多 5 秒，
    并且**改变结果**（实测锁被占 7 秒：flag 关 500、flag 开 200）。这里让桩的 `track` **永远阻塞**：
    `await track_attribution(...)` 仍必须立刻返回。若被改回内联 `await`，这条会超时失败。
    """

    async def scenario() -> None:
        gate = asyncio.Event()
        sink = _RecordingMetricsLog(gate=gate)
        request = _fake_request(sink)

        await asyncio.wait_for(track_attribution(request, route="kb_single_upload", reason=ATTRIBUTION_NONE), timeout=0.5)
        assert sink.calls == [], "写库还卡在 gate 上，此时不该已经落了"
        pending = request.app.state._attribution_metric_tasks
        assert len(pending) == 1, "任务必须被留引用（防 GC）"

        gate.set()
        await drain_attribution_metrics(request.app)
        assert len(sink.calls) == 1, "放行后必须真的写出去（正对照：不是任务被丢了）"
        await asyncio.sleep(0)
        assert not pending, "完成的任务必须被 discard，不能泄漏引用"

    monkeypatch.setenv(_FLAG, "1")
    asyncio.run(scenario())


def test_metrics_failure_never_breaks_the_upload(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    """★★ 打点炸了**绝不能**冒泡到上传路径——否则加一条观测反而把功能弄坏了。

    `MetricsLog.track` 自己是 best-effort，但那只覆盖它内部的 DB 失败；
    这里用一个**必抛**的桩证明后台任务体的兜底真的在（且被告警，不是无声吞掉）。
    """
    monkeypatch.setenv(_FLAG, "1")
    with caplog.at_level(logging.WARNING):
        asyncio.run(_emit_and_drain(_fake_request(_RecordingMetricsLog(boom=True)), route="files_upload", reason=ATTRIBUTION_NONE))
    assert [r for r in caplog.records if "metric_track_failed" in r.getMessage()], "失败必须留下告警，不能无声吞掉"


@pytest.mark.parametrize("bad_request", [SimpleNamespace(), SimpleNamespace(app=None), SimpleNamespace(app=SimpleNamespace())])
def test_request_without_app_state_never_raises(monkeypatch: pytest.MonkeyPatch, bad_request: SimpleNamespace) -> None:
    """★ A 评审：`getattr(request.app.state, …)` 曾写在 try **外面**——传进来的若是没有 `.app` 的替身，
    flag 开时抛 `AttributeError`，而 flag 关时同样的输入正常返回。现在整段都在 try 里。
    """
    monkeypatch.setenv(_FLAG, "1")
    asyncio.run(track_attribution(bad_request, route="files_upload", reason=ATTRIBUTION_NONE))


def test_metrics_log_not_wired_is_silent_not_noisy(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    """没装配 `metrics_log` 的部署：**安静返回**，而不是每次上传都刷一条 warning。

    结果上「提前返回」与「靠 try/except 接住 `None.track`」等价（上传都不受影响），
    所以只断言不抛会放过这个差别；差别在**每次上传多一条 warning**（rollup 路由为这种部署留了 503，
    说明它是被设计允许的状态，不是异常）。
    """
    monkeypatch.setenv(_FLAG, "1")
    with caplog.at_level(logging.DEBUG):
        asyncio.run(_emit_and_drain(_fake_request(None), route="files_upload", reason=ATTRIBUTION_NONE))
    assert [r for r in caplog.records if "artifact.attribution" in r.getMessage()] == []


@pytest.mark.parametrize(
    ("route", "reason"),
    [("kb_single_uplaod", ATTRIBUTION_NONE), ("files_upload", "nonee"), ("", ATTRIBUTION_NONE), ("files_upload", "")],
)
def test_unregistered_route_or_reason_is_dropped_loudly(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, route: str, reason: str
) -> None:
    """拼错的 route / reason 会让 rollup 多出一个孤立的桶而没人发现——运行时直接丢弃并告警。"""
    monkeypatch.setenv(_FLAG, "1")
    sink = _RecordingMetricsLog()
    with caplog.at_level(logging.WARNING):
        asyncio.run(_emit_and_drain(_fake_request(sink), route=route, reason=reason))
    assert sink.calls == []
    assert [r for r in caplog.records if "metric_rejected" in r.getMessage()]


def test_legacy_scope_spelling_is_normalised_into_the_space_bucket(monkeypatch: pytest.MonkeyPatch) -> None:
    """★ B 评审：`Form` 路由（如 `POST /kb/files`）收到的是原始拼写 `workspace`，JSON 路由（`/kb/presign`）经
    pydantic 已归一成 `space`——不归一会把同一个 `space` 桶在 rollup 里劈成两个，读数就不对了。
    """
    monkeypatch.setenv(_FLAG, "1")
    sink = _RecordingMetricsLog()
    request = _fake_request(sink)
    for spelling in ("workspace", "space", "personal", "team"):
        asyncio.run(_emit_and_drain(request, route="kb_single_upload", reason=ATTRIBUTION_SPACE, scope=spelling))
    assert [c["attrs"]["scope"] for c in sink.calls] == ["space", "space", "personal", "team"]


@pytest.mark.parametrize("bad_scope", ["ws_42", "", "TEAM", "u_alice", "workspace_admin"])
def test_scope_values_outside_the_known_set_are_dropped_loudly(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, bad_scope: str
) -> None:
    """★ `_ALLOWED_METRIC_ATTRS` 只校验**键**；调用点若把 `scope_id`（`ws_*` / 用户 id）误当 `scope` 传进来，
    会往 rollup 里灌高基数桶——所以再校验**值**，越界的直接丢并告警。
    """
    monkeypatch.setenv(_FLAG, "1")
    sink = _RecordingMetricsLog()
    with caplog.at_level(logging.WARNING):
        asyncio.run(_emit_and_drain(_fake_request(sink), route="kb_single_upload", reason=ATTRIBUTION_SPACE, scope=bad_scope))
    assert sink.calls == []
    assert [r for r in caplog.records if "metric_rejected_scope" in r.getMessage()]


def test_pending_tasks_are_capped_when_the_write_hangs(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    """★ A 评审：写卡住（磁盘 I/O 停顿）时，5000 次调用留下 5000 个任务、各占一个 aiosqlite 线程。
    上限之内照常排队、超出的直接丢并告警；放行后已排队的必须全部写出去（正对照：不是全丢了）。
    """

    async def scenario() -> None:
        gate = asyncio.Event()
        sink = _RecordingMetricsLog(gate=gate)
        request = _fake_request(sink)
        for _ in range(_MAX_PENDING_METRIC_TASKS + 50):
            await track_attribution(request, route="files_upload", reason=ATTRIBUTION_NONE, source="upload")
        assert len(request.app.state._attribution_metric_tasks) == _MAX_PENDING_METRIC_TASKS
        gate.set()
        await drain_attribution_metrics(request.app)
        assert len(sink.calls) == _MAX_PENDING_METRIC_TASKS

    monkeypatch.setenv(_FLAG, "1")
    with caplog.at_level(logging.WARNING):
        asyncio.run(scenario())
    assert [r for r in caplog.records if "metric_dropped_backlog" in r.getMessage()], "超出上限的丢弃必须留告警"


def test_schedule_failure_closes_the_unawaited_coroutine(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    """★ A 评审：`create_task` 失败时协程从未被 await，不关掉会冒 `RuntimeWarning: never awaited`。
    （生产 uvicorn/asyncio 下不可达，只在 trio 或没有运行中的事件循环时可达——但成本一行，且钉住。）

    ★★ **直接断言 `close()` 被调用，不靠「等 GC 发警告再去 catch_warnings」**：首版就是后者，而它**空转**——
    pytest 的日志捕获把带异常对象的 `LogRecord` 留到测试结束（异常 → traceback → 栈帧 → 协程），
    协程活得比我的 `catch_warnings` 块更久，警告发在我看不到的地方，删掉 `coro.close()` 的突变照样全绿。
    """
    closed: list[bool] = []

    class _SpyCoro:
        def close(self) -> None:
            closed.append(True)

    def _boom(*_a, **_k):
        raise RuntimeError("no running loop")

    async def scenario() -> None:
        with monkeypatch.context() as m:
            m.setattr(attribution_module, "_write_metric", lambda *_a, **_k: _SpyCoro())
            m.setattr(asyncio, "create_task", _boom)
            await track_attribution(_fake_request(_RecordingMetricsLog()), route="files_upload", reason=ATTRIBUTION_NONE)

    monkeypatch.setenv(_FLAG, "1")
    with caplog.at_level(logging.WARNING):
        asyncio.run(scenario())
    assert closed == [True], "调度失败时必须关掉没被 await 的协程"
    assert [r for r in caplog.records if "metric_schedule_failed" in r.getMessage()], "失败必须留下告警，不能无声吞掉"


def test_flag_off_is_silent_even_for_an_unregistered_route(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    """flag 关时**先**返回，路由名校验在它之后：关着的部署里即使有人拼错 route，也不该多出一条 warning。"""
    monkeypatch.delenv(_FLAG, raising=False)
    with caplog.at_level(logging.DEBUG):
        asyncio.run(track_attribution(_fake_request(_RecordingMetricsLog()), route="typo_route", reason=ATTRIBUTION_NONE))
    assert [r for r in caplog.records if "artifact.attribution" in r.getMessage()] == []


def test_metric_name_is_the_operator_facing_literal() -> None:
    """★ 事件名是**对外接口**：运维在 rollup 里按这个名字查（读法见灰度清单），文档也这么写。

    其他用例都从实现里 import 这个常量，常量被改时两边一起变、自洽地全绿，
    而运维手里那条查询会**静默返回 0**——所以在这里把字面量钉死。要改名就得同时改文档与本断言。
    """
    assert ATTRIBUTION_METRIC_NAME == "artifact.attribution"


def _routes_used_at_call_sites() -> set[str]:
    """AST 收集 `routes/` 下所有调用点实际用到的 route 名。

    三种形状：`track_attribution(..., route="x")` 的字面量；`_process_zip_bundle` 形参
    `attribution_route` 的**默认值**；以及调用方传的 `attribution_route="y"`。
    """
    routes_dir = Path(__file__).resolve().parents[1] / "src" / "bowhead_gateway" / "routes"
    found: set[str] = set()
    for path in routes_dir.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call):
                fn = node.func
                callee = fn.id if isinstance(fn, ast.Name) else fn.attr if isinstance(fn, ast.Attribute) else ""
                # The shared KB ingest helper forwards its `route` keyword to track_attribution.
                if callee in {"track_attribution", "ingest_kb_file_bytes"}:
                    for kw in node.keywords:
                        if kw.arg == "route" and isinstance(kw.value, ast.Constant):
                            found.add(kw.value.value)
                for kw in node.keywords:
                    if kw.arg == "attribution_route" and isinstance(kw.value, ast.Constant):
                        found.add(kw.value.value)
            elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                args = node.args
                positional = [*args.posonlyargs, *args.args]
                defaults = [None] * (len(positional) - len(args.defaults)) + list(args.defaults)
                for a, d in [*zip(positional, defaults, strict=True), *zip(args.kwonlyargs, args.kw_defaults, strict=True)]:
                    if a.arg == "attribution_route" and isinstance(d, ast.Constant):
                        found.add(d.value)
    return found


def test_call_site_route_names_match_the_registry_exactly() -> None:
    """★ 调用点里出现的 route 名必须**恰好等于** `ATTRIBUTION_ROUTES`。

    - 调用点用了**未注册**的名字 ⇒ 运行时会被静默丢弃（只留一条 warning），打点整段无声消失；
    - 注册了却**没有调用点**用它 ⇒ 死名字，文档里的清单会对不上实际。
    每个调用点各自的接线由路由驱动的行为用例守着；这条守的是「名字表」本身。
    """
    assert _routes_used_at_call_sites() == set(ATTRIBUTION_ROUTES)


#: Seam implementation file: detector **must** flag it (positive control below).
_SEAM_RELPATH = "artifact_attribution.py"


def _team_empty_ternaries(source: str) -> list[int]:
    """用 **AST** 找「拿 scope 跟 `"team"` 比、且有一支是空串」的三元表达式。

    ★ 为什么必须 AST 而不是子串匹配（2026-09-19 review 实测）：文本指纹
    `!= "team" else ""` 会被**至少 6 种自然改写绕过**——反序三元
    （`"" if scope == "team" else …`，**正是本 seam 自己的写法**，也就是未来抄的人
    最可能粘走的形状）、`ruff format` 的折行（line-length 150，深缩进的拷贝一长就换行）、
    if/else 语句形式、常量代替字面量等。
    文本匹配还会**误报**：任何 `X if <任意> != "team" else ""` 都中招，而
    「会哭狼的守卫，下一个人就把它关掉」——`test_artifact_workspace_id_migration.py`
    的 docstring 已经记过这个教训（文本版 8/8 假阳性）。
    """
    found: list[int] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.IfExp) or not isinstance(node.test, ast.Compare):
            continue
        test = node.test
        if len(test.ops) != 1 or not isinstance(test.ops[0], ast.Eq | ast.NotEq):
            continue
        operands = [test.left, *test.comparators]
        if not any(isinstance(o, ast.Constant) and o.value == "team" for o in operands):
            continue
        if any(isinstance(b, ast.Constant) and b.value == "" for b in (node.body, node.orelse)):
            found.append(node.lineno)
    return found


def test_detector_actually_fires_on_the_seam_itself() -> None:
    """★ 正对照：检测器必须在 seam 自己身上命中。

    比 `scanned > N` 强得多——它证明的不是「读到了文件」，而是
    「**这个形状真的会被抓到**」。没有它，下面那条「零 offender」可能只是检测器坏了。
    """
    seam = Path(__file__).resolve().parents[1] / "src" / "bowhead_gateway" / _SEAM_RELPATH
    assert _team_empty_ternaries(seam.read_text(encoding="utf-8")), "检测器在 seam 自身都不命中 ⇒ 判据坏了"


def test_no_seventh_handwritten_copy() -> None:
    """第 7 份手抄 = 架构错。

    ⚠ 扫描范围是整个 `src/bowhead_gateway/`（递归），不只 `routes/`：review 指出
    `kb_promotion.py` 这类 service 模块才是第 7 份最可能的落点，而它不在 `routes/` 下。
    """
    gateway_src = Path(__file__).resolve().parents[1] / "src" / "bowhead_gateway"
    offenders: list[str] = []
    for path in gateway_src.rglob("*.py"):
        if path.name == _SEAM_RELPATH:
            continue  # Sole implementation naturally contains this shape.
        for lineno in _team_empty_ternaries(path.read_text(encoding="utf-8")):
            offenders.append(f"{path.relative_to(gateway_src)}:{lineno}")

    assert not offenders, f"出现手抄的归属决策：{offenders}（改用 artifact_attribution.decide_for_kb_scope）"
