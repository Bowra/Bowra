"""Inspect synthetic Skill bundles with unsupported or missing dependencies.

Fixtures combine foreign tool declarations, absent paths, and unavailable imports.
They model dependency structures without including private bundle contents.
"""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import pytest
from bowhead_core.skill_import.hook import next_import_status, serialize_report
from bowhead_core.skill_import.inventory import (
    MAX_FINDINGS,
    BundleInventory,
    Finding,
    ResolutionSources,
    inventory_from_bytes,
    inventory_from_dir,
)
from bowhead_core.skill_import.sources import sandbox_node_packages, sandbox_python_modules

from bowhead_skill.catalog import builtin_catalog_dir

_TOOLS = frozenset({"bi_query", "bi_schema", "python_sandbox", "skill_read", "skill_invoke", "write_report", "add_chart"})
_SKILLS = frozenset({"analyst-framework", "pptx", "canvas-pptx"})


def _sources(**kw) -> ResolutionSources:
    base = {
        "tool_names": _TOOLS,
        "skill_names": _SKILLS,
        "script_mode": "sandbox",
        "python_modules": sandbox_python_modules(),
        "node_packages": sandbox_node_packages(),
    }
    base.update(kw)
    return ResolutionSources(**base)


_FOREIGN_SKILL_MD = """---
name: acme-qbr-html-ppt-report
description: Build and maintain ACME QBR reports as offline paginated HTML plus editable PPTX from JSON.
allowed-tools: Read, Bash(python3), data_fetch_skill
---

# QBR HTML + editable PPTX

This is a project-local skill for the workspace that contains `05_full_report`, `07_html_report` and `08_output`.

1. Read the page brief in [05_full_report/QBR_page_brief.md](05_full_report/QBR_page_brief.md) and
   [references/multi-slide-contract.md](references/multi-slide-contract.md).
2. For a new period use `data_fetch_skill`'s meta -> cut -> query workflow, then `skill_read(name="data-fetch")`.
3. Rebuild with PowerShell:

```powershell
python .\\07_html_report\\build_report.py
python .\\scripts\\qa_html_report.py --report .\\08_output\\report.html
```

4. Use `apply_patch` for source edits. Embed the local PptxGenJS bundle from `07_html_report/pptx_exporter.js`.
5. QA opens the HTML through `file://` with Playwright (`C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe`).
"""

_QA_SCRIPT = """from __future__ import annotations
import json
from pathlib import Path
from playwright.sync_api import sync_playwright

def main():
    with sync_playwright() as p:
        pass
"""

_EXPORTER_JS = """const pptxgen = require('pptxgenjs');
const lodash = require('lodash');
module.exports = function build() { return new pptxgen(); };
"""


def _write_foreign_bundle(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "SKILL.md").write_text(_FOREIGN_SKILL_MD, encoding="utf-8")
    (root / "references").mkdir()
    (root / "references" / "multi-slide-contract.md").write_text("# contract\n", encoding="utf-8")
    (root / "scripts").mkdir()
    (root / "scripts" / "qa_html_report.py").write_text(_QA_SCRIPT, encoding="utf-8")
    (root / "scripts" / "pptx_exporter.js").write_text(_EXPORTER_JS, encoding="utf-8")
    return root


def _by_code(inv: BundleInventory) -> dict[str, list[Finding]]:
    out: dict[str, list[Finding]] = {}
    for f in inv.findings:
        out.setdefault(f.code, []).append(f)
    return out


