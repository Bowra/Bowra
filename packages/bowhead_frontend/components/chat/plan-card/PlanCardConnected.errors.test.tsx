/** Plan-card recovery failures: the Toast only explains what happened and what to do; status codes and raw API text go into Copy Diagnostics. */
import { cleanup, fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";
import { _resetAllPlansForTesting, applyPlanSnapshot } from "@/lib/plan-store";

const state = vi.hoisted(() => ({ toast: vi.fn(), copy: vi.fn(), upload: vi.fn() }));
vi.mock("@/lib/kb-api", () => ({ uploadKbFile: (...args: unknown[]) => state.upload(...args) }));
vi.mock("@/lib/use-toast", async (orig) => ({ ...(await orig<Record<string, unknown>>()), toast: (...args: unknown[]) => state.toast(...args) }));
vi.mock("@/lib/clipboard", () => ({ copyTextWithFallback: (...args: unknown[]) => state.copy(...args) }));

import { ApiError } from "@/lib/api-client";
import deCat from "@/messages/de-DE.json";
import enCat from "@/messages/en-US.json";
import zhCat from "@/messages/zh-CN.json";
import { PlanCardConnected } from "./PlanCardConnected";
import { planActionErrorKey } from "./plan-action-errors";

const originalFetch = globalThis.fetch;
const planId = "plan_errors";
const REQUEST_ID = "fedcba9876543210fedcba9876543210";
const failedStep = { id: "ps_failed", agent: "compute", objective: "original task", status: "failed", level: 0, depends_on: [], included: true, needs_corpus: false };

function seed(): void {
  applyPlanSnapshot({ plan_id: planId, plan: { status: "blocked", current_rev: 1, workspace_id: "ws_1" }, steps: [failedStep] });
}

function failWith(status: number, detail: unknown): void {
  globalThis.fetch = vi.fn(async () =>
    new Response(JSON.stringify({ detail }), { status, headers: { "content-type": "application/json", "x-request-id": REQUEST_ID } }),
  ) as typeof fetch;
}

const LABELS = {
  "zh-CN": { skip: "跳过该步", revise: "修订重试", replan: "重新规划", copy: "复制诊断" },
  "en-US": { skip: "Skip step", revise: "Revise & retry", replan: "Replan", copy: "Copy diagnostics" },
  "de-DE": { skip: "Schritt überspringen", revise: "Überarbeiten und erneut versuchen", replan: "Plan neu erstellen", copy: "Diagnoseinformationen kopieren" },
} as const;

type Locale = keyof typeof LABELS;

function clickRecovery(label: string): void {
  const bar = document.querySelector(".pblock-acts")!;
  fireEvent.click([...bar.querySelectorAll("button")].find((button) => button.textContent?.includes(label))!);
}

async function lastToast(): Promise<{ text: string; action?: { label: string; onAction: () => Promise<void> } }> {
  await waitFor(() => expect(state.toast).toHaveBeenCalled());
  return state.toast.mock.calls.at(-1)![0];
}

async function expectCopiesStatus(action: { label: string; onAction: () => Promise<void> } | undefined, locale: Locale, status: number) {
  expect(action?.label).toBe(LABELS[locale].copy);
  state.copy.mockResolvedValue(true);
  await action!.onAction();
  const bundle = String(state.copy.mock.calls.at(-1)![0]);
  expect(bundle).toContain(`http_status: ${status}`);
  expect(bundle).toContain(`request_id: ${REQUEST_ID}`);
}

const LEAK = /\b[45]\d\d\b|plan\.|detail|revise failed|unknown agents|\{|HTTP/;

afterEach(() => {
  cleanup();
  _resetAllPlansForTesting();
  globalThis.fetch = originalFetch;
  state.toast.mockReset();
  state.copy.mockReset();
});

describe("plan card recovery errors", () => {
  it.each(Object.keys(LABELS) as Locale[])("skip 500 says what to do instead, no status (%s)", async (locale) => {
    seed();
    failWith(500, "Internal Server Error");
    renderIntl(<PlanCardConnected planId={planId} onApprove={vi.fn()} />, { locale });

    clickRecovery(LABELS[locale].skip);
    const toast = await lastToast();

    expect(toast.text).not.toMatch(LEAK);
    expect(toast.text).toContain(LABELS[locale].revise);
    await expectCopiesStatus(toast.action, locale, 500);
  });

  it.each(Object.keys(LABELS) as Locale[])("revise preview 422 points to Replan or Skip, no JSON (%s)", async (locale) => {
    seed();
    failWith(422, "revise failed: model returned no steps");
    renderIntl(<PlanCardConnected planId={planId} onApprove={vi.fn()} />, { locale });

    clickRecovery(LABELS[locale].revise);
    const toast = await lastToast();

    expect(toast.text).not.toMatch(LEAK);
    expect(toast.text).toContain(LABELS[locale].replan);
    await expectCopiesStatus(toast.action, locale, 422);
  });

  it.each(Object.keys(LABELS) as Locale[])("replan preview 422 asks for a more specific description (%s)", async (locale) => {
    seed();
    failWith(422, { msg: "unknown agents in replanned steps: ['x']" });
    renderIntl(<PlanCardConnected planId={planId} onApprove={vi.fn()} />, { locale });

    fireEvent.click(screen.getByText(LABELS[locale].replan));
    const input = document.querySelector(".pnl input")!;
    fireEvent.change(input, { target: { value: "use another method" } });
    fireEvent.keyDown(input, { key: "Enter" });
    const toast = await lastToast();

    expect(toast.text).not.toMatch(LEAK);
    expect(toast.text.length).toBeGreaterThan(20);
    await expectCopiesStatus(toast.action, locale, 422);
  });
});

describe("plan card confirm conflict", () => {
  const CATALOGS = { "zh-CN": zhCat, "en-US": enCat, "de-DE": deCat } as const;

  it.each(Object.keys(LABELS) as Locale[])("a busy conflict on confirm asks to discard the preview, not to wait and retry (%s)", async (locale) => {
    seed();
    const reply = (body: unknown, status = 200) =>
      new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json", "x-request-id": REQUEST_ID } });
    globalThis.fetch = vi.fn(async (_url, init) => {
      const body = JSON.parse(String(init?.body));
      if (body.confirm) return reply({ detail: { code: "plan_busy_or_conflict" } }, 409);
      return reply({ expected_rev: 1, revised: { ...failedStep, agent_name: "compute", success_criteria: [] } });
    }) as typeof fetch;
    renderIntl(<PlanCardConnected planId={planId} onApprove={vi.fn()} />, { locale });

    clickRecovery(LABELS[locale].revise);
    fireEvent.click(await screen.findByText(CATALOGS[locale].plan_card.revise_confirm));
    const toast = await lastToast();

    expect(toast.text).toBe(CATALOGS[locale].plan_card.action_errors.preview_conflict);
    expect(toast.text).not.toMatch(LEAK);
  });
});

describe("plan card supply material", () => {
  it.each(Object.keys(LABELS) as Locale[])("upload failure shows the file-page reason, no raw message (%s)", async (locale) => {
    seed();
    state.upload.mockRejectedValue(new ApiError(500, "kb upload exploded", { requestId: REQUEST_ID }));
    const { container } = renderIntl(<PlanCardConnected planId={planId} onApprove={vi.fn()} />, { locale });

    const input = container.querySelector<HTMLInputElement>('input[type="file"]')!;
    fireEvent.change(input, { target: { files: [new File(["x"], "notes.txt", { type: "text/plain" })] } });
    const toast = await lastToast();

    expect(toast.text).not.toMatch(/kb upload exploded|\b500\b/);
    await expectCopiesStatus(toast.action, locale, 500);
  });
});

describe("planActionErrorKey", () => {
  it.each([
    [{ status: 429 }, "skip", "too_frequent"],
    [{ status: 409 }, "replan_confirm", "stale"],
    [{ status: 404 }, "skip", "not_found"],
    [{ status: 422 }, "revise_confirm", "preview_outdated"],
    [{ status: 422 }, "edit", "edit_invalid"],
    [{ status: 503 }, "amend", "amend_failed"],
    [new TypeError("Failed to fetch"), "skip", "network"],
    [{ code: "plan_not_approvable" }, "skip", "stale"],
    [{ status: 409, code: "team_model_unconfigured" }, "replan_preview", "model_unconfigured"],
    [{ status: 409, code: "plan_busy_or_conflict" }, "revise_confirm", "preview_conflict"],
    [{ status: 409, code: "plan_busy_or_conflict" }, "replan_confirm", "preview_conflict"],
    [{ status: 409, code: "plan_busy_or_conflict" }, "skip", "busy"],
    [{ status: 409, code: "plan_agent_choice_required" }, "edit", "agent_choice_required"],
    [{ status: 409, code: "plan_agent_choice_stale" }, "edit", "agent_choice_stale"],
  ] as const)("%o on %s → %s", (error, action, key) => {
    expect(planActionErrorKey(error, action)).toBe(key);
  });
});
