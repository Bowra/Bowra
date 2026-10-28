"""error_catalog 单测：load / render / fallback / 真实 catalog."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from bowhead_core.error_catalog import (
    CodedError,
    ErrorCatalog,
    default_error_catalog_dir,
    load_error_catalog,
)

if TYPE_CHECKING:
    from pathlib import Path


def _write_catalog(tmp_path: Path, lang: str, payload: dict[str, str]) -> None:
    (tmp_path / f"{lang}.json").write_text(
        json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )


@pytest.fixture
def tmp_catalog(tmp_path: Path) -> ErrorCatalog:
    _write_catalog(
        tmp_path,
        "zh-CN",
        {
            "E_TEST_BASIC": "测试基础",
            "E_TEST_PARAM": "路径错误：{path}",
            "E_TEST_MULTI": "{code} 字符超 {max_len}",
        },
    )
    _write_catalog(
        tmp_path,
        "en-US",
        {
            "E_TEST_BASIC": "Basic test",
            "E_TEST_PARAM": "Invalid path: {path}",
            "E_TEST_MULTI": "{code} exceeds {max_len} chars",
        },
    )
    return load_error_catalog(tmp_path)


class TestLoad:
    def test_load_both_languages(self, tmp_catalog: ErrorCatalog) -> None:
        assert "zh-CN" in tmp_catalog.by_lang
        assert "en-US" in tmp_catalog.by_lang
        assert tmp_catalog.by_lang["zh-CN"]["E_TEST_BASIC"] == "测试基础"

    def test_missing_dir_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="no error catalogs"):
            load_error_catalog(tmp_path)  # Empty directory

    def test_missing_one_lang_does_not_raise(self, tmp_path: Path) -> None:
        # Only zh-CN is present.
        _write_catalog(tmp_path, "zh-CN", {"E": "X"})
        cat = load_error_catalog(tmp_path)
        assert cat.by_lang["zh-CN"]["E"] == "X"
        assert cat.by_lang["en-US"] == {}  # Fall back to an empty dict.

    def test_invalid_json_raises(self, tmp_path: Path) -> None:
        (tmp_path / "zh-CN.json").write_text("{not json", encoding="utf-8")
        with pytest.raises(ValueError, match="failed to parse"):
            load_error_catalog(tmp_path)

    def test_non_string_value_raises(self, tmp_path: Path) -> None:
        _write_catalog(tmp_path, "zh-CN", {"E": "ok"})
        (tmp_path / "en-US.json").write_text(
            json.dumps({"E": 123}),
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="value must be string"):
            load_error_catalog(tmp_path)


class TestRender:
    def test_basic_render_zh(self, tmp_catalog: ErrorCatalog) -> None:
        assert tmp_catalog.render("E_TEST_BASIC", "zh-CN") == "测试基础"

    def test_basic_render_en(self, tmp_catalog: ErrorCatalog) -> None:
        assert tmp_catalog.render("E_TEST_BASIC", "en-US") == "Basic test"

    def test_param_substitution(self, tmp_catalog: ErrorCatalog) -> None:
        out = tmp_catalog.render(
            "E_TEST_PARAM",
            "zh-CN",
            path="/etc/passwd",
        )
        assert out == "路径错误：/etc/passwd"

    def test_multiple_params(self, tmp_catalog: ErrorCatalog) -> None:
        out = tmp_catalog.render(
            "E_TEST_MULTI",
            "en-US",
            code="DESC",
            max_len=200,
        )
        assert out == "DESC exceeds 200 chars"

    def test_unknown_code_returns_code(self, tmp_catalog: ErrorCatalog) -> None:
        out = tmp_catalog.render("E_UNKNOWN", "zh-CN")
        assert out == "E_UNKNOWN"  # Unknown codes return the code itself.

    def test_unknown_language_falls_back_to_default(
        self,
        tmp_catalog: ErrorCatalog,
    ) -> None:
        out = tmp_catalog.render("E_TEST_BASIC", "ja-JP")
        assert out == "Basic test"  # Unsupported languages use English.

    def test_missing_param_returns_code_not_crash(
        self,
        tmp_catalog: ErrorCatalog,
    ) -> None:
        # The template requires {path}, but it was not supplied.
        out = tmp_catalog.render("E_TEST_PARAM", "zh-CN")
        assert out == "E_TEST_PARAM"  # Fallback after rendering fails.


class TestHas:
    def test_has_in_lang(self, tmp_catalog: ErrorCatalog) -> None:
        assert tmp_catalog.has("E_TEST_BASIC", "zh-CN")
        assert not tmp_catalog.has("E_UNKNOWN", "zh-CN")

    def test_has_any_lang(self, tmp_catalog: ErrorCatalog) -> None:
        assert tmp_catalog.has("E_TEST_BASIC")
        assert not tmp_catalog.has("E_UNKNOWN")


class TestCodedError:
    def test_basic(self) -> None:
        err = CodedError("E_X", path="/y")
        assert err.code == "E_X"
        assert err.params == {"path": "/y"}

    def test_no_params(self) -> None:
        err = CodedError("E_X")
        assert err.code == "E_X"
        assert err.params == {}

    def test_str_includes_code(self) -> None:
        err = CodedError("E_X", v=1)
        assert "E_X" in str(err)


class TestRealCatalog:
    """加载真实 config/i18n/errors/ 验证不出错."""

    def test_load_real(self) -> None:
        cat = load_error_catalog(default_error_catalog_dir())
        # Must contain at least 5 common codes, each in all three languages.
        for code in [
            "E_SKILL_MISSING_MANIFEST",
            "E_SKILL_INVALID_ZIP_PATH",
            "E_PERMISSION_DENIED",
            "E_DATARC_TIMEOUT",
            "E_INTERNAL",
        ]:
            assert cat.has(code, "zh-CN"), f"missing {code} zh-CN"
            assert cat.has(code, "en-US"), f"missing {code} en-US"
            assert cat.has(code, "de-DE"), f"missing {code} de-DE"
            assert cat.has(code, "fr-FR"), f"missing {code} fr-FR"

    def test_languages_and_documented_catalog_have_identical_code_sets(self) -> None:
        cat = load_error_catalog(default_error_catalog_dir())
        zh_codes = set(cat.by_lang["zh-CN"])
        en_codes = set(cat.by_lang["en-US"])
        de_codes = set(cat.by_lang["de-DE"])
        fr_codes = set(cat.by_lang["fr-FR"])
        assert zh_codes == en_codes == de_codes == fr_codes

        repo_root = Path(__file__).resolve().parents[3]
        catalog_doc = (repo_root / "docs" / "user-facing-error-catalog.md").read_text(encoding="utf-8")
        table = catalog_doc.split(
            "<!-- ERROR_CATALOG_TABLE_START -->",
            maxsplit=1,
        )[1].split("<!-- ERROR_CATALOG_TABLE_END -->", maxsplit=1)[0]
        documented_codes = set(re.findall(r"^\| `([^`]+)`\s+\|", table, re.MULTILINE))
        assert documented_codes == zh_codes

    def test_real_catalog_messages_are_non_empty(self) -> None:
        cat = load_error_catalog(default_error_catalog_dir())
        for language, messages in cat.by_lang.items():
            assert messages
            for code, message in messages.items():
                assert message.strip(), f"empty {language} message for {code}"

    def test_unsafe_path_param_is_not_rendered(self) -> None:
        cat = load_error_catalog(default_error_catalog_dir())
        secret_path = "../../../etc/passwd"
        zh_out = cat.render(
            "E_SKILL_INVALID_ZIP_PATH",
            "zh-CN",
            path=secret_path,
        )
        en_out = cat.render(
            "E_SKILL_INVALID_ZIP_PATH",
            "en-US",
            path=secret_path,
        )
        assert secret_path not in zh_out
        assert secret_path not in en_out
        # Chinese and English differ.
        assert zh_out != en_out


class TestFrenchCatalog:
    """法语错误使用原生文案并保留插值；缺译文时使用英文。"""

    def test_real_french_errors_render_native_copy_and_reference(self) -> None:
        cat = load_error_catalog(default_error_catalog_dir())
        assert cat.render("E_PERMISSION_DENIED", "fr-FR") == "Vous n’avez pas l’autorisation d’effectuer cette action."
        assert cat.render("E_SKILL_TOO_LARGE", "fr-FR", max_mb=32) == "La taille totale du Skill dépasse 32 Mo."
        assert cat.render("E_INTERNAL", "fr-FR", trace_id="trace-example") == (
            "Une erreur interne est survenue. Réessayez dans un instant (référence : trace-example)."
        )

    def test_real_french_catalog_has_no_chinese(self) -> None:
        cat = load_error_catalog(default_error_catalog_dir())
        leaked = [code for code, message in cat.by_lang["fr-FR"].items() if re.search(r"[\u3400-\u9fff]", message)]
        assert leaked == []

    def test_missing_french_code_falls_back_to_english_not_chinese(self, tmp_path: Path) -> None:
        _write_catalog(tmp_path, "zh-CN", {"E_ONLY_ZH_EN": "中文文案"})
        _write_catalog(tmp_path, "en-US", {"E_ONLY_ZH_EN": "English copy"})
        _write_catalog(tmp_path, "fr-FR", {"E_OTHER": "autre"})
        cat = load_error_catalog(tmp_path)
        assert cat.render("E_ONLY_ZH_EN", "fr-FR") == "English copy"
        assert cat.render("E_ONLY_ZH_EN", "zh-CN") == "中文文案"

    def test_supported_language_without_a_loaded_catalog_still_falls_back_to_english(self) -> None:
        """回退看的是「是否受支持」，不是「目录里有没有这门语言」：法语目录没装上也落英文。"""
        cat = ErrorCatalog(by_lang={"zh-CN": {"E_X": "中文"}, "en-US": {"E_X": "English"}})
        assert cat.render("E_X", "fr-FR") == "English"
        assert cat.render("E_X", "de-DE") == "English"
        assert cat.render("E_X", "ja-JP") == "English"
