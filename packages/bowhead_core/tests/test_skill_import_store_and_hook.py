"""skill-import 批1：skill_instances 两列的增量迁移、独立 setter、写路径钩子的状态流转."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

import pytest
from bowhead_core.skill_import.hook import apply_import_inventory
from bowhead_core.skill_import.inventory import ResolutionSources
from bowhead_core.skill_instance_store import SkillInstanceStore


def _run(coro):
    return asyncio.run(coro)


def _skill_dir(root: Path, name: str, *, allowed_tools: str = "") -> Path:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    fm = f"---\nname: {name}\ndescription: d\n" + (f"allowed-tools: {allowed_tools}\n" if allowed_tools else "") + "---\n# body\n"
    (d / "SKILL.md").write_text(fm, encoding="utf-8")
    return d


@pytest.mark.unit
def test_existing_table_gets_the_two_columns_and_old_rows_read_as_null(tmp_path: Path) -> None:
    db = tmp_path / "u.db"
    # Existing table: no new columns.
    con = sqlite3.connect(db)
    con.executescript(
        """
        CREATE TABLE skill_instances (
            id TEXT PRIMARY KEY, user_id TEXT NOT NULL, name TEXT NOT NULL, source_type TEXT NOT NULL,
            source_path TEXT NOT NULL, version TEXT, description TEXT, enabled INTEGER NOT NULL DEFAULT 1,
            source_turn_id TEXT, size_bytes INTEGER DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            UNIQUE(user_id, name));
        INSERT INTO skill_instances VALUES ('sk_1','u1','old','user_upload','/x',NULL,NULL,1,NULL,0,'t','t');
        """
    )
    con.commit()
    con.close()
    store = SkillInstanceStore(str(db))
    _run(store.initialize())
    cols = {r[1] for r in sqlite3.connect(db).execute("PRAGMA table_info(skill_instances)")}
    assert {"import_status", "import_report_json"} <= cols
    inst = _run(store.get(user_id="u1", name="old"))
    assert inst is not None and inst.import_status is None and inst.import_report_json is None and not inst.import_pending
    assert _run(store.set_import_status(user_id="u1", name="old", import_status="pending", import_report_json='{"v":1}'))
    inst = _run(store.get(user_id="u1", name="old"))
    assert inst.import_pending and json.loads(inst.import_report_json) == {"v": 1}
    # upsert leaves these two columns unchanged (preference toggles/content resaves do not erase status).
    _run(store.upsert(user_id="u1", name="old", source_type="user_upload", source_path="/x", enabled=False))
    inst = _run(store.get(user_id="u1", name="old"))
    assert inst.import_status == "pending" and inst.enabled is False
    assert not _run(store.set_import_status(user_id="u1", name="nope", import_status=None, import_report_json=None))


@pytest.mark.unit
def test_hook_flows_pending_cleared_overridden(tmp_path: Path) -> None:
    store = SkillInstanceStore(str(tmp_path / "u.db"))
    _run(store.initialize())
    d = _skill_dir(tmp_path, "acme", allowed_tools="data_fetch_skill")
    _run(store.upsert(user_id="u1", name="acme", source_type="user_upload", source_path=str(d), enabled=True))
    src = ResolutionSources(tool_names=frozenset({"bi_query"}), skill_names=frozenset(), script_mode="sandbox")

    inv = _run(apply_import_inventory(store, user_id="u1", name="acme", skill_dir=d, sources=src))
    assert inv is not None and inv.red_count == 1
    inst = _run(store.get(user_id="u1", name="acme"))
    assert inst.import_status == "pending" and inst.enabled is True  # enabled unchanged.
    report = json.loads(inst.import_report_json)
    assert report["findings"][0]["subject"] == "data_fetch_skill"

    # User overrides → overridden, pinned to the fingerprint; rerunning unchanged content remains overridden.
    _run(store.set_import_status(user_id="u1", name="acme", import_status="overridden", import_report_json=inst.import_report_json))
    _run(apply_import_inventory(store, user_id="u1", name="acme", skill_dir=d, sources=src))
    assert _run(store.get(user_id="u1", name="acme")).import_status == "overridden"

    # Changed content, still red, changed fingerprint → pending again.
    _skill_dir(tmp_path, "acme", allowed_tools="data_fetch_skill, other_missing")
    _run(apply_import_inventory(store, user_id="u1", name="acme", skill_dir=d, sources=src))
    assert _run(store.get(user_id="u1", name="acme")).import_status == "pending"

    # Clear red → cleared; another save with red=0 remains cleared.
    _skill_dir(tmp_path, "acme", allowed_tools="bi_query")
    _run(apply_import_inventory(store, user_id="u1", name="acme", skill_dir=d, sources=src))
    assert _run(store.get(user_id="u1", name="acme")).import_status == "cleared"
    _run(apply_import_inventory(store, user_id="u1", name="acme", skill_dir=d, sources=src))
    assert _run(store.get(user_id="u1", name="acme")).import_status == "cleared"


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("concurrent_change", ["bytes", "owner_decision", "scanner_failure"])
async def test_inventory_cannot_clear_newer_content_or_replace_owner_review(tmp_path, monkeypatch, concurrent_change):
    import threading

    import anyio
    from bowhead_core.skill_import import hook

    store = SkillInstanceStore(str(tmp_path / "skills.db"))
    await store.initialize()
    directory = _skill_dir(tmp_path, "method")
    await store.upsert(user_id="u1", name="method", source_type="user_generated", source_path=str(directory))
    await store.set_import_status(user_id="u1", name="method", import_status="pending", import_report_json='{"original":true}')
    entered, resume = threading.Event(), threading.Event()
    original = hook.inventory_from_dir

    def inspect(*args):
        result = original(*args)
        entered.set()
        assert resume.wait(10), "Inventory test must release its controlled pause"
        if concurrent_change == "scanner_failure":
            raise RuntimeError("Injected scanner failure")
        return result

    monkeypatch.setattr(hook, "inventory_from_dir", inspect)
    task = asyncio.create_task(hook.apply_import_inventory(store, user_id="u1", name="method", skill_dir=directory, sources=ResolutionSources()))
    try:
        assert await anyio.to_thread.run_sync(entered.wait, 10)
        if concurrent_change == "bytes":
            # Simulate an external filesystem writer that does not honor application locks.
            _skill_dir(tmp_path, "method", allowed_tools="missing_operation")
        state = "overridden" if concurrent_change == "owner_decision" else "pending"
        await store.set_import_status(user_id="u1", name="method", import_status=state, import_report_json='{"newer":true}')
    finally:
        resume.set()
    assert await task is None
    current = await store.get(user_id="u1", name="method")
    assert current.import_status == state and current.import_report_json == '{"newer":true}'


@pytest.mark.unit
def test_declaring_an_installable_requirement_resolves_the_import(tmp_path: Path) -> None:
    """声明镜像/wheelhouse 里**有**的包 ⇒ 该 import 算解析，不报 finding."""
    store = SkillInstanceStore(str(tmp_path / "u.db"))
    _run(store.initialize())
    d = tmp_path / "needs-pkg"
    (d / "scripts").mkdir(parents=True)
    (d / "SKILL.md").write_text("---\nname: needs-pkg\ndescription: d\nsandbox_requirements: [pandas]\n---\nbody\n", encoding="utf-8")
    (d / "scripts" / "run.py").write_text("import pandas\nimport json\n", encoding="utf-8")
    _run(store.upsert(user_id="u1", name="needs-pkg", source_type="user_upload", source_path=str(d), enabled=True))
    src = ResolutionSources(tool_names=frozenset(), skill_names=frozenset(), script_mode="sandbox", python_modules=frozenset())
    inv = _run(apply_import_inventory(store, user_id="u1", name="needs-pkg", skill_dir=d, sources=src))
    assert inv is not None and not [f for f in inv.findings if f.code == "script_import_unresolved"]
    assert _run(store.get(user_id="u1", name="needs-pkg")).import_status is None  # Never red ⇒ NULL.


@pytest.mark.unit
def test_declaring_an_uninstallable_requirement_does_not_silence_the_finding(tmp_path: Path) -> None:
    """★ 此前这里是「声明即放行」：`sandbox_python_modules()` 本就含 wheelhouse ⇒ 那个并集**唯一能新增的**
    就是两份 SSOT 里都没有、离线装不上的包。写一行 sandbox_requirements 就能让红消失，而运行时
    `pip --no-index` 找不到它（wheelhouse 自己的注释：不在锁定哈希集里的一律 fail closed）。
    上一版测试用的 `tabulate` 恰恰不在集合里——它把这个 bug 钉成了「正确行为」。"""
    from bowhead_core.skill_import.sources import sandbox_python_dists

    assert "tabulate" not in sandbox_python_dists()  # Criterion prerequisite: actually uninstallable.
    store = SkillInstanceStore(str(tmp_path / "u.db"))
    _run(store.initialize())
    d = tmp_path / "needs-pkg"
    (d / "scripts").mkdir(parents=True)
    (d / "SKILL.md").write_text("---\nname: needs-pkg\ndescription: d\nsandbox_requirements: [tabulate>=0.9]\n---\nbody\n", encoding="utf-8")
    (d / "scripts" / "run.py").write_text("import tabulate\n", encoding="utf-8")
    _run(store.upsert(user_id="u1", name="needs-pkg", source_type="user_upload", source_path=str(d), enabled=True))
    src = ResolutionSources(tool_names=frozenset(), skill_names=frozenset(), script_mode="sandbox", python_modules=frozenset({"pandas"}))
    inv = _run(apply_import_inventory(store, user_id="u1", name="needs-pkg", skill_dir=d, sources=src))
    hits = [f for f in inv.findings if f.code == "script_import_unresolved"]
    assert [f.subject for f in hits] == ["tabulate"] and hits[0].severity == "red"
    assert _run(store.get(user_id="u1", name="needs-pkg")).import_status == "pending"


@pytest.mark.unit
@pytest.mark.parametrize(("dist", "module", "tied"), [("tabulate", "tabulate", True), ("foo-bar", "foo_bar", True), ("python-magic", "magic", False)])
def test_uninstallable_declaration_is_said_accurately_not_called_undeclared(tmp_path: Path, dist: str, module: str, tied: bool) -> None:
    """文案单独一条（与「不消音」拆开：措辞退化和闸失效在信号上要能分开）。

    `python-magic`→`magic` 不在 `_DIST_TO_IMPORT` 表里，认不出它来自哪个声明——那就**不许断言「没声明」**，
    只说可证明的事实。两种情况都不能出现假话。"""
    store = SkillInstanceStore(str(tmp_path / "u.db"))
    _run(store.initialize())
    d = tmp_path / "needs-pkg"
    (d / "scripts").mkdir(parents=True)
    (d / "SKILL.md").write_text(f"---\nname: needs-pkg\ndescription: d\nsandbox_requirements: [{dist}]\n---\nbody\n", encoding="utf-8")
    (d / "scripts" / "run.py").write_text(f"import {module}\n", encoding="utf-8")
    _run(store.upsert(user_id="u1", name="needs-pkg", source_type="user_upload", source_path=str(d), enabled=True))
    src = ResolutionSources(tool_names=frozenset(), skill_names=frozenset(), script_mode="sandbox", python_modules=frozenset({"pandas"}))
    inv = _run(apply_import_inventory(store, user_id="u1", name="needs-pkg", skill_dir=d, sources=src))
    hit = next(f for f in inv.findings if f.code == "script_import_unresolved")
    assert "没有在 frontmatter sandbox_requirements 里声明" not in hit.message_zh  # Neither case may make this false claim.
    if tied:
        assert "已在 frontmatter sandbox_requirements 里声明" in hit.message_zh and "安装会失败" in hit.message_zh
    else:
        assert "不在沙箱镜像与离线 wheelhouse 的可用包集里" in hit.message_zh


@pytest.mark.unit
def test_hook_returns_none_for_unknown_row(tmp_path: Path) -> None:
    store = SkillInstanceStore(str(tmp_path / "u.db"))
    _run(store.initialize())
    d = _skill_dir(tmp_path, "ghost")
    assert _run(apply_import_inventory(store, user_id="u1", name="ghost", skill_dir=d, sources=ResolutionSources())) is None


@pytest.mark.unit
def test_serialize_report_trims_member_paths_before_findings_for_big_bundles() -> None:
    """review P2：KB 包一个 skill 带几百份 references ⇒ 报告 185KB > 64KB，此前直接丢持久化；现在先裁路径列表."""
    import json

    from bowhead_core.skill_import.hook import MAX_IMPORT_REPORT_BYTES, serialize_report
    from bowhead_core.skill_import.inventory import BundleInventory, Component, Finding

    members = [f"acme/references/{'x' * 80}{i}.md" for i in range(1500)]
    comps = [Component(kind="skill_bundle", path="acme/SKILL.md", meta={"name": "acme", "member_paths": members, "references": members})]
    comps += [Component(kind="document", path=f"docs/{'d' * 60}{i}.pdf", meta={"size": 1}) for i in range(600)]
    red = Finding(
        id="f1",
        code="tool_unresolved",
        severity="red",
        path="acme/SKILL.md",
        subject="apply_patch",
        resolved_against="registry",
        message_zh="m",
        message_en="m",
    )
    inv = BundleInventory(layout_digest="d", components=comps, findings=[red])
    text = serialize_report(inv)
    assert len(text.encode("utf-8")) <= MAX_IMPORT_REPORT_BYTES
    data = json.loads(text)
    assert data["truncated"] is True
    assert data["findings"] and data["findings"][0]["severity"] == "red"  # Truncate reds last.
    skill = next(c for c in data["components"] if c["kind"] == "skill_bundle")
    assert len(skill["meta"]["member_paths"]) == 20


@pytest.mark.unit
def test_serialize_report_never_exceeds_cap_and_does_not_mutate_inventory() -> None:
    """re-review P2：50 个 skill 的包 67KB 仍超限且把 200 条 findings 全丢；且裁剪改了活的 inventory."""
    import json

    from bowhead_core.skill_import.hook import MAX_IMPORT_REPORT_BYTES, serialize_report
    from bowhead_core.skill_import.inventory import BundleInventory, Component, Finding

    comps = [
        Component(
            kind="skill_bundle",
            path=f"s{i}/SKILL.md",
            meta={"name": f"s{i}", "description": "d" * 300, "member_paths": [f"s{i}/references/{j}.md" for j in range(30)]},
        )
        for i in range(2000)
    ]
    reds = [
        Finding(
            id=f"f{i}",
            code="tool_unresolved",
            severity="red",
            path=f"s{i}/SKILL.md",
            subject="x",
            resolved_against="registry",
            message_zh="m",
            message_en="m",
        )
        for i in range(300)
    ]
    inv = BundleInventory(layout_digest="d", components=comps, findings=reds)
    text = serialize_report(inv)
    assert len(text.encode("utf-8")) <= MAX_IMPORT_REPORT_BYTES
    data = json.loads(text)
    assert data["truncated"] and data["components_dropped"] >= 1800 and len(data["components"]) <= 200
    assert data["findings"], "组件压缩后应有余量留给红 findings"
    # Do not mutate the input object.
    assert len(inv.components[0].meta["member_paths"]) == 30 and len(inv.components) == 2000


@pytest.mark.unit
def test_serialize_report_trims_landed_skills_too_and_never_exceeds_cap() -> None:
    """批2 review P1-1：`extra` 曾豁免裁剪 ⇒ 200 条 landed_skills 把报告顶到 178KB，
    梯子见底仍超限 ⇒ 整行不落库、只剩一条 warning。现在它参与最后两级裁剪。"""
    import json

    from bowhead_core.skill_import.hook import MAX_IMPORT_REPORT_BYTES, serialize_report
    from bowhead_core.skill_import.inventory import BundleInventory, Component

    comps = [Component(kind="skill_bundle", path=f"s{i}/SKILL.md", meta={"name": f"s{i}"}) for i in range(400)]
    inv = BundleInventory(layout_digest="d", components=comps, findings=[])
    huge = [{"name": "n" * 300, "path": "p" * 300, "code": "c" * 200, "status": "denied", "import_status": None} for _ in range(200)]
    text = serialize_report(inv, extra={"landed_skills": huge})
    assert len(text.encode("utf-8")) <= MAX_IMPORT_REPORT_BYTES
    data = json.loads(text)
    assert data["truncated"]
    # Compress to render-required fields; retain declared_name and red_count (rename matching key + this skill's own red count).
    assert set(data["landed_skills"][0]) == {"name", "declared_name", "status", "import_status", "red_count"}
    # If still too large, reduce item count and record how many were dropped.
    text2 = serialize_report(inv, extra={"landed_skills": huge * 10})
    assert len(text2.encode("utf-8")) <= MAX_IMPORT_REPORT_BYTES
    data2 = json.loads(text2)
    assert len(data2["landed_skills"]) <= 50 and data2["landed_skills_dropped"] > 0


@pytest.mark.unit
def test_serialize_report_extra_cannot_overwrite_inventory_keys() -> None:
    """`extra` 用 setdefault：两个真相源打架时，清点自己的键说了算."""
    import json

    from bowhead_core.skill_import.hook import serialize_report
    from bowhead_core.skill_import.inventory import BundleInventory, Component

    inv = BundleInventory(layout_digest="d", components=[Component(kind="skill_bundle", path="a/SKILL.md", meta={"name": "a"})], findings=[])
    data = json.loads(serialize_report(inv, extra={"grade": "green", "components": [], "landed_skills": [{"name": "a"}]}))
    assert data["grade"] == inv.grade and len(data["components"]) == 1
    assert data["landed_skills"] == [{"name": "a"}]


@pytest.mark.unit
def test_serialize_report_has_an_unconditional_floor_for_any_extra_shape() -> None:
    """re-review P2：梯子只认 list 形状的 landed_skills，别的形状/别的键一路穿到底 ⇒ 返回 200KB。
    最后一级无条件丢掉所有非清点键：超限等于那一行根本不落库。"""
    import json

    from bowhead_core.skill_import.hook import MAX_IMPORT_REPORT_BYTES, serialize_report
    from bowhead_core.skill_import.inventory import BundleInventory, Component

    inv = BundleInventory(layout_digest="d", components=[Component(kind="skill_bundle", path="a/SKILL.md", meta={"name": "a"})], findings=[])
    for extra in (
        {"landed_skills": {"k": "v" * 100000}},
        {"landed_skills": "z" * 200000},
        {"landed_skills": [{"name": "a"}], "notes": "q" * 200000},
    ):
        text = serialize_report(inv, extra=extra)
        assert len(text.encode("utf-8")) <= MAX_IMPORT_REPORT_BYTES, extra
        assert json.loads(text)["extra_dropped"] is True


@pytest.mark.unit
def test_renamed_and_long_names_survive_the_trim_into_the_block() -> None:
    """re-review P1：裁剪把 declared_name 砍掉后，渲染侧对每个改过名的 skill 都谎报「没导入」."""
    import json

    from bowhead_core.skill_import.hook import serialize_report
    from bowhead_core.skill_import.inventory import BundleInventory, Component
    from bowhead_core.skill_import.lead_block import render_bundle_import_block

    comps = [
        Component(kind="skill_bundle", path=f"s{i}/SKILL.md", meta={"name": f"declared-{i}", "member_paths": [f"s{i}/r/{j}.md" for j in range(200)]})
        for i in range(400)
    ]
    inv = BundleInventory(layout_digest="d", components=comps, findings=[])
    landed = [
        {"name": f"real-{i}", "declared_name": f"declared-{i}", "status": "landed", "import_status": "pending", "red_count": 2} for i in range(400)
    ]
    report = json.loads(serialize_report(inv, extra={"landed_skills": landed}))
    block = render_bundle_import_block([{"filename": "p.zip", "path_kind": "kb", "inventory": report}])
    assert "待处理导入" in block and "没有把它导入成个人 skill" not in block
    assert "2 条阻断项" in block
