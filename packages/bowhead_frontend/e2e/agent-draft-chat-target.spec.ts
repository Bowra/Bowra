import { expect, test, type Page } from "@playwright/test";
import { canvasWeek, chooseCanvasWeek, expectCanvasWeek } from "./_canvas-week";
import { mockAuthedApp } from "./_app-mocks";

async function mockEditingTargets(page: Page, protocol: "legacy_v0" | "terminal_v1", referenceInput = false) {
  await mockAuthedApp(page);
  let holdAuthRead = false;
  let preflightEntered = false;
  let releasePreflight!: () => void;
  const preflight = new Promise<void>((resolve) => { releasePreflight = resolve; });
  const posts: Array<Record<string, unknown>> = [];
  let sessionReads = 0;
  let losePostResponse = false;
  let actionLookups = 0;
  let receiptAvailable = false;
  let settingsProposalEnabled = false;
  let weekStart = "monday";
  let settingsReads = 0;
  let settingsWrites = 0;
  const proposalActions: string[] = [];
  let proposalStatus = "proposed";
  const settingsProposal = {
    proposal_id: "ccd_browser", turn_id: "t_target", form_id: "workspace_i18n", team_id: "team_smoke",
    owner_user_id: "u_smoke", workspace_id: "ws_smoke", workspace_name: "Original Space",
    rows: [{ field: "week_start_day", from: "monday", to: "sunday", from_unset: false }], notes: {},
    settings_target: { form_id: "workspace_i18n", user_id: "u_smoke", team_id: "team_smoke", workspace_id: "ws_smoke", edit_generation: 0 },
  };
  await page.route("**/api/v1/auth/me", async (route) => {
    if (holdAuthRead) {
      preflightEntered = true;
      await preflight;
    }
    await route.fulfill({ json: {
      id: "u_smoke", email: "smoke@example.com", name: "Smoke User", tier: "pro",
      team_id: "team_smoke", auth_session_generation: "auth-generation-smoke-1",
      capabilities: {
        composer: { reference_input: referenceInput },
        settings_pages_canvas: true,
        subagent_authoring: { authoring: true, unified_draft: true, draft_chat_edit: true },
      },
    } });
  });
  await page.route("**/api/v1/auth/me/workspaces", (route) => route.fulfill({ json: {
    items: [{ id: "ws_smoke", workspace_id: "ws_smoke", name: "Original Space", role: "admin" }],
    active_id: "ws_smoke",
  } }));
  // An unhydrated protocol hint forces the real Composer preflight. The
  // session decision stays constant; switching a Tab cannot change it.
  await page.route("**/api/v1/sessions/s_browser", async (route) => {
    const isPreflight = ++sessionReads > 1;
    await route.fulfill({ json: {
      session: { id: "s_browser", workspace_id: "ws_smoke", title: "Editing targets", status: "completed" },
      turns: [{ id: "t_existing", seq: 1, user_text: "Existing task", status: "completed", final_answer: "Existing answer", attachments: [] }],
      ...(isPreflight ? { chat_execution: { write_protocol: protocol, config_generation: "test", readiness_generation: "test" } } : {}),
    } });
  });
  await page.route("**/api/v1/team-subagents/tools", (route) => route.fulfill({ json: {
    team_tools: [], native_tools: ["bi_query"], text_only_allowed: false,
  } }));
  await page.route("**/api/v1/team-subagents/skills", (route) => route.fulfill({ json: { builtin: [], team_skills: [] } }));
  await page.route("**/api/v1/team-subagents/drafts/**", (route) => route.fulfill({ json:
    new URL(route.request().url()).pathname.endsWith("/suggestions")
      ? { schema_version: 2, kind: "team_subagent_canvas_suggestions", items: [] }
      : {
        schema_version: 2, kind: "team_subagent_manual_draft_detail", draft_id: "sad_browser",
        source_mode: "turn", entry_mode: "canvas", draft_status: "editing", workspace_id: "ws_smoke",
        purpose: "Analyze sales", definition: {
          name: "sales-bot", display_name: "Sales helper", description: "Analyze sales",
          system_prompt: "Analyze evidence", tool_refs: ["bi_query"], max_iterations: 12,
          model: "analyst", output_schema: null, output_schema_text: "",
        },
        warnings: [], validation: { status: "unknown", issues: [] }, version: 3,
        etag: 'W/"sad_browser:3"', mutation_id: "11111111-1111-4111-8111-111111111111",
        expires_at: "2099-10-01T00:00:00Z", result: null,
        actions: { edit: true, validate: true, create: false, discard: true },
      },
  }));
  await page.route("**/api/v1/workspace-settings/ws_smoke/metadata", (route) => route.fulfill({ json: {
    name: "Original Space", description: "", revision: 1,
  } }));
  await page.route("**/api/v1/workspace-settings/ws_smoke", (route) => {
    settingsReads += 1;
    return route.fulfill({ json: { settings: { week_start_day: weekStart } } });
  });
  await page.route("**/api/v1/workspace-settings/ws_smoke/week_start_day", (route) => {
    settingsWrites += 1;
    weekStart = route.request().postDataJSON().value;
    return route.fulfill({ json: {} });
  });
  await page.route("**/api/v1/config-proposals/ccd_browser**", (route) => {
    const action = new URL(route.request().url()).pathname.split("/").pop();
    if (route.request().method() === "POST") {
      proposalActions.push(action!);
      if (action === "apply") { proposalStatus = "applied"; weekStart = "sunday"; }
      if (action === "undo") { proposalStatus = "reverted"; weekStart = "monday"; }
    }
    return route.fulfill({ json: { ...settingsProposal, status: proposalStatus } });
  });
  const receipt = () => {
    const info = posts.at(-1)?.client_info as Record<string, string> | undefined;
    const turnId = protocol === "terminal_v1" && info?.request_id ? `t_${info.request_id.slice("act_start_".length)}` : "t_target";
    return {
      status: "started", session_id: "s_browser", turn_id: turnId, execution_generation: 1,
      stream_url: `/api/v1/chat/stream/${turnId}`,
      ...(protocol === "terminal_v1" ? { terminal_protocol_version: 1,
        action_id: info?.request_id, action_kind: "start", outcome: "admitted",
        ...(info?.agent_draft_id ? { agent_draft_target: { kind: "agent_draft", id: info.agent_draft_id, version: Number(info.agent_draft_version) } } : {}),
      } : {}),
    };
  };
  await page.route("**/api/v1/chat/send", async (route) => {
    posts.push(route.request().postDataJSON());
    if (losePostResponse) return route.abort("failed");
    await route.fulfill({ json: receipt() });
  });
  await page.route("**/api/v1/chat/actions/**", async (route) => {
    actionLookups += 1;
    if (!receiptAvailable) return route.fulfill({ status: 503, json: { detail: { error: { code: "terminal_dependency_recovering" } } } });
    await route.fulfill({ json: receipt() });
  });
  await page.route("**/api/v1/chat/stream/*", (route) => route.fulfill({
    contentType: "text/event-stream",
    body: (settingsProposalEnabled ? `event: config.lite_propose\ndata: ${JSON.stringify(settingsProposal)}\n\n` : "")
      + `event: done\ndata: ${JSON.stringify({ turn_id: receipt().turn_id, status: "completed" })}\n\n`,
  }));
  return {
    posts, proposalActions,
    setReferenceInput: (enabled: boolean) => { referenceInput = enabled; },
    losePostResponse: () => { losePostResponse = true; },
    actionLookups: () => actionLookups,
    allowReceipt: () => { receiptAvailable = true; },
    enableSettingsProposal: () => { settingsProposalEnabled = true; },
    settingsReads: () => settingsReads, settingsWrites: () => settingsWrites,
    holdPreflight: () => { holdAuthRead = true; },
    preflightEntered: () => preflightEntered,
    releasePreflight,
  };
}

