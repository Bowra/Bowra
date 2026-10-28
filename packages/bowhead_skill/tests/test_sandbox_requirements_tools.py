"""pptx-from-template M2 (wheelhouse) — tools.py injects/strips
`_sandbox_requirements` ONLY for python_sandbox.

For a `python_sandbox` invoke, `_invoke` injects
`input_data["_sandbox_requirements"] = list(spec.sandbox_requirements)` (set from
the RESOLVED SkillSpec, NOT any LLM-emitted value). For any other skill it
DEFENSIVELY strips an LLM-smuggled key (mirrors the `_pi_k_enabled` strip test).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from bowhead_skill.tools import build_skill_tools

from bowhead_skill import SkillCatalog

if TYPE_CHECKING:
    from pathlib import Path


@dataclass
class _FakeResult:
    ok: bool = True
    exit_code: int = 0
    stdout: str = ""
    stderr: str = ""
    result_json: dict = field(default_factory=dict)
    outputs: list = field(default_factory=list)
    workdir: Path | None = None
    duration_ms: int = 0
    error: str | None = None


class _CapturingRuntime:
    """Stand-in for SkillRuntime that records the input_data passed to run()."""

    def __init__(self) -> None:
        self.captured: dict[str, Any] | None = None

    async def run(self, spec, *, entry_script, input_data, extra_args):
        self.captured = input_data
        return _FakeResult()


def _mk_skill(
    root: Path,
    name: str,
    *,
    reqs_fm: str = "",
    script: str = "run.py",
) -> Path:
    d = root / name
    d.mkdir()
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: d\n{reqs_fm}---\n\nbody\n",
        encoding="utf-8",
    )
    scripts = d / "scripts"
    scripts.mkdir()
    (scripts / script).write_text("print('x')\n", encoding="utf-8")
    return d


def _invoke_tool(cat: SkillCatalog, rt: _CapturingRuntime):
    return next(t for t in build_skill_tools(cat, rt) if t.name == "skill_invoke")


async def test_python_sandbox_injects_resolved_reqs(tmp_path: Path) -> None:
    # python_sandbox skill declaring two deps → injected from the SkillSpec.
    _mk_skill(
        tmp_path,
        "python_sandbox",
        reqs_fm="sandbox_requirements:\n  - prophet\n  - cmdstanpy\n",
        script="runner.py",
    )
    cat = SkillCatalog(roots=[tmp_path])
    cat.refresh()
    rt = _CapturingRuntime()
    inv = _invoke_tool(cat, rt)

    await inv.handler(
        {"name": "python_sandbox", "script": "runner.py", "input": {"code": "x"}},
        None,
    )
    assert rt.captured is not None
    assert rt.captured["_sandbox_requirements"] == ["prophet", "cmdstanpy"]


async def test_python_sandbox_no_reqs_injects_empty_list(tmp_path: Path) -> None:
    _mk_skill(tmp_path, "python_sandbox", script="runner.py")
    cat = SkillCatalog(roots=[tmp_path])
    cat.refresh()
    rt = _CapturingRuntime()
    inv = _invoke_tool(cat, rt)

    await inv.handler(
        {"name": "python_sandbox", "script": "runner.py", "input": {"code": "x"}},
        None,
    )
    # Always set (empty) for python_sandbox so the runner has a definite signal.
    assert rt.captured["_sandbox_requirements"] == []


async def test_python_sandbox_ignores_llm_supplied_reqs(tmp_path: Path) -> None:
    # An LLM smuggles its own deps into args.input → MUST be overwritten by the
    # resolved (empty) SkillSpec value, never trusted.
    _mk_skill(tmp_path, "python_sandbox", script="runner.py")
    cat = SkillCatalog(roots=[tmp_path])
    cat.refresh()
    rt = _CapturingRuntime()
    inv = _invoke_tool(cat, rt)

    await inv.handler(
        {
            "name": "python_sandbox",
            "script": "runner.py",
            "input": {"code": "x", "_sandbox_requirements": ["evil-pkg"]},
        },
        None,
    )
    assert rt.captured["_sandbox_requirements"] == []


async def test_python_sandbox_gate_off_passes_explicit_false(tmp_path: Path, monkeypatch) -> None:
    """review 批1-R1：网关 π-K 判 OFF 时必须显式传 _pi_k_enabled=False。原来是 pop 掉——runner 看到
    None 回退 env，而 BOWHEAD_* 早被 _build_env 剥掉、runner 默认又是 ON → BOWHEAD_PI_K_ENABLED=0
    在生产里形同虚设。LLM 夹带的 True 也必须被压成 False。"""
    monkeypatch.setenv("BOWHEAD_PI_K_ENABLED", "0")
    _mk_skill(tmp_path, "python_sandbox", script="runner.py")
    cat = SkillCatalog(roots=[tmp_path])
    cat.refresh()
    rt = _CapturingRuntime()
    inv = _invoke_tool(cat, rt)
    await inv.handler(
        {"name": "python_sandbox", "script": "runner.py", "input": {"code": "x", "_pi_k_enabled": True}},
        None,
    )
    assert rt.captured["_pi_k_enabled"] is False
    assert "_pi_k_redact_pii" not in rt.captured


async def test_python_sandbox_gate_on_passes_true(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("BOWHEAD_PI_K_ENABLED", raising=False)
    monkeypatch.delenv("BOWHEAD_PI_K_ALLOWLIST_WORKSPACES", raising=False)
    _mk_skill(tmp_path, "python_sandbox", script="runner.py")
    cat = SkillCatalog(roots=[tmp_path])
    cat.refresh()
    rt = _CapturingRuntime()
    inv = _invoke_tool(cat, rt)
    await inv.handler({"name": "python_sandbox", "script": "runner.py", "input": {"code": "x"}}, None)
    assert rt.captured["_pi_k_enabled"] is True


async def test_non_sandbox_skill_strips_smuggled_reqs(tmp_path: Path) -> None:
    # A non-python_sandbox skill: an LLM-smuggled _sandbox_requirements is
    # defensively stripped (the else branch).
    _mk_skill(tmp_path, "pdf", script="run.py")
    cat = SkillCatalog(roots=[tmp_path])
    cat.refresh()
    rt = _CapturingRuntime()
    inv = _invoke_tool(cat, rt)

    await inv.handler(
        {
            "name": "pdf",
            "script": "run.py",
            "input": {"hello": "world", "_sandbox_requirements": ["evil-pkg"]},
        },
        None,
    )
    assert "_sandbox_requirements" not in rt.captured
