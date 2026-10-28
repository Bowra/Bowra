import http from "node:http";
import {
  getBySession,
  getSessionArtifacts,
  primeTurn,
  resetCanvasMock,
  serveStream,
} from "./canvas-mock-scenarios.mjs";

const portArg = process.argv.indexOf("--port");
const port = Number(portArg >= 0 ? process.argv[portArg + 1] : process.env.PORT || 18080);
const llmV2Fixture = process.env.BOWHEAD_E2E_LLM_V2 === "1";
const llmV2ProviderId = "llmp_0123456789abcdef0123456789abcdef";
const llmV2PlanId = "lvplan_0123456789abcdef0123456789abcdef";
const llmV2BatchId = "lvb_0123456789abcdef0123456789abcdef";

const now = () => new Date().toISOString();

const corsHeaders = {
  "access-control-allow-origin": "*",
  "access-control-allow-methods": "GET,POST,PUT,PATCH,DELETE,OPTIONS",
  "access-control-allow-headers": "content-type,authorization,idempotency-key,if-match,if-none-match",
  "access-control-expose-headers": "etag",
};

function json(res, status, body, headers = {}) {
  const payload = JSON.stringify(body);
  res.writeHead(status, {
    ...corsHeaders,
    "content-type": "application/json; charset=utf-8",
    "content-length": Buffer.byteLength(payload),
    "cache-control": "no-store",
    ...headers,
  });
  res.end(payload);
}

function empty(res, status = 204) {
  res.writeHead(status, { ...corsHeaders, "cache-control": "no-store" });
  res.end();
}

function me() {
  const composerA = process.env.BOWHEAD_E2E_PROFILE === "sse";
  return {
    id: "u_e2e",
    email: "e2e@bowhead.test",
    display_name: "E2E User",
    avatar_url: null,
    team_id: "team_test",
    capability_revision: composerA ? "composer-a-e2e-v1" : "composer-off-e2e-v1",
    fetched_at: now(),
    auth_session_generation: "auth-generation-e2e-1",
    capabilities: composerA ? {
      composer: {
        model_panel_v2: false,
        thinking_effort: false,
        submission_protocol: false,
      },
      planning: true,
    } : {},
    tier: "ultra",
    role: llmV2Fixture ? "admin" : "member",
    team_role: llmV2Fixture ? "owner" : "member",
    status: "active",
    email_verified: true,
    credit_balance: 1000,
    credit_quota: 1000,
    locale: "zh-CN",
    timezone: "Asia/Shanghai",
    is_team_admin: llmV2Fixture,
    workspace_count: 4,
    needs_onboarding: false,
  };
}

const workspaces = [
  { workspace_id: "ws_test", name: "Test Workspace", description: "Default E2E workspace", role: "member" },
  { workspace_id: "ws_alice", name: "Alice Workspace", description: "Cross-scope fixture", role: "member" },
  { workspace_id: "ws_bob", name: "Bob Workspace", description: "Cross-scope fixture", role: "member" },
  { workspace_id: "ws1", name: "Workspace 1", description: "Legacy fixture", role: "member" },
];

function llmCatalog() {
  return {
    version: 1,
    providers: [
      {
        id: "deepseek_anthropic",
        display_name: "DeepSeek",
        type: "anthropic_compatible",
        models: ["deepseek-v4-pro"],
        supports: { chat: true },
      },
    ],
    default_model: { provider: "deepseek_anthropic", model: "deepseek-v4-pro" },
    kb: { active: {}, reindex: { state: "idle" } },
  };
}

function adminLlmConfig() {
  if (llmV2Fixture) {
    return {
      version: 7,
      config: {
        schema_version: 2,
        providers: {},
        tiers: {},
        kb: { models: {}, active: {}, pending_embed: null },
      },
      providers: [],
      features: {
        model_discovery_v2: true,
        capability_probes: true,
        azure_openai: true,
        custom_http: true,
      },
      provider_identities: {},
      verification: { as_of: now(), entries: [] },
      migration: {
        state: "not_enrolled",
        enrolled_config_version: 7,
        baseline_state: "none",
      },
      reindex: null,
    };
  }
  return {
    version: 1,
    config: {
      providers: {},
      kb: { models: {} },
    },
    providers: [],
    reindex: { state: "idle" },
  };
}

const productThemeDefault = {
  version: 1,
  mode: "system",
  colorProfile: { type: "preset", id: "bowhead" },
  typographyProfile: { type: "preset", id: "bowhead" },
  typeScale: "standard",
  density: "comfortable",
  radius: "balanced",
  motion: "standard",
  chartPalette: "default",
};

