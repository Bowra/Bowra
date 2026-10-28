import { expect, test, type Page, type Route } from "@playwright/test";

// v4: model settings uses an explicit Draft → Review → Apply flow. These smokes
// render it through route-mocked admin APIs and pin layout plus the write contract.

const TEAM_LLM_CONFIG = {
  schema_version: 2,
  providers: {
    "我们的 OpenAI 代理": {
      type: "openai_compatible",
      base_url: "https://proxy.acme.internal/v1",
      fingerprint: "sha256:openaiproxyfp0011",
      models: [
        { id: "gpt-4o-proxy", capabilities: ["chat", "tool", "vision"], source: "preset" },
        { id: "text-embed-3-lg", capabilities: ["embed"], source: "preset" },
        { id: "bge-reranker-v2", capabilities: ["rerank"], source: "manual" },
      ],
      supports: {},
    },
    deepseek_anthropic: {
      type: "anthropic_compatible",
      base_url: "https://api.deepseek.com/anthropic",
      fingerprint: "sha256:testfingerprint00",
      models: ["deepseek-v4-pro"],
      supports: { tool_use: true, extended_thinking: true },
    },
  },
  tiers: {
    primary: { provider: "deepseek_anthropic", model: "deepseek-v4-pro" },
    primary_fallback: [{ provider: "我们的 OpenAI 代理", model: "gpt-4o-proxy" }],
    fast: { follow: "primary" },
    judge: { auto: true },
  },
  kb: {
    models: {
      "qwen-text-embedding-v4": { provider: "我们的 OpenAI 代理", real_id: "text-embed-3-lg", capability: "embed", dim: 1024 },
    },
    active: { embed: "qwen-text-embedding-v4" },
    pending_embed: null,
  },
};

const EMPTY_CONFIG = { schema_version: 2, providers: {}, tiers: {}, kb: { models: {}, active: {}, pending_embed: null } };

function resp(config: unknown, providers: unknown[] = []) {
  return JSON.stringify({ ok: true, version: 7, config, providers, reindex: null });
}

// The Models tab is gated to creator/team_admin (WS8). The default mock user is a member,
// so mock /auth/me (exact path, not /auth/me/workspaces) as a creator for these smokes.
const CREATOR_ME = {
  id: "u_creator", email: "e2e@bowhead.test", display_name: "E2E Creator", avatar_url: null,
  team_id: "team_e2e", team_role: "owner", is_team_admin: true, role: "admin", status: "active",
  email_verified: true, tier: "ultra", credit_balance: 1000, credit_quota: 1000,
  locale: "zh-CN", timezone: "Asia/Shanghai", workspace_count: 1, needs_onboarding: false,
};

test.beforeEach(async ({ page }) => {
  await page.route(/\/api\/v1\/auth\/me$/, async (route: Route) => {
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(CREATOR_ME) });
  });
});

/** Model-draft unapplied notice: the settings-shell bottom Save bar (page acceptance A). */
const draftBar = (page: Page) => page.locator(".ui-savebar");

async function mockLlmConfig(page: Page, config: unknown = TEAM_LLM_CONFIG, providers: unknown[] = [
  { provider_name: "我们的 OpenAI 代理", type: "openai_compatible", base_url: "https://proxy.acme.internal/v1", fingerprint: "sha256:openaiproxyfp0011" },
  { provider_name: "deepseek_anthropic", type: "anthropic_compatible", base_url: "https://api.deepseek.com/anthropic", fingerprint: "sha256:testfingerprint00" },
]): Promise<void> {
  await page.route("**/api/v1/admin/llm-config/validate", async (route: Route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        valid: true,
        version: 7,
        issues: [],
        has_changes: true,
        changed_sections: ["tiers"],
        impact: { active_files: 3, requires_embed_reindex: false },
      }),
    });
  });
  await page.route("**/api/v1/admin/llm-config", async (route: Route) => {
    if (route.request().method() === "PUT") {
      await route.fulfill({ status: 200, contentType: "application/json", body: resp(config, providers) });
      return;
    }
    await route.fulfill({ status: 200, contentType: "application/json", body: resp(config, providers) });
  });
}

