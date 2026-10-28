import { expect, test, type Page } from "@playwright/test";
import { mobilePaneTab } from "./_mobile-pane";
import { canvasWeek, chooseCanvasWeek, expectCanvasWeek } from "./_canvas-week";
import { mockAuthedApp } from "./_app-mocks";

async function mockDrafts(page: Page) {
  await mockAuthedApp(page);
  await page.route("**/api/v1/chat/sessions/*/reference-queue?*", (route) => route.fulfill({ json: { items: [] } }));
  const definitions: Record<string, Record<string, unknown>> = {
    sad_a: {
      name: "agent-a",
      display_name: "Agent A",
      description: "Analyze evidence",
      system_prompt: "Use evidence",
      tool_refs: ["bi_query"],
      model: "analyst",
      max_iterations: 12,
      output_schema: null,
      output_schema_text: "",
    },
    sad_b: {
      name: "agent-b",
      display_name: "Agent B",
      description: "Analyze evidence",
      system_prompt: "Use evidence",
      tool_refs: ["bi_query"],
      model: "analyst",
      max_iterations: 12,
      output_schema: null,
      output_schema_text: "",
    },
    sad_c: {
      name: "agent-c",
      display_name: "Agent C",
      description: "Analyze evidence",
      system_prompt: "Use evidence",
      tool_refs: ["bi_query"],
      model: "analyst",
      max_iterations: 12,
      output_schema: null,
      output_schema_text: "",
    },
  };
  const versions: Record<string, number> = { sad_a: 1, sad_b: 1, sad_c: 1 };
  const writes: Array<{ id: string; body: Record<string, unknown> }> = [];
  const creations: string[] = [];
  const validated = new Set<string>();
  let hold = false;
  let entered = false;
  let release!: () => void;
  const pending = new Promise<void>((resolve) => {
    release = resolve;
  });
  let suggested = false;
  let applied = false;
  let readsDenied = false;
  let rejectWrites = false;
  const scopes: Record<string, string>[] = [];
  const detail = (id: string) => ({
    schema_version: 2,
    kind: "team_subagent_manual_draft_detail",
    draft_id: id,
    source_mode: "turn",
    entry_mode: "canvas",
    draft_status: creations.includes(id) ? "consumed" : "editing",
    workspace_id: "ws_smoke",
    purpose: "Analyze evidence",
    definition: creations.includes(id) ? {} : definitions[id],
    version: versions[id],
    etag: `W/"${id}:${versions[id]}"`,
    mutation_id: "11111111-1111-4111-8111-111111111111",
    expires_at: "2099-10-01T00:00:00Z",
    warnings: [],
    validation: { status: "unknown", issues: [] },
    result: creations.includes(id) ? { definition_id: `osa_${id}`, consumed_at: "2026-10-03T18:00:00Z" } : null,
    actions: { edit: !creations.includes(id), validate: !creations.includes(id), create: validated.has(id) && !creations.includes(id), discard: !creations.includes(id) },
  });
  const suggestion = () => ({
    schema_version: 2,
    kind: "team_subagent_canvas_suggestion",
    suggestion_id: "sas_browser",
    draft_id: "sad_a",
    base_version: versions.sad_a,
    suggestion_kind: "definition",
    status: applied ? "applied" : "pending",
    created_at: "2026-10-03T00:00:00Z",
    expires_at: "2099-10-01T00:00:00Z",
    payload: {
      changed_fields: ["description"],
      definition: { ...definitions.sad_a, description: "Proposed task" },
    },
  });
  await page.route("**/api/v1/auth/me", (route) =>
    route.fulfill({
      json: {
        id: "u_smoke",
        email: "smoke@example.com",
        name: "Smoke User",
        tier: "pro",
        team_id: "team_smoke",
        auth_session_generation: "auth-generation-smoke-1",
        chat_execution: {
          write_protocol: "legacy_v0",
          config_generation: "test",
          readiness_generation: "test",
        },
        capabilities: {
          configuration_canvas: true,
          settings_pages_canvas: true,
          subagent_authoring: {
            authoring: true,
            unified_draft: true,
            draft_chat_edit: true,
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
  await page.route("**/api/v1/sessions/s_browser", (route) =>
    route.fulfill({
      json: {
        session: {
          id: "s_browser",
          workspace_id: "ws_smoke",
          title: "Editing drafts",
          status: "completed",
        },
        turns: [
          {
            id: "t_old",
            seq: 1,
            user_text: "Existing task",
            status: "completed",
            final_answer: "Existing answer",
            attachments: [],
          },
        ],
        chat_execution: {
          write_protocol: "legacy_v0",
          config_generation: "test",
          readiness_generation: "test",
        },
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
  await page.route("**/api/v1/team-subagents/skills", (route) =>
    route.fulfill({ json: { builtin: [], team_skills: [] } }),
  );
  await page.route("**/api/v1/team-subagents/osa_sad_*?detail=manager", (route) => {
    const id = new URL(route.request().url()).pathname.split("/").at(-1)!.replace("osa_", "");
    return route.fulfill({ json: { ...definitions[id], id: `osa_${id}`, team_id: "team_smoke", schema_version: 1,
      kind: "team_owner_manager_detail", tool_count: 1, skills: [], visibility: "creator_only", actions: { bring_to_web_chat: true } } });
  });
  await page.route("**/api/v1/team-subagents/drafts**", async (route) => {
    scopes.push(route.request().headers());
    const path = new URL(route.request().url()).pathname;
    const id = path.includes("/sad_c") ? "sad_c" : path.includes("/sad_b") ? "sad_b" : "sad_a";
    if (path.endsWith("/validate")) {
      validated.add(id);
      return route.fulfill({ json: detail(id) });
    }
    if (path.endsWith("/create")) {
      creations.push(id);
      return route.fulfill({ json: { schema_version: 2, kind: "team_subagent_created_from_draft", definition_id: `osa_${id}`, replayed: false } });
    }
    if (path.endsWith("/drafts"))
      return route.fulfill({
        json: {
          schema_version: 2,
          kind: "team_subagent_draft_list",
          items: ["sad_a", "sad_b", "sad_c"].map((key) => ({
            draft_id: key,
            draft_status: "editing",
            source_mode: "turn",
            title: definitions[key].display_name,
            updated_at: "2026-10-03T00:00:00Z",
            expires_at: "2099-10-01T00:00:00Z",
          })),
        },
      });
    if (path.endsWith("/suggestions")) {
      if (route.request().method() === "POST") {
        suggested = true;
        return route.fulfill({ json: suggestion() });
      }
      return route.fulfill({
        json: {
          schema_version: 2,
          kind: "team_subagent_canvas_suggestions",
          items: id === "sad_a" && suggested ? [suggestion()] : [],
        },
      });
    }
    if (path.endsWith("/apply")) {
      applied = true;
      versions[id] += 1;
      definitions[id].description = "Proposed task";
      return route.fulfill({ json: detail(id) });
    }
    if (route.request().method() === "PATCH") {
      const body = route.request().postDataJSON();
      writes.push({ id, body });
      if (rejectWrites) return route.fulfill({ status: 503, json: { detail: { code: "temporary_failure" } } });
      if (hold && id === "sad_a") {
        entered = true;
        await pending;
      }
      definitions[id] = body.definition;
      versions[id] += 1;
    }
    if (readsDenied && route.request().method() === "GET")
      return route.fulfill({ status: 403, json: { detail: { code: "subagent_draft_not_found", message: "Unavailable" } } });
    return route.fulfill({ json: detail(id) });
  });
  return {
    writes,
    creations,
    scopes,
    rejectWrites: () => { rejectWrites = true; },
    hold: () => {
      hold = true;
    },
    entered: () => entered,
    release,
    denyReads: () => { readsDenied = true; },
  };
}

// Canvas object fields (D9: the identifier lives under the collapsed “高级” section).
const displayName = (page: Page) => page.getByRole("textbox", { name: "名称", exact: true });
const instructions = (page: Page) => page.getByRole("textbox", { name: "工作说明", exact: true });
const composerInput = (page: Page) => page.getByRole("textbox", { name: "问点什么…", exact: true });

async function identifier(page: Page) {
  const toggle = page.getByRole("button", { name: /^高级/ });
  if ((await toggle.getAttribute("aria-expanded")) !== "true") await toggle.click();
  return page.getByRole("textbox", { name: /^标识名/ });
}

/** Below 480 px of canvas width the tab strip becomes a title switcher (C1). */
async function openSwitcher(page: Page) {
  await page.getByRole("button", { name: / · 画布里的对象$/ }).click();
}

/** Object names may carry a status word (“名称 · 未保存”) while the object has unsaved or failed work. */
const objectLabel = (label: string) => new RegExp(`^${label.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}( · .+)?$`);

async function chooseObject(page: Page, compact: boolean, label: string) {
  if (compact) {
    await openSwitcher(page);
    await page.getByRole("menuitemradio", { name: objectLabel(label) }).click();
  } else await page.getByRole("tab", { name: objectLabel(label) }).click();
}

async function openNewAgentEntry(page: Page, compact = false) {
  if (compact) {
    await openSwitcher(page);
    await page.getByRole("menuitem", { name: "打开…", exact: true }).click();
  } else await page.getByRole("button", { name: "打开…", exact: true }).first().click();
  await page.getByRole("menuitem", { name: "新建智能体", exact: true }).click();
}

async function openB(page: Page, compact = false) {
  await openNewAgentEntry(page, compact);
  await page.getByRole("button", { name: "继续", exact: true }).nth(1).click();
  await expect(page).toHaveURL(/agent_draft=sad_b/);
  await expect(displayName(page)).toHaveValue("Agent B");
}

/** The hosted editor's back button carries the open parent's name (C2), or a generic label when the parent is not open. */
const backTo = (page: Page, editor: string, parent: string) =>
  page.getByRole("region", { name: editor, exact: true }).getByRole("button", { name: new RegExp(`^(${parent}|返回智能体)$`) });

test("same-name drafts remain distinguishable after an acknowledged rename", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 });
  const fixture = await mockDrafts(page);
  await page.goto("/?session=s_browser&agent_draft=sad_a");
  await expect(displayName(page)).toHaveValue("Agent A");
  await displayName(page).fill("Agent B");
  // D4: the draft saves itself; there is no save button.
  await expect.poll(() => fixture.writes.length).toBe(1);
  expect(fixture.writes[0].body.definition).toMatchObject({ name: "agent-a", display_name: "Agent B" });
  await openB(page);
  await expect(page.getByRole("tab", { name: "Agent B · 第 1 份", exact: true })).toBeVisible();
  await expect(page.getByRole("tab", { name: "Agent B · 第 2 份", exact: true })).toBeVisible();
  await page.getByRole("tab", { name: "Agent B · 第 1 份", exact: true }).click();
  await expect(page).toHaveURL(/agent_draft=sad_a/);
  await expect(await identifier(page)).toHaveValue("agent-a");
});

test("keyboard closing the selected draft leaves focus on the surviving object", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 });
  await mockDrafts(page);
  await page.goto("/?session=s_browser&agent_draft=sad_a");
  await expect(displayName(page)).toHaveValue("Agent A");
  await openB(page);
  await page.getByRole("tab", { name: "Agent A", exact: true }).click();
  await page.getByRole("button", { name: "关闭「Agent A」", exact: true }).focus();
  await page.keyboard.press("Enter");
  await expect(page.getByRole("tab", { name: "Agent B", exact: true })).toBeFocused();
  await expect(page).toHaveURL(/agent_draft=sad_b/);
});

test("closing a background Agent preserves the visible settings page and its reload target", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 });
  await mockDrafts(page);
  await page.route("**/api/v1/workspace-settings/ws_smoke/metadata", (route) => route.fulfill({ json: { name: "Original Space", description: "", revision: 1 } }));
  await page.route("**/api/v1/workspace-settings/ws_smoke", (route) => route.fulfill({ json: { settings: { week_start_day: "monday" } } }));
  await page.goto("/?session=s_browser&agent_draft=sad_a&canvas_settings=workspace_i18n&canvas_user=u_smoke&canvas_team=team_smoke&canvas_space=ws_smoke&canvas_settings_active=0");
  await expect(displayName(page)).toHaveValue("Agent A");
  await openB(page);
  await page.getByRole("tab", { name: "空间偏好", exact: true }).click();
  const week = canvasWeek(page);
  await expectCanvasWeek(page, "monday");
  const composer = composerInput(page);
  await composer.fill("Keep this unsent request");
  await page.getByRole("button", { name: "关闭「Agent B」", exact: true }).click();
  await expect(page.getByRole("tab", { name: "空间偏好", exact: true })).toHaveAttribute("aria-selected", "true");
  await expect(composer).toHaveValue("Keep this unsent request");
  await expect(page).not.toHaveURL(/agent_draft=/);
  await expect(page).not.toHaveURL(/canvas_settings_active=0/);
  await page.reload();
  await expectCanvasWeek(page, "monday");
  await expect(displayName(page)).toHaveCount(0);
});

test("a denied canonical read clears the original draft fields and target", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 });
  const fixture = await mockDrafts(page);
  await page.goto("/?session=s_browser&agent_draft=sad_a");
  await expect(displayName(page)).toHaveValue("Agent A");
  fixture.denyReads();
  await page.getByRole("button", { name: "根据新的描述重新生成…", exact: true }).first().click();
  await expect(displayName(page)).toHaveCount(0);
  await expect(page.getByRole("button", { name: "重试读取", exact: true })).toBeVisible();
  await expect(page.getByRole("tab", { name: "Agent A", exact: true })).toHaveCount(0);
  expect(fixture.writes).toHaveLength(0);
});

