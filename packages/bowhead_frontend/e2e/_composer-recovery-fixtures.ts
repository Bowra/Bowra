import type { Page } from "@playwright/test";

export async function routeSubmissionCapability(
  page: Page,
  enabled: () => boolean,
  referenceInput = false,
) {
  await page.route("**/api/v1/auth/me", async (route) => {
    const submissionProtocol = enabled();
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        id: "u_e2e",
        email: "e2e@bowhead.test",
        display_name: "E2E User",
        team_id: "team_test",
        tier: "ultra",
        role: "member",
        team_role: "member",
        status: "active",
        email_verified: true,
        capability_revision: submissionProtocol
          ? "composer-a-e2e-legacy-enabled"
          : "composer-a-e2e-default-off",
        fetched_at: new Date().toISOString(),
        auth_session_generation: "auth-generation-e2e-1",
        capabilities: {
          composer: {
            model_panel_v2: false,
            thinking_effort: false,
            submission_protocol: submissionProtocol,
            reference_input: referenceInput,
          },
          planning: true,
        },
      }),
    });
  });
}

export async function routeTerminalChatExecution(
  page: Page,
  writeProtocol: () => "terminal_v1" | "legacy_v0" = () => "terminal_v1",
  attachmentRefsWireVersion: 1 | null = 1,
) {
  await page.route("**/api/v1/auth/me", async (route) => {
    const terminalCapable = route.request().headers()[
      "x-bowhead-terminal-protocol-capability"
    ] === "1";
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        id: "u_e2e",
        email: "e2e@bowhead.test",
        display_name: "E2E User",
        team_id: "team_test",
        tier: "ultra",
        role: "member",
        team_role: "member",
        status: "active",
        email_verified: true,
        capability_revision: "terminal-v1-e2e",
        fetched_at: new Date().toISOString(),
        auth_session_generation: "auth-generation-e2e-1",
        chat_execution: {
          write_protocol: terminalCapable ? writeProtocol() : "legacy_v0",
          config_generation: "config-e2e-1",
          readiness_generation: "readiness-e2e-1",
          ...(attachmentRefsWireVersion === 1
            ? { attachment_refs_wire_version: 1 }
            : {}),
        },
        capabilities: {
          composer: {
            model_panel_v2: false,
            thinking_effort: false,
            submission_protocol: false,
          },
          planning: true,
        },
      }),
    });
  });
}
