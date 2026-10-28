"""批 5ⓑ（compute-agent-optimize）：python_sandbox 的**扁平**原生工具。

靶子：116 30 天 **72 次**「python 代码嵌在 JSON 字符串里流式写崩」。老入口把整段代码塞进
`input` 这个子对象里的一个长字符串（`tools.py:_fill_input_filenames` 的注释里有 offset=857
那次现场），扁平入口把 `code` 提到顶层，少一层嵌套、少一层转义。

⚠ 本文件**证不了「写崩变少了」**——那发生在 provider 流式回传 tool-args 那一层，单测撞不上。
这里钉的是三件能钉的事：形状对、老路没断、以及**这工具在 compute 面上不是死的**
（`feedback_tool_in_allowlist_is_not_in_sop`：进了 allow ≠ 模型会用；没写进 prompt 就等于真机不存在）。
真实效果要等部署后回查 116 的流式截断计数。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from bowhead_skill.tools import build_skill_tools

REPO_ROOT = Path(__file__).resolve().parents[3]
COMPUTE_MD = REPO_ROOT / "packages/bowhead_core/src/bowhead_core/agents/prompts/system/compute.md"
COMPUTE_YAML = REPO_ROOT / "packages/bowhead_core/src/bowhead_core/agents/manifests/system/compute.yaml"


class _StubCatalog:
    def list_specs(self) -> list[Any]:
        return []

    def get(self, name: str) -> Any:
        return None


class _StubRuntime:
    pass


def _defs(monkeypatch: pytest.MonkeyPatch | None = None) -> dict[str, Any]:
    return {d.name: d for d in build_skill_tools(_StubCatalog(), _StubRuntime())}  # type: ignore[arg-type]


def test_flat_tool_registered_by_default() -> None:
    d = _defs()
    assert "python_exec" in d, "扁平入口应注册"
    schema = d["python_exec"].input_schema
    # The shape is the tool's **entire** purpose: all parameters are top-level, code is required, and there is no nested object.
    assert set(schema["properties"]) == {
        "code",
        "intent",
        "inputs",
        "timeout_sec",
        "purpose",
        "deliverable_outputs",
    }
    assert schema["required"] == ["code", "intent"]
    assert schema["properties"]["code"]["type"] == "string"


@pytest.mark.anyio
async def test_flat_handler_rejects_bad_args_before_touching_the_sandbox() -> None:
    """参数形状不对时在**进 `_invoke` 之前**就明确报错，别让它变成一次白跑的沙箱调用。

    判据用「错误里点名了哪个参数」——`invalid_args` 这个 kind 是给 step classifier 看的
    （模型能自愈的 arg-shape 滑手不该弹「临时失败」卡）。
    """
    handler = _defs()["python_exec"].handler

    out = await handler({"inputs": {"rows": {"artifact_id": "art_x"}}}, object())
    assert out["error_kind"] == "invalid_args"
    assert "code" in out["error"], out

    out2 = await handler({"code": "print(1)", "inputs": "not-a-dict"}, object())
    assert out2["error_kind"] == "invalid_args"
    assert "inputs" in out2["error"], out2


@pytest.mark.anyio
async def test_flat_handler_routes_into_the_same_python_sandbox_skill() -> None:
    """**不新建执行路径**：合法参数会一路走进 `_invoke`，并在解析 skill 名那一步落到
    `python_sandbox` 上。stub catalog 里没有任何 skill，所以拿到的是
    `skill 'python_sandbox' not found` —— 这条错误串同时证明了两件事：
    ① 它真的进了 `_invoke`（校验分支不会产出这句）；② 它路由的是 `python_sandbox` 这个 skill。
    """
    handler = _defs()["python_exec"].handler
    out = await handler({"code": "print(1)", "inputs": {"rows": {"artifact_id": "art_x"}}, "timeout_sec": 60}, object())
    assert out == {"error": "skill 'python_sandbox' not found"}, out


def test_flat_tool_is_in_compute_allow_and_taught_in_the_prompt() -> None:
    """两件缺一不可的事——少任何一件这工具在真机上都不存在。

    ① 不在 `compute.yaml tools.allow` 里 → profile 过滤器直接滤掉。
    ② compute.md 还在教老写法 → 模型照旧写老写法（`feedback_tool_in_allowlist_is_not_in_sop`：
       graph-mode 的 `plan_propose` 提案率=0 就是「进了工具面但没进决策树」）。
    """
    assert "\n    - python_exec\n" in COMPUTE_YAML.read_text(encoding="utf-8"), "没进 compute 的 tools.allow"
    md = COMPUTE_MD.read_text(encoding="utf-8")
    assert "python_exec(\n    code=" in md, "compute.md 的 ④ 没教扁平写法——模型会继续写嵌套那版"
    assert 'purpose="analysis"' in md and "deliverable_outputs=[]" in md
    assert 'skill_invoke(\n    name="python_sandbox"' not in md, "④ 里还留着老的嵌套示例"


def test_prompt_handles_an_unavailable_python_tool() -> None:
    """The prompt provides the authorized legacy entry when python_exec is unavailable."""
    md = COMPUTE_MD.read_text(encoding="utf-8")
    assert "没有" in md and 'skill_invoke(name="python_sandbox", script="runner.py"' in md


def test_the_tool_actually_reaches_the_model_not_just_the_registry() -> None:
    """★ 本批最重要的一条闸——**在闸上验，不在闸的上游验**。

    第一版我把工具起名 `python_sandbox`，注册进 registry、写进 `compute.yaml tools.allow`、
    prompt 也教了，AB 一跑**模型一次都没用**。原因不是模型不听话：`python_sandbox` 这个名字在
    manifest allow 里**早已被占用**——`subagent._materialize_authoring_pseudo_tools` 把它当
    authoring 伪工具，① 改写成 `skill_invoke`，② 还往 system prompt **最前面**塞一段
    「`python_sandbox` is an authoring capability, not a directly callable tool」。
    于是模型看到的工具面里根本没有它，还被明文告知别直接调。

    当时的断言只查了 `registry.list_names()`——那是**闸的上游**。真正的闸是
    `manifest_to_profile` 之后的 `PROFILES["compute"].tool_whitelist`（发给 LLM 的工具按它过滤）。
    这条测试钉在闸上：名字必须活着穿过 manifest 翻译，且不许触发伪工具 prelude。
    """
    from bowhead_core.agents.agent_runner_adapter import register_all_system_manifests
    from bowhead_core.agents.registry import AgentRegistry
    from bowhead_core.subagent import PROFILES

    register_all_system_manifests(AgentRegistry.bootstrap())
    profile = PROFILES["compute"]
    assert "python_exec" in profile.tool_whitelist, (
        "工具名没能穿过 manifest 翻译层活到 profile whitelist —— 发给 LLM 的工具按它过滤，"
        "这意味着模型压根看不到这个工具（第一版起名 python_sandbox 时就是这么死的）"
    )
    assert not profile.system_prompt.startswith("## Python sandbox runtime contract"), (
        "触发了 authoring 伪工具的 runtime prelude —— 那段话会明文告诉模型「别直接调」，与 compute.md ④ 教的直接相反"
    )