test("the compact title switcher exposes unsaved status during a pending original save", async ({ page }) => {
  await page.setViewportSize({ width: 412, height: 1000 });
  const fixture = await mockDrafts(page);
  await page.goto("/?session=s_browser&agent_draft=sad_a");
  await expect(displayName(page)).toHaveValue("Agent A");
  fixture.hold();
  try {
    await instructions(page).fill("local-a");
    await expect.poll(fixture.entered).toBe(true);
    await openB(page, true);
    await openSwitcher(page);
    await expect(page.getByRole("menuitemradio", { name: /^Agent A · / })).toBeVisible();
    await page.keyboard.press("Escape");
    await expect(displayName(page)).toHaveValue("Agent B");
  } finally { fixture.release(); }
});

test("a delayed close preserves the newly selected draft and its URL", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 });
  const fixture = await mockDrafts(page);
  await page.goto("/?session=s_browser&agent_draft=sad_a");
  await expect(displayName(page)).toHaveValue("Agent A");
  fixture.hold();
  try {
    await instructions(page).fill("local-a");
    // The tab × waits for the in-flight autosave and closes when it lands (no “save and leave” prompt).
    await page.getByRole("button", { name: "关闭「Agent A」", exact: true }).click();
    await expect.poll(fixture.entered).toBe(true);
    await expect(page.getByRole("dialog")).toHaveCount(0);
    await openB(page);
    await openNewAgentEntry(page);
    await page.getByRole("button", { name: "继续", exact: true }).nth(2).click();
    await expect(displayName(page)).toHaveValue("Agent C");
    fixture.release();
    await expect(page.getByRole("tab", { name: "Agent A", exact: true })).toHaveCount(0);
    await expect(displayName(page)).toHaveValue("Agent C");
    await expect(page).toHaveURL(/agent_draft=sad_c/);
    expect(fixture.writes.filter((write) => write.id === "sad_a")).toHaveLength(1);
  } finally {
    fixture.release();
  }
});

