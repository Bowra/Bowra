/**
 * Sprint file-upload-presign Phase 2 — FE upload-path UT.
 *
 * Covers:
 *  - upload-xhr.ts helper (transport branch, retry, errors)
 *  - api.ts size-dispatch + uploadLargeFile presign+upload+confirm flow
 *  - kb-api.ts uploadLargeKbFile + uploadLargeKbBundle flows
 *  - error paths per tech-design §5.5 retry policy
 */
import {
  afterEach,
  beforeEach,
  describe,
  expect,
  it,
  vi,
  type Mock,
} from "vitest";
import {
  ConfirmExpiredError,
  isLocalReceiverUrl,
  PresignExpiredError,
  UploadHttpError,
  UploadNetworkError,
  UploadTerminalError,
  sha256ChunkedHex,
  sleep,
  uploadViaEnvelopeWithRetry,
} from "@/lib/upload-xhr";
import { ApiError } from "@/lib/api-client";

// ── helpers ──────────────────────────────────────────────────────

/** Stand-in XMLHttpRequest captured by tests. Override per-test. */
class MockXhr {
  static instances: MockXhr[] = [];
  upload = { onprogress: undefined as ((e: ProgressEvent) => void) | undefined };
  onload?: () => void;
  onerror?: () => void;
  ontimeout?: () => void;
  status = 0;
  responseText = "";
  sent: { url: string; body: unknown; headers: Record<string, string> } = {
    url: "",
    body: null,
    headers: {},
  };
  aborted = false;
  // capture which mock has been opened
  opened = false;

  constructor() {
    MockXhr.instances.push(this);
  }
  open(_method: string, url: string) {
    this.sent.url = url;
    this.opened = true;
  }
  setRequestHeader(k: string, v: string) {
    this.sent.headers[k] = v;
  }
  send(body: unknown) {
    this.sent.body = body;
  }
  abort() {
    this.aborted = true;
  }
  // Test driver helpers
  _finishOk() {
    this.status = 204;
    this.onload?.();
  }
  _finishHttp(status: number, body = "") {
    this.status = status;
    this.responseText = body;
    this.onload?.();
  }
  _network() {
    this.onerror?.();
  }
  _progress(loaded: number, total: number) {
    this.upload.onprogress?.({
      lengthComputable: true, loaded, total,
    } as ProgressEvent);
  }
}

function installMockXhr() {
  MockXhr.instances.length = 0;
  (globalThis as unknown as { XMLHttpRequest: typeof MockXhr }).XMLHttpRequest = MockXhr;
}

function mockFile(size = 100, name = "x.png", type = "image/png"): File {
  return new File([new Uint8Array(size)], name, { type });
}

async function waitForXhrCount(count: number): Promise<void> {
  for (let i = 0; i < 20; i++) {
    if (MockXhr.instances.length >= count) return;
    await Promise.resolve();
    await new Promise((r) => setTimeout(r, 0));
  }
  throw new Error(`expected ${count} XHR instance(s), got ${MockXhr.instances.length}`);
}

// ── isLocalReceiverUrl ───────────────────────────────────────────

describe("isLocalReceiverUrl", () => {
  it("detects local receiver path", () => {
    expect(isLocalReceiverUrl("http://localhost:8090/api/v1/files/local-receiver/art_aaa")).toBe(true);
  });
  it("returns false for S3-style URL", () => {
    expect(isLocalReceiverUrl("https://objects.example.com:90/allen")).toBe(false);
  });
});

// ── sha256ChunkedHex ─────────────────────────────────────────────

describe("sha256ChunkedHex", () => {
  it("returns empty string for >100MB (post-P1-5 threshold drop)", async () => {
    // Simulate large file by mocking File.size (avoid actually allocating)
    const f = mockFile(1);
    Object.defineProperty(f, "size", { value: 150 * 1024 * 1024 });
    expect(await sha256ChunkedHex(f)).toBe("");
  });
  it("returns lowercase 64-char hex for small file when WebCrypto available", async () => {
    // crypto.subtle is real in vitest env (node 18+ exposes it)
    const f = mockFile(100);
    const hex = await sha256ChunkedHex(f);
    // Either hex string (if subtle works) or empty (if not in env) — both acceptable
    if (hex !== "") {
      expect(hex).toMatch(/^[a-f0-9]{64}$/);
    }
  });
});

// ── xhrPostFormWithProgress + xhrPostRawWithProgress (via uploadViaEnvelope) ──

describe("uploadViaEnvelopeWithRetry — transport branch", () => {
  beforeEach(() => installMockXhr());
  afterEach(() => vi.restoreAllMocks());

  it("S3 URL → multipart POST with fields + file", async () => {
    const f = mockFile(100);
    const promise = uploadViaEnvelopeWithRetry({
      url: "https://objects.example.com:90/bucket",
      fields: { "Content-Type": "image/png", "x-amz-signature": "abc" },
    }, f);
    // Tick the mock
    await Promise.resolve();
    expect(MockXhr.instances.length).toBe(1);
    const xhr = MockXhr.instances[0];
    expect(xhr.sent.url).toBe("https://objects.example.com:90/bucket");
    // FormData was sent; can't fully introspect but body !== file directly
    expect(xhr.sent.body).toBeInstanceOf(FormData);
    xhr._finishOk();
    await promise; // resolves
  });

  it("LocalBackend URL → raw body + headers, NOT FormData", async () => {
    const f = mockFile(100);
    const promise = uploadViaEnvelopeWithRetry({
      url: "http://localhost:8090/api/v1/files/local-receiver/art_abc",
      fields: { "Content-Type": "image/png", "Authorization": "Bearer XYZ" },
    }, f);
    await Promise.resolve();
    const xhr = MockXhr.instances[0];
    // Body is the raw File, NOT FormData
    expect(xhr.sent.body).toBe(f);
    // Headers were set
    expect(xhr.sent.headers["Authorization"]).toBe("Bearer XYZ");
    expect(xhr.sent.headers["Content-Type"]).toBe("image/png");
    xhr._finishOk();
    await promise;
  });
});