@pytest.mark.unit
def test_foreign_runtime_skill_grades_red_with_provable_findings(tmp_path: Path) -> None:
    inv = inventory_from_dir(_write_foreign_bundle(tmp_path / "acme"), _sources())
    by = _by_code(inv)
    assert inv.grade == "red"
    # Red: only from resolvable structural facts.
    unresolved = {f.subject for f in by["tool_ref_unresolved"]}
    assert "data_fetch_skill" in unresolved  # frontmatter allowed-tools
    assert "data-fetch" in unresolved  # Literal skill_read(name="…").
    assert {f.subject for f in by["file_ref_dangling"]} == {"05_full_report/QBR_page_brief.md"}  # Markdown link outside the bundle.
    assert {f.subject for f in by["script_import_unresolved"]} == {"playwright"}
    assert all(f.severity == "red" for c in ("tool_ref_unresolved", "file_ref_dangling", "script_import_unresolved") for f in by[c])
    # Yellow: foreign shapes + foreign identifiers + capability overlap + missing trigger examples.
    foreign = by["foreign_runtime_marker"]
    assert any(f.subject in ("Read", "Bash(python3)") for f in foreign)  # Foreign allowed-tools shapes are yellow first, never red.
    body_hit = next(f for f in foreign if f.resolved_against == "foreign_runtime_tokens")
    assert {"apply_patch", "powershell", "file://", "playwright", "windows path"} <= set(body_hit.subject.split(", "))
    assert {f.subject for f in by["capability_overlap"]} == {"cap_presentation"}
    assert by["frontmatter_gap"][0].subject == "trigger_examples"
    assert "file_ref_unverified" not in by or all(f.severity == "yellow" for f in by["file_ref_unverified"])
    # Do not report references to existing files.
    assert not any(f.subject == "references/multi-slide-contract.md" for f in inv.findings)
    # The report is serializable and contains no body text.
    report = json.loads(serialize_report(inv))
    assert report["grade"] == "red" and report["red_count"] == inv.red_count
    assert all("body" not in c["meta"] for c in report["components"])


@pytest.mark.unit
def test_resolution_is_against_the_registry_not_a_wordlist(tmp_path: Path) -> None:
    """突变：把工具名放进解析源，对应红消失；拿掉，回来."""
    root = _write_foreign_bundle(tmp_path / "acme")
    with_tool = inventory_from_dir(root, _sources(tool_names=_TOOLS | {"data_fetch_skill"}, skill_names=_SKILLS | {"data-fetch"}))
    assert not [f for f in with_tool.findings if f.code == "tool_ref_unresolved"]
    with_pkg = inventory_from_dir(root, _sources(python_modules=sandbox_python_modules() | {"playwright"}))
    assert not [f for f in with_pkg.findings if f.code == "script_import_unresolved"]
    unknown_pkgs = inventory_from_dir(root, _sources(python_modules=None))
    assert not [f for f in unknown_pkgs.findings if f.code == "script_import_unresolved"]  # Unknown package set ⇒ no judgment.


@pytest.mark.unit
def test_script_executability_is_a_deployment_fact_and_only_yellow(tmp_path: Path) -> None:
    root = _write_foreign_bundle(tmp_path / "acme")
    off = inventory_from_dir(root, _sources(script_mode="disabled"))
    hits = [f for f in off.findings if f.code == "script_not_executable"]
    assert hits and hits[0].severity == "yellow"
    on = inventory_from_dir(root, _sources(script_mode="sandbox"))
    assert not [f for f in on.findings if f.code == "script_not_executable"]


@pytest.mark.unit
def test_zip_form_strips_single_top_dir_and_recognizes_components(tmp_path: Path) -> None:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("proj/onc-skill/SKILL.md", "---\nname: onc-skill\ndescription: d\n---\nbody\n")
        zf.writestr("proj/agents/analyst.md", "---\nname: analyst\ndescription: a\ntools: [bi_query]\n---\nprompt\n")
        zf.writestr("proj/.claude-plugin/plugin.json", json.dumps({"name": "acme", "version": "1"}))
        zf.writestr("proj/api/openapi.json", json.dumps({"openapi": "3.0.0", "info": {"title": "ACME"}, "paths": {"/x": {"get": {}}}}))
        zf.writestr("proj/.mcp.json", json.dumps({"mcpServers": {"local": {"command": "npx"}, "remote": {"url": "https://mcp.example"}}}))
        zf.writestr("proj/data/sales.csv", "a,b\n1,2\n")
        zf.writestr("proj/08_output/deck.pptx", b"PK\x03\x04junk")
        zf.writestr("proj/node_modules/x/index.js", "x")
        zf.writestr("proj/docs/brief.md", "# brief")
    inv = inventory_from_bytes(buf.getvalue(), _sources())
    kinds = {c.kind: c for c in inv.components}
    assert kinds["skill_bundle"].path == "onc-skill/SKILL.md"
    assert kinds["agent_md"].meta["tools"] == ["bi_query"]
    assert kinds["plugin_manifest"].meta["name"] == "acme"
    assert kinds["openapi_spec"].meta == {"title": "ACME", "operation_count": 1}
    assert kinds["mcp_config"].meta == {"http_servers": ["remote"], "stdio_servers": ["local"]}
    assert kinds["data_file"].path == "data/sales.csv"
    assert kinds["office_deliverable"].meta["format"] == "pptx"
    assert kinds["document"].path == "docs/brief.md"
    assert any(c.kind == "noise" and c.path.startswith("node_modules/") for c in inv.components)
    assert inv.capability_shaped()
    by = _by_code(inv)
    assert [f.subject for f in by["transport_unsupported"]] == ["local"]
    assert any(f.code == "capability_overlap" and f.path == "08_output/deck.pptx" for f in inv.findings)


