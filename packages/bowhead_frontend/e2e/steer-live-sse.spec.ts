import { expect, test, type Page } from "@playwright/test";

async function installOrdinaryDraftSettlementFailure(page: Page) {
  await page.addInitScript(() => {
    Object.defineProperty(window, "__failSteerSettlement", {
      configurable: true,
      writable: true,
      value: false,
    });
    const originalTransaction = IDBDatabase.prototype.transaction;
    Object.defineProperty(IDBDatabase.prototype, "transaction", {
      configurable: true,
      value: function (
        this: IDBDatabase,
        storeNames: string | string[],
        mode?: IDBTransactionMode,
        options?: IDBTransactionOptions,
      ): IDBTransaction {
        const names = typeof storeNames === "string"
          ? [storeNames]
          : Array.from(storeNames);
        if (
          (window as unknown as { __failSteerSettlement: boolean })
            .__failSteerSettlement
          && mode === "readwrite"
          && names.includes("ordinary_drafts")
        ) {
          throw new DOMException("forced settlement failure", "UnknownError");
        }
        return Reflect.apply(
          originalTransaction,
          this,
          [storeNames, mode, options],
        ) as IDBTransaction;
      },
    });
  });
}

async function installOneShotSteerMarkerWriteFailure(page: Page) {
  await page.addInitScript(() => {
    Object.defineProperty(window, "__failNextSteerMarkerWrite", {
      configurable: true,
      writable: true,
      value: false,
    });
    const originalTransaction = IDBDatabase.prototype.transaction;
    Object.defineProperty(IDBDatabase.prototype, "transaction", {
      configurable: true,
      value: function (
        this: IDBDatabase,
        storeNames: string | string[],
        mode?: IDBTransactionMode,
        options?: IDBTransactionOptions,
      ): IDBTransaction {
        const names = typeof storeNames === "string"
          ? [storeNames]
          : Array.from(storeNames);
        const state = window as unknown as {
          __failNextSteerMarkerWrite: boolean;
        };
        if (
          state.__failNextSteerMarkerWrite
          && mode === "readwrite"
          && names.includes("ordinary_drafts")
        ) {
          state.__failNextSteerMarkerWrite = false;
          throw new DOMException("forced marker write failure", "QuotaExceededError");
        }
        return Reflect.apply(
          originalTransaction,
          this,
          [storeNames, mode, options],
        ) as IDBTransaction;
      },
    });
  });
}

async function routeTerminalSessionDetail(
  page: Page,
  activeTarget: () => { turnId: string; generation: number } | null = () => null,
) {
  await page.route("**/api/v1/sessions/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    const match = path.match(/\/api\/v1\/sessions\/([^/]+)$/);
    if (request.method() !== "GET" || !match) {
      await route.continue();
      return;
    }
    const sessionId = match[1];
    const active = activeTarget();
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        chat_execution: {
          write_protocol: "terminal_v1",
          config_generation: "config-e2e-1",
          readiness_generation: "readiness-e2e-1",
          active_turn_id: active?.turnId ?? null,
          active_execution_generation: active?.generation ?? null,
        },
        session: {
          id: sessionId,
          workspace_id: "ws_test",
          title: "Terminal steer settlement fixture",
          status: "active",
        },
        turns: [],
      }),
    });
  });
}

