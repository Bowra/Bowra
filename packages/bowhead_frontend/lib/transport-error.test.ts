import { describe, expect, it } from "vitest";

import { adminCreditsErrorKey } from "./admin-credits-error";
import { emailConfigErrorKeys } from "./email-config-error";
import { fileError } from "./file-error";
import { kbFileError } from "./kb-file-error";
import { kbDocTypeError } from "./kb-doc-type-error";
import { kbRecordError } from "./kb-record-error";
import { kbMatrixMutationError } from "./kb-matrix/mutation-error";
import { kbMatrixRunErrorKey } from "./kb-matrix/run-error";
import { kbTeamTemplateError } from "./kb-matrix/team-template-error";
import { modelSettingsErrorKey } from "./model-settings-error";
import { spaceMemberErrorKey } from "./space-member-error";
import { sessionLoadFailedKey } from "./session-load-error";
import { classifyResultsError, classifyResultsResponse } from "./results-workspace-errors";
import { isNetworkFailure } from "./transport-error";

import { saveToMemoryFailedKey } from "@/components/canvas/save-to-memory-button";
import { createToolFailureKey } from "@/components/canvas/sections/create-tool-card";
import { subagentDraftFailureKey } from "@/components/canvas/sections/subagent-draft-errors";
import { createAgentErrorKey } from "@/components/canvas/sections/create-subagent-errors";
import { planActionErrorKey } from "@/components/chat/plan-card/plan-action-errors";
import { rewriteErrorKind } from "./use-ai-rewrite";
import { ruleClarificationErrorPresentation } from "./rule-clarification-error";
import { skillWorkflowErrorPresentation } from "./skill-workflow-error";

const adapters: Array<[string, (error: unknown) => unknown]> = [
  ["credits", adminCreditsErrorKey],
  ["email", emailConfigErrorKeys],
  ["file upload", (error) => fileError(error, "upload")],
  ["KB download", (error) => kbFileError(error, "download")],
  ["document types", (error) => kbDocTypeError(error, "load")],
  ["records", (error) => kbRecordError(error, "edit")],
  ["matrix mutation", kbMatrixMutationError],
  ["matrix run", kbMatrixRunErrorKey],
  ["team template", (error) => kbTeamTemplateError(error, "copy")],
  ["models", modelSettingsErrorKey],
  ["members", spaceMemberErrorKey],
  ["session", sessionLoadFailedKey],
  ["results", classifyResultsError],
  ["memory save", saveToMemoryFailedKey],
  ["tool creation", (error) => createToolFailureKey("create", error)],
  ["Agent draft", subagentDraftFailureKey],
  ["Agent creation", createAgentErrorKey],
  ["plan action", (error) => planActionErrorKey(error, "edit")],
  ["report rewrite", rewriteErrorKind],
];

describe("transport classification", () => {
  it.each(adapters)("%s separates connection failures from programming errors", (_name, resolve) => {
    expect(JSON.stringify(resolve(new TypeError("Failed to fetch")))).toContain("network");
    for (const error of [new TypeError("network configuration is undefined"), new Error("Failed to fetch"), new RangeError("private detail")]) {
      expect(JSON.stringify(resolve(error))).not.toContain("network");
      expect(JSON.stringify(resolve(error))).not.toContain(error.message);
    }
  });

  it.each(["Failed to fetch", "NetworkError when attempting to fetch resource.", "Load failed"])("recognizes the browser transport shape %s", (message) => {
    expect(isNetworkFailure(new TypeError(message))).toBe(true);
  });

  it("does not classify cancellation or an HTTP response as a lost connection", () => {
    expect(isNetworkFailure(new DOMException("Cancelled", "AbortError"))).toBe(false);
    expect(classifyResultsResponse(503)).toBe("unknown");
  });
});

describe("published workflow and clarification transport fallbacks", () => {
  const copy = { generic: "This action could not be completed.", entitlementDenied: "Contact an administrator.", transport: {
    requestTimedOut: "The outcome could not be confirmed.", serviceUnreachable: "Could not connect.", retry: "Retry",
  }};
  it.each([
    ["workflow", (error: unknown) => skillWorkflowErrorPresentation(error, { surface: "task", copy })],
    ["clarification", (error: unknown) => ruleClarificationErrorPresentation(error, { surface: "chat", copy, retryAction: true })],
  ] as const)("%s diagnoses only a recognized browser transport failure", (_name, present) => {
    const unknown = present(new TypeError("Cannot read private configuration"));
    expect(unknown).toEqual({ message: copy.generic });
    const transport = present(new TypeError("Failed to fetch"));
    expect(transport.message).toBe(copy.transport.serviceUnreachable);
    expect(transport.diagnostics?.code).toBe("service_unreachable");
  });
});
