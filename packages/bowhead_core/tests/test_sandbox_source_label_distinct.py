"""沙箱来源标签的区分度：产出文件档 + 撞车补序号。

真机动机（116 十四天语料，172 份含 ≥2 条沙箱来源的报告）：一轮里跑几次沙箱，
每次的首行代码几乎恒为 `df = pd.read_csv(INPUTS[...])`，于是 **64% 的报告出现字面
重复的来源行**。插入「本次产出的文件」档后 23%，再对残留补序号后 0%。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from bowhead_core.sources_resolver import (
    TurnSourcesRegistry,
    number_duplicate_source_labels,
)

_READ_CSV = "import pandas as pd\ndf = pd.read_csv(INPUTS['rows']['path'])\nout = df.head()\n"


def _sandbox_inv(code: str, *, artifact_id: str, filename: str, data_role: str | None = None) -> SimpleNamespace:
    """一次沙箱调用的真实形状：产出清单在 `result.output_artifacts`，
    stdout 那条带 `data_role='stdout'`，文件产物**不带** role（116 实测值域只有这两个）。"""
    item: dict[str, Any] = {"artifact_id": artifact_id, "filename": filename}
    if data_role is not None:
        item["data_role"] = data_role
    return SimpleNamespace(
        tool_name="python_sandbox",
        arguments={"code": code},
        result={"artifact_id": artifact_id, "output_artifacts": [item]},
    )


def _labels(reg: TurnSourcesRegistry) -> list[str]:
    return [str(s.get("label") or "") for s in reg.sources]


# ── ⓿ Explicit agent `intent` ──────────────────────────────────────────────


def test_stated_intent_wins_over_everything_inferred() -> None:
    """`intent` 是一等输入：比刮注释、比产出文件、比代码行都优先。"""
    code = "import pandas as pd\n# 顺手写的注释\ndf = pd.read_csv(INPUTS['rows']['path'])\n"
    inv = _sandbox_inv(code, artifact_id="art_1", filename="prov_rank.csv")
    inv.arguments["intent"] = "各省营业额排名与 z 值"
    reg = TurnSourcesRegistry()
    reg.ingest_tool_invocations([inv])
    (label,) = _labels(reg)
    assert "各省营业额排名与 z 值" in label
    assert "顺手写的注释" not in label
    assert "prov_rank.csv" not in label


def test_stated_intent_read_from_the_nested_entry_too() -> None:
    """两个沙箱入口的 `intent` 落点不同（扁平在顶层、`skill_invoke` 嵌一层）。

    归一是 handler wrapper——跑在 `ToolInvocation` 记录**之后**且只改副本，所以
    被记录的 args 里嵌错的形状原样留着，读取侧必须自己兼容两种 holder。
    """
    reg = TurnSourcesRegistry()
    reg.ingest_tool_invocations(
        [
            SimpleNamespace(
                tool_name="skill_invoke",
                arguments={
                    "name": "python_sandbox",
                    "script": "runner.py",
                    "input": {"code": _READ_CSV, "intent": "品类环比变化额/变化率"},
                },
                result={"artifact_id": "art_1", "output_artifacts": [{"artifact_id": "art_1", "filename": "cat_mom.csv"}]},
            )
        ]
    )
    (label,) = _labels(reg)
    assert "品类环比变化额/变化率" in label


def test_blank_or_non_string_intent_falls_through() -> None:
    """fail-open：填了空白 / 填成对象，一律当没填继续走下面的档位，不报错也不渲空。"""
    for bad in ("", "   ", {"a": 1}, 42, None):
        inv = _sandbox_inv(_READ_CSV, artifact_id="art_1", filename="prov_rank.csv")
        inv.arguments["intent"] = bad
        reg = TurnSourcesRegistry()
        reg.ingest_tool_invocations([inv])
        (label,) = _labels(reg)
        assert "prov_rank.csv" in label, f"intent={bad!r} 没有正确降级"


def test_long_intent_is_capped() -> None:
    inv = _sandbox_inv(_READ_CSV, artifact_id="art_1", filename="x.csv")
    inv.arguments["intent"] = "很长的意图" * 40
    reg = TurnSourcesRegistry()
    reg.ingest_tool_invocations([inv])
    (label,) = _labels(reg)
    assert len(label) <= 120


# ── Three label tiers ─────────────────────────────────────────────────────


def test_produced_file_replaces_first_line_of_code() -> None:
    """没写意图注释时，标签取**本次产出的文件**，不再渲首行代码。"""
    reg = TurnSourcesRegistry()
    reg.ingest_tool_invocations([_sandbox_inv(_READ_CSV, artifact_id="art_1", filename="prov_rank.csv")])
    (label,) = _labels(reg)
    assert "prov_rank.csv" in label
    assert "read_csv" not in label


def test_agent_intent_comment_outranks_the_filename() -> None:
    """agent 自己说的「这段在算什么」信息量大过文件名，永远优先。"""
    code = "import pandas as pd\n# 各省营业额排名与 z 值\ndf = pd.read_csv(INPUTS['rows']['path'])\n"
    reg = TurnSourcesRegistry()
    reg.ingest_tool_invocations([_sandbox_inv(code, artifact_id="art_1", filename="prov_rank.csv")])
    (label,) = _labels(reg)
    assert "各省营业额排名与 z 值" in label
    assert "prov_rank.csv" not in label


def test_stdout_is_not_a_produced_file() -> None:
    """判据是 `data_role`、不是文件名——只 print 的运行没有可展示产物，退回首行代码。

    这条钉住「别改成按名字判」：用户自己也能在 workdir 写一个叫 sandbox-stdout.txt
    的文件，而真 stdout 会被 runner 改名。
    """
    reg = TurnSourcesRegistry()
    reg.ingest_tool_invocations([_sandbox_inv(_READ_CSV, artifact_id="art_1", filename="sandbox-stdout.txt", data_role="stdout")])
    (label,) = _labels(reg)
    assert "sandbox-stdout" not in label
    assert "read_csv" in label


def test_a_file_named_like_stdout_still_counts_when_role_says_otherwise() -> None:
    """反向：名字像 stdout 但 role 不是 ⇒ 它是用户自己写的文件，照用。"""
    reg = TurnSourcesRegistry()
    reg.ingest_tool_invocations([_sandbox_inv(_READ_CSV, artifact_id="art_1", filename="sandbox-stdout.txt")])
    (label,) = _labels(reg)
    assert "sandbox-stdout.txt" in label


def test_two_runs_same_code_different_outputs_are_distinguishable() -> None:
    """真机那个形态：同一轮两次沙箱、代码首行一模一样、各写各的文件。"""
    reg = TurnSourcesRegistry()
    reg.ingest_tool_invocations(
        [
            _sandbox_inv(_READ_CSV, artifact_id="art_1", filename="out_prov.csv"),
            _sandbox_inv(_READ_CSV, artifact_id="art_2", filename="out_mom.csv"),
        ]
    )
    labels = _labels(reg)
    assert len(labels) == 2
    assert len(set(labels)) == 2, labels


# ── Number colliding labels ──────────────────────────────────────────────


def test_duplicate_labels_get_run_ordinals() -> None:
    """两次运行都只 print（或都写成同名 out.csv）⇒ 没有真材料，只标次序。"""
    entries = [
        {"tool": "python_sandbox", "label": "运行代码: 读取同比数据", "artifact_id": "art_1"},
        {"tool": "python_sandbox", "label": "运行代码: 读取同比数据", "artifact_id": "art_2"},
    ]
    number_duplicate_source_labels(entries, lang="zh-CN")
    assert entries[0]["label"] == "运行代码: 读取同比数据（第 1 次运行）"
    assert entries[1]["label"] == "运行代码: 读取同比数据（第 2 次运行）"


def test_run_ordinals_are_idempotent_and_renumber_on_growth() -> None:
    """`upsert` 一轮里反复调用（patch 语义 + prior seed 回灌），跑几遍都不该叠后缀；
    后到的第三条要跟着一起编号，不能光着身子站在两条编过号的旁边。"""
    entries = [
        {"tool": "python_sandbox", "label": "运行代码: 读数", "artifact_id": "art_1"},
        {"tool": "python_sandbox", "label": "运行代码: 读数", "artifact_id": "art_2"},
    ]
    number_duplicate_source_labels(entries, lang="zh-CN")
    number_duplicate_source_labels(entries, lang="zh-CN")  # Idempotent
    assert entries[0]["label"] == "运行代码: 读数（第 1 次运行）"
    assert entries[1]["label"] == "运行代码: 读数（第 2 次运行）"

    entries.append({"tool": "python_sandbox", "label": "运行代码: 读数", "artifact_id": "art_3"})
    number_duplicate_source_labels(entries, lang="zh-CN")
    assert [e["label"] for e in entries] == [
        "运行代码: 读数（第 1 次运行）",
        "运行代码: 读数（第 2 次运行）",
        "运行代码: 读数（第 3 次运行）",
    ]


def test_a_shrunk_group_loses_its_ordinal() -> None:
    """编过号的条目后来不再撞了（子集重渲 —— `_build_sections_from_payload` 从
    `payload.sources` 重建时拿到的可能只是其中一部分），后缀必须掉，
    否则读者看到一个孤零零的「第 1 次运行」，暗示还有第 2 次。"""
    entries = [
        {"tool": "python_sandbox", "label": "运行代码: 读数（第 1 次运行）", "artifact_id": "art_1"},
        {"tool": "python_sandbox", "label": "运行代码: 品类环比", "artifact_id": "art_9"},
    ]
    number_duplicate_source_labels(entries, lang="zh-CN")
    assert entries[0]["label"] == "运行代码: 读数"


def test_distinct_sandbox_labels_are_left_alone() -> None:
    entries = [
        {"tool": "python_sandbox", "label": "运行代码: 省份排名", "artifact_id": "art_1"},
        {"tool": "python_sandbox", "label": "运行代码: 品类环比", "artifact_id": "art_2"},
    ]
    number_duplicate_source_labels(entries, lang="zh-CN")
    assert [e["label"] for e in entries] == ["运行代码: 省份排名", "运行代码: 品类环比"]


def test_run_ordinal_follows_ui_language() -> None:
    entries = [
        {"tool": "python_sandbox", "label": "Run code: load data", "artifact_id": "art_1"},
        {"tool": "python_sandbox", "label": "Run code: load data", "artifact_id": "art_2"},
    ]
    number_duplicate_source_labels(entries, lang="en-US")
    assert entries[0]["label"] == "Run code: load data (run 1)"
    assert entries[1]["label"] == "Run code: load data (run 2)"


def test_run_ordinal_defaults_to_english_without_a_language() -> None:
    entries = [
        {"tool": "python_sandbox", "label": "Run code: load data", "artifact_id": "art_1"},
        {"tool": "python_sandbox", "label": "Run code: load data", "artifact_id": "art_2"},
    ]
    number_duplicate_source_labels(entries)
    assert [e["label"] for e in entries] == ["Run code: load data (run 1)", "Run code: load data (run 2)"]


def test_malformed_entries_do_not_raise() -> None:
    entries: list[Any] = [None, {"tool": "python_sandbox"}, {"label": "x"}, 7]
    number_duplicate_source_labels(entries, lang="zh-CN")  # No crash means pass (best effort on the report-writing path).
    number_duplicate_source_labels(None)


def test_bi_query_duplicates_with_distinct_artifacts_get_ordinals() -> None:
    """同 tool 同 query、artifact 不同 ⇒ 是两次真取数，要分得开。

    `_add_entry` 按 **artifact_id** 去重，(tool, query) 那条桥只并「无 aid 的自报条目」，
    所以两个不同 artifact 的同 query 结果会并排留下两行一模一样的标签。
    116 语料实测（09-12 起 10 份报告）唯一残留的重名就是这个形态。
    """
    entries = [
        {"tool": "bi_query", "label": "查询数据: 2026年1-12月 各 种类大类 营业额", "artifact_id": "art_68bb01347847"},
        {"tool": "bi_query", "label": "查询数据: 2026年1-12月 各 种类大类 营业额", "artifact_id": "art_952147c2f4a0"},
    ]
    number_duplicate_source_labels(entries, lang="zh-CN")
    assert entries[0]["label"].endswith("（第 1 次运行）")
    assert entries[1]["label"].endswith("（第 2 次运行）")


def test_non_sandbox_duplicates_without_distinct_artifacts_are_left_alone() -> None:
    """该折叠而没折叠的，不准用序号伪装成「两次不同的取数」。

    aid 缺失或重复 ⇒ 说明上游去重漏了，编号会把 bug 盖住，比重名更难查。
    """
    missing = [
        {"tool": "bi_query", "label": "查询数据: 各省营业额", "artifact_id": None},
        {"tool": "bi_query", "label": "查询数据: 各省营业额", "artifact_id": "art_1"},
    ]
    number_duplicate_source_labels(missing, lang="zh-CN")
    assert [e["label"] for e in missing] == ["查询数据: 各省营业额"] * 2

    same = [
        {"tool": "bi_query", "label": "查询数据: 各省营业额", "artifact_id": "art_1"},
        {"tool": "bi_query", "label": "查询数据: 各省营业额", "artifact_id": "art_1"},
    ]
    number_duplicate_source_labels(same, lang="zh-CN")
    assert [e["label"] for e in same] == ["查询数据: 各省营业额"] * 2


# ── Dangling opening parentheses left by truncation ───────────────────────


def test_truncation_does_not_leave_a_dangling_open_bracket() -> None:
    """标签按长度截断时，不能把半截括号留给用户。

    116 2026-09-20 实拍：沙箱意图注释被 `[:40]` 正好切在全角 `（` 上，来源行渲成
    `运行代码: …增长/萎缩分类（`。
    """
    from bowhead_core.sources_resolver import _clip_label

    q = "各品类2026年vs2025年营业额同比、环比趋势、销售量合并及增长/萎缩分类（按同比排序）"
    assert q[:40].endswith("（"), "前提：这条真机原文正好切在左括号上，前提变了这测试就失去意义"
    assert not _clip_label(q, 40).endswith("（")
    assert _clip_label(q, 40) == q[:40].rstrip("（")


def test_dangling_bracket_plus_ordinal_does_not_render_double_parens() -> None:
    """半截括号 + 序号后缀 = `（（第 1 次运行）`，这是当初报上来的那个形态。"""
    from bowhead_core.sources_resolver import _clip_label

    q = "各品类2026年vs2025年营业额同比、环比趋势、销售量合并及增长/萎缩分类（按同比排序）"
    entries = [
        {"tool": "python_sandbox", "label": f"运行代码: {_clip_label(q, 40)}", "artifact_id": "a1"},
        {"tool": "python_sandbox", "label": f"运行代码: {_clip_label(q, 40)}", "artifact_id": "a2"},
    ]
    number_duplicate_source_labels(entries, lang="zh-CN")
    for e in entries:
        assert "（（" not in e["label"]
    assert entries[0]["label"].endswith("（第 1 次运行）")


def test_clip_label_leaves_normal_text_alone_and_never_empties() -> None:
    from bowhead_core.sources_resolver import _clip_label

    assert _clip_label("2026年各品类营业额", 40) == "2026年各品类营业额"
    assert _clip_label("（（（", 40) == "（（（", "全被剥空时退回原样，宁可难看也不要只剩工具名"
    assert _clip_label("", 40) == ""
