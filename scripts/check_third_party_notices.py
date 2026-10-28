#!/usr/bin/env python3
"""Generate and verify the third-party license notices.

--write  Inventory the lockfiles and Dockerfiles, read license metadata of locally
         installed packages (never touches the network), and regenerate
         THIRD_PARTY_NOTICES.md plus the license review report under docs/.
--check  Compute the dependency fingerprint from lockfiles and Dockerfiles only and
         compare it with the fingerprint recorded in both generated files. Needs no
         node_modules or .venv; used by `make lint`.

See docs/third-party-licenses.en-US.md for the workflow.
"""

from __future__ import annotations

import argparse
import base64
import email.parser
import hashlib
import io
import json
import re
import sys
import tarfile
import tomllib
from collections import defaultdict, deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
FRONTEND_DIR = ROOT / "packages/bowhead_frontend"
SANDBOX_NODE_DIR = ROOT / "packages/bowhead_skill/catalog/python_sandbox/node_deps"
UV_LOCK = ROOT / "uv.lock"
COMPONENTS_FILE = ROOT / "config/third_party/image_components.json"
DOCKERFILES = {
    "bowhead-app": ROOT / "deploy/Dockerfile.app",
    "bowhead-sandbox": ROOT / "packages/bowhead_skill/catalog/python_sandbox/Dockerfile",
    "bowhead-sandbox-executor": ROOT / "deploy/sandbox-executor/Dockerfile",
}
NOTICES_OUT = ROOT / "THIRD_PARTY_NOTICES.md"
# Preserve the existing Chinese report path alongside the default English report.
REVIEW_OUT = ROOT / "docs/第三方许可审查.md"
REVIEW_EN_OUT = ROOT / "docs/third-party-license-review.md"
WORKFLOW_DOC = "docs/third-party-licenses.en-US.md"
FINGERPRINT_RE = re.compile(r"<!-- third-party-inventory: (sha256:[0-9a-f]{64}) -->")
LICENSE_TEXTS_RE = re.compile(r"<!-- third-party-license-texts: (\d+) -->")
TEXTS_HEADING = "## Copyright Notices and License Texts"
MISSING_HEADING = "### Components without a bundled license text"
REGENERATE_HINT = "make install-frontend && uv sync --all-packages --no-dev, then make third-party-notices"
# Bump whenever the generated output changes for the same inputs, so --check asks for a regeneration.
GENERATOR_VERSION = 7

# Package manager -> (install subcommand, inventory bucket).
PACKAGE_MANAGERS = {
    "apt-get": ("install", "system"),
    "apt": ("install", "system"),
    "apk": ("add", "system"),
    "pip": ("install", "pip"),
    "pip3": ("install", "pip"),
}
PIP_FORBIDDEN_FLAGS = {"-r", "--requirement", "-c", "--constraint", "-e", "--editable"}
PIP_VALUE_FLAGS = {
    "-i",
    "--index-url",
    "--extra-index-url",
    "-f",
    "--find-links",
    "--trusted-host",
    "--timeout",
    "--retries",
    "-t",
    "--target",
    "--prefix",
    "--root",
    "--cache-dir",
    "--platform",
    "--python-version",
}

NPM_CACHE = Path.home() / ".npm/_cacache"
UV_ARCHIVE = Path.home() / ".cache/uv/archive-v0"

SCOPE_FRONTEND = "frontend"
SCOPE_BACKEND = "backend"
SCOPE_SANDBOX_NODE = "sandbox-node"
SCOPE_VENDORED = "vendored"

LICENSE_FILE_RE = re.compile(r"^(licen[cs]e|copying|notice|unlicense)([.-].*)?$", re.IGNORECASE)
# Strong/weak copyleft and restrictive licenses that must be reviewed one by one.
ATTENTION_PATTERNS = [
    ("AGPL", re.compile(r"\bAGPL", re.IGNORECASE)),
    ("LGPL", re.compile(r"\bLGPL", re.IGNORECASE)),
    ("GPL", re.compile(r"(?<![AL])GPL", re.IGNORECASE)),
    ("SSPL", re.compile(r"\bSSPL", re.IGNORECASE)),
    ("BUSL", re.compile(r"\bBUSL|Business Source", re.IGNORECASE)),
]
UNKNOWN_LICENSES = {"", "UNKNOWN", "UNLICENSED", "SEE LICENSE IN LICENSE", "LicenseRef-Unknown"}
# Long standard licenses whose text is identical across packages: the text is printed
# once and each package only contributes its copyright lines and NOTICE files.
LONG_STANDARD_MARKERS = {
    "Apache-2.0": "TERMS AND CONDITIONS FOR USE, REPRODUCTION, AND DISTRIBUTION",
    "MPL-2.0": "Mozilla Public License Version 2.0",
    "LGPL": "GNU LESSER GENERAL PUBLIC LICENSE",
    "GPL": "GNU GENERAL PUBLIC LICENSE",
    "EPL-2.0": "Eclipse Public License - v 2.0",
    "CC-BY-4.0": "Attribution 4.0 International",
}
LICENSE_ALIASES = {
    "apache 2.0": "Apache-2.0",
    "apache-2": "Apache-2.0",
    "apache license 2.0": "Apache-2.0",
    "apache license, version 2.0": "Apache-2.0",
    "apache software license": "Apache-2.0",
    "apache 2": "Apache-2.0",
    "apache": "Apache-2.0",
    "mit license": "MIT",
    "the mit license": "MIT",
    "mit": "MIT",
    "bsd": "BSD-3-Clause",
    "bsd license": "BSD-3-Clause",
    "new bsd": "BSD-3-Clause",
    "new bsd license": "BSD-3-Clause",
    "3-clause bsd license": "BSD-3-Clause",
    "bsd 3-clause": "BSD-3-Clause",
    "bsd-3": "BSD-3-Clause",
    "isc license": "ISC",
    "isc license (iscl)": "ISC",
    "psf": "PSF-2.0",
    "psf license": "PSF-2.0",
    "python software foundation license": "PSF-2.0",
    "mozilla public license 2.0 (mpl 2.0)": "MPL-2.0",
    "mpl 2.0": "MPL-2.0",
    "the unlicense (unlicense)": "Unlicense",
    "hpnd": "HPND",
    "modified bsd license": "BSD-3-Clause",
    "apache license version 2.0": "Apache-2.0",
    "psfl": "PSF-2.0",
}
SPDX_EXPRESSION_RE = re.compile(r"^[\w.+-]+( (AND|OR|WITH) [\w.+-]+)*$")
CLASSIFIER_LICENSES = {
    "MIT License": "MIT",
    "BSD License": "BSD-3-Clause",
    "Apache Software License": "Apache-2.0",
    "ISC License (ISCL)": "ISC",
    "Python Software Foundation License": "PSF-2.0",
    "Mozilla Public License 2.0 (MPL 2.0)": "MPL-2.0",
    "The Unlicense (Unlicense)": "Unlicense",
    "GNU Lesser General Public License v3 (LGPLv3)": "LGPL-3.0-only",
    "GNU Lesser General Public License v2 or later (LGPLv2+)": "LGPL-2.1-or-later",
    "GNU Library or Lesser General Public License (LGPL)": "LGPL-2.1-or-later",
    "GNU General Public License v3 (GPLv3)": "GPL-3.0-only",
    "GNU General Public License v2 (GPLv2)": "GPL-2.0-only",
    "Historical Permission Notice and Disclaimer (HPND)": "HPND",
    "Zope Public License": "ZPL-2.1",
}

