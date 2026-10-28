"""教练可起草资产的配置面组装——路由（采纳）与后台办案共用的**唯一**实现（tech-design §2.8.3 ②）。

一个概念一实现：以前组装散在 `_adopt_agent_def / _adopt_team_skill` 里；后台办案若另抄一份，
可见性门（creator_only 技能、working copy 所有权）和 `surface_fits` 就会各算各的。

调用方给一个 ``actor_user_id``：路由给采纳人；后台给**该资产的资源 owner**
（`resolve_casework_actor`）——系统 actor 不比 owner 看得更多。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from bowhead_core.agent_release_read_store import AgentReleaseReadStore
from bowhead_core.agent_revision_store import (
    AgentReleaseCutoverRequiredError,
    AgentRevisionError,
)
from bowhead_core.coach_drafting import surface_fits
from bowhead_core.feedback_candidate_store import FeedbackCandidate
from bowhead_core.user_store import UserStore

from bowhead_gateway.routes.team_skills import _read_body_md

COACH_KINDS = frozenset({"agent_def", "team_skill"})


class SurfaceError(Exception):
    """带稳定 code；路由映射成 HTTP，worker 映射成 reason_code。"""

    def __init__(self, code: str, *, http_status: int) -> None:
        super().__init__(code)
        self.code = code
        self.http_status = http_status


@dataclass(frozen=True)
class Surface:
    config_surface: dict[str, Any]
    prefill_kind: str
    base: dict[str, Any]
    mounted_assets: list[dict[str, str]]
    fits: bool


def editable_source(detail: dict[str, Any]) -> dict[str, Any] | None:
    """编辑器起草的基线：有 working copy 用它，否则用 live 发布定义（116 dogfood 洞①）。"""

    wc = detail.get("working_copy")
    if isinstance(wc, dict) and wc:
        return wc
    live = detail.get("live")
    return live if isinstance(live, dict) and live else None


def definition_sha256(detail: dict[str, Any]) -> str:
    source = editable_source(detail) or {}
    definition = source.get("definition")
    return hashlib.sha256(json.dumps(definition or {}, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _json_list(definition: dict[str, Any], key: str) -> list[str]:
    raw = definition.get(key)
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            raw = None
    return [x for x in (raw if isinstance(raw, list) else []) if isinstance(x, str) and x]


async def _mounted_assets_from_definition(store: UserStore, team_id: str, definition: dict[str, Any]) -> list[dict[str, str]]:
    """agent 定义里**转案端点真的收得下**的改道目标。

    116 真机（2026-09-09）逮到：曾把 `tool_refs_json` 里的名字一律当 `tool_source` 放进清单，
    但那里面是**内置工具名**（`bi_schema` 等），不在团队工具源表里 ⇒ 教练选中它、UI 画出「转案到「bi_schema」」、
    用户一点必然 404。可改道清单必须与 `_reroute_target_exists` 同一把尺子：只放团队里真有、且转过去有人能审批的资产。
    改不到任何登记资产时清单为空——教练照样能只给文字理由（`target_*` 置空即此语义）。
    """

    out: list[dict[str, str]] = []
    for name in _json_list(definition, "skills_json"):
        sk = await store.get_team_skill(team_id, name)
        if sk is not None and (sk.visibility or "team_wide") == "team_wide" and (getattr(sk, "approval", None) or "approved") == "approved":
            out.append({"kind": "team_skill", "ref": name})
    for ref in _json_list(definition, "tool_refs_json"):
        if await store.get_team_tool_source(team_id, ref) is not None:
            out.append({"kind": "tool_source", "ref": ref})
    return out


async def build_agent_surface(store: UserStore, *, team_id: str, agent_id: str, actor_user_id: str) -> Surface:
    try:
        detail = await AgentReleaseReadStore(store.db_path).working_copy(team_id=team_id, agent_id=agent_id, actor_user_id=actor_user_id)
    except AgentReleaseCutoverRequiredError as exc:
        raise SurfaceError("agent_release_cutover_required", http_status=409) from exc
    except AgentRevisionError as exc:
        raise SurfaceError("feedback_candidate_not_found", http_status=404) from exc
    source = editable_source(detail)
    if source is None:
        raise SurfaceError("agent_working_copy_missing", http_status=409)
    definition = source.get("definition") if isinstance(source.get("definition"), dict) else {}
    return Surface(
        config_surface=detail,
        prefill_kind="agent_system_prompt",
        base={
            "definition_sha256": definition_sha256(detail),
            # FE compares with editor working-copy row_version: mismatch -> read-only + realignment.
            "working_copy_row_version": (detail.get("working_copy") or {}).get("row_version"),
        },
        mounted_assets=await _mounted_assets_from_definition(store, team_id, definition),
        fits=surface_fits(detail),
    )


async def build_skill_surface(store: UserStore, *, team_id: str, skill_id: str, actor_user_id: str) -> Surface:
    sk = await store.get_team_skill(team_id, skill_id)
    if sk is None:
        raise SurfaceError("feedback_candidate_not_found", http_status=404)
    if (sk.visibility or "team_wide") != "team_wide" and sk.created_by != actor_user_id:
        # Same as team_skills._visible_to: creator_only Skills expose no metadata to others.
        raise SurfaceError("feedback_candidate_not_found", http_status=404)
    body_raw = _read_body_md(sk)
    if not body_raw:
        raise SurfaceError("team_skill_body_missing", http_status=409)
    surface = {
        "kind": "team_skill",
        "skill": {"id": sk.id, "name": sk.name, "display_name": sk.display_name, "description": sk.description},
        "body_raw": body_raw,  # Includes frontmatter; editor writes entire SKILL.md.
    }
    return Surface(
        config_surface=surface,
        prefill_kind="team_skill_body",
        base={"body_sha256": sha256_text(body_raw), "updated_at": sk.updated_at},
        mounted_assets=[],
        fits=surface_fits(surface),
    )


async def build_surface(store: UserStore, cand: FeedbackCandidate, *, actor_user_id: str) -> Surface:
    if cand.target_kind == "agent_def":
        return await build_agent_surface(store, team_id=cand.team_id, agent_id=cand.target_ref, actor_user_id=actor_user_id)
    if cand.target_kind == "team_skill":
        return await build_skill_surface(store, team_id=cand.team_id, skill_id=cand.target_ref, actor_user_id=actor_user_id)
    raise SurfaceError("feedback_adopt_kind_unsupported", http_status=409)


async def resolve_casework_actor(store: UserStore, cand: FeedbackCandidate) -> str | None:
    """后台办案以谁的身份看配置面：agent→资源 owner（COALESCE(owner,creator)）；技能→创建者。"""

    if cand.target_kind == "agent_def":
        ag = await store.get_team_subagent(cand.team_id, cand.target_ref)
        if ag is None:
            return None
        return getattr(ag, "owner_user_id", None) or getattr(ag, "created_by", None)
    if cand.target_kind == "team_skill":
        sk = await store.get_team_skill(cand.team_id, cand.target_ref)
        return getattr(sk, "created_by", None) if sk is not None else None
    return None


def base_matches(surface: Surface, cached_base: dict[str, Any]) -> bool:
    """缓存复用判据＝重算指纹比对；row_version/updated_at 只作 FE 提示（None==None 不能当一致）。"""

    if surface.prefill_kind == "agent_system_prompt":
        return bool(cached_base.get("definition_sha256")) and cached_base.get("definition_sha256") == surface.base.get("definition_sha256")
    if surface.prefill_kind == "team_skill_body":
        return bool(cached_base.get("body_sha256")) and cached_base.get("body_sha256") == surface.base.get("body_sha256")
    return False


# ── Batch 3d: caller's own imported Skill (skill-import interface request §1/§1b) ──

USER_SKILL_COACHABLE_STATUSES = frozenset({"pending", "overridden"})  # overridden: knowingly enabled despite red findings; most in need of help.
_IMPORT_REPORT_MAX_FINDINGS = 60
_SURFACE_MAX_FILES = 200
_SURFACE_MAX_NAMES = 300
# Output feasibility: coach must rewrite the **entire** SKILL.md (8192 tokens / 60–120s); a 60k input limit is irrelevant here.
USER_SKILL_BODY_MAX_CHARS = 16_000


@dataclass(frozen=True)
class UserSkillSurface:
    surface: Surface
    body_raw: str  # CRLF→LF normalized: model outputs LF; otherwise one edit diffs the whole file.
    findings_red: list[dict[str, Any]]
    findings_yellow: list[dict[str, Any]]
    finding_ids: frozenset[str]
    truncated: bool
    files: list[dict[str, Any]]
    available_tools: frozenset[str]
    original_allowed_tools: frozenset[str]


def _parse_import_report(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def frontmatter_of(body: str) -> dict[str, Any]:
    """与保存路径（write_bundle_body → _split_frontmatter）**同一把尺子**读 frontmatter；读不出＝空。"""

    from bowhead_core.skill_upload import SkillUploadError, _split_frontmatter

    try:
        fm, _ = _split_frontmatter(body)
    except SkillUploadError:
        return {}
    return fm if isinstance(fm, dict) else {}


def allowed_tools_of(fm: dict[str, Any]) -> frozenset[str]:
    raw = fm.get("allowed-tools") if isinstance(fm, dict) else None
    if isinstance(raw, str):
        raw = [x for x in raw.replace(",", " ").split() if x]
    return frozenset(str(x).strip() for x in raw if str(x).strip()) if isinstance(raw, list) else frozenset()


async def build_user_skill_surface(
    skill_instance_store: Any,
    *,
    user_id: str,
    skill_name: str,
    sources: Any | None = None,
) -> UserSkillSurface:
    """本人 ∧ import_status ∈ {pending, overridden} 才成立，否则 404（不存在与无权同码）。

    读三样：`body_raw`（整份 SKILL.md，`read_bundle_manifest`）、包内文件清单（`list_bundle_files` 给 basename，
    这里拼成 `kind/name`，上限 200）、`import_report`（`import_report_json`；可能 truncated，喂教练前只说「至少 N 条」）。
    ``sources``＝skill-import 线的 `ResolutionSources`（本平台真实存在的工具/skill 名）——教练换名只能从这里选。
    findings 只走 evidence 一个通道（消毒），配置面只放计数与 id。基线指纹对正文算（详情 DTO 的 `content_sha256` 是整包指纹）。
    """

    from pathlib import Path

    from bowhead_skill.bundle_files import BUNDLE_FILE_KINDS

    from bowhead_gateway.skill_bundle_files import list_bundle_files, read_bundle_manifest

    inst = await skill_instance_store.get(user_id=user_id, name=skill_name)
    if inst is None or (inst.import_status or "") not in USER_SKILL_COACHABLE_STATUSES:
        raise SurfaceError("feedback_candidate_not_found", http_status=404)
    skill_dir = Path(inst.source_path)
    body_raw = read_bundle_manifest(skill_dir).replace("\r\n", "\n")
    if not body_raw:
        raise SurfaceError("user_skill_body_missing", http_status=409)
    files: list[dict[str, Any]] = []
    for kind in BUNDLE_FILE_KINDS:
        try:
            for name, size in list_bundle_files(skill_dir, kind):
                files.append({"path": f"{kind}/{name}", "size": int(size)})
        except OSError:
            continue
    report = _parse_import_report(inst.import_report_json)
    raw_findings = report.get("findings")
    findings = [f for f in (raw_findings if isinstance(raw_findings, list) else []) if isinstance(f, dict)][:_IMPORT_REPORT_MAX_FINDINGS]
    red = [f for f in findings if f.get("severity") == "red"]
    yellow = [f for f in findings if f.get("severity") == "yellow"]
    tool_names = frozenset(getattr(sources, "tool_names", None) or ()) if sources is not None else frozenset()
    skill_names = frozenset(getattr(sources, "skill_names", None) or ()) if sources is not None else frozenset()
    surface = {
        "kind": "user_skill",
        "skill": {"name": inst.name, "import_status": inst.import_status, "version": getattr(inst, "version", None)},
        "body_raw": body_raw,
        "files": files[:_SURFACE_MAX_FILES] + ([{"path": f"…还有 {len(files) - _SURFACE_MAX_FILES} 个"}] if len(files) > _SURFACE_MAX_FILES else []),
        "import_report": {
            "grade": report.get("grade"),
            "red_count": len(red),
            "yellow_count": len(yellow),
            "truncated": bool(report.get("truncated")),
            "finding_ids": [str(f.get("id")) for f in findings if f.get("id")],
        },
        "available": {"tools": sorted(tool_names)[:_SURFACE_MAX_NAMES], "skills": sorted(skill_names)[:_SURFACE_MAX_NAMES]},
    }
    fits = surface_fits(surface) and len(body_raw) <= USER_SKILL_BODY_MAX_CHARS
    return UserSkillSurface(
        surface=Surface(
            config_surface=surface,
            prefill_kind="user_skill_body",
            base={"body_sha256": sha256_text(body_raw), "import_status": inst.import_status},
            mounted_assets=[],  # Do not redirect this target type.
            fits=fits,
        ),
        body_raw=body_raw,
        findings_red=red,
        findings_yellow=yellow,
        finding_ids=frozenset(str(f.get("id")) for f in findings if f.get("id")),
        truncated=bool(report.get("truncated")),
        files=files,
        available_tools=tool_names,
        original_allowed_tools=allowed_tools_of(frontmatter_of(body_raw)),
    )


def user_skill_evidence(us: UserSkillSurface) -> list[str]:
    """清点结果按红/黄分组喂教练（不可信上下文，走消毒通道；配置面里不重复放）。truncated 时只说「至少」。"""

    def _line(f: dict[str, Any]) -> str:
        loc = f"{f.get('path')}" + (f":{f.get('line')}" if f.get("line") is not None else "") if f.get("path") else ""
        return (
            f"- [{f.get('id') or '?'}] [{f.get('code')}] {f.get('subject') or ''} {loc} — {f.get('message_zh') or f.get('message_en') or ''}".strip()
        )

    at_least = "至少 " if us.truncated else ""
    out = [f"### 红色阻断项（{at_least}{len(us.findings_red)} 条，清零才生效）\n" + ("\n".join(_line(f) for f in us.findings_red) or "（无）")]
    out.append(
        f"### 黄色提示（{at_least}{len(us.findings_yellow)} 条，不阻断；只说清不删）\n"
        + ("\n".join(_line(f) for f in us.findings_yellow) or "（无）")
    )
    return out
