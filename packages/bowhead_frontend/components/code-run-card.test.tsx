import "@testing-library/jest-dom/vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  focusArtifact: vi.fn(),
  toast: vi.fn(),
}));

vi.mock("next-intl", () => ({
  useTranslations: () => (key: string, values?: Record<string, unknown>) => ({
    last_run: "Last run",
    code_copy: "Copy code",
    code_copied: "Copied",
    code_copy_failed: "Copy failed",
    code_open_output: "Open output",
    code_outputs: "Persisted outputs",
    code_output_n: `Output ${values?.index ?? ""}`,
    code_run_details: "Run details",
    code_detail_status: "Status",
    code_detail_language: "Language",
    code_detail_actor: "Actor",
    code_detail_duration: "Duration",
    code_actor_lead: "Lead",
    code_duration_ms: `${values?.duration ?? ""} ms`,
    python_sandbox_output: "Output",
  }[key] ?? key),
  useLocale: () => "en-US",
}));
vi.mock("@/lib/canvas-workspace-focus", async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  requestCanvasDataFocus: mocks.focusArtifact,
}));
vi.mock("@/lib/use-toast", () => ({ toast: mocks.toast }));

import { CodeRunCard } from "./chat-workbench";

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("CodeRunCard", () => {
  it("copies only this run's source and opens its exact persisted output", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText },
    });
    render(
      <CodeRunCard
        defaultOpen={false}
        run={{
          key: "run_1",
          language: "python",
          label: "Analysis",
          actor: "lead",
          status: "success",
          code: "print('exact code')",
          codeTruncated: false,
          stdout: "not copied",
          durationMs: 80,
          outputs: [{
            artifactId: "art_exact_output",
            filename: "result.csv",
            mime: "text/csv",
          }],
        }}
      />,
    );

    const run = screen.getByRole("article");
    expect(run).toHaveAttribute("data-results-code-run-key", "run_1");
    expect(screen.getByRole("button", { name: "Open output" }))
      .toHaveAttribute("data-results-focus-token", "art_exact_output");

    fireEvent.click(screen.getByRole("button", { name: "Copy code" }));
    await waitFor(() => expect(writeText).toHaveBeenCalledWith("print('exact code')"));
    expect(writeText).not.toHaveBeenCalledWith(expect.stringContaining("not copied"));

    fireEvent.click(screen.getByRole("button", { name: "Open output" }));
    expect(mocks.focusArtifact).toHaveBeenCalledWith("art_exact_output", { entryPoint: "code" });
    expect(screen.queryByText(/reference/i)).not.toBeInTheDocument();
  });
});