# ─── Review report copy ──────────────────────────────────────────────────
# The review report lives in the Chinese docs tree, so its prose is Chinese.
# All Chinese output text of this script is kept in this single block.
REVIEW_COPY = {
    "title": "# 第三方许可审查",
    "generated": "本文件由 `scripts/check_third_party_notices.py --write` 生成，请勿手改；更新方法与处置流程见 [第三方许可.md](第三方许可.md)。",
    "intro": "列出许可证属于 GPL、AGPL、LGPL、SSPL、BUSL，或许可证缺失、未知、需逐项核对的依赖。此处只做登记，是否替换或移除由负责人决定。",
    "reading_hint": (
        "阅读提示：前端依赖按 package-lock.json 的生产依赖全集列出，其中标注 platform binary 的只在对应平台安装；"
        "应用镜像只携带 Next.js 静态导出产物与 playwright / playwright-core，其余前端 npm 包（如 sharp、@next/swc）不随镜像分发。"
        "「可选宽松许可」表示表达式里有 OR 分支可选 MIT 等宽松许可。"
    ),
    "total": "共 {count} 项。",
    "entry": "- **{category}** · `{name}` {version} · `{license}` · {scope} · 引入：{introduced}{note}",
    "missing_license": "（缺失）",
    "separator": "；",
    "image_notes_heading": "## 镜像构建注意事项",
    "note_usr_share_doc_removed": (
        "`deploy/Dockerfile.app` 安装系统包后执行 `rm -rf /usr/share/doc`，同时删掉了各 Debian 包的 `copyright` 文件；"
        "镜像内含 GPL 组件（如 git、poppler-utils、fonts-wqy-zenhei），分发镜像时需另行随附其许可原文。"
    ),
    "note_debian_copyright_kept": (
        "`deploy/Dockerfile.app` 清理文档时保留各 Debian 包的 `/usr/share/doc/<包名>/copyright`，并在构建时断言 git 与 poppler-utils 的版权文件存在；"
        "GPL 组件的对应源码获取方式见 [第三方许可.md](第三方许可.md) 的「镜像内系统包的许可原文与源码」。"
    ),
    "note_sandbox_pip_unlocked": "python_sandbox 镜像的 pip 包只固定了顶层版本，传递依赖在构建时解析，未进入本清单；发版前可在镜像内用 `pip list` 与包元数据复核。",
    "categories": {
        "missing_or_unknown": "许可证缺失或未知",
        "field_missing": "许可证字段缺失（原文推断为 {license}）",
        "not_spdx": "许可证字段非 SPDX 表达式（需人工确认）",
        "permissive_alternative": "（可选宽松许可）",
        "mixed": "混合许可（需逐项核对）",
    },
    "scopes": {
        SCOPE_FRONTEND: "前端生产依赖",
        SCOPE_BACKEND: "后端运行时依赖",
        SCOPE_SANDBOX_NODE: "sandbox 预置 Node 包",
        SCOPE_VENDORED: "前端源码内嵌",
    },
}
REVIEW_COPY_EN = {
    "title": "# Third-party license review",
    "generated": "Generated by `scripts/check_third_party_notices.py --write`; do not edit by hand. See [license maintenance](third-party-licenses.en-US.md). [简体中文](第三方许可审查.md)",
    "intro": "This inventory records GPL, AGPL, LGPL, SSPL, BUSL, missing, unknown, and mixed license entries for individual review. Inclusion records a dependency; it does not approve redistribution.",
    "reading_hint": "Frontend entries include all production dependencies in package-lock.json. Platform binaries install only on matching platforms. The application image carries the Next.js static export and Playwright runtime; other frontend build dependencies are not automatically distributed in that image. A permissive alternative means an OR branch permits choosing a license such as MIT.",
    "total": "{count} entries require review.",
    "entry": "- **{category}** · `{name}` {version} · `{license}` · {scope} · Introduced by: {introduced}{note}",
    "missing_license": "(missing)",
    "separator": "; ",
    "image_notes_heading": "## Image build considerations",
    "note_usr_share_doc_removed": "`deploy/Dockerfile.app` removes `/usr/share/doc`, including Debian copyright files. Distributing the image requires preserving the license texts for its GPL components separately.",
    "note_debian_copyright_kept": "`deploy/Dockerfile.app` preserves each Debian package's `/usr/share/doc/<package>/copyright` and checks the files for git and poppler-utils. See [license maintenance](third-party-licenses.en-US.md#license-texts-and-source-for-image-system-packages) for corresponding source.",
    "note_sandbox_pip_unlocked": "The Python sandbox pins top-level pip packages; build-resolved transitive dependencies are outside this inventory. Review installed package metadata before distributing an image.",
    "categories": {
        "missing_or_unknown": "Missing or unknown license",
        "field_missing": "License field missing (text indicates {license})",
        "not_spdx": "Non-SPDX license field (manual review required)",
        "permissive_alternative": " (permissive alternative available)",
        "mixed": "Mixed licenses (component review required)",
    },
    "scopes": {
        SCOPE_FRONTEND: "Frontend production dependency",
        SCOPE_BACKEND: "Backend runtime dependency",
        SCOPE_SANDBOX_NODE: "Sandbox Node dependency",
        SCOPE_VENDORED: "Vendored source material",
    },
}
# Ranking of review categories, most severe first.
CATEGORY_ORDER = ["AGPL", "SSPL", "BUSL", "GPL", "LGPL", "missing_or_unknown", "field_missing", "not_spdx", "mixed"]


class MissingMetadataError(RuntimeError):
    """License metadata needed by --write is not available offline."""


