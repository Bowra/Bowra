"""批 0 —— 工具改名的读路径规范化机制（`docs/design/工具改名迁移.md`）。

本批**没有改任何工具的名字**：`TOOL_RENAMES` 是空的 ⇒ 线上行为逐字不变。
所以这里的用例分两类：

1. **空表恒等**：证明「装了机制但没启用」时一切照旧（`TestEmptyMapIsIdentity`）；
2. **注入一条假改名**，逐个收口证明它真的被接上了（`TestChokepoints`）。
   ⚠ 每条都是「摘掉那处接线就转红」的形状——因为空表下所有断言都恒真，
   不注入的话这些用例是**死的**（[[feedback_assert_absence_needs_positive_control]]）。
"""

from __future__ import annotations

import datetime as dt
import re

import pytest
from bowhead_core.agents._generated.tool_names import ALL_TOOL_NAMES
from bowhead_core.agents._generated.tool_renames import (
    TOOL_RENAMES,
    canonical_tool_name,
)

pytestmark = pytest.mark.unit

_OLD = "zzz_legacy_probe_tool"
_NEW = "bi_query"  # Use a **real existing** tool to exercise dispatch/catalog/role paths.


@pytest.fixture
def injected_rename():
    """临时往改名表里塞一条。空表下所有断言恒真 ⇒ 没有它这些用例是死的。"""
    TOOL_RENAMES[_OLD] = _NEW
    try:
        yield _OLD, _NEW
    finally:
        TOOL_RENAMES.pop(_OLD, None)


class TestEmptyMapIsIdentity:
    """批 0 的核心承诺：没登记任何改名 ⇒ 一个字节都不变。"""

    #: Approved rename registry. Each addition must **also** update this, exposing unnoticed incidental renames.
    _DECLARED = {
        "bi_ask_question": "bi_query",
        "canvas_add_chart": "add_chart",
        "canvas_add_image": "add_image",
    }

    def test_shipped_map_matches_the_declared_set(self) -> None:
        assert TOOL_RENAMES == self._DECLARED, (
            "改名表与已拍板清单不符：新增改名必须同时更新本用例，并按 docs/design/工具改名迁移.md 走完七个收口 + 提示词同批"
        )

    @pytest.mark.parametrize("name", sorted(ALL_TOOL_NAMES - set(TOOL_RENAMES)))
    def test_every_live_name_maps_to_itself(self, name: str) -> None:
        """没被改名的工具一律恒等——改名只影响登记过的那几个。"""
        assert canonical_tool_name(name) == name

    def test_unknown_name_passes_through(self) -> None:
        assert canonical_tool_name("teammcp.ots_x.get") == "teammcp.ots_x.get"
        assert canonical_tool_name("") == ""


class TestCodegenValidation:
    """改名声明本身的守卫——写错了必须在 codegen 就炸，不能漏到运行时。"""

    @staticmethod
    def _collect(tools):
        from bowhead_cli.codegen_tool_aliases import collect_renames

        return collect_renames(tools)

    def test_rename_from_a_live_canonical_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="不得是活工具"):
            self._collect(
                [
                    {"canonical": "add_chart", "renamed_from": ["bi_query"]},
                    {"canonical": "bi_query"},
                ],
            )

    def test_rename_also_declared_as_legacy_alias_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="两种语义只能选一个"):
            self._collect(
                [
                    {"canonical": "add_chart", "renamed_from": ["old_x"]},
                    {"canonical": "bi_query", "legacy_aliases": ["old_x"]},
                ],
            )

    def test_two_tools_claiming_the_same_old_name_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="被两个工具认领"):
            self._collect(
                [
                    {"canonical": "add_chart", "renamed_from": ["old_x"]},
                    {"canonical": "add_image", "renamed_from": ["old_x"]},
                ],
            )

    def test_old_name_is_reserved_but_gets_no_role_binding(self) -> None:
        """旧名必须进 ALL_TOOL_NAMES（user skill 不许撞名），但**不进** role binding。

        进了 role binding 就会漏进 `NATIVE_ALLOWLIST` ⇒ 工具选择器里多一行
        「勾了等于没勾」的幽灵（见规范 §4 的 bi_ask_question 实例）。
        """
        from bowhead_cli.codegen_tool_aliases import (
            render_role_bindings_py,
            render_tool_names_py,
        )

        tools = [
            {"canonical": "bi_query", "allowed_roles": ["analyst"], "renamed_from": ["old_x"]},
        ]
        rendered = render_tool_names_py(tools)
        # RESERVED literals follow the last marker occurrence.
        all_block, _, reserved_block = rendered.partition("RESERVED_TOOL_NAMES")
        # Old names are **not** tools: reserved, not active, or guards requiring explicit kind for each real tool
        # demand metadata for nonexistent names (observed in batch 1).
        assert "old_x" not in all_block
        assert '"old_x",' in reserved_block
        assert "old_x" not in render_role_bindings_py(tools)


