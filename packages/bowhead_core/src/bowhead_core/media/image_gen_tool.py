"""`image_generate` 工具 —— 文生图（multimodal-v1，整体镜像 web_search.py）。

- 注册 env/key-gated：active image_gen 模型缺 key / 无 media 块 → 不注册，
  Lead/peer 看不到这个工具，环境优雅降级（web_search 同款）。
- handler 在调用时按团队设置解析 MediaModelRegistry（不读取环境变量）。
- 默认生成图 bytes 即刻 `artifact_store.put_bytes(kind="deliverable")` —— 自动获得
  chat 下载/预览 chip 链路（_collect_inline_artifacts）+ inject=stub 不进上下文。
  V2A design 背景图可传 `purpose="design_background"`，此时 artifact 是中间素材
  `kind="tool_data"`，不进入最终 deliverables。
- 失败 fail-soft：返结构化 ``{"error_kind": ...}`` 不 raise（LLM 据此改 prompt
  重试或诚实报告）。
- mime 永远来自客户端魔术字节推导（绝不信 provider 头，review SEC-P0-2）。
"""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from bowhead_router.types import ToolSchema

from bowhead_core.design_flags import is_design_v2a_enabled
from bowhead_core.media.image_gen import (
    DEFAULT_SIZE_PRESET,
    SIZE_PRESETS,
    ImageGenClient,
    ImageGenError,
)
from bowhead_core.tool_registry import ToolExecutionContext, ToolRegistry, ToolSpec

if TYPE_CHECKING:
    from bowhead_core.media.config_loader import MediaModelRegistry

# Resolve the team media registry asynchronously by team_id; missing configuration disables image generation for that team.
TeamMediaResolver = Callable[[str], Awaitable["MediaModelRegistry | None"]]

logger = logging.getLogger(__name__)

# Harden prompt input (review SEC-P1-2): strip control characters and apply a soft length cap.
_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_PROMPT_MAX_CHARS = 2000

_EXT_BY_MIME = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}
_STATUS_LABELS = {
    "auth",
    "bad_request",
    "content_policy",
    "disabled",
    "done",
    "persist_failed",
    "timeout",
    "upstream",
}


def _metric_inc(labels: dict[str, str]) -> None:
    safe = dict(labels)
    status = safe.get("status") or "upstream"
    if status not in _STATUS_LABELS:
        safe["status"] = "upstream"
    try:
        from bowhead_core import metrics as _m

        _m.inc("bowhead_image_generate_purpose_total", labels=safe)
    except Exception as exc:
        logger.debug("image_generate.metric_failed err=%s", exc)


def _slug(s: str, n: int = 40) -> str:
    """文件名 slug（保留 CJK，照 web_search._slug 语义）。"""
    s = re.sub(r"[^\w一-鿿-]+", "_", _CTRL_RE.sub("", s or "").strip()).strip("_")
    return (s or "image")[:n]


def _schema() -> ToolSchema:
    return ToolSchema(
        name="image_generate",
        description=(
            "用文生图模型生成图片（海报 / 插画 / banner / 宣传图）. "
            "prompt 写完整：主体 + 场景 + 视觉风格 + 配色 + 构图，"
            "普通 raster 交付图可把图内文字逐字写入 prompt；可编辑海报背景请传 "
            "purpose=design_background，prompt 不包含必要文案，再用 canvas_create_design "
            "把文案放进 text layers. 返回 artifact_id. "
            "失败时返 error_kind（content_policy → 改写 prompt 重试；timeout → 可重试一次）. "
            "默认 n=1；n≥2 仅在用户明确要多方案对比时用（按张计费）. "
            "**不**用于编辑已有图片."
        ),
        input_schema={
            "type": "object",
            "required": ["prompt"],
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": (
                        "完整图面描述（建议 ≤500 字）：主体/场景/风格/配色/构图 + 普通 raster 图可逐字引号给出图内文字；design 背景不要包含必要文案."
                    ),
                },
                "purpose": {
                    "type": "string",
                    "enum": ["deliverable", "design_background"],
                    "description": ("默认 deliverable。生成可编辑 design 背景素材时传 design_background；输出不作为最终交付件展示。"),
                },
                "size": {
                    "type": "string",
                    "enum": list(SIZE_PRESETS),
                    "description": ("画幅预设：square=方图; portrait_3_4 / portrait_9_16=竖版海报; banner_16_9=横幅. 默认 portrait_3_4."),
                },
                "n": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 4,
                    "description": "张数 1-4（默认 1）. 多张=多方案对比，逐张顺序生成.",
                },
                "negative_prompt": {
                    "type": "string",
                    "description": "（可选）不希望出现的元素.",
                },
            },
        },
    )