async function inputDraftMessages(page: Page): Promise<string[]> {
  return page.evaluate(async () => {
    const request = indexedDB.open("bowhead-composer-recovery-v1");
    const db = await new Promise<IDBDatabase>((resolve, reject) => {
      request.onsuccess = () => resolve(request.result);
      request.onerror = () => reject(request.error);
    });
    try {
      return await new Promise<string[]>((resolve, reject) => {
        const read = db.transaction("ordinary_drafts").objectStore("ordinary_drafts").getAll();
        read.onsuccess = () => resolve(read.result.map((record) => record.message));
        read.onerror = () => reject(read.error);
      });
    } finally { db.close(); }
  });
}

test("home capability refresh preserves a competing draft until explicit merge and one Send", async ({ page, context }) => {
  const fixture = await mockEditingTargets(page, "legacy_v0", true);
  await page.goto("/?agent_draft=sad_browser");
  await expect(page.getByRole("textbox", { name: "名称", exact: true })).toHaveValue("Sales helper");
  await page.getByRole("combobox", { name: "任务输入", exact: true }).fill("Original local task");
  await expect.poll(() => inputDraftMessages(page)).toEqual(["Original local task"]);
  const other = await context.newPage();
  try {
    await mockEditingTargets(other, "legacy_v0", true);
    await other.goto("/?agent_draft=sad_browser");
    const remoteInput = other.getByRole("combobox", { name: "任务输入", exact: true });
    await expect(remoteInput).toHaveText("Original local task");
    await remoteInput.fill("Competing task");
    await expect.poll(() => inputDraftMessages(other)).toEqual(["Competing task"]);
    fixture.setReferenceInput(false);
    await page.evaluate(() => window.dispatchEvent(new Event("focus")));
    const input = page.getByRole("textbox", { name: "问点什么…", exact: true });
    await expect(input).toHaveValue("Original local task");
    await input.press("Enter");
    const merge = page.getByRole("button", { name: "合并最新草稿", exact: true });
    await expect(merge).toBeVisible();
    expect(fixture.posts).toEqual([]);
    expect(await inputDraftMessages(page)).toEqual(["Competing task"]);
    await merge.click();
    await expect(input).toHaveValue("Original local task\n\nCompeting task");
    await input.press("Enter");
    await expect.poll(() => fixture.posts.length).toBe(1);
    expect(fixture.posts[0].message).toBe("Original local task\n\nCompeting task");
  } finally { await other.close(); }
});

