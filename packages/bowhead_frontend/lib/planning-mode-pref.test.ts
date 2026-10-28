import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  getPlanningModePref,
  setPlanningModePref,
} from "./planning-mode-pref";

const originalLocalStorage = window.localStorage;

beforeEach(() => {
  const values = new Map<string, string>();
  Object.defineProperty(window, "localStorage", {
    configurable: true,
    value: {
      getItem: (key: string) => values.get(key) ?? null,
      setItem: (key: string, value: string) => values.set(key, value),
    },
  });
});

afterEach(() => {
  Object.defineProperty(window, "localStorage", {
    configurable: true,
    value: originalLocalStorage,
  });
  vi.restoreAllMocks();
});

describe("planning preference", () => {
  it("defaults to auto", () => {
    expect(getPlanningModePref()).toBe("auto");
  });

  it("persists planning and notifies mounted controls", () => {
    const onChange = vi.fn();
    window.addEventListener("bowhead:planning-mode-changed", onChange);

    setPlanningModePref("planning");

    expect(getPlanningModePref()).toBe("planning");
    expect(onChange).toHaveBeenCalledOnce();
    window.removeEventListener("bowhead:planning-mode-changed", onChange);
  });

  // Three-state integration (2026-08-12): `off` must persist and read back. Previously the frontend had only two states,
  // so turning off stored auto, and backend keyword routing still produced plan cards.
  it("persists off as a distinct value, not a fallback to auto", () => {
    const onChange = vi.fn();
    window.addEventListener("bowhead:planning-mode-changed", onChange);

    setPlanningModePref("off");

    expect(getPlanningModePref()).toBe("off");
    expect(onChange).toHaveBeenCalledOnce();
    window.removeEventListener("bowhead:planning-mode-changed", onChange);
  });

  it("round-trips all three values", () => {
    for (const mode of ["auto", "planning", "off"] as const) {
      setPlanningModePref(mode);
      expect(getPlanningModePref()).toBe(mode);
    }
  });

  // Dirty values must fall back to valid states, or they pass unchanged into chat/send planning_mode.
  // (Backend uses Literal["auto","planning","off"]; dirty strings yield 422).
  it("falls back to auto for unknown persisted values", () => {
    window.localStorage.setItem("bowhead.planning_mode", "PLANNING");
    expect(getPlanningModePref()).toBe("auto");

    window.localStorage.setItem("bowhead.planning_mode", "disabled");
    expect(getPlanningModePref()).toBe("auto");
  });
});
