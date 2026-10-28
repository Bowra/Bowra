"""写路径钩子 —— 个人 skill 每次落盘后重跑清点，按状态机写 import_status（tech-design r3 §2/§3.4）.

状态机（content-based，不靠调用方标记）：
    红>0 ： overridden 且 digest 未变 → overridden；否则 → pending
    红=0 ： 曾有状态（pending/cleared/overridden）→ cleared；从未红过 → NULL
`effective = enabled ∧ import_status != "pending"`；enabled 不在这里动。

六条写路径共用：上传、/generate、skill_save_generated、编辑器 `_restamp_user_skill`、copy-to-personal、
聊天 zip 里的 skill 组件（KB confirm 落库）。调用方负责装配 `ResolutionSources`（sources_from_ctx / gateway）。
"""

from __future__ import annotations

import copy
import json
import logging
import re
from dataclasses import replace
from pathlib import Path
from typing import Any

import anyio
from bowhead_skill.loader import load_skill
from bowhead_skill.script_langs import script_execution_mode

from bowhead_core.skill_bundle_lock import skill_bundle_lock
from bowhead_core.skill_import.inventory import BundleInventory, ResolutionSources, inventory_from_dir
from bowhead_core.skill_import.sources import (
    _normalize_dist,
    import_names_for_dists,
    sandbox_node_packages,
    sandbox_python_dists,
    sandbox_python_modules,
)
from bowhead_core.skill_instance_store import SkillInstance, SkillInstanceStore
from bowhead_core.skill_upload import bundle_sha256
from bowhead_skill.catalog import builtin_skill_names

logger = logging.getLogger(__name__)

MAX_IMPORT_REPORT_BYTES = 64 * 1024


def import_review_summary(instance: SkillInstance | None, offset: int = 0) -> dict:
    """Project bounded stored findings separately from execution evidence."""
    try:
        report = json.loads(instance.import_report_json or "{}") if instance else {}
    except (ValueError, TypeError):
        report = {}
    if not isinstance(report, dict):
        report = {}
    findings = report.get("findings", [])
    findings = findings if isinstance(findings, list) else []
    page = [
        {key: str(item.get(key, ""))[:512] for key in ("code", "severity", "path", "line", "subject", "message_en")}
        for item in findings[offset : offset + 10]
        if isinstance(item, dict)
    ]
    return {
        "status": instance.import_status if instance else None,
        "report_recorded": bool(report),
        "findings": page,
        "next_finding_offset": offset + 10 if offset + 10 < len(findings) else None,
        "source_truncated": bool(report.get("truncated")),
        "business_trial": "not_established_by_import_inspection",
    }


def next_import_status(prior_status: str | None, prior_digest: str | None, inv: BundleInventory) -> str | None:
    if inv.red_count > 0:
        if prior_status == "overridden" and prior_digest == inv.findings_digest:
            return "overridden"
        return "pending"
    if prior_status in ("pending", "cleared", "overridden"):
        return "cleared"
    return None


def declared_sandbox_requirements(skill_dir: Path) -> set[str]:
    """frontmatter ``sandbox_requirements``（wheelhouse 长尾包声明）→ dist 名集；复用 loader 的解析."""
    try:
        reqs = load_skill(skill_dir).sandbox_requirements
    except Exception:  # noqa: BLE001 — validator reports unloadable bundles; this affects only one yellow/red finding
        return set()
    return {re.split(r"[<>=!~\[; ]", str(s).strip(), maxsplit=1)[0] for s in reqs if str(s).strip()}


def with_skill_requirements(sources: ResolutionSources, skill_dir: Path) -> ResolutionSources:
    """把该 skill 声明的 sandbox_requirements 分成两堆：**装得上的**并进 python 模块集，装不上的单独记名。

    ★ 这里此前是「声明即放行」，而 `sandbox_python_modules()` 本来就已经包含 wheelhouse ⇒
    这一并集**唯一能新增的，恰恰是两份 SSOT 里都没有、离线装不上的包**。也就是说：写一行
    `sandbox_requirements: [foo]` 就能让红消失，而运行时 `pip --no-index` 根本找不到 foo。
    典型的「读取面放行、执行面失败」。现在装不上的只用来把文案说准，不再抵消 finding。
    """
    reqs = declared_sandbox_requirements(skill_dir)
    if not reqs or sources.python_modules is None:
        return sources
    installable = {r for r in reqs if _normalize_dist(r) in sandbox_python_dists()}
    unavailable = set(reqs) - installable
    # Keep dist and import names: import-only matching misses unmapped distributions (python-magic→magic).
    unavailable_names = import_names_for_dists(unavailable) | {_normalize_dist(u) for u in unavailable}
    return replace(
        sources,
        python_modules=sources.python_modules | import_names_for_dists(installable),
        declared_unavailable=sources.declared_unavailable | frozenset(unavailable_names),
    )


def _report_bytes(data: dict[str, Any]) -> tuple[str, int]:
    text = json.dumps(data, ensure_ascii=False)
    return text, len(text.encode("utf-8"))