for (const width of [412, 1440]) {
  test(`two real drafts retain independent input and late saves across Tabs and Results at ${width}`, async ({
    page,
  }) => {
    const compact = width < 480;
    await page.setViewportSize({ width, height: 1000 });
    const fixture = await mockDrafts(page);
    await page.goto("/?session=s_browser&agent_draft=sad_a");
    await expect(displayName(page)).toHaveValue("Agent A");
    const original = await instructions(page).elementHandle();
    fixture.hold();
    try {
      await instructions(page).fill("local-a");
      await expect.poll(fixture.entered).toBe(true);
      await openB(page, compact);
      await instructions(page).fill("local-b");
      fixture.release();
      await expect(instructions(page)).toHaveValue("local-b");
      const input = composerInput(page);
      if (width < 1024)
        await page.getByRole("tab", { name: "对话", exact: true }).click();
      await input.fill("Keep this unsent message");
      if (width < 1024)
        await mobilePaneTab(page).click();
      await chooseObject(page, compact, "成果");
      await chooseObject(page, compact, "Agent B");
      if (compact) await chooseObject(page, compact, "Agent A");
      else {
        await page.getByRole("tab", { name: "Agent B", exact: true }).focus();
        await page.keyboard.press("ArrowLeft");
        await expect(
          page.getByRole("tab", { name: "Agent A", exact: true }),
        ).toHaveAttribute("aria-selected", "true");
      }
      await expect(instructions(page)).toHaveValue("local-a");
      const screenshot = test.info().outputPath("retained-editors.png");
      await page.screenshot({ path: screenshot });
      await test.info().attach("Retained Agent editors", {
        path: screenshot,
        contentType: "image/png",
      });
      // The original editor element survives the round trip (no remount).
      expect(await instructions(page).evaluate((element, first) => element === first, original)).toBe(true);
      expect(
        fixture.writes.find((write) => write.id === "sad_a")?.body.definition,
      ).toMatchObject({ system_prompt: "local-a" });
      if (width < 1024)
        await page.getByRole("tab", { name: "对话", exact: true }).click();
      await expect(input).toHaveValue("Keep this unsent message");
    } finally {
      fixture.release();
    }
  });
}

test("generating and adopting a suggestion keep the continuous editor and existing save API", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1440, height: 1100 });
  const fixture = await mockDrafts(page);
  await page.goto("/?session=s_browser&agent_draft=sad_a");
  await expect(displayName(page)).toHaveValue("Agent A");
  await page.getByRole("button", { name: "根据新的描述重新生成…", exact: true }).first().click();
  await page.getByRole("textbox", { name: /新的描述/ }).fill("Improve this task");
  await page.getByRole("button", { name: "生成建议", exact: true }).click();
  // D8: suggestions are one section above the fields, adopted per field or all at once.
  await page.getByRole("button", { name: "全部采用", exact: true }).click();
  await expect(page.getByRole("textbox", { name: /^用途说明/ })).toHaveValue("Proposed task");
  await expect(displayName(page)).toHaveValue("Agent A");
  expect(fixture.writes).toHaveLength(0);
});

test("the first accepted conversation retains the home draft editor and its original target", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1440, height: 1000 });
  await mockDrafts(page);
  const posts: Array<Record<string, unknown>> = [];
  await page.route("**/api/v1/chat/send", (route) => {
    posts.push(route.request().postDataJSON());
    return route.fulfill({
      json: {
        status: "started",
        session_id: "s_browser",
        turn_id: "t_first",
        execution_generation: 1,
        stream_url: "/api/v1/chat/stream/t_first",
      },
    });
  });
  await page.route("**/api/v1/chat/stream/t_first*", (route) =>
    route.fulfill({
      contentType: "text/event-stream",
      body: 'event: done\ndata: {"turn_id":"t_first","status":"completed"}\n\n',
    }),
  );
  await page.goto("/?agent_draft=sad_a");
  await expect(displayName(page)).toHaveValue("Agent A");
  const firstEditor = await instructions(page).elementHandle();
  const input = composerInput(page);
  await input.fill("Explain these instructions");
  await input.press("Enter");
  await expect.poll(() => posts.length).toBe(1);
  expect(posts[0].client_info).toMatchObject({
    agent_draft_id: "sad_a",
    agent_draft_version: "1",
  });
  await expect(page).toHaveURL(/session=s_browser/);
  await expect(displayName(page)).toHaveValue("Agent A");
  expect(await instructions(page).evaluate((element, first) => element === first, firstEditor)).toBe(true);
});

