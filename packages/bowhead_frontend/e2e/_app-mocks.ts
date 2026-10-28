import type { Page } from "@playwright/test";

type MockAuthedAppOptions = {
  catalog?: Record<string, unknown>;
  effectiveTheme?: unknown;
  personalAppearance?: unknown;
  composerSubmissionProtocol?: boolean;
  planning?: boolean;
  subagentManagement?: boolean;
  kbHealthObservation?: boolean;
  kbUseGuard?: boolean;
};

const defaultCatalog = {
  version: 3,
  providers: [{
    id: "deepseek_anthropic",
    display_name: "DeepSeek",
    type: "anthropic_compatible",
    models: ["deepseek-v4-pro"],
    supports: { tool_use: true },
  }],
  default_model: { provider: "deepseek_anthropic", model: "deepseek-v4-pro" },
  kb: { active: {}, reindex: { state: "idle" } },
};

const defaultEffectiveTheme = {
  effective: {
    version: 1,
    mode: "system",
    colorProfile: { type: "preset", id: "bowhead" },
    typographyProfile: { type: "preset", id: "bowhead" },
    typeScale: "standard",
    density: "comfortable",
    radius: "balanced",
    motion: "standard",
    chartPalette: "default",
  },
  locks: {},
  source: {
    mode: "product",
    colorProfile: "product",
    typographyProfile: "product",
    typeScale: "product",
    density: "product",
    radius: "product",
    motion: "product",
    chartPalette: "product",
  },
  userConfig: { version: 1 },
  team: {},
};

const themeTokens = {
  light: {
    background: "#FAF9F6", surface: "#FFFFFF", surface2: "#F4F1EB", surface3: "#ECE7DF",
    line: "#DED8CE", line2: "#C8C0B4", ink: "#1C1917", ink2: "#57534E",
    ink3: "#78716C", ink4: "#A8A29E", accent: "#B04A2A", accentHover: "#963E23",
    accentInk: "#FFFFFF", accentSoft: "#E8B8A7", accentTint: "#F8EAE4", focus: "#B04A2A",
    positive: "#34785D", positiveSoft: "#DCEEE6", warning: "#A76418", warningSoft: "#F8E9D1",
    error: "#B33A3A", errorSoft: "#F8DEDE", info: "#376E8A", infoSoft: "#DDECF3",
  },
  dark: {
    background: "#131210", surface: "#1D1B18", surface2: "#26231F", surface3: "#302C27",
    line: "#3A3631", line2: "#514B44", ink: "#F5F2EB", ink2: "#D6D0C5",
    ink3: "#AAA298", ink4: "#817A72", accent: "#E18A68", accentHover: "#F0A083",
    accentInk: "#23110A", accentSoft: "#703D2B", accentTint: "#35231C", focus: "#E18A68",
    positive: "#68B796", positiveSoft: "#203B31", warning: "#D9A253", warningSoft: "#42321D",
    error: "#E27B7B", errorSoft: "#482525", info: "#79B2D0", infoSoft: "#223743",
  },
};

