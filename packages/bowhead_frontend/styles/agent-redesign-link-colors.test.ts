/**
 * `--c-accent-ink` is the text colour for accent fills (white in the light themes). The Agents page redesign used it
 * for links and chips on light grounds, which rendered them white on white (116 acceptance pass 1, F4). Text links
 * and tinted chips use `--c-accent-text`; `--c-accent-ink` is only allowed where the rule also paints an accent fill.
 */
import { readFileSync } from "node:fs";
import path from "node:path";
import { describe, expect, it } from "vitest";

const FILES = ["agent-list-v2.css", "agent-trial.css", "agent-marketplace.css"];
// Selector families added by the redesign and the owner draft trial.
const REDESIGN = /\.(am-rb|am-task-chip|am-list-v2|am-review-v2|am-trial|bh-trial|am-cfg2|am-opt)/;

function rules(css: string): Array<{ selector: string; body: string }> {
  const out: Array<{ selector: string; body: string }> = [];
  const pattern = /([^{}]+)\{([^{}]*)\}/g;
  for (let match = pattern.exec(css); match; match = pattern.exec(css)) {
    out.push({ selector: match[1].trim(), body: match[2] });
  }
  return out;
}

describe("redesign links and chips are readable", () => {
  it("never colours redesign text with --c-accent-ink unless the rule paints an accent fill", () => {
    const offenders: string[] = [];
    for (const file of FILES) {
      const css = readFileSync(path.join(__dirname, file), "utf8").replace(/\/\*[\s\S]*?\*\//g, "");
      for (const { selector, body } of rules(css)) {
        if (!REDESIGN.test(selector) || !/(^|;|\s)color:\s*var\(--c-accent-ink\)/.test(body)) continue;
        if (/background(-color)?:\s*var\(--c-accent(-hover)?\)/.test(body)) continue;
        offenders.push(`${file}: ${selector}`);
      }
    }
    expect(offenders).toEqual([]);
  });
});
