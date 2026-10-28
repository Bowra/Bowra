"""EvalRunner — 跑一批 case 通过 async agent，走 scorers，汇聚 EvalReport."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from bowhead_core.evalkit.cases import EvalCase
from bowhead_core.evalkit.report import CaseResult, EvalReport

if TYPE_CHECKING:
    from bowhead_core.evalkit.scorers import Scorer

logger = logging.getLogger(__name__)


# Agent function signature: (case) -> (output, trace_dict | None)
AgentFn = Callable[[EvalCase], Awaitable[tuple[Any, dict[str, Any] | None]]]


@dataclass
class EvalRunner:
    scorers: list[Scorer]
    concurrency: int = 1
    per_case_timeout_sec: float = 60.0
    dataset_name: str = "unnamed"
    meta: dict[str, Any] = field(default_factory=dict)

    async def run(
        self,
        cases: list[EvalCase],
        agent: AgentFn,
    ) -> EvalReport:
        sem = asyncio.Semaphore(max(1, self.concurrency))
        start = time.time()

        async def _one(case: EvalCase) -> CaseResult:
            async with sem:
                return await self._run_case(case, agent)

        results = await asyncio.gather(*[_one(c) for c in cases])
        duration = int((time.time() - start) * 1000)
        return EvalReport(
            dataset_name=self.dataset_name,
            case_results=list(results),
            started_at=start,
            duration_ms=duration,
            meta=dict(self.meta),
        )

    async def _run_case(self, case: EvalCase, agent: AgentFn) -> CaseResult:
        t0 = time.perf_counter()
        output: Any = None
        trace: dict[str, Any] | None = None
        err: str | None = None
        try:
            output, trace = await asyncio.wait_for(
                agent(case),
                timeout=self.per_case_timeout_sec,
            )
        except TimeoutError:
            err = f"agent timed out after {self.per_case_timeout_sec}s"
        except Exception as e:
            err = f"agent raised: {e}"
            logger.exception("evalrunner.case_raised case_id=%s", case.id)

        scores = []
        if not err:
            for s in self.scorers:
                try:
                    scores.append(s.score(case, output, trace))
                except Exception as e:
                    from bowhead_core.evalkit.scorers import ScoreResult

                    scores.append(
                        ScoreResult(
                            name=getattr(s, "name", str(type(s))),
                            passed=False,
                            score=0.0,
                            detail=f"scorer raised: {e}",
                        )
                    )
        return CaseResult(
            case_id=case.id,
            tags=list(case.tags),
            output=output,
            scores=scores,
            duration_ms=int((time.perf_counter() - t0) * 1000),
            error=err,
        )
