import { describe, expect, it } from "vitest";

import type {
  FeishuDiagnostics,
  FeishuPublicHealth,
} from "./feishu-integration-api";
import {
  feishuAutomaticChecklist,
  feishuAutomaticChecksPassed,
  feishuCredentialGaps,
  feishuRuntimeState,
} from "./feishu-integration-state";

const health: FeishuPublicHealth = {
  ok: true,
  enabled: true,
  degraded: false,
};

const diagnostics: FeishuDiagnostics = {
  ...health,
  app_type: "internal",
  event_ingress: "webhook",
  group_public_answers_enabled: false,
  auth_mode: "oidc",
  prod_auth_mode: true,
  long_connection: null,
  missing_credentials: [],
  missing_config: [],
  app_id_configured: true,
  app_secret_configured: true,
  verification_token_configured: true,
  encrypt_key_configured: true,
  bot_open_id_configured: true,
  event_store_configured: true,
  event_subscription_url: "https://example.com/events",
  card_callback_url: "https://example.com/cards",
  tenant_token_cache_keys: [],
  required_event_subscriptions: [],
  required_permissions: [],
  setup_checklist: [
    { key: "automatic", label: "Automatic", status: "ok", detail: "Ready" },
    { key: "console", label: "Console", status: "manual", detail: "Human" },
  ],
};

describe("Feishu integration state", () => {
  it("keeps credential readiness visible while the integration is stopped", () => {
    const stoppedDiagnostics = {
      ...diagnostics,
      ok: true,
      enabled: false,
      missing_credentials: ["app_id", "app_secret"],
    };
    const stoppedHealth = { ok: true, enabled: false, degraded: false };

    expect(feishuCredentialGaps(stoppedDiagnostics)).toEqual([
      "app_id",
      "app_secret",
    ]);
    expect(feishuRuntimeState(stoppedDiagnostics, stoppedHealth)).toBe(
      "stopped",
    );
    expect(
      feishuAutomaticChecksPassed(stoppedDiagnostics, stoppedHealth),
    ).toBe(false);
  });

  it("keeps manual console work out of automatic diagnostics", () => {
    expect(feishuAutomaticChecklist(diagnostics)).toEqual([
      diagnostics.setup_checklist?.[0],
    ]);
  });

  it("treats a legacy disabled ingress as stopped even if enabled was stored", () => {
    const legacyDisabled = {
      ...diagnostics,
      event_ingress: "disabled",
    };

    expect(feishuRuntimeState(legacyDisabled, health)).toBe("stopped");
    expect(feishuAutomaticChecksPassed(legacyDisabled, health)).toBe(false);
  });

  it("returns a specific runtime reason instead of a generic error", () => {
    const degraded = {
      ...diagnostics,
      ok: false,
      degraded: true,
      missing_config: ["event_store"],
    };

    expect(feishuRuntimeState(degraded, { ...health, degraded: true })).toBe(
      "event_store",
    );
  });
});
