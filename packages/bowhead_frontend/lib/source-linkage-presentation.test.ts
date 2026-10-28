import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

import {
  MAX_JSON_SAFE_INTEGER,
  normalizeNonnegativeCount,
  projectSourceLinkage,
} from "./source-linkage-presentation";

type Fixture = {
  contract: string;
  cases: Array<{
    name: string;
    input: Record<string, unknown>;
    expected: {
      citation_count: number | null;
      figure_review_count: number | null;
      source_count: number | null;
      visible: boolean;
    };
  }>;
};

const fixture = JSON.parse(
  readFileSync(
    resolve(process.cwd(), "../../eval/fixtures/report_trust/b1a_source_linkage_counts_v1.json"),
    "utf-8",
  ),
) as Fixture;

describe("SourceLinkagePresentation B1a-v1", () => {
  it("matches the shared fixture", () => {
    expect(fixture.contract).toBe("source-linkage-presentation/B1a-v1");
    for (const row of fixture.cases) {
      const value = projectSourceLinkage(row.input);
      expect(
        {
          citation_count: value.citationCount ?? null,
          figure_review_count: value.figureReviewCount ?? null,
          source_count: value.sourceCount ?? null,
          visible: value.visible,
        },
        row.name,
      ).toEqual(row.expected);
    }
  });

  it("rejects JS-only malformed values", () => {
    expect(normalizeNonnegativeCount(Number.NaN)).toBeUndefined();
    expect(normalizeNonnegativeCount(Number.POSITIVE_INFINITY)).toBeUndefined();
    expect(normalizeNonnegativeCount(MAX_JSON_SAFE_INTEGER)).toBe(MAX_JSON_SAFE_INTEGER);
    expect(normalizeNonnegativeCount(MAX_JSON_SAFE_INTEGER + 1)).toBeUndefined();
  });
});