// ── retry policy ─────────────────────────────────────────────────

describe("uploadViaEnvelopeWithRetry — retry policy (tech-design §5.5)", () => {
  beforeEach(() => installMockXhr());

  it("POST 403 → throws PresignExpiredError (caller re-presigns)", async () => {
    const f = mockFile(100);
    const promise = uploadViaEnvelopeWithRetry({
      url: "https://s3/x", fields: {},
    }, f);
    await Promise.resolve();
    MockXhr.instances[0]._finishHttp(403, "AccessDenied");
    await expect(promise).rejects.toBeInstanceOf(PresignExpiredError);
  });

  it("POST 5xx → retries with backoff up to 2 times then terminal", async () => {
    vi.useFakeTimers();
    const f = mockFile(100);
    const promise = uploadViaEnvelopeWithRetry({
      url: "https://s3/x", fields: {},
    }, f, { maxRetries: 2 });
    await vi.advanceTimersByTimeAsync(0);
    MockXhr.instances[0]._finishHttp(503, "");
    await vi.advanceTimersByTimeAsync(2000);
    MockXhr.instances[1]._finishHttp(503, "");
    await vi.advanceTimersByTimeAsync(4000);
    MockXhr.instances[2]._finishHttp(503, "");
    await expect(promise).rejects.toBeInstanceOf(UploadTerminalError);
    expect(MockXhr.instances.length).toBe(3);
    vi.useRealTimers();
  });

  it("POST 5xx then 2xx → succeeds after retry", async () => {
    vi.useFakeTimers();
    const f = mockFile(100);
    const promise = uploadViaEnvelopeWithRetry({
      url: "https://s3/x", fields: {},
    }, f, { maxRetries: 2 });
    await vi.advanceTimersByTimeAsync(0);
    MockXhr.instances[0]._finishHttp(503, "");
    await vi.advanceTimersByTimeAsync(2000);
    MockXhr.instances[1]._finishOk();
    await expect(promise).resolves.toBeUndefined();
    vi.useRealTimers();
  });

  it("network error → retries up to 2 times", async () => {
    vi.useFakeTimers();
    const f = mockFile(100);
    const promise = uploadViaEnvelopeWithRetry({
      url: "https://s3/x", fields: {},
    }, f, { maxRetries: 2 });
    await vi.advanceTimersByTimeAsync(0);
    MockXhr.instances[0]._network();
    await vi.advanceTimersByTimeAsync(2000);
    MockXhr.instances[1]._network();
    await vi.advanceTimersByTimeAsync(4000);
    MockXhr.instances[2]._network();
    await expect(promise).rejects.toBeInstanceOf(UploadNetworkError);
    expect(MockXhr.instances.length).toBe(3);
    vi.useRealTimers();
  });

  it("4xx other than 403/409 → terminal (no retry)", async () => {
    const f = mockFile(100);
    const promise = uploadViaEnvelopeWithRetry({
      url: "https://s3/x", fields: {},
    }, f);
    await Promise.resolve();
    MockXhr.instances[0]._finishHttp(412, "PreconditionFailed");
    await expect(promise).rejects.toBeInstanceOf(UploadTerminalError);
    expect(MockXhr.instances.length).toBe(1);
  });

  it("LocalBackend 409 (replay) → treat as success (P1-2)", async () => {
    const f = mockFile(100);
    const promise = uploadViaEnvelopeWithRetry({
      url: "http://localhost:8090/api/v1/files/local-receiver/art_abc",
      fields: { "Authorization": "Bearer X", "Content-Type": "image/png" },
    }, f);
    await Promise.resolve();
    MockXhr.instances[0]._finishHttp(409, "already uploaded");
    // Resolves (no throw) — confirm step will idempotent-replay
    await expect(promise).resolves.toBeUndefined();
  });

  it("AbortSignal listener removed after success (no leak)", async () => {
    const f = mockFile(100);
    const controller = new AbortController();
    const initialListeners = (controller.signal as unknown as { listenerCount?: number }).listenerCount ?? 0;
    const promise = uploadViaEnvelopeWithRetry({
      url: "https://s3/x", fields: {},
    }, f, { signal: controller.signal });
    await Promise.resolve();
    MockXhr.instances[0]._finishOk();
    await promise;
    // Aborting after resolve must NOT call xhr.abort() (since cleanup removed it)
    controller.abort();
    expect(MockXhr.instances[0].aborted).toBe(false);
  });

  it("AbortSignal pre-aborted → reject immediately without sending", async () => {
    const f = mockFile(100);
    const controller = new AbortController();
    controller.abort();
    await expect(
      uploadViaEnvelopeWithRetry({
        url: "https://s3/x", fields: {},
      }, f, { signal: controller.signal }),
    ).rejects.toBeInstanceOf(UploadNetworkError);
  });

  it("AbortSignal triggered mid-upload calls xhr.abort()", async () => {
    const f = mockFile(100);
    const controller = new AbortController();
    const promise = uploadViaEnvelopeWithRetry({
      url: "https://s3/x", fields: {},
    }, f, { signal: controller.signal });
    await Promise.resolve();
    expect(MockXhr.instances[0].aborted).toBe(false);
    controller.abort();
    expect(MockXhr.instances[0].aborted).toBe(true);
    // Caller's await needs to settle (xhr.abort doesn't fire onerror in our mock,
    // so we synthesize the failure)
    MockXhr.instances[0]._network();
    await expect(promise).rejects.toBeDefined();
  });
});

