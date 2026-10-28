import { act, fireEvent, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";
import { loadReleaseNotes } from "@/lib/help-content";
import { ReleaseNotesPage } from "./release-notes-page";

const { push, router } = vi.hoisted(() => {
  const push = vi.fn();
  return { push, router: { push } };
});
vi.mock("@/lib/use-app-router", () => ({ useAppRouter: () => router }));
vi.mock("@/components/shell/AppShell", () => ({
  AppShell: ({ children }: { children: React.ReactNode }) => (
    <main>{children}</main>
  ),
}));
vi.mock("@/components/shell/Topbar", () => ({ Topbar: () => null }));

let sidebarHeight = 340;
let resize: () => void;

beforeEach(() => {
  sidebarHeight = 340;
  vi.spyOn(HTMLElement.prototype, "clientHeight", "get").mockImplementation(
    function (this: HTMLElement) {
      return this.tagName === "ASIDE" ? sidebarHeight : 0;
    },
  );
  vi.spyOn(HTMLElement.prototype, "scrollHeight", "get").mockImplementation(
    function (this: HTMLElement) {
      return this.hasAttribute("data-release-version") ? 100 : 0;
    },
  );
  vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(
    function (this: HTMLElement) {
      return new DOMRect(
        0,
        0,
        260,
        this.hasAttribute("data-release-version") || this.tagName === "UL"
          ? 100
          : this.tagName === "BUTTON"
            ? 40
            : 0,
      );
    },
  );
  vi.stubGlobal(
    "ResizeObserver",
    class {
      constructor(callback: () => void) {
        resize = callback;
      }
      observe() {}
      disconnect() {}
    },
  );
});

afterEach(() => {
  vi.clearAllMocks();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  window.history.replaceState(null, "", window.location.pathname);
  delete window.__BOWHEAD_BASE_PATH__;
});

describe("ReleaseNotesPage", () => {
  it.each(["en-US", "zh-CN", "de-DE", "fr-FR"] as const)(
    "renders release history in %s",
    (locale) => {
      const metadata = loadReleaseNotes(undefined, locale);
      const article = {
        ...metadata,
        content:
          `# ${metadata.title}\n\n` +
          Array.from(
            { length: 4 },
            (_, index) => `## v1.5.${3 - index}\n\nSynthetic release ${3 - index}.`,
          ).join("\n\n"),
      };
      renderIntl(<ReleaseNotesPage article={article} />, { locale });
      expect(screen.getByRole("heading", { level: 1 }).textContent).toBe(
        article.title,
      );
      expect(screen.getAllByRole("heading", { level: 2 }).length).toBe(3);
      const loadMore = {
        "en-US": "Load more",
        "zh-CN": "加载更多",
        "de-DE": "Mehr laden",
        "fr-FR": "Charger plus",
      }[locale];
      while (screen.queryByRole("button", { name: loadMore })) {
        fireEvent.click(screen.getByRole("button", { name: loadMore }));
      }
      expect(screen.getByRole("link", { name: "v1.5.0" })).toBeTruthy();
    },
  );

  it.each([
    [100, 1],
    [340, 3],
    [2000, 10],
  ])(
    "fits the navigation in %ipx with a ten-version cap and a one-version minimum",
    (height, expected) => {
      sidebarHeight = height;
      const article = {
        ...loadReleaseNotes(undefined, "en-US"),
        content:
          "# Release notes\n\n" +
          Array.from(
            { length: 12 },
            (_, index) =>
              `## v1.5.${12 - index}\n\n### Improvements\n\nHistory ${12 - index}.`,
          ).join("\n\n"),
      };
      renderIntl(<ReleaseNotesPage article={article} />, { locale: "en-US" });
      expect(screen.getAllByRole("heading", { level: 2 })).toHaveLength(
        expected,
      );
      expect(screen.getByRole("button", { name: "Load more" })).toBeTruthy();
    },
  );

  it("refits on resize without hiding manually loaded history", () => {
    const article = {
      ...loadReleaseNotes(undefined, "en-US"),
      content:
        "# Release notes\n\n" +
        Array.from(
          { length: 12 },
          (_, index) =>
            `## v1.5.${12 - index}\n\n### Improvements\n\nHistory ${12 - index}.`,
        ).join("\n\n"),
    };
    renderIntl(<ReleaseNotesPage article={article} />, { locale: "en-US" });
    sidebarHeight = 540;
    act(() => resize());
    expect(screen.getAllByRole("heading", { level: 2 })).toHaveLength(5);
    sidebarHeight = 240;
    act(() => resize());
    expect(screen.getAllByRole("heading", { level: 2 })).toHaveLength(2);
    fireEvent.click(screen.getByRole("button", { name: "Load more" }));
    expect(screen.getAllByRole("heading", { level: 2 })).toHaveLength(5);
    sidebarHeight = 140;
    act(() => resize());
    expect(screen.getAllByRole("heading", { level: 2 })).toHaveLength(5);
    sidebarHeight = 2000;
    act(() => resize());
    expect(screen.getAllByRole("heading", { level: 2 })).toHaveLength(10);
    fireEvent.click(screen.getByRole("button", { name: "Load more" }));
    expect(screen.getAllByRole("heading", { level: 2 })).toHaveLength(12);
    expect(screen.queryByRole("button", { name: "Load more" })).toBeNull();
  });

  it("progressively shows three older versions until the full history is visible", () => {
    const article = {
      ...loadReleaseNotes(undefined, "en-US"),
      content:
        "# Release notes\n\n" +
        Array.from(
          { length: 8 },
          (_, index) =>
            `## v1.5.${8 - index}\n\n### Improvements\n\nHistory ${8 - index}.`,
        ).join("\n\n"),
    };
    renderIntl(<ReleaseNotesPage article={article} />, { locale: "en-US" });
    expect(screen.getAllByRole("heading", { level: 2 })).toHaveLength(3);
    expect(screen.getByRole("status").textContent).toBe(
      "Showing 3 of 8 versions",
    );
    expect(screen.queryByRole("link", { name: "v1.5.5" })).toBeNull();
    expect(screen.queryByText("History 5.")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Load more" }));
    expect(screen.getAllByRole("heading", { level: 2 })).toHaveLength(6);
    expect(screen.getByText("History 3.")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Load more" }));
    expect(screen.getAllByRole("heading", { level: 2 })).toHaveLength(8);
    expect(screen.getByRole("link", { name: "v1.5.1" })).toBeTruthy();
    expect(screen.getByRole("status").textContent).toBe("All 8 versions shown");
    expect(screen.queryByRole("button", { name: "Load more" })).toBeNull();
  });

  it("searches unloaded history and keeps matching section anchors readable", () => {
    const article = {
      ...loadReleaseNotes(undefined, "en-US"),
      content:
        "# Release notes\n\n" +
        Array.from(
          { length: 7 },
          (_, index) =>
            `## v1.5.${7 - index}\n\n### Improvements\n\nArchived feature ${7 - index}.`,
        ).join("\n\n"),
    };
    renderIntl(<ReleaseNotesPage article={article} />, { locale: "en-US" });
    const search = screen.getByRole("searchbox");
    expect(screen.queryByText("Archived feature 1.")).toBeNull();
    fireEvent.change(search, { target: { value: "feature 1" } });
    const result = screen.getByRole("link", { name: /Improvements/ });
    expect(result.getAttribute("href")).toBe("#release-v1-5-1-section-1");
    expect(document.getElementById("release-v1-5-1-section-1")).toBeTruthy();
    expect(document.querySelector("article")?.textContent).toContain(
      "Archived feature 1.",
    );
    expect(screen.getAllByRole("heading", { level: 2 })).toHaveLength(1);
    expect(document.querySelector("article mark")?.textContent).toBe(
      "feature 1",
    );
    fireEvent.change(search, { target: { value: "v1.5.1" } });
    expect(screen.getByRole("link", { name: "v1.5.1" })).toBeTruthy();
    fireEvent.change(search, { target: { value: "" } });
    expect(screen.getAllByRole("heading", { level: 2 })).toHaveLength(3);
    expect(screen.getByRole("button", { name: "Load more" })).toBeTruthy();
  });

  it("opens a deep link to an initially hidden historical section", () => {
    const scrollIntoView = vi.fn();
    const originalScroll = HTMLElement.prototype.scrollIntoView;
    HTMLElement.prototype.scrollIntoView = scrollIntoView;
    window.history.replaceState(null, "", "#release-v1-5-1-section-1");
    try {
      const article = {
        ...loadReleaseNotes(undefined, "en-US"),
        content:
          "# Release notes\n\n" +
          Array.from(
            { length: 7 },
            (_, index) =>
              `## v1.5.${7 - index}\n\n### Improvements\n\nArchived feature ${7 - index}.`,
          ).join("\n\n"),
      };
      renderIntl(<ReleaseNotesPage article={article} />, { locale: "en-US" });
      expect(
        screen.getByRole("link", { name: "v1.5.1" }).getAttribute("href"),
      ).toBe("#release-v1-5-1");
      expect(document.getElementById("release-v1-5-1-section-1")).toBeTruthy();
      expect(scrollIntoView).toHaveBeenCalled();
    } finally {
      HTMLElement.prototype.scrollIntoView = originalScroll;
    }
  });

  it("keeps reading position when loading after an explicit section link", () => {
    const originalScroll = HTMLElement.prototype.scrollIntoView;
    const scrollIntoView = vi.fn();
    HTMLElement.prototype.scrollIntoView = scrollIntoView;
    window.history.replaceState(null, "", "#release-v1-5-7-section-1");
    try {
      const article = {
        ...loadReleaseNotes(undefined, "en-US"),
        content:
          "# Release notes\n\n" +
          Array.from(
            { length: 7 },
            (_, index) =>
              `## v1.5.${7 - index}\n\n### Improvements\n\nArchived feature ${7 - index}.`,
          ).join("\n\n"),
      };
      renderIntl(<ReleaseNotesPage article={article} />, { locale: "en-US" });
      const body = document.querySelector("article")!;
      body.scrollTop = 180;
      const heading = body.querySelector("h2");
      scrollIntoView.mockClear();
      window.history.replaceState(null, "", "#release-v1-5-6-section-1");
      act(() => window.dispatchEvent(new HashChangeEvent("hashchange")));
      expect(scrollIntoView).not.toHaveBeenCalled();
      fireEvent.click(screen.getByRole("button", { name: "Load more" }));
      expect(body.scrollTop).toBe(180);
      expect(body.querySelector("h2")).toBe(heading);
      expect(scrollIntoView).not.toHaveBeenCalled();
    } finally {
      HTMLElement.prototype.scrollIntoView = originalScroll;
    }
  });

  it("restores the list, reading position and original link after clearing search", () => {
    const article = {
      ...loadReleaseNotes(undefined, "en-US"),
      content:
        "# Release notes\n\n" +
        Array.from(
          { length: 7 },
          (_, index) =>
            `## v1.5.${7 - index}\n\n### Improvements\n\nArchived feature ${7 - index}.`,
        ).join("\n\n"),
    };
    renderIntl(<ReleaseNotesPage article={article} />, { locale: "en-US" });
    const body = document.querySelector("article")!;
    const main = body.closest("main")!;
    const sidebar = document.querySelector("aside")!;
    body.scrollTop = 180;
    main.scrollTop = 90;
    sidebar.scrollTop = 40;
    window.history.replaceState(null, "", "#release-v1-5-7");
    fireEvent.change(screen.getByRole("searchbox"), {
      target: { value: "feature 1" },
    });
    expect(body.scrollTop).toBe(0);
    expect(main.scrollTop).toBe(0);
    expect(screen.getAllByRole("heading", { level: 2 })).toHaveLength(1);
    window.history.replaceState(null, "", "#release-v1-5-1");
    fireEvent.change(screen.getByRole("searchbox"), { target: { value: "" } });
    expect(body.scrollTop).toBe(180);
    expect(main.scrollTop).toBe(90);
    expect(sidebar.scrollTop).toBe(40);
    expect(window.location.hash).toBe("#release-v1-5-7");
    expect(screen.getAllByRole("heading", { level: 2 })).toHaveLength(3);
  });

  it("highlights repeated literal keywords without changing formatted content", () => {
    const article = {
      ...loadReleaseNotes(undefined, "en-US"),
      content:
        "# Release notes\n\n## v2.0.0\n\n### Reports\n\nUse **A+B** and a+b, not AAB.",
    };
    renderIntl(<ReleaseNotesPage article={article} />, { locale: "en-US" });
    fireEvent.change(screen.getByRole("searchbox"), {
      target: { value: "a+b" },
    });
    expect(
      Array.from(
        document.querySelectorAll("article mark"),
        (node) => node.textContent,
      ),
    ).toEqual(["A+B", "a+b"]);
    expect(document.querySelector("article strong mark")).toBeTruthy();
  });

  it("follows scrolling without changing the URL and retains the reading version on resize", () => {
    let offset = 0;
    let frame: FrameRequestCallback;
    const originalRect = vi
      .mocked(HTMLElement.prototype.getBoundingClientRect)
      .getMockImplementation()!;
    vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(
      function (this: HTMLElement) {
        const positions: Record<string, number> = {
          "release-v1-5-3": 100,
          "release-v1-5-3-section-1": 150,
          "release-v1-5-2": 500,
          "release-v1-5-2-section-1": 550,
          "release-v1-5-1": 900,
          "release-v1-5-1-section-1": 950,
        };
        return this.id in positions
          ? new DOMRect(0, positions[this.id] + offset, 260, 30)
          : originalRect.call(this);
      },
    );
    vi.stubGlobal("requestAnimationFrame", (callback: FrameRequestCallback) => {
      frame = callback;
      return 1;
    });
    vi.stubGlobal("cancelAnimationFrame", vi.fn());
    const article = {
      ...loadReleaseNotes(undefined, "en-US"),
      content:
        "# Release notes\n\n" +
        [3, 2, 1]
          .map(
            (version) =>
              `## v1.5.${version}\n\n### Improvements\n\nHistory ${version}.`,
          )
          .join("\n\n"),
    };
    renderIntl(<ReleaseNotesPage article={article} />, { locale: "en-US" });
    offset = -525.5;
    fireEvent.scroll(document.querySelector("article")!);
    act(() => frame(0));
    expect(
      document
        .querySelector('a[href="#release-v1-5-2-section-1"]')
        ?.getAttribute("aria-current"),
    ).toBe("location");
    expect(window.location.hash).toBe("");
    sidebarHeight = 140;
    act(() => resize());
    expect(screen.getAllByRole("heading", { level: 2 })).toHaveLength(2);
  });

  it("follows the mobile document scroller at the bottom and restores it after search", () => {
    const descriptor = Object.getOwnPropertyDescriptor(
      document,
      "scrollingElement",
    );
    Object.defineProperty(document, "scrollingElement", {
      configurable: true,
      value: document.documentElement,
    });
    const clientHeight = vi
      .mocked(
        Object.getOwnPropertyDescriptor(HTMLElement.prototype, "clientHeight")!
          .get!,
      )
      .getMockImplementation()!;
    const scrollHeight = vi
      .mocked(
        Object.getOwnPropertyDescriptor(HTMLElement.prototype, "scrollHeight")!
          .get!,
      )
      .getMockImplementation()!;
    vi.spyOn(HTMLElement.prototype, "clientHeight", "get").mockImplementation(
      function (this: HTMLElement) {
        return this.tagName === "HTML" ? 900 : clientHeight.call(this);
      },
    );
    vi.spyOn(HTMLElement.prototype, "scrollHeight", "get").mockImplementation(
      function (this: HTMLElement) {
        return this.tagName === "HTML" ? 2000 : scrollHeight.call(this);
      },
    );
    let frame: FrameRequestCallback;
    vi.stubGlobal("requestAnimationFrame", (callback: FrameRequestCallback) => {
      frame = callback;
      return 1;
    });
    vi.stubGlobal("cancelAnimationFrame", vi.fn());
    try {
      const article = {
        ...loadReleaseNotes(undefined, "en-US"),
        content:
          "# Release notes\n\n" +
          [3, 2, 1]
            .map(
              (version) =>
                `## v1.5.${version}\n\n### Improvements\n\nHistory ${version}.`,
            )
            .join("\n\n"),
      };
      renderIntl(<ReleaseNotesPage article={article} />, { locale: "en-US" });
      document.querySelector("article")!.style.overflowY = "visible";
      document.documentElement.scrollTop = 1100;
      fireEvent.scroll(document);
      act(() => frame(0));
      expect(
        document
          .querySelector('a[href="#release-v1-5-1-section-1"]')
          ?.getAttribute("aria-current"),
      ).toBe("location");
      fireEvent.change(screen.getByRole("searchbox"), {
        target: { value: "History 2" },
      });
      expect(document.documentElement.scrollTop).toBe(0);
      fireEvent.change(screen.getByRole("searchbox"), {
        target: { value: "" },
      });
      expect(document.documentElement.scrollTop).toBe(1100);
    } finally {
      document.documentElement.scrollTop = 0;
      if (descriptor)
        Object.defineProperty(document, "scrollingElement", descriptor);
      else
        delete (document as unknown as Record<string, unknown>)
          .scrollingElement;
    }
  });

  it.each(["en-US", "zh-CN", "de-DE", "fr-FR"] as const)(
    "shows calendar publication dates in %s",
    (locale) => {
      const article = {
        ...loadReleaseNotes(undefined, locale),
        content: "# Release notes\n\n## v2.0.0\n\nNotes.",
        releaseDates: { "v2.0.0": "2026-10-21" },
      };
      renderIntl(<ReleaseNotesPage article={article} />, { locale });
      const dates = document.querySelectorAll("time");
      expect(dates).toHaveLength(2);
      expect(dates[0].getAttribute("dateTime")).toBe("2026-10-21");
      expect(dates[0].textContent).toBe(dates[1].textContent);
      expect(dates[0].textContent).not.toContain("Invalid");
    },
  );

  it("searches body text and links to matching sections with unique anchors", () => {
    const article = {
      ...loadReleaseNotes(undefined, "en-US"),
      content:
        "# Release notes\n\n## v2.0.0\n\n### Reports\n\nRecover revoked credentials.\n\n## v1.0.0\n\n### Reports\n\nExport charts.",
    };
    renderIntl(<ReleaseNotesPage article={article} />, { locale: "en-US" });
    const search = screen.getByRole("searchbox", {
      name: "Search release notes",
    });
    fireEvent.change(search, { target: { value: " REVOKED " } });
    const result = screen.getByRole("link", { name: /Reports/ });
    expect(result.textContent).toContain("Recover revoked credentials.");
    expect(result.getAttribute("href")).toBe("#release-v2-0-0-section-1");
    expect(
      document.getElementById(result.getAttribute("href")!.slice(1))
        ?.textContent,
    ).toBe("Reports");
    expect(screen.queryByRole("link", { name: "v1.0.0" })).toBeNull();
    fireEvent.change(search, { target: { value: "v1.0.0" } });
    expect(
      screen.getByRole("link", { name: "Reports" }).getAttribute("href"),
    ).toBe("#release-v1-0-0-section-1");
    fireEvent.change(search, { target: { value: "missing phrase" } });
    expect(screen.getByRole("status").textContent).toContain("No results");
    fireEvent.change(search, { target: { value: "" } });
    expect(screen.getAllByRole("link", { name: "Reports" })).toHaveLength(2);
  });

  it.each(["", "/bowhead", "/nested/bowhead"])(
    "preserves %s for internal links and native new tabs",
    (prefix) => {
      window.__BOWHEAD_BASE_PATH__ = prefix;
      const article = {
        ...loadReleaseNotes(undefined, "en-US"),
        content: "# Release notes\n\n[Help](/help/)",
      };
      renderIntl(<ReleaseNotesPage article={article} />, { locale: "en-US" });
      const link = screen.getByRole("link", { name: "Help" });
      expect(link.getAttribute("href")).toBe(`${prefix}/help/`);
      fireEvent.click(link, { ctrlKey: true });
      expect(push).not.toHaveBeenCalled();
      fireEvent.click(link);
      expect(push).toHaveBeenCalledWith("/help/");
    },
  );
});
