/** Turn visibility FR5/E4: declared languages get a tag, unknown ones none; live code follows its newest line. */
import { describe, expect, it } from "vitest";

import { codeLanguageTag, lastCodeLines, newestCodeLine } from "./code-languages";

describe("code languages", () => {
  it("tags declared languages and leaves unknown ones untagged", () => {
    expect([codeLanguageTag("python"), codeLanguageTag("SQL"), codeLanguageTag("latex"), codeLanguageTag(null)]).toEqual(["Python", "SQL", undefined, undefined]);
  });

  it("keeps the last lines and finds the newest non-empty one", () => {
    expect(lastCodeLines("a\nb\nc", 2)).toBe("b\nc");
    expect(lastCodeLines("a\nb", 12)).toBe("a\nb");
    expect(newestCodeLine("import json\n  df = load()  \n\n")).toBe("df = load()");
    expect(newestCodeLine("")).toBe("");
  });
});