@pytest.mark.unit
def test_zip_member_names_in_gbk_are_decoded(tmp_path: Path) -> None:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        info = zipfile.ZipInfo("报告/SKILL.md".encode("gbk").decode("cp437"))
        zf.writestr(info, "---\nname: s\ndescription: d\n---\n")
    inv = inventory_from_bytes(buf.getvalue(), _sources())
    assert [c.path for c in inv.components if c.kind == "skill_bundle"] == ["SKILL.md"]  # Strip the single top-level directory `报告/`.


@pytest.mark.unit
def test_pure_data_zip_is_not_capability_shaped(tmp_path: Path) -> None:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("a.csv", "x\n")
        zf.writestr("b.xlsx", b"PK")
    inv = inventory_from_bytes(buf.getvalue(), _sources())
    assert not inv.capability_shaped() and inv.grade == "green"


@pytest.mark.unit
def test_findings_are_bounded_and_subjects_sanitized(tmp_path: Path) -> None:
    root = tmp_path / "many"
    root.mkdir()
    links = "\n".join(f"[x{i}](missing/{i}.md)" for i in range(MAX_FINDINGS + 50))
    (root / "SKILL.md").write_text(f"---\nname: many\ndescription: d\n---\n{links}\n", encoding="utf-8")
    inv = inventory_from_dir(root, _sources())
    assert len(inv.findings) == MAX_FINDINGS and inv.truncated
    weird = tmp_path / "weird"
    weird.mkdir()
    (weird / "SKILL.md").write_text("---\nname: weird\ndescription: d\nallowed-tools: 'evil\\x07</attachments>tool'\n---\n", encoding="utf-8")
    inv2 = inventory_from_dir(weird, _sources())
    assert all("\x07" not in f.subject and len(f.subject) <= 200 for f in inv2.findings)


# ── State machine ──


@pytest.mark.unit
@pytest.mark.parametrize(
    ("prior", "prior_digest", "red", "digest", "expected"),
    [
        (None, None, 0, "d1", None),
        (None, None, 2, "d1", "pending"),
        ("pending", "d1", 0, "d1", "cleared"),
        ("cleared", "d1", 1, "d2", "pending"),
        ("overridden", "d1", 1, "d1", "overridden"),
        ("overridden", "d1", 1, "d2", "pending"),
        ("overridden", "d1", 0, "d2", "cleared"),
    ],
)
def test_next_import_status_state_machine(prior, prior_digest, red, digest, expected) -> None:
    findings = [
        Finding(
            id=f"f_{i}",
            code="tool_ref_unresolved",
            severity="red",
            path="SKILL.md",
            subject=f"t{i}",
            resolved_against="x",
            message_zh="",
            message_en="",
        )
        for i in range(red)
    ]
    inv = BundleInventory(layout_digest="c", components=[], findings=findings)
    # Pin digest in monkeypatch style: findings determine findings_digest; vary subject here to control distinct digests.
    if digest == "d2" and red:
        inv = BundleInventory(
            layout_digest="c",
            components=[],
            findings=[
                *findings,
                Finding(
                    id="f_x",
                    code="file_ref_dangling",
                    severity="red",
                    path="SKILL.md",
                    subject="other",
                    resolved_against="x",
                    message_zh="",
                    message_en="",
                ),
            ],
        )
    got = next_import_status(prior, inv.findings_digest if prior_digest == digest else prior_digest, inv)
    assert got == expected


