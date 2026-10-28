/**
 * feedback-0713 #13 / F5 — URL ?session= soft-navigation guard truth table.
 *
 * Guard all three historical regression shapes:
 * ① Dead notification clicks (final review P1: lastRequested target never updates → previously clicked
 *   notification deduped forever); leaving then returning must still hydrate.
 * ② Rail-switch echo double hydration: URL write echo with sid===active must mark only, never hydrate.
 * ③ Same sid + report target repeatedly triggered while hydrating: ignore.
 */
import { describe, expect, it } from "vitest";

import {
  cleanUrlNavigationOutcome,
  isExternalCleanUrlNavigation,
  resolveUrlSessionNav,
  resolveUrlSessionNavAgainstLocation,
  syncActiveSessionSearchParams,
  urlHasPendingForeignSession,
  urlSessionTargetKey,
  withoutNextRouterHistoryMarkers,
} from "./url-session-nav";

function resolve(args: {
  sid: string | null;
  reportIntent?: boolean;
  activeSessionId: string | null;
  activeReportIntent?: boolean;
  lastRequestedSid?: string | null;
  lastRequestedReportIntent?: boolean;
  // Defaults: snapshot current (liveSid matches sid) + no reset in flight,
  // keeping existing cases’ semantics unchanged; only new explicit arguments take new branches.
  liveSid?: string | null;
  liveReportIntent?: boolean;
  resetInFlight?: boolean;
}) {
  return resolveUrlSessionNav({
    sid: args.sid,
    reportIntent: args.reportIntent ?? false,
    activeSessionId: args.activeSessionId,
    activeReportIntent: args.activeReportIntent ?? false,
    lastRequestedTarget: args.lastRequestedSid
      ? urlSessionTargetKey(args.lastRequestedSid, args.lastRequestedReportIntent ?? false)
      : null,
    liveSid: args.liveSid === undefined ? args.sid : args.liveSid,
    liveReportIntent: args.liveReportIntent ?? args.reportIntent ?? false,
    resetInFlight: args.resetInFlight ?? false,
  });
}

describe("resolveUrlSessionNav", () => {
  it("通知点击（新目标）→ hydrate", () => {
    expect(
      resolve({ sid: "s_a", activeSessionId: null }),
    ).toBe("hydrate");
    expect(
      resolve({ sid: "s_a", activeSessionId: "s_b", lastRequestedSid: "s_b" }),
    ).toBe("hydrate");
  });

  it("回归① 切走再点同一通知 → 仍 hydrate（死点击修复）", () => {
    // Notification A → hydrate → active=A, lastReq=A → rail switches B (URL write, lastReq=B)
    // → notification A again: sid=A ≠ active=B ≠ lastReq=B → must hydrate.
    expect(
      resolve({ sid: "s_a", activeSessionId: "s_b", lastRequestedSid: "s_b" }),
    ).toBe("hydrate");
  });

  it("回归② rail 切换回声（sid===active）→ mark-handled 不重复水合", () => {
    expect(
      resolve({ sid: "s_a", activeSessionId: "s_a" }),
    ).toBe("mark-handled");
  });

  it("同一会话从对话软跳转到报告 → 只同步报告视图", () => {
    expect(
      resolve({
        sid: "s_a",
        reportIntent: true,
        activeSessionId: "s_a",
        activeReportIntent: false,
        lastRequestedSid: "s_a",
      }),
    ).toBe("sync-report");
  });

  it("浏览器返回移除同一会话的 report 参数 → 只同步回对话视图", () => {
    expect(
      resolve({
        sid: "s_a",
        reportIntent: false,
        activeSessionId: "s_a",
        activeReportIntent: true,
        lastRequestedSid: "s_a",
        lastRequestedReportIntent: true,
      }),
    ).toBe("sync-report");
  });

  it("共享报告只读会话水合后强制进报告视图、URL 尚未写 report=1 → ignore，不重新水合", () => {
    // URL `?session=s_a` (lastRequested=[s_a,false]); hydration sets report view (active report=true).
    // Returning sync-report rehydrates as exiting read-only report → forces report again → infinite loop.
    expect(
      resolve({
        sid: "s_a",
        reportIntent: false,
        activeSessionId: "s_a",
        activeReportIntent: true,
        lastRequestedSid: "s_a",
        lastRequestedReportIntent: false,
      }),
    ).toBe("ignore");
  });

  it("回归③ 同一目标已在水合中 → ignore（防双重水合）", () => {
    expect(
      resolve({ sid: "s_a", activeSessionId: null, lastRequestedSid: "s_a" }),
    ).toBe("ignore");
  });

  it("同一 session 的 chat/report 是两个不同的水合目标", () => {
    expect(
      resolve({
        sid: "s_a",
        reportIntent: true,
        activeSessionId: null,
        lastRequestedSid: "s_a",
        lastRequestedReportIntent: false,
      }),
    ).toBe("hydrate");
  });

  it("无 sid（新对话清空 URL）→ ignore", () => {
    expect(
      resolve({ sid: null, activeSessionId: "s_a", lastRequestedSid: "s_a" }),
    ).toBe("ignore");
  });
});

