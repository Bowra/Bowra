import "@testing-library/jest-dom/vitest";
import {
  act,
  cleanup,
  fireEvent,
  screen,
  waitFor,
} from "@testing-library/react";
import { useState } from "react";
import { afterEach, expect, it, vi } from "vitest";
import { renderIntl } from "@/__tests__/intl-render";
import {
  readCoachOperation,
  type CoachOperationOpen,
  type CoachOperationReceipt,
} from "@/lib/coach-operations";
import { CoachOperationView, operationSummary, operationTarget } from "./coach-operation-view";

vi.mock("@/lib/coach-operations", async (original) => ({
  ...(await original<typeof import("@/lib/coach-operations")>()),
  readCoachOperation: vi.fn(),
}));
afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

function caseReceipt(decision: string): CoachOperationReceipt {
  return {
    operation_ref: { kind: "eval_cases", agent_id: "osa_original", proposal_id: "acp_original" },
    domain_result: { proposal: {
      status: decision === "pending" ? "open" : decision,
      rationale: "Keep the original change available",
      suggested_cases: [{
        task: "Check the note before calculating the total.", decision_status: decision,
        case_spec: { expected_contract: { points: [{ id: "p_saved", text: "The total is 300." }] } },
        edit: { before: { task: "Calculate the total, then check the note.", case_spec: { expected_contract: { points: [{ id: "p_before", text: "Report the total." }] } } }, changed_fields: ["task", "expected_contract"] },
      }],
    } },
  };
}

it.each(["pending", "accepted", "dismissed", "unknown"])("keeps original %s case changes collapsed and readable on demand", async (decision) => {
  const original = caseReceipt(decision);
  vi.mocked(readCoachOperation).mockResolvedValue(original);
  renderIntl(<CoachOperationView pointer={{ ...pointer, reference: original.operation_ref }} active onOpen={vi.fn()} />, { locale: "en-US" });
  const toggle = await screen.findByText("View saved changes");
  expect(screen.getByText("Calculate the total, then check the note.")).not.toBeVisible();
  fireEvent.click(toggle);
  expect(screen.getByText("Calculate the total, then check the note.")).toBeVisible();
  expect(screen.getByText("Check the note before calculating the total.")).toBeVisible();
  expect(screen.getByText("Report the total.")).toBeVisible();
  expect(screen.getByText("The total is 300.")).toBeVisible();
  expect(screen.queryByText("p_saved")).not.toBeInTheDocument();
  expect(readCoachOperation).toHaveBeenCalledOnce();
  fireEvent.click(toggle);
  expect(screen.getByText("Report the total.")).not.toBeVisible();
});

it("preserves each item's decision and excludes held-out details", async () => {
  const original = caseReceipt("pending");
  const proposal = original.domain_result.proposal as { suggested_cases: Array<Record<string, unknown>> };
  proposal.suggested_cases.push({ ...proposal.suggested_cases[0], decision_status: "accepted", task: "Accepted saved question" });
  proposal.suggested_cases.push({ held_out: true, task: "Hidden question", edit: { before: { task: "Hidden baseline" } } });
  vi.mocked(readCoachOperation).mockResolvedValue(original);
  renderIntl(<CoachOperationView pointer={{ ...pointer, reference: original.operation_ref }} active />, { locale: "en-US" });
  fireEvent.click(await screen.findByText("View saved changes"));
  expect(screen.getByRole("region", { name: "Suggestion 1" })).toHaveTextContent("Awaiting review");
  expect(screen.getByRole("region", { name: "Suggestion 2" })).toHaveTextContent("Accepted");
  expect(screen.getByText("Held-out content is excluded.")).toBeVisible();
  expect(screen.queryByText("Hidden question")).not.toBeInTheDocument();
  expect(screen.queryByText("Hidden baseline")).not.toBeInTheDocument();
});