class TestChokepoints:
    """七个收口逐个验。少接一个，旧名就从那条缝里漏出去。"""

    def test_c1_agent_loop_renames_in_place(self, injected_rename) -> None:
        from bowhead_core.tool_name_canon import canonicalize_tool_calls

        class _TC:
            def __init__(self, name: str) -> None:
                self.name = name

        calls = [_TC(_OLD), _TC("bi_schema")]
        canonicalize_tool_calls(calls, source="unit")
        # Canonicalize **in place**: message history / event payload / ToolInvocation read names from the same object.
        assert [c.name for c in calls] == [_NEW, "bi_schema"]

    def test_c1_records_deprecation_telemetry(self, injected_rename) -> None:
        from bowhead_core.metrics import counter_items
        from bowhead_core.tool_name_canon import canonicalize_tool_calls

        class _TC:
            def __init__(self, name: str) -> None:
                self.name = name

        canonicalize_tool_calls([_TC(_OLD)], source="unit_probe")
        hits = [
            labels
            for labels, _v in counter_items("bowhead_tool_name_rename_total")
            if labels.get("old") == _OLD and labels.get("source") == "unit_probe"
        ]
        assert hits, "没有遥测就永远不知道旧名还在不在被真的调"

    def test_c2_tool_refs_canonicalize_and_dedupe(self, injected_rename) -> None:
        from bowhead_core.tool_name_canon import canonicalize_tool_refs

        # Both old + new selected ⇒ merge; preserve teammcp.* unchanged and retain order.
        assert canonicalize_tool_refs(
            [_OLD, "teammcp.a.b", _NEW, "", None],
            source="unit",
        ) == [_NEW, "teammcp.a.b"]

    def test_c3_validate_tool_refs_accepts_the_old_name(self, injected_rename) -> None:
        """判据 1c：`tool_refs_json` 里存旧名的自建 agent 必须仍校验通过。"""
        from bowhead_core.subagent_authoring_catalog import AuthoringTool
        from bowhead_core.team_subagent_validation import (
            SubagentAuthoringCatalogSnapshot,
            validate_tool_refs,
        )

        tool = AuthoringTool(
            stable_ref=_NEW,
            display_name=_NEW,
            description="",
            source_kind="native",
            source_display_name="Bowhead",
            capability_classes=frozenset(),
            risk="read",
            discoverable=True,
            selectable=True,
            locked_reason=None,
            risk_source="registry",
            approval_required=False,
            confirmation_summary=None,
            dependency_revision="v1",
        )
        snap = SubagentAuthoringCatalogSnapshot(
            team_id="t",
            actor_user_id="u",
            native_tools=(tool,),
            team_tools=(),
            fingerprint="f",
            dependency_guard=(),
            built_at=dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
        )
        # Catalog has **only new name** (criterion 1d), submitted name old: must allow.
        validate_tool_refs([_OLD], catalog=snap, require_selectable=True)

    def test_c4_display_lookups_resolve_the_old_name(self, injected_rename) -> None:
        from bowhead_core.subagent import _tool_header_zh
        from bowhead_core.tool_category import lookup_kind

        assert lookup_kind(_OLD) == lookup_kind(_NEW)
        assert _tool_header_zh(_OLD) == _tool_header_zh(_NEW)

    def test_c4_static_finding_resolves_the_old_name(self) -> None:
        """靶子必须是**真在** `_STATIC_FINDING_TEMPLATES` 里的工具。

        ⚠ 初版拿 `bi_query` 当靶子——它不在模板表里，两边都返 `None` 而「相等」，
        摘掉接线照样绿（突变实测抓到）。相等断言必须先保证不是「两边都空」。
        """
        from bowhead_core.tool_finding import static_finding_for

        target = "bi_schema"  # In template table.
        TOOL_RENAMES[_OLD] = target
        try:
            result = {"measures": [1], "dimensions": [2, 3]}
            got = static_finding_for(_OLD, {}, result)
            want = static_finding_for(target, {}, result)
        finally:
            TOOL_RENAMES.pop(_OLD, None)
        assert want, "靶子选错了：它不在 _STATIC_FINDING_TEMPLATES 里，这条用例会恒真"
        assert got == want

    async def test_c5_registry_execute_dispatches_the_old_name(self) -> None:
        """兜底收口：绕过 C1 的调用方（后端自调 / 新 loop / 测试）也要能分发。

        靶子刻意用一个**中性**名字而不是 `bi_query`：后者带 datarc 前置闸，
        没连数据源时 execute 会在 handler 之前就返 DatarcUnavailableError
        —— 那会把「改名没接上」和「工具自身的策略闸」混成同一种红。
        """
        from bowhead_core.tool_registry import (
            ToolExecutionContext,
            ToolRegistry,
            ToolSchema,
            ToolSpec,
        )

        target = "zzz_probe_target_tool"
        TOOL_RENAMES[_OLD] = target
        try:
            reg = ToolRegistry()
            seen: list[str] = []

            async def _handler(args, ctx):  # noqa: ANN001, ARG001
                seen.append("called")
                return {"ok": True}

            reg.register(
                ToolSpec(
                    schema=ToolSchema(
                        name=target,
                        description="probe",
                        input_schema={"type": "object"},
                    ),
                    handler=_handler,
                ),
            )
            ctx = ToolExecutionContext(
                workspace_id="ws_x",
                member_uid="m",
                user_id="u",
                turn_id="t1",
                datarc=None,
            )
            inv = await reg.execute(_OLD, {}, ctx)
        finally:
            TOOL_RENAMES.pop(_OLD, None)

        assert inv.error is None, inv.error
        assert seen == ["called"], "旧名没能分发到现名的 handler"
        # Must record the **current name**, or downstream name-based sets must each support both names.
        assert inv.tool_name == target

    def test_c6_derive_capabilities_resolves_the_old_name(self, injected_rename) -> None:
        """planner 侧的第二条 tool_refs 读路径。漏了这条是**静默**的能力丢失。"""
        from bowhead_core.agents.capabilities import derive_capabilities

        assert derive_capabilities([_OLD]) == derive_capabilities([_NEW])
        assert derive_capabilities([_OLD]), "靶子工具应当派生出至少一个能力标签"


