import {
  act,
  cleanup,
  fireEvent,
  screen,
  waitFor,
} from "@testing-library/react";
import { useState } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";
import { DialogShell } from "@/components/ui/dialog";
import {
  _clearAllToasts,
  toast,
  useToasts,
} from "@/lib/use-toast";

import { ToastContainer } from "./toast-container";

beforeEach(() => {
  _clearAllToasts();
});

afterEach(() => {
  vi.useRealTimers();
  cleanup();
  _clearAllToasts();
});

function ToastCount() {
  const toasts = useToasts();
  return <output data-testid="toast-count">{toasts.length}</output>;
}

function WithDialog() {
  const [open, setOpen] = useState(false);
  return (
    <>
      <main data-testid="page">
        <button type="button" onClick={() => setOpen(true)}>
          Open dialog
        </button>
      </main>
      <ToastContainer />
      <ToastCount />
      {open ? (
        <DialogShell onClose={() => setOpen(false)} label="Confirm">
          <button type="button">Inside</button>
        </DialogShell>
      ) : null}
    </>
  );
}

function renderContainer() {
  return renderIntl(
    <>
      <ToastContainer />
      <ToastCount />
    </>,
  );
}

describe("ToastContainer actions", () => {
  it("runs an asynchronous action once, dismisses on success and skips onDismiss", async () => {
    const onAction = vi.fn().mockResolvedValue(undefined);
    const onDismiss = vi.fn();
    renderContainer();
    act(() => {
      toast({
        kind: "success",
        text: "Session archived",
        action: { label: "Undo", onAction },
        onDismiss,
      });
    });

    fireEvent.click(await screen.findByRole("button", { name: "Undo" }));
    await waitFor(() => expect(onAction).toHaveBeenCalledTimes(1));
    await waitFor(() =>
      expect(screen.getByTestId("toast-count").textContent).toBe("0"),
    );
    expect(onDismiss).not.toHaveBeenCalled();
  });

  it("closes a toast with the close button and reports the dismissal", async () => {
    const onDismiss = vi.fn();
    renderContainer();
    act(() => {
      toast({ kind: "error", text: "Upload failed", onDismiss });
    });

    fireEvent.click(await screen.findByRole("button", { name: "关闭" }));
    expect(screen.getByTestId("toast-count").textContent).toBe("0");
    expect(onDismiss).toHaveBeenCalledTimes(1);
  });
});

describe("ToastContainer timing", () => {
  it("resumes an archive countdown after its hovered container unmounts", () => {
    vi.useFakeTimers();
    const view = renderContainer();
    act(() => {
      toast({ kind: "success", text: "Session archived", action: { label: "Undo", onAction: vi.fn() } });
      vi.advanceTimersByTime(3000);
    });
    fireEvent.mouseEnter(screen.getByTestId("toast-success"));
    view.unmount();
    renderContainer();
    act(() => vi.advanceTimersByTime(5000));
    expect(screen.getByTestId("toast-count").textContent).toBe("0");
  });

  it("pauses the countdown while hovered and resumes on leave", () => {
    vi.useFakeTimers();
    renderContainer();
    act(() => {
      vi.runOnlyPendingTimers(); // mount effect
      toast({ kind: "info", text: "Saved" });
    });
    const item = screen.getByTestId("toast-info");

    act(() => {
      vi.advanceTimersByTime(3000);
    });
    fireEvent.mouseEnter(item);
    act(() => {
      vi.advanceTimersByTime(30_000);
    });
    expect(screen.getByTestId("toast-count").textContent).toBe("1");

    fireEvent.mouseLeave(item);
    act(() => {
      vi.advanceTimersByTime(999);
    });
    expect(screen.getByTestId("toast-count").textContent).toBe("1");
    act(() => {
      vi.advanceTimersByTime(1);
    });
    expect(screen.getByTestId("toast-count").textContent).toBe("0");
  });
});

describe("ToastContainer accessibility", () => {
  it("announces errors in an assertive region and the rest in a polite status region", async () => {
    renderContainer();
    act(() => {
      toast({ kind: "error", text: "Delete failed" });
      toast({ kind: "success", text: "Deleted" });
    });

    const assertive = await screen.findByTestId("toast-region-assertive");
    const polite = screen.getByTestId("toast-region-polite");
    expect(assertive.getAttribute("aria-live")).toBe("assertive");
    expect(assertive.textContent).toContain("Delete failed");
    expect(assertive.textContent).not.toContain("Deleted");
    expect(polite.getAttribute("aria-live")).toBe("polite");
    expect(polite.getAttribute("role")).toBe("status");
    expect(polite.textContent).toContain("Deleted");
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("keeps toasts readable and clickable while a modal dialog makes the background inert", async () => {
    const onAction = vi.fn();
    renderIntl(<WithDialog />);
    act(() => {
      toast({ kind: "error", text: "Delete failed" });
      toast({
        kind: "success",
        text: "Deleted",
        action: { label: "Undo", onAction },
      });
    });
    await screen.findByText("Delete failed");

    fireEvent.click(screen.getByRole("button", { name: "Open dialog" }));
    await screen.findByRole("dialog", { name: "Confirm" });
    // Positive control: the dialog really marks its background, or the negative assertion below is meaningless. jsdom lacks inert,
    // so floating-ui falls back to aria-hidden, but the data-floating-ui-inert marker matches real browsers.
    await waitFor(() =>
      expect(
        screen.getByTestId("page").closest("[data-floating-ui-inert]"),
      ).not.toBeNull(),
    );

    for (const node of [
      screen.getByTestId("toast-error"),
      screen.getByTestId("toast-success"),
    ]) {
      expect(node.closest("[inert]")).toBeNull();
      expect(node.closest("[data-floating-ui-inert]")).toBeNull();
      expect(node.closest("[aria-hidden='true']")).toBeNull();
    }

    fireEvent.click(screen.getByRole("button", { name: "Undo" }));
    await waitFor(() => expect(onAction).toHaveBeenCalledTimes(1));
  });
});

describe("ToastContainer repeated errors", () => {
  it("shows a repeated error once with how many times it happened", async () => {
    renderIntl(<ToastContainer />);
    act(() => {
      for (let index = 0; index < 3; index += 1) toast({ kind: "error", text: "保存失败" });
    });

    const region = await screen.findByTestId("toast-region-assertive");
    await waitFor(() => expect(region.querySelectorAll("[data-toast-id]")).toHaveLength(1));
    expect(screen.getByTestId("toast-repeat-count").textContent).toBe("共 3 次");
  });
});
