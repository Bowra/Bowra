import "@testing-library/jest-dom/vitest";
import { act, cleanup, fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const api = vi.hoisted(() => ({
  settings: {} as Record<string, string>,
  failWrite: false,
  writes: [] as Array<[string, string | null]>,
}));
vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  getUserSettings: vi.fn(async () => ({ ...api.settings })),
  setUserSetting: vi.fn(async (key: string, value: string) => {
    if (api.failWrite) throw new Error("503");
    api.writes.push([key, value]);
    api.settings[key] = value;
  }),
  deleteUserSetting: vi.fn(async (key: string) => {
    if (api.failWrite) throw new Error("503");
    api.writes.push([key, null]);
    delete api.settings[key];
  }),
}));
const toasts = vi.hoisted(() => [] as Array<{ kind: string; text: unknown }>);
vi.mock("@/lib/use-toast", async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  toast: (input: { kind: string; text: unknown }) => {
    toasts.push(input);
    return "t1";
  },
}));

import { renderIntl } from "@/__tests__/intl-render";
import { resetRunningViewStyleForTests } from "@/lib/running-view-style";
import { RunningViewStyleSetting } from "@/components/settings/running-view-style-setting";

import { buildRunGroups } from "./run-groups";
import { RunStyleSwitcher } from "./RunStyleSwitcher";
import { RunStyleView } from "./RunStyles";
import type { StepState, StepView } from "./types";

function sv(partial: Partial<StepView> & { stepId: string }): StepView {
  return { parentStepId: null, concurrencyGroupId: null, stepType: "tool_run", toolName: "bi_query", headerZh: "查询数据", state: "done" as StepState, subagentId: "LEAD", ...partial };
}

const groups = buildRunGroups({
  steps: [
    sv({ stepId: "q1", label: "门店", summary: "2 查询 · 80 行", startedAtMs: 0, durationMs: 2_000 }),
    sv({ stepId: "q2", label: "品类", startedAtMs: 2_000, durationMs: 3_000 }),
    sv({ stepId: "c1", toolName: "python_sandbox", headerZh: "运行代码", label: "排名", state: "active", startedAtMs: 6_000 }),
  ],
  peerNames: {},
});
const base = {
  groups,
  activeHelpers: 0,
  stepElapsedMs: () => 12_000,
  formatTime: (ms: number) => `${Math.round(ms / 1000)} 秒`,
  owner: null,
  running: true,
  nowMs: 20_000,
  turnStartMs: 0,
};

beforeEach(() => {
  api.settings = {};
  api.failWrite = false;
  api.writes = [];
  toasts.length = 0;
  window.localStorage.clear();
  resetRunningViewStyleForTests();
});
afterEach(() => cleanup());

describe("the three other styles (chat running state R3.1, R3.4)", () => {
  it("chips merge finished steps of one kind and name each mark by its sentence", () => {
    renderIntl(<RunStyleView {...base} style="chips" />);
    const list = screen.getByRole("list", { name: "Bowhead" });
    expect(list).toBeInTheDocument();
    expect(screen.getByRole("img", { name: /查询数据 × 2/ })).toHaveTextContent("× 2");
    expect(screen.getByRole("img", { name: /运行代码 · 排名/ })).toHaveTextContent("已 12 秒");
  });

  it("lanes draw one dot per step and say what is running now", () => {
    const { container } = renderIntl(<RunStyleView {...base} style="lanes" />);
    expect(container.querySelectorAll(".bh-dot")).toHaveLength(3);
    expect(container.querySelector(".bh-style__now")).toHaveTextContent("正在运行代码");
  });

  it("the timeline places steps on the Turn's clock with a now marker while running", () => {
    const { container } = renderIntl(<RunStyleView {...base} style="timeline" />);
    const segments = [...container.querySelectorAll<HTMLElement>(".bh-track__slot")];
    expect(segments.map((segment) => segment.style.left)).toEqual(["0%", "10%", "30%"]);
    expect(container.querySelector(".bh-track__now")).not.toBeNull();
  });

  it("names the longest step in the expanded finished view, and marks open their step", () => {
    const finished = buildRunGroups({ steps: [sv({ stepId: "a", startedAtMs: 0, durationMs: 40_000 }), sv({ stepId: "b", label: "x", startedAtMs: 40_000, durationMs: 1_000 })], peerNames: {} });
    const onSelectStep = vi.fn();
    renderIntl(<RunStyleView {...base} groups={finished} running={false} style="timeline" expanded onSelectStep={onSelectStep} />);
    expect(screen.getByTestId("timeline-longest")).toHaveTextContent("最长的一段：查询数据（40 秒）");
    fireEvent.click(screen.getAllByRole("button", { name: /查询数据/ })[0]);
    expect(onSelectStep).toHaveBeenCalledWith(expect.objectContaining({ stepId: "a" }));
  });
});

describe("the style setting (chat running state R3.2)", () => {
  it("the account value replaces the browser copy, and the card's switcher saves a pick", async () => {
    window.localStorage.setItem("bowhead.running_view_style", "chips");
    api.settings = { running_view_style: "lanes" };
    renderIntl(<RunStyleSwitcher />);
    expect(screen.getByRole("radio", { name: "胶囊" })).toHaveAttribute("aria-checked", "true");
    await waitFor(() => expect(screen.getByRole("radio", { name: "泳道" })).toHaveAttribute("aria-checked", "true"));
    await act(async () => {
      fireEvent.click(screen.getByRole("radio", { name: "时间轴" }));
    });
    expect(api.writes).toEqual([["running_view_style", "timeline"]]);
    expect(screen.getByRole("radio", { name: "时间轴" })).toHaveAttribute("aria-checked", "true");
  });

  it("moves with arrow keys, and picking the list resets the setting", async () => {
    api.settings = { running_view_style: "chips" };
    renderIntl(<RunStyleSwitcher />);
    await waitFor(() => expect(screen.getByRole("radio", { name: "胶囊" })).toHaveAttribute("tabindex", "0"));
    await act(async () => {
      fireEvent.keyDown(screen.getByRole("radio", { name: "胶囊" }), { key: "ArrowLeft" });
    });
    expect(api.writes).toEqual([["running_view_style", null]]);
    expect(screen.getByRole("radio", { name: "列表" })).toHaveAttribute("aria-checked", "true");
  });

  it("a failed save switches back and says why", async () => {
    api.failWrite = true;
    renderIntl(<RunStyleSwitcher />);
    await act(async () => {
      fireEvent.click(screen.getByRole("radio", { name: "泳道" }));
    });
    expect(screen.getByRole("radio", { name: "列表" })).toHaveAttribute("aria-checked", "true");
    expect(toasts).toHaveLength(1);
    expect(String(toasts[0].text)).toContain("泳道");
  });

  it("the Appearance row shows and saves the same setting", async () => {
    api.settings = { running_view_style: "timeline" };
    renderIntl(<RunningViewStyleSetting />);
    await waitFor(() => expect(screen.getByRole("radio", { name: /时间轴/ })).toHaveAttribute("aria-checked", "true"));
    await act(async () => {
      fireEvent.click(screen.getByRole("radio", { name: /胶囊/ }));
    });
    expect(api.writes).toEqual([["running_view_style", "chips"]]);
    api.failWrite = true;
    await act(async () => {
      fireEvent.click(screen.getByRole("radio", { name: /泳道/ }));
    });
    expect(screen.getByRole("radio", { name: /胶囊/ })).toHaveAttribute("aria-checked", "true");
    expect(screen.getByTestId("personal-appearance-running-view")).toHaveTextContent("没能保存");
  });
});
