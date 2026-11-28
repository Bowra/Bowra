import { expect, test, type Page, type Route } from "@playwright/test";

import messages from "../../edition/shared-messages/zh-CN.json";
import { parseResultsQueryResponse, type ResultItemDTO, type ResultsQueryRequest } from "../../lib/results-api";

// In-place Word editing (docs/sprints/docx-inline-edit-2026-11-26/tech-design.md §8; canvas W1–W10),
// behind FEATURE_DOCX_INLINE_EDIT: the Results preview shows a Word file's pages, 编辑 opens the
// editor in the shared shell, paragraphs are found on the PDF.js text of the pages and edited there.

const apiPort = process.env.PLAYWRIGHT_API_PORT ?? "18080";
const copy = messages.document_edit;
const shell = messages.edit_shell;
const DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document";

type Line = { text: string; x: number; y: number; size: number };

const HEADER: Line = { text: "Quarterly review", x: 260, y: 810, size: 9 };
const PAGES: Line[][] = [
  [
    HEADER,
    { text: "1. Overview", x: 72, y: 760, size: 16 },
    { text: "Sales reached 2,905 thousand in the first quarter.", x: 72, y: 730, size: 11 },
    { text: "Two contracts moved to the second quarter.", x: 72, y: 710, size: 11 },
  ],
  [
    HEADER,
    { text: "2. Regions", x: 72, y: 760, size: 16 },
    { text: "The east region grew by eight percent.", x: 72, y: 730, size: 11 },
  ],
];

/** An A4 PDF whose text sits where a Word render would draw it. */
function documentPdf(pages: Line[][]): Buffer {
  const pageObjects = pages.flatMap((lines, index) => {
    const body = lines.map((line) => `BT /F1 ${line.size} Tf ${line.x} ${line.y} Td (${line.text}) Tj ET`).join("\n") + "\n";
    return [
      `<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 3 0 R >> >> /Contents ${5 + index * 2} 0 R >>`,
      `<< /Length ${Buffer.byteLength(body)} >>\nstream\n${body}endstream`,
    ];
  });
  const objects = [
    "<< /Type /Catalog /Pages 2 0 R >>",
    `<< /Type /Pages /Count ${pages.length} /Kids [${pages.map((_, index) => `${4 + index * 2} 0 R`).join(" ")}] >>`,
    "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ...pageObjects,
  ];
  let pdf = "%PDF-1.4\n";
  const offsets = [0];
  for (const [index, object] of objects.entries()) {
    offsets.push(Buffer.byteLength(pdf));
    pdf += `${index + 1} 0 obj\n${object}\nendobj\n`;
  }
  const xref = Buffer.byteLength(pdf);
  pdf += `xref\n0 ${offsets.length}\n0000000000 65535 f \n`;
  pdf += offsets.slice(1).map((offset) => `${String(offset).padStart(10, "0")} 00000 n \n`).join("");
  pdf += `trailer\n<< /Size ${offsets.length} /Root 1 0 R >>\nstartxref\n${xref}\n%%EOF\n`;
  return Buffer.from(pdf);
}

function inventory(id: string, version: number, overrides: Record<string, unknown> = {}) {
  return {
    artifact_id: id, version_no: version, edit_mode: "version", page: { width: 11906, height: 16838 }, field_token: "￼",
    document: { refusal: null },
    paragraphs: [
      { key: "document:b0", kind: "body", table: null, text: "1. Overview", heading_level: 1, editable: true, reason: null },
      { key: "document:b1", kind: "body", table: null, text: "Sales reached 2,905 thousand in the first quarter.", heading_level: null, editable: true, reason: null },
      { key: "document:b2", kind: "body", table: null, text: "Two contracts moved to the second quarter.", heading_level: null, editable: true, reason: null },
      { key: "document:b3", kind: "body", table: null, text: "2. Regions", heading_level: 1, editable: true, reason: null },
      { key: "document:b4", kind: "body", table: null, text: "The east region grew by eight percent.", heading_level: null, editable: true, reason: null },
    ],
    read_only_parts: { header_footer: 1, note: 0, comment: 0 },
    read_only_paragraphs: [{ key: "header1:b0", part: "header", text: "Quarterly review", reason: "header_footer" }],
    file: { filename: "Review.docx", mime: DOCX, size: 4096 },
    new_filename: null, edited_copies: [], drops_on_save: [],
    ...overrides,
  };
}

