"""i18n v1 · error_catalog — 系统错误消息按 error_code + language 渲染.

设计文档 §3.10b B1 / B2.

为什么不直接 raise 中文 string:
- 多语 user 看不懂 (B1: 英文用户拿到 "缺 SKILL.md")
- 法规审计要求 error_code 可追溯 (pharma 客户)
- LLM 需要看到稳定 code 才能 reason about errors（不是看自然语言文本）

为什么不 LLM 翻译:
- error 文案有限（< 100 条），catalog 化便宜且确定性
- LLM 翻译同一 error 每次说法不一样，UI 测试不稳

层次:
  L2 catalog (本模块)              静态错误模板，per-language
  L3 LLM final_answer prose       动态生成，受 language directive 约束（不进 catalog）
  L5 entity values                DB 实体原文（不翻译）

格式:
- {param} 占位符（subset of ICU MessageFormat，纯 Python str.format）
- 老 ICU 复杂特性（plural / select）v2 再加
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Supported UI languages: error text follows the UI; missing German/French entries explicitly fall back to English.
SUPPORTED_LANGUAGES = ("zh-CN", "en-US", "de-DE", "fr-FR")
DEFAULT_LANGUAGE = "en-US"
# Missing or unsupported translations use English.
TRANSLATION_FALLBACK_LANGUAGE = "en-US"


class CodedError(Exception):
    """带 error_code + 模板参数的异常基类.

    raise CodedError("E_SKILL_MISSING_MANIFEST")
    raise CodedError("E_SKILL_INVALID_ZIP_PATH", path="../../../etc/passwd")

    异常处理器拿 self.code + self.params 走 ErrorCatalog.render() 出 user-facing
    message。raise 处不需要传中文/英文文案。
    """

    def __init__(self, code: str, **params: Any) -> None:
        self.code = code
        self.params: dict[str, Any] = params
        super().__init__(f"{code} {params}" if params else code)


@dataclass(frozen=True)
class ErrorCatalog:
    """加载好的多语 error catalog（{language: {code: template_str}}）."""

    by_lang: dict[str, dict[str, str]]

    def render(self, code: str, language: str, /, **params: Any) -> str:
        """渲染 error message for given language; 受支持的非中文语言找不到先落英文，再落 DEFAULT_LANGUAGE，再不行返 code.

        `code` / `language` 是 positional-only（用 `/` 分隔），避免与模板参数名 'code'/'language' 冲突.
        """
        template = (self.by_lang.get(language) or {}).get(code)
        # Missing translated copy falls back to English.
        if template is None and language in SUPPORTED_LANGUAGES and language != DEFAULT_LANGUAGE:
            template = (self.by_lang.get(TRANSLATION_FALLBACK_LANGUAGE) or {}).get(code)
        if template is None:
            # fallback to default language
            default_pool = self.by_lang.get(DEFAULT_LANGUAGE) or {}
            template = default_pool.get(code)
        if template is None:
            # Unknown code → return the code itself (show "E_XXX" instead of a blank in the frontend, aiding diagnosis).
            logger.warning("error_catalog.unknown_code code=%s lang=%s", code, language)
            return code
        try:
            return template.format(**params)
        except (KeyError, IndexError) as e:
            logger.warning(
                "error_catalog.render_failed code=%s lang=%s err=%s template=%r params=%r",
                code,
                language,
                e,
                template,
                params,
            )
            return code

    def has(self, code: str, language: str | None = None) -> bool:
        """是否有此 code 的翻译（可选指定语言）."""
        if language is not None:
            return code in (self.by_lang.get(language) or {})
        return any(code in pool for pool in self.by_lang.values())


def load_error_catalog(catalog_dir: str | Path) -> ErrorCatalog:
    """加载 catalog_dir 下 {lang}.json 文件.

    遗失文件不抛错（warning + 该 lang 走兜底）；解析错抛 ValueError.
    """
    p = Path(catalog_dir)
    by_lang: dict[str, dict[str, str]] = {}
    for lang in SUPPORTED_LANGUAGES:
        target = p / f"{lang}.json"
        if not target.exists():
            logger.warning("error_catalog.missing_lang_file lang=%s path=%s", lang, target)
            by_lang[lang] = {}
            continue
        try:
            data = json.loads(target.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            raise ValueError(f"failed to parse {target}: {e}") from e
        if not isinstance(data, dict):
            raise ValueError(f"{target} must be a JSON object, got {type(data).__name__}")
        # Validate that all values are strings.
        for k, v in data.items():
            if not isinstance(v, str):
                raise ValueError(f"{target}::{k} value must be string, got {type(v).__name__}")
        by_lang[lang] = data  # type: ignore[assignment]
    if not any(by_lang.values()):
        raise ValueError(f"no error catalogs loaded from {p}")
    return ErrorCatalog(by_lang=by_lang)


def default_error_catalog_dir() -> Path:
    """约定路径：config/i18n/errors/."""
    here = Path(__file__).resolve()
    repo_root = here.parents[4]  # src/bowhead_core/, src/, bowhead_core/, packages/,
    return repo_root / "config" / "i18n" / "errors"


__all__ = [
    "DEFAULT_LANGUAGE",
    "SUPPORTED_LANGUAGES",
    "CodedError",
    "ErrorCatalog",
    "default_error_catalog_dir",
    "load_error_catalog",
]
