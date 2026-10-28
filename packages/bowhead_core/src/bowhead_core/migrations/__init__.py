"""Alembic migrations owned by Bowhead Core."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

__all__ = ["chain_head"]


@lru_cache(maxsize=1)
def chain_head() -> str:
    """当前 revision 链条的唯一 head。

    存在的理由：`HEAD_REVISION = "00xx_..."` 这个字面量此前被**抄在 12 个测试文件里**
    （其中一个还停在 `0020` 没跟上），于是每加一条迁移就要改十几处，
    忘一处就是一条与本次改动无关的红。加迁移 0029 时一次性撞红 25 条即为此
    （[[feedback_one_concept_many_implementations]]：改一处 = 没改，先数清）。

    ⚠ 链条分叉时直接抛——那是必须有人处理的状态，不该被静默取第一个。
    """
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    config = Config()
    config.set_main_option("script_location", str(Path(__file__).resolve().parent))
    heads = ScriptDirectory.from_config(config).get_heads()
    if len(heads) != 1:
        raise AssertionError(f"revision 链分叉了，heads={heads}")
    return heads[0]