it("marks missing baselines and bounded excerpts without rendering raw settings or active markup", async () => {
  const original = caseReceipt("accepted");
  const proposal = original.domain_result.proposal as { suggested_cases: Array<Record<string, unknown>> };
  const item = { ...proposal.suggested_cases[0], task: `<script>unsafe()</script>${"x".repeat(1800)}END_OF_QUESTION`, edit: { changed_fields: ["task", "scorer_config"] } };
  proposal.suggested_cases = Array.from({ length: 9 }, () => item);
  vi.mocked(readCoachOperation).mockResolvedValue(original);
  renderIntl(<CoachOperationView pointer={{ ...pointer, reference: original.operation_ref }} active />, { locale: "en-US" });
  fireEvent.click(await screen.findByText("View saved changes"));
  expect(screen.getAllByText("No readable excerpt was saved for this content.")).toHaveLength(8);
  expect(screen.getAllByText("This excerpt is shortened.")).toHaveLength(8);
  expect(screen.getByText("1 more suggestion is not shown.")).toBeVisible();
  expect(screen.getAllByText(/Those settings are not included in this excerpt/)).toHaveLength(8);
  expect(screen.queryByText(/END_OF_QUESTION/)).not.toBeInTheDocument();
  expect(screen.getByTestId("coach-operation-card").querySelector("script")).toBeNull();
});

it("removes expanded saved changes after ownership is revoked", async () => {
  const original = caseReceipt("accepted");
  vi.mocked(readCoachOperation).mockResolvedValueOnce(original).mockRejectedValueOnce(new Error("Revoked"));
  renderIntl(<CoachOperationView pointer={{ ...pointer, reference: original.operation_ref }} active />, { locale: "en-US" });
  fireEvent.click(await screen.findByText("View saved changes"));
  expect(screen.getByText("Check the note before calculating the total.")).toBeVisible();
  fireEvent.click(screen.getByRole("button", { name: "Refresh" }));
  await screen.findByText("This result can’t be shown right now");
  expect(screen.queryByText("Check the note before calculating the total.")).not.toBeInTheDocument();
  expect(screen.queryByText("View saved changes")).not.toBeInTheDocument();
});

it("uses the original Owner review page for pending cases and current tests for decided cases", async () => {
  const reference = { kind: "eval_cases" as const, agent_id: "osa_1", proposal_id: "acp_original" };
  const accepted = { operation_ref: reference, domain_result: { proposal: { status: "accepted", rationale: "Original order change" } } };
  expect(operationTarget(accepted)).toEqual({ action: "view_current_cases", target: { kind: "agent_cases", agentId: "osa_1", pending: false } });
  expect(operationTarget({ ...accepted, domain_result: { proposal: { status: "dismissed" } } })?.action).toBe("view_current_cases");
  expect(operationTarget({ ...accepted, domain_result: { proposal: { status: "open", suggested_cases: [{ decision_status: "accepted" }, { decision_status: "pending" }] } } })).toEqual({ action: "review_in_agent", target: { kind: "agent_cases", agentId: "osa_1", pending: true } });
  vi.mocked(readCoachOperation).mockResolvedValue(accepted);
  renderIntl(<CoachOperationView pointer={{ ...pointer, reference }} active onOpen={vi.fn()} />);
  expect(await screen.findByTestId("coach-operation-card")).toHaveTextContent("当前用例可能之后又有修改");
  expect(screen.queryByRole("button", { name: "去智能体中审阅" })).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "查看当前用例" })).toBeVisible();
});
const pointer: CoachOperationOpen = {
  requestId: "original",
  turnId: "t_original",
  sessionId: "s_original",
  userId: "u_original",
  teamId: "team_original",
  workspaceId: "ws_original",
  reference: { kind: "skill_save", operation_id: `csw_${"a".repeat(64)}` },
};
const receipt: CoachOperationReceipt = {
  operation_ref: pointer.reference,
  domain_result: {
    outcome: "succeeded",
    original_target: { name: "private-method" },
  },
};

it("clears private results after a denied refresh and leaves closing to the tab", async () => {
  vi.mocked(readCoachOperation)
    .mockResolvedValueOnce(receipt)
    .mockRejectedValueOnce(new Error("Revoked"));
  renderIntl(<CoachOperationView pointer={pointer} active />);
  // The Skill slug never becomes the title; a successful save is named “Skill saved”.
  expect(await screen.findByRole("heading", { name: "技能已保存" })).toBeVisible();
  expect(screen.queryByText("private-method")).not.toBeInTheDocument();
  expect(screen.getByTestId("coach-operation-card")).toHaveAttribute("data-intent", "success");
  expect(screen.getByTestId("coach-operation-card")).toHaveTextContent("技能已保存");
  fireEvent.click(screen.getByRole("button", { name: "刷新" }));
  await screen.findByText("暂时无法查看这个结果");
  expect(screen.queryByTestId("coach-operation-card")).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: /关闭/ })).toBeNull();
  expect(readCoachOperation).toHaveBeenCalledTimes(2);
});

