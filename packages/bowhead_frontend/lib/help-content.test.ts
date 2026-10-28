import { existsSync, mkdtempSync, mkdirSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { describe, expect, it } from "vitest";

import {
  compareVersions,
  FIRST_PUBLISHED_RELEASE,
  HELP_ARTICLE_DEFINITIONS,
  loadHelpArticles,
  loadReleaseNotes,
  RELEASE_NOTES_DEFINITION,
  publishedReleaseSources,
  RELEASE_NOTES_ARTICLE_ID,
  rewriteHelpLinks,
  stripFrontMatter,
} from "./help-content";
import type { Locale } from "./locale";

const HELP_ROOT = [
  path.resolve(process.cwd(), "docs/help"),
  path.resolve(process.cwd(), "../../docs/help"),
].find((candidate) => existsSync(path.join(candidate, "permissions.zh-CN.md")));

/**
 * Capability-gated articles may ship before their German/French translation; until the
 * translated Markdown exists they read English content with any available localized metadata.
 * Ungated articles, and gated articles once translated, must be fully localized.
 */
function awaitsTranslation(
  definition: (typeof HELP_ARTICLE_DEFINITIONS)[number],
  locale: Locale,
): boolean {
  if (!definition.requiresCapability || !HELP_ROOT) return false;
  const localized = definition.source.replace(".zh-CN.md", `.${locale}.md`);
  return !existsSync(path.join(HELP_ROOT, localized));
}

describe("Help Center content", () => {
  it("loads every user-facing article for an explicit Chinese build", () => {
    const articles = loadHelpArticles(undefined, "zh-CN");

    expect(articles.map(({ id }) => id)).toEqual(
      HELP_ARTICLE_DEFINITIONS.map(({ id }) => id),
    );
    expect(articles.find(({ id }) => id === "overview")?.content).toContain(
      "```mermaid",
    );
    expect(
      articles.find(({ id }) => id === "security-sandbox")?.content,
    ).toMatch(/^# 安全与离线(?:沙箱|沙盒)/);
    expect(articles.find(({ id }) => id === "reference")?.content).toContain(
      "# 权限参考",
    );
  });

  it("loads English article metadata and Markdown for the English static build", () => {
    const articles = loadHelpArticles(undefined, "en-US");

    expect(articles.find(({ id }) => id === "overview")).toMatchObject({
      title: "Permissions and access guide",
      description: "Understand the complete permission model and setup flow",
    });
    expect(
      articles.find(({ id }) => id === "security-sandbox")?.content,
    ).toContain("# Security and offline sandbox");
    expect(
      articles.find(({ id }) => id === "getting-started")?.content,
    ).toContain("](/help/?article=space-access)");
  });

  it("loads German metadata and Markdown for every Help Center article", () => {
    const articles = loadHelpArticles(undefined, "de-DE");

    expect(articles).toHaveLength(HELP_ARTICLE_DEFINITIONS.length);
    expect(articles.find(({ id }) => id === "overview")).toMatchObject({
      title: "Anleitung zu Berechtigungen und Zugriff",
      description: "Berechtigungsmodell und Einrichtungsablauf verstehen",
    });
    expect(articles.find(({ id }) => id === "home")?.content).toContain(
      "Dieses Hilfe-Center",
    );
    expect(articles.find(({ id }) => id === "team-mcp-tools")?.content).toContain(
      "MCP-Dienste als Team-Tools",
    );
    expect(articles.find(({ id }) => id === "revise-report")).toMatchObject({
      title: "Einen bestehenden Bericht überarbeiten",
      content: expect.stringContaining(
        "# Einen bestehenden Bericht überarbeiten",
      ),
    });
    expect(articles.find(({ id }) => id === "getting-started")?.content).toContain(
      "](/help/?article=space-access)",
    );
    for (const article of articles) {
      expect(article.content).not.toContain("# Permissions and access guide");
      expect(article.content).not.toContain("# Start an analysis");
    }
    const english = loadHelpArticles(undefined, "en-US");
    articles.forEach((article, index) => {
      if (awaitsTranslation(HELP_ARTICLE_DEFINITIONS[index], "de-DE")) return;
      expect(article.title, article.id).not.toBe(english[index].title);
      expect(article.description, article.id).not.toBe(english[index].description);
    });
  });

  it("falls back to English, never Chinese, for a language without Help translations", () => {
    // Any UI language without translated Help bodies (including future languages) reads English titles, descriptions and bodies.
    const locale = "xx-XX" as Locale;
    const articles = loadHelpArticles(undefined, locale);
    const english = loadHelpArticles(undefined, "en-US");

    expect(articles).toHaveLength(HELP_ARTICLE_DEFINITIONS.length);
    articles.forEach((article, index) => {
      expect(article.title).toBe(english[index].title);
      expect(article.description).toBe(english[index].description);
      expect(article.content).toBe(english[index].content);
    });
    expect(
      rewriteHelpLinks("[Space](permissions/space-access.en-US.md)", "permissions.en-US.md", locale),
    ).toContain("](/help/?article=space-access)");
  });

  it("loads French metadata and Markdown for every Help Center article", () => {
    const french = loadHelpArticles(undefined, "fr-FR");
    const english = loadHelpArticles(undefined, "en-US");

    expect(french.map(({ id }) => id)).toEqual(HELP_ARTICLE_DEFINITIONS.map(({ id }) => id));
    expect(french.find(({ id }) => id === "overview")).toMatchObject({
      title: "Guide des autorisations et des accès",
      content: expect.stringContaining("# Guide des autorisations et des accès"),
    });
    expect(french.find(({ id }) => id === "revise-report")).toMatchObject({
      title: "Modifier un rapport existant",
      content: expect.stringContaining("# Modifier un rapport existant"),
    });
    expect(french.find(({ id }) => id === "getting-started")?.content).toContain("](/help/?article=space-access)");
    french.forEach((article, index) => {
      if (awaitsTranslation(HELP_ARTICLE_DEFINITIONS[index], "fr-FR")) {
        expect(article.content).toEqual(english[index].content);
        expect(article.title).toBe(HELP_ARTICLE_DEFINITIONS[index].title["fr-FR"] ?? english[index].title);
        expect(article.description).toBe(HELP_ARTICLE_DEFINITIONS[index].description["fr-FR"] ?? english[index].description);
        return;
      }
      expect(article.title).not.toBe(english[index].title);
      expect(article.description).not.toBe(english[index].description);
      expect(article.content).not.toBe(english[index].content);
      expect(article.title + article.description + article.content).not.toMatch(/\p{Script=Han}/u);
    });
  });

  it("uses the French article once it exists", () => {
    const root = mkdtempSync(path.join(tmpdir(), "bowhead-help-fr-"));
    for (const { source } of HELP_ARTICLE_DEFINITIONS) {
      const english = path.join(root, source.replace(".zh-CN.md", ".en-US.md"));
      mkdirSync(path.dirname(english), { recursive: true });
      writeFileSync(english, "# English\n");
    }
    const first = HELP_ARTICLE_DEFINITIONS[0];
    writeFileSync(path.join(root, first.source.replace(".zh-CN.md", ".fr-FR.md")), "# Français\n");

    const articles = loadHelpArticles(root, "fr-FR");

    expect(articles[0].content).toBe("# Français\n");
    expect(articles.slice(1).every(({ content }) => content === "# English\n")).toBe(true);
  });

  it("rewrites release-note references to the standalone page", () => {
    expect(rewriteHelpLinks(
      "[Release notes](release-notes/index.en-US.md#latest)",
      "about.en-US.md",
      "en-US",
    )).toBe("[Release notes](/release-notes/#latest)");
  });

  it("rewrites links between Help articles and preserves external links", () => {
    const markdown = [
      "[空间](permissions/space-access.zh-CN.md)",
      "[参考](../permissions-reference.zh-CN.md)",
      "[外部](https://example.com/docs)",
    ].join("\n");

    expect(rewriteHelpLinks(markdown, "permissions.zh-CN.md", "zh-CN")).toContain(
      "](/help/?article=space-access)",
    );
    expect(
      rewriteHelpLinks(markdown, "permissions/configure-capabilities.zh-CN.md", "zh-CN"),
    ).toContain("](/help/?article=reference)");
    expect(rewriteHelpLinks(markdown, "permissions.zh-CN.md", "zh-CN")).toContain(
      "](https://example.com/docs)",
    );
    expect(
      rewriteHelpLinks(
        "[Space](permissions/space-access.en-US.md)",
        "permissions.en-US.md",
        "en-US",
      ),
    ).toContain("](/help/?article=space-access)");
    expect(
      rewriteHelpLinks(
        "[Bereich](permissions/space-access.de-DE.md)",
        "permissions.de-DE.md",
        "de-DE",
      ),
    ).toContain("](/help/?article=space-access)");
  });

  it("registers About in all four languages without a capability gate", () => {
    const titles = (["zh-CN", "en-US", "de-DE", "fr-FR"] as const).map((locale) => {
      const about = loadHelpArticles(undefined, locale).find(({ id }) => id === "about");
      expect(about?.requiresCapability).toBeUndefined();
      expect(about?.content).toContain("<!-- about-version -->");
      return about?.title;
    });
    // The team's application name is filled in by the Help page (withApplicationName)
    expect(titles).toEqual([
      "关于 {applicationName}",
      "About {applicationName}",
      "Über {applicationName}",
      "À propos de {applicationName}",
    ]);
  });

  it("composes selected release pages newest first in all four languages", () => {
    const headings = {
      "zh-CN": "# 更新说明",
      "en-US": "# Release notes",
      "de-DE": "# Versionshinweise",
      "fr-FR": "# Notes de version",
    } as const;
    const root = mkdtempSync(path.join(tmpdir(), "bowhead-help-releases-"));
    const indexSource = RELEASE_NOTES_DEFINITION.source;
    for (const locale of ["zh-CN", "en-US", "de-DE", "fr-FR"] as const) {
      for (const { source } of HELP_ARTICLE_DEFINITIONS) {
        const file = path.join(root, source.replace(".zh-CN.md", `.${locale}.md`));
        mkdirSync(path.dirname(file), { recursive: true });
        writeFileSync(file, "# Synthetic article\n");
      }
      const index = path.join(root, indexSource.replace(".zh-CN.md", `.${locale}.md`));
      mkdirSync(path.dirname(index), { recursive: true });
      const entries = ["unreleased", "v1.5.65", "v1.5.0", "v0.72.2"];
      writeFileSync(index, `---\ntitle: Fixture\n---\n\n${headings[locale]}\n\n${entries.map((version) => `- [${version}](./releases/${version}.${locale}.md)`).join("\n")}\n`);
      const pages = path.join(path.dirname(index), "releases");
      mkdirSync(pages, { recursive: true });
      for (const version of entries) {
        writeFileSync(path.join(pages, `${version}.${locale}.md`), `---\ntitle: ${version}\nreleased: 2026-10-21\n---\n\n# ${version}\n\nSynthetic release content.\n`);
      }
    }
    for (const locale of ["zh-CN", "en-US", "de-DE", "fr-FR"] as const) {
      const notes = loadReleaseNotes(root, locale);
      expect(notes?.group, locale).toBeNull();
      expect(notes.releaseDates).toEqual({ "v1.5.65": "2026-10-21", "v1.5.0": "2026-10-21" });
      expect(notes?.requiresCapability).toBeUndefined();
      const content = notes?.content ?? "";
      // No front matter, one top-level title, every published version as a second-level section
      expect(content.startsWith(headings[locale]), locale).toBe(true);
      expect(content.match(/^# /gm)).toHaveLength(1);
      expect(content).not.toMatch(/^(title|summary|released): /m);
      expect(content).not.toContain("](./releases/");
      const versions = Array.from(content.matchAll(/^## v(\d+\.\d+\.\d+)$/gm), (m) => m[1]);
      // The fixture index orders these two eligible releases newest first.
      expect(versions.slice(0, 2), locale).toEqual(["1.5.65", "1.5.0"]);
      for (let index = 1; index < versions.length; index += 1) {
        expect(compareVersions(versions[index - 1], versions[index]), locale).toBeGreaterThan(0);
      }
      for (const version of versions) {
        expect(compareVersions(version, FIRST_PUBLISHED_RELEASE), version).toBeGreaterThanOrEqual(0);
      }
      // The maintainers' staging page never reaches readers
      expect(content).not.toMatch(/^## (Unreleased|Noch nicht veröffentlicht|À paraître)$/m);
    }
  });

  it("strips only a leading front-matter block", () => {
    expect(stripFrontMatter("---\ntitle: v1\n---\n\n# v1\n")).toBe("# v1\n");
    expect(stripFrontMatter("# Title\n\n---\n\nText")).toBe("# Title\n\n---\n\nText");
  });

  it("publishes only released pages from the first published version, including history and in index order", () => {
    const index = [
      "# Notes",
      "",
      "- [Unreleased](./releases/unreleased.en-US.md) - Staging.",
      "- [v1.10.0](./releases/v1.10.0.en-US.md) - Ten.",
      "- [v1.6.0](./releases/v1.6.0.en-US.md) - Six.",
      "- [v1.5.1](./releases/v1.5.1.en-US.md) - Five one.",
      "- [v1.5.0](./releases/v1.5.0.en-US.md) - Five.",
      "- [v0.72.2](./releases/v0.72.2.en-US.md) - Old.",
    ].join("\n");
    expect(publishedReleaseSources(index, "release-notes/index.en-US.md")).toEqual([
      "release-notes/releases/v1.10.0.en-US.md",
      "release-notes/releases/v1.6.0.en-US.md",
      "release-notes/releases/v1.5.1.en-US.md",
      "release-notes/releases/v1.5.0.en-US.md",
    ]);
    expect(compareVersions("1.10.0", "1.9.9")).toBeGreaterThan(0);
    expect(compareVersions("1.5", "1.5.0")).toBe(0);
  });

  it("keeps internal markers out of the published release notes", () => {
    // Obvious implementation details: flags, environment variables, source paths, module files, internal ids,
    // commit hashes and developer sections. The notes are read by users and administrators.
    const internalMarkers: Array<[string, RegExp]> = [
      ["feature flag", /\bFEATURE_[A-Z0-9_]+/],
      ["environment variable", /\bBOWHEAD_[A-Z0-9_]+/],
      ["source path", /\bpackages\/|\bdocs\/|\bsrc\//],
      ["source file", /\.(py|tsx?|ya?ml|sql)\b/],
      ["internal id", /`[a-z]+_[a-z0-9_]+`|\bws_[a-z0-9]/],
      ["commit hash", /\b(?=[0-9a-f]*\d)(?=[0-9a-f]*[a-f])[0-9a-f]{7,40}\b/],
      ["developer section", /^#+ .*(Developer|开发者|Entwicklung|Développement)/m],
    ];
    expect(HELP_ROOT).toBeDefined();
    for (const locale of ["zh-CN", "en-US", "de-DE", "fr-FR"] as const) {
      const notes = loadReleaseNotes(undefined, locale);
      const content = notes?.content ?? "";
      for (const [name, pattern] of internalMarkers) {
        expect(content.match(pattern)?.[0], `${locale}: ${name}`).toBeUndefined();
      }
    }
  });

  it("fails the build when a declared source file is missing", () => {
    const root = mkdtempSync(path.join(tmpdir(), "bowhead-help-"));
    mkdirSync(path.join(root, "permissions"));
    writeFileSync(path.join(root, "permissions.zh-CN.md"), "# only one file\n");

    expect(() => loadHelpArticles(root)).toThrow();
  });
});
