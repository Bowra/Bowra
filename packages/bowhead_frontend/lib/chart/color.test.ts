/**
 * R7: color composition. Enforce an actual **production-build-only** incident.
 *
 * Production CSS compression changes --c-surface: #ffffff to #fff; old opacity concatenation
 * (theme.surface + "eb") produces five-digit #fffeb. Canvas rejects invalid colors and
 * **retains the previous fillStyle**, rendering numeric-label backings solid black. Dev CSS stays uncompressed,
 * so local screenshots / unit tests / typecheck all miss it.
 *
 * ★ Every case therefore uses **#fff (actual production value)**, rather than #ffffff.
 */

import { describe, expect, it } from "vitest";

import { mixHex, parseHexColor, seriesColor, withAlpha } from "./color";
import { mutedOf } from "./presentation";

/** Color strings canvas / CSS recognize (only generated hex shapes need coverage here). */
const VALID_HEX = /^#([0-9a-f]{3}|[0-9a-f]{6}|[0-9a-f]{8})$/i;

describe("parseHexColor", () => {
  it("★认 3 位 hex —— 生产 CSS 压缩后就是这个形态", () => {
    expect(parseHexColor("#fff")).toEqual([255, 255, 255]);
    expect(parseHexColor("#1b2")).toEqual([0x11, 0xbb, 0x22]); // Double each digit individually
  });

  it("认 6 位与 8 位", () => {
    expect(parseHexColor("#ffffff")).toEqual([255, 255, 255]);
    expect(parseHexColor("#1B1A17")).toEqual([27, 26, 23]);
    expect(parseHexColor("#ffffffeb")).toEqual([255, 255, 255]);
  });

  it("认不了的返回 null（调用方据此走不透明兜底，不产出非法串）", () => {
    expect(parseHexColor("white")).toBeNull();
    expect(parseHexColor("rgb(255,255,255)")).toBeNull();
    expect(parseHexColor("color-mix(in srgb, red, blue)")).toBeNull();
  });
});

describe("withAlpha", () => {
  it("★3 位 hex 加透明度必须先展开（旧写法在这里产出 `#fffeb`，canvas 静默落黑）", () => {
    expect(withAlpha("#fff", "eb")).toBe("#ffffffeb");
    expect("#fff" + "eb").not.toMatch(VALID_HEX); // Old concatenation is indeed invalid; preserve this evidence of the cause
  });

  it("6 位照常", () => {
    expect(withAlpha("#b04a2a", "33")).toBe("#b04a2a33");
  });

  it("解析不了的颜色返回原色（不透明但一定合法）", () => {
    expect(withAlpha("rgb(1,2,3)", "eb")).toBe("rgb(1,2,3)");
  });

  it("★产出永远是合法 hex —— 这是这个 helper 存在的全部意义", () => {
    for (const c of ["#fff", "#ffffff", "#1b1a17", "#b04a2a"]) {
      for (const a of ["eb", "33", "14", "90"]) {
        expect(withAlpha(c, a)).toMatch(VALID_HEX);
      }
    }
  });
});

describe("mutedOf 在生产色值下必须真的褪色", () => {
  it("★surface 是 `#fff` 时不能原样返回 —— 否则主角层褪色在生产里是死的", () => {
    const out = mutedOf("#b04a2a", "#fff");
    expect(out).not.toBe("#b04a2a");
    expect(out).toMatch(VALID_HEX);
    // Move 30/70 toward surface: should be lighter than the original color
    expect(parseHexColor(out)![0]).toBeGreaterThan(parseHexColor("#b04a2a")![0]);
  });

  it("6 位 surface 行为不变（回归保护）", () => {
    expect(mutedOf("#b04a2a", "#ffffff")).toBe(mutedOf("#b04a2a", "#fff"));
  });
});

describe("seriesColor：只消费服务端验证过的 effective palette", () => {
  const P = ["#b04a2a", "#5e7d8a", "#a98028", "#7a5a3f", "#9a8a6a", "#3a342c"];
  const SURFACE = "#ffffff";
  const INK = "#161310";

  it("★ 前 6 条逐字就是色板本身（不改既有观感）", () => {
    expect(P.map((_, i) => seriesColor(P, i, SURFACE, INK))).toEqual(P);
  });

  it("超出容量稳定回绕，不在浏览器制造未验证颜色", () => {
    const mine = Array.from({ length: 20 }, (_, i) => seriesColor(P, i, SURFACE, INK));
    expect(new Set(mine).size).toBe(P.length);
    expect(mine[6]).toBe(P[0]);
    expect(mine[12]).toBe(P[0]);
  });

  it("surface / ink 只作空色板兜底，不参与派生新颜色", () => {
    expect(seriesColor(P, 7, "#000000", "#ffffff")).toBe(P[1]);
    expect(seriesColor(P, 7, "#ffffff", "#000000")).toBe(P[1]);
  });

  it("转很多圈仍只稳定取既有槽位", () => {
    const far = seriesColor(P, 6 * 9 + 1, SURFACE, INK);
    expect(far).toBe(P[1]);
  });

  it("★ 解析不了的颜色不产出非法串（R7 那条事故的同一道防线）", () => {
    expect(seriesColor(["rgb(1,2,3)"], 1, SURFACE, INK)).toBe("rgb(1,2,3)");
    expect(mixHex("#fff", "#000", 0.5)).toBe("#808080"); // Also accept three-digit hex
  });
});
