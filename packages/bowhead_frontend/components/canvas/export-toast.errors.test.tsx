/** Export could not start: explain only what happened and what to do, never show “start export: 500”; Copy Diagnostics stays outside the announcement area. */
import "@testing-library/jest-dom/vitest";

import { cleanup, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";
import { ApiError, HttpStatusError } from "@/lib/api-client";
import { normalizeExportJobDTO } from "@/lib/api";
import type { ExportState } from "@/lib/use-canvas-export";
import de from "@/messages/de-DE.json";
import en from "@/messages/en-US.json";
import zh from "@/messages/zh-CN.json";
import { ExportToast, exportStartErrorInfo } from "./export-ui";

/** Same shape as Gateway `_export_v2_error`: detail = { code, message, retryable, context }. */
function v2Failure(status: number, code?: string, retryable?: boolean): ApiError {
  const body = code === undefined ? { detail: "boom" } : { detail: { code, message: "服务端原话", retryable, context: {} } };
  return new ApiError(status, "服务端原话", { body, code, requestId: REQUEST_ID });
}

const REQUEST_ID = "0123456789abcdef0123456789abcdef";
const failure = (status: number) => new HttpStatusError("start export", new Response(null, { status, headers: { "x-request-id": REQUEST_ID } }));

function renderToast(state: ExportState, locale: "zh-CN" | "en-US" | "de-DE" = "zh-CN") {
  return renderIntl(
    <ExportToast state={state} onDismiss={() => {}} onRetry={() => {}} onRetryDefaultTheme={() => {}} onOpenHistory={() => {}} />,
    { locale },
  );
}

afterEach(() => cleanup());

describe("export start failure", () => {
  it.each([
    ["zh-CN", "导出没有开始", "复制诊断"],
    ["en-US", "The export didn't start", "Copy diagnostics"],
    ["de-DE", "Der Export wurde nicht gestartet", "Diagnoseinformationen kopieren"],
  ] as const)("%s: localized reason, no status code, Copy diagnostics outside the live region", (locale, text, copy) => {
    renderToast({ job: null, timedOut: false, startError: failure(500), lastArgs: { canvasId: "cv_1", fmt: "pdf" } }, locale);

    const toast = screen.getByTestId("export-toast");
    expect(toast.textContent).toContain(text);
    expect(toast.textContent).not.toMatch(/start export|\b500\b/);
    const button = screen.getByRole("button", { name: copy });
    for (const live of toast.querySelectorAll("[role=status], [aria-live]")) {
      expect(live.contains(button)).toBe(false);
    }
  });

  it("a job with an unrecognized status shows the generic localized failure", () => {
    const job = normalizeExportJobDTO({ job_id: "job_1", canvas_id: "cv_1", fmt: "pdf", status: "paused" });
    renderToast({ job, timedOut: false, startError: null, lastArgs: { canvasId: "cv_1", fmt: "pdf" } }, "en-US");

    const toast = screen.getByTestId("export-toast");
    expect(toast.textContent).not.toMatch(/Unknown export status|paused/);
    expect(toast.textContent).toContain("Retry");
  });

  it.each([
    [403, undefined, undefined, "start_failed_forbidden", false],
    [409, undefined, undefined, "start_failed_changed", false],
    [429, undefined, undefined, "start_failed_too_frequent", true],
    [503, undefined, undefined, "start_failed", true],
    [403, "asset_missing_or_forbidden", false, "start_failed_asset_missing", false],
    [403, "canvas_not_owned", false, "start_failed_not_owner", false],
    [413, "snapshot_too_large", false, "start_failed_too_large", false],
    [413, "asset_too_large", false, "start_failed_too_large", false],
    [422, "quarantined_sections", false, "start_failed_quarantined", false],
    [422, "unsupported_required_capability", false, "start_failed_unsupported", false],
    [409, "projection_conflict", true, "start_failed_changed", false],
    [409, "surface_projection_conflict", true, "start_failed_changed", false],
    [429, "export_capacity_exceeded", true, "start_failed_capacity", true],
    [500, "export_worker_down", false, "start_failed", false],
  ] as const)("status %s code %s (retryable %s) → %s, retry %s", (status, code, retryable, key, canRetry) => {
    expect(exportStartErrorInfo(v2Failure(status, code, retryable))).toEqual({ key, retryable: canRetry });
  });

  it("does not classify a programming exception as a network failure", () => {
    expect(exportStartErrorInfo(new TypeError("Cannot read properties of undefined"))).toEqual({ key: "start_failed", retryable: true });
  });

  it("network failure → start_failed_network, retryable", () => {
    expect(exportStartErrorInfo(new TypeError("Failed to fetch"))).toEqual({ key: "start_failed_network", retryable: true });
  });
});

describe("export start failure: Retry follows the server's retryable", () => {
  const CATALOGS = { "zh-CN": zh, "en-US": en, "de-DE": de } as const;

  it.each(["zh-CN", "en-US", "de-DE"] as const)("%s: an out-of-date report asks to export again and offers no Retry", (locale) => {
    renderToast({ job: null, timedOut: false, startError: v2Failure(409, "surface_projection_conflict", true), lastArgs: { canvasId: "cv_1", fmt: "pdf" } }, locale);

    const toast = screen.getByTestId("export-toast");
    expect(toast.textContent).toContain(CATALOGS[locale].export_ui.start_failed_changed);
    expect(toast.textContent).not.toContain(CATALOGS[locale].export_ui.start_retry_hint);
    expect(screen.queryByRole("button", { name: CATALOGS[locale].export_ui.retry })).toBeNull();
  });

  it.each(["zh-CN", "en-US", "de-DE"] as const)("%s: a missing referenced file is not described as a permission problem", (locale) => {
    renderToast({ job: null, timedOut: false, startError: v2Failure(403, "asset_missing_or_forbidden", false), lastArgs: { canvasId: "cv_1", fmt: "pdf" } }, locale);

    const toast = screen.getByTestId("export-toast");
    expect(toast.textContent).toContain(CATALOGS[locale].export_ui.start_failed_asset_missing);
    expect(toast.textContent).not.toContain(CATALOGS[locale].export_ui.start_failed_forbidden);
    expect(screen.queryByRole("button", { name: CATALOGS[locale].export_ui.retry })).toBeNull();
  });

  it("a full queue says wait a few minutes and keeps Retry", () => {
    renderToast({ job: null, timedOut: false, startError: v2Failure(429, "export_capacity_exceeded", true), lastArgs: { canvasId: "cv_1", fmt: "pdf" } });

    const toast = screen.getByTestId("export-toast");
    expect(toast.textContent).toContain(zh.export_ui.start_failed_capacity);
    expect(toast.textContent).toContain(zh.export_ui.start_retry_hint);
    expect(screen.getByRole("button", { name: zh.export_ui.retry })).toBeTruthy();
  });
});
