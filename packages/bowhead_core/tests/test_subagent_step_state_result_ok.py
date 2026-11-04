"""Sprint chat-polish-r2 G1 — _extract_result_ok + _extract_result_error tests.

5 fixture covering:
1. python_sandbox docker fail: `{ok: False, error: "docker unavailable"}`
2. skill_invoke not found: `{error: "skill '' not found"}`
3. bi_query ok: `{ok: True, results: [...]}`
4. subagent_return terminal: `{ok: True, __terminal: True, summary: {...}}`
5. Generic dict no ok signal: `{some_field: "value"}` → None (fallback to exception path)
"""

from bowhead_core.subagent import (
    _extract_result_error,
    _extract_result_ok,
)


class TestExtractResultOk:
    def test_python_sandbox_docker_fail_returns_false(self) -> None:
        """python_sandbox returns {ok: False, error: "..."} on docker timeout."""
        result = {
            "ok": False,
            "exit_code": 0,
            "stderr": "docker timeout",
            "error": "docker unavailable or image missing",
            "duration_ms": 18065,
        }
        assert _extract_result_ok(result) is False

    def test_skill_invoke_not_found_returns_false(self) -> None:
        """skill_invoke with bad name returns {error: "skill '' not found"}."""
        result = {"error": "skill '' not found"}
        assert _extract_result_ok(result) is False

    def test_bi_query_ok_returns_true(self) -> None:
        """bi_query returns {results: [...]} but no explicit ok field — should
        return None (no signal, fall back to exception path = success)."""
        result = {
            "results": [{"ok": True, "artifact_id": "art_abc", "query_text": "..."}],
        }
        assert _extract_result_ok(result) is None  # no top-level ok signal

    def test_subagent_return_terminal_ok_true(self) -> None:
        """subagent_return success path: {ok: True, __terminal: True, ...}."""
        result = {
            "ok": True,
            "__terminal": True,
            "summary": {"title": "report", "insights_count": 5},
            "message": "Payload accepted.",
        }
        assert _extract_result_ok(result) is True

    def test_no_signal_returns_none(self) -> None:
        """Tool returns dict without ok/error fields → None (caller falls back
        to exception-based logic)."""
        result = {"some_field": "value", "count": 42}
        assert _extract_result_ok(result) is None

    def test_non_dict_returns_none(self) -> None:
        """list / scalar return → None."""
        assert _extract_result_ok([{"name": "skill1"}]) is None
        assert _extract_result_ok("string result") is None
        assert _extract_result_ok(None) is None

    def test_error_kind_field_marks_failure(self) -> None:
        """bi_query uses error_kind field for clean errors."""
        result = {"error_kind": "unknown_concept", "error_message": "no matching dim"}
        assert _extract_result_ok(result) is False


class TestExtractResultError:
    def test_extracts_error_kind_and_message(self) -> None:
        result = {
            "ok": False,
            "error_kind": "docker_unavailable",
            "error_message": "image missing",
        }
        kind, msg = _extract_result_error(result)
        assert kind == "docker_unavailable"
        assert msg == "image missing"

    def test_falls_back_to_error_field(self) -> None:
        result = {"error": "skill '' not found"}
        kind, msg = _extract_result_error(result)
        assert kind is None
        assert msg == "skill '' not found"

    def test_truncates_long_message_to_500(self) -> None:
        result = {"error": "x" * 1000}
        _, msg = _extract_result_error(result)
        assert msg is not None
        assert len(msg) == 500

    def test_non_dict_returns_none_tuple(self) -> None:
        assert _extract_result_error("foo") == (None, None)
        assert _extract_result_error([1, 2, 3]) == (None, None)

    def test_handles_dict_message_value(self) -> None:
        """Some tools return {error: {message: "...", detail: "..."}}."""
        result = {"error": {"message": "datarc auth failed", "detail": "401"}}
        _, msg = _extract_result_error(result)
        assert msg == "datarc auth failed"

    def test_skill_invoke_invalid_args_remains_a_failed_attempt(self) -> None:
        """A correctable argument error still describes a rejected attempt."""
        result = {
            "error_kind": "invalid_args",
            "error": "skill_invoke requires a name; check skill_list_for_role.",
        }
        kind, msg = _extract_result_error(result)
        assert kind == "invalid_args"
        assert "name" in (msg or "")
        assert _extract_result_ok(result) is False  # both signals the classifier checks
