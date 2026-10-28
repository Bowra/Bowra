import { expect, test } from "@playwright/test";
import { mockAuthedApp } from "./_app-mocks";

for (const width of [412, 1440]) for (const mode of ["light", "dark"] as const) {
  test(`Owner saves optional method policy and restores it at ${width} ${mode}`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: 1000 });
    await page.emulateMedia({ colorScheme: mode });
    await mockAuthedApp(page, { subagentManagement: true });
    const hash = `sha256:${"a".repeat(64)}`;
    const baseline = { system_prompt: "分析企业经营状况。", max_iterations: 10, tool_refs: ["bi_query"], skill_grants: [] };
    const live = { release_id: "arl_optional", release_sequence: 1, revision_id: "arv_optional", revision_kind: "imported_baseline", definition: baseline,
      listing: { display_name: "经营分析师", description: "解释企业的经营表现。" },
      audience: { schema_version: 1, effective_mode: "private", user_ids: [], group_ids: [] }, activated_at: "2026-09-15T00:00:00Z" };
    let working: Record<string, unknown> | null = null;
    const writes: Array<{ definition: Record<string, unknown>; expected_row_version: number | null }> = [];
    await page.route("**/api/v1/agents/**", async (route) => {
      const request = route.request();
      const path = new URL(request.url()).pathname;
      let body: unknown;
      if (path.endsWith("/authoring-catalog")) body = {
        kind: "agent_authoring_catalog", schema_version: 2, fingerprint: hash, behavior_catalog_fingerprint: hash, risk_registry_version: "risk-v1",
        model_policy: { allowed_roles: ["analyst"], effective_role: "analyst", locked: true, policy_version: "v1" }, primary_capabilities: [], skills: [],
        tools: [{ stable_ref: "bi_query", display_name: "查询数据", description: "查询当前可用数据", source_kind: "native", source_display_name: "Bowhead",
          capability_classes: ["data_query"], risk: "read", selectable: true, locked_reason: null, approval_required: false, confirmation_summary: null }],
      };
      else if (path.endsWith("/optional-skill-catalog")) body = { kind: "agent_optional_skill_catalog", schema_version: 1, agent_id: "osa_optional",
        items: ["builtin", "team", "user"].map((source) => ({ resource: { kind: "skill", source_kind: source, id: `${source}:dupont`, version: hash },
          label: "杜邦分析", description: "从利润率、资产周转率与权益乘数分析盈利能力。", allowed_audiences: source === "user" ? ["private"] : ["private", "custom", "teamwide"] })), next_offset: null };
      else if (path.endsWith("/working-copy")) {
        if (request.method() === "PATCH") {
          const payload = request.postDataJSON();
          writes.push(payload);
          working = { ...payload, id: "awc_optional", base_release_id: live.release_id, row_version: 1, updated_at: "2026-09-15T00:00:00Z" };
          body = { kind: "agent_working_copy", schema_version: 1, agent_id: "osa_optional", ...working };
        } else body = { kind: "agent_working_copy_detail", schema_version: 1, agent_id: "osa_optional", aggregate_row_version: 1,
          aggregate: { name: "analyst", status: "enabled", lifecycle_state: "active" }, live, working_copy: working, pending_candidate: null,
          actions: { edit: true, publish: Boolean(working), enable: false, disable: true, archive: true, restore: false } };
      } else if (path.endsWith("/releases")) body = { kind: "agent_release_history", schema_version: 1, agent_id: "osa_optional", items: [] };
      else { await route.fulfill({ status: 404, contentType: "application/json", body: '{"detail":"unavailable"}' }); return; }
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(body) });
    });
    // The Owner route first reads the manager detail to pick the revisioned release workbench.
    await page.route("**/api/v1/team-subagents/osa_optional?detail=manager", (route) => route.fulfill({ json: {
      kind: "team_owner_manager_detail", schema_version: 1, id: "osa_optional", definition_mode: "revisioned",
    } }));
    // The release workbench also lists team capability bundles (none in this fixture).
    await page.route("**/api/v1/team-capabilities", (route) => route.fulfill({ json: [] }));
    await page.goto("/agents/manage/?agent_id=osa_optional");
    // Owner configuration is split into views; optional methods live with the audience settings.
    await page.getByRole("navigation", { name: "智能体配置内容" }).getByRole("link", { name: "可见范围" }).click();
    await page.getByRole("button", { name: "配置可选方法" }).click();
    const choices = page.locator('input[name="agent-optional-skill"]');
    await expect(choices).toHaveCount(3);
    await choices.nth(2).check();
    await page.getByRole("checkbox", { name: "允许用户为单个任务添加方法" }).check();
    await page.getByRole("combobox", { name: "每个任务最多选择" }).selectOption("2");
    const panel = page.locator(".am-optional-skills");
    await panel.scrollIntoViewIfNeeded();
    const box = await panel.boundingBox();
    expect(box && box.x >= 0 && box.x + box.width <= width + 1).toBeTruthy();
    const contrasts = await panel.locator('.am-release-picker-grid > label[data-selected="true"]').evaluate((element) => {
      const canvas = document.createElement("canvas");
      canvas.width = canvas.height = 1;
      const context = canvas.getContext("2d")!;
      const luminance = (color: string) => {
        context.clearRect(0, 0, 1, 1);
        context.fillStyle = color;
        context.fillRect(0, 0, 1, 1);
        const channels = Array.from(context.getImageData(0, 0, 1, 1).data).slice(0, 3).map((value) => {
          const channel = value / 255;
          return channel <= 0.04045 ? channel / 12.92 : ((channel + 0.055) / 1.055) ** 2.4;
        });
        return channels[0] * 0.2126 + channels[1] * 0.7152 + channels[2] * 0.0722;
      };
      const background = luminance(getComputedStyle(element).backgroundColor);
      return [...element.querySelectorAll("strong, small, em")].map((text) => {
        const foreground = luminance(getComputedStyle(text).color);
        return (Math.max(background, foreground) + 0.05) / (Math.min(background, foreground) + 0.05);
      });
    });
    expect(contrasts.length).toBe(3);
    for (const ratio of contrasts) expect(ratio).toBeGreaterThanOrEqual(4.5);
    await panel.screenshot({ path: testInfo.outputPath("optional-methods.png") });
    await page.getByRole("button", { name: "保存草稿" }).click();
    await expect.poll(() => writes.length).toBe(1);
    expect(writes[0].definition.runtime_extensions).toMatchObject({ schema_version: 1, skill_read: { enabled: true, max_selected: 2,
      resource_allowlist: [{ source_kind: "user", id: "user:dupont", version: hash }] } });
    expect(writes[0].definition.skill_grants).toEqual([]);
    expect(writes[0].definition.tool_refs).toEqual(baseline.tool_refs);
    expect(writes[0].expected_row_version).toBeNull();
    await expect(page.getByRole("button", { name: "保存草稿" })).toBeDisabled();
    await page.reload();
    await expect(page.getByRole("checkbox", { name: "允许用户为单个任务添加方法" })).toBeChecked();
    await expect(page.getByRole("combobox", { name: "每个任务最多选择" })).toHaveValue("2");
    expect(writes).toHaveLength(1);
  });
}
