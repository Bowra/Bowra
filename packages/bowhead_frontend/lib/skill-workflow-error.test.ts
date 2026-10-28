import { afterEach, describe, expect, it } from "vitest";

import { ApiError } from "./api-client";
import {
  semanticSkillWorkflowErrorPresentation,
  skillWorkflowErrorPresentation,
} from "./skill-workflow-error";

const copy = {
  generic: "Safe generic failure",
  transport: {
    requestTimedOut: "Request timed out",
    serviceUnreachable: "Service unreachable",
    retry: "Retry",
  },
};

afterEach(() => {
  document.documentElement.lang = "";
});

describe("skillWorkflowErrorPresentation", () => {
  it("shows actionable localized guidance for an unlicensed MCP feature", () => {
    const result = skillWorkflowErrorPresentation(
      new ApiError(403, "未授权功能: mcp", {
        code: "feature_unlicensed",
        body: {
          detail: {
            error: {
              code: "feature_unlicensed",
              details: { feature: "mcp" },
            },
          },
        },
      }),
      { surface: "settings", copy },
    );

    expect(result.message).toBe(
      "当前许可证未启用 MCP 功能，暂时无法使用。请联系管理员使用包含该功能的许可证更新授权。",
    );
  });

  it("explains how to recover from a stale space selection", () => {
    const result = skillWorkflowErrorPresentation(
      new ApiError(403, "ws_access_denied:not_a_member", {
        code: "ws_access_denied:not_a_member",
      }),
      { surface: "settings", copy },
    );

    expect(result.message).toBe(
      "当前选择的空间不可访问。请切换到你所属的空间后重试；如果仍无法访问，请联系空间管理员将你加入该空间。",
    );
  });

  it("shows a localized message only when the named publisher authenticates it", () => {
    const error = new ApiError(404, "The skill could not be found.", {
      code: "skill_not_found",
      diagnostics: {
        schemaVersion: 1,
        disclosure: "authenticated",
        status: 404,
        code: "skill_not_found",
        diagnosticCode: "skill_workflow_failure",
        safeDetail: "phase=lookup; failure_class=not_found",
        retryable: false,
      },
    });

    const result = skillWorkflowErrorPresentation(error, {
      surface: "settings",
      copy,
    });

    expect(result.message).toBe("The skill could not be found.");
    expect(result.diagnostics?.code).toBe("skill_not_found");
  });

  it("does not promote an untyped exception message", () => {
    const secret = "password=hunter2 at /srv/private/config.yaml";
    const result = skillWorkflowErrorPresentation(new Error(secret), {
      surface: "settings",
      copy,
    });

    expect(result.message).toBe(copy.generic);
    expect(JSON.stringify(result)).not.toContain(secret);
    expect(result.diagnostics).toBeUndefined();
  });

  it("fails closed for an API message without published diagnostics", () => {
    const secret = "browser public URL is not registered: https://internal";
    const result = skillWorkflowErrorPresentation(
      new ApiError(422, secret, { code: "unknown_legacy_code" }),
      { surface: "settings", copy },
    );

    expect(result).toEqual({ message: "Safe generic failure" });
    expect(JSON.stringify(result)).not.toContain(secret);
  });

  it("keeps finite framework validation facts without field values", () => {
    const error = new ApiError(422, "Check the highlighted fields and try again.", {
      code: "request_validation_failed",
      diagnostics: {
        schemaVersion: 1,
        disclosure: "public-neutral",
        status: 422,
        code: "request_validation_failed",
        diagnosticCode: "request_validation_failure",
        safeDetail: "phase=validate_input; failure_class=validation",
        retryable: false,
      },
    });

    const result = skillWorkflowErrorPresentation(error, {
      surface: "settings",
      copy,
    });

    expect(result.message).toBe("Check the highlighted fields and try again.");
    expect(result.diagnostics?.diagnosticCode).toBe(
      "request_validation_failure",
    );
  });
});

describe("semanticSkillWorkflowErrorPresentation", () => {
  it("keeps a catalog message and finite diagnostics from a semantic failure", () => {
    const result = semanticSkillWorkflowErrorPresentation(
      {
        code: "workflow_distill_timeout",
        message: "Workflow distillation timed out.",
        diagnostics: {
          schema_version: 1,
          disclosure: "authenticated",
          diagnostic_code: "skill_workflow_failure",
          safe_detail: "phase=distill; failure_class=timeout",
          retryable: true,
        },
      },
      "Safe fallback",
    );

    expect(result.message).toBe("Workflow distillation timed out.");
    expect(result.diagnostics?.code).toBe("workflow_distill_timeout");
  });

  it("does not display legacy semantic prose without the publisher marker", () => {
    const secret = "Traceback: token=secret";
    const result = semanticSkillWorkflowErrorPresentation(
      {
        code: "workflow_distill_failed",
        message: secret,
        diagnostics: null,
      },
      "Safe fallback",
    );

    expect(result).toEqual({ message: "Safe fallback" });
    expect(JSON.stringify(result)).not.toContain(secret);
  });
});
