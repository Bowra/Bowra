import { expect, test, type Page } from "@playwright/test";
import { installReferenceComponent } from "./_composer-reference-component-modules";

const target = { kind: "document_revision" as const, version: 1 as const, session_id: "s_review", job_id: "djob_original", draft_digest: "a".repeat(64) };
const started = { status: "started", session_id: "s_review", turn_id: "t_revision", execution_generation: 1, stream_url: "/api/v1/chat/stream/t_revision", document_revision_target: target };
async function load(page: Page) {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.addInitScript(() => {
    (globalThis as unknown as { process: { env: Record<string, string> } }).process = { env: {} };
  });
  await page.route("**/api/**", (route) => route.fulfill({ status: 404, json: {} }));
  await page.route("**/api/v1/auth/me", (route) => route.fulfill({ json: { id: "u_revision", team_id: "team_revision", workspaces: [], capabilities: { composer: { reference_input: false } } } }));
  await installReferenceComponent(page, "", "_composer-document-revision-entry.tsx");
  await page.goto("/__reference_component_test");
  try { await expect(page.getByRole("textbox")).toBeEnabled(); }
  catch (error) { throw new Error(JSON.stringify(errors) + String(error)); }
  await expect.poll(() => page.evaluate(() => window.revisionComposer.ready())).toBe(true);
}
async function stage(page: Page) {
  await expect.poll(() => page.evaluate(() => window.revisionComposer.stage())).toMatchObject({ status: "applied" });
  await expect(page.getByTestId("composer-document-revision")).toBeVisible();
}

for (const width of [412, 1440]) {
  test(`exact document target remains readable and removable at ${width}`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: 800 });
    await load(page);
    expect(await page.evaluate((original) => window.revisionComposer.stage({ target: { ...original, job_id: `djob_${"a".repeat(120)}` } }), target)).toMatchObject({ status: "applied" });
    const note = page.getByTestId("composer-document-revision");
    await expect(note).toBeVisible();
    const bounds = await note.boundingBox();
    const button = await note.getByRole("button").boundingBox();
    expect(bounds!.x + bounds!.width).toBeLessThanOrEqual(width);
    expect(button!.height).toBeGreaterThanOrEqual(28); // Chat Cards Spec §9: 28, 44 on touch
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(width);
    await page.screenshot({ path: testInfo.outputPath(`document-target-${width}.png`) });
    await note.getByRole("button").click();
    await expect(note).toHaveCount(0);
    await expect(page.getByRole("textbox")).toHaveValue("Shorten the title");
  });
}

test("explicit staging preserves edits through remount and sends the exact target using ordinary admission", async ({ page }) => {
  await load(page);
  await stage(page);
  await page.getByRole("textbox").fill("My final title edit");
  await page.evaluate(() => window.revisionComposer.configure({ epoch: 1 }));
  await expect(page.getByRole("textbox")).toHaveValue("My final title edit");
  await page.evaluate(() => window.revisionComposer.configure({ otherCanvas: true }));
  await expect.poll(() => page.evaluate(() => window.revisionComposer.stage())).toMatchObject({ status: "applied" });
  await expect(page.getByRole("textbox")).toHaveValue("My final title edit");
  const requests: unknown[] = [];
  await page.route("**/api/v1/chat/send", async (route) => {
    const body = route.request().postDataJSON();
    const saved = await page.evaluate(() => window.revisionComposer.read());
    expect(saved.pending?.wireSnapshot?.documentRevisionTarget).toEqual(target);
    expect(saved.pending?.sourceDraftRevision).toBe(saved.draft?.revision);
    expect(body).toMatchObject({ message: "My final title edit", document_revision_target: target, client_submission_id: saved.pending?.clientSubmissionId });
    expect(body).not.toHaveProperty("input_document");
    expect(body).not.toHaveProperty("terminal_protocol_version");
    requests.push(body);
    await route.fulfill({ json: started });
  });
  await page.getByRole("button", { name: "Send", exact: true }).click();
  await expect(page.locator("#turn")).toHaveText(started.turn_id);
  await expect.poll(() => page.evaluate(() => window.revisionComposer.read())).toEqual({ draft: null, pending: null });
  await expect(page.getByTestId("composer-document-revision")).toHaveCount(0);
  expect(requests).toHaveLength(1);
});

test("dirty input and stale scope block staging without overwriting text", async ({ page }) => {
  await load(page);
  await page.getByRole("textbox").fill("Keep my question");
  expect(await page.evaluate(() => window.revisionComposer.stage())).toEqual({ status: "blocked", reason: "draft_not_empty" });
  await expect(page.getByRole("textbox")).toHaveValue("Keep my question");
  await page.evaluate(() => window.revisionComposer.configure({ otherScope: true }));
  await expect(page.getByRole("textbox")).toHaveValue("");
  expect(await page.evaluate(() => window.revisionComposer.stage())).toEqual({ status: "stale" });
  await page.evaluate(() => window.revisionComposer.configure({ otherScope: false }));
  await expect(page.getByRole("textbox")).toHaveValue("Keep my question");
});

