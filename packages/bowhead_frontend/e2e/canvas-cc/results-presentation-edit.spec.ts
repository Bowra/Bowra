import { expect, test, type Page, type Route } from "@playwright/test";

import messages from "../../edition/shared-messages/zh-CN.json";
import { parseResultsQueryResponse, type ResultItemDTO, type ResultsQueryRequest } from "../../lib/results-api";

// In-place deck editing (docs/sprints/pptx-inline-edit-2026-11-24/tech-design.md §7): Edit opens the
// editor in the full view over the existing PDF.js render, a save becomes the next version, the
// overflow check reads the new render, and leaving opens the new version without rendering it again.

const apiPort = process.env.PLAYWRIGHT_API_PORT ?? "18080";
const copy = messages.presentation_edit;
const PPTX = "application/vnd.openxmlformats-officedocument.presentationml.presentation";

/** The table's cells as drawn: two 240-pixel columns and two 81-pixel rows from (48, 216). */
const TABLE_TEXT = [["Line", "Sales"], ["B", "2,905"]];

/**
 * A one-slide 960×540 PDF whose title sits where the inventory says the title box is; with
 * `table`, the table's text is drawn in its cells too, so the editor finds them on the render.
 */
function slidePdf(lines: string[], table = false, pages = 1): Buffer {
  const cells = table
    ? TABLE_TEXT.flatMap((row, r) => row.map((cell, c) => `BT /F1 24 Tf ${60 + c * 240} ${540 - (216 + r * 81 + 50)} Td (${cell}) Tj ET`))
    : [];
  const text = [...lines.map((line, index) => `BT /F1 36 Tf 60 ${470 - index * 50} Td (${line}) Tj ET`), ...cells].join("\n") + "\n";
  // Page k (from 0) is object 4 + 2k, its content 5 + 2k; later pages say only their number.
  const pageObjects = Array.from({ length: pages }, (_, index) => {
    const body = index === 0 ? text : `BT /F1 36 Tf 60 470 Td (Slide ${index + 1}) Tj ET\n`;
    return [
      `<< /Type /Page /Parent 2 0 R /MediaBox [0 0 960 540] /Resources << /Font << /F1 3 0 R >> >> /Contents ${5 + index * 2} 0 R >>`,
      `<< /Length ${Buffer.byteLength(body)} >>\nstream\n${body}endstream`,
    ];
  }).flat();
  const objects = [
    "<< /Type /Catalog /Pages 2 0 R >>",
    `<< /Type /Pages /Count ${pages} /Kids [${Array.from({ length: pages }, (_, index) => `${4 + index * 2} 0 R`).join(" ")}] >>`,
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

function inventory(id: string, version: number, title: string, table = false, chart = false, pages = 1) {
  return {
    artifact_id: id, version_no: version, edit_mode: "version", slide_width: 12192000, slide_height: 6858000, field_token: "￼",
    slides: [{ slide_id: 256, index: 1, inherited_text: false, elements: [
      { key: "256:2", shape_id: 2, name: "Title 1", kind: "text", box: [0.05, 0.08, 0.6, 0.12], editable: true, reason: null,
        paragraphs: [{ text: title, mixed_style: false, fields: 0 }] },
      { key: "256:3", shape_id: 3, name: "Table 2", kind: "table", box: [0.05, 0.4, 0.5, 0.3], editable: true, reason: null,
        rows: [[{ text: "Line", editable: true, reason: null }, { text: "Sales", editable: true, reason: null }],
          [{ text: "B", editable: true, reason: null }, { text: "2,905", editable: true, reason: null }]],
        ...(table ? { col_widths: [0.5, 0.5], row_heights: [0.5, 0.5] } : {}) },
      { key: "256:4", shape_id: 4, name: "Logo", kind: "picture", box: [0.85, 0.82, 0.1, 0.12], editable: false, reason: "picture" },
      // A chart low on the slide, so its data must open higher up to stay inside the page.
      ...(chart ? [{ key: "256:5", shape_id: 5, name: "Sales chart", kind: "chart", box: [0.6, 0.66, 0.22, 0.14], editable: true, reason: null,
        chart: { type: "COLUMN_CLUSTERED", categories: ["Jan", "Feb", "Mar", "Apr", "May", "Jun"], series: [{ name: "Sales", values: [905, 965, 1180, 1010, 990, 1240], format: "#,##0" }] } }] : []),
    ] },
    // Later slides of a longer deck: a title only, matching the PDF's pages.
    ...Array.from({ length: pages - 1 }, (_, index) => ({ slide_id: 257 + index, index: index + 2, inherited_text: false, elements: [
      { key: `${257 + index}:2`, shape_id: 2, name: "Title 1", kind: "text", box: [0.05, 0.08, 0.6, 0.12], editable: true, reason: null,
        paragraphs: [{ text: `Slide ${index + 2}`, mixed_style: false, fields: 0 }] },
    ] })),
    ],
  };
}

async function openDeck(page: Page, options: { table?: boolean; chart?: boolean; pages?: number } = {}) {
  const table = Boolean(options.table);
  const chart = Boolean(options.chart);
  const pages = options.pages ?? 1;
  const state = { latest: "art_deck_1", renders: {} as Record<string, number>, saved: null as unknown, conflictOnce: false };
  await page.request.post(`http://127.0.0.1:${apiPort}/api/v1/_test/reset`);
  await page.route("**/api/v1/chat/sessions/*/reference-queue?*", (route) => route.fulfill({ json: { items: [] } }));
  await page.route("**/api/v1/results/capabilities", (route) => route.fulfill({ json: {
    dto_version: 1, workspace: true, complete_list: true, scale_nav: true, max_page_size: 50,
  } }));
  await page.route("**/api/v1/sessions/*/results/query", async (route: Route) => {
    const request = route.request().postDataJSON() as ResultsQueryRequest;
    const count = state.latest === "art_deck_1" ? 1 : 2;
    const entry: ResultItemDTO = {
      key: "artifact:art_deck_1", object_type: "artifact", title: "Board.pptx",
      primary_group: "deliverables", secondary_group: null, viewer_profile: "document", status: "ready",
      route: { type: "artifact", artifact_id: state.latest, default_mode: "preview" },
      capabilities: ["download", "view_details", "reference_in_chat", "edit_presentation", ...(count > 1 ? ["view_history" as const] : [])],
      relation_roles: ["main_deliverable"], execution: null, version: null, lineage: { number: count, count }, position: 1,
      display_context: { source_role: "generated", output_role: "main_deliverable", execution_label: null,
        occurred_at: "2026-11-24T00:00:00Z", duplicate_ordinal: null, duplicate_count: null },
      artifact_metadata: { mime: PPTX, size: 4096, source: "pptx", kind: "deliverable", origin: "compute", trust: "trusted",
        created_at: "2026-11-24T00:00:00Z", producer_kind: "pptx" },
    };
    const keys = ["artifact:art_deck_1", "artifact:art_deck_2"];
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
    if (mode === "pptx-pdf") {
      state.renders[id] = (state.renders[id] ?? 0) + 1;
      const lines = id === "art_deck_1" ? ["Quarterly review"] : ["Quarterly review for", "the whole year"];
      await route.fulfill({ contentType: "application/pdf", body: slidePdf(lines, table, pages) });
      return;
    }
    await route.fulfill({ json: { family: "presentation", representation: "details", revision: `details-${id}`, details: {
      id, filename: "Board.pptx", mime: PPTX, size: 4096, source: "pptx", created_at: "2026-11-24T00:00:00Z", kind: "deliverable",
    } } });
  });
  await page.route("**/api/v1/sessions/*/results/presentation-edit/inventory", async (route: Route) => {
    const { artifact_id: id } = route.request().postDataJSON() as { artifact_id: string };
    await route.fulfill({ json: id === "art_deck_1" || (id === "art_deck_2" && state.conflictOnce === false && state.saved === "conflict")
      ? inventory(id, id === "art_deck_1" ? 1 : 2, "Quarterly review", table, chart, pages)
      : inventory(id, Number(id.slice(-1)), "Quarterly review for the whole year", table, chart, pages) });
  });
  await page.route("**/api/v1/sessions/*/results/presentation-edits", async (route: Route) => {
    state.saved = route.request().postDataJSON();
    if (state.conflictOnce) {
      state.conflictOnce = false;
      await route.fulfill({ status: 409, json: { detail: { error: { code: "version_target_not_latest", latest: {
        artifact_id: "art_deck_2", version_no: 2, created_at: "2026-11-24T10:16:02Z", produced_by: "agent", request_excerpt: "把第 2 页的标题改成区域明细",
      } } } } });
      return;
    }
    const next = state.latest === "art_deck_1" ? 2 : 3;
    state.latest = `art_deck_${next}`;
    await route.fulfill({ status: 201, json: { artifact_id: state.latest, version_no: next, filename: "Board.pptx", new_file: false, changes: [] } });
  });

  await page.goto("/");
  const composer = page.locator("textarea").first();
  await expect(composer).toBeVisible();
  await composer.fill("resultsworkspace");
  const sent = page.waitForResponse((response) => response.url().endsWith("/api/v1/chat/send"));
  await composer.press("Enter");
  const { turn_id: turnId, session_id: sessionId } = await (await sent).json();
  await expect(page.locator('[data-presentation="results-reading"]')).toHaveCount(1);
  // The mock stores Canvas, but its session-detail fixture otherwise has no turns.
  const detail = await (await page.request.get(`/api/v1/sessions/${sessionId}`)).json();
  await page.route(`**/api/v1/sessions/${sessionId}`, (route) => route.fulfill({ json: {
    ...detail, turns: [{ id: turnId, seq: 1, user_text: "resultsworkspace", intent: "analysis", status: "completed",
      started_at: "2026-11-24T00:00:00Z", ended_at: "2026-11-24T00:00:01Z", final_answer: "报告已生成",
      trace_steps: [], summary: null }],
  } }));
  await page.goto(`/?session=${sessionId}&result=art_deck_1&mode=preview`);
  return state;
}

test("a deck is edited in place, saved as version 2, checked for overflow and reopened without a second render", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  const state = await openDeck(page);
  await page.getByRole("button", { name: messages.artifact_viewer.edit_presentation, exact: true }).click();
  const editor = page.getByTestId("presentation-editor");
  await expect(editor).toBeVisible();
  await expect(editor.getByText(copy.editing_version.replace("{version}", "1"), { exact: false })).toBeVisible();
  await expect(editor.getByRole("img", { name: "Board.pptx · 第 1 页" })).toBeVisible();
  await page.screenshot({ path: test.info().outputPath("1-edit-mode.png") });

  // A click selects; a second click edits the text where it is drawn (design §13.3).
  const slide = editor.getByRole("img", { name: "Board.pptx · 第 1 页" });
  const before = await slide.boundingBox();
  const title = editor.getByRole("button", { name: /^文字：Quarterly review/ });
  await expect(editor.locator("[data-layout-ready]")).toHaveCount(1);
  await title.click();
  // The selection keeps the box's own field focused, hidden, for typing (design §13.4); the field then
  // stands for the element, so its hit leaves the accessibility tree and is reached by its key.
  await expect(editor.locator("textarea[data-armed]")).toBeFocused();
  await expect(title).toHaveCount(0);
  await editor.locator('[data-element-key="256:2"]').click();
  const field = editor.getByRole("textbox", { name: "文字：Quarterly review" });
  await expect(field).toBeFocused();
  await expect(field).not.toHaveAttribute("data-armed");
  await field.fill("Quarterly review for the whole year");
  await expect(editor.getByText("1 处未保存的修改")).toBeVisible();
  // Esc keeps the text and returns to the selection; the new text is painted on the slide.
  await page.keyboard.press("Escape");
  const selection = editor.getByRole("textbox", { name: /^文字：Quarterly review for the who.* · 未保存的第 1 处修改$/ });
  await expect(selection).toBeFocused();
  await expect(selection).toHaveAttribute("data-armed", "true");
  await expect(editor.locator("[data-painted='text']", { hasText: "Quarterly review for the whole year" })).toBeVisible();
  // The save bar's space was kept, so the slide did not move.
  expect((await slide.boundingBox())?.y).toBe(before?.y);
  await page.screenshot({ path: test.info().outputPath("2-pending.png") });

  await editor.getByRole("button", { name: copy.save_version, exact: true }).click();
  await expect(editor.getByText("已保存为第 2 版", { exact: false }).first()).toBeVisible();
  expect(state.saved).toEqual({
    artifact_id: "art_deck_1",
    edits: [{ kind: "text", key: "256:2", paragraph: 0, text: "Quarterly review for the whole year", expect: "Quarterly review" }],
  });
  await expect(editor.getByText("第 1 页「Quarterly review for the who…」比原来长，在新预览里超出了文本框。")).toBeVisible();
  await page.screenshot({ path: test.info().outputPath("3-saved.png") });

  await editor.getByRole("button", { name: copy.exit, exact: true }).click();
  await expect(page).toHaveURL(/result=art_deck_2/);
  await expect(editor).toHaveCount(0);
  // Editing started in the Results panel, so leaving returns there, not to the full view (design §13.1).
  await expect(page.getByRole("dialog", { name: "Board.pptx" })).toHaveCount(0);
  await expect(page.getByLabel("Board.pptx · 1", { exact: true })).toBeVisible();
  expect(state.renders.art_deck_2).toBe(1);
  await page.screenshot({ path: test.info().outputPath("4-after-exit.png") });

  // 「引用这一页」 under a page puts that slide in the Composer (design §13.2).
  await page.getByRole("button", { name: messages.artifact_viewer.reference_page_label.replace("{page}", "1"), exact: true }).click();
  await expect(page.getByText(messages.attachment.presentation_slide.replace("{page}", "1"), { exact: true }).first()).toBeVisible();
  await page.screenshot({ path: test.info().outputPath("5-page-reference.png") });
});

test("a table cell is edited on the slide where it is drawn", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await openDeck(page, { table: true });
  await page.getByRole("button", { name: messages.artifact_viewer.edit_presentation, exact: true }).click();
  const editor = page.getByTestId("presentation-editor");
  // The page's text is read from the render before its cells can be found.
  await expect(editor.locator("[data-layout-ready]")).toHaveCount(1);
  const table = editor.getByRole("button", { name: /^表格：/ });
  await table.click();
  await expect(editor.locator("input[data-armed]")).toBeFocused();
  await expect(editor.locator("aside").getByText(copy.in_place_table_hint)).toBeVisible();

  // A second click opens the cell under the pointer: 「2,905」 in row 2, column 2.
  // Selected, the table's hit leaves the accessibility tree for its field; it is found by its key.
  const box = await editor.locator('[data-element-key="256:3"]').boundingBox();
  if (!box) throw new Error("table hit area not laid out");
  await page.mouse.click(box.x + box.width * 0.75, box.y + box.height * 0.75);
  const cell = editor.getByRole("textbox", { name: /第 2 行第 2 列 · Sales$/ });
  await expect(cell).toBeFocused();
  await expect(cell).toHaveValue("2,905");
  await cell.fill("3,050");
  await page.screenshot({ path: test.info().outputPath("8-cell-in-place.png") });
  // Enter moves down; below the last row the field hides, back to the selection, and the value stays painted.
  await page.keyboard.press("Enter");
  await expect(editor.getByRole("textbox", { name: /^表格：.* · 未保存的第 1 处修改$/ })).toBeFocused();
  await expect(editor.locator("[data-painted='cell']", { hasText: "3,050" })).toBeVisible();
  await expect(editor.getByText("1 处未保存的修改")).toBeVisible();

  // Typing on the selected table continues in the cell it was in, at the end; Shift+Tab moves back.
  await page.keyboard.type("0");
  await expect(cell).toBeFocused();
  await expect(cell).toHaveValue("3,0500");
  await page.keyboard.press("Shift+Tab");
  const previous = editor.getByRole("textbox", { name: /第 2 行第 1 列 · Line$/ });
  await expect(previous).toBeFocused();
  await page.keyboard.type("x");
  await expect(previous).toHaveValue("Bx");
  await page.keyboard.press("Escape");
  await expect(editor.getByText("2 处未保存的修改")).toBeVisible();
  await page.screenshot({ path: test.info().outputPath("9-cells-painted.png") });
});

