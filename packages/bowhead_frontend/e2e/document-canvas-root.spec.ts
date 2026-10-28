import { expect, test, type Page } from "@playwright/test";
import { mobilePaneTab } from "./_mobile-pane";
import { canvasWeek, chooseCanvasWeek, expectCanvasWeek } from "./_canvas-week";
import { createHash } from "node:crypto";
import { readFile } from "node:fs/promises";
import { mockAuthedApp } from "./_app-mocks";
import fixture from "../__tests__/fixtures/document-draft-canvas.json";
import messages from "../edition/shared-messages/zh-CN.json";
import englishMessages from "../edition/shared-messages/en-US.json";

const png = Buffer.from(
  "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/l1cAAAAASUVORK5CYII=",
  "base64",
);
const t = messages.documents;

/** A finished turn collapses its run into one summary row; failed step reasons sit behind “Expand trace”. */
async function expandTrace(page: Page, label: string) {
  await page.getByRole("button", { name: label, exact: true }).click();
}

for (const locale of ["zh-CN", "en-US"] as const) test.describe(`document failure notices in ${locale}`, () => {
  test.use({ locale });
  const errors = (locale === "zh-CN" ? messages : englishMessages).error_messages.reasons;
  for (const reason of ["document_information_incomplete", "document_planning_failed"] as const) {
  test(`document failure keeps its explanation after refresh (${reason})`, async ({ page }, testInfo) => {
    const data = await mockDocuments(page);
    await page.route("**/api/v1/sessions/s_browser", (route) => route.fulfill({ json: {
      session: { id: "s_browser", workspace_id: "ws_smoke", title: "Document work", status: "completed" },
      turns: [{ id: "t_doc", seq: 1, user_text: "Update my presentation", status: "failed", error_kind: reason,
        final_answer: "The document task stopped without publishing any files.", attachments: [] }],
      chat_execution: { write_protocol: "legacy_v0", config_generation: "test", readiness_generation: "test" },
    } }));
    // A missing event tail must preserve the original durable domain result.
    await page.route("**/api/v1/turns/t_doc/events", (route) => route.fulfill({ json: { events: [] } }));
    await page.goto("/?session=s_browser");
    // The final failure is a ChatCard (intent="failure"); it carries the domain explanation in its body.
    const explanation = page.getByTestId("final-fail").filter({ hasText: errors[reason] });
    await expect(explanation).toBeVisible();
    await page.reload();
    await expect(explanation).toBeVisible();
    await expect(page.getByText(errors.lead_internal_error, { exact: true })).toBeHidden();
    expect(data.posts).toEqual([]);
    await page.screenshot({ path: `/tmp/bowhead-composer-${reason}-${locale}-${testInfo.project.name}.png` });
  });
  }
});

for (const locale of ["zh-CN", "en-US"] as const) test.describe(`historical step feedback in ${locale}`, () => {
  test.use({ locale });
  const catalog = locale === "zh-CN" ? messages : englishMessages;
  for (const width of [412, 1440]) {
    test(`a stopped step agrees with the failed Canvas after reload at ${width}`, async ({ page }, testInfo) => {
      await page.setViewportSize({ width, height: 915 });
      const data = await mockDocuments(page);
      const reason = "Synthetic document handoff did not complete.";
      const failed = { ...fixture.review, id: "djob_a", state: "failed", can_confirm: false,
        can_revise: false, draft: undefined, draft_digest: null, error_code: "document_execution_failed",
        usage: { ...fixture.review.usage, settled_credits: 0 },
      };
      await page.route("**/api/v1/sessions/s_browser/documents/djob_a", (route) => route.fulfill({ json: failed }));
      await page.route("**/api/v1/sessions/s_browser", (route) => route.fulfill({ json: {
        session: { id: "s_browser", workspace_id: "ws_smoke", title: "Stopped document work", status: "completed" },
        turns: [{ id: "t_doc", seq: 1, user_text: "Update my presentation", status: "failed",
          error_kind: "document_planning_failed", final_answer: "The document task stopped.", attachments: [] }],
        chat_execution: { write_protocol: "legacy_v0", config_generation: "test", readiness_generation: "test" },
      } }));
      const events = [
        { event_type: "step_list_card_created", payload: { card_id: "t_doc" } },
        { event_type: "step_started", payload: { turn_id: "t_doc", card_id: "t_doc", step_id: "doc_step",
          step_type: "tool_run", tool_name: "template_document_submit", header_zh: "Generate presentation" } },
        { event_type: "step_done", payload: { turn_id: "t_doc", card_id: "t_doc", step_id: "doc_step",
          state: "failure", error: { is_transient: true, message_zh: reason } } },
        { event_type: "step_list_card_finalized", payload: { card_id: "t_doc", state: "failed" } },
        { event_type: "done", payload: { turn_id: "t_doc", status: "failed", error_kind: "document_planning_failed" } },
      ].map((event, index) => ({ ...event, payload: { ...event.payload, seq: index + 1 }, seq: index + 1, occurred_at: "2026-10-05T00:00:00Z" }));
      await page.route("**/api/v1/turns/t_doc/events", (route) => route.fulfill({ json: { events } }));
      await page.goto("/?session=s_browser&canvas_document=djob_a&canvas_doc_session=s_browser&canvas_doc_user=u_smoke&canvas_doc_team=team_smoke&canvas_doc_space=ws_smoke");
      await expect(page.getByText(catalog.documents.canvas_failure_generic, { exact: true })).toBeVisible();
      await page.getByRole("button", { name: catalog.documents.canvas_return_to_chat, exact: true }).click();
      await expandTrace(page, catalog.step_list.expand_trace);
      await expect(page.getByText(reason, { exact: true })).toBeVisible();
      await expect(page.getByText(catalog.step_list.failure_running_hint, { exact: true })).toBeHidden();
      await page.reload();
      if (width < 560) {
        await page.getByRole("tab", { name: catalog.canvas.tab_chat, exact: true }).click();
      }
      await expandTrace(page, catalog.step_list.expand_trace);
      await expect(page.getByText(reason, { exact: true })).toBeVisible();
      await expect(page.getByText(catalog.step_list.failure_running_hint, { exact: true })).toBeHidden();
      expect(data.posts).toEqual([]);
      expect(data.commands).toEqual([]);
      await page.screenshot({ path: `/tmp/bowhead-step-feedback-${locale}-${width}-${testInfo.project.name}.png` });
    });
  }
});

for (const width of [412, 1440]) test(`live step feedback preserves the reason after completion at ${width}`, async ({ page }) => {
  await page.setViewportSize({ width, height: 915 });
  const data = await mockDocuments(page);
  const reason = "Synthetic live step did not complete.";
  let release!: () => void;
  const gate = new Promise<void>((resolve) => { release = resolve; });
  const events = [
    { event_type: "step_list_card_created", payload: { card_id: "t_plain" } },
    { event_type: "step_started", payload: { card_id: "t_plain", step_id: "failed_live",
      step_type: "tool_run", tool_name: "template_document_submit", header_zh: "Generate presentation" } },
    { event_type: "step_done", payload: { card_id: "t_plain", step_id: "failed_live", state: "failure",
      error: { is_transient: true, message_zh: reason } } },
    { event_type: "step_list_card_finalized", payload: { card_id: "t_plain", state: "failed" } },
    { event_type: "done", payload: { turn_id: "t_plain", status: "failed", error_kind: "document_planning_failed" } },
  ];
  await page.route("**/api/v1/chat/stream/t_plain*", async (route) => {
    await gate;
    return route.fulfill({ contentType: "text/event-stream", body: events.map((event, index) =>
      `event: ${event.event_type}\ndata: ${JSON.stringify({ ...event.payload, seq: index + 1 })}\n\n`).join("") });
  });
  try {
    await page.goto("/?session=s_browser");
    const input = page.getByRole("textbox", { name: "问点什么…", exact: true });
    await input.fill("Run the synthetic failed document step");
    await input.press("Enter");
    await expect(page.getByRole("button", { name: "停止", exact: true })).toBeVisible();
    release();
    await expect(page.getByRole("button", { name: "停止", exact: true })).toBeHidden();
    await expandTrace(page, messages.step_list.expand_trace);
    await expect(page.getByText(reason, { exact: true })).toBeVisible();
    await expect(page.getByText(messages.step_list.failure_running_hint, { exact: true })).toBeHidden();
    await expect(page.getByRole("button", { name: "停止", exact: true })).toBeHidden();
    expect(data.posts).toHaveLength(1);
    expect(data.commands).toEqual([]);
  } finally { release(); }
});

