import { afterEach, describe, expect, it } from "vitest";

import {
  backslashKeyOnLayout,
  hasOpenOverlay,
  isResultsMaximizeShortcut,
  resultsMaximizeAriaKeyshortcuts,
  resultsMaximizeShortcutLabel,
} from "./workbench-shortcuts";

const base = { key: "|", code: "Backslash", metaKey: true, ctrlKey: false, shiftKey: true, altKey: false };

describe("成果区最大化快捷键 ⌘⇧\\ / Ctrl+Shift+\\", () => {
  it("macOS ⌘⇧\\、Windows Ctrl+Shift+\\ 都认（按物理键位，key 是「|」「\\」或中文标点「、」都行）", () => {
    expect(isResultsMaximizeShortcut(base)).toBe(true);
    expect(isResultsMaximizeShortcut({ ...base, metaKey: false, ctrlKey: true })).toBe(true);
    expect(isResultsMaximizeShortcut({ ...base, key: "\\" })).toBe(true);
    expect(isResultsMaximizeShortcut({ ...base, key: "、" })).toBe(true);
  });

  it("不带 Shift 的 ⌘\\ 归侧栏；带 Alt、没有 ⌘ / Ctrl、别的键都不算", () => {
    expect(isResultsMaximizeShortcut({ ...base, shiftKey: false, key: "\\" })).toBe(false);
    expect(isResultsMaximizeShortcut({ ...base, altKey: true })).toBe(false);
    expect(isResultsMaximizeShortcut({ ...base, metaKey: false })).toBe(false);
    expect(isResultsMaximizeShortcut({ ...base, key: "]", code: "BracketRight" })).toBe(false);
  });

  it("输入法组字中不算", () => {
    expect(isResultsMaximizeShortcut({ ...base, isComposing: true })).toBe(false);
    expect(isResultsMaximizeShortcut({ ...base, keyCode: 229 })).toBe(false);
  });

  it("提示写法随平台", () => {
    expect(resultsMaximizeShortcutLabel("MacIntel")).toBe("⌘⇧\\");
    expect(resultsMaximizeShortcutLabel("iPad")).toBe("⌘⇧\\");
    expect(resultsMaximizeShortcutLabel("Win32")).toBe("Ctrl+Shift+\\");
    expect(resultsMaximizeShortcutLabel(undefined)).toBe("Ctrl+Shift+\\");
    // Non-US layouts: use the character printed on the layout's backslash key
    expect(resultsMaximizeShortcutLabel("MacIntel", "#")).toBe("⌘⇧#");
    expect(resultsMaximizeShortcutLabel("Win32", "#")).toBe("Ctrl+Shift+#");
  });

  it("aria-keyshortcuts 与提示用同一个键：按布局写键位字符，拿不到布局时写美式键盘的 \\", () => {
    expect(resultsMaximizeAriaKeyshortcuts("MacIntel")).toBe("Meta+Shift+\\");
    expect(resultsMaximizeAriaKeyshortcuts("Win32")).toBe("Control+Shift+\\");
    expect(resultsMaximizeAriaKeyshortcuts(undefined)).toBe("Control+Shift+\\");
    expect(resultsMaximizeAriaKeyshortcuts("MacIntel", "#")).toBe("Meta+Shift+#");
    expect(resultsMaximizeAriaKeyshortcuts("Win32", "<")).toBe("Control+Shift+<");
    // “+” and space delimit aria-keyshortcuts, so use their specified names
    expect(resultsMaximizeAriaKeyshortcuts("Win32", "+")).toBe("Control+Shift+Plus");
    expect(resultsMaximizeAriaKeyshortcuts("Win32", " ")).toBe("Control+Shift+Space");
  });

  it("按键盘布局取 \\ 键位上的字符；浏览器不提供布局时为 null", async () => {
    const withLayout = { keyboard: { getLayoutMap: async () => new Map([["Backslash", "#"]]) } } as unknown as Navigator;
    expect(await backslashKeyOnLayout(withLayout)).toBe("#");
    expect(await backslashKeyOnLayout({} as Navigator)).toBeNull();
    const failing = { keyboard: { getLayoutMap: async () => { throw new Error("denied"); } } } as unknown as Navigator;
    expect(await backslashKeyOnLayout(failing)).toBeNull();
  });
});

describe("hasOpenOverlay：有浮层打开时快捷键让位", () => {
  afterEach(() => { document.body.innerHTML = ""; });
  const add = (html: string) => { document.body.insertAdjacentHTML("beforeend", html); };

  it("没有浮层时为 false；常驻的 Tooltip 不算", () => {
    add('<span role="tooltip">发送</span>');
    expect(hasOpenOverlay()).toBe(false);
  });

  it.each([
    ["模态弹窗", '<div role="dialog" aria-modal="true"></div>'],
    ["非模态浮层", '<div role="dialog"></div>'],
    ["菜单", '<div role="menu"></div>'],
    ["补全 / 下拉列表", '<div role="listbox"></div>'],
  ])("%s 打开时为 true", (_label, html) => {
    add(html);
    expect(hasOpenOverlay()).toBe(true);
  });

  it("藏起来的不算（hidden、aria-hidden、display:none、visibility:hidden）", () => {
    add('<div hidden><div role="menu"></div></div>');
    add('<div aria-hidden="true"><div role="dialog"></div></div>');
    add('<div style="display:none"><div role="listbox"></div></div>');
    add('<div role="menu" style="visibility:hidden"></div>');
    expect(hasOpenOverlay()).toBe(false);
  });
});
