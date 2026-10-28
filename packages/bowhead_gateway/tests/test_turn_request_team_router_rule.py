"""批 2-a C7b —— 两条结构层守卫（tech design §5.4）.

**规则 A（C2 渠道）**：`bowhead_gateway/src/**` 里构造的 `TurnRequest`，在流向
`run_turn`/`spawn_turn` 之前必须被赋过一个**非 None** 的 `team_router`。

判据为什么必须带「非 None」：`turn_runner._effective_router` 是
`getattr(request, "team_router", None) or getattr(orch, "router", None)` —— 传
`None` 与压根不传是**同一个**后果（回退进程级装配占位）。只判「这个 kwarg 被赋过」
的规则会被 `team_router=None` 满足，而那正是要堵的逃逸口：设计初稿一度把它写成
站点 8 的推荐做法，`clarifications.py` / `turn_service.py` 也早就是这个形状。

**这条规则抓不到什么**（写清楚，免得下一个人把它当全能闸）：它是静态的，只认
字面 `None` 与「压根没给」。一个**运行期**才变成 None 的变量（`resolve_*` 返回
None 那类）静态上看是个 `Name`，规则放行。所以它必须与另外两层配合：站点 10/11
的 None 分支做了运行期日志 + 计数，站点 3 有断言 `team_router is not None` 的契约
测试。**真正兜住所有情况的是装配 router 自报身份（§3.2）——运行期一定响。**

**boot 断言**：`app.state.router` 已删除（C7b）。断言必须挂在**进过 lifespan** 的
boot 上——两处赋值原本都在 `lifespan()` 内，而 gateway 集成测试默认 bypass
lifespan（见 `tests/conftest.py`），挂在 `create_app()` 冒烟里是恒绿的真空断言。
"""

from __future__ import annotations

import ast
import pathlib

import pytest

_SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "bowhead_gateway"

# ── Named allowlist ────────────────────────────────────────────────────────
# Every entry needs a reason. **Allowlist records design decisions, not escape hatches**; explain
# why each path needs no team router, or it is a defect, not an exemption.
_ALLOWED_BY_FILE: dict[str, str] = {
    # dev-only body-id routes (`_dev_enabled` at plans.py:62-65 requires AUTH_MODE=dev),
    # unreachable in production; team_id comes from body, deliberately untrusted here. Real user entry uses
    # chat.py in-turn hook1. Team router wiring awaits M1 authenticated approve endpoint.
    "routes/plans.py": "dev-only body-id routes; prod unreachable (see plans.py:62-65)",
    # Best-effort turn continuation: parent passed 409 gate; resolution failure **deliberately** degrades without interrupting
    # clarification resume. None branch explicit (logger.error + bowhead_team_router_miss_total).
    # Proper fix (resume also resolves team router and clearly informs users) remains chat-side debt.
    "routes/clarifications.py": "best-effort clarification resume; None branch is logged + counted",
    "turn_service.py": "shared resume builder; attribution is the caller's duty (see clarifications.py)",
}

# ── Site registration (finer than whole-file exemption) ─────────────────────
# Rule guards **executed** TurnRequests requiring team routers, but a third valid shape exists:
# reconstruction **used only for digest calculation, never executed** (`web_start_execution_intent` → digest,
# for replay conflict detection; function never reads team_router).
#
# Such sites must not use `_ALLOWED_BY_FILE`: chat.py is most likely to gain **real dispatch sites**,
# so whole-file exemption opens a gap where guarding matters most (this file's `_enclosing_function` comments
# already hit this for the same reason). Register at **that line**: above construction write
#
#     # team-router-rule: <reason>
#
# Empty reason counts as absent. Registration still records design decisions, never an escape hatch.
_SITE_MARKER = "# team-router-rule:"


def _site_exemption_reason(path: pathlib.Path, call_lineno: int) -> str | None:
    lines = path.read_text(encoding="utf-8").splitlines()
    # Recognize only the comment block immediately above construction, so markers elsewhere cannot exempt another site.
    index = call_lineno - 2
    while index >= 0:
        stripped = lines[index].strip()
        if not stripped.startswith("#"):
            return None
        if stripped.startswith(_SITE_MARKER):
            return stripped[len(_SITE_MARKER) :].strip() or None
        index -= 1
    return None


