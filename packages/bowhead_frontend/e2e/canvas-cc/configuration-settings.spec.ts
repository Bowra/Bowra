import { expect, test } from "@playwright/test";
import { mobilePaneTab } from "../_mobile-pane";
import { canvasWeek, chooseCanvasWeek, expectCanvasWeek } from "../_canvas-week";
import { mockAuthedApp } from "../_app-mocks";

for (const width of [412, 1440]) {
  for (const recover of [false, true]) {
  test(`settings Tool ${recover ? "replays with explicit saved review" : "opens the original page"} and confirmation reads back at ${width}`, async ({ page }) => {
    const runtimeErrors: string[] = [];
    page.on("pageerror", (error) => runtimeErrors.push(error.message));
    // The development-only Next.js indicator overlaps the mobile confirmation action.
    // Keep normal pointer hit testing while checking runtime errors separately.
    await page.addInitScript(() => {
      document.addEventListener("DOMContentLoaded", () => {
        const style = document.createElement("style");
        style.textContent = "nextjs-portal { display: none !important; }";
        document.head.appendChild(style);
      });
    });
    await page.setViewportSize({ width, height: 1000 });
    await mockAuthedApp(page);
    await page.route("**/api/v1/auth/me", (route) => route.fulfill({ json: {
      id: "u_smoke", email: "smoke@example.com", name: "Smoke User", tier: "pro", team_id: "team_smoke",
      auth_session_generation: "auth-generation-smoke-1", capabilities: { settings_pages_canvas: true, configuration_agent: true },
    } }));
    await page.route("**/api/v1/auth/me/workspaces", (route) => route.fulfill({ json: {
      items: [{ id: "ws_smoke", workspace_id: "ws_smoke", name: "Original Space", role: "admin" }], active_id: "ws_smoke",
    } }));
    await page.route("**/api/v1/workspace-settings/ws_smoke/metadata", (route) => route.fulfill({ json: { name: "Original Space", description: "", revision: 1 } }));
    let week = "monday";
    let writes = 0;
    let status = "proposed";
    await page.route("**/api/v1/workspace-settings/ws_smoke", (route) => route.fulfill({ json: { settings: { week_start_day: week } } }));
    const rows = [{ field: "week_start_day", from: "monday", to: "sunday", from_unset: false }];
    const receipt = () => ({ proposal_id: "ccd_browser", form_id: "workspace_i18n", owner_user_id: "u_smoke", team_id: "team_smoke", workspace_id: "ws_smoke", status });
    await page.route("**/api/v1/config-proposals/ccd_browser", (route) => route.fulfill({ json: receipt() }));
    await page.route("**/api/v1/config-proposals/ccd_browser/settings-review", (route) => route.fulfill({ json: { ...receipt(), turn_id: "t_settings", rows, notes: {}, settings_target: null } }));
    await page.route("**/api/v1/config-proposals/ccd_browser/apply", (route) => {
      writes += 1;
      week = "sunday";
      status = "applied";
      return route.fulfill({ json: receipt() });
    });
    await page.route("**/api/v1/chat/send", (route) => route.fulfill({ json: { turn_id: "t_settings", session_id: "s_settings", stream_url: "/api/v1/chat/stream/t_settings" } }));
    const target = { page: "workspace_i18n", user_id: "u_smoke", team_id: "team_smoke", workspace_id: "ws_smoke" };
    const frames: [string, Record<string, unknown>][] = [
      ["turn_started", { seq: 1, turn_id: "t_settings", session_id: "s_settings", user_text: "Set weeks to start on Sunday." }],
      ["configuration.canvas_open", { seq: 2, turn_id: "t_settings", target }],
      ["config.lite_propose", { seq: 3, turn_id: "t_settings", ...receipt(), rows, notes: {} }],
      ["final_answer", { seq: 4, turn_id: "t_settings", text: "Please confirm the suggestion." }],
      ["done", { seq: 5, turn_id: "t_settings" }],
    ];
    await page.route("**/api/v1/chat/stream/t_settings*", (route) => route.fulfill({ contentType: "text/event-stream", body: frames.map(([name, data]) => `event: ${name}\ndata: ${JSON.stringify(data)}\n\n`).join("") }));
    await page.route("**/api/v1/turns/t_settings/events", (route) => route.fulfill({ json: {
      events: frames.map(([event_type, payload]) => ({ seq: payload.seq, event_type, payload })),
    } }));
    await page.route("**/api/v1/sessions/s_settings", (route) => route.fulfill({ json: {
      session: { id: "s_settings", workspace_id: "ws_smoke", title: "Settings review", status: "completed" },
      turns: [{ id: "t_settings", seq: 1, user_text: "Set weeks to start on Sunday.", status: "completed", final_answer: "Please confirm the suggestion.", attachments: [] }],
      chat_execution: { write_protocol: "legacy_v0", config_generation: "test", readiness_generation: "test" },
    } }));
    await page.goto("/");
    const composer = page.locator("textarea").first();
    await expect(composer).toBeVisible();
    await composer.fill("Set weeks to start on Sunday.");
    await composer.press("Enter");
    const weekControl = canvasWeek(page);
    await expectCanvasWeek(page, "monday", { timeout: 20_000 });
    expect(writes).toBe(0);
    // The hosted page is the Canvas tab panel of the 空间偏好 object.
    const hosted = page.getByRole("tabpanel", { name: /^空间偏好/ });
    await expect(hosted.getByTestId("lite-propose")).toBeVisible();
    if (recover) {
      await page.reload();
      await expectCanvasWeek(page, "monday");
      await expect(page.getByTestId("lite-propose")).toHaveCount(0);
      expect(writes).toBe(0);
      const review = page.getByRole("button", { name: "重新审阅已保存建议", exact: true });
      // The saved card stays in Chat until explicitly admitted to the reopened page.
      if (width < 1024) await page.getByRole("tab", { name: "对话", exact: true }).click();
      await review.click();
      expect(writes).toBe(0);
      if (width < 1024) await mobilePaneTab(page).click();
      await expect(hosted.getByTestId("lite-propose")).toBeVisible();
    }
    const confirmation = width === 1440 ? page.getByTestId("lite-propose").first() : hosted.getByTestId("lite-propose");
    await expect(confirmation).toBeVisible();
    await confirmation.getByRole("button", { name: "应用", exact: true }).click();
    await expectCanvasWeek(page, "sunday");
    expect(writes).toBe(1);
    await expect(hosted.getByTestId("lite-applied")).toBeVisible();
    expect(runtimeErrors).toEqual([]);
  });
  }
}
