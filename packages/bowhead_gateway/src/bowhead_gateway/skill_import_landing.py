"""聊天 zip 里的 skill 组件落库（tech-design r4 §3.4「拖进聊天框＝设置页上传，同一落点」）.

与 `routes/skills.py:upload_skill` 同一套闸：write capability、按名 grant、配额、`validate_and_extract`、
`store.upsert`、清点钩子、catalog refresh。区别只有两条：不覆盖同名个人 skill（报 `exists`）、
任何失败都只记进结果不抛（KB 入库不能因一个 skill 组件失败而中断）。
"""

from __future__ import annotations

import contextlib
import io
import json
import logging
import shutil
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

import anyio
from bowhead_core.skill_import.hook import apply_import_inventory
from bowhead_core.skill_import.inventory import BundleInventory, Component, ResolutionSources, decode_zip_member_name, sanitize_path
from bowhead_core.skill_upload import SkillUploadError, validate_and_extract
from fastapi import HTTPException, Request

logger = logging.getLogger(__name__)

_MAX_SKILL_ZIP_BYTES = 16 * 1024 * 1024
# Land only in personal scope: chat ZIPs use scope=personal; team/space knowledge bundles skip indexing capability components per decision ④,
# but **never** silently create personal skills for uploaders (review P2: team-KB upload side effects landed in private accounts without UI notice).
_LANDING_SCOPES = frozenset({"personal"})


class _SubtreeError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _skill_subtree_zip(zf: zipfile.ZipFile, inv: BundleInventory, comp: Component) -> bytes:
    """把一个 skill 组件的子树重新打成以 SKILL.md 为根的 zip（validate_and_extract 认这个形状）.

    同名成员出现两次直接拒（review P1：清点看到第一份 SKILL.md 的 name 过闸，extractall 留下的是最后一份，
    真落地的名字和过闸的名字不是同一个 ⇒ 按名 grant 被绕过）。声明大小撒谎 ⇒ `BadZipFile` 在这里抛，
    由调用方按组件记 invalid（不拖累包里其他 skill）。
    """
    root = comp.meta.get("root", "")
    prefix = f"{root}/" if root else ""
    members = set(comp.meta.get("member_paths", [])) | {comp.path}
    buf = io.BytesIO()
    total = 0
    seen: set[str] = set()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as out:
        for info in zf.infolist():
            if info.is_dir():
                continue
            rel = inv.relpath(decode_zip_member_name(info))
            if rel not in members:
                continue
            inner = rel[len(prefix) :] if prefix and rel.startswith(prefix) else rel
            if not inner or ".." in PurePosixPath(inner).parts:
                continue
            if inner in seen:
                raise _SubtreeError("E_SKILL_DUPLICATE_MEMBER")
            seen.add(inner)
            total += int(info.file_size)
            if total > _MAX_SKILL_ZIP_BYTES:
                raise _SubtreeError("E_SKILL_TOO_LARGE")
            with zf.open(info) as fh:
                out.writestr(inner, fh.read())
    return buf.getvalue()


def _reserved_names(request: Request) -> set[str]:
    from bowhead_skill.catalog import builtin_skill_names

    cat = getattr(request.app.state, "skill_catalog", None)
    names: set[str] = set(builtin_skill_names())
    if cat is not None:
        with contextlib.suppress(Exception):
            names |= set(cat.list_names()) if callable(getattr(cat, "list_names", None)) else {str(x.name) for x in cat.list_specs()}
    return names