describe("syncActiveSessionSearchParams", () => {
  it("clears source-session Results state on an accepted session switch", () => {
    const next = syncActiveSessionSearchParams({
      current: new URLSearchParams(
        "session=s_a&results=all&result=code&mode=raw&tab=data&focus=art_a&keep=1",
      ),
      activeSessionId: "s_b",
      reportLinkSessionId: null,
      clearSessionScopedResults: true,
    });

    expect(next.get("session")).toBe("s_b");
    expect(next.get("result")).toBeNull();
    expect(next.get("results")).toBeNull();
    expect(next.get("mode")).toBeNull();
    expect(next.get("tab")).toBeNull();
    expect(next.get("focus")).toBeNull();
    expect(next.get("keep")).toBe("1");
  });

  it("preserves an explicit initial result deep link without a reset intent", () => {
    const next = syncActiveSessionSearchParams({
      current: new URLSearchParams("session=s_b&result=artifact%3Aart_b&mode=preview"),
      activeSessionId: "s_b",
      reportLinkSessionId: null,
      clearSessionScopedResults: false,
    });

    expect(next.get("result")).toBe("artifact:art_b");
    expect(next.get("mode")).toBe("preview");
  });
});


describe("resolveUrlSessionNav after switching sessions and starting new work", () => {
  // Regression sequence for two synthetic sessions A and B:
  //   1. Page maintains URL via window.history.replaceState;
  //   2. Next useSearchParams does not follow native writes ⇒ snapshot may stay A after A→B;
  //   3. Click “新工作” → activeSessionId null → effect reruns, sees stale A;
  //   4. Old guard ignores only closing session (B), A ≠ B ⇒ permits hydration of A;
  //   5. After 2.1s hydration sets activeSessionId back A → URL rewritten ?session=A.
  // Two-layer criterion; either alone prevents step 4.
  // ⚠ Cannot reproduce with **one** session: stale sid equals closing session, so old guard blocks it.

  it("★ 快照过期（sid ≠ live URL）→ ignore，绝不拿旧 sid 去水合", () => {
    expect(
      resolve({
        sid: "s_example_a",
        liveSid: "s_example_b",
        activeSessionId: null,
      }),
    ).toBe("ignore");
  });

  it("★ 重置期内，URL 里出现**任何**会话都 ignore（不只是被关掉的那条）", () => {
    expect(
      resolve({
        sid: "s_example_a",
        liveSid: "s_example_a",
        activeSessionId: null,
        resetInFlight: true,
      }),
    ).toBe("ignore");
  });

  it("重置期内被关掉的那条同样 ignore（旧守卫唯一挡得住的形状，不能回归）", () => {
    expect(
      resolve({
        sid: "s_example_b",
        liveSid: "s_example_b",
        activeSessionId: null,
        resetInFlight: true,
      }),
    ).toBe("ignore");
  });

  it("重置结束（观察到干净 URL 后 resetInFlight=false）→ 正常深链恢复 hydrate", () => {
    expect(
      resolve({
        sid: "s_new",
        liveSid: "s_new",
        activeSessionId: null,
        resetInFlight: false,
      }),
    ).toBe("hydrate");
  });

  it("快照与 live 一致且非重置期 → 既有语义完全不变", () => {
    expect(resolve({ sid: "s_a", liveSid: "s_a", activeSessionId: "s_a" })).toBe("mark-handled");
    expect(
      resolve({ sid: "s_a", liveSid: "s_a", activeSessionId: "s_a", reportIntent: true }),
    ).toBe("sync-report");
  });

  it("negative control：判据没写死成恒 ignore —— 同一个 sid 在正常态必须 hydrate", () => {
    expect(
      resolve({ sid: "s_example_a", liveSid: "s_example_a", activeSessionId: null }),
    ).toBe("hydrate");
  });
});