# Inventory-owned keys; other keys from `extra` are dropped wholesale at the final fallback.
_INVENTORY_KEYS = frozenset(
    {"version", "layout_digest", "grade", "red_count", "findings_digest", "truncated", "components", "findings", "components_dropped"}
)
_CAPABILITY_KINDS = ("skill_bundle", "agent_md", "plugin_manifest", "openapi_spec", "mcp_config")


def serialize_report(inv: BundleInventory, *, extra: dict[str, Any] | None = None) -> str:
    """返回 **保证** ≤ MAX_IMPORT_REPORT_BYTES 的 JSON；不动传入的 inventory（深拷贝后再裁）。

    `extra` 里的键（KB 路径的 `landed_skills`）与组件同级写进报告；它**参与裁剪的最后两级**——
    豁免它会让整条梯子失效（review 实测：200 条 landed_skills 把报告顶到 178KB，梯子见底仍超限 ⇒
    整行不落库，只剩一条 warning）。裁剪时先压成 `{name,status,import_status}`，再截条数。

    裁剪梯子（每级先量，够了就停，都标 truncated）：① 组件的路径列表裁到 20 → ② 丢非能力组件（只留计数）
    → ③ 能力组件压成 {kind, path, name} → ④ 能力组件数封顶 200 → ⑤ 从尾裁 findings（红排前，先掉黄）
    → ⑥ 组件清空只留计数。review 实测：50 个 skill 的包 67KB（此前只裁 findings 就把 200 条 findings 全丢
    还超限）、2000 个 skill 19MB。
    """
    data = copy.deepcopy(inv.to_dict())
    for key, value in (extra or {}).items():
        # Do not overwrite inventory-owned keys (`grade` / `components` / `findings` …): that creates conflicting sources of truth.
        data.setdefault(key, copy.deepcopy(value))
    findings = sorted(data["findings"], key=lambda f: 0 if f["severity"] == "red" else 1)
    data["findings"] = findings
    text, size = _report_bytes(data)
    if size <= MAX_IMPORT_REPORT_BYTES:
        return text
    data["truncated"] = True
    for comp in data["components"]:
        meta = comp.get("meta") or {}
        for key in ("member_paths", "references", "scripts"):
            if key in meta:
                meta[key] = meta[key][:20]
    text, size = _report_bytes(data)
    if size <= MAX_IMPORT_REPORT_BYTES:
        return text
    keep = [c for c in data["components"] if c.get("kind") in _CAPABILITY_KINDS]
    data["components_dropped"] = len(data["components"]) - len(keep)
    data["components"] = keep
    text, size = _report_bytes(data)
    if size <= MAX_IMPORT_REPORT_BYTES:
        return text
    data["components"] = [{"kind": c["kind"], "path": c["path"], "meta": {"name": (c.get("meta") or {}).get("name", "")}} for c in keep]
    text, size = _report_bytes(data)
    if size <= MAX_IMPORT_REPORT_BYTES:
        return text
    if len(data["components"]) > 200:
        data["components_dropped"] += len(data["components"]) - 200
        data["components"] = data["components"][:200]
        text, size = _report_bytes(data)
        if size <= MAX_IMPORT_REPORT_BYTES:
            return text
    landed = data.get("landed_skills")
    if isinstance(landed, list) and landed:
        # Reduce to fields rendering actually needs. **Keep** `declared_name` and `red_count`: the former matches renamed landed skills
        # (dropping it falsely reports imported skills as absent, observed in re-review); the latter is this skill's own red count.
        data["landed_skills"] = [
            {
                "name": str(x.get("name", ""))[:64],
                "declared_name": str(x.get("declared_name", ""))[:64],
                "status": x.get("status"),
                "import_status": x.get("import_status"),
                "red_count": x.get("red_count"),
            }
            for x in landed
            if isinstance(x, dict)
        ]
        text, size = _report_bytes(data)
        if size <= MAX_IMPORT_REPORT_BYTES:
            return text
        if len(data["landed_skills"]) > 50:
            data["landed_skills_dropped"] = len(data["landed_skills"]) - 50
            data["landed_skills"] = data["landed_skills"][:50]
            text, size = _report_bytes(data)
            if size <= MAX_IMPORT_REPORT_BYTES:
                return text
    while findings:
        findings = findings[:-1]
        data["findings"] = findings
        text, size = _report_bytes(data)
        if size <= MAX_IMPORT_REPORT_BYTES:
            return text
    data["components_dropped"] += len(data["components"])
    data["components"] = []
    text, size = _report_bytes(data)
    if size <= MAX_IMPORT_REPORT_BYTES:
        return text
    # Unconditional final fallback: `extra` may have any shape (non-list landed_skills, other keys), missed by every level above.
    # Prefer dropping it entirely to returning an oversized string, which prevents row persistence (re-review observed 200KB responses).
    for key in [k for k in data if k not in _INVENTORY_KEYS]:
        data.pop(key)
        data["extra_dropped"] = True
    text, _size = _report_bytes(data)
    return text


