/**
 * stream-liveness ③ — do not invent progress when there is no signal.
 *
 * Problem: `preparing` rendered「正在想下一步」on the very path where **not a single event has arrived**.
 * The same file's comments said not to claim thinking about the next step (Lead is not thinking):
 * **the team made known progress honest, yet still invented progress when nothing was known**.
 * A synthetic turn with zero SSE events and no response headers must not claim that the agent is
 * "Working out the next step".
 *
 * ★ This branch **does not attribute a cause** (it only says how long nothing has arrived), so false attribution is impossible;
 *   administrator diagnostics do attribute causes and require many guards.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render } from "@testing-library/react";

import { RunningBand } from "./RunningBand";
import { createTransportSample, noteEvent, NO_SIGNAL_MS } from "@/lib/transport-health";
import type { StepView } from "./types";

vi.mock("next-intl", () => ({
  useTranslations: () => (key: string, vals?: Record<string, unknown>) =>
    vals ? `${key}:${Object.values(vals).join(",")}` : key,
}));

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

function actionText(container: HTMLElement): string {
  return container.querySelector(".bh-band__act")?.textContent ?? "";
}

/** The last bytes arrived `agoMs` ago. */
function sampleSilentFor(agoMs: number) {
  const s = createTransportSample();
  noteEvent(s, Date.now() - agoMs);
  return s;
}

const steps: StepView[] = [];

describe("没信号时说实话", () => {
  it("超过阈值 ⇒ 报「已 N 秒未收到进度信号」，不再渲「正在想下一步」", () => {
    const { container } = render(
      <RunningBand steps={steps} running startedAt={Date.now() - 60_000}
        transport={sampleSilentFor(NO_SIGNAL_MS + 5_000)} />,
    );
    expect(actionText(container)).toContain("band_no_signal");
    expect(actionText(container)).not.toContain("band_thinking");
  });

  it("★正对照：刚收到字节 ⇒ 绝不许说没信号（保持原文案）", () => {
    // Without this, an always-true condition also passes the preceding test — a vacuous gate.
    // This sprint family has already hit vacuous gates three times; this test specifically guards against them.
    const { container } = render(
      <RunningBand steps={steps} running startedAt={Date.now() - 60_000}
        transport={sampleSilentFor(1_000)} />,
    );
    expect(actionText(container)).not.toContain("band_no_signal");
    expect(actionText(container)).toContain("band_thinking");
  });

  it("★正对照：阈值边界内（差 1 秒）仍不许说没信号", () => {
    const { container } = render(
      <RunningBand steps={steps} running startedAt={Date.now() - 60_000}
        transport={sampleSilentFor(NO_SIGNAL_MS - 1_000)} />,
    );
    expect(actionText(container)).not.toContain("band_no_signal");
  });

  it("★正对照：不传 transport ⇒ 行为与从前逐字一致", () => {
    const { container } = render(
      <RunningBand steps={steps} running startedAt={Date.now() - 60_000} />,
    );
    expect(actionText(container)).not.toContain("band_no_signal");
    expect(actionText(container)).toContain("band_thinking");
  });

  it("turn 已结束（running=false）⇒ 不说没信号（那是完结不是失联）", () => {
    const { container } = render(
      <RunningBand steps={steps} running={false} startedAt={Date.now() - 60_000}
        transport={sampleSilentFor(NO_SIGNAL_MS + 60_000)} />,
    );
    expect(actionText(container)).not.toContain("band_no_signal");
  });

  it("秒数是真实静默时长，不是写死的", () => {
    const { container } = render(
      <RunningBand steps={steps} running startedAt={Date.now() - 120_000}
        transport={sampleSilentFor(95_000)} />,
    );
    // The mock appends parameters to the key: band_no_signal:95.
    expect(actionText(container)).toMatch(/band_no_signal:9[45]/);
  });
});
