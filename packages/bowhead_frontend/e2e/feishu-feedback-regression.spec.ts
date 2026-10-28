import { expect, test, type Locator, type Page } from "@playwright/test";

import { mockAuthedApp } from "./_app-mocks";

const PNG_1X1 = Buffer.from(
  "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII=",
  "base64",
);

function json(body: unknown, status = 200) {
  return { status, contentType: "application/json", body: JSON.stringify(body) };
}

async function openSelectionToolbar(
  page: Page,
  opts: { allowEditorFallback?: boolean } = {},
): Promise<Locator> {
  const allowEditorFallback = opts.allowEditorFallback ?? true;
  const paragraph = page.locator('[data-section-kind="narrative"] p').first();
  await expect(paragraph).toBeVisible();
  await paragraph.scrollIntoViewIfNeeded();
  const box = await paragraph.boundingBox();
  expect(box).not.toBeNull();
  const midY = box!.y + Math.min(18, box!.height / 2);
  await page.mouse.move(box!.x + 12, midY);
  await page.mouse.down();
  await page.mouse.move(box!.x + Math.min(220, box!.width - 4), midY, { steps: 8 });
  await page.mouse.up();

  const toolbar = page.getByRole("toolbar", { name: /文本格式|Text formatting/ });
  if (!(await toolbar.isVisible().catch(() => false)) && allowEditorFallback) {
    const selected = await page.evaluate(() => {
      const editor = (window as unknown as {
        __bowheadEditor?: {
          state: {
            doc: {
              descendants: (cb: (node: unknown, pos: number) => boolean | void) => void;
              textBetween: (from: number, to: number, separator?: string) => string;
            };
          };
          chain: () => {
            focus: () => {
              setTextSelection: (range: { from: number; to: number }) => { run: () => boolean };
            };
          };
        };
      }).__bowheadEditor;
      if (!editor) return "";
      let range: { from: number; to: number } | null = null;
      editor.state.doc.descendants((node: unknown, pos: number) => {
        const n = node as {
          type?: { name?: string };
          descendants?: (cb: (child: unknown, childPos: number) => boolean | void) => void;
        };
        if (range || n.type?.name !== "section_narrative" || !n.descendants) return !range;
        n.descendants((child: unknown, childPos: number) => {
          const c = child as { isText?: boolean; text?: string };
          if (!c.isText || !c.text || !c.text.trim()) return true;
          const start = Math.max(0, c.text.search(/\S/));
          const end = Math.min(c.text.length, start + 12);
          range = { from: pos + 1 + childPos + start, to: pos + 1 + childPos + end };
          return false;
        });
        return false;
      });
      const selectionRange = range as { from: number; to: number } | null;
      if (!selectionRange) return "";
      editor.chain().focus().setTextSelection(selectionRange).run();
      return editor.state.doc.textBetween(selectionRange.from, selectionRange.to, "\n");
    });
    expect(selected.trim().length).toBeGreaterThan(0);
  }

  await expect(toolbar).toBeVisible();
  return toolbar;
}