export const E2E_PERSONAL_APPEARANCE = {
  schemaVersion: 3,
  materialized: false,
  rowVersion: 0,
  overrides: {},
  effective: {
    mode: "system",
    typographyProfile: { type: "preset", id: "bowhead" },
    typeScale: "standard",
    density: "comfortable",
    radius: "balanced",
    motion: "standard",
    colorScheme: {
      tokenSchemaVersion: 2,
      ...themeTokens,
      checksum: `sha256:${"a".repeat(64)}`,
    },
  },
  sources: {
    mode: "product", accent: "product", background: "product",
    typographyProfile: "product", typeScale: "product", density: "product",
    radius: "product", motion: "product",
  },
  team: { revisionId: null, revisionNumber: 0 },
  registries: {
    palette: {
      version: 1,
      items: [{
        id: "saas-blue", labelKey: "appearance.palette.saasBlue",
        value: {
          accent: { kind: "custom", seed: "#2563EB" },
          background: { kind: "custom", light: "#F8FAFC", dark: "#0F172A" },
        },
        swatch: {
          light: { primary: "#2563EB", secondary: "#F8FAFC", onPrimary: "#FFFFFF" },
          dark: { primary: "#427FFF", secondary: "#0F172A", onPrimary: "#07142D" },
        },
        availability: "available",
      }],
    },
    accent: {
      version: 1,
      items: [{
        id: "ocean", labelKey: "appearance.accent.ocean",
        value: { kind: "preset", registryVersion: 1, id: "ocean" },
        swatch: {
          light: { primary: "#2F6F84", onPrimary: "#FFFFFF" },
          dark: { primary: "#73B6C9", onPrimary: "#102126" },
        },
        availability: "available",
      }],
    },
    background: {
      version: 1,
      items: [{
        id: "pure-white", labelKey: "appearance.background.pureWhite",
        value: { kind: "preset", registryVersion: 1, id: "pure-white" },
        swatch: {
          light: { primary: "#FFFFFF", secondary: "#F7F7F7", onPrimary: "#1C1917" },
          dark: { primary: "#121212", secondary: "#1A1A1A", onPrimary: "#F5F2EB" },
        },
        availability: "available",
      }],
    },
  },
  degradations: [],
  pendingUndo: null,
};