def _iter_turn_request_calls():
    for path in sorted(_SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.id if isinstance(func, ast.Name) else (func.attr if isinstance(func, ast.Attribute) else None)
            if name in {"TurnRequest", "_TurnRequest"}:
                yield path, node


def _kwarg(node: ast.Call, key: str) -> ast.expr | None:
    for kw in node.keywords:
        if kw.arg == key:
            return kw.value
    return None


def _enclosing_function(tree: ast.AST, lineno: int) -> ast.AST | None:
    """包住该行的最内层函数。规则必须按**函数作用域**判，不是按文件。

    实现层 review 实测：按文件判时，同一处泄漏放在 `chat.py:1037` 之前全绿、放到
    文件尾才变红——因为 `_dc_replace(turn_req, **_byo_overrides)` 在文件里"存在"
    就被当成补齐了。而 chat.py / feishu/bridge.py 恰恰是最可能新增派发点的两个
    文件，等于洞正好开在最需要守的地方。
    """
    best: ast.AST | None = None
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        if node.lineno <= lineno <= (node.end_lineno or node.lineno):
            if best is None or node.lineno > best.lineno:  # type: ignore[attr-defined]
                best = node
    return best


def _assigns_team_router_later(tree: ast.AST, call_lineno: int) -> bool:
    """构造点之后、**同一函数内**是否有 `replace(..., team_router=<非 None>)` 补齐。

    chat.py 与 feishu/bridge.py 都是「裸构造 + 事后 replace」的正确形态。
    """
    scope = _enclosing_function(tree, call_lineno)
    if scope is None:
        return False
    scope_end = scope.end_lineno or call_lineno  # type: ignore[attr-defined]
    saw_explicit_team_router = False
    candidates: list[ast.Call] = []
    for node in ast.walk(scope):
        if not isinstance(node, ast.Call) or not (call_lineno < node.lineno <= scope_end):
            continue
        fname = node.func.id if isinstance(node.func, ast.Name) else (node.func.attr if isinstance(node.func, ast.Attribute) else "")
        if fname in {"replace", "_dc_replace"}:
            candidates.append(node)
    # Check explicit non-None team_router assignment within the same function (including collection forms such as
    # `d["team_router"] = x` at chat.py:973), supporting the permissive **kwargs case.
    for node in ast.walk(scope):
        if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant) and node.slice.value == "team_router":
            saw_explicit_team_router = True
        if isinstance(node, ast.keyword) and node.arg == "team_router" and not _is_literal_none(node.value):
            saw_explicit_team_router = True
    for node in candidates:
        value = _kwarg(node, "team_router")
        if value is not None and not _is_literal_none(value):
            return True
        # `**overrides`: cannot statically determine value, but **require explicit team_router assignment
        # within the same function** (original comments required it but code did not; implementation review caught it).
        if any(kw.arg is None for kw in node.keywords) and saw_explicit_team_router:
            return True
    return False


def _is_literal_none(node: ast.expr) -> bool:
    return isinstance(node, ast.Constant) and node.value is None


def test_turn_request_always_carries_a_real_team_router() -> None:
    offenders: list[str] = []
    for path, node in _iter_turn_request_calls():
        rel = path.relative_to(_SRC).as_posix()
        if rel in _ALLOWED_BY_FILE:
            continue
        value = _kwarg(node, "team_router")
        if value is not None and not _is_literal_none(value):
            continue
        if _site_exemption_reason(path, node.lineno):
            continue  # Site registration: digest-only reconstruction, never executed.
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        if value is None and _assigns_team_router_later(tree, node.lineno):
            continue  # Bare construction + later replace (correct chat/Feishu shape).
        why = "team_router=None（与不传同后果：回退装配占位）" if value is not None else "未赋 team_router 且事后无 replace 补齐"
        offenders.append(f"{rel}:{node.lineno} — {why}")

    assert not offenders, (
        "以下 TurnRequest 会回退到进程级装配 router：\n  "
        + "\n  ".join(offenders)
        + (
            "\n\n如果确属有意：整条路径不需要团队 router → 加进 _ALLOWED_BY_FILE；"
            "只是「算摘要不执行」的重建 → 在构造点上方写 `# team-router-rule: <理由>`。"
            "两者都是设计决策的登记处，不是逃生口。"
        )
    )


