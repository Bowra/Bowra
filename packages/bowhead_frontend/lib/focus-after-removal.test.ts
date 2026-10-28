import { afterEach, describe, expect, it } from "vitest";

import { keepFocusAfterRemoval } from "./focus-after-removal";

const flush = () => new Promise((resolve) => setTimeout(resolve, 0));

function setup() {
  document.body.innerHTML = `
    <h2 id="heading" tabindex="-1">列表</h2>
    <ul id="list"><li id="row"><button id="rowButton">删除</button></li></ul>
    <button id="other">别处</button>`;
  const $ = (id: string) => document.getElementById(id) as HTMLElement;
  return { heading: $("heading"), row: $("row"), rowButton: $("rowButton"), other: $("other") };
}

let stop: (() => void) | undefined;
afterEach(() => {
  stop?.();
  document.body.innerHTML = "";
});

describe("keepFocusAfterRemoval", () => {
  it("焦点所在的行被删掉后，把焦点放到目标上", async () => {
    const { heading, row, rowButton } = setup();
    rowButton.focus();
    stop = keepFocusAfterRemoval(() => heading);
    row.remove();
    await flush();
    expect(document.activeElement).toBe(heading);
  });

  it("焦点在别的元素上时不动它", async () => {
    const { heading, row, other } = setup();
    other.focus();
    stop = keepFocusAfterRemoval(() => heading);
    row.remove();
    await flush();
    expect(document.activeElement).toBe(other);
  });

  it("焦点先被还给行内按钮、之后那一行才被删掉，仍然转到目标", async () => {
    const { heading, row, rowButton } = setup();
    stop = keepFocusAfterRemoval(() => heading);
    await flush();
    expect(document.activeElement).toBe(heading);
    rowButton.focus();
    row.remove();
    await flush();
    expect(document.activeElement).toBe(heading);
  });

  it("用户按下鼠标后停止", async () => {
    const { heading, row, rowButton } = setup();
    rowButton.focus();
    stop = keepFocusAfterRemoval(() => heading);
    document.dispatchEvent(new Event("pointerdown"));
    row.remove();
    await flush();
    expect(document.activeElement).toBe(document.body);
  });

  it("新的一次调用取代上一次：上一次的目标不再抢焦点", async () => {
    const { heading, row, rowButton, other } = setup();
    keepFocusAfterRemoval(() => heading);
    stop = keepFocusAfterRemoval(() => other);
    rowButton.focus();
    row.remove();
    await flush();
    expect(document.activeElement).toBe(other);
  });
});