@dataclass
class Dep:
    ecosystem: str  # npm / pypi / system / image / vendored
    name: str
    version: str
    license: str
    scope: str  # SCOPE_* constant or an image name such as "bowhead-app"
    introduced_by: list[str] = field(default_factory=list)
    license_texts: list[tuple[str, str]] = field(default_factory=list)  # (file name, text)
    note: str = ""
    license_source: str = ""
    declared: bool = True  # False = package metadata has no license field; license inferred from its text


# ─── Lockfile parsing (the only part --check depends on) ────────────────────


def _npm_name(path_key: str, entry: dict[str, Any]) -> str:
    return entry.get("name") or path_key.rsplit("node_modules/", 1)[-1]


def _npm_lock_license(entry: dict[str, Any]) -> str:
    value = entry.get("license")
    if isinstance(value, dict):
        value = value.get("type")
    return str(value or "")


def _npm_production_entries(lock_path: Path) -> dict[str, dict[str, Any]]:
    """Entries installed by `npm ci --omit=dev` from a v3 package-lock.

    Platform optionals and `devOptional` entries are included: `--omit=dev` only
    skips entries flagged `dev`.
    """
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    return {key: entry for key, entry in lock["packages"].items() if key and not entry.get("dev") and not entry.get("link")}


def _npm_resolve(parent_key: str, dep: str, entries: dict[str, dict[str, Any]]) -> str | None:
    """Resolve `dep` from `parent_key` using node's walk-up node_modules lookup."""
    base = parent_key
    while True:
        candidate = f"{base}/node_modules/{dep}" if base else f"node_modules/{dep}"
        if candidate in entries:
            return candidate
        if not base:
            return None
        idx = base.rfind("/node_modules/")
        base = base[:idx] if idx >= 0 else ""


def _npm_introducers(lock_path: Path, entries: dict[str, dict[str, Any]]) -> dict[str, list[str]]:
    """Map every lock entry to the shortest chain from a top-level direct dependency."""
    manifest = json.loads((lock_path.parent / "package.json").read_text(encoding="utf-8"))
    chains: dict[str, list[str]] = {}
    queue: deque[str] = deque()
    for dep in sorted({**manifest.get("dependencies", {}), **manifest.get("optionalDependencies", {})}):
        key = _npm_resolve("", dep, entries)
        if key and key not in chains:
            chains[key] = [dep]
            queue.append(key)
    while queue:
        key = queue.popleft()
        entry = entries[key]
        for dep in sorted({**entry.get("dependencies", {}), **entry.get("optionalDependencies", {}), **entry.get("peerDependencies", {})}):
            child = _npm_resolve(key, dep, entries)
            if child and child not in chains:
                chains[child] = [*chains[key], dep]
                queue.append(child)
    return chains


def _norm_py(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _python_markers() -> dict[tuple[str, str], set[str]]:
    """Environment markers on every edge; a single unmarked edge means installed on all platforms."""
    lock = tomllib.loads(UV_LOCK.read_text(encoding="utf-8"))
    markers: dict[tuple[str, str], set[str]] = defaultdict(set)
    for pkg in lock["package"]:
        edges = list(pkg.get("dependencies", [])) + [d for deps in pkg.get("optional-dependencies", {}).values() for d in deps]
        for dep in edges:
            markers[(_norm_py(dep["name"]), dep.get("version", ""))].add(dep.get("marker", ""))
    return markers


def _python_runtime_packages() -> tuple[list[dict[str, Any]], dict[tuple[str, str], list[str]]]:
    """Runtime closure of all workspace members in uv.lock (no dev groups, no unrequested extras)."""
    lock = tomllib.loads(UV_LOCK.read_text(encoding="utf-8"))
    members = {_norm_py(n) for n in lock["manifest"]["members"]}
    by_name: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for pkg in lock["package"]:
        by_name[_norm_py(pkg["name"])].append(pkg)

    def resolve(dep: dict[str, Any]) -> dict[str, Any]:
        candidates = by_name[_norm_py(dep["name"])]
        if "version" in dep:
            candidates = [p for p in candidates if p["version"] == dep["version"]]
        if len(candidates) != 1:
            raise ValueError(f"ambiguous uv.lock dependency: {dep}")
        return candidates[0]

    chains: dict[tuple[str, str], list[str]] = {}
    seen_nodes: set[tuple[str, str, str]] = set()
    queue: deque[tuple[dict[str, Any], str, list[str]]] = deque()
    for member in sorted(members):
        for pkg in by_name[member]:
            queue.append((pkg, "", [pkg["name"]]))
    while queue:
        pkg, extra, chain = queue.popleft()
        node = (_norm_py(pkg["name"]), pkg.get("version", ""), extra)
        if node in seen_nodes:
            continue
        seen_nodes.add(node)
        deps = pkg.get("optional-dependencies", {}).get(extra, []) if extra else pkg.get("dependencies", [])
        for dep in deps:
            target = resolve(dep)
            target_key = (_norm_py(target["name"]), target["version"])
            target_chain = [*chain, target["name"]]
            if target_key[0] not in members and target_key not in chains:
                chains[target_key] = target_chain
            queue.append((target, "", target_chain))
            for dep_extra in dep.get("extra", []):
                queue.append((target, dep_extra, target_chain))
    packages = []
    for pkg in lock["package"]:
        key = (_norm_py(pkg["name"]), pkg["version"])
        if key in chains:
            packages.append(pkg)
    return packages, chains


def _dockerfile_instructions(path: Path) -> list[str]:
    lines: list[str] = []
    current = ""
    for raw in path.read_text(encoding="utf-8").splitlines():
        stripped = raw.strip()
        if stripped.startswith("#"):
            continue
        if stripped.endswith("\\"):
            current += stripped[:-1] + " "
            continue
        current += stripped
        if current.strip():
            lines.append(current.strip())
        current = ""
    return lines


def _parse_install_args(manager: str, target: str, args: list[str], errors: list[str], result: dict[str, list[str]]) -> None:
    skip_next = False
    for token in args:
        token = token.strip("'\"")
        if skip_next:
            skip_next = False
            continue
        if target == "pip":
            flag = token.split("=", 1)[0]
            if flag in PIP_FORBIDDEN_FLAGS:
                errors.append(f"`{manager} install {token}` installs packages the parser cannot enumerate; pin each package with ==")
                continue
            if token.startswith("-"):
                skip_next = token in PIP_VALUE_FLAGS
                continue
            if "==" not in token or token.startswith("$"):
                errors.append(f"`{manager} install {token}` is not an exact `name==version` pin")
                continue
        elif token.startswith(("-", "$")):
            continue
        result[target].append(token)


def _parse_dockerfile(path: Path) -> dict[str, list[str]]:
    """Extract base images, apt/apk system packages and pinned pip packages.

    Installs the parser cannot enumerate are reported under "errors": non-exact pip
    requirements, requirement files, and global npm or `curl | sh` style installers
    that are not registered in image_components.json.
    """
    installer_matches = [
        match for component in json.loads(COMPONENTS_FILE.read_text(encoding="utf-8"))["components"] for match in component.get("installer_match", [])
    ]
    result: dict[str, list[str]] = {"base_images": [], "system": [], "pip": [], "errors": []}
    for instruction in _dockerfile_instructions(path):
        keyword, _, rest = instruction.partition(" ")
        if keyword.upper() == "FROM":
            image = rest.split()[0]
            if "${DOCKER_REGISTRY}" in image:
                result["base_images"].append(image.replace("${DOCKER_REGISTRY}", ""))
            continue
        if keyword.upper() != "RUN":
            continue
        for segment in re.split(r"&&|;|\|\|", rest):
            tokens = segment.split()
            global_npm = len(tokens) > 1 and "npm" in tokens and any(t in {"-g", "--global"} for t in tokens)
            if (global_npm or re.search(r"\|\s*(sudo\s+)?(sh|bash)\b", segment)) and not any(m in segment for m in installer_matches):
                result["errors"].append(f"unregistered installer `{segment.strip()}`; register it in components[].installer_match")
            for i, token in enumerate(tokens):
                if token not in PACKAGE_MANAGERS:
                    continue
                subcommand, target = PACKAGE_MANAGERS[token]
                j = i + 1
                while j < len(tokens) and tokens[j].startswith("-"):
                    j += 1
                if j < len(tokens) and tokens[j] == subcommand:
                    _parse_install_args(token, target, tokens[j + 1 :], result["errors"], result)
    for key in result:
        result[key] = sorted(set(result[key]))
    return result


def _select_dockerfiles(names: object) -> dict[str, Path]:
    """Select reviewed build contexts from the fixed image inventory."""
    if not isinstance(names, list) or not names or any(not isinstance(name, str) or name not in DOCKERFILES for name in names):
        raise ValueError("image_contexts must list known image inventory names")
    if len(set(names)) != len(names) or "bowhead-app" not in names:
        raise ValueError("image_contexts must be unique and include the application")
    return {name: DOCKERFILES[name] for name in names}


def _inventory_dockerfiles() -> dict[str, Path]:
    registry = json.loads(COMPONENTS_FILE.read_text(encoding="utf-8"))
    return _select_dockerfiles(registry.get("image_contexts", list(DOCKERFILES)))


def inventory_fingerprint() -> str:
    """Fingerprint determined only by lockfiles, Dockerfiles and the component registry."""
    payload: dict[str, Any] = {}
    for label, directory in (("npm-frontend", FRONTEND_DIR), ("npm-sandbox", SANDBOX_NODE_DIR)):
        entries = _npm_production_entries(directory / "package-lock.json")
        payload[label] = sorted([_npm_name(k, v), v.get("version", ""), _npm_lock_license(v)] for k, v in entries.items())
    packages, _ = _python_runtime_packages()
    payload["pypi"] = sorted([p["name"], p["version"]] for p in packages)
    payload["images"] = {image: _parse_dockerfile(path) for image, path in _inventory_dockerfiles().items()}
    payload["components"] = json.loads(COMPONENTS_FILE.read_text(encoding="utf-8"))
    payload["generator_version"] = GENERATOR_VERSION
    payload["license_files"] = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted((COMPONENTS_FILE.parent / "licenses").glob("*"))}
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"