async function openDocument(page: Page, options: { refusal?: string } = {}) {
  const state = { latest: "art_doc_1", saved: null as unknown };
  await page.request.post(`http://127.0.0.1:${apiPort}/api/v1/_test/reset`);
  await page.route("**/api/v1/auth/me", async (route: Route) => {
    const response = await route.fetch();
    const body = await response.json() as { capabilities?: Record<string, unknown> };
    await route.fulfill({ response, json: { ...body, capabilities: { ...(body.capabilities ?? {}), docx_inline_edit: true } } });
  });
  await page.route("**/api/v1/chat/sessions/*/reference-queue?*", (route) => route.fulfill({ json: { items: [] } }));
  await page.route("**/api/v1/results/capabilities", (route) => route.fulfill({ json: {
    dto_version: 1, workspace: true, complete_list: true, scale_nav: true, max_page_size: 50,
  } }));
  await page.route("**/api/v1/sessions/*/results/query", async (route: Route) => {
    const request = route.request().postDataJSON() as ResultsQueryRequest;
    const count = state.latest === "art_doc_1" ? 1 : 2;
    const entry: ResultItemDTO = {
      key: "artifact:art_doc_1", object_type: "artifact", title: "Review.docx",
      primary_group: "deliverables", secondary_group: null, viewer_profile: "document", status: "ready",
      route: { type: "artifact", artifact_id: state.latest, default_mode: "preview" },
      capabilities: ["download", "view_details", "reference_in_chat", "edit_document", ...(count > 1 ? ["view_history" as const] : [])],
      relation_roles: ["main_deliverable"], execution: null, version: null, lineage: { number: count, count }, position: 1,
      display_context: { source_role: "generated", output_role: "main_deliverable", execution_label: null,
        occurred_at: "2026-11-27T00:00:00Z", duplicate_ordinal: null, duplicate_count: null },
      artifact_metadata: { mime: DOCX, size: 4096, source: "python_sandbox", kind: "deliverable", origin: "compute", trust: "trusted",
        created_at: "2026-11-27T00:00:00Z", producer_kind: "python_sandbox" },
    };
    const keys = ["artifact:art_doc_1", "artifact:art_doc_2"];
    if (request.surface === "locate" && !keys.includes(request.anchor_key ?? "")) {
      await route.fulfill({ status: 409, json: { detail: { error: { code: "results_anchor_not_in_view" } } } });
      return;
    }
    await route.fulfill({ json: parseResultsQueryResponse({
      dto_version: 1, capability: { complete_list: true, scale_nav: true }, snapshot_token: "snapshot",
      items: [entry], authorized_total: 1, matched_total: 1, loaded_count: 1,
      group_counts: { deliverables: 1, evidence: 0, data: 0, code: 0, input: 0 },
      page_start: 0, next_cursor: null, previous_cursor: null, has_more: false,
      projection_status: { report_relations: "ready", execution_relations: "unavailable", export_relations: "degraded" },
      projection_revision: `revision-${state.latest}`, new_results: { count: null, projection_changed: false },
      resolved_current_key: request.current_key && keys.includes(request.current_key) ? entry.key : null,
    }) });
  });
  await page.route("**/api/v1/files/*/representations/*", async (route: Route) => {
    const [, id, mode] = new URL(route.request().url()).pathname.match(/files\/([^/]+)\/representations\/([^/]+)/) ?? [];
    if (mode === "docx-pdf") {
      await route.fulfill({ contentType: "application/pdf", body: documentPdf(PAGES) });
      return;
    }
    // A Word preview first reads the server's bounded text as its validation gate.
    if (mode === "preview") {
      await route.fulfill({ json: { family: "document", representation: "preview", revision: `preview-${id}`, mode: "text",
        text: PAGES.flat().map((line) => line.text).join("\n"), truncated: false } });
      return;
    }
    await route.fulfill({ json: { family: "document", representation: "details", revision: `details-${id}`, details: {
      id, filename: "Review.docx", mime: DOCX, size: 4096, source: "python_sandbox", created_at: "2026-11-27T00:00:00Z", kind: "deliverable",
    } } });
  });
  await page.route("**/api/v1/files/*", async (route: Route) => {
    if (route.request().url().includes("/representations/")) return route.fallback();
    await route.fulfill({ contentType: DOCX, body: Buffer.from("PK") });
  });
  await page.route("**/api/v1/sessions/*/results/document-edit/inventory", async (route: Route) => {
    const { artifact_id: id } = route.request().postDataJSON() as { artifact_id: string };
    await route.fulfill({ json: inventory(id, id === "art_doc_1" ? 1 : 2, options.refusal ? { document: { refusal: options.refusal } } : {}) });
  });
  await page.route("**/api/v1/sessions/*/results/document-edits", async (route: Route) => {
    state.saved = route.request().postDataJSON();
    state.latest = "art_doc_2";
    await route.fulfill({ status: 201, json: { artifact_id: "art_doc_2", version_no: 2, filename: "Review.docx", new_file: false, changes: [] } });
  });

  await page.goto("/");
  const composer = page.locator("textarea").first();
  await expect(composer).toBeVisible();
  await composer.fill("resultsworkspace");
  const sent = page.waitForResponse((response) => response.url().endsWith("/api/v1/chat/send"));
  await composer.press("Enter");
  const { turn_id: turnId, session_id: sessionId } = await (await sent).json();
  await expect(page.locator('[data-presentation="results-reading"]')).toHaveCount(1);
  const detail = await (await page.request.get(`/api/v1/sessions/${sessionId}`)).json();
  await page.route(`**/api/v1/sessions/${sessionId}`, (route) => route.fulfill({ json: {
    ...detail, turns: [{ id: turnId, seq: 1, user_text: "resultsworkspace", intent: "analysis", status: "completed",
      started_at: "2026-11-27T00:00:00Z", ended_at: "2026-11-27T00:00:01Z", final_answer: "报告已生成",
      trace_steps: [], summary: null }],
  } }));
  await page.goto(`/?session=${sessionId}&result=art_doc_1&mode=preview`);
  return state;
}