describe("resolveUrlSessionNav — 页面自己写 URL 的回声", () => {
  // Removing Next history markers from page URL writes makes Next sync every write into searchParams, guard effect
  // reruns with fresh snapshot. Echo must never double-hydrate or revert user’s latest view switch.
  // Advance as page effect does below: every non-ignore decision records target in lastRequested.
  type Snapshot = { sid: string | null; reportIntent: boolean };
  type Live = Snapshot;
  function makePage(initial: { activeSessionId: string | null; activeReportIntent?: boolean; lastRequested?: Snapshot }) {
    const page = {
      activeSessionId: initial.activeSessionId,
      activeReportIntent: initial.activeReportIntent ?? false,
      lastRequestedTarget: initial.lastRequested?.sid
        ? urlSessionTargetKey(initial.lastRequested.sid, initial.lastRequested.reportIntent)
        : null,
      observe(snapshot: Snapshot, live: Live) {
        const action = resolveUrlSessionNav({
          sid: snapshot.sid,
          reportIntent: snapshot.reportIntent,
          activeSessionId: page.activeSessionId,
          activeReportIntent: page.activeReportIntent,
          lastRequestedTarget: page.lastRequestedTarget,
          liveSid: live.sid,
          liveReportIntent: live.reportIntent,
          resetInFlight: false,
        });
        if (action !== "ignore" && snapshot.sid) {
          page.lastRequestedTarget = urlSessionTargetKey(snapshot.sid, snapshot.reportIntent);
        }
        return action;
      },
    };
    return page;
  }
  const chat = (sid: string | null): Snapshot => ({ sid, reportIntent: false });
  const report = (sid: string): Snapshot => ({ sid, reportIntent: true });

  it("侧栏切到 s3、页面写下 ?session=s3：回声只记录，不重复水合；之后深链回 s2 照常打开（R6）", () => {
    const page = makePage({ activeSessionId: "s2", lastRequested: chat("s2") });
    page.activeSessionId = "s3";
    // Guard runs before write in same commit: snapshot/live still s2.
    expect(page.observe(chat("s2"), chat("s2"))).toBe("ignore");
    // After ?session=s3 write Next syncs, echo arrives.
    expect(page.observe(chat("s3"), chat("s3"))).toBe("mark-handled");
    // Notification/deep link back to s2.
    expect(page.observe(chat("s2"), chat("s2"))).toBe("hydrate");
  });

  it("回声晚到、视图已切到下一条会话 → ignore，不把用户弹回上一条", () => {
    const page = makePage({ activeSessionId: "s4", lastRequested: chat("s2") });
    // Page writes s3 then s4; s3 echo arrives after s4 lands.
    expect(page.observe(chat("s3"), chat("s4"))).toBe("ignore");
    expect(page.observe(chat("s4"), chat("s4"))).toBe("mark-handled");
  });

  it("回声已记录、视图在同一次提交里又切走（新写入还没落地）→ ignore", () => {
    const page = makePage({ activeSessionId: "s3", lastRequested: chat("s2") });
    expect(page.observe(chat("s3"), chat("s3"))).toBe("mark-handled");
    page.activeSessionId = "s4";
    expect(page.observe(chat("s3"), chat("s3"))).toBe("ignore");
  });

  it("连续切换报告 / 对话视图：report=1 的回声晚到 → ignore，不把刚切回的对话改回报告", () => {
    const page = makePage({ activeSessionId: "s3", lastRequested: chat("s3") });
    page.activeReportIntent = true; // Open report, page writes report=1.
    page.activeReportIntent = false; // Immediately switch back to chat, page removes report.
    // report=1 echo arrives only now, live URL already lacks report.
    expect(page.observe(report("s3"), chat("s3"))).toBe("ignore");
    expect(page.observe(chat("s3"), chat("s3"))).toBe("mark-handled");
  });

  it("negative control：同样的快照与 live URL 一致时是真实导航 → 同步报告视图", () => {
    const page = makePage({ activeSessionId: "s3", lastRequested: chat("s3") });
    expect(page.observe(report("s3"), report("s3"))).toBe("sync-report");
  });

  it("共享报告只读会话：水合强制报告视图后页面写 report=1，回声只记录，不再水合", () => {
    const page = makePage({ activeSessionId: null, lastRequested: chat("s_shared") });
    page.activeSessionId = "s_shared";
    page.activeReportIntent = true;
    expect(page.observe(chat("s_shared"), chat("s_shared"))).toBe("ignore");
    expect(page.observe(report("s_shared"), report("s_shared"))).toBe("mark-handled");
  });

  it("新工作写下干净 URL：回声没有 sid → ignore", () => {
    const page = makePage({ activeSessionId: null, lastRequested: chat("s3") });
    expect(page.observe(chat(null), chat(null))).toBe("ignore");
  });
});