test("a late settings Tool opening preserves the newly selected Agent and its input", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1440, height: 1000 });
  await mockDrafts(page);
  await page.route("**/api/v1/workspace-settings/ws_smoke/metadata", (route) =>
    route.fulfill({
      json: { name: "Original Space", description: "", revision: 1 },
    }),
  );
  await page.route("**/api/v1/workspace-settings/ws_smoke", (route) =>
    route.fulfill({ json: { settings: { week_start_day: "monday" } } }),
  );
  let release!: () => void;
  let streaming = false;
  const pending = new Promise<void>((resolve) => {
    release = resolve;
  });
  await page.route("**/api/v1/chat/send", (route) =>
    route.fulfill({
      json: {
        status: "started",
        turn_id: "t_background_settings",
        session_id: "s_browser",
        execution_generation: 1,
        stream_url: "/api/v1/chat/stream/t_background_settings",
      },
    }),
  );
  await page.route(
    "**/api/v1/chat/stream/t_background_settings*",
    async (route) => {
      streaming = true;
      await pending;
      return route.fulfill({
        contentType: "text/event-stream",
        body: [
          'event: configuration.canvas_open\ndata: {"seq":1,"turn_id":"t_background_settings","target":{"page":"workspace_i18n","user_id":"u_smoke","team_id":"team_smoke","workspace_id":"ws_smoke"}}\n\n',
          'event: done\ndata: {"seq":2,"turn_id":"t_background_settings","status":"completed"}\n\n',
        ].join(""),
      });
    },
  );
  try {
    await page.goto("/?session=s_browser&agent_draft=sad_a");
    await expect(displayName(page)).toHaveValue("Agent A");
    const composer = composerInput(page);
    await composer.fill("Open Space preferences");
    await composer.press("Enter");
    await expect.poll(() => streaming).toBe(true);
    await openB(page);
    await instructions(page).fill("keep-local-b");
    release();
    await expect(
      page.getByRole("tab", { name: "空间偏好", exact: true }),
    ).toBeVisible();
    await expect(instructions(page)).toHaveValue("keep-local-b");
    await expect(
      page.getByRole("tab", { name: /^Agent B/ }),
    ).toHaveAttribute("aria-selected", "true");
    await page.getByRole("tab", { name: "空间偏好", exact: true }).click();
    await expectCanvasWeek(page, "monday");
  } finally {
    release();
  }
});

test("original Coach results preserve another editor and refresh reads without repeating effects", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 });
  await mockDrafts(page);
  const posts: Array<Record<string, unknown>> = [];
  const reads: Array<Record<string, unknown>> = [];
  let release!: () => void;
  let streaming = false;
  let denied = false;
  const pending = new Promise<void>((resolve) => { release = resolve; });
  const reference = { kind: "skill_save", operation_id: `csw_${"a".repeat(64)}` };
  const payload = { seq: 1, request_id: "b".repeat(32), turn_id: "t_operation", session_id: "s_browser", user_id: "u_smoke", team_id: "team_smoke", workspace_id: "ws_smoke", reference };
  const events = [
    { seq: 1, event_type: "coach.operation_open", payload },
    { seq: 2, event_type: "done", payload: { seq: 2, turn_id: "t_operation", status: "completed" } },
  ];
  await page.route("**/api/v1/chat/send", (route) => {
    posts.push(route.request().postDataJSON());
    return route.fulfill({ json: { status: "started", turn_id: "t_operation", session_id: "s_browser", execution_generation: 1, stream_url: "/api/v1/chat/stream/t_operation" } });
  });
  await page.route("**/api/v1/chat/stream/t_operation*", async (route) => {
    streaming = true;
    await pending;
    return route.fulfill({ contentType: "text/event-stream", body: events.map((event) => `event: ${event.event_type}\ndata: ${JSON.stringify(event.payload)}\n\n`).join("") });
  });
  await page.route("**/api/v1/turns/t_operation/events", (route) => route.fulfill({ json: { events } }));
  await page.route("**/api/v1/sessions/s_browser", (route) => posts.length ? route.fulfill({ json: {
    session: { id: "s_browser", workspace_id: "ws_smoke", title: "Original operation", status: "completed" },
    turns: [{ id: "t_operation", seq: 1, user_text: "Show the Skill result", status: "completed", final_answer: "Original result is available.", attachments: [] }],
    chat_execution: { write_protocol: "legacy_v0", config_generation: "test", readiness_generation: "test" },
  } }) : route.fallback());
  await page.route("**/api/v1/agent-coach/operations/read", (route) => {
    reads.push(route.request().postDataJSON());
    return route.fulfill(denied ? { status: 404, json: { detail: { error: { code: "coach_operation_unavailable" } } } } : { json: {
      ok: true, schema_version: 1, read_status: "available", effects_applied_by_this_read: false, operation_ref: reference,
      domain_result: { ok: false, state: "prepared", outcome: "unknown", original_target: { name: "private-skill-result" } },
    } });
  });
  try {
    await page.goto("/?session=s_browser&agent_draft=sad_a");
    await expect(displayName(page)).toHaveValue("Agent A");
    const composer = composerInput(page);
    await composer.fill("Show the Skill result");
    await composer.press("Enter");
    await expect.poll(() => streaming).toBe(true);
    await openB(page);
    await instructions(page).fill("keep-local-b");
    release();
    // An unknown outcome keeps the neutral operation name; it never claims the Skill was saved.
    const resultTab = page.getByRole("tab", { name: "技能保存", exact: true });
    await expect(resultTab).toBeVisible();
    await expect(page.getByRole("tab", { name: /^Agent B/ })).toHaveAttribute("aria-selected", "true");
    await expect(instructions(page)).toHaveValue("keep-local-b");
    expect(reads).toHaveLength(0);
    await resultTab.click();
    // The Skill slug is an identifier, not a title: the header falls back to the operation name.
    await expect(page.getByRole("heading", { name: "技能保存", exact: true })).toBeVisible();
    await expect(page.getByText("private-skill-result")).toHaveCount(0);
    await expect(page.getByTestId("coach-operation-card")).toContainText("结果尚不确定");
    await composer.fill("Explain this result");
    await composer.press("Enter");
    await expect.poll(() => posts.length).toBe(2);
    expect(posts[1].client_info).not.toHaveProperty("agent_draft_id");
    await page.reload();
    await expect(page.getByTestId("coach-operation-card")).toContainText("结果尚不确定");
    expect(posts).toHaveLength(2);
    expect(reads.length).toBeGreaterThanOrEqual(2);
    expect(reads.every((body) => JSON.stringify(body) === JSON.stringify({ reference, workspace_id: "ws_smoke", session_id: "s_browser" }))).toBe(true);
    denied = true;
    await page.getByRole("button", { name: "刷新", exact: true }).click();
    await expect(page.getByText("暂时无法查看这个结果", { exact: true })).toBeVisible();
    await expect(page.getByTestId("coach-operation-card")).toHaveCount(0);
    const count = reads.length;
    await page.getByRole("button", { name: "关闭「技能保存」", exact: true }).click();
    await expect(resultTab).toHaveCount(0);
    expect(reads).toHaveLength(count);
    expect(posts).toHaveLength(2);
  } finally { release(); }
});

