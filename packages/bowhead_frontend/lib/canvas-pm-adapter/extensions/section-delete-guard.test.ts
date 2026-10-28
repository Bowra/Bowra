/**
 * A0-2 (report-edit-audit P0-2 containment) — pure-function logic for top-level section deletion guards.
 */

import { describe, expect, it } from "vitest";
import type { Node as PMNode } from "@tiptap/pm/model";
import {
  decideSectionRemoval,
  removedSectionKinds,
  sectionRemovedBetween,
  topLevelSectionIds,
} from "./section-delete-guard";

type FakeChild = { type: { name: string }; attrs: Record<string, unknown> };

function fakeDoc(children: FakeChild[]): PMNode {
  return {
    content: { size: 100 },
    forEach: (fn: (child: FakeChild) => void) => children.forEach((c) => fn(c)),
  } as unknown as PMNode;
}

const sec = (id: string, kind = "section_narrative"): FakeChild => ({
  type: { name: kind },
  attrs: { sectionId: id },
});
const para = (): FakeChild => ({ type: { name: "paragraph" }, attrs: {} });

describe("topLevelSectionIds", () => {
  it("只收 section_* 顶层节点的 sectionId", () => {
    const ids = topLevelSectionIds(
      fakeDoc([sec("a"), para(), sec("b", "section_kpi_row")]),
    );
    expect([...ids].sort()).toEqual(["a", "b"]);
  });

  it("缺 sectionId 的节点忽略", () => {
    const bad: FakeChild = { type: { name: "section_h1" }, attrs: {} };
    expect(topLevelSectionIds(fakeDoc([bad])).size).toBe(0);
  });
});

describe("sectionRemovedBetween", () => {
  it("删掉一个顶层 section → true", () => {
    expect(
      sectionRemovedBetween(fakeDoc([sec("a"), sec("b")]), fakeDoc([sec("a")])),
    ).toBe(true);
  });

  it("内容编辑不动 section 集合 → false", () => {
    expect(
      sectionRemovedBetween(
        fakeDoc([sec("a"), sec("b")]),
        fakeDoc([sec("b"), sec("a")]),
      ),
    ).toBe(false);
  });

  it("新增 section (AI rewrite 加段) → false", () => {
    expect(
      sectionRemovedBetween(fakeDoc([sec("a")]), fakeDoc([sec("a"), sec("c")])),
    ).toBe(false);
  });

  it("空文档基线 → false (spike 页等无 section 场景不干预)", () => {
    expect(sectionRemovedBetween(fakeDoc([para()]), fakeDoc([]))).toBe(false);
  });

  it("整组替换成别的 canvas 的 section → true (由 full-doc-replace 例外放行, 不在本函数职责)", () => {
    expect(
      sectionRemovedBetween(fakeDoc([sec("a")]), fakeDoc([sec("x"), sec("y")])),
    ).toBe(true);
  });
});

describe("decideSectionRemoval (guard-fulldoc-hole §4.1 放行判定表)", () => {
  const base = { hasAllowMeta: false, hasPreventUpdateMeta: false, removedKinds: [] as string[] };

  it("显式 allow meta → 放行", () => {
    expect(decideSectionRemoval({ ...base, hasAllowMeta: true, removedKinds: ["narrative"] }))
      .toBe("allow");
  });

  it("preventUpdate meta 存在即放行——值为 false 也算(setContent(x,true) 的写法)", () => {
    // filterTransaction passes a presence boolean; both meta values produce true
    expect(decideSectionRemoval({ ...base, hasPreventUpdateMeta: true, removedKinds: ["h1", "chart"] }))
      .toBe("allow");
  });

  it("没有移除 → 放行(普通编辑/重排)", () => {
    expect(decideSectionRemoval(base)).toBe("allow");
  });

  it("全选删除(多 kind, 无 meta) → block_multi", () => {
    expect(
      decideSectionRemoval({ ...base, removedKinds: ["eyebrow", "h1", "meta", "narrative"] }),
    ).toBe("block_multi");
  });

  it("单块有菜单 kind → block_menu(指路菜单)", () => {
    expect(decideSectionRemoval({ ...base, removedKinds: ["narrative"] })).toBe("block_menu");
    expect(decideSectionRemoval({ ...base, removedKinds: ["image"] })).toBe("block_menu");
  });

  it("单块无菜单 kind → block_unsupported(不指向不存在的入口)", () => {
    expect(decideSectionRemoval({ ...base, removedKinds: ["meta"] })).toBe("block_unsupported");
    expect(decideSectionRemoval({ ...base, removedKinds: ["kpi_row"] })).toBe("block_unsupported");
  });

  it("整文档替换不再靠结构放行: 无 meta 的全 doc 清空必被拦", () => {
    // The old isFullDocReplace exception permitted Cmd+A select-all deletion here
    expect(
      decideSectionRemoval({ ...base, removedKinds: ["eyebrow", "h1", "meta", "kpi_row", "chart"] }),
    ).not.toBe("allow");
  });
});

describe("removedSectionKinds (r3 §5.1 toast 分叉判据)", () => {
  it("返回被删节点的 kind（section_ 前缀已剥）", () => {
    expect(
      removedSectionKinds(
        fakeDoc([sec("a", "section_meta"), sec("b", "section_narrative")]),
        fakeDoc([sec("b", "section_narrative")]),
      ),
    ).toEqual(["meta"]);
  });

  it("无删除 → 空数组", () => {
    expect(
      removedSectionKinds(fakeDoc([sec("a")]), fakeDoc([sec("a")])),
    ).toEqual([]);
  });

  it("多删多 kind 全列出", () => {
    expect(
      removedSectionKinds(
        fakeDoc([sec("a", "section_image"), sec("b", "section_kpi_row"), sec("c")]),
        fakeDoc([sec("c")]),
      ).sort(),
    ).toEqual(["image", "kpi_row"]);
  });
});
