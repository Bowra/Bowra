import { StrictMode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";

const mocks = vi.hoisted(() => ({ api: vi.fn() }));
vi.mock("next-intl", () => ({
  useTranslations:
    () => (key: string, values?: Record<string, string>) =>
      values ? `${key}:${Object.values(values).join(",")}` : key,
}));
vi.mock("@/lib/email-send-confirmation-api", () => ({
  emailSendConfirmationApi: mocks.api,
}));
import { EmailSendConfirmations } from "./email-send-confirmations";
import { CONFIRMATION_POLL_MS } from "@/lib/use-confirmation-queue-poll";
import {
  _resetAllTurnsForTesting,
  applyStepDone,
  applyStepStarted,
} from "@/lib/turn-store";
import type { ToolName } from "@/components/chat/step-list/types";

afterEach(() => {
  cleanup();
  vi.resetAllMocks();
  _resetAllTurnsForTesting();
});

const confirmation = {
  id: "esc_one",
  turn_id: "t_one",
  session_id: "s_one",
  to: "customer@example.com",
  subject: "本月品类销售概况",
  body: "护肤美妆用品约 9.54 亿元，位列第一。",
  expires_at: "2030-01-01T00:00:00Z",
};

describe("email send confirmation", () => {
  it("shows the exact email and sends nothing until the owner approves", async () => {
    mocks.api.mockImplementation(async (path: string) =>
      path === "" ? { confirmations: [confirmation] } : { ok: true },
    );
    render(<EmailSendConfirmations />);
    await screen.findByRole("dialog");
    expect(screen.getByText("to:customer@example.com")).toBeTruthy();
    expect(screen.getByText("subject:本月品类销售概况")).toBeTruthy();
    expect(screen.getByText(/9\.54 亿元/)).toBeTruthy();
    expect(
      mocks.api.mock.calls.some((call) => String(call[0]).endsWith("/decision")),
    ).toBe(false);
    await waitFor(() =>
      expect(document.activeElement).toBe(
        screen.getByRole("button", { name: "reject" }),
      ),
    );
    fireEvent.click(screen.getByRole("button", { name: "approve" }));
    await waitFor(() =>
      expect(mocks.api).toHaveBeenCalledWith("/esc_one/decision", "POST", {
        decision: "approve",
      }),
    );
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  });

  it("renders on the top layer outside the app shell", async () => {
    // Desktop shell .v2-app-root creates its own stacking context; rendering in place would put this beneath an open dialog
    // (Real-browser evidence: e2e/layer-stacking.spec.ts, Email Send Confirmation).
    mocks.api.mockImplementation(async () => ({ confirmations: [confirmation] }));
    const { container } = render(<EmailSendConfirmations />);
    const dialog = await screen.findByRole("dialog");
    expect(container.contains(dialog)).toBe(false);
    expect(dialog.closest<HTMLElement>("[data-settings-dialog-portal]")?.style.zIndex).toBe("var(--z-dialog-top)");
  });

  it("lets the owner decline", async () => {
    mocks.api.mockImplementation(async (path: string) =>
      path === "" ? { confirmations: [confirmation] } : { ok: true },
    );
    render(<EmailSendConfirmations />);
    await screen.findByRole("dialog");
    fireEvent.click(screen.getByRole("button", { name: "reject" }));
    await waitFor(() =>
      expect(mocks.api).toHaveBeenCalledWith("/esc_one/decision", "POST", {
        decision: "reject",
      }),
    );
  });

  it("shows attachment names and sizes before approving", async () => {
    mocks.api.mockImplementation(async (path: string) =>
      path === "" ? { confirmations: [{
        ...confirmation,
        attachments: [{ artifact_id: "art_deck", filename: "销售报告.pptx", mime: "application/vnd.openxmlformats-officedocument.presentationml.presentation", size: 2048 }],
      }] } : { ok: true },
    );
    render(<EmailSendConfirmations />);
    const files = await screen.findByRole("region", { name: "attachments" });
    expect(files.textContent).toContain("销售报告.pptx (2 KB)");
    expect(mocks.api.mock.calls.some((call) => String(call[0]).endsWith("/decision"))).toBe(false);
    fireEvent.click(screen.getByRole("button", { name: "approve" }));
    await waitFor(() => expect(mocks.api).toHaveBeenCalledWith("/esc_one/decision", "POST", { decision: "approve" }));
  });

  it("is not dismissed by Escape or a backdrop click: only the buttons decide", async () => {
    mocks.api.mockImplementation(async (path: string) =>
      path === "" ? { confirmations: [confirmation] } : { ok: true },
    );
    render(<EmailSendConfirmations />);
    const dialog = await screen.findByRole("dialog");
    fireEvent.keyDown(dialog, { key: "Escape" });
    fireEvent.click(dialog.parentElement!);
    expect(screen.getByRole("dialog")).toBe(dialog);
    expect(
      mocks.api.mock.calls.some((call) => String(call[0]).endsWith("/decision")),
    ).toBe(false);
  });

  it("reads once on mount, then polls only while a send_email step is running", async () => {
    vi.useFakeTimers();
    try {
      mocks.api.mockResolvedValue({ confirmations: [] });
      const reads = () => mocks.api.mock.calls.length;
      // Let React commit between timer steps (the gate is render state), as a browser would.
      const tick = (ms: number) => act(() => vi.advanceTimersByTimeAsync(ms));
      render(<EmailSendConfirmations />);
      await tick(0);
      expect(reads()).toBe(1);

      // Idle: no turn is sending email, so nothing is polled.
      await tick(CONFIRMATION_POLL_MS * 10);
      expect(reads()).toBe(1);

      // An unrelated running tool does not start polling.
      applyStepStarted({ turnId: "t_one", stepId: "st_bi", stepType: "tool_run", toolName: "bi_query", headerZh: "查询数据" });
      await tick(CONFIRMATION_POLL_MS * 3);
      expect(reads()).toBe(1);

      // send_email running: read right away, then at the shared interval.
      applyStepStarted({
        turnId: "t_one",
        stepId: "st_mail",
        stepType: "tool_run",
        toolName: "send_email" as ToolName,
        headerZh: "发送邮件",
      });
      await tick(0);
      expect(reads()).toBe(2);
      await tick(CONFIRMATION_POLL_MS);
      expect(reads()).toBe(3);

      // Step finished: polling stops again.
      applyStepDone({ turnId: "t_one", stepId: "st_mail", state: "done" });
      await tick(CONFIRMATION_POLL_MS * 10);
      expect(reads()).toBe(3);
    } finally {
      vi.useRealTimers();
    }
  });

  it("shows a confirmation from the first read under StrictMode, without waiting a poll interval", async () => {
    // StrictMode mounts, unmounts and mounts the poll effect while the first read is out;
    // the second run must take that read's result, not wait CONFIRMATION_POLL_MS for the next.
    vi.useFakeTimers();
    try {
      mocks.api.mockImplementation(
        () =>
          new Promise((resolve) =>
            setTimeout(() => resolve({ confirmations: [confirmation] }), 50),
          ),
      );
      render(
        <StrictMode>
          <EmailSendConfirmations />
        </StrictMode>,
      );
      await act(() => vi.advanceTimersByTimeAsync(100));
      expect(screen.getByRole("dialog")).toBeTruthy();
      expect(mocks.api).toHaveBeenCalledTimes(1);
    } finally {
      vi.useRealTimers();
    }
  });

  it("keeps the dialog and reports when the decision is not accepted", async () => {
    mocks.api.mockImplementation(async (path: string) => {
      if (path === "") return { confirmations: [confirmation] };
      throw new Error("gone");
    });
    render(<EmailSendConfirmations />);
    await screen.findByRole("dialog");
    fireEvent.click(screen.getByRole("button", { name: "approve" }));
    await screen.findByRole("alert");
    expect(screen.getByRole("dialog")).toBeTruthy();
  });
});
