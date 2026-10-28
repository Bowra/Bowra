"""ImageGenClient unit tests using HTTPX2 mocked through RESPX (multimodal-v1).

覆盖（tech-design §8，review VERIFY 强化项全收）：
- dashscope 提交断言 X-DashScope-Async header → PENDING→RUNNING→SUCCEEDED → 下载 bytes
- FAILED(DataInspectionFailed) → ImageGenContentRejected（不重试）
- HTTP 400 Arrearage → ImageGenAuthError；ark 404 ModelNotOpen → AuthError
- 轮询超预算 → ImageGenTimeout
- 429/5xx → tenacity 重试（耗尽 → Retryable）
- 结果下载非 https 拒绝；魔术字节校验（坏 bytes → ImageGenError）
- ark b64 解码 happy path
- preset 尺寸映射双 provider（断言请求体）
- n=3 → 顺序 3 次调用
"""

from __future__ import annotations

import base64
import json

import httpx2
import pytest
import respx
from bowhead_core.media import image_gen as ig
from bowhead_core.media.config_loader import MediaModelSpec
from bowhead_core.media.image_gen import (
    GeneratedImage,
    ImageGenAuthError,
    ImageGenClient,
    ImageGenContentRejected,
    ImageGenError,
    ImageGenRetryableError,
    ImageGenTimeout,
    sniff_image_mime,
)

_DS_BASE = "https://dashscope.aliyuncs.com/api/v1"
_ARK_BASE = "https://ark.cn-beijing.volces.com/api/v3"
_SUBMIT_URL = f"{_DS_BASE}/services/aigc/text2image/image-synthesis"
_OSS_URL = "https://oss.example.com/result.png"

# Minimal valid PNG/JPEG magic headers + padding.
_PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
_JPEG_BYTES = b"\xff\xd8\xff\xe0" + b"\x00" * 32


def _ds_spec(**kw) -> MediaModelSpec:
    base = {
        "name": "qwen-image-plus",
        "capability": "image_gen",
        "provider": "aliyun_dashscope_native",
        "real_id": "qwen-image-plus",
        "base_url": _DS_BASE,
        "api_key": "sk-test",
        "timeout_sec": 30,
        "protocol": "dashscope_async",
    }
    base.update(kw)
    return MediaModelSpec(**base)


def _ark_spec(**kw) -> MediaModelSpec:
    base = {
        "name": "doubao-seedream",
        "capability": "image_gen",
        "provider": "volces_ark_image",
        "real_id": "doubao-seedream-4-0-250828",
        "base_url": _ARK_BASE,
        "api_key": "sk-ark",
        "timeout_sec": 30,
        "protocol": "ark_sync",
    }
    base.update(kw)
    return MediaModelSpec(**base)


@pytest.fixture(autouse=True)
def _fast_poll(monkeypatch):
    """轮询间隔归零，测试不真睡。"""
    monkeypatch.setattr(ig, "_POLL_INTERVAL_SEC", 0.0)


def _submit_ok(task_id: str = "task-1") -> httpx2.Response:
    return httpx2.Response(
        200,
        json={
            "request_id": "r1",
            "output": {"task_id": task_id, "task_status": "PENDING"},
        },
    )


def _task_status(status: str, **output_extra) -> httpx2.Response:
    return httpx2.Response(
        200,
        json={
            "request_id": "r2",
            "output": {"task_id": "task-1", "task_status": status, **output_extra},
        },
    )


def _task_succeeded(url: str = _OSS_URL, actual_prompt: str | None = None) -> httpx2.Response:
    result: dict = {"orig_prompt": "p", "url": url}
    if actual_prompt is not None:
        result["actual_prompt"] = actual_prompt
    return _task_status("SUCCEEDED", results=[result])


# ─── dashscope_async ────────────────────────────────────────────────


