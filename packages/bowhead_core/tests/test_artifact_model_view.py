"""批 2d UT — 模型面投影 seam + 选择性摘掉原始文件名。

一句话不变量：**机器产出的数据不给模型原始文件名；用户上传 / KB 给。**

前者是「另一个看起来像路径的串」——116 实测 22 次「把中文原始文件名当路径」，
离线 2×2 实测同一任务多这一个候选串正确率 100%→85.9%；它的语义职能已由批 2b 的
`origin=` 接管且信息更全。后者是用户自己起的名字、是他认领自己文件的锚点（拍板 10）。

设计：docs/sprints/artifact-addressing-2026-08-19/tech-design-batch2.md §3.4a
"""

from __future__ import annotations

import json

import pytest
from bowhead_core._ref_allowlist import _USER_OWNED_SOURCES, RefAllowlist
from bowhead_core.agents.lead_orchestrator import (
    _USER_OWNED_SOURCES as _LEAD_USER_OWNED,
)
from bowhead_core.agents.lead_orchestrator import (
    render_pinned_datasets_block,
)

from bowhead_core import model_view

_BI_FLAT = {
    "artifact_id": "art_bi0000000001",
    "artifact_filename": "2026年7月省营业额_45ecf83.csv",
    "_debug_csv_filename": "2026年7月省营业额_45ecf83.csv",
    "columns": ["省", "营业额 / 元"],
    "rows": [{"省": "上海", "营业额 / 元": 3.088e8}],
    "row_count": 5,
}
_BI_BATCH = {"results": [dict(_BI_FLAT), dict(_BI_FLAT, artifact_id="art_bi0000000002")]}


# ── Projection seam ─────────────────────────────────────────────────────────


def test_projection_drops_filename_from_bi_result() -> None:
    out = model_view.project_tool_result("bi_query", _BI_FLAT)
    assert "artifact_filename" not in out
    assert "_debug_csv_filename" not in out
    # Everything the model actually needs to complete its work must remain intact.
    assert out["artifact_id"] == "art_bi0000000001"
    assert out["columns"] == ["省", "营业额 / 元"]
    assert out["rows"] == _BI_FLAT["rows"]


def test_projection_reaches_into_batch_shape() -> None:
    """batch 是 `{"results": [...]}`，只看顶层会漏——这是最容易写错的一处。"""
    out = model_view.project_tool_result("bi_query", _BI_BATCH)
    assert all("artifact_filename" not in r for r in out["results"])
    assert [r["artifact_id"] for r in out["results"]] == ["art_bi0000000001", "art_bi0000000002"]


def test_projection_does_not_mutate_the_original() -> None:
    """后端与用户面还要读原字段：`canvas_composers/mappers.py` 用 artifact_filename
    渲 Code tab 的 `-- artifact:` 行。投影必须只作用于送模型的副本。"""
    src = dict(_BI_FLAT)
    model_view.project_tool_result("bi_query", src)
    assert src["artifact_filename"] == "2026年7月省营业额_45ecf83.csv"


def test_projection_is_opt_in_per_tool() -> None:
    """未登记的工具原样返回——不做「猜哪些键像内部的」。"""
    payload = {"artifact_filename": "keep.csv", "x": 1}
    assert model_view.project_tool_result("some_other_tool", payload) == payload


def test_projection_flag_can_be_turned_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """留一个即时 kill switch：出问题不用回滚镜像。"""
    monkeypatch.setenv("BOWHEAD_MODEL_VIEW_PROJECTION", "0")
    out = model_view.project_tool_result("bi_query", _BI_FLAT)
    assert "artifact_filename" in out, "关掉后必须原样透传，否则这个开关是假的"


def test_serialize_applies_the_projection() -> None:
    """★ 闸打在真正送进 LLM 的那句上，而不是只测投影函数本身。

    在 2d 之前 `_serialize` 是一句裸 json.dumps —— 「模型能看见什么」由每个工具隐式决定。
    """
    from bowhead_core.subagent import ToolInvocation, _serialize

    inv = ToolInvocation(tool_name="bi_query", arguments={}, call_id="c1", result=dict(_BI_FLAT))
    payload = json.loads(_serialize(inv))
    assert "artifact_filename" not in payload
    assert payload["artifact_id"] == "art_bi0000000001"
    # The original object remains unchanged (as above: backend/user surfaces still need it).
    assert "artifact_filename" in inv.result


# ── Inventory surface: retain filenames selectively ──────────────────────────


def test_available_refs_hides_machine_filename_keeps_user_filename() -> None:
    al = RefAllowlist()
    al.add("data", "art_bi", filename="2026年7月省营业额_45ecf83.csv", source="bi_fetch_rows", origin="2026年7月 各省 营业额 按省分组", size=20480)
    al.add("data", "art_up", filename="26H1 PIE选拔笔试成绩.xlsx", source="upload", size=81920)
    al.add("doc", "chunk_x", file_name="财务SOP.pdf", page=3)
    xml = al.to_prompt_xml()
    assert "2026年7月省营业额_45ecf83.csv" not in xml, "机器产出的文件名不该进模型视野"
    assert "2026年7月 各省 营业额 按省分组" in xml, "语义要由 origin 接住，不能一起没了"
    assert "26H1 PIE选拔笔试成绩.xlsx" in xml, "用户上传的文件名是用户的锚点，必须留"
    assert "财务SOP.pdf" in xml, "KB 文档同理"