test("an input method types on a selected box from its first key", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await openDeck(page, { table: true });
  await page.getByRole("button", { name: messages.artifact_viewer.edit_presentation, exact: true }).click();
  const editor = page.getByTestId("presentation-editor");
  await expect(editor.locator("[data-layout-ready]")).toHaveCount(1);
  // Chrome's protocol composes text as an input method does (design §13.4, spike §11).
  const client = await page.context().newCDPSession(page);

  await editor.getByRole("button", { name: /^文字：Quarterly review/ }).click();
  await client.send("Input.imeSetComposition", { text: "huan", selectionStart: 4, selectionEnd: 4 });
  // The field shows as soon as composition starts, with the marked text at the end.
  const title = editor.getByRole("textbox", { name: /^文字：Quarterly review/ });
  await expect(title).toBeVisible();
  await expect(title).not.toHaveAttribute("data-armed");
  await expect(title).toHaveValue("Quarterly reviewhuan");
  await client.send("Input.insertText", { text: "环比" });
  await expect(title).toHaveValue("Quarterly review环比");
  await page.keyboard.press("Escape");
  await expect(editor.locator("[data-painted='text']", { hasText: "Quarterly review环比" })).toBeVisible();

  // Space on the selection starts editing, as on a button, without adding a space.
  await page.keyboard.press("Space");
  await expect(title).not.toHaveAttribute("data-armed");
  await expect(title).toHaveValue("Quarterly review环比");
  await page.keyboard.press("Escape");

  // Tab selects the next element, and the table takes the composition in its first cell. Keys
  // pressed while composing belong to the input method: ArrowDown does not move to the next row.
  await page.keyboard.press("Tab");
  await expect(editor.locator('[data-element-key="256:3"]')).toHaveAttribute("aria-pressed", "true");
  await client.send("Input.imeSetComposition", { text: "bei", selectionStart: 3, selectionEnd: 3 });
  const first = editor.getByRole("textbox", { name: /第 1 行第 1 列$/ });
  await page.keyboard.press("ArrowDown");
  await expect(first).toBeFocused();
  await client.send("Input.insertText", { text: "北" });
  await expect(first).toHaveValue("Line北");
  // Backspace on a selection changes nothing; the next Esc clears it.
  await page.keyboard.press("Escape");
  await page.keyboard.press("Backspace");
  await expect(editor.locator("[data-painted='cell']", { hasText: "Line北" })).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(editor.getByRole("button", { name: /^表格：.* · 未保存的第 2 处修改$/ })).toBeFocused();
  await expect(editor.getByRole("button", { name: /^表格：/ })).toHaveAttribute("aria-pressed", "false");
  await expect(editor.getByText("2 处未保存的修改")).toBeVisible();

  // Leaving the selection by keyboard and coming back keeps it ready for an input method.
  await editor.locator('[data-element-key="256:2"]').click();
  await page.keyboard.press("Shift+Tab");
  await page.keyboard.press("Tab");
  await expect(editor.locator("textarea[data-armed]")).toBeFocused();
  await client.send("Input.imeSetComposition", { text: "zhong", selectionStart: 5, selectionEnd: 5 });
  await client.send("Input.insertText", { text: "中" });
  await expect(title).toHaveValue("Quarterly review环比中");
  // Tab while editing moves on to the next element, as from the selection, not back to the first.
  await page.keyboard.press("Tab");
  await expect(editor.locator('[data-element-key="256:3"]')).toHaveAttribute("aria-pressed", "true");
  await page.screenshot({ path: test.info().outputPath("10-input-method.png") });
});

