"""KB **直传**上传路径的产物空间归属（空间隔离批4 ①）——行为级断言。

覆盖 `routes/kb.py` 三个写入点：单文件 `POST /files`、bundle 根包、bundle 子件
（后两者都在 `_process_zip_bundle` 里）。团队档必须落显式哨兵 `__team__`，空间档必须落真实 `ws_*`。
（文件后半段另有**归属打点**的路由驱动用例，见 `# 归属打点` 一节。）

**为什么要单开一个文件**（不是随手新建）：仓里**没有任何**测试同时做到
「POST `/api/v1/kb/files`」+「接一个真的 `ArtifactStore`」——六个候选文件里
`ArtifactStore` 计数全是 0，唯一有的 `test_files_kb_promotion_routes.py` 又不走上传路由
⇒ 查不到 `artifacts` 表 ⇒ 这三处此前只有**源码文本级**覆盖。

**为什么不能等生产数据补课**：2026-09-19 的真 C4 实测证明这条路生产上也测不到——
切点后 KB 上传路**零样本**（`kb_upload` 末次 08-25、`kb_bundle_upload` 末次 08-13），
且 116 日产物量已从 1022 掉到 0 ⇒ **靠等窗口收敛不了，缺的是流量不是时间**。

⇒ **归属**这件事，这三条路除本文件外没有第二个背书来源。
⚠ 措辞按 2026-09-19 review 更正过：`test_kb_bundle_direct_concurrency.py` 也用真 `ArtifactStore`
直调 `_process_zip_bundle` 并断言子件数量，所以「子件到底有没有被造出来」**有**第二个来源；
它只是**不断言归属**。别把本行读成「这三条路完全没人测」。

留 NULL 的后果：批4 把「NULL 且创建于切点之后」翻成 fail-close，而 fail-close 是按**产物**判的
⇒ 切点后这样的产物对**所有人**关门，**包括上传者本人**。
⚠ **爆炸半径没那么大，别写成「整条路全 403」**（2026-09-19 自核更正）：fail-close 只由
`files.py` 的 `_require_artifact_operation`（12 处直接调用，2026-09-21 逐处数过）与 `sessions.py` 执行；
KB 自己的预览/下载（`kb.py` 的 `/files/{id}/preview`、`/download`）走 `_kb_scope_check`、按 `kb_files.scope` 授权，
**根本不查产物归属闸** ⇒ 一个 NULL 的 KB 产物在 KB 页面照常渲染，只在**资产库/文件下载**那一族出口 403。

⚠ **夹具保真度**（与 `test_kb_presign_phase14b.py` 同款，写在这里省得下一个人重查）：
`_FakeUserStore` 不带 RBAC 方法 ⇒ `has_workspace_access_source()` 为 False
⇒ `_make_scope_context_checked` 直接返回**请求头推出的** ctx，不经 `build_access_context`。
归属表达式在两条路上逐字相同（后者只校验成员资格、**不改写** `workspace_id`），
所以这里的断言仍然成立；但**授权**那一面不由本文件覆盖。
"""

from __future__ import annotations

import asyncio
import io
import zipfile
from typing import TYPE_CHECKING

import aiosqlite
import pytest
from bowhead_core.artifact_store import TEAM_SCOPE_WORKSPACE_ID, LocalBackend
from bowhead_core.kb.files_store import KbFilesStore
from bowhead_core.user_store import User
from bowhead_gateway.routes import kb, kb_presign
from bowhead_gateway.routes.auth import _current_user
from fastapi import FastAPI
from fastapi.testclient import TestClient

from bowhead_core import ArtifactStore

if TYPE_CHECKING:
    from pathlib import Path