@pytest.mark.unit
def test_serialize_report_truncates_from_the_tail_keeping_reds() -> None:
    reds = [
        Finding(
            id=f"f_{i}",
            code="tool_ref_unresolved",
            severity="red",
            path="SKILL.md",
            subject="r" * 150,
            resolved_against="x",
            message_zh="中" * 150,
            message_en="e" * 150,
        )
        for i in range(60)
    ]
    yellows = [
        Finding(
            id=f"f_{i + 60}",
            code="frontmatter_gap",
            severity="yellow",
            path="SKILL.md",
            subject="y" * 150,
            resolved_against="x",
            message_zh="中" * 150,
            message_en="e" * 150,
        )
        for i in range(120)
    ]
    inv = BundleInventory(layout_digest="c", components=[], findings=reds + yellows)
    data = json.loads(serialize_report(inv))
    assert len(json.dumps(data, ensure_ascii=False).encode("utf-8")) <= 64 * 1024
    assert data["truncated"] is True
    assert sum(1 for f in data["findings"] if f["severity"] == "red") == 60  # Retain all red findings.


# ── Positive control: inventory built-in skills with the **production assembler** ──
#
# Production uploads use only sandbox packages (`sandbox_python_modules()`), while built-in skill scripts run in the app container.
# Do not erase this difference by taking the union with venv packages; **assert it as a known result**: the rule is correct, but these built-in skills'
# reference scripts import packages absent from the sandbox. They are not uploads, so do not become pending, but uploading an identical
# bundle should produce these findings. Fixing built-in skills is outside this batch.
_EXPECTED_BUILTIN_IMPORT_FINDINGS: dict[str, set[str]] = {
    "canvas-pdf": {"reportlab"},
    "pdf": {"pdf2image"},
    "bowra-docx": {"jsonschema"},
    "bowra-pdf": {"jsonschema", "reportlab"},
    "bowra-pptx": {"jsonschema"},
    "bowra-xlsx": {"jsonschema"},
}
# mcp-builder scripts also import anthropic/mcp, absent from the sandbox, but kind: knowledge (reference code) makes them yellow, excluding them above.


@pytest.mark.unit
async def test_builtin_catalog_under_production_sources_has_only_the_known_findings() -> None:
    from bowhead_core.skill_import.hook import build_sources
    from bowhead_core.tool_registry import ToolRegistry

    cat = builtin_catalog_dir()
    skill_dirs = sorted(p.parent for p in cat.glob("*/SKILL.md"))
    assert len(skill_dirs) >= 30
    # Production-equivalent assembly: a real ToolRegistry (empty registry ⇒ empty tool-name set, not None) + built-in skill names; sandbox packages.
    src = await build_sources(
        registry=ToolRegistry(),
        skill_catalog=None,
        personal_store=None,
        user_id=None,
        team_store=None,
        team_id=None,
        team_tool_names=frozenset(),
    )
    src = ResolutionSources(**{**src.__dict__, "script_mode": "sandbox"})
    assert src.tool_names is not None and src.python_modules
    got_reds: dict[str, set[tuple[str, str]]] = {}
    for d in skill_dirs:
        inv = inventory_from_dir(d, src)
        reds = {(f.code, f.subject) for f in inv.findings if f.severity == "red"}
        if reds:
            got_reds[d.name] = reds
    expected = {name: {("script_import_unresolved", m) for m in mods} for name, mods in _EXPECTED_BUILTIN_IMPORT_FINDINGS.items()}
    # Exact equality: fewer means broken rules/expired known exceptions; more means false positives or new true positives.
    assert got_reds == expected, (
        f"diff: unexpected={ {k: v - expected.get(k, set()) for k, v in got_reds.items() if v - expected.get(k, set())} } missing={ {k: v - got_reds.get(k, set()) for k, v in expected.items() if v - got_reds.get(k, set())} }"
    )


@pytest.mark.unit
def test_builtin_catalog_has_no_false_red_on_runtime_independent_rules() -> None:
    """脚本 import 之外的红规则（工具/skill 引用、链接、stdio）对 37 个内置 skill 必须零红，
    且不需要任何手写工具名——内置 skill 的 allowed-tools 全是外来形态（归黄）或不写."""
    cat = builtin_catalog_dir()
    skill_dirs = sorted(p.parent for p in cat.glob("*/SKILL.md"))
    src = ResolutionSources(
        tool_names=frozenset(),
        skill_names=frozenset(d.name for d in skill_dirs),
        script_mode="disabled",  # Script rules become yellow; check only runtime-independent reds.
        python_modules=sandbox_python_modules(),
        node_packages=sandbox_node_packages(),
    )
    reds = {d.name: {(f.code, f.subject) for f in inventory_from_dir(d, src).findings if f.severity == "red"} for d in skill_dirs}
    assert not {k: v for k, v in reds.items() if v}