async function mockDocuments(page: Page) {
  await mockAuthedApp(page);
  await page.addInitScript(() => {
    document.addEventListener("DOMContentLoaded", () => {
      const style = document.createElement("style");
      style.textContent = "nextjs-portal{display:none!important}";
      document.head.appendChild(style);
    });
  });
  await page.route("**/api/v1/auth/me", (route) =>
    route.fulfill({
      json: {
        id: "u_smoke",
        email: "smoke@example.com",
        name: "Smoke User",
        team_id: "team_smoke",
        tier: "pro",
        auth_session_generation: "auth_doc_browser",
        capabilities: { configuration_canvas: true, settings_pages_canvas: true, subagent_authoring: { authoring: true } },
        chat_execution: { write_protocol: "legacy_v0", config_generation: "test", readiness_generation: "test" },
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
  await page.route("**/api/v1/sessions/s_browser", (route) =>
    route.fulfill({
      json: {
        session: { id: "s_browser", workspace_id: "ws_smoke", title: "Document work", status: "completed" },
        turns: [
          {
            id: "t_doc",
            seq: 1,
            user_text: "Update my presentation",
            status: "completed",
            final_answer: "The private draft is ready",
            attachments: [],
          },
        ],
        chat_execution: { write_protocol: "legacy_v0", config_generation: "test", readiness_generation: "test" },
      },
    }),
  );
  await page.route("**/api/v1/results/capabilities", (route) =>
    route.fulfill({
      json: { dto_version: 1, workspace: true, complete_list: false, scale_nav: false, max_page_size: 50 },
    }),
  );
  await page.route("**/api/v1/chat/sessions/*/reference-queue?*", (route) => route.fulfill({ json: { items: [] } }));
  const a = {
    ...structuredClone(fixture.review),
    id: "djob_a",
    can_revise: false,
    draft: { ...structuredClone(fixture.review.draft), output_stem: "Quarterly A" },
  };
  const b = {
    ...structuredClone(fixture.review),
    id: "djob_b",
    can_revise: false,
    draft: { ...structuredClone(fixture.review.draft), output_stem: "Quarterly B" },
  };
  const fileBytes = {
    art_canvas_source: Buffer.from("original source fixture"),
    art_canvas_pdf: Buffer.from("corresponding PDF fixture"),
  };
  a.draft.files["candidate.pptx"].sha256 = createHash("sha256").update(fileBytes.art_canvas_source).digest("hex");
  a.draft.files["candidate.pdf"].sha256 = createHash("sha256").update(fileBytes.art_canvas_pdf).digest("hex");
  const downloads: string[] = [];
  await page.context().route("**/api/v1/files/art_canvas_*", (route) => {
    const id = new URL(route.request().url()).pathname.split("/").at(-1)! as keyof typeof fileBytes;
    downloads.push(id);
    const extension = id === "art_canvas_pdf" ? "pdf" : "pptx";
    return route.fulfill({
      contentType: "application/octet-stream",
      headers: { "Content-Disposition": `attachment; filename="Quarterly A.${extension}"` },
      body: fileBytes[id],
    });
  });
  let missingRevisionEcho = false;
  let published = false;
  let denied = false;
  let loseConfirmation = false;
  let hold = false;
  let release!: () => void;
  const pending = new Promise<void>((resolve) => {
    release = resolve;
  });
  const commands: unknown[] = [];
  const posts: Record<string, unknown>[] = [];
  const scopes: Record<string, string>[] = [];
  const state = () => (published ? { ...a, state: "published", can_revise: false } : a);
  await page.route("**/api/v1/sessions/s_browser/documents", (route) => {
    scopes.push(route.request().headers());
    return denied
      ? route.fulfill({ status: 403, json: { detail: "Unavailable" } })
      : route.fulfill({ json: { jobs: [state(), b], next_cursor: null } });
  });
  await page.route("**/api/v1/sessions/s_browser/documents/*", (route) => {
    scopes.push(route.request().headers());
    return denied
      ? route.fulfill({ status: 403, json: { detail: "Unavailable" } })
      : route.fulfill({ json: route.request().url().endsWith("djob_b") ? b : state() });
  });
  await page.route("**/api/v1/documents/*/preview/page-1.png", (route) =>
    route.fulfill({ contentType: "image/png", body: png }),
  );
  await page.route("**/api/v1/documents/djob_a/confirm", async (route) => {
    commands.push(route.request().postDataJSON());
    if (hold) await pending;
    if (loseConfirmation) {
      loseConfirmation = false;
      return route.abort("failed");
    }
    published = true;
    await route.fulfill({ json: state() });
  });
  const artifacts = [
    {
      id: "art_canvas_source",
      filename: "Quarterly A.pptx",
      mime: "application/vnd.openxmlformats-officedocument.presentationml.presentation",
      size: fileBytes.art_canvas_source.length,
      source: "document",
      kind: "deliverable",
      created_by_turn: "t_doc",
      created_at: "2026-10-03T12:00:00Z",
      preview_available: false,
    },
    {
      id: "art_canvas_pdf",
      filename: "Quarterly A.pdf",
      mime: "application/pdf",
      size: fileBytes.art_canvas_pdf.length,
      source: "document",
      kind: "deliverable",
      created_by_turn: "t_doc",
      derived_from: "art_canvas_source",
      created_at: "2026-10-03T12:00:00Z",
      preview_available: true,
    },
  ];
  await page.route("**/api/v1/files/art_canvas_*/representations/*", (route) => {
    const path = new URL(route.request().url()).pathname.split("/");
    const item = artifacts.find((artifact) => artifact.id === path.at(-3))!;
    return path.at(-1) === "details"
      ? route.fulfill({ json: { representation: "details", revision: "published-1", details: item } })
      : route.fulfill({ status: 422, json: { detail: { error: { code: "artifact_representation_unsupported" } } } });
  });
  let artifactReads = 0;
  await page.route("**/api/v1/sessions/s_browser/artifacts*", (route) => {
    artifactReads += 1;
    return route.fulfill({ json: { items: published ? artifacts : [] } });
  });
  await page.route("**/api/v1/workspace-settings/ws_smoke/metadata", (route) =>
    route.fulfill({ json: { name: "Original Space", description: "", revision: 1 } }),
  );
  await page.route("**/api/v1/workspace-settings/ws_smoke", (route) =>
    route.fulfill({ json: { settings: { week_start_day: "monday" } } }),
  );
  await page.route("**/api/v1/chat/send", (route) => {
    posts.push(route.request().postDataJSON());
    return route.fulfill({
      json: {
        status: "started",
        ...(!missingRevisionEcho && route.request().postDataJSON().document_revision_target
          ? { document_revision_target: route.request().postDataJSON().document_revision_target }
          : {}),
        session_id: "s_browser",
        turn_id: "t_plain",
        execution_generation: 1,
        stream_url: "/api/v1/chat/stream/t_plain",
      },
    });
  });
  await page.route("**/api/v1/chat/stream/t_plain*", (route) =>
    route.fulfill({
      contentType: "text/event-stream",
      body: 'event: done\ndata: {"seq":1,"turn_id":"t_plain","status":"completed"}\n\n',
    }),
  );
  return {
    expandReview: () => {
      a.draft.manifest.changes = Array.from({ length: 5 }, (_, index) => ({
        ...structuredClone(fixture.review.draft.manifest.changes[0]),
        before: `Original quarter content ${index + 1}`,
        after: `Reviewed quarter content ${index + 1}`,
      }));
    },
    publish: () => { published = true; },
    sameTitles: () => { a.draft.output_stem = "Quarterly"; b.draft.output_stem = "Quarterly"; },
    nativeTurn: (turnId: string) => { a.origin_turn_id = turnId; },
    newDraft: () => { a.id = "djob_new"; a.origin_turn_id = "t_new"; a.draft_digest = "b".repeat(64); },
    revisable: () => {
      a.can_revise = true;
      b.can_revise = true;
    },
    missingRevisionEcho: (value: boolean) => {
      missingRevisionEcho = value;
    },
    commands,
    posts,
    scopes,
    fileBytes,
    downloads,
    artifactReads: () => artifactReads,
    hold: () => {
      hold = true;
    },
    release,
    readOnly: () => {
      a.can_confirm = false;
    },
    loseConfirmation: () => {
      loseConfirmation = true;
    },
    deny: () => {
      denied = true;
    },
  };
}
const openLabel = messages.settings_canvas.open_menu;
/** Below 480 px of canvas width the C1 tab strip becomes a title switcher. */
const canvasSwitcher = (page: Page) => page.getByRole("button", { name: / · 画布里的对象$/ });
/** Tab names carry a status word (“Quarterly A · 待你审阅”); tabs stay in the DOM at every width. */
const documentTab = (page: Page, name: string) => page.locator(`[role="tab"][aria-label^="${name}"]`);
/** Document names keep the source extension (“Quarterly A.pptx”). */
const closeDocument = (page: Page, name: string) => page.getByRole("button", { name: `关闭「${name.includes(".pptx") ? name : `${name}.pptx`}」`, exact: true });

/** Document drafts open from the C1 “+” (Open…) menu, or its submenu in the narrow title switcher. */
async function openDocumentMenu(page: Page) {
  const opener = page.getByRole("button", { name: openLabel, exact: true });
  await expect(page.getByRole("button", { name: new RegExp(`^${openLabel}$| · 画布里的对象$`), includeHidden: true }).first()).toBeAttached();
  if (!(await opener.filter({ visible: true }).count()) && !(await canvasSwitcher(page).isVisible())) {
    const reopen = page.getByRole("button", { name: "重开成果", exact: true });
    if (await reopen.isVisible()) await reopen.click();
    else await page.getByRole("tab", { name: "成果", exact: true }).click();
  }
  if (await canvasSwitcher(page).isVisible()) {
    await canvasSwitcher(page).click();
    await page.getByRole("menuitem", { name: openLabel, exact: true }).click();
  } else await opener.filter({ visible: true }).first().click();
}

/** The pinned Results tab (C1) replaces “Back to results”. */
async function showCanvasResults(page: Page) {
  const tab = page.getByRole("tab", { name: "成果", exact: true });
  if (await canvasSwitcher(page).isVisible()) {
    await canvasSwitcher(page).click();
    await page.getByRole("menuitemradio", { name: "成果", exact: true }).click();
  } else await tab.click();
}
async function openDocument(page: Page, name: string) {
  await openDocumentMenu(page);
  const entry = page.getByRole("menuitem", { name: new RegExp(name) });
  await expect(entry).toBeVisible();
  await entry.click();
  await expect(
    page.getByRole("img", { name: t.page.replace("{page}", "1").replace("{total}", "1"), exact: true }),
  ).toBeVisible();
}

for (const width of [412, 1440]) {
  test(`document discovery, exact history and close/reopen preserve input at ${width}`, async ({ page }) => {
    await page.setViewportSize({ width, height: 1000 });
    const data = await mockDocuments(page);
    await page.goto("/?session=s_browser");
    const input = page.getByRole("textbox", { name: "问点什么…", exact: true });
    await input.fill("Keep this unsent text");
    if (width < 1024) await mobilePaneTab(page).click();
    await openDocument(page, "Quarterly A");
    await expect(page).toHaveURL(/canvas_document=djob_a/);
    await page.screenshot({ path: `/tmp/bowhead-document-canvas-${width}.png` });
    await expect(page.getByRole("button", { name: "新建智能体", exact: true })).toHaveCount(0);
    await openDocument(page, "Quarterly B");
    await expect(page).toHaveURL(/canvas_document=djob_b/);
    await page.goBack();
    await expect(page).toHaveURL(/canvas_document=djob_a/);
    await expect(documentTab(page, "Quarterly A")).toHaveAttribute("aria-selected", "true");
    await page.reload();
    await expect(
      page.getByRole("img", { name: t.page.replace("{page}", "1").replace("{total}", "1"), exact: true }),
    ).toBeVisible();
    const close = closeDocument(page, "Quarterly A");
    await close.focus();
    await close.press("Enter");
    await expect(page).not.toHaveURL(/canvas_document=/);
    await expect(page.locator(".v2-split-canvas")).toBeHidden();
    await expect(width < 1024 ? page.getByRole("tab", { name: "成果", exact: true })
      : page.getByRole("button", { name: "重开成果", exact: true })).toBeFocused();
    await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    await openDocument(page, "Quarterly A");
    await showCanvasResults(page);
    await expect(page).toHaveURL(/canvas_doc_view=results/);
    await page.reload();
    if (width < 1024) await mobilePaneTab(page).click();
    await expect(
      page.getByRole("img", { name: t.page.replace("{page}", "1").replace("{total}", "1"), exact: true }),
    ).not.toBeVisible();
    await openDocument(page, "Quarterly A");
    expect(data.commands).toEqual([]);
    expect(
      data.scopes.every((headers) => headers["x-team-id"] === "team_smoke" && headers["x-space-id"] === "ws_smoke"),
    ).toBe(true);
    if (width < 1024) await page.getByRole("tab", { name: "对话", exact: true }).click();
    await expect(input).toHaveValue("Keep this unsent text");
  });
}

test("a document send excludes a retained settings target and background publication does not change selection", async ({
  page,
  browserName,
}) => {
  await page.setViewportSize({ width: 1440, height: 1000 });
  const data = await mockDocuments(page);
  await page.goto(
    "/?session=s_browser&canvas_settings=workspace_i18n&canvas_user=u_smoke&canvas_team=team_smoke&canvas_space=ws_smoke",
  );
  await expectCanvasWeek(page, "monday");
  await openDocument(page, "Quarterly A");
  const input = page.getByRole("textbox", { name: "问点什么…", exact: true });
  await input.fill("Explain the review process");
  await input.press("Enter");
  await expect.poll(() => data.posts.length).toBe(1);
  expect(data.posts[0].client_info).not.toHaveProperty("configuration_settings_target");
  expect(data.posts[0].client_info).not.toHaveProperty("agent_draft_id");
  await page.getByRole("checkbox").check();
  data.hold();
  await page.getByRole("button", { name: t.confirm, exact: true }).click();
  await expect.poll(() => data.commands.length).toBe(1);
  await page.getByRole("tab", { name: "空间偏好", exact: true }).click();
  await input.fill("Do not replace this");
  await input.focus();
  const reads = data.artifactReads();
  data.release();
  await expect.poll(data.artifactReads).toBeGreaterThan(reads);
  await expectCanvasWeek(page, "monday");
  await expect(input).toHaveValue("Do not replace this");
  await expect(input).toBeFocused();
  await showCanvasResults(page);
  await page.getByRole("button", { name: "成果", exact: true }).click();
  const navigator = page.getByRole("dialog", { name: "成果导航", exact: true });
  await expect(navigator.getByText("Quarterly A.pptx", { exact: true })).toBeVisible();
  await expect(navigator.getByText("Quarterly A.pdf", { exact: true })).toBeVisible();
  for (const [id, extension] of [
    ["art_canvas_source", "pptx"],
    ["art_canvas_pdf", "pdf"],
  ] as const) {
    if (extension === "pdf") await page.getByRole("button", { name: "成果", exact: true }).click();
    await page
      .getByRole("dialog", { name: "成果导航", exact: true })
      .getByText(`Quarterly A.${extension}`, { exact: true })
      .click();
    await expect(page).toHaveURL(new RegExp(`result=${id}`));
    await page.getByRole("tab", { name: "详情", exact: true }).click();
    const download = page.getByRole("link", { name: "下载", exact: true }).first();
    await expect(download).toHaveAttribute("href", `/api/v1/files/${id}`);
    // This WebKit routed target=_blank fixture exposes no Playwright download event.
    // Verify the original file request there; Chromium additionally verifies browser-saved bytes.
    const saved = browserName === "chromium" ? page.waitForEvent("download") : null;
    await download.click();
    await expect.poll(() => data.downloads).toContain(id);
    if (saved) {
      const file = await saved;
      expect(file.suggestedFilename()).toBe(`Quarterly A.${extension}`);
      expect(await readFile((await file.path())!)).toEqual(data.fileBytes[id]);
    }
  }
});

test("unknown confirmation survives close/reopen and revoked access clears the original preview", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 });
  const data = await mockDocuments(page);
  await page.goto("/?session=s_browser");
  await openDocument(page, "Quarterly A");
  await page.getByRole("checkbox").check();
  data.loseConfirmation();
  await page.getByRole("button", { name: t.confirm, exact: true }).click();
  await expect(page.getByRole("button", { name: t.canvas_retry_confirmation, exact: true })).toBeVisible();
  await closeDocument(page, "Quarterly A").click();
  await openDocument(page, "Quarterly A");
  await page.getByRole("checkbox").check();
  await page.getByRole("button", { name: t.confirm, exact: true }).click();
  await expect(page.getByText(t.state_published, { exact: true }).first()).toBeVisible();
  expect(data.commands).toHaveLength(2);
  expect(data.commands[0]).toEqual(data.commands[1]);
  await openDocumentMenu(page);
  const refresh = page.getByRole("menuitem", { name: t.canvas_refresh_list, exact: true });
  await expect(refresh).toBeEnabled();
  data.deny();
  await refresh.click();
  await expect(
    page.getByRole("img", { name: t.page.replace("{page}", "1").replace("{total}", "1"), exact: true }),
  ).toHaveCount(0);
  await expect(documentTab(page, "Quarterly A")).toHaveCount(0);
  expect(data.commands).toHaveLength(2);
});

test("an authorized read-only document stays discoverable without allowing confirmation", async ({ page }) => {
  const data = await mockDocuments(page);
  data.readOnly();
  await page.route("**/api/v1/auth/me/workspaces", (route) =>
    route.fulfill({
      json: {
        items: [{ id: "ws_smoke", workspace_id: "ws_smoke", name: "Original Space", role: "member" }],
        active_id: "ws_smoke",
      },
    }),
  );
  await page.goto("/?session=s_browser");
  await openDocument(page, "Quarterly A");
  await expect(page.getByText(t.canvas_read_only, { exact: true })).toBeVisible();
  // A read-only footer states why; it offers neither the acknowledgement nor publication (C9).
  await expect(page.getByRole("checkbox")).toHaveCount(0);
  await expect(page.getByRole("button", { name: t.confirm, exact: true })).toHaveCount(0);
  expect(data.commands).toEqual([]);
});

async function localRevisionState(page: Page) {
  return page.evaluate(async () => {
    const db = await new Promise<IDBDatabase>((resolve, reject) => {
      const request = indexedDB.open("bowhead-composer-recovery-v1");
      request.onsuccess = () => resolve(request.result);
      request.onerror = () => reject(request.error);
    });
    try {
      return await Promise.all(
        ["ordinary_drafts", "manifests"].map(
          (name) =>
            new Promise<unknown[]>((resolve, reject) => {
              const request = db.transaction(name).objectStore(name).getAll();
              request.onsuccess = () => resolve(request.result);
              request.onerror = () => reject(request.error);
            }),
        ),
      );
    } finally {
      db.close();
    }
  });
}

for (const width of [412, 1440])
  test(`explicit document revision keeps the original target after switching views at ${width}`, async ({ page }) => {
    await page.setViewportSize({ width, height: 1000 });
    const data = await mockDocuments(page);
    data.revisable();
    await page.goto(
      "/?session=s_browser&canvas_settings=workspace_i18n&canvas_user=u_smoke&canvas_team=team_smoke&canvas_space=ws_smoke",
    );
    await expectCanvasWeek(page, "monday");
    await openDocument(page, "Quarterly A");
    await page.getByRole("button", { name: t.canvas_revision_action, exact: true }).click();
    const input = page.getByRole("textbox", { name: "问点什么…", exact: true });
    await expect(page.getByTestId("composer-document-revision")).toBeVisible();
    await expect(input).toBeFocused();
    expect(data.posts).toEqual([]);
    expect(data.commands).toEqual([]);
    await input.fill("Shorten the original A title");
  await page.screenshot({ path: `/tmp/bowhead-document-revision-${width}.png` });
    await expect
      .poll(async () => JSON.stringify(await localRevisionState(page)))
      .toContain("Shorten the original A title");
    await page.reload();
    if (width < 1024) await page.getByRole("tab", { name: "对话", exact: true }).click();
    await expect(input).toHaveValue("Shorten the original A title");
    await expect(page.getByTestId("composer-document-revision")).toBeVisible();
    if (width < 1024)
      await mobilePaneTab(page)
        .click();
    if (width < 480) {
      await canvasSwitcher(page).click();
      await page.getByRole("menuitemradio", { name: /^空间偏好/ }).click();
    } else await page.getByRole("tab", { name: "空间偏好", exact: true }).click();
    await expectCanvasWeek(page, "monday");
    if (width < 1024) await page.getByRole("tab", { name: "对话", exact: true }).click();
    await input.press("Enter");
    await expect.poll(() => data.posts.length).toBe(1);
    expect(data.posts[0]).toMatchObject({
      message: "Shorten the original A title",
      submission_kind: "start",
      submission_contract_version: "chat-submit.v1",
      document_revision_target: {
        kind: "document_revision",
        version: 1,
        session_id: "s_browser",
        job_id: "djob_a",
        draft_digest: fixture.review.draft_digest,
      },
    });
    expect(data.posts[0].client_submission_id).toBeTruthy();
    expect(data.posts[0].client_info).not.toHaveProperty("configuration_settings_target");
    await expect.poll(() => localRevisionState(page)).toEqual([[], []]);
    await expect(page.getByTestId("composer-document-revision")).toHaveCount(0);
    expect(data.commands).toEqual([]);
  });

test("revision staging protects existing input and close/reopen never redirects a staged target", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 });
  const data = await mockDocuments(page);
  data.revisable();
  await page.goto("/?session=s_browser");
  const input = page.getByRole("textbox", { name: "问点什么…", exact: true });
  await expect(input).toBeEditable();
  await input.fill("Keep my unsent question");
  await openDocument(page, "Quarterly A");
  await page.getByRole("button", { name: t.canvas_revision_action, exact: true }).click();
  await expect(page.getByText(t.canvas_revision_draft_not_empty, { exact: true })).toBeVisible();
  await expect(input).toHaveValue("Keep my unsent question");
  await expect(page.getByTestId("composer-document-revision")).toHaveCount(0);
  await input.fill("");
  await page.getByRole("button", { name: t.canvas_revision_action, exact: true }).click();
  await expect(page.getByTestId("composer-document-revision")).toBeVisible();
  await input.fill("Change A only");
  await closeDocument(page, "Quarterly A").click();
  await openDocument(page, "Quarterly B");
  await page.getByRole("button", { name: t.canvas_revision_action, exact: true }).click();
  await expect(page.getByText(t.canvas_revision_draft_not_empty, { exact: true })).toBeVisible();
  await input.press("Enter");
  await expect.poll(() => data.posts.length).toBe(1);
  expect(data.posts[0].document_revision_target).toMatchObject({ job_id: "djob_a" });
  await expect.poll(() => localRevisionState(page)).toEqual([[], []]);
});

