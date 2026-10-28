import { expect, test, type Page } from "@playwright/test";
import { installReferenceComponent } from "./_composer-reference-component-modules";
import { referenceDraftInput } from "./_composer-reference-fixtures";

const started = {
  status: "started",
  session_id: "s_reference_send",
  turn_id: "t_reference_send",
  stream_url: "/api/v1/chat/stream/t_reference_send",
  execution_generation: 1,
};
async function load(page: Page) {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.addInitScript(() => {
    // Next normally inlines public env values. This standalone browser bundle
    // uses the unset defaults, without replacing any runner or persistence API.
    (
      globalThis as unknown as { process: { env: Record<string, string> } }
    ).process = { env: {} };
    localStorage.setItem(
      "bowhead.user_profile",
      JSON.stringify({ display_name: "Later profile" }),
    );
    class QuietEventSource {
      static CONNECTING = 0;
      static OPEN = 1;
      static CLOSED = 2;
      readyState = 1;
      onerror = null;
      constructor(readonly url: string) {}
      addEventListener() {}
      removeEventListener() {}
      close() {
        this.readyState = 2;
      }
    }
    window.EventSource = QuietEventSource as unknown as typeof EventSource;
  });
  await installReferenceComponent(
    page,
    "",
    "_composer-reference-send-entry.tsx",
  );
  await page.goto("/__reference_component_test");
  try {
    await expect(page.getByRole("combobox")).toBeVisible();
  } catch (error) {
    throw new Error(JSON.stringify(errors) + String(error));
  }
}
async function submit(page: Page) {
  await page.getByRole("combobox").press("End");
  await page.getByRole("combobox").press("Enter");
}

test("editor sends the complete durable reference source once and adopts a confirmed receipt", async ({
  page,
}) => {
  const posts: Record<string, unknown>[] = [];
  await load(page);
  await page.evaluate(() =>
    window.referenceSend.configure({ pendingMutation: true }),
  );
  await page.route("**/api/v1/chat/send", async (route) => {
    const state = await page.evaluate(() => window.referenceSend.read());
    const body = route.request().postDataJSON();
    expect(state.pending?.clientSubmissionId).toBe(body.client_submission_id);
    expect(state.pending?.input_document).toEqual(
      referenceDraftInput.input_document,
    );
    expect(state.draft?.input_document).toEqual(
      referenceDraftInput.input_document,
    );
    expect(route.request().headers()["content-type"]).toBe(
      "application/vnd.bowhead.chat-references.v1+json",
    );
    posts.push(body);
    await route.fulfill({ json: started });
  });
  await submit(page);
  await expect(page.locator("#turn")).toHaveText(started.turn_id);
  expect(posts).toHaveLength(1);
  expect(posts[0]).toMatchObject({
    message: referenceDraftInput.message,
    input_document: referenceDraftInput.input_document,
    submission_contract_version: "chat-references.v1",
    model_control_batch: "references",
    submission_kind: "start",
    preferred_provider: "original-provider",
    preferred_model: "original-model",
  });
  expect(posts[0]).not.toHaveProperty("user_profile");
  expect(await page.evaluate(() => window.referenceSend.read())).toMatchObject({
    pending: null,
  });
  expect(await page.evaluate(() => window.referenceSend.settled)).toEqual([
    true,
  ]);
});

test("a lost response keeps original bytes and a reload retries only the original identity", async ({
  page,
}) => {
  const ids: string[] = [];
  await load(page);
  await page.route("**/api/v1/chat/submissions/**", (route) =>
    route.fulfill({ status: 404, json: {} }),
  );
  await page.route("**/api/v1/chat/send", (route) => {
    ids.push(route.request().postDataJSON().client_submission_id);
    return ids.length === 1
      ? route.abort("failed")
      : route.fulfill({ json: started });
  });
  await submit(page);
  await expect
    .poll(() => page.evaluate(() => window.referenceSend.settled))
    .toEqual([false]);
  const before = await page.evaluate(() => window.referenceSend.read());
  expect(before.pending?.input_document).toEqual(
    referenceDraftInput.input_document,
  );
  await page.reload();
  await expect(page.getByRole("combobox")).toBeVisible();
  expect(ids).toHaveLength(1);
  await page.locator("#recover").click();
  await expect(page.locator("#turn")).toHaveText(started.turn_id);
  expect(ids).toEqual([
    before.pending!.clientSubmissionId,
    before.pending!.clientSubmissionId,
  ]);
});

test("a lost POST response adopts a confirmed original receipt without another POST", async ({
  page,
}) => {
  await load(page);
  const ids: string[] = [];
  await page.route("**/api/v1/chat/send", (route) => {
    ids.push(route.request().postDataJSON().client_submission_id);
    return route.abort("failed");
  });
  const receiptIds: string[] = [];
  await page.route("**/api/v1/chat/submissions/**", (route) => {
    receiptIds.push(new URL(route.request().url()).pathname.split("/").at(-1)!);
    return route.fulfill({ json: { outcome: "started", response: started } });
  });
  await submit(page);
  await expect(page.locator("#turn")).toHaveText(started.turn_id);
  expect(ids).toHaveLength(1);
  expect(receiptIds).toEqual(ids);
  expect(
    (await page.evaluate(() => window.referenceSend.read())).pending,
  ).toBeNull();
});

