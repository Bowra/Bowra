"""Bowhead EvalKit — agent 输出评估的最小可用框架.

**定位**：跑一批带标签的 case 通过任意 async agent 函数，按 scorer 打分，汇总报告.
不包含训练／hyperparam sweep／bandit / RLHF，就只做"这版 agent 在这个数据集上得几分"。

典型流：
    cases = load_cases_jsonl(Path("datasets/diagnosis_v1.jsonl"))
    async def run(case: EvalCase) -> str: ...
    scorers = [ContainsScorer(), NoFabricationScorer()]
    report = await EvalRunner(scorers).run(cases, run)
    report.write_json(Path("reports/diagnosis_v1.json"))

Scope 取舍：
- 同步/异步 agent 都支持（runner 内部 always await）
- 并发：默认 1（避免打爆 LLM rate limit），可调
- 失败重试：**不内建**，case runner 应该自行幂等重试再报告失败
- golden diff：简单 string/set 级，复杂结构比较由 scorer 子类化自己做
"""

from bowhead_core.evalkit.cases import EvalCase, load_cases_jsonl, write_cases_jsonl
from bowhead_core.evalkit.report import CaseResult, EvalReport
from bowhead_core.evalkit.runner import EvalRunner
from bowhead_core.evalkit.scorers import (
    ContainsScorer,
    ExactMatchScorer,
    JSONFieldScorer,
    MRRScorer,
    NoFabricationScorer,
    NumberToleranceScorer,
    RecallAtKScorer,
    Scorer,
    ScoreResult,
)

__all__ = [
    "CaseResult",
    "ContainsScorer",
    "EvalCase",
    "EvalReport",
    "EvalRunner",
    "ExactMatchScorer",
    "JSONFieldScorer",
    "MRRScorer",
    "NoFabricationScorer",
    "NumberToleranceScorer",
    "RecallAtKScorer",
    "ScoreResult",
    "Scorer",
    "load_cases_jsonl",
    "write_cases_jsonl",
]
