import { describe, expect, it } from "vitest";

import {
  buildDiagnosticCopyBundle,
  normalizeErrorDiagnostics,
  parseErrorDiagnosticsWire,
  parsePublishedErrorDiagnostics,
  parseValidationIssues,
  projectClientBoundaryFailure,
  projectTransportFailure,
  toErrorPresentation,
  toFieldValidationIssues,
} from "./error-presentation";

const REQUEST_ID = "0123456789abcdef0123456789abcdef";
const OBSERVED_AT = "2026-07-29T10:30:00.000Z";

describe("published error diagnostics", () => {
  it("preserves a bounded provider message through Chat wire and diagnostic copy", () => {
    const parsed = parseErrorDiagnosticsWire({
      schema_version: 1,
      disclosure: "authenticated",
      diagnostic_code: "chat_llm_failed",
      provider_message: "Model ID is unavailable",
    });
    expect(parsed?.providerMessage).toBe("Model ID is unavailable");
    expect(buildDiagnosticCopyBundle({ diagnostics: parsed!, surface: "chat", version: "1.0" }))
      .toContain("provider_message: Model ID is unavailable");
  });

  it("omits an unsafe provider message while keeping the model failure facts", () => {
    const parsed = parseErrorDiagnosticsWire({
      schema_version: 1,
      disclosure: "authenticated",
      diagnostic_code: "chat_llm_failed",
      safe_detail: "phase=invoke; failure_class=request",
      provider_message: "See https://example.test/?api_key=secret",
    });
    expect(parsed?.safeDetail).toBe("phase=invoke; failure_class=request");
    expect(parsed?.providerMessage).toBeUndefined();
  });

  it.each([
    {
      detail: {
        code: "public_entry_not_registered",
        message: "Published message",
        diagnostics: diagnosticWire(),
      },
    },
    {
      detail: {
        error: {
          code: "public_entry_not_registered",
          message: "Published message",
          diagnostics: diagnosticWire(),
        },
      },
    },
    {
      error: {
        code: "public_entry_not_registered",
        message: "Published message",
        diagnostics: diagnosticWire(),
      },
    },
  ])("accepts each supported envelope shape", (body) => {
    expect(
      parsePublishedErrorDiagnostics(body, {
        status: 421,
        code: "public_entry_not_registered",
        requestId: REQUEST_ID,
        observedAt: OBSERVED_AT,
      }),
    ).toEqual({
      schemaVersion: 1,
      disclosure: "public-neutral",
      status: 421,
      code: "public_entry_not_registered",
      diagnosticCode: "public_entry_registry_miss",
      requestId: REQUEST_ID,
      retryable: false,
      observedAt: OBSERVED_AT,
    });
  });

  it("ignores the client-only digest field in HTTP diagnostics", () => {
    const diagnostics = parsePublishedErrorDiagnostics(
      {
        detail: {
          code: "public_entry_not_registered",
          diagnostics: {
            ...diagnosticWire(),
            digest: "backend-injected-digest",
          },
        },
      },
      {
        status: 421,
        code: "public_entry_not_registered",
        requestId: REQUEST_ID,
        observedAt: OBSERVED_AT,
      },
    );

    expect(diagnostics).toBeDefined();
    expect(diagnostics?.digest).toBeUndefined();
  });

  it.each([
    { ...diagnosticWire(), schema_version: 2 },
    { ...diagnosticWire(), disclosure: "owner" },
    { ...diagnosticWire(), diagnostic_code: "Not Stable" },
    { ...diagnosticWire(), retryable: "false" },
    { ...diagnosticWire(), request_id: "request-123" },
    { ...diagnosticWire(), safe_detail: "password=hunter2" },
    {
      ...diagnosticWire(),
      safe_detail: "https://internal.example/path?token=secret",
    },
    { ...diagnosticWire(), safe_detail: "/srv/bowhead/private/config.yaml" },
    { ...diagnosticWire(), safe_detail: "SELECT * FROM users" },
    { ...diagnosticWire(), safe_detail: "Traceback (most recent call last)" },
    { ...diagnosticWire(), safe_detail: "<!doctype html><html>" },
    { ...diagnosticWire(), safe_detail: "line\u0000break" },
  ])("drops the whole diagnostics object when a known field is unsafe", (diagnostics) => {
    const body = {
      detail: {
        code: "public_entry_not_registered",
        message: "Published message",
        diagnostics,
      },
    };
    expect(
      parsePublishedErrorDiagnostics(body, {
        status: 421,
        code: "public_entry_not_registered",
        requestId: REQUEST_ID,
        observedAt: OBSERVED_AT,
      }),
    ).toBeUndefined();
  });

  it("keeps the safe main projection when disclosure is unauthorized", () => {
    const presentation = toErrorPresentation(
      {
        code: "future_admin_error",
        message: "RAW_SQLITE_PATH_AND_SCHEMA",
        diagnostics: {
          schemaVersion: 1,
          disclosure: "admin",
          status: 500,
          diagnosticCode: "provider_failure",
          requestId: REQUEST_ID,
        },
      },
      {
        locale: "en-US",
        surface: "login",
        allowedDisclosures: ["public-neutral"],
        genericMessage: "Something went wrong.",
        messageForCode: () => null,
      },
    );

    expect(presentation).toEqual({ message: "Something went wrong." });
    expect(JSON.stringify(presentation)).not.toContain("RAW_SQLITE");
  });
});