for (const width of [412, 1440]) {
test(`original Skill Canvas preserves parent and child input and replays only authorized reads at ${width}`, async ({ page }) => {
  const compact = width < 480;
  await page.setViewportSize({ width, height: 1000 });
  await mockDrafts(page);
  const posts: Array<Record<string, unknown>> = [];
  const reads: Array<Record<string, unknown>> = [];
  const reference = { kind: "skill", source_kind: "user", id: "sk_browser", revision: `sha256:${"a".repeat(64)}` };
  const parent = { kind: "agent_draft", id: "sad_a", version: 1 };
  const detail = {
    id: "sk_browser", name: "browser-method", title: "Browser method", source_type: "user_upload", enabled: true,
    description: "Read evidence", size_bytes: 200, content_sha256: "a".repeat(64), body_md: "Read evidence.",
    body_raw: "---\nname: browser-method\ndescription: Read evidence\n---\nRead evidence.\n",
    scripts: [], references: [], allowed_tools: [], trigger_examples: [], evals_count: 0, has_evals: false,
  };
  let denied = false;
  const events = [
    { seq: 1, event_type: "coach.resource_open", payload: { seq: 1, request_id: "c".repeat(32), turn_id: "t_resource", session_id: "s_browser", user_id: "u_smoke", team_id: "team_smoke", workspace_id: "ws_smoke", reference, parent } },
    { seq: 2, event_type: "done", payload: { seq: 2, turn_id: "t_resource", status: "completed" } },
  ];
  await page.route("**/api/v1/chat/send", (route) => {
    posts.push(route.request().postDataJSON());
    return route.fulfill({ json: { status: "started", turn_id: "t_resource", session_id: "s_browser", execution_generation: 1, stream_url: "/api/v1/chat/stream/t_resource" } });
  });
  await page.route("**/api/v1/chat/stream/t_resource*", (route) => route.fulfill({ contentType: "text/event-stream", body: events.map((event) => `event: ${event.event_type}\ndata: ${JSON.stringify(event.payload)}\n\n`).join("") }));
  await page.route("**/api/v1/turns/t_resource/events", (route) => route.fulfill({ json: { events } }));
  await page.route("**/api/v1/sessions/s_browser", (route) => posts.length ? route.fulfill({ json: {
    session: { id: "s_browser", workspace_id: "ws_smoke", title: "Original resource", status: "completed" },
    turns: [{ id: "t_resource", seq: 1, user_text: "Open the Skill", status: "completed", final_answer: "Skill available.", attachments: [] }],
    chat_execution: { write_protocol: "legacy_v0", config_generation: "test", readiness_generation: "test" },
  } }) : route.fallback());
  await page.route("**/api/v1/skills/browser-method*", (route) => route.fulfill({ json: detail }));
  await page.route("**/api/v1/agent-coach/resources/editor", (route) => {
    reads.push(route.request().postDataJSON());
    return route.fulfill(denied ? { status: 404, json: { detail: { error: { code: "coach_resource_editor_unavailable" } } } } : { json: {
      ok: true, schema_version: 1, reference, parent, editor_kind: "skill", title: "Browser method", read_only: false, target: { kind: "skill", skill: detail },
    } });
  });
  await page.goto("/?session=s_browser&agent_draft=sad_a");
  await expect(displayName(page)).toHaveValue("Agent A");
  const composer = composerInput(page);
  if (width < 1024) await page.getByRole("tab", { name: "对话", exact: true }).click();
  await composer.fill("Open the Skill");
  await composer.press("Enter");
  await expect.poll(() => posts.length).toBe(1);
  if (width < 1024) await mobilePaneTab(page).click();
  await instructions(page).fill("parent-local-input");
  await expect(instructions(page)).toHaveValue("parent-local-input");
  expect(reads).toHaveLength(0);
  await chooseObject(page, compact, "资源编辑器");
  // Hosted Skill editors start in edit mode on the instructions only (D10): no manifest header is shown.
  const body = page.getByRole("textbox", { name: "步骤说明", exact: true });
  const skillSteps = "Read evidence.\n";
  await expect(body).toHaveValue(skillSteps);
  await body.fill(`${skillSteps}Local child input.\n`);
  await backTo(page, "Browser method", "Agent A").click();
  await expect(instructions(page)).toHaveValue("parent-local-input");
  await chooseObject(page, compact, "Browser method");
  await expect(body).toHaveValue(`${skillSteps}Local child input.\n`);
  if (width < 1024) await page.getByRole("tab", { name: "对话", exact: true }).click();
  await composer.fill("Explain this Skill");
  await composer.press("Enter");
  await expect(page.getByText("请先保存或放弃此编辑器中的修改，再发送消息。", { exact: true })).toBeVisible();
  expect(posts).toHaveLength(1);
  await expect(composer).toHaveValue("Explain this Skill");
  if (width < 1024) await mobilePaneTab(page).click();
  await body.fill(skillSteps);
  if (width < 1024) await page.getByRole("tab", { name: "对话", exact: true }).click();
  await composer.press("Enter");
  await expect.poll(() => posts.length).toBe(2);
  expect(posts[1].client_info).not.toHaveProperty("agent_draft_id");
  expect(JSON.parse((posts[1].client_info as Record<string, string>).coach_canvas_target)).toMatchObject({
    schema_version: 1, user_id: "u_smoke", team_id: "team_smoke", workspace_id: "ws_smoke", session_id: "s_browser",
    target: reference, dirty: false, pending_write: false,
  });
  page.once("dialog", (dialog) => dialog.accept());
  await page.reload();
  if (width < 1024) await mobilePaneTab(page).click();
  else await expect(page.getByRole("tab", { name: "Browser method", exact: true })).toBeVisible();
  await expect(body).toHaveValue(skillSteps);
  expect(posts).toHaveLength(2);
  for (const read of reads) expect(read).toEqual({ reference, parent, workspace_id: "ws_smoke", session_id: "s_browser" });
  await backTo(page, "Browser method", "Agent A").click();
  denied = true;
  await chooseObject(page, compact, "Browser method");
  await expect(page.getByText("无法读取这个技能", { exact: true })).toBeVisible();
  await expect(body).toHaveCount(0);
  expect(posts).toHaveLength(2);
});

}

