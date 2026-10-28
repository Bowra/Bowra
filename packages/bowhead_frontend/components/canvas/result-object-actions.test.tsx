import "@testing-library/jest-dom/vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { ResultCapability } from "@/lib/results-workspace-model";
import { ResultObjectActions } from "./result-object-actions";

vi.mock("next-intl", () => ({
  useTranslations: () => (key: string) => ({
    download: "Download",
    reference_in_chat: "Reference in chat",
    manage_in_files: "Manage in Files",
    view_raw: "View Raw Content",
    view_details: "View Details",
    version_history: "Version History",
    feedback_unsupported: "Request This Format",
    feedback_recorded_reason: "Format request recorded",
    more_actions: "More actions",
  }[key] ?? key),
}));

function capabilities(...values: ResultCapability[]): ReadonlySet<ResultCapability> {
  return new Set(values);
}

afterEach(cleanup);

describe("ResultObjectActions", () => {
  it("uses the exact artifact ID for Files and keeps data download secondary", () => {
    render(
      <ResultObjectActions
        artifactId="art/precise id"
        capabilities={capabilities(
          "download",
          "reference_in_chat",
          "manage_in_files",
          "view_raw",
          "view_details",
        )}
        role="data"
        mode="preview"
        downloadHref="/api/v1/files/art_precise"
        onReference={() => {}}
        onModeChange={() => {}}
      />,
    );

    expect(screen.getByRole("button", { name: "Reference in chat" })).toBeInTheDocument();
    fireEvent.click(screen.getByText("More actions"));
    expect(screen.getByRole("menuitem", { name: "Download" })).toHaveAttribute(
      "href",
      "/api/v1/files/art_precise",
    );
    expect(screen.getByRole("menuitem", { name: "Manage in Files" })).toHaveAttribute(
      "href",
      "/files/?focus=art%2Fprecise%20id",
    );
  });

  it("shows unsupported feedback without exposing a reference action", () => {
    const onFeedback = vi.fn();
    const { rerender } = render(
      <ResultObjectActions
        artifactId="art_secret"
        capabilities={capabilities(
          "download",
          "manage_in_files",
          "view_details",
          "feedback_unsupported",
        )}
        role="unsupported"
        mode="details"
        downloadHref="/api/v1/files/art_secret"
        onFeedbackUnsupported={onFeedback}
      />,
    );

    expect(screen.queryByRole("button", { name: "Reference in chat" })).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Download" })).toBeInTheDocument();
    fireEvent.click(screen.getByText("More actions"));
    fireEvent.click(screen.getByRole("menuitem", { name: "Request This Format" }));
    expect(onFeedback).toHaveBeenCalledTimes(1);

    rerender(
      <ResultObjectActions
        artifactId="art_secret"
        capabilities={capabilities("download", "feedback_unsupported")}
        role="unsupported"
        mode="details"
        downloadHref="/api/v1/files/art_secret"
        feedbackRecorded
        onFeedbackUnsupported={onFeedback}
      />,
    );
    fireEvent.click(screen.getByText("More actions"));
    // A recorded request keeps the action name, disabled with the reason on a second line.
    const recorded = screen.getByRole("menuitem", { name: "Request This Format" });
    expect(recorded).toHaveAttribute("aria-disabled", "true");
    expect(recorded).toHaveAccessibleDescription("Format request recorded");
  });

  describe("more-actions menu behaviour", () => {
    function renderMenu() {
      return render(
        <>
          <ResultObjectActions
            artifactId="art_1"
            capabilities={capabilities("download", "reference_in_chat", "view_raw", "view_details")}
            role="data"
            mode="preview"
            downloadHref="/api/v1/files/art_1"
            onReference={() => {}}
            onModeChange={() => {}}
          />
          <p>Outside</p>
        </>,
      );
    }

    it("opens into the first action on the keyboard, moves with arrows and returns focus on Escape", async () => {
      renderMenu();
      const trigger = screen.getByRole("button", { name: "More actions" });
      fireEvent.click(trigger, { detail: 0 });
      await waitFor(() => expect(document.activeElement).toBe(screen.getByRole("menuitem", { name: "Download" })));
      fireEvent.keyDown(screen.getByRole("menu"), { key: "ArrowDown" });
      expect(document.activeElement).toBe(screen.getByRole("menuitem", { name: "View Raw Content" }));

      fireEvent.keyDown(document.activeElement as Element, { key: "Escape" });

      await waitFor(() => expect(screen.queryByRole("menu")).toBeNull());
      await waitFor(() => expect(document.activeElement).toBe(trigger));
    });

    it("closes on a press outside", async () => {
      renderMenu();
      fireEvent.click(screen.getByRole("button", { name: "More actions" }));
      expect(screen.getByRole("menu")).toBeInTheDocument();

      fireEvent.pointerDown(screen.getByText("Outside"));

      await waitFor(() => expect(screen.queryByRole("menu")).toBeNull());
    });
  });
});
