/**
 * Batch 5 dependency graph view: component behavior.
 *
 * Pin design review's two P1 findings and the no-functionality-loss invariant:
 * ① No lost edges after a second revision (chain resolution): graph-layout.test.ts tests pure logic; this tests rendering.
 * ② Running-node details must not be clipped by shimmer (pin no overflow:hidden on .gnode).
 * ③ The graph must not be the sole action path: list recovery strips remain (mandate ⑤-1).
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, screen } from "@testing-library/react";
import { readFileSync } from "node:fs";
import { join } from "node:path";

import { renderIntl } from "@/__tests__/intl-render";
import { PlanCard } from "./PlanCard";
import type { PlanStepView, PlanView } from "./types";

afterEach(() => cleanup());

function step(id: string, over: Partial<PlanStepView> = {}): PlanStepView {
  return {
    id,
    agent: "compute",
    objective: "目标 " + id,
    level: 0,
    dependsOn: [],
    needsCorpus: false,
    status: "pending",
    included: true,
    seq: 0,
    ...over,
  };
}

function plan(steps: PlanStepView[], status: PlanView["status"] = "running"): PlanView {
  return { planId: "plan_1", title: "对本商超做一次经营尽调", status, steps };
}

describe("图区渲染与降级", () => {
  it("多步计划渲染图区", () => {
    renderIntl(<PlanCard plan={plan([step("a"), step("b", { seq: 1 })])} />);
    expect(screen.getByTestId("plan-graph")).toBeTruthy();
  });

  it("单步计划不渲染图区（图此时零信息量）", () => {
    renderIntl(<PlanCard plan={plan([step("a")])} />);
    expect(screen.queryByTestId("plan-graph")).toBeNull();
  });

  it("并行组即使 0 边也渲染——并行本身是形状", () => {
    renderIntl(<PlanCard plan={plan([step("a"), step("b", { seq: 1 }), step("c", { seq: 2 })])} />);
    expect(screen.getByTestId("plan-graph")).toBeTruthy();
    expect(screen.getByText("3 步并行")).toBeTruthy();
  });
});

describe("★P1-1 二次修订：依赖指向最老的旧步，节点仍要出现且旧步不占画面", () => {
  const steps = [
    step("a", { status: "superseded", supersededBy: "a2" }),
    step("a2", { status: "superseded", supersededBy: "a3", seq: 1 }),
    step("a3", { status: "done", seq: 2, origin: "revision" }),
    step("other", { status: "done", seq: 3 }),
    // The fan-in step depends on the **oldest** a (as real data does).
    step("j", { level: 1, seq: 4, status: "running", dependsOn: ["a", "other"] }),
  ];

  it("画面上只有活步 a3 与 j，两代旧步都不出现", () => {
    renderIntl(<PlanCard plan={plan(steps)} />);
    expect(screen.getByTestId("graph-node-a3")).toBeTruthy();
    expect(screen.getByTestId("graph-node-j")).toBeTruthy();
    expect(screen.queryByTestId("graph-node-a")).toBeNull();
    expect(screen.queryByTestId("graph-node-a2")).toBeNull();
  });

  it("汇合注记算 2 路——链解析失败的话只剩 1 路（设计稿的一跳解析就会退化成这样）", () => {
    renderIntl(<PlanCard plan={plan(steps)} />);
    expect(screen.getAllByText("2 路汇合").length).toBeGreaterThan(0);
    expect(screen.queryByText("1 路汇合")).toBeNull();
  });

  it("替代步戴「已修订」章，并给出修订历史入口", () => {
    renderIntl(<PlanCard plan={plan(steps)} />);
    const node = screen.getByTestId("graph-node-a3");
    expect(node.textContent).toContain("已修订");
    expect(screen.getByRole("button", { name: "查看修订历史" })).toBeTruthy();
  });

  it("展开修订历史 → 幽灵旧步挂出来并带「已被修订替换」注记", () => {
    renderIntl(<PlanCard plan={plan(steps)} />);
    fireEvent.click(screen.getByRole("button", { name: "查看修订历史" }));
    expect(screen.getByTestId("graph-node-a2")).toBeTruthy();
    expect(screen.getByText("↳ 已被修订替换")).toBeTruthy();
    // Ghosts cannot receive focus.
    expect(screen.getByTestId("graph-node-a2").getAttribute("tabindex")).toBe("-1");
  });
});

describe("★P1-2 running 节点的详情卡不能被裁掉", () => {
  // Note: a prior test read CSS and asserted no "overflow: hidden"; implementation review
  // proved it **falsely passed** with equivalent clip-path / contain:paint (also clipping popovers),
  // or border-radius:inherit moved to an unrelated selector. Text matching masqueraded as a behavior
  // test and was removed. Actual clipping protection requires real-browser geometry (see dogfood records); here instead pin
  // the decidable fact that no clipping properties are introduced, explicitly acknowledging its limits.
  it("图区样式表不得引入会裁掉详情卡的属性(overflow/clip-path/contain:paint)", () => {
    const css = readFileSync(join(process.cwd(), "styles/plan-graph.css"), "utf-8");
    // Strip comments first, then extract declarations, not lines (single-line rules contain multiple declarations).
    const decls = css.replace(/\/\*[\s\S]*?\*\//g, "");
    const found = [...decls.matchAll(/(?<![\w-])(overflow(?:-x|-y)?|clip-path|contain)\s*:\s*([^;}]+)/g)].map(
      (m) => `${m[1]}:${m[2].trim()}`,
    );
    // Allowlist: every entry must preserve unclipped details:
    const allowed = new Set([
      "overflow-x:auto", // .gcanvas.is-scroll: also switch .gpop to in-place expansion (in flow, safe from clipping).
      "overflow:visible", // .gedges
      "overflow:hidden", // .gn-tx text ellipsis: no ancestor relationship to .gpop.
    ]);
    for (const d of found) expect([...allowed]).toContain(d);
    // Conversely, the node itself must never clip (the original P1-2 bug).
    const nodeBlock = decls.slice(decls.indexOf(".gwrap .gnode {"), decls.indexOf(".gwrap .gn-ic"));
    expect(/overflow|clip-path|contain\s*:\s*paint/.test(nodeBlock)).toBe(false);
  });

  it("running 步的目标全文在详情卡里可读", () => {
    renderIntl(
      <PlanCard plan={plan([step("a", { status: "running", objective: "分析会员转化漏斗" }), step("b", { seq: 1 })])} />,
    );
    const node = screen.getByTestId("graph-node-a");
    expect(node.textContent).toContain("分析会员转化漏斗");
  });

  it("详情卡用 :focus-within 显示——焦点进到里面的恢复按钮时不能消失", () => {
    const css = readFileSync(join(process.cwd(), "styles/plan-graph.css"), "utf-8");
    expect(css).toContain(".gnode:focus-within .gpop");
    expect(css).not.toContain(".gnode:focus-visible .gpop");
  });
});

describe("断裂表达与恢复直达", () => {
  const blocked = plan(
    [
      step("ok", { status: "done", verify: { verdict: "pass", unmet: [] } }),
      step("bad", { status: "failed", seq: 1 }),
      step("j", { level: 1, seq: 2, status: "blocked", dependsOn: ["bad"] }),
    ],
    "blocked",
  );

  it("failed 节点在图内给「修订重试 / 跳过该步」直达", () => {
    const onRevise = vi.fn();
    const onSkip = vi.fn();
    renderIntl(
      <PlanCard
        plan={blocked}
        recovery={{ onRetry: vi.fn(), onSkip, onSupply: vi.fn(), onRevise }}
      />,
    );
    const node = screen.getByTestId("graph-node-bad");
    const btn = [...node.querySelectorAll("button")].find((b) => b.textContent?.includes("修订重试"));
    expect(btn).toBeTruthy();
    fireEvent.click(btn!);
    expect(onRevise).toHaveBeenCalledWith("bad");
  });

  it("被上游拖累的 blocked 节点只给因果注记，不给操作（要处理的是上游）", () => {
    renderIntl(
      <PlanCard
        plan={blocked}
        recovery={{ onRetry: vi.fn(), onSkip: vi.fn(), onSupply: vi.fn(), onRevise: vi.fn() }}
      />,
    );
    const node = screen.getByTestId("graph-node-j");
    expect(node.textContent).toContain("上游失败，等待处理");
    expect([...node.querySelectorAll("button")].length).toBe(0);
  });

  it("★不减功能：列表侧的恢复条依然在（图不是唯一路径）", () => {
    renderIntl(
      <PlanCard
        plan={blocked}
        recovery={{ onRetry: vi.fn(), onSkip: vi.fn(), onSupply: vi.fn(), onRevise: vi.fn() }}
      />,
    );
    // List recovery and graph buttons share copy → "修订重试" must appear more than once.
    expect(screen.getAllByText("修订重试").length).toBeGreaterThan(1);
  });
});

describe("图 ↔ 列表联动", () => {
  it("点节点 → 对应列表行加 is-hl", () => {
    const { container } = renderIntl(
      <PlanCard plan={plan([step("a"), step("b", { seq: 1 })])} />,
    );
    expect(container.querySelector('.pstep.is-hl')).toBeNull();
    fireEvent.click(screen.getByTestId("graph-node-a"));
    expect(container.querySelector('.pstep[data-row="a"].is-hl')).toBeTruthy();
  });

  it("再点一次收起（触屏点按展开的对偶）", () => {
    const { container } = renderIntl(
      <PlanCard plan={plan([step("a"), step("b", { seq: 1 })])} />,
    );
    const node = screen.getByTestId("graph-node-a");
    fireEvent.click(node);
    expect(node.getAttribute("aria-expanded")).toBe("true");
    fireEvent.click(node);
    expect(node.getAttribute("aria-expanded")).toBe("false");
    expect(container.querySelector(".pstep.is-hl")).toBeNull();
  });
});

describe("a11y 与状态可分", () => {
  it("节点 aria-label 含角色名+状态+核验，不只靠颜色", () => {
    renderIntl(
      <PlanCard
        plan={plan([
          step("a", { status: "done", verify: { verdict: "partial", unmet: ["缺口径"] } }),
          step("b", { seq: 1 }),
        ])}
      />,
    );
    // chat-ux-2026-08 plan integration: replace `plan_card.role_*` vocabulary with chat's canonical
    // `agentDisplayName` (see roles.ts `planAgentLabel`); data/compute agents' canonical name
    // is "数据与计算分析师". The assertion's **intent** stays unchanged (aria-label includes role + status + verification, beyond color);
    // only vocabulary changes: old compute and business both mapped to role_data, giving two agents
    // the same name on the card and names inconsistent with disclosure outside it.
    expect(screen.getByTestId("graph-node-a").getAttribute("aria-label")).toBe(
      "数据与计算分析师 · 已完成 · 部分达标",
    );
  });

  it("折叠钮有 aria-expanded 且能折起画布", () => {
    const { container } = renderIntl(<PlanCard plan={plan([step("a"), step("b", { seq: 1 })])} />);
    const fold = container.querySelector(".gtop-fold") as HTMLButtonElement;
    expect(fold.getAttribute("aria-expanded")).toBe("true");
    fireEvent.click(fold);
    expect(fold.getAttribute("aria-expanded")).toBe("false");
    expect(container.querySelector(".gwrap")?.className).toContain("folded");
  });
});

describe("proposed 态", () => {
  it("取消勾选的步在图上同步弱化（批准前看到的形状=将要执行的形状）", () => {
    const { container } = renderIntl(
      <PlanCard
        plan={plan([step("a", { included: false }), step("b", { seq: 1 })], "proposed")}
        edit={{
          onEditObjective: vi.fn(),
          onToggleInclude: vi.fn(),
          onDeleteStep: vi.fn(),
          onReorder: vi.fn(),
        }}
      />,
    );
    expect(container.querySelector('[data-node="a"]')?.className).toContain("is-off");
    expect(container.querySelector('[data-node="b"]')?.className).not.toContain("is-off");
  });
});

describe("联网检索不是角色（设计稿造了个后端没有的角色，实现不照抄）", () => {
  it("needsWeb 步的身份色仍来自 agent，联网用附加徽标表达", () => {
    const { container } = renderIntl(
      <PlanCard plan={plan([step("a", { needsWeb: true }), step("b", { seq: 1 })])} />,
    );
    const node = container.querySelector('[data-node="a"]')!;
    expect(node.querySelector(".gn-web")).toBeTruthy();
    // Role name still comes from the agent mapping, not "联网检索".
    // (Vocabulary: see above comment, role_* → agentDisplayName.)
    expect(node.querySelector(".gn-name")?.textContent).toBe("数据与计算分析师");
    expect(container.querySelector('[data-node="b"] .gn-web')).toBeNull();
  });
});

describe("★接线与守卫（实现 review 实测这些改坏了也不变红，故补钉）", () => {
  it("★阶梯布局（设计稿 1c）：层分行、深计划不再出横向滚动", () => {
    const { container } = renderIntl(
      <PlanCard
        plan={plan([
          step("a"),
          step("b", { level: 1, seq: 1, dependsOn: ["a"] }),
          step("c", { level: 2, seq: 2, dependsOn: ["b"] }),
        ])}
      />,
    );
    // 3 layers = 3 rows, each with one numbered ring.
    expect(container.querySelectorAll(".gstep-row")).toHaveLength(3);
    expect(container.querySelectorAll(".gstep-ring")).toHaveLength(3);
    // Links only connect layers; none after the last layer.
    expect(container.querySelectorAll(".gstep-link")).toHaveLength(2);
    // ★This redesign starts by entirely removing horizontal scrolling, together with its
    // expand-details-in-place patch. Keeping the old assertion would approve a regression.
    expect(container.querySelector(".gcanvas.is-scroll")).toBeNull();
    expect(container.querySelector(".gedges")).toBeNull();
  });


  it("★窄卡两行时必须撤掉 `· ` 分隔符（真机视觉 diff 逮到，单测全绿）", () => {
    // This requires CSS text assertions: jsdom does not apply global stylesheets, so rendering cannot see ::before.
    // Problem: `.gn-topic::before{content:"· "}` separates **inline** layout; switching to two lines leaves it
    // at the second line's start, looking like a list item.
    const css = readFileSync(join(process.cwd(), "styles/plan-graph.css"), "utf-8");
    const narrow = css.slice(css.indexOf("@container (max-width: 460px)"));
    expect(narrow).toContain(".gwrap .gn-topic::before { content: none; }");
    // Positive control: wide cards keep the separator, or role name and deliverable run together without a break.
    const wide = css.slice(0, css.indexOf("@container (max-width: 460px)"));
    expect(wide).toContain('.gwrap .gn-topic::before { content: "\u00b7 "; }');
  });

  it("★编号列就是进度条：圈与连线按层的真实状态取色", () => {
    const { container } = renderIntl(
      <PlanCard
        plan={plan([
          step("a", { status: "done" }),
          step("a2", { seq: 1, status: "failed" }),
          step("b", { level: 1, seq: 2, dependsOn: ["a"], status: "running" }),
          step("c", { level: 2, seq: 3, dependsOn: ["b"] }),
        ])}
      />,
    );
    const rings = container.querySelectorAll(".gstep-ring");
    const links = container.querySelectorAll(".gstep-link");
    // ★2026-08-14 correction: this originally asserted one failure in layer one means its ring is not done,
    // contradicting graph-layout.ts's own comment (three successes and one failure should still mean this layer has run)
    // **and** design 2b (a layer with failed members = `sn dn` ✓ green ring + `sv bk` dashed red link).
    // Written against the then-current implementation, it baked the bug into assertions. Observed on device 116: a layer with both steps failed
    // showed a gray ring = not reached, although the left column is the designed progress bar.
    // Rings answer progress; links/node colors/breakpoint badges carry failures. Both must hold together.
    expect(rings[0].className).toContain("is-done");
    expect(links[0].className).toContain("is-broken");
    // Layer two is running.
    expect(rings[1].className).toContain("is-running");
    expect(links[1].className).toContain("is-running");
    // No link after the last layer.
    expect(rings[2].className).toContain("is-pending");
  });

  it("两层不挂 is-scroll（免得常见形状无谓出滚动条）", () => {
    const { container } = renderIntl(
      <PlanCard plan={plan([step("a"), step("b", { level: 1, seq: 1, dependsOn: ["a"] })])} />,
    );
    expect(container.querySelector(".gcanvas.is-scroll")).toBeNull();
  });

  it("层内 >6 节点 → 该层挂 is-grid（6 不挂，7 挂）", () => {
    const six = Array.from({ length: 6 }, (_, i) => step("s" + i, { seq: i }));
    const { container: c6 } = renderIntl(<PlanCard plan={plan(six)} />);
    expect(c6.querySelector(".glayer.is-grid")).toBeNull();
    cleanup();
    const { container: c7 } = renderIntl(
      <PlanCard plan={plan([...six, step("s6", { seq: 6 })])} />,
    );
    expect(c7.querySelector(".glayer.is-grid")).toBeTruthy();
  });

  it.skip("[已废] has-edges —— 阶梯布局无 SVG 边层，该 class 与其 CSS 消费者一并删除", () => {
    const { container: withEdge } = renderIntl(
      <PlanCard plan={plan([step("a"), step("b", { level: 1, seq: 1, dependsOn: ["a"] })])} />,
    );
    expect(withEdge.querySelector(".gwrap.has-edges")).toBeTruthy();
    cleanup();
    const { container: flat } = renderIntl(<PlanCard plan={plan([step("a"), step("b", { seq: 1 })])} />);
    expect(flat.querySelector(".gwrap.has-edges")).toBeNull();
  });

  it("M10 守卫：单上游不叫「1 路汇合」，单节点层不叫「1 步并行」", () => {
    renderIntl(
      <PlanCard plan={plan([step("a"), step("b", { level: 1, seq: 1, dependsOn: ["a"] })])} />,
    );
    expect(screen.queryByText("1 路汇合")).toBeNull();
    expect(screen.queryByText("1 步并行")).toBeNull();
    expect(screen.queryByText("2 步并行")).toBeNull(); // a alone occupies layer one.
  });
});

describe("★P1-4/P1-5 图内恢复路径（review 用真实按键/在途态实测出的两个 bug）", () => {
  const blockedPlan = plan(
    [
      step("bad", { status: "failed" }),
      step("j", { level: 1, seq: 1, status: "blocked", dependsOn: ["bad"] }),
    ],
    "blocked",
  );

  it("键盘：焦点在图内「修订重试」上按 Enter 必须触发回调（原先被节点 keydown 吞掉）", () => {
    const onRevise = vi.fn();
    renderIntl(
      <PlanCard
        plan={blockedPlan}
        recovery={{ onRetry: vi.fn(), onSkip: vi.fn(), onSupply: vi.fn(), onRevise }}
      />,
    );
    const node = screen.getByTestId("graph-node-bad");
    const btn = [...node.querySelectorAll("button")].find((b) => b.textContent?.includes("修订重试"))!;
    const ev = fireEvent.keyDown(btn, { key: "Enter", bubbles: true });
    // The node must not preventDefault the event (otherwise default button activation is cancelled).
    expect(ev).toBe(true);
    btn.click();
    expect(onRevise).toHaveBeenCalledWith("bad");
  });

  it("在途（busy）时图内按钮必须禁用——否则成了绕过列表在途闸的第二个入口", () => {
    renderIntl(
      <PlanCard
        plan={blockedPlan}
        recovery={{ onRetry: vi.fn(), onSkip: vi.fn(), onSupply: vi.fn(), onRevise: vi.fn(), busy: true }}
      />,
    );
    const node = screen.getByTestId("graph-node-bad");
    const btns = [...node.querySelectorAll("button")];
    expect(btns.length).toBeGreaterThan(0);
    for (const b of btns) expect(b.disabled).toBe(true);
  });

  it("revise 预览打开时图内同样置灰（与列表隐藏恢复条同语义）", () => {
    renderIntl(
      <PlanCard
        plan={blockedPlan}
        recovery={{ onRetry: vi.fn(), onSkip: vi.fn(), onSupply: vi.fn(), onRevise: vi.fn() }}
        revise={{
          stepId: "bad",
          agent: "compute",
          objective: "改写后的目标",
          deliverable: "",
          criteria: [],
          onConfirm: vi.fn(),
          onDiscard: vi.fn(),
        }}
      />,
    );
    const node = screen.getByTestId("graph-node-bad");
    for (const b of [...node.querySelectorAll("button")]) expect(b.disabled).toBe(true);
  });
});

describe("批5 列表侧补件", () => {
  it("验收标准逐条列（.pacc-r），不再挤成一行正文", () => {
    const { container } = renderIntl(
      <PlanCard
        plan={plan([
          step("a", { successCriteria: ["口径与上季一致", "至少覆盖三个品类", "标注数据来源"] }),
          step("b", { seq: 1 }),
        ])}
      />,
    );
    const rows = container.querySelectorAll(".pacc .pacc-r");
    expect(rows).toHaveLength(3);
    expect(rows[1].textContent).toContain("至少覆盖三个品类");
    // The old single-line placeholder class must no longer appear.
    expect(container.querySelector(".pstep-criteria")).toBeNull();
  });

  it("核验徽标改用与图内同一套 .vpill，四态类名各自可分", () => {
    const mk = (verdict: "pass" | "partial" | "fail" | "skipped") =>
      renderIntl(
        <PlanCard
          plan={plan([step("a", { status: "done", verify: { verdict, unmet: [] } }), step("b", { seq: 1 })])}
        />,
      );
    const cases = [
      ["pass", "ok"],
      ["partial", "part"],
      ["fail", "unmet"],
      ["skipped", "none"],
    ] as const;
    for (const [verdict, cls] of cases) {
      const { container } = mk(verdict);
      const row = container.querySelector(".pstep .vpill");
      expect(row, verdict).toBeTruthy();
      expect(row!.className).toContain(cls);
      cleanup();
    }
  });

  it("★失败步的恢复条走 err 红，受阻走 warn 琥珀——与图区分色一致", () => {
    const failedPlan = plan([step("bad", { status: "failed" }), step("b", { seq: 1 })], "blocked");
    const { container: f } = renderIntl(
      <PlanCard plan={failedPlan} recovery={{ onRetry: vi.fn(), onSkip: vi.fn(), onSupply: vi.fn() }} />,
    );
    expect(f.querySelector(".pblock.is-failed")).toBeTruthy();
    cleanup();
    const blockedPlan = plan([step("bk", { status: "blocked" }), step("b", { seq: 1 })], "blocked");
    const { container: bl } = renderIntl(
      <PlanCard plan={blockedPlan} recovery={{ onRetry: vi.fn(), onSkip: vi.fn(), onSupply: vi.fn() }} />,
    );
    expect(bl.querySelector(".pblock")).toBeTruthy();
    expect(bl.querySelector(".pblock.is-failed")).toBeNull();
  });

  it("有图时列表区带自己的眉标；单步计划无图也无眉标", () => {
    const { container } = renderIntl(<PlanCard plan={plan([step("a"), step("b", { seq: 1 })])} />);
    const hd = container.querySelector(".plist-hd");
    expect(hd).toBeTruthy();
    expect(hd!.textContent).toContain("列表");
    cleanup();
    const { container: single } = renderIntl(<PlanCard plan={plan([step("a")])} />);
    expect(single.querySelector(".plist-hd")).toBeNull();
  });
});