const baseThemeTokens = {
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

const personalPaletteFixtures = [
  ["bowhead-editorial", "appearance.palette.bowheadEditorial", "#B04A2A", "#F7F2E8", "#17130F"],
  ["saas-blue", "appearance.palette.saasBlue", "#2563EB", "#F8FAFC", "#0F172A"],
  ["product-indigo", "appearance.palette.productIndigo", "#635BFF", "#F6F9FC", "#101323"],
  ["collaboration-teal", "appearance.palette.collaborationTeal", "#0F766E", "#F0FDFA", "#0B1F1D"],
  ["focus-violet", "appearance.palette.focusViolet", "#7C3AED", "#FAF5FF", "#171126"],
  ["neutral-graphite", "appearance.palette.neutralGraphite", "#4F5B5C", "#F7F7F5", "#141617"],
].map(([id, labelKey, accent, lightBackground, darkBackground]) => ({
  id,
  labelKey,
  value: {
    accent: { kind: "custom", seed: accent },
    background: { kind: "custom", light: lightBackground, dark: darkBackground },
  },
  swatch: {
    light: { primary: accent, secondary: lightBackground, onPrimary: "#FFFFFF" },
    dark: { primary: accent, secondary: darkBackground, onPrimary: "#FFFFFF" },
  },
  availability: "available",
}));

const personalRegistries = {
  palette: {
    version: 1,
    items: personalPaletteFixtures,
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
};

let userAppearanceThemeConfig = { version: 1 };
let personalAppearanceOverrides = {};
let personalAppearanceRowVersion = 0;
let personalAppearanceUndo = null;
let teamThemePolicy = null;
const liveSseTurns = new Map();
const liveSseSessionActiveTurn = new Map();

function sparseUserSettings() {
  return Object.keys(userAppearanceThemeConfig).length > 1
    ? { appearance_theme_config: JSON.stringify(userAppearanceThemeConfig) }
    : {};
}

function effectiveTheme() {
  const teamDefaultColorProfile = teamThemePolicy?.defaults?.colorProfile;
  const teamLocksColorProfile = Boolean(teamThemePolicy?.locks?.colorProfile && teamDefaultColorProfile);
  const effective = {
    ...productThemeDefault,
    ...(teamDefaultColorProfile ? { colorProfile: teamDefaultColorProfile } : {}),
    ...userAppearanceThemeConfig,
  };
  if (userAppearanceThemeConfig.colorProfile) {
    effective.colorProfile = userAppearanceThemeConfig.colorProfile;
  }
  if (teamLocksColorProfile) {
    effective.colorProfile = teamDefaultColorProfile;
  }
  Object.assign(effective, Object.fromEntries(
    Object.entries(personalAppearanceOverrides).filter(([key]) => !["accent", "background"].includes(key)),
  ));
  const colorScheme = personalColorScheme(personalAppearanceOverrides);
  if (personalAppearanceOverrides.accent?.kind === "preset") {
    effective.colorProfile = {
      type: "preset",
      id: personalAppearanceOverrides.accent.id,
    };
  }
  return {
    schemaVersion: 2,
    brand: {
      applicationName: "Bowhead",
      logoAlt: "Bowhead",
      logoOnLight: { kind: "text", text: "Bowhead" },
      logoOnDark: { kind: "text", text: "Bowhead" },
      mark: { kind: "generatedInitial", text: "B" },
      favicon: { kind: "text", text: "Bowhead" },
    },
    effective: { ...effective, colorScheme },
    locks: {},
    source: {
      mode: personalAppearanceOverrides.mode || userAppearanceThemeConfig.mode ? "user" : "product",
      colorProfile: personalAppearanceOverrides.accent || userAppearanceThemeConfig.colorProfile ? "user" : teamDefaultColorProfile ? "team_default" : "product",
      colorScheme: personalAppearanceOverrides.accent || personalAppearanceOverrides.background ? "user" : teamDefaultColorProfile ? "team_default" : "product",
      typographyProfile: personalAppearanceOverrides.typographyProfile || userAppearanceThemeConfig.typographyProfile ? "user" : "product",
      typeScale: personalAppearanceOverrides.typeScale || userAppearanceThemeConfig.typeScale ? "user" : "product",
      density: personalAppearanceOverrides.density || userAppearanceThemeConfig.density ? "user" : "product",
      radius: personalAppearanceOverrides.radius || userAppearanceThemeConfig.radius ? "user" : "product",
      motion: personalAppearanceOverrides.motion || userAppearanceThemeConfig.motion ? "user" : "product",
      chartPalette: userAppearanceThemeConfig.chartPalette ? "user" : "product",
    },
    userConfig: userAppearanceThemeConfig,
    team: {
      id: "team_test",
      themeRevision: null,
      revisionId: null,
      configurationType: "legacy",
    },
  };
}

function personalColorScheme(overrides) {
  const light = { ...baseThemeTokens.light };
  const dark = { ...baseThemeTokens.dark };
  if (overrides.background?.kind === "preset" && overrides.background.id === "pure-white") {
    Object.assign(light, { background: "#FFFFFF", surface: "#F7F7F7" });
    Object.assign(dark, { background: "#121212", surface: "#1A1A1A" });
  } else if (overrides.background?.kind === "custom") {
    light.background = overrides.background.light;
    dark.background = overrides.background.dark;
  }
  if (overrides.accent?.kind === "preset" && overrides.accent.id === "ocean") {
    Object.assign(light, { accent: "#2F6F84", focus: "#2F6F84" });
    Object.assign(dark, { accent: "#73B6C9", focus: "#73B6C9" });
  } else if (overrides.accent?.kind === "custom") {
    light.accent = overrides.accent.seed;
    dark.accent = overrides.accent.seed;
  }
  return {
    tokenSchemaVersion: 2,
    light,
    dark,
    checksum: `sha256:${"c".repeat(64)}`,
  };
}

function personalAppearanceEtag() {
  return `"personal-v3:${personalAppearanceRowVersion.toString(16).padStart(64, "0")}"`;
}

function personalAppearanceResource() {
  const effective = effectiveTheme().effective;
  const sources = Object.fromEntries(
    ["mode", "accent", "background", "typographyProfile", "typeScale", "density", "radius", "motion"]
      .map((field) => [field, personalAppearanceOverrides[field] ? "user" : "product"]),
  );
  return {
    schemaVersion: 3,
    materialized: personalAppearanceRowVersion > 0,
    rowVersion: personalAppearanceRowVersion,
    overrides: personalAppearanceOverrides,
    effective: {
      mode: effective.mode,
      typographyProfile: effective.typographyProfile,
      typeScale: effective.typeScale,
      density: effective.density,
      radius: effective.radius,
      motion: effective.motion,
      colorScheme: effective.colorScheme,
    },
    sources,
    team: { revisionId: null, revisionNumber: 0 },
    registries: personalRegistries,
    degradations: [],
    pendingUndo: personalAppearanceUndo,
  };
}

function readJsonBody(req) {
  return new Promise((resolve) => {
    let body = "";
    req.setEncoding("utf8");
    req.on("data", (chunk) => {
      body += chunk;
    });
    req.on("end", () => {
      if (!body) {
        resolve({});
        return;
      }
      try {
        resolve(JSON.parse(body));
      } catch {
        resolve({});
      }
    });
    req.on("error", () => resolve({}));
  });
}

function sseWrite(res, event, payload) {
  res.write(`event: ${event}\n`);
  res.write(`data: ${JSON.stringify(payload)}\n\n`);
}

function emitLiveSse(turn, event, payload = {}) {
  if (!turn || turn.closed) return;
  const framePayload = {
    ...payload,
    seq: ++turn.seq,
    turn_id: turn.turnId,
  };
  turn.events.push({ event, payload: framePayload });
  for (const client of turn.clients) {
    sseWrite(client, event, framePayload);
  }
}

function closeLiveSseTurn(turn) {
  if (!turn || turn.closed) return;
  turn.closed = true;
  const active = liveSseSessionActiveTurn.get(turn.sessionId);
  if (active === turn.turnId) liveSseSessionActiveTurn.delete(turn.sessionId);
  for (const timer of turn.timers) clearTimeout(timer);
  for (const client of turn.clients) {
    try {
      client.end();
    } catch {
      // ignore
    }
  }
  turn.clients.clear();
}

function scheduleLiveSseTurn(turn) {
  if (turn.started) return;
  turn.started = true;
  emitLiveSse(turn, "turn_started", {
    user_text: turn.userText,
    session_id: turn.sessionId,
    execution_generation: 1,
  });
  turn.timers.push(setTimeout(() => {
    emitLiveSse(turn, "intent_classified", {
      intent: "lookup",
      complexity_score: 0.15,
      rationale: "E2E live SSE steer fixture",
    });
  }, 120));
  turn.timers.push(setTimeout(() => {
    emitLiveSse(turn, "partial_answer", {
      delta: "Live SSE turn is running. ",
    });
  }, 260));
  turn.timers.push(setTimeout(() => {
    emitLiveSse(turn, "done", {
      status: "completed",
      duration_ms: 5200,
      cost_usd: 0,
      tool_invocation_count: 0,
      response_kind: "chat",
    });
    closeLiveSseTurn(turn);
  }, 5200));
}

function createLiveSseTurn({ sessionId, userText }) {
  const turnId = `t_sse_${Date.now()}`;
  const turn = {
    turnId,
    sessionId,
    userText,
    seq: 0,
    events: [],
    clients: new Set(),
    timers: [],
    started: false,
    closed: false,
  };
  liveSseTurns.set(turnId, turn);
  liveSseSessionActiveTurn.set(sessionId, turnId);
  return turn;
}

function sessions() {
  return {
    groups: [
      {
        id: "today",
        label: "Today",
        items: [
          {
            id: "s_e2e",
            title: "E2E mock session",
            intents: ["lookup"],
            time: "09:00",
            state: "done",
          },
        ],
      },
    ],
  };
}

function turnEvents(turnId) {
  return {
    events: [
      {
        seq: 1,
        event_type: "turn_started",
        payload: { turn_id: turnId },
        occurred_at: now(),
      },
    ],
  };
}

const server = http.createServer(async (req, res) => {
  const url = new URL(req.url || "/", `http://${req.headers.host || "localhost"}`);
  const path = url.pathname;
  const method = req.method || "GET";

  if (path === "/health") {
    return json(res, 200, {
      ok: true,
      status: "ok",
      version: "0.57.2",
      frontend_min: "0.57.2",
      force_reload: false,
      feature_flags: {},
    });
  }

  if (!path.startsWith("/api/v1/")) {
    return json(res, 404, { detail: "not found" });
  }

  if (method === "OPTIONS") {
    return empty(res);
  }

  if (path === "/api/v1/auth/me" && method === "GET") {
    return json(res, 200, me());
  }

  if (path === "/api/v1/auth/me/workspaces" && method === "GET") {
    return json(res, 200, { items: workspaces, active_id: "ws_test" });
  }

  if (path === "/api/v1/auth/me/workspaces" && method === "POST") {
    return json(res, 200, { workspace_id: `ws_${Date.now()}` });
  }

  if (/^\/api\/v1\/auth\/me\/workspaces\/[^/]+$/.test(path)) {
    return json(res, 200, { ok: true });
  }

  if (
    /^\/api\/v1\/workspaces\/[^/]+\/members$/.test(path) &&
    method === "GET"
  ) {
    return json(res, 200, { members: [] });
  }

  if (
    /^\/api\/v1\/workspaces\/[^/]+\/invites$/.test(path) &&
    method === "GET"
  ) {
    return json(res, 200, { invites: [] });
  }

  if (path === "/api/v1/user-settings" && method === "GET") {
    return json(res, 200, { settings: sparseUserSettings() });
  }

  if (/^\/api\/v1\/user-settings\/[^/]+$/.test(path)) {
    const key = decodeURIComponent(path.split("/").pop() || "");
    if (key === "appearance_theme_config") {
      if (method === "PUT") {
        const body = await readJsonBody(req);
        try {
          const parsed = JSON.parse(typeof body.value === "string" ? body.value : "{}");
          userAppearanceThemeConfig = parsed?.version === 1 ? parsed : { version: 1 };
        } catch {
          userAppearanceThemeConfig = { version: 1 };
        }
        return json(res, 200, { ok: true });
      }
      if (method === "DELETE") {
        userAppearanceThemeConfig = { version: 1 };
        return json(res, 200, { ok: true });
      }
    }
    return json(res, 200, { ok: true });
  }

  if (path === "/api/v1/theme/personal" && method === "GET") {
    const etag = personalAppearanceEtag();
    if (req.headers["if-none-match"] === etag) {
      return empty(res, 304);
    }
    return json(res, 200, personalAppearanceResource(), { etag });
  }

  if (path === "/api/v1/theme/personal" && method === "PATCH") {
    const body = await readJsonBody(req);
    const changes = body?.changes && typeof body.changes === "object"
      ? body.changes
      : {};
    for (const [field, value] of Object.entries(changes)) {
      if (value === null) delete personalAppearanceOverrides[field];
      else personalAppearanceOverrides[field] = value;
    }
    personalAppearanceRowVersion += 1;
    personalAppearanceUndo = null;
    return json(res, 200, personalAppearanceResource(), {
      etag: personalAppearanceEtag(),
    });
  }

  if (path === "/api/v1/theme/personal" && method === "DELETE") {
    personalAppearanceUndo = {
      resetId: "a".repeat(32),
      expiresAt: new Date(Date.now() + 10 * 60_000).toISOString(),
    };
    personalAppearanceOverrides = {};
    personalAppearanceRowVersion += 1;
    return json(res, 200, {
      resource: personalAppearanceResource(),
      undo: personalAppearanceUndo,
    }, { etag: personalAppearanceEtag() });
  }

  if (path === "/api/v1/theme/personal/compile" && method === "POST") {
    const body = await readJsonBody(req);
    return json(res, 200, {
      candidateId: body.candidateId,
      baseEtag: body.baseEtag,
      team: { revisionId: null, revisionNumber: 0 },
      personalOverlayVersion: 1,
      colorScheme: personalColorScheme(body.candidate || {}),
      inputSeeds: {},
      validation: { status: "valid", blocking: [], warnings: [] },
      contextRecommendations: personalRegistries,
    });
  }

  if (path === "/api/v1/theme/personal/undo" && method === "POST") {
    personalAppearanceUndo = null;
    personalAppearanceRowVersion += 1;
    return json(res, 200, personalAppearanceResource(), {
      etag: personalAppearanceEtag(),
    });
  }

  if (path === "/api/v1/theme/effective" && method === "GET") {
    return json(res, 200, effectiveTheme());
  }

  if (path === "/api/v1/team-settings/theme-policy" && method === "GET") {
    return json(res, 200, { value: teamThemePolicy ? JSON.stringify(teamThemePolicy) : null });
  }

  if (path === "/api/v1/team-settings/theme-policy" && method === "PUT") {
    const body = await readJsonBody(req);
    try {
      const parsed = JSON.parse(typeof body.value === "string" ? body.value : "{}");
      teamThemePolicy = parsed?.version === 1 ? parsed : null;
    } catch {
      teamThemePolicy = null;
    }
    return json(res, 200, { ok: true });
  }

  if (path === "/api/v1/team-settings/theme-policy" && method === "DELETE") {
    teamThemePolicy = null;
    return json(res, 200, { ok: true });
  }

  if (path === "/api/v1/llm/catalog" && method === "GET") {
    return json(res, 200, llmCatalog());
  }

  if (path === "/api/v1/admin/llm-config") {
    return json(res, 200, adminLlmConfig());
  }

  if (
    llmV2Fixture
    && path === "/api/v1/admin/llm-config/verification-batches"
    && method === "GET"
  ) {
    return json(res, 200, { batches: [] });
  }

  if (
    llmV2Fixture
    && path === "/api/v1/admin/llm-config/providers/precheck"
    && method === "POST"
  ) {
    return json(res, 200, {
      ok: true,
      provider_id: llmV2ProviderId,
      discovery: { supported: true },
      request_previews: [{
        purpose: "models",
        method: "GET",
        safe_url: "https://models.example.test/v1/models",
      }],
      warnings: [],
    });
  }

  if (
    llmV2Fixture
    && path === `/api/v1/admin/llm-config/providers/${llmV2ProviderId}/discover-models`
    && method === "POST"
  ) {
    return json(res, 200, {
      ok: true,
      provider_id: llmV2ProviderId,
      discovered_at: now(),
      candidates: [
        {
          model_id: "gpt-enterprise",
          already_added: false,
          capability_state: "unknown",
        },
        {
          model_id: "embedding-enterprise",
          already_added: false,
          capability_state: "unknown",
        },
      ],
      returned_count: 2,
      truncated: false,
      limit: 1000,
    });
  }

  if (
    llmV2Fixture
    && path === "/api/v1/admin/llm-config/verification-plans"
    && method === "POST"
  ) {
    return json(res, 200, {
      plan_id: llmV2PlanId,
      confirmation_token: "signed-confirmation-token-long-enough",
      expires_at: new Date(Date.now() + 15 * 60_000).toISOString(),
      item_count: 2,
      provider_request_count: 2,
      max_concurrency: 3,
      items: [
        {
          provider_id: llmV2ProviderId,
          provider_display_name: "corp-openai",
          model_id: "gpt-enterprise",
          capability: "chat",
          testability: "testable",
          provider_request_count: 1,
          testability_contract_version: "openai-chat-testability-v1",
          probe_contract_version: "chat-response-v1",
          probe_execution_policy_version: "probe-policy-v1",
        },
        {
          provider_id: llmV2ProviderId,
          provider_display_name: "corp-openai",
          model_id: "gpt-enterprise",
          capability: "tool",
          testability: "testable",
          provider_request_count: 1,
          testability_contract_version: "openai-tool-testability-v1",
          probe_contract_version: "tool-call-v1",
          probe_execution_policy_version: "probe-policy-v1",
        },
      ],
      notices: ["provider_may_charge"],
    });
  }

  if (
    llmV2Fixture
    && path === "/api/v1/admin/llm-config/verification-batches"
    && method === "POST"
  ) {
    return json(res, 202, {
      batch_id: llmV2BatchId,
      plan_id: llmV2PlanId,
      state: "queued",
      revision: 0,
      replayed: false,
    });
  }

  if (
    llmV2Fixture
    && path === `/api/v1/admin/llm-config/verification-batches/${llmV2BatchId}`
    && method === "GET"
  ) {
    return json(res, 200, {
      batch_id: llmV2BatchId,
      plan_id: llmV2PlanId,
      state: "completed",
      revision: 4,
      base_config_version: 7,
      counts: { verified: 2 },
      items: [
        {
          item_id: "lvi_chat",
          ordinal: 0,
          provider_id: llmV2ProviderId,
          model_id: "gpt-enterprise",
          capability: "chat",
          state: "verified",
          revision: 2,
        },
        {
          item_id: "lvi_tool",
          ordinal: 1,
          provider_id: llmV2ProviderId,
          model_id: "gpt-enterprise",
          capability: "tool",
          state: "verified",
          revision: 4,
        },
      ],
    });
  }

  if (path === "/api/v1/admin/members" && method === "GET") {
    return json(res, 200, {
      items: [],
      owner_id: "u_e2e",
      your_role: "owner",
    });
  }

  if (path === "/api/v1/admin/members/invites" && method === "GET") {
    return json(res, 200, { invites: [] });
  }

  if (path === "/api/v1/admin/credits" && method === "GET") {
    return json(res, 200, {
      team_id: "team_test",
      team_credit_balance: 50_000,
      team_credit_quota: 100_000,
      tier_credits: { pro: 10_000, ultra: 30_000 },
      contract_expires_at: null,
    });
  }

  if (path === "/api/v1/admin/credits/summary" && method === "GET") {
    return json(res, 200, {
      team: {
        balance: 50_000,
        quota: 100_000,
        period_start: null,
        last_reset_at: null,
      },
      last_30d: {
        spend: 1_200,
        turns: 12,
        tokens_in: 10_000,
        tokens_out: 5_000,
      },
      active_users_7d: 3,
      total_users_active: 5,
      view_attention: [],
      view_top_users: [],
      view_top_workspaces: [],
      activation_attention: [],
      credit_actor: {
        team_role: "owner",
        capabilities: {
          credits_read: true,
          credit_grant: true,
          credit_user_quota_write: true,
          credit_user_tier_write: true,
          credit_period_reset: true,
          credit_team_quota_write: true,
          credit_attention_dismiss: true,
        },
      },
    });
  }

  if (path === "/api/v1/admin/credits/users" && method === "GET") {
    return json(res, 200, {
      items: [],
      total: 0,
      page: 1,
      size: 20,
    });
  }

  if (
    path === "/api/v1/integrations/feishu/health" &&
    method === "GET"
  ) {
    return json(res, 200, {
      ok: false,
      enabled: false,
      degraded: false,
    });
  }

  if (
    path === "/api/v1/integrations/feishu/admin/diagnostics" &&
    method === "GET"
  ) {
    return json(res, 200, {
      ok: false,
      enabled: false,
      degraded: false,
      app_type: "custom",
      event_ingress: "webhook",
      group_public_answers_enabled: false,
      auth_mode: "prod",
      prod_auth_mode: true,
      long_connection: null,
      missing_config: [],
      app_id_configured: false,
      app_secret_configured: false,
      verification_token_configured: false,
      encrypt_key_configured: false,
      bot_open_id_configured: false,
      event_store_configured: false,
      event_subscription_url: "",
      card_callback_url: "",
      tenant_token_cache_keys: [],
      required_event_subscriptions: [],
      required_permissions: [],
      setup_checklist: [],
    });
  }

  if (
    path === "/api/v1/integrations/feishu/admin/config" &&
    method === "GET"
  ) {
    return json(res, 200, {
      source: "env",
      tenant_key: "",
      app_type: "internal",
      enabled: false,
      event_ingress: "webhook",
      app_id: "",
      bot_open_id: "",
      group_public_answers_enabled: false,
      app_secret_configured: false,
      verification_token_configured: false,
      encrypt_key_configured: false,
      secret_sources: {
        app_secret: "missing",
        verification_token: "missing",
        encrypt_key: "missing",
      },
      updated_at: null,
      updated_by_user_id: null,
    });
  }

  if (path === "/api/v1/admin/email-config" && method === "GET") {
    return json(res, 200, { configured: "none", version: 0 });
  }

  if (path === "/api/v1/admin/routing" && method === "GET") {
    return json(res, 200, {
      providers: ["deepseek_anthropic"],
      bindings: [{
        agent_type: "lead",
        profile: null,
        primary_effective: ["deepseek_anthropic", "deepseek-v4-pro"],
        primary_original: ["deepseek_anthropic", "deepseek-v4-pro"],
        fallback: [],
        overridden: false,
      }],
    });
  }

  if (path === "/api/v1/sessions" && method === "GET") {
    return json(res, 200, sessions());
  }

  if (/^\/api\/v1\/sessions\/[^/]+\/artifacts$/.test(path) && method === "GET") {
    const sessionId = path.split("/").at(-2);
    return json(res, 200, {
      items: getSessionArtifacts(sessionId),
    });
  }

  if (
    /^\/api\/v1\/sessions\/[^/]+\/artifacts\/art_delivery\/content$/.test(path) &&
    method === "GET"
  ) {
    const payload = "<!doctype html><html><body><main>Executive brief</main></body></html>";
    res.writeHead(200, {
      ...corsHeaders,
      "content-type": "text/html; charset=utf-8",
      "content-length": Buffer.byteLength(payload),
      "content-disposition": 'attachment; filename="executive-brief.html"',
      "x-content-type-options": "nosniff",
      "cache-control": "no-store",
    });
    res.end(payload);
    return;
  }

  const representationMatch = path.match(/^\/api\/v1\/(?:files|canvas\/by-session\/([^/]+)\/artifacts)\/([^/]+)\/representations\/(preview|raw|details)$/);
  if (representationMatch && method === "GET") {
    const [, sessionId, artifactId, representation] = representationMatch;
    const item = getSessionArtifacts(sessionId || "s_e2e").find((candidate) => candidate.id === artifactId);
    if (!item) return json(res, 404, { detail: { error: { code: "artifact_scope_unavailable" } } });
    const text = artifactId === "art_calc" ? "# Calculation\n\n19×29=551\n"
      : artifactId === "art_input" ? "Source notes for the E2E report.\n" : "19×29=551\n";
    return json(res, 200, {
      representation, revision: `${artifactId}-${representation}-1`, family: "text", mode: "text",
      ...(representation === "details" ? { details: item } : { text, truncated: false }),
    });
  }

  if (path === "/api/v1/files/art_stdout/preview" && method === "GET") {
    return json(res, 200, {
      family: "text",
      mode: "text",
      text: "19×29=551\n",
      truncated: false,
    });
  }

  if (path === "/api/v1/files/art_calc/preview" && method === "GET") {
    return json(res, 200, {
      family: "text",
      mode: "text",
      text: "# Calculation\n\n19×29=551\n",
      truncated: false,
    });
  }

  if (path === "/api/v1/files/art_input/preview" && method === "GET") {
    return json(res, 200, {
      family: "text",
      mode: "text",
      text: "Source notes for the E2E report.\n",
      truncated: false,
    });
  }

  if (path === "/api/v1/files/art_delivery" && method === "GET") {
    const payload = "<!doctype html><html><body><main>Executive brief</main></body></html>";
    res.writeHead(200, {
      ...corsHeaders,
      "content-type": "text/html; charset=utf-8",
      "content-length": Buffer.byteLength(payload),
      "cache-control": "no-store",
    });
    res.end(payload);
    return;
  }

  if (path === "/api/v1/files/art_video" && method === "GET") {
    const payload = Buffer.from("00000018667479706d703432", "hex");
    res.writeHead(200, {
      ...corsHeaders,
      "content-type": "video/mp4",
      "content-length": payload.byteLength,
      "accept-ranges": "bytes",
      "cache-control": "no-store",
    });
    res.end(payload);
    return;
  }

  if (path === "/api/v1/search" && method === "POST") {
    const body = await readJsonBody(req);
    const query = typeof body.query === "string" ? body.query.trim() : "";
    return json(res, 200, {
      query,
      partial: false,
      groups: ["session", "report", "file", "knowledge"].map((kind) => ({
        kind,
        unavailable: false,
        items: [],
      })),
    });
  }

  if (/^\/api\/v1\/sessions\/[^/]+$/.test(path) && method === "GET") {
    // canvas-comprehensive WP-7: session detail must carry `turns` (handlePickSessionFromUrl
    // reads detail.turns.length on F5 hydrate) — empty is fine, the canvas hydrates from
    // GET /canvas/by-session which returns the primed report.
    const sessionId = path.split("/").pop();
    return json(res, 200, {
      session: {
        id: sessionId,
        workspace_id: "ws_test",
        title: "E2E mock session",
        started_at: now(),
        last_active_at: now(),
        status: "completed",
        cost_usd: 0,
        tokens_total: 0,
        intents: [],
      },
      turns: [],
    });
  }

  if (path === "/api/v1/chat/send" && method === "POST") {
    const body = await readJsonBody(req);
    // Route by profile (mock server inherits BOWHEAD_E2E_PROFILE from playwright's env):
    // canvas profile → canvas mock (canvas-comprehensive); else → live-SSE steer mock.
    if (process.env.BOWHEAD_E2E_PROFILE === "canvas") {
      // canvas-comprehensive WP-0: inject dropped-frame/stream-cut instructions through message `::drop=<spec>` / `::cut=<n>`
      // → encode into stream_url query, consumed by serveStream.
      const rawMsg = typeof body.message === "string" ? body.message : "";
      const dropM = rawMsg.match(/::drop=(\S+)/);
      const cutM = rawMsg.match(/::cut=(\d+)/);
      const cleanMsg = rawMsg.replace(/::(drop|cut)=\S+/g, "").trim();
      const { turnId, sessionId } = primeTurn(cleanMsg, "s_e2e");
      const q = [];
      if (dropM) q.push(`drop=${encodeURIComponent(dropM[1])}`);
      if (cutM) q.push(`cut=${cutM[1]}`);
      const qs = q.length ? `?${q.join("&")}` : "";
      return json(res, 200, {
        ok: true,
        request_id: `req_${Date.now()}`,
        turn_id: turnId,
        session_id: sessionId,
        stream_url: `/api/v1/chat/stream/${turnId}${qs}`,
      });
    }
    // origin/main: live-SSE steer mock (steer-live-sse.spec.ts).
    const sessionId = typeof body.session_id === "string" && body.session_id
      ? body.session_id
      : `s_sse_${Date.now()}`;
    const expectedTurnId =
      body.client_info && typeof body.client_info.expected_turn_id === "string"
        ? body.client_info.expected_turn_id
        : "";
    if (expectedTurnId) {
      const activeTurnId = liveSseSessionActiveTurn.get(sessionId) || "";
      if (!activeTurnId || activeTurnId !== expectedTurnId) {
        return json(res, 200, {
          session_id: sessionId,
          turn_id: activeTurnId,
          stream_url: "",
          status: "completed",
        });
      }
      const turn = liveSseTurns.get(activeTurnId);
      emitLiveSse(turn, "emoji_ack", {
        target_msg_id: typeof body.client_info.user_msg_id === "string"
          ? body.client_info.user_msg_id
          : `steer_${Date.now()}`,
        emoji: "🤝",
        text: typeof body.message === "string" ? body.message : "",
        ts: now(),
      });
      return json(res, 200, {
        session_id: sessionId,
        turn_id: activeTurnId,
        stream_url: `/api/v1/chat/stream/${activeTurnId}`,
        status: "buffered",
      });
    }
    const turn = createLiveSseTurn({
      sessionId,
      userText: typeof body.message === "string" ? body.message : "",
    });
    return json(res, 200, {
      ok: true,
      request_id: `req_${Date.now()}`,
      turn_id: turn.turnId,
      session_id: sessionId,
      stream_url: `/api/v1/chat/stream/${turn.turnId}`,
      execution_generation: 1,
    });
  }

  // canvas-comprehensive WP-0: canvas SSE stream (scripted scenario + drop/cut).
  // Guarded to the canvas profile so the sse profile falls through to the live-SSE
  // stream handler below.
  const canvasStreamM = path.match(/^\/api\/v1\/chat\/stream\/([^/]+)$/);
  if (canvasStreamM && method === "GET" && process.env.BOWHEAD_E2E_PROFILE === "canvas") {
    return serveStream(req, res, url, decodeURIComponent(canvasStreamM[1]));
  }

  // canvas-comprehensive WP-0: stateful canvas by-session (reconcile / F5 source) + test reset.
  const bySessionM = path.match(/^\/api\/v1\/canvas\/by-session\/([^/]+)$/);
  if (bySessionM && method === "GET") {
    return json(res, 200, getBySession(decodeURIComponent(bySessionM[1])));
  }
  if (path === "/api/v1/canvas" && method === "GET") {
    return json(res, 200, { items: [] });
  }
  if (path === "/api/v1/_test/reset" && method === "POST") {
    resetCanvasMock();
    return json(res, 200, { ok: true });
  }

  // origin/main: live-SSE stream (steer profile).
  if (/^\/api\/v1\/chat\/stream\/[^/]+$/.test(path) && method === "GET") {
    const turnId = decodeURIComponent(path.split("/").pop() || "");
    const turn = liveSseTurns.get(turnId);
    if (!turn) return json(res, 404, { detail: "turn not found" });
    res.writeHead(200, {
      ...corsHeaders,
      "content-type": "text/event-stream; charset=utf-8",
      "cache-control": "no-store, no-transform",
      connection: "keep-alive",
      "x-accel-buffering": "no",
    });
    res.write(": connected\n\n");
    turn.clients.add(res);
    for (const item of turn.events) {
      sseWrite(res, item.event, item.payload);
    }
    scheduleLiveSseTurn(turn);
    req.on("close", () => {
      turn.clients.delete(res);
    });
    return;
  }

  if (/^\/api\/v1\/clarifications\/[^/]+\/resolve$/.test(path) && method === "POST") {
    return json(res, 200, {
      ok: true,
      action: "turn_resume",
      resume_turn_id: "t_rho_peer_hitl_1",
    });
  }

  if (/^\/api\/v1\/clarifications\/[^/]+\/resume_turn$/.test(path) && method === "POST") {
    return json(res, 200, {
      ok: true,
      turn_id: "t_rho_peer_hitl_1",
      stream_url: "/api/v1/chat/stream/t_rho_peer_hitl_1",
    });
  }

  if (/^\/api\/v1\/turns\/[^/]+\/events$/.test(path) && method === "GET") {
    const turnId = decodeURIComponent(path.split("/")[4] || "t_e2e");
    return json(res, 200, turnEvents(turnId));
  }

  if (path === "/api/v1/turns" && method === "GET") {
    return json(res, 200, { turn_ids: ["t_e2e"] });
  }

  if (path === "/api/v1/datarc/authorize" && method === "POST") {
    return json(res, 200, { auth_data: "mock", login_url: "https://datarc.example/mock" });
  }

  if (path === "/api/v1/skills" && method === "GET") {
    return json(res, 200, {
      builtin: [],
      user_upload: [],
      user_generated: [],
      total_enabled: 0,
    });
  }

  if (path === "/api/v1/memory/overview" && method === "GET") {
    return json(res, 200, {
      schema_version: 2,
      about_you: {
        content: "",
        scope: {
          type: "global",
          space_id: null,
          space_name: null,
          availability: "available",
          label: "Personal",
        },
        version: "0",
        updated_at: null,
        is_locked: false,
        capabilities: {
          edit: true,
          soft_delete: false,
          restore: false,
          permanent_delete: false,
        },
      },
      suggestions: [],
      stats: {
        notes_configured: false,
        saved_entries: 0,
        pending_candidates: 0,
        spaces_represented: 0,
        trash_count: 0,
        recoverable_resets: 0,
      },
      learning: { automatic_learning: "active" },
      capabilities: {
        v2_enabled: true,
        v2_ready: true,
        presentation: "v2",
        schema_version: 3,
        kind_policy_version: 1,
        actions: {
          edit: true,
          permanent_delete: false,
          recent_resets: true,
          destructive_operations: false,
        },
        permanent_delete_ready: false,
        permanent_delete_reason: "mock",
        reason: null,
      },
    });
  }

  if (path === "/api/v1/memory/items" && method === "GET") {
    return json(res, 200, { items: [], next_cursor: null });
  }

  if (path === "/api/v1/memory/resets" && method === "GET") {
    return json(res, 200, { items: [], next_cursor: null });
  }

  if (path === "/api/v1/memory/snapshot" && method === "GET") {
    return json(res, 200, {
      user_id: "u_e2e",
      items: [],
      stats: {
        active_count: 0,
        trash_count: 0,
        pending_candidates: 0,
      },
      dreaming: { paused: false },
    });
  }

  if (path === "/api/v1/memory/trash" && method === "GET") {
    return json(res, 200, { items: [] });
  }

  if (path === "/api/v1/memory/dreaming" && method === "GET") {
    return json(res, 200, { paused: false, updated_at: now() });
  }

  if (path.startsWith("/api/v1/memory/")) {
    return json(res, 200, { ok: true, items: [] });
  }

  if (
    (path === "/api/v1/files/" || path === "/api/v1/files") &&
    method === "GET"
  ) {
    return json(res, 200, {
      total: 0,
      size_total_bytes: 0,
      items: [],
    });
  }

  if (path === "/api/v1/kb/files" && method === "GET") {
    return json(res, 200, { files: [], total: 0, next_cursor: null });
  }

  if (path === "/api/v1/kb/doc_types" && method === "GET") {
    return json(res, 200, { items: [] });
  }

  if (path.startsWith("/api/v1/kb/")) {
    return json(res, 200, { ok: true, files: [], items: [], total: 0 });
  }

  if (path === "/api/v1/metrics/event") {
    return json(res, 200, { ok: true });
  }

  return json(res, 404, { detail: `unmocked endpoint: ${method} ${path}` });
});

server.listen(port, "127.0.0.1", () => {
  console.log(`[mock-api] listening on http://127.0.0.1:${port}`);
});
