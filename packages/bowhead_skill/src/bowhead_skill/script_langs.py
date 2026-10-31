"""Skill 脚本语言契约 SSOT 读取（``catalog/script_langs.json``）+ 多运行时 flag.

skill-multiruntime S1：单一来源、四处引用（校验器 / loader / 镜像 build+image-contract
测试 / skill-creator）。运行时不再各写一份扩展名→解释器映射（三处漂移是同事踩坑的根）。

flag OFF（默认）时 :func:`entry_extensions` 收窄回 ``{'.py'}``——上传校验与 loader 扫描
据此天然回退到"只 .py"，保证 flag-OFF 逐字节不变。
"""

from __future__ import annotations

import json
import os
from functools import lru_cache
from typing import Any

_FLAG_ENV = "BOWHEAD_SKILL_MULTIRUNTIME"
_INVOKE_COMMAND_ENV = "BOWHEAD_SKILL_INVOKE_COMMAND"


def multiruntime_enabled() -> bool:
    """S1 feature flag（默认 off）。ON 才放行 ``.sh/.js/.mjs`` + 多运行时新校验。

    默认值**按部署显式开启**，不翻成全局 ON（2026-09-08 翻过一次又回退）：执行期授权
    `resource_access.resource_grant_targets_for_tool` 把 ``skill_read`` 与 ``skill_invoke``
    都映射成同一个 ``("skill", name, "use")``。已发布 Agent 现额外执行 read/invoke
    操作 scope，但普通/legacy 入口、精确材料绑定及审批恢复还未完整分层；OFF 时
    `tools.py` 对用户/团队 skill 的 fail-closed 总闸仍须保留。按部署显式设
    ``BOWHEAD_SKILL_MULTIRUNTIME=1`` 不等于已完成精确 invoke 授权验收；全入口验收后再议默认。"""
    return os.environ.get(_FLAG_ENV, "0").strip() == "1"


def invoke_command_enabled() -> bool:
    """follow-up② feature flag（默认 off）：`skill_invoke` 的 model-driven ``command``
    argv 路（让上传 skill 按 SKILL.md 文档化命令跑，而非固定 ``--input/--workdir`` shim）。

    是 :func:`multiruntime_enabled` 的**严格子门**——command 模式只在 multiruntime rewrite
    分支内可达（staging 在此发生）。回滚方向独立（关它 → shim-only，不动 S1/S2）。
    """
    return os.environ.get(_INVOKE_COMMAND_ENV, "0").strip() == "1"


def argv0_allowlist() -> frozenset[str]:
    """follow-up②：model-driven ``command`` 的 argv[0] 白名单（defense-in-depth）。

    = SSOT 全量解释器（``python3``/``node``/``bash``）∪ ``guaranteed_cli`` 命令名。
    **不新写映射**（复用 SSOT），符合四处引用铁律。``bash -c "<shell>"`` 仍可跑任意 shell，
    但只在沙箱内（沙箱是信任边界，见 design §4.2）；此闸只挡「把 staged 二进制直接当 argv[0]」。
    """
    interps = {str(v) for v in all_entry_extensions().values()}
    # Interpreter aliases supplied by the image: ecosystem SKILL.md files commonly use `python`, not `python3`, in commands
    # (Anthropic/OpenAI documentation and this slides skill's bowhead-integration.md all use `python scripts/…`).
    # Live-stack LLM dogfood found the model following SKILL.md with `python …`, failing the argv[0] gate and forcing a fallback retry.
    # In the image, `command -v python` = /usr/local/bin/python (→python3), so allowing it is safe.
    interps |= {"python"}
    clis = {e["cmd"] for e in guaranteed_cli()}
    return frozenset(interps | clis)


def all_entry_extensions() -> dict[str, str]:
    """SSOT 全量 ``ext→interpreter`` 映射（**不看 flag**；build / image-contract / 文档用）."""
    ext = _raw_spec().get("entry_extensions", {})
    return {str(k): str(v) for k, v in ext.items()}


def entry_extensions() -> frozenset[str]:
    """当前放行的 entry 扩展名集（**flag 感知**）。OFF → ``{'.py'}``."""
    if not multiruntime_enabled():
        return frozenset({".py"})
    return frozenset(all_entry_extensions())


def interpreter_for(ext: str) -> str | None:
    """扩展名 → 规范解释器（``.js`` → ``node``）；未知返 None."""
    return all_entry_extensions().get(ext.lower())


def guaranteed_runtimes() -> dict[str, str]:
    """image-contract：保证存在的运行时 → 版本前缀（``node`` → ``22``）."""
    return {str(k): str(v) for k, v in _raw_spec().get("guaranteed_runtimes", {}).items()}


def guaranteed_cli() -> list[dict[str, str]]:
    """image-contract：保证存在的 CLI ``{cmd, pkg}`` 对（真实命令名，如 ``fdfind`` / ``7zz``）."""
    out: list[dict[str, str]] = []
    for e in _raw_spec().get("guaranteed_cli", []):
        if isinstance(e, dict) and e.get("cmd") and e.get("pkg"):
            out.append({"cmd": str(e["cmd"]), "pkg": str(e["pkg"])})
    return out


def guaranteed_node_packages() -> list[dict[str, str]]:
    """image-contract（S2）：镜像根 ``/node_modules`` 预置的 npm 包契约 ``{name, version}``.

    仅列出的顶层包是契约，传递依赖不承诺；权威安装源 =
    ``catalog/python_sandbox/node_deps/package.json``（UT 防漂移绑定）。
    """
    out: list[dict[str, str]] = []
    for e in _raw_spec().get("guaranteed_node_packages", []):
        if isinstance(e, dict) and e.get("name") and e.get("version"):
            out.append({"name": str(e["name"]), "version": str(e["version"])})
    return out


@lru_cache(maxsize=1)
def _raw_spec() -> dict[str, Any]:
    """读 SSOT JSON（catalog 目录随包不可变 → lru_cache 一次）."""
    from bowhead_skill.catalog import builtin_catalog_dir

    path = builtin_catalog_dir() / "script_langs.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path}: script_langs.json must be a JSON object")
    return data


def script_execution_mode(*, has_scripts: bool, builtin: bool = False, knowledge: bool = False, name: str = "") -> str:
    """配置层可执行性；sandbox 表示允许调度，不代表环境健康或脚本已经试跑。"""
    if not has_scripts or knowledge:
        return "none"
    if builtin:
        return "sandbox" if name == "latex-report" else "builtin"
    return "sandbox" if multiruntime_enabled() else "disabled"
