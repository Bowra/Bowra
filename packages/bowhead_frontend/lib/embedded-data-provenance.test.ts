import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

import { projectEmbeddedDataProvenance } from "./embedded-data-provenance";

type Fixture = {
  contract: string;
  cases: Array<{
    name: string;
    input: Record<string, unknown>;
    expected: {
      status: "injected" | "computed_unchecked";
      source_indexes: number[];
      has_sources: boolean;
    };
  }>;
};

const fixture = JSON.parse(
  readFileSync(
    resolve(process.cwd(), "../../eval/fixtures/report_trust/b1a_embedded_data_provenance_v1.json"),
    "utf-8",
  ),
) as Fixture;

describe("EmbeddedDataProvenance B1a-v1", () => {
  it("matches the shared fixture", () => {
    expect(fixture.contract).toBe("embedded-data-provenance/B1a-v1");
    for (const row of fixture.cases) {
      const value = projectEmbeddedDataProvenance(row.input);
      expect(
        {
          status: value.status,
          source_indexes: value.sourceIndexes,
          has_sources: value.hasSources,
        },
        row.name,
      ).toEqual(row.expected);
    }
  });

  it("keeps Web embedded-data copy aligned with the shared finite catalog", () => {
    const copyFixture = JSON.parse(readFileSync(
      resolve(process.cwd(), "../../eval/fixtures/report_trust/b1a_copy_contract_v1.json"),
      "utf-8",
    )) as {
      embedded_data: Record<string, Record<string, string>>;
      source_linkage: Record<string, Record<string, string>>;
    };
    for (const locale of ["zh-CN", "en-US"]) {
      const messages = JSON.parse(readFileSync(
        resolve(process.cwd(), `messages/${locale}.json`),
        "utf-8",
      )) as {
        html_artifact: Record<string, string>;
        trust_strip: Record<string, string>;
        report_status: Record<string, string>;
      };
      const expected = copyFixture.embedded_data[locale];
      expect(messages.html_artifact.footer_injected_sources).toBe(expected.injected_sources);
      expect(messages.html_artifact.footer_injected_no_sources).toBe(expected.injected_no_sources);
      expect(messages.html_artifact.footer_computed_sources).toBe(expected.computed_sources);
      expect(messages.html_artifact.footer_computed_no_sources).toBe(expected.computed_no_sources);
      expect(messages.trust_strip.info_short)
        .toBe(copyFixture.source_linkage[locale].short_definition);
      expect(messages.report_status.verified)
        .toBe(copyFixture.source_linkage[locale].lifecycle_verified);
    }
  });

  it("keeps affected Web copy outside the shared semantic deny fixture", () => {
    const copyFixture = JSON.parse(readFileSync(
      resolve(process.cwd(), "../../eval/fixtures/report_trust/b1a_copy_contract_v1.json"),
      "utf-8",
    )) as { semantic_deny: string[] };
    for (const locale of ["zh-CN", "en-US"]) {
      const messages = JSON.parse(readFileSync(
        resolve(process.cwd(), `messages/${locale}.json`),
        "utf-8",
      )) as Record<string, unknown>;
      const affected = JSON.stringify({
        html_artifact: messages.html_artifact,
        trust_strip: messages.trust_strip,
        report_status: messages.report_status,
        source_panel: messages.source_panel,
        sources: messages.sources,
        provenance: messages.provenance,
        export: messages.export,
      }).toLocaleLowerCase("en-US");
      expect(
        copyFixture.semantic_deny.filter((phrase) => (
          affected.includes(phrase.toLocaleLowerCase("en-US"))
        )),
      ).toEqual([]);
    }
  });

  it("rejects cycles, non-plain objects, non-finite values, depth and node overflow", () => {
    const cyclic: unknown[] = [];
    cyclic.push(cyclic);
    expect(projectEmbeddedDataProvenance({ data: cyclic }).status).toBe("computed_unchecked");
    expect(projectEmbeddedDataProvenance({ data: new Date() }).status).toBe("computed_unchecked");
    expect(projectEmbeddedDataProvenance({ data: { x: Number.NaN } }).status).toBe("computed_unchecked");
    expect(projectEmbeddedDataProvenance({ data: { [Symbol("hidden")]: 1 } }).status)
      .toBe("computed_unchecked");

    const nested: unknown[] = [];
    let cursor = nested;
    for (let index = 0; index < 65; index += 1) {
      const child: unknown[] = [];
      cursor.push(child);
      cursor = child;
    }
    expect(projectEmbeddedDataProvenance({ data: nested }).status).toBe("computed_unchecked");
    expect(projectEmbeddedDataProvenance({ data: Array.from({ length: 100_001 }, () => null) }).status)
      .toBe("computed_unchecked");
  });
});