async function mockLlmOverview(page: Page): Promise<void> {
  const openaiProviderId = "llmp_0123456789abcdef0123456789abcdef";
  const anthropicProviderId = "llmp_fedcba9876543210fedcba9876543210";
  await page.route("**/api/v1/admin/llm-config/verification-batches?state=active**", async (route: Route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ batches: [] }),
    });
  });
  await page.route("**/api/v1/admin/llm-config/verification-plans", async (route: Route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        plan_id: "lvplan_0123456789abcdef0123456789abcdef",
        confirmation_token: "signed-confirmation-token",
        expires_at: "2026-08-01T12:15:00Z",
        item_count: 2,
        provider_request_count: 2,
        max_concurrency: 2,
        notices: ["provider_may_charge"],
        items: [
          {
            provider_id: anthropicProviderId,
            provider_display_name: "deepseek_anthropic",
            model_id: "deepseek-v4-pro",
            capability: "tool",
            testability: "testable",
            provider_request_count: 1,
            testability_contract_version: "anthropic-tool-v1",
            probe_execution_policy_version: "probe-policy-v1",
          },
          {
            provider_id: openaiProviderId,
            provider_display_name: "我们的 OpenAI 代理",
            model_id: "text-embed-3-lg",
            capability: "embed",
            testability: "testable",
            provider_request_count: 1,
            testability_contract_version: "openai-embed-v1",
            probe_execution_policy_version: "probe-policy-v1",
          },
        ],
      }),
    });
  });
  await page.route("**/api/v1/admin/llm-config/validate", async (route: Route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        valid: false,
        version: 7,
        issues: [],
        has_changes: false,
        changed_sections: [],
        readiness: {
          as_of: "2026-07-31T08:00:00Z",
          draft_semantic_hash: "a".repeat(64),
          may_apply: false,
          rows: [
            {
              role_key: "tiers.primary",
              provider_id: anthropicProviderId,
              provider_display_name: "deepseek_anthropic",
              model_id: "deepseek-v4-pro",
              capability: "chat",
              routing_critical: true,
              declared: true,
              fact_state: "verified",
              attempt_state: "errored",
              observed_at: "2026-07-30T12:00:00Z",
              status: "ready",
              changed: false,
            },
            {
              role_key: "tiers.primary",
              provider_id: anthropicProviderId,
              provider_display_name: "deepseek_anthropic",
              model_id: "deepseek-v4-pro",
              capability: "tool",
              routing_critical: true,
              declared: true,
              fact_state: "unknown",
              status: "blocked",
              changed: false,
            },
            {
              role_key: "kb.active.embed",
              provider_id: openaiProviderId,
              provider_display_name: "我们的 OpenAI 代理",
              model_id: "text-embed-3-lg",
              capability: "embed",
              routing_critical: false,
              declared: true,
              fact_state: "aged",
              observed_at: "2026-06-01T08:00:00Z",
              status: "warning",
              changed: false,
            },
          ],
          blocking: [{
            code: "capability_unknown",
            field_path: "tiers.primary",
            role_key: "tiers.primary",
            provider_id: anthropicProviderId,
            model_id: "deepseek-v4-pro",
            capability: "tool",
            action_code: "verify_capability",
          }],
          warnings: [],
          required_acceptances: [],
          migration_acceptance: null,
          external_rows: [
            { subject_kind: "media_image_generation", configured: false, status: "unavailable" },
            { subject_kind: "web_search", configured: true, status: "configured" },
          ],
          baseline_will_invalidate: false,
        },
      }),
    });
  });
  await page.route("**/api/v1/admin/llm-config", async (route: Route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        ok: true,
        version: 7,
        config: TEAM_LLM_CONFIG,
        providers: [],
        features: {
          model_discovery_v2: true,
          capability_probes: true,
          azure_openai: true,
          custom_http: true,
        },
        provider_identities: {
          "我们的 OpenAI 代理": openaiProviderId,
          deepseek_anthropic: anthropicProviderId,
        },
        reindex: null,
      }),
    });
  });
}