test("a Word paragraph is edited on its page, a longer one stays pending, and the save writes version 2", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  const state = await openDocument(page);
  await page.getByRole("button", { name: messages.artifact_viewer.edit_presentation, exact: true }).click();
  const editor = page.getByTestId("document-editor");
  await expect(editor.getByRole("navigation", { name: copy.outline })).toBeVisible();
  const sales = editor.getByRole("button", { name: /^正文，Sales reached 2,905/ });
  await expect(sales).toBeVisible();
  await page.screenshot({ path: test.info().outputPath("word-1-idle.png") });

  // A click selects; a second click edits the paragraph where it is drawn (canvas W1).
  await sales.click();
  await expect(editor.getByRole("group", { name: /^选中的内容：正文/ })).toBeVisible();
  await editor.locator('[data-hit-key="document:b1"][data-first]').click();
  const field = editor.locator("textarea:not([data-armed])");
  await expect(field).toBeFocused();
  await field.fill("Sales reached 3,105 thousand in the first quarter.");
  await page.keyboard.press("Enter");
  await expect(editor.locator("[data-painted='paragraph']")).toHaveText("Sales reached 3,105 thousand in the first quarter.");
  await page.screenshot({ path: test.info().outputPath("word-2-painted.png") });

  // An edit that adds a line stays pending, with its text in the margin (canvas W2).
  await editor.locator('[data-hit-key="document:b2"][data-first]').click();
  await editor.locator('[data-hit-key="document:b2"][data-first]').click();
  await editor.locator("textarea:not([data-armed])").fill("Two contracts moved to the second quarter; both are now signed and the full-year target is unchanged.");
  await page.keyboard.press("Escape");
  await expect(editor.getByRole("note", { name: copy.pending_badge.replace("{number}", "2") })).toBeVisible();
  await expect(editor.getByRole("status").filter({ hasText: "2 处未保存的修改" })).toBeVisible();
  await page.screenshot({ path: test.info().outputPath("word-3-pending.png") });

  await editor.getByRole("button", { name: shell.rail_changes_count.replace("{count}", "2") }).click();
  await expect(editor.getByRole("complementary", { name: new RegExp(shell.panel_changes) })).toBeVisible();
  await page.screenshot({ path: test.info().outputPath("word-4-changes.png") });

  await editor.getByRole("button", { name: copy.save_version, exact: true }).click();
  await expect(editor.getByRole("button", { name: copy.edit_again, exact: true })).toBeFocused();
  expect(state.saved).toEqual({
    artifact_id: "art_doc_1",
    edits: [
      { key: "document:b1", expect: "Sales reached 2,905 thousand in the first quarter.", text: "Sales reached 3,105 thousand in the first quarter." },
      { key: "document:b2", expect: "Two contracts moved to the second quarter.", text: "Two contracts moved to the second quarter; both are now signed and the full-year target is unchanged." },
    ],
  });
  await page.screenshot({ path: test.info().outputPath("word-5-saved.png") });
});