test("slide marks stay visible, focus stays in the editor, and a low chart's data fits the page", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 760 });
  await openDeck(page, { table: true, chart: true });
  await page.getByRole("button", { name: messages.artifact_viewer.edit_presentation, exact: true }).click();
  const editor = page.getByTestId("presentation-editor");
  await expect(editor.locator("[data-layout-ready]")).toHaveCount(1);
  const border = (key: string) => editor.locator(`[data-element-key="${key}"]`).evaluate((node) => {
    const style = getComputedStyle(node);
    return `${style.borderTopWidth} ${style.borderTopStyle} ${style.borderTopColor}`;
  });
  // At rest, editable elements show the dashed hint the guide talks about.
  expect(await border("256:2")).toMatch(/^1px dashed (?!rgba\(0, 0, 0, 0\))/);

  // The selection is a solid 2 px blue outline, an unsaved element amber; both carry a white ring
  // and keep their colours in every app theme, because the slide does.
  const title = editor.getByRole("button", { name: /^文字：Quarterly review/ });
  await title.click();
  await expect.poll(() => border("256:2")).toBe("2px solid rgb(23, 84, 219)");
  await page.keyboard.type("!");
  await page.keyboard.press("Escape");
  await page.keyboard.press("Escape");
  await expect.poll(() => border("256:2")).toBe("2px solid rgb(180, 83, 9)");

  // The chart's data opens inside the page even though the chart sits low on it, and closes once
  // focus leaves it.
  const chart = editor.getByRole("button", { name: /^图表数据：/ });
  await chart.click();
  await chart.click();
  const popover = editor.getByRole("dialog", { name: /Sales chart/ });
  await expect(popover).toBeVisible();
  const slide = await editor.getByRole("img", { name: "Board.pptx · 第 1 页" }).boundingBox();
  const placed = await popover.boundingBox();
  expect(placed && slide && placed.y + placed.height).toBeLessThanOrEqual((slide?.y ?? 0) + (slide?.height ?? 0));
  await expect(popover.getByRole("button", { name: "完成" })).toBeInViewport();
  await popover.getByRole("button", { name: "完成" }).focus();
  await page.keyboard.press("Tab");
  await expect(popover).toHaveCount(0);

  // Discarding more than one change asks first; afterwards focus rests on the page.
  await editor.locator('[data-element-key="256:3"]').click();
  await page.keyboard.type("s");
  await page.keyboard.press("Escape");
  await expect(editor.getByText("2 处未保存的修改")).toBeVisible();
  await editor.getByRole("button", { name: copy.discard, exact: true }).click();
  const confirm = page.getByRole("alertdialog").or(page.getByRole("dialog", { name: messages.canvas_object.guard_title }));
  await expect(confirm).toContainText("这 2 处修改还没保存");
  await confirm.getByRole("button", { name: messages.canvas_object.guard_discard, exact: true }).click();
  await expect(editor.getByText("2 处未保存的修改")).toHaveCount(0);
  await expect(editor.getByRole("group", { name: "编辑模式 · 第 1 / 1 页" })).toBeFocused();

  // After a save, focus is on the summary of what was saved, and the slide stays where it was (design §13.7).
  await editor.locator('[data-element-key="256:2"]').click();
  await page.keyboard.type("!");
  await page.keyboard.press("Escape");
  const beforeSave = await editor.getByRole("img", { name: "Board.pptx · 第 1 页" }).boundingBox();
  await editor.getByRole("button", { name: copy.save_version, exact: true }).click();
  await expect(editor.getByText("已保存为第 2 版", { exact: false }).first()).toBeVisible();
  await expect(page.locator(":focus")).toContainText("1 处修改已存为第 2 版");
  expect((await editor.getByRole("img", { name: "Board.pptx · 第 1 页" }).boundingBox())?.y).toBe(beforeSave?.y);
});