test("locked recovery reads the typed draft without writing or accepting text-only prefill", async ({ page, context }) => {
  await load(page);
  await stage(page);
  const saved = await page.evaluate(() => window.revisionComposer.read());
  await page.evaluate(() => window.revisionComposer.configure({ epoch: 1, locked: true, prefill: "Unbound recovery text" }));
  await expect(page.getByRole("textbox")).toHaveValue("Shorten the title");
  await expect(page.getByRole("textbox")).toHaveAttribute("aria-readonly", "true");
  await expect(page.getByTestId("composer-document-revision")).toContainText(/文档草稿|Document draft/);
  await expect(page.getByTestId("composer-document-revision").getByRole("button")).toBeDisabled();
  await expect(page.getByRole("button", { name: "Send", exact: true })).toBeDisabled();
  expect(await page.evaluate(() => window.revisionComposer.stage())).toEqual({ status: "blocked", reason: "pending_submission" });
  await page.evaluate(() => window.revisionComposer.configure({ epoch: 2 }));
  await expect(page.getByRole("textbox")).toHaveValue("Shorten the title");
  expect(await page.evaluate(() => window.revisionComposer.read())).toEqual(saved);

  const second = await context.newPage();
  await load(second);
  await second.getByRole("textbox").fill("Newer draft from second tab");
  await expect.poll(() => second.evaluate(async () => (await window.revisionComposer.read()).draft?.message)).toBe("Newer draft from second tab");
  await page.evaluate(() => window.revisionComposer.configure({ locked: false, prefill: "" }));
  await expect(page.getByRole("textbox")).toHaveValue("Newer draft from second tab");
  await expect(page.getByTestId("composer-document-revision")).toBeVisible();

  await page.evaluate(() => window.revisionComposer.configure({ locked: true, epoch: 3 }));
  await expect(page.getByRole("textbox")).toHaveValue("Newer draft from second tab");
  await second.route("**/api/v1/chat/send", (route) => route.fulfill({ json: started }));
  await second.getByRole("button", { name: "Send", exact: true }).click();
  await expect.poll(() => second.evaluate(() => window.revisionComposer.read())).toEqual({ draft: null, pending: null });
  await page.evaluate(() => window.revisionComposer.configure({ locked: false }));
  await expect(page.getByRole("textbox")).toHaveValue("");
  await expect(page.getByTestId("composer-document-revision")).toHaveCount(0);
});

test("an empty staged instruction survives reload and explicit removal retains edited text", async ({ page }) => {
  await load(page);
  await expect.poll(() => page.evaluate(() => window.revisionComposer.stage({ instruction: "" }))).toMatchObject({ status: "applied" });
  await page.reload();
  await expect(page.getByTestId("composer-document-revision")).toBeVisible();
  await expect(page.getByRole("button", { name: "Send", exact: true })).toBeDisabled();
  await page.getByRole("textbox").fill("Keep this edit");
  await page.getByTestId("composer-document-revision").getByRole("button").click();
  await expect.poll(() => page.evaluate(async () => (await window.revisionComposer.read()).draft?.documentRevision)).toBeUndefined();
  await page.reload();
  await expect(page.getByRole("textbox")).toHaveValue("Keep this edit");
});

for (const failure of ["missing_target", "wrong_target", "cleanup"] as const) {
  test(`accepted ${failure} leaves the immutable recovery record available`, async ({ page }) => {
    await load(page);
    await stage(page);
    if (failure === "cleanup") await page.evaluate(() => window.revisionComposer.configure({ cleanupFailure: true }));
    await page.route("**/api/v1/chat/send", (route) => route.fulfill({ json: { ...started, document_revision_target: failure === "missing_target" ? undefined : failure === "wrong_target" ? { ...target, job_id: "djob_other" } : target } }));
    await page.getByRole("button", { name: "Send", exact: true }).click();
    await expect.poll(() => page.evaluate(() => window.revisionComposer.settled)).toEqual([false]);
    const saved = await page.evaluate(() => window.revisionComposer.read());
    expect(saved.pending?.wireSnapshot?.documentRevisionTarget).toEqual(target);
    expect(saved.draft?.documentRevision?.target).toEqual(target);
    await page.reload();
    await page.evaluate(() => window.revisionComposer.configure({ epoch: 1, locked: true }));
    await expect(page.getByRole("textbox")).toHaveValue(saved.pending!.message);
    await expect(page.getByTestId("composer-document-revision")).toContainText(/文档草稿|Document draft/);
    await page.route("**/api/v1/chat/send", async (route) => {
      expect(route.request().postDataJSON().client_submission_id).toBe(saved.pending!.clientSubmissionId);
      await route.fulfill({ json: started });
    });
    await expect.poll(() => page.evaluate(() => window.revisionComposer.ready())).toBe(true);
    await page.evaluate(() => window.revisionComposer.recover());
    await expect(page.locator("#turn")).toHaveText(started.turn_id);
    await expect.poll(() => page.evaluate(() => window.revisionComposer.read())).toEqual({ draft: null, pending: null });
    await page.evaluate(() => window.revisionComposer.configure({ locked: false }));
    await expect(page.getByTestId("composer-document-revision")).toHaveCount(0);
  });
}

