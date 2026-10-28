import { appPath } from "@/lib/base-path";
import { readApiErrorBody } from "@/lib/api-client";
import { genericUserFacingErrorMessage } from "@/lib/user-facing-error";

/**
 * Sprint file-upload-presign Phase 2 — XHR-based upload helpers.
 *
 * Used by uploadLargeFile / uploadLargeKbFile / uploadLargeKbBundle (the
 * presign path for files ≥10MB). `fetch()` cannot expose upload progress;
 * XHR can via `xhr.upload.onprogress`.
 *
 * Two transports for the same backend envelope:
 *   - S3-compatible (prod):  multipart/form-data POST with fields + file
 *   - LocalBackend (dev):    raw body POST with `fields` interpreted as
 *                             HTTP headers (Authorization: Bearer ...)
 *
 * FE branches on URL prefix via `isLocalReceiverUrl`. Tech-design §6.3 +
 * §7.4. Phase 1.4a/b/c backend wire confirmed via 116 backend UT.
 */

/** Endpoint-prefix detection — backend's LocalBackend.presign_post()
 * generates URLs ending in `/api/v1/files/local-receiver/{aid}` (verified
 * `bowhead_core/artifact_store.py:312`). S3 / S3-compatible URLs go to an
 * external host. */
export function isLocalReceiverUrl(url: string): boolean {
  return url.includes("/api/v1/files/local-receiver/");
}

/** HTTP error with status + body for the retry policy. */
export class UploadHttpError extends Error {
  readonly status: number;
  readonly body: string;
  readonly code?: string;
  constructor(status: number, body: string, message?: string) {
    let parsedBody: unknown = null;
    try {
      parsedBody = JSON.parse(body) as unknown;
    } catch {
      parsedBody = null;
    }
    const parsed = readApiErrorBody(
      parsedBody,
      genericUserFacingErrorMessage(),
    );
    super(message ?? parsed.message);
    this.status = status;
    this.body = body;
    this.code = parsed.code;
  }
}

/** Marker for "PUT/POST 403 → presign expired, caller should re-presign". */
export class PresignExpiredError extends Error {
  constructor() {
    super("presign expired (403 from object store)");
  }
}

/** Marker for "confirm 410 → presign expired before head() succeeded".
 * Same outer-loop handling as PresignExpiredError per tech-design §5.5
 * "confirm | expired and not uploaded | redo from presign". Phase 2 review P1-1.
 */
export class ConfirmExpiredError extends Error {
  constructor() {
    super("confirm 410 — presign expired before upload could be verified");
  }
}

/** Marker for "object store reachable but rejected — terminal, no retry". */
export class UploadTerminalError extends Error {
  constructor(readonly cause: UploadHttpError) {
    super(cause.message);
  }
}

/** Network failure (TCP / DNS / timeout) — distinct from HTTP errors. */
export class UploadNetworkError extends Error {
  constructor(detail = "network") {
    super(`upload network error: ${detail}`);
  }
}

/**
 * The request body was only partly sent when the connection dropped — typically
 * a gateway/reverse proxy rejecting the body by size and closing the socket.
 * Decided from transfer progress (some bytes sent, upload `load` never fired),
 * never from the file size. Subclass of UploadNetworkError so existing callers
 * and the retry policy keep treating it as a network failure.
 */
export class UploadCutOffError extends UploadNetworkError {
  constructor() {
    super("cut off in transit");
  }
}

/**
 * Wire upload progress on an XHR and track whether the body finished sending.
 * Returns a probe that is true once body bytes were reported sent but the
 * upload `load` event never fired (= cut off in transit).
 *
 * Only attaches `xhr.upload` handlers when `onProgress` is given: upload
 * listeners force a CORS preflight on cross-origin object-store POSTs, and
 * callers that pass onProgress already opted into that. Without onProgress
 * the probe is always false (falls back to a plain network error).
 */
export function watchUploadTransfer(
  xhr: XMLHttpRequest,
  onProgress?: (pct: number) => void,
): () => boolean {
  let sent = 0;
  let complete = false;
  if (onProgress) {
    xhr.upload.onprogress = (e) => {
      if (e.loaded > sent) sent = e.loaded;
      if (e.lengthComputable) onProgress((e.loaded / e.total) * 100);
    };
    xhr.upload.onload = () => {
      complete = true;
    };
  }
  return () => sent > 0 && !complete;
}

