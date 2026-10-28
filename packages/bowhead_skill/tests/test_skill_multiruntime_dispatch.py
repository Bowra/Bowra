"""skill-multiruntime S1 —— Block 2 分发/改写/runner 单测.

覆盖：tools.py 把用户 skill invoke 改写为 python_sandbox（P1-1 换 name+spec、P1-2 透传
业务 payload、P1-4 timeout 注入、skill_result 解包）；runner `_stage_user_skill` /
`_relay_skill_result`；runtime.py D6 host `.py`-only 守卫。

容器内真执行（node/bash）是 dogfood gate（需 rebuild 带 node 的镜像 + docker），不在 UT。
"""

from __future__ import annotations

import importlib.util
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from bowhead_skill.loader import load_skill
from bowhead_skill.runtime import RunResult, SkillRuntime
from bowhead_skill.tools import build_skill_tools

from bowhead_skill import SkillCatalog

_RUNNER_PATH = Path(__file__).resolve().parents[1] / "catalog" / "python_sandbox" / "scripts" / "runner.py"
_FLAG = "BOWHEAD_SKILL_MULTIRUNTIME"


def _load_runner() -> Any:
    spec = importlib.util.spec_from_file_location("_mr_runner_under_test", _RUNNER_PATH)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ── Test scaffolding ──────────────────────────────────────────────────
@dataclass
class _FakeResult:
    ok: bool = True
    exit_code: int = 0
    stdout: str = ""
    stderr: str = ""
    result_json: dict = field(default_factory=dict)
    outputs: list = field(default_factory=list)
    workdir: Any = None
    duration_ms: int = 0
    error: str | None = None


class _CaptureRuntime:
    def __init__(self, result: _FakeResult) -> None:
        self._result = result
        self.calls: list[dict] = []

    async def run(self, spec, *, entry_script, input_data, extra_args):  # noqa: ANN001
        self.calls.append({"spec_name": spec.name, "entry_script": entry_script, "input_data": dict(input_data)})
        return self._result


@dataclass
class _Rec:
    source_path: str
    source_type: str = "user_upload"
    enabled: bool = True


class _FakeUserStore:
    def __init__(self, recs: dict[str, str]) -> None:
        self._recs = recs  # name -> source_path

    async def get(self, *, user_id: str, name: str) -> _Rec | None:
        p = self._recs.get(name)
        return _Rec(source_path=p) if p else None

    async def list_for_user(self, *, user_id: str) -> list:
        return []


class _Ctx:
    def __init__(self, store: _FakeUserStore) -> None:
        self.user_id = "u1"
        self.team_id = None
        self.skill_instance_store = store
        self.team_skill_store = None
        self.session_id = "s_x"
        self.turn_id = "t_x"


def _mk_sandbox(root: Path) -> None:
    d = root / "python_sandbox"
    (d / "scripts").mkdir(parents=True)
    (d / "SKILL.md").write_text("---\nname: python_sandbox\ndescription: d\n---\n\nbody\n", encoding="utf-8")
    (d / "scripts" / "runner.py").write_text("print('x')\n", encoding="utf-8")
    (d / "scripts" / "dirty_table.py").write_text("def read_dirty_table(): ...\n", encoding="utf-8")


def _mk_user_skill(root: Path, name: str, entry: str, content: str = "echo hi\n") -> Path:
    d = root / name
    (d / "scripts").mkdir(parents=True)
    (d / "SKILL.md").write_text(f"---\nname: {name}\ndescription: d\n---\n\nbody\n", encoding="utf-8")
    (d / "scripts" / entry).write_text(content, encoding="utf-8")
    return d


def _invoke_tool(cat: SkillCatalog, rt: Any):  # noqa: ANN001
    return next(t for t in build_skill_tools(cat, rt) if t.name == "skill_invoke")


