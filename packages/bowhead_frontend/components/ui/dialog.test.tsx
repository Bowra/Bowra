/**
 * Dialog interaction contract (component unification P1 step 1, docs/design/组件统一-技术设计.md).
 *
 * jsdom lacks inert/browser Tab navigation; enforce only wraparound key handling here, full behavior in real browser
 * (2026-09-25 local dev: member-invite drawer and report-delete dialog, each 25 Tabs + 6 Shift+Tabs,
 * zero focus escapes; Esc returns focus to trigger and removes all background inert).
 */
import { useRef, useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";

import { renderIntl } from "@/__tests__/intl-render";
import zhMessages from "@/messages/zh-CN.json";

import { ConfirmDialog } from "./confirm-dialog";
import { Dialog, DialogShell } from "./dialog";

afterEach(() => {
  cleanup();
});

function Opener({ onClose }: { onClose?: () => void }) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button type="button" onClick={() => setOpen(true)}>Open</button>
      {open ? (
        <Dialog
          title="Outer"
          onClose={() => {
            onClose?.();
            setOpen(false);
          }}
        >
          <button type="button">Inside</button>
        </Dialog>
      ) : null}
    </>
  );
}

function Nested({ onOuterClose }: { onOuterClose: () => void }) {
  const [inner, setInner] = useState(true);
  return (
    <Dialog title="Outer" onClose={onOuterClose}>
      <button type="button">Outer action</button>
      {inner ? (
        <Dialog title="Inner" onClose={() => setInner(false)}>
          <button type="button">Inner action</button>
        </Dialog>
      ) : null}
    </Dialog>
  );
}