def _make_handler(media_registry: MediaModelRegistry, team_media_resolver: TeamMediaResolver | None = None):
    """每次调用只解析团队 media registry；不使用全局或环境变量 Provider。"""

    async def _handler(args: dict[str, Any], ctx: ToolExecutionContext) -> Any:
        purpose = str(args.get("purpose") or "deliverable").strip()
        if purpose not in {"deliverable", "design_background"}:
            purpose = "deliverable"
        if purpose == "design_background" and not is_design_v2a_enabled():
            _metric_inc({"purpose": purpose, "status": "disabled"})
            return {
                "error_kind": "disabled",
                "message": "editable design is disabled; generate a deliverable image instead",
                "images": [],
            }
        prompt = _CTRL_RE.sub("", str(args.get("prompt") or "")).strip()
        if not prompt:
            _metric_inc({"purpose": purpose, "status": "bad_request"})
            return {"error_kind": "bad_request", "message": "prompt required", "images": []}
        if len(prompt) > _PROMPT_MAX_CHARS:
            logger.warning("image_generate: prompt truncated %d→%d chars", len(prompt), _PROMPT_MAX_CHARS)
            prompt = prompt[:_PROMPT_MAX_CHARS]

        reg = None
        if team_media_resolver is not None and (getattr(ctx, "team_id", "") or ""):
            try:
                team_reg = await team_media_resolver(ctx.team_id)
                if team_reg is not None:
                    reg = team_reg
            except Exception:
                logger.warning("image_generate: team media resolver failed team=%s", getattr(ctx, "team_id", "?"), exc_info=True)
        spec = reg.get_active("image_gen") if reg is not None else None
        if spec is None or not spec.api_key:
            _metric_inc({"purpose": purpose, "status": "auth"})
            return {
                "error_kind": "auth",
                "message": "image generation not configured (no active model/key)",
                "images": [],
            }

        size_preset = str(args.get("size") or DEFAULT_SIZE_PRESET)
        try:
            n = int(args.get("n") or 1)
        except (TypeError, ValueError):
            n = 1
        negative = str(args.get("negative_prompt") or "").strip() or None
        is_deliverable = purpose == "deliverable"

        try:
            client = ImageGenClient(spec)
            generated = await client.generate(
                prompt=prompt,
                size_preset=size_preset,
                n=n,
                negative_prompt=negative,
            )
        except ImageGenError as exc:
            logger.warning("image_generate: %s (%s)", exc.error_kind, str(exc)[:200])
            _metric_inc({"purpose": purpose, "status": exc.error_kind})
            return {"error_kind": exc.error_kind, "message": str(exc)[:200], "images": []}
        except Exception as exc:
            logger.warning("image_generate: unexpected %s: %s", type(exc).__name__, str(exc)[:200])
            _metric_inc({"purpose": purpose, "status": "upstream"})
            return {"error_kind": "upstream", "message": str(exc)[:200], "images": []}

        astore = getattr(ctx, "artifact_store", None)
        if astore is None:
            _metric_inc({"purpose": purpose, "status": "upstream"})
            return {"error_kind": "upstream", "message": "artifact store unavailable", "images": []}

        slug = _slug(prompt)
        images_out: list[dict[str, Any]] = []
        revised: str | None = None
        for i, img in enumerate(generated, start=1):
            ext = _EXT_BY_MIME.get(img.mime, "png")
            try:
                art = await astore.put_bytes(
                    content=img.image_bytes,
                    filename=f"gen_{slug}_{i}.{ext}",
                    mime=img.mime,  # Inferred from magic bytes.
                    source="image_generate",
                    created_by=getattr(ctx, "user_id", None) or None,
                    session_id=getattr(ctx, "session_id", None) or None,
                    kind="deliverable" if is_deliverable else "tool_data",
                    inject="stub",
                    producer_kind="image_generate",
                    created_by_turn=getattr(ctx, "turn_id", None),
                )
            except Exception as exc:
                logger.warning("image_generate: persist failed err=%s", exc)
                _metric_inc({"purpose": purpose, "status": "persist_failed"})
                return {"error_kind": "upstream", "message": f"image persist failed: {str(exc)[:150]}", "images": []}
            images_out.append(
                {
                    "artifact_id": art.id,
                    "width": img.width,
                    "height": img.height,
                    "size_preset": size_preset,
                    "mime": img.mime,
                    "purpose": purpose,
                    "is_deliverable": is_deliverable,
                }
            )
            revised = revised or img.revised_prompt

        out: dict[str, Any] = {
            "images": images_out,
            "purpose": purpose,
            "is_deliverable": is_deliverable,
            "note": (
                "图已注册为可下载交付件; 用 add_image(artifact_id) 加进画布."
                if is_deliverable
                else "背景素材已生成; 用 canvas_create_design 创建可编辑设计稿."
            ),
        }
        if revised:
            out["revised_prompt"] = revised  # Already truncated to ≤200 characters by the client.
        _metric_inc({"purpose": purpose, "status": "done"})
        return out

    return _handler


def register_image_gen_tool(
    registry: ToolRegistry,
    media_registry: MediaModelRegistry,
    team_media_resolver: TeamMediaResolver | None = None,
) -> bool:
    """注册团队级 `image_generate`；无团队解析器时不得注册。"""
    if team_media_resolver is None:
        logger.info("image_generate: no team media resolver — tool not registered")
        return False
    registry.register(ToolSpec(schema=_schema(), handler=_make_handler(media_registry, team_media_resolver)))
    logger.info("image_generate registered (per_team=True)")
    return True
