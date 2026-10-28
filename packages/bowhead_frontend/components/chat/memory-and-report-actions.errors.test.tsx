/**
 * Workbench report-header clear/delete, memory-candidate card, Save to Memory and /remember failures:
 * explain only what happened and what to do; status codes and raw API text go into Copy Diagnostics (outside the announcement container).
 */
import "@testing-library/jest-dom/vitest";

import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { cleanup, fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";
import de from "@/messages/de-DE.json";
import en from "@/messages/en-US.json";
import zh from "@/messages/zh-CN.json";

const state = vi.hoisted(() => ({ toast: vi.fn(), deleteEntry: vi.fn() }));
vi.mock("@/lib/use-toast", async (orig) => ({ ...(await orig<Record<string, unknown>>()), toast: (...args: unknown[]) => state.toast(...args) }));
vi.mock("@/lib/memory-api", async (orig) => ({ ...(await orig<Record<string, unknown>>()), deleteEntry: (...args: unknown[]) => state.deleteEntry(...args) }));

import { HttpStatusError } from "@/lib/api-client";
import type { PendingCandidate } from "@/lib/memory-api";
import { dispatchRememberLine, rememberFailure } from "@/lib/remember-slash";
import { reportHeaderActionFailedKey } from "@/lib/report-header-errors";
import { SaveToMemoryButton, saveToMemoryFailedKey } from "@/components/canvas/save-to-memory-button";
import { MemoryCandidateCard } from "./memory-candidate-card";

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "../..");
const REQUEST_ID = "0123456789abcdef0123456789abcdef";
const failure = (status: number, label = "x") => new HttpStatusError(label, new Response(null, { status, headers: { "x-request-id": REQUEST_ID } }));
const LOCALES = [
  ["zh-CN", zh, "复制诊断"],
  ["en-US", en, "Copy diagnostics"],
  ["de-DE", de, "Diagnoseinformationen kopieren"],
] as const;
const originalFetch = globalThis.fetch;

afterEach(() => {
  cleanup();
  state.toast.mockReset();
  state.deleteEntry.mockReset();
  globalThis.fetch = originalFetch;
});

