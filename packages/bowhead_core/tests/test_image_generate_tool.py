"""`image_generate` 工具层单测（multimodal-v1，client 行为已在 test_image_gen_client 覆盖）。

覆盖（tech-design §8）：
- 注册 gating：无团队解析器 → False 不注册（部署级注册被禁）；有解析器 → True
- 配置缺席（无 active 模型 / 无 key / 无 team_id）→ **调用期** auth，不回落任何进程级 registry
- happy path：put_bytes(kind=deliverable, mime=魔术字节值, 全 ctx 字段) + token-light 返回
- prompt 控制字符 strip / 空 prompt → bad_request
- ContentRejected → {"error_kind": "content_policy"}（fail-soft 不 raise）
- 团队改配置后下一次调用即生效（registry 每次调用现解析，不在注册期缓存）
- artifact_store 缺失 → upstream
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from bowhead_core.media import image_gen_tool as igt
from bowhead_core.media.config_loader import (
    MediaModelRegistry,
    MediaModelSpec,
)
from bowhead_core.media.config_loader import (
    empty_media_registry as igt_empty,
)
from bowhead_core.media.image_gen import GeneratedImage, ImageGenContentRejected
from bowhead_core.tool_registry import ToolRegistry

from bowhead_core import metrics as core_metrics

_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


def _spec(api_key: str = "sk-x") -> MediaModelSpec:
    return MediaModelSpec(
        name="qwen-image-plus",
        capability="image_gen",
        provider="aliyun_dashscope_native",
        real_id="qwen-image-plus",
        base_url="https://dashscope.aliyuncs.com/api/v1",
        api_key=api_key,
        timeout_sec=30,
        protocol="dashscope_async",
    )


def _registry(api_key: str = "sk-x") -> MediaModelRegistry:
    spec = _spec(api_key)
    return MediaModelRegistry(by_name={spec.name: spec}, active={"image_gen": spec.name})


class _FakeStore:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def put_bytes(self, **kw: Any) -> SimpleNamespace:
        self.calls.append(kw)
        return SimpleNamespace(id=f"art_{len(self.calls):012x}")


def _ctx(store: _FakeStore | None, *, team_id: str = "team_1") -> SimpleNamespace:
    return SimpleNamespace(
        artifact_store=store,
        user_id="usr_1",
        session_id="sess_1",
        turn_id="turn_1",
        # Model keys come only from **team** configuration (no deployment/environment sources); the handler resolves
        # the registry at call time by ctx.team_id; no team_id means no configuration.
        team_id=team_id,
    )


def _resolver(reg: MediaModelRegistry | None = None):
    """team_media_resolver 替身——镜像 main.py 的 _team_media_resolver 契约。"""

    async def _resolve(team_id: str) -> MediaModelRegistry | None:
        assert team_id, "resolver 不该在无 team_id 时被调用"
        return _registry() if reg is None else reg

    return _resolve


class _StubClient:
    """替身 ImageGenClient —— 记录入参，返固定图。"""

    last_kwargs: dict[str, Any] = {}

    def __init__(self, spec: MediaModelSpec) -> None:
        self.spec = spec

    async def generate(self, **kw: Any) -> list[GeneratedImage]:
        _StubClient.last_kwargs = kw
        n = kw.get("n", 1)
        return [GeneratedImage(image_bytes=_PNG, mime="image/png", width=1140, height=1472, revised_prompt="rp") for _ in range(n)]


@pytest.fixture(autouse=True)
def _reset_metrics():
    core_metrics.get_registry().reset()
    yield
    core_metrics.get_registry().reset()


# ─── Registration gating ──────────────────────────────────────────


def test_register_false_without_team_resolver():
    """image_gen 团队化后注册的真闸是「有没有团队解析器」——没有就不注册（部署级注册被禁）。

    注意此前这里断言的是「无 active 模型 → 不注册」，那条现在恒为真但**理由不对**
    （registry 参数已不参与注册判定），属于空转守卫，故改成守真闸；模型/密钥缺席
    改由下面两条调用期用例覆盖。
    """
    reg = ToolRegistry()
    assert igt.register_image_gen_tool(reg, MediaModelRegistry(by_name={}, active={})) is False
    assert igt.register_image_gen_tool(reg, _registry()) is False
    assert "image_generate" not in reg.list_names()


def test_register_true_with_team_resolver():
    reg = ToolRegistry()
    assert igt.register_image_gen_tool(reg, igt_empty(), team_media_resolver=_resolver()) is True
    assert "image_generate" in reg.list_names()


async def test_auth_error_when_team_has_no_active_model():
    """团队配置里没有 active image_gen → 调用期返 auth（不是注册期拒绝）。"""
    handler = igt._make_handler(igt_empty(), _resolver(MediaModelRegistry(by_name={}, active={})))
    out = await handler({"prompt": "x"}, _ctx(_FakeStore()))
    assert out["error_kind"] == "auth"
    assert out["images"] == []


async def test_auth_error_when_team_model_key_missing():
    handler = igt._make_handler(igt_empty(), _resolver(_registry(api_key="")))
    out = await handler({"prompt": "x"}, _ctx(_FakeStore()))
    assert out["error_kind"] == "auth"
    assert out["images"] == []


async def test_auth_error_when_ctx_has_no_team():
    """无 team_id = 无团队配置 → 同样 auth，不得回落到任何进程级 registry。"""
    handler = igt._make_handler(_registry(), _resolver())
    out = await handler({"prompt": "x"}, _ctx(_FakeStore(), team_id=""))
    assert out["error_kind"] == "auth"


# ─── Handler behavior ─────────────────────────────────────────────


async def test_happy_path_persists_deliverable_and_returns_token_light(monkeypatch):
    monkeypatch.setattr(igt, "ImageGenClient", _StubClient)
    handler = igt._make_handler(igt_empty(), _resolver())
    store = _FakeStore()

    out = await handler(
        {"prompt": "夏季奶茶海报\x00\x01，主标题：“夏日冰爽”", "size": "portrait_3_4", "n": 2},
        _ctx(store),
    )

    # Strip control characters before passing the prompt to the client.
    assert "\x00" not in _StubClient.last_kwargs["prompt"]
    assert _StubClient.last_kwargs["size_preset"] == "portrait_3_4"

    # Persist both images as deliverables with all ctx fields.
    assert len(store.calls) == 2
    first = store.calls[0]
    assert first["kind"] == "deliverable"
    assert first["mime"] == "image/png"
    assert first["source"] == "image_generate"
    assert first["producer_kind"] == "image_generate"
    assert first["created_by"] == "usr_1"
    assert first["session_id"] == "sess_1"
    assert first["created_by_turn"] == "turn_1"
    assert first["filename"].startswith("gen_") and first["filename"].endswith(".png")

    # token-light: no bytes / base64 / URL; include artifact_id + dimensions.
    assert len(out["images"]) == 2
    img0 = out["images"][0]
    assert set(img0) == {
        "artifact_id",
        "width",
        "height",
        "size_preset",
        "mime",
        "purpose",
        "is_deliverable",
    }
    assert img0["artifact_id"].startswith("art_")
    assert img0["purpose"] == "deliverable"
    assert img0["is_deliverable"] is True
    assert out["purpose"] == "deliverable"
    assert out["is_deliverable"] is True
    assert out["revised_prompt"] == "rp"
    blob = repr(out)
    assert "PNG" not in blob and "base64" not in blob and "https://" not in blob
    assert "bowhead_image_generate_purpose_total" in core_metrics.get_registry().render_prometheus()


async def test_design_background_persists_tool_data_and_not_deliverable(monkeypatch):
    monkeypatch.setenv("BOWHEAD_DESIGN_V2A", "1")
    monkeypatch.setattr(igt, "ImageGenClient", _StubClient)
    handler = igt._make_handler(igt_empty(), _resolver())
    store = _FakeStore()

    out = await handler(
        {
            "prompt": "清爽奶茶背景，无文字，预留标题区域",
            "purpose": "design_background",
        },
        _ctx(store),
    )

    first = store.calls[0]
    assert first["kind"] == "tool_data"
    assert first["inject"] == "stub"
    assert first["source"] == "image_generate"
    assert first["producer_kind"] == "image_generate"
    assert out["purpose"] == "design_background"
    assert out["is_deliverable"] is False
    assert out["images"][0]["purpose"] == "design_background"
    assert out["images"][0]["is_deliverable"] is False
    assert "canvas_create_design" in out["note"]


async def test_design_background_disabled_when_design_flag_off(monkeypatch):
    monkeypatch.setenv("BOWHEAD_DESIGN_V2A", "0")
    monkeypatch.setattr(igt, "ImageGenClient", _StubClient)
    handler = igt._make_handler(igt_empty(), _resolver())
    store = _FakeStore()

    out = await handler(
        {
            "prompt": "清爽奶茶背景，无文字，预留标题区域",
            "purpose": "design_background",
        },
        _ctx(store),
    )

    assert out["error_kind"] == "disabled"
    assert out["images"] == []
    assert store.calls == []
    rendered = core_metrics.get_registry().render_prometheus()
    assert 'purpose="design_background"' in rendered
    assert 'status="disabled"' in rendered


async def test_empty_prompt_bad_request(monkeypatch):
    monkeypatch.setattr(igt, "ImageGenClient", _StubClient)
    handler = igt._make_handler(igt_empty(), _resolver())
    out = await handler({"prompt": "  \x00 "}, _ctx(_FakeStore()))
    assert out["error_kind"] == "bad_request"
    assert out["images"] == []


async def test_content_policy_fail_soft(monkeypatch):

    class _Rejecting(_StubClient):
        async def generate(self, **kw: Any):
            raise ImageGenContentRejected("DataInspectionFailed: nope")

    monkeypatch.setattr(igt, "ImageGenClient", _Rejecting)
    handler = igt._make_handler(igt_empty(), _resolver())
    out = await handler({"prompt": "边缘内容"}, _ctx(_FakeStore()))
    assert out["error_kind"] == "content_policy"
    assert out["images"] == []
    assert "DataInspectionFailed" in out["message"]


async def test_key_lost_between_calls_returns_auth(monkeypatch):
    """团队把 active 换成无 key 的模型后，**下一次调用**就返 auth（不 raise）。

    守的是 `_make_handler` 的「每次调用现解析团队 registry」——注册期缓存住 registry
    的话，团队改配置/撤密钥要重启才生效，且撤销不了已发出的密钥。
    """
    monkeypatch.setattr(igt, "ImageGenClient", _StubClient)
    keyless = MediaModelSpec(
        name="doubao-seedream",
        capability="image_gen",
        provider="volces_ark_image",
        real_id="doubao-seedream-4-0-250828",
        base_url="https://ark.example.com/api/v3",
        api_key="",
        timeout_sec=30,
        protocol="ark_sync",
    )
    team_reg = [_registry()]

    async def _resolve(team_id: str) -> MediaModelRegistry | None:
        return team_reg[0]

    handler = igt._make_handler(igt_empty(), _resolve)
    ok = await handler({"prompt": "x"}, _ctx(_FakeStore()))
    assert ok.get("error_kind") is None

    team_reg[0] = MediaModelRegistry(by_name={keyless.name: keyless}, active={"image_gen": keyless.name})
    out = await handler({"prompt": "x"}, _ctx(_FakeStore()))
    assert out["error_kind"] == "auth"


async def test_missing_artifact_store_upstream(monkeypatch):
    monkeypatch.setattr(igt, "ImageGenClient", _StubClient)
    handler = igt._make_handler(igt_empty(), _resolver())
    out = await handler({"prompt": "x"}, _ctx(None))
    assert out["error_kind"] == "upstream"


async def test_prompt_truncated_at_cap(monkeypatch):
    monkeypatch.setattr(igt, "ImageGenClient", _StubClient)
    handler = igt._make_handler(igt_empty(), _resolver())
    await handler({"prompt": "长" * 5000}, _ctx(_FakeStore()))
    assert len(_StubClient.last_kwargs["prompt"]) == igt._PROMPT_MAX_CHARS
