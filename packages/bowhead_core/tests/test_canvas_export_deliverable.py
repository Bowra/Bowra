"""said-not-done WP-0 —— canvas 导出 skill 的产物登记。

断点（2026-08-05 实测 116 prod）：`canvas-pptx` / `canvas-pdf` / `canvas-xlsx` 三个
专门做导出的 skill 脚本全叫 `render_from_canvas.py`，而全仓唯一从 skill 收产物的路径
只认 `render_deck.py` + `.pptx` + `profile.deck_inputs`(仅 consultant) ⇒ 这三个 skill
跑成功后文件写进 workdir 然后蒸发，永远不会变成可下载 artifact。

真实后果（prod t_ 两轮，2026-07-23）：用户说「把画布上的这些内容导出成一个PDF发给我」，
`skill_invoke` 返回 ok，答案写「PDF 已经导出好了！…可以直接下载使用」，
而该 turn 名下 **0 个 artifact** —— 用户什么都拿不到。

⚠ 反向闸（[[feedback_synthetic_click_hides_event_bugs]] 同理）：
`test_prod_shape_pdf_export_is_registered` 是本文件的核心。把
`_collect_canvas_export_deliverables` 摘掉或改回只认 `render_deck.py`，它**必须变红**。
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from bowhead_core.artifact_store import ArtifactStore, LocalBackend
from bowhead_core.subagent import _collect_canvas_export_deliverables


@pytest.fixture
async def store() -> ArtifactStore:
    tmp = Path(tempfile.mkdtemp())
    backend = LocalBackend(root_dir=tmp / "files")
    s = ArtifactStore(backend=backend, db_path=tmp / "test.db")
    await s.initialize()
    return s


@pytest.fixture
def workdir() -> Path:
    return Path(tempfile.mkdtemp())


def _inv(
    script: str,
    outputs: list[str],
    *,
    ok: bool = True,
    tool: str = "skill_invoke",
    name: str = "canvas-pdf",
    workdir: str | None = None,
):
    """一次 skill_invoke 的形状（真实字段：tool_name / arguments / result / workdir）。"""
    wd = workdir if workdir is not None else (str(Path(outputs[0]).parent) if outputs else None)
    return SimpleNamespace(
        tool_name=tool,
        arguments={"name": name, "script": script},
        result={"ok": ok, "outputs": outputs, "workdir": wd},
    )


def _write(workdir: Path, name: str) -> str:
    p = workdir / name
    p.write_bytes(b"%PDF-1.4 fake bytes")
    return str(p)


async def _collect(store, invs, *, user_id="u_1", turn_id="t_1"):
    return await _collect_canvas_export_deliverables(
        invs,
        store,
        session_id="s_1",
        user_id=user_id,
        turn_id=turn_id,
    )


# ── ★ Core: reproduce the production shape ────────────────────────────


async def test_prod_shape_pdf_export_is_registered(store: ArtifactStore, workdir: Path) -> None:
    """07-23 那两轮的形状：render_from_canvas.py 成功产出 output.pdf。

    修复前：0 artifact（答案却说"PDF 已经导出好了"）。
    修复后：一个 kind=deliverable 的 artifact，且被 list_deliverables_for_turn 捞到
    —— 后者正是下载 chip 的数据源。
    """
    out = _write(workdir, "output.pdf")
    delivs = await _collect(store, [_inv("render_from_canvas.py", [out])])

    assert len(delivs) == 1
    assert delivs[0]["kind"] == "pdf"
    assert delivs[0]["artifact_id"].startswith("art_")

    # End-to-end: download chips' data source must retrieve it.
    rows = await store.list_deliverables_for_turn("t_1")
    assert [a.id for a in rows] == [delivs[0]["artifact_id"]]
    assert rows[0].kind == "deliverable"
    assert rows[0].mime == "application/pdf"


async def test_all_four_export_formats_registered(store: ArtifactStore, workdir: Path) -> None:
    invs = [
        _inv("render_from_canvas.py", [_write(workdir, "output.pptx")], name="canvas-pptx"),
        _inv("render_from_canvas.py", [_write(workdir, "output.pdf")], name="canvas-pdf"),
        _inv("render_from_canvas.py", [_write(workdir, "output.xlsx")], name="canvas-xlsx"),
        _inv("render_from_canvas.py", [_write(workdir, "output.docx")], name="canvas-docx"),
    ]
    delivs = await _collect(store, invs)
    assert {d["kind"] for d in delivs} == {"pptx", "pdf", "xlsx", "docx"}
    assert len(await store.list_deliverables_for_turn("t_1")) == 4


async def test_docx_registered_with_correct_mime(store: ArtifactStore, workdir: Path) -> None:
    """docx 走的是与 pdf 同一条登记路径（含 SEC-5 三闸），mime 必须对。

    mime 错的话前端 chip 图标与下载 Content-Type 都会歪。
    """
    out = _write(workdir, "output.docx")
    delivs = await _collect(store, [_inv("render_from_canvas.py", [out], name="canvas-docx")])
    assert [d["kind"] for d in delivs] == ["docx"]
    rows = await store.list_deliverables_for_turn("t_1")
    assert rows[0].mime == "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


async def test_docx_output_outside_workdir_is_rejected(store: ArtifactStore, workdir: Path, tmp_path: Path) -> None:
    """SEC-5 ② 对新格式同样生效 —— 闸是按 skill 名 + workdir 判的，与格式无关。"""
    outside = tmp_path / "evil.docx"
    outside.write_bytes(b"x")
    delivs = await _collect(
        store,
        [_inv("render_from_canvas.py", [str(outside)], name="canvas-docx", workdir=str(workdir))],
    )
    assert delivs == []
    assert await store.list_deliverables_for_turn("t_1") == []


# ── No overlap with consultant deck collection (avoids double registration/interference) ───


async def test_render_deck_script_not_collected_here(store: ArtifactStore, workdir: Path) -> None:
    """`render_deck.py` 是 consultant deck 那条路的**权威**收集对象。

    本 helper 必须不碰它，否则同一个 .pptx 会被两处各登记一次（用户看到两个重复下载）。
    """
    out = _write(workdir, "output.pptx")
    delivs = await _collect(store, [_inv("render_deck.py", [out])])
    assert delivs == []
    assert await store.list_deliverables_for_turn("t_1") == []


# ── Security: SEC-4 must reject registration without an owner ───────────


async def test_no_owner_refuses_to_register(store: ArtifactStore, workdir: Path) -> None:
    """created_by=NULL 会绕过下载归属校验 → world-readable。宁可没有文件。"""
    out = _write(workdir, "output.pdf")
    delivs = await _collect(store, [_inv("render_from_canvas.py", [out])], user_id=None)
    assert delivs == []
    assert await store.list_deliverables_for_turn("t_1") == []


async def test_no_artifact_store_is_noop(workdir: Path) -> None:
    out = _write(workdir, "output.pdf")
    assert (
        await _collect_canvas_export_deliverables(
            [_inv("render_from_canvas.py", [out])],
            None,
            session_id="s_1",
            user_id="u_1",
            turn_id="t_1",
        )
        == []
    )


# ── Do not collect ineligible outputs ─────────────────────────────────


async def test_failed_skill_invoke_not_registered(store: ArtifactStore, workdir: Path) -> None:
    """skill 报错时产物不可信（prod 里大量 `tool args parse failed` 的轮次）。"""
    out = _write(workdir, "output.pdf")
    assert await _collect(store, [_inv("render_from_canvas.py", [out], ok=False)]) == []


async def test_non_export_extension_ignored(store: ArtifactStore, workdir: Path) -> None:
    out = _write(workdir, "result.txt")
    assert await _collect(store, [_inv("render_from_canvas.py", [out])]) == []


async def test_missing_file_ignored(store: ArtifactStore) -> None:
    """outputs 里报了路径但文件不在（渲染中途失败）——不能凭 LLM/结果自报就登记。"""
    assert await _collect(store, [_inv("render_from_canvas.py", ["/nope/output.pdf"])]) == []


async def test_other_tool_ignored(store: ArtifactStore, workdir: Path) -> None:
    out = _write(workdir, "output.pdf")
    inv = _inv("render_from_canvas.py", [out], tool="python_sandbox")
    assert await _collect(store, [inv]) == []


# ── Retry semantics: last output per extension wins; no duplicate user files ───


async def test_retry_same_format_last_wins(store: ArtifactStore, workdir: Path) -> None:
    first = _write(workdir, "output.pdf")
    second_dir = Path(tempfile.mkdtemp())
    second = _write(second_dir, "output.pdf")
    delivs = await _collect(
        store,
        [
            _inv("render_from_canvas.py", [first]),
            _inv("render_from_canvas.py", [second]),
        ],
    )
    assert len(delivs) == 1
    rows = await store.list_deliverables_for_turn("t_1")
    assert len(rows) == 1


# ── ★ SEC-5: arbitrary file read found in security review (2026-08-05) ───
#
# Attack chain: users/teams can upload custom skills (.data/user_skills/<uid>/), with unrestricted script filenames.
# Naming one render_from_canvas.py enters this registration path; place a symlink in workdir,
# x.pdf → /proc/self/environ. _scan_outputs collects via is_file() (following symlinks), while
# read_bytes() resolves inside the **app process**, crossing the trust boundary to read all app-container
# environment variables (FERNET_KEY / JWT secret / executor token) and register them as an attacker-downloadable
# PDF. procfs st_size is zero, also bypassing the 50MB output limit.


async def test_symlink_output_is_rejected(store: ArtifactStore, workdir: Path) -> None:
    """★ 直接复刻攻击载荷：workdir 里的 symlink 指向 workdir 之外的敏感文件。

    ⚠ 诚实标注：**承重的是 workdir containment 那道闸**（resolve 后越界即拒）——
    实测把 `is_symlink()` 检查摘掉，本用例仍绿。保留 `is_symlink()` 属纵深防御
    （缩小 resolve→read 之间的 TOCTOU 窗口 + 日志可归因），本文件不独立覆盖它。
    """
    secret_dir = Path(tempfile.mkdtemp())
    secret = secret_dir / "environ"
    secret.write_bytes(b"FERNET_KEY=super-secret\x00JWT_SECRET=abc")

    link = workdir / "output.pdf"
    link.symlink_to(secret)
    assert link.is_file(), "前提：is_file() 跟随 symlink（这正是 _scan_outputs 会收录它的原因）"

    delivs = await _collect(store, [_inv("render_from_canvas.py", [str(link)], workdir=str(workdir))])
    assert delivs == [], "symlink 产物必须被拒"
    assert await store.list_deliverables_for_turn("t_1") == []


async def test_output_outside_workdir_is_rejected(store: ArtifactStore, workdir: Path) -> None:
    """产物路径落在本次调用 workdir 之外一律拒（兜住中间目录是 symlink 的情形）。"""
    other = Path(tempfile.mkdtemp())
    out = _write(other, "output.pdf")
    delivs = await _collect(store, [_inv("render_from_canvas.py", [out], workdir=str(workdir))])
    assert delivs == []


async def test_non_catalog_skill_name_is_rejected(store: ArtifactStore, workdir: Path) -> None:
    """只比脚本文件名不够 —— 用户自建 skill 可以随便叫 render_from_canvas.py。"""
    out = _write(workdir, "output.pdf")
    inv = _inv("render_from_canvas.py", [out], name="my-evil-skill")
    assert await _collect(store, [inv]) == []


def test_skill_name_whitelist_matches_ssot() -> None:
    """防漂移：core 侧这份白名单必须与 bowhead_skill 的权威集合一致。"""
    from bowhead_core.subagent import _CANVAS_EXPORT_SKILL_NAMES
    from bowhead_skill.tools import _CANVAS_EXPORT_SKILLS

    assert set(_CANVAS_EXPORT_SKILL_NAMES) == set(_CANVAS_EXPORT_SKILLS)


# ── P2-a / P2-b (dev 1.0.77 dogfood): report-title filenames, no longer all Bowhead Canvas ───


class _FakeCanvasStore:
    """只提供取名要用的两个方法。"""

    def __init__(self, sections, *, title=None, canvas=True, raises=False):
        self._sections = sections
        self._title = title
        self._canvas = canvas
        self._raises = raises

    async def get_canvas_by_session(self, session_id: str):
        if self._raises:
            raise RuntimeError("db down")
        return SimpleNamespace(id="cv_1", title=self._title) if self._canvas else None

    async def list_export_projection(self, canvas_id: str):
        return self._sections


def _h1(text: str):
    return SimpleNamespace(kind="h1", payload={"text": text})


async def test_filename_comes_from_report_h1(store: ArtifactStore, workdir: Path) -> None:
    """dogfood 实测：库里躺着 3 份同名 `Bowhead Canvas.docx`，用户分不清哪份是哪份。"""
    out = _write(workdir, "output.docx")
    delivs = await _collect_canvas_export_deliverables(
        [_inv("render_from_canvas.py", [out], name="canvas-docx")],
        store,
        session_id="s_1",
        user_id="u_1",
        turn_id="t_1",
        canvas_store=_FakeCanvasStore([_h1("2026 年 8 月经营汇报")]),
    )
    assert delivs[0]["filename"] == "2026 年 8 月经营汇报.docx"
    rows = await store.list_deliverables_for_turn("t_1")
    assert rows[0].filename == "2026 年 8 月经营汇报.docx"


async def test_two_reports_get_distinct_filenames(store: ArtifactStore, workdir: Path) -> None:
    """修复的价值就在这条：不同报告导出后名字必须不同（此前恒同名）。"""
    names = []
    for i, title in enumerate(("华东区复盘", "华南区复盘")):
        wd = Path(tempfile.mkdtemp())
        out = _write(wd, "output.docx")
        d = await _collect_canvas_export_deliverables(
            [_inv("render_from_canvas.py", [out], name="canvas-docx")],
            store,
            session_id="s_1",
            user_id="u_1",
            turn_id=f"t_{i}",
            canvas_store=_FakeCanvasStore([_h1(title)]),
        )
        names.append(d[0]["filename"])
    assert names == ["华东区复盘.docx", "华南区复盘.docx"]
    assert len(set(names)) == 2


async def test_no_canvas_store_keeps_legacy_name(store: ArtifactStore, workdir: Path) -> None:
    """老调用方不传 canvas_store —— 必须还是原来的名字，不能报错。"""
    out = _write(workdir, "output.pdf")
    delivs = await _collect(store, [_inv("render_from_canvas.py", [out])])
    assert delivs[0]["filename"] == "Bowhead Canvas.pdf"


async def test_title_lookup_failure_falls_back_not_raises(store: ArtifactStore, workdir: Path) -> None:
    """取名是 best-effort：画布读挂了也必须照常登记（宁可名字丑，不可文件丢）。"""
    out = _write(workdir, "output.xlsx")
    delivs = await _collect_canvas_export_deliverables(
        [_inv("render_from_canvas.py", [out], name="canvas-xlsx")],
        store,
        session_id="s_1",
        user_id="u_1",
        turn_id="t_1",
        canvas_store=_FakeCanvasStore([], raises=True),
    )
    assert len(delivs) == 1
    assert delivs[0]["filename"] == "Bowhead Canvas.xlsx"
