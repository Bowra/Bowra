"use client";

/**
 * Expected-point verdicts in comparison results (evaluation-credibility PRD R13, R26, R28).
 *
 * Every graded point is listed with its verdict and the sentence it relied on; met points fold under
 * the attempt so a wrong pass is as easy to report as a wrong fail. "Judged wrongly" records the
 * disputed verdict for calibration and shows Coach's rewording; it never changes this comparison.
 */

import { useEffect, useState } from "react";
import { useTranslations } from "next-intl";

import { Button } from "@/components/ui/button";
import { toolLabel } from "@/lib/tool-labels";
import {
  getAgentVersionUnmetPoints,
  reportAgentEvalPointJudgedWrongly,
  type AgentEvalCasePointDefinition,
  type AgentEvalGradedPoint,
  type AgentVersionUnmetPoint,
} from "@/lib/agents-api";

type CaseResult = { run_id: string; case_revision_id: string; attempt: number };
type Translate = ReturnType<typeof useTranslations>;

/** Tools by the names the Owner picked them under, quoted: 「运行代码」或「查询数据」. */
export function quotedToolNames(tools: readonly string[], t: Translate): string {
  return tools.map((tool) => t("version_quoted", { name: toolLabel(tool).name })).join(t("eval_point_tool_or"));
}

/** The sentence the verdict relied on, or why there is none. */
function Evidence({ point, tools }: { point: Pick<AgentEvalGradedPoint, "met" | "quote" | "applicable" | "reason">; tools?: string[] }) {
  const t = useTranslations("agents_marketplace");
  // A required action is checked from the calls made: say which Tool wasn't called or couldn't run.
  if (point.reason === "tool_not_called") return <span className="am-ci-note">{t("eval_point_reason_tool_not_called", { tools: quotedToolNames(tools ?? [], t) })}</span>;
  if (point.reason === "tool_not_run_in_eval") return <span className="am-ci-note">{t("eval_point_reason_tool_not_run_in_eval", { tools: quotedToolNames(tools ?? [], t) })}</span>;
  if (!point.applicable) return <span className="am-ci-note">{t("eval_point_not_applicable")}</span>;
  if (point.met === null) return <span className="am-ci-note">{t(point.reason === "not_graded" ? "eval_point_missing_grade" : "eval_point_not_graded")}</span>;
  if (!point.quote) return null;
  return <q className="am-ci-point-quote">{point.quote}</q>;
}

function JudgedWrongly({ agentId, pointId, pointText, caseResult }: { agentId: string; pointId: string; pointText: string; caseResult: CaseResult }) {
  const t = useTranslations("agents_marketplace");
  const [state, setState] = useState<"idle" | "sending" | "done" | "failed">("idle");
  const [rewording, setRewording] = useState<string | null>(null);
  const send = () => {
    setState("sending");
    void reportAgentEvalPointJudgedWrongly(agentId, pointId, caseResult).then((value) => {
      setRewording(value.rewording?.trim() !== pointText.trim() ? value.rewording : null);
      setState("done");
    }).catch(() => setState("failed"));
  };
  if (state === "done") {
    return (
      <p className="am-ci-note" role="status">
        {t("eval_point_judged_wrongly_recorded")}
        {rewording ? <> {t("eval_point_rewording", { text: rewording })}</> : null}
      </p>
    );
  }
  return (
    <span className="am-ci-point-act">
      <Button size="sm" variant="ghost" loading={state === "sending"} onClick={send}>{t("eval_point_judged_wrongly")}</Button>
      {state === "failed" ? <span className="am-ci-note" role="alert">{t("eval_point_judged_wrongly_failed")}</span> : null}
    </span>
  );
}

function PointRow({ agentId, definition, point, caseResult }: {
  agentId: string;
  definition: AgentEvalCasePointDefinition | { id: string; text: string; kind?: undefined; tools?: undefined };
  point: AgentEvalGradedPoint;
  caseResult: CaseResult | null;
}) {
  const t = useTranslations("agents_marketplace");
  const verdict = !point.applicable ? "na" : point.met === null ? "none" : point.met ? "met" : "unmet";
  // A required action is decided by the calls made, not by the evaluation model, so there is no verdict to dispute.
  const judged = definition.kind !== "required_action";
  return (
    <li className="am-ci-point" data-verdict={verdict}>
      <span className="am-ci-point-head">
        <b>{t(`eval_point_verdict_${verdict}` as never)}</b>
        <span>{definition.text}</span>
      </span>
      <Evidence point={point} tools={definition.tools} />
      {judged && caseResult && point.met !== null && point.applicable ? <JudgedWrongly agentId={agentId} pointId={point.id} pointText={definition.text} caseResult={caseResult} /> : null}
    </li>
  );
}