for (const width of [412, 1440]) {
  test(`original Plugin Canvas keeps input across parent return and enforces its saved baseline at ${width}`, async ({ page }) => {
    const compact = width < 480;
    await page.setViewportSize({ width, height: 1000 });
    await mockDrafts(page);
    const reference = { kind: "capability", id: "tcap_browser", revision: "1" };
    const parent = { kind: "agent_draft", id: "sad_a", version: 1 };
    let plugin = { id: "tcap_browser", team_id: "team_smoke", name: "Original Plugin", description: "Original purpose",
      row_version: 1, revision: 1, content_hash: "first", status: "active", tool_refs: ["bi_query"], skill_grants: [], agent_count: 1 };
    const writes: Array<Record<string, unknown>> = [];
    let readOnly = false;
    let denied = false;
    await page.route("**/api/v1/agents/authoring-catalog", (route) => route.fulfill({ json: {
      kind: "agent_authoring_catalog", schema_version: 2, fingerprint: "plugin-fixture", behavior_catalog_fingerprint: "plugin-fixture",
      risk_registry_version: "test", model_policy: { allowed_roles: [], effective_role: "", locked: false, policy_version: "test" },
      primary_capabilities: [], skills: [], tools: [{ stable_ref: "bi_query", display_name: "BI query", description: "Read provided data",
        source_kind: "native", source_display_name: null, capability_classes: [], risk: "read", selectable: true, locked_reason: null,
        approval_required: false, confirmation_summary: null }],
    } }));
    const event = { seq: 1, event_type: "coach.resource_open", payload: { seq: 1, request_id: "d".repeat(32), turn_id: "t_plugin", session_id: "s_browser", user_id: "u_smoke", team_id: "team_smoke", workspace_id: "ws_smoke", reference, parent } };
    await page.route("**/api/v1/sessions/s_browser", (route) => route.fulfill({ json: {
      session: { id: "s_browser", workspace_id: "ws_smoke", title: "Plugin editing", status: "completed" },
      turns: [{ id: "t_plugin", seq: 1, user_text: "Open the Plugin", status: "completed", final_answer: "Original Plugin is available.", attachments: [] }],
      chat_execution: { write_protocol: "legacy_v0", config_generation: "test", readiness_generation: "test" },
    } }));
    await page.route("**/api/v1/turns/t_plugin/events", (route) => route.fulfill({ json: { events: [event, { seq: 2, event_type: "done", payload: { seq: 2, turn_id: "t_plugin", status: "completed" } }] } }));
    await page.route("**/api/v1/agent-coach/resources/editor", (route) => route.fulfill(denied ? { status: 404, json: { detail: "Unavailable" } } : { json: {
      ok: true, schema_version: 1, reference: { ...reference, revision: String(plugin.revision) }, parent, editor_kind: "plugin", title: plugin.name, read_only: readOnly,
      target: { kind: "plugin", plugin },
    } }));
    await page.route("**/api/v1/team-capabilities/tcap_browser", (route) => {
      const body = route.request().postDataJSON(); writes.push(body);
      if (readOnly) return route.fulfill({ status: 403, json: { detail: "Denied" } });
      if (body.row_version !== plugin.row_version) return route.fulfill({ status: 409, json: { detail: "capability_version_conflict" } });
      plugin = { ...plugin, ...body, row_version: plugin.row_version + 1, revision: plugin.revision + 1 };
      return route.fulfill({ json: plugin });
    });
    // Hosted Plugin editors save through the shared save bar (C9), shown only with unsaved changes.
    const save = page.getByRole("button", { name: "保存", exact: true });
    await page.goto("/?session=s_browser&agent_draft=sad_a");
    await expect(displayName(page)).toHaveValue("Agent A");
    await instructions(page).fill("retained-parent");
    await chooseObject(page, compact, "资源编辑器");
    const name = page.getByRole("textbox", { name: "名称", exact: true });
    await expect(name).toHaveValue("Original Plugin");
    await name.fill("Saved Plugin");
    await save.click();
    await expect.poll(() => writes.length).toBe(1);
    expect(writes[0]).toMatchObject({ row_version: 1, name: "Saved Plugin", tool_refs: ["bi_query"] });
    const description = page.getByRole("textbox", { name: "介绍", exact: true });
    await description.fill("Newer local input");
    await backTo(page, "Saved Plugin", "Agent A").click();
    await expect(instructions(page)).toHaveValue("retained-parent");
    plugin = { ...plugin, row_version: 3, revision: 3, description: "New server content" };
    await chooseObject(page, compact, "Saved Plugin");
    await expect(description).toHaveValue("Newer local input");
    await save.click();
    await expect(page.getByText("无法保存插件，请检查资源和权限后重试。", { exact: true })).toBeVisible();
    expect(writes[1]).toMatchObject({ row_version: 2, description: "Newer local input" });
    await expect(description).toHaveValue("Newer local input");
    await backTo(page, "Saved Plugin", "Agent A").click();
    readOnly = true;
    await chooseObject(page, compact, "Saved Plugin");
    await expect(name).toBeDisabled();
    await expect(save).toHaveCount(0);
    page.once("dialog", (dialog) => dialog.accept());
    await page.reload();
    if (width < 1024) await mobilePaneTab(page).click();
    await expect(description).toHaveValue("New server content");
    await expect(name).toBeDisabled();
    expect(writes).toHaveLength(2);
    await backTo(page, "Saved Plugin", "Agent A").click();
    denied = true;
    await chooseObject(page, compact, "Saved Plugin");
    await expect(page.getByText("无法读取这个插件", { exact: true })).toBeVisible();
    await expect(name).toHaveCount(0);
    expect(writes).toHaveLength(2);
  });
}

