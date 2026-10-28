import { describe, expect, it } from "vitest";
import { terminalErrorDiagnostics } from "./chat-failure-diagnostics";

const payload = {
  diagnostics: {
    schema_version: 1, disclosure: "authenticated", diagnostic_code: "chat_llm_failed",
    code: "llm_request_rejected", turn_id: "t_model", occurrence_id: "a".repeat(32),
    safe_detail: "phase=invoke; failure_class=request; http_status=400; api_surface=responses; outcome=unknown",
    provider_message: "Unsupported request", retryable: false,
  },
};

describe("terminalErrorDiagnostics", () => {
  it("keeps the same provider facts for live events and persisted history", () => {
    const live = terminalErrorDiagnostics(payload, "llm_request_rejected", "t_model");
    const history = terminalErrorDiagnostics(JSON.parse(JSON.stringify(payload)), "llm_request_rejected", "t_model");
    expect(history).toEqual(live);
    expect(history).toMatchObject({ code: "llm_request_rejected", providerMessage: "Unsupported request", retryable: false });
    expect(history?.safeDetail).toContain("api_surface=responses");
  });

  it("rejects another turn's diagnostic and sensitive provider text", () => {
    expect(terminalErrorDiagnostics(payload, "llm_request_rejected", "t_other")).toBeUndefined();
    expect(terminalErrorDiagnostics({ diagnostics: { ...payload.diagnostics, provider_message: "Authorization: Bearer credential" } }, "llm_request_rejected", "t_model")?.providerMessage).toBeUndefined();
  });
});
