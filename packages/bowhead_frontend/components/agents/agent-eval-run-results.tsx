"use client";

import { useEffect, useRef, useState } from "react";
import { useTranslations } from "next-intl";
import { Button } from "@/components/ui/button";
import { Markdown } from "@/lib/md";
import { getAgentEvalRunCaseAnswers, type AgentEvalQuality, type AgentEvalRun, type AgentEvalRunCaseAnswers } from "@/lib/agents-api";
import { AttemptPointVerdicts, PointCalibrationNotices } from "./agent-eval-point-verdicts";

/** Execution and answer criteria are independent; every model judgment remains advisory. */
export function AnswerQuality({ quality }: { quality?: AgentEvalQuality }) {
  const t = useTranslations("agents_marketplace");
  if (!quality) return null;
  return <p className="am-ci-note" data-tone={quality.unmet || quality.ungraded ? "warning" : undefined}>
    {t(`eval_quality_${quality.state}` as never, { met: quality.met, unmet: quality.unmet, ungraded: quality.ungraded } as never)}
    {quality.point_assessments > 0 ? <> {t("eval_quality_advisory")}</> : null}
  </p>;
}

/** Only the selected case loads its full answer; switching runs discards its local cache. */
export function AgentEvalRunResults({ agentId, run }: { agentId: string; run: AgentEvalRun }) {
  const t = useTranslations("agents_marketplace");
  const [answers, setAnswers] = useState<Record<string, AgentEvalRunCaseAnswers>>({});
  const [failures, setFailures] = useState<Set<string>>(() => new Set());
  const pending = useRef(new Set<string>());
  const mounted = useRef(true);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  const load = async (caseId: string) => {
    if (pending.current.has(caseId) || answers[caseId]) return;
    pending.current.add(caseId);
    setFailures((value) => { const next = new Set(value); next.delete(caseId); return next; });
    try {
      const value = await getAgentEvalRunCaseAnswers(agentId, run.run_id, caseId);
      if (mounted.current) setAnswers((current) => ({ ...current, [caseId]: value }));
    } catch {
      if (mounted.current) setFailures((current) => new Set(current).add(caseId));
    } finally {
      pending.current.delete(caseId);
    }
  };
  const cases = [...new Map((run.results ?? []).map((result) => [result.case_revision_id, result])).values()];
  return <div className="am-ci-answers">
    {cases.map((result, index) => {
      const value = answers[result.case_revision_id];
      return <details key={result.case_revision_id} className="am-ci-answer" onToggle={(event) => {
        if (event.currentTarget.open) void load(result.case_revision_id);
      }}>
        <summary>{t("eval_case_result", { number: index + 1, question: result.task || result.case_key })}</summary>
        {value ? <>
          <PointCalibrationNotices definitions={value.points} />
          {value.attempts.map((attempt) => <div key={attempt.attempt}>
            <p className="am-ci-note">{t("eval_execution_result", { status: t(`eval_execution_${attempt.status === "passed" ? "passed" : attempt.status === "failed" ? "failed" : "incomplete"}` as never) })}</p>
            <AnswerQuality quality={attempt.quality} />
            {attempt.answer ? <div className="am-ci-answer-text" data-markdown="true"><Markdown markdown={attempt.answer} allowImages={false} /></div>
              : <p className="am-ci-note">{t(attempt.answer_withheld === "stopped_without_answer" ? "version_answer_stopped_without_answer" : "version_answer_missing")}</p>}
            {attempt.answer_truncated ? <p className="am-ci-note">{t("version_answer_truncated")}</p> : null}
            <AttemptPointVerdicts agentId={agentId} definitions={value.points} points={attempt.points ?? []}
              caseResult={{ run_id: run.run_id, case_revision_id: value.case_revision_id, attempt: attempt.attempt }} />
          </div>)}
        </> : failures.has(result.case_revision_id) ? <p role="alert" className="am-ci-note">{t("eval_answers_failed")} <Button variant="ghost" size="sm" onClick={() => void load(result.case_revision_id)}>{t("eval_answers_retry")}</Button></p>
          : <p role="status" className="am-ci-note">{t("eval_answers_loading")}</p>}
      </details>;
    })}
    {!cases.length ? <p className="am-ci-note">{t("eval_incomplete_result")}</p> : null}
  </div>;
}
