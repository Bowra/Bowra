"""步骤摘要（`output_preview`）必须跟 **UI 语言** 走。

user 在 116 en-US 实测：运行带的行右侧渲的是中文（`已添加 · bar`、`· 3 洞察 · 2 建议`、
`27 测度 + 8 维度`）—— 那是后端 `_prettify_output_preview` 把 UI 文案**写死成中文**
拼进 `tool_result.output_preview`，FE 只拿到成品串，无从本地化。

★ 语言从 `LanguageContext.ui_language` 来（`types.py` 的 `TurnRequest` 上早就有，
  与 `output_language`（模型答复语言）、`data_language` 分开）——**不是新契约**。

⚠ 本文件第一组是**金标**：中文侧输出必须**逐字不变**。这 26 条是改造前从真实实现
  跑出来的，不是手写的预期——改文案表时它就是「没把中文侧改坏」的唯一凭据。
"""

import pytest
from bowhead_core.subagent import _prettify_output_preview as P

CJK = __import__("re").compile(r"[一-鿿]")

# (name, tool, result, error, expected_zh)
GOLDEN: list[tuple] = [
    ("err_bi", "bi_query", None, "DatarcUnavailableError: boom", "BI 数据源在本轮不可用（说明见回复）"),
    ("err_generic", "bi_query", None, "ValueError: nope", "失败: ValueError: nope"),
    ("none", "bi_query", None, None, "无返回"),
    ("clarify", "bi_query", {"error_kind": "needs_clarification", "clarification": {"options": [1], "prompt": "选门店"}}, None, "等待你选择: 选门店"),
    ("clarify_noprompt", "bi_query", {"error_kind": "needs_clarification", "clarification": {"options": [1]}}, None, "等待你补充选择"),
    ("not_acc_dym", "bi_query", {"error_kind": "not_accessible", "did_you_mean": ["a", "b"]}, None, "未匹配到该工作区 · 你可能想找: a、b"),
    ("not_acc", "bi_query", {"error_kind": "not_accessible"}, None, "未匹配到该工作区 · 已返回可选列表"),
    ("pending", "bi_query", {"error_kind": "pending_user_confirmation"}, None, "已在等待你确认选择 · 本轮不重复询问"),
    ("refused", "bi_query", {"error_kind": "clarification_refused_on_resume"}, None, "本轮已澄清过 · 按已有答案继续"),
    ("schema", "bi_schema", {"measures": [1, 2, 3], "dimensions": [1, 2]}, None, "3 测度 + 2 维度"),
    ("web", "web_search", {"summary": "s", "results": [1, 2]}, None, "已检索 · 2 来源"),
    ("web_nosrc", "web_search", {"summary": "s", "results": []}, None, "已检索"),
    ("biq_batch", "bi_query", {"results": [{"ok": True, "total_rows": 5}, {"ok": True, "total_rows": 3}]}, None, "2 查询 · 2 成功 · 8 行"),
    ("biq_err", "bi_query", {"results": [{"ok": True, "total_rows": 5}, {"ok": False}]}, None, "2 查询 · 1 成功 · 1 出错 · 5 行"),
    ("biq_single_row", "bi_query", {"results": [{"ok": True, "sample_rows": [{"营业额": 12}]}]}, None, "1 查询 · 1 成功 · 营业额: 12"),
    ("biq_nodata", "bi_query", {"results": [{"ok": True, "row_count": 0}]}, None, "1 查询 · 1 成功 · 无数据"),
    ("biq_cols", "bi_query", {"rows": [1, 2], "columns": ["a", "b", "c"]}, None, "2 行 · 3 列"),
    # e4f261f8e (08-31): concept-recall rewrite changed each token to matches + verdict (old candidates/kind keys no longer exist;
    # old goldens always rendered "0 候选", the user-reported defect). Regenerate this row from the real implementation's new shape.
    ("concept", "bi_concept_resolve", {"results": [{"token": "t", "verdict": "ambiguous", "matches": [1, 2]}]}, None, "t 2 候选 · 有歧义"),
    ("chart", "add_chart", {"kind": "bar"}, None, "已添加 · bar"),
    ("sandbox_ok", "python_sandbox", {"outputs": [1, 2, 3]}, None, "运行成功 · 3 输出"),
    ("sandbox_err", "python_sandbox", {"error": "x"}, None, "运行成功"),
    ("skill_list", "skill_list", [1, 2], None, "2 个 skill"),
    (
        "subret",
        "subagent_return",
        {"ok": True, "summary": {"title": "T", "insights_count": 3, "recommendations_count": 2}},
        None,
        "T · 3 洞察 · 2 建议",
    ),
    ("subret_zero", "subagent_return", {"ok": True, "summary": {"title": "T", "insights_count": 0, "recommendations_count": 0}}, None, "T"),
    ("subret_rej", "subagent_return", {"ok": False, "error": "bad"}, None, "结果被退回: bad"),
    ("mem", "memory_search", {"hits": [1, 2]}, None, "找到 2 条记忆"),
    ("remember", "remember", {"ok": True, "id": "mem_85133efb3d33", "stored": "x"}, None, "已存入记忆"),
    ("recall", "recall", {"found": True, "items": [1]}, None, "找到 1 条记忆"),
]


@pytest.mark.parametrize(("name", "tool", "result", "error", "expected"), GOLDEN)
def test_zh_output_is_byte_identical(name, tool, result, error, expected):
    """Golden: the Chinese side stays byte-identical when the UI language is zh-CN."""
    assert P(tool, result, error, ui_language="zh-CN") == expected, name


