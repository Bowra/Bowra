"""Media generation capability (multimodal-v1, 2026-06-05).

Layer 0 媒体模型注册表 + 客户端。当前仅 image_gen（文生图）；
video / tts 留 capability 枚举扩展位，明确不在 v1 范围。

- 配置: 团队模型设置的 ``media:`` 块（models + active）。
- 客户端: :class:`ImageGenClient` —— 单类 + protocol 开关
  （dashscope_async 提交轮询 / ark_sync 同步），bytes 即刻下载落地。
- 工具层: image_gen_tool.register_image_gen_tool（仅团队配置）。

Spike 实测协议形状: docs/sprints/multimodal-v1-2026-06-05/spike-image-gen.md
"""

from bowhead_core.media.config_loader import (
    MediaModelConfigError,
    MediaModelRegistry,
    MediaModelSpec,
)
from bowhead_core.media.image_gen import (
    GeneratedImage,
    ImageGenAuthError,
    ImageGenClient,
    ImageGenContentRejected,
    ImageGenError,
    ImageGenRetryableError,
    ImageGenTimeout,
)

__all__ = [
    "GeneratedImage",
    "ImageGenAuthError",
    "ImageGenClient",
    "ImageGenContentRejected",
    "ImageGenError",
    "ImageGenRetryableError",
    "ImageGenTimeout",
    "MediaModelConfigError",
    "MediaModelRegistry",
    "MediaModelSpec",
]