test("模型 V4 overview preserves evidence semantics across desktop, mobile, and dark mode", async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 900 });
  await mockLlmOverview(page);
  await page.goto("/team/settings/models/", { waitUntil: "domcontentloaded" });

  await expect(page.getByRole("heading", { name: /当前生效连接|Active connections/ })).toBeVisible();
  await expect(page.getByText("sha256:testfingerprint00")).toBeVisible();
  await page.getByRole("tab", { name: /模型分工|Model assignments/ }).click();

  // Since 96a5eb706, resting state has no “当前配置还不能安全应用” verdict panel: actual issues show only a quiet hint
  // linking to detailed evidence below (fixture: 1 awaiting confirmation, 1 older evidence).
  const attention = page.locator(".overview-quiet.is-attention");
  await expect(attention).toContainText(/1 项等你确认、1 条依据时间较早|1 awaiting your confirmation, 1 with older evidence/);
  await expect(attention.getByRole("link", { name: /逐条查看|Review one by one/ })).toHaveAttribute("href", "#mset-overview-evidence");
  await expect(page.getByText(/验证依据|Verification evidence/, { exact: true }).first()).toBeVisible();
  await expect(page.getByText(/最近尝试|Latest attempt/, { exact: true }).first()).toBeVisible();
  await expect(page.getByText(/^已验证$|^Verified$/).first()).toBeVisible();
  await expect(page.getByText(/本次请求出错|Latest request errored/)).toBeVisible();
  await expect(page.locator("[data-testid='model-settings-editor']")).toBeHidden();
  // f7789bf1b: page root fills the uniform settings-shell content width (the 880px form width), never narrows independently.
  const desktop = await page.locator(".mset").evaluate((element) => ({
    width: element.getBoundingClientRect().width,
    shellWidth: element.closest(".settings-content-inner")?.getBoundingClientRect().width,
  }));
  expect(desktop.width).toBe(desktop.shellWidth);
  expect(desktop.width).toBe(880);
  await page.screenshot({ path: "test-results/models-v4-overview-1280-light.png", fullPage: true });

  const verifyAssignments = page.getByRole("button", {
    name: /验证当前分工|Verify current assignments/,
  });
  await verifyAssignments.click();
  const selector = page.getByRole("dialog", { name: /选择要验证的模型能力|Choose model capabilities to verify/ });
  await expect(selector).toBeVisible();
  await expect(selector.getByRole("searchbox")).toBeFocused();
  await expect(selector.getByRole("checkbox", { checked: true })).toHaveCount(2);
  await expect(selector.getByText(/已选择 2 项能力|2 capability item/)).toBeVisible();
  await page.screenshot({ path: "test-results/models-v4-selector-1280-light.png", fullPage: true });
  await selector.getByRole("button", { name: /关闭|Close/ }).click();
  await expect(verifyAssignments).toBeFocused();

  await verifyAssignments.click();
  await selector.getByRole("button", {
    name: /生成验证计划|Create verification plan/,
  }).click();
  await expect(page.getByText(/计划检查 2 项模型能力|2 model capability check/)).toBeVisible();
  await expect(verifyAssignments).toBeDisabled();
  await expect(page.getByRole("button", {
    name: /编辑分工|Edit assignments/,
  })).toBeDisabled();
  await page.getByRole("button", { name: /^取消$|^Cancel$/ }).click();
  await expect(verifyAssignments).toBeEnabled();

  await page.setViewportSize({ width: 360, height: 800 });
  await expect(page.getByRole("button", { name: /验证当前分工|Verify current assignments/ })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(360);
  const mobileActions = page.locator(".overview-section-actions .btn");
  for (const size of await mobileActions.evaluateAll((buttons) => buttons.map((button) => {
    const rect = button.getBoundingClientRect();
    return { left: rect.left, right: rect.right, width: rect.width, height: rect.height };
  }))) {
    expect(size.left).toBeGreaterThanOrEqual(0);
    expect(size.right).toBeLessThanOrEqual(360);
    // A narrow window with a fine pointer keeps the control height tiers (at least the 24px WCAG 2.2 target);
    // coarse pointers get 44px controls, checked in the touch test below.
    expect(size.height).toBeGreaterThanOrEqual(24);
  }
  await page.screenshot({ path: "test-results/models-v4-overview-360-light.png", fullPage: true });

  await page.evaluate(() => {
    document.documentElement.dataset.theme = "dark";
  });
  // `.overview-readiness` verdict panel retired in 96a5eb706; its quiet replacement hint verifies dark mode.
  await expect(page.locator(".overview-quiet.is-attention")).toHaveCSS("color-scheme", "dark");
  const darkBodyContrast = await page.locator(".overview-section-head p").first().evaluate((element) => {
    const rgb = (value: string) => (value.match(/[\d.]+/g) || []).slice(0, 3).map(Number);
    const luminance = (value: number[]) => value
      .map((channel) => channel / 255)
      .map((channel) => channel <= 0.03928
        ? channel / 12.92
        : ((channel + 0.055) / 1.055) ** 2.4)
      .reduce((total, channel, index) => total + channel * [0.2126, 0.7152, 0.0722][index], 0);
    const foreground = rgb(getComputedStyle(element).color);
    let parent: Element | null = element;
    let background = [0, 0, 0];
    while (parent) {
      const value = getComputedStyle(parent).backgroundColor;
      if (value !== "rgba(0, 0, 0, 0)" && value !== "transparent") {
        background = rgb(value);
        break;
      }
      parent = parent.parentElement;
    }
    const [lighter, darker] = [luminance(foreground), luminance(background)]
      .sort((left, right) => right - left);
    return (lighter + 0.05) / (darker + 0.05);
  });
  expect(darkBodyContrast).toBeGreaterThanOrEqual(4.5);
  await page.screenshot({ path: "test-results/models-v4-overview-360-dark.png", fullPage: true });
});

