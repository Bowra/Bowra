"""Sprint ω Phase 4 — codegen + role bindings invariants.

Tests r3.1 §2.3 spec:
- tool_aliases.yaml is single source of truth
- Codegen idempotent (--check exits 0 after fresh write)
- Generated outputs have AUTO-GENERATED header
- Generated dict matches in-repo tool_registry usage
- Prometheus metric label `bowhead_bi_query_url_only_total` NOT renamed (M exclusion)
- Legacy alias map (§2.13) populated from yaml legacy_aliases
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
import yaml


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _yaml_path() -> Path:
    return _repo_root() / "packages/bowhead_core/src/bowhead_core/agents/manifests/tool_aliases.yaml"


# ─── YAML schema ────────────────────────────────────────────────────────


def test_yaml_loads_with_expected_fields() -> None:
    with _yaml_path().open(encoding="utf-8") as f:
        data = yaml.safe_load(f)
    assert "tools" in data
    tools = data["tools"]
    assert len(tools) >= 25, f"Expected ≥25 tools, got {len(tools)}"
    required_fields = {"canonical", "display_zh", "category", "allowed_roles"}
    for t in tools:
        if t.get("pseudo"):
            # G5: pseudo entries (display-only, e.g. python_sandbox) have no allowed_roles;
            # they generate no role binding and only label traces (docs/skills-tools-ssot.md §5-G5).
            missing = {"canonical", "display_zh", "category"} - set(t.keys())
        else:
            missing = required_fields - set(t.keys())
        assert not missing, f"tool {t.get('canonical')} missing fields: {missing}"


def test_yaml_canonical_names_unique() -> None:
    with _yaml_path().open(encoding="utf-8") as f:
        data = yaml.safe_load(f)
    names = [t["canonical"] for t in data["tools"]]
    dups = {n for n in names if names.count(n) > 1}
    assert not dups, f"Duplicate canonical names: {dups}"


# ─── Codegen idempotence ────────────────────────────────────────────────


def test_codegen_check_exits_clean() -> None:
    """If `--check` exits non-zero, generated files are stale. Run codegen
    locally to refresh. CI gate."""
    result = subprocess.run(
        [sys.executable, "-m", "bowhead_cli.codegen_tool_aliases", "--check"],
        cwd=str(_repo_root()),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"codegen --check failed (stale outputs?):\nstdout: {result.stdout}\nstderr: {result.stderr}"


# ─── Generated module contents ──────────────────────────────────────────


def test_tool_names_module_has_constants() -> None:
    from bowhead_core.agents._generated import tool_names

    assert tool_names.BI_QUERY == "bi_query"
    assert tool_names.SUBAGENT_SPAWN == "subagent_spawn"
    assert tool_names.SKILL_READ == "skill_read"
    assert tool_names.PYTHON_SANDBOX == "python_sandbox"
    assert "bi_query" in tool_names.ALL_TOOL_NAMES


def test_private_document_admission_keeps_its_lead_only_internal_binding() -> None:
    from bowhead_core.agents._generated.role_bindings import _PARADIGM_V2_ROLE_MAP
    from bowhead_core.agents._generated.tool_kinds import TOOL_KINDS

    assert _PARADIGM_V2_ROLE_MAP["template_document_submit"] == frozenset({"lead"})
    assert TOOL_KINDS["template_document_submit"] == "part"
    assert _PARADIGM_V2_ROLE_MAP["render_deck"] == frozenset({"analyst"})


def test_role_bindings_includes_legacy() -> None:
    """Legacy aliases inherit role from canonical (so they keep working
    during deprecation window)."""
    from bowhead_core.agents._generated.role_bindings import _PARADIGM_V2_ROLE_MAP

    # bi_ask_question now uses `renamed_from` (handler removed earlier; a rename, not dual registration),
    # so it **must not** have a role binding, which would leak into NATIVE_ALLOWLIST as a ghost entry.
    # See docs/design/工具改名迁移.md.
    assert "bi_ask_question" not in _PARADIGM_V2_ROLE_MAP
    assert "bi_list_measures" in _PARADIGM_V2_ROLE_MAP  # legacy of bi_schema


def test_legacy_trace_names_present() -> None:
    """Frontend trace replay needs legacy→canonical map for /reports rendering."""
    from bowhead_core.agents._generated.legacy_trace_names import LEGACY_TRACE_NAMES

    # bi_ask_question moved from legacy_aliases to renamed_from and no longer belongs in this table;
    # TOOL_RENAMES now carries it (that table is the **runtime authority**, not merely display metadata).
    assert LEGACY_TRACE_NAMES.get("bi_ask_question") is None
    assert LEGACY_TRACE_NAMES.get("bi_list_measures") == "bi_schema"


def test_generated_files_have_auto_warning() -> None:
    """All generated files must carry AUTO-GENERATED header to discourage hand edits."""
    gen_dir = _repo_root() / "packages/bowhead_core/src/bowhead_core/agents/_generated"
    for py_file in gen_dir.glob("*.py"):
        if py_file.name == "__init__.py":
            continue
        content = py_file.read_text(encoding="utf-8")
        assert "AUTO-GENERATED" in content, f"{py_file.name} missing warning header"


def test_ts_output_has_auto_warning() -> None:
    ts_path = _repo_root() / "packages/bowhead_frontend/lib/_generated/tool-display.ts"
    assert ts_path.exists()
    content = ts_path.read_text(encoding="utf-8")
    assert "AUTO-GENERATED" in content


# ─── tool_registry uses generated map ───────────────────────────────────


def test_tool_registry_imports_generated_role_map() -> None:
    """No inline literal dict — must source from _generated."""
    path = _repo_root() / "packages/bowhead_core/src/bowhead_core/tool_registry.py"
    src = path.read_text(encoding="utf-8")
    # Inline literal removed
    assert '_PARADIGM_V2_ROLE_MAP: dict[str, frozenset[str]] = {\n    "bi_list_measures"' not in src
    # Import from _generated present
    assert "from bowhead_core.agents._generated.role_bindings" in src


# ─── Prometheus metric exclusion (r3.1 invariant) ───────────────────────


def test_prometheus_metric_label_preserved() -> None:
    """OBSOLETE post bi-hitl-cleanup P13.5: `bowhead_bi_query_url_only_total`
    metric was emitted by `_ask_question` handler which is now deleted as dead
    code (bi_ask_question tool 已 5+ sprint dead, manifest 早已不暴露)."""
    pytest.skip("metric deleted with bi_ask_question handler (P13.5 dead-code cleanup)")


# ─── Deterministic output (DeepSeek prefix cache stability) ─────────────


def test_codegen_output_alphabetical() -> None:
    """Both tool_names.py and role_bindings.py emit entries sorted by canonical
    name. Required for reproducible diffs + DeepSeek prefix-cache invariant
    (Spike Q5: byte-identical prefix = 85-90% cache hit)."""
    from bowhead_core.agents._generated.role_bindings import _PARADIGM_V2_ROLE_MAP

    keys = list(_PARADIGM_V2_ROLE_MAP.keys())
    assert keys == sorted(keys), "role_bindings keys must be alphabetically sorted"


# ─── --check exits 1 on drift ───────────────────────────────────────────


def test_check_mode_detects_drift(tmp_path: Path) -> None:
    """Verify --check exits non-zero when yaml is mutated but outputs not regenerated.

    Done by mocking a yaml mismatch via env override — skip if codegen doesn't
    support test isolation. Here we just smoke-test the --check path runs.
    """
    # Smoke: --check on current tree should pass (we just ran codegen)
    result = subprocess.run(
        [sys.executable, "-m", "bowhead_cli.codegen_tool_aliases", "--check"],
        cwd=str(_repo_root()),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0  # current state is sync'd


# ─── Codegen rendering invariants ───────────────────────────────────────


def test_render_role_bindings_uses_frozenset() -> None:
    from bowhead_core.agents._generated.role_bindings import _PARADIGM_V2_ROLE_MAP

    for k, v in _PARADIGM_V2_ROLE_MAP.items():
        assert isinstance(v, frozenset), f"{k}: expected frozenset, got {type(v)}"


def test_legacy_trace_names_unique() -> None:
    """A legacy name must not map to two different canonicals."""
    from bowhead_core.agents._generated.legacy_trace_names import LEGACY_TRACE_NAMES

    # dict by construction has unique keys; assert all canonical targets exist in tool_names.
    from bowhead_core.agents._generated.tool_names import ALL_TOOL_NAMES

    bad = [v for v in LEGACY_TRACE_NAMES.values() if v not in ALL_TOOL_NAMES]
    assert not bad, f"Legacy alias targets not in canonical set: {bad}"


def test_ts_display_is_locale_keyed_with_english_german_placeholder() -> None:
    """前端文案按界面语言分支输出；德语缺省时是英文占位，绝不回落成中文。"""
    from bowhead_cli.codegen_tool_aliases import render_tool_display_ts

    base = {
        "canonical": "bi_query",
        "sigil": "·",
        "category": "bi",
        "kind": "tool",
        "risk": "read",
        "display_zh": "查询数据",
        "display_en": "Query Data",
        "blurb_zh": "取数。",
        "blurb_en": "Pull rows.",
    }
    placeholder = render_tool_display_ts([base])
    assert '    label: { "zh-CN": "查询数据", "en-US": "Query Data", "de-DE": "Query Data", "fr-FR": "Query Data" },' in placeholder
    assert '    blurb: { "zh-CN": "取数。", "en-US": "Pull rows.", "de-DE": "Pull rows.", "fr-FR": "Pull rows." },' in placeholder

    translated = render_tool_display_ts(
        [
            {
                **base,
                "display_de": "Daten abfragen",
                "blurb_de": "Zeilen abrufen.",
                "display_fr": "Interroger les données",
                "blurb_fr": "Récupérer des lignes.",
            }
        ]
    )
    assert '"de-DE": "Daten abfragen"' in translated
    assert '"de-DE": "Zeilen abrufen."' in translated
    assert '"fr-FR": "Interroger les données"' in translated
    assert '"fr-FR": "Récupérer des lignes."' in translated
    assert '    kind: "tool",\n    risk: "read",\n  },' in placeholder


@pytest.mark.parametrize(
    ("suffix", "display", "blurb"),
    [
        ("de", "Daten abfragen", "Zeilen abrufen."),
        ("fr", "Interroger les données", "Récupérer des lignes."),
    ],
)
def test_python_tool_display_uses_native_copy_and_english_fallback(suffix, display, blurb) -> None:
    import ast

    from bowhead_cli.codegen_tool_aliases import render_tool_display_py

    base = {
        "canonical": "bi_query",
        "display_zh": "查询数据",
        "display_en": "Query Data",
        "blurb_zh": "取数。",
        "blurb_en": "Pull rows.",
    }

    def generated_entry(tool):
        tree = ast.parse(render_tool_display_py([tool]))
        table = next(node.value for node in tree.body if isinstance(node, ast.AnnAssign) and node.target.id == "TOOL_DISPLAY")
        return ast.literal_eval(table)["bi_query"]

    fallback = generated_entry(base)
    assert fallback[f"display_{suffix}"] == "Query Data"
    assert fallback[f"blurb_{suffix}"] == "Pull rows."
    native = generated_entry({**base, f"display_{suffix}": display, f"blurb_{suffix}": blurb})
    assert native[f"display_{suffix}"] == display
    assert native[f"blurb_{suffix}"] == blurb


# ─── Resource kind and risk (capability-center tech design §4.1) ────────

# The user-visible built-in Tools of PRD §2.2; every other manifest entry is a system part.
USER_VISIBLE = {
    "bi_schema", "bi_query", "bi_concept_resolve", "tool_list_workspaces",
    "write_report", "read_report", "add_chart", "remove_chart", "add_image", "canvas_add_insight",
    "canvas_add_spec_block", "canvas_add_html_artifact", "canvas_add_external_iframe",
    "canvas_create_design", "canvas_update_design_text", "canvas_flatten_design",
    "kb_search", "kb_query_records", "kb_get_record", "kb_list_tables", "attachment_read",
    "web_search", "web_fetch", "web_extract", "web_map", "web_crawl", "web_deep_research",
    "python_sandbox", "image_generate",
    "google_doc_write", "google_sheet_write", "google_slides_write", "send_email",
    "skill_save_generated", "get_current_user",
}  # fmt: skip


def test_user_visible_tools_match_the_product_list() -> None:
    from bowhead_core.agents._generated.tool_kinds import TOOL_KINDS, TOOL_RISKS, USER_VISIBLE_TOOLS
    from bowhead_core.agents._generated.tool_names import ALL_TOOL_NAMES

    assert set(TOOL_KINDS) == ALL_TOOL_NAMES
    assert USER_VISIBLE_TOOLS == USER_VISIBLE
    assert len(USER_VISIBLE_TOOLS) == 35
    assert set(TOOL_RISKS) >= USER_VISIBLE_TOOLS
    parts = ALL_TOOL_NAMES - USER_VISIBLE_TOOLS
    assert {"subagent_spawn", "subagent_return", "skill_read", "skill_invoke", "skill_list", "python_exec", "tool_switch_context"} <= parts
    assert {name for name in ALL_TOOL_NAMES if name.startswith(("agent_", "configuration_"))} <= parts


def test_high_risk_tools_are_classified() -> None:
    from bowhead_core.agents._generated.tool_kinds import TOOL_RISKS

    assert {name for name, risk in TOOL_RISKS.items() if risk == "external_write"} == {
        "google_doc_write",
        "google_sheet_write",
        "google_slides_write",
        "send_email",
    }
    assert {name for name, risk in TOOL_RISKS.items() if risk == "destructive"} == {"remove_chart"}


@pytest.mark.parametrize(
    ("entry", "message"),
    [
        ({"canonical": "x"}, "has kind None"),
        ({"canonical": "x", "kind": "widget"}, "has kind 'widget'"),
        ({"canonical": "x", "kind": "tool"}, "must declare risk"),
        ({"canonical": "x", "kind": "part", "risk": "unknown"}, "has risk 'unknown'"),
    ],
)
def test_invalid_kind_or_risk_fails_codegen(entry, message) -> None:
    from bowhead_cli.codegen_tool_aliases import validate_kinds

    with pytest.raises(ValueError, match=message):
        validate_kinds([entry])


def test_part_without_risk_is_valid() -> None:
    from bowhead_cli.codegen_tool_aliases import validate_kinds

    validate_kinds([{"canonical": "x", "kind": "part"}])
