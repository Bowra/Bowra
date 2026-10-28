"""Bound repeated retrieval without new evidence inside one KB assignment."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


@dataclass
class KnowledgeSearchBudget:
    """Allow an initial no-progress round and two bounded reformulations."""

    empty_rounds: int = 0
    queries: set[str] = field(default_factory=set)
    evidence: set[str] = field(default_factory=set)
    round_queries: int = 0

    @property
    def exhausted(self) -> bool:
        return self.empty_rounds >= 3

    def admit(self, arguments: dict[str, Any]) -> bool:
        normalized = dict(arguments)
        normalized["query"] = " ".join(str(arguments.get("query", "")).casefold().split())
        key = json.dumps(normalized, sort_keys=True, ensure_ascii=False)
        if self.exhausted or self.round_queries >= 3 or key in self.queries:
            return False
        self.queries.add(key)
        self.round_queries += 1
        return True

    def observe(self, invocations: list[Any], *, search_requested: bool) -> str | None:
        self.round_queries = 0
        found = set()
        for inv in invocations:
            if inv.tool_name != "kb_search" or inv.error or not isinstance(inv.result, dict):
                continue
            for chunk in inv.result.get("chunks", []):
                if isinstance(chunk, dict):
                    found.add(
                        str(
                            chunk.get("chunk_id")
                            or json.dumps([chunk.get(k) for k in ("file_id", "page", "section_title", "content")], ensure_ascii=False)
                        )
                    )
        new = found - self.evidence
        self.evidence.update(found)
        if new:
            self.empty_rounds = 0
        elif search_requested:
            self.empty_rounds += 1
            return (
                f"KB retrieval produced no new evidence in {self.empty_rounds} consecutive round(s). "
                + (
                    "The retrieval budget is exhausted. Only write_report and subagent_return are available to close out verified findings and explicit gaps. "
                    if self.exhausted
                    else "Use at most two further meaningful reformulation rounds after the initial search; stop earlier when no useful reformulation remains. "
                )
                + "No matches do not prove the knowledge base is empty. Do not invent citations, use unrelated queries, or switch to BI/web to fill missing KB evidence."
            )
        return None


def retrieval_receipt(invocations: list[Any]) -> dict[str, int]:
    """Count physical search outcomes, excluding rejected or unexecuted calls."""
    searches = [inv for inv in invocations if inv.tool_name == "kb_search"]
    return {
        "searches_executed": len(searches),
        "searches_succeeded": sum(inv.error is None and isinstance(inv.result, dict) and inv.result.get("ok") is not False for inv in searches),
    }