test.describe("B09 live SSE steer dogfood", () => {
  test.skip(process.env.BOWHEAD_E2E_PROFILE !== "sse", "requires NEXT_PUBLIC_BOWHEAD_SSE_MODE=sse");

  test("streams through EventSource and buffers steer against the active turn", async ({ page }) => {
    const chatSendBodies: Array<Record<string, unknown>> = [];
    page.on("request", (request) => {
      const url = new URL(request.url());
      if (request.method() !== "POST" || url.pathname !== "/api/v1/chat/send") return;
      const raw = request.postData();
      if (!raw) return;
      try {
        chatSendBodies.push(JSON.parse(raw) as Record<string, unknown>);
      } catch {
        // ignore malformed test telemetry
      }
    });

    await page.goto("/");
    const textarea = page.locator("textarea").first();
    await expect(textarea).toBeVisible();

    await textarea.fill("start a live SSE steer fixture");
    await textarea.press("Enter");

    await expect(page.getByRole("button", { name: /停止|Stop/ })).toBeVisible();
    await expect(textarea).toBeEnabled();
    await expect(page.getByText("Live SSE turn is running.")).toBeVisible();
    await expect.poll(() => {
      const session = new URL(page.url()).searchParams.get("session") ?? "";
      return session;
    }).toMatch(/^s_sse_/);

    const steerResponsePromise = page.waitForResponse((response) => {
      const req = response.request();
      return req.method() === "POST"
        && new URL(response.url()).pathname === "/api/v1/chat/send"
        && (req.postData() ?? "").includes("please include west region");
    });
    await textarea.fill("please include west region");
    await textarea.press("Enter");

    const steerResponse = await steerResponsePromise;
    await expect(textarea).toHaveValue("");
    await expect(
      page.locator(".bh-steer-mount").getByText("please include west region"),
    ).toBeVisible();

    const steerJson = await steerResponse.json();
    expect(steerJson).toMatchObject({
      status: "buffered",
      stream_url: expect.stringMatching(/^\/api\/v1\/chat\/stream\/t_sse_/),
    });

    const steerBody = chatSendBodies.find((body) => body.message === "please include west region");
    expect(steerBody).toBeTruthy();
    expect(steerBody).toMatchObject({
      session_id: expect.stringMatching(/^s_sse_/),
      client_info: {
        expected_turn_id: expect.stringMatching(/^t_sse_/),
      },
    });
    expect(steerBody).not.toHaveProperty("client_submission_id");
    expect(steerBody).not.toHaveProperty("submission_kind");
    expect(steerBody).not.toHaveProperty("submission_contract_version");
    expect(steerBody).not.toHaveProperty("model_control_batch");
    expect(steerBody).not.toHaveProperty("expected_parent_turn_id");
    expect(steerBody).not.toHaveProperty("expected_execution_generation");
    expect(chatSendBodies.filter((body) => body.message === "start a live SSE steer fixture")).toHaveLength(1);
    expect(chatSendBodies.filter((body) => body.message === "please include west region")).toHaveLength(1);
  });

  test("keeps Terminal steer recovery locked when local marker settlement fails", async ({ page }) => {
    await installOrdinaryDraftSettlementFailure(page);
    await routeTerminalSessionDetail(page);
    let steerPosts = 0;
    await page.route("**/api/v1/chat/send", async (route) => {
      const body = route.request().postDataJSON() as {
        message?: string;
        session_id?: string;
        client_info?: {
          expected_turn_id?: string;
          expected_execution_generation?: string;
        };
      };
      if (body.message !== "settlement must stay locked") {
        await route.continue();
        return;
      }
      steerPosts += 1;
      await page.evaluate(() => {
        (window as unknown as { __failSteerSettlement: boolean })
          .__failSteerSettlement = true;
      });
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          session_id: body.session_id,
          turn_id: body.client_info?.expected_turn_id,
          stream_url: `/api/v1/chat/stream/${body.client_info?.expected_turn_id}`,
          status: "buffered",
          execution_generation: Number(
            body.client_info?.expected_execution_generation,
          ),
          terminal_protocol_version: 1,
        }),
      });
    });

    await page.goto("/");
    const textarea = page.locator("textarea").first();
    await textarea.fill("start Terminal steer settlement fixture");
    await textarea.press("Enter");
    await expect(page.getByRole("button", { name: /停止|Stop/ })).toBeVisible();

    await textarea.fill("settlement must stay locked");
    await textarea.press("Enter");

    await expect.poll(() => steerPosts).toBe(1);
    await expect(page.getByRole("region", { name: "发送状态待确认" })).toBeVisible();
    await expect(textarea).toHaveAttribute("readonly", "");
    await expect(textarea).toHaveValue("settlement must stay locked");
    await expect(page.getByRole("button", { name: "恢复草稿或再次发送" }))
      .toHaveCount(0);
    await textarea.press("Enter");
    expect(steerPosts).toBe(1);
  });

  test("settles an ambiguous steer POST through one GET without reloading", async ({ page }) => {
    await page.addInitScript(() => {
      const key = "__steerLookupDocumentLoads";
      sessionStorage.setItem(
        key,
        String(Number(sessionStorage.getItem(key) ?? "0") + 1),
      );
    });
    let activeTarget: { turnId: string; generation: number } | null = null;
    let activeSessionId = "";
    await routeTerminalSessionDetail(page, () => activeTarget);
    let steerPosts = 0;
    let steerLookups = 0;
    let steerActionId = "";
    await page.route("**/api/v1/chat/actions/**", async (route) => {
      steerLookups += 1;
      const actionId = new URL(route.request().url()).pathname.split("/").at(-1) ?? "";
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          action_id: actionId,
          action_kind: "steer",
          outcome: "accepted",
          session_id: activeSessionId,
          turn_id: activeTarget?.turnId,
          execution_generation: activeTarget?.generation,
          terminal_protocol_version: 1,
          stream_url: `/api/v1/chat/stream/${activeTarget?.turnId}`,
        }),
      });
    });
    await page.route("**/api/v1/chat/send", async (route) => {
      const body = route.request().postDataJSON() as {
        message?: string;
        client_info?: { request_id?: string };
      };
      if (body.message !== "ambiguous steer converges by lookup") {
        await route.continue();
        return;
      }
      steerPosts += 1;
      steerActionId = body.client_info?.request_id ?? "";
      await route.fulfill({
        status: 409,
        contentType: "application/json",
        body: JSON.stringify({
          detail: {
            error: {
              code: "terminal_protocol_admission_rejected",
              message: "action outcome requires lookup",
            },
          },
        }),
      });
    });

    await page.goto("/");
    const textarea = page.locator("textarea").first();
    const startResponsePromise = page.waitForResponse((response) => {
      const request = response.request();
      return request.method() === "POST"
        && new URL(response.url()).pathname === "/api/v1/chat/send"
        && (request.postData() ?? "").includes("start ambiguous steer fixture");
    });
    await textarea.fill("start ambiguous steer fixture");
    await textarea.press("Enter");
    const startBody = await startResponsePromise.then((response) => response.json()) as {
      session_id: string;
      turn_id: string;
      execution_generation?: number;
    };
    activeTarget = {
      turnId: startBody.turn_id,
      generation: startBody.execution_generation ?? 1,
    };
    activeSessionId = startBody.session_id;
    await expect(page.getByRole("button", { name: /停止|Stop/ })).toBeVisible();
    expect(activeSessionId).toMatch(/^s_sse_/);
    const documentLoadsBeforeSteer = await page.evaluate(() => (
      sessionStorage.getItem("__steerLookupDocumentLoads")
    ));

    await textarea.fill("ambiguous steer converges by lookup");
    await textarea.press("Enter");

    await expect.poll(() => steerPosts).toBe(1);
    await expect.poll(() => steerLookups).toBe(1);
    expect(steerActionId).toMatch(/^act_steer_[0-9a-f]{32}$/);
    await expect(page.getByRole("region", { name: "发送状态待确认" })).toHaveCount(0);
    await expect(textarea).not.toHaveAttribute("readonly", "");
    await expect(textarea).toHaveValue("");
    expect(await page.evaluate(() => (
      sessionStorage.getItem("__steerLookupDocumentLoads")
    ))).toBe(documentLoadsBeforeSteer);
    expect(steerPosts).toBe(1);
  });

  test("falls back to one in-memory steer when marker persistence becomes unavailable", async ({ page }) => {
    await installOneShotSteerMarkerWriteFailure(page);
    await routeTerminalSessionDetail(page);
    let steerPosts = 0;
    await page.route("**/api/v1/chat/send", async (route) => {
      const body = route.request().postDataJSON() as {
        message?: string;
        session_id?: string;
        client_info?: {
          expected_turn_id?: string;
          expected_execution_generation?: string;
        };
      };
      if (body.message !== "one in-memory steer") {
        await route.continue();
        return;
      }
      steerPosts += 1;
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          session_id: body.session_id,
          turn_id: body.client_info?.expected_turn_id,
          stream_url: `/api/v1/chat/stream/${body.client_info?.expected_turn_id}`,
          status: "buffered",
          execution_generation: Number(
            body.client_info?.expected_execution_generation,
          ),
          terminal_protocol_version: 1,
        }),
      });
    });
    await page.goto("/");
    const textarea = page.locator("textarea").first();
    await textarea.fill("start marker fallback steer fixture");
    await textarea.press("Enter");
    await expect(page.getByRole("button", { name: /停止|Stop/ })).toBeVisible();

    await textarea.fill("one in-memory steer");
    await page.evaluate(() => {
      (window as unknown as { __failNextSteerMarkerWrite: boolean })
        .__failNextSteerMarkerWrite = true;
    });
    await textarea.press("Enter");

    await expect.poll(() => steerPosts).toBe(1);
    await expect(textarea).toHaveValue("");
    expect(steerPosts).toBe(1);
  });

  test("keeps a pending steer single-attempt across a workspace roundtrip", async ({ page }) => {
    await routeTerminalSessionDetail(page);
    let steerPosts = 0;
    let releaseSteer: () => void = () => {};
    const steerReleased = new Promise<void>((resolve) => {
      releaseSteer = resolve;
    });
    await page.route("**/api/v1/chat/send", async (route) => {
      const body = route.request().postDataJSON() as {
        message?: string;
        session_id?: string;
        client_info?: {
          expected_turn_id?: string;
          expected_execution_generation?: string;
        };
      };
      if (body.message !== "pending steer survives roundtrip") {
        await route.continue();
        return;
      }
      steerPosts += 1;
      await steerReleased;
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          session_id: body.session_id,
          turn_id: body.client_info?.expected_turn_id,
          stream_url: `/api/v1/chat/stream/${body.client_info?.expected_turn_id}`,
          status: "buffered",
          execution_generation: Number(
            body.client_info?.expected_execution_generation,
          ),
          terminal_protocol_version: 1,
        }),
      });
    });
    await page.goto("/");
    const textarea = page.locator("textarea").first();
    await textarea.fill("start steer roundtrip fixture");
    await textarea.press("Enter");
    await expect(page.getByRole("button", { name: /停止|Stop/ })).toBeVisible();
    await textarea.fill("pending steer survives roundtrip");
    await textarea.press("Enter");
    await expect.poll(() => steerPosts).toBe(1);

    await page.getByTitle("Test Workspace").first().click();
    await page.getByRole("menuitem", { name: /Alice Workspace/ }).click();
    await page.getByTitle("Alice Workspace").first().click();
    await page.getByRole("menuitem", { name: /Test Workspace/ }).click();

    await expect(page.getByRole("region", { name: "发送状态待确认" })).toBeVisible();
    await expect(page.getByRole("button", { name: "恢复草稿或再次发送" }))
      .toHaveCount(0);
    expect(steerPosts).toBe(1);
    releaseSteer();
    await expect.poll(() => steerPosts).toBe(1);
    await expect(page.getByRole("region", { name: "发送状态待确认" })).toHaveCount(0);
    await expect(textarea).not.toHaveAttribute("readonly", "");
    await expect(textarea).toHaveValue("");
  });

  test("unlocks a stale steer resend when its exact active target has ended", async ({ page }) => {
    let activeTarget: { turnId: string; generation: number } | null = null;
    await routeTerminalSessionDetail(page, () => activeTarget);
    let candidatePosts = 0;
    await page.route("**/api/v1/chat/actions/**", async (route) => {
      await route.fulfill({
        status: 404,
        contentType: "application/json",
        body: JSON.stringify({
          detail: { error: { code: "chat_action_not_found" } },
        }),
      });
    });
    await page.route("**/api/v1/chat/send", async (route) => {
      const body = route.request().postDataJSON() as { message?: string };
      if (body.message === "stale steer must unlock") {
        candidatePosts += 1;
        await route.abort("failed");
        return;
      }
      await route.continue();
    });
    await page.goto("/");
    const textarea = page.locator("textarea").first();
    const startResponsePromise = page.waitForResponse((response) => {
      const request = response.request();
      return request.method() === "POST"
        && new URL(response.url()).pathname === "/api/v1/chat/send"
        && (request.postData() ?? "").includes("start stale steer fixture");
    });
    await textarea.fill("start stale steer fixture");
    await textarea.press("Enter");
    const startBody = await startResponsePromise.then((response) => response.json()) as {
      turn_id: string;
      execution_generation?: number;
    };
    activeTarget = {
      turnId: startBody.turn_id,
      generation: startBody.execution_generation ?? 1,
    };
    await expect(page.getByRole("button", { name: /停止|Stop/ })).toBeVisible();

    await textarea.fill("stale steer must unlock");
    await textarea.press("Enter");
    await expect.poll(() => candidatePosts).toBe(1);
    await expect(page.getByRole("region", { name: "发送状态待确认" })).toBeVisible();

    activeTarget = null;
    await page.getByRole("button", { name: "重新核对结果" }).click();
    await page.getByRole("button", { name: "恢复草稿或再次发送" }).click();

    await expect(page.getByRole("region", { name: "发送状态待确认" })).toHaveCount(0);
    await expect(textarea).not.toHaveAttribute("readonly", "");
    await expect(textarea).toHaveValue("stale steer must unlock");
    expect(candidatePosts).toBe(1);
  });

  for (const { settlementFails, rejectionCode, status } of [
    { settlementFails: false, rejectionCode: "request_validation_failed", status: 422 },
    { settlementFails: true, rejectionCode: "request_validation_failed", status: 422 },
    { settlementFails: false, rejectionCode: "client_refresh_required", status: 422 },
    { settlementFails: true, rejectionCode: "auth_required", status: 401 },
  ]) {
    test(`handles ${rejectionCode} Terminal steer when marker settlement ${settlementFails ? "fails" : "succeeds"}`, async ({ page }) => {
      if (settlementFails) await installOrdinaryDraftSettlementFailure(page);
      await routeTerminalSessionDetail(page);
      let steerPosts = 0;
      let authMeRequests = 0;
      page.on("request", (request) => {
        if (new URL(request.url()).pathname === "/api/v1/auth/me") {
          authMeRequests += 1;
        }
      });
      await page.route("**/api/v1/chat/send", async (route) => {
        const body = route.request().postDataJSON() as { message?: string };
        if (body.message !== "reject this Terminal steer") {
          await route.continue();
          return;
        }
        steerPosts += 1;
        if (settlementFails) {
          await page.evaluate(() => {
            (window as unknown as { __failSteerSettlement: boolean })
              .__failSteerSettlement = true;
          });
        }
        await route.fulfill({
          status,
          contentType: "application/json",
          body: JSON.stringify({
            detail: {
              code: rejectionCode,
              message: "请检查标出的内容后重试。",
              field_errors: [],
            },
          }),
        });
      });

      await page.goto("/");
      const textarea = page.locator("textarea").first();
      await textarea.fill("start rejected Terminal steer fixture");
      await textarea.press("Enter");
      await expect(page.getByRole("button", { name: /停止|Stop/ })).toBeVisible();

      await textarea.fill("reject this Terminal steer");
      await textarea.press("Enter");
      await expect.poll(() => steerPosts).toBe(1);
      if (rejectionCode === "auth_required") {
        await expect.poll(() => authMeRequests).toBeGreaterThanOrEqual(2);
      }

      if (settlementFails) {
        await expect(page.getByRole("region", { name: "发送状态待确认" })).toBeVisible();
        await expect(textarea).toHaveAttribute("readonly", "");
      } else {
        await expect(page.getByText(rejectionCode === "client_refresh_required"
          ? "当前页面需要刷新后才能安全发送，草稿已保留。"
          : "补充内容没有发送成功，已保留输入，请重试。"))
          .toBeVisible();
        await expect(page.getByRole("region", { name: "发送状态待确认" })).toHaveCount(0);
        await expect(textarea).not.toHaveAttribute("readonly", "");
        await expect(textarea).toHaveValue("reject this Terminal steer");
      }
      expect(steerPosts).toBe(1);
    });
  }
});
