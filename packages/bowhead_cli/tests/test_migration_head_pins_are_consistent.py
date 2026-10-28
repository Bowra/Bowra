"""所有写死 migration head 的地方必须和真正的 head 一致。

2026-08-14 加 revision 0018 时被这条坑到：head 常量在全库有十几处副本，名字还不统一
（`HEAD_REVISION` / `EXPECTED_HEAD` / `_HEAD`，以及直接内联字面量的）。按名字批量改只覆盖了
一部分，**内联那处（`test_composer_submission_store.py`）漏了**，只有跑整套后端才暴露；
更要紧的是 `bowhead_cli/composer_a_rollback_drill.py` 的 `_HEAD` 是**运维代码**，漏改会让
回滚演练报出过期的 head。

这条把「下一次加 revision 时又漏一处」变成立刻可见：判据是**真实 head**（从 revision 链
算出来），不是另一个手写常量——否则就又是同一判据的第 N 份副本。
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
VERSIONS_DIR = REPO_ROOT / "packages/bowhead_core/src/bowhead_core/migrations/versions"
#: These locations **intentionally** reference non-head revisions (the migration chain itself and tests starting from a specific revision).
EXEMPT = {
    "packages/bowhead_gateway/tests/test_alembic_capability_evidence_actual_model.py",
    "packages/bowhead_core/tests/test_agent_marketplace_migration.py",
    #: The scanner's **own** test, `test_parser_accepts_every_assignment_style_present_in_the_repo`,
    #: must use real revision IDs as fixtures (including the annotated assignment in `0081` and the multiple-parent tuple in `0085`);
    #: otherwise its positive control would cover only hypothetical forms. These are not head copies and cannot become stale.
    "packages/bowhead_cli/tests/test_migration_head_pins_are_consistent.py",
    #: Added on 2026-09-17: this guard had failed during parsing since `0081` landed (see the positive control at the end of this file),
    #: so it had never scanned the tests added since then for individual migrations.
    #: They pin the **upstream/downstream revisions of their own migration** (`_OLD`/`_NEW`, `_upgrade(..., "0074_x", ...)`,
    #: the two parents of a merge revision, or the `0001_initial` baseline); **none pins head**.
    #: ⇒ They fit the existing exemption category of tests starting from a specific revision and cannot become stale when revisions are added.
    #: ⚠ Exempting an entire file would also allow a future head copy in that file —
    #: `test_nobody_pins_the_current_head_even_in_exempt_files` guards against that separately.
    "packages/bowhead_core/tests/test_reference_primary_execution_migration.py",
    "packages/bowhead_core/tests/test_skill_reference_claim.py",
    "packages/bowhead_core/tests/test_reference_runtime_migration.py",
    "packages/bowhead_core/tests/test_agent_reference_merge_migration.py",
    "packages/bowhead_core/tests/test_agent_version_operations_migration.py",
    # Historical upgrades deliberately target specific revisions, not the head.
    "packages/bowhead_core/tests/test_reference_prompt_http.py",
    "packages/bowhead_gateway/tests/test_alembic_subagent_drafts.py",
    # Per-revision upgrade tests: each imports or stamps its own migration's revision and parent.
    "packages/bowhead_core/tests/test_ordinary_multi_helper.py",
    "packages/bowhead_gateway/tests/test_alembic_model_runtime_parameters.py",
    # Upgrades from the parent of `0179_account_deletion` to seed rows that predate account deletion.
    "packages/bowhead_core/tests/test_account_deletion.py",
}
#: Assignment styles for `revision` / `down_revision` **vary** across the repository; a regex accepting only one makes this guard unreliable:
#:   · `revision = "0096_x"` (most files)
#:   · `revision: str = '0081_x'` (type annotation + single quotes, 6 files)
#:   · `down_revision = ("0084_a", "0084_b")` (merge revision, multiple parents)
#: Before 2026-09-17, only the first style was accepted ⇒ all 4 cases in this file had **failed continuously** since `0081` landed,
#: despite being the gate that detects stale head copies after a new revision is added — **an unreachable criterion does not mean a pass**.
#: ⚠ The tuple form is especially critical: missing it treats a merge revision as parentless and omits both parent revisions
#: from `parents`, potentially producing a **wrong** answer for "exactly one head" rather than crashing, which is more dangerous.
_ASSIGN_TEMPLATE = r"^{name}(?:\s*:\s*[^=\n]+)?\s*=\s*(.+)$"
_REVISION_ASSIGN = re.compile(_ASSIGN_TEMPLATE.format(name="revision"), re.M)
_DOWN_ASSIGN = re.compile(_ASSIGN_TEMPLATE.format(name="down_revision"), re.M)
_QUOTED = re.compile(r"[\"']([^\"']+)[\"']")
#: ⚠ This regex **intentionally accepts only double quotes**; do not widen it to single quotes — the two regex groups have opposite requirements:
#: the group above parses assignments **in migration files themselves** (single-quoted assignments exist and must be accepted);
#: this regex finds copies of the head constant as Python literals. Most single-quoted `0019_x` values occur
#: **inside SQL strings** (`UPDATE alembic_version SET version_num='0019_x'`,
#: `INSERT INTO alembic_version VALUES ('0028_x')`), where test fixtures set a starting revision,
#: rather than copying head. Widening it would turn these into false positives (observed on 2026-09-17: 3 additional files).
REVISION_LITERAL = re.compile(r"\"(\d{4}_[a-z0-9_]+)\"")


def _revision_of(source: str) -> str | None:
    """取 revision id；三种写法都认（见上）。取不到返回 None 交调用方 assert。"""
    assignment = _REVISION_ASSIGN.search(source)
    if assignment is None:
        return None
    quoted = _QUOTED.search(assignment.group(1))
    return quoted.group(1) if quoted else None


def _down_revisions_of(source: str) -> tuple[str, ...]:
    """父 revision，**可能有多个**（merge revision 是元组）；`None` / 缺失返回空元组。"""
    assignment = _DOWN_ASSIGN.search(source)
    if assignment is None:
        return ()
    return tuple(_QUOTED.findall(assignment.group(1)))


def _actual_head() -> str:
    """head = 没有被任何人当作 down_revision 的那个 revision。"""
    revisions: dict[str, tuple[str, ...]] = {}
    for path in VERSIONS_DIR.glob("[0-9]*.py"):
        source = path.read_text(encoding="utf-8")
        revision = _revision_of(source)
        assert revision, f"{path.name} 缺 revision"
        revisions[revision] = _down_revisions_of(source)

    parents = {parent for downs in revisions.values() for parent in downs}
    heads = sorted(set(revisions) - parents)
    assert len(heads) == 1, f"revision 链应有且只有一个 head，实际: {heads}"
    return heads[0]


def _pinned_head_literals(*, include_exempt: bool = False) -> dict[str, set[str]]:
    """全库里出现的 revision 字面量（总是跳过迁移文件自身；豁免项默认也跳过）。

    ``include_exempt=True`` 用于「连豁免文件也不许抄 head」那条更窄的不变式。
    """
    known_revisions = {revision for path in VERSIONS_DIR.glob("[0-9]*.py") if (revision := _revision_of(path.read_text(encoding="utf-8")))}
    found: dict[str, set[str]] = {}
    for path in (REPO_ROOT / "packages").rglob("*.py"):
        rel = path.relative_to(REPO_ROOT).as_posix()
        if "/migrations/versions/" in rel or (not include_exempt and rel in EXEMPT):
            continue
        hits = set(REVISION_LITERAL.findall(path.read_text(encoding="utf-8"))) & known_revisions
        if hits:
            found[rel] = hits
    return found


def test_revision_chain_has_exactly_one_head() -> None:
    # Positive control: the scanner actually read the revision chain rather than scanning an empty directory.
    assert len(list(VERSIONS_DIR.glob("[0-9]*.py"))) > 10
    assert re.fullmatch(r"\d{4}_[a-z0-9_]+", _actual_head())


def test_revision_ids_match_image_filenames() -> None:
    """Deployment admission resolves the current DB revision in the candidate image by filename."""
    for path in VERSIONS_DIR.glob("[0-9]*.py"):
        revision = _revision_of(path.read_text(encoding="utf-8"))
        assert revision, f"{path.name} 缺 revision"
        assert path.stem == revision, f"{path.name} 必须与 revision id 一致，否则候选镜像无法验证当前数据库 revision"


def test_no_file_pins_a_head_revision_literal() -> None:
    """★ 2026-09-02 升级：从「副本必须不过期」改成「**不许再有副本**」。

    原判据是「每处写死的 head 都要等于真 head」——它把**抄十几份**当既成事实，
    只保证抄错会被发现。加 revision `0029` 时这条如期报警，一次点出 17 处
    （14 个测试 + 运维代码 `composer_a_rollback_drill._HEAD` + 2 处内联），
    于是索性把根因消掉：全部改读 `bowhead_core.migrations.chain_head()`。

    副本归零之后，原判据变成**恒真**（没有副本就没有过期的副本）⇒ 必须换成
    更强的不变式，否则这条守卫从此哑火
    （[[feedback_assert_absence_needs_positive_control]]）。
    """
    pinned = _pinned_head_literals()
    assert pinned == {}, (
        "这些位置又把 migration head 抄了一份。别写字面量，改读 "
        "`bowhead_core.migrations.chain_head()`——写死的副本会在下一条 revision 悄悄过期，"
        f"运维代码里的那份还会让回滚演练报出错的 head：{pinned}"
    )


def test_the_scanner_is_not_dead() -> None:
    """positive control：上面那条断言的是「找不到」，所以必须证明扫描器**能找到**。

    副本清零后，全库扫描恒返 `{}`——正则失配、路径写错、遍历到空目录，
    结果长得**一模一样**。所以拿一份**确定含 revision 字面量**的文件（迁移链自身，
    它被主扫描按设计跳过）过同一条正则，证明识别逻辑还活着。
    """
    known = {revision for path in VERSIONS_DIR.glob("[0-9]*.py") if (revision := _revision_of(path.read_text(encoding="utf-8")))}
    assert len(known) > 10, "revision 链都没读到，扫描器的输入就是坏的"

    sample = VERSIONS_DIR / f"{_actual_head()}.py"
    hits = set(REVISION_LITERAL.findall(sample.read_text(encoding="utf-8"))) & known
    assert hits, f"同一条正则在 {sample.name} 上一个 revision 字面量都没抓到——正则失配了"

    # Exempt entries must exist and contain literals; otherwise EXEMPT would exempt a file that has disappeared.
    for rel in EXEMPT:
        path = REPO_ROOT / rel
        assert path.is_file(), f"EXEMPT 里的 {rel} 不存在了，该清理"


def test_parser_accepts_every_assignment_style_present_in_the_repo() -> None:
    """★ 2026-09-17：本文件 4 条用例自 `0081` 落地起**一直全红**，因为解析正则只认
    `revision = "..."`，而 `0081` 写的是 `revision: str = '...'`。

    这条把「解析器认得全」变成可证伪的断言。没有它，下一个人把正则收窄回去时，
    守卫不会崩、只会**看不全**——`_actual_head()` 会算出一个错的 head 而无人察觉，
    那比现在直接崩更危险（[[feedback_gate_that_cannot_parse_must_not_pass]]）。
    """
    cases = {
        'revision = "0096_x"\ndown_revision = "0095_y"\n': ("0096_x", ("0095_y",)),
        "revision: str = '0081_x'\ndown_revision = '0080_y'\n": ("0081_x", ("0080_y",)),
        'revision = "0085_x"\ndown_revision = ("0084_a", "0084_b")\n': ("0085_x", ("0084_a", "0084_b")),
        'revision = "0001_initial"\ndown_revision = None\n': ("0001_initial", ()),
    }
    for source, (revision, downs) in cases.items():
        assert _revision_of(source) == revision, source
        assert _down_revisions_of(source) == downs, source

    # Positive control: all three assignment styles above exist **in the repository**; they are not hypothetical forms.
    annotated = (VERSIONS_DIR / "0081_skill_read_extensions.py").read_text(encoding="utf-8")
    assert _revision_of(annotated) == "0081_skill_read_extensions", "带类型注解 + 单引号的写法没被认出来"
    merged = (VERSIONS_DIR / "0085_merge_slack_reference.py").read_text(encoding="utf-8")
    assert _down_revisions_of(merged) == (
        "0084_slack_channel",
        "0084_reference_auto_receiver",
    ), "merge revision 的元组多父没被认全——会被当成孤儿节点，head 就算错了"


def test_nobody_pins_the_current_head_even_in_exempt_files() -> None:
    """EXEMPT 的含义是「允许引用**别的** revision」，**不是**「允许抄 head」。

    整文件豁免会顺带让豁免项里的 head 副本也隐身，而 head 副本正是本守卫要防的那一种
    真实故障：下一条 revision 一落地它就悄悄过期。所以这条**不看豁免**，只问一件事——
    有没有人把**当前 head** 写成了字面量。
    """
    head = _actual_head()
    offenders = {rel: hits for rel, hits in _pinned_head_literals(include_exempt=True).items() if head in hits}
    assert offenders == {}, (
        f"这些位置把当前 head `{head}` 抄成了字面量，下一条 revision 落地即过期，改读 `bowhead_core.migrations.chain_head()`：{offenders}"
    )