for (const referenceInput of [false, true]) {
  test(`passive Agent Canvas reopen retains the home draft revision with references ${referenceInput}`, async ({ page, context }) => {
    const fixture = await mockEditingTargets(page, "legacy_v0", referenceInput);
    await page.goto("/?agent_draft=sad_browser");
    await expect(page.getByRole("textbox", { name: "名称", exact: true })).toHaveValue("Sales helper");
    const input = referenceInput ? page.getByRole("combobox", { name: "任务输入", exact: true }) : page.getByRole("textbox", { name: "问点什么…", exact: true });
    await input.fill("Synthetic home draft handoff");
    await expect.poll(() => inputDraftMessages(page)).toEqual(["Synthetic home draft handoff"]);
    const other = await context.newPage();
    try {
      await mockEditingTargets(other, "legacy_v0", referenceInput);
      await other.goto("/?agent_draft=sad_browser");
      const preview = referenceInput ? other.getByRole("combobox", { name: "任务输入", exact: true }) : other.getByRole("textbox", { name: "问点什么…", exact: true });
      if (referenceInput) await expect(preview).toHaveText("Synthetic home draft handoff");
      else await expect(preview).toHaveValue("Synthetic home draft handoff");
      await expect(preview).toBeEditable();
    } finally { await other.close(); }
    await input.press("Enter");
    await expect.poll(() => fixture.posts.length).toBe(1);
    expect(fixture.posts[0].message).toBe("Synthetic home draft handoff");
  });
}

test("the surviving visible settings page owns a new send even when its retained URL was inactive", async ({ page }) => {
  const fixture = await mockEditingTargets(page, "legacy_v0");
  await page.goto("/?session=s_browser&canvas_settings=workspace_i18n&canvas_user=u_smoke&canvas_team=team_smoke&canvas_space=ws_smoke&canvas_settings_active=0");
  await expectCanvasWeek(page, "monday");
  const input = page.getByRole("textbox", { name: "问点什么…", exact: true });
  await input.fill("Set weeks to start on Sunday");
  await input.press("Enter");
  await expect.poll(() => fixture.posts.length).toBe(1);
  const info = fixture.posts[0].client_info as Record<string, string>;
  expect(JSON.parse(info.configuration_settings_target)).toMatchObject({ form_id: "workspace_i18n", user_id: "u_smoke", team_id: "team_smoke", workspace_id: "ws_smoke", edit_generation: 0 });
  expect(info).not.toHaveProperty("agent_draft_id");
  expect(fixture.settingsWrites()).toBe(0);
});

