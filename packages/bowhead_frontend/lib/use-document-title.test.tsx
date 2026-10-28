/**
 * Tab titles: every authenticated page registers its own title as the default (AppShell), and a more specific
 * title registered by the page content (the open report) still wins.
 */
import { cleanup, render } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { composeDocumentTitle } from "./brand-metadata";
import { getPageTitleSnapshot } from "./page-title-store";
import { useDocumentTitle, usePageDefaultDocumentTitle } from "./use-document-title";

function Page({ title, content }: { title: string | null; content?: string | null }) {
  usePageDefaultDocumentTitle(title, "app-shell-page");
  return content !== undefined ? <Content title={content} /> : null;
}

function Content({ title }: { title: string | null }) {
  useDocumentTitle(title, "content");
  return null;
}

function Harness({ title }: { title: string | null }) {
  useDocumentTitle(title, "test-page");
  return null;
}

afterEach(() => {
  cleanup();
  document.title = "";
});

describe("useDocumentTitle", () => {
  it("registers raw page state without writing document metadata", () => {
    document.title = "sentinel";
    const view = render(<Harness title=" Q3 营收 " />);

    expect(getPageTitleSnapshot()).toBe("Q3 营收");
    expect(document.title).toBe("sentinel");

    view.rerender(<Harness title="Q4 营收" />);
    expect(getPageTitleSnapshot()).toBe("Q4 营收");
    expect(document.title).toBe("sentinel");

    view.unmount();
    expect(getPageTitleSnapshot()).toBeNull();
  });
});

describe("usePageDefaultDocumentTitle", () => {
  it("registers the page title for the tab", () => {
    render(<Page title="My reports" />);
    expect(getPageTitleSnapshot()).toBe("My reports");
    expect(composeDocumentTitle({ applicationName: "Bowhead", pageTitle: getPageTitleSnapshot() })).toBe("My reports · Bowhead");
  });

  it("lets a more specific content title win over the page default", () => {
    render(<Page title="New Work" content="7月营业额环比 -21.94% 归因拆解" />);
    expect(getPageTitleSnapshot()).toBe("7月营业额环比 -21.94% 归因拆解");
  });

  it("falls back to the page default when the content has no title", () => {
    render(<Page title="New Work" content={null} />);
    expect(getPageTitleSnapshot()).toBe("New Work");
  });

  it("clears its registration on unmount", () => {
    const { unmount } = render(<Page title="Files" />);
    unmount();
    expect(getPageTitleSnapshot()).toBeNull();
  });
});
