"""skill-multiruntime follow-up② —— model-driven `command` argv 桥接单测.

覆盖（design s-followup-invoke-contract-bridge-design.md）：
- tools.py：command 门控（子门 loud / flag-off ignore / smuggle strip）+ 控制键透传 + schema flag 门控广告。
- runner.py `_resolve_command`：token 替换 / 未知 token 回显词表 / argv[0] allowlist / 路径守卫 /
  argv 向量无 shell 解释。
- runner.py `main()` command 模式：deliverable-presence 守卫（P0 静默假成功）+ capture-on-partial-failure。

容器内真执行 = dogfood gate（需带 node/soffice 的镜像 + docker），不在 UT。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from bowhead_skill.tools import build_skill_tools

from bowhead_skill import SkillCatalog

_RUNNER_PATH = Path(__file__).resolve().parents[1] / "catalog" / "python_sandbox" / "scripts" / "runner.py"
_MR = "BOWHEAD_SKILL_MULTIRUNTIME"
_CMD = "BOWHEAD_SKILL_INVOKE_COMMAND"


def _load_runner() -> Any:
    spec = importlib.util.spec_from_file_location("_cmd_runner_under_test", _RUNNER_PATH)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ── Scaffolding (aligned with test_skill_multiruntime_dispatch.py) ───────
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
    def __init__(self, result: _FakeResult | None = None) -> None:
        self._result = result or _FakeResult()
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
        self._recs = recs

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


def _mk_user_skill(root: Path, name: str, entry: str, content: str = "echo hi\n") -> Path:
    d = root / name
    (d / "scripts").mkdir(parents=True)
    (d / "SKILL.md").write_text(f"---\nname: {name}\ndescription: d\n---\n\nbody\n", encoding="utf-8")
    (d / "scripts" / entry).write_text(content, encoding="utf-8")
    return d


def _invoke_tool(cat: SkillCatalog, rt: Any):  # noqa: ANN001
    return next(t for t in build_skill_tools(cat, rt) if t.name == "skill_invoke")


def _setup(tmp_path: Path, rt: Any) -> tuple[Any, _Ctx, Path]:
    _mk_sandbox(tmp_path / "catalog")
    user_dir = _mk_user_skill(tmp_path / "userskills", "myskill", "run_pipeline.py", content="print(1)\n")
    cat = SkillCatalog(roots=[tmp_path / "catalog"])
    cat.refresh()
    tool = _invoke_tool(cat, rt)
    ctx = _Ctx(_FakeUserStore({"myskill": str(user_dir)}))
    return tool, ctx, user_dir


_CMD_ARGV = ["python3", "scripts/run_pipeline.py", "--spec", "{inputs.spec}", "--output", "{output_dir}/deck.pptx"]


# ── tools.py gating + control-key pass-through ─────────────────────────
async def test_command_mode_threads_control_keys(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv(_MR, "1")
    monkeypatch.setenv(_CMD, "1")
    rt = _CaptureRuntime()
    tool, ctx, user_dir = _setup(tmp_path, rt)

    await tool.handler({"name": "myskill", "command": _CMD_ARGV, "input": {"inputs": {}}}, ctx)

    call = rt.calls[0]
    assert call["spec_name"] == "python_sandbox"  # Switch identity.
    inp = call["input_data"]
    assert inp["_skill_command"] == _CMD_ARGV  # Original argv with tokens; substitution happens host-side in runner.
    assert inp["_skill_command_enabled"] is True  # Server marker.
    assert "python3" in inp["_skill_argv0_allowlist"]
    assert inp["_skill_dir"] == str(user_dir)  # Staging is still required.


async def test_command_ignored_when_invoke_flag_off(monkeypatch, tmp_path) -> None:
    """MULTIRUNTIME ON 但 INVOKE_COMMAND OFF → command 不生效（走 legacy），零行为变化。"""
    monkeypatch.setenv(_MR, "1")
    monkeypatch.delenv(_CMD, raising=False)
    rt = _CaptureRuntime()
    tool, ctx, _ = _setup(tmp_path, rt)

    # script is required by the legacy path; command is ignored.
    await tool.handler(
        {"name": "myskill", "script": "run_pipeline.py", "command": _CMD_ARGV, "input": {}},
        ctx,
    )
    inp = rt.calls[0]["input_data"]
    assert "_skill_command" not in inp
    assert "_skill_command_enabled" not in inp


async def test_command_subgate_requires_multiruntime(monkeypatch, tmp_path) -> None:
    """INVOKE_COMMAND ON 但 MULTIRUNTIME OFF = 无效组合 → loud error（非静默丢）。"""
    monkeypatch.delenv(_MR, raising=False)
    monkeypatch.setenv(_CMD, "1")
    rt = _CaptureRuntime()
    tool, ctx, _ = _setup(tmp_path, rt)

    res = await tool.handler({"name": "myskill", "command": _CMD_ARGV, "input": {}}, ctx)
    assert (res.get("error_kind"), res.get("reason")) == ("skill_unavailable", "script_runtime_disabled")
    assert "MULTIRUNTIME" in res["error"]
    assert not rt.calls  # runtime.run was never reached.


async def test_command_on_builtin_skill_rejected(monkeypatch, tmp_path) -> None:
    """command 模式只用于上传 skill；对内置 catalog skill 报错。"""
    monkeypatch.setenv(_MR, "1")
    monkeypatch.setenv(_CMD, "1")
    rt = _CaptureRuntime()
    tool, ctx, _ = _setup(tmp_path, rt)

    res = await tool.handler({"name": "python_sandbox", "command": ["python3", "x.py"], "input": {}}, ctx)
    assert res.get("error_kind") == "invalid_args"
    assert "内置" in res["error"]


async def test_command_not_list_rejected(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv(_MR, "1")
    monkeypatch.setenv(_CMD, "1")
    rt = _CaptureRuntime()
    tool, ctx, _ = _setup(tmp_path, rt)

    res = await tool.handler({"name": "myskill", "command": "python3 scripts/x.py", "input": {}}, ctx)
    assert res.get("error_kind") == "invalid_args"
    assert "argv 数组" in res["error"]


async def test_smuggled_skill_command_enabled_stripped(monkeypatch, tmp_path) -> None:
    """flag OFF 时 LLM 把 _skill_command_enabled 塞进 input 不能绕过灰度（Security P1-2）。"""
    monkeypatch.setenv(_MR, "1")
    monkeypatch.delenv(_CMD, raising=False)
    rt = _CaptureRuntime()
    tool, ctx, _ = _setup(tmp_path, rt)

    await tool.handler(
        {
            "name": "myskill",
            "script": "run_pipeline.py",
            "input": {"_skill_command": ["bash", "-c", "evil"], "_skill_command_enabled": True},
        },
        ctx,
    )
    inp = rt.calls[0]["input_data"]
    assert "_skill_command" not in inp  # Strip the smuggled value.
    assert "_skill_command_enabled" not in inp


async def test_command_mode_no_script_required(monkeypatch, tmp_path) -> None:
    """command 模式不需要 script 参数（argv 自带脚本路径）。"""
    monkeypatch.setenv(_MR, "1")
    monkeypatch.setenv(_CMD, "1")
    rt = _CaptureRuntime()
    tool, ctx, _ = _setup(tmp_path, rt)

    res = await tool.handler({"name": "myskill", "command": _CMD_ARGV, "input": {}}, ctx)
    assert "error" not in res or not res.get("error")  # No "script required" error.
    assert rt.calls[0]["spec_name"] == "python_sandbox"


def test_command_param_schema_flag_gated(monkeypatch, tmp_path) -> None:
    """`command` 参数只在两 flag 都 ON 时出现在 tool schema（SRE P2：免空传浪费 token；
    且 command 是 multiruntime 子门——单开 INVOKE_COMMAND 不广告，免误导模型撞子门 loud error）。"""
    _mk_sandbox(tmp_path / "catalog")
    cat = SkillCatalog(roots=[tmp_path / "catalog"])
    cat.refresh()
    rt = _CaptureRuntime()

    monkeypatch.delenv(_CMD, raising=False)
    monkeypatch.delenv(_MR, raising=False)
    assert "command" not in _invoke_tool(cat, rt).input_schema["properties"]

    # Only INVOKE_COMMAND enabled (the subordinate gate is off) → still do not advertise.
    monkeypatch.setenv(_CMD, "1")
    assert "command" not in _invoke_tool(cat, rt).input_schema["properties"]

    # Both flags enabled → advertise.
    monkeypatch.setenv(_MR, "1")
    assert "command" in _invoke_tool(cat, rt).input_schema["properties"]


# ── runner._resolve_command guards ────────────────────────────────────
def test_resolve_command_substitutes_tokens() -> None:
    runner = _load_runner()
    resolved = {"spec": {"path": "/workdir/spec_deck.json"}}
    allow = {"python3"}
    argv, err = runner._resolve_command(
        ["python3", "scripts/run_pipeline.py", "--spec", "{inputs.spec}", "--output", "{output_dir}/deck.pptx"],
        resolved,
        allow,
    )
    assert err is None
    assert argv == ["python3", "scripts/run_pipeline.py", "--spec", "/workdir/spec_deck.json", "--output", "/workdir/deck.pptx"]


def test_resolve_command_unknown_token_echoes_vocab() -> None:
    runner = _load_runner()
    argv, err = runner._resolve_command(
        ["python3", "x.py", "--spec", "{inputs.missing}"],
        {"spec": {"path": "/workdir/s.json"}},
        {"python3"},
    )
    assert argv is None
    assert "missing" in err and "spec" in err and "output_dir" in err  # Echo the allowed vocabulary.


def test_argv0_allowlist_includes_python_alias() -> None:
    """真栈 LLM dogfood 逮出：模型照 SKILL.md 拼 `python scripts/…`（生态惯用别名，非 python3）
    撞 argv[0] 闸失败。镜像 `command -v python`=/usr/local/bin/python→python3，须放行。"""
    from bowhead_skill.script_langs import argv0_allowlist

    al = argv0_allowlist()
    for interp in ("python", "python3", "node", "bash"):
        assert interp in al, f"{interp!r} 应在 argv[0] allowlist"


def test_resolve_command_argv0_not_allowed() -> None:
    runner = _load_runner()
    argv, err = runner._resolve_command(["/workdir/skill/scripts/evil", "x"], {}, {"python3", "node", "bash"})
    assert argv is None
    assert "argv[0]" in err


def test_resolve_command_path_escape_rejected() -> None:
    runner = _load_runner()
    for bad in ["../../etc/passwd", "/etc/passwd", "scripts/../../secret"]:
        argv, err = runner._resolve_command(["python3", bad], {}, {"python3"})
        assert argv is None, f"should reject {bad!r}"
        assert "跳出" in err


def test_resolve_command_argv_vector_no_shell_interp() -> None:
    """shell 元字符只是普通 argv 元素（值向量、非 shell 串）——不报注入错，逐字保留。"""
    runner = _load_runner()
    argv, err = runner._resolve_command(
        ["bash", "-c", "echo hi; rm -rf /tmp/x $(whoami)"],
        {},
        {"bash"},
    )
    assert err is None
    assert argv[2] == "echo hi; rm -rf /tmp/x $(whoami)"  # Entire string remains one element; no splitting/interpretation.


def test_resolve_command_substitution_value_is_server_path() -> None:
    """{inputs.X} 替换值恒服务端 /workdir 路径，即便嵌进 bash -c 元素也是纯路径（CTO P2-3）。"""
    runner = _load_runner()
    argv, err = runner._resolve_command(
        ["bash", "-c", "cat {inputs.spec}"],
        {"spec": {"path": "/workdir/spec_x.json"}},
        {"bash"},
    )
    assert err is None
    assert argv[2] == "cat /workdir/spec_x.json"


# ── runner.main() command mode: deliverable-presence + capture-on-partial-failure ──
def _stage_skill_under_data(tmp_path: Path) -> Path:
    skill = _mk_user_skill(tmp_path / ".data" / "user_skills" / "u1", "s", "run_pipeline.py", content="print(1)\n")
    return skill


def _run_main(runner: Any, monkeypatch, workdir: Path, payload: dict, *, create_file: str | None, ok: bool, exit_code: int) -> dict:
    def _fake_executor(_url: str, _token: str, **kw: Any) -> dict:
        wd = kw["workdir"]
        if create_file:
            (wd / create_file).write_bytes(b"PK\x03\x04fakedeck")
        return {"ok": ok, "exit_code": exit_code, "stdout": "", "stderr": "", "duration_ms": 1}

    def _fake_upload(p: Path, gateway: str, **kw: Any) -> dict:
        return {"name": p.name, "artifact_id": "art_fake", "mime": "application/octet-stream", "size": p.stat().st_size}

    monkeypatch.setattr(runner, "_run_via_executor", _fake_executor)
    monkeypatch.setattr(runner, "_upload_artifact", _fake_upload)
    inp = workdir / "input.json"
    payload.setdefault("_sandbox_executor_url", "http://executor.test:8091")
    payload.setdefault("_sandbox_executor_token", "test-token")
    inp.write_text(json.dumps(payload), encoding="utf-8")
    old = sys.argv
    sys.argv = ["runner.py", "--input", str(inp), "--workdir", str(workdir)]
    try:
        runner.main()
    finally:
        sys.argv = old
    return json.loads((workdir / "result.json").read_text(encoding="utf-8"))


def _cmd_payload(skill_dir: Path) -> dict:
    return {
        "_skill_dir": str(skill_dir),
        "_skill_entry": "",
        "_skill_command": ["python3", "scripts/run_pipeline.py", "--output", "{output_dir}/deck.pptx"],
        "_skill_command_enabled": True,
        "_sandbox_executor_url": "http://executor.test:8091",
        "_sandbox_executor_token": "test-token",
        "_skill_argv0_allowlist": ["python3", "node", "bash"],
        "inputs": {},
        "timeout_sec": 30,
    }


def test_main_command_deliverable_presence_guard(monkeypatch, tmp_path) -> None:
    """exit0 但顶层无产物 → 判失败 + model-legible 提示（P0 静默假成功修复）。"""
    runner = _load_runner()
    skill = _stage_skill_under_data(tmp_path)
    wd = tmp_path / "wd"
    wd.mkdir()
    result = _run_main(runner, monkeypatch, wd, _cmd_payload(skill), create_file=None, ok=True, exit_code=0)
    assert result["ok"] is False
    assert "output_dir" in result["error"]
    assert result["outputs"] == []
    # The manifest contains the command form with tokens substituted.
    manifest = json.loads((wd / "skill_manifest.json").read_text())
    assert manifest["command"][-1] == "/workdir/deck.pptx"
    assert manifest["cwd"] == "/workdir/skill"


def test_main_command_capture_on_partial_failure(monkeypatch, tmp_path) -> None:
    """exit≠0 也先扫顶层输出（不丢已生成 deck），但 ok 保持 False。"""
    runner = _load_runner()
    skill = _stage_skill_under_data(tmp_path)
    wd = tmp_path / "wd"
    wd.mkdir()
    result = _run_main(runner, monkeypatch, wd, _cmd_payload(skill), create_file="deck.pptx", ok=False, exit_code=1)
    assert result["ok"] is False  # Nonzero exit = failure.
    assert len(result["outputs"]) == 1  # Capture outputs anyway (capture-on-partial-failure).
    assert result["outputs"][0]["artifact_id"] == "art_fake"


def test_main_code_capture_on_partial_failure(monkeypatch, tmp_path) -> None:
    """普通 Python 失败也不能丢弃已经写出的 Markdown/Data 文件。"""
    runner = _load_runner()
    wd = tmp_path / "wd"
    wd.mkdir()
    result = _run_main(
        runner,
        monkeypatch,
        wd,
        {"code": "raise RuntimeError('after write')", "inputs": {}},
        create_file="partial.md",
        ok=False,
        exit_code=1,
    )
    assert result["ok"] is False
    assert result["outputs"][0]["name"] == "partial.md"
    assert result["outputs"][0]["artifact_id"] == "art_fake"


def test_main_command_success(monkeypatch, tmp_path) -> None:
    """exit0 + 有产物 → ok True + 产物捕获。"""
    runner = _load_runner()
    skill = _stage_skill_under_data(tmp_path)
    wd = tmp_path / "wd"
    wd.mkdir()
    result = _run_main(runner, monkeypatch, wd, _cmd_payload(skill), create_file="deck.pptx", ok=True, exit_code=0)
    assert result["ok"] is True
    assert len(result["outputs"]) == 1


def test_main_command_bad_argv_fails_before_run(monkeypatch, tmp_path) -> None:
    """token/守卫失败 → 提前 fail（不跑容器），错误可读。"""
    runner = _load_runner()
    skill = _stage_skill_under_data(tmp_path)
    wd = tmp_path / "wd"
    wd.mkdir()
    payload = _cmd_payload(skill)
    payload["_skill_command"] = ["python3", "scripts/x.py", "--spec", "{inputs.nope}"]
    result = _run_main(runner, monkeypatch, wd, payload, create_file=None, ok=True, exit_code=0)
    assert result["ok"] is False
    assert "nope" in result["error"]


def test_main_ignores_command_without_server_marker(monkeypatch, tmp_path) -> None:
    """runner 侧 anti-smuggle：_skill_command 存在但无服务端 marker `_skill_command_enabled`
    → 不触发 command 模式（走 legacy shim），走私 command 不被采纳（Security P1-2 的 runner 半边）。"""
    runner = _load_runner()
    skill = _stage_skill_under_data(tmp_path)
    wd = tmp_path / "wd"
    wd.mkdir()
    payload = {
        "_skill_dir": str(skill),
        "_skill_entry": "run_pipeline.py",  # Legacy entry: without a marker, use the legacy path.
        "_skill_command": ["bash", "-c", "evil"],  # Smuggled value, without _skill_command_enabled.
        "inputs": {},
        "timeout_sec": 30,
    }
    _run_main(runner, monkeypatch, wd, payload, create_file="out.txt", ok=True, exit_code=0)
    manifest = json.loads((wd / "skill_manifest.json").read_text(encoding="utf-8"))
    assert manifest.get("entry") == "run_pipeline.py"  # Legacy mode.
    assert "command" not in manifest  # The smuggled command was not accepted.


def test_friendly_runtime_error_command_mode_narrowing() -> None:
    """command 模式：skill 自身 stderr 含 docker-daemon 关键词但 exit≠125 → 不误判为 sandbox 未就绪
    （SRE P2：否则 prod blank 掉真错、defeats self-correction）。"""
    runner = _load_runner()
    stderr = "Error: cannot connect to the docker daemon at unix:///var/run/docker.sock"
    # Non-command mode: exit 1 + match → friendly classification (backward compatibility unchanged).
    assert runner._friendly_runtime_error("", stderr, 1, 1) is not None
    # Command mode + skill's own exit 1 → avoid misclassification; expose the actual error.
    assert runner._friendly_runtime_error("", stderr, 1, 1, command_mode=True) is None
    # Command mode + exit 125 (actual docker-launch failure) → still classify with a friendly message.
    assert runner._friendly_runtime_error("", stderr, 1, 125, command_mode=True) is not None
