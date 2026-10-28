"""ImageGenClient —— 文生图客户端，单类 + protocol 开关（multimodal-v1）。

协议（spike 实测形状，docs/sprints/multimodal-v1-2026-06-05/spike-image-gen.md）：

- ``dashscope_async``（qwen-image-plus @ DashScope 原生）：
    POST {base}/services/aigc/text2image/image-synthesis
      headers: Authorization Bearer + X-DashScope-Async: enable
      body: {"model", "input": {"prompt"[, "negative_prompt"]},
             "parameters": {"size": "1140*1472", "n": 1}}
    → output.task_id → 轮询 GET {base}/tasks/{task_id}（2.5s 固定间隔，
      预算 60s/张；实测 4~10s）→ SUCCEEDED: output.results[].url（即刻下载）
      / FAILED: output.code+message。
- ``ark_sync``（doubao-seedream @ Volces ARK /api/v3）：
    POST {base}/images/generations
      body: {"model", "prompt", "size": "1536x2048",
             "response_format": "b64_json", "n": 1}
    同步 200 → data[].b64_json（或 url 回退下载）。

硬化（review SEC 吸收）：结果 URL 必须 https；下载 timeout 10s；
mime 永远由魔术字节推导（PNG/JPEG/WebP 白名单），不信 provider 响应头。
v1 信任 app-global provider（不开 SSRF guard）；team BYO 接入时 spec.ssrf_guard
必须 True 并改用 build_guarded_async_client。

错误分类：
- ImageGenRetryableError —— 5xx / 429 / 网络 / 超时（tenacity 4 次 exp 1-8s）
- ImageGenContentRejected —— 内容政策（DataInspectionFailed 类），不重试，
  caller 返结构化 error_kind 让 LLM 改 prompt
- ImageGenAuthError —— 401/403/Arrearage/InvalidApiKey/ModelNotOpen
- ImageGenTimeout —— 轮询预算耗尽
"""

from __future__ import annotations

import asyncio
import base64
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import httpx2
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

if TYPE_CHECKING:
    from bowhead_core.media.config_loader import MediaModelSpec

logger = logging.getLogger(__name__)


# ─── Size presets: LLMs supply only a preset; the client maps it per provider ───
# dashscope uses `*` (qwen-image-plus tier; square verified ✓); ark uses `x`.
_SIZE_TABLE: dict[str, dict[str, str]] = {
    "square": {"dashscope_async": "1328*1328", "ark_sync": "2048x2048"},
    "portrait_3_4": {"dashscope_async": "1140*1472", "ark_sync": "1536x2048"},
    "portrait_9_16": {"dashscope_async": "928*1664", "ark_sync": "1152x2048"},
    "banner_16_9": {"dashscope_async": "1664*928", "ark_sync": "2048x1152"},
}
DEFAULT_SIZE_PRESET = "portrait_3_4"
SIZE_PRESETS: tuple[str, ...] = tuple(_SIZE_TABLE)

_POLL_INTERVAL_SEC = 2.5  # Fixed interval (bursty generation latency; exponential backoff would overshoot).
_POLL_BUDGET_SEC = 60.0  # Polling budget per request (observed completion in 4–10s).
_DOWNLOAD_TIMEOUT_SEC = 10.0  # Generation is complete; OSS downloads should take seconds.

# Content-policy FAILED codes (official DashScope vocabulary; defensive list, unknown codes treated as upstream errors).
_CONTENT_POLICY_CODES = {"datainspectionfailed", "ipinfringementsuspect"}
# Authentication/billing codes (appear in HTTP 400/401/403 envelopes).
_AUTH_CODES = {"arrearage", "invalidapikey", "invalid_api_key", "modelnotopen", "accessdenied", "unauthorized"}


class ImageGenError(Exception):
    """图像生成失败基类（caller 映射成结构化 error_kind）。"""

    error_kind = "upstream"


class ImageGenRetryableError(ImageGenError):
    error_kind = "upstream"


class ImageGenContentRejected(ImageGenError):
    error_kind = "content_policy"


