/**
 * Sprint ρ Phase B — toast hook + store.
 *
 * Asserts:
 *   1. `toast()` returns id and appears in `useToasts()` snapshot.
 *   2. Duration rules: 4 seconds by default; at least 8 with an action; errors never auto-dismiss.
 *   3. Custom `durationMs` honored.
 *   4. `dismissToast` removes by id; `onDismiss` runs only if the action was not performed.
 *   5. Subscriber list cleans up on unmount.
 */
import { renderHook, act } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  toast,
  dismissToast,
  pauseToast,
  resumeToast,
  useToasts,
  _clearAllToasts,
} from "@/lib/use-toast";

beforeEach(() => {
  vi.useFakeTimers();
  _clearAllToasts();
});

afterEach(() => {
  vi.useRealTimers();
  _clearAllToasts();
});

describe("use-toast", () => {
  it("push → subscriber sees toast", () => {
    const { result } = renderHook(() => useToasts());
    expect(result.current).toEqual([]);
    let id = "";
    act(() => {
      id = toast({ kind: "error", text: "Boom" });
    });
    expect(result.current).toHaveLength(1);
    expect(result.current[0].id).toBe(id);
    expect(result.current[0].kind).toBe("error");
    expect(result.current[0].text).toBe("Boom");
  });

  it("auto-dismiss after default 4000ms", () => {
    const { result } = renderHook(() => useToasts());
    act(() => {
      toast({ kind: "info", text: "hi" });
    });
    expect(result.current).toHaveLength(1);
    act(() => {
      vi.advanceTimersByTime(3999);
    });
    expect(result.current).toHaveLength(1);
    act(() => {
      vi.advanceTimersByTime(1);
    });
    expect(result.current).toHaveLength(0);
  });

  it("honors custom durationMs", () => {
    const { result } = renderHook(() => useToasts());
    act(() => {
      toast({ kind: "warning", text: "x", durationMs: 500 });
    });
    act(() => {
      vi.advanceTimersByTime(499);
    });
    expect(result.current).toHaveLength(1);
    act(() => {
      vi.advanceTimersByTime(1);
    });
    expect(result.current).toHaveLength(0);
  });

  it("keeps a toast with an action for at least 8 seconds", () => {
    const { result } = renderHook(() => useToasts());
    act(() => {
      toast({
        kind: "success",
        text: "deleted",
        durationMs: 5000,
        action: { label: "Undo", onAction: () => undefined },
      });
    });
    expect(result.current[0].durationMs).toBe(8000);
    act(() => {
      vi.advanceTimersByTime(7999);
    });
    expect(result.current).toHaveLength(1);
    act(() => {
      vi.advanceTimersByTime(1);
    });
    expect(result.current).toHaveLength(0);
  });

  it("keeps a longer requested duration for a toast with an action", () => {
    const { result } = renderHook(() => useToasts());
    act(() => {
      toast({
        kind: "info",
        text: "retry",
        durationMs: 12_000,
        action: { label: "Retry", onAction: () => undefined },
      });
      vi.advanceTimersByTime(11_999);
    });
    expect(result.current).toHaveLength(1);
    act(() => {
      vi.advanceTimersByTime(1);
    });
    expect(result.current).toHaveLength(0);
  });

  it("never auto-dismisses an error, even with a requested duration", () => {
    const { result } = renderHook(() => useToasts());
    let id = "";
    act(() => {
      id = toast({ kind: "error", text: "failed", durationMs: 500 });
      vi.advanceTimersByTime(60 * 60 * 1000);
    });
    expect(result.current).toHaveLength(1);
    expect(result.current[0].durationMs).toBe(0);
    act(() => {
      dismissToast(id);
    });
    expect(result.current).toHaveLength(0);
  });

  it("calls onDismiss on timeout or manual close, but not after the action ran", () => {
    const onTimeout = vi.fn();
    const onManual = vi.fn();
    const onActioned = vi.fn();
    let manualId = "";
    let actionedId = "";
    act(() => {
      toast({ kind: "success", text: "a", onDismiss: onTimeout });
      manualId = toast({ kind: "error", text: "b", onDismiss: onManual });
      actionedId = toast({
        kind: "success",
        text: "c",
        action: { label: "Undo", onAction: () => undefined },
        onDismiss: onActioned,
      });
    });
    act(() => {
      dismissToast(manualId);
      dismissToast(actionedId, "action");
      vi.advanceTimersByTime(4000);
    });
    expect(onTimeout).toHaveBeenCalledTimes(1);
    expect(onManual).toHaveBeenCalledTimes(1);
    expect(onActioned).not.toHaveBeenCalled();
  });

  it("dismissToast removes by id", () => {
    const { result } = renderHook(() => useToasts());
    let aId = "";
    act(() => {
      aId = toast({ kind: "info", text: "a" });
      toast({ kind: "info", text: "b" });
    });
    expect(result.current).toHaveLength(2);
    act(() => {
      dismissToast(aId);
    });
    expect(result.current).toHaveLength(1);
    expect(result.current[0].text).toBe("b");
  });

  it("pauses and resumes the remaining auto-dismiss duration", () => {
    const { result } = renderHook(() => useToasts());
    let id = "";
    act(() => {
      id = toast({ kind: "success", text: "undo", durationMs: 1000 });
      vi.advanceTimersByTime(400);
      pauseToast(id);
      vi.advanceTimersByTime(2000);
    });
    expect(result.current).toHaveLength(1);

    act(() => {
      resumeToast(id);
      vi.advanceTimersByTime(599);
    });
    expect(result.current).toHaveLength(1);
    act(() => {
      vi.advanceTimersByTime(1);
    });
    expect(result.current).toHaveLength(0);
  });

  it("retains an asynchronous action on the toast snapshot", () => {
    const { result } = renderHook(() => useToasts());
    const onAction = vi.fn();
    act(() => {
      toast({
        kind: "success",
        text: "archived",
        action: { label: "Undo", onAction },
      });
    });
    expect(result.current[0].action).toEqual({
      label: "Undo",
      onAction,
    });
  });

  it("unmount cleans up subscription", () => {
    const { result, unmount } = renderHook(() => useToasts());
    unmount();
    // Push after unmount — no throw.
    act(() => {
      toast({ kind: "info", text: "after-unmount" });
    });
    // result.current is the last rendered snapshot ([]).
    expect(result.current).toEqual([]);
  });
});
