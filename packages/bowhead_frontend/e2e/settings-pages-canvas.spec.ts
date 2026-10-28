import { expect, test, type Page } from "@playwright/test";
import { canvasWeek, chooseCanvasWeek, expectCanvasWeek } from "./_canvas-week";
import { mockAuthedApp } from "./_app-mocks";

/** Object names may carry a status word (“Sales helper · 未保存”). */
const objectLabel = (label: string) => new RegExp(`^${label}( · .+)?$`);

/** C1: object tabs at ≥ 480 px of canvas width, a title switcher below. */
async function chooseObject(page: Page, compact: boolean, label: string) {
  if (compact) {
    await page.getByRole("button", { name: / · 画布里的对象$/ }).click();
    await page.getByRole("menuitemradio", { name: objectLabel(label) }).click();
  } else await page.getByRole("tab", { name: objectLabel(label) }).click();
}

/** Space settings close only from the tab's close button (D1). */
const closeObject = (page: Page) => page.getByRole("button", { name: /^关闭「.+」$/ });

for (const width of [412, 1440]) {
  test(`retained Agent input survives a late settings save at ${width}`, async ({ page }) => {
    await page.setViewportSize({ width, height: 1000 });
    await mockAuthedApp(page);
    await page.route("**/api/v1/auth/me", (route) =>
      route.fulfill({
        json: {
          id: "u_smoke",
          email: "smoke@example.com",
          name: "Smoke User",
          tier: "pro",
          team_id: "team_smoke",
          auth_session_generation: "auth-generation-smoke-1",
          capabilities: {
            settings_pages_canvas: true,
            subagent_authoring: {
              authoring: true,
              unified_draft: true,
              draft_chat_edit: false,
            },
          },
        },
      }),
    );
    await page.route("**/api/v1/auth/me/workspaces", (route) =>
      route.fulfill({
        json: {
          items: [
            {
              id: "ws_smoke",
              workspace_id: "ws_smoke",
              name: "Original Space",
              role: "admin",
            },
          ],
          active_id: "ws_smoke",
        },
      }),
    );
    await page.route("**/api/v1/team-subagents/tools", (route) =>
      route.fulfill({
        json: {
          team_tools: [],
          native_tools: ["bi_query"],
          text_only_allowed: false,
        },
      }),
    );
    await page.route("**/api/v1/team-subagents/skills", (route) => route.fulfill({ json: { builtin: [], team_skills: [] } }));
    await page.route("**/api/v1/team-subagents/drafts/**", (route) =>
      route.fulfill({
        json: new URL(route.request().url()).pathname.endsWith("/suggestions")
          ? {
              schema_version: 2,
              kind: "team_subagent_canvas_suggestions",
              items: [],
            }
          : {
              schema_version: 2,
              kind: "team_subagent_manual_draft_detail",
              draft_id: "sad_browser",
              source_mode: "turn",
              entry_mode: "canvas",
              draft_status: "editing",
              workspace_id: "ws_smoke",
              purpose: "Analyze sales",
              definition: {
                name: "sales-bot",
                display_name: "Sales helper",
                description: "Analyze sales",
                system_prompt: "Analyze evidence",
                tool_refs: ["bi_query"],
                max_iterations: 12,
                model: "analyst",
                output_schema: null,
                output_schema_text: "",
              },
              warnings: [],
              validation: { status: "unknown", issues: [] },
              version: 1,
              etag: 'W/"sad_browser:1"',
              mutation_id: "11111111-1111-4111-8111-111111111111",
              expires_at: "2099-10-01T00:00:00Z",
              result: null,
              actions: {
                edit: true,
                validate: true,
                create: false,
                discard: true,
              },
            },
      }),
    );
    await page.route("**/api/v1/workspace-settings/ws_smoke/metadata", (route) =>
      route.fulfill({
        json: { name: "Original Space", description: "", revision: 1 },
      }),
    );
    await page.route("**/api/v1/workspace-settings/ws_smoke", (route) => route.fulfill({ json: { settings: { week_start_day: "monday" } } }));
    let finishSave!: () => void;
    const save = new Promise<void>((resolve) => {
      finishSave = resolve;
    });
    let writes = 0;
    await page.route("**/api/v1/workspace-settings/ws_smoke/week_start_day", async (route) => {
      expect(route.request().method()).toBe("PUT");
      expect(route.request().postDataJSON()).toEqual({ value: "sunday" });
      writes += 1;
      await save;
      await route.fulfill({ json: { ok: true } });
    });
    await page.goto(
      "/?session=s_browser&agent_draft=sad_browser&canvas_settings=workspace_i18n&canvas_user=u_smoke&canvas_team=team_smoke&canvas_space=ws_smoke",
    );
    const compact = width < 480;
    await chooseObject(page, compact, "Sales helper");
    const instructions = page.getByRole("textbox", { name: "工作说明", exact: true });
    await expect(instructions).toHaveValue("Analyze evidence");
    await instructions.fill("keep-my-draft-input");
    await chooseObject(page, compact, "空间偏好");
    const week = canvasWeek(page);
    await chooseCanvasWeek(page, "sunday");
    await expect.poll(() => writes).toBe(1);
    await chooseObject(page, compact, "Sales helper");
    finishSave();
    await expect(instructions).toHaveValue("keep-my-draft-input");
    await expect(page).toHaveURL(/canvas_settings_active=0/);
    await chooseObject(page, compact, "空间偏好");
    await expectCanvasWeek(page, "sunday");
    await chooseObject(page, compact, "Sales helper");
    await expect(instructions).toHaveValue("keep-my-draft-input");
    expect(writes).toBe(1);
  });
}