@pytest.mark.parametrize(("name", "tool", "result", "error", "expected"), GOLDEN)
def test_zh_turn_context_matches_explicit(name, tool, result, error, expected):
    """With `ui_language=None`, the turn-head UI ContextVar decides; a zh-CN turn renders the golden."""
    from bowhead_core.agents._lead_context import reset_current_ui_language, set_current_ui_language

    token = set_current_ui_language("zh-CN")
    try:
        assert P(tool, result, error) == expected, name
    finally:
        reset_current_ui_language(token)


@pytest.mark.parametrize(("name", "tool", "result", "error", "expected"), GOLDEN)
def test_unset_ui_language_defaults_to_english(name, tool, result, error, expected):
    """No language context ⇒ English product copy (docs/design/language-policy.md, "AI answers":
    without browser context or saved preferences, deterministic product copy uses English)."""
    assert P(tool, result, error) == P(tool, result, error, ui_language="en-US"), name


# Backend **UI words** inserted into summaries, not data. None may appear in English.
# ⚠ Deliberately do not require the whole string to lack CJK: the column name in `营业额: 12` and the prompt in `等待你选择: 选门店`
# are **data** and must remain verbatim; a blanket check would mistake Chinese data for missing translation.
UI_WORDS = [
    "无返回",
    "失败",
    "等待你",
    "未匹配到该工作区",
    "已在等待",
    "本轮已澄清",
    "测度",
    "维度",
    "已检索",
    "来源",
    "查询",
    "成功",
    "出错",
    "空行",
    "无数据",
    "行",
    "列",
    "候选",
    "找到",
    "条",
    "表",
    "已添加",
    "运行出错",
    "运行成功",
    "输出",
    "派",
    "已调用",
    "个 skill",
    "拒收",
    "洞察",
    "建议",
    "已交",
    "记忆",
    "写入失败",
    "已记",
    "已存入",
    "未存入",
    "没有相关",
    "置信",
    "无法回答",
    "已答",
    "项",
    "已查",
    "异步",
    "结果已取",
    "已写",
    "不可用",
]


@pytest.mark.parametrize(("name", "tool", "result", "error", "expected_zh"), GOLDEN)
def test_en_has_no_chinese_ui_words(name, tool, result, error, expected_zh):
    """英文界面：摘要里不许出现任何中文 **UI 词**（数据里的中文不算）。"""
    out = P(tool, result, error, ui_language="en-US")
    hits = [w for w in UI_WORDS if w in out]
    assert not hits, f"{name}: {out!r} 仍含 UI 词 {hits}"


def test_en_and_zh_actually_differ_where_it_matters():
    """★ 正对照：别把英文侧做成「原样返回中文」也能过 —— 至少这几条必须真的变了。"""
    for tool, res in [
        ("bi_schema", {"measures": [1], "dimensions": [1]}),
        ("add_chart", {"kind": "bar"}),
        ("subagent_return", {"ok": True, "summary": {"title": "T", "insights_count": 2, "recommendations_count": 1}}),
    ]:
        zh = P(tool, res, None, ui_language="zh-CN")
        en = P(tool, res, None, ui_language="en-US")
        assert zh != en, f"{tool}: en 与 zh 相同（{zh!r}）"
        assert not CJK.search(en), f"{tool}: en 仍有中文 {en!r}"


@pytest.mark.parametrize("lang", ["zh", "zh-CN", "zh-TW", "ZH-cn"])
def test_zh_prefix_variants_are_chinese(lang):
    """判据只看前缀：zh / zh-CN / zh-TW 都算中文。"""
    assert P("add_chart", {"kind": "bar"}, None, ui_language=lang) == "已添加 · bar"


@pytest.mark.parametrize("lang", ["en", "en-US", "en-GB", "fr-FR"])
def test_non_zh_is_english(lang):
    """其余语言一律走英文（目前只有 zh / en 两套）。"""
    assert P("add_chart", {"kind": "bar"}, None, ui_language=lang) == "Added · bar"


# ── live == persist == replay ────────────────────────────────────────────
# Independent review HIGH: `_persist_turn_state` runs **after** run_turn's finally;
# the ContextVar set at turn start has already reset to zh-CN. Without explicit language, persistence is Chinese while
# live is English; persist_turn_trace uses INSERT OR REPLACE and overwrites the earlier correct version.
# ⇒ en-US users see the entire trace in Chinese after F5. This is the chat invariant named in CLAUDE.md.


def test_persist_path_threads_ui_language():
    """落库用的摘要必须跟 live 同语言（显式传参，不靠 ContextVar）。"""
    from bowhead_core.turn_runner import _invocations_to_trace_rows
    from bowhead_core.types import ToolInvocation

    inv = ToolInvocation(
        tool_name="add_chart",
        arguments={},
        result={"kind": "bar"},
        started_at="2026-01-01T00:00:00Z",
        ended_at="2026-01-01T00:00:01Z",
    )
    rows_en = _invocations_to_trace_rows(
        turn_id="t1",
        intent=None,
        invocations=[inv],
        ui_language="en-US",
    )
    rows_zh = _invocations_to_trace_rows(
        turn_id="t1",
        intent=None,
        invocations=[inv],
        ui_language="zh-CN",
    )
    en = [getattr(r, "summary", "") for r in rows_en if getattr(r, "summary", "")]
    zh = [getattr(r, "summary", "") for r in rows_zh if getattr(r, "summary", "")]
    assert any("Added" in s for s in en), en
    assert not any(CJK.search(s) for s in en), en
    # ★ Positive control: Chinese stays Chinese (do not force all persistence to English).
    assert any("已添加" in s for s in zh), zh


def test_persist_signature_accepts_ui_language_keyword():
    """签名闸：`_persist_turn_state` 必须收 `ui_language` —— 少了它调用点就悄悄退回默认。"""
    import inspect

    from bowhead_core.turn_runner import _persist_turn_state

    assert "ui_language" in inspect.signature(_persist_turn_state).parameters