def test_available_refs_exposes_bytes_as_the_dup_discriminator() -> None:
    """轮内近重复数据集 22% 只差行数；行数不在 artifact 记录上，字节数是免迁移的替代。"""
    al = RefAllowlist()
    al.add("data", "a1", filename="x.csv", source="bi", origin="Q 前20", size=2048)
    al.add("data", "a2", filename="y.csv", source="bi", origin="Q 前30", size=3072)
    xml = al.to_prompt_xml()
    assert 'bytes="2048"' in xml and 'bytes="3072"' in xml


def test_pinned_block_hides_machine_filename_keeps_user_filename() -> None:
    out = render_pinned_datasets_block(
        [
            {
                "filename": "2026年7月省营业额_45ecf83.csv",
                "mime": "text/csv",
                "size": 20480,
                "artifact_id": "art_bi",
                "origin_query": "2026年7月 各省 营业额 按省分组",
                "source": "bi_fetch_rows",
            },
            {"filename": "26H1 PIE选拔笔试成绩.xlsx", "mime": "application/x", "size": 81920, "artifact_id": "art_up", "source": "upload"},
        ],
        2,
    )
    assert "2026年7月省营业额_45ecf83.csv" not in out
    assert "origin=2026年7月 各省 营业额 按省分组" in out
    assert "26H1 PIE选拔笔试成绩.xlsx" in out
    assert "20480B" in out, "字节数要留着——摘掉文件名后它是清单里仅剩的定量区分维度"


def test_kill_switch_restores_filenames_on_all_three_surfaces(monkeypatch: pytest.MonkeyPatch) -> None:
    """★ 开关必须盖住**整个 2d**，不只是 _serialize。

    只盖一处的话，出事时翻开关会得到「工具结果恢复了、但两个 prompt 面照旧不给文件名」
    的半截状态——开关名不副实，救不了场。
    """
    monkeypatch.setenv("BOWHEAD_MODEL_VIEW_PROJECTION", "0")
    al = RefAllowlist()
    al.add("data", "art_bi", filename="2026年7月省营业额_45ecf83.csv", source="bi_fetch_rows", origin="2026年7月 各省 营业额 按省分组", size=20480)
    assert "2026年7月省营业额_45ecf83.csv" in al.to_prompt_xml(), "清单面没跟着开关走"

    out = render_pinned_datasets_block(
        [
            {
                "filename": "2026年7月省营业额_45ecf83.csv",
                "mime": "text/csv",
                "size": 20480,
                "artifact_id": "art_bi",
                "origin_query": "2026年7月 各省 营业额 按省分组",
                "source": "bi_fetch_rows",
            }
        ],
        1,
    )
    assert "2026年7月省营业额_45ecf83.csv" in out, "Lead pinned 块没跟着开关走"


def test_no_origin_means_keep_the_filename() -> None:
    """★ review 逮到的 P1：**摘掉语义之前必须先有替代品**。

    只按 source 判会让存量 artifact 变成完全匿名——`<data id="1" bytes="20480" source="bi"/>`，
    peer 无从识别。116 上 7,490 个 artifact 里只有批 2a 之后产出的才有 origin；
    canvas 导出物等类别本就没有。2d 要拿掉的是**多余**的候选串，不是**唯一**的那个。
    """
    al = RefAllowlist()
    al.add("data", "art_legacy", filename="2026年品牌退款额_e82297b.csv", source="bi_fetch_rows", size=20480)  # Existing record: no origin
    al.add("data", "art_deck", filename="各省经营对比分析报告.xlsx", source="canvas_export_skill", size=81920)  # Deliverable: no origin by design
    xml = al.to_prompt_xml()
    assert "2026年品牌退款额_e82297b.csv" in xml, "无 origin ⇒ 必须留文件名，否则该条完全匿名"
    assert "各省经营对比分析报告.xlsx" in xml
    # Negative control: the same kind **with** origin still loses the filename (otherwise this fix would undo all of 2d).
    al2 = RefAllowlist()
    al2.add("data", "art_new", filename="2026年7月省营业额_45ecf83.csv", source="bi_fetch_rows", origin="2026年7月 各省 营业额 按省分组", size=20480)
    assert "2026年7月省营业额_45ecf83.csv" not in al2.to_prompt_xml()


def test_pinned_block_no_origin_means_keep_the_filename() -> None:
    """同上，Lead 那一面。"""
    out = render_pinned_datasets_block(
        [
            {
                "filename": "2026年品牌退款额_e82297b.csv",
                "mime": "text/csv",
                "size": 20480,
                "artifact_id": "art_legacy",
                "source": "bi_fetch_rows",
            },  # No origin_query
        ],
        1,
    )
    assert "2026年品牌退款额_e82297b.csv" in out, "无 origin ⇒ 留文件名，别渲成一行空壳"
    assert "origin=(not recorded)" in out


def test_user_owned_sources_is_a_single_object_not_two_copies() -> None:
    """两个面必须引用**同一个对象**，而不是两份拷贝靠断言对拍。

    复制常量再写个 parity 测试，正是本 sprint 自己诊断出的「一个概念 N 处实现」反模式：
    测试只能在有人跑它时发现分叉，共享对象让分叉根本不可能。
    """
    assert _USER_OWNED_SOURCES is model_view.USER_OWNED_SOURCES
    assert _LEAD_USER_OWNED is model_view.USER_OWNED_SOURCES