async def land_skill_components(
    request: Request,
    user: Any,
    *,
    zf: zipfile.ZipFile,
    inv: BundleInventory,
    sources: ResolutionSources,
) -> list[dict[str, Any]]:
    """对包里每个 skill_bundle 组件：按上传者身份落成个人 skill（进钩子 ⇒ 红即 pending）。返回逐个结果.

    子树重打包 + `validate_and_extract`（解压、落盘、搬目录）都是同步 IO，放线程；一个 16MB skill 在 116 那台
    VM 上约 1–2s，×N 个 skill 串行，不能挂在事件循环上（review P2）。线程里跑时事件循环在等它，`zf` 无并发访问。
    """
    from bowhead_gateway.routes import skills as skill_routes

    store = getattr(request.app.state, "skill_instance_store", None)
    results: list[dict[str, Any]] = []
    if store is None:
        return results
    reserved = _reserved_names(request)
    dest_root = (Path(".data") / "user_skills").resolve()
    for comp in inv.components:
        if comp.kind != "skill_bundle":
            continue
        name = str(comp.meta.get("name") or "")
        # This row persists in inventory, then enters Lead prompts: sanitize/cap strings using the same inventory rules.
        item: dict[str, Any] = {
            "name": sanitize_path(name)[:64],
            "declared_name": sanitize_path(name)[:64],
            "path": sanitize_path(comp.path)[:200],
            "status": "invalid",
            "import_status": None,
        }
        results.append(item)
        try:
            await skill_routes._require_skill_capability_for_request(request, user, permission="write")
            if name:
                await skill_routes._require_skill_grant_for_request(request, user, name, permission="write")
        except HTTPException:
            item["status"] = "denied"
            continue
        existing = await store.list_for_user(user_id=user.id)
        user_skills = {i.name: (i.size_bytes or 0) for i in existing if i.source_type in ("user_upload", "user_generated")}

        def _extract(comp: Component = comp, name: str = name, user_skills: dict[str, int] = user_skills) -> Any:
            data = _skill_subtree_zip(zf, inv, comp)
            return validate_and_extract(
                data=data,
                filename=f"{name or 'skill'}.zip",
                user_id=user.id,
                dest_root=dest_root,
                existing_user_skills=user_skills,
                overwrite=False,
                reserved_skill_names=reserved,
            )

        try:
            vs = await anyio.to_thread.run_sync(_extract)
        except _SubtreeError as e:
            item["code"] = e.code
            continue
        except SkillUploadError as e:
            item["status"] = "exists" if e.code == "E_SKILL_NAME_EXISTS" else "invalid"
            item["code"] = e.code
            continue
        except zipfile.BadZipFile:
            item["code"] = "E_SKILL_INVALID_ZIP"
            continue
        except Exception as e:  # noqa: BLE001
            logger.warning("skill_import.landing_failed name=%s err=%s", name, type(e).__name__)
            item["code"] = "E_SKILL_LANDING_FAILED"
            continue
        if vs.name != name:
            # Gate used the inventoried name but extraction produced another; recheck grants for the actual name, removing landed directories if denied.
            try:
                await skill_routes._require_skill_grant_for_request(request, user, vs.name, permission="write")
            except HTTPException:
                shutil.rmtree(str(vs.source_dir), ignore_errors=True)
                item["status"] = "denied"
                continue
        await store.upsert(
            user_id=user.id,
            name=vs.name,
            source_type="user_upload",
            source_path=str(vs.source_dir),
            version=vs.version,
            description=vs.description,
            enabled=True,
            size_bytes=vs.size_bytes,
            expected_content_hash=vs.content_hash,
            # Same column as upload_skill: omitting it makes the dependency compiler mark every invocation risk=unknown/available=False (review P1).
            risk_report_json=json.dumps(vs.risk_report, ensure_ascii=False) if vs.risk_report else None,
            reset_verdict=True,
        )
        item["name"] = vs.name  # declared_name retains the inventoried name; renamed skills resolve through both rendering keys.
        item["status"] = "landed"
        try:
            await apply_import_inventory(store, user_id=user.id, name=vs.name, skill_dir=Path(str(vs.source_dir)), sources=sources)
            refreshed = await store.get(user_id=user.id, name=vs.name)
            item["import_status"] = getattr(refreshed, "import_status", None)
            # This skill's own blocker count (whole-bundle counts are wrong when applied to every skill).
            raw_report = getattr(refreshed, "import_report_json", None)
            if raw_report:
                with contextlib.suppress(ValueError, TypeError):
                    parsed_report = json.loads(raw_report)
                    if isinstance(parsed_report, dict):
                        item["red_count"] = int(parsed_report.get("red_count") or 0)
        except Exception as e:  # noqa: BLE001
            logger.error("skill_import.inventory_failed skill=%s err=%s", vs.name, type(e).__name__)
        catalog = getattr(request.app.state, "skill_catalog", None)
        if catalog is not None:
            with contextlib.suppress(Exception):
                catalog.refresh()
    return results