@respx.mock
async def test_dashscope_happy_path_full_poll_sequence():
    """提交（断言 async header + body 形状）→ PENDING → RUNNING → SUCCEEDED → 下载。"""
    submit_route = respx.post(_SUBMIT_URL).mock(return_value=_submit_ok())
    respx.get(f"{_DS_BASE}/tasks/task-1").mock(
        side_effect=[
            _task_status("PENDING"),
            _task_status("RUNNING"),
            _task_succeeded(actual_prompt="扩写后的长 prompt" * 50),  # >200 characters → truncate.
        ]
    )
    respx.get(_OSS_URL).mock(return_value=httpx2.Response(200, content=_PNG_BYTES))

    images = await ImageGenClient(_ds_spec()).generate(
        prompt="一张海报",
        size_preset="portrait_3_4",
    )

    assert len(images) == 1
    img = images[0]
    assert isinstance(img, GeneratedImage)
    assert img.image_bytes == _PNG_BYTES
    assert img.mime == "image/png"
    assert (img.width, img.height) == (1140, 1472)
    assert img.revised_prompt is not None and len(img.revised_prompt) <= 200

    req = submit_route.calls[0].request
    assert req.headers["X-DashScope-Async"] == "enable"  # VERIFY strengthening item.
    assert req.headers["Authorization"] == "Bearer sk-test"
    body = json.loads(req.content)
    assert body["model"] == "qwen-image-plus"
    assert body["input"]["prompt"] == "一张海报"
    assert body["parameters"]["size"] == "1140*1472"  # dashscope uses `*`.
    assert body["parameters"]["n"] == 1


@respx.mock
async def test_dashscope_content_policy_failed_no_retry():
    submit_route = respx.post(_SUBMIT_URL).mock(return_value=_submit_ok())
    respx.get(f"{_DS_BASE}/tasks/task-1").mock(
        return_value=_task_status(
            "FAILED",
            code="DataInspectionFailed",
            message="inappropriate content",
        )
    )
    with pytest.raises(ImageGenContentRejected):
        await ImageGenClient(_ds_spec()).generate(prompt="违规内容")
    assert submit_route.call_count == 1  # ContentRejected is not retried.


@respx.mock
async def test_dashscope_arrearage_400_is_auth_error():
    """spike 实测的欠费包络：HTTP 400 + code=Arrearage → AuthError（不重试）。"""
    route = respx.post(_SUBMIT_URL).mock(
        return_value=httpx2.Response(
            400,
            json={
                "code": "Arrearage",
                "message": "Access denied, please make sure your account is in good standing.",
            },
        )
    )
    with pytest.raises(ImageGenAuthError):
        await ImageGenClient(_ds_spec()).generate(prompt="x")
    assert route.call_count == 1


@respx.mock
async def test_dashscope_poll_budget_exhausted(monkeypatch):
    monkeypatch.setattr(ig, "_POLL_BUDGET_SEC", 0.05)
    respx.post(_SUBMIT_URL).mock(return_value=_submit_ok())
    respx.get(f"{_DS_BASE}/tasks/task-1").mock(return_value=_task_status("PENDING"))
    with pytest.raises(ImageGenTimeout):
        await ImageGenClient(_ds_spec()).generate(prompt="x")


@respx.mock
async def test_dashscope_429_retries_then_succeeds():
    respx.post(_SUBMIT_URL).mock(
        side_effect=[
            httpx2.Response(429, json={"code": "Throttling", "message": "slow down"}),
            _submit_ok(),
        ]
    )
    respx.get(f"{_DS_BASE}/tasks/task-1").mock(return_value=_task_succeeded())
    respx.get(_OSS_URL).mock(return_value=httpx2.Response(200, content=_PNG_BYTES))
    images = await ImageGenClient(_ds_spec()).generate(prompt="x")
    assert images[0].mime == "image/png"


@respx.mock
async def test_dashscope_5xx_exhausts_retries():
    route = respx.post(_SUBMIT_URL).mock(
        return_value=httpx2.Response(503, json={"message": "busy"}),
    )
    with pytest.raises(ImageGenRetryableError):
        await ImageGenClient(_ds_spec()).generate(prompt="x")
    assert route.call_count == 4  # tenacity 4 attempts


@respx.mock
async def test_result_url_must_be_https():
    respx.post(_SUBMIT_URL).mock(return_value=_submit_ok())
    respx.get(f"{_DS_BASE}/tasks/task-1").mock(
        return_value=_task_succeeded(url="http://insecure.example.com/x.png"),
    )
    with pytest.raises(ImageGenError) as ei:
        await ImageGenClient(_ds_spec()).generate(prompt="x")
    assert "https" in str(ei.value)


