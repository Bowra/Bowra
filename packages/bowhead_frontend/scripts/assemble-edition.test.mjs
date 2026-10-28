import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { mkdtempSync, mkdirSync, readFileSync, writeFileSync, existsSync, rmSync, copyFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { test } from "node:test";

const script = fileURLToPath(new URL("./assemble-edition.mjs", import.meta.url));
function fixture(t) {
  const repo = mkdtempSync(path.join(tmpdir(), "bowra-edition-test-"));
  t.after(() => rmSync(repo, { recursive: true, force: true }));
  const root = path.join(repo, "packages/bowhead_frontend");
  const put = (relative, content) => {
    const target = path.join(root, relative);
    mkdirSync(path.dirname(target), { recursive: true });
    writeFileSync(target, content);
  };
  put("edition/community/settings.ts", "export const entries = [];\n");
  put("edition/shared-messages/en-US.json", '{"title":"Bowhead","user":"{name}"}');
  put("scripts/assemble-edition.mjs", "");
  copyFileSync(script, path.join(root, "scripts/assemble-edition.mjs"));
  return {
    root,
    put,
    edition: (edition) => put("build-manifest.json", JSON.stringify({ schema_version: 1, edition })),
    business: () => {
      put("../../enterprise/frontend/messages/en-US.json", '{"private":"Enterprise setup"}');
      put("../../enterprise/frontend/app/(app)/admin/audit/page.tsx", "export default function Audit() {}\n");
      put("../../enterprise/frontend/edition/settings.ts", "export const entries = ['audit'];\n");
    },
    run: () =>
      execFileSync(process.execPath, [path.join(root, "scripts/assemble-edition.mjs")], {
        encoding: "utf8",
        stdio: "pipe",
      }),
  };
}
test("Community assembles independently and keeps interpolation values intact", (t) => {
  const f = fixture(t);
  f.edition("community");
  f.run();
  assert.deepEqual(JSON.parse(readFileSync(path.join(f.root, "messages/en-US.json"))), {
    title: "Bowra",
    user: "{name}",
  });
  const first = readFileSync(path.join(f.root, ".edition-files.json"), "utf8");
  f.run();
  assert.equal(readFileSync(path.join(f.root, ".edition-files.json"), "utf8"), first);
});
test("changing edition removes private routes and every previous locale artifact", (t) => {
  const f = fixture(t);
  f.business();
  f.edition("business");
  f.run();
  for (const directory of [".next", "out", "out-en-US", "out-zh-CN", "out-de-DE", "out-fr-FR"])
    f.put(`${directory}/private.js`, "private");
  f.edition("community");
  f.run();
  assert.equal(existsSync(path.join(f.root, "app/(app)/admin/audit/page.tsx")), false);
  for (const directory of [".next", "out", "out-en-US", "out-zh-CN", "out-de-DE", "out-fr-FR"])
    assert.equal(existsSync(path.join(f.root, directory)), false);
});
test("Business fails without its private source", (t) => {
  const f = fixture(t);
  f.edition("business");
  assert.throws(f.run, /Business frontend source is missing/);
});
test("assembly preserves edited generated files and unrelated source", (t) => {
  const f = fixture(t);
  f.edition("community");
  f.run();
  f.put("edition/settings.ts", "human edit");
  assert.throws(f.run, /Generated frontend file was edited/);
  assert.equal(readFileSync(path.join(f.root, "edition/settings.ts"), "utf8"), "human edit");
});
test("unowned collisions and malformed build manifests fail before mutation", (t) => {
  const f = fixture(t);
  f.edition("community");
  f.put("edition/settings.ts", "existing source");
  assert.throws(f.run, /unowned file/);
  f.put("build-manifest.json", '{"schema_version":1,"edition":"community","fallback":"business"}');
  assert.throws(f.run, /Invalid frontend build manifest/);
});