def _build_zip(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in entries.items():
            zf.writestr(name, content)
    return buf.getvalue()


def _make_user(uid: str = "u_alice", team: str = "team_test", email: str = "alice@test.com") -> User:
    return User(id=uid, email=email, team_id=team, tier="ultra", email_verified=True)


class _FakeUserStore:
    """只有 `rate_limit_check` ⇒ `has_workspace_access_source()` 为 False（见模块 docstring）。"""

    async def rate_limit_check(self, bucket: str, *, limit: int, window_s: int) -> bool:
        return True


class _FakeWorker:
    def __init__(self) -> None:
        self.called: list[str] = []

    async def parse_and_index(self, file_id: str) -> None:
        self.called.append(file_id)

    async def parse_and_index_many(self, file_ids: list[str]) -> None:
        for fid in file_ids:
            await self.parse_and_index(fid)


@pytest.fixture
async def app(initialize_migrated_kb_files_database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, initialize_migrated_core_database) -> FastAPI:
    # Same as the two kb_presign test files: default `_resolve_role` returns 'user';
    # only team cases elevate as needed (team is the only `can_write_to_scope` branch checking role).
    monkeypatch.delenv("BOWHEAD_AUTO_ADMIN_DOMAINS", raising=False)

    a = FastAPI()
    a.include_router(kb.router)

    # ★ Use conftest's **shared migrated snapshot** (`docs/单元测试规范.md`: clean real core `upgrade head`
    # snapshot + SQLite backup copy, avoiding full migrations for each case).
    #
    # Why `ArtifactStore.initialize()` alone is insufficient: bundle children write
    # `chat_attachment_bundle_members`, created only by migration `0010`, **not** the store's ALTER loop.
    # With only `initialize()`, child persistence raises `no such table`, **swallowed as a per-entry skip** by the route
    # (`kb_bundle_entry_ingest_failed`, HTTP still **200**)⇒0 children
    # ⇒ assertions that all child attributions are correct **pass vacuously**. The first version failed this way, caught by `len(...) == 2`.
    #
    # ★ Also avoid a trap: `bowhead_cli.migrate.upgrade()` extracts only the **directory** from the URL;
    # filename hardcoded to `TARGET_FILES["core"] == "bowhead.db"`. Calling `upgrade()` with another database name
    # **silently migrates another file**. Backup copying has no such constraint;
    # deliberately named `artifacts.db` here to prove the filename is no longer load-bearing.
    artifact_db_path = tmp_path / "artifacts.db"
    initialize_migrated_core_database(artifact_db_path)
    store = ArtifactStore(
        db_path=str(artifact_db_path),
        backend=LocalBackend(root_dir=tmp_path / "blobs"),
    )
    await store.initialize()
    a.state.artifact_store = store

    initialize_migrated_kb_files_database(tmp_path / "kb_files.db")
    files_store = KbFilesStore(tmp_path / "kb_files.db")
    await files_store.initialize()
    a.state.kb_files_store = files_store

    a.state.user_store = _FakeUserStore()
    a.state.kb_worker = _FakeWorker()
    return a


@pytest.fixture
def stub_current_user(app: FastAPI) -> dict:
    state = {"user": _make_user()}

    async def _fake() -> User:
        return state["user"]

    app.dependency_overrides[_current_user] = _fake
    return state


@pytest.fixture
def client(app: FastAPI, stub_current_user) -> TestClient:
    return TestClient(app)


async def _artifacts(app: FastAPI) -> dict[str, str | None]:
    """全表 `{artifact_id: workspace_id}`。

    ★ 按 **id 锚定**取值（id 来自响应体），而不是按 `source` 字符串筛选：后者一旦漂移
      就会静默选空集。同时保留**全表条数**作反空转断言——两者各管一件事：
      条数管「没多写、没漏写」，id 管「取到的确实是这次写的那一条」。
    """
    async with aiosqlite.connect(app.state.artifact_store.db_path) as db:
        cur = await db.execute("SELECT id, workspace_id FROM artifacts")
        return {row[0]: row[1] for row in await cur.fetchall()}


async def _kb_file_rows(app: FastAPI) -> list[tuple[str, str]]:
    """kb_files 的 `(scope, workspace_id)`。

    ★★ 为什么要连 `workspace_id` 一起读（2026-09-19 review 逮到）：全仓**没有任何测试**
      读过 `kb_files.workspace_id`，所以「调用点把哨兵 `__team__` 喂给 kb_files」这种错
      会**全绿溜过去**——而那恰恰是本次收口最容易犯、后果最重的一种：
      团队行会 materialize 成一个名叫 `__team__` 的假空间，把按空间计数/去重的查询和
      `WHERE workspace_id != ''` 那个偏索引一起带偏。
    """
    async with aiosqlite.connect(app.state.kb_files_store._db_path) as db:
        cur = await db.execute("SELECT scope, workspace_id FROM kb_files")
        return [(r[0], r[1]) for r in await cur.fetchall()]


#: Two settings = **discriminating controls**: team alone misses unconditional sentinel writes; space alone misses
#: removing the `or TEAM_SCOPE_WORKSPACE_ID` fallback. Same policy as both kb_presign files.
_CASES = [
    ("team", "team_test", "team", TEAM_SCOPE_WORKSPACE_ID),
    ("space", "ws_42", "space", "ws_42"),
]
_IDS = ["team_scope_sentinel", "space_scope_real_id"]


@pytest.mark.parametrize(("scope", "scope_id", "expected_scope", "expected_workspace"), _CASES, ids=_IDS)
def test_kb_single_upload_persists_artifact_workspace_attribution(
    client: TestClient,
    app: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
    scope: str,
    scope_id: str,
    expected_scope: str,
    expected_workspace: str,
) -> None:
    """★ `POST /api/v1/kb/files` 直传必须把归属真的写进库。

    这是**行为断言**（查库里的真值）。源码文本级守卫对「把写入值改成 `None`」是瞎的——
    2026-09-18 在 `kb_presign` 上实测过同族突变：值改成 `None` 之后
    `test_every_artifact_writer_decides_about_workspace_id` 与
    `test_every_put_bytes_call_site_supplies_an_attribution_input` **仍然全绿**
    （前者只在文件里做子串匹配，后者只看关键字**在不在**、不看值）。
    [[feedback_gate_that_cannot_parse_must_not_pass]]
    """
    if scope == "team":
        monkeypatch.setenv("BOWHEAD_AUTO_ADMIN_DOMAINS", "test.com")

    r = client.post(
        "/api/v1/kb/files",
        files={"file": ("doc.pdf", b"%PDF-1.4\nsingle upload body", "application/pdf")},
        data={"scope": scope, "scope_id": scope_id},
        # ★ The team case **deliberately also** carries a space header; do not remove it: this additionally proves the team branch
        # (`scope == "team"` in `decide_for_kb_scope`) **ignores** headers and persists the sentinel, not `ws_42`.
        headers={"X-Space-Id": "ws_42"},
    )
    assert r.status_code == 200, r.text
    aid = r.json()["artifact_id"]

    arts = asyncio.run(_artifacts(app))
    kb_rows = asyncio.run(_kb_file_rows(app))

    # Prevent vacuous pass: exactly one artifact (0⇒next assertion only proves no row; 2⇒extra write).
    assert len(arts) == 1, f"应恰好落一条产物，实得 {len(arts)} 条"
    # Prerequisite: this upload **actually** used the tested scope.
    # ★★ Also pin `kb_files.workspace_id`: team scope must use an **empty string**, **not** the sentinel.
    # These columns have opposite values for team scope, easily miswired during consolidation; previously no repository guard covered this column.
    expected_kb_ws = "" if scope == "team" else "ws_42"
    assert kb_rows == [(expected_scope, expected_kb_ws)], f"kb_files 行不对（期望 scope={expected_scope!r} ws={expected_kb_ws!r}）：{kb_rows!r}"
    assert arts[aid] == expected_workspace, f"归属写错：实得 {arts[aid]!r}，应为 {expected_workspace!r}（留 NULL 会在切点后被 fail-close 拦掉）"


@pytest.mark.parametrize(("scope", "scope_id", "expected_scope", "expected_workspace"), _CASES, ids=_IDS)
def test_kb_bundle_upload_persists_attribution_for_root_and_children(
    client: TestClient,
    app: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
    scope: str,
    scope_id: str,
    expected_scope: str,
    expected_workspace: str,
) -> None:
    """★ 一条用例同时钉住 `_process_zip_bundle` 里 bundle **根包**与 bundle **子件**两个写入点。

    ★ `len(arts) == 2`（根包 + 恰好一个子件）是**反空转**断言：子件被吞掉时它是 1 而不是 2，
      而「子件归属都对」那种写法在同样情形下会**空过**——这正是「断言没发生」和
      「用例根本没跑到」长得一样的那一类。首版就撞上了：夹具缺
      `chat_attachment_bundle_members` 时，路由把子件写库异常**吞成 per-entry skip、HTTP 仍 200**。

    ★ 根包与子件各用**响应体里的 id** 锚定（`bundle_artifact_id` / `uploaded[0].artifact_id`），
      不按 `source` 字符串筛选：后者一旦漂移就会静默选空集，而空集会让断言无声通过。
    """
    if scope == "team":
        monkeypatch.setenv("BOWHEAD_AUTO_ADMIN_DOMAINS", "test.com")

    zip_body = _build_zip({"child.pdf": b"%PDF-1.4\nchild body"})
    r = client.post(
        "/api/v1/kb/files/bundle",
        files={"file": ("pack.zip", zip_body, "application/zip")},
        data={"scope": scope, "scope_id": scope_id},
        headers={"X-Space-Id": "ws_42"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert [f["filename"] for f in body["uploaded"]] == ["child.pdf"], r.text

    root_id = body["bundle_artifact_id"]
    child_id = body["uploaded"][0]["artifact_id"]
    arts = asyncio.run(_artifacts(app))

    # Prevent vacuous pass: root bundle + exactly one child. A swallowed (skipped) child gives 1, not 2 here;
    # checking only that all child attributions are correct would **pass vacuously** in the same situation.
    assert len(arts) == 2, f"应为根包+1 子件共 2 条，实得 {len(arts)} 条：{arts}"

    assert arts[root_id] == expected_workspace, f"根包归属写错：实得 {arts[root_id]!r}，应为 {expected_workspace!r}"
    assert arts[child_id] == expected_workspace, f"子件归属写错：实得 {arts[child_id]!r}，应为 {expected_workspace!r}"


# ───────────────────────── Attribution metrics (metrics_event_log) ──────────
#
# Wiring points must be driven **through real routes**: helper already has unit/end-to-end tests, but actual calls, correct route/reason,
# and rejected requests not counted require running routes; other tests all disable the flag, leaving missed/wrong wiring green.
#
# ★ Metrics run as **background tasks outside the request path**: use `with client:` (persistent portal event loop), then `attribution_metrics.drain(client)`
# to deterministically await writes; otherwise reads always return 0, making the flag-off control vacuous too.

_METRICS_FLAG = "FEATURE_ARTIFACT_ATTRIBUTION_METRICS"

#: (scope, scope_id, space header present, expected reason, expected scope in rollup).
#: ⚠ **`personal_without_space_header → none` is reachable only in this fixture, not production** (2026-09-21 C review):
#: fixture `_FakeUserStore` lacks RBAC hooks, so `_make_scope_context_checked` **does not call** `build_access_context`;
#: real RBAC returns 400 `workspace_required` first without a space header (see final `test_kb_write_without_space_header_is_rejected_under_real_rbac`).
#: This setting therefore only verifies correct route reason wiring, **not that production KB can have none**.
#: ★ Fourth setting pins personal + space header ⇒ space; wrongly recording none with a personal header previously survived the whole suite.
#: ★ Fifth setting: old spelling `workspace`. `Form` routes receive raw spelling; JSON routes already normalized by pydantic;
#: without normalization in metrics, the same `space` bucket splits into two (B review).
_METRIC_CASES = [
    ("team", "team_test", True, "team", "team"),
    ("space", "ws_42", True, "space", "space"),
    ("personal", "u_alice", False, "none", "personal"),
    ("personal", "u_alice", True, "space", "personal"),
    ("workspace", "ws_42", True, "space", "space"),
]
_METRIC_IDS = ["team", "space", "personal_without_space_header_HARNESS_ONLY", "personal_with_space_header", "legacy_workspace_spelling"]


@pytest.mark.integration
@pytest.mark.parametrize(("scope", "scope_id", "with_space_header", "expected_reason", "expected_scope"), _METRIC_CASES, ids=_METRIC_IDS)
def test_kb_single_upload_emits_one_attribution_metric(
    client: TestClient,
    app: FastAPI,
    attribution_metrics,
    monkeypatch: pytest.MonkeyPatch,
    scope: str,
    scope_id: str,
    with_space_header: bool,
    expected_reason: str,
    expected_scope: str,
) -> None:
    monkeypatch.setenv(_METRICS_FLAG, "1")
    if scope == "team":
        monkeypatch.setenv("BOWHEAD_AUTO_ADMIN_DOMAINS", "test.com")
    attribution_metrics.attach(app)

    with client:
        r = client.post(
            "/api/v1/kb/files",
            files={"file": ("doc.pdf", b"%PDF-1.4\nsingle upload body", "application/pdf")},
            data={"scope": scope, "scope_id": scope_id},
            headers={"X-Space-Id": "ws_42"} if with_space_header else {},
        )
        assert r.status_code == 200, r.text
        attribution_metrics.drain(client)

    assert attribution_metrics.events() == [{"route": "kb_single_upload", "reason": expected_reason, "scope": expected_scope}]
    assert attribution_metrics.user_ids() == [None]


@pytest.mark.integration
def test_kb_single_upload_flag_off_control_writes_no_metric(client: TestClient, app: FastAPI, attribution_metrics) -> None:
    """★ 对照：同一请求、同一读取路径，flag 关着必须 0 行。drain 之后再读，所以「0」只可能是没排过任务。"""
    attribution_metrics.attach(app)
    with client:
        r = client.post(
            "/api/v1/kb/files",
            files={"file": ("doc.pdf", b"%PDF-1.4\nsingle upload body", "application/pdf")},
            data={"scope": "space", "scope_id": "ws_42"},
            headers={"X-Space-Id": "ws_42"},
        )
        assert r.status_code == 200, r.text
        attribution_metrics.drain(client)
    assert attribution_metrics.events() == []


@pytest.mark.integration
def test_kb_single_upload_rejected_requests_are_not_counted(
    client: TestClient, app: FastAPI, attribution_metrics, monkeypatch: pytest.MonkeyPatch
) -> None:
    """★ 被拒的请求不计数——打点在**成功之后**。若被误挪到 403/415 的前面，`none` 桶（本打点要量的那一个）会被拒绝请求灌水。

    每个请求都先断言状态码：前置不成立时（拿到的不是预期的拒绝），本用例要报出来，而不是空过。
    """
    monkeypatch.setenv(_METRICS_FLAG, "1")
    attribution_metrics.attach(app)
    with client:
        # 403: team scope but caller not admin (fixture defaults role='user').
        denied = client.post(
            "/api/v1/kb/files",
            files={"file": ("doc.pdf", b"%PDF-1.4\nbody", "application/pdf")},
            data={"scope": "team", "scope_id": "team_test"},
        )
        assert denied.status_code == 403, denied.text
        # 415: unsupported file type.
        unsupported = client.post(
            "/api/v1/kb/files",
            files={"file": ("x.bin", b"\x00\x01\x02\x03", "application/octet-stream")},
            data={"scope": "space", "scope_id": "ws_42"},
            headers={"X-Space-Id": "ws_42"},
        )
        assert unsupported.status_code == 415, unsupported.text
        attribution_metrics.drain(client)
    assert attribution_metrics.events() == []


@pytest.mark.integration
@pytest.mark.parametrize(("scope", "scope_id", "with_space_header", "expected_reason", "expected_scope"), _METRIC_CASES, ids=_METRIC_IDS)
def test_kb_bundle_upload_emits_exactly_one_metric_not_one_per_artifact(
    client: TestClient,
    app: FastAPI,
    attribution_metrics,
    monkeypatch: pytest.MonkeyPatch,
    scope: str,
    scope_id: str,
    with_space_header: bool,
    expected_reason: str,
    expected_scope: str,
) -> None:
    """★ 一次 bundle 上传只记**一次**（根包 + 两个子件共 3 条产物，但归属只决定了一次）。

    若把打点误挪进子件循环，rollup 的计数会变成「产物数」而不是「上传次数」，
    而这个分布正是用来估「有多少上传推不出归属」的——被子件数放大后比例就失真了。
    四档参数化：只测 space 档时，「把 reason/scope 写死成 space」这类改错照样绿。
    """
    monkeypatch.setenv(_METRICS_FLAG, "1")
    if scope == "team":
        monkeypatch.setenv("BOWHEAD_AUTO_ADMIN_DOMAINS", "test.com")
    attribution_metrics.attach(app)

    with client:
        r = client.post(
            "/api/v1/kb/files/bundle",
            files={"file": ("pack.zip", _build_zip({"a.pdf": b"%PDF-1.4\nA", "b.pdf": b"%PDF-1.4\nB"}), "application/zip")},
            data={"scope": scope, "scope_id": scope_id},
            headers={"X-Space-Id": "ws_42"} if with_space_header else {},
        )
        assert r.status_code == 200, r.text
        assert len(r.json()["uploaded"]) == 2, r.text  # Prevent vacuous pass: both children really persisted.
        attribution_metrics.drain(client)

    assert attribution_metrics.events() == [{"route": "kb_bundle_upload", "reason": expected_reason, "scope": expected_scope}]


@pytest.mark.integration
def test_kb_bundle_upload_flag_off_control_writes_no_metric(client: TestClient, app: FastAPI, attribution_metrics) -> None:
    attribution_metrics.attach(app)
    with client:
        r = client.post(
            "/api/v1/kb/files/bundle",
            files={"file": ("pack.zip", _build_zip({"a.pdf": b"%PDF-1.4\nA"}), "application/zip")},
            data={"scope": "space", "scope_id": "ws_42"},
            headers={"X-Space-Id": "ws_42"},
        )
        assert r.status_code == 200, r.text
        attribution_metrics.drain(client)
    assert attribution_metrics.events() == []


@pytest.mark.integration
def test_kb_admin_import_is_counted_under_its_own_route_name(
    client: TestClient, app: FastAPI, attribution_metrics, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """★ `/kb/admin/import` 与用户 bundle 上传共用 `_process_zip_bundle`——不单独命名的话，
    管理员服务端导入会混进 `kb_bundle_upload` 的分布里（B 评审：这是第 8 个入口，不是 7 个）。
    """
    monkeypatch.setenv(_METRICS_FLAG, "1")
    monkeypatch.setenv("BOWHEAD_AUTO_ADMIN_DOMAINS", "test.com")  # Route requires ws_admin / team_admin.
    monkeypatch.setattr(kb, "_ADMIN_IMPORT_ALLOWED_PREFIXES", (str(tmp_path.resolve()) + "/",))
    attribution_metrics.attach(app)

    zip_path = tmp_path / "import.zip"
    zip_path.write_bytes(_build_zip({"a.pdf": b"%PDF-1.4\nA"}))

    with client:
        r = client.post(
            "/api/v1/kb/admin/import",
            json={"server_path": str(zip_path.resolve()), "scope": "space", "scope_id": "ws_42"},
            headers={"X-Space-Id": "ws_42"},
        )
        assert r.status_code == 200, r.text
        assert len(r.json()["uploaded"]) == 1, r.text  # Prevent vacuous pass.
        attribution_metrics.drain(client)

    assert attribution_metrics.events() == [{"route": "kb_admin_import", "reason": "space", "scope": "space"}]


@pytest.mark.integration
def test_kb_bundle_upload_and_admin_import_rejected_requests_are_not_counted(
    client: TestClient, app: FastAPI, attribution_metrics, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """★ C 评审：「有用例逐一钉住被拒不计数」对 bundle 直传与 admin import 此前是**过头的话**——
    这两条路的拒绝（坏 zip / 路径越界 / 文件不存在 / 非 zip）从没有人在 flag 开着时发过。
    每个请求先断言状态码，前置不成立时本用例要报出来而不是空过。
    """
    monkeypatch.setenv(_METRICS_FLAG, "1")
    monkeypatch.setenv("BOWHEAD_AUTO_ADMIN_DOMAINS", "test.com")
    monkeypatch.setattr(kb, "_ADMIN_IMPORT_ALLOWED_PREFIXES", (str(tmp_path.resolve()) + "/",))
    attribution_metrics.attach(app)

    not_a_zip = tmp_path / "plain.txt"
    not_a_zip.write_text("not a zip")
    body = {"scope": "space", "scope_id": "ws_42"}
    with client:
        bad_zip = client.post(
            "/api/v1/kb/files/bundle",
            files={"file": ("pack.zip", b"this is not a zip archive", "application/zip")},
            data=body,
            headers={"X-Space-Id": "ws_42"},
        )
        assert bad_zip.status_code == 400, bad_zip.text
        outside = client.post("/api/v1/kb/admin/import", json={"server_path": "/etc/passwd", **body}, headers={"X-Space-Id": "ws_42"})
        assert outside.status_code == 403, outside.text
        missing = client.post(
            "/api/v1/kb/admin/import",
            json={"server_path": str((tmp_path / "nope.zip").resolve()), **body},
            headers={"X-Space-Id": "ws_42"},
        )
        assert missing.status_code == 404, missing.text
        wrong_type = client.post("/api/v1/kb/admin/import", json={"server_path": str(not_a_zip.resolve()), **body}, headers={"X-Space-Id": "ws_42"})
        assert wrong_type.status_code == 415, wrong_type.text
        attribution_metrics.drain(client)
    assert attribution_metrics.events() == []


@pytest.mark.integration
def test_kb_single_upload_failure_after_the_decision_is_not_counted(
    app: FastAPI, stub_current_user, attribution_metrics, monkeypatch: pytest.MonkeyPatch
) -> None:
    """★ B 评审：「成功之后才计数」此前只被**决策之前**的拒绝（403/415）保护，从没有请求在决策**之后**失败过——
    把打点挪回决策点、这些用例照样全绿。这里让请求在**紧贴打点之前的最后一步**失败
    （`files_store.get` 读不回刚建的行 ⇒ 500 `kb_file_write_failed`）：任何更早的位置都会把这次失败计进去。
    放行后再发一次（正对照：同样的夹具下成功的请求**会**被计一行，证明「0」不是流水线坏了）。
    """
    monkeypatch.setenv(_METRICS_FLAG, "1")
    attribution_metrics.attach(app)
    files_store = app.state.kb_files_store
    real_get = files_store.get

    async def _lost_row(_file_id):
        return None

    upload = {
        "files": {"file": ("doc.pdf", b"%PDF-1.4\nbody", "application/pdf")},
        "data": {"scope": "space", "scope_id": "ws_42"},
        "headers": {"X-Space-Id": "ws_42"},
    }
    with TestClient(app, raise_server_exceptions=False) as strict:
        monkeypatch.setattr(files_store, "get", _lost_row)
        failed = strict.post("/api/v1/kb/files", **upload)
        assert failed.status_code == 500, failed.text  # Prerequisite: actually failed after the decision.
        attribution_metrics.drain(strict)
        assert attribution_metrics.events() == [], "失败的那次不得计数"

        monkeypatch.setattr(files_store, "get", real_get)
        ok = strict.post("/api/v1/kb/files", **upload)
        assert ok.status_code == 200, ok.text
        attribution_metrics.drain(strict)
    assert attribution_metrics.events() == [{"route": "kb_single_upload", "reason": "space", "scope": "space"}]


@pytest.mark.integration
def test_kb_bundle_upload_failure_after_the_decision_is_not_counted(
    app: FastAPI, stub_current_user, attribution_metrics, fail_audit_event, monkeypatch: pytest.MonkeyPatch
) -> None:
    """同上，bundle 直传（`_process_zip_bundle`）：让紧贴打点之前的 `kb.uploaded.bundle` 审计抛错 ⇒ 500。"""
    monkeypatch.setenv(_METRICS_FLAG, "1")
    attribution_metrics.attach(app)
    upload = {
        "files": {"file": ("pack.zip", _build_zip({"a.pdf": b"%PDF-1.4\nA"}), "application/zip")},
        "data": {"scope": "space", "scope_id": "ws_42"},
        "headers": {"X-Space-Id": "ws_42"},
    }
    with TestClient(app, raise_server_exceptions=False) as strict:
        restore = fail_audit_event(kb, "kb.uploaded.bundle")
        failed = strict.post("/api/v1/kb/files/bundle", **upload)
        assert failed.status_code == 500, failed.text
        attribution_metrics.drain(strict)
        assert attribution_metrics.events() == [], "失败的那次不得计数"

        restore()
        ok = strict.post("/api/v1/kb/files/bundle", **upload)
        assert ok.status_code == 200, ok.text
        attribution_metrics.drain(strict)
    assert attribution_metrics.events() == [{"route": "kb_bundle_upload", "reason": "space", "scope": "space"}]


@pytest.fixture
async def real_rbac_kb(initialize_migrated_kb_files_database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, initialize_migrated_core_database):
    """**真** `UserStore`（带 RBAC 钩子，与生产网关一致）+ 真 workspace + kb 路由。

    本文件其余夹具用的是不带 RBAC 的 `_FakeUserStore`，那条路**跳过** `build_access_context`——
    所以「KB 缺空间头会 400」这件事在它们里根本看不见（C 评审：KB 的 `reason=none` 生产不可达）。
    """
    from bowhead_core.user_store import UserStore

    monkeypatch.delenv("BOWHEAD_AUTO_ADMIN_DOMAINS", raising=False)
    users = UserStore(tmp_path / "bowhead_users.db")
    await users.initialize()
    team = await users.create_team(name="RBAC Team", max_seats=10)
    user = await users.create_user(team_id=team.id, email="rbac@example.com", password="Passw0rd!")
    workspace_id = await users.create_local_workspace(team_id=team.id, name="Space A", creator_user_id=user.id)

    artifact_db_path = tmp_path / "rbac_artifacts.db"
    initialize_migrated_core_database(artifact_db_path)
    store = ArtifactStore(db_path=str(artifact_db_path), backend=LocalBackend(root_dir=tmp_path / "rbac_blobs"))
    await store.initialize()
    initialize_migrated_kb_files_database(tmp_path / "rbac_kb_files.db")
    files_store = KbFilesStore(tmp_path / "rbac_kb_files.db")
    await files_store.initialize()

    a = FastAPI()
    a.include_router(kb.router)
    a.include_router(kb_presign.router)  # presign / confirm live in this router; omitting it makes all those entries 404.
    a.dependency_overrides[_current_user] = lambda: user
    a.dependency_overrides[kb_presign._current_user] = lambda: user
    a.state.user_store = users
    a.state.artifact_store = store
    a.state.kb_files_store = files_store
    a.state.kb_worker = _FakeWorker()
    return a, user, workspace_id


@pytest.mark.integration
def test_every_kb_write_entry_rejects_a_missing_space_header_under_real_rbac(real_rbac_kb) -> None:
    """★★ 第三轮复核逮到：只钉 `POST /kb/files` 一个入口，文档却说「7 个入口都先 400」——复核者在副本里给
    `kb_presign` 加上 `allow_missing_workspace=True`，上面那条照样通过。所以这里**逐入口**钉：
    5 个直接入口 × 3 档 scope（personal / team / space）+ bundle confirm（team）。
    `space` and `personal` confirms take the Space stored on the marker at presign time and must not 400.
    请求都不带空间头；期望**全部** 400 `workspace_required`。失败信息会指名是哪个入口哪一档。
    """
    app, user, workspace_id = real_rbac_kb
    files_store = app.state.kb_files_store
    scopes = {"personal": user.id, "team": user.team_id, "space": workspace_id}

    async def _marker(scope: str, scope_id: str) -> str:
        return await files_store.create(
            artifact_id="art_marker",
            filename="pack.zip",
            mime_type="application/zip",
            size_bytes=100,
            scope=scope,
            scope_id=scope_id,
            workspace_id=workspace_id,
            team_id=user.team_id,
            uploaded_by=user.id,
            initial_parse_status="awaiting_upload",
        )

    not_rejected: list[str] = []
    with TestClient(app, raise_server_exceptions=False) as client:
        for scope, scope_id in scopes.items():
            body = {"scope": scope, "scope_id": scope_id}
            presign = {"filename": "a.pdf", "mime": "application/pdf", "size_bytes": 100, **body}
            bundle_presign = {"filename": "p.zip", "mime": "application/zip", "size_bytes": 100, **body}
            attempts = {
                "POST /kb/files": client.post("/api/v1/kb/files", files={"file": ("d.pdf", b"%PDF-1.4\nx", "application/pdf")}, data=body),
                "POST /kb/files/bundle": client.post(
                    "/api/v1/kb/files/bundle", files={"file": ("p.zip", _build_zip({"a.pdf": b"%PDF-1.4\nA"}), "application/zip")}, data=body
                ),
                "POST /kb/admin/import": client.post("/api/v1/kb/admin/import", json={"server_path": "/tmp/x.zip", **body}),
                "POST /kb/presign": client.post("/api/v1/kb/presign", json=presign),
                "POST /kb/presign/bundle": client.post("/api/v1/kb/presign/bundle", json=bundle_presign),
            }
            if scope == "team":
                file_id = asyncio.run(_marker(scope, scope_id))
                attempts["POST /kb/presign/bundle/{id}/confirm"] = client.post(f"/api/v1/kb/presign/bundle/{file_id}/confirm", json={})
            elif scope == "personal":
                # de17723dc: a personal bundle confirm, like a space one, takes the Space stored on its
                # marker at presign time, so it must get past the Space gate without the header.
                file_id = asyncio.run(_marker(scope, scope_id))
                confirm = client.post(f"/api/v1/kb/presign/bundle/{file_id}/confirm", json={})
                assert "workspace_required" not in confirm.text, confirm.text
            for entry, r in attempts.items():
                if r.status_code != 400 or "workspace_required" not in r.text:
                    not_rejected.append(f"{entry} [scope={scope}] → {r.status_code} {r.text[:80]}")

    assert not_rejected == [], f"以下入口缺空间头时**没有**被 400 workspace_required 拒掉：{not_rejected}"


@pytest.mark.integration
def test_kb_write_without_space_header_is_rejected_under_real_rbac(real_rbac_kb, attribution_metrics, monkeypatch: pytest.MonkeyPatch) -> None:
    """★★ 回归警报：**真实 RBAC 下，KB 上传缺空间头 ⇒ 400 `workspace_required`**，所以 KB 的 `reason=none` 在生产里不可达。

    灰度清单里「KB 路由只会记 `space` / `team`」这句话靠它撑着；将来有人放宽了这道 400
    （比如给上传路径也传 `allow_missing_workspace=True`），这条会红——那时 KB 的 `none` 就真的会出现，
    文档与路由表都得跟着改。带头则 200，且记 `space`。
    """
    app, user, workspace_id = real_rbac_kb
    monkeypatch.setenv(_METRICS_FLAG, "1")
    attribution_metrics.attach(app)
    upload = {"files": {"file": ("doc.pdf", b"%PDF-1.4\nbody", "application/pdf")}, "data": {"scope": "personal", "scope_id": user.id}}

    with TestClient(app, raise_server_exceptions=False) as client:
        no_header = client.post("/api/v1/kb/files", **upload)
        assert no_header.status_code == 400, no_header.text
        assert "workspace_required" in no_header.text
        with_header = client.post("/api/v1/kb/files", **upload, headers={"X-Space-Id": workspace_id})
        assert with_header.status_code == 200, with_header.text
        attribution_metrics.drain(client)

    assert attribution_metrics.events() == [{"route": "kb_single_upload", "reason": "space", "scope": "personal"}]