test("an expired login keeps the pending source and never refreshes or retries the POST", async ({
  page,
}) => {
  await load(page);
  let posts = 0;
  let refreshes = 0;
  await page.route("**/api/v1/chat/send", (route) => {
    posts++;
    return route.fulfill({ status: 401, json: { detail: "expired login" } });
  });
  await page.route("**/api/v1/auth/refresh", (route) => {
    refreshes++;
    return route.fulfill({ status: 200, json: {} });
  });
  await submit(page);
  await expect
    .poll(() => page.evaluate(() => window.referenceSend.settled))
    .toEqual([false]);
  expect(posts).toBe(1);
  expect(refreshes).toBe(0);
  expect(
    (await page.evaluate(() => window.referenceSend.read())).pending
      ?.input_document,
  ).toEqual(referenceDraftInput.input_document);
  expect(await page.evaluate(() => window.referenceSend.cleared)).toEqual([]);
});

test("an invalid receipt after a lost POST cannot adopt a turn or clear the source", async ({
  page,
}) => {
  await load(page);
  await page.route("**/api/v1/chat/send", (route) => route.abort("failed"));
  await page.route("**/api/v1/chat/submissions/**", (route) =>
    route.fulfill({
      json: {
        outcome: "started",
        response: { ...started, execution_generation: 2 },
      },
    }),
  );
  await submit(page);
  await expect
    .poll(() => page.evaluate(() => window.referenceSend.settled))
    .toEqual([false]);
  await expect(page.locator("#turn")).toBeEmpty();
  expect(
    (await page.evaluate(() => window.referenceSend.read())).pending
      ?.input_document,
  ).toEqual(referenceDraftInput.input_document);
  expect(await page.evaluate(() => window.referenceSend.cleared)).toEqual([]);
});

for (const response of [
  {},
  { ...started, status: "buffered" },
  { ...started, terminal_protocol_version: 1 },
  { ...started, stream_url: "/api/v1/chat/stream/t_other" },
])
  test(`invalid acknowledgement retains the source: ${JSON.stringify(response)}`, async ({
    page,
  }) => {
    await load(page);
    await page.route("**/api/v1/chat/send", (route) =>
      route.fulfill({ json: response }),
    );
    await page.route("**/api/v1/chat/submissions/**", (route) =>
      route.fulfill({ status: 404, json: {} }),
    );
    await submit(page);
    await expect
      .poll(() => page.evaluate(() => window.referenceSend.settled))
      .toEqual([false]);
    const saved = await page.evaluate(() => window.referenceSend.read());
    expect(saved.pending?.input_document).toEqual(
      referenceDraftInput.input_document,
    );
    expect(await page.evaluate(() => window.referenceSend.cleared)).toEqual([]);
    await expect(page.locator("#turn")).toBeEmpty();
  });

test("closed adapter preserves the ordinary reference draft without a POST or pending identity", async ({
  page,
}) => {
  await load(page);
  const posts: string[] = [];
  await page.route("**/api/v1/chat/send", (route) => {
    posts.push(route.request().url());
    return route.fulfill({ json: started });
  });
  await page.evaluate(() => window.referenceSend.configure({ enabled: false }));
  await submit(page);
  await expect(page.locator("#error")).toHaveText("unsupported_entry");
  const saved = await page.evaluate(() => window.referenceSend.read());
  expect(saved.draft?.input_document).toEqual(
    referenceDraftInput.input_document,
  );
  expect(saved.pending).toBeNull();
  expect(posts).toEqual([]);
});

test("a failed durable write prevents the HTTP call and preserves the editable source", async ({
  page,
}) => {
  await load(page);
  const posts: string[] = [];
  await page.route("**/api/v1/chat/send", (route) => {
    posts.push(route.request().url());
    return route.fulfill({ json: started });
  });
  await page.evaluate(() => {
    const put = IDBObjectStore.prototype.put;
    IDBObjectStore.prototype.put = function (...args) {
      if (this.name === "manifests")
        throw new Error("injected manifest write failure");
      return Reflect.apply(put, this, args);
    };
  });
  await submit(page);
  await expect
    .poll(() => page.evaluate(() => window.referenceSend.settled))
    .toEqual([false]);
  const saved = await page.evaluate(() => window.referenceSend.read());
  expect(saved.draft?.input_document).toEqual(
    referenceDraftInput.input_document,
  );
  expect(saved.pending).toBeNull();
  expect(posts).toEqual([]);
});

test("a committed server result does not clear the pending UI fence when local cleanup fails", async ({
  page,
}) => {
  await load(page);
  await page.route("**/api/v1/chat/send", async (route) => {
    await page.evaluate(() => {
      const remove = IDBObjectStore.prototype.delete;
      IDBObjectStore.prototype.delete = function (...args) {
        if (this.name === "manifests")
          throw new Error("injected cleanup failure");
        return Reflect.apply(remove, this, args);
      };
    });
    await route.fulfill({ json: started });
  });
  await submit(page);
  // The accepted receipt settles as "not adopted" while the local journal still
  // holds the submission: the runner stays on the composer so the saved source
  // can be retried under the same client submission ID.
  await expect
    .poll(() => page.evaluate(() => window.referenceSend.settled))
    .toEqual([false]);
  await expect(page.locator("#turn")).toHaveText("");
  expect(
    (await page.evaluate(() => window.referenceSend.read())).pending
      ?.input_document,
  ).toEqual(referenceDraftInput.input_document);
  expect(await page.evaluate(() => window.referenceSend.cleared)).toEqual([]);
});
