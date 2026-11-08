import { expect, test } from "@playwright/test";
import { mockAuthedApp } from "./_app-mocks";
import messages from "../edition/shared-messages/zh-CN.json";

/** A real nine-page PDF exercises the existing PDF.js renderer without an Office worker. */
function previewPdf(): Buffer {
  const objects = [
    "<< /Type /Catalog /Pages 2 0 R >>",
    `<< /Type /Pages /Count 9 /Kids [${Array.from({ length: 9 }, (_, i) => `${4 + i * 2} 0 R`).join(" ")}] >>`,
    "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
  ];
  for (let index = 0; index < 9; index++) {
    objects.push(`<< /Type /Page /Parent 2 0 R /MediaBox [0 0 480 270] /Resources << /Font << /F1 3 0 R >> >> /Contents ${5 + index * 2} 0 R >>`);
    const content = `BT /F1 24 Tf 40 180 Td (Synthetic slide ${index + 1}) Tj ET\n`;
    objects.push(`<< /Length ${Buffer.byteLength(content)} >>\nstream\n${content}endstream`);
  }
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

for (const width of [412, 1440]) {
  test(`PPT busy recovery keeps nine real pages through Full view at ${width}`, async ({ page }) => {
    await page.setViewportSize({ width, height: 1000 });
    await mockAuthedApp(page);
    await page.route("**/api/v1/chat/sessions/*/reference-queue?*", (route) => route.fulfill({ json: { items: [] } }));
    const artifact = {
      id: "art_preview_busy", filename: "Board.pptx", size: 1024, source: "upload",
      mime: "application/vnd.openxmlformats-officedocument.presentationml.presentation",
      created_at: "2026-11-08T00:00:00Z", preview_available: true,
    };
    await page.route("**/api/v1/results/capabilities", (route) => route.fulfill({ json: {
      dto_version: 1, workspace: true, complete_list: false, scale_nav: false, max_page_size: 50,
    } }));
    await page.route("**/api/v1/sessions/s_preview_busy/artifacts*", (route) => route.fulfill({ json: { items: [artifact] } }));
    await page.route("**/api/v1/files/art_preview_busy/representations/details", (route) => route.fulfill({ json: {
      representation: "details", family: "presentation", revision: "fixture-1", details: artifact,
    } }));
    let conversions = 0;
    let sends = 0;
    await page.route("**/api/v1/chat/send", (route) => { sends += 1; return route.fulfill({ status: 500 }); });
    await page.route("**/api/v1/files/art_preview_busy/representations/pptx-pdf", (route) => {
      conversions += 1;
      return conversions === 1 ? route.fulfill({ status: 503, headers: { "Retry-After": "2" }, json: {
        detail: { error: { code: "artifact_representation_busy", message: "Private worker diagnostics" } },
      } }) : route.fulfill({ contentType: "application/pdf", body: previewPdf() });
    });
    await page.goto("/?session=s_preview_busy&result=art_preview_busy&mode=preview");
    const copy = messages.artifact_viewer;
    await expect(page.getByRole("status").filter({ hasText: copy.presentation_preview_busy })).toBeVisible();
    await expect(page.getByText(copy.presentation_preview_failed, { exact: true })).toBeHidden();
    await expect(page.getByText("Private worker diagnostics", { exact: true })).toBeHidden();
    expect(conversions).toBe(1);
    await page.getByRole("button", { name: copy.retry, exact: true }).click();
    await expect(page.getByLabel("Board.pptx · 9", { exact: true })).toBeVisible();
    expect(conversions).toBe(2);
    await page.getByRole("button", { name: copy.open_full, exact: true }).click();
    await expect(page.getByLabel("Board.pptx · 9", { exact: true })).toBeVisible();
    await page.screenshot({ path: test.info().outputPath("ppt-full-view.png") });
    await page.keyboard.press("Escape");
    await expect(page.getByRole("button", { name: copy.open_full, exact: true })).toBeFocused();
    expect(conversions).toBe(2);
    expect(sends).toBe(0);
  });
}