test("a missing revision receipt retains the exact pending send through reload and recovers only that target", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1440, height: 1000 });
  const data = await mockDocuments(page);
  data.revisable();
  data.missingRevisionEcho(true);
  await page.goto("/?session=s_browser");
  await openDocument(page, "Quarterly A");
  await page.getByRole("button", { name: t.canvas_revision_action, exact: true }).click();
  const input = page.getByRole("textbox", { name: "问点什么…", exact: true });
  await expect(page.getByTestId("composer-document-revision")).toBeVisible();
  await input.fill("Recover A exactly");
  await input.press("Enter");
  await expect.poll(() => data.posts.length).toBe(1);
  await expect(page.getByRole("button", { name: messages.composer.submission_retry, exact: true })).toBeVisible();
  await page.reload();
  await expect(page.getByRole("button", { name: messages.composer.submission_retry, exact: true })).toBeVisible();
  await expect(page.getByTestId("composer-document-revision")).toBeVisible();
  data.missingRevisionEcho(false);
  await page.getByRole("button", { name: messages.composer.submission_retry, exact: true }).click();
  await expect.poll(() => data.posts.length).toBe(2);
  expect(data.posts[1]).toEqual(data.posts[0]);
  await expect.poll(() => localRevisionState(page)).toEqual([[], []]);
  expect(data.commands).toEqual([]);
});