test("a header is explained, not edited, and a document with tracked changes offers download first", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await openDocument(page);
  await page.getByRole("button", { name: messages.artifact_viewer.edit_presentation, exact: true }).click();
  const editor = page.getByTestId("document-editor");
  await editor.locator('[data-hit-key="header1:b0"][data-first]').click();
  await expect(editor.getByRole("group", { name: /^选中的内容：页眉/ })).toContainText(copy.reason_header_footer);
  await page.screenshot({ path: test.info().outputPath("word-6-readonly.png") });

  await page.unrouteAll({ behavior: "ignoreErrors" });
  await openDocument(page, { refusal: "track_changes_on" });
  await page.getByRole("button", { name: messages.artifact_viewer.edit_presentation, exact: true }).click();
  const blocked = page.getByRole("alertdialog", { name: copy.blocked_title_track_changes_on });
  await expect(blocked).toBeVisible();
  await expect(blocked.getByRole("button").first()).toHaveText(copy.download_file);
  await page.screenshot({ path: test.info().outputPath("word-7-blocked.png") });
});

for (const width of [1100, 960]) {
  test(`the Word editor at ${width} px: an open panel hides the outline and the pages fit`, async ({ page }) => {
    await page.setViewportSize({ width, height: 800 });
    await openDocument(page);
    await page.getByRole("button", { name: messages.artifact_viewer.edit_presentation, exact: true }).click();
    const editor = page.getByTestId("document-editor");
    await expect(editor.getByRole("button", { name: /^正文，Sales reached/ })).toBeVisible();
    await editor.getByRole("button", { name: shell.panel_items, exact: true }).click();
    await expect(editor.getByRole("navigation", { name: copy.outline })).toHaveCount(0);
    await expect(editor.getByRole("button", { name: copy.outline, exact: true })).toBeVisible();
    const pageBox = await editor.locator("[data-page='1']").boundingBox();
    const panel = await editor.getByRole("complementary").boundingBox();
    expect((pageBox?.x ?? 0) + (pageBox?.width ?? 0)).toBeLessThanOrEqual(panel?.x ?? 0);
    await page.screenshot({ path: test.info().outputPath(`word-8-narrow-${width}.png`) });
  });
}

