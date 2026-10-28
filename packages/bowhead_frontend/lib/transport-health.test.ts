import { describe, expect, it } from "vitest";

import {
  BUFFERED_RATIO_THRESHOLD,
  createTransportSample,
  describeTransport,
  keepaliveGaps,
  msSinceLastEvent,
  noteEvent,
  noteKeepalive,
  verdictOf,
} from "./transport-health";

/** Synthetic buffered stream: a 200-second turn is delivered across six seconds. */
const BUFFERED_DURATION_MS = 200_000;
const BUFFERED_SPREAD_MS = 6_000;

describe("verdictOf —— 缓冲判定", () => {
  it("classifies a synthetic compressed stream as buffered", () => {
    const s = createTransportSample();
    noteEvent(s, 1_000);
    noteEvent(s, 1_000 + BUFFERED_SPREAD_MS);
    const v = verdictOf(s, BUFFERED_DURATION_MS);
    expect(v.kind).toBe("buffered");
    expect(v.kind === "buffered" && Math.round(v.ratio)).toBe(33);
  });

  it("★正对照：健康链路（事件铺满整轮）判成 ok", () => {
    const s = createTransportSample();
    noteEvent(s, 0);
    noteEvent(s, 100_000);
    expect(verdictOf(s, 100_000)).toEqual({ kind: "ok", ratio: 1 });
  });

  it("阈值边界：恰好到阈值判 buffered，略低于判 ok", () => {
    // ⚠ Duration must exceed MIN_TURN_MS_FOR_VERDICT, otherwise too_short handles it first
    // (the initial 4000ms case was rejected by guard order — guards correctly take precedence).
    const spread = 10_000;
    const at = createTransportSample();
    noteEvent(at, 0);
    noteEvent(at, spread);
    expect(verdictOf(at, spread * BUFFERED_RATIO_THRESHOLD).kind).toBe("buffered");

    const below = createTransportSample();
    noteEvent(below, 0);
    noteEvent(below, spread);
    expect(verdictOf(below, spread * BUFFERED_RATIO_THRESHOLD - 1).kind).toBe("ok");
  });
});

describe("守卫 —— 证据不足时必须沉默，不能猜", () => {
  it("切后台过 ⇒ unknown(hidden)，即使形状完全像缓冲", () => {
    const s = createTransportSample();
    noteEvent(s, 1_000);
    noteEvent(s, 1_000 + BUFFERED_SPREAD_MS);
    s.hiddenAtSomePoint = true;
    expect(verdictOf(s, BUFFERED_DURATION_MS)).toEqual({ kind: "unknown", reason: "hidden" });
  });

  it("发生过重连 ⇒ unknown(reconnected)（补发会把历史事件压在一起，同形）", () => {
    const s = createTransportSample();
    noteEvent(s, 1_000);
    noteEvent(s, 1_000 + BUFFERED_SPREAD_MS);
    s.reconnects = 1;
    expect(verdictOf(s, BUFFERED_DURATION_MS)).toEqual({ kind: "unknown", reason: "reconnected" });
  });

  it("turn 太短 ⇒ unknown(too_short)", () => {
    const s = createTransportSample();
    noteEvent(s, 0);
    noteEvent(s, 10);
    expect(verdictOf(s, 4_999)).toEqual({ kind: "unknown", reason: "too_short" });
  });

  it("classifies a short turn with a strong buffering ratio", () => {
    // An eight-second synthetic turn arrives across 50ms.
    // A long minimum-duration floor would incorrectly hide this strong ratio.
    const s = createTransportSample();
    noteEvent(s, 1_000);
    noteEvent(s, 1_050);
    const v = verdictOf(s, 8_000);
    expect(v.kind).toBe("buffered");
    expect(v.kind === "buffered" && Math.round(v.ratio)).toBe(160);
  });

  it("classifies a longer synthetic turn near the buffering threshold", () => {
    // Preserve a near-threshold ratio with independent synthetic timings.
    const s = createTransportSample();
    noteEvent(s, 0);
    noteEvent(s, 8_200);
    const v = verdictOf(s, 40_000);
    expect(v.kind).toBe("buffered");
    expect(v.kind === "buffered" && Number(v.ratio.toFixed(1))).toBe(4.9);
  });

  it("accepts a healthy long turn whose events span the execution", () => {
    // Same condition on a healthy path; together with the two preceding cases, anchors ground truth on both sides.
    const s = createTransportSample();
    noteEvent(s, 0);
    noteEvent(s, 72_000);
    expect(verdictOf(s, 70_000).kind).toBe("ok");
  });

  it("中途接流 ⇒ unknown(mid_turn)（分子是整轮时长、分母只有接流后那段）", () => {
    // review #2: F5 / hydrating into a running turn / follow-up stream all hit this.
    // Refresh can attach partway through a turn, so the partial span cannot measure full-turn buffering.
    const s = createTransportSample();
    noteEvent(s, 100_000);
    noteEvent(s, 130_000);
    s.attachedMidTurn = true;
    expect(verdictOf(s, 130_000)).toEqual({ kind: "unknown", reason: "mid_turn" });
  });

  it("一个事件都没有 ⇒ unknown(no_events)，不是 ok", () => {
    expect(verdictOf(createTransportSample(), 100_000)).toEqual({
      kind: "unknown",
      reason: "no_events",
    });
  });
});