for (const width of [412, 1440]) {
  test(`original Space preferences save and reopen in Canvas at ${width}`, async ({ page }) => {
    await page.setViewportSize({ width, height: 1000 });
    await mockAuthedApp(page);
    await page.route("**/api/v1/auth/me", (route) =>
      route.fulfill({
        json: {
          id: "u_smoke",
          email: "smoke@example.com",
          name: "Smoke User",
          tier: "pro",
          team_id: "team_smoke",
          auth_session_generation: "auth-generation-smoke-1",
          capabilities: { settings_pages_canvas: true },
        },
      }),
    );
    await page.route("**/api/v1/auth/me/workspaces", (route) =>
      route.fulfill({
        json: {
          items: [
            {
              id: "ws_smoke",
              workspace_id: "ws_smoke",
              name: "Original Space",
              role: "admin",
            },
          ],
          active_id: "ws_smoke",
        },
      }),
    );
    let settings: Record<string, unknown> = {
      week_start_day: "monday",
      default_data_language: "en-US",
    };
    const writes: Array<{ path: string; value: unknown }> = [];
    await page.route("**/api/v1/workspace-settings/ws_smoke/**", (route) => {
      const path = new URL(route.request().url()).pathname;
      if (path.endsWith("/metadata"))
        return route.fulfill({
          json: { name: "Original Space", description: "", revision: 1 },
        });
      const value = route.request().postDataJSON().value;
      writes.push({ path, value });
      settings = { ...settings, [path.split("/").at(-1)!]: value };
      return route.fulfill({ json: { ok: true } });
    });
    await page.route("**/api/v1/workspace-settings/ws_smoke", (route) => route.fulfill({ json: { settings } }));
    await page.goto("/space/settings/general/");
    const pageWeek = page.getByRole("combobox", { name: "周首日", exact: true });
    await expect(pageWeek).toHaveValue("monday");
    await page.getByRole("button", { name: "在 Canvas 中打开", exact: true }).click();
    await expect(page).toHaveURL(/canvas_space=ws_smoke/);
    await expect(page.getByRole("complementary", { name: "空间常规设置", exact: true })).toBeVisible();
    const week = canvasWeek(page);
    await expectCanvasWeek(page, "monday");
    await chooseCanvasWeek(page, "sunday");
    await expect.poll(() => writes.length).toBe(1);
    expect(writes[0]).toEqual({
      path: "/api/v1/workspace-settings/ws_smoke/week_start_day",
      value: "sunday",
    });
    await expectCanvasWeek(page, "sunday");
    const box = await week.boundingBox();
    expect(box && box.x >= 0 && box.x + box.width <= width + 1).toBeTruthy();
    await page.reload();
    await expectCanvasWeek(page, "sunday");
    await closeObject(page).click();
    await expect(page).not.toHaveURL(/canvas_settings=/);
    await expect(week).toHaveCount(0);
    await page.goto("/space/settings/general/");
    await expect(pageWeek).toHaveValue("sunday");
    expect(writes).toHaveLength(1);
  });
}

