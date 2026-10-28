import { act, cleanup, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { NextIntlClientProvider } from "next-intl";

import { renderIntl } from "@/__tests__/intl-render";
import type { EffectDisclosureOverlayDTO } from "@/lib/api";
import { fetchEffectDisclosure } from "@/lib/api";
import type { TurnRunnerState } from "@/lib/types";
import { useTurnRunner } from "@/lib/use-turn-runner";
import zhMessages from "@/messages/zh-CN.json";

import { EffectDisclosureNotice, TurnContainer } from "./TurnContainer";

vi.mock("@/lib/api", async (importOriginal) => ({
  ...await importOriginal<typeof import("@/lib/api")>(),
  fetchEffectDisclosure: vi.fn(),
}));

const ORIGINAL_FETCH = globalThis.fetch;
const ORIGINAL_ES = globalThis.EventSource;

class FakeEventSource {
  static instances: FakeEventSource[] = [];
  static readonly CONNECTING = 0;
  static readonly OPEN = 1;
  static readonly CLOSED = 2;
  readyState = FakeEventSource.OPEN;
  onerror: (() => void) | null = null;
  private listeners = new Map<string, Array<(event: MessageEvent) => void>>();

  constructor(public readonly url: string) {
    FakeEventSource.instances.push(this);
  }

  addEventListener(name: string, listener: (event: MessageEvent) => void) {
    const current = this.listeners.get(name) ?? [];
    current.push(listener);
    this.listeners.set(name, current);
  }

  removeEventListener() {}

  close() {
    this.readyState = FakeEventSource.CLOSED;
  }

  emit(name: string, payload: Record<string, unknown>) {
    for (const listener of this.listeners.get(name) ?? []) {
      listener({ data: JSON.stringify(payload) } as MessageEvent);
    }
  }
}

const UNKNOWN: EffectDisclosureOverlayDTO = {
  protocol: 2,
  receipt_id: "tr_1",
  attempt_count: 1,
  unknown_count: 1,
  counts: {
    accounting: { applied: 0, compensated: 0, not_applied: 0, unsettled: 0 },
    inference_internal: { applied: 0, compensated: 0, not_applied: 0, unsettled: 0 },
    user_action: { applied: 0, compensated: 0, not_applied: 0, unsettled: 1 },
  },
  proof_digest: "proof_unknown",
  oldest_unknown_attempt_started_at: "2026-08-14T00:00:00Z",
  oldest_unknown_age_upper_bound_seconds: 1,
  warning_message_keys: ["turn.effect_user_action_unsettled"],
  finalized: false,
};

const SETTLED: EffectDisclosureOverlayDTO = {
  ...UNKNOWN,
  unknown_count: 0,
  counts: {
    ...UNKNOWN.counts,
    user_action: { applied: 1, compensated: 0, not_applied: 0, unsettled: 0 },
  },
  proof_digest: "proof_settled",
  oldest_unknown_attempt_started_at: null,
  oldest_unknown_age_upper_bound_seconds: null,
  warning_message_keys: [],
  finalized: true,
};

const ZERO: EffectDisclosureOverlayDTO = {
  ...SETTLED,
  receipt_id: null,
  attempt_count: 0,
  counts: {
    accounting: { applied: 0, compensated: 0, not_applied: 0, unsettled: 0 },
    inference_internal: { applied: 0, compensated: 0, not_applied: 0, unsettled: 0 },
    user_action: { applied: 0, compensated: 0, not_applied: 0, unsettled: 0 },
  },
  proof_digest: "proof_zero",
  finalized: false,
};

const INFERENCE_UNKNOWN: EffectDisclosureOverlayDTO = {
  ...UNKNOWN,
  counts: {
    ...UNKNOWN.counts,
    inference_internal: {
      applied: 0,
      compensated: 0,
      not_applied: 0,
      unsettled: 1,
    },
    user_action: { applied: 0, compensated: 0, not_applied: 0, unsettled: 0 },
  },
};

const ACCOUNTING_SETTLED: EffectDisclosureOverlayDTO = {
  ...SETTLED,
  counts: {
    accounting: { applied: 1, compensated: 0, not_applied: 0, unsettled: 0 },
    inference_internal: { applied: 0, compensated: 0, not_applied: 0, unsettled: 0 },
    user_action: { applied: 0, compensated: 0, not_applied: 0, unsettled: 0 },
  },
};

const read = (
  overlay: EffectDisclosureOverlayDTO | null,
  currentGeneration = 1,
) => ({ currentGeneration, overlay });

function runningRunner(
  effectDisclosure: EffectDisclosureOverlayDTO | null,
): TurnRunnerState {
  return {
    user: "触发外部操作",
    intent: null,
    complexity: null,
    chips: [],
    answer: "",
    artifact: null,
    source: null,
    summary: null,
    traceSteps: [],
    turnId: "t_recovery_pending",
    executionGeneration: 1,
    terminalProtocolVersion: 1,
    sessionId: "s_1",
    sessionTitle: null,
    running: true,
    terminalStatus: null,
    effectDisclosure,
    started: true,
    messages: [],
    userAttachments: [],
    thinking: "",
    thinkingLength: 0,
    lastThinkingHintAttachedAt: "",
    clarifications: [],
    createToolOpen: null,
    subagentCreatorOpen: null,
    solidifyOpen: null,
    plan: null,
  };
}

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.mocked(fetchEffectDisclosure).mockReset();
  globalThis.fetch = ORIGINAL_FETCH;
  (globalThis as Record<string, unknown>).EventSource = ORIGINAL_ES;
  FakeEventSource.instances = [];
});