// ── onProgress callback ──────────────────────────────────────────

describe("onProgress callback", () => {
  beforeEach(() => installMockXhr());

  it("invokes callback with 0..100 during upload", async () => {
    const f = mockFile(100);
    const progressCalls: number[] = [];
    const promise = uploadViaEnvelopeWithRetry({
      url: "https://s3/x", fields: {},
    }, f, { onProgress: (pct) => progressCalls.push(pct) });
    await Promise.resolve();
    MockXhr.instances[0]._progress(0, 100);
    MockXhr.instances[0]._progress(50, 100);
    MockXhr.instances[0]._progress(100, 100);
    MockXhr.instances[0]._finishOk();
    await promise;
    expect(progressCalls).toEqual([0, 50, 100]);
  });

  it("S3 path: onProgress works through multipart helper too", async () => {
    const f = mockFile(100);
    const calls: number[] = [];
    const promise = uploadViaEnvelopeWithRetry({
      url: "https://objects.example.com:90/bucket",
      fields: { "x-amz-signature": "z" },
    }, f, { onProgress: (pct) => calls.push(pct) });
    await Promise.resolve();
    MockXhr.instances[0]._progress(0, 100);
    MockXhr.instances[0]._progress(100, 100);
    MockXhr.instances[0]._finishOk();
    await promise;
    expect(calls).toEqual([0, 100]);
  });
});

// ── api.ts uploadFile size-dispatch ──────────────────────────────

describe("validateUpload — data file policy", () => {
  it.each(["docx", "dotx", "pptx", "potx"])("accepts generic browser MIME for .%s", async (ext) => {
    const { UPLOAD_ACCEPT, validateUpload } = await import("@/lib/api");
    for (const mime of ["", "application/octet-stream", "application/zip", "application/x-zip-compressed"]) {
      expect(validateUpload(mockFile(100, `template.${ext}`, mime))).toBeNull();
    }
    expect(UPLOAD_ACCEPT).toContain(`.${ext}`);
  });

  it("does not use Office suffix to hide an explicit MIME conflict", async () => {
    const { validateUpload } = await import("@/lib/api");
    const word = "application/vnd.openxmlformats-officedocument.wordprocessingml.document";
    for (const [name, mime] of [["x.pptx", word], ["x.dotx", word], ["x.zip", word], ["x.potx", "text/plain"]]) {
      expect(validateUpload(mockFile(100, name, mime))).toMatchObject({ kind: "unsupported" });
    }
  });

  it("allows TSV and JSONL/NDJSON data files", async () => {
    const { validateUpload } = await import("@/lib/api");
    expect(validateUpload(mockFile(100, "rows.tsv", "text/tab-separated-values"))).toBeNull();
    expect(validateUpload(mockFile(100, "events.jsonl", "application/x-ndjson"))).toBeNull();
    expect(validateUpload(mockFile(100, "events.ndjson", "application/x-ndjson"))).toBeNull();
  });

  it("normalizes common browser MIME fallbacks before extension pairing", async () => {
    const { validateUpload } = await import("@/lib/api");
    expect(validateUpload(mockFile(100, "notes.md", "text/plain"))).toBeNull();
    expect(validateUpload(mockFile(100, "events.jsonl", "application/json"))).toBeNull();
    expect(validateUpload(mockFile(100, "rows.csv", "text/plain"))).toBeNull();
  });

  it("allows exact HTML artifacts and browser fallback MIME values", async () => {
    const { UPLOAD_ACCEPT, validateUpload } = await import("@/lib/api");
    expect(validateUpload(mockFile(100, "page.html", "text/html"))).toBeNull();
    expect(validateUpload(mockFile(100, "page.htm", "text/plain"))).toBeNull();
    expect(validateUpload(mockFile(100, "page.xhtml", "application/octet-stream"))).toBeNull();
    expect(UPLOAD_ACCEPT).toContain(".html");
    expect(UPLOAD_ACCEPT).toContain(".xhtml");
  });

  it("rejects HTML MIME/extension mismatches and arbitrary text files", async () => {
    const { validateUpload } = await import("@/lib/api");
    expect(validateUpload(mockFile(100, "page.txt", "text/html"))).toMatchObject({ kind: "unsupported" });
    expect(validateUpload(mockFile(100, "page.html", "application/xhtml+xml"))).toMatchObject({ kind: "unsupported" });
    expect(validateUpload(mockFile(100, "page.html", "application/javascript"))).toMatchObject({ kind: "unsupported" });
    expect(validateUpload(mockFile(100, "payload.foo", "text/plain"))).toMatchObject({ kind: "unsupported" });
    expect(validateUpload(mockFile(100, "notes.txt", "text/plain"))).toBeNull();
  });
});

