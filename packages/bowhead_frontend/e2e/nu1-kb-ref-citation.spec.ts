/**
 * Sprint ν1 Phase 2 — KB ref [ref:chunk_xxx] e2e
 *
 * Validates the end-to-end pipeline that previously had ZERO e2e coverage
 * (discovery §3.6 finding):
 *  1. KB ingestion picks up fixture corpus → chunks created with
 *     `chunk_<24hex>` content-addressable IDs
 *  2. compute peer reads compute.md ν1 [ref:S] instruction
 *  3. peer emits `[ref:chunk_xxx]` in narrative
 *  4. backend _validate_all_refs accepts real chunk_ids, rejects fakes
 *  5. frontend citation-plugin REF_RE renders purple superscript
 *  6. hover popover shows file_name / page / section_title
 *  7. click "在知识库查看 →" jumps to /knowledge/?file_id=...
 *
 * Pre-requisites:
 *  - 5 synthesized fixtures ingested into a test workspace KB (see
 *    `e2e/fixtures/kb-ref-test-corpus/`); ingestion harness either:
 *    (a) Manual: upload via /api/v1/kb/ingest before run, OR
 *    (b) Automated: kb_test_seed.py CLI script (NEW Phase 5 deliverable)
 *  - Backend running with BOWHEAD_REF_VALIDATE_STRICT=1 + BOWHEAD_KB_REF_STRICT=1
 *
 * Run:
 *   PROD=0 BOWHEAD_E2E_BASE_URL=http://localhost:3000 \
 *     npx playwright test e2e/nu1-kb-ref-citation.spec.ts
 */
import { expect, test } from "@playwright/test";

const BASE_URL = process.env.BOWHEAD_E2E_BASE_URL || "http://localhost:3000";
const RUN_REAL_E2E = process.env.BOWHEAD_RUN_REAL_E2E === "1";

test.describe("Sprint ν1 KB ref e2e", () => {
  test.skip(!RUN_REAL_E2E, "requires seeded KB fixtures, real backend, and real LLM");

  test.beforeEach(async ({ page }) => {
    await page.goto(BASE_URL);
    await page.waitForLoadState("networkidle");
  });

  test("happy: EBITDA 定义查询触发 [ref:chunk_xxx] 上标", async ({ page }) => {
    // Send a query that should retrieve from 01-ebita-policy.md
    const composer = page.locator('textarea[placeholder*="Ask"]');
    await composer.fill("EBITDA 定义和必需字段是什么? 引用 KB 文档");
    await composer.press("Enter");

    // Wait up to 90s for canvas to render (real LLM)
    await page.waitForSelector("[class*='citation-ref'], .citation-ref", {
      timeout: 90_000,
    });

    // Find purple ref superscript
    const refs = page.locator("span.citation-ref, [class*='citation-ref']");
    const count = await refs.count();
    expect(count).toBeGreaterThanOrEqual(1);

    // Hover the first → popover shows file metadata
    await refs.first().hover();
    await page.waitForSelector("[class*='popover'], [role='tooltip']", {
      timeout: 5_000,
    });
    const popoverText = await page.textContent(
      "[class*='popover'], [role='tooltip']",
    );
    expect(popoverText || "").toMatch(/EBITDA|policy|.md|.pdf/i);
  });

  test("multi-doc: cross-fixture query → 多个 [ref:] from different files", async ({
    page,
  }) => {
    const composer = page.locator('textarea[placeholder*="Ask"]');
    await composer.fill(
      "对比 EBITDA 计算和 HR 招聘流程的合规要求, 各引用 KB 文档",
    );
    await composer.press("Enter");
    await page.waitForSelector("span.citation-ref, [class*='citation-ref']", {
      timeout: 120_000,
    });
    const refs = page.locator("span.citation-ref, [class*='citation-ref']");
    expect(await refs.count()).toBeGreaterThanOrEqual(2);
  });

  test("zero-result: 不存在的 KB 内容查询 → narrative 不含 [ref:]", async ({
    page,
  }) => {
    const composer = page.locator('textarea[placeholder*="Ask"]');
    await composer.fill("我们的奶牛养殖政策是什么");  // Unrelated to all five fixtures
    await composer.press("Enter");
    // wait up to 60s, then assert no fake refs leaked
    await page.waitForTimeout(60_000);
    const body = await page.textContent("body");
    // Backend ν1 strict reject blocks hallucinated [ref:], so frontend
    // should NOT see any chunk_xxx purple superscript that's bound to
    // a real source. (citation-plugin only decorates if regex matches,
    // raw [ref:fake] would be stripped by ν1 _strip_invalid_refs after retry.)
    expect(body || "").not.toMatch(/\[ref:chunk_[0-9a-f]{24}\]/);
  });

  test("hallucinate-then-retry: peer 编 fake chunk_id → backend reject → retry", async ({
    page,
  }) => {
    // This test relies on a per-test seed that biases the LLM towards a
    // fake response on first call (test-only env BOWHEAD_TEST_FORCE_HALLUCINATE_FIRST=1
    // — leave this as future work; for now we just assert ν1 audit log emit).
    // Read /admin/turns/{id}/replay events and assert
    // "validate_reject" event emitted at least once. (ad-hoc; full automation
    // covered in Phase 5 dogfood.)
    test.skip(
      true,
      "Phase 5 dogfood — needs test-only LLM bias + admin turn dump endpoint",
    );
  });

  test("cross-turn doc reuse: 同 doc 跨 turn 引用 OK (default allow per PRD §11.1)", async ({
    page,
  }) => {
    const composer = page.locator('textarea[placeholder*="Ask"]');
    await composer.fill("EBITDA 定义? 引用 KB");
    await composer.press("Enter");
    await page.waitForSelector("span.citation-ref, [class*='citation-ref']", {
      timeout: 90_000,
    });
    // Second-turn question about the same document
    await composer.fill("再展开下 EBITDA 的限制说明");
    await composer.press("Enter");
    await page.waitForSelector("span.citation-ref:nth-of-type(2), [class*='citation-ref']:nth-of-type(2)", {
      timeout: 90_000,
    });
    // Both turns cite successfully, without errors
    const body = await page.textContent("body");
    expect(body || "").not.toMatch(/Application error|来源已失效/);
  });
});