test("模型 page renders the three blocks (populated)", async ({ page }) => {
  await mockLlmConfig(page);
  await page.goto("/team/settings/models/", { waitUntil: "domcontentloaded" });

  // page head + three block headings
  await expect(page.getByRole("heading", { name: /^模型$|^Models$/, level: 1 })).toBeVisible();
  await expect(page.getByRole("heading", { name: /你的模型来源|Your model sources/ })).toBeVisible();
  await expect(page.getByRole("heading", { name: /模型分配|Model assignment/ })).toBeVisible();
  await expect(page.getByRole("heading", { name: /知识库模型|Knowledge base models/ })).toBeVisible();

  // ① provider cards present (both providers)
  await expect(page.getByText("deepseek_anthropic").first()).toBeVisible();
  await expect(page.getByText("我们的 OpenAI 代理").first()).toBeVisible();
  // ② primary tier model reflected in first (primary model) select
  await expect(page.locator(".mset .default-sel select").first()).toHaveValue("deepseek_anthropic::deepseek-v4-pro");
  // ③ KB vector select is populated (not the empty state)
  await expect(page.locator(".mset .kb-sel")).toBeVisible();
  // f7789bf1b uniform team-settings width: settings shell solely owns main content width (the 880px form width);
  // .mset root must fill settings-content-inner, never narrow independently (docs/design/团队设置页面.md).
  const { width, shellWidth } = await page.locator(".mset").evaluate((element) => ({
    width: element.getBoundingClientRect().width,
    shellWidth: element.closest(".settings-content-inner")?.getBoundingClientRect().width,
  }));
  expect(width).toBe(shellWidth);
  expect(width).toBe(880);

  await page.screenshot({ path: "test-results/models-1440-light.png", fullPage: true });
});

test.describe("touch device", () => {
  test.use({ hasTouch: true, isMobile: true });
  test("模型 page renders on mobile (360)", async ({ page }) => {
    await page.setViewportSize({ width: 360, height: 900 });
    await mockLlmConfig(page);
    await page.goto("/team/settings/models/", { waitUntil: "domcontentloaded" });
    await expect(page.getByRole("heading", { name: /你的模型来源|Your model sources/ })).toBeVisible();

    // Connection actions sit behind the card's "⋯" menu button.
    const providerActions = page.locator(".mset .pcard-actions .ui-row-more");
    const actionSizes = await providerActions.evaluateAll((buttons) =>
      buttons.map((button) => {
        const rect = button.getBoundingClientRect();
        return { width: rect.width, height: rect.height };
      }),
    );
    expect(actionSizes.length).toBeGreaterThan(0);
    for (const size of actionSizes) {
      expect(size.width).toBeGreaterThanOrEqual(44);
      expect(size.height).toBeGreaterThanOrEqual(44);
    }
    const providerCardsFit = await page.locator(".mset .pcard").evaluateAll(
      (cards) => cards.every((card) => {
        const rect = card.getBoundingClientRect();
        return rect.left >= 0 && rect.right <= window.innerWidth;
      }),
    );
    expect(providerCardsFit).toBe(true);

    await page.locator(".mset .pcard-expand").first().click();
    await expect(page.locator(".mset .key-fingerprint").first()).toContainText(
      /SHA-256 (指纹|fingerprint)/,
    );
    await expect(page.locator(".mset .key-fingerprint").first()).not.toContainText("••••");

    await page.locator(".mset .default-sel select").first().selectOption("我们的 OpenAI 代理::gpt-4o-proxy");
    const dock = draftBar(page);
    await expect(dock).toBeVisible();
    const dockBox = await dock.boundingBox();
    expect(dockBox).not.toBeNull();
    expect(dockBox!.y).toBeGreaterThanOrEqual(0);
    expect(dockBox!.y + dockBox!.height).toBeLessThanOrEqual(900);

    await page.screenshot({ path: "test-results/models-412-light.png", fullPage: true });
  });
});