for (const echo of ["match", "wrong"] as const)
  test(`accepted abandonment requires the original document target: ${echo}`, async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 1000 });
    const data = await mockDocuments(page);
    data.revisable();
    data.missingRevisionEcho(true);
    await page.goto("/?session=s_browser");
    await openDocument(page, "Quarterly A");
    await page.getByRole("button", { name: t.canvas_revision_action, exact: true }).click();
    const input = page.getByRole("textbox", { name: "问点什么…", exact: true });
    await expect(page.getByTestId("composer-document-revision")).toBeVisible();
    await input.fill("Reconcile A before another request");
    await input.press("Enter");
    await expect.poll(() => data.posts.length).toBe(1);
    await expect(page.getByRole("button", { name: messages.composer.submission_abandon, exact: true })).toBeVisible();
    const original = await localRevisionState(page);
    await page.route("**/api/v1/chat/submissions/*/abandon", (route) =>
      route.fulfill({
        json: {
          client_submission_id: data.posts[0].client_submission_id,
          outcome: "started",
          response: {
            session_id: "s_browser",
            turn_id: "t_plain",
            document_revision_target: {
              ...(data.posts[0].document_revision_target as object),
              job_id: echo === "match" ? "djob_a" : "djob_other",
            },
          },
        },
      }),
    );
    await page.getByRole("button", { name: messages.composer.submission_abandon, exact: true }).click();
    if (echo === "match") {
      await expect.poll(() => localRevisionState(page)).toEqual([[], []]);
      await expect(page.getByRole("button", { name: messages.composer.submission_retry, exact: true })).toHaveCount(0);
    } else {
      await expect(page.getByText(messages.composer.submission_recovery_failed, { exact: true })).toBeVisible();
      expect(await localRevisionState(page)).toEqual(original);
    }
    expect(data.posts).toHaveLength(1);
    expect(data.commands).toEqual([]);
  });

