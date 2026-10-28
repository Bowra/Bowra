"""批1/c7 (workspace-space-split) — 空间/北极九章工作区命名与兜底守卫.

批1 把 B 面（datarc）符号统一成 `datarc_workspace_*` 前缀，并退役了三类跨义兜底
（Bowhead space id 冒充 datarc workspace / team id 冒充 workspace / space id 冒充 team id）。
本守卫是源码级 pin，锁住不复活；详见 docs/sprints/workspace-space-split/plan.md §1。
"""

from __future__ import annotations

import re
from pathlib import Path

_PKGS = Path(__file__).resolve().parents[2]


def _src(rel: str) -> str:
    return (_PKGS / rel).read_text(encoding="utf-8")


def test_memory_governance_id_prefixes_are_registered() -> None:
    terminology = (_PKGS.parent / "docs/design/概念术语.md").read_text(encoding="utf-8")
    privacy = _src(
        "bowhead_core/src/bowhead_core/memory_privacy.py",
    )
    for prefix in (
        "cand_",
        "gl_",
        "snap_",
        "anc_",
        "mop_",
        "mrg_",
        "pdr_",
    ):
        assert f"`{prefix}`" in terminology
    for prefix in ("mop_", "mrg_", "pdr_"):
        assert prefix in privacy


def test_datarc_surface_has_no_bare_workspace_id() -> None:
    """datarc 面（bi_bridge 全包 + member_resolver）禁裸 `workspace_id`——必须带
    `datarc_` 前缀，否则批1/c1 的语义收口被回退。"""
    for rel in (
        "bowhead_bi_bridge/src/bowhead_bi_bridge/datarc_client.py",
        "bowhead_bi_bridge/src/bowhead_bi_bridge/types.py",
        "bowhead_gateway/src/bowhead_gateway/member_resolver.py",
    ):
        bare = re.findall(r"(?<!datarc_)\bworkspace_id\b", _src(rel))
        assert not bare, f"{rel}: 出现裸 workspace_id ×{len(bare)} — datarc 面必须写 datarc_workspace_id"


def test_cross_meaning_fallbacks_stay_dead() -> None:
    """三类跨义兜底（批1/c2-c5 退役）不许复活。每条 pattern 都对应一个修掉的真实故障形状。"""
    revived: list[str] = []
    checks = [
        # c3: pass Bowhead space ID directly to datarc (resolve_datarc_project layer 3)
        ("bowhead_core/src/bowhead_core/tool_registry.py", r"return self\.workspace_id"),
        # c2: bare fallback for datarc_project_for_calls
        ("bowhead_gateway/src/bowhead_gateway/datarc_context.py", r"datarc_project_for_calls=active_project\s*\n?\s*or workspace_id"),
        # c4: team ID masquerades as workspace (KB scope)
        ("bowhead_gateway/src/bowhead_gateway/routes/kb.py", r'X-Workspace-Id"\)\s*or\s*user\.team_id'),
        ("bowhead_gateway/src/bowhead_gateway/routes/kb_admin.py", r'X-Workspace-Id"\)\s*or\s*user\.team_id'),
        # c5: space ID masquerades as team ID
        ("bowhead_core/src/bowhead_core/turn_runner.py", r'team_id=getattr\(request, "team_id", ""\)\s*or\s*getattr\(request, "workspace_id"'),
        ("bowhead_core/src/bowhead_core/tool_registry.py", r"team_id=ctx\.team_id\s+or\s+ctx\.workspace_id"),
        # c3: duck-typed bypass (ctx.workspace_id falls back into datarc semantics)
        ("bowhead_core/src/bowhead_core/embed_search.py", r'_project or getattr\(ctx, "workspace_id"'),
        ("bowhead_core/src/bowhead_core/subagent.py", r'else getattr\(ctx, "workspace_id", ""\) or ""'),
        # c11 (impl review): bare parameter names in datarc member resolution; chat helper and feishu bridge
        # carry datarc IDs, so parameters need the datarc_ prefix (c7's complete scan of 3 files misses these two locations).
        ("bowhead_gateway/src/bowhead_gateway/routes/chat.py", r"def _resolve_member_uid_first_hit\([^)]*[^_]workspace_id: str"),
        ("bowhead_gateway/src/bowhead_gateway/feishu/bridge.py", r"identity: str, workspace_id: str"),
        # c11: serialized chat 403 detail key; datarc IDs must no longer use a bare workspace_id key.
        ("bowhead_gateway/src/bowhead_gateway/routes/chat.py", r'"workspace_id": str\(datarc_ws\)'),
        # The c11 suggestions check was removed with `workspace_suggestions.py` (2026-08-07):
        # that module's sole consumer was a dead endpoint always returning 503. Remove the check too: `_src()` directly calls
        # `read_text()`, so a missing file raises FileNotFoundError and kills the guard as an error
        # rather than an assertion failure; errors are more easily dismissed as environment issues.
    ]
    for rel, pat in checks:
        if re.search(pat, _src(rel)):
            revived.append(f"{rel} ← {pat!r}")
    assert not revived, "跨义兜底复活:\n" + "\n".join(revived)


