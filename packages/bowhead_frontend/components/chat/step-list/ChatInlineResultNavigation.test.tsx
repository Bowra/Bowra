import "@testing-library/jest-dom/vitest";

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ChatInlineFile } from "./ChatInlineFile";
import { ChatInlineImage } from "./ChatInlineImage";

const mocks = vi.hoisted(() => ({
  capability: false,
  download: vi.fn(),
  focus: vi.fn(),
}));

vi.mock("next-intl", () => ({
  useTranslations: (namespace: string) =>
    (key: string, values?: Record<string, unknown>) =>
      `${namespace}:${key}${values?.filename ? `:${values.filename}` : ""}`,
}));
vi.mock("@/lib/canvas-workspace-focus", () => ({
  requestCanvasDataFocus: mocks.focus,
}));
vi.mock("@/lib/use-file-download", () => ({
  useFileDownload: () => ({
    status: "idle",
    pct: 0,
    download: mocks.download,
  }),
}));
vi.mock("@/lib/use-results-workspace-capability", () => ({
  useResultsWorkspaceCapabilitySnapshot: () => mocks.capability,
}));

afterEach(() => {
  cleanup();
  mocks.capability = false;
  mocks.download.mockReset();
  mocks.focus.mockReset();
});

const fileData = {
  id: "art_deck_exact",
  filename: "deck.pptx",
  fileType: "PPTX",
  downloadUrl: "/api/v1/files/art_deck_exact",
};

const imageData = {
  id: "art_image_exact",
  filename: "poster.png",
  url: "/api/v1/files/art_image_exact?disposition=inline",
  downloadUrl: "/api/v1/files/art_image_exact",
};

describe("chat result entry points", () => {
  it("keeps the legacy whole-file-card download behavior when disabled", () => {
    render(<ChatInlineFile data={fileData} />);

    fireEvent.click(screen.getByRole("button"));

    expect(mocks.download).toHaveBeenCalledTimes(1);
    expect(mocks.focus).not.toHaveBeenCalled();
  });

  it("separates opening the exact file result from downloading it", () => {
    mocks.capability = true;
    render(<ChatInlineFile data={fileData} />);

    fireEvent.click(screen.getByRole("button", {
      name: "results_workspace:open_object:deck.pptx",
    }));
    expect(mocks.focus).toHaveBeenCalledWith("art_deck_exact", {
      entryPoint: "chat",
    });
    expect(mocks.download).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", {
      name: "attachment:file_aria_download:deck.pptx",
    }));
    expect(mocks.download).toHaveBeenCalledTimes(1);
  });

  it("opens the exact image result while preserving a separate download link", () => {
    mocks.capability = true;
    render(<ChatInlineImage data={imageData} />);

    fireEvent.click(screen.getByRole("button", {
      name: "results_workspace:open_object:poster.png",
    }));

    expect(mocks.focus).toHaveBeenCalledWith("art_image_exact", {
      entryPoint: "chat",
    });
    expect(screen.getByRole("link", {
      name: "attachment:image_download:poster.png",
    })).toHaveAttribute("href", "/api/v1/files/art_image_exact");
  });

  it("does not add an image-open action when the workspace is disabled", () => {
    render(<ChatInlineImage data={imageData} />);

    expect(screen.queryByRole("button")).not.toBeInTheDocument();
    expect(mocks.focus).not.toHaveBeenCalled();
  });
});