test("closing during revision authorization leaves a reopened view and input untouched", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 });
  const data = await mockDocuments(page);
  data.revisable();
  await page.goto("/?session=s_browser");
  await openDocument(page, "Quarterly A");
  let release!: () => void;
  const held = new Promise<void>((resolve) => {
    release = resolve;
  });
  let requested = false;
  await page.route("**/api/v1/sessions/s_browser/documents/djob_a", async (route) => {
    if (requested) return route.fallback();
    requested = true;
    await held;
    await route.fulfill({ json: { ...fixture.review, id: "djob_a", can_revise: true } }).catch(() => undefined);
  });
  await page.getByRole("button", { name: t.canvas_revision_action, exact: true }).click();
  await expect.poll(() => requested).toBe(true);
  await closeDocument(page, "Quarterly A").click();
  await openDocument(page, "Quarterly A");
  const input = page.getByRole("textbox", { name: "问点什么…", exact: true });
  await input.fill("Keep the replacement view input");
  await input.focus();
  release();
  await expect(input).toHaveValue("Keep the replacement view input");
  await expect(input).toBeFocused();
  await expect(page.getByTestId("composer-document-revision")).toHaveCount(0);
  expect(data.posts).toEqual([]);
  expect(data.commands).toEqual([]);
});

const draftOpen = { version: 1, turn_id: "t_doc", session_id: "s_browser", user_id: "u_smoke",
  team_id: "team_smoke", workspace_id: "ws_smoke", job_id: "djob_a", draft_digest: fixture.review.draft_digest };
async function replayDocumentOpen(page: Page, pointer = draftOpen) {
  await page.route("**/api/v1/turns/t_doc/events", (route) => route.fulfill({ json: { events: [1, 2].map((seq) => ({
    seq, occurred_at: "2026-10-04T00:00:00Z", event_type: "document.draft_open", payload: pointer,
  })) } }));
}

for (const width of [412, 1440]) test(`native document opening replays without writes or reopening closed views at ${width}`, async ({ page }) => {
  await page.setViewportSize({ width, height: 1000 });
  const data = await mockDocuments(page);
  await replayDocumentOpen(page);
  await page.goto("/?session=s_browser");
  await expect(page).toHaveURL(/canvas_document=djob_a/);
  await expect(documentTab(page, "Quarterly A")).toHaveAttribute("aria-selected", "true");
  await expect(page.getByRole("img", { name: t.page.replace("{page}", "1").replace("{total}", "1"), exact: true })).toBeVisible();
  expect(data.commands).toHaveLength(0);
  const close = closeDocument(page, "Quarterly A");
  await close.focus();
  await close.press("Enter");
  await expect(page.locator(".v2-split-canvas")).toBeHidden();
  await expect(page.getByRole("textbox", { name: "问点什么…", exact: true })).toBeVisible();
  if (width < 1024) await expect(page.getByRole("tab", { name: "对话", exact: true })).toHaveAttribute("aria-selected", "true");
  await expect(width < 1024 ? page.getByRole("tab", { name: "成果", exact: true })
    : page.getByRole("button", { name: "重开成果", exact: true })).toBeFocused();
  const replay = page.waitForResponse((response) => response.url().includes("/turns/t_doc/events"));
  await page.reload();
  await replay;
  await expect(page.getByRole("textbox", { name: "问点什么…", exact: true })).toBeVisible();
  if (width < 1024) await mobilePaneTab(page).click();
  else await page.getByRole("button", { name: "重开成果", exact: true }).click();
  await expect(page.getByRole("region", { name: messages.results_workspace.choose_result_title, exact: true })).toBeVisible();
  await expect(documentTab(page, "Quarterly A")).toHaveCount(0);
  await openDocumentMenu(page);
  await page.keyboard.press("Escape");
  await expect(documentTab(page, "Quarterly A")).toHaveCount(0);
  await openDocument(page, "Quarterly A");
  await page.reload();
  await expect(documentTab(page, "Quarterly A")).toHaveCount(1);
  expect(data.commands).toHaveLength(0);
  expect(data.posts).toHaveLength(0);
});

for (const width of [412, 1440]) test(`manual Results offers original drafts and exact files without an empty report at ${width}`, async ({ page }) => {
  await page.setViewportSize({ width, height: 1000 });
  const data = await mockDocuments(page);
  data.publish();
  await page.goto("/?session=s_browser");
  const input = page.getByRole("textbox", { name: "问点什么…", exact: true });
  await input.fill("Keep my next question");
  if (width < 1024) await mobilePaneTab(page).click();
  else await page.getByRole("button", { name: "重开成果", exact: true }).click();
  const chooser = page.getByRole("region", { name: messages.results_workspace.choose_result_title, exact: true });
  await expect(chooser).toBeVisible();
  await expect(page.getByText(messages.canvas.empty_main, { exact: true })).not.toBeVisible();
  const draft = chooser.getByRole("region", { name: t.canvas_documents, exact: true }).getByRole("button", { name: /Quarterly A/ });
  await expect(draft).toBeVisible();
  const row = await draft.evaluate((element) => {
    const title = element.querySelector('span > span')!.getBoundingClientRect();
    const button = element.getBoundingClientRect();
    return { inset: title.left - button.left, width: button.width, viewport: window.innerWidth, documentWidth: document.documentElement.scrollWidth };
  });
  expect(row.inset).toBeLessThan(24);
  expect(row.documentWidth).toBeLessThanOrEqual(row.viewport);
  await expect(page.getByRole("button", { name: messages.results_workspace.back_to_results, exact: true })).toHaveCount(0);
  if (width < 1024) expect((await draft.boundingBox())!.height).toBeGreaterThanOrEqual(44);
  if (process.env.CANVAS_NAVIGATION_EVIDENCE_DIR) await page.screenshot({
    path: `${process.env.CANVAS_NAVIGATION_EVIDENCE_DIR}/results-${width}.png`, animations: "disabled",
  });
  await draft.click();
  await expect(page).toHaveURL(/canvas_document=djob_a/);
  await showCanvasResults(page);
  await expect(chooser).toBeVisible();
  // The document draft and its published source share a name; open the published file.
  await chooser.getByRole("navigation", { name: "成果导航" }).getByRole("button", { name: /Quarterly A.pptx/ }).click();
  await expect(page).toHaveURL(/result=art_canvas_source/);
  await expect(chooser).not.toBeVisible();
  await expect(page.getByRole("textbox", { name: "问点什么…", exact: true, includeHidden: true })).toHaveValue("Keep my next question");
  await page.reload();
  await expect(page).toHaveURL(/result=art_canvas_source/);
  await expect(page.locator(".results-object-surface--artifact")).toBeVisible();
  await expect(chooser).not.toBeVisible();
  expect(data.posts).toHaveLength(0);
  expect(data.commands).toHaveLength(0);
});