def test_prefetch_old_name_stays_dead() -> None:
    """批1/c9 (impl review P0): `_prefetch_workspace_meta` 改名时 lead_orchestrator.py 的 lazy import 漏改，
    ImportError 被 blanket except 吞掉、静默失效。2026-10-17 预取已整体删除（datarc 数据只经工具到模型）；
    旧名仍禁复活。lazy import + 宽 except 的组合让运行时测试抓不到，只能源码级 pin。"""
    revived: list[str] = []
    for py in sorted((_PKGS / "bowhead_core/src/bowhead_core").rglob("*.py")):
        src = py.read_text(encoding="utf-8")
        if re.search(r"_prefetch_workspace_meta\b", src):
            revived.append(str(py.relative_to(_PKGS)))
    assert not revived, "旧名 _prefetch_workspace_meta 复活:\n" + "\n".join(revived)


def test_deprecated_workspaces_list_route_stays_deleted() -> None:
    """批1/c6: `GET /api/v1/workspaces[/]`（datarc id 冒充 workspace id 的列表路由）已删，
    canonical 空间列表 = /auth/me/workspaces。

    2026-08-07：`routes/workspaces.py` **整个文件**已删除（它最后剩的端点
    `GET /{workspace_id}/suggestions` 自 `9e186d5d2` 起恒 503）。文件不存在是比
    「文件里没有那个路由」更强的保证，所以本闸升级为「文件不许复活」——
    真要重建空间路由模块，必须重新审视 c6 这条废弃约定，而不是默默复活。
    """
    path = _PKGS / "bowhead_gateway/src/bowhead_gateway/routes/workspaces.py"
    assert not path.exists(), (
        "routes/workspaces.py 复活了。若确需重建：canonical 空间列表仍是 "
        "/auth/me/workspaces，禁止再出 `GET /api/v1/workspaces[/]`（c6：datarc id "
        "冒充 workspace id），且不要重蹈 `Depends(_datarc)` 读 `app.state.datarc` 的覆辙。"
    )


# ─── Batch 4: source guard for batch 3 renaming (executable terminology.md) ─────


# The only qualified spellings of "workspace" allowed in zh copy: the Datarc
# workspace, plus a third-party product's own workspace, which must carry the
# product name so readers never mistake it for a Bowhead space. Exempted by
# spelling, not by key: the same key still fails if it later drops the qualifier.
_QUALIFIED_WORKSPACE_TERMS = ("北极九章工作区", "Slack 工作区", "Power BI 工作区")


def test_i18n_zh_a_face_has_no_bare_workspace() -> None:
    """zh user-facing copy must not use a bare 「工作区」: a Bowhead space is 「空间」, a Datarc
    workspace is 「北极九章工作区」, and third-party workspaces are product-qualified; see
    _QUALIFIED_WORKSPACE_TERMS."""
    import json

    msgs = json.loads(_src("bowhead_frontend/messages/zh-CN.json"))
    bad: list[str] = []

    def walk(obj: object, path: str = "") -> None:
        if isinstance(obj, dict):
            for k, v in obj.items():
                walk(v, f"{path}.{k}" if path else k)
        elif isinstance(obj, str) and "工作区" in obj:
            # Every occurrence must belong to one of the qualified spellings.
            if obj.count("工作区") != sum(obj.count(term) for term in _QUALIFIED_WORKSPACE_TERMS):
                bad.append(path)

    walk(msgs)
    assert not bad, f"zh-CN.json 裸「工作区」({len(bad)}): {bad[:8]} — A 面写「空间」，B 面写「北极九章工作区」（docs/terminology.md）"


