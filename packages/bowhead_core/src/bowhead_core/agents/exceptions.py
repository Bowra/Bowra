"""Agent manifest 异常类."""

from __future__ import annotations


class ManifestValidationError(Exception):
    """Manifest schema 校验失败 (字段缺/非法值/JSONSchema 不合法等)."""


class ManifestSecurityError(Exception):
    """Manifest 安全检查失败 (path traversal / symlink / 强约束 user 不可设字段等).

    和 ValidationError 分开是因为 security-class 错误必须 fail-loud +
    audit log 记 actor, 不只是返 400 给 user.
    """