def _unregistered_image_components() -> list[str]:
    registry = json.loads(COMPONENTS_FILE.read_text(encoding="utf-8"))
    registry_path = COMPONENTS_FILE.relative_to(ROOT)
    problems = []
    for image, path in _inventory_dockerfiles().items():
        parsed = _parse_dockerfile(path)
        problems += [f"{image}: {error}" for error in parsed["errors"]]
        for name in parsed["system"]:
            if name not in registry["system_packages"]:
                problems.append(f"{image}: system package {name} has no license registered in {registry_path}")
        for pin in parsed["pip"]:
            name = _norm_py(pin.split("==")[0])
            if name not in {_norm_py(n) for n in registry["pip_packages"]}:
                problems.append(f"{image}: pip package {name} has no license registered in {registry_path}")
        for image_ref in parsed["base_images"]:
            if image_ref.split(":")[0] not in registry["base_images"]:
                problems.append(f"{image}: base image {image_ref} has no license registered in {registry_path}")
    return problems


# ─── License metadata (used by --write, fully offline) ─────────────────────


def normalize_license(raw: str) -> str:
    value = (raw or "").strip()
    if not value:
        return ""
    if value.startswith("(") and value.endswith(")") and value.count("(") == 1:
        value = value[1:-1]
    alias = LICENSE_ALIASES.get(value.lower())
    return alias or value


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return path.read_text(encoding="latin-1")


def _license_files_in(directory: Path) -> list[tuple[str, str]]:
    if not directory.is_dir():
        return []
    found = []
    for path in sorted(directory.iterdir()):
        if path.is_file() and LICENSE_FILE_RE.match(path.name):
            found.append((path.name, _read_text(path)))
    return found


def _npm_cache_tarball(integrity: str) -> tarfile.TarFile | None:
    if not integrity or not NPM_CACHE.is_dir():
        return None
    algo, _, digest = integrity.split(" ")[0].partition("-")
    try:
        hex_digest = base64.b64decode(digest).hex()
    except ValueError:
        return None
    content = NPM_CACHE / "content-v2" / algo / hex_digest[:2] / hex_digest[2:4] / hex_digest[4:]
    if not content.is_file():
        return None
    try:
        return tarfile.open(fileobj=io.BytesIO(content.read_bytes()))
    except tarfile.TarError:
        return None


def _npm_package_file(directory: Path, key: str, entry: dict[str, Any], relative: str) -> bytes | None:
    """Read one file of a locked npm package from node_modules or, failing that, the local npm cache."""
    installed = directory / key
    manifest_path = installed / "package.json"
    if manifest_path.is_file() and json.loads(manifest_path.read_text(encoding="utf-8")).get("version") == entry.get("version"):
        target = installed / relative
        return target.read_bytes() if target.is_file() else None
    tarball = _npm_cache_tarball(entry.get("integrity", ""))
    if tarball is None:
        return None
    for member in tarball.getmembers():
        if member.isfile() and member.name.split("/", 1)[-1] == relative:
            extracted = tarball.extractfile(member)
            return extracted.read() if extracted is not None else None
    return None