test("模型 page renders in dark theme", async ({ page }) => {
  await page.emulateMedia({ colorScheme: "dark" });
  await mockLlmConfig(page);
  await page.goto("/team/settings/models/", { waitUntil: "domcontentloaded" });
  await expect(page.getByRole("heading", { name: /你的模型来源|Your model sources/ })).toBeVisible();
  // Exercise the production theme contract, not a test-only `.dark` class.
  await page.evaluate(() => {
    document.documentElement.dataset.theme = "dark";
  });
  await page.screenshot({ path: "test-results/models-1440-dark.png", fullPage: true });
});

test("模型 page empty state guides connecting a first source", async ({ page }) => {
  await mockLlmConfig(page, EMPTY_CONFIG, []);
  await page.goto("/team/settings/models/", { waitUntil: "domcontentloaded" });
  await expect(page.getByText(/还没有接入任何模型来源|No model sources connected yet/)).toBeVisible();
  await expect(page.getByRole("button", { name: /接入你的第一个模型来源|Connect your first model source/ })).toBeVisible();
  await page.screenshot({ path: "test-results/models-1440-empty.png", fullPage: true });
});

test("模型 page: expand a provider card + open the connect panel", async ({ page }) => {
  await mockLlmConfig(page);
  await page.goto("/team/settings/models/", { waitUntil: "domcontentloaded" });

  // expand the anthropic provider card → base_url + model catalog visible
  await page.locator(".mset .pcard-expand", { hasText: "deepseek_anthropic" }).click();
  await expect(page.getByText("https://api.deepseek.com/anthropic")).toBeVisible();
  await expect(page.getByText("deepseek-v4-pro").first()).toBeVisible();

  // open the add-source connect panel → protocol selector + test button
  await page.getByRole("button", { name: /接入模型来源|Connect a model source/ }).click();
  await expect(page.locator(".mset .connect")).toBeVisible();
  await expect(page.getByRole("button", { name: /测试连接|Test connection/ })).toBeVisible();
  await page.screenshot({ path: "test-results/models-connect-panel.png", fullPage: true });
});

test("模型 page: changing the main model stays draft-only until one explicit apply", async ({ page }) => {
  let sawPut = false;
  let putBody: Record<string, unknown> | null = null;
  let effectiveConfig: unknown = TEAM_LLM_CONFIG;
  await page.route("**/api/v1/admin/llm-config/validate", async (route: Route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        valid: true,
        version: 7,
        issues: [],
        has_changes: true,
        changed_sections: ["tiers"],
        impact: { active_files: 3, requires_embed_reindex: false },
      }),
    });
  });
  await page.route("**/api/v1/admin/llm-config", async (route: Route) => {
    if (route.request().method() === "PUT") {
      sawPut = true;
      putBody = route.request().postDataJSON() as Record<string, unknown>;
      effectiveConfig = (putBody as { config: unknown }).config;
      await route.fulfill({ status: 200, contentType: "application/json", body: resp(effectiveConfig, []) });
      return;
    }
    await route.fulfill({ status: 200, contentType: "application/json", body: resp(effectiveConfig, []) });
  });
  await page.goto("/team/settings/models/", { waitUntil: "domcontentloaded" });

  await page.locator(".mset .default-sel select").first().selectOption("我们的 OpenAI 代理::gpt-4o-proxy");
  await page.waitForTimeout(150);
  expect(sawPut).toBe(false);
  await expect(draftBar(page)).toBeVisible();
  // “检查并应用” is inside the Save bar; the former draft dock is gone.
  await expect(page.locator(".draft-dock")).toHaveCount(0);
  await page.getByRole("button", { name: /检查并应用|Review and apply/ }).click();
  await expect(page.getByRole("heading", { name: /应用模型配置|Apply model configuration/ })).toBeVisible();
  await page.screenshot({ path: "test-results/models-review-drawer.png", fullPage: true });
  await page.getByRole("button", { name: /一次应用|Apply once/ }).click();

  await expect.poll(() => sawPut).toBe(true);
  const cfg = (putBody as unknown as {
    config?: {
      tiers?: {
        primary?: { model?: string };
        primary_fallback?: Array<{ provider?: string; model?: string }>;
      };
    };
  } | null)?.config;
  expect(cfg?.tiers?.primary?.model).toBe("gpt-4o-proxy");
  expect(cfg?.tiers?.primary_fallback).not.toContainEqual({
    provider: "我们的 OpenAI 代理",
    model: "gpt-4o-proxy",
  });
  expect((putBody as { expected_version?: number } | null)?.expected_version).toBe(7);
  await expect(draftBar(page)).toBeHidden();
});