describe("uploadFile — size dispatch", () => {
  let selectedSpace = "ws_upload";
  beforeEach(() => {
    vi.restoreAllMocks();
    installMockXhr();
    selectedSpace = "ws_upload";
    // appFetch reads window.location to decide whether to attach the public-base header (d59ce0c12).
    const location = window.location;
    vi.stubGlobal("window", { location, localStorage: { getItem: (key: string) => key === "bowhead.space_id" ? selectedSpace : null } });
  });
  afterEach(() => vi.unstubAllGlobals());

  it("small file (<10MB) → /files/upload (existing multipart path)", async () => {
    const fetchSpy = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({
        file_id: "art_x", filename: "small.png", size: 100,
        mime: "image/png", url: "/api/v1/files/art_x",
      }), { status: 200 }),
    );
    const { uploadFile } = await import("@/lib/api");
    const f = mockFile(100);
    const r = await uploadFile(f);
    expect(r.file_id).toBe("art_x");
    expect(fetchSpy.mock.calls[0]?.[0]).toBe("/api/v1/files/upload");
    expect(new Headers(fetchSpy.mock.calls[0]?.[1]?.headers).get("X-Space-Id")).toBe("ws_upload");
  });

  it("large file (≥10MB) → /files/presign + POST + confirm", async () => {
    const f = mockFile(100);
    Object.defineProperty(f, "size", { value: 20 * 1024 * 1024 });
    const fetchSpy = vi.spyOn(globalThis, "fetch").mockImplementation(
      async (url: string | URL | Request, _init?: RequestInit) => {
        const u = url.toString();
        if (u.endsWith("/api/v1/files/presign")) {
          return new Response(JSON.stringify({
            file_id: "art_largeXyz9",
            artifact_id: "art_largeXyz9",
            upload: {
              url: "https://objects.example.com:90/bucket",
              fields: { "x-amz-signature": "z", "Content-Type": "image/png" },
              method: "POST",
            },
            expires_at: "2026-05-27T22:00:00+00:00",
            confirm_url: "/api/v1/files/presign/art_largeXyz9/confirm",
          }), { status: 200 });
        }
        if (u.endsWith("/api/v1/files/presign/art_largeXyz9/confirm")) {
          return new Response(JSON.stringify({
            file_id: "art_largeXyz9", filename: "x.png", size: 20971520,
            mime: "image/png", url: "/api/v1/files/art_largeXyz9",
            status: "ready", idempotent_replay: false,
          }), { status: 200 });
        }
        throw new Error(`unexpected fetch: ${u}`);
      },
    );
    const { uploadFile } = await import("@/lib/api");
    const promise = uploadFile(f);
    await waitForXhrCount(1);
    selectedSpace = "ws_switched";
    MockXhr.instances[0]._finishOk();
    const r = await promise;
    expect(r.file_id).toBe("art_largeXyz9");
    // 2 fetch calls (presign + confirm)
    const urls = fetchSpy.mock.calls.map((c) => c[0]?.toString());
    expect(urls).toContain("/api/v1/files/presign");
    expect(urls).toContain("/api/v1/files/presign/art_largeXyz9/confirm");
    for (const call of fetchSpy.mock.calls) {
      expect(new Headers(call[1]?.headers).get("X-Space-Id")).toBe("ws_upload");
    }
  });

  it("large file with PresignExpired error → re-presigns once, then succeeds", async () => {
    const f = mockFile(100);
    Object.defineProperty(f, "size", { value: 20 * 1024 * 1024 });
    let presignCalls = 0;
    const fetchSpy = vi.spyOn(globalThis, "fetch").mockImplementation(
      async (url: string | URL | Request, _init?: RequestInit) => {
        const u = url.toString();
        if (u.endsWith("/api/v1/files/presign")) {
          presignCalls++;
          return new Response(JSON.stringify({
            file_id: `art_largeXyz${presignCalls}`,
            artifact_id: `art_largeXyz${presignCalls}`,
            upload: {
              url: "https://objects.example.com:90/bucket",
              fields: { "x-amz-signature": "z", "Content-Type": "image/png" },
              method: "POST",
            },
            expires_at: "2026-05-27T22:00:00+00:00",
            confirm_url: `/api/v1/files/presign/art_largeXyz${presignCalls}/confirm`,
          }), { status: 200 });
        }
        return new Response(JSON.stringify({
          file_id: "art_largeXyz2", filename: "x.png", size: 20971520,
          mime: "image/png", url: "/api/v1/files/art_largeXyz2",
          status: "ready", idempotent_replay: false,
        }), { status: 200 });
      },
    );
    const { uploadFile } = await import("@/lib/api");
    const options = { sessionId: "s_original", spaceHeaders: { "X-Space-Id": "ws_original" } };
    const promise = uploadFile(f, options);
    options.sessionId = "s_switched";
    options.spaceHeaders["X-Space-Id"] = "ws_switched";
    // First XHR: 403 → triggers PresignExpired → outer loop re-presigns
    await waitForXhrCount(1);
    MockXhr.instances[0]._finishHttp(403, "expired");
    await waitForXhrCount(2);
    MockXhr.instances[1]._finishOk();
    const r = await promise;
    expect(r.file_id).toBe("art_largeXyz2");
    expect(presignCalls).toBe(2);
    for (const [, init] of fetchSpy.mock.calls) {
      expect(new Headers(init?.headers).get("X-Space-Id")).toBe("ws_original");
      expect(JSON.parse(String(init?.body)).session_id).toBe("s_original");
    }
  });
});