test("on a phone the pages are a list, a tapped paragraph is edited in a sheet, and 修改记录 saves from the bottom", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  // Sheets settle at once, so the screenshots show them, not their entrance.
  await page.emulateMedia({ reducedMotion: "reduce" });
  const state = await openDocument(page);
  await page.getByRole("button", { name: messages.artifact_viewer.edit_presentation, exact: true }).click();
  const editor = page.getByTestId("document-editor");
  await expect(editor).toHaveAttribute("data-layout", "phone");
  const editPage = editor.getByRole("button", { name: /^修改第 1 页/ });
  await expect(editPage).toBeVisible();
  await page.screenshot({ path: test.info().outputPath("word-phone-1-list.png") });

  // Tap areas grow toward 44 px; between two close lines the boundary is the middle, so no tap falls between them (canvas WP1).
  const tap = editor.locator('[data-hit-key="document:b1"][data-first]');
  const next = await editor.locator('[data-hit-key="document:b2"][data-first]').boundingBox();
  const box = await tap.boundingBox();
  expect(Math.abs((box?.y ?? 0) + (box?.height ?? 0) - (next?.y ?? 0))).toBeLessThan(1);
  expect(box?.height ?? 0).toBeGreaterThan(11 * (358 / 595) + 4);
  await tap.click();
  const sheet = page.getByRole("dialog", { name: /Overview · 第 1 段/ });
  const field = sheet.getByRole("textbox");
  await expect(field).toBeVisible();
  expect(await field.evaluate((node) => getComputedStyle(node).fontSize)).toBe("16px");
  await field.fill("Sales reached 3,105 thousand in the first quarter.");
  await page.screenshot({ path: test.info().outputPath("word-phone-2-paragraph.png") });
  await sheet.getByRole("button", { name: copy.done, exact: true }).click();
  await expect(sheet).toHaveCount(0);
  await expect(editor.getByText("第 1 段改为「Sales reached 3,105 thousand in the first quarter.」")).toBeVisible();

  // The page's sheet lists what can be edited on it.
  await editPage.click();
  const pageSheet = page.getByRole("dialog", { name: "第 1 页的内容" });
  await expect(pageSheet.getByRole("button", { name: /第 2 段/ })).toBeVisible();
  await page.screenshot({ path: test.info().outputPath("word-phone-3-page-sheet.png") });
  await pageSheet.getByRole("button", { name: "关闭" }).first().click();
  await expect(pageSheet).toHaveCount(0);

  await editor.getByRole("button", { name: "1 处未保存，查看修改记录" }).click();
  const changes = page.getByRole("dialog", { name: /^修改记录/ });
  await expect(changes).toBeVisible();
  await page.screenshot({ path: test.info().outputPath("word-phone-4-changes.png") });
  await changes.getByRole("button", { name: copy.save_version, exact: true }).click();
  await expect(editor.getByRole("button", { name: copy.edit_again, exact: true })).toBeFocused();
  expect(state.saved).toMatchObject({ edits: [{ key: "document:b1", text: "Sales reached 3,105 thousand in the first quarter." }] });
  await page.screenshot({ path: test.info().outputPath("word-phone-5-saved.png") });
});

test("on a phone a header is explained in a sheet with 引用这一页 and download", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.emulateMedia({ reducedMotion: "reduce" });
  await openDocument(page);
  await page.getByRole("button", { name: messages.artifact_viewer.edit_presentation, exact: true }).click();
  const editor = page.getByTestId("document-editor");
  await editor.locator('[data-page="1"] [data-hit-key="header1:b0"]').click();
  const sheet = page.getByRole("dialog", { name: /Quarterly review/ });
  await expect(sheet.getByText(copy.reason_header_footer, { exact: false })).toBeVisible();
  await expect(sheet.getByRole("button", { name: copy.download_file })).toBeVisible();
  await page.screenshot({ path: test.info().outputPath("word-phone-6-readonly.png") });
});