for (const width of [412, 1440]) {
  test(`original Agent Canvas retains the working copy and original save precondition at ${width}`, async ({ page }) => {
    const compact = width < 480;
    await page.setViewportSize({ width, height: 1000 });
    await mockDrafts(page);
    const reference = { kind: "agent", id: "osa_browser", revision: "sha256:original" };
    const parent = { kind: "agent_draft", id: "sad_a", version: 1 };
    const audience = { schema_version: 1, effective_mode: "private", user_ids: [], group_ids: [] };
    let working = { id: "awc_browser", base_release_id: "arl_browser", row_version: 2,
      definition: { system_prompt: "Use original evidence", tool_refs: ["bi_query"], max_iterations: 12 },
      listing: { display_name: "Existing Agent", description: "Original purpose" }, audience,
      validation: { status: "not_validated" }, change_kind: "authored", rollback_source_release_id: null, updated_at: "2026-10-03T00:00:00Z" };
    const detail = () => ({ kind: "agent_working_copy_detail", schema_version: 1, agent_id: reference.id, aggregate_row_version: 1,
      aggregate: { name: "existing-agent", status: "enabled", lifecycle_state: "active" },
      live: { release_id: "arl_browser", release_sequence: 1, revision_id: "arv_browser", revision_kind: "authored",
        definition: working.definition, listing: working.listing, audience, activated_at: "2026-10-03T00:00:00Z" },
      working_copy: working, pending_candidate: null,
      actions: { edit: true, publish: true, enable: false, disable: true, archive: true, restore: false } });
    const posts: Array<Record<string, unknown>> = [];
    let refuseStaleSend = false;
    await page.route("**/api/v1/chat/send", (route) => {
      posts.push(route.request().postDataJSON());
      if (refuseStaleSend) return route.fulfill({ status: 409, json: { error: { code: "coach_canvas_target_unavailable", admission: "not_started" } } });
      return route.fulfill({ json: { status: "started", session_id: "s_browser", turn_id: "t_agent_reply", stream_url: "/api/v1/chat/stream/t_agent_reply" } });
    });
    await page.route("**/api/v1/chat/stream/t_agent_reply*", (route) => route.fulfill({ contentType: "text/event-stream",
      body: 'event: done\ndata: {"turn_id":"t_agent_reply","status":"completed"}\n\n' }));
    const writes: Array<Record<string, unknown>> = [];
    let readOnly = false;
    let denied = false;
    await page.route("**/api/v1/agents/authoring-catalog", (route) => route.fulfill({ json: {
      kind: "agent_authoring_catalog", schema_version: 2, fingerprint: "agent-fixture", behavior_catalog_fingerprint: "agent-fixture",
      risk_registry_version: "test", model_policy: { allowed_roles: [], effective_role: "analyst", locked: true, policy_version: "test" },
      primary_capabilities: [], skills: [], tools: [{ stable_ref: "bi_query", display_name: "BI query", description: "Read provided data",
        source_kind: "native", source_display_name: null, capability_classes: [], risk: "read", selectable: true, locked_reason: null,
        approval_required: false, confirmation_summary: null }],
    } }));
    await page.route("**/api/v1/team-capabilities", (route) => route.fulfill({ json: [] }));
    await page.route("**/api/v1/agents/osa_browser/releases", (route) => route.fulfill({ json: { kind: "agent_release_history", schema_version: 1, agent_id: reference.id, items: [] } }));
    await page.route("**/api/v1/agents/osa_browser/fork-settings", (route) => route.fulfill({ status: 404, json: { detail: "Not found" } }));
    await page.route("**/api/v1/agents/osa_browser/working-copy", (route) => {
      if (route.request().method() === "GET") return route.fulfill({ json: detail() });
      const body = route.request().postDataJSON(); writes.push(body);
      if (body.expected_row_version !== working.row_version) return route.fulfill({ status: 409, json: { detail: "Original working copy changed" } });
      working = { ...working, definition: body.definition, listing: body.listing, audience: body.audience, row_version: working.row_version + 1 };
      return route.fulfill({ json: { ...working, kind: "agent_working_copy", schema_version: 1, agent_id: reference.id } });
    });
    const event = { seq: 1, event_type: "coach.resource_open", payload: { seq: 1, request_id: "e".repeat(32), turn_id: "t_agent", session_id: "s_browser", user_id: "u_smoke", team_id: "team_smoke", workspace_id: "ws_smoke", reference, parent } };
    await page.route("**/api/v1/sessions/s_browser", (route) => route.fulfill({ json: {
      session: { id: "s_browser", workspace_id: "ws_smoke", title: "Agent editing", status: "completed" },
      turns: [{ id: "t_agent", seq: 1, user_text: "Open the Agent", status: "completed", final_answer: "Existing Agent is available.", attachments: [] }],
      chat_execution: { write_protocol: "legacy_v0", config_generation: "test", readiness_generation: "test" },
    } }));
    await page.route("**/api/v1/turns/t_agent/events", (route) => route.fulfill({ json: { events: [event, { seq: 2, event_type: "done", payload: { seq: 2, turn_id: "t_agent", status: "completed" } }] } }));
    await page.route("**/api/v1/agent-coach/resources/editor", (route) => route.fulfill(denied ? { status: 404, json: { detail: "Unavailable" } } : { json: {
      ok: true, schema_version: 1, reference, parent, editor_kind: "agent", title: "Existing Agent", read_only: readOnly,
      target: { kind: "agent", agent_id: reference.id, detail: detail() },
    } }));
    const editor = page.getByRole("region", { name: "Existing Agent", exact: true });
    const save = page.getByRole("button", { name: "保存", exact: true });
    const reloadSaved = async () => {
      await editor.getByRole("button", { name: "更多操作", exact: true }).click();
      await page.getByRole("menuitem", { name: "载入已保存版本…", exact: true }).click();
    };
    await page.goto("/?session=s_browser&agent_draft=sad_a");
    await expect(displayName(page)).toHaveValue("Agent A");
    await instructions(page).fill("retained-parent");
    await chooseObject(page, compact, "资源编辑器");
    const prompt = page.getByRole("textbox", { name: "智能体指令", exact: true });
    await expect(prompt).toHaveValue("Use original evidence");
    await prompt.fill("Saved focused instructions");
    await save.click();
    await expect.poll(() => writes.length).toBe(1);
    await expect(save).toHaveCount(0);
    expect(writes[0].expected_row_version).toBe(2);
    if (width < 1024) await page.getByRole("tab", { name: "对话", exact: true }).click();
    const composer = composerInput(page);
    await composer.fill("Explain this saved Agent");
    await composer.press("Enter");
    await expect.poll(() => posts.length).toBe(1);
    expect(JSON.parse((posts[0].client_info as Record<string, string>).coach_canvas_target)).toMatchObject({
      target: { kind: "agent", id: reference.id, version: 1, live_release_id: "arl_browser",
        working_copy: { id: "awc_browser", version: 3, base_release_id: "arl_browser" } }, dirty: false, pending_write: false,
    });
    refuseStaleSend = true;
    await composer.fill("Keep this request after the resource changes");
    await composer.press("Enter");
    await expect.poll(() => posts.length).toBe(2);
    await expect(page.getByTestId("send-init-error").getByText("原资源或保存版本已不可用，请检查编辑器后重新发送；消息内容已保留。", { exact: true })).toBeVisible();
    await expect(composer).toHaveValue("Keep this request after the resource changes");
    if (width < 1024) await mobilePaneTab(page).click();
    await prompt.fill("Newer retained input");
    await backTo(page, "Existing Agent", "Agent A").click();
    await expect(instructions(page)).toHaveValue("retained-parent");
    working = { ...working, row_version: 4, definition: { ...working.definition, system_prompt: "New server instructions" } };
    await chooseObject(page, compact, "Existing Agent");
    await expect(prompt).toHaveValue("Newer retained input");
    await save.click();
    await expect.poll(() => writes.length).toBe(2);
    expect(writes[1].expected_row_version).toBe(3);
    await expect(prompt).toHaveValue("Newer retained input");
    // C14: one guard copy for every object.
    await reloadSaved();
    await page.getByRole("dialog").getByRole("button", { name: "继续编辑", exact: true }).click();
    await expect(prompt).toHaveValue("Newer retained input");
    await reloadSaved();
    await page.getByRole("dialog").getByRole("button", { name: "放弃修改", exact: true }).click();
    await expect(prompt).toHaveValue("New server instructions");
    expect(writes).toHaveLength(2);
    await backTo(page, "Existing Agent", "Agent A").click();
    await expect(instructions(page)).toHaveValue("retained-parent");
    readOnly = true;
    await chooseObject(page, compact, "Existing Agent");
    await expect(prompt).toBeDisabled();
    page.once("dialog", (dialog) => dialog.accept());
    await page.reload();
    if (width < 1024) await mobilePaneTab(page).click();
    await expect(prompt).toHaveValue("New server instructions");
    await expect(prompt).toBeDisabled();
    expect(writes).toHaveLength(2);
    await backTo(page, "Existing Agent", "Agent A").click();
    denied = true;
    await chooseObject(page, compact, "Existing Agent");
    await expect(page.getByText("无法读取这个智能体", { exact: true })).toBeVisible();
    await expect(prompt).toHaveCount(0);
  });
}