class TestChokepointDriftGuard:
    """新增一条 agent loop 而忘了接 C1 ⇒ 旧名从那条缝里漏出去，且**不会有任何报错**。

    单测覆盖不了「有没有人新开了一条 loop」，只能扫源码。
    ([[feedback_one_concept_many_implementations]]：改一处=没改，先数清)
    """

    #: Loops consuming `resp.tool_calls` and **deciding by name** must each call C1.
    _LOOPS_REQUIRING_C1 = {
        "subagent.py",  # Shared by Lead + all peers.
        "report_writer_loop.py",  # Own handler table; bypasses ToolRegistry.execute.
    }
    #: Consumers of `.tool_calls` **without name-based decisions** are exempt (explain; no silent exemptions).
    _EXEMPT = {
        # Checks only tool_call presence and rejects all in JSON mode; does not inspect names.
        "home_proposals.py": "只判存在性即 fallback，无名字键控",
        # Context compression summary prints `· called {tc.name}` for display only.
        "turn_runner.py": "压缩摘要展示，不参与判定",
        # Salvages ToolCall from body; caller (subagent.py) then passes output through C1.
        "text_tool_call_salvage.py": "产出物由调用方过 C1",
        # 2026-09-06: `tool_calls` here is an **evaluation record accumulator**; we append
        # each call's {tool_name, args_hash, effect_class, ...} for scorer export,
        # not a model-issued call list. Names come from `registry.execute(name, ...)` arguments;
        # agent loop C1 already canonicalized `tc.name` in place, so they are canonical here.
        # Same name, different meaning: the guard's textual criterion collided.
        "agent_eval_effects.py": "评测记录累加器，名字来自 execute() 入参、上游已过 C1",
        # 2026-10: the eval service reads that same `EvalTraceCollector.tool_calls` record to check
        # whether the case's required Tools ran; the names are already canonical.
        "agent_eval_service.py": "reads the eval trace record above; names come from execute() after C1",
        # 2026-09-14 4cccae211 / 09-25 f732098af: continuation checkpoint **serialization and validation**. Transcript
        # comes from subagent.py loop (already C1-checked); only compare saved and expected tool names verbatim here.
        # Any mismatch rejects continuation (fail-closed); no path executes an old name as another tool.
        "bi_workspace_continuation.py": "续跑检查点校验，transcript 来自已过 C1 的 agent 循环，名字不符即拒绝",
        "reference_read_question.py": "续跑检查点校验，transcript 来自已过 C1 的 agent 循环，名字不符即拒绝",
        # 2026-10 794e26462 / b2a755598: physical-call settlement receipts pair tool results with
        # their calls by call id only; the name is copied into the receipt as recorded data and
        # never selects behavior. The messages come from the agent loop, already canonicalized by C1.
        "physical_usage.py": "settlement receipts match calls by id; the name is recorded, not used to decide",
        # 2026-10-05 fabf9b1f0: ordinary Lead clarification continuation checkpoint capture and
        # validation. Messages come from the subagent.py loop (already canonicalized by C1); the
        # helper spawn is matched by call id and exact arguments against the recorded invocation,
        # and any mismatch raises OrdinaryContinuationUnavailable (fail-closed). No path executes
        # a call from here.
        "ordinary_lead_checkpoint.py": "continuation checkpoint validation; transcript comes from the C1-checked loop, mismatches reject",
        "ordinary_lead_runtime.py": "continuation checkpoint capture; transcript comes from the C1-checked loop, mismatches reject",
    }
    #: 2026-10-13 7f2a04a53: the team daily usage report reads and upserts a `tool_calls` count
    #: column in SQL (`o.tool_calls`, `excluded.tool_calls`). These exact SQL qualifiers name a number,
    #: not a model-issued call list, so only they are ignored; any other `.tool_calls` in the same
    #: files (team_operations_report.py, session_store.py) still trips the guard.
    _SQL_COUNT_COLUMN = re.compile(r"\b(?:o|excluded)\.tool_calls\b")

    def _consumer_text(self, text: str) -> str:
        return self._SQL_COUNT_COLUMN.sub("", text)

    def test_sql_count_column_is_ignored_but_a_real_consumer_is_not(self) -> None:
        assert ".tool_calls" not in self._consumer_text('"tool_calls=MAX(tool_calls,excluded.tool_calls)" "SUM(o.tool_calls)"')
        assert ".tool_calls" in self._consumer_text("for tc in resp.tool_calls: ...  # excluded.tool_calls")

    def test_every_tool_calls_consumer_is_wired_or_exempt(self) -> None:
        import pathlib

        src = pathlib.Path(__file__).resolve().parents[1] / "src" / "bowhead_core"
        offenders: list[str] = []
        for path in src.rglob("*.py"):
            text = self._consumer_text(path.read_text(encoding="utf-8"))
            if ".tool_calls" not in text:
                continue
            name = path.name
            if name in self._EXEMPT:
                continue
            if name not in self._LOOPS_REQUIRING_C1:
                offenders.append(f"{name}: 新的 .tool_calls 消费方，需接 C1 或登记豁免理由")
                continue
            if "canonicalize_tool_calls(" not in text:
                offenders.append(f"{name}: 消费 tool_calls 但没调 canonicalize_tool_calls（C1 掉了）")
        assert not offenders, "工具改名收口漂移：\n" + "\n".join(offenders)