describe("resolveUrlSessionNavAgainstLocation — 页面守卫的真实入口", () => {
  const base = { activeSessionId: "s3", activeReportIntent: false, lastRequestedTarget: urlSessionTargetKey("s3", false), resetInFlight: false };

  it("快照晚一拍带着 report=1、live URL 已切回对话 → ignore（会话一致也要比 report）", () => {
    expect(resolveUrlSessionNavAgainstLocation({
      ...base,
      snapshot: new URLSearchParams("session=s3&report=1"),
      location: "https://bowhead.example/bowhead/?session=s3",
    })).toBe("ignore");
  });

  it("快照晚一拍停在上一条会话 → ignore", () => {
    expect(resolveUrlSessionNavAgainstLocation({
      ...base,
      snapshot: new URLSearchParams("session=s2"),
      location: "https://bowhead.example/?session=s3",
    })).toBe("ignore");
  });

  it("negative control：快照与 live URL 一致 → 同步报告视图 / 打开新目标", () => {
    expect(resolveUrlSessionNavAgainstLocation({
      ...base,
      snapshot: new URLSearchParams("session=s3&report=1"),
      location: "https://bowhead.example/?session=s3&report=1",
    })).toBe("sync-report");
    expect(resolveUrlSessionNavAgainstLocation({
      ...base,
      snapshot: new URLSearchParams("session=s2"),
      location: "https://bowhead.example/?session=s2",
    })).toBe("hydrate");
  });
});

describe("isExternalCleanUrlNavigation — 前进 / 后退到干净的首页", () => {
  it("页面接手过会话目标、URL 被外部换成干净的 / → 回到新工作", () => {
    expect(isExternalCleanUrlNavigation({ snapshotSid: null, liveSid: null, handledTarget: urlSessionTargetKey("s2", false) })).toBe(true);
  });

  it("页面自己重置写下的 /：重置先清掉接手目标 → 不算外部导航", () => {
    expect(isExternalCleanUrlNavigation({ snapshotSid: null, liveSid: null, handledTarget: null })).toBe(false);
  });

  it("快照或 live URL 仍带会话（这一拍快照过期）→ 先不动", () => {
    const handledTarget = urlSessionTargetKey("s2", false);
    expect(isExternalCleanUrlNavigation({ snapshotSid: "s2", liveSid: null, handledTarget })).toBe(false);
    expect(isExternalCleanUrlNavigation({ snapshotSid: null, liveSid: "s2", handledTarget })).toBe(false);
  });
});

