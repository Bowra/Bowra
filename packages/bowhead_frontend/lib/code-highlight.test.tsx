/**
 * Shared syntax highlighting: language resolution, Markdown fences, and the React wrapper used by
 * report code blocks, execution steps, run sources and artifact previews.
 */
import { describe, expect, it } from "vitest";
import { render } from "@testing-library/react";
import { HighlightedCode } from "@/components/highlighted-code";
import {
  MAX_HIGHLIGHT_CHARS,
  codeLanguageForFile,
  resolveCodeLanguage,
} from "@/lib/code-highlight";
import { Markdown } from "@/lib/md";

describe("resolveCodeLanguage", () => {
  it("accepts display labels, highlight.js aliases and the dialect names models emit", () => {
    expect(resolveCodeLanguage("SQL")).toBe("sql");
    expect(resolveCodeLanguage(" Python ")).toBe("python");
    expect(resolveCodeLanguage("py")).toBe("py");
    expect(resolveCodeLanguage("shell")).toBe("shell");
    expect(resolveCodeLanguage("postgresql")).toBe("postgresql");
    expect(resolveCodeLanguage("tsx")).toBe("tsx");
  });

  it("keeps missing, plain-text and unknown labels plain", () => {
    for (const label of [undefined, null, "", "text", "txt", "plaintext", "stdout", "math", "mermaid", "nope"]) {
      expect(resolveCodeLanguage(label), String(label)).toBeNull();
    }
  });
});

describe("codeLanguageForFile", () => {
  it("prefers the file extension, then the MIME type", () => {
    expect(codeLanguageForFile("analysis.py", "application/octet-stream")).toBe("py");
    expect(codeLanguageForFile("query.SQL", "text/plain")).toBe("sql");
    expect(codeLanguageForFile("Dockerfile", "")).toBe("dockerfile");
    expect(codeLanguageForFile("result", "application/json; charset=utf-8")).toBe("json");
    expect(codeLanguageForFile("payload", "application/vnd.api+json")).toBe("json");
  });

  it("leaves data and prose files plain", () => {
    expect(codeLanguageForFile("notes.txt", "text/plain")).toBeNull();
    expect(codeLanguageForFile("rows.csv", "text/csv")).toBeNull();
    expect(codeLanguageForFile("run.log", "text/plain")).toBeNull();
  });
});

describe("HighlightedCode", () => {
  it("colors tokens without changing the text", () => {
    const code = "SELECT name, 42 FROM users -- active\nWHERE id = 'x'";
    const { container } = render(<code><HighlightedCode code={code} lang="SQL" /></code>);
    expect(container.textContent).toBe(code);
    expect(container.querySelector(".hljs-keyword")?.textContent).toBe("SELECT");
    expect(container.querySelector(".hljs-number")?.textContent).toBe("42");
    expect(container.querySelector(".hljs-string")?.textContent).toBe("'x'");
    expect(container.querySelector(".hljs-comment")?.textContent).toBe("-- active");
  });

  it("renders markup inside code as text", () => {
    const code = '<img src=x onerror="alert(1)">';
    const { container } = render(<code><HighlightedCode code={code} lang="python" /></code>);
    expect(container.querySelector("img")).toBeNull();
    expect(container.textContent).toBe(code);
  });

  it("falls back to plain text for unknown languages and oversized input", () => {
    const plain = render(<code><HighlightedCode code="x = 1" lang="mermaid" /></code>);
    expect(plain.container.querySelector("span")).toBeNull();
    const big = "x = 1\n".repeat(Math.ceil(MAX_HIGHLIGHT_CHARS / 6) + 1);
    const oversized = render(<code><HighlightedCode code={big} lang="python" /></code>);
    expect(oversized.container.querySelector("span")).toBeNull();
    expect(oversized.container.textContent).toBe(big);
  });
});

describe("Markdown fences", () => {
  it("highlights labeled fences and leaves inline code and unlabeled fences plain", () => {
    const { container } = render(
      <Markdown markdown={"Use `def` here.\n\n```python\ndef f():\n    return 1\n```\n\n```\ndef g(): pass\n```"} />,
    );
    const blocks = container.querySelectorAll("pre code");
    expect(blocks).toHaveLength(2);
    expect(blocks[0].classList.contains("hljs")).toBe(true);
    expect(blocks[0].querySelector(".hljs-keyword")?.textContent).toBe("def");
    expect(blocks[0].textContent).toBe("def f():\n    return 1\n");
    expect(blocks[1].querySelector("span")).toBeNull();
    expect(container.querySelector("p code")?.querySelector("span")).toBeNull();
  });

  it("keeps math fences for KaTeX", () => {
    const { container } = render(<Markdown markdown={"```math\nx^2\n```"} />);
    expect(container.querySelector(".katex")).not.toBeNull();
    expect(container.querySelector(".hljs")).toBeNull();
  });
});
