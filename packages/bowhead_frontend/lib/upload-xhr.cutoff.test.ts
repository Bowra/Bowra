// FR5.2: ≥10MB uses presigned direct upload (uploadViaEnvelopeWithRetry), likewise using transfer progress to distinguish
// disconnection before the full request body was sent from failure before sending the request.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { kbFileError } from "./kb-file-error";
import {
  UploadCutOffError,
  UploadNetworkError,
  uploadViaEnvelopeWithRetry,
} from "./upload-xhr";

class MockXhr {
  static last: MockXhr | null = null;
  upload: { onprogress?: (e: ProgressEvent) => void; onload?: () => void } = {};
  onload?: () => void;
  onerror?: () => void;
  onabort?: () => void;
  ontimeout?: () => void;
  status = 0;
  responseText = "";
  constructor() {
    MockXhr.last = this;
  }
  open() {}
  setRequestHeader() {}
  send() {}
  abort() {
    this.onabort?.();
  }
}

const ENVELOPE = { url: "https://s3.example.com/bucket", fields: { key: "k" } };
const file = () => new File([new Uint8Array(64)], "big.pdf");

beforeEach(() => {
  vi.stubGlobal("XMLHttpRequest", MockXhr);
});
afterEach(() => {
  vi.unstubAllGlobals();
});

describe("uploadViaEnvelopeWithRetry · 传输中被断开", () => {
  it("已发出部分请求体后断开 → UploadCutOffError → 网关大小提示", async () => {
    const p = uploadViaEnvelopeWithRetry(ENVELOPE, file(), { onProgress: () => {}, maxRetries: 0 });
    const xhr = MockXhr.last!;
    xhr.upload.onprogress?.({ lengthComputable: true, loaded: 16, total: 64 } as ProgressEvent);
    xhr.onerror?.();
    const err = await p.catch((e: unknown) => e);
    expect(err).toBeInstanceOf(UploadCutOffError);
    expect(kbFileError(err, "upload")).toBe("errors.upload_cut_off");
  });

  it("一个字节都没发出就失败 → 普通网络失败", async () => {
    const p = uploadViaEnvelopeWithRetry(ENVELOPE, file(), { onProgress: () => {}, maxRetries: 0 });
    MockXhr.last!.onerror?.();
    const err = await p.catch((e: unknown) => e);
    expect(err).toBeInstanceOf(UploadNetworkError);
    expect(err).not.toBeInstanceOf(UploadCutOffError);
    expect(kbFileError(err, "upload")).toBe("errors.network");
  });

  it("中止（取消上传）会让请求结束，而不是一直挂起", async () => {
    const ctrl = new AbortController();
    const p = uploadViaEnvelopeWithRetry(ENVELOPE, file(), { onProgress: () => {}, signal: ctrl.signal });
    ctrl.abort();
    await expect(p).rejects.toBeInstanceOf(UploadNetworkError);
  });
});
