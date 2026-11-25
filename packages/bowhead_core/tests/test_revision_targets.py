"""Revision targets: a delivered file is revised as a file, never through Canvas report revision.

the original regression (1.7.18): the user asked to fix charts in a delivered HTML file. The
spawn schema only understood the Canvas report, so Lead chose canvas_mode=revise and passed
the HTML's chapter names as revise_scope; the server refused them against Canvas headings.
Design: docs/sprints/revision-target-2026-11-10/tech-design.md.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from bowhead_core.artifact_presentation import PresentationUnavailable
from bowhead_core.artifact_store import Artifact
from bowhead_core.canvas_store import CanvasStore
from bowhead_core.subagent import PROFILES, SubagentResult, register_subagent_tool
from bowhead_core.tool_registry import ToolExecutionContext, build_default_registry
from test_canvas_revise_b2 import _Bus, _seed_plan_report
from test_canvas_revise_b2 import store as store

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)


def _artifact(artifact_id: str, name: str, *, turn: str = "t1", producer: str | None = "python_sandbox", minutes: int = 30) -> Artifact:
    return Artifact(
        id=artifact_id,
        filename=name,
        mime="text/html" if name.endswith(".html") else "application/octet-stream",
        size=10,
        source="python_sandbox",
        path=f"/x/{artifact_id}",
        canvas_id=None,
        created_by="u",
        created_at=(NOW - timedelta(minutes=minutes)).isoformat(),
        kind="deliverable",
        producer_kind=producer,
        created_by_turn=turn,
    )


class _Files:
    """Session file membership as the artifact store reports it."""

    def __init__(self, *artifacts: Artifact, fail: bool = False) -> None:
        self.members = {artifact.id: artifact for artifact in artifacts}
        self.fail = fail

    async def list_for_session(self, *, session_id, user_id, artifact_id=None, limit=100, **_):
        if self.fail:
            raise OSError("store unavailable")
        found = self.members.get(artifact_id)
        return [found] if found else []

    async def list_session_deliverables(self, *, session_id, user_id, exclude_turn_id="", limit=12):
        if self.fail:
            raise OSError("store unavailable")
        return [artifact for artifact in self.members.values() if artifact.created_by_turn != exclude_turn_id][:limit]


@pytest.fixture
def files_enabled(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("FEATURE_REVISION_TARGETS", "1")
    monkeypatch.setenv("FEATURE_CANVAS_SECTION_REVISE", "1")

    async def available(ctx, artifact_id, *, produced=False):
        artifact = ctx.artifact_store.members.get(artifact_id)
        if artifact is None or artifact.producer_kind == "unavailable":
            raise PresentationUnavailable("artifact_presentation_reference_unavailable")
        return artifact

    monkeypatch.setattr("bowhead_core.artifact_presentation.available_artifact", available)


HTML = _artifact("art_html", "rbu_operating_report_2026_detailed.html")


async def _spawn(store: CanvasStore, bus: _Bus, args: dict, *, files: _Files, batch=None, ctx_out: list | None = None):
    registry = build_default_registry()
    register_subagent_tool(registry)
    captured: dict = {}

    class _Cap:
        async def run(self, **kwargs):
            captured.update(kwargs)
            payload = {"delivery_surface": "report", "exec_summary": "Charts fixed in the new HTML version."}
            return SubagentResult(profile="compute", subagent_id="sub_f", ok=True, final_answer="done", payload=payload)

    ctx = ToolExecutionContext(
        workspace_id="1", member_uid="m", user_id="u", turn_id="t2", datarc=None,
        subagent_runner=_Cap(), parent_bus=bus, session_id="s1",
    )  # fmt: skip
    ctx.canvas_store = store
    ctx.artifact_store = files
    ctx.user_text = "后半部分的图表没有正确生成，再优化一下"
    if batch is not None:
        ctx._lead_spawn_batch_seq = batch
    from bowhead_core.effective_language import pin_turn_output_language

    pin_turn_output_language(bus, "zh-CN")
    inv = await registry.execute("subagent_spawn", {"profile": "compute", "task": "修复后半部分图表", **args}, ctx)
    if ctx_out is not None:
        ctx_out.append(ctx)
    return inv.result, captured


@pytest.mark.parametrize("target", [["art_html"], " art_html "])
async def test_file_target_dispatch_skips_canvas_revision(store: CanvasStore, files_enabled, target: list[str] | str) -> None:
    """The the original regression shape: a Canvas report exists, names come from the HTML, and Lead asked for a report."""
    from bowhead_core.canvas_revise import lead_canvas_mode, revise_scope_missed_earlier
    from bowhead_core.delivered_files import CANVAS_WRITE_TOOLS

    await _seed_plan_report(store)
    bus = _Bus()
    args = {"revise_files": target, "revise_scope": ["后半部分图表", "大区与品牌拆解"], "canvas_mode": "revise", "delivery": "report"}
    contexts: list = []
    result, captured = await _spawn(store, bus, args, files=_Files(HTML), batch=1, ctx_out=contexts)
    assert result.get("ok") is True, result
    assert not getattr(contexts[0], "_m137_subagent_payloads", None), "A file revision's payload never joins report composition"
    assert captured["tool_deny"] == CANVAS_WRITE_TOOLS and "write_report" in captured["tool_deny"]
    assert captured["parent_ctx"].revision_target_files == ("art_html",)
    instructions = str(captured.get("extra_instructions") or "")
    assert "Revision target: delivered files" in instructions and "rbu_operating_report_2026_detailed.html" in instructions
    assert "后半部分图表; 大区与品牌拆解" in instructions
    assert "canvas_mode**: revise" not in instructions and "add_chart（会内联进聊天）" not in instructions
    assert lead_canvas_mode(bus) is None and bus._lead_delivery_spawn == "chat"
    assert not revise_scope_missed_earlier(bus, 2)
    # Regression check M2: Lead learns the Canvas report was not part of this dispatch.
    assert result["revised_files"] == {
        "targets": [{"artifact_id": "art_html", "name": HTML.filename}],
        "new_files": [],
        "canvas_mode_not_applied": "revise",
    }
    assert "new_files is empty" in result["directive"]
    assert "Do not tell the user the Canvas report is up to date" in result["directive"]
    # Revision-target design §9: the file is the report; nothing suggests syncing the Canvas report to it.
    assert "not kept in sync" in result["directive"] and "separate dispatch" not in result["directive"]
    assert bus._file_revision_dispatched is True, "The done branch reads this to retract a pointer to the unchanged Canvas"


async def test_empty_file_target_list_allows_chat_lookup(store: CanvasStore, files_enabled) -> None:
    from bowhead_core.canvas_revise import lead_canvas_mode
    from bowhead_core.delivered_files import CANVAS_WRITE_TOOLS

    await _seed_plan_report(store)
    bus = _Bus()
    result, captured = await _spawn(
        store,
        bus,
        {"revise_files": [], "delivery": "chat", "task": "Read the existing sales file and answer with monthly sales."},
        files=_Files(HTML),
        batch=1,
    )

    assert result["ok"] is True and "revised_files" not in result
    assert lead_canvas_mode(bus) is None
    assert captured["tool_deny"] == CANVAS_WRITE_TOOLS


@pytest.mark.parametrize(
    ("files", "ids", "reason", "kind"),
    [
        (_Files(HTML), ["art_other"], "revise_files_not_found", "invalid_args"),
        (_Files(HTML), "art_other", "revise_files_not_found", "invalid_args"),
        (
            _Files(HTML, _artifact("art_doc", "brief.docx", producer="template_document_v1")),
            ["art_doc"],
            "revise_files_document_flow",
            "invalid_args",
        ),
        (_Files(HTML), [], "revise_files_invalid", "invalid_args"),
        (_Files(HTML, fail=True), ["art_html"], "revise_files_unavailable", "unavailable"),
    ],
)
async def test_unusable_files_are_refused_before_dispatch(store: CanvasStore, files_enabled, files, ids, reason, kind) -> None:
    from bowhead_core.canvas_revise import lead_canvas_mode, revise_scope_missed_earlier

    await _seed_plan_report(store)
    bus = _Bus()
    result, captured = await _spawn(store, bus, {"revise_files": ids, "canvas_mode": "revise"}, files=files, batch=1)
    assert (result["reason_code"], result["error_kind"], result["path"]) == (reason, kind, "revise_files")
    assert not captured and lead_canvas_mode(bus) is None and not revise_scope_missed_earlier(bus, 2)
    assert not getattr(bus, "_file_revision_dispatched", False), "A refused file target revised nothing"
    if reason == "revise_files_not_found":
        assert "art_html (rbu_operating_report_2026_detailed.html)" in result["hint"]


async def test_profile_without_a_file_producer_is_refused(store: CanvasStore, files_enabled, monkeypatch: pytest.MonkeyPatch) -> None:
    from dataclasses import replace

    name = "reader-only"
    monkeypatch.setitem(PROFILES, name, replace(PROFILES["compute"], name=name, tool_whitelist=("read_report", "subagent_return")))
    registry = build_default_registry()
    register_subagent_tool(registry)
    ctx = ToolExecutionContext(workspace_id="1", member_uid="m", user_id="u", turn_id="t2", datarc=None, session_id="s1")
    ctx.subagent_runner, ctx.parent_bus, ctx.canvas_store, ctx.artifact_store = SimpleNamespace(), _Bus(), store, _Files(HTML)
    result = (await registry.execute("subagent_spawn", {"profile": name, "task": "fix", "revise_files": ["art_html"]}, ctx)).result
    assert result["reason_code"] == "revise_files_invalid" and "cannot write a new file version" in result["error"]


async def test_switch_off_keeps_canvas_revision(store: CanvasStore, files_enabled, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FEATURE_REVISION_TARGETS", "0")
    await _seed_plan_report(store)
    result, captured = await _spawn(
        store, _Bus(), {"revise_files": ["art_html"], "revise_scope": ["后半部分图表"], "canvas_mode": "revise"}, files=_Files(HTML), batch=1
    )
    assert result["reason_code"] == "revise_scope_unresolved" and not captured


async def test_unresolved_scope_offers_delivered_files_and_a_later_file_dispatch_runs(store: CanvasStore, files_enabled) -> None:
    """The the original regression shape: the named part is in a delivered HTML file, not in the Canvas report.

    The refusal lists the file and the question offers it. Since 2026-11-10 a later step may dispatch the file revision
    in the same Turn; the scope miss no longer locks the Turn (docs/sprints/revision-scope-retry-2026-11-10/tech-design.md).
    """
    await _seed_plan_report(store)
    bus = _Bus()
    files = _Files(HTML)
    first, _ = await _spawn(store, bus, {"revise_scope": ["后半部分图表"], "canvas_mode": "revise"}, files=files, batch=1)
    assert first["reason_code"] == "revise_scope_unresolved"
    assert "art_html (rbu_operating_report_2026_detailed.html)" in first["hint"] and "dispatch with revise_files" in first["hint"]
    assert "也可能是之前交付的文件：rbu_operating_report_20…" in first["suggested_question"]["question"]
    later, captured = await _spawn(store, bus, {"revise_files": ["art_html"], "revise_scope": ["后半部分图表"]}, files=files, batch=2)
    assert later.get("ok") is True and captured["parent_ctx"].revision_target_files == ("art_html",)
    assert "scope_after_miss" not in later
    assert "canvas_mode_not_applied" not in later["revised_files"], "No Canvas mode was asked for, so nothing was dropped"


def test_switch_defaults_on_and_zero_disables(monkeypatch: pytest.MonkeyPatch) -> None:
    from bowhead_core.delivered_files import revision_targets_enabled

    monkeypatch.delenv("FEATURE_REVISION_TARGETS", raising=False)
    assert revision_targets_enabled()
    monkeypatch.setenv("FEATURE_REVISION_TARGETS", "0")
    assert not revision_targets_enabled()


def test_lead_spawn_delivery_counts_file_targets_as_chat(monkeypatch: pytest.MonkeyPatch) -> None:
    from bowhead_core.agents.lead_orchestrator import _lead_spawn_delivery

    file_spawn = SimpleNamespace(tool_name="subagent_spawn", arguments={"revise_files": ["art_html"], "delivery": "report"})
    canvas_spawn = SimpleNamespace(tool_name="subagent_spawn", arguments={"canvas_mode": "revise", "delivery": "report"})
    monkeypatch.setenv("FEATURE_REVISION_TARGETS", "1")
    assert _lead_spawn_delivery([file_spawn]) == "chat"
    assert _lead_spawn_delivery([file_spawn, canvas_spawn]) == "report"
    monkeypatch.setenv("FEATURE_REVISION_TARGETS", "0")
    assert _lead_spawn_delivery([file_spawn]) == "report"


def test_new_versions_come_from_this_dispatch_declarations() -> None:
    from bowhead_core.delivered_files import FileTarget, revised_files_facts

    target = FileTarget(artifact_id="art_html", name="report.html", type="html")
    declared = SimpleNamespace(
        tool_name="python_exec",
        error=None,
        arguments={"purpose": "deliverable", "deliverable_outputs": ["report.html"]},
        result={
            "output_artifacts": [
                {"artifact_id": "art_new", "name": "report.html"},
                {"artifact_id": "art_html", "name": "report.html"},
                {"artifact_id": "art_out", "name": "stdout.txt", "data_role": "stdout"},
            ]
        },
        nested=[],
    )
    analysis = SimpleNamespace(
        tool_name="python_exec",
        error=None,
        arguments={"purpose": "analysis"},
        result={"output_artifacts": [{"artifact_id": "art_csv", "name": "x.csv"}]},
    )
    facts = revised_files_facts([target], [analysis, declared])
    assert facts["new_files"] == [{"artifact_id": "art_new", "name": "report.html", "recorded_as_version": False}]


def test_inventory_names_a_checkpoint_only_when_unique() -> None:
    from bowhead_core.delivered_files import render_delivered_files_block

    files = [_artifact("art_a", "a.html", turn="t1"), _artifact("art_b", "b.xlsx", turn="t3", minutes=180)]
    checkpoints = [
        SimpleNamespace(id="rsc_1", turn_id="t1"),
        SimpleNamespace(id="rsc_3a", turn_id="t3"),
        SimpleNamespace(id="rsc_3b", turn_id="t3"),
    ]
    block = render_delivered_files_block(files, checkpoints=checkpoints, now=NOW)
    assert 'artifact_id=art_a name="a.html" type=html 30 min ago checkpoint_id=rsc_1' in block
    assert 'artifact_id=art_b name="b.xlsx" type=xlsx 3 h ago\n' in block
    assert "revise_files=[<artifact_id>]" in block
    # Regression check M2: a delivered file is a report of its own, revised as a file, not synced with the Canvas report.
    assert "report in its own right, not a copy of the Canvas report" in block
    assert "use revise_files, not canvas_mode" in block
    # Regression check N3: a file the user asked for is the report before its first edit, even beside a Canvas report.
    assert "asked for the report as such a file" in block and "even if a Canvas report was made too" in block
    assert render_delivered_files_block([]) == ""


def test_a_cut_list_says_how_many_files_exist_and_names_the_lookup() -> None:
    from bowhead_core.delivered_files import refusal, render_delivered_files_block

    files = [_artifact(f"art_{number}", f"store-{number}.html") for number in range(12)]
    cut = render_delivered_files_block(files, now=NOW, total=20)
    notice = "Showing the newest 12 of 20 delivered files. To find another, call find_results with part of its name."
    assert cut.splitlines()[-2:] == [notice, "</delivered_files>"]
    # A list that shows everything, or whose total is unknown, adds nothing.
    for total in (12, None):
        assert "find_results" not in render_delivered_files_block(files, now=NOW, total=total)

    # A refusal lists the same newest files; it says so, and where an older file's identifier comes from.
    refused = refusal("revise_files_not_found", ids=["art_missing"], available=files[:2])
    assert "Newest files delivered in this session: art_0 (store-0.html); art_1 (store-1.html)." in refused["hint"]
    assert "call find_results with part of its name to get its artifact_id" in refused["hint"]
    assert (refused["reason_code"], refused["path"], refused["error_kind"]) == ("revise_files_not_found", "revise_files", "invalid_args")
    # A newer version is named outright, so that refusal needs no lookup.
    assert "find_results" not in refusal("revise_files_not_latest", ids=["art_0"], latest="art_9")["hint"]


async def test_lead_counts_the_delivered_files_only_when_the_list_is_full(files_enabled) -> None:
    from bowhead_core.agents.lead_orchestrator import _add_delivered_files_block

    class Store:
        def __init__(self, listed: int, total: int) -> None:
            self.files = [_artifact(f"art_{number}", f"store-{number}.html") for number in range(listed)]
            self.total, self.counted = total, 0

        async def list_session_deliverables(self, **_scope):
            return self.files

        async def count_session_deliverables(self, *, session_id, user_id, exclude_turn_id):
            assert (session_id, user_id, exclude_turn_id) == ("s1", "u", "t9"), "the total uses the list's own scope"
            self.counted += 1
            return self.total

    async def block_for(store: Store) -> str:
        blocks: list[str] = []
        ctx = SimpleNamespace(artifact_store=store, session_id="s1", user_id="u")
        await _add_delivered_files_block(ctx, "t9", lambda block, **_more: blocks.append(block), checkpoints=())
        return blocks[0]

    full = Store(listed=12, total=20)
    assert "Showing the newest 12 of 20 delivered files." in await block_for(full)
    assert full.counted == 1
    # A list below its limit shows everything, so nothing is counted and nothing is added.
    short = Store(listed=3, total=3)
    assert "find_results" not in await block_for(short)
    assert short.counted == 0


def test_spawn_schema_and_history_hint_treat_a_requested_file_as_the_report(monkeypatch) -> None:
    """Regression check N3: the first follow-up to a requested HTML file revises the file, not the Canvas report."""
    from bowhead_core.cross_turn_digest import render_history_rules_block
    from bowhead_core.subagent import register_subagent_tool
    from bowhead_core.tool_registry import ToolRegistry

    monkeypatch.setenv("FEATURE_REVISION_TARGETS", "1")
    monkeypatch.setenv("FEATURE_CANVAS_SECTION_REVISE", "1")
    registry = ToolRegistry()
    register_subagent_tool(registry)
    properties = registry.get("subagent_spawn").schema.input_schema["properties"]
    assert "When the user asked for the report as such a file" in properties["revise_files"]["description"]
    assert "用户当初要的就是这个文件" in properties["canvas_mode"]["description"]
    rules = render_history_rules_block([], force=True, section_revise=True)
    assert "also when the user first asked for the report as that file" in rules


def test_scope_question_offers_files_as_options_or_lists_them() -> None:
    from bowhead_core.canvas_revise import ScopeUnit, revise_scope_question

    def unit(title: str) -> ScopeUnit:
        return ScopeUnit(title=title, section_ids=frozenset({title}))

    small = revise_scope_question([unit("月度趋势")], ["后半部分图表"], files=["report.html"])
    assert small["type"] == "single_select" and small["options"] == ["月度趋势", "文件：report.html", "都不是，新加一章"]
    large = revise_scope_question([unit(f"第{i}节") for i in range(5)], ["后半部分图表"], files=["report.html"])
    assert large["type"] == "free_text" and "也可能是之前交付的文件：report.html。" in large["question"]
    assert revise_scope_question([unit("月度趋势")], ["x"]) == revise_scope_question([unit("月度趋势")], ["x"], files=[])


@pytest.fixture
async def artifacts(tmp_path):
    import aiosqlite
    from bowhead_core.artifact_store import ArtifactStore, LocalBackend

    artifact_store = ArtifactStore(db_path=str(tmp_path / "art.db"), backend=LocalBackend(root_dir=tmp_path / "blob"))
    await artifact_store.initialize()
    async with aiosqlite.connect(artifact_store.db_path) as db:
        # Minimal shapes of tables owned by other stores (turns: session_store; purge claims: migration 0046).
        await db.executescript(
            "CREATE TABLE IF NOT EXISTS turns (id TEXT PRIMARY KEY, session_id TEXT);"
            "CREATE TABLE IF NOT EXISTS artifact_purge_claims (artifact_id TEXT PRIMARY KEY);"
        )
        await db.executemany("INSERT INTO turns(id, session_id) VALUES (?, ?)", [("t1", "s1"), ("t2", "s1"), ("t9", "s_other")])
        await db.commit()
    return artifact_store


async def test_session_deliverables_lists_only_revisable_files_of_earlier_turns(artifacts) -> None:
    import aiosqlite
    from bowhead_core.results_artifact_policy import SANDBOX_STDOUT_PRODUCER

    async def put(name: str, *, turn: str = "t1", user: str = "u", kind: str = "deliverable", producer: str | None = None):
        return await artifacts.put_bytes(
            content=b"x", filename=name, mime="text/html", created_by=user, source="python_sandbox",
            kind=kind, producer_kind=producer, created_by_turn=turn,
        )  # fmt: skip

    older = await put("older.html")
    newest = await put("report.xlsx")
    await put("rows.csv", kind="tool_data")
    await put("brief.docx", producer="template_document_v1")
    await put("stdout.txt", producer=SANDBOX_STDOUT_PRODUCER)
    await put("this-turn.html", turn="t2")
    await put("other-user.html", user="v")
    await put("other-session.html", turn="t9")
    purged = await put("purged.html")
    async with aiosqlite.connect(artifacts.db_path) as db:
        await db.execute("INSERT INTO artifact_purge_claims(artifact_id) VALUES (?)", (purged.id,))
        await db.commit()
    listed = await artifacts.list_session_deliverables(session_id="s1", user_id="u", exclude_turn_id="t2")
    assert [artifact.id for artifact in listed] == [newest.id, older.id]
    # The total a cut list states is counted with the list's own rule, exclusions included.
    assert await artifacts.count_session_deliverables(session_id="s1", user_id="u", exclude_turn_id="t2") == 2
    assert await artifacts.count_session_deliverables(session_id="s1", user_id="u") == 3
    assert await artifacts.count_session_deliverables(session_id="s_other", user_id="u") == 1
    assert [artifact.id for artifact in await artifacts.list_session_deliverables(session_id="s1", user_id="u", limit=1)] == [
        (await artifacts.list_session_deliverables(session_id="s1", user_id="u"))[0].id
    ]
