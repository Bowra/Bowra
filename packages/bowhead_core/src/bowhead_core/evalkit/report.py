"""EvalReport — 汇总 case × scorer 矩阵 + 整体指标."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

    from bowhead_core.evalkit.scorers import ScoreResult


@dataclass
class CaseResult:
    case_id: str
    tags: list[str]
    output: Any
    scores: list[ScoreResult]
    duration_ms: int
    error: str | None = None

    def passed(self) -> bool:
        """所有 scorer 全 pass 才算通过."""
        return not self.error and all(s.passed for s in self.scores)

    def avg_score(self) -> float:
        if not self.scores:
            return 0.0
        return sum(s.score for s in self.scores) / len(self.scores)

    def to_json(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "tags": list(self.tags),
            "output": self.output,
            "scores": [{"name": s.name, "passed": s.passed, "score": s.score, "detail": s.detail, "data": s.data} for s in self.scores],
            "duration_ms": self.duration_ms,
            "error": self.error,
            "passed": self.passed(),
        }


@dataclass
class EvalReport:
    dataset_name: str
    case_results: list[CaseResult]
    started_at: float = field(default_factory=time.time)
    duration_ms: int = 0
    meta: dict[str, Any] = field(default_factory=dict)

    # ─── aggregates ────────────────────────────────────────
    def pass_rate(self) -> float:
        if not self.case_results:
            return 0.0
        return sum(1 for c in self.case_results if c.passed()) / len(self.case_results)

    def avg_score(self) -> float:
        if not self.case_results:
            return 0.0
        return sum(c.avg_score() for c in self.case_results) / len(self.case_results)

    def by_scorer(self) -> dict[str, dict[str, float]]:
        """{scorer_name: {pass_rate, avg_score}}."""
        agg: dict[str, list[ScoreResult]] = {}
        for c in self.case_results:
            for s in c.scores:
                agg.setdefault(s.name, []).append(s)
        return {
            name: {
                "pass_rate": sum(1 for s in items if s.passed) / len(items),
                "avg_score": sum(s.score for s in items) / len(items),
                "n": len(items),
            }
            for name, items in agg.items()
        }

    def by_tag(self) -> dict[str, dict[str, float]]:
        agg: dict[str, list[CaseResult]] = {}
        for c in self.case_results:
            for tag in c.tags or ["(untagged)"]:
                agg.setdefault(tag, []).append(c)
        return {
            tag: {
                "pass_rate": sum(1 for c in items if c.passed()) / len(items),
                "n": len(items),
            }
            for tag, items in agg.items()
        }

    # ─── serialization ─────────────────────────────────────
    def to_json(self) -> dict[str, Any]:
        return {
            "dataset": self.dataset_name,
            "n_cases": len(self.case_results),
            "pass_rate": self.pass_rate(),
            "avg_score": self.avg_score(),
            "duration_ms": self.duration_ms,
            "started_at": self.started_at,
            "by_scorer": self.by_scorer(),
            "by_tag": self.by_tag(),
            "cases": [c.to_json() for c in self.case_results],
            "meta": self.meta,
        }

    def write_json(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(self.to_json(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
