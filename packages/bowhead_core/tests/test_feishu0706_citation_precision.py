"""飞书07.06 #11 — 数字溯源精确化回归锁。

真机事故 (prod 116, s_8a063ae8cd8e / t_de2b93219ee6):
narrative `同比增长[num:4.18%:src:call_e5a0…][3]` — 同一数字双标记;
点数字走 call_id → source_ref_aggregator subs[0] 塌缩 → 指到批次第一张
csv (一级分行) 而非真出处 (同比变化率)。 [3] 编号按 sources[] 下标反而正确。

修复面 (全部在此锁定):
  A. `_build_value_to_source_map` — 批量子结果的值绑到**子表 artifact_id**,
     不再绑批次 call_id。
  B. `citation_marks.bind_to_numbered`（M3-4 前为 _autowrap_plain_numbers/_bind_inline_num_sources）— 数字紧跟 [N]
     编号引用时跳过 (writer 已显式引用, 别叠第二代标记)。
  C. `aggregate_source_refs_from_events` — 批量 (≥2 子结果) 不再塌缩
     `artifact_id=subs[0]`; 单子结果保留精确单链。
  D. `resolve_in_text` lazy-add — 子表 artifact 的条目 label/query/preview
     取自那张子表 (query_text/sample_rows), 不是整批顶层。
  E. `_ref_validation._scan_markers` — `:src:` 接受**本轮** artifact id
     (turn_artifacts 集), 不再只认 call_id; 跨轮 id 仍拒 (review F5)。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from bowhead_core._ref_validation import _scan_markers
from bowhead_core.canvas_composers.answer_decomposer import (
    _build_value_to_source_map,
)
from bowhead_core.source_ref_aggregator import aggregate_source_refs_from_events
from bowhead_core.sources_resolver import resolve_in_text


def _inv(call_id: str, result: dict[str, Any], tool: str = "bi_query") -> Any:
    return SimpleNamespace(
        call_id=call_id,
        tool_name=tool,
        result=result,
        error=None,
        arguments={},
    )


_BATCH = {
    "results": [
        {
            "artifact_id": "art_branch01",
            "query_text": "2025年 一级分行 刷卡金额",
            "metrics": {"total": 40719385.17},
            "sample_rows": [{"一级分行": "海南分行", "刷卡金额": 40719385.17}],
            "total_rows": 30,
        },
        {
            "artifact_id": "art_yoy02",
            "query_text": "2025年 刷卡金额 同比",
            "metrics": {"rate": 0.0418},
            "sample_rows": [{"刷卡金额同比变化率": 0.0418}],
            "total_rows": 1,
        },
    ],
}


# ── A. value map: batch subresults → exact artifact ────────────────────────────────


def test_value_map_batch_binds_sub_artifact_not_call_id() -> None:
    m = _build_value_to_source_map([_inv("call_batch", _BATCH)])
    # Incident shape: YoY once bound to call_batch, collapsing onto art_branch01 (wrong table).
    assert m[0.0418] == "art_yoy02"
    assert m[40719385.17] == "art_branch01"
    assert "call_batch" not in m.values()


def test_value_map_cross_sub_collision_drops() -> None:
    batch = {
        "results": [
            {"artifact_id": "art_a", "metrics": {"v": 88.0}},
            {"artifact_id": "art_b", "metrics": {"v": 88.0}},
        ],
    }
    m = _build_value_to_source_map([_inv("call_x", batch)])
    # Equal values in two subtables → ambiguous → drop (formerly falsely bound to call_id).
    assert 88.0 not in m


def test_value_map_no_artifact_falls_back_to_call_id() -> None:
    m = _build_value_to_source_map([_inv("call_plain", {"metrics": {"v": 7.5}})])
    assert m[7.5] == "call_plain"


def test_value_map_artifact_prefix_guard() -> None:
    # Data row has an artifact_id column with non-art_ value → ownership does not switch.
    batch = {"results": [{"artifact_id": "XJ-0071", "metrics": {"v": 3.14}}]}
    m = _build_value_to_source_map([_inv("call_g", batch)])
    assert m[3.14] == "call_g"


def test_value_map_ref_artifact_id_trusted_without_prefix() -> None:
    batch = {"results": [{"_ref_artifact_id": "tl_legacy9", "metrics": {"v": 6.6}}]}
    m = _build_value_to_source_map([_inv("call_h", batch)])
    assert m[6.6] == "tl_legacy9"


# ── B. [N]-adjacency skip: no duplicate markers ────────────────────────────────────


def test_binder_skips_number_already_cited_with_numbered_ref() -> None:
    """真机事故（prod 116 t_de2b93219ee6）的 gen3 形态锁：writer 已写 `4.18%[3]`，
    绑定器**不得**再给同一数字叠一个引用记号（当年叠成 `[num:4.18%:src:…][3]`
    → FE 双装饰 + trust 双计数）。M3-4 换形态：绑定动作现在是 `bind_to_numbered`
    产 `值 [N]`，adjacent-[N] 跳过判据逐字复用。"""
    from bowhead_core.citation_marks import bind_to_numbered
    from bowhead_core.sources_resolver import TurnSourcesRegistry

    def _bind(text: str) -> str:
        reg = TurnSourcesRegistry()
        for _ in range(3):  # Make [3] valid (simulate 3 existing sources in the real pipeline).
            reg.register_source(f"art_pre{_}", tool="bi_query", query="q")
        return bind_to_numbered(
            text,
            value_to_source={0.0418: "art_yoy02"},
            sources=reg.sources,
            id_to_index=reg.id_to_index,
        )

    # Two original incident shapes (adjacent / space-separated) must both avoid stacked markers.
    assert _bind("同比增长4.18%[3]。") == "同比增长4.18%[3]。"
    assert _bind("同比增长4.18% [3]。") == "同比增长4.18% [3]。"
    # Numbers without [N] bind as usual (normal path unaffected).
    assert _bind("同比增长4.18%。") == "同比增长4.18% [4]。"


# ── C. aggregator: batches never collapse to subs[0] ──────────────────────────────


def _events(call_id: str, result: dict[str, Any]) -> list[Any]:
    return [
        SimpleNamespace(
            event_type="tool_call",
            payload={
                "call_id": call_id,
                "tool_name": "bi_query",
                "arguments_preview": {"query": "批量查询"},
            },
        ),
        SimpleNamespace(
            event_type="tool_result",
            payload={
                "call_id": call_id,
                "tool_name": "bi_query",
                "result_inline": result,
            },
        ),
    ]


def test_aggregator_multi_sub_no_collapse() -> None:
    refs = aggregate_source_refs_from_events("t_1", _events("call_b", _BATCH))
    (ref,) = refs
    assert [s["artifact_id"] for s in ref["sub_sources"]] == [
        "art_branch01",
        "art_yoy02",
    ]
    # Incident root cause: hardcoded subs[0]=art_branch01 as single link, sending popovers to the wrong table.
    assert "artifact_id" not in ref


def test_aggregator_single_sub_keeps_precise_link() -> None:
    single = {"results": [_BATCH["results"][1]]}
    refs = aggregate_source_refs_from_events("t_2", _events("call_s", single))
    (ref,) = refs
    assert ref["artifact_id"] == "art_yoy02"


def test_aggregator_strips_top_level_collapsed_artifact_on_batch() -> None:
    """真栈 dogfood 逮到: result_meta 顶层带一个 artifact_id (上游塌缩残留),
    绕过 subs[0] 守卫 → 批量 ref 仍指向单一子表。顶层 id ∈ 子表集合时摘掉。"""
    batch_with_top = {"artifact_id": "art_branch01", **_BATCH}
    refs = aggregate_source_refs_from_events("t_3", _events("call_t", batch_with_top))
    (ref,) = refs
    assert len(ref["sub_sources"]) == 2
    assert "artifact_id" not in ref
    # Top-level id belongs to no subtable (actual batch-level artifact, e.g. merged CSV) → retain.
    batch_with_own = {"artifact_id": "art_combined99", **_BATCH}
    refs2 = aggregate_source_refs_from_events("t_4", _events("call_u", batch_with_own))
    (ref2,) = refs2
    assert ref2["artifact_id"] == "art_combined99"


# ── D. resolve_in_text lazy-add: subtable-granularity entries ──────────────────────


def test_resolve_lazy_add_uses_sub_result_label_and_preview() -> None:
    inv = _inv("call_batch", _BATCH)
    text = "同比增长 [num:4.18:%:src:art_yoy02] 明显"
    new_text, sources, id2idx = resolve_in_text(
        text,
        sources=[],
        id_to_index={},
        tool_invocations=[inv],
    )
    assert "[1]" in new_text and ":src:" not in new_text
    (entry,) = sources
    assert entry["artifact_id"] == "art_yoy02"
    # label/query come from subtable query_text, not batch top level.
    assert "同比" in entry["label"]
    assert entry["query"] == "2025年 刷卡金额 同比"
    # Subtable sample_rows enter preview (citation hover shows original text).
    assert entry.get("preview_rows")
    assert id2idx["art_yoy02"] == 1


# ── E. ref validation: valid artifact ids ────────────────────────────────────


def test_scan_markers_accepts_turn_artifact_src() -> None:
    # review F5: artifact allowlist = this turn's invocation outputs (turn_artifacts),
    # not all session data (data); cross-turn ids remain rejected.
    refs = {"tl": {"call_1"}, "data": {"art_yoy02", "art_oldturn9999"}, "turn_artifacts": {"art_yoy02"}, "doc": set(), "chart": set(), "code": set()}
    ok = _scan_markers("增长 [num:4.18:%:src:art_yoy02]", "f", refs)
    assert ok == []
    still_ok = _scan_markers("增长 [num:4.18:%:src:call_1]", "f", refs)
    assert still_ok == []
    # Present in session, not produced this turn → reject (cross-turn guard)
    stale = _scan_markers("增长 [num:4.18:%:src:art_oldturn9999]", "f", refs)
    assert len(stale) == 1 and stale[0]["kind"] == "num_src_not_in_allowlist"
    bad = _scan_markers("增长 [num:4.18:%:src:art_missing]", "f", refs)
    assert len(bad) == 1


@pytest.mark.asyncio
async def test_build_real_refs_turn_artifacts_from_invocations() -> None:
    from bowhead_core._ref_validation import _build_real_refs

    class _NoStore:
        async def list_section_ids_by_kind(self, *a, **k):
            raise RuntimeError

        async def list_for_session(self, **k):
            raise RuntimeError

    inv = _inv("call_b", _BATCH)
    refs = await _build_real_refs(
        canvas_id="c",
        session_id="s",
        user_id="u",
        canvas_store=_NoStore(),
        artifact_store=_NoStore(),
        tool_invocations=[inv],
    )
    assert refs["turn_artifacts"] == {"art_branch01", "art_yoy02"}


def test_extract_turn_artifacts_covers_sandbox_outputs_and_camelcase() -> None:
    # review[1]-fix: python_sandbox output artifacts + camelCase artifactId both count
    # as this turn's outputs, or their [num:…:src:art_x] references are falsely stripped.
    from bowhead_core._ref_validation import _extract_turn_artifact_ids

    invs = [
        SimpleNamespace(result={"result_json": {"outputs": [{"artifact_id": "art_out01"}]}}),
        SimpleNamespace(result={"results": [{"artifactId": "art_camel02"}]}),
        SimpleNamespace(result={"inputs": {"monthly": {"artifact_id": "art_in03"}}}),
        SimpleNamespace(result={"_ref_artifact_id": "tl_legacy04"}),
    ]
    got = _extract_turn_artifact_ids(invs)
    assert got == {"art_out01", "art_camel02", "art_in03", "tl_legacy04"}


# ── source-accessibility presentation e2: sandbox stdout as a weak value source ────────────
# Rules: strong always wins (ambiguous drops stay dropped) / bind only unique weak matches / omit trivial integers.


def _sbx(call_id: str, stdout: str, aid: str | None = None) -> Any:
    result: dict[str, Any] = {"stdout": stdout}
    if aid:
        result["artifact_id"] = aid
    return _inv(call_id, result, tool="python_sandbox")


def test_v2s_weak_sandbox_stdout_unique_value_binds() -> None:
    """stdout 里唯一出现的派生值（LMDI 贡献类）进 map，src=结果 artifact id。"""
    m = _build_value_to_source_map([_sbx("call_s1", "量贡献 60484 价贡献 -22142", aid="art_sbx1")])
    assert m.get(60484.0) == "art_sbx1"
    assert m.get(-22142.0) == "art_sbx1"


def test_v2s_weak_falls_back_to_call_id_without_artifact() -> None:
    m = _build_value_to_source_map([_sbx("call_s2", "客单 351.4")])
    assert m.get(351.4) == "call_s2"


def test_v2s_strong_wins_over_weak() -> None:
    """同值同时在 bi metrics（strong）与 stdout（weak）⇒ 绑 strong 源，weak 不覆盖。"""
    strong = _inv("call_bi", {"results": [{"artifact_id": "art_bi1", "metrics": {"v": 616.4}}]})
    m = _build_value_to_source_map([strong, _sbx("call_s3", "计算得 616.4", aid="art_sbx3")])
    assert m.get(616.4) == "art_bi1"


def test_v2s_weak_never_rescues_strong_ambiguous() -> None:
    """strong 内两源撞同值（歧义 drop）⇒ 即使 stdout 唯一出现也保持 drop——
    strong 歧义是真数据歧义，weak 翻案就是 ι-15 的错绑形态。"""
    a = _inv("call_a", {"results": [{"artifact_id": "art_a", "metrics": {"v": 8300.0}}]})
    b = _inv("call_b", {"results": [{"artifact_id": "art_b", "metrics": {"v": 8300.0}}]})
    m = _build_value_to_source_map([a, b, _sbx("call_s4", "增速 8300", aid="art_sbx4")])
    assert 8300.0 not in m


def test_v2s_weak_multi_sandbox_collision_drops() -> None:
    m = _build_value_to_source_map([_sbx("call_s5", "结果 127024", aid="art_x"), _sbx("call_s6", "127024", aid="art_y")])
    assert 127024.0 not in m


def test_v2s_weak_trivial_integers_excluded() -> None:
    """stdout 的行号/件数级小整数不入 weak（|v|<100 且整数）；非整小值仍可绑。"""
    m = _build_value_to_source_map([_sbx("call_s7", "index 7 行数 42 客单 69.4 单价 13.0", aid="art_sbx7")])
    assert 7.0 not in m
    assert 42.0 not in m
    assert m.get(69.4) == "art_sbx7"
    # Decimal "13.0" is a measurement, not an ordinal: must enter weak (token shape, not is_integer).
    assert m.get(13.0) == "art_sbx7"


def test_v2s_weak_ignores_failed_or_non_sandbox() -> None:
    bad = SimpleNamespace(call_id="call_e", tool_name="python_sandbox", result={"stdout": "555.5"}, error="boom", arguments={})
    other = _inv("call_o", {"stdout": "777.7"}, tool="bi_query")
    m = _build_value_to_source_map([bad, other])
    assert 555.5 not in m
    assert 777.7 not in m


def test_v2s_weak_accepts_flat_python_exec_entrance() -> None:
    """批 5ⓑ盲区回归钉（2026-08-29 真机 t_1c34b8e87ef9）：compute 扁平入口
    `python_exec` 的 stdout 嵌在 result_json 里——weak 臂必须认这一入口/形状，
    否则量贡献类派生值整条裸奔。"""
    flat = SimpleNamespace(
        call_id="call_flat",
        tool_name="python_exec",
        result={"ok": True, "result_json": {"stdout": "量贡献: -35,055,167.27 元 = -3,505.52 万元"}},
        error=None,
        arguments={},
    )
    m = _build_value_to_source_map([flat])
    assert m.get(-3505.52) == "call_flat"
    assert m.get(-35055167.27) == "call_flat"


def _runner_output(artifact_id: str, name: str, data_role: str | None = None) -> dict[str, Any]:
    """**照抄 runner 真产的 outputs[] 记录**（`runner._upload_artifact` 返
    `{name, artifact_id, mime, size}`，`_materialize_data_outputs` 再盖 `data_role`）。

    ⚠ 这里**不许**出现 `filename` 键：review F1 逮到初版判据读 `filename` 在生产上
    永不命中，而单测因为 fixture 自己发明了那个键照样全绿
    （[[feedback_my_own_fixtures_are_the_happy_path]]）。"""
    rec: dict[str, Any] = {"name": name, "artifact_id": artifact_id, "mime": "text/plain", "size": 42}
    if data_role:
        rec["data_role"] = data_role
    return rec


def test_v2s_weak_src_id_prefers_sandbox_output_artifact_over_call_id() -> None:
    """e2-r2 第二半（1.1.94 真机 t_1831b22ed3c1）：扁平入口的产物 id 在
    `result_json.outputs[]`，不读它就退 call_id——而 call_id 不是任何归属解析器
    认得的形状 ⇒ 来源判 `linked_unavailable`，用户点开角标是「该来源不可用」。
    只认 stdout 产物（weak 值的出处），它是本 turn 产物、走既有 art_ 解析器。"""
    flat = SimpleNamespace(
        call_id="call_flat2",
        tool_name="python_exec",
        result={
            "ok": True,
            "result_json": {
                "ok": True,
                "exit_code": 0,
                "stdout": "价贡献: -4,053.47 万元",
                "outputs": [
                    _runner_output("art_csvout0001", "out.csv", "structured"),
                    _runner_output("art_stdout00001", "sandbox-stdout.txt", "stdout"),
                ],
            },
        },
        error=None,
        arguments={},
    )
    m = _build_value_to_source_map([flat])
    assert m.get(-4053.47) == "art_stdout00001"


def test_v2s_weak_skips_a_failed_sandbox_run() -> None:
    """review F3：扁平入口把失败编码成 外层 ok=True + result_json.ok=False 且
    `inv.error is None` ⇒ 崩溃前打印的中间量不得成为引用来源（判据同 mappers D17）。"""
    crashed = SimpleNamespace(
        call_id="call_boom",
        tool_name="python_exec",
        result={
            "ok": True,
            "result_json": {
                "ok": False,
                "exit_code": 1,
                "error": "ZeroDivisionError",
                "stdout": "中间量: 12,345.67",
                "outputs": [_runner_output("art_stdoutboom01", "sandbox-stdout.txt", "stdout")],
            },
        },
        error=None,
        arguments={},
    )
    m = _build_value_to_source_map([crashed])
    assert 12345.67 not in m
    assert 123456700.0 not in m  # Ten-thousand scale bridge must not admit it either.


def test_v2s_weak_keeps_a_run_that_only_failed_artifact_materialization() -> None:
    """review R2-1：产物物化失败**不是**跑挂。runner 的 `_fail_data_materialization`
    会因「某个文件超 50MB / 上传失败 / 声明的 deliverable 没产出」把**算完并打印了全部
    派生值**的运行标成 ok=False+exit_code=1（它自己写着 "Run success remains independent
    from artifact presence"）。照 D17 原样跳过 ⇒「stdout 派生值裸奔」这个 e2 要修的回归
    静默复发。"""
    materialization_only = SimpleNamespace(
        call_id="call_bigcsv",
        tool_name="python_exec",
        result={
            "ok": False,
            "exit_code": 1,
            "error": "sandbox structured exceeds Data artifact limit",
            "result_json": {
                "ok": False,
                "exit_code": 1,
                "error": "sandbox structured exceeds Data artifact limit",
                "stdout": "量贡献: -3,505.52 万元",
                "outputs": [_runner_output("art_stdoutbig001", "sandbox-stdout.txt", "stdout")],
            },
        },
        error=None,
        arguments={},
    )
    assert _build_value_to_source_map([materialization_only]).get(-3505.52) == "art_stdoutbig001"

    # Actual error mixed in (crash before materialization failure) ⇒ still failed.
    mixed = SimpleNamespace(
        call_id="call_mixed",
        tool_name="python_exec",
        result={
            "ok": True,
            "result_json": {
                "ok": False,
                "exit_code": 1,
                "error": "ZeroDivisionError: division by zero; sandbox stdout Data artifact upload failed",
                "stdout": "中间量: 777.7",
                "outputs": [],
            },
        },
        error=None,
        arguments={},
    )
    assert 777.7 not in _build_value_to_source_map([mixed])

    # Environment failure (executor unreachable) with cause ⇒ failed.
    env_fail = SimpleNamespace(
        call_id="call_env",
        tool_name="python_exec",
        result={"ok": False, "exit_code": -1, "cause": "executor_unreachable", "error": "暂不支持代码执行", "stdout": "888.8"},
        error=None,
        arguments={},
    )
    assert 888.8 not in _build_value_to_source_map([env_fail])


def test_v2s_weak_data_role_beats_a_user_file_named_like_stdout() -> None:
    """review R2-3：先整轮找权威 `data_role`，再退名字启发。runner 先扫 workdir 的用户
    文件、真 stdout 记录最后追加且被 `_unique_data_output_path` 改名 ——
    单遍逐元素判会让用户自己写的 `sandbox-stdout.txt` 抢先命中，角标指到错的产物上。"""
    inv = SimpleNamespace(
        call_id="call_shadow",
        tool_name="python_exec",
        result={
            "ok": True,
            "result_json": {
                "ok": True,
                "exit_code": 0,
                "stdout": "价贡献: -4,053.47 万元",
                "outputs": [
                    _runner_output("art_userfile00001", "sandbox-stdout.txt"),  # User-written, no data_role
                    _runner_output("art_realstdout001", "sandbox-stdout-data-1.txt", "stdout"),
                ],
            },
        },
        error=None,
        arguments={},
    )
    assert _build_value_to_source_map([inv]).get(-4053.47) == "art_realstdout001"


def test_v2s_weak_reads_top_level_output_artifacts_envelope() -> None:
    """沙箱 envelope（`bowhead_skill/tools.py::_invoke`）把产物投影到顶层
    `output_artifacts`，`result_json.outputs` 在到手结果里可能已被剥空——两处同源，
    都要认得。"""
    inv = SimpleNamespace(
        call_id="call_env2",
        tool_name="python_exec",
        result={
            "ok": True,
            "exit_code": 0,
            "stdout": "单价: 2,108.71 元",
            "outputs": ["out.csv", "sandbox-stdout.txt"],  # Top-level outputs are **path strings**.
            "output_artifacts": [_runner_output("art_toplevel00001", "sandbox-stdout.txt", "stdout")],
            "result_json": {"ok": True, "exit_code": 0, "outputs": []},
        },
        error=None,
        arguments={},
    )
    assert _build_value_to_source_map([inv]).get(2108.71) == "art_toplevel00001"


def test_v2s_weak_src_id_never_borrows_a_non_stdout_artifact() -> None:
    """**只认 stdout 产物**：weak 值抽自 stdout 文本，拿 `out.csv` 顶替＝「看着有出处、
    点开对不上」（CSV 预览只给前几行）。没有 stdout 产物就退 call_id，宁可不可点。
    ⚠ 这条分支真会走到：stdout 超 `_MAX_OUTPUT_BYTES` 时 runner 不落 stdout artifact，
    而那正是「输出很多数字」的场景。"""
    only_csv = SimpleNamespace(
        call_id="call_a",
        tool_name="python_exec",
        result={"result_json": {"stdout": "指标 351.4", "outputs": [_runner_output("art_only0000001", "out.csv", "structured")]}},
        error=None,
        arguments={},
    )
    assert _build_value_to_source_map([only_csv]).get(351.4) == "call_a"

    bad_shape = SimpleNamespace(
        call_id="call_b",
        tool_name="python_exec",
        result={"result_json": {"stdout": "指标 469.4", "outputs": [{"artifact_id": {"id": 1}}, {"artifact_id": "tbl_x"}]}},
        error=None,
        arguments={},
    )
    assert _build_value_to_source_map([bad_shape]).get(469.4) == "call_b"

    # Legacy record without data_role → fall back to filename; actual names use `_unique_data_output_path`'s
    # collision-avoidance shape `{stem}-data-{n}{suffix}` (review F5b correction: not `_1`).
    renamed = SimpleNamespace(
        call_id="call_c",
        tool_name="python_exec",
        result={"result_json": {"stdout": "指标 588.2", "outputs": [_runner_output("art_renamed00001", "sandbox-stdout-data-1.txt")]}},
        error=None,
        arguments={},
    )
    assert _build_value_to_source_map([renamed]).get(588.2) == "art_renamed00001"


def test_v2s_weak_accepts_skill_invoke_entrance() -> None:
    """老入口 skill_invoke(name="python_sandbox") 同样要认（三入口对齐
    _extract_sandbox_tables，见批 5ⓑ）。"""
    wrapped = SimpleNamespace(
        call_id="call_sk",
        tool_name="skill_invoke",
        result={"ok": True, "result_json": {"output": "客单 351.4"}},
        error=None,
        arguments={"name": "python_sandbox"},
    )
    other_skill = SimpleNamespace(
        call_id="call_sk2",
        tool_name="skill_invoke",
        result={"ok": True, "result_json": {"output": "999.9"}},
        error=None,
        arguments={"name": "web_clip"},
    )
    m = _build_value_to_source_map([wrapped, other_skill])
    assert m.get(351.4) == "call_sk"
    assert 999.9 not in m


def test_v2s_weak_comma_numbers_taken_whole_not_fragmented() -> None:
    """review C2：stdout 的千分位数字整取——`1,234,567` 不得切成 234/567 假值。"""
    m = _build_value_to_source_map([_sbx("call_c2", "合计 1,234,567 元", aid="art_c2")])
    assert m.get(1234567.0) == "art_c2"
    assert 234.0 not in m
    assert 567.0 not in m


def test_v2s_weak_src_id_guards_mirror_strong_walk() -> None:
    """review C6：artifact_id 非 str / 非 art_ 前缀 ⇒ 退 call_id，不产垃圾源。"""
    bad_shape = _inv("call_g1", {"stdout": "指标 351.4", "artifact_id": {"id": 1}}, tool="python_sandbox")
    bad_prefix = _inv("call_g2", {"stdout": "指标 469.4", "artifact_id": "tbl_x"}, tool="python_sandbox")
    m = _build_value_to_source_map([bad_shape, bad_prefix])
    assert m.get(351.4) == "call_g1"
    assert m.get(469.4) == "call_g2"


def test_v2s_window_strong_priority_keeps_existing_binding() -> None:
    """review F2(a)：近邻 weak 键不得把既有 strong 窗口绑定挤成歧义。
    「6.05万」原本唯一窗口命中 strong 60484.0；stdout 混入 60484.37 后必须仍绑 strong。"""
    from bowhead_core.canvas_composers.answer_decomposer import _parse_value_to_float, _resolve_value

    strong = _inv("call_bi", {"results": [{"artifact_id": "art_bi", "metrics": {"v": 60484.0}}]})
    m = _build_value_to_source_map([strong, _sbx("call_s", "精确值 60484.37", aid="art_sbx")])
    v = _parse_value_to_float("6.05万")
    assert _resolve_value(v, m, raw_value_str="6.05万", strong_keys=m.strong_keys) == "art_bi"


def test_v2s_window_weak_cannot_rescue_strong_ambiguous() -> None:
    """review F2(b)：strong 歧义 drop 的值，不得经精度窗口被近邻 weak 键翻案。"""
    from bowhead_core.canvas_composers.answer_decomposer import _parse_value_to_float, _resolve_value

    a = _inv("call_a", {"results": [{"artifact_id": "art_a", "metrics": {"v": 60484.0}}]})
    b = _inv("call_b", {"results": [{"artifact_id": "art_b", "metrics": {"v": 60484.0}}]})
    m = _build_value_to_source_map([a, b, _sbx("call_s", "精确值 60484.37", aid="art_sbx")])
    v = _parse_value_to_float("6.05万")
    assert _resolve_value(v, m, raw_value_str="6.05万", strong_keys=m.strong_keys) is None


def test_v2s_window_weak_only_still_binds() -> None:
    """窗内无任何 strong 时 weak 照常可绑（「6.05万」≈ stdout 60484.37 是合法归属）。"""
    from bowhead_core.canvas_composers.answer_decomposer import _parse_value_to_float, _resolve_value

    m = _build_value_to_source_map([_sbx("call_s", "精确值 60484.37", aid="art_sbx")])
    v = _parse_value_to_float("6.05万")
    assert _resolve_value(v, m, raw_value_str="6.05万", strong_keys=m.strong_keys) == "art_sbx"


def test_v2s_weak_scale_bridge_wan_and_yi() -> None:
    """e2-r1：stdout 打印缩放值、prose 写原值——×1e4/×1e8 桥接。"""
    m = _build_value_to_source_map([_sbx("call_sc", "量贡献 -3068.5 亿口径 19.9", aid="art_sc")])
    assert m.get(-30685000.0) == "art_sc"  # -3068.5 ×1e4 (-3,068.5万元)
    # 19.9×1e8 float and prose parsing share the same multiplication bit-for-bit; assert using the same calculation,
    # not a decimal literal (1.99e9 ≠ 19.9*1e8 at the final digit).
    assert m.get(19.9 * 1e8) == "art_sc"  # 19.9 亿元


def test_v2s_weak_scale_bridge_does_not_do_percent() -> None:
    """e2-r1 否决面钉死：÷100 百分桥**不做**（低熵分数误绑+角标洪泛，空跑记档）。"""
    m = _build_value_to_source_map([_sbx("call_pc", "比率 1670.5", aid="art_pc")])
    assert 16.705 not in m


def test_v2s_weak_scale_expanded_key_yields_to_strong() -> None:
    """扩展键撞 strong 同值 ⇒ strong 恒胜，不被 weak 接手。"""
    strong = _inv("call_bi", {"results": [{"artifact_id": "art_bi", "metrics": {"v": 30685000.0}}]})
    m = _build_value_to_source_map([strong, _sbx("call_sc2", "量贡献 3068.5", aid="art_sc2")])
    assert m.get(30685000.0) == "art_bi"


def test_v2s_weak_scale_expanded_collision_drops() -> None:
    """两个 sandbox 的扩展键撞同值 ⇒ 照旧歧义 drop。"""
    m = _build_value_to_source_map([_sbx("c1", "值 3068.5", aid="art_x1"), _sbx("c2", "3068.5", aid="art_x2")])
    assert 30685000.0 not in m


def test_bind_partial_currency_match_never_lands_mid_number() -> None:
    """e2-r1 空跑逮到的匹配骨架 bug：`¥1,588.2M` 不许回溯成 `¥1` 部分匹配把
    角标插进数字中间（`¥1 [1],588.2M`）——这形态宁可整个不绑。"""
    from bowhead_core.citation_marks import bind_to_numbered

    out = bind_to_numbered(
        "GMV ¥1,588.2M vs ¥1,309.9M",
        value_to_source={1.0: "art_one"},
        sources=[],
        id_to_index={},
        tool_invocations=[],
    )
    assert "[1]" not in out
    # Other half of review F3: scanner restarts after commas, treating `588.2M` as an independent number; even if
    # its value resolves in the chart (M branch ×1e6), never insert a citation inside the number.
    out2 = bind_to_numbered(
        "GMV ¥1,588.2M vs ¥1,309.9M",
        value_to_source={588200000.0: "art_frag"},
        sources=[],
        id_to_index={},
        tool_invocations=[],
    )
    assert "[1]" not in out2


def test_v2s_bridge_skips_sub_one_fractions() -> None:
    """review F9a：|v|<1 的分数形不桥——0.17×1e4=1700.0 是低熵圆整键，会给
    prose 里无关的「1,700元」安上 sandbox 出处。"""
    m = _build_value_to_source_map([_sbx("call_fr", "比率 0.17 系数 0.93", aid="art_fr")])
    assert 1700.0 not in m
    assert 9300.0 not in m


def test_v2s_bridge_key_never_demotes_genuine_weak_value() -> None:
    """review F9b：桥接键是二等公民——sandbox A 的桥接键撞上 sandbox B 的真值
    时，B 的既有唯一绑定必须保留，不得被挤成歧义 drop。"""
    m = _build_value_to_source_map([_sbx("cA", "缩放值 3068.5", aid="art_A"), _sbx("cB", "原值 30685000", aid="art_B")])
    assert m.get(30685000.0) == "art_B"
