import { mkdtempSync, mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import ts from "typescript";
import { describe, expect, it } from "vitest";

function config(name: string, directory = process.cwd()): ts.ParsedCommandLine {
  const filename = path.join(directory, name);
  const source = ts.readConfigFile(filename, ts.sys.readFile);
  if (source.error) throw new Error(ts.flattenDiagnosticMessageText(source.error.messageText, "\n"));
  return ts.parseJsonConfigFileContent(source.config, ts.sys, directory);
}

describe("TypeScript check scopes", () => {
  it("keeps every file from the complete check in production or test checks", () => {
    const complete = config("tsconfig.json");
    const production = config("tsconfig.production.json");
    const tests = config("tsconfig.tests.json");
    const checked = new Set([...production.fileNames, ...tests.fileNames]);
    expect(complete.fileNames.filter((file) => !checked.has(file))).toEqual([]);
    expect(production.fileNames.some((file) => file.includes("/e2e/") || /\.(test|spec)\.tsx?$/.test(file))).toBe(false);
    expect(tests.fileNames.some((file) => file.includes("/e2e/"))).toBe(true);
  });

  it("reports errors in production, unit tests, E2E helpers and test configuration", () => {
    const directory = mkdtempSync(path.join(tmpdir(), "bowhead-typecheck-"));
    const files = ["app.ts", "feature.test.ts", "e2e/helper.ts", "vitest.config.ts"];
    try {
      for (const file of files) {
        mkdirSync(path.dirname(path.join(directory, file)), { recursive: true });
        writeFileSync(path.join(directory, file), 'export const value: number = "invalid";\n');
      }
      for (const name of ["production", "tests"]) {
        const definition = JSON.parse(readFileSync(`tsconfig.${name}.json`, "utf8"));
        definition.extends = path.resolve("tsconfig.json");
        if (name === "production") definition.include = ["**/*.ts"];
        writeFileSync(path.join(directory, `tsconfig.${name}.json`), JSON.stringify(definition));
        const parsed = config(`tsconfig.${name}.json`, directory);
        const program = ts.createProgram(parsed.fileNames, {
          ...parsed.options,
          types: [],
          noLib: true,
          incremental: false,
        });
        const errors = ts.getPreEmitDiagnostics(program)
          .filter((diagnostic) => diagnostic.code === 2322 && diagnostic.file)
          .map((diagnostic) => path.relative(directory, diagnostic.file!.fileName))
          .sort();
        expect(errors).toEqual(name === "production" ? ["app.ts"] : files.slice(1).sort());
      }
    } finally {
      rmSync(directory, { recursive: true, force: true });
    }
  });
});