describe("cleanUrlNavigationOutcome — 前进到 / 处理完之后", () => {
  it("已重置 → done", () => {
    expect(cleanUrlNavigationOutcome({ result: "created", viewSessionId: "s2", liveSid: null })).toBe("done");
  });

  it("停在会话上取消 → 把会话 URL 写回当前记录", () => {
    expect(cleanUrlNavigationOutcome({ result: "cancelled", viewSessionId: "s2", liveSid: null })).toBe("restore-session-url");
  });

  it("会话还没打开完却被取消（如有待确认的发送）→ 放下接手目标，免得 / 被再次当成外部导航", () => {
    expect(cleanUrlNavigationOutcome({ result: "cancelled", viewSessionId: null, liveSid: null })).toBe("release-target");
  });

  it("确认期间地址栏又回到某条会话 → 什么都不写", () => {
    expect(cleanUrlNavigationOutcome({ result: "cancelled", viewSessionId: "s2", liveSid: "s2" })).toBe("leave");
    expect(cleanUrlNavigationOutcome({ result: "created", viewSessionId: "s2", liveSid: "s3" })).toBe("leave");
  });
});

describe("urlHasPendingForeignSession", () => {
  it("更早开始的水合落地时，live URL 已被通知 / 深链换成别的会话 → 先不写，别把新目标覆盖掉", () => {
    expect(
      urlHasPendingForeignSession({ liveSid: "s3", activeSessionId: "s2", lastWrittenSid: "s2", userSwitch: false }),
    ).toBe(true);
  });

  it("live URL 上是页面自己上次写下的会话（侧栏切换后 activeSessionId 先变）→ 照常写", () => {
    expect(
      urlHasPendingForeignSession({ liveSid: "s2", activeSessionId: "s3", lastWrittenSid: "s2", userSwitch: false }),
    ).toBe(false);
  });

  it("用户在页面里主动切换会话 → 以用户的选择为准，照常写", () => {
    expect(
      urlHasPendingForeignSession({ liveSid: "s3", activeSessionId: "s4", lastWrittenSid: "s2", userSwitch: true }),
    ).toBe(false);
  });

  it("URL 与当前会话一致、URL 上没有会话、或当前没有会话 → 照常写", () => {
    expect(
      urlHasPendingForeignSession({ liveSid: "s2", activeSessionId: "s2", lastWrittenSid: null, userSwitch: false }),
    ).toBe(false);
    expect(
      urlHasPendingForeignSession({ liveSid: null, activeSessionId: "s2", lastWrittenSid: "s1", userSwitch: false }),
    ).toBe(false);
    expect(
      urlHasPendingForeignSession({ liveSid: "s3", activeSessionId: null, lastWrittenSid: "s2", userSwitch: false }),
    ).toBe(false);
  });
});

describe("withoutNextRouterHistoryMarkers", () => {
  it("去掉 Next 的历史标记（否则 replaceState 不同步 useSearchParams），保留页面自己的字段", () => {
    const state = { __NA: true, _N: true, __PRIVATE_NEXTJS_INTERNALS_TREE: ["", {}], results: { id: "r1" } };
    expect(withoutNextRouterHistoryMarkers(state)).toEqual({ results: { id: "r1" } });
    expect(state.__NA).toBe(true);
  });

  it("空状态给空对象", () => {
    expect(withoutNextRouterHistoryMarkers(null)).toEqual({});
    expect(withoutNextRouterHistoryMarkers(undefined)).toEqual({});
  });
});
