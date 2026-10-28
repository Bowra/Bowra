"""数据里有 inf / nan：按空值画图，并告诉模型哪几格没画出来。

116 1.3.51 `t_6acd16abddee`：compute 算环比时基数为 0 的品类得出 inf，pandas 写进 CSV 是 "inf"，
`_parse_csv_with_types` 里 `int(float("inf"))` 抛 OverflowError ⇒ add_chart 两次整张失败
（`failed to parse artifact CSV: OverflowError: cannot convert float infinity to integer`）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest
from bowhead_core.canvas_tools import _add_chart, _parse_csv_with_types


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


async def _chart(csv: str, args: dict[str, Any]) -> tuple[dict[str, Any], _Canvas]:
    art = _Artifact(id="art_aaaaaaaaaaaa", filename="ranking.csv", path="ranking.csv", source="python_sandbox")
    canvas = _Canvas()
    ctx = _Ctx(artifact_store=_ArtifactStore(art, csv.encode()), canvas_store=canvas)
    out = await _add_chart({"data_artifact_id": art.id, **args}, ctx)
    return out, canvas


# The 116 turn's shape: a ranking table with inf caused by a zero baseline in the growth-rate column.
_CSV = "种类大类,平均月度环比增长率 (%),标准化趋势斜率 (%/月)\n文体,2.66,0.62\n新品类,inf,-inf\n冷饮,7.13,-1.86\n个洗用品,5.54,nan\n"
_ARGS = {
    "hint": "rank",
    "title": "各品类大类增长排名",
    "series": ["平均月度环比增长率 (%)", "标准化趋势斜率 (%/月)"],
    "sort_by": "平均月度环比增长率 (%)",
}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_inf_cells_no_longer_kill_the_chart() -> None:
    out, canvas = await _chart(_CSV, _ARGS)

    assert "error" not in out, out
    assert canvas.payload is not None
    cols = {c["key"]: c for c in canvas.payload["spec"]["columns"]}
    assert cols["平均月度环比增长率 (%)"].get("type") == "numeric"
    assert cols["标准化趋势斜率 (%/月)"].get("type") == "numeric"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_the_model_is_told_which_cells_were_not_drawn() -> None:
    """图上少了点，模型却照自己算的表写正文——note 必须点名列与格数。"""
    out, _ = await _chart(_CSV, _ARGS)

    note = out.get("note") or ""
    assert "平均月度环比增长率 (%)（1 格）" in note
    assert "标准化趋势斜率 (%/月)（2 格）" in note
    assert "基数为 0" in note


@pytest.mark.unit
@pytest.mark.asyncio
async def test_clean_data_gets_no_non_finite_note() -> None:
    """负对照：没有 inf / nan 就不该出现这句。"""
    clean = "种类大类,平均月度环比增长率 (%),标准化趋势斜率 (%/月)\n文体,2.66,0.62\n冷饮,7.13,-1.86\n个洗用品,5.54,-3.29\n"
    out, _ = await _chart(clean, _ARGS)

    assert "error" not in out, out
    assert "无穷大" not in (out.get("note") or "")


@pytest.mark.unit
def test_parser_maps_every_non_finite_spelling_to_empty() -> None:
    counts: dict[str, int] = {}
    rows, _ = _parse_csv_with_types(b"k,v\na,inf\nb,-inf\nc,Infinity\nd,nan\ne,NaN\nf,3\n", non_finite=counts)

    assert [r["v"] for r in rows] == [None, None, None, None, None, 3]
    assert counts == {"v": 5}