test("several pages of a deck gather in one Composer chip and are sent as one reference", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await openDeck(page, { pages: 3 });
  const pageButton = (number: number) => page.getByRole("button", { name: new RegExp(`^(引用这一页|已引用) · 第 ${number} 页$`) });
  await expect(pageButton(3)).toBeVisible();

  // Each 「引用这一页」 is a toggle: pages 1 and 3 join one chip; pressing page 1 again removes it.
  await pageButton(1).click();
  await pageButton(3).click();
  await expect(page.getByText("第 1、3 页", { exact: true }).first()).toBeVisible();
  await expect(pageButton(1)).toHaveAttribute("aria-pressed", "true");
  await expect(pageButton(1)).toHaveText(messages.artifact_viewer.referenced_page);
  await expect(pageButton(2)).toHaveAttribute("aria-pressed", "false");
  await pageButton(1).click();
  await expect(page.getByText("第 3 页", { exact: true }).first()).toBeVisible();
  await expect(pageButton(1)).toHaveAttribute("aria-pressed", "false");
  await pageButton(2).click();
  await page.screenshot({ path: test.info().outputPath("11-pages-chip.png") });

  // Send carries one reference with a version 2 page set; afterwards the pages' buttons turn back.
  const sent = page.waitForRequest((request) => request.url().endsWith("/api/v1/chat/send") && request.method() === "POST");
  const composer = page.locator("textarea").first();
  await composer.fill("这两页分别讲什么");
  await composer.press("Enter");
  const body = (await sent).postDataJSON() as { attachment_refs?: Array<{ fileId: string; presentationSelection?: unknown }> };
  const deckRefs = (body.attachment_refs ?? []).filter((ref) => ref.presentationSelection);
  expect(deckRefs).toEqual([expect.objectContaining({
    presentationSelection: { version: 2, artifactId: deckRefs[0]?.fileId, slides: [{ slideIndex: 2 }, { slideIndex: 3 }] },
  })]);
  await expect(pageButton(2)).toHaveAttribute("aria-pressed", "false");
  await expect(pageButton(3)).toHaveAttribute("aria-pressed", "false");
});