for (const { startsOnAgent, protocol } of [
  { startsOnAgent: true, protocol: "legacy_v0" as const },
  { startsOnAgent: false, protocol: "legacy_v0" as const },
  { startsOnAgent: true, protocol: "terminal_v1" as const },
]) {
  test(`${protocol} send retains ${startsOnAgent ? "Agent" : "Space settings"} target while preflight switches the Canvas selection`, async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 1000 });
    const fixture = await mockEditingTargets(page, protocol);
    await page.goto(`/?session=s_browser&agent_draft=sad_browser&canvas_settings=workspace_i18n&canvas_user=u_smoke&canvas_team=team_smoke&canvas_space=ws_smoke&canvas_settings_active=${startsOnAgent ? "0" : "1"}`);
    // C1 object tabs replace the object <select>; names may carry a status word.
    const objectTab = (name: string) => page.getByRole("tab", { name: new RegExp(`^${name}( · .+)?$`) });
    await expect(objectTab("Sales helper")).toBeVisible();
    await objectTab("Sales helper").click();
    await expect(page.getByRole("textbox", { name: "名称", exact: true })).toHaveValue("Sales helper");
    if (!startsOnAgent) await objectTab("空间偏好").click();
    const input = page.getByRole("textbox", { name: "问点什么…", exact: true });
    await input.fill("Shorten the instructions");
    fixture.holdPreflight();
    await input.press("Enter");
    await expect.poll(fixture.preflightEntered).toBe(true);
    const switched = objectTab(startsOnAgent ? "空间偏好" : "Sales helper");
    await switched.click();
    fixture.releasePreflight();
    await expect.poll(() => fixture.posts.length).toBe(1);
    const clientInfo = fixture.posts[0].client_info;
    if (!startsOnAgent) expect(JSON.parse((clientInfo as Record<string, string>).configuration_settings_target)).toMatchObject({ workspace_id: "ws_smoke", edit_generation: 0 });
    if (startsOnAgent) expect(clientInfo).toMatchObject({ agent_draft_id: "sad_browser", agent_draft_version: "3" });
    else expect(clientInfo).not.toHaveProperty("agent_draft_id");
    await expect(switched).toBeFocused();
  });
}

test("Terminal saves the exact draft target and recovers it after a lost POST response and refresh", async ({ page }) => {
  const fixture = await mockEditingTargets(page, "terminal_v1");
  fixture.losePostResponse();
  await page.goto("/?session=s_browser&agent_draft=sad_browser");
  await expect(page.getByRole("textbox", { name: "名称", exact: true })).toHaveValue("Sales helper");
  const input = page.getByRole("textbox", { name: "问点什么…", exact: true });
  await input.fill("Shorten the instructions");
  await input.press("Enter");
  await expect.poll(() => fixture.posts.length).toBe(1);
  expect(fixture.posts[0].client_info).toMatchObject({ action_kind: "start", agent_draft_id: "sad_browser", agent_draft_version: "3" });
  const pendingTargets = () => page.evaluate(async () => {
    const db = await new Promise<IDBDatabase>((resolve, reject) => {
      const request = indexedDB.open("bowhead-composer-recovery-v1");
      request.onsuccess = () => resolve(request.result);
      request.onerror = () => reject(request.error);
    });
    try {
      const records = await new Promise<Array<{ pendingAction?: { agentDraftTarget?: unknown } }>>((resolve, reject) => {
        const request = db.transaction("ordinary_drafts", "readonly").objectStore("ordinary_drafts").getAll();
        request.onsuccess = () => resolve(request.result);
        request.onerror = () => reject(request.error);
      });
      return records.flatMap((record) => record.pendingAction?.agentDraftTarget ? [record.pendingAction.agentDraftTarget] : []);
    } finally { db.close(); }
  });
  await expect(async () => {
    expect(await pendingTargets()).toContainEqual({ kind: "agent_draft", id: "sad_browser", version: 3 });
  }).toPass({ timeout: 5000 });
  const lookupsBeforeReload = fixture.actionLookups();
  fixture.allowReceipt();
  await page.reload();
  await expect.poll(fixture.actionLookups).toBeGreaterThan(lookupsBeforeReload);
  // Receipt adoption can navigate while the browser read is in flight.
  await expect(async () => { expect(await pendingTargets()).toEqual([]); }).toPass({ timeout: 5000 });
  expect(fixture.posts).toHaveLength(1);
});