# ── Shared KB bundle path: inventory + skip set + landing + persistence ──


class BundleImportOutcome:
    """两个 KB 门共用的清点结果载体。清点失败 ⇒ `inventory=None`、跳过集为空、什么都不落（fail-open）."""

    def __init__(self) -> None:
        self.inventory: BundleInventory | None = None
        self.sources: ResolutionSources | None = None
        self.capability_paths: frozenset[str] = frozenset()
        self.landed_skills: list[dict[str, Any]] = []

    def is_capability_member(self, entry: zipfile.ZipInfo) -> bool:
        """按 ZipInfo 判（不是按 KB 门自己解码出的名字）：三处解码器的回退分支不一样（Big5/Shift-JIS 顶层目录
        下 KB 门与清点会解出不同字符串，SKILL.md 就漏进 KB 而 skill 照样落地——review P3）；用同一份解码器."""
        if self.inventory is None or not self.capability_paths:
            return False
        return self.inventory.relpath(decode_zip_member_name(entry)) in self.capability_paths

    def report_dict(self) -> dict[str, Any] | None:
        if self.inventory is None:
            return None
        import json

        from bowhead_core.skill_import.hook import serialize_report

        return json.loads(serialize_report(self.inventory, extra={"landed_skills": self.landed_skills}))


async def inventory_bundle_for_kb(request: Request, user: Any, zf: zipfile.ZipFile) -> BundleImportOutcome:
    """KB 包入库前跑清点（线程内）。只有能力形状的包才产生跳过集；纯文档/数据包与今天完全一样."""
    import anyio
    from bowhead_core.skill_import.inventory import inventory_from_zipfile

    from bowhead_gateway.skill_import_sources import build_resolution_sources

    out = BundleImportOutcome()
    try:
        out.sources = await build_resolution_sources(request, user)
        out.inventory = await anyio.to_thread.run_sync(inventory_from_zipfile, zf, out.sources)
        if out.inventory.capability_shaped():
            out.capability_paths = out.inventory.capability_member_paths()
    except Exception as e:  # noqa: BLE001 — Inventory failures do not affect KB ingestion.
        logger.error("skill_import.kb_inventory_failed err=%s", type(e).__name__)
        out.inventory = None
        out.capability_paths = frozenset()
    return out


async def finish_bundle_import(
    request: Request,
    user: Any,
    *,
    zf: zipfile.ZipFile,
    outcome: BundleImportOutcome,
    bundle_artifact_id: str,
    artifact_db_path: str,
    scope: str = "personal",
) -> None:
    """入库循环之后：落 skill 组件（红 ⇒ pending；只在 personal 域）、按根包 artifact 持久化清点结果."""
    if outcome.inventory is None or outcome.sources is None:
        return
    if outcome.inventory.capability_shaped() and scope in _LANDING_SCOPES:
        try:
            outcome.landed_skills = await land_skill_components(request, user, zf=zf, inv=outcome.inventory, sources=outcome.sources)
        except Exception as e:  # noqa: BLE001
            logger.error("skill_import.kb_landing_failed err=%s", type(e).__name__)
    try:
        from bowhead_core.skill_import.bundle_inventory_store import save_bundle_inventory
        from bowhead_core.skill_import.hook import serialize_report

        # Persist landed_skills too: batch 2's <bundle_import> uses it to state whether a skill landed or is pending;
        # without it, only vague answers are possible (the distinction between attachment and KB paths).
        saved = await save_bundle_inventory(
            artifact_db_path,
            bundle_artifact_id=bundle_artifact_id,
            created_by=user.id,
            inventory_json=serialize_report(outcome.inventory, extra={"landed_skills": outcome.landed_skills}),
        )
        if not saved:
            logger.warning("skill_import.kb_inventory_persist_skipped bundle=%s reason=oversize", bundle_artifact_id)
    except Exception as e:  # noqa: BLE001 — Missing table (unmigrated)/write failure: lose persistence only; response still includes inventory.
        logger.warning("skill_import.kb_inventory_persist_failed err=%s", type(e).__name__)
