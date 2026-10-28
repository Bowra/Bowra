/**
 * P2 batch 4, per the P2 Navigation Spec breakpoints:
 * - from 1024 the sidebar is persistent (1024–1279 at 232, from 1280 at 248), expanded by default, and
 *   below 1280 the workbench collapses it to the icon rail while the results canvas is open;
 * - 768–1023 the sidebar is hidden and the top bar's menu button opens it as an overlay;
 * - below 768 the mobile drawer.
 */
export const SIDEBAR_DESKTOP_BREAKPOINT = 1024;
export const SIDEBAR_OVERLAY_BREAKPOINT = 768;
export const SIDEBAR_AUTO_COMPACT_BELOW = 1280;

/** 768–1023 opens the sidebar as an overlay from the top bar instead of the drawer. */
export function sidebarTabletBand(viewportWidth: number): boolean {
  return viewportWidth >= SIDEBAR_OVERLAY_BREAKPOINT && viewportWidth < SIDEBAR_DESKTOP_BREAKPOINT;
}

/** Presentation inputs supplied by the page. */
export interface SidebarLayoutOptions {
  /** The workbench asked for the icon rail (results canvas open). */
  autoCompact?: boolean;
}

/** The icon rail is forced (and the expand control opens the overlay) at this width. */
export function sidebarAutoCompactActive(
  viewportWidth: number,
  options: SidebarLayoutOptions = {},
): boolean {
  return Boolean(
    options.autoCompact &&
      viewportWidth >= SIDEBAR_DESKTOP_BREAKPOINT &&
      viewportWidth < SIDEBAR_AUTO_COMPACT_BELOW,
  );
}

/** Widths at which the docked sidebar honours the remembered expanded / compact choice. */
export function sidebarDockedModeApplies(
  viewportWidth: number,
  options: SidebarLayoutOptions = {},
): boolean {
  return viewportWidth >= SIDEBAR_DESKTOP_BREAKPOINT && !sidebarAutoCompactActive(viewportWidth, options);
}
export const SIDEBAR_BOOTSTRAP_STORAGE_KEY =
  "bowhead.sidebar.mode.current";

export type SidebarDesktopMode = "expanded" | "compact";
export type SidebarPresentation =
  | SidebarDesktopMode
  | "overlay"
  | "drawer";

export interface SidebarUiState {
  preferredDesktopMode: SidebarDesktopMode;
  overlayOpen: boolean;
  drawerOpen: boolean;
  workRecoveryOpen: boolean;
}

export type SidebarAction =
  | { type: "set-desktop-mode"; mode: SidebarDesktopMode }
  | { type: "set-overlay-open"; open: boolean }
  | { type: "set-drawer-open"; open: boolean }
  | { type: "set-work-recovery-open"; open: boolean }
  | { type: "close-transient" };

export function createSidebarState(
  preferredDesktopMode: SidebarDesktopMode,
): SidebarUiState {
  return {
    preferredDesktopMode,
    overlayOpen: false,
    drawerOpen: false,
    workRecoveryOpen: false,
  };
}

export function sidebarReducer(
  state: SidebarUiState,
  action: SidebarAction,
): SidebarUiState {
  switch (action.type) {
    case "set-desktop-mode":
      return { ...state, preferredDesktopMode: action.mode };
    case "set-overlay-open":
      return { ...state, overlayOpen: action.open };
    case "set-drawer-open":
      return { ...state, drawerOpen: action.open };
    case "set-work-recovery-open":
      return { ...state, workRecoveryOpen: action.open };
    case "close-transient":
      return createSidebarState(state.preferredDesktopMode);
  }
}

export function deriveSidebarPresentation(
  state: SidebarUiState,
  viewportWidth: number,
  options: SidebarLayoutOptions = {},
): SidebarPresentation {
  if (sidebarTabletBand(viewportWidth)) {
    return state.overlayOpen ? "overlay" : "drawer";
  }
  if (viewportWidth < SIDEBAR_DESKTOP_BREAKPOINT) return "drawer";
  if (!sidebarDockedModeApplies(viewportWidth, options)) {
    return state.overlayOpen ? "overlay" : "compact";
  }
  return state.preferredDesktopMode;
}

export function sidebarModeStorageKey(userId: string): string | null {
  const normalized = userId.trim();
  return normalized ? `bowhead.sidebar.mode.${normalized}` : null;
}

export function defaultDesktopMode(viewportWidth: number): SidebarDesktopMode {
  return viewportWidth >= SIDEBAR_DESKTOP_BREAKPOINT ? "expanded" : "compact";
}

export function readPreferredDesktopMode(
  userId: string,
  viewportWidth: number,
): SidebarDesktopMode {
  const fallback = defaultDesktopMode(viewportWidth);
  return readStoredPreferredDesktopMode(userId) ?? fallback;
}

/**
 * Returns only an explicit, per-user choice. Callers that own a route-level
 * default must be able to distinguish “no choice yet” from the width default.
 */
export function readStoredPreferredDesktopMode(
  userId: string,
): SidebarDesktopMode | null {
  const key = sidebarModeStorageKey(userId);
  if (!key || typeof window === "undefined") return null;
  try {
    const stored = window.localStorage?.getItem?.(key);
    return stored === "expanded" || stored === "compact" ? stored : null;
  } catch {
    return null;
  }
}

export function readBootstrappedDesktopMode(
  viewportWidth: number,
): SidebarDesktopMode {
  const fallback = defaultDesktopMode(viewportWidth);
  if (typeof document !== "undefined") {
    const bootstrapped = document.documentElement.dataset.sidebarMode;
    if (bootstrapped === "expanded" || bootstrapped === "compact") {
      return bootstrapped;
    }
  }
  if (typeof window === "undefined") return fallback;
  try {
    const stored = window.localStorage?.getItem?.(
      SIDEBAR_BOOTSTRAP_STORAGE_KEY,
    );
    return stored === "expanded" || stored === "compact"
      ? stored
      : fallback;
  } catch {
    return fallback;
  }
}

export function writeBootstrappedDesktopMode(
  mode: SidebarDesktopMode,
): void {
  if (typeof document !== "undefined") {
    document.documentElement.dataset.sidebarMode = mode;
  }
  if (typeof window === "undefined") return;
  try {
    window.localStorage?.setItem?.(
      SIDEBAR_BOOTSTRAP_STORAGE_KEY,
      mode,
    );
  } catch {
    // The in-memory mode remains authoritative when storage is unavailable.
  }
}

export function writePreferredDesktopMode(
  userId: string,
  mode: SidebarDesktopMode,
): void {
  const key = sidebarModeStorageKey(userId);
  if (!key || typeof window === "undefined") return;
  writeBootstrappedDesktopMode(mode);
  try {
    window.localStorage?.setItem?.(key, mode);
  } catch {
    // Preference persistence must never block navigation.
  }
}
