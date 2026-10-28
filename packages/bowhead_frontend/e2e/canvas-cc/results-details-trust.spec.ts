import { expect, test } from "@playwright/test";
import type { ResultItemDTO, ResultsQueryResponse } from "../../lib/results-api";

for (const { storageTrust, completeList, trust } of [
  { storageTrust: true, completeList: false, trust: "export_copy_legacy_source_missing" },
  { storageTrust: false, completeList: false, trust: "export_copy_legacy_source_missing" },
  { storageTrust: false, completeList: true, trust: "export_copy_legacy_regenerable" },
  { storageTrust: false, completeList: true, trust: "export_copy_current" },
]) {
  test(`Details preserves export evaluation (${completeList ? `complete catalog ${trust}` : storageTrust ? "older storage trust" : "metadata only"})`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width: 1280, height: 800 });
    await page.request.post(`http://127.0.0.1:${process.env.PLAYWRIGHT_API_PORT ?? "18080"}/api/v1/_test/reset`);
    await page.route("**/api/v1/results/capabilities", (route) => route.fulfill({
      json: { dto_version: 1, workspace: true, complete_list: completeList, scale_nav: completeList, max_page_size: 50 },
    }));
    const artifact = {
      id: "art_delivery", filename: "report.pdf", mime: "application/pdf", size: 320,
      source: "canvas_export", created_at: "2026-09-08T00:00:00Z", kind: "canvas_export",
      preview_available: true, trust,
    };
    const surfaces: string[] = [];
    if (completeList) {
      // Browser contract coverage only: the real SQLite/blob HTTP regression
      // test_results_export_trust.py verifies this wire trust against Files.
      await page.route("**/api/v1/sessions/*/results/query", (route) => {
        const request = route.request().postDataJSON();
        surfaces.push(request.surface);
        const output: ResultItemDTO = {
          key: `artifact:${artifact.id}`, object_type: "artifact", title: artifact.filename,
          primary_group: "deliverables", secondary_group: null, viewer_profile: "document", status: "ready",
          route: { type: "artifact", artifact_id: artifact.id, default_mode: "preview" },
          capabilities: ["download", "view_details", "manage_in_files"], relation_roles: [],
          execution: null, version: null, position: 2,
          display_context: { source_role: "canvas_export", output_role: null, execution_label: null,
            occurred_at: artifact.created_at, duplicate_ordinal: null, duplicate_count: null },
          artifact_metadata: { mime: artifact.mime, size: artifact.size, source: artifact.source, kind: artifact.kind,
            origin: "export", trust, created_at: artifact.created_at, producer_kind: null },
        };
        const sessionId = new URL(route.request().url()).pathname.split("/")[4];
        const items: ResultItemDTO[] = [{
          ...output, key: `report:cv_${sessionId}`, object_type: "report", title: "E2E 报告", viewer_profile: "report",
          route: { type: "report" }, capabilities: ["read_report"], artifact_metadata: null, position: 1,
        }, output];
        const response: ResultsQueryResponse = {
          dto_version: 1, capability: { complete_list: true, scale_nav: true }, snapshot_token: "trust-snapshot",
          items: request.surface === "changes" ? [] : items,
          authorized_total: 2, matched_total: 2, loaded_count: request.surface === "changes" ? 0 : 2,
          group_counts: { deliverables: 2, evidence: 0, data: 0, code: 0, input: 0 },
          page_start: 0, next_cursor: null, previous_cursor: null, has_more: false,
          projection_status: { report_relations: "ready", execution_relations: "unavailable", export_relations: "degraded" },
          projection_revision: "trust-revision", new_results: { count: 0, projection_changed: false }, resolved_current_key: null,
        };
        return route.fulfill({ json: response });
      });
    }
    await page.route(/\/api\/v1\/sessions\/[^/]+\/artifacts(?:\?.*)?$/, async (route) => {
      const response = await route.fetch();
      const body = await response.json();
      return route.fulfill({ response, json: {
        ...body, items: body.items.map((item: { id: string }) => item.id === artifact.id ? artifact : item),
      } });
    });
    await page.route("**/api/v1/files/art_delivery/representations/details", (route) => {
      const { trust: _evaluatedTrust, ...metadata } = artifact;
      return route.fulfill({ json: {
        representation: "details", revision: "metadata-1", family: "pdf",
        details: { ...metadata, filename: "renamed-report.pdf", ...(storageTrust ? { trust: "trusted" } : {}) },
      } });
    });
    // An unreadable output still has readable metadata; don't start a binary renderer.
    await page.route("**/api/v1/files/art_delivery/representations/preview", (route) => route.fulfill({
      status: 422, json: { detail: { error: { code: "artifact_representation_corrupt" } } },
    }));
    await page.goto("/");
    const composer = page.locator("textarea").first();
    await composer.fill("resultsworkspace");
    const sent = page.waitForResponse((response) => response.url().endsWith("/api/v1/chat/send"));
    await composer.press("Enter");
    const { turn_id: turnId, session_id: sessionId } = await (await sent).json();
    await expect(page.locator('[data-presentation="results-reading"]')).toBeVisible();
    const detailResponse = await (await page.request.get(`/api/v1/sessions/${sessionId}`)).json();
    await page.route(`**/api/v1/sessions/${sessionId}`, (route) => route.fulfill({ json: {
      ...detailResponse,
      turns: [{
        id: turnId,
        seq: 1,
        user_text: "resultsworkspace",
        intent: "analysis",
        status: "completed",
        started_at: "2026-09-08T00:00:00Z",
        ended_at: "2026-09-08T00:00:01Z",
        final_answer: "报告已生成",
        trace_steps: [],
        summary: null,
      }],
    } }));
    await page.getByRole("button", { name: "成果", exact: true }).click();
    if (completeList) {
      await page.getByRole("button", { name: /浏览全部成果/ }).click();
      await page.locator('.results-explorer-pane [data-result-key="artifact:art_delivery"] button').click();
      expect(surfaces).toContain("quick");
      expect(surfaces).toContain("explorer");
    } else {
      await page.getByRole("dialog", { name: "成果导航", exact: true })
        .locator(".results-nav-item", { hasText: "report.pdf" }).click();
    }
    await page.getByRole("tab", { name: "详情", exact: true }).click();
    const details = page.locator(".artifact-details");
    await expect(details.locator(".artifact-details-head strong")).toHaveText("renamed-report.pdf");
    await expect(details.locator("dl > div").filter({ has: page.locator("dt", { hasText: "可信状态" }) }).locator("dd"))
      .toHaveText(artifact.trust);
    await expect(details.locator("a[href='/api/v1/files/art_delivery']").first()).toBeVisible();
    if (completeList) {
      const notice = page.locator('.result-file-viewer [role="note"]');
      if (trust === "export_copy_legacy_regenerable") {
        await expect(notice).toBeVisible();
        await expect(notice.getByRole("button")).toBeVisible();
      } else {
        await expect(notice).toHaveCount(0);
      }

      surfaces.length = 0;
      await page.goto(`/?session=${sessionId}&results=all&result=art_delivery&mode=details`);
      await expect(details.locator(".artifact-details-head strong")).toHaveText(
        "renamed-report.pdf",
        { timeout: 15_000 },
      );
      await expect.poll(() => surfaces.filter((surface) => surface !== "changes"))
        .toEqual(["locate"]);
      await page.screenshot({ path: testInfo.outputPath(`${trust}-details-1280.png`) });
    }
  });
}
