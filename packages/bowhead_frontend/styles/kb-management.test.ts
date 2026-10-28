/**
 * Source contract for styles/kb-management.css narrow-screen controls.
 *
 * jsdom does no cascade, so this pins the selector/specificity facts; pixels are verified in real browsers.
 */
import { describe, expect, it } from "vitest";

import { decls, norm, parseRules, read, specificity, splitSelectors, stripComments } from "./css-contract";

const RULES = parseRules(stripComments(read("styles/kb-management.css")));
const PHONE = "@media (max-width: 640px)";

describe("styles/kb-management.css: hit sizes and type tiles", () => {
  const rule = (sel: string) => RULES.filter((r) => r.media === null && splitSelectors(r.selector).some((s) => norm(s) === sel)).map((r) => decls(r.body));
  it("row checkboxes keep a small drawn box but a 28 px hit area", () => {
    expect(rule(".kb-cb").some((d) => d.position === "relative")).toBe(true);
    expect(rule(".kb-cb::before")[0]).toMatchObject({ content: '""', position: "absolute", inset: "-6px" });
  });
  it("type tiles use at least 9 px text on a 24 px tile that grows with large text", () => {
    expect(rule(".kb-ftype")[0]).toMatchObject({ width: "max(24px, 2.6em)", height: "max(24px, 2.6em)", "font-size": "0.5625rem" });
  });
});

describe("styles/kb-management.css: pager", () => {
  it("keeps the pager buttons in one unwrapped group with a tight gap for 44 px touch targets on phones", () => {
    const own = RULES.filter((r) => r.media === null && splitSelectors(r.selector).some((s) => norm(s) === ".kb-pagenav")).map((r) => decls(r.body));
    expect(own[0]).toMatchObject({ display: "flex", "flex-wrap": "nowrap" });
    const phone = RULES.filter((r) => norm(r.media ?? "") === PHONE && splitSelectors(r.selector).some((s) => norm(s) === ".kb-pagenav"));
    expect(decls(phone[0].body)["column-gap"]).toBe("4px");
  });
});

describe("styles/kb-management.css: phone search row", () => {
  it("the search field takes its own full row at ≤ 640 px and no stronger rule shrinks it", () => {
    const phone = RULES.filter((r) => norm(r.media ?? "") === PHONE);
    const own = phone.find((r) => splitSelectors(r.selector).some((s) => norm(s) === ".kb-search"));
    expect(own && decls(own.body).flex).toBe("1 0 100%");
    // Any other rule that reaches .kb-search and sets flex must not outrank the full-row rule
    // (`.kb-controls .kb-search { flex: 1 1 0 }` collapsed the field to ~0 px once the filters opened).
    const overrides = RULES.filter((r) => decls(r.body).flex !== undefined)
      .flatMap((r) => splitSelectors(r.selector).filter((s) => /\.kb-search$/.test(norm(s)) && norm(s) !== ".kb-search"))
      .filter((s) => specificity(s)[1] > 1);
    expect(overrides).toEqual([]);
  });
});
