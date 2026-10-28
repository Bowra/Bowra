"""ImageGenClient 真 API 集成测试（opt-in，CI 默认 skip）。

跑法（消耗真实计费，1-3 张图）：
    BOWHEAD_RUN_REAL_MEDIA_TESTS=1 \
    BOWHEAD_REAL_MEDIA_API_KEY=sk-xxx \
    uv run pytest packages/bowhead_core/tests/test_image_gen_client_real.py -q

用途：mock 锁死前对真协议形状的最后防线（review VERIFY P0 mock-drift 项）——
提交/轮询字段路径、X-DashScope-Async 行为、OSS 下载、魔术字节、时延 vs 60s 预算。

密钥来源：本用例**显式**从自己的环境变量拿 key，再拼成一份团队 config dict 走
``build_team_media_registry``。原先读 ``config/routing.dev.yaml`` 的部署级 registry
已随「禁止环境变量/部署级模型密钥入口」删除——产品代码只认团队配置，测试也不得
从那个入口偷渡（这里的 env 只喂测试自己，不经过任何产品解析路径）。
"""

from __future__ import annotations

import os

import pytest
from bowhead_core.media.config_loader import MediaModelRegistry, build_team_media_registry
from bowhead_core.media.image_gen import ImageGenClient

_RUN = os.environ.get("BOWHEAD_RUN_REAL_MEDIA_TESTS") == "1"

pytestmark = pytest.mark.skipif(
    not _RUN,
    reason="set BOWHEAD_RUN_REAL_MEDIA_TESTS=1 to run paid real-API tests",
)

_MODEL = os.environ.get("BOWHEAD_REAL_MEDIA_MODEL", "qwen-image-plus")
_BASE_URL = os.environ.get("BOWHEAD_REAL_MEDIA_BASE_URL", "https://dashscope.aliyuncs.com/api/v1")


def _registry() -> MediaModelRegistry:
    """按团队 config 的形状拼一份 registry（与 main.py 的 _team_media_resolver 同路径）。"""
    return build_team_media_registry(
        {
            "providers": {
                "real": {
                    "base_url": _BASE_URL,
                    "api_key": os.environ.get("BOWHEAD_REAL_MEDIA_API_KEY", ""),
                    "timeout_sec": 120,
                },
            },
            "media": {
                "models": {
                    _MODEL: {
                        "provider": "real",
                        "real_id": _MODEL,
                        "capability": "image_gen",
                        "protocol": "dashscope_async",
                    },
                },
                "active": {"image_gen": _MODEL},
            },
        }
    )


async def test_real_generate_one_chinese_poster():
    spec = _registry().get_active("image_gen")
    assert spec is not None and spec.api_key, "set BOWHEAD_REAL_MEDIA_API_KEY to a real key"
    client = ImageGenClient(spec)
    images = await client.generate(
        prompt="极简测试图：纯色背景，中央白色文字“测试”",
        size_preset="square",
        n=1,
    )
    assert len(images) == 1
    img = images[0]
    assert img.mime in ("image/png", "image/jpeg", "image/webp")
    assert len(img.image_bytes) > 10_000  # A real image is at least 10KB.
    assert (img.width, img.height) == (1328, 1328)


async def test_real_generate_n2_sequential():
    spec = _registry().get_active("image_gen")
    assert spec is not None and spec.api_key
    images = await ImageGenClient(spec).generate(
        prompt="极简测试图：纯色几何海报",
        size_preset="banner_16_9",
        n=2,
    )
    assert len(images) == 2
    assert all(i.mime.startswith("image/") for i in images)
