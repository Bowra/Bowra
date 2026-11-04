import { expect, test, type Route } from "@playwright/test";
import { mockAuthedApp } from "./_app-mocks";

const rows = [
  { unit: "OBU", manager: "Manager", region: "South", decile: 9, hospitals: 14 },
  { unit: "OBU", manager: "Manager", region: "South", decile: 10, hospitals: 12 },
  { unit: "OBU", manager: "Manager", region: "South", decile: 2, hospitals: 1 },
];
const columns = ["unit", "manager", "region", "decile", "hospitals"].map((key) => ({
  key, header: key, type: ["decile", "hospitals"].includes(key) ? "numeric" : "categorical",
}));
const json = (body: unknown) => ({ contentType: "application/json", body: JSON.stringify(body) });

test("saved explicit tables and Decile plot retain fields and constant grouping after refresh", async ({ page }) => {
  await mockAuthedApp(page);
  await page.route("**/api/v1/sessions/s_explicit", (route: Route) => route.fulfill(json({
    session: { id: "s_explicit", workspace_id: "ws_smoke", title: "Explicit presentation", status: "done", started_at: "2026-11-02T00:00:00Z", last_active_at: "2026-11-02T00:00:00Z", cost_usd: 0, tokens_total: 0, intents: ["analysis"] },
    turns: [{ id: "t_explicit", seq: 1, user_text: "Show the prepared result", intent: "analysis", status: "done", started_at: "2026-11-02T00:00:00Z", ended_at: "2026-11-02T00:00:00Z", final_answer: "Ready", agent_type: "lead", attachments: [], summary: null }],
  })));
  await page.route("**/api/v1/turns/t_explicit/events", (route: Route) => route.fulfill(json({ events: [] })));
  await page.route("**/api/v1/canvas/by-session/s_explicit**", (route: Route) => {
    if (new URL(route.request().url()).pathname.endsWith("/artifacts")) return route.fulfill(json({ items: [] }));
    return route.fulfill(json({
      canvas: { id: "cv_explicit", sessionId: "s_explicit", workspaceId: "ws_smoke", title: "Explicit presentation", status: "done", createdAt: "2026-11-02T00:00:00Z", updatedAt: "2026-11-02T00:00:00Z" },
      sections: [
        { id: "h1_explicit", kind: "h1", text: "Explicit presentation" },
        { id: "all_fields", kind: "chart", spec: { kind: "table", title: "All fields", row_count: 3, rows, columns, explicit_presentation: { version: 1, mode: "table", columns: columns.map((column) => column.key) } } },
        { id: "exact_fields", kind: "chart", spec: { kind: "table", title: "Exact fields", row_count: 3, rows: rows.map(({ region, decile, hospitals }) => ({ region, decile, hospitals })), columns: [columns[2], columns[3], columns[4]], explicit_presentation: { version: 1, mode: "table", columns: ["region", "decile", "hospitals"] } } },
        { id: "decile_plot", kind: "chart", spec: { kind: "bar", title: "Hospitals by Decile", row_count: 3, rows: rows.map(({ region, decile, hospitals }) => ({ region, decile: String(decile), hospitals })), columns: [{ ...columns[3], type: "categorical", role: "x" }, { ...columns[2], role: "color" }, { ...columns[4], role: "y", y_axis_id: 0 }], show_legend: true, show_data_labels: true, explicit_presentation: { version: 1, mode: "plot", axes: { x: "decile", y: ["hospitals"], x_scale: "category" }, group_by: ["region"] } } },
      ], sourceRefs: [],
    }));
  });
  await page.addInitScript(() => {
    const fillText = CanvasRenderingContext2D.prototype.fillText;
    CanvasRenderingContext2D.prototype.fillText = function (text, ...parameters) {
      const canvas = this.canvas as HTMLCanvasElement & { explicitLabels?: string[] };
      (canvas.explicitLabels ??= []).push(String(text));
      return fillText.call(this, text, ...parameters);
    };
  });
  await page.goto("/?session=s_explicit&report=1");
  for (let load = 0; load < 2; load++) {
    await expect(page.getByRole("heading", { name: "Explicit presentation", level: 1 })).toBeVisible({ timeout: 30_000 });
    const all = page.getByRole("region", { name: "All fields", exact: true });
    const exact = page.getByRole("region", { name: "Exact fields", exact: true });
    await expect(all.getByRole("columnheader")).toHaveText(["unit", "manager", "region", "decile", "hospitals"]);
    await expect(exact.getByRole("columnheader")).toHaveText(["region", "decile", "hospitals"]);
    await expect(exact.getByRole("cell", { name: "South", exact: true })).toHaveCount(3);
    const canvas = page.getByRole("figure", { name: "Hospitals by Decile", exact: true }).locator("canvas");
    await expect(canvas).toBeVisible();
    await expect.poll(() => canvas.evaluate((element) => (element as HTMLCanvasElement & { explicitLabels?: string[] }).explicitLabels ?? [])).toEqual(expect.arrayContaining(["9", "10", "2", "14", "12", "South"]));
    if (load === 0) await page.reload();
  }
});