describe("non-HTTP diagnostics wire", () => {
  it("parses a versioned authenticated Chat projection", () => {
    expect(
      parseErrorDiagnosticsWire({
        schema_version: 1,
        disclosure: "authenticated",
        diagnostic_code: "chat_turn_failed",
        occurrence_id: REQUEST_ID,
        turn_id: "t_safe_123",
        safe_detail: "phase=chat; failure_class=timeout",
        retryable: true,
      }),
    ).toEqual({
      schemaVersion: 1,
      disclosure: "authenticated",
      diagnosticCode: "chat_turn_failed",
      occurrenceId: REQUEST_ID,
      turnId: "t_safe_123",
      safeDetail: "phase=chat; failure_class=timeout",
      retryable: true,
    });
  });

  it("drops the whole Chat projection when a known field is unsafe", () => {
    expect(
      parseErrorDiagnosticsWire({
        schema_version: 1,
        disclosure: "authenticated",
        diagnostic_code: "chat_turn_failed",
        occurrence_id: REQUEST_ID,
        turn_id: "t_safe_123",
        safe_detail: "password=raw-secret",
        retryable: true,
      }),
    ).toBeUndefined();
  });
});

describe("request validation projection", () => {
  it("keeps only finite issues, safe paths and bounded params", () => {
    const body = {
      detail: {
        code: "request_validation_failed",
        message: "Check the highlighted fields and try again.",
        field_errors: [
          {
            path: "display_name",
            code: "value_too_short",
            params: { min_length: 2 },
            msg: "Value error, password=hunter2",
            input: "private user value",
          },
          {
            path: "password",
            code: "invalid_value",
            params: {},
          },
          {
            path: "items.0.name",
            code: "invalid_value",
            params: {},
          },
          {
            path: "email",
            code: "future_pydantic_type",
            params: {},
          },
          {
            code: "invalid_value",
            params: {},
          },
        ],
      },
    };

    const issues = parseValidationIssues(body);
    expect(issues).toEqual([
      {
        path: "display_name",
        code: "value_too_short",
        params: { min_length: 2 },
      },
      { code: "invalid_value", params: {} },
    ]);
    expect(JSON.stringify(issues)).not.toContain("hunter2");
    expect(JSON.stringify(issues)).not.toContain("private user value");
  });

  it("maps only fields explicitly owned by a form", () => {
    const validationIssues = [
      { path: "email", code: "invalid_format", params: {} },
      { path: "display_name", code: "field_required", params: {} },
      { code: "invalid_value", params: {} },
    ] as const;

    expect(
      toFieldValidationIssues(
        { validationIssues },
        ["email"],
      ),
    ).toEqual([
      { path: "email", code: "invalid_format", params: {} },
    ]);
  });
});

describe("transport projection and copy bundle", () => {
  it("projects timeout using only caller-proven facts", () => {
    const presentation = projectTransportFailure(
      {
        phase: "receive",
        timeoutTriggered: true,
        userAborted: false,
        onlineHint: true,
      },
      {
        requestTimedOut: "The request timed out.",
        serviceUnreachable: "Service unavailable.",
        retry: "Try again",
      },
    );

    expect(presentation).toMatchObject({
      message: "The request timed out.",
      action: { kind: "retry", label: "Try again" },
      diagnostics: {
        schemaVersion: 1,
        disclosure: "public-neutral",
        code: "request_timed_out",
        safeDetail: "phase=receive",
        retryable: true,
        serverRequestIdUnavailable: true,
      },
    });
    expect(presentation?.diagnostics?.occurrenceId).toMatch(/^[0-9a-f]{32}$/);
    expect(presentation?.diagnostics?.requestId).toBeUndefined();
  });

  it("does not turn an explicit user abort into an error", () => {
    expect(
      projectTransportFailure(
        {
          phase: "stream",
          timeoutTriggered: false,
          userAborted: true,
        },
        {
          requestTimedOut: "timeout",
          serviceUnreachable: "unreachable",
          retry: "retry",
        },
      ),
    ).toBeNull();
  });

  it("serializes a fixed, redacted and deterministic support bundle", () => {
    const diagnostics = normalizeErrorDiagnostics({
      schemaVersion: 1,
      disclosure: "public-neutral",
      status: 421,
      code: "public_entry_not_registered",
      diagnosticCode: "public_entry_registry_miss",
      requestId: REQUEST_ID,
      retryable: false,
      observedAt: OBSERVED_AT,
      ignoredBody: "password=hunter2",
    });
    expect(diagnostics).toBeDefined();

    const bundle = buildDiagnosticCopyBundle({
      diagnostics: diagnostics!,
      surface: "login",
      version: "1.2.3",
    });
    expect(bundle).toBe(
      [
        "Bowhead error diagnostic v1",
        "surface: login",
        "version: 1.2.3",
        `observed_at: ${OBSERVED_AT}`,
        "http_status: 421",
        "error_code: public_entry_not_registered",
        "diagnostic_code: public_entry_registry_miss",
        `request_id: ${REQUEST_ID}`,
        "retryable: false",
      ].join("\n"),
    );
    expect(bundle).not.toContain("ignoredBody");
    expect(bundle).not.toContain("hunter2");
  });
});

