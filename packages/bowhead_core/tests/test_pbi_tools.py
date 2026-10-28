"""Power BI adapter (P1) — pbi_* handler behavior (fail-closed, impersonation threading)."""

from __future__ import annotations

from bowhead_core.pbi_client import (
    PbiResult,
    PbiTurnHandle,
    PbiUnavailableHandle,
)
from bowhead_core.pbi_tools import (
    _pbi_execute_dax,
    _pbi_execute_template,
    _pbi_fetch_rows,
    _pbi_list_measures,
    _pbi_list_templates,
)


class _Ctx:
    """Minimal duck-typed ToolExecutionContext stub."""

    def __init__(self, pbi=None) -> None:
        if pbi is not None:
            self.pbi = pbi


class _FakeClient:
    def __init__(self, result: PbiResult) -> None:
        self._result = result
        self.seen: str | None = None

    async def execute_dax(self, dax: str) -> PbiResult:
        self.seen = dax
        return self._result


def _handle(result: PbiResult, catalog=None) -> PbiTurnHandle:
    return PbiTurnHandle(client=_FakeClient(result), catalog=catalog)


async def test_execute_dax_fail_closed_when_unbound() -> None:
    out = await _pbi_execute_dax({"dax": 'EVALUATE ROW("n",1)'}, _Ctx(pbi=None))
    assert out["error"] == "pbi_connection_unavailable"
    assert out["error_code"] == "pbi_connection_unavailable"
    assert out["diagnostics"]["diagnostic_code"] == "powerbi_query_failure"
    assert "PbiUnavailable" not in repr(out)


async def test_execute_dax_preserves_finite_unavailable_cause() -> None:
    out = await _pbi_execute_dax(
        {"dax": "EVALUATE X"},
        _Ctx(pbi=PbiUnavailableHandle("pbi_connection_credential_unavailable")),
    )

    assert out["error_code"] == "pbi_connection_credential_unavailable"
    assert out["diagnostics"]["safe_detail"] == ("phase=prepare; failure_class=configuration")


async def test_execute_dax_happy_uses_team_service_principal() -> None:
    h = _handle(PbiResult(columns=["v"], rows=[{"v": 42}], row_count=1))
    out = await _pbi_execute_dax({"dax": "EVALUATE X"}, _Ctx(pbi=h))
    assert out["row_count"] == 1 and out["rows"] == [{"v": 42}]
    assert h.client.seen == "EVALUATE X"


async def test_execute_dax_rejects_empty_dax() -> None:
    h = _handle(PbiResult())
    out = await _pbi_execute_dax({"dax": "   "}, _Ctx(pbi=h))
    assert out["error_code"] == "pbi_query_input_required"


async def test_execute_dax_rejects_non_string_dax() -> None:
    h = _handle(PbiResult())
    out = await _pbi_execute_dax({"dax": {"secret": "raw"}}, _Ctx(pbi=h))

    assert out["error_code"] == "pbi_query_input_required"
    assert "raw" not in repr(out)


async def test_execute_dax_surfaces_truncation_error() -> None:
    h = _handle(
        PbiResult(
            error_code="pbi_query_result_incomplete",
            truncated=True,
        ),
    )
    out = await _pbi_execute_dax({"dax": "EVALUATE BigTable"}, _Ctx(pbi=h))
    assert out["error_code"] == "pbi_query_result_incomplete"
    assert out["truncated"] is True
    assert "rows" not in out  # never hands back partial data


async def test_execute_dax_zero_rows_is_explicit() -> None:
    h = _handle(PbiResult(columns=["v"], rows=[], row_count=0))
    out = await _pbi_execute_dax({"dax": "EVALUATE X"}, _Ctx(pbi=h))
    assert out["row_count"] == 0 and out["empty"] is True


async def test_fetch_rows_uses_same_path() -> None:
    h = _handle(PbiResult(columns=["v"], rows=[{"v": 7}], row_count=1))
    out = await _pbi_fetch_rows({"dax": "EVALUATE SUMMARIZECOLUMNS()"}, _Ctx(pbi=h))
    assert out["rows"] == [{"v": 7}] and h.client.seen == "EVALUATE SUMMARIZECOLUMNS()"


async def test_list_templates_uses_team_catalog() -> None:
    out = await _pbi_list_templates({}, _Ctx(pbi=_handle(PbiResult(), catalog={"templates": [{"name": "Revenue"}]})))
    assert out == {"templates": [{"name": "Revenue"}]}


async def test_execute_template_replaces_only_declared_scalar_parameters() -> None:
    client = _FakeClient(PbiResult(columns=["v"], rows=[{"v": 1}], row_count=1))
    handle = PbiTurnHandle(client=client, catalog={"templates": [{"id": "t1", "dax_template": 'EVALUATE ROW("v", {{name}})'}]})
    out = await _pbi_execute_template({"template_id": "t1", "parameters": {"name": 'A"B'}}, _Ctx(pbi=handle))
    assert out["rows"] == [{"v": 1}]
    assert client.seen == 'EVALUATE ROW("v", "A""B")'


async def test_execute_template_rejects_missing_or_extra_parameters() -> None:
    handle = _handle(PbiResult(), catalog={"templates": [{"id": "t1", "dax_template": "EVALUATE {{value}}"}]})
    out = await _pbi_execute_template({"template_id": "t1", "parameters": {"other": "x"}}, _Ctx(pbi=handle))
    assert out["error_code"] == "pbi_query_input_required"


async def test_list_measures_fails_when_no_catalog() -> None:
    out = await _pbi_list_measures({}, _Ctx(pbi=_handle(PbiResult())))
    assert out["error_code"] == "pbi_catalog_unavailable"
    assert out["diagnostics"]["retryable"] is False


async def test_list_measures_returns_seeded_catalog() -> None:
    h = _handle(PbiResult(), catalog={"measures": [{"name": "总存款"}]})
    out = await _pbi_list_measures({}, _Ctx(pbi=h))
    assert out["measures"] == [{"name": "总存款"}]


async def test_list_measures_fail_closed_when_unbound() -> None:
    out = await _pbi_list_measures({}, _Ctx(pbi=None))
    assert out["error_code"] == "pbi_connection_unavailable"