test("a new native draft still opens after an earlier draft was closed and reloaded", async ({ page }) => {
  const data = await mockDocuments(page);
  await replayDocumentOpen(page);
  await page.goto("/?session=s_browser");
  await expect(page).toHaveURL(/canvas_document=djob_a/);
  await closeDocument(page, "Quarterly A").click();
  data.newDraft();
  await page.route("**/api/v1/sessions/s_browser", (route) => route.fulfill({ json: {
    session: { id: "s_browser", workspace_id: "ws_smoke", title: "Document work", status: "completed" },
    turns: [{ id: "t_new", seq: 2, user_text: "Prepare a new draft", status: "completed", final_answer: "A new draft is ready", attachments: [] }],
    chat_execution: { write_protocol: "legacy_v0", config_generation: "test", readiness_generation: "test" },
  } }));
  await page.route("**/api/v1/turns/t_new/events", (route) => route.fulfill({ json: { events: [{
    seq: 1, occurred_at: "2026-10-04T00:00:00Z", event_type: "document.draft_open",
    payload: { ...draftOpen, job_id: "djob_new", turn_id: "t_new", draft_digest: "b".repeat(64) },
  }] } }));
  await page.reload();
  await expect(page).toHaveURL(/canvas_document=djob_new/);
  await expect(documentTab(page, "Quarterly A")).toHaveAttribute("aria-selected", "true");
  expect(data.commands).toHaveLength(0);
  expect(data.posts).toHaveLength(0);
});

test("late native document opening preserves the selected object and Composer focus", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 });
  const data = await mockDocuments(page);
  await replayDocumentOpen(page);
  let release!: () => void;
  let reading = false;
  const pending = new Promise<void>((done) => { release = done; });
  await page.route("**/api/v1/sessions/s_browser/documents/djob_a", async (route) => {
    reading = true;
    await pending;
    await route.fulfill({ json: { ...fixture.review, id: "djob_a", can_revise: false,
      draft: { ...fixture.review.draft, output_stem: "Quarterly A" } } });
  });
  try {
    await page.goto("/?session=s_browser");
    await expect.poll(() => reading).toBe(true);
    await openDocument(page, "Quarterly B");
    const composer = page.getByRole("textbox", { name: "问点什么…", exact: true });
    await composer.fill("Keep this newer input");
    release();
    await expect(documentTab(page, "Quarterly A")).toHaveAttribute("aria-selected", "false");
    await expect(documentTab(page, "Quarterly B")).toHaveAttribute("aria-selected", "true");
    await expect(composer).toBeFocused();
    await expect(composer).toHaveValue("Keep this newer input");
    await expect(page).toHaveURL(/canvas_document=djob_b/);
    expect(data.commands).toHaveLength(0);
  } finally { release(); }
});

test("native document opening rejects wrong scope and current authorization failure", async ({ page }) => {
  await mockDocuments(page);
  let reads = 0;
  await page.route("**/api/v1/sessions/s_browser/documents/djob_a", (route) => {
    reads += 1;
    return route.fulfill({ status: 403, json: { detail: "Unavailable" } });
  });
  await replayDocumentOpen(page, { ...draftOpen, user_id: "u_other" });
  await page.goto("/?session=s_browser");
  await expect(page.getByRole("textbox", { name: "问点什么…", exact: true })).toBeVisible();
  await expect(page).not.toHaveURL(/canvas_document=/);
  expect(reads).toBe(0);
  await replayDocumentOpen(page);
  await page.reload();
  await expect.poll(() => reads).toBeGreaterThan(0);
  await expect(documentTab(page, "Quarterly A")).toHaveCount(0);
  await expect(page).not.toHaveURL(/canvas_document=/);
});


test("live native document completion opens the same original review without publication", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 });
  const data = await mockDocuments(page);
  data.nativeTurn("t_plain");
  let release!: () => void;
  const submitted = new Promise<void>((done) => { release = done; });
  await page.route("**/api/v1/chat/stream/t_plain*", async (route) => { await submitted; return route.fulfill({ contentType: "text/event-stream", body: [
    `event: document.draft_open\ndata: ${JSON.stringify({ ...draftOpen, turn_id: "t_plain", seq: 1 })}\n\n`,
    'event: done\ndata: {"seq":2,"turn_id":"t_plain","status":"completed"}\n\n',
  ].join("") }); });
  await page.goto("/?session=s_browser");
  const input = page.getByRole("textbox", { name: "问点什么…", exact: true });
  await input.fill("Prepare the private document draft");
  await input.press("Enter");
  await expect(input).toHaveValue("");
  release();
  await expect(page).toHaveURL(/canvas_document=djob_a/);
  await expect(documentTab(page, "Quarterly A")).toHaveAttribute("aria-selected", "true");
  expect(data.commands).toHaveLength(0);
  expect(data.posts).toHaveLength(1);
});

test("native document completion keeps a newer compact Chat input visible", async ({ page }) => {
  await page.setViewportSize({ width: 412, height: 1000 });
  await mockDocuments(page);
  await replayDocumentOpen(page);
  let release!: () => void;
  let reading = false;
  const pending = new Promise<void>((done) => { release = done; });
  await page.route("**/api/v1/sessions/s_browser/documents/djob_a", async (route) => {
    reading = true;
    await pending;
    await route.fulfill({ json: { ...fixture.review, id: "djob_a", draft: { ...fixture.review.draft, output_stem: "Quarterly A" } } });
  });
  try {
    await page.goto("/?session=s_browser");
    await expect.poll(() => reading).toBe(true);
    const input = page.getByRole("textbox", { name: "问点什么…", exact: true });
    await input.fill("A newer question I am still writing");
    release();
    await expect(documentTab(page, "Quarterly A")).toBeAttached();
    await expect(input).toBeVisible();
    await expect(input).toBeFocused();
    await expect(input).toHaveValue("A newer question I am still writing");
    await expect(page).not.toHaveURL(/canvas_document=/);
  } finally { release(); }
});


test("expanded compact review scrolls to confirmation and keeps the selected close control reachable", async ({ page }) => {
  await page.setViewportSize({ width: 412, height: 915 });
  const data = await mockDocuments(page);
  data.expandReview();
  await page.goto("/?session=s_browser");
  await page.getByRole("tab", { name: "成果", exact: true }).click();
  await openDocument(page, "Quarterly A");
  const panel = page.getByRole("tabpanel", { name: /Quarterly A/ });
  await expect(panel.getByText(/^Reviewed quarter content \d$/)).toHaveCount(5);
  // The object body (C3) scrolls inside the panel while the footer stays fixed.
  await expect.poll(() => panel.evaluate((node) => [...node.querySelectorAll<HTMLElement>("*")].some((element) =>
    element.scrollHeight > element.clientHeight + 1 && ["auto", "scroll"].includes(getComputedStyle(element).overflowY)))).toBe(true);
  const box = await panel.boundingBox();
  await page.mouse.move(box!.x + box!.width / 2, box!.y + Math.min(box!.height / 2, 300));
  await page.mouse.wheel(0, 2000);
  const confirm = page.getByRole("button", { name: t.confirm, exact: true });
  await expect(confirm).toBeInViewport({ ratio: 1 });
  await page.getByRole("checkbox").check();
  await expect(confirm).toBeEnabled();
  await page.screenshot({ path: "/tmp/bowhead-canvas-expanded-review-fixed.png" });
  await openDocument(page, "Quarterly B");
  const close = closeDocument(page, "Quarterly B");
  await expect(close).toBeInViewport({ ratio: 1 });
  await close.focus();
  await close.press("Enter");
  await expect(page).toHaveURL(/canvas_document=djob_a/);
  expect(data.commands).toEqual([]);
});

