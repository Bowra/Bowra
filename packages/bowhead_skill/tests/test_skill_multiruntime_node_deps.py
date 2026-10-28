"""skill-multiruntime S2 —— curated node 预置集的防漂移 / 供应链 UT（零 docker，进 make ci）.

四组闸（s2-tech-design §3.4）：
1. SSOT ``guaranteed_node_packages`` ↔ ``node_deps/package.json`` 完全一致（精确 pin）；
2. lockfile trust-root：每 entry sha512 + resolved 钉 registry.npmjs.team + 无 bundleDeps，
   顶层版本与 SSOT 交叉（逮"改了 package.json 忘 regen lockfile"，早于镜像 build）；
3. 传递树快照：包名全集写死——传递树换血必过 review，堵"只看顶层"的静默通道；
4. frozen floor：v1 五包恒在契约集（防手滑删除；只增不删是 process 规则非自动闸，
   安全事件豁免见 design §2.1）。

镜像侧的同一契约由 image-contract build gate 断言（build-sandbox-image.sh，含运行时
walk-up 解析探针）；本文件是 repo 侧的另一半。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, cast

from bowhead_skill import guaranteed_node_packages

_NODE_DEPS_DIR = Path(__file__).resolve().parents[1] / "catalog" / "python_sandbox" / "node_deps"
_EXACT_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")

# v1 contract set (frozen floor): additions only; any removal requires explicitly updating this set + design review.
_V1_FLOOR = {"pptxgenjs", "jszip", "lodash", "papaparse", "dayjs"}

# Lockfile transitive-tree snapshot (generated on 2026-07-15 against registry.npmjs.team; 22 packages).
# When bumping the curated set, regenerate the lockfile with s2-tech-design §3.2 commands and update the full set here;
# the diff is visible during review, which is the purpose of this snapshot.
_LOCK_TREE_SNAPSHOT = {
    "@types/node",
    "core-util-is",
    "dayjs",
    "https",
    "image-size",
    "immediate",
    "inherits",
    "isarray",
    "jszip",
    "lie",
    "lodash",
    "pako",
    "papaparse",
    "pptxgenjs",
    "process-nextick-args",
    "queue",
    "readable-stream",
    "safe-buffer",
    "setimmediate",
    "string_decoder",
    "undici-types",
    "util-deprecate",
}


def _package_json() -> dict[str, Any]:
    return cast("dict[str, Any]", json.loads((_NODE_DEPS_DIR / "package.json").read_text(encoding="utf-8")))


def _lock() -> dict[str, Any]:
    return cast("dict[str, Any]", json.loads((_NODE_DEPS_DIR / "package-lock.json").read_text(encoding="utf-8")))


def _lock_entries() -> dict[str, dict[str, Any]]:
    """lockfile v3 ``packages`` map，剔除根 entry（key=""），key 归一为包名."""
    out: dict[str, dict[str, Any]] = {}
    for key, entry in _lock()["packages"].items():
        if not key:
            continue
        out[key.removeprefix("node_modules/")] = entry
    return out


# ── Gate 1: SSOT ↔ package.json ────────────────────────────────────────
def test_ssot_matches_package_json_exactly() -> None:
    ssot = {e["name"]: e["version"] for e in guaranteed_node_packages()}
    deps = _package_json()["dependencies"]
    assert ssot == deps, "SSOT guaranteed_node_packages 与 node_deps/package.json 漂移"


def test_package_json_has_no_other_dependency_kinds() -> None:
    pkg = _package_json()
    for kind in ("devDependencies", "optionalDependencies", "peerDependencies"):
        assert not pkg.get(kind), f"{kind} 必须为空——curated 契约只走 dependencies"


def test_versions_are_exact_pins() -> None:
    for name, ver in _package_json()["dependencies"].items():
        assert _EXACT_VERSION_RE.match(ver), f"{name}: {ver} 不是精确 pin（禁 ^/~ 浮动）"
    for e in guaranteed_node_packages():
        assert _EXACT_VERSION_RE.match(e["version"]), f"SSOT {e['name']}: {e['version']} 非精确 pin"


# ── Gate 2: lockfile trust root ────────────────────────────────────────
def test_lockfile_every_entry_pinned_to_npmjs_with_sha512() -> None:
    for name, entry in _lock_entries().items():
        resolved = entry.get("resolved", "")
        integrity = entry.get("integrity", "")
        assert resolved.startswith("https://registry.npmjs.org/"), (
            f"{name}: resolved={resolved!r} 必须钉 registry.npmjs.team"
            "（integrity 锚 = 官方 registry；CN 镜像靠 replace-registry-host 换源，"
            "对镜像生成的 lockfile 会把 fallback 纸面化——s2-tech-design §3.2）"
        )
        assert integrity.startswith("sha512-"), f"{name}: integrity 缺失或非 sha512"
        assert "bundleDependencies" not in entry and "bundledDependencies" not in entry, f"{name}: bundled dependencies 绕过 lockfile 可见性，禁"


def test_lockfile_top_level_versions_match_ssot() -> None:
    entries = _lock_entries()
    for e in guaranteed_node_packages():
        assert e["name"] in entries, f"{e['name']} 不在 lockfile——package.json 改了忘 regen？"
        got = entries[e["name"]].get("version")
        assert got == e["version"], f"{e['name']}: lockfile {got} != SSOT {e['version']}"


# ── Gate 3: transitive-tree snapshot ───────────────────────────────────
def test_lockfile_tree_snapshot() -> None:
    assert set(_lock_entries()) == _LOCK_TREE_SNAPSHOT, (
        "lockfile 传递树变了——若是有意 bump：regen lockfile 后更新 _LOCK_TREE_SNAPSHOT，让 diff 在 review 里可见；若非有意：查供应链"
    )


# ── Gate 4: frozen floor ───────────────────────────────────────────────
def test_v1_packages_never_removed() -> None:
    names = {e["name"] for e in guaranteed_node_packages()}
    assert names >= _V1_FLOOR, f"v1 契约包被移除: {_V1_FLOOR - names}——curated 集只增不删（安全豁免走 design §2.1 流程，需同时改此 floor + 记录）"


# ── Accessor shape ────────────────────────────────────────────────────
def test_accessor_shape() -> None:
    pkgs = guaranteed_node_packages()
    assert pkgs, "guaranteed_node_packages 不可为空"
    for e in pkgs:
        assert set(e) == {"name", "version"}
