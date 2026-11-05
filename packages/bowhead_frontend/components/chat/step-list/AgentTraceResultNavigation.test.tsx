import "@testing-library/jest-dom/vitest";

import { cleanup, fireEvent, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";
import { AgentTrace } from "./AgentTrace";

const mocks = vi.hoisted(() => ({
  focus: vi.fn(),
}));

vi.mock("@/lib/canvas-workspace-focus", () => ({
  requestCanvasDataFocus: mocks.focus,
}));

afterEach(() => {
  cleanup();
  mocks.focus.mockReset();
});

describe("AgentTrace Results navigation", () => {
  it.each(["done", "failed"] as const)("downloads retained diagnostics and opens delivered Markdown from a %s step", (state) => {
    renderIntl(
      <AgentTrace
        steps={[
          {
            stepId: "step_python",
            parentStepId: null,
            concurrencyGroupId: null,
            stepType: "tool_run",
            toolName: "python_sandbox",
            headerZh: "运行代码",
            state,
            detail: { kind: "stdout", text: "19×29=551" },
            outputArtifacts: [
              {
                artifactId: "art_stdout",
                filename: "sandbox-stdout.txt",
                dataRole: "stdout",
              },
              { artifactId: "art_calc", filename: "calc.md", title: "Calculation", description: "Retained original" },
              { artifactId: "art_without_filename" },
            ],
          },
        ]}
        peerNames={{}}
        peerProse={{}}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: /运行代码/ }));
    const log = screen.getByRole("link", { name: "下载 sandbox-stdout.txt" });
    expect(log).toHaveAttribute("href", "/api/v1/files/art_stdout");
    expect(log).toHaveAttribute("download", "sandbox-stdout.txt");
    expect(mocks.focus).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", {
      name: "在成果中打开 Calculation · calc.md",
    }));
    expect(screen.getByTitle("Retained original")).toBeInTheDocument();
    expect(mocks.focus).toHaveBeenLastCalledWith("art_calc", {
      entryPoint: "chat",
    });

    fireEvent.click(screen.getByRole("button", {
      name: "在成果中打开 分析依据 3",
    }));
    expect(mocks.focus).toHaveBeenLastCalledWith("art_without_filename", {
      entryPoint: "chat",
    });
    expect(screen.queryByText("art_without_filename")).not.toBeInTheDocument();
  });
});