test("original draft requests ignore ambient Space and Team changes", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 });
  const fixture = await mockDrafts(page);
  await page.goto("/?session=s_browser&agent_draft=sad_a");
  await expect(displayName(page)).toHaveValue("Agent A");
  await page.evaluate(() => {
    localStorage.setItem("bowhead.team_id", "team_other");
    localStorage.setItem("bowhead.space_id", "ws_other");
    localStorage.setItem("bowhead.workspace_id", "ws_other");
  });
  fixture.scopes.length = 0;
  await displayName(page).fill("Saved in original Space");
  await expect.poll(() => fixture.writes.length).toBe(1);
  await page.getByRole("button", { name: "根据新的描述重新生成…", exact: true }).first().click();
  await expect(page.getByRole("textbox", { name: /新的描述/ })).toBeVisible();
  expect(fixture.scopes.length).toBeGreaterThan(0);
  for (const headers of fixture.scopes) {
    expect(headers["x-team-id"]).toBe("team_smoke");
    expect(headers["x-space-id"]).toBe("ws_smoke");
  }
});

test("failed-save input and Chat input survive desktop and compact transitions", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 });
  const fixture = await mockDrafts(page);
  await page.goto("/?session=s_browser&agent_draft=sad_a");
  const name = displayName(page);
  await expect(name).toHaveValue("Agent A");
  const composer = composerInput(page);
  await composer.fill("Keep my unsent task");
  fixture.rejectWrites();
  await name.fill("Unsaved after failure");
  await expect.poll(() => fixture.writes.length).toBe(1);
  // The toolbar reports the failed autosave and offers a retry (C2).
  await expect(page.getByRole("alert").filter({ hasText: "保存失败" })).toBeVisible();
  await expect(page.getByRole("button", { name: "重试保存", exact: true })).toBeVisible();
  await page.setViewportSize({ width: 412, height: 915 });
  await expect(composer).toHaveValue("Keep my unsent task");
  await mobilePaneTab(page).click();
  await expect(name).toHaveValue("Unsaved after failure");
  await page.setViewportSize({ width: 1440, height: 1000 });
  await expect(name).toHaveValue("Unsaved after failure");
  await page.getByRole("button", { name: "重开对话", exact: true }).click();
  await expect(composer).toHaveValue("Keep my unsent task");
  expect(fixture.writes).toHaveLength(1);
});

for (const width of [412, 1440]) for (const locale of ["zh-CN", "en-US"]) {
  test(`Canvas keeps one object through editing, creation and receipt reload at ${width} in ${locale}`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: 1000 });
    await page.context().addCookies([{ name: "NEXT_LOCALE", value: locale, url: String(testInfo.project.use.baseURL) }]);
    const fixture = await mockDrafts(page);
    const copy = locale === "zh-CN" ? { name: "名称", create: "创建私有智能体", receipt: "智能体已创建", steps: "最多步数", close: "关闭「Agent A」", manage: "管理智能体" }
      : { name: "Name", create: "Create private Agent", receipt: "Agent created", steps: "Maximum steps", close: "Close “Agent A”", manage: "Manage Agent" };
    const compact = width < 480;
    const objectName = (label: string) => compact
      ? page.getByRole("button", { name: new RegExp(`^${label} · `) })
      : pane.getByRole("tab", { name: label, exact: true });
    await page.goto("/?session=s_browser&agent_draft=sad_a");
    await expect(page.getByRole("textbox", { name: copy.name, exact: true })).toHaveValue("Agent A");
    const pane = page.locator(".configuration-canvas-pane");
    await expect(objectName("Agent A")).toBeVisible();
    await expect(pane).toHaveCSS("border-left-width", "0px");
    await expect(pane.locator(".configuration-canvas-pane-body")).toHaveCSS("padding-left", "0px");
    if (width === 1440) {
      const divider = page.locator(".v2-split-divider");
      await expect(divider.locator(".v2-split-divider-line")).toHaveCSS("background-color", "rgba(0, 0, 0, 0)");
      const previous = Number(await divider.getAttribute("aria-valuenow"));
      await divider.focus();
      await page.keyboard.press("ArrowRight");
      await expect.poll(async () => Number(await divider.getAttribute("aria-valuenow"))).toBeGreaterThan(previous);
      await pane.getByRole("tab", { name: "Agent A", exact: true }).focus();
    }
    await page.screenshot({ path: testInfo.outputPath("canvas-draft.png") });
    await page.getByRole("button", { name: copy.create, exact: true }).click();
    const receipt = page.getByRole("group", { name: `${copy.receipt} Agent A`, exact: true });
    await expect(receipt.getByText("Agent A", { exact: true })).toBeVisible();
    await expect(receipt.locator("dl")).toContainText(copy.steps);
    await expect(receipt.getByRole("button", { name: copy.manage, exact: true })).toBeVisible();
    expect(fixture.creations).toEqual(["sad_a"]);
    await expect(objectName("Agent A")).toBeVisible();
    expect(await receipt.evaluate((element) => element.scrollWidth <= element.clientWidth)).toBe(true);
    await page.screenshot({ path: testInfo.outputPath("canvas-created.png") });
    await page.reload();
    await expect(receipt.getByText("Agent A", { exact: true })).toBeVisible();
    await expect(objectName("Agent A")).toBeVisible();
    expect(fixture.creations).toEqual(["sad_a"]);
    if (locale === "en-US") {
      await page.emulateMedia({ colorScheme: "dark", reducedMotion: "reduce" });
      await page.reload();
      await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
      await expect(receipt.getByRole("button", { name: copy.manage, exact: true })).toBeVisible();
      await expect(objectName("Agent A")).toBeVisible();
      await page.screenshot({ path: testInfo.outputPath("canvas-created-dark.png") });
    }
    const close = pane.getByRole("button", { name: copy.close, exact: true });
    await close.focus();
    await page.keyboard.press("Enter");
    await expect(pane).toHaveCount(0);
    expect(fixture.creations).toEqual(["sad_a"]);
  });
}

test("created Agent management preserves another draft's failed-save input", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 });
  const fixture = await mockDrafts(page);
  await page.goto("/?session=s_browser&agent_draft=sad_a");
  await page.getByRole("button", { name: "创建私有智能体", exact: true }).click();
  await expect(page.getByTestId("agent-creation-receipt")).toContainText("智能体已创建");
  await openB(page);
  fixture.rejectWrites();
  const name = displayName(page);
  await name.fill("Keep this unfinished Agent");
  await expect.poll(() => fixture.writes.filter((write) => write.id === "sad_b").length).toBe(1);
  await expect(page.getByRole("alert").filter({ hasText: "保存失败" })).toBeVisible();
  await page.getByRole("tab", { name: "Agent A", exact: true }).click();
  await page.getByRole("button", { name: "管理智能体", exact: true }).click();
  // Leaving after a failed autosave keeps the draft open until its saved state is checked.
  await expect(page.getByRole("button", { name: "核对已保存草稿", exact: true })).toBeVisible();
  await expect(page.getByRole("tab", { name: /^Agent B/ })).toHaveAttribute("aria-selected", "true");
  await expect(name).toHaveValue("Keep this unfinished Agent");
  expect(new URL(page.url()).pathname).toBe("/");
  expect(fixture.writes.filter((write) => write.id === "sad_b").length).toBeGreaterThanOrEqual(1);
  expect(fixture.creations).toEqual(["sad_a"]);
});
