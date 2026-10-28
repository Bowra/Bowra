"""EvalCase dataclass + JSONL I/O.

JSONL schema：每行一个 case.
    {"id": "diag-001", "input": "为什么 GMV 下滑", "expected": "...", "tags": ["diagnosis"], "meta": {...}}

- `id` 必填、数据集内唯一
- `input` = 给 agent 的输入（一般是用户文本；复杂场景可为 dict，scorer 自行解析）
- `expected` = 参考答案（string 或 dict）；可为 None（某些 scorer 比如 NoFabrication 不依赖参考）
- `tags` = 子集 / 场景标签（报告里可按 tag 汇总）
- `meta` = 自由元数据（版本号、bug 号）
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path


@dataclass
class EvalCase:
    id: str
    input: Any
    expected: Any | None = None
    tags: list[str] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "input": self.input,
            "expected": self.expected,
            "tags": list(self.tags),
            "meta": dict(self.meta),
        }

    @classmethod
    def from_json(cls, obj: dict[str, Any]) -> EvalCase:
        if "id" not in obj:
            raise ValueError("EvalCase missing 'id'")
        return cls(
            id=str(obj["id"]),
            input=obj.get("input"),
            expected=obj.get("expected"),
            tags=list(obj.get("tags") or []),
            meta=dict(obj.get("meta") or {}),
        )


def load_cases_jsonl(path: Path) -> list[EvalCase]:
    """读 JSONL，跳过空行/#注释行."""
    cases: list[EvalCase] = []
    seen_ids: set[str] = set()
    with path.open("r", encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as e:
                raise ValueError(f"{path}:{lineno} invalid JSON: {e}") from e
            case = EvalCase.from_json(obj)
            if case.id in seen_ids:
                raise ValueError(f"{path}:{lineno} duplicate case id: {case.id}")
            seen_ids.add(case.id)
            cases.append(case)
    return cases


def write_cases_jsonl(path: Path, cases: list[EvalCase]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for c in cases:
            f.write(json.dumps(c.to_json(), ensure_ascii=False) + "\n")