test("entering edit mode puts focus on the page, so PageDown turns it at once", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await openDeck(page, { pages: 3 });
  await page.getByRole("button", { name: messages.artifact_viewer.edit_presentation, exact: true }).click();
  const editor = page.getByTestId("presentation-editor");
  await expect(editor.getByRole("group", { name: "编辑模式 · 第 1 / 3 页" })).toBeFocused();
  await page.keyboard.press("PageDown");
  await expect(editor.locator('[data-rail-page="2"]')).toHaveAttribute("aria-current", "page");
});

test("on a phone, referencing pages stays on Results and a toast leads to the chat", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await openDeck(page, { pages: 2 });
  const pageButton = (number: number) => page.getByRole("button", { name: new RegExp(`^(引用这一页|已引用) · 第 ${number} 页$`) });
  await pageButton(1).click();
  await pageButton(2).click();
  // Still on Results, so the next page can be picked; the toast says how many and leads to the chat.
  await expect(pageButton(2)).toHaveAttribute("aria-pressed", "true");
  const toast = page.getByText(messages.artifact_viewer.pages_referenced.replace("{count}", "2"), { exact: true });
  await expect(toast).toBeVisible();
  // One toast, kept current, not one per page.
  await expect(page.getByText(/^已引用 \d+ 页$/)).toHaveCount(1);
  await page.screenshot({ path: test.info().outputPath("12-phone-pages-toast.png") });
  await page.getByRole("button", { name: messages.artifact_viewer.go_to_chat, exact: true }).last().click();
  await expect(page.getByText("第 1、2 页", { exact: true }).first()).toBeVisible();
});