test.describe("Feishu feedback regression dogfood", () => {
  // hermetic: the /spike/p31 fixture embeds BI cards (datarc_iframe) on a synthetic host. Abort those
  // requests, and any real BI domain, so a hanging external load never blocks the page load event.
  test.beforeEach(async ({ page }) => {
    await page.route(/bi\.example\.invalid|(?:demo\.)?datarc\.cn/, (route) => route.abort());
  });

  test("running composer keeps a steer draft when delivery is refused in mock mode", async ({ page }) => {
    await mockAuthedApp(page);
    await page.goto("/?scenario=long");

    const textarea = page.locator("textarea").first();
    await expect(page.getByRole("button", { name: /停止|Stop/ })).toBeVisible();
    await expect(textarea).toBeEnabled();

    await textarea.fill("keep this running draft");
    await textarea.press("Enter");

    await expect(textarea).toHaveValue("keep this running draft");
  });

  // Since c3451cf1c, report=1 no longer universally read-only: probe session details once to distinguish owner (full hydration + report maximization)
  // from recipient (403 → read-only report). Recipient allowed this one probe only; session-scoped artifact reads still forbidden.
  test("real shared report link falls back to report-only when owner session detail is forbidden", async ({ page }) => {
    await mockAuthedApp(page);
    let sessionDetailRequests = 0;
    let sessionArtifactRequests = 0;
    const canvasRequests: string[] = [];

    await page.route("**/api/v1/sessions/sess_shared_real", async (route) => {
      sessionDetailRequests += 1;
      await route.fulfill(json({ detail: "session detail is owner-only" }, 403));
    });
    await page.route("**/api/v1/sessions/sess_shared_real/artifacts**", async (route) => {
      sessionArtifactRequests += 1;
      await route.fulfill(json({ detail: "session artifacts are owner-only" }, 403));
    });
    await page.route("**/api/v1/canvas/by-session/sess_shared_real**", async (route) => {
      const url = new URL(route.request().url());
      if (url.pathname.endsWith("/artifacts")) {
        await route.fulfill(json({
          items: [{
            id: "art_shared_csv",
            filename: "shared.csv",
            mime: "text/csv",
            size: 18,
            source: "python_sandbox",
            created_at: "2026-07-03T00:00:00Z",
            preview_available: true,
            kind: "sandbox_output",
            origin: "compute",
            trust: "trusted",
            created_by_turn: null,
          }],
        }));
        return;
      }
      // Since bb3f6ef7c, Results viewer reads report-scoped artifacts through representations/{details|preview|raw}
      // (routes/canvas.py get_report_artifact_representation); CSV preview uses spreadsheet/rows shape.
      const representation = url.pathname.match(
        /\/artifacts\/art_shared_csv\/representations\/(details|preview|raw)$/,
      )?.[1];
      if (representation) {
        const base = { family: "spreadsheet", representation, revision: `rev_shared_${representation}`, text: null };
        const raw = "note\nreport-scoped artifact body\n";
        await route.fulfill(json(
          representation === "details"
            ? {
                ...base,
                details: {
                  id: "art_shared_csv",
                  filename: "shared.csv",
                  mime: "text/csv",
                  size: 18,
                  source: "python_sandbox",
                  created_at: "2026-07-03T00:00:00Z",
                  kind: "sandbox_output",
                },
              }
            : representation === "preview"
              ? {
                  ...base,
                  mode: "rows",
                  columns: ["note"],
                  rows: [{ note: "report-scoped artifact body" }],
                  total_rows: 1,
                  shown_rows: 1,
                  truncated: false,
                }
              : {
                  ...base,
                  mode: "text",
                  text: raw,
                  encoding: "utf-8",
                  truncated: false,
                  total_byte_count: raw.length,
                  returned_byte_count: raw.length,
                },
        ));
        return;
      }
      canvasRequests.push(route.request().url());
      await route.fulfill(json({
        canvas: {
          id: "cv_shared_real",
          sessionId: "sess_shared_real",
          workspaceId: "ws_smoke",
          title: "Shared Report",
          status: "done",
          createdAt: "2026-07-03T00:00:00Z",
          updatedAt: "2026-07-03T00:00:00Z",
        },
        sections: [
          { id: "sec_shared_h1", kind: "h1", text: "Shared Report" },
          { id: "sec_shared_body", kind: "narrative", markdown: "Shared report body" },
        ],
        sourceRefs: [],
      }));
    });

    await page.goto("/?session=sess_shared_real&report=1");

    await expect(page.getByText("Shared report body")).toBeVisible();
    // Report-view tabs follow UI language (since 9ba363385, zh-CN says “数据”).
    await page.getByRole("tab", { name: /^(Data|数据)$/ }).click();
    await expect(page.getByText("shared.csv")).toBeVisible();
    await page.getByText("shared.csv").click();
    await expect(page.getByText("report-scoped artifact body")).toBeVisible();
    expect(sessionDetailRequests).toBe(1);
    expect(sessionArtifactRequests).toBe(0);
    expect(canvasRequests).toHaveLength(1);
    expect(new URL(canvasRequests[0]).searchParams.get("expect_report")).toBe("true");
  });

  test("owner report intent follows same-session soft navigation, history, and reload", async ({ page }) => {
    await mockAuthedApp(page);
    await page.route("**/api/v1/sessions/sess_owner_report", async (route) => {
      await route.fulfill(json({
        session: {
          id: "sess_owner_report",
          workspace_id: "ws_smoke",
          title: "Owner Report",
          started_at: "2026-07-03T00:00:00Z",
          last_active_at: "2026-07-03T01:00:00Z",
          status: "done",
          cost_usd: 0,
          tokens_total: 0,
          intents: ["analysis"],
        },
        turns: [{
          id: "t_owner_report",
          seq: 1,
          user_text: "Build the owner report",
          intent: "analysis",
          status: "done",
          started_at: "2026-07-03T00:00:00Z",
          ended_at: "2026-07-03T01:00:00Z",
          final_answer: "Owner report ready",
          agent_type: "lead",
          attachments: [],
          summary: null,
        }],
      }));
    });
    await page.route("**/api/v1/turns/t_owner_report/events", async (route) => {
      await route.fulfill(json({ events: [] }));
    });
    await page.route("**/api/v1/canvas/by-session/sess_owner_report**", async (route) => {
      await route.fulfill(json({
        canvas: {
          id: "cv_owner_report",
          sessionId: "sess_owner_report",
          workspaceId: "ws_smoke",
          title: "Owner Report",
          status: "done",
          createdAt: "2026-07-03T00:00:00Z",
          updatedAt: "2026-07-03T00:00:00Z",
        },
        sections: [
          { id: "sec_owner_h1", kind: "h1", text: "Owner Report" },
          { id: "sec_owner_body", kind: "narrative", markdown: "Owner report body" },
        ],
        sourceRefs: [],
      }));
    });

    await page.goto("/?session=sess_owner_report");
    await expect(page.getByText("Owner report body")).toBeVisible();
    await expect(page.locator(".v2-workspace")).not.toHaveClass(/canvas-maxed/);

    await page.evaluate(() => {
      window.history.pushState(null, "", "/?session=sess_owner_report&report=1");
    });
    await expect(page).toHaveURL(/\?session=sess_owner_report&report=1$/);
    await expect(page.locator(".v2-workspace")).toHaveClass(/canvas-maxed/);

    await page.goBack();
    await expect(page).toHaveURL(/\?session=sess_owner_report$/);
    await expect(page.locator(".v2-workspace")).not.toHaveClass(/canvas-maxed/);

    await page.evaluate(() => {
      window.history.pushState(null, "", "/?session=sess_owner_report&report=1");
    });
    await expect(page.locator(".v2-workspace")).toHaveClass(/canvas-maxed/);

    await page.reload();
    await expect(page.getByText("Owner report body")).toBeVisible();
    await expect(page).toHaveURL(/\?session=sess_owner_report&report=1$/);
  });

  test("shared report source panel uses report-scoped preview/download and chart can switch line to bar", async ({ page }) => {
    await page.route("**/api/v1/canvas/by-session/sess_spike_p31/artifacts/art_demo_numbered_001/preview**", async (route) => {
      await route.fulfill(json({
        family: "text",
        text: "report-scoped source body\nP0-A canonical numbered source",
        mode: "text",
      }));
    });

    await page.goto("/spike/p31?shared=1&dateChart=1");

    const citation = page.locator("[data-citation-kind='numbered']").first();
    await expect(citation).toBeVisible();
    await citation.click();

    // The pointer stays on the badge, so its non-modal hover card may reopen once the source
    // panel closes; follow the source panel drawer itself rather than any dialog.
    const dialog = page.getByRole("dialog").filter({ has: page.getByTestId("source-panel-drawer") });
    await expect(dialog).toBeVisible();
    await expect(page.getByRole("dialog")).toHaveCount(1);
    await expect(dialog).toContainText("report-scoped source body");
    await expect(dialog.getByRole("link", { name: /下载|Download/ })).toHaveAttribute(
      "href",
      "/api/v1/canvas/by-session/sess_spike_p31/artifacts/art_demo_numbered_001",
    );
    await expect(dialog.getByRole("link", { name: "Open in Files" })).toHaveCount(0);

    await page.keyboard.press("Escape");
    await expect(dialog).toHaveCount(0);

    const chart = page.getByRole("figure", { name: "Feishu dogfood date chart" });
    await expect(chart).toBeVisible();
    await chart.getByRole("button", { name: /折线/ }).click();
    await page.getByRole("menuitemradio", { name: "柱状" }).click();
    await expect(chart.getByRole("button", { name: /柱状/ })).toBeVisible();
    await expect(chart.getByText("已切换为表格视图")).toHaveCount(0);
  });

  test("chart hover keeps the chart frame stable", async ({ page }) => {
    const consoleErrors: string[] = [];
    page.on("console", (msg) => {
      if (msg.type() === "error") consoleErrors.push(msg.text());
    });
    // Editable /spike/p31 design sample background reads art_000000000001; mock backend lacks file, would 404,
    // unrelated to chart hover: supply 1×1 PNG, keeping console assertion about chart itself.
    await page.route("**/api/v1/files/art_000000000001?**", async (route) => {
      await route.fulfill({ status: 200, contentType: "image/png", body: PNG_1X1 });
    });
    await page.goto("/spike/p31?dateChart=1");

    const chartSection = page.locator('[data-section-id="sec_feishu_date_line_chart"]');
    await expect(chartSection).toBeVisible();
    const canvas = chartSection.locator("canvas").first();
    await expect(canvas).toBeVisible();
    await canvas.scrollIntoViewIfNeeded();

    const before = await canvas.boundingBox();
    expect(before).not.toBeNull();
    const beforePixels = await canvas.screenshot();
    for (let i = 1; i <= 8; i += 1) {
      await page.mouse.move(
        before!.x + (before!.width * i) / 9,
        before!.y + before!.height * 0.45,
      );
    }
    // Desktop tooltip nearest + intersect: appears only over datapoint (d61d7aae9/5211aa67b).
    // Point location changes with y-axis/end-label padding; never hardcode coordinates: inspect actual rendered line,
    // hover rightmost saturated-color pixel column (terminal datapoint). Grid/area/text low-saturation.
    const lastPoint = await canvas.evaluate((el) => {
      const c = el as HTMLCanvasElement;
      const ctx = c.getContext("2d");
      if (!ctx) return null;
      const { width, height } = c;
      const data = ctx.getImageData(0, 0, width, height).data;
      for (let x = width - 1; x >= 0; x -= 1) {
        const ys: number[] = [];
        for (let y = 0; y < height; y += 1) {
          const i = (y * width + x) * 4;
          const r = data[i]!, g = data[i + 1]!, b = data[i + 2]!, a = data[i + 3]!;
          if (a > 200 && Math.max(r, g, b) - Math.min(r, g, b) > 60) ys.push(y);
        }
        if (ys.length) {
          const rect = c.getBoundingClientRect();
          const y = ys[Math.floor(ys.length / 2)]!;
          return { x: ((x - 2) * rect.width) / width, y: (y * rect.height) / height };
        }
      }
      return null;
    });
    expect(lastPoint).not.toBeNull();
    await page.mouse.move(before!.x + lastPoint!.x, before!.y + lastPoint!.y);
    await page.waitForTimeout(200);
    const hoverPixels = await canvas.screenshot();
    const after = await canvas.boundingBox();
    expect(after).not.toBeNull();
    expect(Math.abs(after!.x - before!.x)).toBeLessThan(1);
    expect(Math.abs(after!.y - before!.y)).toBeLessThan(1);
    expect(Math.abs(after!.width - before!.width)).toBeLessThan(1);
    expect(Math.abs(after!.height - before!.height)).toBeLessThan(1);
    expect(Buffer.compare(beforePixels, hoverPixels)).not.toBe(0);
    expect(consoleErrors).toEqual([]);
  });

  // Since 20cfdf37f, skills page removed pagination/“启用本页”, groups by category with “全部启用/全部停用” headers;
  // since 407980fa5, personal skill requests include ?scope=personal. Large-group enable still sends only one bulk request.
  test("skills page enables a large category group with one bulk patch", async ({ page }) => {
    await mockAuthedApp(page);
    const skills = Array.from({ length: 55 }, (_, i) => ({
      name: `skill-${String(i + 1).padStart(2, "0")}`,
      description: "dogfood skill",
      version: null,
      source_type: "builtin",
      enabled: false,
      size_bytes: 100,
      created_at: null,
    }));
    let bulkBody: unknown = null;
    let bulkRequests = 0;

    await page.route("**/api/v1/team-skills", async (route) => {
      await route.fulfill(json({ items: [] }));
    });
    await page.route(/\/api\/v1\/skills(\?.*)?$/, async (route) => {
      await route.fulfill(json({ builtin: skills, user_upload: [], user_generated: [] }));
    });
    await page.route(/\/api\/v1\/skills\/bulk(\?.*)?$/, async (route) => {
      bulkRequests += 1;
      bulkBody = route.request().postDataJSON();
      await route.fulfill(json({
        items: skills.map((skill) => ({ ...skill, enabled: true, body_md: "", allowed_tools: [], scripts: [], source_turn_id: null, trigger_examples: [], references: [], evals_count: 0, has_evals: false })),
        errors: [],
      }));
    });

    await page.goto("/skills");
    // skill-* lacks category, slug fallback groups under “平台与工具”.
    const group = page.locator("section.sgroup").filter({ hasText: "平台与工具" });
    await expect(group.getByText("启用 0/55")).toBeVisible();
    await group.getByRole("button", { name: "全部启用" }).click();
    await expect.poll(() => bulkBody).toMatchObject({
      items: expect.arrayContaining([
        expect.objectContaining({ name: "skill-01", enabled: true }),
        expect.objectContaining({ name: "skill-55", enabled: true }),
      ]),
    });
    expect((bulkBody as { items: unknown[] }).items).toHaveLength(55);
    expect(bulkRequests).toBe(1);
  });

  test("skills page keeps partial bulk failure visible after refresh", async ({ page }) => {
    await mockAuthedApp(page);
    const skills = Array.from({ length: 3 }, (_, i) => ({
      name: `skill-${String(i + 1).padStart(2, "0")}`,
      description: "dogfood skill",
      version: null,
      source_type: "builtin",
      enabled: false,
      size_bytes: 100,
      created_at: null,
    }));

    await page.route("**/api/v1/team-skills", async (route) => {
      await route.fulfill(json({ items: [] }));
    });
    await page.route(/\/api\/v1\/skills(\?.*)?$/, async (route) => {
      await route.fulfill(json({ builtin: skills, user_upload: [], user_generated: [] }));
    });
    await page.route(/\/api\/v1\/skills\/bulk(\?.*)?$/, async (route) => {
      await route.fulfill(json({
        items: skills.slice(0, 2).map((skill) => ({ ...skill, enabled: true, body_md: "", allowed_tools: [], scripts: [], source_turn_id: null, trigger_examples: [], references: [], evals_count: 0, has_evals: false })),
        errors: [{ name: "skill-03", status: 403, detail: "denied" }],
      }));
    });

    await page.goto("/skills");
    const group = page.locator("section.sgroup").filter({ hasText: "平台与工具" });
    await group.getByRole("button", { name: "全部启用" }).click();
    await expect(page.getByText("批量操作部分失败: 1 个技能未更新")).toBeVisible();
  });

  test("report editor overlay manager closes block AI on Escape and outside click", async ({ page }) => {
    await page.route("**/api/v1/metrics/event", async (route) => {
      await route.fulfill(json({ ok: true }));
    });

    await page.goto("/spike/p31?designEdit=1");
    await expect(page.locator(".canvas-report-shell")).toHaveAttribute("data-edit-mode", "on");

    const narrative = page.locator('[data-section-kind="narrative"]').first();
    await expect(narrative).toBeVisible();
    await narrative.hover();
    await narrative.getByRole("button", { name: /AI 改写|Edit with AI/ }).click();

    const blockAI = page.getByRole("dialog", { name: /块级 AI 改写|Edit block with AI/ });
    await expect(blockAI).toBeVisible();
    await page.keyboard.press("Escape");
    await expect(blockAI).toHaveCount(0);

    await narrative.hover();
    await narrative.getByRole("button", { name: /AI 改写|Edit with AI/ }).click();
    await expect(blockAI).toBeVisible();
    await page.locator("header").click();
    await expect(blockAI).toHaveCount(0);
  });

  test("report editor selection toolbar opens from real pointer selection", async ({ page }) => {
    await page.route("**/api/v1/metrics/event", async (route) => {
      await route.fulfill(json({ ok: true }));
    });

    await page.goto("/spike/p31?designEdit=1");
    await expect(page.locator(".canvas-report-shell")).toHaveAttribute("data-edit-mode", "on");

    await openSelectionToolbar(page, { allowEditorFallback: false });
  });

  test("report editor selection toolbar closes after an AI rewrite action", async ({ page }) => {
    await page.route("**/api/v1/metrics/event", async (route) => {
      await route.fulfill(json({ ok: true }));
    });
    await page.route("**/api/v1/canvas/cv_spike_p31/sections/_/rewrite", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "text/event-stream",
        body:
          'event: chunk\ndata: {"delta":"E2E rewrite"}\n\n' +
          'event: done\ndata: {"final_text":"E2E rewrite","unverified_numbers":[]}\n\n',
      });
    });

    await page.goto("/spike/p31?designEdit=1");
    await expect(page.locator(".canvas-report-shell")).toHaveAttribute("data-edit-mode", "on");

    const toolbar = await openSelectionToolbar(page);
    // Feishu07.06 #15c: ✦改写 now opens custom-instruction input; instruction + Enter starts rewrite.
    await toolbar.getByRole("button", { name: /改写|Rewrite/ }).first().click();
    const input = toolbar.locator("input").first();
    await expect(input).toBeVisible();
    await input.fill("换个角度重写");
    await input.press("Enter");

    await expect(toolbar).toHaveCount(0);
    // Result strip nonmodal action group (decide while reading rewritten body), not dialog.
    const result = page.getByRole("group", { name: /改写完成|Rewrite complete|核对|verify/i });
    await expect(result).toBeVisible();
    await expect(result.getByRole("button", { name: /接受|Accept/ })).toBeVisible();
  });

  test("report editor AI rewrite failure shows feedback and leaves original text", async ({ page }) => {
    await page.route("**/api/v1/metrics/event", async (route) => {
      await route.fulfill(json({ ok: true }));
    });
    await page.route("**/api/v1/canvas/cv_spike_p31/sections/_/rewrite", async (route) => {
      await route.fulfill(json({ detail: "rewrite backend unavailable" }, 500));
    });

    await page.goto("/spike/p31?designEdit=1");
    await expect(page.locator(".canvas-report-shell")).toHaveAttribute("data-edit-mode", "on");

    const toolbar = await openSelectionToolbar(page);
    // #15c: ✦改写 → custom input → Enter starts.
    await toolbar.getByRole("button", { name: /改写|Rewrite/ }).first().click();
    const input = toolbar.locator("input").first();
    await expect(input).toBeVisible();
    await input.fill("换个角度重写");
    await input.press("Enter");

    await expect(toolbar).toHaveCount(0);
    const alert = page.getByRole("alert").filter({ hasText: /改写失败|Rewrite failed/ });
    await expect(alert).toBeVisible();
    // Explain the failure in product words; never echo the raw server body or status code.
    await expect(alert).toContainText("改写服务暂时不可用，原文没有改动");
    await expect(alert).not.toContainText("rewrite backend unavailable");
    await expect(alert).not.toContainText("500");
  });

  test("report editor selection toolbar closes when clicking a chart block", async ({ page }) => {
    await page.route("**/api/v1/metrics/event", async (route) => {
      await route.fulfill(json({ ok: true }));
    });

    await page.goto("/spike/p31?designEdit=1&dateChart=1");
    await expect(page.locator(".canvas-report-shell")).toHaveAttribute("data-edit-mode", "on");

    const toolbar = await openSelectionToolbar(page);
    const chartSection = page.locator('[data-section-id="sec_feishu_date_line_chart"]');
    await expect(chartSection).toBeVisible();
    await chartSection.click();

    await expect(toolbar).toHaveCount(0);
  });

  // Since c7a14d057, space settings grant access only to existing team members; email invitation moved to team-settings “成员” invitation drawer;
  // copying invite link must still fall back to prompt if Clipboard denied.
  test("team members invite-link copy falls back to prompt when Clipboard is denied", async ({ page }) => {
    await page.addInitScript(() => {
      Object.defineProperty(navigator, "clipboard", {
        configurable: true,
        value: {
          writeText: () => Promise.reject(new Error("denied")),
        },
      });
      (window as unknown as { __promptCalls: Array<[string | undefined, string | undefined]> }).__promptCalls = [];
      window.prompt = (message?: string, value?: string) => {
        (window as unknown as { __promptCalls: Array<[string | undefined, string | undefined]> }).__promptCalls.push([message, value]);
        return null;
      };
    });
    await mockAuthedApp(page, { subagentManagement: true });
    let inviteBody: unknown = null;
    await page.route("**/api/v1/auth/capabilities", async (route) => {
      await route.fulfill(json({
        deployment_mode: "saas",
        account_creation_policy: "invite_only",
        email_verification_policy: "smtp_required",
        auth_methods: ["password"],
        public_registration: false,
        bootstrap_required: false,
        invite_activation: true,
      }));
    });
    await page.route(/\/api\/v1\/admin\/members(\/.*)?(\?.*)?$/, async (route) => {
      const req = route.request();
      const url = new URL(req.url());
      if (req.method() === "GET" && url.pathname === "/api/v1/admin/members") {
        await route.fulfill(json({ items: [], total: 0, owner_id: "u_smoke", your_role: "owner" }));
        return;
      }
      if (req.method() === "GET" && url.pathname === "/api/v1/admin/members/invites") {
        await route.fulfill(json({ invites: [], total: 0 }));
        return;
      }
      if (req.method() === "POST" && url.pathname === "/api/v1/admin/members/invite") {
        inviteBody = req.postDataJSON();
        await route.fulfill(json({
          method: "email_invite",
          email_sent: false,
          invite_url: "http://x/invite?token=E2E_COPY",
        }));
        return;
      }
      await route.fulfill(json({ detail: "not found" }, 404));
    });

    await page.goto("/team/settings/people/");
    await page.getByRole("button", { name: "邀请成员" }).click();
    const drawer = page.getByRole("dialog", { name: "邀请成员" });
    await expect(drawer).toBeVisible();
    const email = drawer.getByLabel("邮箱");
    await expect(email).toBeVisible();
    await email.fill("new@example.com");
    const send = drawer.getByRole("button", { name: "发送邀请" });
    await expect(send).toBeEnabled();
    await send.click();

    await expect.poll(() => inviteBody).toMatchObject({ email: "new@example.com" });
    await expect(drawer.locator(".people-invite-link code")).toHaveText("http://x/invite?token=E2E_COPY");
    await drawer.getByRole("button", { name: "复制链接" }).click();

    await expect.poll(async () =>
      page.evaluate(() => (window as unknown as { __promptCalls?: Array<[string | undefined, string | undefined]> }).__promptCalls ?? []),
    ).toEqual([["复制邀请链接", "http://x/invite?token=E2E_COPY"]]);
  });

  test("reports page uses an in-app delete confirmation dialog instead of native confirm", async ({ page }) => {
    await mockAuthedApp(page);
    await page.addInitScript(() => {
      window.confirm = () => {
        throw new Error("native confirm should not be used for report deletion");
      };
    });

    const report = {
      id: "cv_report_delete_1",
      session_id: "sess_report_delete_1",
      workspace_id: "ws_smoke",
      workspace_name: "Smoke workspace",
      title: "季度营收复盘",
      status: "active",
      effective_status: "active",
      quarantined_count: 0,
      created_at: "2026-07-01T00:00:00Z",
      updated_at: "2026-07-03T00:00:00Z",
      sections_count: 3,
      first_section_title: null,
      audience: "private",
      share_count: 0,
      shared_by: null,
    };
    let reports = [report];
    let deleteCount = 0;

    await page.route("**/api/v1/canvas**", async (route) => {
      const req = route.request();
      const url = new URL(req.url());
      if (req.method() === "GET" && url.pathname === "/api/v1/canvas") {
        await route.fulfill(json({ items: url.searchParams.get("scope") === "shared" ? [] : reports }));
        return;
      }
      if (req.method() === "DELETE" && url.pathname === `/api/v1/canvas/${report.id}`) {
        deleteCount += 1;
        reports = [];
        await route.fulfill(json({}, 204));
        return;
      }
      await route.fulfill(json({ detail: "not found" }, 404));
    });

    await page.goto("/reports");
    await expect(page.getByRole("link", { name: /季度营收复盘/ })).toBeVisible();

    // Delete lives in the card's "⋯" menu; it confirms first, so the item ends with "…".
    await page.getByRole("button", { name: /「季度营收复盘」的更多操作|More actions for/ }).click();
    await page.getByRole("menuitem", { name: /^(删除|Delete)/ }).click();
    const dialog = page.getByRole("dialog", { name: /删除「季度营收复盘」|Delete report "季度营收复盘"/ });
    await expect(dialog).toBeVisible();
    await dialog.getByRole("button", { name: /取消|Cancel/ }).click();
    await expect(dialog).toHaveCount(0);
    expect(deleteCount).toBe(0);

    // Delete lives in the card's "⋯" menu; it confirms first, so the item ends with "…".
    await page.getByRole("button", { name: /「季度营收复盘」的更多操作|More actions for/ }).click();
    await page.getByRole("menuitem", { name: /^(删除|Delete)/ }).click();
    await expect(dialog).toBeVisible();
    await dialog.getByRole("button", { name: /删除报告|Delete report/ }).click();
    await expect.poll(() => deleteCount).toBe(1);
    await expect(page.getByRole("link", { name: /季度营收复盘/ })).toHaveCount(0);
  });
});
