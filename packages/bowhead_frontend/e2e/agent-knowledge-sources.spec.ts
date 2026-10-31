import { expect, test } from "@playwright/test";
import { mockAuthedApp } from "./_app-mocks";

for (const width of [412, 1440]) for (const mode of ["light", "dark"] as const) {
  test(`Owner saves source selection and restores it at ${width} ${mode}`, async ({ page }, testInfo) => {
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
      else if (path.endsWith("/knowledge-sources")) body = { enabled: true, available: true,
        items: [{ id: "kbf_source", name: "Source One", scope: "personal" }], selected_items: [{ id: "kbf_source", name: "Source One", scope: "personal" }], total: 1, max_selected: 64, top_k_min: 1, top_k_max: 50 };
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
    await page.getByRole("button", { name: /^谁能用、怎么上架/ }).click();
    const panel = page.locator(".am-knowledge-sources");
    await panel.getByRole("combobox", { name: "来源范围" }).selectOption("selected_files");
    await expect(panel.getByText("当前不允许读取任何知识文件。添加文件后，此智能体才能读取对应知识。")).toBeVisible();
    await panel.getByRole("checkbox", { name: "Source One" }).check();
    await panel.getByRole("spinbutton").fill("5");
    await panel.scrollIntoViewIfNeeded();
    const box = await panel.boundingBox();
    expect(box && box.x >= 0 && box.x + box.width <= width + 1).toBeTruthy();
    await panel.screenshot({ path: testInfo.outputPath("knowledge-sources.png") });
    await page.getByRole("button", { name: "保存草稿" }).click();
    await expect.poll(() => writes.length).toBe(1);
    expect(writes[0].definition.knowledge_access).toEqual({ schema_version: 1, mode: "selected_files", file_ids: ["kbf_source"], defaults: { top_k: 5 } });
    await page.reload();
    await page.getByRole("button", { name: /^谁能用、怎么上架/ }).click();
    await expect(panel.getByRole("combobox", { name: "来源范围" })).toHaveValue("selected_files");
    await expect(panel.getByRole("checkbox", { name: "Source One" })).toBeChecked();
    await panel.getByRole("button", { name: "移除 Source One" }).click();
    await page.getByRole("button", { name: "保存草稿" }).click();
    await expect.poll(() => writes.length).toBe(2);
    expect(writes[1].definition.knowledge_access).toEqual({ schema_version: 1, mode: "selected_files", file_ids: [], defaults: { top_k: 5 } });
    await panel.getByRole("combobox", { name: "来源范围" }).selectOption("context_readable");
    await page.getByRole("button", { name: "保存草稿" }).click();
    await expect.poll(() => writes.length).toBe(3);
    expect(writes[2].definition.knowledge_access).toEqual({ schema_version: 1, mode: "context_readable", defaults: { top_k: 5 } });
  });
}
