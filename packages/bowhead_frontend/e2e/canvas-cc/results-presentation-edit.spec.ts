import { expect, test, type Page, type Route } from "@playwright/test";

import messages from "../../edition/shared-messages/zh-CN.json";
import { parseResultsQueryResponse, type ResultItemDTO, type ResultsQueryRequest } from "../../lib/results-api";

// In-place deck editing (docs/sprints/pptx-inline-edit-2026-11-24/tech-design.md §7): Edit opens the
// editor in the full view over the existing PDF.js render, a save becomes the next version, the
// overflow check reads the new render, and leaving opens the new version without rendering it again.

const apiPort = process.env.PLAYWRIGHT_API_PORT ?? "18080";
const copy = messages.presentation_edit;
const PPTX = "application/vnd.openxmlformats-officedocument.presentationml.presentation";

/** A one-slide 960×540 PDF whose title sits where the inventory says the title box is. */
function slidePdf(lines: string[]): Buffer {
  const text = lines.map((line, index) => `BT /F1 36 Tf 60 ${470 - index * 50} Td (${line}) Tj ET`).join("\n") + "\n";
  const objects = [
    "<< /Type /Catalog /Pages 2 0 R >>",
    "<< /Type /Pages /Count 1 /Kids [4 0 R] >>",
    "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 960 540] /Resources << /Font << /F1 3 0 R >> >> /Contents 5 0 R >>",
    `<< /Length ${Buffer.byteLength(text)} >>\nstream\n${text}endstream`,
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

function inventory(id: string, version: number, title: string) {
  return {
    artifact_id: id, version_no: version, edit_mode: "version", slide_width: 12192000, slide_height: 6858000, field_token: "￼",
    slides: [{ slide_id: 256, index: 1, inherited_text: false, elements: [
      { key: "256:2", shape_id: 2, name: "Title 1", kind: "text", box: [0.05, 0.08, 0.6, 0.12], editable: true, reason: null,
        paragraphs: [{ text: title, mixed_style: false, fields: 0 }] },
      { key: "256:3", shape_id: 3, name: "Table 2", kind: "table", box: [0.05, 0.4, 0.5, 0.3], editable: true, reason: null,
        rows: [[{ text: "Line", editable: true, reason: null }, { text: "Sales", editable: true, reason: null }],
          [{ text: "B", editable: true, reason: null }, { text: "2,905", editable: true, reason: null }]] },
      { key: "256:4", shape_id: 4, name: "Logo", kind: "picture", box: [0.85, 0.82, 0.1, 0.12], editable: false, reason: "picture" },
    ] }],
  };
}

async function openDeck(page: Page) {
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
      await route.fulfill({ contentType: "application/pdf", body: slidePdf(lines) });
      return;
    }
    await route.fulfill({ json: { family: "presentation", representation: "details", revision: `details-${id}`, details: {
      id, filename: "Board.pptx", mime: PPTX, size: 4096, source: "pptx", created_at: "2026-11-24T00:00:00Z", kind: "deliverable",
    } } });
  });
  await page.route("**/api/v1/sessions/*/results/presentation-edit/inventory", async (route: Route) => {
    const { artifact_id: id } = route.request().postDataJSON() as { artifact_id: string };
    await route.fulfill({ json: id === "art_deck_1" || (id === "art_deck_2" && state.conflictOnce === false && state.saved === "conflict")
      ? inventory(id, id === "art_deck_1" ? 1 : 2, "Quarterly review")
      : inventory(id, Number(id.slice(-1)), "Quarterly review for the whole year") });
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

  await editor.getByRole("button", { name: /^文字：Quarterly review/ }).click();
  const field = editor.getByRole("textbox", { name: copy.text_label });
  await expect(field).toBeFocused();
  await field.fill("Quarterly review for the whole year");
  await expect(editor.getByText("1 处未保存的修改")).toBeVisible();
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
  await expect(page.getByRole("dialog", { name: "Board.pptx" })).toBeVisible();
  await expect(page.getByLabel("Board.pptx · 1", { exact: true })).toBeVisible();
  expect(state.renders.art_deck_2).toBe(1);
  await page.screenshot({ path: test.info().outputPath("4-after-exit.png") });
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
  await editor.getByRole("button", { name: /^表格：/ }).click();
  await editor.getByRole("textbox", { name: "第 2 行第 2 列" }).fill("3,050");
  await page.screenshot({ path: test.info().outputPath("6-narrow-table.png") });
  await page.keyboard.press("Escape");
  await expect(editor.getByText("第 1 页可修改的内容")).toBeVisible();
  await expect(editor.getByRole("button", { name: /^表格：.* · 未保存$/ })).toBeFocused();
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