def _npm_package_info(directory: Path, key: str, entry: dict[str, Any]) -> tuple[str, list[tuple[str, str]], str]:
    """Return (license, license files, source). Prefer node_modules, then the local npm cache, then the lockfile."""
    installed = directory / key / "package.json"
    if installed.is_file():
        manifest = json.loads(installed.read_text(encoding="utf-8"))
        if manifest.get("version") == entry.get("version"):
            license_value = manifest.get("license")
            if isinstance(license_value, dict):
                license_value = license_value.get("type")
            if not license_value and isinstance(manifest.get("licenses"), list):
                license_value = " OR ".join(str(item.get("type", item)) for item in manifest["licenses"] if item)
            texts = _license_files_in(installed.parent)
            return normalize_license(str(license_value or "")), texts, "node_modules"
    tarball = _npm_cache_tarball(entry.get("integrity", ""))
    if tarball is not None:
        texts = []
        license_value = ""
        manifest = {}
        for member in tarball.getmembers():
            parts = member.name.split("/")
            if len(parts) != 2 or not member.isfile():
                continue
            extracted = tarball.extractfile(member)
            if extracted is None:
                continue
            data = extracted.read()
            if parts[1] == "package.json":
                manifest = json.loads(data.decode("utf-8"))
                license_value = manifest.get("license") or ""
                if isinstance(license_value, dict):
                    license_value = license_value.get("type", "")
            elif LICENSE_FILE_RE.match(parts[1]):
                texts.append((parts[1], data.decode("utf-8", errors="replace")))
        return normalize_license(str(license_value or _npm_lock_license(entry))), sorted(texts), "npm cache"
    return normalize_license(_npm_lock_license(entry)), [], "package-lock.json (not installed locally, license text unavailable)"


def _dist_info_dirs() -> dict[tuple[str, str], Path]:
    found: dict[tuple[str, str], Path] = {}
    roots = sorted((ROOT / ".venv/lib").glob("python*/site-packages"))
    roots += sorted(UV_ARCHIVE.glob("*")) if UV_ARCHIVE.is_dir() else []
    for root in roots:
        for dist in root.glob("*.dist-info"):
            name, _, version = dist.name[: -len(".dist-info")].partition("-")
            found.setdefault((_norm_py(name), version), dist)
    return found


def _python_metadata_license(dist: Path) -> tuple[str, list[tuple[str, str]], bool]:
    message = email.parser.Parser().parsestr(_read_text(dist / "METADATA"))
    license_value = message.get("License-Expression") or ""
    raw_license = (message.get("License") or "").strip()
    if not license_value and raw_license and "\n" not in raw_license and len(raw_license) < 80:
        license_value = raw_license
    if not license_value and raw_license:
        # Some packages put the full license text into License; its first line names the license.
        first_line = raw_license.splitlines()[0].strip()
        if first_line.lower() in LICENSE_ALIASES:
            license_value = first_line
    if not license_value:
        classifiers = [c.split("::")[-1].strip() for c in message.get_all("Classifier") or [] if c.startswith("License ::")]
        mapped = [CLASSIFIER_LICENSES.get(c, c) for c in classifiers if c != "OSI Approved"]
        license_value = " OR ".join(dict.fromkeys(mapped))
    texts = _license_files_in(dist)
    licenses_dir = dist / "licenses"
    if licenses_dir.is_dir():
        for path in sorted(licenses_dir.rglob("*")):
            if path.is_file():
                texts.append((str(path.relative_to(licenses_dir)), _read_text(path)))
    if not texts and raw_license and ("\n" in raw_license or len(raw_license) >= 80):
        texts.append(("METADATA License", raw_license))
    license_value = normalize_license(license_value)
    declared = bool(license_value)
    if not license_value and texts:
        license_value = _detect_license_from_text(texts[0][1])
    return license_value, texts, declared


def _detect_license_from_text(text: str) -> str:
    head = text[:3000]
    if "Permission is hereby granted, free of charge" in head:
        return "MIT"
    if "Redistribution and use in source and binary forms" in head:
        return "BSD-3-Clause" if "endorse or promote" in head else "BSD-2-Clause"
    if "Apache License" in head and "Version 2.0" in head:
        return "Apache-2.0"
    if "Permission to use, copy, modify, and/or distribute" in head:
        return "ISC"
    return ""


# ─── Inventory ───────────────────────────────────────────────────────────


def _collect_npm(directory: Path, scope: str) -> list[Dep]:
    lock_path = directory / "package-lock.json"
    entries = _npm_production_entries(lock_path)
    chains = _npm_introducers(lock_path, entries)
    deps = []
    for key, entry in sorted(entries.items()):
        license_value, texts, source = _npm_package_info(directory, key, entry)
        declared = bool(license_value)
        if not license_value and texts:
            license_value = _detect_license_from_text(texts[0][1])
        chain = chains.get(key, [])
        platform = ""
        if entry.get("os") or entry.get("cpu"):
            platform = "platform binary (" + "/".join([*entry.get("os", []), *entry.get("cpu", [])]) + ")"
        deps.append(
            Dep(
                ecosystem="npm",
                name=_npm_name(key, entry),
                version=entry.get("version", ""),
                license=license_value,
                scope=scope,
                introduced_by=[" → ".join(chain)] if chain else ["(not reachable from package.json)"],
                license_texts=texts,
                note=platform,
                license_source=source,
                declared=declared,
            )
        )
    return _dedupe(deps)


def _dedupe(deps: list[Dep]) -> list[Dep]:
    merged: dict[tuple[str, str, str], Dep] = {}
    for dep in deps:
        key = (dep.ecosystem, dep.name, dep.version)
        if key in merged:
            for chain in dep.introduced_by:
                if chain not in merged[key].introduced_by:
                    merged[key].introduced_by.append(chain)
        else:
            merged[key] = dep
    return list(merged.values())