@pytest.mark.asyncio
async def test_c2_team_profile_canonicalizes_stored_tool_refs(injected_rename, monkeypatch) -> None:
    """C2 收口：客户库里存的旧名，在 profile 解析时归一成现名。

    不归一的话旧名会进 `tool_whitelist`，然后 `to_schemas` 因
    `n in self._specs` 为假而**静默丢弃** ⇒ 创建者以为选了 N 个工具，
    agent 实际少一个，而且不报错。
    """
    from bowhead_core.agents import plan_admission
    from bowhead_core.subagent import SubagentRunner
    from bowhead_core.tool_registry import ToolRegistry
    from bowhead_core.user_store import TeamSubagent

    async def _admitted(*_args, **_kwargs) -> bool:
        return True

    # The fake store has no assignment database, which fails closed since 8e6f85c9a; this case
    # covers stored tool-name canonicalization after admission.
    monkeypatch.setattr(plan_admission, "team_agent_runtime_allowed", _admitted)

    class _FakeTeamStore:
        async def get_team_subagent_by_name(self, team_id, name, only_enabled=True):  # noqa: ANN001, ARG002, FBT002
            return TeamSubagent(
                id="sa_1",
                team_id=team_id,
                name=name,
                system_prompt="probe",
                tool_refs=[_OLD],
            )

    runner = SubagentRunner(
        router=None,
        tool_registry=ToolRegistry(),  # type: ignore[arg-type]
        team_subagent_store=_FakeTeamStore(),  # type: ignore[arg-type]
    )
    profile = await runner._resolve_profile("probe", user_id="u1", team_id="team_1")
    assert profile is not None
    assert _NEW in profile.tool_whitelist, "旧名没被归一 ⇒ 白名单里是个没注册的名字"
    assert _OLD not in profile.tool_whitelist


