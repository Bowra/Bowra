/**
 * V4 PRD-D-5 · SaveToMemoryButton unit tests.
 *
 * Coverage:
 *  - hidden when canvasId null
 *  - shows button labeled 📌 保存到 memory by default
 *  - clicking opens textarea + cancel/save buttons
 *  - 200 char cap on input
 *  - save disabled until summary non-empty
 *  - save calls /api/v1/memory/notes/save-from-canvas with right body
 *  - save error displays alert
 *  - isVerifiedFactKind whitelist check
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, screen, waitFor } from "@testing-library/react";

import { renderIntl } from "@/__tests__/intl-render";
import {
  SaveToMemoryButton,
  isVerifiedFactKind,
} from "./save-to-memory-button";

describe("isVerifiedFactKind", () => {
  it("accepts kpi_row, chart, data_table", () => {
    expect(isVerifiedFactKind("kpi_row")).toBe(true);
    expect(isVerifiedFactKind("chart")).toBe(true);
    expect(isVerifiedFactKind("data_table")).toBe(true);
  });
  it("rejects narrative, kpi (old name), insight_card, eyebrow", () => {
    expect(isVerifiedFactKind("narrative")).toBe(false);
    expect(isVerifiedFactKind("kpi")).toBe(false);  // spike: real name is kpi_row
    expect(isVerifiedFactKind("insight_card")).toBe(false);
    expect(isVerifiedFactKind("eyebrow")).toBe(false);
    expect(isVerifiedFactKind("chart_placeholder")).toBe(false);
  });
});

describe("SaveToMemoryButton", () => {
  let fetchSpy: ReturnType<typeof vi.spyOn>;

  beforeEach(() => {
    // memory-revive S1: create a fresh Response per call; handleOpen fetchNotes first
    // consumes the body, so reusing a Response makes later save r.json() fail with body-consumed.
    fetchSpy = vi.spyOn(globalThis, "fetch").mockImplementation(async () =>
      new Response(
        JSON.stringify({
          user_id: "u1",
          content: "📌 some fact",
          char_count: 12,
          pinned_count: 1,
          bullet_count: 0,
          free_text_lines: 0,
          last_edited_at: null,
          last_edited_source: "verified_in_report",
          is_locked: false,
          lock_expires_at: null,
          verification_anchor: {
            canvas_id: "cnv_1",
            section_id: "sec_1",
            verifier_result_hash: "abc",
            saved_at: "2026-05-03T10:00:00Z",
          },
          verification_status: "valid",
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      ),
    );
  });

  afterEach(() => {
    fetchSpy.mockRestore();
    cleanup();
  });

  it("hides when canvasId is null", () => {
    const { container } = renderIntl(
      <SaveToMemoryButton canvasId={null} sectionId="sec_1" />,
    );
    expect(container.querySelector('[data-testid="save-to-memory-btn"]')).toBeNull();
  });

  it("shows button by default", () => {
    renderIntl(<SaveToMemoryButton canvasId="cnv_1" sectionId="sec_1" />);
    expect(screen.getByTestId("save-to-memory-btn")).toBeTruthy();
    expect(screen.queryByTestId("save-to-memory-popover")).toBeNull();
  });

  it("clicking opens popover with textarea + cancel/save", () => {
    renderIntl(<SaveToMemoryButton canvasId="cnv_1" sectionId="sec_1" />);
    fireEvent.click(screen.getByTestId("save-to-memory-btn"));
    expect(screen.getByTestId("save-to-memory-popover")).toBeTruthy();
    expect(screen.getByTestId("save-to-memory-textarea")).toBeTruthy();
    expect(screen.getByTestId("save-to-memory-confirm")).toBeTruthy();
  });

  it("save disabled on untouched as-of prefill (防呆)", () => {
    // memory-revive S1: prefill "截至 X年X月，" on opening; the prefix alone is not a valid summary.
    renderIntl(<SaveToMemoryButton canvasId="cnv_1" sectionId="sec_1" />);
    fireEvent.click(screen.getByTestId("save-to-memory-btn"));
    const textarea = screen.getByTestId("save-to-memory-textarea") as HTMLTextAreaElement;
    expect(textarea.value).toContain("截至");
    const confirm = screen.getByTestId("save-to-memory-confirm") as HTMLButtonElement;
    expect(confirm.disabled).toBe(true);
    // Empty input also disables saving
    fireEvent.change(textarea, { target: { value: "" } });
    expect(confirm.disabled).toBe(true);
  });

  it("shows near-limit warning when notes usage >= 80%", async () => {
    // memory-revive S1 usage guard: GET /notes reports high usage → warn inside overlay.
    fetchSpy.mockImplementation(async () =>
      new Response(
        JSON.stringify({
          user_id: "u1",
          content: "x",
          char_count: 2500,
          char_limit: 3000,
          pinned_count: 0,
          bullet_count: 0,
          free_text_lines: 0,
          is_locked: false,
          lock_expires_at: null,
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      ),
    );
    renderIntl(<SaveToMemoryButton canvasId="cnv_1" sectionId="sec_1" />);
    fireEvent.click(screen.getByTestId("save-to-memory-btn"));
    await waitFor(() => {
      expect(screen.getByTestId("save-to-memory-warn")).toBeTruthy();
    });
  });

  it("caps input at 200 chars", () => {
    renderIntl(<SaveToMemoryButton canvasId="cnv_1" sectionId="sec_1" />);
    fireEvent.click(screen.getByTestId("save-to-memory-btn"));
    const textarea = screen.getByTestId("save-to-memory-textarea") as HTMLTextAreaElement;
    fireEvent.change(textarea, { target: { value: "x".repeat(300) } });
    expect(textarea.value.length).toBe(200);
  });

  it("typing enables save and POSTs correct body on click", async () => {
    renderIntl(<SaveToMemoryButton canvasId="cnv_1" sectionId="sec_1" />);
    fireEvent.click(screen.getByTestId("save-to-memory-btn"));
    const textarea = screen.getByTestId("save-to-memory-textarea");
    fireEvent.change(textarea, { target: { value: "Q1 营收 1200万" } });
    const confirm = screen.getByTestId("save-to-memory-confirm") as HTMLButtonElement;
    expect(confirm.disabled).toBe(false);
    fireEvent.click(confirm);
    await waitFor(() => {
      expect(fetchSpy).toHaveBeenCalledWith(
        "/api/v1/memory/notes/save-from-canvas",
        expect.objectContaining({
          method: "POST",
          body: JSON.stringify({
            canvas_id: "cnv_1",
            section_id: "sec_1",
            summary_text: "Q1 营收 1200万",
            pinned: false,
          }),
        }),
      );
    });
    // Popover closes after success, button shows ✓
    await waitFor(() => {
      expect(screen.queryByTestId("save-to-memory-popover")).toBeNull();
    });
    expect(screen.getByLabelText("已保存")).toBeTruthy();
  });

  it("shows error message when save fails", async () => {
    fetchSpy.mockResolvedValue(
      new Response(JSON.stringify({ detail: "section kind 'narrative' is not a verified-fact" }), {
        status: 400,
        headers: { "Content-Type": "application/json" },
      }),
    );
    renderIntl(<SaveToMemoryButton canvasId="cnv_1" sectionId="sec_1" />);
    fireEvent.click(screen.getByTestId("save-to-memory-btn"));
    fireEvent.change(screen.getByTestId("save-to-memory-textarea"), {
      target: { value: "x" },
    });
    fireEvent.click(screen.getByTestId("save-to-memory-confirm"));
    await waitFor(() => {
      expect(screen.getByRole("alert")).toBeTruthy();
    });
    // Popover stays open
    expect(screen.queryByTestId("save-to-memory-popover")).not.toBeNull();
  });

  it("trims whitespace before validating min-length", () => {
    renderIntl(<SaveToMemoryButton canvasId="cnv_1" sectionId="sec_1" />);
    fireEvent.click(screen.getByTestId("save-to-memory-btn"));
    fireEvent.change(screen.getByTestId("save-to-memory-textarea"), {
      target: { value: "    " },
    });
    const confirm = screen.getByTestId("save-to-memory-confirm") as HTMLButtonElement;
    expect(confirm.disabled).toBe(true);
  });
});