def _collect_python() -> list[Dep]:
    packages, chains = _python_runtime_packages()
    dists = _dist_info_dirs()
    registry = {_norm_py(k): v for k, v in json.loads(COMPONENTS_FILE.read_text(encoding="utf-8"))["pip_packages"].items()}
    markers = _python_markers()
    deps = []
    for pkg in sorted(packages, key=lambda p: _norm_py(p["name"])):
        key = (_norm_py(pkg["name"]), pkg["version"])
        dist = dists.get(key)
        if dist is not None:
            license_value, texts, declared = _python_metadata_license(dist)
            source = "installed package metadata"
        elif key[0] in registry:
            license_value, texts, source, declared = registry[key[0]], [], "image_components.json (not installed locally)", True
        else:
            license_value, texts, source, declared = "", [], "not installed (run uv sync --all-packages --no-dev)", False
        edge_markers = markers.get(key, set()) | markers.get((key[0], ""), set())
        note = ""
        if edge_markers and "" not in edge_markers:
            note = "platform-specific install (" + "; ".join(sorted(edge_markers)) + ")"
        deps.append(
            Dep(
                ecosystem="pypi",
                name=pkg["name"],
                version=pkg["version"],
                license=license_value,
                scope=SCOPE_BACKEND,
                introduced_by=[" → ".join(chains[key])],
                license_texts=texts,
                note=note,
                license_source=source,
                declared=declared,
            )
        )
    return deps


def _collect_images() -> list[Dep]:
    registry = json.loads(COMPONENTS_FILE.read_text(encoding="utf-8"))
    dists = _dist_info_dirs()
    deps: list[Dep] = []
    for image, path in _inventory_dockerfiles().items():
        rel = str(path.relative_to(ROOT))
        parsed = _parse_dockerfile(path)
        for image_ref in parsed["base_images"]:
            name, _, tag = image_ref.partition(":")
            info = registry["base_images"][name]
            deps.append(Dep("image", f"base image {name}", tag, info["license"], image, [rel], note=info.get("note", "")))
        for name in parsed["system"]:
            info = registry["system_packages"][name]
            version = "Alpine repository version (unpinned)" if image == "bowhead-sandbox-executor" else "Debian repository version (unpinned)"
            deps.append(Dep("system", name, version, info["license"], image, [rel], note=info.get("note", "")))
        for pin in parsed["pip"]:
            raw_name, _, version = pin.partition("==")
            name = _norm_py(raw_name)
            license_value = next(v for k, v in registry["pip_packages"].items() if _norm_py(k) == name)
            dist = dists.get((name, version))
            texts: list[tuple[str, str]] = []
            source = "image_components.json"
            if dist is not None:
                meta_license, texts, _ = _python_metadata_license(dist)
                license_value = meta_license or license_value
                source = "cached metadata of the same version"
            deps.append(
                Dep(
                    "pypi",
                    name,
                    version,
                    license_value,
                    image,
                    [rel],
                    texts,
                    note="pip install in image; transitive deps unpinned",
                    license_source=source,
                )
            )
    playwright_key = "node_modules/playwright-core"
    playwright_entry = _npm_production_entries(FRONTEND_DIR / "package-lock.json").get(playwright_key)
    browsers_json = _npm_package_file(FRONTEND_DIR, playwright_key, playwright_entry, "browsers.json") if playwright_entry else None
    if browsers_json is None:
        raise MissingMetadataError("cannot read playwright-core/browsers.json from node_modules or the npm cache; the Chromium version is unknown")
    shell = next(b for b in json.loads(browsers_json)["browsers"] if b["name"] == "chromium-headless-shell")
    deps.append(
        Dep(
            "image",
            "Chromium Headless Shell (downloaded by Playwright)",
            f"{shell['browserVersion']} (revision {shell['revision']})",
            "BSD-3-Clause AND LicenseRef-Mixed",
            "bowhead-app",
            ["deploy/Dockerfile.app: playwright install --only-shell chromium"],
            note="Chromium itself is BSD-3-Clause and bundles many third-party components (including LGPL and MPL); see about:credits",
        )
    )
    for component in registry["components"]:
        deps.append(
            Dep(
                "image",
                component["name"],
                component["version"],
                component["license"],
                component["image"],
                [component["source"]],
                note=component.get("note", ""),
            )
        )
    for vendored in registry["vendored"]:
        text_path = ROOT / vendored["license_file"]
        deps.append(
            Dep(
                "vendored",
                vendored["name"],
                vendored["version"],
                vendored["license"],
                SCOPE_VENDORED,
                [vendored["source"]],
                [(text_path.name, text_path.read_text(encoding="utf-8"))],
                note=vendored.get("note", ""),
                license_source=vendored["license_file"],
            )
        )
    return deps


# ─── Rendering ───────────────────────────────────────────────────────────


def attention_category(license_value: str, *, declared: bool = True) -> tuple[str, str]:
    """Return (category code, license detail) or ("", "") when the license needs no review."""
    value = (license_value or "").strip()
    if not value or value.upper() in {v.upper() for v in UNKNOWN_LICENSES} or value.startswith("LicenseRef-Unknown"):
        return "missing_or_unknown", ""
    if not declared:
        return "field_missing", value
    if not SPDX_EXPRESSION_RE.match(value.replace("(", "").replace(")", "")):
        return "not_spdx", ""
    hits = [label for label, pattern in ATTENTION_PATTERNS if pattern.search(value)]
    if hits:
        alternatives = [part.strip() for part in re.split(r"\bOR\b", value)]
        permissive_option = len(alternatives) > 1 and any(not any(p.search(alt) for _, p in ATTENTION_PATTERNS) for alt in alternatives)
        return "/".join(hits), "permissive_alternative" if permissive_option else ""
    if re.search(r"LicenseRef-(?!Public-Domain)", value):
        return "mixed", ""
    return "", ""


def _copyright_lines(text: str) -> list[str]:
    lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if re.match(r"^(copyright\s*(\(c\)|©|\d{4})|\(c\)\s*\d{4}|©\s*\d{4})", stripped, re.IGNORECASE):
            lines.append(" ".join(stripped.split()))
    return list(dict.fromkeys(lines))


def _standard_long_license(text: str, expression: str = "") -> str | None:
    # Exception clauses are component-specific permissions and must stay with
    # their complete license text, even when the base license is shared.
    if " WITH " in expression.upper():
        return None
    # OFL notices include project-specific reserved names and attribution. Keep
    # their complete text instead of reducing them to copyright-only summaries.
    for label, marker in LONG_STANDARD_MARKERS.items():
        if marker.lower() in text[:4000].lower() and len(text) > 4000:
            return label
    return None


def _fence(text: str) -> str:
    body = text.replace("\r\n", "\n").replace("\r", "\n").strip("\n")
    body = "\n".join(line.rstrip() for line in body.splitlines())
    fence = "```"
    while fence in body:
        fence += "`"
    return f"{fence}text\n{body}\n{fence}"


def _has_text_section(dep: Dep) -> bool:
    return dep.ecosystem not in {"system", "image"}


def _dedupe_by_key(deps: list[Dep]) -> list[Dep]:
    return list({(d.name, d.version): d for d in deps}.values())


