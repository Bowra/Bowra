import { afterEach, describe, expect, it } from "vitest";

import { PUBLIC_COLOR_SCHEME_BOOTSTRAP_SCRIPT } from "./public-color-scheme-bootstrap-script";

function runAt(pathname: string, dark: boolean, preset?: string) {
  window.history.replaceState(null, "", pathname);
  const root = document.documentElement;
  delete root.dataset.theme;
  delete root.dataset.themeMode;
  root.style.colorScheme = "";
  if (preset) root.dataset.theme = preset;
  const listeners: Array<(event: { matches: boolean }) => void> = [];
  window.matchMedia = ((query: string) => ({
    matches: dark,
    media: query,
    addEventListener: (_: string, fn: (event: { matches: boolean }) => void) => listeners.push(fn),
  })) as unknown as typeof window.matchMedia;
  new Function(PUBLIC_COLOR_SCHEME_BOOTSTRAP_SCRIPT)();
  return { root, listeners };
}

afterEach(() => {
  window.history.replaceState(null, "", "/");
  delete document.documentElement.dataset.theme;
  delete document.documentElement.dataset.themeMode;
});

describe("public color-scheme bootstrap", () => {
  it("follows the system dark setting for the product default brand", () => {
    const { root, listeners } = runAt("/login/", true);
    expect(root.dataset.theme).toBe("dark");
    expect(root.dataset.themeMode).toBe("system");
    listeners.forEach((fn) => fn({ matches: false }));
    expect(root.dataset.theme).toBe("light");
  });

  it("keeps a theme the gateway brand injection already wrote", () => {
    const { root } = runAt("/login/", true, "light");
    expect(root.dataset.theme).toBe("light");
  });

  it("does not follow dark on canvas export pages under a runtime prefix", () => {
    const { root, listeners } = runAt("/ai/apps/bowhead/internal/canvas-export/visual/", true);
    expect(root.dataset.theme).toBeUndefined();
    expect(listeners).toHaveLength(0);
  });
});
