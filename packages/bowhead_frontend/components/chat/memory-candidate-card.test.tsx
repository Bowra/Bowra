/**
 * memory-revive S1 · MemoryCandidateCard unit tests.
 *
 * Coverage:
 *  - empty items → renders nothing
 *  - renders candidate text + reason
 *  -「记住」→ decide(cid, "remember");「忽略」→ decide(cid, "dismiss")
 *  - nonempty actingId disables buttons
 */

import { useState } from "react";

import { describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";
import type { PendingCandidate, TurnMemoryReceiptEntry } from "@/lib/memory-api";
import { MemoryCandidateCard } from "./memory-candidate-card";

vi.mock("@/lib/use-toast", () => ({ toast: vi.fn() }));
const deleteEntryMock = vi.hoisted(() => vi.fn());
vi.mock("@/lib/memory-api", async (orig) => ({
  ...(await orig<Record<string, unknown>>()),
  deleteEntry: (...args: unknown[]) => deleteEntryMock(...args),
}));

// eslint-disable-next-line import/first
import { toast } from "@/lib/use-toast";

afterEach(cleanup);
afterEach(() => deleteEntryMock.mockReset());

function _cand(overrides: Partial<PendingCandidate> = {}): PendingCandidate {
  return {
    id: "cand_1",
    userId: "u1",
    // ★ Default to draft origin: three-state/「改一下」cases exercise that path.
    // Separate negative control for distilled candidates (must not show「改一下」).
    origin: "feedback_draft",
    kind: "preference",
    key: "prefer_qoq",
    value: "偏好环比",
    reason: "user said so",
    sourceTurnId: "t_1",
    status: "pending",
    decidedBy: null,
    decidedAt: null,
    createdAt: "2026-07-22T00:00:00Z",
    updatedAt: "2026-07-22T00:00:00Z",
    ...overrides,
  } as PendingCandidate;
}

describe("MemoryCandidateCard", () => {
  it("renders nothing when items empty", () => {
    const { container } = renderIntl(
      <MemoryCandidateCard items={[]} actingId={null} decide={vi.fn()} />,
    );
    expect(
      container.querySelector('[data-testid="memory-candidate-card"]'),
    ).toBeNull();
  });

  it("renders candidate text and reason", () => {
    renderIntl(
      <MemoryCandidateCard
        items={[_cand()]}
        actingId={null}
        decide={vi.fn()}
      />,
    );
    expect(screen.getByTestId("memory-candidate-card")).toBeTruthy();
    expect(screen.getByText("偏好环比")).toBeTruthy();
    expect(screen.getByText("user said so")).toBeTruthy();
  });

  it("renders the server-provided source and effective target", () => {
    renderIntl(
      <MemoryCandidateCard
        items={[
          _cand({
            source_space: {
              type: "space",
              space_id: "ws_retail",
              space_name: "零售分析",
              availability: "available",
              label: "仅供你使用 · 零售分析",
              requires_confirmation: false,
            },
            default_target: {
              type: "space",
              space_id: "ws_retail",
              space_name: "零售分析",
              availability: "available",
              label: "仅供你使用 · 零售分析",
              requires_confirmation: false,
            },
          }),
        ]}
        actingId={null}
        decide={vi.fn()}
      />,
    );
    expect(screen.getByText(/来自: 零售分析/)).toBeTruthy();
    expect(screen.getByText(/批准后: 仅供你使用 · 零售分析/)).toBeTruthy();
  });

  it("remember button calls decide with 'remember'", async () => {
    const decide = vi.fn().mockResolvedValue({ ok: true });
    renderIntl(
      <MemoryCandidateCard items={[_cand()]} actingId={null} decide={decide} />,
    );
    fireEvent.click(screen.getByTestId("memory-candidate-remember"));
    expect(decide).toHaveBeenCalledWith("cand_1", "remember");
  });

  it("requires confirmation before a global target", async () => {
    const decide = vi.fn().mockResolvedValue({ ok: true });
    renderIntl(
      <MemoryCandidateCard
        items={[
          _cand({
            default_target: {
              type: "global",
              space_id: null,
              space_name: null,
              availability: "available",
              label: "仅供你使用 · 所有空间",
              requires_confirmation: true,
            },
          }),
        ]}
        actingId={null}
        decide={decide}
      />,
    );
    fireEvent.click(screen.getByTestId("memory-candidate-remember"));
    const dialog = await screen.findByRole("dialog", { name: "在所有空间记住这条内容？" });
    fireEvent.click(within(dialog).getByRole("button", { name: "取消" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(decide).not.toHaveBeenCalled();

    fireEvent.click(screen.getByTestId("memory-candidate-remember"));
    fireEvent.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "记住" }));
    await waitFor(() => expect(decide).toHaveBeenCalledWith("cand_1", "remember"));
  });

  it("dismiss button calls decide with 'dismiss'", async () => {
    const decide = vi.fn().mockResolvedValue({ ok: true });
    renderIntl(
      <MemoryCandidateCard items={[_cand()]} actingId={null} decide={decide} />,
    );
    fireEvent.click(screen.getByTestId("memory-candidate-dismiss"));
    expect(decide).toHaveBeenCalledWith("cand_1", "dismiss");
  });

  it("buttons disabled while acting", () => {
    renderIntl(
      <MemoryCandidateCard
        items={[_cand()]}
        actingId="cand_1"
        decide={vi.fn()}
      />,
    );
    expect(
      (screen.getByTestId("memory-candidate-remember") as HTMLButtonElement)
        .disabled,
    ).toBe(true);
    expect(
      (screen.getByTestId("memory-candidate-dismiss") as HTMLButtonElement)
        .disabled,
    ).toBe(true);
  });
});

describe("write gate 拒绝的呈现", () => {
  it("策略拒绝显示真实理由，而不是「请稍后再试」", async () => {
    // ★ Write-gate rejection is **by design**: retrying this content can never succeed.
    // Previously grouped with temporary failures (full database/locks) as action_failed (「请稍后再试」),
    // asking users to retry something impossible; the error classification was lying.
    const decide = vi
      .fn()
      .mockResolvedValue({ ok: false, refusal: "one_off" });
    renderIntl(
      <MemoryCandidateCard items={[_cand()]} actingId={null} decide={decide} />,
    );
    fireEvent.click(screen.getByTestId("memory-candidate-remember"));
    // The reason is written inside the card (no toast).
    const text = (await screen.findByRole("alert")).textContent ?? "";
    expect(text).toContain("这一轮的临时要求");
    expect(text).not.toContain("稍后再试");
    expect(toast).not.toHaveBeenCalled();
  });

  it("非策略失败仍回落通用文案（满库/锁定这类是真的可以重试的）", async () => {
    const decide = vi.fn().mockResolvedValue({ ok: false });
    renderIntl(
      <MemoryCandidateCard items={[_cand()]} actingId={null} decide={decide} />,
    );
    fireEvent.click(screen.getByTestId("memory-candidate-remember"));
    const text = (await screen.findByRole("alert")).textContent ?? "";
    expect(text).toContain("稍后再试");
  });

  it("被拒之后卡片仍在——用户还能按「忽略」收场（reject 不过闸）", async () => {
    const decide = vi
      .fn()
      .mockResolvedValue({ ok: false, refusal: "contains_secret" });
    renderIntl(
      <MemoryCandidateCard items={[_cand()]} actingId={null} decide={decide} />,
    );
    fireEvent.click(screen.getByTestId("memory-candidate-remember"));
    await vi.waitFor(() => expect(decide).toHaveBeenCalled());
    expect(screen.getByTestId("memory-candidate-dismiss")).toBeTruthy();
  });
});

describe("三态（批 1c 设计稿 A）", () => {
  it("★★ 记住之后卡片**留在原位**变成终态，而不是消失", async () => {
    // Successful approval removes the candidate from items immediately. If the card disappears too,
    // users cannot see「已记住」receipt or find「撤销」,
    // yet undoability is precisely the prerequisite for automatic capture.
    // ⚠ Never use testing-library rerender: it replaces the **bare element**,
    // losing renderIntl's NextIntlClientProvider wrapper (observed error:
    // 「context from NextIntlClientProvider was not found」）。
    // Use a small stateful wrapper to simulate the parent removing candidates after approval.
    const decide = vi.fn().mockResolvedValue({ ok: true, entryId: "mem_1" });
    function Host() {
      const [items, setItems] = useState([_cand()]);
      return (
        <MemoryCandidateCard
          items={items}
          actingId={null}
          decide={async (cid, action) => {
            const r = await decide(cid, action);
            setItems([]);
            return r;
          }}
        />
      );
    }
    renderIntl(<Host />);
    fireEvent.click(screen.getByTestId("memory-candidate-remember"));
    await vi.waitFor(() =>
      expect(screen.getByTestId("memory-candidate-settled-cand_1")).toBeTruthy(),
    );
    expect(screen.getByText("已记住")).toBeTruthy();
  });

  it("★ 撤销按钮只在**真有落点**时出现", async () => {
    // When backend returned no entry ID (old version / failed write), showing Undo would
    // make clicking fail or fake success; a lying receipt is worse than none.
    const decide = vi.fn().mockResolvedValue({ ok: true, entryId: null });
    renderIntl(
      <MemoryCandidateCard items={[_cand()]} actingId={null} decide={decide} />,
    );
    fireEvent.click(screen.getByTestId("memory-candidate-remember"));
    await vi.waitFor(() =>
      expect(screen.getByTestId("memory-candidate-settled-cand_1")).toBeTruthy(),
    );
    expect(screen.queryByTestId("memory-candidate-undo-cand_1")).toBeNull();
  });

  it("★★ Undo shows 「已撤销」, not the 「仅本次生效」 once text", async () => {
    // Regression: undo used to set `{ undone: true, as: "once" }`, and the renderer showed the once state.
    deleteEntryMock.mockResolvedValue({ deleted: "mem_1" });
    const decide = vi.fn().mockResolvedValue({ ok: true, entryId: "mem_1" });
    renderIntl(
      <MemoryCandidateCard items={[_cand()]} actingId={null} decide={decide} />,
    );
    fireEvent.click(screen.getByTestId("memory-candidate-remember"));
    fireEvent.click(await screen.findByTestId("memory-candidate-undo-cand_1"));
    await vi.waitFor(() =>
      expect(
        screen.getByTestId("memory-candidate-settled-cand_1").getAttribute("data-state"),
      ).toBe("undone"),
    );
    const settled = screen.getByTestId("memory-candidate-settled-cand_1");
    expect(deleteEntryMock).toHaveBeenCalledWith("mem_1");
    expect(within(settled).getByText("已撤销")).toBeTruthy();
    expect(settled.textContent).not.toContain("仅本次生效");
    expect(settled.textContent).not.toContain("本轮已按你说的做");
    expect(screen.queryByTestId("memory-candidate-undo-cand_1")).toBeNull();
    expect(within(settled).getByRole("link").textContent).toBe("在记忆中查看");
  });

  it("★★ live undo and a refresh render the same receipt", async () => {
    // Live: the receipt entry exists (hook added it on approval); Undo goes through the shared
    // `undoEntry`, which flips the entry to `undone` like the server-derived state.
    const remembered: TurnMemoryReceiptEntry = {
      id: "mem_1",
      kind: "preference",
      key: "prefer_qoq",
      display_value: "偏好环比",
      origin: "candidate",
      state: "remembered",
      undoable: true,
      candidate_id: "cand_1",
    };
    function Live() {
      const [entries, setEntries] = useState<TurnMemoryReceiptEntry[]>([remembered]);
      return (
        <MemoryCandidateCard
          items={[]}
          actingId={null}
          decide={vi.fn()}
          entries={entries}
          undoEntry={async (id) => {
            setEntries((prev) =>
              prev.map((e) => (e.id === id ? { ...e, state: "undone" as const, undoable: false } : e)),
            );
            return true;
          }}
        />
      );
    }
    const live = renderIntl(<Live />);
    fireEvent.click(screen.getByTestId("memory-candidate-undo-cand_1"));
    await vi.waitFor(() =>
      expect(
        screen.getByTestId("memory-candidate-settled-cand_1").getAttribute("data-state"),
      ).toBe("undone"),
    );
    // useId differs between mounts; everything else must be identical.
    const withoutIds = (html: string) => html.replace(/ (id|aria-labelledby)="[^"]*"/g, "");
    const liveHtml = withoutIds(screen.getByTestId("memory-candidate-card").innerHTML);
    live.unmount();

    // Refresh: no local state, only the server receipt with the derived `undone` state.
    renderIntl(
      <MemoryCandidateCard
        items={[]}
        actingId={null}
        decide={vi.fn()}
        entries={[{ ...remembered, state: "undone", undoable: false }]}
        undoEntry={vi.fn()}
      />,
    );
    expect(withoutIds(screen.getByTestId("memory-candidate-card").innerHTML)).toBe(liveHtml);
    expect(deleteEntryMock).not.toHaveBeenCalled();
  });

  it("a restored entry reads 「已记住」 again after a refresh", () => {
    renderIntl(
      <MemoryCandidateCard
        items={[]}
        actingId={null}
        decide={vi.fn()}
        entries={[
          {
            id: "mem_1",
            kind: "preference",
            key: "prefer_qoq",
            display_value: "偏好环比",
            origin: "candidate",
            state: "remembered",
            undoable: true,
            candidate_id: "cand_1",
          },
        ]}
      />,
    );
    const settled = screen.getByTestId("memory-candidate-settled-cand_1");
    expect(settled.getAttribute("data-state")).toBe("remembered");
    expect(within(settled).getByText("已记住")).toBeTruthy();
    expect(screen.getByTestId("memory-candidate-undo-cand_1")).toBeTruthy();
  });

  it("★★ 蒸馏候选不显示「改一下」——后端对它返 400，按钮点了必报错", () => {
    // All 92 production candidates were dreaming_light; without origin gating,
    // 100% of cards would contain a broken button. Caught on 116 dogfood (2026-09-09).
    renderIntl(
      <MemoryCandidateCard
        items={[_cand({ origin: "dreaming_light" })]}
        actingId={null}
        decide={vi.fn()}
        redraft={vi.fn()}
      />,
    );
    expect(screen.queryByTestId("memory-candidate-revise")).toBeNull();
  });

  it("★「改一下」按钮只在父层给了 redraft 时出现", () => {
    renderIntl(
      <MemoryCandidateCard
        items={[_cand()]}
        actingId={null}
        decide={vi.fn()}
      />,
    );
    expect(screen.queryByTestId("memory-candidate-revise")).toBeNull();
  });

  it("★★ 空意见不发请求 —— 重跑一遍只会拿到几乎一样的东西", async () => {
    const redraft = vi.fn().mockResolvedValue({ ok: true });
    renderIntl(
      <MemoryCandidateCard
        items={[_cand()]}
        actingId={null}
        decide={vi.fn()}
        redraft={redraft}
      />,
    );
    fireEvent.click(screen.getByTestId("memory-candidate-revise"));
    fireEvent.click(screen.getByTestId("memory-candidate-redraft"));
    expect(redraft).not.toHaveBeenCalled();
    expect(screen.getByText(/说一句哪里不对/)).toBeTruthy();
  });

  it("填了意见就把原话发回去重起草", async () => {
    const redraft = vi.fn().mockResolvedValue({ ok: true });
    renderIntl(
      <MemoryCandidateCard
        items={[_cand()]}
        actingId={null}
        decide={vi.fn()}
        redraft={redraft}
      />,
    );
    fireEvent.click(screen.getByTestId("memory-candidate-revise"));
    fireEvent.change(
      screen.getByTestId("memory-candidate-revise-input-cand_1"),
      { target: { value: "只看东北大区" } },
    );
    fireEvent.click(screen.getByTestId("memory-candidate-redraft"));
    await vi.waitFor(() =>
      expect(redraft).toHaveBeenCalledWith("cand_1", "只看东北大区"),
    );
  });
});

describe("跨轮：终态不能粘住", () => {
  it("★★ 上一轮的「已记住」不许出现在下一轮", async () => {
    // ⚠ MemoryCandidateCard has **one instance per entire session** (held atop TurnContainer,
    // following latestDoneTurnId), not one per turn. Local terminal state thus
    // needs cross-turn reset; otherwise turn 1's「记住」leaves its「已记住」card
    // below every later turn, reporting something long past.
    const decide = vi.fn().mockResolvedValue({ ok: true, entryId: "mem_1" });
    function Host() {
      const [turn, setTurn] = useState("t1");
      const [items, setItems] = useState([_cand()]);
      return (
        <>
          <button type="button" data-testid="next-turn" onClick={() => {
            setTurn("t2");
            setItems([]); // Hook clears candidates at the start of a new turn.
          }}>
            next
          </button>
          <MemoryCandidateCard
            turnId={turn}
            items={items}
            actingId={null}
            decide={async (cid, action) => {
              const r = await decide(cid, action);
              setItems([]);
              return r;
            }}
          />
        </>
      );
    }
    renderIntl(<Host />);
    fireEvent.click(screen.getByTestId("memory-candidate-remember"));
    await vi.waitFor(() =>
      expect(screen.getByTestId("memory-candidate-settled-cand_1")).toBeTruthy(),
    );

    fireEvent.click(screen.getByTestId("next-turn"));
    expect(screen.queryByTestId("memory-candidate-settled-cand_1")).toBeNull();
    expect(screen.queryByTestId("memory-candidate-card")).toBeNull();
  });
});
