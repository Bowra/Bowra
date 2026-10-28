"""Sprint ι (PRD §4.A.2 / §1.5): _map_code_block 3 trigger paths + turn_id plumbing."""

from __future__ import annotations

from bowhead_core.canvas_composers.mappers import MapperInput, _map_code_block
from bowhead_core.types import IntentType, ToolInvocation


def _mi(invs: list[ToolInvocation], turn_id: str = "t_test_iota_1") -> MapperInput:
    return MapperInput(
        intent=IntentType.COMPUTE,
        question="",
        tool_invocations=invs,
        turn_id=turn_id,
    )


# ─── trigger 1: python_sandbox ──────────────────────────────────
def test_python_sandbox_invoke_form() -> None:
    """skill.invoke(name="python_sandbox") form."""
    inv = ToolInvocation(
        tool_name="skill_invoke",
        arguments={
            "name": "python_sandbox",
            "input": {
                "code": "import pandas as pd\ndf = pd.read_csv(inp)\nprint(df.head())",
                "inputs": {"inp": {"artifact_id": "art_in_1", "filename": "source.csv", "mime": "text/csv"}},
            },
        },
        result={
            "stdout": "first 5 rows...",
            "result_json": {
                "outputs": [
                    {"artifact_id": "art_out_1", "name": "summary.csv"},
                ],
            },
        },
        duration_ms=842,
    )
    out = _map_code_block(_mi([inv]))
    assert len(out) == 1
    p = out[0]
    assert p["lang"] == "python"
    assert p["call_id"] == inv.call_id
    assert p["turn_id"] == "t_test_iota_1"
    assert p["dur_ms"] == 842
    assert "import pandas" in p["code"]
    assert p["input_artifact_ids"] == ["art_in_1"]
    assert p["output_artifact_ids"] == ["art_out_1"]
    assert p["input_artifacts"] == [
        {"artifact_id": "art_in_1", "filename": "source.csv", "mime": "text/csv"},
    ]
    assert p["output_artifacts"] == [
        {"artifact_id": "art_out_1", "filename": "summary.csv"},
    ]
    assert p["stdout_preview"] == "first 5 rows..."
    assert p["summary_payload"]["dur_ms"] == 842
    # legacy alias for sql-block compat
    assert p["sql"] == p["code"]


def test_python_sandbox_direct_form() -> None:
    """direct python_sandbox tool name."""
    inv = ToolInvocation(
        tool_name="python_sandbox",
        arguments={"code": "x = 1", "inputs": {}},
        result={"stdout": "", "outputs": []},
        duration_ms=10,
    )
    out = _map_code_block(_mi([inv]))
    assert len(out) == 1
    assert out[0]["lang"] == "python"
    assert out[0]["code"] == "x = 1"


def test_python_sandbox_skip_when_no_code() -> None:
    inv = ToolInvocation(
        tool_name="python_sandbox",
        arguments={"code": "", "inputs": {}},
        result={"stdout": "", "outputs": []},
    )
    out = _map_code_block(_mi([inv]))
    assert out == []


# ─── trigger 2: bi.ask_question with explanations_sql ───────────
def test_bi_ask_question_emits_sql() -> None:
    inv = ToolInvocation(
        tool_name="bi_ask_question",
        arguments={"text": "近 7 天营业额"},
        result={"explanations_sql": ["SELECT SUM(amount) FROM sales WHERE date >= ..."]},
        duration_ms=300,
    )
    out = _map_code_block(_mi([inv]))
    assert len(out) == 1
    assert out[0]["lang"] == "sql"
    assert out[0]["call_id"] == inv.call_id
    assert out[0]["turn_id"] == "t_test_iota_1"
    assert "SELECT" in out[0]["code"]


def test_bi_ask_question_multi_sql_distinct_call_id_suffix() -> None:
    inv = ToolInvocation(
        tool_name="bi_ask_question",
        arguments={"text": "复合查询"},
        result={"explanations_sql": ["SELECT 1", "SELECT 2"]},
    )
    out = _map_code_block(_mi([inv]))
    assert len(out) == 2
    # Second one gets #sql1 suffix to avoid call_id collision
    assert out[0]["call_id"] == inv.call_id
    assert out[1]["call_id"] == f"{inv.call_id}#sql1"


# ─── trigger 3: bi.attribute synthetic-SQL fallback ─────────────
def test_bi_attribute_synthetic_sql_fallback() -> None:
    inv = ToolInvocation(
        tool_name="bi_attribute",
        arguments={"question": "为什么 4 月销售下降", "comparison_type": "ring"},
        result={"rows": [{"dim": "a"}, {"dim": "b"}]},
    )
    out = _map_code_block(_mi([inv]))
    assert len(out) == 1
    assert out[0]["lang"] == "sql"
    assert out[0]["meta"].get("kind") == "pseudo_attribute"
    assert "环比" in out[0]["code"]
    assert out[0]["turn_id"] == "t_test_iota_1"


def test_bi_attribute_with_explanations_sql_uses_real() -> None:
    inv = ToolInvocation(
        tool_name="bi_attribute",
        arguments={"question": "Q"},
        result={"explanations_sql": ["SELECT a FROM t"], "rows": []},
    )
    out = _map_code_block(_mi([inv]))
    assert len(out) == 1
    assert out[0]["code"] == "SELECT a FROM t"
    # NOT pseudo_attribute meta kind when real SQL present
    assert out[0]["meta"].get("kind") != "pseudo_attribute"


# ─── error / non-trigger paths ──────────────────────────────────
def test_error_inv_skipped() -> None:
    inv = ToolInvocation(
        tool_name="python_sandbox",
        arguments={"code": "x"},
        result=None,
        error="boom",
    )
    out = _map_code_block(_mi([inv]))
    assert out == []


def test_unrelated_tool_skipped() -> None:
    inv = ToolInvocation(
        tool_name="bi_list_measures",
        arguments={},
        result={"items": []},
    )
    out = _map_code_block(_mi([inv]))
    assert out == []


# ─── turn_id plumbing default ───────────────────────────────────
def test_turn_id_default_empty_when_not_passed() -> None:
    inv = ToolInvocation(
        tool_name="python_sandbox",
        arguments={"code": "x = 1"},
        result={"stdout": "", "outputs": []},
    )
    mi = MapperInput(
        intent=IntentType.COMPUTE,
        question="",
        tool_invocations=[inv],
        # turn_id not provided
    )
    out = _map_code_block(mi)
    assert len(out) == 1
    assert out[0]["turn_id"] == ""


# ─── stdout scrubbing ───────────────────────────────────────────
def test_stdout_pii_scrubbed() -> None:
    inv = ToolInvocation(
        tool_name="python_sandbox",
        arguments={"code": "print(x)"},
        result={"stdout": "用户 alice@example.com 已注册", "outputs": []},
    )
    out = _map_code_block(_mi([inv]))
    assert len(out) == 1
    assert "[REDACTED:邮箱]" in out[0]["stdout_preview"]
    assert "alice@example.com" not in out[0]["stdout_preview"]