def test_fe_localstorage_ws_key_single_chokepoint() -> None:
    """FE 禁直读/写 localStorage 'bowhead.workspace_id'——唯一入口是
    lib/space-storage.ts（批3/c2 收口；wire 4+ = 统一惯例）。"""
    fe = _PKGS / "bowhead_frontend"
    offenders: list[str] = []
    for sub in ("lib", "components", "app"):
        for f in (fe / sub).rglob("*.ts*"):
            if ".test." in f.name or f.name == "space-storage.ts":
                continue
            if "bowhead.workspace_id" in f.read_text(encoding="utf-8"):
                offenders.append(str(f.relative_to(fe)))
    assert not offenders, f"绕过 space-storage 直用旧 LS 键: {offenders}"


def test_be_ws_header_readers_are_dual_read() -> None:
    """BE 读 X-Workspace-Id 的行必须同时读 X-Space-Id（批3/c2 双读契约）——
    新增裸读者=打破 header 迁移。"""
    gw = _PKGS / "bowhead_gateway" / "src"
    offenders: list[str] = []
    for f in gw.rglob("*.py"):
        for i, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            if 'headers.get("X-Workspace-Id")' in line and "X-Space-Id" not in line:
                offenders.append(f"{f.name}:{i}")
    assert not offenders, f"X-Workspace-Id 裸读（缺 X-Space-Id 双读）: {offenders}"


def test_fe_ws_header_senders_send_both() -> None:
    """FE 发 X-Workspace-Id 的文件必须同发 X-Space-Id（space-storage 双发口径）。"""
    fe = _PKGS / "bowhead_frontend"
    offenders: list[str] = []
    for sub in ("lib", "components", "app"):
        for f in (fe / sub).rglob("*.ts*"):
            if ".test." in f.name:
                continue
            src = f.read_text(encoding="utf-8")
            if '"X-Workspace-Id"' in src and '"X-Space-Id"' not in src:
                offenders.append(str(f.relative_to(fe)))
    assert not offenders, f"只发旧 header 的文件: {offenders}"


def test_kb_scope_no_bare_workspace_equality() -> None:
    """KB 面禁单值比较 scope == 'workspace'——canonical='space'，
    迁移窗口必须双认 in ('space','workspace')（批3/c1）。"""
    offenders: list[str] = []
    for rel in (
        "bowhead_core/src/bowhead_core/kb/files_store.py",
        "bowhead_core/src/bowhead_core/kb/permission.py",
        "bowhead_core/src/bowhead_core/kb/store.py",
        "bowhead_gateway/src/bowhead_gateway/routes/kb.py",
        "bowhead_gateway/src/bowhead_gateway/routes/kb_presign.py",
    ):
        for i, line in enumerate(_src(rel).splitlines(), 1):
            if re.search(r'scope\s*==\s*["\']workspace["\']', line):
                offenders.append(f"{rel}:{i}")
    assert not offenders, f"scope 单值比较旧拼写: {offenders}"


# Verbatim terms from third-party admin consoles; users look them up there, so
# they cannot be rewritten to "team". Whole terms only: a bare "organization"
# still fails.
_THIRD_PARTY_ORG_TERMS = ("组织应用目录",)  # Microsoft Teams admin center app catalog


def test_i18n_zh_has_no_org_wording() -> None:
    """zh user-facing copy must not use 「组织」: the Bowhead tenant is always 「团队」
    (概念术语.md section 1.1; org->team wording must not come back). Third-party verbatim terms
    are listed in _THIRD_PARTY_ORG_TERMS."""
    import json

    msgs = json.loads(_src("bowhead_frontend/messages/zh-CN.json"))
    bad: list[str] = []

    def walk(obj: object, path: str = "") -> None:
        if isinstance(obj, dict):
            for k, v in obj.items():
                walk(v, f"{path}.{k}" if path else k)
        elif isinstance(obj, str) and "组织" in obj:
            rest = obj
            for term in _THIRD_PARTY_ORG_TERMS:
                rest = rest.replace(term, "")
            if "组织" in rest:
                bad.append(path)

    walk(msgs)
    assert not bad, f"zh-CN.json 出现「组织」({len(bad)}): {bad[:8]} — 用户面统一「团队」（docs/design/概念术语.md）"


def test_i18n_en_has_no_an_team_typo() -> None:
    """概念术语审计批3: org→team 机器替换的 'an team' 语法残次不得回流。"""
    assert "an team" not in _src("bowhead_frontend/messages/en-US.json")