@respx.mock
async def test_downloaded_bytes_must_be_raster():
    """魔术字节校验：HTML 伪装 PNG → ImageGenError（SEC-P0-2）。"""
    respx.post(_SUBMIT_URL).mock(return_value=_submit_ok())
    respx.get(f"{_DS_BASE}/tasks/task-1").mock(return_value=_task_succeeded())
    respx.get(_OSS_URL).mock(
        return_value=httpx2.Response(
            200,
            content=b"<html><script>alert(1)</script></html>",
        )
    )
    with pytest.raises(ImageGenError) as ei:
        await ImageGenClient(_ds_spec()).generate(prompt="x")
    assert "PNG/JPEG/WebP" in str(ei.value)


@respx.mock
async def test_n_three_sequential_calls():
    submit_route = respx.post(_SUBMIT_URL).mock(return_value=_submit_ok())
    respx.get(f"{_DS_BASE}/tasks/task-1").mock(return_value=_task_succeeded())
    respx.get(_OSS_URL).mock(return_value=httpx2.Response(200, content=_PNG_BYTES))
    images = await ImageGenClient(_ds_spec()).generate(prompt="x", n=3)
    assert len(images) == 3
    assert submit_route.call_count == 3  # Sequential single-image loop.


# ─── ark_sync ───────────────────────────────────────────────────────


@respx.mock
async def test_ark_b64_happy_path():
    route = respx.post(f"{_ARK_BASE}/images/generations").mock(
        return_value=httpx2.Response(
            200,
            json={
                "data": [
                    {
                        "b64_json": base64.b64encode(_JPEG_BYTES).decode("ascii"),
                        "revised_prompt": "rp",
                    }
                ],
            },
        ),
    )
    images = await ImageGenClient(_ark_spec()).generate(
        prompt="海报",
        size_preset="banner_16_9",
    )
    img = images[0]
    assert img.image_bytes == _JPEG_BYTES
    assert img.mime == "image/jpeg"
    assert (img.width, img.height) == (2048, 1152)
    assert img.revised_prompt == "rp"

    body = json.loads(route.calls[0].request.content)
    assert body["size"] == "2048x1152"  # ark uses `x`.
    assert body["response_format"] == "b64_json"
    assert body["model"] == "doubao-seedream-4-0-250828"


@respx.mock
async def test_ark_model_not_open_is_auth_error():
    """spike 实测：404 + error.code=ModelNotOpen → AuthError。"""
    respx.post(f"{_ARK_BASE}/images/generations").mock(
        return_value=httpx2.Response(
            404,
            json={
                "error": {"code": "ModelNotOpen", "message": "account has not activated the model", "type": "Not Found"},
            },
        ),
    )
    with pytest.raises(ImageGenAuthError):
        await ImageGenClient(_ark_spec()).generate(prompt="x")


@respx.mock
async def test_ark_url_fallback_download():
    respx.post(f"{_ARK_BASE}/images/generations").mock(
        return_value=httpx2.Response(200, json={"data": [{"url": _OSS_URL}]}),
    )
    respx.get(_OSS_URL).mock(return_value=httpx2.Response(200, content=_PNG_BYTES))
    images = await ImageGenClient(_ark_spec()).generate(prompt="x")
    assert images[0].mime == "image/png"


# ─── Miscellaneous ────────────────────────────────────────────────


def test_empty_api_key_rejected_at_construction():
    with pytest.raises(ValueError):
        ImageGenClient(_ds_spec(api_key=""))


def test_sniff_image_mime():
    assert sniff_image_mime(_PNG_BYTES) == "image/png"
    assert sniff_image_mime(_JPEG_BYTES) == "image/jpeg"
    assert sniff_image_mime(b"RIFF\x00\x00\x00\x00WEBPVP8 ") == "image/webp"
    assert sniff_image_mime(b"<svg xmlns=...") is None
    assert sniff_image_mime(b"") is None


@respx.mock
async def test_unknown_preset_falls_back_to_default():
    submit_route = respx.post(_SUBMIT_URL).mock(return_value=_submit_ok())
    respx.get(f"{_DS_BASE}/tasks/task-1").mock(return_value=_task_succeeded())
    respx.get(_OSS_URL).mock(return_value=httpx2.Response(200, content=_PNG_BYTES))
    images = await ImageGenClient(_ds_spec()).generate(prompt="x", size_preset="weird")
    assert (images[0].width, images[0].height) == (1140, 1472)  # portrait_3_4 default.
    body = json.loads(submit_route.calls[0].request.content)
    assert body["parameters"]["size"] == "1140*1472"