for (const width of [412, 1440]) {
  for (const appearance of ["light", "dark"] as const) {
    test(`document review stays readable and actionable at ${width} in ${appearance}`, async ({
      page,
    }, testInfo) => {
      await page.setViewportSize({ width, height: 1000 });
      await page.emulateMedia({ colorScheme: appearance });
      const data = await mockDocuments(page);
      data.revisable();
      // A synthetic slide gives layout checks a realistic aspect ratio, not renderer certification.
      await page.route("**/api/v1/documents/*/preview/page-1.png", (route) =>
        route.fulfill({
          contentType: "image/svg+xml",
          body: `<svg xmlns="http://www.w3.org/2000/svg" width="1280" height="720" viewBox="0 0 1280 720">
          <rect width="1280" height="720" fill="#fff"/>
          <g fill="#183b4e" font-family="sans-serif"><text x="72" y="118" font-size="44">Quarterly operations</text>
          <text x="72" y="185" font-size="24">Dispatch review · Q2</text></g>
          <rect x="72" y="265" width="1136" height="58" fill="#183b4e"/>
          <g fill="#fff" font-family="sans-serif" font-size="24"><text x="96" y="303">Hub</text><text x="690" y="303">Dispatched parcels</text></g>
          <rect x="72" y="323" width="1136" height="136" fill="#eef3f5"/>
          <g fill="#183b4e" font-family="sans-serif" font-size="24"><text x="96" y="373">West</text><text x="690" y="373">100</text>
          <text x="96" y="432">East</text><text x="690" y="432">50</text><text x="72" y="638" font-size="16">Internal operations report</text></g>
        </svg>`,
        }),
      );
      await page.goto("/?session=s_browser");
      if (width < 1024)
        await page.getByRole("tab", { name: "成果", exact: true }).click();
      await openDocument(page, "Quarterly A");
      await expect(page.locator("html")).toHaveAttribute(
        "data-theme",
        appearance,
      );
      const panel = page.getByRole("tabpanel", { name: /Quarterly A/ });
      await expect(panel.getByText(t.before, { exact: true })).toBeVisible();
      await expect(panel.getByText(t.after, { exact: true })).toBeVisible();
      await page.screenshot({
        animations: "disabled",
        path: `/tmp/bowhead-canvas-review-top-${width}-${appearance}-${testInfo.project.name}.png`,
      });
      const bounds = await panel.boundingBox();
      const acknowledgement = panel.getByRole("checkbox");
      const confirm = panel.getByRole("button", {
        name: t.confirm,
        exact: true,
      });
      await confirm.scrollIntoViewIfNeeded();
      await expect(acknowledgement).toBeInViewport({ ratio: 1 });
      await expect(confirm).toBeDisabled();
      await expect(confirm).toBeInViewport({ ratio: 1 });
      await acknowledgement.check();
      await expect(confirm).toBeEnabled();
      const confirmBounds = await confirm.boundingBox();
      expect(confirmBounds!.x).toBeGreaterThan(bounds!.x);
      expect(confirmBounds!.x + confirmBounds!.width).toBeLessThan(
        bounds!.x + bounds!.width,
      );
      if (width < 1024)
        expect(confirmBounds!.height).toBeGreaterThanOrEqual(44);
      await expect
        .poll(() =>
          page.evaluate(
            () => document.documentElement.scrollWidth <= window.innerWidth,
          ),
        )
        .toBe(true);
      await page.screenshot({
        animations: "disabled",
        path: `/tmp/bowhead-canvas-review-${width}-${appearance}-${testInfo.project.name}.png`,
      });
      await panel.getByRole("button", { name: "更多操作", exact: true }).click();
      await page.getByRole("menuitem", { name: t.preview_details_menu, exact: true }).click();
      await expect(
        page.getByText(t.renderer_note, { exact: true }),
      ).toBeVisible();
      await page.keyboard.press("Escape");
      await expect(
        panel.getByRole("button", { name: new RegExp(`^${t.font_substitutions}`) }),
      ).toBeVisible();
      await page.addStyleTag({
        content:
          '[role="tabpanel"] { --fs-md: 26px; --fs-sm: 24px; --fs-lg: 32px; }',
      });
      await confirm.scrollIntoViewIfNeeded();
      await expect(confirm).toBeInViewport({ ratio: 1 });
      await expect(acknowledgement).toBeInViewport({ ratio: 1 });
      await expect
        .poll(() =>
          page.evaluate(
            () => document.documentElement.scrollWidth <= window.innerWidth,
          ),
        )
        .toBe(true);
      expect(data.commands).toEqual([]);
      expect(data.posts).toEqual([]);
    });
  }
}

test("published document opens its exact source and PDF without the empty report detour", async ({ page }) => {
  const data = await mockDocuments(page);
  data.publish();
  await page.goto("/?session=s_browser");
  for (const [label, id] of [[t.canvas_open_pptx, "art_canvas_source"], [t.canvas_open_pdf, "art_canvas_pdf"]]) {
    await openDocument(page, "Quarterly A");
    await page.getByRole("button", { name: label, exact: true }).click();
    await expect(page).toHaveURL(new RegExp(`result=${id}`));
    await expect(page).toHaveURL(/canvas_doc_view=results/);
    await page.getByRole("tab", { name: "详情", exact: true }).click();
    await expect(page.getByRole("link", { name: "下载", exact: true }).first()).toHaveAttribute("href", `/api/v1/files/${id}`);
  }
  expect(data.commands).toEqual([]);
});

test("Session recovery and shell remount retain the original Space while another tab changes preference", async ({ page, context }) => {
  const data = await mockDocuments(page);
  await page.route("**/api/v1/auth/me/workspaces", (route) => route.fulfill({ json: {
    items: [
      { id: "ws_other", workspace_id: "ws_other", name: "Other Space", role: "admin" },
      { id: "ws_smoke", workspace_id: "ws_smoke", name: "Original Space", role: "admin" },
    ], active_id: "ws_other",
  } }));
  await page.addInitScript(() => {
    localStorage.setItem("bowhead.space_id", "ws_other");
    localStorage.setItem("bowhead.workspace_id", "ws_other");
  });
  await page.goto("/?session=s_browser");
  await openDocument(page, "Quarterly A");
  await expect(page.getByRole("button", { name: /当前：Original Space$/ })).toBeVisible();
  const other = await context.newPage();
  await mockDocuments(other);
  await other.goto("/");
  await other.evaluate(() => {
    localStorage.setItem("bowhead.space_id", "ws_other");
    localStorage.setItem("bowhead.workspace_id", "ws_other");
  });
  await page.setViewportSize({ width: 412, height: 915 });
  await page.setViewportSize({ width: 1440, height: 1000 });
  await expect(page.getByRole("button", { name: /当前：Original Space$/ })).toBeVisible();
  await page.reload();
  await expect(documentTab(page, "Quarterly A")).toBeVisible();
  await expect(page.getByRole("button", { name: /当前：Original Space$/ })).toBeVisible();
  expect(data.scopes.length).toBeGreaterThan(0);
  expect(data.scopes.every((headers) => headers["x-space-id"] === "ws_smoke")).toBe(true);
  expect(data.commands).toEqual([]);
  await other.close();
});


test("duplicate document names retain their identity after reverse opening and closing", async ({ page }) => {
  const data = await mockDocuments(page);
  data.sameTitles();
  await page.goto("/?session=s_browser");
  const first = `Quarterly.pptx · ${messages.settings_canvas.duplicate_ordinal.replace("{number}", "1")}`;
  const second = `Quarterly.pptx · ${messages.settings_canvas.duplicate_ordinal.replace("{number}", "2")}`;
  await openDocument(page, second);
  await expect(page).toHaveURL(/canvas_document=djob_b/);
  await openDocument(page, first);
  await expect(page).toHaveURL(/canvas_document=djob_a/);
  await expect(documentTab(page, first)).toHaveAttribute("aria-selected", "true");
  await expect(documentTab(page, second)).toHaveAttribute("aria-selected", "false");
  await closeDocument(page, second).click();
  await openDocument(page, second);
  await expect(documentTab(page, second)).toHaveAttribute("aria-selected", "true");
  await expect(page).toHaveURL(/canvas_document=djob_b/);
  expect(data.commands).toEqual([]);
});

