import { beforeEach, describe, expect, it } from "vitest";

import {
  SIDEBAR_BOOTSTRAP_STORAGE_KEY,
  SIDEBAR_DESKTOP_BREAKPOINT,
  createSidebarState,
  defaultDesktopMode,
  deriveSidebarPresentation,
  readBootstrappedDesktopMode,
  readPreferredDesktopMode,
  sidebarAutoCompactActive,
  sidebarDockedModeApplies,
  sidebarTabletBand,
  sidebarModeStorageKey,
  sidebarReducer,
  writeBootstrappedDesktopMode,
  writePreferredDesktopMode,
} from "./sidebar-state";

function installMemStorage() {
  let store: Record<string, string> = {};
  const storage: Storage = {
    getItem: (key) => store[key] ?? null,
    setItem: (key, value) => {
      store[key] = String(value);
    },
    removeItem: (key) => {
      delete store[key];
    },
    clear: () => {
      store = {};
    },
    key: (index) => Object.keys(store)[index] ?? null,
    get length() {
      return Object.keys(store).length;
    },
  };
  Object.defineProperty(window, "localStorage", {
    value: storage,
    configurable: true,
  });
}

describe("sidebar state", () => {
  beforeEach(() => {
    installMemStorage();
    delete document.documentElement.dataset.sidebarMode;
  });

  it("closes transient surfaces without changing the desktop preference", () => {
    const open = {
      ...createSidebarState("compact"),
      overlayOpen: true,
      drawerOpen: true,
      workRecoveryOpen: true,
    };

    expect(sidebarReducer(open, { type: "close-transient" })).toEqual(
      createSidebarState("compact"),
    );
  });

  it("persists the desktop mode per signed-in user", () => {
    expect(sidebarModeStorageKey("usr_1")).toBe(
      "bowhead.sidebar.mode.usr_1",
    );
    expect(readPreferredDesktopMode("usr_1", 1600)).toBe("expanded");
    expect(readPreferredDesktopMode("usr_1", 900)).toBe("compact");

    writePreferredDesktopMode("usr_1", "compact");
    expect(readPreferredDesktopMode("usr_1", 1600)).toBe("compact");
    expect(
      window.localStorage.getItem(SIDEBAR_BOOTSTRAP_STORAGE_KEY),
    ).toBe("compact");
    expect(document.documentElement.dataset.sidebarMode).toBe(
      "compact",
    );
  });

  it("hydrates the first desktop frame from the anonymous mode cache", () => {
    expect(readBootstrappedDesktopMode(1600)).toBe("expanded");

    writeBootstrappedDesktopMode("compact");

    expect(readBootstrappedDesktopMode(1600)).toBe("compact");
  });

  it("does not create an anonymous preference key", () => {
    writePreferredDesktopMode("", "expanded");
    expect(window.localStorage.length).toBe(0);
  });
});

describe("sidebar breakpoints (P2 batch 4)", () => {
  beforeEach(() => {
    installMemStorage();
    delete document.documentElement.dataset.sidebarMode;
  });

  it("keeps the expanded sidebar persistent from 1024", () => {
    const expanded = createSidebarState("expanded");
    expect(SIDEBAR_DESKTOP_BREAKPOINT).toBe(1024);
    expect(deriveSidebarPresentation(expanded, 1024)).toBe("expanded");
    expect(deriveSidebarPresentation(expanded, 1199)).toBe("expanded");
    expect(deriveSidebarPresentation(expanded, 1320)).toBe("expanded");
    expect(sidebarDockedModeApplies(1100)).toBe(true);
  });

  it("hides the sidebar at 768–1023 and opens it as an overlay; below 768 the drawer", () => {
    const expanded = createSidebarState("expanded");
    const overlay = sidebarReducer(expanded, { type: "set-overlay-open", open: true });
    expect(deriveSidebarPresentation(expanded, 900)).toBe("drawer");
    expect(deriveSidebarPresentation(overlay, 768)).toBe("overlay");
    expect(deriveSidebarPresentation(overlay, 1023)).toBe("overlay");
    expect(deriveSidebarPresentation(overlay, 767)).toBe("drawer");
    expect(sidebarTabletBand(900)).toBe(true);
    expect(sidebarTabletBand(767)).toBe(false);
    expect(sidebarTabletBand(1024)).toBe(false);
  });

  it("collapses to the rail while the canvas asks for it, below 1280 only", () => {
    const expanded = createSidebarState("expanded");
    const options = { autoCompact: true };
    expect(deriveSidebarPresentation(expanded, 1240, options)).toBe("compact");
    expect(sidebarAutoCompactActive(1240, options)).toBe(true);
    const overlay = sidebarReducer(expanded, { type: "set-overlay-open", open: true });
    expect(deriveSidebarPresentation(overlay, 1240, options)).toBe("overlay");
    expect(deriveSidebarPresentation(expanded, 1280, options)).toBe("expanded");
    expect(deriveSidebarPresentation(expanded, 1440, options)).toBe("expanded");
    expect(sidebarAutoCompactActive(1240)).toBe(false);
  });

  it("defaults to expanded from 1024", () => {
    expect(defaultDesktopMode(1240)).toBe("expanded");
    expect(defaultDesktopMode(1024)).toBe("expanded");
    expect(defaultDesktopMode(1023)).toBe("compact");
    expect(readPreferredDesktopMode("usr_1", 1240)).toBe("expanded");
    expect(readBootstrappedDesktopMode(1240)).toBe("expanded");
  });
});