test("the narrow desktop layout swaps the page rail for a page switcher", async ({ page }) => {
  await page.setViewportSize({ width: 1100, height: 800 });
  await openDeck(page);
  await page.getByRole("button", { name: messages.artifact_viewer.edit_presentation, exact: true }).click();
  const editor = page.getByTestId("presentation-editor");
  await expect(editor.getByText("第 1 / 1 页")).toBeVisible();
  await editor.getByRole("button", { name: /^图片：Logo/ }).click();
  await expect(editor.getByText(copy.unsupported_title_picture)).toBeVisible();
  await page.screenshot({ path: test.info().outputPath("5-narrow-unsupported.png") });
  // This table's text is not on the render, so its cells are edited in the inspector.
  await editor.getByRole("button", { name: /^表格：/ }).click();
  await editor.getByRole("textbox", { name: "第 2 行第 2 列 · Sales" }).fill("3,050");
  await page.screenshot({ path: test.info().outputPath("6-narrow-table.png") });
  await page.keyboard.press("Escape");
  await expect(editor.getByText("第 1 页可修改的内容")).toBeVisible();
  await expect(editor.getByRole("button", { name: /^表格：.* · 未保存的第 1 处修改$/ })).toBeFocused();
  await page.keyboard.press("Escape");
  await expect(page.getByRole("button", { name: messages.canvas_object.guard_keep, exact: true })).toBeVisible();
  await page.screenshot({ path: test.info().outputPath("7-leave-guard.png") });
  await page.getByRole("button", { name: messages.canvas_object.guard_keep, exact: true }).click();
  await expect(editor).toBeVisible();
});