for (const width of [412, 1440]) {
  test(`pending document discovery does not open an empty report at ${width}`, async ({ page }) => {
    await page.setViewportSize({ width, height: 915 });
    const data = await mockDocuments(page);
    let failed = false;
    const question = () => ({
      ...fixture.review, id: "djob_question", draft: undefined, draft_digest: null,
      can_confirm: false, state: failed ? "failed" : "question",
      question: failed ? null : "Which quarter should be included?",
      error_code: failed ? "document_information_incomplete" : null,
    });
    await page.route("**/api/v1/sessions/s_browser/documents", (route) =>
      route.fulfill({ json: { jobs: [question()], next_cursor: null } }));
    await page.route("**/api/v1/sessions/s_browser/documents/djob_question", (route) =>
      route.fulfill({ json: question() }));
    await page.goto("/?session=s_browser");
    const menu = page.getByRole("button", { name: openLabel, exact: true, includeHidden: true }).first();
    await expect(menu).toBeAttached();
    await expect(page.locator(".v2-split-canvas")).toBeHidden();
    failed = true;
    await page.reload();
    await expect(menu).toBeAttached();
    await expect(page.locator(".v2-split-canvas")).toBeHidden();
    if (width < 1024) await mobilePaneTab(page).click();
    else await page.getByRole("button", { name: "重开成果", exact: true }).click();
    await page.getByRole("button", { name: openLabel, exact: true }).filter({ visible: true }).first().click();
    await page.getByRole("menuitem", { name: new RegExp(t.title) }).click();
    await expect(page).toHaveURL(/canvas_document=djob_question/);
    await expect(page.locator(".v2-split-canvas")).toBeVisible();
    expect(data.commands).toEqual([]);
    expect(data.posts).toEqual([]);
  });
}

for (const locale of ["zh-CN", "en-US"] as const) {
  test.describe(`terminal document failure in ${locale}`, () => {
    test.use({ locale });
    const copy = locale === "zh-CN" ? messages.documents : englishMessages.documents;
    for (const width of [412, 1440]) {
      for (const appearance of ["light", "dark"] as const) {
        test(`failure recovery preserves input and original target at ${width} in ${appearance}`, async ({ page }, testInfo) => {
          await page.setViewportSize({ width, height: 1000 });
          await page.emulateMedia({ colorScheme: appearance });
          const data = await mockDocuments(page);
          const job = {
            ...fixture.review, id: "djob_12345678", state: "failed", error_code: "document_information_incomplete",
            draft: undefined, draft_digest: null, can_confirm: true, can_revise: false,
            request_text: "Update the quarterly presentation using the supplied figures.\nKeep the existing layout and preserve all source references. ".repeat(4),
            usage: { settled_credits: 11, pending: false },
          };
          const reads: Record<string, string>[] = [];
          let hold = false;
          let release!: () => void;
          const pending = new Promise<void>((resolve) => { release = resolve; });
          await page.route("**/api/v1/sessions/s_browser", (route) => route.fulfill({ json: {
            session: { id: "s_browser", workspace_id: "ws_smoke", title: "Document work", status: "completed" },
            turns: [{ id: "t_doc", seq: 1, user_text: "Update my presentation", status: "completed", final_answer: copy.error_incomplete, attachments: [] }],
            chat_execution: { write_protocol: "legacy_v0", config_generation: "test", readiness_generation: "test" },
          } }));
          await page.route("**/api/v1/sessions/s_browser/documents", (route) => route.fulfill({ json: { jobs: [job], next_cursor: null } }));
          await page.route("**/api/v1/sessions/s_browser/documents/djob_12345678", async (route) => {
            reads.push(route.request().headers());
            if (hold) await pending;
            await route.fulfill({ json: job });
          });
          await page.goto("/?session=s_browser");
          const input = page.locator(".composer-v2 textarea").first();
          await expect(input).toBeVisible();
          await input.fill("Keep my existing unsent request");
          if (width < 1024) await page.getByRole("tab", { name: locale === "zh-CN" ? "成果" : "Results", exact: true }).click();
          else await page.getByRole("button", { name: locale === "zh-CN" ? "重开成果" : "Reopen Results", exact: true }).click();
          const list = page.getByRole("region", { name: copy.canvas_documents });
          const title = copy.title;
          await expect(list.getByText(title, { exact: true })).toBeVisible();
          await expect(list.getByText(copy.state_incomplete, { exact: true })).toBeVisible();
          await page.getByRole("button", { name: (locale === "zh-CN" ? messages : englishMessages).settings_canvas.open_menu, exact: true })
            .filter({ visible: true }).first().click();
          const menuitem = page.getByRole("menuitem", { name: new RegExp(title) });
          await expect(menuitem).toContainText(copy.state_incomplete);
          if (width >= 1024) {
            const bounds = await page.getByRole("menu").boundingBox();
            expect(bounds?.width).toBeLessThanOrEqual(360);
          }
          if (appearance === "light") await page.screenshot({ animations: "disabled", path: `/tmp/bowhead-canvas-failure-menu-${locale}-${width}-${testInfo.project.name}.png` });
          await menuitem.focus();
          await menuitem.press("Enter");
          const panel = page.getByRole("tabpanel", { name: new RegExp(title) });
          await expect(panel.getByText(copy.error_incomplete, { exact: true })).toBeVisible();
          // The object toolbar uses the shared status vocabulary (C11).
          await expect(panel.getByText((locale === "zh-CN" ? messages : englishMessages).canvas_object.status.failed, { exact: true })).toBeVisible();
          // D7: settled credits moved from the body into the toolbar's “⋯ → Usage” dialog.
          const ui = locale === "zh-CN" ? messages : englishMessages;
          await panel.getByRole("button", { name: ui.common.more_actions, exact: true }).click();
          await page.getByRole("menuitem", { name: copy.usage_menu, exact: true }).click();
          await expect(page.getByRole("dialog").getByText(locale === "zh-CN" ? "用量 · 11 积分" : "Usage · 11 credits", { exact: true })).toBeVisible();
          await page.keyboard.press("Escape");
          await expect(panel.getByRole("button", { name: copy.confirm })).toHaveCount(0);
          await expect(panel.getByRole("button", { name: copy.canvas_revision_action })).toHaveCount(0);
          await expect(page.locator("html")).toHaveAttribute("data-theme", appearance);
          const check = panel.getByRole("button", { name: copy.canvas_check_status });
          const initialReads = reads.length;
          hold = true;
          await check.focus();
          await check.press("Enter");
          await expect(check).toHaveAttribute("aria-disabled", "true");
          await expect(check).toBeFocused();
          await expect.poll(() => reads.length).toBe(initialReads + 1);
          await check.press("Enter");
          const checkedReads = reads.length;
          release();
          await expect(check).not.toHaveAttribute("aria-disabled", "true");
          await expect(check).toBeFocused();
          expect(reads.length).toBe(checkedReads);
          expect(reads.every((headers) => headers["x-team-id"] === "team_smoke" && headers["x-space-id"] === "ws_smoke")).toBe(true);
          await expect(page).toHaveURL(/canvas_document=djob_12345678/);
          await page.screenshot({ animations: "disabled", path: `/tmp/bowhead-canvas-failure-${locale}-${width}-${appearance}-${testInfo.project.name}.png` });
          await panel.getByText(copy.source_request, { exact: true }).click();
          await expect(panel.getByText(job.request_text, { exact: true })).toBeVisible();
          expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
          const navigate = panel.getByRole("button", { name: copy.canvas_return_to_chat });
          await navigate.focus();
          await navigate.press("Enter");
          await expect(input).toBeVisible();
          await expect(input).toHaveValue("Keep my existing unsent request");
          await expect(input).toBeFocused();
          await expect(page).toHaveURL(/canvas_document=djob_12345678/);
          expect(data.posts).toEqual([]);
          expect(data.commands).toEqual([]);
          expect(data.downloads).toEqual([]);
        });
      }
    }
  });
}
