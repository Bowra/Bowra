"""导入出处（B 线批 4 / 跨线 ⑨）：迁移 0064 + 服务端现算 + 审批面投影。

钉的是三件事：只有 overridden 才带痕迹；**报告读不出来时计数是 None 不是 0**（否则审批面会说
「作者明知有 0 条阻断项仍然启用」——既自相矛盾又替作者开脱）；只带红项。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from bowhead_core.skill_import_provenance import build_import_provenance, public_import_provenance

from bowhead_core import migrations

pytestmark = pytest.mark.integration


def _upgrade(path: Path, target: str = "core") -> None:
    config = Config()
    config.set_main_option("script_location", str(Path(migrations.__file__).resolve().parent))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    config.attributes["bowhead_target"] = target
    command.upgrade(config, "head")


def _columns(path: Path, table: str) -> set[str]:
    con = sqlite3.connect(path)
    try:
        return {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
    finally:
        con.close()


def test_0064_adds_the_column_on_core_only_and_is_repeatable(tmp_path: Path) -> None:
    core = tmp_path / "core.db"
    _upgrade(core)
    assert "import_provenance_json" in _columns(core, "team_skill")
    _upgrade(core)  # Repeated execution is idempotent.
    assert "import_provenance_json" in _columns(core, "team_skill")
    kb = tmp_path / "files.db"
    _upgrade(kb, target="files")
    assert not _columns(kb, "team_skill")  # Other targets are no-ops.


_REPORT = {
    "grade": "red",
    "red_count": 2,
    "truncated": False,
    "findings_digest": "d1",
    "findings": [
        {
            "id": "f1",
            "code": "tool_ref_unresolved",
            "severity": "red",
            "path": "SKILL.md",
            "line": 4,
            "subject": "notion_search",
            "message_zh": "工具不存在",
        },
        {"id": "f2", "code": "script_import_unresolved", "severity": "red", "subject": "notion_client", "message_zh": "包不可用"},
        {"id": "f3", "code": "frontmatter_gap", "severity": "yellow", "subject": "trigger_examples", "message_zh": "缺示例"},
    ],
}


def test_only_overridden_carries_provenance() -> None:
    rep = json.dumps(_REPORT)
    assert build_import_provenance(import_status="overridden", import_report_json=rep)
    for status in ("cleared", "pending", None, "", "unknown"):
        assert build_import_provenance(import_status=status, import_report_json=rep) is None


def test_red_only_and_counts_come_from_the_report() -> None:
    data = json.loads(build_import_provenance(import_status="overridden", import_report_json=json.dumps(_REPORT)))
    assert data["status"] == "overridden" and data["red_count"] == 2 and data["truncated"] is False
    assert [f["id"] for f in data["findings"]] == ["f1", "f2"]  # Do not carry yellow findings.
    assert all(f["severity"] == "red" for f in data["findings"])
    assert data["findings"][0]["message_zh"] == "工具不存在"


@pytest.mark.parametrize(
    "report",
    [
        "not json",
        "",
        None,
        "[]",
        # ★ A **readable report with the wrong shape** must not yield 0 either (review P1); another workstream owns the report JSON,
        # so this is a defensive boundary. Strings/floats/bools/negative values/clean reports all mean the count is unknown.
        '{"grade":"green","findings":[]}',
        '{"red_count":0}',
        '{"red_count":"3"}',
        '{"red_count":2.0}',
        '{"red_count":-5}',
        '{"red_count":true}',
        '{"findings":[{"severity":"yellow"}]}',
    ],
)
def test_never_reports_zero_blocking_issues(report: str | None) -> None:
    """作者确实强开了，我们只是不知道几条——写 0 会渲染成「明知有 0 条仍然启用」，
    既自相矛盾又替作者开脱。任何数不出正数的输入都必须落 None。"""

    data = json.loads(build_import_provenance(import_status="overridden", import_report_json=report))
    assert data["status"] == "overridden"
    assert data["red_count"] is None, report
    assert not any(isinstance(v, int) and v == 0 for k, v in data.items() if k == "red_count")


def test_oversized_digest_still_fits_the_column_budget() -> None:
    """兜底分支的职责就是在输入耍赖时兜住——digest 不截断的话它兜不住（review P2）。"""

    huge = json.dumps({"red_count": 3, "findings_digest": "x" * 200_000, "findings": []})
    text = build_import_provenance(import_status="overridden", import_report_json=huge)
    assert len(text.encode("utf-8")) <= 64_000


def test_truncated_and_oversized_reports_stay_within_the_column_budget() -> None:
    many = {
        "red_count": 90,
        "truncated": True,
        "findings": [{"id": f"f{i}", "severity": "red", "code": "c", "message_zh": "x" * 400} for i in range(90)],
    }
    data = json.loads(build_import_provenance(import_status="overridden", import_report_json=json.dumps(many)))
    assert data["red_count"] == 90 and data["truncated"] is True
    assert len(data["findings"]) <= 20
    assert len(json.dumps(data, ensure_ascii=False).encode()) <= 64_000


def test_public_projection_drops_internal_digest_and_tolerates_garbage() -> None:
    pub = public_import_provenance(build_import_provenance(import_status="overridden", import_report_json=json.dumps(_REPORT)))
    assert set(pub) == {"status", "red_count", "truncated", "findings"} and "findings_digest" not in pub
    assert public_import_provenance(None) is None and public_import_provenance("not json") is None


async def test_writer_round_trip_and_tolerates_a_pre_0064_database(tmp_path: Path) -> None:
    """写入口的正对照＋缺列不炸（review P1-2）。

    读侧早有 `_row_opt` 容忍缺列，写侧没有 ⇒ 在 `initialize()` 建的库（所有既有测试都这么建）
    上裸 UPDATE 会 OperationalError，对方线接 promote 时会当场撞上。
    """

    from bowhead_core.user_store import UserStore

    # 1) A database created by initialize() lacks migration 0064's columns: writes return False without raising.
    legacy = UserStore(db_path=str(tmp_path / "legacy.db"))
    await legacy.initialize()
    assert await legacy.set_team_skill_import_provenance("t1", "sk1", '{"status":"overridden"}') is False

    # 2) A migrated database: values can be written and read back.
    migrated_path = tmp_path / "core.db"
    _upgrade(migrated_path)
    store = UserStore(db_path=str(migrated_path))
    con = sqlite3.connect(migrated_path)
    con.execute(
        "INSERT INTO team_skill (id, team_id, name, display_name, description, source_path, created_at, updated_at) "
        "VALUES ('sk1','t1','s','S','d','/tmp/s','2026-01-01','2026-01-01')",
    )
    con.commit()
    con.close()
    payload = build_import_provenance(import_status="overridden", import_report_json=json.dumps(_REPORT))
    assert await store.set_team_skill_import_provenance("t1", "sk1", payload) is True
    assert await store.set_team_skill_import_provenance("t1", "nope", payload) is False  # Nonexistent row.
    got = await store.get_team_skill("t1", "sk1")
    assert public_import_provenance(got.import_provenance_json)["red_count"] == 2


def test_review_projection_carries_provenance_and_member_view_does_not() -> None:
    """跨线接缝：skill-import 线保证 promote 时**写进库**（他们的 `test_promote_records_override_provenance…`），
    这里保证**读面按视角给**——两边各自的测试都盖不到这一段。

    审批人（admin ∨ 作者）看得到「作者明知有 N 条阻断项仍然启用」；普通成员连字段都拿不到：
    finding 的 `subject`/`message_zh` 会点名内部工具，而那是别人对自己资产的知情选择。
    """

    from bowhead_core.user_store import TeamSkill
    from bowhead_gateway.routes.team_skills import _public

    sk = TeamSkill(
        id="oskill_1",
        team_id="t1",
        name="ops-digest",
        display_name="周度运营简报",
        description="d",
        source_path="/tmp/x",
        created_by="u_author",
        created_at="2026-09-10T00:00:00+00:00",
        updated_at="2026-09-10T00:00:00+00:00",
        import_provenance_json=build_import_provenance(import_status="overridden", import_report_json=json.dumps(_REPORT)),
    )
    reviewer_view = _public(sk, include_review=True)
    assert reviewer_view["import_provenance"]["status"] == "overridden"
    assert reviewer_view["import_provenance"]["red_count"] == 2
    assert {f["code"] for f in reviewer_view["import_provenance"]["findings"]} == {"tool_ref_unresolved", "script_import_unresolved"}
    assert "findings_digest" not in reviewer_view["import_provenance"]  # Exclude internal digest keys from the projection.

    member_view = _public(sk)
    assert "import_provenance" not in member_view
    assert "notion_search" not in json.dumps(member_view, ensure_ascii=False)  # Leak no characters of tool names from blocking findings.

    # Skills never overridden: reviewers also receive null, not an empty object (the approval UI uses this to omit the badge).
    clean = TeamSkill(
        id="oskill_2",
        team_id="t1",
        name="clean",
        display_name="c",
        description="d",
        source_path="/tmp/y",
        created_at="2026-09-10T00:00:00+00:00",
        updated_at="2026-09-10T00:00:00+00:00",
    )
    assert _public(clean, include_review=True)["import_provenance"] is None