# ── tools.py rewrite seam ─────────────────────────────────────────────
@pytest.mark.parametrize("entry", ["python_exec", "skill_invoke", "uploaded_skill"])
@pytest.mark.parametrize("requested,expected", [(None, 30), (600, 60)])
async def test_team_timeout_policy_reaches_every_sandbox_entry(monkeypatch, tmp_path, entry, requested, expected):
    from bowhead_skill.execution_policy import SandboxExecutionPolicy, sandbox_execution_policy

    monkeypatch.setenv(_FLAG, "1")
    _mk_sandbox(tmp_path / "catalog")
    user_dir = _mk_user_skill(tmp_path / "userskills", "myskill", "gen.sh")
    catalog = SkillCatalog(roots=[tmp_path / "catalog"])
    catalog.refresh()
    runtime = _CaptureRuntime(_FakeResult(result_json={"ok": True}))
    tools = {tool.name: tool for tool in build_skill_tools(catalog, runtime)}
    payload = {"code": "print(1)", **({"timeout_sec": requested} if requested is not None else {})}
    if entry == "python_exec":
        args = payload
    else:
        args = {
            "name": "myskill" if entry == "uploaded_skill" else "python_sandbox",
            "script": "gen.sh" if entry == "uploaded_skill" else "runner.py",
            "input": payload,
        }
    token = sandbox_execution_policy.set(SandboxExecutionPolicy(30, 60))
    try:
        result = await tools["python_exec" if entry == "python_exec" else "skill_invoke"].handler(
            args, _Ctx(_FakeUserStore({"myskill": str(user_dir)}))
        )
    finally:
        sandbox_execution_policy.reset(token)
    assert runtime.calls[0]["input_data"]["timeout_sec"] == expected
    assert result["execution_policy"] == {"timeout_sec": expected, "maximum_timeout_sec": 60}


