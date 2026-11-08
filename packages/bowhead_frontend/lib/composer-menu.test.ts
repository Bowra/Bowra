import { describe, expect, it } from "vitest";

import { composerFocusFallbackTarget } from "./composer-menu";

describe("composerFocusFallbackTarget", () => {
  it("returns the reference editor even when the attachment row's status comes first", () => {
    // Shape of the reference-input Composer with one attached file: the attachment row (and its
    // screen-reader status) renders above the editor, and there is no textarea to prefer.
    const root = document.createElement("div");
    root.innerHTML = `
      <div class="v2-composer-attachments"><span role="status"></span></div>
      <div contenteditable="true" aria-label="Task input"></div>`;
    document.body.append(root);
    expect(composerFocusFallbackTarget(root, null)).toBe(root.querySelector("[contenteditable]"));
    root.remove();
  });

  it("falls back to the status row when the Composer has no input", () => {
    const root = document.createElement("div");
    root.innerHTML = `<div role="status" tabindex="-1"></div>`;
    document.body.append(root);
    expect(composerFocusFallbackTarget(root)).toBe(root.querySelector('[role="status"]'));
    root.remove();
  });
});
