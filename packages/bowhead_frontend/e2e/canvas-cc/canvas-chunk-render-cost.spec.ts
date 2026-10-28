import { expect, test } from "@playwright/test";

/**
 * report-streaming batch B prerequisite probe—**frontend rendering cost of chunks in a realistically sized report**.
 *
 * This measures discovery §8 unverified item #4 rather than functionality: the report body is one TipTap editor,
 * the whole report one ProseMirror doc; any section content change triggers `setContent` **to rebuild the entire document**.
 * Streaming through this path rebuilds the whole doc per chunk. Whether to bypass the editor with read-only
 * preview blocks (discovery §6.3) depends on this measurement.
 *
 * Size comes from measurements on 116 prod: canvas sections p50=11 / p90=28 / max=96;
 * narrative body avg 1702 characters. Scenario: `perfburst` in canvas-mock-scenarios.mjs.
 *
 * Criteria (browser main thread):
 *   - Long-task (>50ms) count and total duration: directly correspond to user-perceived jank.
 *   - Wall-clock time from first chunk to stable DOM.
 *
 * ⚠ This spec **asserts no hard threshold** (local/CI machine differences make thresholds flaky noise).
 * It attaches a measurement report to Playwright output for human design decisions. Assert only that it runs
 * and actually receives chunks, avoiding a vacuous test that always passes.
 */

async function resetMock(page: import("@playwright/test").Page) {
  await page.request.post("http://localhost:18080/api/v1/_test/reset");
}

async function sendMessage(page: import("@playwright/test").Page, msg: string) {
  const textarea = page.locator("textarea").first();
  await expect(textarea).toBeVisible({ timeout: 15_000 });
  await textarea.fill(msg);
  await textarea.press("Enter");
}

type Probe = {
  longTasks: number;
  longTaskMs: number;
  maxTaskMs: number;
  sections: number;
  grownLen: number;
};

for (const nSections of [11, 28, 96] as const) {
  test(`chunk 渲染成本 @ ${nSections} 段（prod ${nSections === 11 ? "p50" : nSections === 28 ? "p90" : "max"}）`, async ({
    page,
  }, testInfo) => {
    await resetMock(page);
    await page.goto("/");

    // Install the long-task observer before any events arrive.
    await page.evaluate(() => {
      const w = window as unknown as Record<string, unknown>;
      const tasks: number[] = [];
      w.__PERF_TASKS__ = tasks;
      try {
        new PerformanceObserver((list) => {
          for (const e of list.getEntries()) tasks.push(e.duration);
        }).observe({ entryTypes: ["longtask"] });
      } catch {
        /* This browser lacks longtask support; use wall-clock time below as fallback. */
      }
    });

    const t0 = Date.now();
    await sendMessage(page, `perfburst${nSections}`);

    // Wait until all chunks reach the store (last section body length stabilizes).
    await expect
      .poll(
        async () =>
          page.evaluate(() => {
            const snap = (
              window as unknown as { __CANVAS_SNAPSHOT__?: () => { sections: unknown[] } }
            ).__CANVAS_SNAPSHOT__?.();
            const secs = (snap?.sections ?? []) as Array<{ id?: string; markdown?: string }>;
            const grown = secs.find((s) => s.id === "sec_perf_narr");
            return grown?.markdown?.length ?? 0;
          }),
        { timeout: 60_000, message: "chunk 没灌进 store" },
      )
      .toBeGreaterThan(600);
    const wallMs = Date.now() - t0;

    const probe: Probe = await page.evaluate(() => {
      const w = window as unknown as {
        __PERF_TASKS__?: number[];
        __CANVAS_SNAPSHOT__?: () => { sections: unknown[] };
      };
      const tasks = w.__PERF_TASKS__ ?? [];
      const snap = w.__CANVAS_SNAPSHOT__?.();
      const secs = (snap?.sections ?? []) as Array<{ id?: string; markdown?: string }>;
      const grown = secs.find((s) => s.id === "sec_perf_narr");
      return {
        longTasks: tasks.length,
        longTaskMs: Math.round(tasks.reduce((a, b) => a + b, 0)),
        maxTaskMs: Math.round(tasks.length ? Math.max(...tasks) : 0),
        sections: secs.length,
        grownLen: grown?.markdown?.length ?? 0,
      };
    });

    const report = [
      `场景: perfburst${nSections}  (60 个 chunk 灌进一段 narrative)`,
      `画布段数(实际到达): ${probe.sections}`,
      `累积正文长度: ${probe.grownLen} 字符`,
      `墙钟(发消息 → chunk 全部落库): ${wallMs} ms`,
      `长任务(>50ms) 个数: ${probe.longTasks}`,
      `长任务总时长: ${probe.longTaskMs} ms`,
      `最长单个任务: ${probe.maxTaskMs} ms`,
      "",
      "读数指引: 60 个 chunk ≈ 真实流式 3 秒(@50ms 节流)。",
      "若长任务总时长接近或超过墙钟 → 主线程基本被 setContent 占满 = 编辑器内联不可行,",
      "应走 discovery §6.3 的只读预览块方案。",
    ].join("\n");
    // eslint-disable-next-line no-console
    console.log(`\n=== chunk 渲染成本 @ ${nSections} 段 ===\n${report}\n`);
    await testInfo.attach(`chunk-render-cost-${nSections}.txt`, {
      body: report,
      contentType: "text/plain",
    });

    // Assert only that the probe itself is valid (avoid a vacuous test that always passes).
    expect(probe.grownLen, "chunk 必须真的累积进了 store").toBeGreaterThan(600);
    expect(probe.sections, "画布必须真的装载了目标体量").toBeGreaterThanOrEqual(nSections - 2);
  });
}