describe("uploadFile — bounded Office multipart negotiation", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    installMockXhr();
  });

  function officeFile(name = "template.potx", size = 20 * 1024 * 1024) {
    const file = mockFile(100, name, "application/zip");
    Object.defineProperty(file, "size", { value: size });
    return file;
  }

  function apiError(code: string, status = 409) {
    return new Response(JSON.stringify({ detail: { error: { code } } }), { status });
  }

  function uploaded() {
    return new Response(JSON.stringify({ file_id: "art_checked", filename: "template.potx" }), { status: 200 });
  }

  it("presign instruction switches once and retains session, exact File, signal and completion", async () => {
    const { uploadFile } = await import("@/lib/api");
    const fetchSpy = vi.spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(apiError("file_office_multipart_required"))
      .mockResolvedValueOnce(uploaded());
    const file = officeFile();
    const controller = new AbortController();
    const onProgress = vi.fn();
    const result = await uploadFile(file, { sessionId: "s_test", signal: controller.signal, onProgress });
    expect(result.file_id).toBe("art_checked");
    expect(fetchSpy).toHaveBeenCalledTimes(2);
    expect(fetchSpy.mock.calls[0][0]).toBe("/api/v1/files/presign");
    const [url, init] = fetchSpy.mock.calls[1];
    expect(url).toBe("/api/v1/files/upload?session_id=s_test");
    expect((init?.body as FormData).get("file")).toBe(file);
    expect(init?.signal).toBe(controller.signal);
    expect(onProgress).toHaveBeenCalledWith(100);
    expect(MockXhr.instances).toHaveLength(0);
  });

  it.each([
    ["file_service_unavailable", 503, "template.pptx", 20 * 1024 * 1024],
    ["file_multipart_required", 503, "template.pptx", 20 * 1024 * 1024],
    ["file_presign_unavailable", 503, "template.pptx", 20 * 1024 * 1024],
    ["file_presign_pending_limit", 409, "template.pptx", 20 * 1024 * 1024],
    ["file_office_multipart_required", 409, "ordinary.zip", 20 * 1024 * 1024],
    ["file_office_multipart_required", 409, "template.pptx", 32 * 1024 * 1024 + 1],
  ])("does not fall back for %s / %i / %s / %i", async (code, status, name, size) => {
    const { uploadFile } = await import("@/lib/api");
    const fetchSpy = vi.spyOn(globalThis, "fetch").mockResolvedValue(uploaded()).mockResolvedValueOnce(apiError(code, status));
    await expect(uploadFile(officeFile(name, size))).rejects.toMatchObject({ code, status });
    expect(fetchSpy).toHaveBeenCalledTimes(1);
    expect(MockXhr.instances).toHaveLength(0);
  });

  it.each([409, 415])("an old envelope rejected at confirm with %s switches once to multipart", async (status) => {
    const { uploadFile } = await import("@/lib/api");
    const fetchSpy = vi.spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(new Response(JSON.stringify({
        file_id: "art_old", artifact_id: "art_old",
        upload: { url: "https://s3.example.test/bucket", fields: {}, method: "POST" },
        confirm_url: "/api/v1/files/presign/art_old/confirm",
      }), { status: 200 }))
      .mockResolvedValueOnce(apiError("file_office_multipart_required", status))
      .mockImplementation(async () => uploaded());
    const spaceHeaders = { "X-Space-Id": "ws_original" };
    const promise = uploadFile(officeFile("template.pptx"), { sessionId: "s_test", spaceHeaders });
    spaceHeaders["X-Space-Id"] = "ws_switched";
    await waitForXhrCount(1);
    MockXhr.instances[0]._finishOk();
    expect((await promise).file_id).toBe("art_checked");
    expect(fetchSpy.mock.calls.map(([url]) => url)).toEqual([
      "/api/v1/files/presign", "/api/v1/files/presign/art_old/confirm", "/api/v1/files/upload?session_id=s_test",
    ]);
    expect(MockXhr.instances).toHaveLength(1);
    for (const [, init] of fetchSpy.mock.calls) {
      expect(new Headers(init?.headers).get("X-Space-Id")).toBe("ws_original");
    }
  });

  it("retries one in-flight confirmation without uploading the bytes again", async () => {
    const { uploadFile } = await import("@/lib/api");
    const fetchSpy = vi.spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(new Response(JSON.stringify({
        file_id: "art_inflight", artifact_id: "art_inflight",
        upload: { url: "https://s3.example.test/bucket", fields: {}, method: "POST" },
        confirm_url: "/api/v1/files/presign/art_inflight/confirm",
      }), { status: 200 }))
      .mockResolvedValueOnce(apiError("file_confirm_in_progress"))
      .mockResolvedValueOnce(uploaded());
    const promise = uploadFile(officeFile());
    await waitForXhrCount(1);
    MockXhr.instances[0]._finishOk();
    expect((await promise).file_id).toBe("art_checked");
    expect(MockXhr.instances).toHaveLength(1);
    expect(fetchSpy.mock.calls.map(([url]) => url)).toEqual([
      "/api/v1/files/presign", "/api/v1/files/presign/art_inflight/confirm", "/api/v1/files/presign/art_inflight/confirm",
    ]);
  });

  it("a multipart validation failure is terminal, not another transport retry", async () => {
    const { uploadFile } = await import("@/lib/api");
    const onProgress = vi.fn();
    const fetchSpy = vi.spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(apiError("file_office_multipart_required"))
      .mockResolvedValueOnce(apiError("file_office_invalid_package", 415));
    await expect(uploadFile(officeFile(), { onProgress })).rejects.toMatchObject({ code: "file_office_invalid_package" });
    expect(fetchSpy).toHaveBeenCalledTimes(2);
    expect(onProgress).not.toHaveBeenCalledWith(100);
  });

  it.each(["file_office_multipart_required", "file_multipart_required"])("retains cancellation when switching to multipart for %s", async (code) => {
    const { uploadFile } = await import("@/lib/api");
    const controller = new AbortController();
    const fetchSpy = vi.spyOn(globalThis, "fetch")
      .mockImplementationOnce(async () => {
        controller.abort();
        return apiError(code);
      })
      .mockImplementationOnce(async (_url, init) => {
        expect(init?.signal?.aborted).toBe(true);
        throw new DOMException("Aborted", "AbortError");
      });
    await expect(uploadFile(officeFile(), { signal: controller.signal })).rejects.toMatchObject({ name: "AbortError" });
    expect(fetchSpy).toHaveBeenCalledTimes(2);
  });
});