export async function mockAuthedApp(page: Page, options: MockAuthedAppOptions = {}) {
  const json = (body: unknown, status = 200) => ({
    status,
    contentType: "application/json",
    body: JSON.stringify(body),
  });

  await page.route("**/api/v1/auth/me", async (route) => {
    const submissionProtocol = options.composerSubmissionProtocol ?? false;
    const planning = options.planning ?? false;
    const subagentManagement = options.subagentManagement ?? false;
    await route.fulfill(json({
      id: "u_smoke",
      email: "smoke@example.com",
      name: "Smoke User",
      tier: "pro",
      capability_revision: "composer-e2e-v1",
      fetched_at: "2026-08-03T00:00:00Z",
      auth_session_generation: "auth-generation-smoke-1",
      credit_balance: 1_000,
      credit_quota: 10_000,
      team_id: "team_smoke",
      team_role: subagentManagement ? "owner" : undefined,
      is_team_admin: subagentManagement,
      capabilities: planning || submissionProtocol || subagentManagement || options.kbHealthObservation ? {
        ...(submissionProtocol ? {
          composer: {
            model_panel_v2: false,
            thinking_effort: false,
            submission_protocol: true,
          },
        } : {}),
        planning,
        subagent_management_v2: subagentManagement,
        kb_health_observation: options.kbHealthObservation ?? false,
        kb_use_guard: options.kbUseGuard ?? false,
      } : {},
    }));
  });
  await page.route("**/api/v1/auth/me/workspaces", async (route) => {
    await route.fulfill(json({
      items: [{
        id: "ws_smoke",
        workspace_id: "ws_smoke",
        name: "Smoke workspace",
        role: "owner",
      }],
      active_id: "ws_smoke",
    }));
  });
  await page.route("**/api/v1/user-settings**", async (route) => {
    await route.fulfill(json({ settings: {} }));
  });
  await page.route("**/api/v1/theme/effective", async (route) => {
    await route.fulfill(json(options.effectiveTheme ?? defaultEffectiveTheme));
  });
  await page.route("**/api/v1/theme/personal", async (route) => {
    await route.fulfill({
      ...json(options.personalAppearance ?? E2E_PERSONAL_APPEARANCE),
      headers: { ETag: `"personal-v3:${"b".repeat(64)}"` },
    });
  });
  await page.route("**/api/v1/team-settings/theme-policy", async (route) => {
    await route.fulfill(json({ value: null }));
  });
  await page.route("**/api/v1/llm/catalog", async (route) => {
    await route.fulfill(json(options.catalog ?? defaultCatalog));
  });
  await page.route("**/api/v1/sessions**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (/\/api\/v1\/sessions\/[^/]+\/documents$/.test(path)) {
      await route.fulfill(json({ jobs: [], next_cursor: null }));
      return;
    }
    if (/\/api\/v1\/sessions\/[^/]+$/.test(path)) {
      const sessionId = path.split("/").pop();
      await route.fulfill(json({
        session: {
          id: sessionId,
          workspace_id: "ws_smoke",
          title: "Composer workbench fixture",
          started_at: "2026-08-03T00:00:00Z",
          last_active_at: "2026-08-03T00:00:00Z",
          status: "completed",
          cost_usd: 0,
          tokens_total: 0,
          intents: [],
        },
        turns: [{
          id: "t_composer_workbench",
          seq: 1,
          user_text: "Existing workbench turn",
          intent: "open_qa",
          status: "completed",
          execution_generation: 1,
          started_at: "2026-08-03T00:00:00Z",
          ended_at: "2026-08-03T00:00:01Z",
          final_answer: "Existing answer",
          error: null,
          error_kind: null,
          agent_type: "lead",
          attachments: [],
          trace_steps: [],
          summary: null,
        }],
      }));
      return;
    }
    await route.fulfill(json({ items: [] }));
  });
  await page.route("**/api/v1/home/snapshot**", async (route) => {
    const workspaceId = new URL(route.request().url()).searchParams.get("workspace_id") ?? "ws_smoke";
    await route.fulfill(json({
      version: "home.snapshot.v1",
      generatedAt: "2026-07-17T08:00:00Z",
      scope: {
        workspaceId,
        workspaceName: "Smoke workspace",
      },
      attentionScope: "user",
      attention: [],
      active: [],
      outputs: [],
      counts: {
        attention: 0,
        active: 0,
        outputs: 0,
      },
      partial: false,
      issues: [],
    }));
  });
  await page.route("**/api/v1/home/proposals**", async (route) => {
    await route.fulfill(json({ detail: "home proposals disabled" }, 404));
  });
  await page.route("**/api/v1/canvas/by-session/**", async (route) => {
    await route.fulfill(json({
      canvas: null,
      sections: [],
      sourceRefs: {},
    }));
  });
  await page.route("**/api/v1/turns/*/events", async (route) => {
    await route.fulfill(json({ events: [] }));
  });
  await page.route("**/api/v1/memory/snapshot", async (route) => {
    await route.fulfill(json({ items: [] }));
  });
  await page.route("**/api/v1/metrics/event", async (route) => {
    await route.fulfill(json({ ok: true }));
  });
  await page.route("**/api/v1/metrics/composer-event", async (route) => {
    await route.fulfill(json({ ok: true, duplicate: false }));
  });
}

/**
 * Turn on the Results workspace the way a deployment does: `/health` reports
 * `feature_flags.results_workspace` and the capabilities endpoint is absent
 * (the older backend shape), so negotiation falls back to the health flag.
 * The `bowhead.feature.results_workspace` localStorage override is read only
 * by development builds, so on its own it cannot enable the workspace in the
 * production build that the e2e gate runs against.
 */
export async function enableResultsWorkspace(page: Page) {
  await page.route("**/health", async (route) => {
    const response = await route.fetch().catch(() => null);
    const body: Record<string, unknown> = response?.ok()
      ? await response.json().catch(() => ({}))
      : {};
    const flags = body.feature_flags && typeof body.feature_flags === "object"
      ? body.feature_flags as Record<string, unknown>
      : {};
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ ...body, feature_flags: { ...flags, results_workspace: true } }),
    });
  });
  await page.route("**/api/v1/results/capabilities", (route) =>
    route.fulfill({ status: 404, contentType: "application/json", body: JSON.stringify({ detail: "not found" }) }),
  );
  await page.addInitScript(() => window.localStorage.setItem("bowhead.feature.results_workspace", "1"));
}
