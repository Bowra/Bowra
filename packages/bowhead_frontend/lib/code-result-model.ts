import type { PyCellSpec, ReportSection } from "@/lib/types";
import type { PythonExecutionStep } from "@/lib/use-running-python";

type CodeSection = Extract<ReportSection, { kind: "sql_block" | "code_block" }>;

export type CodeOutputRef = {
  artifactId: string;
  filename: string | null;
  mime: string | null;
};

export type CodeRunItem = {
  key: string;
  language: "python" | "sql" | "shell";
  label: string | null;
  actor: "lead" | "subagent" | null;
  status: "running" | "success" | "error";
  code: string | null;
  codeTruncated: boolean;
  stdout: string | null;
  durationMs: number | null;
  outputs: readonly CodeOutputRef[];
};

function outputsFromSection(section: CodeSection): CodeOutputRef[] {
  if (section.kind !== "code_block") return [];
  const refs: Array<{ artifact_id: string; filename?: string; mime?: string }> = section.output_artifacts?.length
    ? section.output_artifacts
    : (section.output_artifact_ids ?? []).map((artifact_id) => ({ artifact_id }));
  return refs
    .filter((ref) => Boolean(ref.artifact_id))
    .map((ref) => ({
      artifactId: ref.artifact_id,
      filename: ref.filename ?? null,
      mime: ref.mime ?? null,
    }));
}

function runFromSection(section: CodeSection): CodeRunItem | null {
  const language = section.lang;
  const code = section.code;
  if (!code) return null;
  const outputs = outputsFromSection(section);
  return {
    key: `section:${section.id}`,
    language,
    label: outputs.map((output) => output.filename).find(Boolean)
      ?? (section.kind === "code_block"
        ? section.summary_payload?.statement_summary ?? null
        : null),
    actor: "lead",
    status: "success",
    code,
    codeTruncated: false,
    stdout: section.kind === "code_block" ? section.stdout_preview ?? null : null,
    durationMs: section.kind === "code_block"
      ? section.summary_payload?.dur_ms ?? section.dur_ms ?? null
      : null,
    outputs,
  };
}

function runFromLegacyCell(cell: PyCellSpec, index: number): CodeRunItem {
  return {
    key: `legacy-cell:${index}:${cell.n}`,
    language: "python",
    label: cell.name || null,
    actor: cell.subagentId ? "subagent" : "lead",
    status: cell.status,
    code: cell.code || null,
    codeTruncated: false,
    stdout: cell.output,
    durationMs: cell.durMs,
    outputs: [],
  };
}

export function projectCodeRuns({
  sections,
  executionSteps = [],
  legacyPyCells = [],
}: {
  sections: readonly ReportSection[];
  executionSteps?: readonly PythonExecutionStep[];
  legacyPyCells?: readonly PyCellSpec[];
}): CodeRunItem[] {
  const codeSections = sections.filter((section): section is CodeSection => (
    section.kind === "code_block"
  ));
  const sectionCallIds = new Set(codeSections
    .filter((section) => section.kind === "code_block")
    .map((section) => section.call_id));
  const eventRuns: CodeRunItem[] = executionSteps
    .filter((step) => !step.callId || !sectionCallIds.has(step.callId))
    .filter((step) => (
      step.state === "active"
      || Boolean(step.codeInput)
      || Boolean(step.stdout)
      || step.outputs.length > 0
    ))
    .map((step) => ({
    key: `event:${step.turnId}:${step.stepId}`,
    language: "python",
    label: step.label || null,
    actor: step.actor,
    status: step.state === "active"
      ? "running"
      : step.state === "failed"
        ? "error"
        : "success",
    code: step.codeInput?.code ?? null,
    codeTruncated: step.codeInput?.truncated ?? false,
    stdout: step.stdout,
    durationMs: step.durationMs,
    outputs: step.outputs,
  }));
  const completed = codeSections
    .map(runFromSection)
    .filter((run): run is CodeRunItem => run !== null);
  const running = eventRuns.filter((run) => run.status === "running");
  const completedEvents = eventRuns.filter((run) => run.status !== "running").reverse();
  return [
    ...running,
    ...completed,
    ...completedEvents,
    ...legacyPyCells.map(runFromLegacyCell),
  ];
}