async def test_rewrite_swaps_to_python_sandbox(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv(_FLAG, "1")
    _mk_sandbox(tmp_path / "catalog")
    user_dir = _mk_user_skill(tmp_path / "userskills", "myskill", "gen.sh")
    cat = SkillCatalog(roots=[tmp_path / "catalog"])
    cat.refresh()
    rt = _CaptureRuntime(_FakeResult(result_json={"ok": True, "skill_result": {"ok": True, "answer": 42}}))
    tool = _invoke_tool(cat, rt)
    ctx = _Ctx(_FakeUserStore({"myskill": str(user_dir)}))

    res = await tool.handler({"name": "myskill", "script": "gen.sh", "input": {"title": "Q3"}}, ctx)

    # P1-1: switch to python_sandbox identity; otherwise _build_env drops sandbox settings, degrading gVisor.
    assert len(rt.calls) == 1
    call = rt.calls[0]
    assert call["spec_name"] == "python_sandbox"
    assert call["entry_script"] == "runner.py"
    # Inject control keys.
    assert call["input_data"]["_skill_dir"] == str(user_dir)
    assert call["input_data"]["_skill_entry"] == "gen.sh"
    # P1-4: server-side timeout injection.
    assert call["input_data"]["timeout_sec"] == 120
    # P1-2: preserve the user business payload for pass-through to the script.
    assert call["input_data"]["title"] == "Q3"
    # Unwrap skill_result: external result_json = child-script return.
    assert res["result_json"] == {"ok": True, "answer": 42}


async def test_flag_off_user_skill_fail_closed(monkeypatch, tmp_path) -> None:
    """skill-audit 批C：flag OFF 时用户 skill 拒绝执行（fail-closed），不回落 host 裸跑.

    旧行为（S1「现状兼容」）是保留 host 路径——security review 定性为条件性 P0
    （host 路 PYTHONPATH 可 import bowhead_core），改为结构性拒绝。
    """
    monkeypatch.delenv(_FLAG, raising=False)
    _mk_sandbox(tmp_path / "catalog")
    user_dir = _mk_user_skill(tmp_path / "userskills", "pyskill", "run.py", content="print(1)\n")
    cat = SkillCatalog(roots=[tmp_path / "catalog"])
    cat.refresh()
    rt = _CaptureRuntime(_FakeResult())
    tool = _invoke_tool(cat, rt)
    ctx = _Ctx(_FakeUserStore({"pyskill": str(user_dir)}))

    res = await tool.handler({"name": "pyskill", "script": "run.py", "input": {}}, ctx)

    assert rt.calls == []  # Never reach runtime.run; the host path is structurally disabled.
    assert (res["error_kind"], res["skill"], res["reason"]) == ("skill_unavailable", "pyskill", "script_runtime_disabled")
    assert "BOWHEAD_SKILL_MULTIRUNTIME" in res["error"]


async def test_flag_off_builtin_skill_still_runs(monkeypatch, tmp_path) -> None:
    """fail-closed 只针对非信任根：catalog（内置）skill flag OFF 照常走 host 路径."""
    monkeypatch.delenv(_FLAG, raising=False)
    _mk_sandbox(tmp_path / "catalog")
    cat = SkillCatalog(roots=[tmp_path / "catalog"])
    cat.refresh()
    rt = _CaptureRuntime(_FakeResult())
    tool = _invoke_tool(cat, rt)
    ctx = _Ctx(_FakeUserStore({}))

    await tool.handler(
        {
            "name": "python_sandbox",
            "script": "runner.py",
            "input": {"code": "print(1)"},
        },
        ctx,
    )

    assert len(rt.calls) == 1
    assert rt.calls[0]["spec_name"] == "python_sandbox"


async def test_builtin_python_sandbox_defaults_to_canonical_runner(
    monkeypatch,
    tmp_path,
) -> None:
    """The catalog contains runner.py plus helper modules, so omitting script must
    still deterministically dispatch runner.py instead of returning script required."""
    monkeypatch.delenv(_FLAG, raising=False)
    _mk_sandbox(tmp_path / "catalog")
    cat = SkillCatalog(roots=[tmp_path / "catalog"])
    cat.refresh()
    rt = _CaptureRuntime(_FakeResult())
    tool = _invoke_tool(cat, rt)
    ctx = _Ctx(_FakeUserStore({}))

    result = await tool.handler(
        {
            "name": "python_sandbox",
            "input": {"code": "print(17 * 23)"},
        },
        ctx,
    )

    assert not result.get("error")
    assert rt.calls[0]["entry_script"] == "runner.py"


async def test_builtin_python_sandbox_rejects_noncanonical_entry(
    monkeypatch,
    tmp_path,
) -> None:
    """A hallucinated entrypoint is rejected so trace and execution stay aligned."""
    monkeypatch.delenv(_FLAG, raising=False)
    _mk_sandbox(tmp_path / "catalog")
    cat = SkillCatalog(roots=[tmp_path / "catalog"])
    cat.refresh()
    rt = _CaptureRuntime(_FakeResult())
    tool = _invoke_tool(cat, rt)
    ctx = _Ctx(_FakeUserStore({}))

    result = await tool.handler(
        {
            "name": "python_sandbox",
            "script": "compute.py",
            "input": {"code": "print(17 * 23)"},
        },
        ctx,
    )

    assert rt.calls == []
    assert result["error_kind"] == "invalid_args"
    assert "runner.py" in result["error"]


async def test_builtin_python_sandbox_requires_input_code(
    monkeypatch,
    tmp_path,
) -> None:
    """Missing code is an argument error, not a runtime failure or retryable outage."""
    monkeypatch.delenv(_FLAG, raising=False)
    _mk_sandbox(tmp_path / "catalog")
    cat = SkillCatalog(roots=[tmp_path / "catalog"])
    cat.refresh()
    rt = _CaptureRuntime(_FakeResult())
    tool = _invoke_tool(cat, rt)
    ctx = _Ctx(_FakeUserStore({}))

    result = await tool.handler(
        {"name": "python_sandbox", "input": {}},
        ctx,
    )

    assert rt.calls == []
    assert result["error_kind"] == "invalid_args"
    assert 'script":"runner.py"' in result["error"]
    assert '"code":"<python>"' in result["error"]


async def test_smuggled_skill_dir_stripped(monkeypatch, tmp_path) -> None:
    """LLM 供的 _skill_dir 被无条件剥除（防越权指向任意路径）。"""
    monkeypatch.setenv(_FLAG, "1")
    _mk_sandbox(tmp_path / "catalog")
    user_dir = _mk_user_skill(tmp_path / "userskills", "myskill", "gen.sh")
    cat = SkillCatalog(roots=[tmp_path / "catalog"])
    cat.refresh()
    rt = _CaptureRuntime(_FakeResult())
    tool = _invoke_tool(cat, rt)
    ctx = _Ctx(_FakeUserStore({"myskill": str(user_dir)}))

    await tool.handler(
        {"name": "myskill", "script": "gen.sh", "input": {"_skill_dir": "/etc", "_skill_entry": "passwd"}},
        ctx,
    )
    # The server injects the actual skill directory, not an LLM-smuggled /etc.
    assert rt.calls[0]["input_data"]["_skill_dir"] == str(user_dir)
    assert rt.calls[0]["input_data"]["_skill_entry"] == "gen.sh"


# ── runner helpers ───────────────────────────────────────────
@pytest.mark.parametrize("source_kind", ["user_skills", "team_skills", "org_skills"])
def test_stage_user_skill_copies_bundle(tmp_path, source_kind) -> None:
    runner = _load_runner()
    # Must be under .data/{user,team}_skills (root-prefix guard).
    skill = _mk_user_skill(tmp_path / ".data" / source_kind / "u1", "s", "gen.sh")
    (skill / "references").mkdir()
    (skill / "references" / "guide.md").write_text("ref\n", encoding="utf-8")
    workdir = tmp_path / "wd"
    workdir.mkdir()

    err = runner._stage_user_skill(workdir, skill, "gen.sh")
    assert err is None
    assert (workdir / "skill" / "scripts" / "gen.sh").is_file()
    assert (workdir / "skill" / "references" / "guide.md").is_file()  # Entire bundle.
    import json as _j

    assert _j.loads((workdir / "skill_manifest.json").read_text())["entry"] == "gen.sh"
    # Exec bits removed.
    assert (workdir / "skill" / "scripts" / "gen.sh").stat().st_mode & 0o111 == 0


def test_stage_user_skill_rejects_missing_manifest(tmp_path) -> None:
    runner = _load_runner()
    bad = tmp_path / "nope"
    bad.mkdir()
    workdir = tmp_path / "wd"
    workdir.mkdir()
    err = runner._stage_user_skill(workdir, bad, "x.sh")
    assert err and "SKILL.md" in err


def test_relay_skill_result(tmp_path) -> None:
    runner = _load_runner()
    workdir = tmp_path / "wd"
    workdir.mkdir()
    (workdir / "skill_result.json").write_text('{"ok": true, "v": 7}', encoding="utf-8")
    result: dict = {"ok": True, "outputs": []}
    runner._relay_skill_result(workdir, result)
    assert result["skill_result"] == {"ok": True, "v": 7}


def test_relay_skill_result_absent_noop(tmp_path) -> None:
    runner = _load_runner()
    workdir = tmp_path / "wd"
    workdir.mkdir()
    result: dict = {"ok": True}
    runner._relay_skill_result(workdir, result)
    assert "skill_result" not in result


# ── runtime.py D6 host guard ─────────────────────────────────
async def test_host_guard_rejects_non_py(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv(_FLAG, "1")  # Allow loader to include .sh in spec.scripts.
    skill = _mk_user_skill(tmp_path, "s", "gen.sh")
    spec = load_skill(skill)
    rt = SkillRuntime()
    result: RunResult = await rt.run(spec, entry_script="gen.sh", input_data={})
    assert result.ok is False
    assert "only .py" in (result.error or "")


# ── Review P1: child-script ok=True must not mask envelope.ok=False (missing input) ──
async def test_envelope_ok_false_not_masked_by_child_ok(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv(_FLAG, "1")
    _mk_sandbox(tmp_path / "catalog")
    user_dir = _mk_user_skill(tmp_path / "userskills", "myskill", "gen.sh")
    cat = SkillCatalog(roots=[tmp_path / "catalog"])
    cat.refresh()
    # runner process exits 0 (result.ok=True), but envelope.ok=False (_mark_input_errors: input fetch failed);
    # child reports ok:True. Before the fix → false success; after → authoritative envelope.ok → failure.
    rt = _CaptureRuntime(
        _FakeResult(ok=True, result_json={"ok": False, "error": "input errors: art_x 404", "skill_result": {"ok": True, "answer": 1}})
    )
    tool = _invoke_tool(cat, rt)
    ctx = _Ctx(_FakeUserStore({"myskill": str(user_dir)}))
    res = await tool.handler({"name": "myskill", "script": "gen.sh", "input": {}}, ctx)
    assert res["ok"] is False  # Preserve the envelope's missing-input signal.
    assert "input errors" in (res["error"] or "")
    assert res["result_json"] == {"ok": True, "answer": 1}  # Still expose the child-script structured return.


def test_shim_interp_map_matches_ssot() -> None:
    """review nit: shim 的 hardcoded ext→interp 必须与 SSOT script_langs.json 一致（防漂移）。"""
    import ast
    import re

    from bowhead_skill import all_entry_extensions

    runner = _load_runner()
    m = re.search(r"_BH_EXT_INTERP = (\{[^}]*\})", runner._SKILL_DISPATCH_SHIM)
    assert m, "shim _BH_EXT_INTERP not found"
    assert ast.literal_eval(m.group(1)) == all_entry_extensions()


async def test_nested_script_name_reaches_sandbox_unchanged(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv(_FLAG, "1")
    _mk_sandbox(tmp_path / "catalog")
    directory = _mk_user_skill(tmp_path / "userskills", "nested-skill", "run.py", "print(1)\n")
    (directory / "scripts/nested").mkdir()
    (directory / "scripts/run.py").rename(directory / "scripts/nested/run.py")
    cat = SkillCatalog(roots=[tmp_path / "catalog"])
    cat.refresh()
    runtime = _CaptureRuntime(_FakeResult())
    result = await _invoke_tool(cat, runtime).handler(
        {"name": "nested-skill", "script": "nested/run.py", "input": {}},
        _Ctx(_FakeUserStore({"nested-skill": str(directory)})),
    )
    assert "error" not in result or result["error"] is None, result
    assert runtime.calls[0]["input_data"]["_skill_entry"] == "nested/run.py"


def test_stage_accepts_bundles_over_the_old_five_mb_limit(tmp_path) -> None:
    runner = _load_runner()
    skill = tmp_path / ".data/team_skills/team_a/large-skill"
    (skill / "references").mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: large-skill\ndescription: large bundle\n---\nbody\n")
    (skill / "references/data.txt").write_bytes(b"x" * (6 * 1024 * 1024))
    workdir = tmp_path / "work"
    workdir.mkdir()
    assert runner._stage_user_skill(workdir, skill, "run.py") is None
    assert (workdir / "skill/references/data.txt").stat().st_size == 6 * 1024 * 1024


@pytest.mark.parametrize("multiruntime,command", [("0", "0"), ("0", "1"), ("1", "0"), ("1", "1")])
async def test_direct_handlers_enforce_agent_operations_before_skill_io(monkeypatch, tmp_path, multiruntime, command) -> None:
    """Even both rollout flags ON cannot turn a read-only grant into execution."""
    from bowhead_skill.runtime_scope import ExactSkillRuntimeScope

    monkeypatch.setenv(_FLAG, multiruntime)
    monkeypatch.setenv("BOWHEAD_SKILL_INVOKE_COMMAND", command)
    _mk_sandbox(tmp_path / "catalog")
    user_dir = _mk_user_skill(tmp_path / "userskills", "myskill", "gen.py", "print(42)\n")
    (user_dir / "references").mkdir()
    (user_dir / "references" / "method.md").write_text("DuPont method", encoding="utf-8")
    cat = SkillCatalog(roots=[tmp_path / "catalog"])
    cat.refresh()
    rt = _CaptureRuntime(_FakeResult())
    handlers = {tool.name: tool.handler for tool in build_skill_tools(cat, rt)}
    ctx = _Ctx(_FakeUserStore({"myskill": str(user_dir)}))
    ctx.skill_runtime_scope = ExactSkillRuntimeScope(read_names=("myskill",), invoke_names=())

    read = await handlers["skill_read"]({"name": "myskill", "file": "references/method.md"}, ctx)
    assert read["body"] == "DuPont method"
    for tool, args in [
        ("skill_invoke", {"name": "myskill", "script": "gen.py", "input": {}}),
        ("skill_invoke", {"name": "myskill", "command": ["python3", "scripts/gen.py"], "input": {}}),
        ("python_exec", {"code": "print(42)"}),
        ("skill_read", {"name": "unknown", "file": "references/method.md"}),
    ]:
        denied = await handlers[tool](args, ctx)
        assert (denied["error_kind"], denied["reason"]) == ("skill_unavailable", "not_granted")
    assert rt.calls == []
    # The scoped list shows only the Agent's own Skills, not the installed sandbox.
    assert {entry["name"] for entry in await handlers["skill_list"]({}, ctx)} <= {"myskill"}
    unscoped = _Ctx(ctx.skill_instance_store)
    assert "python_sandbox" in {entry["name"] for entry in await handlers["skill_list"]({}, unscoped)}

    ctx.skill_runtime_scope = ExactSkillRuntimeScope(read_names=("myskill",), invoke_names=("myskill",))
    result = await handlers["skill_invoke"]({"name": "myskill", "script": "gen.py", "input": {}}, ctx)
    if multiruntime == "1":
        assert result["ok"] is True
        assert rt.calls[0]["spec_name"] == "python_sandbox"
        assert rt.calls[0]["input_data"]["_skill_dir"] == str(user_dir)
    else:
        assert "BOWHEAD_SKILL_MULTIRUNTIME" in result["error"]
        assert rt.calls == []


async def test_invoke_scope_does_not_bypass_pending_import(monkeypatch, tmp_path) -> None:
    from bowhead_skill.runtime_scope import ExactSkillRuntimeScope

    class PendingStore(_FakeUserStore):
        async def get(self, *, user_id, name):
            rec = await super().get(user_id=user_id, name=name)
            if rec is not None:
                rec.import_status = "pending"
            return rec

    monkeypatch.setenv(_FLAG, "1")
    monkeypatch.setenv("BOWHEAD_SKILL_INVOKE_COMMAND", "1")
    _mk_sandbox(tmp_path / "catalog")
    user_dir = _mk_user_skill(tmp_path / "userskills", "myskill", "gen.py")
    cat = SkillCatalog(roots=[tmp_path / "catalog"])
    cat.refresh()
    rt = _CaptureRuntime(_FakeResult())
    ctx = _Ctx(PendingStore({"myskill": str(user_dir)}))
    ctx.skill_runtime_scope = ExactSkillRuntimeScope(read_names=("myskill",), invoke_names=("myskill",))
    denied = await _invoke_tool(cat, rt).handler({"name": "myskill", "command": ["python3", "scripts/gen.py"], "input": {}}, ctx)
    assert denied["error_kind"] == "skill_import_pending"
    assert rt.calls == []