describe("msSinceLastEvent —— ③ 用它，且它不归因", () => {
  it("收到过事件 ⇒ 距最后一条的时长", () => {
    const s = createTransportSample();
    noteEvent(s, 5_000);
    expect(msSinceLastEvent(s, 12_000, 1_000)).toBe(7_000);
  });

  it("★一个事件都没收到时用 turn 起点兜底 —— 这正是最该说话的场景", () => {
    const s = createTransportSample();
    expect(msSinceLastEvent(s, 40_000, 1_000)).toBe(39_000);
  });

  it("完全没有锚点才返回 null", () => {
    expect(msSinceLastEvent(createTransportSample(), 40_000, null)).toBeNull();
  });
});

describe("keepaliveGaps —— 只作佐证", () => {
  it("首条不入账，只算相邻间隔", () => {
    const s = createTransportSample();
    noteKeepalive(s, 1_000);
    noteKeepalive(s, 16_000);
    noteKeepalive(s, 31_000);
    expect(keepaliveGaps(s)).toEqual([15_000, 15_000]);
  });

  it("被攒在一起的心跳间隔极小", () => {
    const s = createTransportSample();
    noteKeepalive(s, 100_000);
    noteKeepalive(s, 100_020);
    expect(keepaliveGaps(s)).toEqual([20]);
  });

  it("少于两条时没有间隔可算", () => {
    const s = createTransportSample();
    noteKeepalive(s, 1_000);
    expect(keepaliveGaps(s)).toEqual([]);
  });
});


describe("describeTransport —— ② 复制给排障用的一句人话", () => {
  function sampleWith(durationMs: number | null, spreadMs: number) {
    const s = createTransportSample();
    if (spreadMs >= 0) {
      noteEvent(s, 1_000);
      noteEvent(s, 1_000 + spreadMs);
    }
    s.doneDurationMs = durationMs;
    return s;
  }

  it("被缓冲时给出比值、形状数字和**具体修法**", () => {
    const line = describeTransport(sampleWith(BUFFERED_DURATION_MS, BUFFERED_SPREAD_MS));
    expect(line).toContain("缓冲");
    expect(line).toContain("33.3×");
    expect(line).toContain("proxy_buffering off"); // Saying it is broken is insufficient; explain the fix.
    expect(line).toContain("重连 0");
  });

  it("★正对照：健康链路说正常，且不含修法噪音", () => {
    const line = describeTransport(sampleWith(100_000, 100_000));
    expect(line).toContain("正常");
    expect(line).not.toContain("proxy_buffering");
  });

  it("★证据不足时说「未判定」并给出原因，**绝不说「正常」**", () => {
    // Reporting unmeasured as healthy is precisely this sprint's target problem.
    const s = sampleWith(BUFFERED_DURATION_MS, BUFFERED_SPREAD_MS);
    s.hiddenAtSomePoint = true;
    const line = describeTransport(s);
    expect(line).toContain("未判定");
    expect(line).toContain("后台");
    expect(line).not.toContain("正常");
    expect(line).not.toContain("缓冲（");
  });

  it("后端没发 duration_ms ⇒ 未判定（不拿客户端时长顶替）", () => {
    expect(describeTransport(sampleWith(null, 5_000))).toContain("未判定");
  });

  it("中文逐字：被缓冲时的整句", () => {
    expect(describeTransport(sampleWith(BUFFERED_DURATION_MS, BUFFERED_SPREAD_MS))).toBe(
      `传输：❌ 整条 SSE 疑似被缓冲（比值 33.3×） [后端 ${(BUFFERED_DURATION_MS / 1000).toFixed(1)}s / 到达跨度 ${(BUFFERED_SPREAD_MS / 1000).toFixed(1)}s, 重连 0]`
        + " —— 请检查反向代理 / WAF / 负载均衡是否对 text/event-stream 关闭了缓冲（proxy_buffering off、不压缩、HTTP/1.1）",
    );
  });

  it.each(["en-US", "de-DE", "fr-FR"] as const)("%s 界面不落回中文，数字与修法照样给出", (locale) => {
    document.documentElement.lang = locale;
    try {
      const buffered = describeTransport(sampleWith(BUFFERED_DURATION_MS, BUFFERED_SPREAD_MS));
      expect(buffered).toContain("33.3×");
      expect(buffered).toContain("proxy_buffering off");
      const hidden = sampleWith(BUFFERED_DURATION_MS, BUFFERED_SPREAD_MS);
      hidden.hiddenAtSomePoint = true;
      const lines = [
        buffered,
        describeTransport(sampleWith(100_000, 100_000)),
        describeTransport(hidden),
        describeTransport(sampleWith(null, 5_000)),
        describeTransport(sampleWith(1_000, 500)),
      ];
      for (const line of lines) expect(line).not.toMatch(/\p{Script=Han}/u);
    } finally {
      document.documentElement.lang = "";
    }
  });
});