it("names and marks a failed save by its outcome and keeps the marker while the tab is inactive", async () => {
  vi.mocked(readCoachOperation).mockResolvedValue({
    operation_ref: { kind: "plugin_save", operation_id: `cpw_${"a".repeat(64)}` },
    domain_result: { outcome: "failed", original_target: { name: "Retail data pack" } },
  });
  const onMarker = vi.fn();
  const onLabel = vi.fn();
  const failedPointer = { ...pointer, reference: { kind: "plugin_save" as const, operation_id: `cpw_${"a".repeat(64)}` } };
  function Host() {
    const [active, setActive] = useState(true);
    return <>
      <button type="button" onClick={() => setActive(false)}>Select another tab</button>
      <CoachOperationView pointer={failedPointer} active={active} onMarker={onMarker} onLabel={onLabel} />
    </>;
  }
  renderIntl(<Host />);
  const card = await screen.findByTestId("coach-operation-card");
  expect(card).toHaveAttribute("data-intent", "failure");
  expect(card).toHaveTextContent("插件没能保存");
  expect(screen.getByRole("heading", { name: "插件没能保存" })).toBeVisible();
  await waitFor(() => expect(onMarker).toHaveBeenLastCalledWith({ marker: "failed", statusLabel: "失败" }));
  expect(onLabel).toHaveBeenLastCalledWith("插件没能保存");
  fireEvent.click(screen.getByRole("button", { name: "Select another tab" }));
  expect(screen.queryByTestId("coach-operation-card")).not.toBeInTheDocument();
  // An inactive tab does not read, so it keeps the last known marker and name.
  expect(onMarker).toHaveBeenLastCalledWith({ marker: "failed", statusLabel: "失败" });
  expect(onLabel).toHaveBeenLastCalledWith("插件没能保存");
});

it("fences a late result from the previously mounted target", async () => {
  let settle!: (value: CoachOperationReceipt) => void;
  vi.mocked(readCoachOperation).mockImplementationOnce(
    () =>
      new Promise((resolve) => {
        settle = resolve;
      }),
  );
  const first = renderIntl(
    <CoachOperationView pointer={pointer} active />,
  );
  await waitFor(() => expect(readCoachOperation).toHaveBeenCalledOnce());
  first.unmount();
  vi.mocked(readCoachOperation).mockResolvedValue({
    ...receipt,
    domain_result: {
      outcome: "unknown",
      original_target: { name: "second-method" },
    },
  });
  renderIntl(
    <CoachOperationView
      pointer={{ ...pointer, requestId: "second" }}
      active
    />,
  );
  // An unknown outcome keeps the neutral operation name.
  expect(await screen.findByRole("heading", { name: "技能保存" })).toBeVisible();
  await act(async () => settle(receipt));
  expect(screen.queryByRole("heading", { name: "技能已保存" })).not.toBeInTheDocument();
  expect(screen.getByTestId("coach-operation-card")).toHaveTextContent("结果尚不确定");
});

it("does not equate successful reads or CI authorization with successful execution", () => {
  expect(
    operationSummary({
      ...receipt,
      domain_result: { ok: true, state: "prepared", outcome: "unknown" },
    }).status,
  ).toBe("unknown");
  expect(
    operationSummary({
      operation_ref: {
        kind: "ci_delegation",
        agent_id: "osa_original",
        delegation_id: `acig_${"b".repeat(32)}`,
      },
      domain_result: { ok: true, delegation: { state: "authorized" } },
    }).status,
  ).toBe("authorized");
});

