"""能力协议块 —— 「手里有这个工具的执行体，该知道怎么用它」。

## 为什么有这个模块

「怎么用 datarc 取数」「多于一行的结果必须出产物」这类纪律，此前只长在 `compute.md`
这一个 agent 的常驻提示词里。116 近 30 天实测：9 个内置 agent 里 8 个手上有 `bi_query`，
只有 compute 拿到了纪律，而**非 compute 的取数占 29.6%**（1297 次里的 384 次）。

协议该挂在 `capability × implementation` 上，不该挂在某个 agent 上——反向验证：
`pbi_*`（Power BI）在整个 `agents/prompts/` 面零命中，换个 connector 这套纪律一个字
都跟不过去。权威见 `docs/sprints/builtin-capability-composition-2026-09-17/`。

## 选中规则（两条，语义刻意不同）

    模块被选中 = 它声明了 anchor_tools 且与本轮工具**有交集**
              ↳ 没声明 anchor_tools → 看它所属 Implementation 的 required_tools 是否**被包含**

`required_tools` 是「全都要有」（⊆），`anchor_tools` 是「有其一即可」（∩）。
后者服务的是「一段协议同时管一族工具」（图与图片、读与写），前者是依赖声明。

⚠ **`required_tools` 为空的 Implementation 不算命中。** 空集的 `⊆` 恒真，照它匹配
等于「所有人都拿到」——`capi_report_canvas` 正是空的（它的依赖表达在 Adapter 的
delegate_tools 上）。同一条保守规则在 `infer_capabilities_from_tools` 里已有先例。

## Who receives the block

The Agent's own definition decides, never a list of routing names: built-in
Agents declare `capability_protocol_block: true` in their manifest, and team
Agents enable the same definition field in the version editor and publish a new
revision. Per the A/B conclusion (tech-design §12.1), compute stays off.

## 三条边界（别扩大）

1. **只注入提示词，不授予任何执行。** 因此**不看** `review_state`：`reviewed` 那道闸管的是
   「这个能力能不能被当作已验证的可执行契约」，与「手里有工具的人该不该知道怎么用」无关。
   ⚠ 反过来也成立：**协议块出现过，不构成 capability 可执行的证据**，消费面的
   `evidence_level` 不受本模块影响。
2. **排除带 exact skill 依赖的 Implementation**（如 `capi_compute_python_skill`）：
   它们要 skill grant 才算数，本层判不了，判了就是假证据。
3. **Consume the advertised Tool snapshot; do not probe connections.** The runner
   supplies names after this turn's visibility/readiness filters, so hidden or
   unregistered Tools cannot activate protocols. The same advertised names still
   produce identical cacheable bytes. Protocol selection grants no execution rights.
"""

from __future__ import annotations

import logging
from collections.abc import Collection

from bowhead_core.agents._generated.tool_renames import canonical_tool_name

logger = logging.getLogger(__name__)

__all__ = ["render_capability_protocols"]

# Token budget for protocol blocks. Exceeding it skips assembly (fail open to an empty string), without truncation:
# half a protocol is more dangerous than none. Scale reference: three modules total about 1.6k tokens.
_PROTOCOL_TOKEN_BUDGET = 4000


def _selected_modules(tool_names: Collection[str]) -> tuple[object, ...]:
    """按上面两条规则挑出本轮该发的协议模块（已去重、未排序）。"""
    from bowhead_core.agent_artifact_capability import default_artifact_registry
    from bowhead_core.agent_capability_catalog import default_catalog

    catalog = default_catalog()
    registry = default_artifact_registry()
    if catalog is None or registry is None:
        return ()

    # Same root cause as C6: unnormalized old tool names look incapable, while dispatch tests pass (silent loss).
    tools = {canonical_tool_name(str(name or "").strip()) for name in tool_names}
    tools.discard("")

    selected: dict[tuple[str, int], object] = {}
    for definition in catalog.definitions:
        for implementation in catalog.implementations_for(definition):
            if implementation.lifecycle != "active" or implementation.required_skills:
                continue
            required = set(implementation.required_tools)
            impl_matched = bool(required) and required.issubset(tools)
            for ref in implementation.prompt_modules:
                try:
                    module, _ = registry.get_prompt_module(ref.id, ref.version)
                except Exception:  # noqa: BLE001 — One missing module must not break the whole block.
                    logger.warning("capability_protocol.module_missing ref=%s@%s", ref.id, ref.version)
                    continue
                if module.lifecycle != "active":
                    continue
                anchors = {canonical_tool_name(name) for name in module.anchor_tools}
                hit = bool(anchors & tools) if anchors else impl_matched
                if hit:
                    selected[(ref.id, ref.version)] = ref
    return tuple(selected.values())


def render_capability_protocols(tool_names: Collection[str]) -> str:
    """渲染本轮的协议块；没有命中的模块时返回空串（调用方整段省略）。

    fail-open：catalog / registry 加载不了、摘要对不上、预算超了 —— 一律返回空串并记日志。
    协议缺席只是回到今天的状态；抛异常会让整个 turn 挂掉。
    """
    from bowhead_core.agent_artifact_capability import default_artifact_registry
    from bowhead_core.token_estimator import estimate_tokens

    refs = _selected_modules(tool_names)
    if not refs:
        return ""
    registry = default_artifact_registry()
    if registry is None:
        return ""
    try:
        bundle = registry.compile_prompt_bundle(
            refs,  # type: ignore[arg-type]
            max_tokens=_PROTOCOL_TOKEN_BUDGET,
            token_counter=estimate_tokens,
        )
    except Exception as exc:  # noqa: BLE001 — See docstring: omit protocols rather than crash the turn.
        logger.warning("capability_protocol.compile_failed err=%s", exc)
        return ""
    return bundle.content.strip()