class ImageGenAuthError(ImageGenError):
    error_kind = "auth"


class ImageGenTimeout(ImageGenError):
    error_kind = "timeout"


@dataclass(frozen=True)
class GeneratedImage:
    image_bytes: bytes
    mime: str  # Inferred from magic bytes: image/png | image/jpeg | image/webp.
    width: int  # From the preset table (no pixel parsing; PIL probing is unnecessary in v1).
    height: int
    revised_prompt: str | None  # dashscope actual_prompt (truncated to ≤200 characters), or None.


def sniff_image_mime(data: bytes) -> str | None:
    """魔术字节 → mime；不认识返 None（绝不信 provider 响应头/扩展名）。"""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _preset_dims(preset: str, protocol: str) -> tuple[int, int]:
    raw = _SIZE_TABLE[preset][protocol]
    sep = "*" if "*" in raw else "x"
    w, h = raw.split(sep)
    return int(w), int(h)


class ImageGenClient:
    """单 media model 的文生图客户端（protocol 开关见模块 docstring）。"""

    def __init__(self, spec: MediaModelSpec) -> None:
        if not spec.api_key:
            raise ValueError(f"ImageGenClient requires non-empty api_key (model={spec.name})")
        self._spec = spec

    async def generate(
        self,
        *,
        prompt: str,
        size_preset: str = DEFAULT_SIZE_PRESET,
        n: int = 1,
        negative_prompt: str | None = None,
    ) -> list[GeneratedImage]:
        """生成 n 张图（bytes 已下载落地，URL 不出本方法）。

        n>1 走顺序循环单图调用 —— spike 只实测过 n=1，qwen-image-plus 的原生
        多图支持未验证；顺序循环对 caller 语义透明、对两个 protocol 一致，
        且天然规避单 task 多 result 的解析分歧。（Phase 1 review 裁决）
        """
        if size_preset not in _SIZE_TABLE:
            size_preset = DEFAULT_SIZE_PRESET
        n = max(1, min(4, int(n)))
        out: list[GeneratedImage] = []
        for _ in range(n):
            out.append(
                await self._generate_one(
                    prompt=prompt,
                    size_preset=size_preset,
                    negative_prompt=negative_prompt,
                )
            )
        return out

    # ─── Single image (tenacity retries; only Retryable is retried) ───
    async def _generate_one(
        self,
        *,
        prompt: str,
        size_preset: str,
        negative_prompt: str | None,
    ) -> GeneratedImage:

        @retry(
            stop=stop_after_attempt(4),
            wait=wait_exponential(min=1, max=8),
            retry=retry_if_exception_type(ImageGenRetryableError),
            reraise=True,
        )
        async def _call() -> GeneratedImage:
            if self._spec.protocol == "ark_sync":
                return await self._gen_ark_sync(
                    prompt=prompt,
                    size_preset=size_preset,
                    negative_prompt=negative_prompt,
                )
            return await self._gen_dashscope_async(
                prompt=prompt,
                size_preset=size_preset,
                negative_prompt=negative_prompt,
            )

        return await _call()

    # ─── dashscope_async ──────────────────────────────────────────
    async def _gen_dashscope_async(
        self,
        *,
        prompt: str,
        size_preset: str,
        negative_prompt: str | None,
    ) -> GeneratedImage:
        size = _SIZE_TABLE[size_preset]["dashscope_async"]
        body: dict[str, Any] = {
            "model": self._spec.real_id,
            "input": {"prompt": prompt},
            "parameters": {"size": size, "n": 1},
        }
        if negative_prompt:
            body["input"]["negative_prompt"] = negative_prompt

        async with self._client() as client:
            submit = await self._post_json(
                client,
                f"{self._spec.base_url}/services/aigc/text2image/image-synthesis",
                body,
                extra_headers={"X-DashScope-Async": "enable"},
            )
            task_id = ((submit.get("output") or {}).get("task_id") or "").strip()
            if not task_id:
                raise ImageGenRetryableError(f"no task_id in submit response: {str(submit)[:200]}")

            deadline = asyncio.get_event_loop().time() + _POLL_BUDGET_SEC
            while True:
                if asyncio.get_event_loop().time() >= deadline:
                    raise ImageGenTimeout(f"poll budget {_POLL_BUDGET_SEC}s exhausted (task={task_id})")
                await asyncio.sleep(_POLL_INTERVAL_SEC)
                data = await self._get_json(client, f"{self._spec.base_url}/tasks/{task_id}")
                output = data.get("output") or {}
                status = str(output.get("task_status") or "").upper()
                if status in ("PENDING", "RUNNING", ""):
                    continue
                if status == "SUCCEEDED":
                    return await self._dashscope_result_to_image(
                        client,
                        output,
                        size_preset,
                    )
                # FAILED / CANCELED / UNKNOWN
                code = str(output.get("code") or "").strip()
                message = str(output.get("message") or "")[:300]
                self._raise_for_task_failure(code, message, task_id)

    async def _dashscope_result_to_image(
        self,
        client: httpx2.AsyncClient,
        output: dict[str, Any],
        size_preset: str,
    ) -> GeneratedImage:
        results = output.get("results") or []
        first = next((r for r in results if isinstance(r, dict) and r.get("url")), None)
        if first is None:
            # A failure embedded in one result (e.g. moderation rejection) also has this shape: results[0] contains code/message.
            inner = next((r for r in results if isinstance(r, dict) and r.get("code")), None)
            if inner is not None:
                self._raise_for_task_failure(
                    str(inner.get("code") or ""),
                    str(inner.get("message") or "")[:300],
                    "-",
                )
            raise ImageGenRetryableError(f"SUCCEEDED but no url in results: {str(output)[:200]}")
        image_bytes = await self._download(client, str(first["url"]))
        mime = sniff_image_mime(image_bytes)
        if mime is None:
            raise ImageGenError("downloaded bytes are not PNG/JPEG/WebP")
        w, h = _preset_dims(size_preset, "dashscope_async")
        revised = str(first.get("actual_prompt") or "").strip()[:200] or None
        return GeneratedImage(
            image_bytes=image_bytes,
            mime=mime,
            width=w,
            height=h,
            revised_prompt=revised,
        )

    # ─── ark_sync ─────────────────────────────────────────────────
    async def _gen_ark_sync(
        self,
        *,
        prompt: str,
        size_preset: str,
        negative_prompt: str | None,
    ) -> GeneratedImage:
        size = _SIZE_TABLE[size_preset]["ark_sync"]
        # ARK images API has no separate negative_prompt field; incorporate it into prompt.
        full_prompt = prompt if not negative_prompt else f"{prompt}\n避免出现：{negative_prompt}"
        body = {
            "model": self._spec.real_id,
            "prompt": full_prompt,
            "size": size,
            "response_format": "b64_json",
            "n": 1,
        }
        async with self._client() as client:
            data = await self._post_json(
                client,
                f"{self._spec.base_url}/images/generations",
                body,
            )
            items = data.get("data") or []
            first = items[0] if items and isinstance(items[0], dict) else {}
            if first.get("b64_json"):
                try:
                    image_bytes = base64.b64decode(first["b64_json"])
                except Exception as exc:
                    raise ImageGenError(f"b64 decode failed: {exc}") from exc
            elif first.get("url"):
                image_bytes = await self._download(client, str(first["url"]))
            else:
                raise ImageGenRetryableError(f"no image in response: {str(data)[:200]}")
        mime = sniff_image_mime(image_bytes)
        if mime is None:
            raise ImageGenError("downloaded bytes are not PNG/JPEG/WebP")
        w, h = _preset_dims(size_preset, "ark_sync")
        revised = str(first.get("revised_prompt") or "").strip()[:200] or None
        return GeneratedImage(
            image_bytes=image_bytes,
            mime=mime,
            width=w,
            height=h,
            revised_prompt=revised,
        )

    # ─── HTTP helpers ─────────────────────────────────────────────
    def _client(self) -> httpx2.AsyncClient:
        # v1: trusted app-global providers, without an SSRF guard; when team BYO is introduced,
        # spec.ssrf_guard=True must switch to build_guarded_async_client (see kb/vision_llm.py).
        return httpx2.AsyncClient(timeout=self._spec.timeout_sec)

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._spec.api_key}",
            "Content-Type": "application/json",
        }

    async def _post_json(
        self,
        client: httpx2.AsyncClient,
        url: str,
        body: dict[str, Any],
        *,
        extra_headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        headers = {**self._headers, **(extra_headers or {})}
        try:
            resp = await client.post(url, json=body, headers=headers)
        except httpx2.TimeoutException as exc:
            raise ImageGenRetryableError(f"timeout: {exc}") from exc
        except httpx2.HTTPError as exc:
            raise ImageGenRetryableError(f"network: {exc}") from exc
        return self._parse_response(resp)

    async def _get_json(self, client: httpx2.AsyncClient, url: str) -> dict[str, Any]:
        try:
            resp = await client.get(url, headers=self._headers)
        except httpx2.TimeoutException as exc:
            raise ImageGenRetryableError(f"timeout: {exc}") from exc
        except httpx2.HTTPError as exc:
            raise ImageGenRetryableError(f"network: {exc}") from exc
        return self._parse_response(resp)

    def _parse_response(self, resp: httpx2.Response) -> dict[str, Any]:
        """HTTP 状态 + 错误包络 → 分类异常；200 → json dict。"""
        status = resp.status_code
        if status == 200:
            try:
                data = resp.json()
            except Exception as exc:
                raise ImageGenRetryableError(f"bad json: {exc}") from exc
            return data if isinstance(data, dict) else {}

        # Two error-envelope shapes (observed in the spike):
        #   model-router/DashScope: {"code": "Arrearage", "message": ...}
        #   OpenAI style / ARK:    {"error": {"code": "ModelNotOpen", "message": ...}}
        code, message = "", ""
        try:
            payload = resp.json()
            err = payload.get("error") if isinstance(payload.get("error"), dict) else payload
            code = str(err.get("code") or "")
            message = str(err.get("message") or "")[:300]
        except Exception:
            message = resp.text[:300]

        code_l = code.lower()
        if status in (401, 403) or code_l in _AUTH_CODES:
            raise ImageGenAuthError(f"[{status}] {code}: {message}")
        if code_l in _CONTENT_POLICY_CODES:
            raise ImageGenContentRejected(f"[{status}] {code}: {message}")
        if status == 429 or status >= 500:
            raise ImageGenRetryableError(f"[{status}] {code}: {message}")
        # Other 4xx: the request itself is invalid; retrying is pointless.
        raise ImageGenError(f"[{status}] {code}: {message}")

    def _raise_for_task_failure(self, code: str, message: str, task_id: str) -> None:
        code_l = code.lower()
        if code_l in _CONTENT_POLICY_CODES:
            raise ImageGenContentRejected(f"task {task_id}: {code}: {message}")
        if code_l in _AUTH_CODES:
            raise ImageGenAuthError(f"task {task_id}: {code}: {message}")
        raise ImageGenError(f"task {task_id} failed: {code}: {message}")

    async def _download(self, client: httpx2.AsyncClient, url: str) -> bytes:
        """结果 URL → bytes。https-only；不带 auth header（OSS 签名在 URL 里）。"""
        if not url.startswith("https://"):
            raise ImageGenError(f"result url must be https, got {url[:60]!r}")
        try:
            resp = await client.get(url, timeout=_DOWNLOAD_TIMEOUT_SEC)
        except httpx2.TimeoutException as exc:
            raise ImageGenRetryableError(f"result download timeout: {exc}") from exc
        except httpx2.HTTPError as exc:
            raise ImageGenRetryableError(f"result download network: {exc}") from exc
        if resp.status_code != 200:
            raise ImageGenRetryableError(f"result download [{resp.status_code}]")
        return resp.content