/** Verdicts of one attempt: points not met or not graded first, met points folded (R26). */
export function AttemptPointVerdicts({ agentId, definitions, points, caseResult }: {
  agentId: string;
  definitions: AgentEvalCasePointDefinition[];
  points: AgentEvalGradedPoint[];
  caseResult: CaseResult | null;
}) {
  const t = useTranslations("agents_marketplace");
  if (points.length === 0) return null;
  const definition = (id: string) => definitions.find((item) => item.id === id) ?? { id, text: id };
  const open = points.filter((point) => point.met !== true);
  const met = points.filter((point) => point.met === true);
  return (
    <div className="am-ci-points">
      {open.length ? (
        <ul className="am-ci-point-list" aria-label={t("eval_points_label")}>
          {open.map((point) => <PointRow key={point.id} agentId={agentId} definition={definition(point.id)} point={point} caseResult={caseResult} />)}
        </ul>
      ) : null}
      {met.length ? (
        <details className="am-ci-points-met">
          <summary>{t("eval_points_met", { count: met.length })}</summary>
          <ul className="am-ci-point-list">
            {met.map((point) => <PointRow key={point.id} agentId={agentId} definition={definition(point.id)} point={point} caseResult={caseResult} />)}
          </ul>
        </details>
      ) : null}
    </div>
  );
}

/** A point that failed calibration says why and offers Coach's rewording (R12). */
export function PointCalibrationNotices({ definitions }: { definitions: AgentEvalCasePointDefinition[] }) {
  const t = useTranslations("agents_marketplace");
  const failed = definitions.filter((item) => item.calibration_failure);
  if (!failed.length) return null;
  return (
    <ul className="am-ci-point-list" aria-label={t("eval_point_calibration_label")}>
      {failed.map((item) => (
        <li key={item.id} className="am-ci-point">
          <span className="am-ci-point-head"><span>{item.text}</span></span>
          <span className="am-ci-note">
            {t(`eval_point_calibration_${item.calibration_failure?.reason}` as never)}
            {item.calibration_failure?.rewording && item.calibration_failure.rewording.trim() !== item.text.trim() ? <> {t("eval_point_rewording", { text: item.calibration_failure.rewording })}</> : null}
          </span>
        </li>
      ))}
    </ul>
  );
}

/** R28: in the risk acknowledgement, each unmet point with its quote and Judged wrongly beside it. */
export function UnmetPointsList({ agentId, candidateId, executionId }: { agentId: string; candidateId: string; executionId: string }) {
  const t = useTranslations("agents_marketplace");
  const [items, setItems] = useState<AgentVersionUnmetPoint[] | null>(null);
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    let cancelled = false;
    void getAgentVersionUnmetPoints(agentId, candidateId, executionId).then((value) => {
      if (!cancelled) setItems(value.items);
    }).catch(() => {
      if (!cancelled) setFailed(true);
    });
    return () => { cancelled = true; };
  }, [agentId, candidateId, executionId]);
  if (failed) return <p className="am-ci-note">{t("eval_points_unmet_failed")}</p>;
  if (!items?.length) return null;
  return (
    <ul className="am-ci-point-list" aria-label={t("eval_points_unmet_label")}>
      {items.map((item) => (
        <li key={`${item.case_revision_id}:${item.point_id}`} className="am-ci-point" data-verdict="unmet">
          <span className="am-ci-point-head">
            <b>{item.case_label || item.case_key}</b>
            <span>{item.text}</span>
          </span>
          {item.quote ? <q className="am-ci-point-quote">{item.quote}</q> : null}
          <JudgedWrongly agentId={agentId} pointId={item.point_id} pointText={item.text} caseResult={{ run_id: item.run_id, case_revision_id: item.case_revision_id, attempt: item.attempt }} />
        </li>
      ))}
    </ul>
  );
}
