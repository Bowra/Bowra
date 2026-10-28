"""Verify percentage shape through the full add_chart pipeline.

Calculated percentage points must not be scaled a second time. Cover parsing,
projection, metadata enrichment, inference, and persisted column markers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest
from bowhead_core.canvas_tools import _add_chart


@dataclass
class _Ctx:
    turn_id: str = "t_test"
    user_id: str = "u"
    workspace_id: str = "w"
    session_id: str = "s1"
    artifact_store: Any = None
    canvas_store: Any = None
    parent_bus: Any = None
    extras: dict[str, Any] = field(default_factory=dict)


@dataclass
class _Artifact:
    id: str
    filename: str
    path: str
    source: str
    mime: str = "text/csv"


class _Backend:
    def __init__(self, files: dict[str, bytes]) -> None:
        self.files = files

    async def read(self, path: str) -> bytes:
        return self.files[path]


class _ArtifactStore:
    def __init__(self, art: _Artifact, body: bytes) -> None:
        self.art = art
        self.backend = _Backend({art.path: body})

    async def get(self, artifact_id: str, *, check_created_by: str | None = None) -> _Artifact | None:
        return self.art if artifact_id == self.art.id else None

    async def list_for_session(self, **_: Any) -> list[_Artifact]:
        return [self.art]


class _Canvas:
    def __init__(self) -> None:
        self.payload: dict[str, Any] | None = None

    async def ensure_canvas(self, **_: Any) -> Any:
        return SimpleNamespace(id="cv_test")

    async def list_sections_appended_by_turn(self, *_: Any, **__: Any) -> list[Any]:
        return []

    async def append_section(self, **kwargs: Any) -> Any:
        self.payload = kwargs["payload"]
        return SimpleNamespace(id="sec_test", payload=kwargs["payload"], seq=1, rev=1)


async def _chart(csv: str, *, source: str, args: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """跑一遍真 `_add_chart`，返回落库 payload 里的列（按 key）。"""
    art = _Artifact(id="art_aaaaaaaaaaaa", filename="out.csv", path="out.csv", source=source)
    canvas = _Canvas()
    ctx = _Ctx(artifact_store=_ArtifactStore(art, csv.encode()), canvas_store=canvas)
    out = await _add_chart({"data_artifact_id": art.id, **args}, ctx)
    assert "error" not in out, out
    assert canvas.payload is not None
    return {c["key"]: c for c in canvas.payload["spec"]["columns"]}


@pytest.mark.asyncio
async def test_unsafe_numeric_plot_is_actionable_and_table_preserves_exact_tokens() -> None:
    artifact = _Artifact(id="art_aaaaaaaaaaaa", filename="out.csv", path="out.csv", source="upload")
    canvas = _Canvas()
    context = _Ctx(
        artifact_store=_ArtifactStore(artifact, b"category,amount\nA,9007199254740993\nB,9007199254740995\n"),
        canvas_store=canvas,
    )
    result = await _add_chart({"data_artifact_id": artifact.id, "force_kind": "bar", "hint": "compare"}, context)
    assert result["error_kind"] == "unsupported_exact_numeric_plot"
    assert "force_kind=table" in result["error"]
    assert canvas.payload is None

    result = await _add_chart({"data_artifact_id": artifact.id, "force_kind": "table", "hint": "raw"}, context)
    assert "error" not in result, result
    assert canvas.payload is not None
    assert [row["amount"] for row in canvas.payload["spec"]["rows"]] == ["9007199254740993", "9007199254740995"]


# Synthetic rows include fractional, scaled, and negative growth values.
_SCALED_CSV = "门店,营业额同比增长率,增长率百分点\n示例店甲,0.0064,0.64\n示例店乙,0.0042,0.42\n示例店丙,0.0018,0.18\n示例店丁,-0.0025,-0.25\n"
_SCALED_ARGS = {
    "hint": "raw",
    "title": "各门店营业额同比增长率排名",
    "force_kind": "table",
    "series": ["营业额同比增长率", "增长率百分点"],
    "column_types": {"营业额同比增长率": "percent_fraction"},
}


@pytest.mark.asyncio
async def test_undeclared_column_in_sandbox_data_is_not_guessed() -> None:
    """An undeclared scaled column in sandbox data must not be guessed as a fraction."""
    cols = await _chart(_SCALED_CSV, source="python_sandbox", args=_SCALED_ARGS)
    assert cols["增长率百分点"].get("percent_shape_unknown") is True
    assert "percent_scale" not in cols["增长率百分点"]
    # Columns declared in the same call: preserve their shape without marking.
    rate = cols["营业额同比增长率"]
    assert (rate.get("format"), rate.get("percent_scale"), rate.get("percent_shape_unknown")) == (
        "percent",
        "fraction",
        None,
    )
    # Do not mark category columns (harmless, but keep persisted fields clean).
    assert cols["门店"].get("percent_shape_unknown") is None


@pytest.mark.asyncio
async def test_percent_text_column_counts_as_a_declaration() -> None:
    """Percentage text declares points and must retain its shape below one percent."""
    csv = "门店,增长率\n示例店甲,0.51%\n示例店乙,0.29%\n示例店丙,0.29%\n"
    cols = await _chart(csv, source="python_sandbox", args={"hint": "rank", "title": "各门店增长率"})
    assert cols["增长率"].get("percent_shape_unknown") is None
    assert cols["增长率"].get("unit") == "%"
    # ★ Recording units alone is insufficient: all three values are **below 1%**; frontend range guessing scales them ×100 (0.51% → 51.0%).
    # Only the code stripping % knows the shape with certainty; record the shape too.
    assert cols["增长率"].get("percent_scale") == "points"
    assert cols["增长率"].get("format") == "percent"


@pytest.mark.asyncio
async def test_bi_without_metadata_has_unknown_percentage_shape() -> None:
    """Source identity alone does not establish a percentage interpretation."""
    csv = "事业部,销售目标达成率\nDivision A,1.25\nDivision B,1.04\nDivision C,0.95\n"
    cols = await _chart(csv, source="bi_fetch_rows", args={"hint": "rank", "title": "各事业部达成率"})
    assert cols["销售目标达成率"].get("percent_shape_unknown") is True


@pytest.mark.asyncio
async def test_upload_without_metadata_has_unknown_percentage_shape() -> None:
    """An uploaded file follows the same source-independent semantics."""
    csv = "品牌,占比\nA,0.41\nB,0.59\n"
    cols = await _chart(csv, source="upload", args={"hint": "share", "title": "品牌占比"})
    assert cols["占比"].get("percent_shape_unknown") is True


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["bi_fetch_rows", "python_sandbox", "upload"])
async def test_valid_percent_declarations_survive_coincidentally_hundredfold_values(source: str) -> None:
    columns = await _chart(
        _SCALED_CSV,
        source=source,
        args={**_SCALED_ARGS, "column_types": {"营业额同比增长率": "percent_fraction", "增长率百分点": "percent_fraction"}},
    )
    for key in ("营业额同比增长率", "增长率百分点"):
        assert columns[key]["percent_scale"] == "fraction"


@pytest.mark.asyncio
async def test_every_percent_unit_column_also_declares_its_shape() -> None:
    """★ 契约：凡是被判成「单位是 %」的列，**必须**同时带上形状声明。

    这是「修过很多次」的那一族的收口闸：前端判档链第 ① 档只认 `percent_scale`，
    拿不到就落到按整列值域猜——而值域这一档在**数值都很小**时会把 points 猜成 fraction，
    屏幕上放大 100 倍。值域三档都覆盖：全部 <1%、跨 1%、全部 >1%。
    """
    shapes = {
        "全部小于1%": "0.35%\n0.80%\n0.12%\n",
        "跨过1%": "0.35%\n1.20%\n12.50%\n",
        "全部大于1%": "15.37%\n22.10%\n31.40%\n",
        "带正负号": "+1.50%\n-0.35%\n2.00%\n",
    }
    for name, body in shapes.items():
        cols = await _chart(
            "门店,退款率\n" + "".join(f"S{i},{v}\n" for i, v in enumerate(body.strip().split("\n"))),
            source="python_sandbox",
            args={"hint": "rank", "title": "退款率"},
        )
        col = cols["退款率"]
        assert col.get("unit") == "%", f"{name}: 单位没认出来"
        assert col.get("percent_scale") == "points", f"{name}: 认了单位却没声明形状 —— 前端只能去猜"


@pytest.mark.asyncio
async def test_mixed_column_of_plain_and_grouped_numbers_is_numeric() -> None:
    """★ 混列：BI 导出常只给大数字加千分位（`1,101,000.00` 与 `500` 同列，116 实测 12 列）。

    此前只要列里有一个真数值就整列不动 ⇒ 判成类目列 ⇒ **这列再也进不了数值轴**。
    """
    cols = await _chart('门店,营业额\nA,"1,101,000.00"\nB,500\nC,"2,202,000.50"\n', source="bi_fetch_rows", args={"hint": "rank", "title": "营业额"})
    assert cols["营业额"].get("type") == "numeric"


@pytest.mark.asyncio
async def test_percent_text_mixed_with_bare_numbers_stays_untouched() -> None:
    """负对照：`13.5%` 与裸数 `13.5` 同列时形状推不出来（116 实测 7 列），整列不动、交给前端判。"""
    cols = await _chart("门店,占比\nA,13.5%\nB,13.5\nC,20%\n", source="bi_fetch_rows", args={"hint": "rank", "title": "占比"})
    assert cols["占比"].get("unit") is None
    assert cols["占比"].get("percent_scale") is None


@pytest.mark.asyncio
async def test_signed_grouped_numbers_parse_like_the_frontend_does() -> None:
    """前后端对同一个写法必须解析成同一个结果。

    前端 `coerceNumericCell`（`view-transforms.ts`）一直认 `+1,234.50`，后端此前只认 `-`：
    同一列走「建图」与走「展开/细看补行」两条路会得到不同的类型（一条数值、一条文字）。
    116 上这种写法为 0 例，纯粹是把两处口径抹平。
    """
    cols = await _chart('门店,金额\nA,"+1,234.50"\nB,"-2,345.00"\nC,"3,456.00"\n', source="bi_fetch_rows", args={"hint": "rank", "title": "金额"})
    assert cols["金额"].get("type") == "numeric"


@pytest.mark.asyncio
async def test_a_blank_cell_does_not_kill_the_chart() -> None:
    """★★ 复审逮到的 P0（本批引入）：列里有个纯空白格 `"   "` 就抛 ValueError，整张图失败。

    过滤时按 `strip()` 判空、写回时只判 `""`，两处口径不同源 ⇒ `float("")` 炸。
    归一成 None 之后这列照样是数值列（空格是缺数据，不是类目）。
    """
    cols = await _chart(
        '门店,营业额\nA,"1,101,000.00"\nB,"   "\nC,"2,202,000.50"\n', source="bi_fetch_rows", args={"hint": "rank", "title": "营业额"}
    )
    assert cols["营业额"].get("type") == "numeric"

    pct = await _chart('门店,退款率\nA,15.37%\nB,"  "\nC,22.10%\n', source="python_sandbox", args={"hint": "rank", "title": "退款率"})
    assert pct["退款率"].get("type") == "numeric"
    assert pct["退款率"].get("percent_scale") == "points"


@pytest.mark.asyncio
async def test_percent_text_beats_a_contradicting_model_declaration() -> None:
    """★ 文本里写着 `0.35%` 是机器事实，模型说它是小数时以事实为准。

    `contradicts_fraction` 那道闸拦不住这一形——它要求整列 max>1.5，而这类值恰恰都很小
    （0.35 看起来像个合法的小数）。不修就仍然 ×100：0.35% 渲成 35%。
    """
    cols = await _chart(
        "门店,退款率\nA,0.35%\nB,0.80%\nC,0.12%\n",
        source="python_sandbox",
        args={"hint": "rank", "title": "退款率", "column_types": {"退款率": "percent_fraction"}},
    )
    assert cols["退款率"].get("percent_scale") == "points"


@pytest.mark.asyncio
async def test_percent_text_column_keeps_its_shape_when_a_sibling_is_hundredfold() -> None:
    """★ 「两列差 100 倍」那道闸只该丢**声明**，不该把文本里剥出来的事实一起丢掉。"""
    # That gate fires only for an exact 100× rowwise difference: `0.35%` strips to 0.35; the sibling column is 35.
    cols = await _chart(
        "门店,率,率放大\nA,0.35%,35\nB,0.80%,80\nC,0.12%,12\n",
        source="python_sandbox",
        args={"hint": "rank", "title": "率", "column_types": {"率放大": "percent_points"}},
    )
    assert cols["率"].get("percent_scale") == "points", "文本剥出来的事实不该被丢"
    assert cols["率放大"].get("percent_scale") == "points", "Coincidentally proportional values do not contradict declared semantics"
    assert cols["率放大"].get("percent_shape_unknown") is None


@pytest.mark.asyncio
async def test_percent_text_column_is_not_also_marked_unknown() -> None:
    """★ 同一列不能既写着形状、又打「别猜」标——两个消费方会读出相反的结论。"""
    cols = await _chart(
        "门店,率\nA,0.35%\nB,0.80%\nC,0.12%\n",
        source="python_sandbox",
        args={"hint": "rank", "title": "率", "column_types": {"率": "numeric"}},
    )
    col = cols["率"]
    assert col.get("percent_scale") == "points"
    assert col.get("percent_shape_unknown") is None