@pytest.mark.unit
def test_knowledge_kind_or_flag_off_downgrades_missing_import_to_yellow(tmp_path: Path) -> None:
    root = tmp_path / "k"
    (root / "scripts").mkdir(parents=True)
    (root / "scripts" / "ref.py").write_text("import pdf2image\n", encoding="utf-8")
    (root / "SKILL.md").write_text("---\nname: k\ndescription: d\nkind: knowledge\n---\nbody\n", encoding="utf-8")
    inv = inventory_from_dir(root, _sources(script_mode="sandbox"))
    hit = next(f for f in inv.findings if f.code == "script_import_unresolved")
    assert hit.severity == "yellow" and inv.grade == "yellow"
    (root / "SKILL.md").write_text("---\nname: k\ndescription: d\n---\nbody\n", encoding="utf-8")
    assert (
        next(f for f in inventory_from_dir(root, _sources(script_mode="disabled")).findings if f.code == "script_import_unresolved").severity
        == "yellow"
    )
    assert (
        next(f for f in inventory_from_dir(root, _sources(script_mode="sandbox")).findings if f.code == "script_import_unresolved").severity == "red"
    )


@pytest.mark.unit
def test_override_digest_ignores_deployment_facts(tmp_path: Path) -> None:
    """overridden 钉在红指纹上：只翻黄的部署事实（script_not_executable）不改指纹；
    但让脚本缺包从黄变红的翻转会改——那是新阻断，回 pending 是对的."""
    root = _write_foreign_bundle(tmp_path / "acme")
    resolvable = sandbox_python_modules() | {"playwright"}
    on = inventory_from_dir(root, _sources(script_mode="sandbox", python_modules=resolvable))
    off = inventory_from_dir(root, _sources(script_mode="disabled", python_modules=resolvable))
    assert {f.code for f in off.findings} - {f.code for f in on.findings} == {"script_not_executable"}
    assert on.findings_digest == off.findings_digest
    off_missing = inventory_from_dir(root, _sources(script_mode="disabled"))
    on_missing = inventory_from_dir(root, _sources(script_mode="sandbox"))
    assert off_missing.findings_digest != on_missing.findings_digest


@pytest.mark.unit
def test_adversarial_inputs_stay_linear(tmp_path: Path) -> None:
    """安全 review 逮到的 ReDoS：缺闭合围栏的 frontmatter（DOTALL 正则 250KB 实测 178s）与 `[`×256KB 的链接正则
    （19s）都跑在 gateway 事件循环上。改成线性解析后必须毫秒级；`agents/*.md` 不经上传校验就进清点，所以两条都要钉."""
    import time

    root = tmp_path / "adv"
    (root / "agents").mkdir(parents=True)
    (root / "SKILL.md").write_text("---\nname: adv\ndescription: d\n---\n" + "[" * 250_000, encoding="utf-8")
    (root / "agents" / "x.md").write_text("---\n" + "\n" * 250_000, encoding="utf-8")
    (root / "scripts").mkdir()
    (root / "scripts" / "deep.py").write_text("-" * 10_000 + "1\n", encoding="utf-8")  # CPython parser stack-overflow guard.
    (root / "scripts" / "newlines.py").write_text(" \n" * 128_000, encoding="utf-8")  # `^\s*` with MULTILINE once took 20s.
    (root / "references").mkdir()
    (root / "references" / "links.md").write_text("[a](" * 64_000, encoding="utf-8")
    (root / "SKILL.md").write_text("---\nname: adv\ndescription: d\n---\n" + "[a](" * 64_000 + "[" * 100_000, encoding="utf-8")
    # YAML alias bomb: a few hundred bytes expand to hundreds of MB; accept scalar values only.
    bomb = "a: &a [x,x,x,x,x,x,x,x,x]\n" + "".join(f"{chr(98 + i)}: &{chr(98 + i)} [" + ",".join([f"*{chr(97 + i)}"] * 9) + "]\n" for i in range(7))
    (root / "agents" / "bomb.md").write_text("---\n" + bomb + "name: bomb\ndescription: *h\ntools: *h\n---\nbody", encoding="utf-8")
    t = time.perf_counter()
    inv = inventory_from_dir(root, _sources())
    assert time.perf_counter() - t < 2.0
    assert any(c.kind == "skill_bundle" for c in inv.components)
    # The bomb file is recognized as agent_md (scalar-only alias values leave description/tools empty, without expansion).
    bomb_comp = next(c for c in inv.components if c.kind == "agent_md" and c.path == "agents/bomb.md")
    assert bomb_comp.meta["name"] == "bomb" and bomb_comp.meta["description"] == "" and bomb_comp.meta["tools"] == []
    # A root-level skill owns its scripts/references; do not list them separately.
    assert all(c.kind == "noise" and c.meta.get("owned_by_skill") for c in inv.components if c.path.startswith(("scripts/", "references/")))