it("shows a stopped CI receipt and the recorded Tool requirement", async () => {
  const stopped: CoachOperationReceipt = {
    operation_ref: {
      kind: "ci_delegation",
      agent_id: "osa_original",
      delegation_id: `acig_${"b".repeat(32)}`,
    },
    domain_result: { ok: true, delegation: { state: "revoked" } },
  };
  vi.mocked(readCoachOperation).mockResolvedValue(stopped);
  renderIntl(
    <CoachOperationView
      pointer={{ ...pointer, reference: stopped.operation_ref }}
      active
    />,
  );
  expect(await screen.findByTestId("coach-operation-card")).toHaveTextContent("评测已停止");
  expect(
    operationSummary({
      operation_ref: {
        kind: "tool_requirement",
        requirement_id: `ctrq_${"c".repeat(32)}`,
        observation_id: null,
      },
      domain_result: {
        requirement: {
          operation: "Read the quarterly dataset",
          reason: "The current connection cannot read the required dataset.",
        },
      },
    }),
  ).toMatchObject({
    status: "requirement_recorded",
    title: "Read the quarterly dataset",
    summary: "The current connection cannot read the required dataset.",
  });
});

it("opens the saved Skill from the receipt card", async () => {
  vi.mocked(readCoachOperation).mockResolvedValue({
    ...receipt,
    domain_result: {
      outcome: "succeeded",
      original_target: { kind: "personal_skill", name: "weekly-summary", id: null, revision: null },
      result: { target: { kind: "personal_skill", name: "weekly-summary", id: "sk_saved", revision: "sha256:abc" } },
    },
  });
  const onOpen = vi.fn();
  renderIntl(<CoachOperationView pointer={pointer} active onOpen={onOpen} />);
  fireEvent.click(await screen.findByRole("button", { name: "打开技能" }));
  expect(onOpen).toHaveBeenCalledWith({
    kind: "resource",
    reference: { kind: "skill", source_kind: "user", id: "sk_saved", revision: "sha256:abc" },
  });
});

it("maps every receipt kind to the action the spec names, and none for Tool requirements", () => {
  expect(operationTarget({
    operation_ref: { kind: "plugin_save", operation_id: `cpw_${"a".repeat(64)}` },
    domain_result: { state: "failed", original_target: { kind: "team_plugin", id: "tcap_1", version: 2, revision: 3 } },
  })).toEqual({ action: "open_plugin", target: { kind: "resource", reference: { kind: "capability", id: "tcap_1", revision: "3" } } });
  expect(operationTarget({
    operation_ref: { kind: "agent_change", agent_id: "osa_a", proposal_id: "acp_1" }, domain_result: {},
  })).toMatchObject({ action: "review_in_agent", target: { kind: "resource", reference: { kind: "agent", id: "osa_a" } } });
  expect(operationTarget({
    operation_ref: { kind: "agent_change", agent_id: "osa_a", proposal_id: "acp_2" }, domain_result: { proposal: { status: "accepted" } },
  })).toMatchObject({ action: "view_in_agent", target: { kind: "resource", reference: { kind: "agent", id: "osa_a" } } });
  expect(operationTarget({
    operation_ref: { kind: "ci_delegation", agent_id: "osa_a", delegation_id: `acig_${"b".repeat(32)}` }, domain_result: {},
  })?.action).toBe("view_in_agent");
  expect(operationTarget({
    operation_ref: { kind: "draft_suggestion", draft_id: "sad_a", suggestion_id: "sas_000000000001" }, domain_result: {},
  })).toEqual({ action: "review_in_draft", target: { kind: "agent_draft", draftId: "sad_a" } });
  expect(operationTarget({
    operation_ref: { kind: "tool_requirement", requirement_id: `ctrq_${"c".repeat(32)}`, observation_id: null }, domain_result: {},
  })).toBeNull();
  expect(operationTarget({ ...receipt, domain_result: { outcome: "failed", original_target: { name: "x", id: null } } })).toBeNull();
});

it("hides the card action when the host cannot open resources", async () => {
  const reference = { kind: "agent_change" as const, agent_id: "osa_a", proposal_id: "acp_1" };
  vi.mocked(readCoachOperation).mockResolvedValue({ operation_ref: reference, domain_result: { proposal: { status: "pending" } } });
  renderIntl(<CoachOperationView pointer={{ ...pointer, reference }} active />);
  await screen.findByTestId("coach-operation-card");
  expect(screen.queryByRole("button", { name: "去智能体中审阅" })).toBeNull();
});
