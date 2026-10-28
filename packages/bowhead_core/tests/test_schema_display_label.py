"""schema 的模型面投影：`display` 按本轮输出语言挑，`names` 一个字不动。

为什么分开：`names` 是拼给 datarc NL 引擎的**查询标识符**，翻译了就可能解析不出来；
`display` 只是给人看的标签。两者混用是这条改动最容易犯的错，所以每条用例都同时断言
「display 对了」**和**「names 没被动过」。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from bowhead_bi_bridge.types import DimensionMeta, MeasureMeta
from bowhead_core.tool_registry import _dimension_public, _measure_public, _out_lang


def _measure(**kw: Any) -> MeasureMeta:
    base = {"id": 1, "names": ["营业额", "sales"], "display_names": {"zh-CN": "营业额", "en-US": "GMV"}}
    base.update(kw)
    return MeasureMeta(**base)  # type: ignore[arg-type]


@pytest.mark.unit
def test_percentage_format_reaches_the_model_facing_schema() -> None:
    """★「这是不是百分比」必须出现在**模型看到的 schema** 里——agent 出表时就该知道。

    116 实测的坑在取数层（公式指标的 format 只在概念层，原先只读模型层 ⇒ 恒空）。
    取数补上后，这条事实要真的走到投影里，否则 agent 仍然不知道 0.1802 是 18.02%。
    """
    out = _measure_public(_measure(names=["利润率", "profit_margin"], value_format="percentage"), "en-US")
    assert out["format"] == "percentage"
    assert out["names"] == ["利润率", "profit_margin"], "names 被动过——查询会解析不出来"


@pytest.mark.unit
def test_english_turn_gets_english_label() -> None:
    out = _measure_public(_measure(), "en-US")
    assert out["display"] == "GMV"
    assert out["names"] == ["营业额", "sales"], "names 被动过——查询会解析不出来"


@pytest.mark.unit
def test_chinese_turn_omits_label_when_same_as_primary_name() -> None:
    """★中文轮里 display 与 names[0] 相同 ⇒ 不出这个键。

    省 token，也避免模型把它当成第二个可查询的名字。
    """
    out = _measure_public(_measure(), "zh-CN")
    assert "display" not in out


@pytest.mark.unit
def test_falls_back_to_the_other_locale() -> None:
    """只配了一种语言时用它，别退成空。"""
    out = _measure_public(_measure(display_names={"en-US": "GMV"}), "zh-CN")
    assert out["display"] == "GMV"


@pytest.mark.unit
def test_no_display_names_means_no_key() -> None:
    """★负对照：没有 displayName 的老 workspace 投影与改动前完全一致。"""
    out = _measure_public(_measure(display_names={}), "en-US")
    assert "display" not in out
    assert out["names"] == ["营业额", "sales"]


@pytest.mark.unit
def test_dimension_projection_matches_measure_rules() -> None:
    """维度走同一套规矩——只接指标那一半的话，维度名在英文轮仍是中文。"""
    d = DimensionMeta(id=48, names=["市", "city"], display_names={"zh-CN": "市", "en-US": "city"})
    en = _dimension_public(d, "en-US")
    assert en["display"] == "city"
    assert en["names"] == ["市", "city"]
    assert "display" not in _dimension_public(d, "zh-CN")


@pytest.mark.unit
def test_locale_comes_from_output_language_not_data_language() -> None:
    """★★挑的是 output_language，不是 data_language。

    三轴里 `data_language` 说的是**库里存的是什么语言**（`names` 该跟的轴），
    `output_language` 才是「用户看到的输出用什么语言」。挑错轴的话，一个
    data_language=zh-CN 的库在英文对话里仍然吐中文标签。
    """
    ctx = SimpleNamespace(language_context=SimpleNamespace(output_language="en-US", data_language="zh-CN", ui_language="zh-CN"))
    assert _out_lang(ctx) == "en-US"


@pytest.mark.unit
def test_missing_language_context_defaults_to_english() -> None:
    """A missing language context must not crash; it falls back to English (LanguageContext default)."""
    assert _out_lang(SimpleNamespace()) == "en-US"
    assert _out_lang(SimpleNamespace(language_context=None)) == "en-US"


@pytest.mark.unit
def test_schema_tool_tells_the_model_to_use_display_in_prose() -> None:
    """Configured display names apply to all user-visible prose and labels."""
    from bowhead_core.tool_registry import build_default_registry

    reg = build_default_registry()
    d = reg.get("bi_schema").schema.description
    assert "拼 query 只用 `names`" in d, "查询侧的约束没了——换名会让查询解析不出来"
    assert "用户会看到的一切文字" in d
    assert "散文" in d
    assert "有 `display` 就一律用 `display`" in d


@pytest.mark.unit
def test_compute_prompt_teaches_display_for_user_facing_text() -> None:
    """The compute prompt follows the same display-name contract as bi_schema."""
    from pathlib import Path

    md = Path(__file__).resolve().parents[1] / "src/bowhead_core/agents/prompts/system/compute.md"
    body = md.read_text(encoding="utf-8")
    assert "display" in body, "compute.md 没教 display——它会继续用中文原名写散文"
    assert "用户会看到的一切文字" in body
    # Preserve query-side constraints: replacing names prevents bi_query from resolving them.
    assert "`names` 只用来拼 `bi_query`" in body
    # Entity values must not be replaced.
    assert "实体**值**" in body