interface XhrPostOptions {
  onProgress?: (pct: number) => void;
  /** Abort controller for cancel; XHR will be `xhr.abort()`'d. */
  signal?: AbortSignal;
}

/**
 * Multipart POST for S3-compatible backends (tech-design §5.2).
 * `fields` MUST include the boto3-generated policy fields (key, Content-Type,
 * x-amz-algorithm, x-amz-credential, x-amz-date, policy, x-amz-signature).
 *
 * Backend returns HTTP 204 on success (verified Phase 0.5 spike).
 */
export function xhrPostFormWithProgress(
  url: string,
  fields: Record<string, string>,
  file: File,
  opts: XhrPostOptions = {},
): Promise<void> {
  return new Promise((resolve, reject) => {
    const fd = new FormData();
    for (const [k, v] of Object.entries(fields)) fd.append(k, v);
    fd.append("file", file);
    const xhr = new XMLHttpRequest();
    xhr.open("POST", appPath(url));
    const cutOff = watchUploadTransfer(xhr, opts.onProgress);
    // Phase 2 review P1-3: removeEventListener after done/error so the
    // AbortSignal doesn't leak listeners across retries / shared signals.
    const onAbort = () => xhr.abort();
    const cleanup = () => {
      if (opts.signal) opts.signal.removeEventListener("abort", onAbort);
    };
    xhr.onload = () => {
      cleanup();
      if (xhr.status < 300) {
        resolve();
      } else {
        reject(new UploadHttpError(xhr.status, xhr.responseText));
      }
    };
    xhr.onerror = () => {
      cleanup();
      reject(cutOff() ? new UploadCutOffError() : new UploadNetworkError("xhr.onerror"));
    };
    xhr.ontimeout = () => { cleanup(); reject(new UploadNetworkError("timeout")); };
    // xhr.abort() fires `abort`, not `error` — without this the promise never settles.
    xhr.onabort = () => { cleanup(); reject(new UploadNetworkError("aborted")); };
    if (opts.signal) {
      if (opts.signal.aborted) {
        reject(new UploadNetworkError("pre-aborted"));
        return;
      }
      opts.signal.addEventListener("abort", onAbort);
    }
    xhr.send(fd);
  });
}

/**
 * Raw-body POST for LocalBackend dev receiver (tech-design §6.3).
 * `headers` MUST include `Authorization: Bearer <jwt>` — the receiver
 * route parses the JWT to derive target path + size_max. Body is the
 * raw file bytes (NO multipart wrapper).
 *
 * Backend returns HTTP 204 on success.
 *
 * Note: some browsers forbid setting certain headers via setRequestHeader
 * (e.g., Content-Length is auto-set; cookies are immutable). Setting
 * Authorization is permitted in all browsers.
 */
export function xhrPostRawWithProgress(
  url: string,
  headers: Record<string, string>,
  file: File,
  opts: XhrPostOptions = {},
): Promise<void> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", appPath(url));
    for (const [k, v] of Object.entries(headers)) {
      try {
        xhr.setRequestHeader(k, v);
      } catch (e) {
        // Browser may forbid (e.g. "Cookie"); skip silently
        void e;
      }
    }
    const cutOff = watchUploadTransfer(xhr, opts.onProgress);
    // Phase 2 review P1-3: removeEventListener on done so multiple uploads
    // sharing one AbortSignal don't accumulate listeners.
    const onAbort = () => xhr.abort();
    const cleanup = () => {
      if (opts.signal) opts.signal.removeEventListener("abort", onAbort);
    };
    xhr.onload = () => {
      cleanup();
      if (xhr.status < 300) {
        resolve();
      } else {
        reject(new UploadHttpError(xhr.status, xhr.responseText));
      }
    };
    xhr.onerror = () => {
      cleanup();
      reject(cutOff() ? new UploadCutOffError() : new UploadNetworkError("xhr.onerror"));
    };
    xhr.ontimeout = () => { cleanup(); reject(new UploadNetworkError("timeout")); };
    // xhr.abort() fires `abort`, not `error` — without this the promise never settles.
    xhr.onabort = () => { cleanup(); reject(new UploadNetworkError("aborted")); };
    if (opts.signal) {
      if (opts.signal.aborted) {
        reject(new UploadNetworkError("pre-aborted"));
        return;
      }
      opts.signal.addEventListener("abort", onAbort);
    }
    xhr.send(file);
  });
}