test("模型 page: a failed server review stays visible and cannot be applied", async ({ page }) => {
  await mockLlmConfig(page);
  await page.route("**/api/v1/admin/llm-config/validate", async (route: Route) => {
    await route.fulfill({
      status: 503,
      contentType: "application/json",
      body: JSON.stringify({ detail: { code: "review_unavailable" } }),
    });
  });
  await page.goto("/team/settings/models/", { waitUntil: "domcontentloaded" });

  await page.locator(".mset .default-sel select").first().selectOption("我们的 OpenAI 代理::gpt-4o-proxy");
  await page.getByRole("button", { name: /检查并应用|Review and apply/ }).click();

  const review = page.getByRole("dialog", { name: /应用模型配置|Apply model configuration/ });
  await expect(review.locator(".review-issues")).toBeVisible();
  await expect(review.getByRole("button", { name: /一次应用|Apply once/ })).toBeDisabled();
});

test("模型 page: a stale review becomes read-only and preserves the draft", async ({ page }) => {
  await mockLlmConfig(page);
  await page.route("**/api/v1/admin/llm-config", async (route: Route) => {
    if (route.request().method() === "PUT") {
      await route.fulfill({
        status: 409,
        contentType: "application/json",
        body: JSON.stringify({ detail: { code: "config_version_conflict" } }),
      });
      return;
    }
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: resp(TEAM_LLM_CONFIG, []),
    });
  });
  await page.goto("/team/settings/models/", { waitUntil: "domcontentloaded" });

  await page.locator(".mset .default-sel select").first().selectOption("我们的 OpenAI 代理::gpt-4o-proxy");
  await page.getByRole("button", { name: /检查并应用|Review and apply/ }).click();
  const review = page.getByRole("dialog", { name: /应用模型配置|Apply model configuration/ });
  await review.getByRole("button", { name: /一次应用|Apply once/ }).click();

  await expect(review.getByText(/这份检查结果已经失效|review is no longer current/)).toBeVisible();
  await expect(review.getByRole("button", { name: /载入最新配置并重新检查|Load latest and review again/ })).toBeVisible();
  await expect(review.getByRole("button", { name: /一次应用|Apply once/ })).toHaveCount(0);
  await expect(draftBar(page)).toBeHidden();
});