class TestBiAskQuestionIsAPureRename:
    """批 0b —— `bi_ask_question` 是**第一个真实使用者**（也是 R1 那个 live bug 的载体）。

    它的 handler 早在 P13.5 随死码清理删了 ⇒ 它不是「双注册旧名」，是**纯改名**。
    留在 `legacy_aliases` 会经 role binding 漏进 `NATIVE_ALLOWLIST`，
    成为工具选择器里一行「勾了等于没勾」的幽灵。
    """

    def test_it_is_declared_as_a_rename_not_a_legacy_alias(self) -> None:
        from bowhead_core.agents._generated.legacy_trace_names import (
            LEGACY_TRACE_NAMES,
        )

        assert TOOL_RENAMES.get("bi_ask_question") == "bi_query"
        assert "bi_ask_question" not in LEGACY_TRACE_NAMES

    def test_the_ghost_row_is_gone_from_the_tool_picker(self) -> None:
        """★ R1 的修复点。摘掉 `renamed_from` 它就会重新冒出来。"""
        from bowhead_core.subagent_authoring_catalog import NATIVE_ALLOWLIST

        assert "bi_ask_question" not in NATIVE_ALLOWLIST

    def test_the_name_stays_reserved_against_user_skills(self) -> None:
        """旧名让出去 → user skill 可以叫这个名字，而历史 trace 里分不出来。

        它进的是 **RESERVED**（保留名）不是 ALL（现役工具名）：后者混进旧名会让
        「每个真工具都要显式选 kind」这类守卫对着一个不存在的名字要元数据。
        """
        from bowhead_core.agents._generated.tool_names import RESERVED_TOOL_NAMES

        assert "bi_ask_question" in RESERVED_TOOL_NAMES
        assert "bi_ask_question" not in ALL_TOOL_NAMES

    def test_it_gets_no_role_binding(self) -> None:
        from bowhead_core.agents._generated.role_bindings import (
            _PARADIGM_V2_ROLE_MAP,
        )

        assert "bi_ask_question" not in _PARADIGM_V2_ROLE_MAP

    def test_stored_tool_refs_still_resolve(self) -> None:
        """116 实测：`sales-marketing`（enabled）的 tool_refs 里就存着它。

        它同时还勾了 `bi_query` ⇒ 归一后两条合并成一条，**实际能力一个没变**。
        """
        from bowhead_core.tool_name_canon import canonicalize_tool_refs

        stored = ["bi_query", "bi_concept_resolve", "bi_ask_question", "bi_schema"]
        assert canonicalize_tool_refs(stored, source="unit") == [
            "bi_query",
            "bi_concept_resolve",
            "bi_schema",
        ]

    async def test_historical_bi_tool_name_resolves_to_current_handler(self) -> None:
        """Historical BI tool names still resolve through the current registry."""
        from bowhead_core.tool_registry import (
            ToolExecutionContext,
            ToolRegistry,
            ToolSchema,
            ToolSpec,
        )

        reg = ToolRegistry()
        seen: list[str] = []

        async def _handler(args, ctx):  # noqa: ANN001, ARG001
            seen.append("called")
            return {"type": "metric", "metrics": [{"name": "GMV", "value": 1}]}

        reg.register(
            ToolSpec(
                schema=ToolSchema(
                    name="bi_query",
                    description="probe",
                    input_schema={"type": "object"},
                ),
                handler=_handler,
            ),
        )
        ctx = ToolExecutionContext(
            workspace_id="ws_x",
            member_uid="m",
            user_id="u",
            turn_id="t1",
            datarc=None,
        )
        inv = await reg.execute("bi_ask_question", {"text": "GMV"}, ctx)
        # ★ Key evidence: resolves to current name. Before the change, this was
        # `KeyError: Tool 'bi_ask_question' not registered` (swallowed by except into silent degradation).
        assert inv.tool_name == "bi_query"
        assert "not registered" not in (inv.error or "")
        # ⚠ Do **not** assert handler invocation here: `bi_*` also has a Datarc precondition gate,
        # returning DatarcUnavailableError before the handler if no data source is connected. That is tool policy,
        # unrelated to renaming. Mixing them conflates two failures (C5 hit this; deliberately switched to a neutral target).
        assert seen == [] or seen == ["called"]

    def test_historical_preview_shapes_still_render(self) -> None:
        """★ 归一把老 trace 从 `bi_ask_question` 分支挪到了 `bi_query` 分支。

        必须确认新分支**覆盖**老形状——`build_preview_for_tool` 是**按形状分支**、
        不是查表，这类地方归一前一定要先验，否则历史轮的 preview 会静默变空。
        （实测：`type=metric` 两边是逐字相同的构造；`rows` 侧 bi_query 是超集。）
        """
        from bowhead_core.tool_result_preview import build_preview_for_tool

        metric = {"type": "metric", "metrics": [{"name": "GMV", "content": "1.2亿"}]}
        p = build_preview_for_tool("bi_ask_question", metric)
        assert p is not None and p["kind"] == "kpi"
        assert p["value"] == "1.2亿"

        rows = {"type": "rows", "rows": [{"省": "广东", "GMV": 1}, {"省": "江苏", "GMV": 2}]}
        p2 = build_preview_for_tool("bi_ask_question", rows)
        assert p2 is not None and p2["kind"] == "table"