def _text_keys(deps: list[Dep]) -> set[tuple[str, str]]:
    """(name, version) of every component whose license text is reproduced in the notices."""
    return {(d.name, d.version) for d in deps if _has_text_section(d) and d.license_texts}


def _missing_text_keys(deps: list[Dep]) -> set[tuple[str, str]]:
    return {(d.name, d.version) for d in deps if _has_text_section(d) and not d.license_texts}


def _notices_sections(text: str) -> tuple[set[tuple[str, str]], set[tuple[str, str]]]:
    """Parse a generated notices file into (components with license text, components without)."""
    with_text: set[tuple[str, str]] = set()
    without_text: set[tuple[str, str]] = set()
    section = ""
    fence = ""
    for line in text.splitlines():
        if fence:
            if line == fence:
                fence = ""
            continue
        opening = re.match(r"^(`{3,})text$", line)
        if opening:
            fence = opening.group(1)
            continue
        if line.startswith("## "):
            section = "texts" if line == TEXTS_HEADING else ""
            continue
        if section and line.startswith("### "):
            section = "missing" if line == MISSING_HEADING else "texts"
        if section == "texts" and line.startswith("### `"):
            with_text.update(re.findall(r"`([^`]+)` (.*?)(?:, (?=`)|$)", line[4:]))
        elif section == "texts" and line.startswith("- `"):
            match = re.match(r"- `([^`]+)` (.*?)(?:: |$)", line)
            if match:
                with_text.add((match.group(1), match.group(2)))
        elif section == "missing" and line.startswith("- `"):
            match = re.match(r"- `([^`]+)` (.*?) — ", line)
            if match:
                without_text.add((match.group(1), match.group(2)))
    return with_text, without_text


def render_notices(deps: list[Dep], fingerprint: str) -> str:
    image_title = "Additional components installed in images"
    groups = [
        ("Frontend production dependencies (npm)", [d for d in deps if d.scope == SCOPE_FRONTEND]),
        ("Backend runtime dependencies (PyPI)", [d for d in deps if d.scope == SCOPE_BACKEND]),
        ("python_sandbox preinstalled Node packages (npm)", [d for d in deps if d.scope == SCOPE_SANDBOX_NODE]),
        (image_title, [d for d in deps if d.ecosystem in {"system", "image"} or (d.ecosystem == "pypi" and d.scope.startswith("bowhead-"))]),
        ("Third-party material vendored into source", [d for d in deps if d.ecosystem == "vendored"]),
    ]
    out = [
        "# Third-Party Notices",
        "",
        f"<!-- third-party-inventory: {fingerprint} -->",
        f"<!-- third-party-license-texts: {len(_text_keys(deps))} -->",
        "",
        f"Generated by `scripts/check_third_party_notices.py --write`; do not edit by hand. See [{WORKFLOW_DOC}]({WORKFLOW_DOC}) for how to update it.",
        "",
        "This product includes third-party software listed below. Each component is licensed under its own terms; "
        "the copyright notices and license texts required by permissive licenses are reproduced at the end of this file.",
        "",
        "## Summary",
        "",
    ]
    for title, items in groups:
        out.append(f"- {title}: {len(items)}")
    out.append(f"- Total: {sum(len(items) for _, items in groups)}")
    out.append("")
    for title, items in groups:
        out += [f"## {title}", ""]
        for dep in sorted(items, key=lambda d: (d.scope, d.name.lower(), d.version)):
            suffix = f" ({dep.scope})" if title == image_title else ""
            note = f" — {dep.note}" if dep.note else ""
            out.append(f"- `{dep.name}` {dep.version or '-'} — {dep.license or 'UNKNOWN'}{suffix}{note}")
        out.append("")

    out += [TEXTS_HEADING, ""]
    out.append(
        "Identical license texts are shown once. Long standard texts such as Apache-2.0 and MPL-2.0 appear once; each component lists only its copyright lines and NOTICE files."
    )
    out.append("")
    standard_texts: dict[str, str] = {}
    standard_members: dict[str, list[tuple[Dep, list[str], list[tuple[str, str]]]]] = defaultdict(list)
    short_groups: dict[str, list[Dep]] = defaultdict(list)
    short_text: dict[str, str] = {}
    missing: list[Dep] = []
    for dep in sorted(deps, key=lambda d: (d.name.lower(), d.version)):
        if dep.ecosystem in {"system", "image"}:
            continue
        if not dep.license_texts:
            missing.append(dep)
            continue
        notices = [(n, t) for n, t in dep.license_texts if n.lower().startswith("notice")]
        licenses = [(n, t) for n, t in dep.license_texts if not n.lower().startswith("notice")]
        for _, text in licenses:
            label = _standard_long_license(text, dep.license)
            if label:
                standard_texts.setdefault(label, text)
                standard_members[label].append((dep, _copyright_lines(text), notices))
                notices = []
                continue
            digest = hashlib.sha256(text.strip().encode("utf-8")).hexdigest()
            short_text.setdefault(digest, text)
            if dep not in short_groups[digest]:
                short_groups[digest].append(dep)
        for _, text in notices:
            digest = hashlib.sha256(text.strip().encode("utf-8")).hexdigest()
            short_text.setdefault(digest, text)
            short_groups[digest].append(dep)

    for digest, members in sorted(short_groups.items(), key=lambda item: (item[1][0].name.lower(), item[0])):
        names = ", ".join(f"`{d.name}` {d.version}" for d in members)
        out += [f"### {names}", "", _fence(short_text[digest]), ""]
    for label in sorted(standard_members):
        out += [f"### {label} components", ""]
        for dep, copyrights, _ in standard_members[label]:
            line = f"- `{dep.name}` {dep.version}"
            if copyrights:
                line += ": " + "; ".join(copyrights)
            out.append(line)
        out.append("")
        for dep, _, notices in standard_members[label]:
            for name, text in notices:
                out += [f"#### `{dep.name}` {dep.version} {name}", "", _fence(text), ""]
        out += [f"#### {label} license text", "", _fence(standard_texts[label]), ""]
    if missing:
        out += [MISSING_HEADING, ""]
        out.append(
            "These components ship no license file, or are not installed locally so the text could not be read offline; their licenses are listed above."
        )
        out.append("")
        for dep in _dedupe_by_key(missing):
            out.append(f"- `{dep.name}` {dep.version} — {dep.license or 'UNKNOWN'}")
        out.append("")
    out += ["### Image system components", ""]
    out.append(
        "Debian/Alpine system packages and base images are aggregated as separate programs; their copyright and license texts live in the image "
        "under `/usr/share/doc/<package>/copyright` (Alpine: `/usr/share/licenses`). "
        "Corresponding source for the Debian packages, including the GPL-licensed ones, is available from the Debian archives; "
        "use snapshot.debian.org for the exact versions installed in an image (list them with `dpkg-query -W` inside the container). "
        "For questions about obtaining source, contact the support channel of the organization that deployed the application for you."
    )
    out.append("")
    return "\n".join(out).rstrip() + "\n"


