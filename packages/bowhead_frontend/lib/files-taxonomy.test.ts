// File-page Category: server filters and badge rules must agree — every asset belongs to exactly the category shown by its badge.
import { describe, expect, it } from "vitest";

import { ASSET_KINDS, type AssetClass, assetClassOf, assetClassQuery } from "./files-taxonomy";

const CLASSES: AssetClass[] = ["upload", "result", "export", "kb"];

function selected(cls: AssetClass | "all", kind: string, inKb: boolean): boolean {
  const q = assetClassQuery(cls);
  return q.kind.includes(kind) && (q.in_kb === undefined || q.in_kb === (inKb ? 1 : 0));
}

describe("assetClassQuery 与 assetClassOf 一致", () => {
  for (const kind of ASSET_KINDS) {
    for (const inKb of [false, true]) {
      it(`${kind}${inKb ? " · 已加入知识库" : ""}`, () => {
        const badge = assetClassOf({ kind, in_kb: inKb });
        expect(CLASSES.filter((c) => selected(c, kind, inKb))).toEqual([badge]);
        expect(selected("all", kind, inKb)).toBe(true);
      });
    }
  }
});