def test_site_exemption_requires_an_adjacent_marker_with_a_reason(tmp_path) -> None:
    """★ 正对照：站点级登记不得变成万能后门。

    只放行「紧邻构造点上方、且写了理由」的标记：没写理由不算、隔着代码行不算、
    文件里别处出现的标记也不算——否则一个标记会顺手放行整份文件里所有站点，
    等于把 _ALLOWED_BY_FILE 的粗粒度问题又原样搬回来。
    """
    marked = tmp_path / "marked.py"
    marked.write_text(
        "# team-router-rule: digest only\nTurnRequest(session_id='s')\n",
        encoding="utf-8",
    )
    assert _site_exemption_reason(marked, 2) == "digest only"

    empty_reason = tmp_path / "empty.py"
    empty_reason.write_text(
        "# team-router-rule:\nTurnRequest(session_id='s')\n",
        encoding="utf-8",
    )
    assert _site_exemption_reason(empty_reason, 2) is None, "理由为空视同没写"

    detached = tmp_path / "detached.py"
    detached.write_text(
        "# team-router-rule: digest only\nx = 1\nTurnRequest(session_id='s')\n",
        encoding="utf-8",
    )
    assert _site_exemption_reason(detached, 3) is None, "隔着代码行的标记不得放行"

    unmarked = tmp_path / "plain.py"
    unmarked.write_text("TurnRequest(session_id='s')\n", encoding="utf-8")
    assert _site_exemption_reason(unmarked, 1) is None


@pytest.mark.integration
async def test_lifespan_boot_does_not_expose_assembly_router(tmp_path, monkeypatch) -> None:
    """批 2-a C7b：`app.state.router` 必须**不存在**。

    ★ 必须进 lifespan：两处赋值原本都在 `lifespan()` 里，`create_app()` 单独调用时
    `app.state` 根本没有 `router` 字段——挂在 create_app 冒烟里删不删都绿（真空断言）。
    把 `app.state.router = router` 加回 main.py 必须让本条红。
    """
    monkeypatch.chdir(tmp_path)
    # 2026-08-07: `1c9a2a3fe fix(chat): 收口 Composer 恢复与澄清失效协议` added a hard startup dependency:
    # without core migration 0017, `RuntimeError: clarification invalidation schema is
    # unavailable`. Empty-DB boot no longer works; test degraded from guarding an invariant to crashing at boot.
    # ⚠ `upgrade()` uses only the URL **directory**, with filename always `TARGET_FILES["core"]`; other filenames
    # succeed but migrate a newly created DB in that directory (previously boot.db here).
    from bowhead_cli.migrate import TARGET_FILES, upgrade

    core_path = tmp_path / TARGET_FILES["core"]
    assert upgrade(f"sqlite+aiosqlite:///{core_path}") == tuple(TARGET_FILES), "迁移未跑到 head"

    monkeypatch.setenv("BOWHEAD_DB_URL", f"sqlite+aiosqlite:///{core_path}")
    monkeypatch.setenv("BOWHEAD_DEPLOYMENT_MODE", "saas")
    # lifespan derives artifact-fetch key from BOWHEAD_JWT_SECRET, rejecting startup without it; no host env/.env dependency.
    monkeypatch.setenv("BOWHEAD_JWT_SECRET", "test-team-router-rule-jwt-secret-32bytes")
    monkeypatch.setenv("BOWHEAD_SANDBOX_BACKEND", "subprocess")

    from bowhead_gateway.main import create_app

    app = create_app()
    async with app.router.lifespan_context(app):
        assert not hasattr(app.state, "router"), (
            "进程级装配 router 不得挂在 app.state 上——它是 C1 渠道的入口。per-route LLM 调用一律走 resolve_team_router / team_router_or_409。"
        )
        # Positive control: verify this is an actually assembled app (otherwise assertions above might pass merely because
        # lifespan never finished).
        assert getattr(app.state, "orchestrator", None) is not None
