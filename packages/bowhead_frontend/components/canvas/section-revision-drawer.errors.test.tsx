/** Revision history drawer: loading, restore and rollback failures only explain what happened and what to do; status codes go into Copy Diagnostics (outside the alert). */
import "@testing-library/jest-dom/vitest";

import { cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";

const state = vi.hoisted(() => ({ apiFetch: vi.fn(), patch: vi.fn(), toast: vi.fn() }));
vi.mock("@/lib/api-client", async (orig) => ({ ...(await orig<Record<string, unknown>>()), apiFetch: (...args: unknown[]) => state.apiFetch(...args) }));
vi.mock("@/lib/canvas-pm-adapter", () => ({ patchCanvasSection: (...args: unknown[]) => state.patch(...args) }));
vi.mock("@/lib/use-toast", async (orig) => ({ ...(await orig<Record<string, unknown>>()), toast: (...args: unknown[]) => state.toast(...args) }));

import { HttpStatusError } from "@/lib/api-client";
import { SectionRevisionDrawer } from "./section-revision-drawer";

const REQUEST_ID = "0123456789abcdef0123456789abcdef";
const failed = (status: number) => new Response("Internal Server Error", { status, headers: { "x-request-id": REQUEST_ID } });
const json = (body: unknown) => new Response(JSON.stringify(body), { status: 200, headers: { "content-type": "application/json" } });

const LOCALES = [
  ["zh-CN", "没能加载修改历史", "复制诊断", "恢复此版本…"],
  ["en-US", "Edit history couldn’t be loaded", "Copy diagnostics", "Restore this version…"],
  ["de-DE", "Der Änderungsverlauf konnte nicht geladen werden", "Diagnoseinformationen kopieren", "Diese Version wiederherstellen…"],
] as const;

const LEAK = /HTTP|\b[45]\d\d\b|Internal Server Error|sec_/;

afterEach(() => {
  cleanup();
  state.apiFetch.mockReset();
  state.patch.mockReset();
  state.toast.mockReset();
});

describe("revision drawer errors", () => {
  it.each(LOCALES)("load failure (%s): no status code, Copy diagnostics outside the alert", async (locale, text, copy) => {
    state.apiFetch.mockResolvedValue(failed(500));
    renderIntl(<SectionRevisionDrawer canvasId="cv_1" open onClose={vi.fn()} />, { locale });

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(text);
    expect(alert.textContent).not.toMatch(LEAK);
    const button = screen.getByRole("button", { name: copy });
    expect(alert.contains(button)).toBe(false);
  });

  it.each(LOCALES)("rollback failure (%s): toast without status, with Copy diagnostics", async (locale, _text, copy, restoreLabel) => {
    state.apiFetch.mockResolvedValue(json({ items: [{ id: "rev_1", sectionId: "sec_1", op: "patch", createdAt: "2026-09-06T00:00:00Z", prevPayload: { title: "Before" }, newPayload: { title: "After" } }] }));
    state.patch.mockResolvedValue({ ok: false, status: "error", message: "localized", error: new HttpStatusError("patch section", failed(500)) });
    renderIntl(<SectionRevisionDrawer canvasId="cv_1" open onClose={vi.fn()} />, { locale });

    // Select the revision, then restore it from the sheet footer (confirm first).
    fireEvent.click(await screen.findByRole("option"));
    fireEvent.click(screen.getByRole("button", { name: restoreLabel }));
    const dialogs = await screen.findAllByRole("dialog");
    fireEvent.click(within(dialogs.at(-1)!).getAllByRole("button").at(-1)!);

    await waitFor(() => expect(state.toast).toHaveBeenCalled());
    const shown = state.toast.mock.calls.at(-1)![0];
    expect(shown.kind).toBe("error");
    expect(String(shown.text)).not.toMatch(LEAK);
    expect(String(shown.text)).not.toContain("localized");
    expect(shown.action?.label).toBe(copy);
  });

  it.each([
    [500, "没能恢复这一版"],
    [403, "没有修改这份报告的权限"],
    [404, "这条修改记录已经不在了"],
    [422, "这条旧记录不支持恢复"],
  ] as const)("restore %s says what happened, no status code", async (status, text) => {
    state.apiFetch.mockImplementation(async (url: string) =>
      url.endsWith("/restore")
        ? failed(status)
        : json({ items: [{ id: "rev_2", sectionId: "sec_2", op: "delete", restorable: true, createdAt: "2026-09-06T00:00:00Z", prevPayload: { batch: "b1", batch_host: "sec_2" } }] }),
    );
    renderIntl(<SectionRevisionDrawer canvasId="cv_1" open onClose={vi.fn()} />);

    fireEvent.click(await screen.findByRole("option"));
    fireEvent.click(screen.getByRole("button", { name: "恢复这一节…" }));
    const dialogs = await screen.findAllByRole("dialog");
    fireEvent.click(within(dialogs.at(-1)!).getAllByRole("button").at(-1)!);

    await waitFor(() => expect(state.toast).toHaveBeenCalled());
    const shown = state.toast.mock.calls.at(-1)![0];
    expect(String(shown.text)).toContain(text);
    expect(String(shown.text)).not.toMatch(LEAK);
    expect(shown.action?.label).toBe("复制诊断");
  });
});