test("a second tab retains strict revision conflict protection", async ({ page, context }) => {
  await load(page);
  await stage(page);
  const second = await context.newPage();
  await load(second);
  await expect(second.getByRole("textbox")).toHaveValue("Shorten the title");
  await second.getByRole("textbox").fill("Second tab edit");
  await expect.poll(() => second.evaluate(async () => (await window.revisionComposer.read()).draft?.message)).toBe("Second tab edit");
  await page.getByRole("button", { name: "Send", exact: true }).click();
  await expect(page.getByRole("button", { name: "Merge latest", exact: true })).toBeVisible();
  expect(await page.evaluate(() => window.revisionComposer.calls)).toEqual([]);
  expect((await second.evaluate(() => window.revisionComposer.read())).draft?.message).toBe("Second tab edit");
});

for (const transition of ["remount", "scope", "recovery"] as const) {
  test(`a delayed native write cannot stage into the replacement ${transition}`, async ({ page }) => {
    await load(page);
    await page.evaluate(() => {
      const native = IDBObjectStore.prototype.put;
      let held = false;
      let released = false;
      const state = window as unknown as { revisionWriteHeld: boolean; releaseRevisionWrite: () => void; revisionStageResult?: unknown };
      state.revisionWriteHeld = false;
      state.releaseRevisionWrite = () => { released = true; };
      IDBObjectStore.prototype.put = function (...args) {
        const request = native.apply(this, args);
        if (this.name === "ordinary_drafts" && !held) {
          held = true;
          state.revisionWriteHeld = true;
          IDBObjectStore.prototype.put = native;
          const keepAlive = () => { if (!released) this.get("test-only-keepalive").onsuccess = keepAlive; };
          keepAlive();
        }
        return request;
      };
      void window.revisionComposer.stage().then((result) => { state.revisionStageResult = result; });
    });
    await expect.poll(() => page.evaluate(() => (window as unknown as { revisionWriteHeld: boolean }).revisionWriteHeld)).toBe(true);
    await page.evaluate((transition) => window.revisionComposer.configure(transition === "scope" ? { otherScope: true } : { epoch: 1, locked: transition === "recovery" }), transition);
    await page.evaluate(() => (window as unknown as { releaseRevisionWrite: () => void }).releaseRevisionWrite());
    await expect.poll(() => page.evaluate(() => (window as unknown as { revisionStageResult: unknown }).revisionStageResult)).toEqual({ status: "stale" });
    if (transition === "scope") {
      await expect(page.getByRole("textbox")).toHaveValue("");
      await expect(page.getByTestId("composer-document-revision")).toHaveCount(0);
      await page.evaluate(() => window.revisionComposer.configure({ otherScope: false }));
    }
    await expect(page.getByRole("textbox")).toHaveValue("Shorten the title");
    await expect(page.getByTestId("composer-document-revision")).toBeVisible();
    expect((await page.evaluate(() => window.revisionComposer.read())).draft?.documentRevision?.target).toEqual(target);
  });
}

test("a failed native write retains the target and cannot become a text-only Send", async ({ page }) => {
  await load(page);
  const result = await page.evaluate(async () => {
    const native = IDBObjectStore.prototype.put;
    IDBObjectStore.prototype.put = function (...args) {
      if (this.name === "ordinary_drafts") throw new DOMException("Synthetic storage failure", "QuotaExceededError");
      return native.apply(this, args);
    };
    try { return await window.revisionComposer.stage(); }
    finally { IDBObjectStore.prototype.put = native; }
  });
  expect(result).toEqual({ status: "blocked", reason: "persistence_unavailable" });
  await expect(page.getByTestId("composer-document-revision")).toBeVisible();
  await expect(page.getByRole("button", { name: "Send", exact: true })).toBeDisabled();
  expect(await page.evaluate(() => window.revisionComposer.calls)).toEqual([]);
});

for (const exact of [true, false]) {
  test(`lost acknowledgement recovers only an ${exact ? "exact" : "unmatched"} target receipt without resending`, async ({ page }) => {
    await load(page);
    await stage(page);
    let sends = 0;
    await page.route("**/api/v1/chat/send", async (route) => { sends++; await route.abort("failed"); });
    await page.route("**/api/v1/chat/submissions/**", (route) => route.fulfill({ json: { outcome: "started", response: { ...started, document_revision_target: exact ? target : { ...target, draft_digest: "b".repeat(64) } } } }));
    await page.getByRole("button", { name: "Send", exact: true }).click();
    await expect.poll(() => page.evaluate(() => window.revisionComposer.settled)).toEqual([exact]);
    expect(sends).toBe(1);
    const saved = await page.evaluate(() => window.revisionComposer.read());
    if (exact) expect(saved).toEqual({ draft: null, pending: null });
    else expect(saved.pending?.wireSnapshot?.documentRevisionTarget).toEqual(target);
  });
}
