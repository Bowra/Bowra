"""feedback-0713 F5 review — 导出图片注入的安全回归锁.

两条被 review 逮到的面（`_inject_image_paths`）:
- IDOR: `artifact_id` 是用户可 PATCH 的 section 字段（不在 _PATCH_PROTECTED_FIELDS），
  注入必须传 check_created_by=canvas.user_id，否则用户把别租户 artifact_id 塞进自己
  image section → 导出读到他人 artifact 字节。
- 任意路径读: `_local_path` 同样用户可 PATCH → 注入前必须无条件 strip，只有本函数
  成功 get 到 artifact 才重新设，杜绝塞任意宿主路径。
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from bowhead_core.canvas_export import _inject_image_paths


@dataclass
class _FakeArt:
    id: str
    filename: str = "fig.png"


class _FakeArtifactStore:
    """只放行 created_by == 请求者 的 artifact（镜像真 store 的 #54 owner 校验）。"""

    def __init__(self, owners: dict[str, str]):
        self._owners = owners  # artifact_id -> created_by
        self.get_calls: list[tuple[str, str | None]] = []

    async def get(self, artifact_id: str, *, check_created_by: str | None = None, **_kw):
        self.get_calls.append((artifact_id, check_created_by))
        owner = self._owners.get(artifact_id)
        if owner is None:
            return None
        if check_created_by is not None and owner != check_created_by:
            return None  # #54: owner mismatch uniformly returns None, hiding existence
        return _FakeArt(id=artifact_id)

    async def open_read(self, art: _FakeArt) -> bytes:
        return b"\x89PNG\r\n\x1a\n"  # Fake PNG header; sufficient to write to disk


async def test_cross_tenant_artifact_id_denied():
    """用户把别租户 artifact_id 塞进自己 image section → 注入拒绝，不落 _local_path。"""
    store = _FakeArtifactStore(owners={"art_victim": "other_user"})
    sections = [{"kind": "image", "artifact_id": "art_victim"}]
    await _inject_image_paths(sections, store, owner_user_id="me")
    assert "_local_path" not in sections[0], "跨租户 artifact 不得被注入"
    # And get actually included the owner check.
    assert store.get_calls == [("art_victim", "me")]


async def test_own_artifact_injected():
    """自己的 artifact 正常注入 _local_path。"""
    store = _FakeArtifactStore(owners={"art_mine": "me"})
    sections = [{"kind": "image", "artifact_id": "art_mine"}]
    await _inject_image_paths(sections, store, owner_user_id="me")
    assert sections[0].get("_local_path"), "自己的 artifact 应被注入"


async def test_user_supplied_local_path_stripped_when_artifact_unresolvable():
    """用户 PATCH 塞的 _local_path（无有效 artifact_id）必须被 strip，不残留。"""
    store = _FakeArtifactStore(owners={})
    sections = [{"kind": "image", "artifact_id": "", "_local_path": "/etc/passwd"}]
    await _inject_image_paths(sections, store, owner_user_id="me")
    assert "_local_path" not in sections[0], "用户塞的 _local_path 必须被清掉"


async def test_user_supplied_local_path_stripped_on_cross_tenant_artifact():
    """既塞 _local_path 又塞别租户 artifact_id → 两个攻击面都被挡（strip + IDOR）。"""
    store = _FakeArtifactStore(owners={"art_victim": "other_user"})
    sections = [{"kind": "image", "artifact_id": "art_victim", "_local_path": "/etc/passwd"}]
    await _inject_image_paths(sections, store, owner_user_id="me")
    assert "_local_path" not in sections[0]


async def test_strip_applies_recursively_in_body():
    """嵌套 section.body 里的 _local_path 同样被 strip。"""
    store = _FakeArtifactStore(owners={})
    sections = [
        {"kind": "section", "body": [{"kind": "image", "artifact_id": "", "_local_path": "/secret"}]},
    ]
    await _inject_image_paths(sections, store, owner_user_id="me")
    assert "_local_path" not in sections[0]["body"][0]


@pytest.mark.parametrize("owner", [None, "me"])
async def test_owner_threaded_into_get(owner):
    """owner_user_id（含 None=旧行为）如实传给 artifact_store.get 的 check_created_by。"""
    store = _FakeArtifactStore(owners={"art_x": "me"})
    sections = [{"kind": "image", "artifact_id": "art_x"}]
    await _inject_image_paths(sections, store, owner_user_id=owner)
    assert store.get_calls[0] == ("art_x", owner)