test("模型 V2: discovery stays unknown until declaration and verification confirmation", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1280, height: 900 });
  const providerId = "llmp_0123456789abcdef0123456789abcdef";
  const planId = "lvplan_0123456789abcdef0123456789abcdef";
  const batchId = "lvb_0123456789abcdef0123456789abcdef";
  await page.route("**/api/v1/admin/llm-config**", async (route: Route) => {
    const request = route.request();
    const url = new URL(request.url());
    const json = (body: unknown, status = 200) => route.fulfill({
      status,
      contentType: "application/json",
      body: JSON.stringify(body),
    });

    if (url.pathname.endsWith("/providers/precheck")) {
      await json({
        ok: true,
        provider_id: providerId,
        discovery: { supported: true },
        request_previews: [{
          purpose: "models",
          method: "GET",
          safe_url: "https://models.example.test/v1/models",
        }],
        warnings: [],
      });
      return;
    }
    if (url.pathname.endsWith(`/providers/${providerId}/discover-models`)) {
      await json({
        ok: true,
        provider_id: providerId,
        discovered_at: "2026-07-30T00:00:00Z",
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
      return;
    }
    if (url.pathname.endsWith("/verification-plans")) {
      await json({
        plan_id: planId,
        confirmation_token: "signed-confirmation-token-long-enough",
        expires_at: "2026-07-30T00:15:00Z",
        item_count: 2,
        provider_request_count: 2,
        max_concurrency: 3,
        items: [
          {
            provider_id: providerId,
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
            provider_id: providerId,
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
      return;
    }
    if (
      url.pathname.endsWith("/verification-batches")
      && request.method() === "POST"
    ) {
      await json({
        batch_id: batchId,
        plan_id: planId,
        state: "queued",
        revision: 0,
        replayed: false,
      }, 202);
      return;
    }
    if (url.pathname.endsWith(`/verification-batches/${batchId}`)) {
      await json({
        batch_id: batchId,
        plan_id: planId,
        state: "completed",
        revision: 4,
        base_config_version: 7,
        counts: { verified: 2 },
        items: [
          {
            item_id: "lvi_chat",
            ordinal: 0,
            provider_id: providerId,
            model_id: "gpt-enterprise",
            capability: "chat",
            state: "verified",
            revision: 2,
          },
          {
            item_id: "lvi_tool",
            ordinal: 1,
            provider_id: providerId,
            model_id: "gpt-enterprise",
            capability: "tool",
            state: "verified",
            revision: 4,
          },
        ],
      });
      return;
    }
    if (url.pathname.endsWith("/verification-batches")) {
      await json({ batches: [] });
      return;
    }
    await json({
      ok: true,
      version: 7,
      config: EMPTY_CONFIG,
      providers: [],
      features: {
        model_discovery_v2: true,
        capability_probes: true,
        azure_openai: true,
        custom_http: true,
      },
      provider_identities: {},
      verification: {
        as_of: "2026-07-30T00:00:00Z",
        entries: [],
      },
      migration: {
        state: "not_enrolled",
        enrolled_config_version: 7,
        baseline_state: "none",
      },
      reindex: null,
    });
  });

  await page.goto("/team/settings/models/", {
    waitUntil: "domcontentloaded",
  });
  await page.getByRole("button", {
    name: /添加第一个连接|Add the first connection|接入你的第一个模型来源|Connect your first model source/,
  }).click();
  await page.locator("#mset-provider-name").fill("corp-openai");
  await page.locator("#mset-provider-base-url").fill(
    "https://models.example.test/v1",
  );
  await page.locator("#mset-provider-api-key").fill("test-secret");
  await page.getByRole("button", {
    name: /读取模型目录|Read model catalog/,
  }).click();

  const dialog = page.getByRole("dialog", {
    name: /选择要添加的模型|Choose models to add/,
  });
  await expect(dialog).toBeVisible();
  await expect(dialog.getByText(/能力未知|Capabilities unknown/)).toHaveCount(2);
  await expect(dialog.getByRole("button", {
    name: /添加所选模型|Add selected models/,
  })).toBeDisabled();
  const dialogWidth = await dialog.evaluate(
    (element) => element.getBoundingClientRect().width,
  );
  expect(dialogWidth).toBeLessThanOrEqual(640);
  await page.setViewportSize({ width: 360, height: 800 });
  const mobileDialogBox = await dialog.boundingBox();
  expect(mobileDialogBox).not.toBeNull();
  expect(mobileDialogBox!.x).toBeGreaterThanOrEqual(0);
  expect(mobileDialogBox!.width).toBeLessThanOrEqual(360);
  expect(await page.evaluate(() => document.documentElement.scrollWidth))
    .toBeLessThanOrEqual(360);
  await page.screenshot({
    path: "test-results/models-v2-discovery-360-light.png",
    fullPage: true,
  });
  await page.setViewportSize({ width: 1280, height: 900 });

  await dialog.getByRole("option", { name: /gpt-enterprise/ }).click();
  await dialog.getByRole("button", {
    name: /添加所选模型|Add selected models/,
  }).click();
  const editor = page.locator(".medit-row").first();
  await expect(page.getByText(
    /请选择模型能力：|Choose model capabilities:/,
  )).toBeVisible();
  await editor.getByRole("button", { name: /^对话$|^Chat$/ }).click();
  await editor.getByRole("button", { name: /^工具$|^Tools$/ }).click();
  await page.getByRole("button", {
    name: /制定验证计划|Plan verification/,
  }).click();

  await expect(page.getByRole("heading", {
    name: /确认验证计划|Confirm verification plan/,
  })).toBeVisible();
  await expect(page.getByText(
    /将向模型服务发送 2 次请求|2 request\(s\) will be sent/,
  )).toBeVisible();
  await page.screenshot({
    path: "test-results/models-v2-plan-1280-light.png",
    fullPage: true,
  });
  await page.evaluate(() => {
    document.documentElement.dataset.theme = "dark";
  });
  // Off-state switch background uses runtime theme --c-surface. Since ae38ec503, runtime palette overrides
  // globals.css static values; e2e mock dark surface is #1D1B18 (mock-api-server.mjs baseThemeTokens).
  await expect(editor.locator(".captoggle.off").first())
    .toHaveCSS("background-color", "rgb(29, 27, 24)");
  await page.screenshot({
    path: "test-results/models-v2-plan-1280-dark.png",
    fullPage: true,
  });

  await page.getByRole("button", {
    name: /确认并开始验证|Confirm and start/,
  }).click();
  await expect(page.locator(".verification-item.state-verified")).toHaveCount(2);
});

// Each section has its own authenticated editor route.
test("模型设置分区：连接、分工和外部能力使用各自页面", async ({ page }) => {
  await mockLlmOverview(page);
  await page.route("**/api/v1/admin/llm-config", async (route: Route) => {
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({
      version: 7, config: TEAM_LLM_CONFIG, providers: [],
      features: { model_discovery_v2: true },
      provider_identities: {
        "我们的 OpenAI 代理": "llmp_0123456789abcdef0123456789abcdef",
        deepseek_anthropic: "llmp_fedcba9876543210fedcba9876543210",
      },
      verification: { as_of: "2026-09-17T00:00:00Z", entries: [] },
    }) });
  });
  await page.goto("/team/settings/models/");
  await page.getByRole("button", { name: "编辑连接 deepseek_anthropic" }).click();
  await expect(page).toHaveURL(/\/models\/connections\/edit\/?\?id=llmp_/);
  await expect(page.getByRole("heading", { name: "连接配置", exact: true })).toBeVisible();
  await expect(page.locator('input[name="provider_base_url"]')).toHaveValue("https://api.deepseek.com/anthropic");
  const contextWindow = page.locator('input[name="provider_model_context_window_0"]');
  const contextLabel = page.locator('label[for="mset-model-context-window-0"]');
  await expect(contextWindow).toBeVisible();
  const [labelBox, inputBox] = await Promise.all([contextLabel.boundingBox(), contextWindow.boundingBox()]);
  expect(labelBox && inputBox && Math.abs(labelBox.y - inputBox.y) < 12).toBeTruthy();
  await page.screenshot({ path: "/tmp/bowhead-model-connection.png", fullPage: true });
  await page.getByRole("button", { name: "返回概览" }).click();
  await page.getByRole("tab", { name: "模型分工" }).click();
  await page.getByRole("button", { name: "编辑分工" }).click();
  await expect(page).toHaveURL(/\/models\/assignments\/?$/);
  await expect(page.locator('select[name="primary_model"]')).toHaveValue("deepseek_anthropic::deepseek-v4-pro");
  await page.getByRole("button", { name: "返回概览" }).click();
  await page.getByRole("tab", { name: "外部能力" }).click();
  await expect(page.getByRole("heading", { name: "外部能力" })).toBeVisible();
  await page.getByRole("button", { name: "配置", exact: true }).first().click();
  await expect(page).toHaveURL(/\/models\/external\/image\/?$/);
  await page.getByRole("button", { name: "添加服务模型" }).click();
  await expect(page.getByLabel("模型 ID", { exact: true })).toBeVisible();
  await expect(page.getByLabel("连接", { exact: true })).toHaveValue("我们的 OpenAI 代理");
});

test("知识库索引失败在知识库页面进入审阅重试", async ({ page }) => {
  const failedConfig = {
    ...TEAM_LLM_CONFIG,
    kb: {
      ...TEAM_LLM_CONFIG.kb,
      pending_embed: {
        model: "qwen-text-embedding-v4",
        task_id: "kb_reindex_failed_test",
        state: "failed",
      },
    },
  };
  await mockLlmConfig(page, failedConfig);
  await page.goto("/knowledge/index/");
  await expect(page.getByRole("heading", { name: "知识库索引" })).toBeVisible();
  await page.getByRole("button", { name: "查看并重试" }).click();
  await expect(draftBar(page)).toBeVisible();
});
