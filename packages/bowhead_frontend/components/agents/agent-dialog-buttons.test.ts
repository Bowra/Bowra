/**
 * Footer-button styling contract for Agent management confirmation dialogs (disable, set / unset team default).
 *
 * `.am-dialog footer button` assigns transparent backgrounds and outlines with specificity (0,1,2), exceeding `.am-action-primary` (0,1,0):
 * the Set as Team Default primary button therefore has only light text on a transparent background, making it nearly invisible. Footer primary actions and danger confirmations must use
 * `.am-dialog footer .x` (0,2,1) to override it.
 */
import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const CSS = readFileSync(join(dirname(fileURLToPath(import.meta.url)), "../../styles/agent-marketplace.css"), "utf8").replace(/\/\*[\s\S]*?\*\//g, "");

function decls(selector: string): Record<string, string> {
  const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const m = new RegExp(`(?:^|})\\s*${escaped}\\s*\\{([^}]*)\\}`).exec(CSS);
  expect(m, selector).not.toBeNull();
  return Object.fromEntries(
    m![1]
      .split(";")
      .map((d) => d.trim())
      .filter(Boolean)
      .map((d) => [d.slice(0, d.indexOf(":")).trim(), d.slice(d.indexOf(":") + 1).trim()]),
  );
}

describe("智能体管理确认框底栏", () => {
  it("通用底栏按钮是透明底描边（取消）", () => {
    expect(decls(".am-dialog footer button")).toMatchObject({ background: "transparent" });
  });

  it("主操作在底栏里写全：强调色底、on-accent 字，权重压过通用底栏按钮", () => {
    expect(decls(".am-dialog footer .am-action-primary")).toEqual({
      color: "var(--c-accent-ink)",
      background: "var(--c-accent)",
      "border-color": "var(--c-accent)",
    });
    expect(decls(".am-dialog footer .am-action-primary:not(:disabled):hover")).toMatchObject({ background: "var(--c-accent-hover)" });
  });

  it("危险确认同样写在底栏里（实心红）", () => {
    expect(decls(".am-dialog footer .am-danger-confirm")).toMatchObject({ background: "var(--c-err)" });
  });
});
