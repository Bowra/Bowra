import { describe, expect, it } from "vitest";

import { plainExcerpt } from "./plain-excerpt";

describe("plainExcerpt", () => {
  it("drops markdown markup and keeps the words on one line", () => {
    expect(plainExcerpt("**2026年1月**销售额为 *12* 万元")).toBe("2026年1月销售额为 12 万元");
    expect(plainExcerpt("## Summary\n- Sales grew `12%`\n- See [the report](https://example.com/r)<br>Done")).toBe(
      "Summary Sales grew 12% See the report Done",
    );
    expect(plainExcerpt("> __Note__: ~~old~~ new\n\n| a | b |\n|---|---|\n| 1 | 2 |")).toBe("Note: old new | a | b | | 1 | 2 |");
  });

  it("leaves words with inner underscores, asterisks and plain text alone", () => {
    expect(plainExcerpt("snake_case_name and 3 * 4 * 5")).toBe("snake_case_name and 3 * 4 * 5");
    expect(plainExcerpt("Sales in January 2030 grew.")).toBe("Sales in January 2030 grew.");
  });
});
