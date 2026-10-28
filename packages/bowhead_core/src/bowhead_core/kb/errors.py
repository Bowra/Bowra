"""KB error catalog. 跟 bowhead_core/error_catalog.py 同模式."""

from __future__ import annotations


class KbError(Exception):
    """Base exception for KB module."""


class UnsupportedMimeError(KbError):
    """Loader dispatch 不识别 mime."""

    def __init__(self, mime_type: str) -> None:
        super().__init__(f"unsupported mime type: {mime_type}")
        self.mime_type = mime_type


class EmbedError(KbError):
    """dashscope embed call 失败 (含 retry exhausted)."""


class ScopeChangeMigrationError(KbError):
    """P0-O2 跨 collection 物理迁移失败 (add 阶段或 delete 阶段)."""

    def __init__(self, file_id: str, stage: str, error: str) -> None:
        super().__init__(f"scope change migration failed at {stage} for file_id={file_id}: {error}")
        self.file_id = file_id
        self.stage = stage
