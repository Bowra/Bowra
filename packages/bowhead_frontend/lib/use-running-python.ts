/**
 * Durable Python execution projection for Results > Code.
 *
 * Python steps arrive as persisted step_started/step_done events. Reading all
 * turn-store entries for the current session makes live, archived and F5 replay
 * share one source instead of dropping the row when the active step completes.
 */

import { useMemo } from "react";

import type { StepCodeInput } from "@/components/chat/step-list/types";
import { useTurns } from "./turn-store";

export interface PythonExecutionOutput {
  artifactId: string;
  filename: string | null;
  mime: string | null;
}

export interface PythonExecutionStep {
  turnId: string;
  stepId: string;
  callId: string | null;
  label: string | null;
  actor: "lead" | "subagent";
  state: "active" | "done" | "failed" | "partial";
  codeInput: StepCodeInput | null;
  stdout: string | null;
  durationMs: number | null;
  outputs: readonly PythonExecutionOutput[];
}

export function useSessionPythonSteps(turnIds: readonly string[]): PythonExecutionStep[] {
  const turns = useTurns(turnIds);
  return useMemo(() => turns.flatMap((turn) => turn.steps
    .filter((step): step is typeof step & { state: PythonExecutionStep["state"] } => (
      step.toolName === "python_sandbox"
      && step.state !== "pending"
      // Preparing (model still writing parameters) is not execution: no codeInput, nothing running yet.
      // Including it flashes a running entry without code in Results > Code; when execution arrives,
      // closing writing (done, no code) filters it away. Execution steps (`step_<call_id>`) are the code entries,
      // matching step-list `foldWritingSteps`: show the execution step only after execution begins.
      && step.stepType !== "lead_thinking_writing"
    ))
    .map((step): PythonExecutionStep => ({
      turnId: turn.turnId,
      stepId: step.stepId,
      callId: step.callId,
      label: step.headerZh || step.label,
      actor: step.subagentId ? "subagent" : "lead",
      state: step.state,
      codeInput: step.codeInput,
      stdout: step.detail?.kind === "stdout" ? step.detail.text : null,
      durationMs: step.durationMs,
      outputs: (step.outputArtifacts ?? []).map((output) => ({
        artifactId: output.artifactId,
        filename: output.filename ?? null,
        mime: output.mime ?? null,
      })),
    }))), [turns]);
}