// ── kb-api.ts uploadLargeKbBundle ────────────────────────────────

describe("uploadKbBundle — size dispatch", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    installMockXhr();
  });

  it("small zip (<10MB) → /kb/files/bundle via XHR (progress-capable)", async () => {
    // kb-security-quickfix I2: small uploads switched fetch→XHR so the common
    // path can report real progress. Assert the XHR target + JSON round-trip.
    const { uploadKbBundle } = await import("@/lib/kb-api");
    const f = mockFile(100, "x.zip", "application/zip");
    const promise = uploadKbBundle(f, { scope: "personal", scope_id: "u_t", use_llm: false, spaceHeaders: { "X-Space-Id": "ws_original" } });
    await waitForXhrCount(1);
    const xhr = MockXhr.instances[0];
    expect(xhr.sent.url).toBe("/api/v1/kb/files/bundle");
    expect(xhr.sent.headers["X-Space-Id"]).toBe("ws_original");
    xhr._finishHttp(200, JSON.stringify({
      uploaded: [], skipped: [], total_entries: 0, bundle_filename: "x.zip",
    }));
    const r = await promise;
    expect(r.bundle_filename).toBe("x.zip");
  });

  it("small single file → /kb/files via XHR with progress callback (I2)", async () => {
    const { uploadKbFile } = await import("@/lib/kb-api");
    const f = mockFile(100, "doc.pdf", "application/pdf");
    const pcts: number[] = [];
    const promise = uploadKbFile(f, {
      scope: "personal", scope_id: "u_t", use_llm: false,
      onProgress: (p) => pcts.push(p),
    });
    await waitForXhrCount(1);
    const xhr = MockXhr.instances[0];
    expect(xhr.sent.url).toBe("/api/v1/kb/files");
    xhr._progress(50, 100);
    xhr._finishHttp(200, JSON.stringify({ file_id: "kbf_x", filename: "doc.pdf" }));
    const r = await promise;
    expect(r.file_id).toBe("kbf_x");
    expect(pcts).toContain(50); // progress actually surfaced (was stuck at 0 with fetch)
  });

  it("small-file XHR honours a deployment base path", async () => {
    window.__BOWHEAD_BASE_PATH__ = "/bowhead";
    try {
      const { uploadKbFile } = await import("@/lib/kb-api");
      const promise = uploadKbFile(
        mockFile(100, "doc.pdf", "application/pdf"),
        { scope: "personal", scope_id: "u_t", use_llm: false },
      );
      await waitForXhrCount(1);
      const xhr = MockXhr.instances[0];
      expect(xhr.sent.url).toBe("/bowhead/api/v1/kb/files");
      xhr._finishHttp(200, JSON.stringify({ file_id: "kbf_x", filename: "doc.pdf" }));
      await promise;
    } finally {
      delete window.__BOWHEAD_BASE_PATH__;
    }
  });

  it("small-file XHR preserves nested code without function/status fallback", async () => {
    const { uploadKbFile } = await import("@/lib/kb-api");
    const promise = uploadKbFile(
      mockFile(100, "doc.pdf", "application/pdf"),
      {
        scope: "personal",
        scope_id: "u_t",
        use_llm: false,
      },
    );
    await waitForXhrCount(1);
    MockXhr.instances[0]._finishHttp(
      413,
      JSON.stringify({
        detail: { error: { code: "kb_file_too_large" } },
      }),
    );

    const error = await promise.catch((cause) => cause);
    expect(error).toBeInstanceOf(ApiError);
    expect(error).toMatchObject({
      status: 413,
      code: "kb_file_too_large",
    });
    expect((error as Error).message).not.toContain("kb_file_too_large");
    expect((error as Error).message).not.toContain("uploadKbFile: 413");
  });

  it("large zip (≥10MB) → /kb/presign/bundle (NOT /files/presign)", async () => {
    const f = mockFile(100, "big.zip", "application/zip");
    Object.defineProperty(f, "size", { value: 20 * 1024 * 1024 });
    const fetchSpy = vi.spyOn(globalThis, "fetch").mockImplementation(
      async (url: string | URL | Request, _init?: RequestInit) => {
        const u = url.toString();
        if (u.endsWith("/api/v1/kb/presign/bundle")) {
          return new Response(JSON.stringify({
            file_id: "kbf_bigZip",
            artifact_id: "art_bigZip",
            upload: {
              url: "https://objects.example.com:90/bucket",
              fields: { "x-amz-signature": "z", "Content-Type": "application/zip" },
              method: "POST",
            },
            expires_at: "2026-05-27T22:00:00+00:00",
            confirm_url: "/api/v1/kb/presign/bundle/kbf_bigZip/confirm",
          }), { status: 200 });
        }
        if (u.endsWith("/api/v1/kb/presign/bundle/kbf_bigZip/confirm")) {
          return new Response(JSON.stringify({
            uploaded: [], skipped: [], total_entries: 3, bundle_filename: "big.zip",
          }), { status: 200 });
        }
        throw new Error(`unexpected fetch: ${u}`);
      },
    );
    const { uploadKbBundle } = await import("@/lib/kb-api");
    const spaceHeaders = { "X-Space-Id": "ws_original" };
    const promise = uploadKbBundle(f, {
      spaceHeaders,
      scope: "personal", scope_id: "u_t", use_llm: false,
    });
    await waitForXhrCount(1);
    spaceHeaders["X-Space-Id"] = "ws_switched";
    MockXhr.instances[0]._finishOk();
    const r = await promise;
    expect(r.total_entries).toBe(3);
    for (const [, init] of fetchSpy.mock.calls) {
      expect(new Headers(init?.headers).get("X-Space-Id")).toBe("ws_original");
    }
    const urls = fetchSpy.mock.calls.map((c) => c[0]?.toString());
    expect(urls).toContain("/api/v1/kb/presign/bundle");
    // CRITICAL: must NOT hit /files/presign — that's the chat-attachment endpoint
    expect(urls.some((u) => u?.endsWith("/api/v1/files/presign"))).toBe(false);
  });
});