describe("EffectDisclosureNotice", () => {
  it("shows hydrated unsettled proof while recovery is still running", () => {
    vi.mocked(fetchEffectDisclosure).mockResolvedValue(read(UNKNOWN));
    renderIntl(<TurnContainer runner={runningRunner(UNKNOWN)} />);

    expect(screen.getByTestId("effect-disclosure-notice").textContent)
      .toContain("结果待确认");
  });

  it("keeps live UNKNOWN visible, then bounded-polls to settled", async () => {
    vi.useFakeTimers();
    vi.mocked(fetchEffectDisclosure)
      .mockResolvedValueOnce(read(UNKNOWN))
      .mockResolvedValueOnce(read(SETTLED));

    renderIntl(
      <EffectDisclosureNotice
        turnId="t_1"
        executionGeneration={1}
        initial={UNKNOWN}
      />,
    );
    expect(screen.getByTestId("effect-disclosure-notice").textContent)
      .toContain("结果待确认");

    await act(async () => { await Promise.resolve(); });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5_000);
    });

    expect(screen.getByTestId("effect-disclosure-notice").textContent)
      .toContain("结果已确认");
    expect(fetchEffectDisclosure).toHaveBeenCalledTimes(2);
  });

  it("polls a hydrated DISPATCHED disclosure until recovery settles it", async () => {
    vi.useFakeTimers();
    const dispatched = { ...UNKNOWN, unknown_count: 0 };
    vi.mocked(fetchEffectDisclosure)
      .mockResolvedValueOnce(read(dispatched))
      .mockResolvedValueOnce(read(SETTLED));

    renderIntl(
      <EffectDisclosureNotice
        turnId="t_dispatched"
        executionGeneration={1}
        initial={dispatched}
      />,
    );
    await act(async () => { await Promise.resolve(); });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5_000);
    });

    expect(screen.getByTestId("effect-disclosure-notice").textContent)
      .toContain("结果已确认");
    expect(fetchEffectDisclosure).toHaveBeenCalledTimes(2);
  });

  it("renders an F5-settled overlay without polling", () => {
    renderIntl(
      <EffectDisclosureNotice
        turnId="t_1"
        executionGeneration={1}
        initial={SETTLED}
      />,
    );

    expect(screen.getByTestId("effect-disclosure-notice").textContent)
      .toContain("结果已确认");
    expect(fetchEffectDisclosure).not.toHaveBeenCalled();
  });

  it("does not probe archived turns whose hydrated overlay is null", () => {
    renderIntl(
      <>
        {Array.from({ length: 20 }, (_, index) => (
          <EffectDisclosureNotice
            key={index}
            turnId={`t_${index}`}
            executionGeneration={1}
            initial={null}
          />
        ))}
      </>,
    );

    expect(fetchEffectDisclosure).not.toHaveBeenCalled();
  });

  it("discovers UNKNOWN after the former ten-minute client cutoff", async () => {
    vi.useFakeTimers();
    let reads = 0;
    vi.mocked(fetchEffectDisclosure).mockImplementation(async () => {
      reads += 1;
      return read(reads > 121 ? UNKNOWN : ZERO);
    });

    renderIntl(<TurnContainer runner={runningRunner(ZERO)} />);
    await act(async () => { await Promise.resolve(); });

    expect(fetchEffectDisclosure).toHaveBeenCalledTimes(1);
    expect(screen.queryByTestId("effect-disclosure-notice")).toBeNull();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(605_000);
    });

    expect(fetchEffectDisclosure).toHaveBeenCalledTimes(122);
    expect(screen.getByTestId("effect-disclosure-notice").textContent)
      .toContain("结果待确认");
  });

  it("keeps a polled UNKNOWN across running-to-terminal handoff", async () => {
    vi.useFakeTimers();
    vi.mocked(fetchEffectDisclosure)
      .mockResolvedValueOnce(read(UNKNOWN))
      .mockResolvedValueOnce(read(SETTLED));
    const view = renderIntl(
      <TurnContainer runner={runningRunner(null)} />,
    );
    await act(async () => { await Promise.resolve(); });
    expect(screen.getByTestId("effect-disclosure-notice").textContent)
      .toContain("结果待确认");

    view.rerender(
      <NextIntlClientProvider locale="zh-CN" messages={zhMessages}>
        <TurnContainer
          runner={{
            ...runningRunner(null),
            running: false,
            terminalStatus: "cancelled",
          }}
        />
      </NextIntlClientProvider>,
    );
    expect(screen.getByTestId("effect-disclosure-notice").textContent)
      .toContain("结果待确认");

    await act(async () => {
      await vi.advanceTimersByTimeAsync(5_000);
    });
    expect(screen.getByTestId("effect-disclosure-notice").textContent)
      .toContain("结果已确认");
  });

  it("reads immediately after terminal even when the running read saw ZERO", async () => {
    vi.useFakeTimers();
    vi.mocked(fetchEffectDisclosure)
      .mockResolvedValueOnce(read(ZERO))
      .mockResolvedValueOnce(read(UNKNOWN));
    const view = renderIntl(
      <TurnContainer runner={runningRunner(ZERO)} />,
    );
    await act(async () => { await Promise.resolve(); });
    expect(fetchEffectDisclosure).toHaveBeenCalledTimes(1);
    expect(screen.queryByTestId("effect-disclosure-notice")).toBeNull();

    view.rerender(
      <NextIntlClientProvider locale="zh-CN" messages={zhMessages}>
        <TurnContainer
          runner={{
            ...runningRunner(ZERO),
            running: false,
            terminalStatus: "cancelled",
            effectDisclosureDiscoveryPending: true,
          }}
        />
      </NextIntlClientProvider>,
    );
    await act(async () => { await Promise.resolve(); });

    expect(fetchEffectDisclosure).toHaveBeenCalledTimes(2);
    expect(screen.getByTestId("effect-disclosure-notice").textContent)
      .toContain("结果待确认");
  });

  it("starts polling when progressive hydrate supplies UNKNOWN", async () => {
    vi.useFakeTimers();
    vi.mocked(fetchEffectDisclosure)
      .mockResolvedValueOnce(read(UNKNOWN))
      .mockResolvedValueOnce(read(SETTLED));
    const view = renderIntl(
      <EffectDisclosureNotice
        turnId="t_progressive"
        executionGeneration={1}
        initial={null}
      />,
    );
    expect(fetchEffectDisclosure).not.toHaveBeenCalled();

    view.rerender(
      <NextIntlClientProvider locale="zh-CN" messages={zhMessages}>
        <EffectDisclosureNotice
          turnId="t_progressive"
          executionGeneration={1}
          initial={UNKNOWN}
        />
      </NextIntlClientProvider>,
    );
    await act(async () => { await Promise.resolve(); });
    expect(fetchEffectDisclosure).toHaveBeenCalledTimes(1);
    expect(screen.getByTestId("effect-disclosure-notice").textContent)
      .toContain("结果待确认");

    await act(async () => {
      await vi.advanceTimersByTimeAsync(5_000);
    });
    expect(fetchEffectDisclosure).toHaveBeenCalledTimes(2);
    expect(screen.getByTestId("effect-disclosure-notice").textContent)
      .toContain("结果已确认");
  });

  it("bounds terminal UNKNOWN polling after the mandatory read succeeds", async () => {
    vi.useFakeTimers();
    vi.mocked(fetchEffectDisclosure).mockResolvedValue(read(UNKNOWN));
    renderIntl(
      <EffectDisclosureNotice
        turnId="t_terminal_budget"
        executionGeneration={1}
        initial={ZERO}
        discoverUntilRead
      />,
    );
    await act(async () => { await Promise.resolve(); });
    expect(fetchEffectDisclosure).toHaveBeenCalledTimes(1);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(60_000);
    });
    expect(fetchEffectDisclosure).toHaveBeenCalledTimes(6);
    expect(screen.getByTestId("effect-disclosure-notice").textContent)
      .toContain("结果待确认");
  });

  it("converges running ZERO to one archived UNKNOWN notice on v1 done", async () => {
    FakeEventSource.instances = [];
    (globalThis as Record<string, unknown>).EventSource = FakeEventSource;
    globalThis.fetch = vi.fn(async (url: RequestInfo | URL) => {
      if (String(url).includes("/chat/send")) {
        return new Response(JSON.stringify({
          turn_id: "t_effect_handoff",
          session_id: "s_effect_handoff",
          stream_url: "/api/v1/chat/stream/t_effect_handoff",
          execution_generation: 1,
          terminal_protocol_version: 1,
        }), {
          status: 200,
          headers: { "content-type": "application/json" },
        });
      }
      return new Response("{}", {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    }) as typeof fetch;
    vi.mocked(fetchEffectDisclosure)
      .mockResolvedValueOnce(read(ZERO))
      .mockResolvedValueOnce(read(UNKNOWN));
    let latestRunner = runningRunner(null);

    function Harness() {
      const runner = useTurnRunner({
        mode: "sse",
        message: "触发外部操作",
        workspaceId: "w_1",
        memberUid: "m_1",
        sendTick: 1,
      });
      latestRunner = runner;
      return <TurnContainer runner={runner} />;
    }

    renderIntl(<Harness />);
    await waitFor(() => expect(FakeEventSource.instances).toHaveLength(1));
    await waitFor(() => expect(fetchEffectDisclosure).toHaveBeenCalledTimes(1));
    const stream = FakeEventSource.instances[0];
    act(() => {
      stream.emit("stream.checkpoint", {
        terminal_protocol_version: 1,
        current_generation: 1,
        persistent_event_highwater: 1,
        current_receipt_id: "receipt_effect_handoff",
      });
      stream.emit("done", {
        terminal_protocol_version: 1,
        event_id: "event_effect_handoff_done",
        event_seq: 1,
        evt_seq: 1,
        generation: 1,
        terminal_current: true,
        terminal_receipt_id: "receipt_effect_handoff",
        status: "completed",
      });
    });

    await waitFor(() => expect(fetchEffectDisclosure).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(
      screen.getAllByTestId("effect-disclosure-notice"),
    ).toHaveLength(1));
    expect(screen.getByTestId("effect-disclosure-notice").textContent)
      .toContain("结果待确认");
    expect(latestRunner?.effectDisclosureDiscoveryPending).toBe(false);
    expect(
      latestRunner?.archivedTurns?.at(-1)
        ?.effectDisclosureDiscoveryPending,
    ).toBe(false);
  });

  it("drops an in-flight read after the execution generation changes", async () => {
    let resolveOld: ((value: ReturnType<typeof read>) => void) | undefined;
    let resolveCurrent: ((value: ReturnType<typeof read>) => void) | undefined;
    vi.mocked(fetchEffectDisclosure)
      .mockImplementationOnce(() => new Promise((resolve) => {
        resolveOld = resolve;
      }))
      .mockImplementationOnce(() => new Promise((resolve) => {
        resolveCurrent = resolve;
      }));
    const onUpdate = vi.fn();
    const view = renderIntl(
      <EffectDisclosureNotice
        turnId="t_generation"
        executionGeneration={1}
        initial={ZERO}
        discoverUntilRead
        onUpdate={onUpdate}
      />,
    );
    await act(async () => { await Promise.resolve(); });
    view.rerender(
      <NextIntlClientProvider locale="zh-CN" messages={zhMessages}>
        <EffectDisclosureNotice
          turnId="t_generation"
          executionGeneration={2}
          initial={null}
          discoverUntilRead
          onUpdate={onUpdate}
        />
      </NextIntlClientProvider>,
    );
    await act(async () => { await Promise.resolve(); });
    await act(async () => { resolveOld?.(read(UNKNOWN, 1)); });
    expect(onUpdate).not.toHaveBeenCalled();

    await act(async () => { resolveCurrent?.(read(UNKNOWN, 2)); });
    expect(onUpdate).toHaveBeenCalledTimes(1);
    expect(onUpdate).toHaveBeenCalledWith("t_generation", 2, UNKNOWN);
  });

  it.each([
    ["a new generation", "t_identity", 2],
    ["a new turn", "t_identity_next", 1],
  ])("never renders the prior UNKNOWN for %s", async (_, nextTurnId, nextGeneration) => {
    vi.mocked(fetchEffectDisclosure).mockImplementation(
      () => new Promise(() => {}),
    );
    const view = renderIntl(
      <EffectDisclosureNotice
        turnId="t_identity"
        executionGeneration={1}
        initial={UNKNOWN}
      />,
    );
    expect(screen.getByTestId("effect-disclosure-notice").textContent)
      .toContain("结果待确认");
    expect(fetchEffectDisclosure).toHaveBeenCalledTimes(1);

    view.rerender(
      <NextIntlClientProvider locale="zh-CN" messages={zhMessages}>
        <EffectDisclosureNotice
          turnId={nextTurnId}
          executionGeneration={nextGeneration}
          initial={null}
        />
      </NextIntlClientProvider>,
    );

    expect(screen.queryByTestId("effect-disclosure-notice")).toBeNull();
    await act(async () => { await Promise.resolve(); });
    expect(fetchEffectDisclosure).toHaveBeenCalledTimes(1);
  });

  it("uses model-invocation wording for inference effects", () => {
    renderIntl(
      <EffectDisclosureNotice
        turnId="t_1"
        executionGeneration={1}
        initial={INFERENCE_UNKNOWN}
      />,
    );

    const text = screen.getByTestId("effect-disclosure-notice").textContent;
    expect(text).toContain("模型调用");
    expect(text).not.toContain("外部操作");
  });

  it("does not render accounting-only effects in chat", () => {
    renderIntl(
      <EffectDisclosureNotice
        turnId="t_1"
        executionGeneration={1}
        initial={ACCOUNTING_SETTLED}
      />,
    );

    expect(screen.queryByTestId("effect-disclosure-notice")).toBeNull();
  });

  it("renders user-action and inference status independently", () => {
    renderIntl(
      <EffectDisclosureNotice
        turnId="t_1"
        executionGeneration={1}
        initial={{
          ...INFERENCE_UNKNOWN,
          attempt_count: 2,
          counts: {
            ...INFERENCE_UNKNOWN.counts,
            user_action: {
              applied: 1,
              compensated: 0,
              not_applied: 0,
              unsettled: 0,
            },
          },
        }}
      />,
    );

    const text = screen.getByTestId("effect-disclosure-notice").textContent;
    expect(text).toContain("外部操作的结果已确认");
    expect(text).toContain("模型调用的结果待确认");
  });
});
