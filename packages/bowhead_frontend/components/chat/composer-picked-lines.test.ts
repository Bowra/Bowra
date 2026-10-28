import { describe, expect, it } from "vitest";

import { insertPickedLines } from "./v2-composer";

describe("Google Drive links inserted into the composer", () => {
  const links = "Q3 https://docs.google.com/document/d/a/edit";

  it("inserts at the caret on its own line", () => {
    expect(insertPickedLines("Summarize  please", links, 10)).toBe(`Summarize \n${links}\n please`);
  });

  it("appends at the end when the caret is unknown or out of range", () => {
    expect(insertPickedLines("Read this", links, null)).toBe(`Read this\n${links}`);
    expect(insertPickedLines("Read this", links, 99)).toBe(`Read this\n${links}`);
    expect(insertPickedLines("", links, 0)).toBe(links);
  });

  it("does not add blank lines next to existing line breaks", () => {
    expect(insertPickedLines("A\n", links, 2)).toBe(`A\n${links}`);
    expect(insertPickedLines("A\nB", links, 2)).toBe(`A\n${links}\nB`);
  });
});