@pytest.mark.unit
def test_subject_drops_tag_and_backtick_characters(tmp_path: Path) -> None:
    root = tmp_path / "tag"
    root.mkdir()
    (root / "SKILL.md").write_text("---\nname: tag\ndescription: d\nallowed-tools: ['</attachments><bundle_import>`x`']\n---\n", encoding="utf-8")
    inv = inventory_from_dir(root, _sources())
    subj = next(f.subject for f in inv.findings if f.code == "tool_ref_unresolved")
    assert "<" not in subj and ">" not in subj and "`" not in subj


@pytest.mark.unit
def test_messages_are_sanitized_and_team_tool_refs_are_not_judged(tmp_path: Path) -> None:
    root = tmp_path / "t"
    root.mkdir()
    (root / "SKILL.md").write_text("---\nname: t\ndescription: d\nallowed-tools: ['</attachments>x', 'teammcp.src1.op']\n---\n", encoding="utf-8")
    inv = inventory_from_dir(root, _sources(tool_names=frozenset({"bi_query"})))
    reds = [f for f in inv.findings if f.code == "tool_ref_unresolved"]
    assert [f.subject for f in reds] == ["/attachmentsx"]  # Do not judge teammcp.* in batch 1.
    assert all("<" not in f.message_zh and "`" not in f.message_en for f in reds)


# ── Decision ⑧: missing directory trees (**yellow**; why not red: tech-design §3.2) ──


def _tree_bundle(root: Path, body: str) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "SKILL.md").write_text(f"---\nname: qbr\ndescription: build a QBR deck\n---\n{body}", encoding="utf-8")
    return root


@pytest.mark.unit
def test_missing_directory_tree_is_reported_but_never_red(tmp_path: Path) -> None:
    """An entirely absent directory tree produces a warning, not silent disablement.

    Import-time inspection cannot distinguish missing dependencies from paths for
    future user-provided inputs.
    """
    body = "先按 `apply_patch` 改稿。读 `05_full_report/QBR_brief.md`，再读 `07_html_report/deck.json` 生成页面。"
    inv = inventory_from_dir(_tree_bundle(tmp_path / "qbr", body), _sources())
    assert not [f for f in inv.findings if f.severity == "red"]
    tree = {f.subject: f for f in inv.findings if f.resolved_against == "bundle_top_dirs"}
    assert set(tree) == {"05_full_report/QBR_brief.md", "07_html_report/deck.json"}
    assert all(f.severity == "yellow" for f in tree.values())
    # Sanitization strips message backticks (strip all backticks/angle brackets from strings entering prompts).
    assert "整个没有 05_full_report/ 这个目录" in tree["05_full_report/QBR_brief.md"].message_zh


@pytest.mark.unit
def test_present_directory_gets_the_generic_message_not_the_absent_one(tmp_path: Path) -> None:
    """目录在、只是这个文件不在 ⇒ 用普通文案。说「整个没有 X/」时包里真的不能有 X/（review 逮到过说假话）."""
    root = _tree_bundle(tmp_path / "s", "读 `notes/a.md` 与 `notes/missing.md`。")
    (root / "notes").mkdir()
    (root / "notes" / "a.md").write_text("x", encoding="utf-8")
    inv = inventory_from_dir(root, _sources())
    hits = [f for f in inv.findings if f.subject == "notes/missing.md"]
    assert hits and hits[0].severity == "yellow" and hits[0].resolved_against == "bundle_files"
    assert "整个没有" not in hits[0].message_zh