describe("workbench report header clear / delete", () => {
  it.each([
    ["clear", 500, "hdr_clear_failed_plain"],
    ["delete", 500, "hdr_delete_failed_plain"],
    ["delete", 403, "hdr_action_forbidden"],
    ["clear", 404, "hdr_action_gone"],
  ] as const)("%s %s → %s", (action, status, key) => {
    expect(reportHeaderActionFailedKey(action, failure(status))).toBe(key);
  });

  it.each(LOCALES)("%s catalog has every sentence, none with a status code or {err}", (_l, catalog) => {
    for (const key of ["hdr_clear_failed_plain", "hdr_delete_failed_plain", "hdr_action_forbidden", "hdr_action_gone", "hdr_action_network"]) {
      const text = (catalog.canvas as Record<string, string>)[key];
      expect(text).toBeTruthy();
      expect(text).not.toMatch(/\{|\b[45]\d\d\b/);
    }
  });

  it("工作台报告头的失败路径用本地化句子 + 复制诊断（接线闸）", () => {
    const source = readFileSync(join(ROOT, "components/chat-workbench.tsx"), "utf8");
    expect(source).toContain('text: t(reportHeaderActionFailedKey("clear", error)), action: copyDiagnosticsAction(error, "route")');
    expect(source).toContain('text: t(reportHeaderActionFailedKey("delete", error)), action: copyDiagnosticsAction(error, "route")');
    expect(source).not.toMatch(/hdr_(clear|delete)_failed", \{ err/);
  });
});

describe("/remember", () => {
  it("an API failure keeps the status off the message and hands the error over for diagnostics", async () => {
    globalThis.fetch = vi.fn(async () => new Response(JSON.stringify({ detail: "boom" }), { status: 500, headers: { "x-request-id": REQUEST_ID } })) as typeof fetch;
    const result = await dispatchRememberLine("/remember k=v");
    if (result.ok) throw new Error("expected failure");
    expect(result.reason).toBe("failed");
    expect(result.error).toMatchObject({ status: 500, requestId: REQUEST_ID });
  });

  it.each([
    [{ status: 413 }, { reason: "too_long" }],
    [{ status: 422, code: "memory_write_gate_refused", body: { detail: { code: "memory_write_gate_refused", reason: "contains_secret" } } }, { reason: "refused", refusal: "contains_secret" }],
    [{ status: 422, code: "memory_write_gate_refused", body: { detail: { code: "memory_write_gate_refused", reason: "something_new" } } }, { reason: "refused" }],
    // A 422 without an error code means an invalid request (name too long, bad format), not a write-policy rejection.
    [{ status: 422, body: { detail: [{ loc: ["body", "key"], msg: "too long" }] } }, { reason: "invalid" }],
    [{ status: 409, code: "memory_scope_conflict" }, { reason: "scope_conflict" }],
    [{ status: 423 }, { reason: "locked" }],
    [{ status: 503 }, { reason: "failed" }],
  ] as const)("%o → %o", (error, expected) => {
    expect(rememberFailure(error)).toEqual(expected);
  });

  it("a gate refusal carries the gate's reason through dispatch", async () => {
    globalThis.fetch = vi.fn(async () => new Response(
      JSON.stringify({ detail: { code: "memory_write_gate_refused", reason: "one_off", message: "LLM prose" } }),
      { status: 422, headers: { "content-type": "application/json", "x-request-id": REQUEST_ID } },
    )) as typeof fetch;
    const result = await dispatchRememberLine("/remember k=v");
    if (result.ok) throw new Error("expected failure");
    expect(result).toMatchObject({ reason: "refused", refusal: "one_off" });
  });

  it("a syntax error names the right format", async () => {
    const result = await dispatchRememberLine("/remember nonsense");
    if (result.ok) throw new Error("expected failure");
    expect(result.reason).toBe("syntax");
  });

  it.each(LOCALES)("%s catalog: every /remember failure explains how to fix it without a status", (_l, catalog) => {
    for (const reason of ["syntax", "empty_key", "too_long", "invalid", "refused", "scope_conflict", "locked", "failed"]) {
      const text = (catalog.composer as unknown as Record<string, string>)[`remember_failed_${reason}`];
      expect(text).toBeTruthy();
      expect(text).not.toMatch(/\{|\b[45]\d\d\b|remember:/);
    }
    expect((catalog.composer as unknown as Record<string, string>).remember_failed_syntax).toContain("/remember");
  });
});

describe("save to memory", () => {
  it.each(LOCALES)("%s: 413 says memory is full, no status code, Copy diagnostics outside the alert", async (locale, catalog, copy) => {
    globalThis.fetch = vi.fn(async (url: RequestInfo | URL) =>
      String(url).includes("save-from-canvas")
        ? new Response(JSON.stringify({ detail: "notes char limit 3000 exceeded" }), { status: 413, headers: { "content-type": "application/json", "x-request-id": REQUEST_ID } })
        : new Response(JSON.stringify({ char_count: 0, char_limit: 3000 }), { status: 200, headers: { "content-type": "application/json" } }),
    ) as typeof fetch;
    renderIntl(<SaveToMemoryButton canvasId="cnv_1" sectionId="sec_1" />, { locale });
    fireEvent.click(screen.getByTestId("save-to-memory-btn"));
    fireEvent.change(screen.getByTestId("save-to-memory-textarea"), { target: { value: "x" } });
    fireEvent.click(screen.getByTestId("save-to-memory-confirm"));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent((catalog.canvas as Record<string, string>).stm_failed_full);
    expect(alert.textContent).not.toMatch(/413|char limit|memory_api/);
    const button = screen.getByRole("button", { name: copy });
    expect(alert.contains(button)).toBe(false);
  });

  it.each([
    [413, "stm_failed_full"],
    [423, "stm_failed_locked"],
    [400, "stm_failed_invalid"],
    [404, "stm_failed_gone"],
    [503, "stm_failed_unavailable"],
    [500, "stm_failed_plain"],
  ] as const)("%s → %s", (status, key) => {
    expect(saveToMemoryFailedKey(failure(status))).toBe(key);
  });
});

describe("memory candidate card undo", () => {
  const candidate = {
    id: "cand_1", userId: "u1", origin: "feedback_draft", kind: "preference", key: "k", value: "偏好环比", reason: "r",
    sourceTurnId: "t_1", status: "pending", decidedBy: null, decidedAt: null, createdAt: "", updatedAt: "",
  } as unknown as PendingCandidate;

  it.each(LOCALES)("%s: undo failure settles in the card with no status code and offers Retry", async (locale, catalog) => {
    state.deleteEntry.mockRejectedValue(failure(500, "delete memory"));
    renderIntl(<MemoryCandidateCard items={[candidate]} actingId={null} decide={vi.fn().mockResolvedValue({ ok: true, entryId: "mem_1" })} />, { locale });
    fireEvent.click(screen.getByTestId("memory-candidate-remember"));
    fireEvent.click(await screen.findByTestId("memory-candidate-undo-cand_1"));

    const row = await screen.findByTestId("memory-candidate-settled-cand_1");
    await waitFor(() => expect(row.getAttribute("data-state")).toBe("undo_failed"));
    expect(row.textContent).toBe(catalog.memory_card.undo_failed_row.replace(/<\/?b>/g, "") + catalog.memory_card.retry);
    expect(String(row.textContent)).not.toMatch(/500|delete memory/);
    expect(state.toast).not.toHaveBeenCalled();
  });

  it("a non-policy decide failure keeps the generic sentence in the card with technical details", async () => {
    renderIntl(<MemoryCandidateCard items={[candidate]} actingId={null} decide={vi.fn().mockResolvedValue({ ok: false, error: failure(500) })} />);
    fireEvent.click(screen.getByTestId("memory-candidate-remember"));

    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toBe("没能记住，稍后再试。");
    expect(alert.textContent).not.toMatch(/500/);
    expect(screen.getByRole("button", { name: "技术详情" })).toBeTruthy();
    expect(state.toast).not.toHaveBeenCalled();
  });
});
