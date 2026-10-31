"""LaTeX compilation isolation, bounded repair diagnostics and native dispatch."""

import importlib.util
import json
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from bowhead_skill.tools import build_skill_tools

from bowhead_skill.catalog import SkillCatalog, builtin_catalog_dir

_PATH = builtin_catalog_dir() / "latex-report/scripts/compile_report.py"


@pytest.fixture
def compiler():
    spec = importlib.util.spec_from_file_location("latex_compiler_under_test", _PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("source", [None, "", "\x00", "\ud800"])
def test_invalid_source_never_starts_compiler(compiler, tmp_path, monkeypatch, source):
    run = AsyncMock()
    monkeypatch.setattr(compiler.subprocess, "run", run)
    assert compiler.compile_report(source, tmp_path)["cause"] == "invalid_source"
    assert not run.called
    assert not (tmp_path / "report.pdf").exists()


def test_missing_runtime_preserves_source(compiler, tmp_path, monkeypatch):
    monkeypatch.setattr(compiler.shutil, "which", lambda _: None)
    source = "\\documentclass{article}\nChinese source: 中文"
    result = compiler.compile_report(source, tmp_path)
    assert result["cause"] == "runtime_unavailable"
    assert (tmp_path / "main.tex").read_text() == source
    assert not (tmp_path / "report.pdf").exists()


def test_existing_output_symlink_is_not_followed(compiler, tmp_path):
    target = tmp_path / "missing-target"
    (tmp_path / "main.tex").symlink_to(target)
    assert compiler.compile_report("source", tmp_path)["cause"] == "output_exists"
    assert not target.exists()


def _runtime(monkeypatch, compiler, callback):
    monkeypatch.setattr(compiler.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(compiler.subprocess, "run", callback)


def test_later_failed_pass_never_promotes_earlier_pdf(compiler, tmp_path, monkeypatch):
    passes = []

    def run(argv, **kwargs):
        if argv[0] != "xelatex":
            return SimpleNamespace(returncode=0)
        passes.append(argv)
        assert kwargs["cwd"] == tmp_path / ".latex-build"
        assert kwargs["env"]["openout_any"] == "p"
        assert "-no-shell-escape" in argv
        (kwargs["cwd"] / "main.pdf").write_bytes(b"unfinished PDF")
        kwargs["stdout"].write(b"Rerun" if len(passes) == 1 else b"main.tex:7: Undefined control sequence.\n" * 1000)
        return SimpleNamespace(returncode=0 if len(passes) == 1 else 1)

    _runtime(monkeypatch, compiler, run)
    result = compiler.compile_report("source", tmp_path)
    assert result["cause"] == "source_error"
    assert result["compile_passes"] == 2
    assert result["compilation_complete"] is False
    assert "next_action" not in result
    assert "main.tex:7" in result["error"]
    assert len(result["error"]) <= 2000
    assert not (tmp_path / "report.pdf").exists()
    assert (tmp_path / "main.tex").read_text() == "source"


def test_reference_pass_limit_and_shared_deadline(compiler, tmp_path, monkeypatch):
    budgets = []
    clock = iter(range(20))
    monkeypatch.setattr(compiler.time, "monotonic", lambda: next(clock))

    def run(argv, **kwargs):
        budgets.append(kwargs["timeout"])
        kwargs["stdout"].write(b"Rerun to get cross-references right.")
        return SimpleNamespace(returncode=0)

    _runtime(monkeypatch, compiler, run)
    result = compiler.compile_report("source", tmp_path, timeout_sec=120)
    assert result["cause"] == "unresolved_references"
    assert result["compile_passes"] == 3
    assert all(a > b for a, b in zip(budgets, budgets[1:], strict=False))
    assert not (tmp_path / "report.pdf").exists()


def test_timeout_retains_source_without_retry(compiler, tmp_path, monkeypatch):
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    _runtime(monkeypatch, compiler, run)
    result = compiler.compile_report("source", tmp_path)
    assert result["cause"] == "execution_timeout"
    assert len(calls) == 1
    assert (tmp_path / "main.tex").exists()
    assert not (tmp_path / "report.pdf").exists()


@pytest.mark.parametrize("multiruntime", ["0", "1"])
async def test_builtin_latex_routes_through_sandbox_and_preserves_grant(monkeypatch, multiruntime):
    monkeypatch.setenv("BOWHEAD_SKILL_MULTIRUNTIME", multiruntime)
    monkeypatch.setenv("BOWHEAD_SKILL_INVOKE_COMMAND", "0")
    runtime = SimpleNamespace(
        run=AsyncMock(
            return_value=SimpleNamespace(
                ok=False,
                exit_code=1,
                stdout="",
                stderr="",
                result_json={"skill_result": {"ok": False, "cause": "source_error"}},
                outputs=[],
                workdir=None,
                duration_ms=1,
                error=None,
            )
        )
    )
    grant = AsyncMock(return_value=None)
    catalog = SkillCatalog(roots=[builtin_catalog_dir()])
    tools = {tool.name: tool for tool in build_skill_tools(catalog, runtime, authorize_invoke=grant)}
    ctx = SimpleNamespace(user_id="u_test", team_id=None, session_id="s_test", turn_id="t_test", workspace_id="ws_test")
    arguments = {"name": "latex-report", "script": "compile_report.py", "input": {"source": "source"}}
    result = await tools["skill_invoke"].handler(arguments, ctx)
    assert not result["ok"]
    grant.assert_awaited_with("latex-report", ctx)
    call = runtime.run.call_args
    assert call.args[0].name == "python_sandbox"
    assert call.kwargs["entry_script"] == "runner.py"
    payload = call.kwargs["input_data"]
    assert payload["_skill_entry"] == "compile_report.py"
    assert payload["_requires_isolated_runtime"] is True
    assert payload["_output_purpose"] == "deliverable"
    assert payload["_deliverable_outputs"] == ["main.tex", "report.pdf"]
    assert (await tools["skill_read"].handler({"name": "latex-report"}, ctx))["script_execution"] == "sandbox"
    runtime.run.reset_mock()
    override = await tools["skill_invoke"].handler({**arguments, "extra_args": ["--input", "/tmp/other.json"]}, ctx)
    assert override["error_kind"] == "invalid_args"
    runtime.run.assert_not_awaited()
    grant.return_value = {"error": "Skill access revoked"}
    assert (await tools["skill_invoke"].handler(arguments, ctx))["error"] == "Skill access revoked"
    runtime.run.assert_not_awaited()


def test_loaded_rerunfilecheck_package_is_not_an_unresolved_reference(compiler, tmp_path, monkeypatch):
    from pypdf import PdfWriter

    compiler_passes = []

    def run(argv, **kwargs):
        if argv[0] == "xelatex":
            compiler_passes.append(argv)
            kwargs["stdout"].write(b"(/tex/latex/rerunfilecheck/rerunfilecheck.sty)\n")
            writer = PdfWriter()
            writer.add_blank_page(width=612, height=792)
            with (kwargs["cwd"] / "main.pdf").open("wb") as stream:
                writer.write(stream)
        elif argv[0] == "pdftoppm":
            (kwargs["cwd"] / "page-1.png").write_bytes(b"renderer output")
        return SimpleNamespace(returncode=0)

    _runtime(monkeypatch, compiler, run)
    result = compiler.compile_report("source", tmp_path)
    assert result["ok"] is True
    assert result["compile_passes"] == 2
    assert len(compiler_passes) == 2
    assert result["compilation_complete"] is True
    assert result["next_action"]
    assert (tmp_path / "report.pdf").is_file()


@pytest.mark.parametrize("multiruntime", ["0", "1"])
async def test_latex_completion_guidance_reaches_the_model(monkeypatch, multiruntime):
    monkeypatch.setenv("BOWHEAD_SKILL_MULTIRUNTIME", multiruntime)
    monkeypatch.setenv("BOWHEAD_SKILL_INVOKE_COMMAND", "0")
    completion = {
        "ok": True,
        "source": "main.tex",
        "pdf": "report.pdf",
        "compile_passes": 2,
        "compilation_complete": True,
        "next_action": "Review and deliver the retained source/PDF; compiler passes are complete.",
    }
    runtime = SimpleNamespace(
        run=AsyncMock(
            return_value=SimpleNamespace(
                ok=True,
                exit_code=0,
                stdout="",
                stderr="",
                result_json={"ok": True, "skill_result": completion},
                outputs=[],
                workdir=None,
                duration_ms=1,
                error=None,
            )
        )
    )
    catalog = SkillCatalog(roots=[builtin_catalog_dir()])
    tool = next(tool for tool in build_skill_tools(catalog, runtime) if tool.name == "skill_invoke")
    ctx = SimpleNamespace(user_id="u_test", team_id=None, session_id="s_test", turn_id="t_test", workspace_id="ws_test")
    result = await tool.handler({"name": "latex-report", "script": "compile_report.py", "input": {"source": "source"}}, ctx)
    assert result["ok"] is True
    assert result["result_json"] == completion
    runtime.run.assert_awaited_once()


def test_isolated_skill_cannot_use_host_subprocess(tmp_path, monkeypatch):
    path = builtin_catalog_dir() / "python_sandbox/scripts/runner.py"
    spec = importlib.util.spec_from_file_location("latex_host_guard_under_test", path)
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    monkeypatch.setenv("BOWHEAD_SANDBOX_BACKEND", "subprocess")
    execution = AsyncMock()
    monkeypatch.setattr(runner, "_run_subprocess", execution)
    input_path = tmp_path / "input.json"
    input_path.write_text(json.dumps({"code": "pass", "_requires_isolated_runtime": True}))
    monkeypatch.setattr(sys, "argv", ["runner.py", "--input", str(input_path), "--workdir", str(tmp_path)])
    runner.main()
    result = json.loads((tmp_path / "result.json").read_text())
    assert result["cause"] == "isolated_runtime_required"
    assert result["execution_state"] == "not_started"
    execution.assert_not_called()


def test_bundled_skill_staging_threads_effective_budget_without_credentials(tmp_path, monkeypatch):
    path = builtin_catalog_dir() / "python_sandbox/scripts/runner.py"
    spec = importlib.util.spec_from_file_location("latex_runner_budget_under_test", path)
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    monkeypatch.setenv("BOWHEAD_SANDBOX_BACKEND", "executor")
    observed = []

    def execute(_url, _token, **kwargs):
        observed.append(json.loads((kwargs["workdir"] / "input.json").read_text()))
        assert (kwargs["workdir"] / "skill/scripts/compile_report.py").is_file()
        return {"ok": True, "exit_code": 0, "stdout": "", "stderr": "", "duration_ms": 1}

    monkeypatch.setattr(runner, "_run_via_executor", execute)
    payload = {
        "_skill_dir": str(builtin_catalog_dir() / "latex-report"),
        "_skill_entry": "compile_report.py",
        "_requires_isolated_runtime": True,
        "_sandbox_executor_url": "http://executor.test",
        "_sandbox_executor_token": "secret",
        "_artifact_fetch_token": "secret",
        "source": "source",
        "timeout_sec": 240,
    }
    input_path = tmp_path / "input.json"
    input_path.write_text(json.dumps(payload))
    monkeypatch.setattr(sys, "argv", ["runner.py", "--input", str(input_path), "--workdir", str(tmp_path)])
    runner.main()
    assert len(observed) == 1
    assert observed[0]["timeout_sec"] == 240
    assert observed[0]["source"] == "source"
    assert not any(key.startswith("_") for key in observed[0])
