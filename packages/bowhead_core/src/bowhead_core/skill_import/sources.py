"""解析源装配 —— 清点只认调用方给的名集；这里把各 SSOT 读成名集.

- 沙箱 python 包：``catalog/python_sandbox/constraints.txt``（镜像 baked）+
  ``wheelhouse-requirements.txt``（长尾，须 frontmatter ``sandbox_requirements`` 声明才装）。
  dist 名 → import 名用一张小表（python-pptx→pptx 这类），默认 ``-``→``_``。
- 沙箱 node 包：``script_langs.guaranteed_node_packages()``。
- 工具/skill 名由 gateway / tool 侧按当前 actor 装配（见 routes/skills.py 与 skill_save_tool.py）。
"""

from __future__ import annotations

import re
from functools import lru_cache

from bowhead_skill.script_langs import guaranteed_node_packages

from bowhead_skill.catalog import builtin_catalog_dir

# Distribution names (PEP 503 normalized: lowercase, `-`) → top-level import names; fall back to `-`→`_` for entries absent from the table.
_DIST_TO_IMPORT: dict[str, tuple[str, ...]] = {
    "python-pptx": ("pptx",),
    "python-docx": ("docx",),
    "pillow": ("PIL",),
    "scikit-learn": ("sklearn",),
    "beautifulsoup4": ("bs4",),
    "pyyaml": ("yaml",),
    "opencv-python": ("cv2",),
    "opencv-python-headless": ("cv2",),
    "python-dateutil": ("dateutil",),
    "pypdf": ("pypdf",),
    "pdfplumber": ("pdfplumber",),
    "pyarrow": ("pyarrow",),
    "matplotlib": ("matplotlib", "mpl_toolkits"),
    "setuptools": ("setuptools", "pkg_resources"),
    "python-dotenv": ("dotenv",),
    "reportlab": ("reportlab",),
}
_REQ_LINE_RE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")


def _normalize_dist(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def import_names_for_dists(dists: set[str]) -> frozenset[str]:
    out: set[str] = set()
    for d in dists:
        key = _normalize_dist(d)
        for mod in _DIST_TO_IMPORT.get(key, (key.replace("-", "_"),)):
            out.add(mod)
    return frozenset(out)


def _read_requirement_names(text: str) -> set[str]:
    names: set[str] = set()
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", "-")):
            continue
        m = _REQ_LINE_RE.match(stripped)
        if m:
            names.add(m.group(1))
    return names


@lru_cache(maxsize=1)
def sandbox_python_dists() -> frozenset[str]:
    """镜像 baked（constraints）+ 离线 wheelhouse **可装**的 dist 名（PEP 503 归一）。

    `sandbox_requirements` 的安装是 `pip --no-index --find-links <wheelhouse> --require-hashes`、
    `--network=none` 从不放宽——不在这两份 SSOT 里的包，声明了也装不上。
    """
    root = builtin_catalog_dir() / "python_sandbox"
    dists: set[str] = set()
    for fname in ("constraints.txt", "wheelhouse-requirements.txt"):
        path = root / fname
        if path.is_file():
            dists |= _read_requirement_names(path.read_text(encoding="utf-8"))
    return frozenset(_normalize_dist(d) for d in dists)


@lru_cache(maxsize=1)
def sandbox_python_modules() -> frozenset[str]:
    """镜像 baked + wheelhouse 可装的顶层模块名（SSOT 随包不可变 → 缓存一次）."""
    return import_names_for_dists(set(sandbox_python_dists()))


@lru_cache(maxsize=1)
def sandbox_node_packages() -> frozenset[str]:
    return frozenset(e["name"] for e in guaranteed_node_packages())