describe("Dialog", () => {
  it("focuses the first control on open and returns focus to the trigger on close", async () => {
    renderIntl(<Opener />);
    const trigger = screen.getByRole("button", { name: "Open" });
    trigger.focus();
    fireEvent.click(trigger);

    expect(document.activeElement).toBe(screen.getByRole("button", { name: "Inside" }));
    fireEvent.keyDown(document.activeElement!, { key: "Escape" });

    expect(screen.queryByRole("dialog")).toBeNull();
    await vi.waitFor(() => expect(document.activeElement).toBe(trigger));
  });

  it("prefers the element marked as initial focus", () => {
    renderIntl(
      <Dialog title="Confirm" onClose={vi.fn()}>
        <button type="button">First</button>
        <button type="button" data-dialog-initial-focus="">Safe</button>
      </Dialog>,
    );
    expect(document.activeElement).toBe(screen.getByRole("button", { name: "Safe" }));
  });

  // Equivalent mutation: nested outer dialog stays open via two independent mechanisms: inner backdrop stopPropagation (React events
  // cannot reach outer panel Esc handler) and useDismiss bubbles.escapeKey=false. Removing one still passes;
  // removing both fails (2026-09-25 observed).
  it("closes only the topmost dialog on Escape", () => {
    const onOuterClose = vi.fn();
    renderIntl(<Nested onOuterClose={onOuterClose} />);

    fireEvent.keyDown(screen.getByRole("button", { name: "Inner action" }), { key: "Escape" });
    expect(screen.queryByRole("dialog", { name: "Inner" })).toBeNull();
    expect(onOuterClose).not.toHaveBeenCalled();

    fireEvent.keyDown(screen.getByRole("button", { name: "Outer action" }), { key: "Escape" });
    expect(onOuterClose).toHaveBeenCalledTimes(1);
  });

  it("closes on backdrop click but not on clicks inside the panel", () => {
    const onClose = vi.fn();
    renderIntl(
      <Dialog title="Backdrop" onClose={onClose}>
        <button type="button">Inside</button>
      </Dialog>,
    );
    const dialog = screen.getByRole("dialog", { name: "Backdrop" });
    fireEvent.click(screen.getByRole("button", { name: "Inside" }));
    expect(onClose).not.toHaveBeenCalled();
    const backdrop = dialog.closest<HTMLElement>("[data-settings-dialog-portal]")!;
    fireEvent.pointerDown(backdrop);
    fireEvent.click(backdrop);
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("does not close when a press starts inside the panel and is released on the backdrop", () => {
    // Press in input, drag-select text, release on backdrop: browser dispatches click to common ancestor (backdrop).
    const onClose = vi.fn();
    renderIntl(
      <Dialog title="Drag" onClose={onClose}>
        <input aria-label="Name" />
      </Dialog>,
    );
    const backdrop = screen.getByRole("dialog", { name: "Drag" }).closest<HTMLElement>("[data-settings-dialog-portal]")!;
    fireEvent.pointerDown(screen.getByRole("textbox", { name: "Name" }));
    fireEvent.click(backdrop);
    expect(onClose).not.toHaveBeenCalled();
  });

  it("closes once per Escape even when mounted inline under a document-level React root", async () => {
    const { createRoot } = await import("react-dom/client");
    const { act } = await import("@testing-library/react");
    // Next App Router mounts React root on document: panel synthetic onKeyDown and useDismiss native listener share document.
    const onClose = vi.fn();
    const host = document.implementation.createHTMLDocument("root");
    const root = createRoot(host);
    await act(async () => {
      root.render(
        <html><head /><body>
          <DialogShell onClose={onClose} label="Inline" mount="inline"><input aria-label="q" /></DialogShell>
        </body></html>,
      );
    });
    await act(async () => { await new Promise((r) => setTimeout(r, 20)); });
    const input = host.querySelector("input")!;
    input.focus();
    await act(async () => {
      input.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    });
    expect(onClose).toHaveBeenCalledTimes(1);
    await act(async () => root.unmount());
  });

  it("keeps a caller's own aria-describedby from panelProps", () => {
    renderIntl(
      <DialogShell onClose={vi.fn()} label="Own" panelProps={{ "aria-describedby": "own-error" }}>
        <p id="own-error">Bad input</p>
      </DialogShell>,
    );
    expect(screen.getByRole("dialog", { name: "Own" }).getAttribute("aria-describedby")).toBe("own-error");
  });

  it("links the confirm description for screen readers", () => {
    renderIntl(
      <ConfirmDialog title="Delete report" description="3 sections will be removed." confirmLabel="Delete" onCancel={vi.fn()} onConfirm={vi.fn()} />,
    );
    const dialog = screen.getByRole("dialog", { name: "Delete report" });
    const describedBy = dialog.getAttribute("aria-describedby");
    expect(describedBy).toBeTruthy();
    expect(document.getElementById(describedBy!)?.textContent).toBe("3 sections will be removed.");
  });

  it("keeps keyboard events from reaching handlers outside the dialog", () => {
    const outerKeyDown = vi.fn();
    renderIntl(
      <div onKeyDown={outerKeyDown}>
        <Dialog title="Scoped" onClose={vi.fn()}>
          <button type="button">Inside</button>
        </Dialog>
      </div>,
    );
    fireEvent.keyDown(screen.getByRole("button", { name: "Inside" }), { key: "Enter" });
    expect(outerKeyDown).not.toHaveBeenCalled();
  });

  it("wraps Tab from the last control to the first and Shift+Tab back", () => {
    renderIntl(
      <Dialog title="Wrap" onClose={vi.fn()}>
        <button type="button">First</button>
        <button type="button">Last</button>
      </Dialog>,
    );
    const first = screen.getByRole("button", { name: "First" });
    const last = screen.getByRole("button", { name: "Last" });
    last.focus();
    fireEvent.keyDown(last, { key: "Tab" });
    expect(document.activeElement).toBe(first);
    fireEvent.keyDown(first, { key: "Tab", shiftKey: true });
    expect(document.activeElement).toBe(last);
  });
});

describe("DialogShell", () => {
  it("keeps the caller's own markup and attributes so migrated dialogs look the same", () => {
    const onClose = vi.fn();
    renderIntl(
      <DialogShell
        onClose={onClose}
        label="Custom"
        closeOnBackdrop={false}
        backdropClassName="kb-modal-backdrop"
        panelClassName="kb-modal"
        panelProps={{ "data-testid": "kb-modal" } as Record<string, string>}
      >
        <button type="button">Inside</button>
      </DialogShell>,
    );
    const panel = screen.getByRole("dialog", { name: "Custom" });
    expect(panel.className).toBe("kb-modal");
    expect(panel.getAttribute("data-testid")).toBe("kb-modal");
    const backdrop = panel.closest<HTMLElement>("[data-settings-dialog-portal]")!;
    expect(backdrop.className).toContain("kb-modal-backdrop");

    fireEvent.pointerDown(backdrop);
    fireEvent.click(backdrop);
    expect(onClose).not.toHaveBeenCalled();
    fireEvent.keyDown(screen.getByRole("button", { name: "Inside" }), { key: "Escape" });
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("renders in place when mounted inline so container-scoped styles still apply", () => {
    renderIntl(
      <div className="mset" data-testid="scope">
        <DialogShell onClose={vi.fn()} label="Scoped" mount="inline" panelClassName="review-panel">
          <button type="button">Inside</button>
        </DialogShell>
      </div>,
    );
    const panel = screen.getByRole("dialog", { name: "Scoped" });
    expect(screen.getByTestId("scope").contains(panel)).toBe(true);
    expect(document.activeElement).toBe(screen.getByRole("button", { name: "Inside" }));
  });

  describe('overlay="none"', () => {
    function Anchored({ onClose, busy = false, closeOnBackdrop }: { onClose: () => void; busy?: boolean; closeOnBackdrop?: boolean }) {
      return (
        <div data-testid="workspace">
          <button type="button">Outside</button>
          <DialogShell
            onClose={onClose}
            busy={busy}
            closeOnBackdrop={closeOnBackdrop}
            label="Switcher"
            mount="inline"
            overlay="none"
            panelClassName="results-quick"
          >
            <button type="button">First</button>
            <button type="button">Last</button>
          </DialogShell>
        </div>
      );
    }

    it("renders the panel without a wrapping overlay, in the caller's own container", () => {
      renderIntl(<Anchored onClose={vi.fn()} />);
      const panel = screen.getByRole("dialog", { name: "Switcher" });
      expect(panel.parentElement).toBe(screen.getByTestId("workspace"));
      expect(document.querySelector("[data-settings-dialog-portal]")).toBeNull();
    });

    it("closes on a press outside the panel but not inside", () => {
      const onClose = vi.fn();
      renderIntl(<Anchored onClose={onClose} />);
      fireEvent.pointerDown(screen.getByRole("button", { name: "First" }));
      expect(onClose).not.toHaveBeenCalled();
      fireEvent.pointerDown(screen.getByTestId("workspace"));
      expect(onClose).toHaveBeenCalledTimes(1);
    });

    it("stays open on an outside press while busy or when closeOnBackdrop is off", () => {
      const onClose = vi.fn();
      const { unmount } = renderIntl(<Anchored onClose={onClose} busy />);
      fireEvent.pointerDown(screen.getByTestId("workspace"));
      fireEvent.keyDown(screen.getByRole("button", { name: "First" }), { key: "Escape" });
      unmount();
      renderIntl(<Anchored onClose={onClose} closeOnBackdrop={false} />);
      fireEvent.pointerDown(screen.getByTestId("workspace"));
      expect(onClose).not.toHaveBeenCalled();
    });

    it("still closes on Escape and wraps Tab inside the panel", () => {
      const onClose = vi.fn();
      renderIntl(<Anchored onClose={onClose} />);
      const first = screen.getByRole("button", { name: "First" });
      const last = screen.getByRole("button", { name: "Last" });
      expect(document.activeElement).toBe(first);
      last.focus();
      fireEvent.keyDown(last, { key: "Tab" });
      expect(document.activeElement).toBe(first);
      fireEvent.keyDown(first, { key: "Escape" });
      expect(onClose).toHaveBeenCalledTimes(1);
    });
  });

  it("skips controls that are not rendered when wrapping Tab", () => {
    renderIntl(
      <DialogShell onClose={vi.fn()} label="Upload">
        <button type="button">Close</button>
        <button type="button">Upload</button>
        <input type="file" style={{ display: "none" }} />
        <div hidden><button type="button">Hidden</button></div>
      </DialogShell>,
    );
    const close = screen.getByRole("button", { name: "Close" });
    const upload = screen.getByRole("button", { name: "Upload" });
    upload.focus();
    fireEvent.keyDown(upload, { key: "Tab" });
    expect(document.activeElement).toBe(close);
  });

  it("keeps a caller-provided panel id and cycles Tab through embedded frames, skipping tabindex=-1", () => {
    renderIntl(
      <DialogShell onClose={vi.fn()} label="Preview" panelProps={{ id: "preview-panel" }}>
        <a href="/download">Download</a>
        <iframe title="frame" />
        <button type="button" tabIndex={-1}>Scrim</button>
      </DialogShell>,
    );
    expect(screen.getByRole("dialog", { name: "Preview" }).id).toBe("preview-panel");
    const download = screen.getByRole("link", { name: "Download" });
    expect(document.activeElement).toBe(download);
    fireEvent.keyDown(download, { key: "Tab", shiftKey: true });
    expect(document.activeElement).toBe(screen.getByTitle("frame"));
  });

  it("pulls focus back to the next control when a middle Tab lands outside (WebKit skips buttons)", () => {
    vi.useFakeTimers();
    try {
      renderIntl(
        <DialogShell onClose={vi.fn()} label="Tools">
          <button type="button">Close</button>
          <button type="button">Upload</button>
          <button type="button">Help</button>
        </DialogShell>,
      );
      const close = screen.getByRole("button", { name: "Close" });
      close.focus();
      fireEvent.keyDown(close, { key: "Tab" });
      // Simulate WebKit: browser default action moves focus to body.
      (document.activeElement as HTMLElement).blur();
      vi.runAllTimers();
      expect(document.activeElement).toBe(screen.getByRole("button", { name: "Upload" }));
    } finally {
      vi.useRealTimers();
    }
  });

  it("returns focus to returnFocusRef when the opener is gone (e.g. a menu item)", async () => {
    function MenuOpened() {
      const moreRef = useRef<HTMLButtonElement>(null);
      const [menu, setMenu] = useState(false);
      const [open, setOpen] = useState(false);
      return (
        <>
          <button type="button" ref={moreRef} onClick={() => setMenu(true)}>More</button>
          {menu ? (
            <button type="button" onClick={() => { setMenu(false); setOpen(true); }}>Delete report</button>
          ) : null}
          {open ? (
            <DialogShell onClose={() => setOpen(false)} label="Confirm" returnFocusRef={moreRef}>
              <button type="button">Cancel</button>
            </DialogShell>
          ) : null}
        </>
      );
    }
    renderIntl(<MenuOpened />);
    fireEvent.click(screen.getByRole("button", { name: "More" }));
    const item = screen.getByRole("button", { name: "Delete report" });
    item.focus();
    fireEvent.click(item);
    fireEvent.keyDown(screen.getByRole("button", { name: "Cancel" }), { key: "Escape" });
    await vi.waitFor(() => expect(document.activeElement).toBe(screen.getByRole("button", { name: "More" })));
  });

  it("focuses the panel itself when the panel carries the initial-focus marker", () => {
    renderIntl(
      <DialogShell
        onClose={vi.fn()}
        label="Sidebar"
        mount="inline"
        overlay="none"
        panelProps={{ "data-dialog-initial-focus": "" } as Record<string, string>}
      >
        <a href="/">Home</a>
      </DialogShell>,
    );
    expect(document.activeElement).toBe(screen.getByRole("dialog", { name: "Sidebar" }));
  });

  it("runs the caller's own key handler before containing the event", () => {
    const outer = vi.fn();
    const own = vi.fn();
    renderIntl(
      <div onKeyDown={outer}>
        <DialogShell onClose={vi.fn()} label="Own keys" mount="inline" overlay="none" panelProps={{ onKeyDown: own }}>
          <button type="button">Inside</button>
        </DialogShell>
      </div>,
    );
    fireEvent.keyDown(screen.getByRole("button", { name: "Inside" }), { key: "k", ctrlKey: true });
    expect(own).toHaveBeenCalledTimes(1);
    expect(outer).not.toHaveBeenCalled();
  });

  it("leaves elements from getInsideElements out of the background marking", () => {
    function WithSiblingScrim() {
      const scrimRef = useRef<HTMLButtonElement>(null);
      return (
        <div>
          <button type="button" ref={scrimRef} tabIndex={-1} aria-label="scrim" />
          <p>Page content</p>
          <DialogShell
            onClose={vi.fn()}
            label="Drawer"
            mount="inline"
            overlay="none"
            closeOnBackdrop={false}
            getInsideElements={() => (scrimRef.current ? [scrimRef.current] : [])}
          >
            <button type="button">Inside</button>
          </DialogShell>
        </div>
      );
    }
    renderIntl(<WithSiblingScrim />);
    const marked = "[inert], [aria-hidden='true'], [data-floating-ui-inert]";
    expect(screen.getByText("Page content").closest(marked)).not.toBeNull();
    expect(screen.getByRole("button", { name: "scrim", hidden: true }).closest(marked)).toBeNull();
  });
});

describe("Dialog busy, background and drawer", () => {
  it("blocks backdrop and Escape close while busy", () => {
    const onClose = vi.fn();
    const { rerender } = render(
      <NextIntlClientProvider locale="zh-CN" messages={zhMessages}>
        <Dialog title="Busy dialog" onClose={onClose} busy>
          <button type="button">Focusable</button>
        </Dialog>
      </NextIntlClientProvider>,
    );

    const dialog = screen.getByRole("dialog", { name: "Busy dialog" });
    const backdrop = dialog.closest<HTMLElement>("[data-settings-dialog-portal]")!;
    fireEvent.keyDown(dialog, { key: "Escape" });
    fireEvent.pointerDown(backdrop);
    fireEvent.click(backdrop);
    expect(onClose).not.toHaveBeenCalled();

    rerender(
      <NextIntlClientProvider locale="zh-CN" messages={zhMessages}>
        <Dialog title="Busy dialog" onClose={onClose}>
          <button type="button">Focusable</button>
        </Dialog>
      </NextIntlClientProvider>,
    );
    fireEvent.keyDown(screen.getByRole("dialog", { name: "Busy dialog" }), { key: "Escape" });
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("makes background content inert and restores it on close", () => {
    const background = document.createElement("main");
    const decorative = document.createElement("div");
    decorative.setAttribute("aria-hidden", "true");
    document.body.append(background, decorative);

    const { unmount } = renderIntl(
      <Dialog title="Modal dialog" onClose={vi.fn()}>
        <button type="button">Focusable</button>
      </Dialog>,
    );

    // floating-ui sets inert where supported; jsdom lacks it, falling back to its marker + aria-hidden.
    expect(background.hasAttribute("inert") || background.hasAttribute("data-floating-ui-inert")).toBe(true);
    expect(background.getAttribute("aria-hidden")).toBe("true");
    unmount();
    expect(background.hasAttribute("inert") || background.hasAttribute("data-floating-ui-inert")).toBe(false);
    expect(background.getAttribute("aria-hidden")).toBeNull();
    // Element already hidden from screen readers remains hidden after closing.
    expect(decorative.getAttribute("aria-hidden")).toBe("true");
    background.remove();
    decorative.remove();
  });

  it("localizes the default drawer close label", () => {
    renderIntl(
      <Dialog title="Drawer" variant="drawer" onClose={vi.fn()}>
        <p>Body</p>
      </Dialog>,
      { locale: "en-US" },
    );
    expect(screen.getByRole("button", { name: "Close" })).toBeTruthy();
  });
});

// Layers (design heading “图层”): jsdom only computes inline-style levels; actual covering is verified in
// e2e/layer-stacking.spec.ts with chromium and webkit.
describe("DialogShell layer", () => {
  function HighHost() {
    const [open, setOpen] = useState(false);
    return (
      <div style={{ position: "fixed", zIndex: 9999 }}>
        <button type="button" onClick={() => setOpen(true)}>Open from host</button>
        {open ? (
          <DialogShell onClose={vi.fn()} label="Nested" backdropStyle={{ zIndex: 1300 }}>
            <button type="button">Inside</button>
          </DialogShell>
        ) : null}
      </div>
    );
  }

  it("uses the top-layer token for layer=top", async () => {
    renderIntl(
      <DialogShell onClose={vi.fn()} label="Confirm" layer="top">
        <button type="button">OK</button>
      </DialogShell>,
    );
    const backdrop = await waitFor(() => {
      const node = document.querySelector<HTMLElement>("[data-settings-dialog-portal]");
      expect(node).not.toBeNull();
      return node!;
    });
    expect(backdrop.style.zIndex).toBe("var(--z-dialog-top)");
  });

  it("lifts a body dialog above the root layer of the element that opened it", async () => {
    renderIntl(<HighHost />);
    const trigger = screen.getByRole("button", { name: "Open from host" });
    trigger.focus();
    fireEvent.click(trigger);
    await waitFor(() => {
      const node = document.querySelector<HTMLElement>("[data-settings-dialog-portal]");
      expect(node?.style.zIndex).toBe("10000");
    });
  });
});