def _prior_digest(inst: Any) -> str | None:
    raw = getattr(inst, "import_report_json", None)
    if not raw:
        return None
    try:
        return (json.loads(raw) or {}).get("findings_digest")
    except (ValueError, TypeError):
        return None


async def apply_import_inventory(
    store: SkillInstanceStore,
    *,
    user_id: str,
    name: str,
    skill_dir: Path,
    sources: ResolutionSources,
    expected_content_hash: str | None = None,
) -> BundleInventory | None:
    """Inspect one locked package; settle only against its original review state.

    An unavailable or stale inspection leaves current state unchanged. Returning
    None avoids legacy callers clearing a newer pending state in their exception
    fallback. This result is not a completed validation or permission grant.
    """
    try:
        with skill_bundle_lock(skill_dir):
            inst = await store.get(user_id=user_id, name=name)
            if inst is None or Path(inst.source_path).resolve() != skill_dir.resolve():
                return None
            digest = "sha256:" + bundle_sha256(skill_dir)
            if expected_content_hash is not None and digest != expected_content_hash:
                return None
            sources = with_skill_requirements(sources, skill_dir)
            inv = await anyio.to_thread.run_sync(inventory_from_dir, skill_dir, sources)
            status = next_import_status(inst.import_status, _prior_digest(inst), inv)
            report = serialize_report(inv) if (status is not None or inv.findings) else None
            if not await store.set_import_inventory(expected=inst, content_hash=digest, import_status=status, import_report_json=report):
                return None
            if status == "pending":
                logger.info("skill_import.pending user=%s skill=%s red=%d", user_id, name, inv.red_count)
            return inv
    except Exception as exc:
        logger.warning("skill_import.inventory_not_applied user=%s skill=%s error_type=%s", user_id, name, type(exc).__name__)
        return None


# ── Resolution-source assembly: turns and gateway share one assembler; inconsistent rules would flip overridden fingerprints
# between saves in Settings and overwrites in chat. ──


async def build_sources(
    *,
    registry: Any | None,
    skill_catalog: Any | None,
    personal_store: Any | None,
    user_id: str | None,
    team_store: Any | None,
    team_id: str | None,
    team_tool_names: frozenset[str] | None = None,
) -> ResolutionSources:
    """任何一块装不出来 ⇒ 对应规则不判（None），不让整份变红、更不让保存失败."""
    tool_names: frozenset[str] | None
    try:
        tool_names = frozenset(registry.list_names()) if registry is not None else None
    except Exception:  # noqa: BLE001
        logger.warning("skill_import.sources.tool_registry_failed")
        tool_names = None
    if tool_names is not None and team_tool_names:
        tool_names = tool_names | team_tool_names  # Reserved for batch 2: determines team_tools_known then
    skill_names: set[str] = set(builtin_skill_names())
    if skill_catalog is not None:
        try:
            skill_names.update(skill_catalog.list_names())
        except Exception:  # noqa: BLE001
            logger.warning("skill_import.sources.skill_catalog_failed")
    if personal_store is not None and user_id:
        try:
            skill_names.update(i.name for i in await personal_store.list_for_user(user_id=user_id))
        except Exception:  # noqa: BLE001
            logger.warning("skill_import.sources.personal_skills_failed")
    if team_store is not None and team_id:
        try:
            if hasattr(team_store, "list_team_skills"):
                rows = await team_store.list_team_skills(team_id, only_enabled=True)
            else:
                rows = await team_store.list(team_id, only_enabled=True)
            skill_names.update(str(getattr(r, "name", "")) for r in rows)
        except Exception:  # noqa: BLE001
            logger.warning("skill_import.sources.team_skills_failed")
    # Batch 1 skips teammcp.* references: team tools intersect actor/entitlement, and turns (tool_view=None without overlay)
    # differ from Settings' catalog service. Checking now would flip overridden fingerprints; enable after batch 2 unifies assembly.
    return ResolutionSources(
        tool_names=tool_names,
        skill_names=frozenset(n for n in skill_names if n),
        team_tools_known=False,
        script_mode=script_execution_mode(has_scripts=True),
        python_modules=sandbox_python_modules(),
        node_packages=sandbox_node_packages(),
    )


async def sources_from_ctx(ctx: Any, tool_registry: Any | None = None) -> ResolutionSources:
    """turn 内：tool_view（含团队 overlay）优先；没有 overlay 时团队工具名视为未知（不判 teammcp.*）."""
    view = getattr(ctx, "tool_view", None)
    return await build_sources(
        registry=view or tool_registry,
        skill_catalog=getattr(ctx, "skill_catalog", None),
        personal_store=getattr(ctx, "skill_instance_store", None),
        user_id=getattr(ctx, "user_id", None),
        team_store=getattr(ctx, "team_skill_store", None),
        team_id=getattr(ctx, "team_id", None),
        team_tool_names=None if view is None else frozenset(),
    )