test("on a phone the slides are a list and elements are edited in sheets", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  const state = await openDeck(page);
  await page.getByRole("button", { name: messages.artifact_viewer.edit_presentation, exact: true }).click();
  const editor = page.getByTestId("presentation-editor");
  await expect(editor).toBeVisible();
  await expect(editor.getByText(copy.guide_text_touch)).toBeVisible();
  await page.screenshot({ path: test.info().outputPath("8-phone-edit-mode.png") });

  await editor.getByRole("button", { name: "修改第 1 页 · 2 处可改" }).click();
  const list = page.getByRole("dialog", { name: "第 1 页可修改的内容" });
  await expect(list).toBeVisible();
  await page.screenshot({ path: test.info().outputPath("9-phone-list.png") });
  await list.getByRole("button", { name: /Quarterly review/ }).click();

  const sheet = page.getByRole("dialog", { name: "Quarterly review" });
  const field = sheet.getByRole("textbox", { name: copy.text_label });
  await expect(field).toBeVisible();
  expect(await field.evaluate((node) => getComputedStyle(node).fontSize)).toBe("16px");
  await field.fill("Quarterly review 2026");
  await page.screenshot({ path: test.info().outputPath("10-phone-text-sheet.png") });
  await sheet.getByRole("button", { name: copy.done, exact: true }).click();
  await expect(sheet).toHaveCount(0);
  await expect(editor.getByText("1 处已改，未保存")).toBeVisible();

  await editor.getByRole("button", { name: copy.save_version, exact: true }).click();
  await expect(editor.getByText("已保存为第 2 版", { exact: false }).first()).toBeVisible();
  expect(state.saved).toMatchObject({ edits: [{ kind: "text", text: "Quarterly review 2026", expect: "Quarterly review" }] });
  await page.screenshot({ path: test.info().outputPath("11-phone-saved.png") });
});