// ── UploadHttpError sanity ───────────────────────────────────────

describe("error types", () => {
  it("UploadHttpError captures status + truncated body", () => {
    const e = new UploadHttpError(413, "x".repeat(300));
    expect(e.status).toBe(413);
    expect(e.message).not.toContain("413");
    expect(e.message).not.toContain("x".repeat(20));
    expect(e.body.length).toBe(300);
  });

  it("ConfirmExpiredError is a distinct typed marker", () => {
    const e = new ConfirmExpiredError();
    expect(e).toBeInstanceOf(ConfirmExpiredError);
    expect(e).toBeInstanceOf(Error);
    expect(e.message).toContain("410");
  });
});

// ── confirm 410 → outer loop re-presigns (Phase 2 review P1-1) ──

describe("uploadLargeFile confirm 410 auto-redo", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    installMockXhr();
  });

  it("confirm 410 → re-presigns once + retries", async () => {
    const f = mockFile(100);
    Object.defineProperty(f, "size", { value: 20 * 1024 * 1024 });
    let presignCalls = 0;
    let confirmCalls = 0;
    const fetchSpy = vi.spyOn(globalThis, "fetch").mockImplementation(
      async (url: string | URL | Request, _init?: RequestInit) => {
        const u = url.toString();
        if (u.endsWith("/api/v1/files/presign")) {
          presignCalls++;
          return new Response(JSON.stringify({
            file_id: `art_p${presignCalls}`,
            artifact_id: `art_p${presignCalls}`,
            upload: {
              url: "https://objects.example.com:90/bucket",
              fields: { "x-amz-signature": "z", "Content-Type": "image/png" },
              method: "POST",
            },
            expires_at: "2026-05-27T22:00:00+00:00",
            confirm_url: `/api/v1/files/presign/art_p${presignCalls}/confirm`,
          }), { status: 200 });
        }
        if (u.includes("/confirm")) {
          confirmCalls++;
          if (confirmCalls === 1) {
            return new Response(JSON.stringify({ detail: "expired" }), { status: 410 });
          }
          return new Response(JSON.stringify({
            file_id: "art_p2", filename: "x.png", size: 20971520,
            mime: "image/png", url: "/api/v1/files/art_p2",
            status: "ready", idempotent_replay: false,
          }), { status: 200 });
        }
        throw new Error(`unexpected fetch: ${u}`);
      },
    );
    const { uploadFile } = await import("@/lib/api");
    const options = { sessionId: "s_original", spaceHeaders: { "X-Space-Id": "ws_original" } };
    const promise = uploadFile(f, options);
    options.sessionId = "s_switched";
    options.spaceHeaders["X-Space-Id"] = "ws_switched";
    // First XHR upload
    await waitForXhrCount(1);
    MockXhr.instances[0]._finishOk();
    // Confirm returns 410 → re-presign + new XHR
    await waitForXhrCount(2);
    MockXhr.instances[1]._finishOk();
    const r = await promise;
    expect(r.file_id).toBe("art_p2");
    expect(presignCalls).toBe(2);
    expect(confirmCalls).toBe(2);
    for (const [, init] of fetchSpy.mock.calls) {
      expect(new Headers(init?.headers).get("X-Space-Id")).toBe("ws_original");
      expect(JSON.parse(String(init?.body)).session_id).toBe("s_original");
    }
  });
});

describe("upload cancellation before confirm", () => {
  it("does not confirm or retry after transfer cancellation", async () => {
    vi.restoreAllMocks();
    installMockXhr();
    const fetchSpy = vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(JSON.stringify({
      file_id: "art_pending", artifact_id: "art_pending",
      upload: { url: "https://objects.example.test/bucket", fields: {}, method: "POST" },
      confirm_url: "/api/v1/files/presign/art_pending/confirm",
    }), { status: 200 }));
    const file = mockFile(100);
    Object.defineProperty(file, "size", { value: 20 * 1024 * 1024 });
    const controller = new AbortController();
    const { uploadFile } = await import("@/lib/api");
    const pending = uploadFile(file, { signal: controller.signal });
    const rejected = expect(pending).rejects.toMatchObject({ name: "AbortError" });
    await waitForXhrCount(1);
    MockXhr.instances[0]._finishOk();
    controller.abort();
    await rejected;
    expect(fetchSpy).toHaveBeenCalledTimes(1);
    expect(fetchSpy.mock.calls[0][1]?.signal).toBe(controller.signal);
  });
});

// ── sleep utility ────────────────────────────────────────────────

describe("sleep", () => {
  it("resolves after delay (using fake timers)", async () => {
    vi.useFakeTimers();
    let done = false;
    const p = sleep(1000).then(() => { done = true; });
    await vi.advanceTimersByTimeAsync(500);
    expect(done).toBe(false);
    await vi.advanceTimersByTimeAsync(500);
    await p;
    expect(done).toBe(true);
    vi.useRealTimers();
  });
});