for (const width of [412, 1440]) {
  test(`original Space metadata retains newer input during save at ${width}`, async ({ page }) => {
    await page.setViewportSize({ width, height: 1000 });
    await mockAuthedApp(page);
    await page.route("**/api/v1/auth/me", (route) =>
      route.fulfill({
        json: {
          id: "u_smoke",
          email: "smoke@example.com",
          name: "Smoke User",
          tier: "pro",
          team_id: "team_smoke",
          auth_session_generation: "auth-generation-smoke-1",
          capabilities: { settings_pages_canvas: true },
        },
      }),
    );
    await page.route("**/api/v1/auth/me/workspaces", (route) =>
      route.fulfill({
        json: {
          items: [{ id: "ws_smoke", workspace_id: "ws_smoke", name: "Original Space", role: "admin" }],
          active_id: "ws_smoke",
        },
      }),
    );
    await page.route("**/api/v1/workspace-settings/ws_smoke", (route) => route.fulfill({ json: { settings: { week_start_day: "monday" } } }));
    let metadata = { name: "Original Space", description: "Original description", revision: 7 };
    const writes: unknown[] = [];
    let finishSave!: () => void;
    const pendingSave = new Promise<void>((resolve) => {
      finishSave = resolve;
    });
    await page.route("**/api/v1/workspace-settings/ws_smoke/metadata", async (route) => {
      if (route.request().method() === "GET") return route.fulfill({ json: metadata });
      const payload = route.request().postDataJSON();
      writes.push(payload);
      await pendingSave;
      metadata = { ...metadata, ...payload, revision: 8 };
      return route.fulfill({ json: metadata });
    });
    await page.goto("/?session=s_browser&canvas_settings=space_general&canvas_user=u_smoke&canvas_team=team_smoke&canvas_space=ws_smoke");
    const name = page.getByRole("textbox", { name: "Space 名称", exact: true });
    await expect(name).toHaveValue("Original Space");
    expect(writes).toHaveLength(0);
    await name.fill("Finance");
    expect(writes).toHaveLength(0);
    await page.getByRole("button", { name: "保存", exact: true }).click();
    await expect.poll(() => writes.length).toBe(1);
    await name.fill("Newer local name");
    // Closing during a save waits for it (toolbar note), then asks about the newer unsaved input (C14).
    await closeObject(page).click();
    await expect(page.getByText("保存中，完成后关闭…", { exact: true })).toBeVisible();
    await expect(name).toHaveValue("Newer local name");
    finishSave();
    await page.getByRole("dialog").getByRole("button", { name: "继续编辑", exact: true }).click();
    await expect(page.getByRole("button", { name: "保存", exact: true })).toBeEnabled();
    await expect(name).toHaveValue("Newer local name");
    await expect(page.getByText("保存中，完成后关闭…", { exact: true })).toHaveCount(0);
    expect(writes).toEqual([{ name: "Finance", expected_revision: 7 }]);
    const box = await name.boundingBox();
    expect(box && box.x >= 0 && box.x + box.width <= width + 1).toBeTruthy();
    await page.getByRole("button", { name: "放弃", exact: true }).click();
    await expect(name).toHaveValue("Finance");
    await closeObject(page).click();
    await expect(name).toHaveCount(0);
    await expect(page).toHaveURL("/?session=s_browser");
    await page.goto("/space/settings/general/");
    await expect(name).toHaveValue("Finance");
    expect(writes).toHaveLength(1);
  });
}
