import { afterEach, describe, expect, it, vi } from "vitest";

import { fetchAdminMembers } from "./admin-members-api";
import { fetchTeamSpacesPage } from "./admin-spaces-api";
import { buildDiagnosticCopyBundle, errorDiagnosticsFrom } from "./error-presentation";

const requestId = "0123456789abcdef0123456789abcdef";
afterEach(() => vi.restoreAllMocks());

describe("domain error diagnostics", () => {
  it.each([
    ["members", () => fetchAdminMembers()],
    ["spaces", () => fetchTeamSpacesPage({ status: "active", pageSize: 20 })],
  ] as const)("preserves published correlation through the %s HTTP wrapper", async (_name, request) => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(JSON.stringify({
      error: {
        code: "validation_error",
        message: "private exception text",
        diagnostics: {
          schema_version: 1,
          disclosure: "public-neutral",
          diagnostic_code: "request_validation_failure",
          retryable: false,
        },
      },
    }), { status: 422, headers: { "content-type": "application/json", "x-request-id": requestId } }));
    const error = await request().catch((cause: unknown) => cause);
    expect(error).toMatchObject({ status: 422, requestId, code: "validation_error" });
    const diagnostics = errorDiagnosticsFrom(error);
    expect(diagnostics).toMatchObject({ requestId, diagnosticCode: "request_validation_failure", retryable: false });
    const bundle = buildDiagnosticCopyBundle({ diagnostics: diagnostics!, surface: "settings", version: "test" });
    expect(bundle).toContain(requestId);
    expect(bundle).not.toContain("private exception text");
  });
});