/**
 * Dual-transport dispatcher — branches on URL prefix.
 * Caller passes the backend's `PresignUploadEnvelope`; we pick the right
 * transport without the caller knowing whether prod or dev.
 */
export async function uploadViaEnvelope(
  upload: { url: string; fields: Record<string, string> },
  file: File,
  opts: XhrPostOptions = {},
): Promise<void> {
  if (isLocalReceiverUrl(upload.url)) {
    return xhrPostRawWithProgress(upload.url, upload.fields, file, opts);
  }
  return xhrPostFormWithProgress(upload.url, upload.fields, file, opts);
}

/**
 * Chunked SHA-256 hex digest of a File. Uses `crypto.subtle.digest` on the
 * whole buffer for small/medium files (≤500MB). For very large files we
 * skip the checksum entirely (return "") — the backend's S3 Conditions
 * `content-length-range` plus ETag at confirm provide sufficient integrity.
 *
 * Why no streaming: WebCrypto.subtle has no incremental SHA-256 API; true
 * streaming requires Web Worker + WASM. Out of scope for this sprint;
 * server-side bounds + ETag cover the >500MB case.
 *
 * Returns lowercase hex string (matches backend regex `^[a-fA-F0-9]{64}$`).
 */
export async function sha256ChunkedHex(file: File): Promise<string> {
  // Phase 2 review P1-5: drop 500MB → 100MB. file.arrayBuffer() on a
  // 500MB blob OOM-tabs Chrome on 4GB devices. Backend's S3 Conditions
  // content-length-range + ETag at confirm cover integrity for files
  // above this threshold. True chunked hashing requires Web Worker +
  // WASM (out of sprint scope).
  const SKIP_THRESHOLD = 100 * 1024 * 1024;
  if (file.size > SKIP_THRESHOLD) {
    return "";
  }
  if (typeof crypto === "undefined" || !crypto.subtle) {
    return ""; // Test / SSR env without WebCrypto
  }
  const buf = await file.arrayBuffer();
  const digest = await crypto.subtle.digest("SHA-256", buf);
  const bytes = new Uint8Array(digest);
  let hex = "";
  for (let i = 0; i < bytes.length; i++) {
    hex += bytes[i].toString(16).padStart(2, "0");
  }
  return hex;
}

/** Sleep helper for backoff. */
export function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

/**
 * Upload with retry per tech-design §5.5:
 *   - PUT/POST 403 (presign expired) → throw PresignExpiredError so outer
 *     caller can re-presign + retry once.
 *   - PUT/POST 5xx or network → retry up to `maxRetries` (default 2) with
 *     exponential backoff (2s, 4s).
 *   - Other HTTP codes → terminal (UploadTerminalError).
 */
export async function uploadViaEnvelopeWithRetry(
  upload: { url: string; fields: Record<string, string> },
  file: File,
  opts: XhrPostOptions & { maxRetries?: number } = {},
): Promise<void> {
  const maxRetries = opts.maxRetries ?? 2;
  let attempt = 0;
  for (;;) {
    try {
      await uploadViaEnvelope(upload, file, opts);
      return;
    } catch (e) {
      if (e instanceof UploadHttpError) {
        if (e.status === 403) {
          throw new PresignExpiredError();
        }
        // Phase 2 review P1-2: LocalBackend 409 (replay) = treat as success.
        // Confirm step will return idempotent_replay=true / pull existing DTO.
        // S3 doesn't 409 on this path; the early-return is a no-op there.
        if (e.status === 409) {
          return;
        }
        if (e.status >= 500 && attempt < maxRetries) {
          attempt++;
          await sleep(2000 * attempt); // 2s, 4s
          continue;
        }
        throw new UploadTerminalError(e);
      }
      if (e instanceof UploadNetworkError && attempt < maxRetries) {
        // Phase 2 review P1-3 + abort tests: do NOT retry if the user aborted.
        // signal.aborted is true once dispatchEvent("abort") fires.
        if (opts.signal?.aborted) {
          throw e;
        }
        attempt++;
        await sleep(2000 * attempt);
        continue;
      }
      throw e;
    }
  }
}
