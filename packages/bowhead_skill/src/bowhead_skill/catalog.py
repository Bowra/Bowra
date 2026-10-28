"""SkillCatalog — 扫盘 + 按 name 查 skill.

production：
- 支持多个 root dir（Bowhead 自带 + 用户自定义）
- 扫描结果缓存；调 refresh() 重新加载
- load 失败记 log 不中断（避免一个损坏 skill 拖累整体）
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from bowhead_skill.loader import SkillSpec, load_skill

logger = logging.getLogger(__name__)


@dataclass
class SkillCatalog:
    roots: list[Path] = field(default_factory=list)
    _skills: dict[str, SkillSpec] = field(default_factory=dict, init=False)
    _loaded: bool = field(default=False, init=False)

    def refresh(self) -> None:
        """扫描所有 roots，重建索引."""
        skills: dict[str, SkillSpec] = {}
        for root in self.roots:
            if not root.is_dir():
                logger.warning("skill_catalog.root_missing path=%s", root)
                continue
            for md in root.rglob("SKILL.md"):
                skill_dir = md.parent
                try:
                    spec = load_skill(skill_dir)
                except Exception as e:
                    logger.warning(
                        "skill_catalog.load_failed dir=%s err=%s",
                        skill_dir,
                        e,
                    )
                    continue
                if spec.name in skills:
                    logger.warning(
                        "skill_catalog.duplicate_name name=%s prev=%s new=%s",
                        spec.name,
                        skills[spec.name].source_dir,
                        skill_dir,
                    )
                skills[spec.name] = spec
        self._skills = skills
        self._loaded = True
        logger.info(
            "skill_catalog.loaded n=%d names=%s",
            len(skills),
            sorted(skills.keys()),
        )

    def _ensure(self) -> None:
        if not self._loaded:
            self.refresh()

    def get(self, name: str) -> SkillSpec | None:
        self._ensure()
        return self._skills.get(name)

    def list_names(self) -> list[str]:
        self._ensure()
        return sorted(self._skills.keys())

    def list_specs(self) -> list[SkillSpec]:
        self._ensure()
        return [self._skills[n] for n in self.list_names()]


def builtin_catalog_dir() -> Path:
    """Bowhead 自带（随包分发的）skill 根目录."""
    here = Path(__file__).resolve().parent
    return here.parent.parent / "catalog"


def user_catalog_dir() -> Path:
    """每个系统用户的 skill 根（跨项目）."""
    return Path.home() / ".bowhead" / "skills"


def project_catalog_dir(cwd: Path | None = None) -> Path:
    """当前项目 skill 根."""
    return (cwd or Path.cwd()) / ".bowhead" / "skills"


def default_catalog(cwd: Path | None = None) -> SkillCatalog:
    """Bowhead 默认分层 catalog：builtin + user + project（顺序 = 优先级由低到高）.

    顺序意味着：相同 name 的后一层覆盖前一层（Claude Code 行为）。
    """
    return SkillCatalog(
        roots=[
            builtin_catalog_dir(),
            user_catalog_dir(),
            project_catalog_dir(cwd),
        ]
    )


@lru_cache(maxsize=1)
def builtin_skill_names() -> frozenset[str]:
    """内置 catalog 的 skill 名集合（lru_cached，全库 rglob + frontmatter parse 只一次）.

    review-F8: gateway boot 的 namespace 守卫 + skill_upload 的 reserved floor + namespace-
    disjoint 测试都需要「仅内置根」的名字集，原先各处重复
    `SkillCatalog(roots=[builtin_catalog_dir()]).list_names()`（boot 期还多走一次全盘 IO，
    因为 build_default_skill_stack 已扫过同目录）。收敛到此单一 owner。内置目录随包不可变，
    lru_cache 安全（测试如需刷新可 `builtin_skill_names.cache_clear()`）。
    """
    return frozenset(SkillCatalog(roots=[builtin_catalog_dir()]).list_names())
