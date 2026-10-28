/**
 * Home-session opening failures no longer show “加载会话失败：session detail: 500”.
 *
 * Home page.tsx is too large; lock three layers here: sentence-selecting pure function, three-language copy, and Toast Copy Diagnostics
 * carrying status codes and request IDs. Add a source wiring gate proving the failure path uses these rather than err.message.
 */
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { act, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";
import de from "@/messages/de-DE.json";
import en from "@/messages/en-US.json";
import fr from "@/messages/fr-FR.json";
import zh from "@/messages/zh-CN.json";

const state = vi.hoisted(() => ({ copy: vi.fn() }));
vi.mock("@/lib/clipboard", () => ({ copyTextWithFallback: (...args: unknown[]) => state.copy(...args) }));

import { useCopyDiagnosticsToastAction } from "@/components/error-notice";
import { HttpStatusError } from "./api-client";
import { SessionDetailError } from "./api";
import { clearSessionLoadFailure, sessionLoadFailedKey, showSessionLoadFailure } from "./session-load-error";
import { _clearAllToasts, useToasts } from "./use-toast";

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..");
const REQUEST_ID = "0123456789abcdef0123456789abcdef";

afterEach(() => state.copy.mockReset());

describe("sessionLoadFailedKey", () => {
  it.each([
    [new SessionDetailError(403, REQUEST_ID), "load_failed_forbidden"],
    [new SessionDetailError(404), "load_failed_gone"],
    [new SessionDetailError(500), "load_failed_plain"],
    [new HttpStatusError("canvas", new Response(null, { status: 502 })), "load_failed_plain"],
    [new TypeError("Failed to fetch"), "load_failed_network"],
  ] as const)("%s → %s", (error, key) => {
    expect(sessionLoadFailedKey(error)).toBe(key);
  });

  it.each([
    ["zh-CN", zh],
    ["en-US", en],
    ["de-DE", de],
    ["fr-FR", fr],
  ] as const)("%s: every sentence exists and carries no status code or placeholder", (_locale, catalog) => {
    for (const key of ["load_failed_forbidden", "load_failed_gone", "load_failed_plain", "load_failed_network"] as const) {
      const text = (catalog.session as Record<string, string>)[key];
      expect(text).toBeTruthy();
      expect(text).not.toMatch(/\{|\b[45]\d\d\b|session detail|canvas:/);
    }
  });

  it("the toast's Copy diagnostics carries the status and request id", async () => {
    let action: { label: string; onAction: () => Promise<void> } | undefined;
    function Probe() {
      action = useCopyDiagnosticsToastAction()(new SessionDetailError(500, REQUEST_ID), "route") as typeof action;
      return null;
    }
    state.copy.mockResolvedValue(true);
    renderIntl(<Probe />, { locale: "en-US" });

    expect(action?.label).toBe("Copy diagnostics");
    await action!.onAction();
    const bundle = String(state.copy.mock.calls[0][0]);
    expect(bundle).toContain("http_status: 500");
    expect(bundle).toContain(`request_id: ${REQUEST_ID}`);
  });

  it("repeated failures keep a single toast; a successful open closes it", () => {
    _clearAllToasts();
    const toasts = renderHook(() => useToasts());
    const action = { label: "Copy diagnostics", onAction: () => {} };

    act(() => showSessionLoadFailure({ kind: "error", text: "couldn't open", action }));
    act(() => showSessionLoadFailure({ kind: "error", text: "couldn't open", action }));
    act(() => showSessionLoadFailure({ kind: "error", text: "no permission", action }));
    expect(toasts.result.current.map((item) => item.text)).toEqual(["no permission"]);

    act(() => clearSessionLoadFailure());
    expect(toasts.result.current).toEqual([]);
  });

  it("首页选会话失败的路径用本地化句子 + 复制诊断，不再拼 err.message（接线闸）", () => {
    const page = readFileSync(join(ROOT, "app/(app)/page.tsx"), "utf8");
    expect(page).toContain('text: ts(sessionLoadFailedKey(err)), action: copyDiagnosticsAction(err, "route")');
    expect(page).not.toMatch(/ts\("load_failed", \{ err:/);
    // Both success paths (full opening, read-only report) must dismiss the old failure Toast.
    expect(page.match(/clearSessionLoadFailure\(\);/g)?.length).toBe(2);
    expect(page).toContain("showSessionLoadFailure({ kind: \"error\", text: ts(sessionLoadFailedKey(err))");
  });
});
