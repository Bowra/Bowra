"""skill-team-governance — per-skill asyncio.Lock（审批盘/DB 一致性的单进程串行化点）.

key=``(team_id, name)`` 而非 skill id：L1 首提时可能尚无行 id，且盘命名空间就是
``.data/team_skills/<team_id>/<name>[__candidate|__old]``——id 键锁不住 L1 与目录。
approve/reject/submit/withdraw/promote/DELETE 全部纳锁（team_skills 路由与 skills
路由的 promote 同 import 本模块，保证锁字典唯一）。

⚠ 单进程网关假设：多 worker 化时本锁失效，CAS（内存预检 + DB WHERE 双层）是唯一
防线——锁只是把常态并发串行化，CAS 兜锁外陈旧读与理论竞态（tech-design P1b）。
锁表不清理：条目数 = 团队 skill 名基数，量级小；弱引用清理不值得复杂度。
"""

from __future__ import annotations

import asyncio

_locks: dict[tuple[str, str], asyncio.Lock] = {}


def team_skill_lock(team_id: str, name: str) -> asyncio.Lock:
    """该 (team_id, name) 的进程级互斥锁（惰性创建，永驻）."""
    return _locks.setdefault((team_id, name), asyncio.Lock())