class TestPrefixCouplingGuard:
    """★ 批 1 最贵的一条教训：**「硬匹配点」不等于「按名字判定的点」**。

    discovery 数出「32 处硬匹配」，靠的是 grep 全名。但真正把改名打断的是
    **前缀判定** —— `tool_name.startswith("canvas")` 之类，grep 全名一个都看不见。
    批 1 实测踩到 5 处（3 后端 2 前端），其中 `subagent._canvas_sections_from_invocations`
    的那处会让**整条 chat 行内图链静默失效**，且不报任何错。

    所以留这条闸：源码里不许再对工具名裸写 canvas 前缀判定，
    要用 `tool_category.is_canvas_tool` / `is_canvas_add_tool`。
    """

    #: Allowed bare-prefix locations (with reasons, no silent exemptions).
    _EXEMPT = {
        # Pre-omicron **dotted** legacy name (`canvas.add_chart`) is unrelated: rename
        # affects only underscore names; dotted names never become add_chart.
        "lead_orchestrator.py": 'startswith("canvas.") —— 点号老名，改名不影响',
        # Decision function's own body.
        "tool_category.py": "判定函数本体",
    }

    def test_no_raw_canvas_prefix_check_on_tool_names(self) -> None:
        import pathlib as _p
        import re

        src = _p.Path(__file__).resolve().parents[1] / "src" / "bowhead_core"
        # Catch only underscore family and bare "canvas"; permit legacy "canvas." dotted names.
        pat = re.compile(r'startswith\(\s*\(?\s*"canvas(?!\.)')
        offenders: list[str] = []
        for f in src.rglob("*.py"):
            if f.name in self._EXEMPT:
                continue
            for i, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
                # Code only: comments/docstrings citing the bad pattern (e.g. "do not write this")
                # do not violate it, or the guard catches the comment explaining itself.
                code = line.split("#", 1)[0]
                if not pat.search(code):
                    continue
                offenders.append(f"{f.name}:{i}: {line.strip()[:90]}")
        assert not offenders, (
            "工具名的 canvas 前缀裸判定（改名后会静默漏掉 add_chart / add_image）：\n"
            + "\n".join(offenders)
            + "\n改用 tool_category.is_canvas_tool / is_canvas_add_tool。"
        )