@pytest.mark.unit
def test_nested_skill_does_not_claim_absence_for_a_bundle_root_directory(tmp_path: Path) -> None:
    """嵌套 skill 读不到 `helpers/a.md`，但包根确实有 `helpers/` ⇒ 只能说文件不在，不能说目录不在."""
    root = tmp_path / "bundle"
    (root / "acme").mkdir(parents=True)
    (root / "acme" / "SKILL.md").write_text("---\nname: acme\ndescription: d\n---\n读 `helpers/a.md` 与 `helpers/b.md`。", encoding="utf-8")
    (root / "helpers").mkdir()
    (root / "helpers" / "z.md").write_text("unrelated", encoding="utf-8")
    inv = inventory_from_dir(root, _sources())
    assert not [f for f in inv.findings if f.severity == "red"]
    assert all("整个没有" not in f.message_zh for f in inv.findings)


@pytest.mark.unit
def test_relative_and_scheme_references_are_not_called_directories(tmp_path: Path) -> None:
    """review 实测过的胡话：「包里根本没有 `s3:/` 这个目录」。scheme 与 `./` 都不当目录名判，
    而 `./x/y` 必须与 `x/y` 同一判定（否则加个点就绕过整条规则）。"""
    body = "读 `./05_full_report/a.md` 与 `s3://lake/2026/sales.json`，另见 [湖](s3://lake/meta.yaml)。"
    inv = inventory_from_dir(_tree_bundle(tmp_path / "s", body), _sources())
    assert not [f for f in inv.findings if f.severity == "red"]  # Scheme links no longer count as missing uploads.
    subjects = {f.subject for f in inv.findings if f.code == "file_ref_unverified"}
    assert subjects == {"./05_full_report/a.md"}  # Skip all schemes.
    hit = next(f for f in inv.findings if f.subject == "./05_full_report/a.md")
    assert hit.resolved_against == "bundle_top_dirs" and "整个没有 05_full_report/ 这个目录" in hit.message_zh


@pytest.mark.unit
def test_same_path_written_differently_counts_once(tmp_path: Path) -> None:
    body = "读 `proj/a.md`，也就是 `./proj/a.md`，写全是 `proj//a.md`。"
    inv = inventory_from_dir(_tree_bundle(tmp_path / "s", body), _sources())
    assert len([f for f in inv.findings if f.code == "file_ref_unverified"]) == 1


@pytest.mark.unit
def test_parent_reference_resolves_inside_the_bundle(tmp_path: Path) -> None:
    """`acme/../shared/x.md` 指的就是包里的 `shared/x.md`——折平后能对上就不该报任何东西."""
    root = tmp_path / "bundle"
    (root / "acme").mkdir(parents=True)
    (root / "acme" / "SKILL.md").write_text("---\nname: acme\ndescription: d\n---\n读 [x](../shared/x.md) 与 `../shared/y.md`。", encoding="utf-8")
    (root / "shared").mkdir()
    for n in ("x.md", "y.md"):
        (root / "shared" / n).write_text("s", encoding="utf-8")
    inv = inventory_from_dir(root, _sources())
    assert not [f for f in inv.findings if f.code in ("file_ref_dangling", "file_ref_unverified")]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("label", "body"),
    [
        ("pip install", "首次使用先 `pip install pandas`。取数后写到 `sales_out/2026-Q1.md` 与 `sales_out/summary.md`。"),
        ("Windows 支持说明", "本 skill 支持 Windows：路径形如 `C:\\Users\\me`。把底稿放到 `底稿/q1.md` 与 `底稿/q2.md`。"),
        ("否定式提及", "注意：本平台没有 `apply_patch`，改文件请用沙箱。输入是 `素材/brief.md` 与 `素材/data.json`。"),
        ("产物目录", "`apply_patch` 之后，结果写到 `reports/2026-Q1.md` 与 `reports/summary.md`。"),
        ("兄弟 skill", "`apply_patch` 后照 `../acme-fetch/contract.md` 与 `../acme-render/theme.md` 走。"),
    ],
)
def test_no_red_for_these_shapes(tmp_path: Path, label: str, body: str) -> None:
    """review 逐条实测出的误杀面。红会让 skill 静默停用，这些都不能红。"""
    inv = inventory_from_dir(_tree_bundle(tmp_path / str(abs(hash(label))), body), _sources())
    reds = [(f.code, f.subject) for f in inv.findings if f.severity == "red"]
    assert not reds, f"{label} 被误判为红：{reds}"