test("a newer version while editing keeps the editor and its edits, and the save re-applies them", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  const state = await openDeck(page);
  await page.getByRole("button", { name: messages.artifact_viewer.edit_presentation, exact: true }).click();
  const editor = page.getByTestId("presentation-editor");
  await editor.getByRole("button", { name: /^表格：/ }).click();
  await editor.getByRole("textbox", { name: "第 2 行第 2 列" }).fill("3,050");

  // An agent Turn saves version 2 meanwhile; returning to the window refreshes Results, which now lists version 2.
  state.latest = "art_deck_2";
  state.conflictOnce = true;
  state.saved = "conflict";
  await page.evaluate(() => { window.dispatchEvent(new Event("blur")); window.dispatchEvent(new Event("focus")); document.dispatchEvent(new Event("visibilitychange")); });
  await page.waitForTimeout(1500);
  await expect(editor).toBeVisible();
  await expect(editor.getByText("1 处未保存的修改")).toBeVisible();

  await editor.getByRole("button", { name: copy.save_version, exact: true }).click();
  await expect(editor.getByText(/AI 在 .* 把这份文件存成了第 2 版（对话「把第 2 页的标题改成区域明细」）/)).toBeVisible();
  await expect(editor.getByText(copy.conflict_applicable, { exact: true })).toBeVisible();
  await page.screenshot({ path: test.info().outputPath("12-conflict.png") });
  await editor.getByRole("button", { name: "在第 2 版上重新套用 1 处" }).click();
  await expect(editor.getByText("已在第 2 版上套用 1 处修改，还没保存。")).toBeVisible();
  await editor.getByRole("button", { name: copy.save_version, exact: true }).click();
  await expect(editor.getByText("已保存为第 3 版", { exact: false }).first()).toBeVisible();
  expect(state.saved).toMatchObject({ artifact_id: "art_deck_2", edits: [{ kind: "cell", text: "3,050", expect: "2,905" }] });
});