class TestRenamedChartAndImage:
    """批 1 —— `canvas_add_chart` → `add_chart`、`canvas_add_image` → `add_image`。"""

    def test_both_renames_are_declared(self) -> None:
        assert TOOL_RENAMES["canvas_add_chart"] == "add_chart"
        assert TOOL_RENAMES["canvas_add_image"] == "add_image"

    def test_only_the_two_figure_tools_are_renamed(self) -> None:
        """其余 canvas_add_* **不改名**：它们只写画布，前缀是准确的。

        改名的理由是「这两个产的图既可能进画布也可能只进 chat 气泡」
        （`_INLINEABLE_CANVAS_TOOLS`），不是「canvas_ 前缀不好看」。
        """
        assert "canvas_add_insight" in ALL_TOOL_NAMES
        assert "canvas_add_html_artifact" in ALL_TOOL_NAMES
        assert "canvas_add_insight" not in TOOL_RENAMES

    def test_old_names_still_resolve_everywhere(self) -> None:
        from bowhead_core.tool_category import (
            is_canvas_add_tool,
            is_canvas_tool,
            lookup_kind,
        )

        for old, new in (("canvas_add_chart", "add_chart"), ("canvas_add_image", "add_image")):
            assert canonical_tool_name(old) == new
            assert lookup_kind(old) == lookup_kind(new)
            assert is_canvas_tool(old) and is_canvas_tool(new)
            assert is_canvas_add_tool(old) and is_canvas_add_tool(new)

    def test_inline_figure_chain_accepts_both_spellings(self) -> None:
        """★ 这条链是上一个 sprint 刚做通的，改名最容易把它弄哑（实测踩过）。"""
        from types import SimpleNamespace

        from bowhead_core.subagent import _canvas_sections_from_invocations

        for name in ("add_chart", "canvas_add_chart", "add_image", "canvas_add_image"):
            inv = SimpleNamespace(
                tool_name=name,
                result={"section_id": "sec_x", "kind": "bar", "title": "T"},
                arguments={},
            )
            got = _canvas_sections_from_invocations([inv])
            assert len(got) == 1, f"{name} 没被认成画布 section"
            assert got[0]["id"] == "sec_x"


class TestDeprecationTelemetryIsReachable:
    """★ 遥测必须**在生产上取得到**，不只是「进程内记了一笔」。

    116 实测：`/metrics` 端点默认关（`BOWHEAD_METRICS_ENDPOINT_ENABLED` 未设 → 404），
    且该端点无鉴权、标签里带 `workspace_id`，公网裸开不合适。
    ⇒ 旧名命中必须**同时**打一行日志，不依赖任何 env 开关。
    """

    def test_a_rename_hit_emits_a_log_line(self, injected_rename, caplog) -> None:
        import logging

        from bowhead_core.tool_name_canon import canonicalize_tool_refs

        with caplog.at_level(logging.INFO, logger="bowhead_core.tool_name_canon"):
            canonicalize_tool_refs([_OLD], source="probe")

        hits = [r for r in caplog.records if "rename_resolved" in r.getMessage()]
        assert hits, "旧名命中没留下日志——生产上这份数据就取不到了"
        msg = hits[0].getMessage()
        assert _OLD in msg and _NEW in msg and "probe" in msg
