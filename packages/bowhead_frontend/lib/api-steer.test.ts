import { afterEach, describe, expect, it, vi } from "vitest";

import {
  fetchSessionChatExecutionPreflight,
  fetchSessionDetail,
  postSteerMessage,
} from "./api";
import type { ComposerChatAction } from "./composer-chat-action";
import type { InputDocument } from "./composer-input-document";
import { viewerTimeZone } from "./time-zone";

const originalFetch = globalThis.fetch;

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

afterEach(() => {
  globalThis.fetch = originalFetch;
  window.__BOWHEAD_BASE_PATH__ = undefined;
  vi.restoreAllMocks();
});

describe("postSteerMessage submission recovery", () => {
  const terminalSteerAction: ComposerChatAction = {
    actionId: "act_steer_00112233445546778899aabbccddeeff",
    actionKind: "steer",
    attempted: true,
    sourceRevision: null,
    sessionId: "s_1",
    expectedTurnId: "t_parent",
    expectedExecutionGeneration: 2,
  };

  it("declares Terminal reader capability on Session bootstrap", async () => {
    let requestHeaders: HeadersInit | undefined;
    globalThis.fetch = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
      requestHeaders = init?.headers;
      return jsonResponse(200, {
        chat_execution: {
          write_protocol: "terminal_v1",
          config_generation: "config-1",
          readiness_generation: "ready-1",
        },
        session: {},
        turns: [],
      });
    }) as typeof fetch;

    await fetchSessionDetail("s_1");

    expect(new Headers(requestHeaders).get(
      "X-Bowhead-Terminal-Protocol-Capability",
    )).toBe("1");
  });

  it("uses the same capability header on the bounded action preflight", async () => {
    window.__BOWHEAD_BASE_PATH__ = "/bowhead";
    let requestInput: RequestInfo | URL | undefined;
    let requestHeaders: HeadersInit | undefined;
    let requestCache: RequestCache | undefined;
    globalThis.fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      requestInput = input;
      requestHeaders = init?.headers;
      requestCache = init?.cache;
      return jsonResponse(200, { session: {}, turns: [] });
    }) as typeof fetch;

    await fetchSessionChatExecutionPreflight("s_1");

    expect(requestInput).toBe("/bowhead/api/v1/sessions/s_1");
    expect(requestCache).toBe("no-store");
    expect(new Headers(requestHeaders).get(
      "X-Bowhead-Terminal-Protocol-Capability",
    )).toBe("1");
  });

  it("sends a Terminal steer exactly once with its frozen target", async () => {
    let postCount = 0;
    let body: Record<string, unknown> = {};
    let headers = new Headers();
    globalThis.fetch = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
      postCount += 1;
      body = JSON.parse(String(init?.body)) as Record<string, unknown>;
      headers = new Headers(init?.headers);
      return jsonResponse(200, {
        session_id: "s_1",
        turn_id: "t_parent",
        stream_url: "/api/v1/chat/stream/t_parent",
        status: "buffered",
        execution_generation: 2,
        terminal_protocol_version: 1,
      });
    }) as typeof fetch;

    await expect(postSteerMessage(
      "ws_1",
      "s_1",
      "steer once",
      "t_parent",
      2,
      [],
      undefined,
      null,
      terminalSteerAction,
    )).resolves.toMatchObject({ status: "buffered" });

    expect(postCount).toBe(1);
    expect(body).toMatchObject({
      client_info: {
        request_id: terminalSteerAction.actionId,
        action_kind: "steer",
        user_msg_id: "u_r_00112233445546778899aabbccddeeff",
        expected_turn_id: "t_parent",
        expected_execution_generation: "2",
      },
    });
    expect(body).not.toHaveProperty("attachment_refs");
    expect(body).not.toHaveProperty("client_submission_id");
    expect(headers.get("X-Bowhead-Time-Zone")).toBe(viewerTimeZone());
  });

  it("rejects a Terminal steer target mismatch before network", async () => {
    const fetchSpy = vi.fn();
    globalThis.fetch = fetchSpy as typeof fetch;

    await expect(postSteerMessage(
      "ws_1",
      "s_other",
      "mismatched steer",
      "t_parent",
      2,
      [],
      undefined,
      null,
      terminalSteerAction,
    )).rejects.toThrow("target mismatch");
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it("rejects Terminal steer attachments before network", async () => {
    const fetchSpy = vi.fn();
    globalThis.fetch = fetchSpy as typeof fetch;

    await expect(postSteerMessage(
      "ws_1",
      "s_1",
      "unsupported attachment",
      "t_parent",
      2,
      [{ fileId: "art_1", filename: "a.pdf", size: 1, mime: "application/pdf" }],
      undefined,
      null,
      terminalSteerAction,
    )).rejects.toThrow("attachments require durable submission support");
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it("rejects identity-free legacy steer attachments before network", async () => {
    const fetchSpy = vi.fn();
    globalThis.fetch = fetchSpy as typeof fetch;

    await expect(postSteerMessage(
      "ws_1",
      "s_1",
      "legacy steer",
      "t_parent",
      2,
      [{ fileId: "art_1", filename: "a.pdf", size: 1, mime: "application/pdf" }],
    )).rejects.toThrow("attachments require durable submission support");

    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it("sends typed refs for a legacy submission steer with attachments", async () => {
    let body: Record<string, unknown> = {};
    globalThis.fetch = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
      body = JSON.parse(String(init?.body)) as Record<string, unknown>;
      return jsonResponse(200, {
        session_id: "s_1",
        turn_id: "t_parent",
        stream_url: "/api/v1/chat/stream/t_parent",
        status: "buffered",
        execution_generation: 2,
      });
    }) as typeof fetch;

    await postSteerMessage(
      "ws_1",
      "s_1",
      "legacy submission steer",
      "t_parent",
      2,
      [{ fileId: "art_1", filename: "a.pdf", size: 1, mime: "application/pdf" }],
      undefined,
      "018f0cc3-e4a2-7b21-a109-001122334533",
    );

    expect(body).toMatchObject({
      attachments: ["art_1"],
      attachment_refs: [{
        fileId: "art_1",
        filename: "a.pdf",
        size: 1,
        mime: "application/pdf",
      }],
      client_submission_id: "018f0cc3-e4a2-7b21-a109-001122334533",
      submission_kind: "steer",
    });
  });

  it("sends an exact reference steer under the dedicated media contract", async () => {
    let body: Record<string, unknown> = {};
    let headers = new Headers();
    const document = {
      schema_version: 1,
      nodes: [
        { type: "text", text: "请继续 " },
        {
          type: "reference",
          node_id: "n_agent",
          label: "经营分析师",
          parameters: {},
          resource: {
            kind: "agent",
            source_kind: "team",
            id: "osa_1",
            version: "rev_1",
            assignment_id: "assign_1",
            release_id: "release_1",
          },
        },
      ],
    } as InputDocument;
    globalThis.fetch = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
      headers = new Headers(init?.headers);
      body = JSON.parse(String(init?.body)) as Record<string, unknown>;
      return jsonResponse(200, {
        session_id: "s_1",
        turn_id: "t_parent",
        stream_url: "/api/v1/chat/stream/t_parent",
        status: "buffered",
        execution_generation: 2,
      });
    }) as typeof fetch;

    await postSteerMessage(
      "ws_1",
      "s_1",
      "请继续 @经营分析师",
      "t_parent",
      2,
      [],
      undefined,
      "018f0cc3-e4a2-7b21-a109-001122334533",
      null,
      document,
      () => true,
    );

    expect(headers.get("content-type")).toBe("application/vnd.bowhead.chat-references.v1+json");
    expect(body).toMatchObject({
      client_submission_id: "018f0cc3-e4a2-7b21-a109-001122334533",
      submission_kind: "steer",
      submission_contract_version: "chat-references.v1",
      model_control_batch: "references",
      input_document: document,
      expected_parent_turn_id: "t_parent",
      expected_execution_generation: 2,
    });
  });

  it("waits for a slow reference steer instead of cutting it off at 15s, like an ordinary steer", async () => {
    vi.useFakeTimers();
    const document = {
      schema_version: 1,
      nodes: [
        { type: "text", text: "请继续 " },
        {
          type: "reference",
          node_id: "n_agent",
          label: "经营分析师",
          parameters: {},
          resource: { kind: "agent", source_kind: "team", id: "osa_1", version: "rev_1", assignment_id: "assign_1", release_id: "release_1" },
        },
      ],
    } as InputDocument;
    let aborted = false;
    let release!: () => void;
    globalThis.fetch = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
      init?.signal?.addEventListener("abort", () => { aborted = true; }, { once: true });
      await new Promise<void>((resolve) => { release = resolve; });
      return jsonResponse(200, {
        session_id: "s_1", turn_id: "t_parent", stream_url: "/api/v1/chat/stream/t_parent", status: "buffered", execution_generation: 2,
      });
    }) as typeof fetch;

    const pending = postSteerMessage(
      "ws_1", "s_1", "请继续 @经营分析师", "t_parent", 2, [], undefined,
      "018f0cc3-e4a2-7b21-a109-001122334534", null, document, () => true,
    );
    await vi.advanceTimersByTimeAsync(60_000);
    expect(aborted).toBe(false);
    release();
    await expect(pending).resolves.toBeTruthy();
    expect(globalThis.fetch).toHaveBeenCalledTimes(1);
    vi.useRealTimers();
  });

  it("rejects a reference steer after its composer scope changes", async () => {
    const fetchSpy = vi.fn();
    globalThis.fetch = fetchSpy as typeof fetch;

    await expect(postSteerMessage(
      "ws_1",
      "s_1",
      "reference steer",
      "t_parent",
      2,
      [],
      undefined,
      "018f0cc3-e4a2-7b21-a109-001122334533",
      null,
      { schema_version: 1, nodes: [{ type: "text", text: "reference steer" }] },
      () => false,
    )).rejects.toMatchObject({ code: "client_refresh_required" });
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it("rejects a reference steer response that does not match its frozen target", async () => {
    let postCount = 0;
    globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
      if (String(input).includes("/api/v1/chat/submissions/")) {
        return jsonResponse(404, {});
      }
      postCount += 1;
      return jsonResponse(200, {
        session_id: "s_other",
        turn_id: "t_parent",
        stream_url: "/api/v1/chat/stream/t_parent",
        status: "buffered",
        execution_generation: 2,
      });
    }) as typeof fetch;

    await expect(postSteerMessage(
      "ws_1",
      "s_1",
      "reference steer",
      "t_parent",
      2,
      [],
      undefined,
      "018f0cc3-e4a2-7b21-a109-001122334533",
      null,
      { schema_version: 1, nodes: [{ type: "text", text: "reference steer" }] },
      () => true,
    )).rejects.toThrow("target mismatch");
    expect(postCount).toBe(2);
  });

  it("rejects a reference steer receipt bound to another execution", async () => {
    let postCount = 0;
    let receiptCount = 0;
    globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
      if (String(input).includes("/api/v1/chat/submissions/")) {
        receiptCount += 1;
        return jsonResponse(200, {
          client_submission_id: "018f0cc3-e4a2-7b21-a109-001122334533",
          outcome: "buffered",
          response: {
            session_id: "s_1",
            turn_id: "t_parent",
            stream_url: "/api/v1/chat/stream/t_parent",
            status: "buffered",
            execution_generation: 3,
          },
        });
      }
      postCount += 1;
      throw new TypeError("response lost");
    }) as typeof fetch;

    await expect(postSteerMessage(
      "ws_1",
      "s_1",
      "reference steer",
      "t_parent",
      2,
      [],
      undefined,
      "018f0cc3-e4a2-7b21-a109-001122334533",
      null,
      { schema_version: 1, nodes: [{ type: "text", text: "reference steer" }] },
      () => true,
    )).rejects.toThrow("target mismatch");
    expect(postCount).toBe(1);
    expect(receiptCount).toBe(1);
  });

  it("rejects a reference steer receipt for another submission identity", async () => {
    let postCount = 0;
    globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
      if (String(input).includes("/api/v1/chat/submissions/")) {
        return jsonResponse(200, {
          client_submission_id: "018f0cc3-e4a2-7b21-a109-001122334599",
          outcome: "buffered",
          response: {
            session_id: "s_1",
            turn_id: "t_parent",
            stream_url: "/api/v1/chat/stream/t_parent",
            status: "buffered",
            execution_generation: 2,
          },
        });
      }
      postCount += 1;
      throw new TypeError("response lost");
    }) as typeof fetch;

    await expect(postSteerMessage(
      "ws_1",
      "s_1",
      "reference steer",
      "t_parent",
      2,
      [],
      undefined,
      "018f0cc3-e4a2-7b21-a109-001122334533",
      null,
      { schema_version: 1, nodes: [{ type: "text", text: "reference steer" }] },
      () => true,
    )).rejects.toThrow("receipt identity mismatch");
    expect(postCount).toBe(1);
  });

  it.each(["completed", "cancelled"] as const)(
    "accepts an exact historical %s reference steer receipt",
    async (status) => {
      let postCount = 0;
      globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
        if (String(input).includes("/api/v1/chat/submissions/")) {
          return jsonResponse(200, {
            client_submission_id: "018f0cc3-e4a2-7b21-a109-001122334533",
            outcome: status,
            response: {
              session_id: "s_1",
              turn_id: "t_current",
              stream_url: "",
              status,
            },
          });
        }
        postCount += 1;
        throw new TypeError("response lost");
      }) as typeof fetch;

      await expect(postSteerMessage(
        "ws_1",
        "s_1",
        "reference steer",
        "t_parent",
        2,
        [],
        undefined,
        "018f0cc3-e4a2-7b21-a109-001122334533",
        null,
        { schema_version: 1, nodes: [{ type: "text", text: "reference steer" }] },
        () => true,
      )).resolves.toMatchObject({ status });
      expect(postCount).toBe(1);
    },
  );

  it("accepts a direct completed reference response for the current turn", async () => {
    let postCount = 0;
    globalThis.fetch = vi.fn(async () => {
      postCount += 1;
      return jsonResponse(200, {
        session_id: "s_1",
        turn_id: "t_current",
        stream_url: "",
        status: "completed",
      });
    }) as typeof fetch;

    await expect(postSteerMessage(
      "ws_1",
      "s_1",
      "reference steer",
      "t_parent",
      2,
      [],
      undefined,
      "018f0cc3-e4a2-7b21-a109-001122334533",
      null,
      { schema_version: 1, nodes: [{ type: "text", text: "reference steer" }] },
      () => true,
    )).resolves.toMatchObject({ status: "completed", turn_id: "t_current" });
    expect(postCount).toBe(1);
  });

  it("accepts a cancelled reference response from the bounded second POST", async () => {
    let postCount = 0;
    globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
      if (String(input).includes("/api/v1/chat/submissions/")) {
        return jsonResponse(404, {});
      }
      postCount += 1;
      if (postCount === 1) return jsonResponse(200, {});
      return jsonResponse(200, {
        session_id: "s_1",
        turn_id: "t_current",
        stream_url: "",
        status: "cancelled",
      });
    }) as typeof fetch;

    await expect(postSteerMessage(
      "ws_1",
      "s_1",
      "reference steer",
      "t_parent",
      2,
      [],
      undefined,
      "018f0cc3-e4a2-7b21-a109-001122334533",
      null,
      { schema_version: 1, nodes: [{ type: "text", text: "reference steer" }] },
      () => true,
    )).resolves.toMatchObject({ status: "cancelled", turn_id: "t_current" });
    expect(postCount).toBe(2);
  });

  it("does not retry a Terminal steer after transport loss", async () => {
    let postCount = 0;
    globalThis.fetch = vi.fn(async () => {
      postCount += 1;
      throw new TypeError("response lost");
    }) as typeof fetch;

    await expect(postSteerMessage(
      "ws_1",
      "s_1",
      "steer once",
      "t_parent",
      2,
      [],
      undefined,
      null,
      terminalSteerAction,
    )).rejects.toThrow("response lost");
    expect(postCount).toBe(1);
  });

  it("rejects mixed Terminal action and legacy submission identity before network", async () => {
    const fetchSpy = vi.fn();
    globalThis.fetch = fetchSpy as typeof fetch;

    await expect(postSteerMessage(
      "ws_1",
      "s_1",
      "invalid mixed steer",
      "t_parent",
      2,
      [],
      undefined,
      "018f0cc3-e4a2-7b21-a109-001122334533",
      terminalSteerAction,
    )).rejects.toThrow("cannot use legacy submission identity");
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it("surfaces a durable rejected receipt instead of decoding it as success", async () => {
    let postCount = 0;
    let receiptCount = 0;
    globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
      const target = String(input);
      if (target.includes("/api/v1/chat/send")) {
        postCount += 1;
        throw new TypeError("response lost");
      }
      if (target.includes("/api/v1/chat/submissions/")) {
        receiptCount += 1;
        return jsonResponse(200, {
          client_submission_id: "018f0cc3-e4a2-7b21-a109-001122334533",
          outcome: "rejected",
          response: {
            http_status: 409,
            detail: { error: { code: "turn_execution_generation_conflict" } },
          },
        });
      }
      return jsonResponse(404, {});
    }) as typeof fetch;

    await expect(postSteerMessage(
      "ws_1",
      "s_1",
      "steer text",
      "t_parent",
      2,
      [],
      undefined,
      "018f0cc3-e4a2-7b21-a109-001122334533",
    )).rejects.toMatchObject({
      status: 409,
      code: "turn_execution_generation_conflict",
    });
    expect(postCount).toBe(1);
    expect(receiptCount).toBe(1);
  });
});