describe("client error boundary projection", () => {
  const messages = {
    routeRenderFailed: "The route failed.",
    globalRenderFailed: "The app failed.",
    chunkLoadFailed: "The app was updated.",
    retry: "Try again",
    reload: "Reload",
  };

  it("publishes only finite route facts without reading message or stack", () => {
    const error = {
      name: "Error",
      digest: "next-digest-123",
      get message(): never {
        throw new Error("message must not be read");
      },
      get stack(): never {
        throw new Error("stack must not be read");
      },
    };

    const presentation = projectClientBoundaryFailure(
      error,
      "route",
      messages,
    );

    expect(presentation).toMatchObject({
      message: "The route failed.",
      action: { kind: "retry", label: "Try again" },
      diagnostics: {
        schemaVersion: 1,
        disclosure: "authenticated",
        code: "route_render_failed",
        diagnosticCode: "route_error_boundary_caught",
        digest: "next-digest-123",
        safeDetail: "phase=render",
        retryable: true,
      },
    });
    expect(presentation.diagnostics?.occurrenceId).toMatch(
      /^[0-9a-f]{32}$/,
    );
    expect(JSON.stringify(presentation)).not.toContain(
      "message must not be read",
    );
  });

  it("classifies chunks by finite name equality and drops an unsafe digest", () => {
    const presentation = projectClientBoundaryFailure(
      {
        name: "ChunkLoadError",
        digest: "https://internal.example/private",
        message: "Loading chunk 12 failed at https://secret.example",
      },
      "route",
      messages,
    );

    expect(presentation).toMatchObject({
      message: "The app was updated.",
      action: { kind: "refresh", label: "Reload" },
      diagnostics: {
        code: "client_chunk_load_failed",
        diagnosticCode: "client_chunk_load_failed",
        safeDetail: "phase=chunk_load",
      },
    });
    expect(presentation.diagnostics?.digest).toBeUndefined();
    expect(JSON.stringify(presentation)).not.toContain("secret.example");
  });

  it("uses a public-neutral projection for the root boundary", () => {
    const presentation = projectClientBoundaryFailure(
      {
        get name(): never {
          throw new Error("unsafe getter");
        },
        get digest(): never {
          throw new Error("unsafe getter");
        },
      },
      "global",
      messages,
    );

    expect(presentation).toMatchObject({
      message: "The app failed.",
      diagnostics: {
        disclosure: "public-neutral",
        code: "global_render_failed",
        diagnosticCode: "global_error_boundary_caught",
      },
    });
  });
});

describe("safe_detail 的长度预算", () => {
  // Server `chat_llm_failed` detail includes the three extra upstream_type/code/param components,
  // so its policy cap is 420 including API surface and uncertain outcome. A lower frontend cap **discards the entire block rather than truncating** (optionalValue → null
  // → normalizeErrorDiagnostics entirely undefined), leaving the page without even phase/http_status.
  // Gateway test_error_diagnostics_detail_budget_parity locks the server side.
  const realistic =
    "phase=invoke; failure_class=request; provider_family=openai_compatible; " +
    "http_status=400; stream_state=not_started; fallback=not_attempted; attempts=1; " +
    "upstream_type=invalid_request_error; upstream_code=unsupported_value; " +
    "upstream_param=temperature";

  it("收得下服务端允许发的最长 detail（含 provider 指认的三段）", () => {
    expect(
      normalizeErrorDiagnostics({
        schemaVersion: 1,
        disclosure: "authenticated",
        safeDetail: realistic,
      })?.safeDetail,
    ).toBe(realistic);

    expect(
      normalizeErrorDiagnostics({
        schemaVersion: 1,
        disclosure: "authenticated",
        safeDetail: "a".repeat(420),
      })?.safeDetail,
    ).toHaveLength(420);
  });

  it("rejects diagnostics exceeding the detail budget", () => {
    expect(
      normalizeErrorDiagnostics({
        schemaVersion: 1,
        disclosure: "authenticated",
        safeDetail: "a".repeat(421),
      }),
    ).toBeUndefined();
  });
});

function diagnosticWire() {
  return {
    schema_version: 1,
    disclosure: "public-neutral",
    diagnostic_code: "public_entry_registry_miss",
    retryable: false,
  };
}