test("the original settings page reviews without saving and confirms through the same domain service", async ({ page }) => {
  const fixture = await mockEditingTargets(page, "legacy_v0");
  fixture.enableSettingsProposal();
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto("/?session=s_browser&canvas_settings=workspace_i18n&canvas_user=u_smoke&canvas_team=team_smoke&canvas_space=ws_smoke");
  const week = canvasWeek(page);
  await expectCanvasWeek(page, "monday");
  const input = page.getByRole("textbox", { name: "问点什么…", exact: true });
  await input.fill("Change week start to Sunday");
  await input.press("Enter");
  await expect.poll(() => fixture.posts.length).toBe(1);
  const confirmation = page.getByTestId("lite-propose").last();
  await expect(confirmation).toBeVisible();
  expect(fixture.settingsWrites()).toBe(0);
  expect(fixture.proposalActions).toEqual([]);
  const readsBeforeApply = fixture.settingsReads();
  await confirmation.getByRole("button", { name: "应用", exact: true }).click();
  await expectCanvasWeek(page, "sunday");
  await expect.poll(() => fixture.settingsReads()).toBe(readsBeforeApply + 1);
  expect(fixture.proposalActions).toEqual(["apply"]);
  expect(fixture.settingsWrites()).toBe(0);
  await page.getByTestId("lite-applied").last().getByRole("button", { name: "撤销", exact: true }).click();
  await expectCanvasWeek(page, "monday");
  expect(fixture.proposalActions).toEqual(["apply", "undo"]);
  expect(fixture.settingsWrites()).toBe(0);
});

test("unsupported Terminal settings editing preserves the message and performs no mutation", async ({ page }) => {
  const fixture = await mockEditingTargets(page, "terminal_v1");
  await page.goto("/?session=s_browser&canvas_settings=workspace_i18n&canvas_user=u_smoke&canvas_team=team_smoke&canvas_space=ws_smoke");
  await expectCanvasWeek(page, "monday");
  const input = page.getByRole("textbox", { name: "问点什么…", exact: true });
  await input.fill("Change week start to Sunday");
  await input.press("Enter");
  await expect(page.getByText("设置编辑需要普通 Web 发送", { exact: false })).toBeVisible();
  await expect(input).toHaveValue("Change week start to Sunday");
  expect(fixture.posts).toEqual([]);
  expect(fixture.settingsWrites()).toBe(0);
});


test("settings send freezes the original generation before auth preparation and newer manual changes", async ({ page }) => {
  const fixture = await mockEditingTargets(page, "legacy_v0");
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto("/?session=s_browser&canvas_settings=workspace_i18n&canvas_user=u_smoke&canvas_team=team_smoke&canvas_space=ws_smoke");
  const week = canvasWeek(page);
  await expectCanvasWeek(page, "monday");
  const input = page.getByRole("textbox", { name: "问点什么…", exact: true });
  await input.fill("Explain the week setting");
  fixture.holdPreflight();
  await input.press("Enter");
  await expect.poll(fixture.preflightEntered).toBe(true);
  await chooseCanvasWeek(page, "sunday");
  await expect.poll(fixture.settingsWrites).toBe(1);
  fixture.releasePreflight();
  await expect.poll(() => fixture.posts.length).toBe(1);
  const info = fixture.posts[0].client_info as Record<string, string>;
  expect(JSON.parse(info.configuration_settings_target)).toMatchObject({ workspace_id: "ws_smoke", edit_generation: 0 });
  await expectCanvasWeek(page, "sunday");
});