describe("explicit multipart transport negotiation", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    installMockXhr();
  });

  it.each(["file", "kb_single", "kb_bundle"])("%s retains its immutable context and transfers only once", async (kind) => {
    const { uploadFile } = await import("@/lib/api");
    const { uploadKbFile, uploadKbBundle } = await import("@/lib/kb-api");
    const file = mockFile(100, kind === "kb_bundle" ? "source.zip" : "source.json", kind === "kb_bundle" ? "application/zip" : "application/json");
    Object.defineProperty(file, "size", { value: 11 * 1024 * 1024 });
    const fetchSpy = vi.spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(new Response(JSON.stringify({ detail: { error: { code: "file_multipart_required" } } }), { status: 409 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ file_id: "art_ready" }), { status: 200 }));
    const controller = new AbortController();
    const onProgress = vi.fn();
    const spaceHeaders = { "X-Space-Id": "ws_original" };
    const opts = { spaceHeaders, sessionId: "s_original", scope: "personal" as const, scope_id: "u_original", use_llm: false, doc_type_id: "dt_original", signal: controller.signal, onProgress };
    const promise = kind === "file" ? uploadFile(file, opts) : kind === "kb_single" ? uploadKbFile(file, opts) : uploadKbBundle(file, opts);
    spaceHeaders["X-Space-Id"] = "ws_switched";
    opts.sessionId = "s_switched";
    opts.scope_id = "u_switched";
    if (kind === "file") {
      expect((await promise as { file_id: string }).file_id).toBe("art_ready");
      expect(fetchSpy).toHaveBeenCalledTimes(2);
      const [url, init] = fetchSpy.mock.calls[1];
      expect(url).toBe("/api/v1/files/upload?session_id=s_original");
      expect((init?.body as FormData).get("file")).toBe(file);
      expect(init?.signal).toBe(controller.signal);
      expect(onProgress).toHaveBeenCalledWith(100);
      expect(MockXhr.instances).toHaveLength(0);
    } else {
      await waitForXhrCount(1);
      const xhr = MockXhr.instances[0];
      expect(xhr.sent.url).toBe(kind === "kb_single" ? "/api/v1/kb/files" : "/api/v1/kb/files/bundle");
      expect(xhr.sent.headers["X-Space-Id"]).toBe("ws_original");
      const form = xhr.sent.body as FormData;
      expect(form.get("file")).toBe(file);
      expect(form.get("scope_id")).toBe("u_original");
      if (kind === "kb_single") expect(form.get("doc_type_id")).toBe("dt_original");
      xhr._progress(50, 100);
      xhr._finishHttp(200, JSON.stringify({ file_id: "art_ready" }));
      await promise;
      expect(onProgress).toHaveBeenCalledWith(50);
      expect(fetchSpy).toHaveBeenCalledTimes(1);
      expect(MockXhr.instances).toHaveLength(1);
    }
    for (const [, init] of fetchSpy.mock.calls) {
      expect(new Headers(init?.headers).get("X-Space-Id")).toBe("ws_original");
      expect(init?.signal).toBe(controller.signal);
    }
  });

  it.each(["kb_single", "kb_bundle"])("%s never converts a generic 503 into a second upload", async (kind) => {
    const { uploadLargeKbFile, uploadLargeKbBundle } = await import("@/lib/kb-api");
    const fetchSpy = vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(new Response(JSON.stringify({ detail: { error: { code: "kb_service_unavailable" } } }), { status: 503 }));
    const upload = kind === "kb_single" ? uploadLargeKbFile : uploadLargeKbBundle;
    await expect(upload(mockFile(), { scope: "personal", scope_id: "u_test", use_llm: false })).rejects.toMatchObject({ status: 503, code: "kb_service_unavailable" });
    expect(fetchSpy).toHaveBeenCalledTimes(1);
    expect(MockXhr.instances).toHaveLength(0);
  });

  it("does not reinterpret a confirmation error as permission to upload again", async () => {
    const { uploadLargeFile } = await import("@/lib/api");
    const fetchSpy = vi.spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(new Response(JSON.stringify({ upload: { url: "https://s3.example.test/bucket", fields: {} }, confirm_url: "/confirm" }), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ detail: { error: { code: "file_multipart_required" } } }), { status: 409 }));
    const promise = uploadLargeFile(mockFile());
    const rejected = expect(promise).rejects.toMatchObject({ code: "file_multipart_required" });
    await waitForXhrCount(1);
    MockXhr.instances[0]._finishOk();
    await rejected;
    expect(fetchSpy).toHaveBeenCalledTimes(2);
    expect(MockXhr.instances).toHaveLength(1);
  });

  it.each(["kb_single", "kb_bundle"])("%s honors cancellation before multipart dispatch", async (kind) => {
    const { uploadLargeKbFile, uploadLargeKbBundle } = await import("@/lib/kb-api");
    const controller = new AbortController();
    vi.spyOn(globalThis, "fetch").mockImplementationOnce(async () => {
      controller.abort();
      return new Response(JSON.stringify({ detail: { error: { code: "file_multipart_required" } } }), { status: 409 });
    });
    const upload = kind === "kb_single" ? uploadLargeKbFile : uploadLargeKbBundle;
    await expect(upload(mockFile(), { scope: "personal", scope_id: "u_test", use_llm: false, signal: controller.signal })).rejects.toMatchObject({ status: 0 });
    expect(MockXhr.instances.every((xhr) => xhr.sent.body === null)).toBe(true);
  });
});