def _review_category_label(code: str, detail: str, copy: dict[str, Any]) -> str:
    labels = copy["categories"]
    if code == "field_missing":
        return labels["field_missing"].format(license=detail)
    if code in labels:
        return labels[code]
    return code + (labels[detail] if detail else "")


def render_review(deps: list[Dep], fingerprint: str, dockerfile_notes: list[str], *, copy: dict[str, Any] = REVIEW_COPY) -> str:
    flagged = [(attention_category(d.license, declared=d.declared), d) for d in deps]
    flagged = [(c, d) for c, d in flagged if c[0]]
    sep = copy["separator"]
    out = [
        copy["title"],
        "",
        f"<!-- third-party-inventory: {fingerprint} -->",
        "",
        copy["generated"],
        "",
        copy["intro"],
        "",
        copy["reading_hint"],
        "",
        copy["total"].format(count=len(flagged)),
        "",
    ]

    def sort_key(item: tuple[tuple[str, str], Dep]) -> tuple[int, str, str]:
        (code, _), dep = item
        rank = next((i for i, label in enumerate(CATEGORY_ORDER) if code.startswith(label)), len(CATEGORY_ORDER))
        return (rank, dep.name.lower(), dep.version)

    for (code, detail), dep in sorted(flagged, key=sort_key):
        out.append(
            copy["entry"].format(
                category=_review_category_label(code, detail, copy),
                name=dep.name,
                version=dep.version,
                license=dep.license or copy["missing_license"],
                scope=copy["scopes"].get(dep.scope, dep.scope),
                introduced=sep.join(dep.introduced_by),
                note=f"{sep}{dep.note}" if dep.note else "",
            )
        )
    out.append("")
    if dockerfile_notes:
        out += [copy["image_notes_heading"], ""]
        out += [f"- {note}" for note in dockerfile_notes]
        out.append("")
    return "\n".join(out).rstrip() + "\n"


def _dockerfile_notes(copy: dict[str, Any] = REVIEW_COPY) -> list[str]:
    notes = []
    app = DOCKERFILES["bowhead-app"].read_text(encoding="utf-8")
    if "rm -rf /usr/share/doc" in app:
        notes.append(copy["note_usr_share_doc_removed"])
    else:
        notes.append(copy["note_debian_copyright_kept"])
    if "bowhead-sandbox" in _inventory_dockerfiles():
        notes.append(copy["note_sandbox_pip_unlocked"])
    return notes


def write(*, allow_missing: bool) -> int:
    fingerprint = inventory_fingerprint()
    try:
        deps = _collect_npm(FRONTEND_DIR, SCOPE_FRONTEND) + _collect_python() + _collect_npm(SANDBOX_NODE_DIR, SCOPE_SANDBOX_NODE) + _collect_images()
    except MissingMetadataError as exc:
        sys.stderr.write(f"error: {exc}\nfix: {REGENERATE_HINT}\n")
        return 1
    committed_missing = _notices_sections(NOTICES_OUT.read_text(encoding="utf-8"))[1] if NOTICES_OUT.is_file() else set()
    new_missing = sorted(_missing_text_keys(deps) - committed_missing)
    if new_missing and not allow_missing:
        sys.stderr.write(f"error: {len(new_missing)} components have no license text available locally and were not missing before:\n")
        for name, version in new_missing[:20]:
            sys.stderr.write(f"  - {name} {version}\n")
        if len(new_missing) > 20:
            sys.stderr.write(f"  ... and {len(new_missing) - 20} more\n")
        sys.stderr.write(
            f"fix: {REGENERATE_HINT}. If a new package genuinely ships no license file, rerun with --allow-missing. Nothing was written.\n"
        )
        return 1
    NOTICES_OUT.write_text(render_notices(deps, fingerprint), encoding="utf-8")
    REVIEW_OUT.write_text(render_review(deps, fingerprint, _dockerfile_notes()), encoding="utf-8")
    REVIEW_EN_OUT.write_text(render_review(deps, fingerprint, _dockerfile_notes(REVIEW_COPY_EN), copy=REVIEW_COPY_EN), encoding="utf-8")
    sys.stdout.write(f"wrote {NOTICES_OUT.relative_to(ROOT)} and review reports: {len(deps)} entries, {len(_text_keys(deps))} with license text\n")
    return 0


def check() -> int:
    problems = _unregistered_image_components()
    expected = inventory_fingerprint()
    for path in (NOTICES_OUT, REVIEW_OUT, REVIEW_EN_OUT):
        rel = path.relative_to(ROOT)
        if not path.is_file():
            problems.append(f"{rel} does not exist")
            continue
        match = FINGERPRINT_RE.search(path.read_text(encoding="utf-8"))
        if not match:
            problems.append(f"{rel} has no inventory fingerprint")
        elif match.group(1) != expected:
            problems.append(f"{rel} is out of date: dependencies changed but the notices were not regenerated")
    if NOTICES_OUT.is_file():
        notices_text = NOTICES_OUT.read_text(encoding="utf-8")
        recorded = LICENSE_TEXTS_RE.search(notices_text)
        present = len(_notices_sections(notices_text)[0])
        if not recorded:
            problems.append(f"{NOTICES_OUT.relative_to(ROOT)} has no license text count")
        elif int(recorded.group(1)) != present:
            problems.append(
                f"{NOTICES_OUT.relative_to(ROOT)} records {recorded.group(1)} components with license text but contains {present}; regenerate it"
            )
    if problems:
        sys.stderr.write("third-party notices check failed:\n")
        for problem in problems:
            sys.stderr.write(f"  - {problem}\n")
        sys.stderr.write(f"fix: {REGENERATE_HINT}; commit the generated files (see {WORKFLOW_DOC}).\n")
        return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write", action="store_true", help="regenerate the notices and the review report")
    mode.add_argument("--check", action="store_true", help="verify the notices match the lockfiles")
    parser.add_argument("--allow-missing", action="store_true", help="with --write, accept components whose license text is newly unavailable")
    args = parser.parse_args()
    if args.write:
        problems = _unregistered_image_components()
        if problems:
            sys.stderr.write("\n".join(problems) + "\n")
            return 1
        return write(allow_missing=args.allow_missing)
    return check()


if __name__ == "__main__":
    sys.exit(main())
