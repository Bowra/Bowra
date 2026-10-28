/** Report-edit autosave failures: the error bar only explains what happened and what to do, without section IDs or status codes; Copy Diagnostics stays outside the alert. */
import "@testing-library/jest-dom/vitest";

import { useState } from "react";
import { cleanup, fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";
import { patchCanvasSection } from "@/lib/canvas-pm-adapter/canvas-patch";
import type { ReportSection } from "@/lib/types";

const saveState = vi.hoisted(() => ({ saving: false, lastError: null as null | { sectionId: string; message: string; error?: unknown } }));

vi.mock("@/lib/use-debounced-sync", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/use-debounced-sync")>();
  return {
    ...actual,
    useDebouncedSync: (opts: Parameters<typeof actual.useDebouncedSync>[0]) => ({
      ...actual.useDebouncedSync(opts),
      lastError: saveState.lastError,
      ...(saveState.saving ? { saving: true } : {}),
    }),
  };
});

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    fetchCanvasStatus: vi.fn(async () => ({ status: "active", effectiveStatus: "active", quarantinedCount: 0 })),
    setCanvasStatus: vi.fn(),
  };
});

import { InlineEditableText } from "@/lib/canvas-pm-adapter/extensions/inline-text";

import { ReportView } from "./report-view";

vi.stubGlobal("matchMedia", (query: string) => ({
  matches: false,
  media: query,
  addEventListener: () => {},
  removeEventListener: () => {},
  addListener: () => {},
  removeListener: () => {},
}));

const SECTIONS = [{ id: "sec_n1", kind: "narrative", markdown: "华东区 GMV 环比下降 8.4%。" }] as unknown as ReportSection[];
const originalFetch = globalThis.fetch;
const REQUEST_ID = "0123456789abcdef0123456789abcdef";

afterEach(() => {
  cleanup();
  saveState.lastError = null;
  saveState.saving = false;
  globalThis.fetch = originalFetch;
  document.documentElement.lang = "";
});

describe("report save failure banner", () => {
  it.each([
    ["zh-CN", "这次修改没有保存", "复制诊断"],
    ["en-US", "This change wasn't saved", "Copy diagnostics"],
    ["de-DE", "Diese Änderung wurde nicht gespeichert", "Diagnoseinformationen kopieren"],
  ] as const)("%s: no section id or status code, Copy diagnostics outside the alert", async (locale, text, copy) => {
    document.documentElement.lang = locale;
    globalThis.fetch = vi.fn(async () => new Response("<html>oops</html>", { status: 500, headers: { "x-request-id": REQUEST_ID } })) as typeof fetch;
    const failed = await patchCanvasSection("cv_1", "sec_n1", { markdown: "changed" }, 3);
    expect(failed.ok).toBe(false);
    if (failed.ok || failed.status !== "error") throw new Error("expected a save error");
    saveState.lastError = { sectionId: "sec_n1", message: failed.message, error: failed.error };

    renderIntl(<ReportView sections={SECTIONS} canvasId="cv_1" canEdit isEditMode />, { locale });

    const alert = await waitFor(() => {
      const found = document.querySelector<HTMLElement>(".canvas-report-alert--error");
      expect(found).not.toBeNull();
      return found!;
    });
    expect(alert).toHaveAttribute("role", "alert");
    expect(alert).toHaveTextContent(text);
    expect(alert.textContent).not.toMatch(/500|sec_n1|HTTP|patch section|保存失败 \(/);
    const button = screen.getByRole("button", { name: copy });
    expect(alert.contains(button)).toBe(false);
  });
});

 describe("report reading lifecycle", () => {
  it("protects pending-only inline input before blur and releases protection after reverting", async () => {
    const changed = vi.fn();
    renderIntl(<><ReportView sections={SECTIONS} canvasId="cv_1" canEdit isEditMode />
      <InlineEditableText value="Original" isEditMode onChange={changed} /></>);
    const input = await screen.findByText("Original");
    input.textContent = "Pending change";
    fireEvent.input(input);
    const unload = new Event("beforeunload", { cancelable: true });
    window.dispatchEvent(unload);
    expect(unload.defaultPrevented).toBe(true);
    expect(input).toHaveTextContent("Pending change");
    expect(changed).not.toHaveBeenCalled();
    input.textContent = "Original";
    fireEvent.input(input);
    const cleanUnload = new Event("beforeunload", { cancelable: true });
    window.dispatchEvent(cleanUnload);
    expect(cleanUnload.defaultPrevented).toBe(false);
  });
  it("exposes a named reading document and switches back to editor semantics", async () => {
    function Harness() {
      const [editing, setEditing] = useState(false);
      return <><button onClick={() => setEditing((value) => !value)}>Start editing</button><ReportView sections={SECTIONS} canvasId="cv_1" canEdit isEditMode={editing} /></>;
    }
    renderIntl(<Harness />);
    const document = await screen.findByRole("document");
    expect(document).toHaveAccessibleName();
    expect(document).toHaveAttribute("contenteditable", "false");
    fireEvent.click(screen.getByRole("button", { name: "Start editing" }));
    await waitFor(() => expect(document).toHaveAttribute("role", "textbox"));
    expect(document).toHaveAccessibleName();
    fireEvent.click(screen.getByRole("button", { name: "Start editing" }));
    await waitFor(() => expect(document).toHaveAttribute("role", "document"));
    expect(document).not.toHaveAttribute("aria-multiline");
  });
  it("protects an in-flight save even when no dirty section remains", () => {
    saveState.saving = true;
    renderIntl(<ReportView sections={SECTIONS} canvasId="cv_1" canEdit isEditMode />);
    const unload = new Event("beforeunload", { cancelable: true });
    window.dispatchEvent(unload);
    expect(unload.defaultPrevented).toBe(true);
  });
});
