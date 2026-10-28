"use client";

import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { useFormatter, useTranslations } from "next-intl";
import { unified } from "unified";
import remarkParse from "remark-parse";
import type { Parent, Root, RootContent } from "mdast";
import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";

import { AppShell } from "@/components/shell/AppShell";
import { Button } from "@/components/ui/button";
import { Topbar } from "@/components/shell/Topbar";
import { withUiHooks } from "@/components/settings/ui-hook-classes";
import { appPath } from "@/lib/base-path";
import type { HelpArticle } from "@/lib/help-content";
import { shouldHandleAppLinkClick } from "@/lib/navigation-click";
import { useAppRouter } from "@/lib/use-app-router";

import moduleStyles from "./help-center-page.module.css";

import releaseStyles from "./release-notes-page.module.css";

const RELEASE_BATCH_SIZE = 3;
const DEFAULT_RELEASE_LIMIT = 10;

const styles = withUiHooks(moduleStyles, "ui-help");

interface NoteSection {
  id: string;
  title: string;
  text: string;
  offset: number;
  children: NoteSection[];
}

function nodeText(node: RootContent): string {
  if ("value" in node) return node.value;
  if ("children" in node)
    return node.children.map((child) => nodeText(child)).join("");
  return "";
}

function releaseNavigation(content: string): NoteSection[] {
  const nodes = unified()
    .use(remarkParse)
    .use(remarkGfm)
    .parse(content).children;
  const versions: NoteSection[] = [];
  let current: NoteSection | undefined;
  let section: NoteSection | undefined;
  for (const node of nodes) {
    if (
      node.type === "heading" &&
      (node.depth === 2 || (node.depth === 3 && current))
    ) {
      const title = nodeText(node);
      const version = node.depth === 2;
      const item: NoteSection = {
        id: version
          ? `release-${title.toLowerCase().replace(/[^a-z0-9]+/g, "-")}`
          : `${current!.id}-section-${current!.children.length + 1}`,
        title,
        text: title,
        offset: node.position!.start.offset!,
        children: [],
      };
      if (version) {
        versions.push(item);
        current = item;
        section = undefined;
      } else {
        current!.children.push(item);
        current!.text += ` ${title}`;
        section = item;
      }
    } else if (current) {
      const text = nodeText(node);
      current.text += ` ${text}`;
      if (section) section.text += ` ${text}`;
    }
  }
  return versions;
}

function searchSnippet(text: string, query: string): string {
  const index = text.toLowerCase().indexOf(query);
  const start = Math.max(0, index - 35);
  return `${start ? "…" : ""}${text.slice(start, start + 140)}${text.length > start + 140 ? "…" : ""}`;
}

function highlightSearch(query: string) {
  return () => (tree: Root) => {
    if (!query) return;
    const walk = (parent: Parent) => {
      parent.children = parent.children.flatMap((node): RootContent[] => {
        if ("children" in node) walk(node);
        if (node.type !== "text") return [node];
        const parts: RootContent[] = [];
        const lower = node.value.toLowerCase();
        let start = 0;
        let match = lower.indexOf(query);
        while (match >= 0) {
          parts.push({ type: "text", value: node.value.slice(start, match) });
          parts.push({
            type: "emphasis",
            data: { hName: "mark" },
            children: [
              {
                type: "text",
                value: node.value.slice(match, match + query.length),
              },
            ],
          });
          start = match + query.length;
          match = lower.indexOf(query, start);
        }
        parts.push({ type: "text", value: node.value.slice(start) });
        return parts;
      });
    };
    walk(tree);
  };
}

export function ReleaseNotesPage({ article }: { article: HelpArticle }) {
  const router = useAppRouter();
  const t = useTranslations("release_notes");
  const format = useFormatter();
  const [targetId, setTargetId] = useState("");
  const articleRef = useRef<HTMLElement>(null);
  const beforeSearch = useRef<{
    article: number;
    count: number;
    main: number;
    page: number;
    sidebar: number;
    active: string;
    hash: string;
  } | null>(null);
  const [search, setSearch] = useState("");
  const [activeId, setActiveId] = useState("");
  const [fittedCount, setFittedCount] = useState(1);
  const [loadedCount, setLoadedCount] = useState(0);
  const visibleCount = Math.max(fittedCount, loadedCount);
  const sidebarRef = useRef<HTMLElement>(null);
  const navigationRef = useRef<HTMLElement>(null);
  const statusRef = useRef<HTMLParagraphElement>(null);
  const versions = useMemo(
    () => releaseNavigation(article.content),
    [article.content],
  );
  const query = search.trim().toLowerCase();
  const matches = versions.filter((version, index) =>
    query
      ? version.text.toLowerCase().includes(query)
      : index < Math.max(visibleCount, DEFAULT_RELEASE_LIMIT),
  );
  const { content, headings } = useMemo(() => {
    let content = article.content.slice(0, versions[0]?.offset);
    const headings = new Map<number, string>();
    for (const [index, version] of versions.entries()) {
      if (
        query
          ? !version.text.toLowerCase().includes(query)
          : index >= visibleCount
      )
        continue;
      for (const heading of [version, ...version.children]) {
        headings.set(
          content.length + heading.offset - version.offset,
          heading.id,
        );
      }
      content += article.content.slice(
        version.offset,
        versions[index + 1]?.offset,
      );
    }
    return { content, headings };
  }, [article.content, versions, visibleCount, query]);
  const highlight = useMemo(() => highlightSearch(query), [query]);
  const releaseDate = (title: string) => {
    const date = article.releaseDates?.[title];
    return date ? (
      <time dateTime={date}>
        {t("released_on", {
          date: format.dateTime(new Date(`${date}T00:00:00Z`), {
            dateStyle: "medium",
            timeZone: "UTC",
          }),
        })}
      </time>
    ) : null;
  };

  const headingLookup = useRef(headings);
  headingLookup.current = headings;
  // Stable renderer types keep heading DOM nodes and browser reading anchors intact.
  const markdownComponents = useMemo<Components>(
    () => ({
      h2({ node, children, ...props }) {
        const id = headingLookup.current.get(
          node?.position?.start.offset ?? -1,
        );
        const version = versions.find((version) => version.id === id);
        return (
          <>
            <h2 {...props} id={id} tabIndex={-1}>
              {children}
            </h2>
            {version && article.releaseDates?.[version.title] ? (
              <p className={releaseStyles.date}>{releaseDate(version.title)}</p>
            ) : null}
          </>
        );
      },
      h3({ node, children, ...props }) {
        return (
          <h3
            {...props}
            id={headingLookup.current.get(node?.position?.start.offset ?? -1)}
            tabIndex={-1}
          >
            {children}
          </h3>
        );
      },
      a({ href = "", children, ...props }) {
        const internal = href.startsWith("/") && !href.startsWith("//");
        const external = /^https?:\/\//i.test(href);
        return (
          <a
            {...props}
            href={internal ? appPath(href) : href}
            target={external ? "_blank" : undefined}
            rel={external ? "noreferrer" : undefined}
            onClick={
              internal
                ? (event) => {
                    if (!shouldHandleAppLinkClick(event)) return;
                    event.preventDefault();
                    router.push(href);
                  }
                : undefined
            }
          >
            {children}
          </a>
        );
      },
      img({ src = "", alt = "", ...props }) {
        const source = typeof src === "string" ? src : "";
        return (
          // Release images are build-owned assets with authored alternative text.
          // eslint-disable-next-line @next/next/no-img-element
          <img
            {...props}
            src={source.startsWith("/") ? appPath(source) : source}
            alt={alt}
          />
        );
      },
      table({ children, ...props }) {
        return (
          <div className={styles.tableScroll} tabIndex={0}>
            <table {...props}>{children}</table>
          </div>
        );
      },
    }),
    [versions, article.releaseDates, format, t, router],
  );

  const changeSearch = (value: string) => {
    if (!query && value.trim()) {
      beforeSearch.current = {
        article: articleRef.current?.scrollTop ?? 0,
        count: visibleCount,
        main: articleRef.current?.closest("main")?.scrollTop ?? 0,
        page: document.scrollingElement?.scrollTop ?? 0,
        sidebar: sidebarRef.current?.scrollTop ?? 0,
        active: activeId,
        hash: window.location.hash,
      };
    }
    setSearch(value);
  };

  useLayoutEffect(() => {
    const sidebar = sidebarRef.current;
    const navigation = navigationRef.current;
    const loadMore = navigation?.querySelector("button");
    const status = statusRef.current;
    if (query || !sidebar || !navigation || !loadMore || !status) return;
    const candidates = Array.from(
      navigation.querySelectorAll<HTMLElement>("[data-release-version]"),
    ).slice(0, DEFAULT_RELEASE_LIMIT);
    const measure = () => {
      if (!sidebar.clientHeight) return;
      const available =
        sidebar.clientHeight -
        (parseFloat(getComputedStyle(sidebar).paddingBottom) || 0) -
        (navigation.getBoundingClientRect().top -
          sidebar.getBoundingClientRect().top +
          sidebar.scrollTop) -
        status.getBoundingClientRect().height -
        (parseFloat(getComputedStyle(status).marginTop) || 0);
      const buttonHeight =
        loadMore.getBoundingClientRect().height +
        (parseFloat(getComputedStyle(loadMore).marginTop) || 0);
      let height = 0;
      let count = 0;
      for (const [index, candidate] of candidates.entries()) {
        // Hidden groups have zero height, but their final child keeps its natural layout.
        height +=
          candidate.lastElementChild!.getBoundingClientRect().bottom -
          candidate.getBoundingClientRect().top +
          (parseFloat(getComputedStyle(candidate).marginTop) || 0);
        const reserve = index + 1 < versions.length ? buttonHeight : 0;
        if (height + reserve <= available) count = index + 1;
      }
      setFittedCount(Math.max(1, count));
    };
    measure();
    const observer = new ResizeObserver(measure);
    for (const element of [
      sidebar,
      loadMore,
      status,
      ...candidates.flatMap((candidate) => Array.from(candidate.children)),
    ])
      observer.observe(element);
    return () => observer.disconnect();
  }, [versions, query]);

  useEffect(() => {
    const syncHash = (initial = false) => {
      const id = window.location.hash.slice(1);
      setActiveId(id);
      const target = document.getElementById(id);
      // Native hash navigation already positions visible targets; only defer hidden targets.
      setTargetId(
        initial || !target || !articleRef.current?.contains(target) ? id : "",
      );
      const index = versions.findIndex(
        (version) =>
          version.id === id ||
          version.children.some((section) => section.id === id),
      );
      if (index >= 0 && !beforeSearch.current)
        setLoadedCount((count) => Math.max(count, index + 1));
    };
    syncHash(true);
    const onHashChange = () => syncHash();
    window.addEventListener("hashchange", onHashChange);
    return () => window.removeEventListener("hashchange", onHashChange);
  }, [versions]);

  useLayoutEffect(() => {
    if (!targetId) return;
    const target = document.getElementById(targetId);
    if (target && articleRef.current?.contains(target)) {
      target.scrollIntoView();
      setTargetId("");
    }
  }, [content, targetId]);

  useLayoutEffect(() => {
    const article = articleRef.current;
    const main = article?.closest("main");
    if (!article || !main) return;
    if (query) {
      article.scrollTop = 0;
      main.scrollTop = 0;
      if (document.scrollingElement) document.scrollingElement.scrollTop = 0;
    } else if (beforeSearch.current) {
      const saved = beforeSearch.current;
      article.scrollTop = saved.article;
      main.scrollTop = saved.main;
      if (document.scrollingElement)
        document.scrollingElement.scrollTop = saved.page;
      if (sidebarRef.current) sidebarRef.current.scrollTop = saved.sidebar;
      setActiveId(saved.active);
      setLoadedCount((count) => Math.max(count, saved.count));
      setTargetId("");
      const url = new URL(window.location.href);
      url.hash = saved.hash;
      window.history.replaceState(window.history.state, "", url);
      beforeSearch.current = null;
    }
  }, [query]);

  useEffect(() => {
    const article = articleRef.current;
    const main = article?.closest("main");
    if (!article || !main) return;
    let frame = 0;
    const update = (preserve = false) => {
      frame = 0;
      const root =
        getComputedStyle(article).overflowY === "visible"
          ? main.scrollHeight > main.clientHeight
            ? main
            : (document.scrollingElement ?? main)
          : article;
      const top =
        Math.max(
          0,
          root.getBoundingClientRect().top,
          ...Array.from(
            document.querySelectorAll(".v2-mobile-topbar, .v2-main-head"),
            (element) => element.getBoundingClientRect().bottom,
          ),
        ) + 24;
      const nodes = Array.from(
        article.querySelectorAll<HTMLElement>("h2[id], h3[id]"),
      );
      let current = nodes[0];
      for (const node of nodes) {
        // Native anchor alignment can differ by a fractional pixel.
        if (node.getBoundingClientRect().top > top + 1) break;
        current = node;
      }
      if (
        root.scrollTop > 0 &&
        root.scrollTop + root.clientHeight >= root.scrollHeight - 1
      )
        current = nodes[nodes.length - 1];
      if (!current) return;
      setActiveId(current.id);
      if (preserve && !query) {
        const index = versions.findIndex(
          (version) =>
            version.id === current.id ||
            version.children.some((section) => section.id === current.id),
        );
        setLoadedCount((count) => Math.max(count, index + 1));
      }
    };
    const schedule = (event: Event) => {
      if (
        event.target instanceof Node &&
        sidebarRef.current?.contains(event.target)
      )
        return;
      if (!frame) frame = requestAnimationFrame(() => update(true));
    };
    update();
    document.addEventListener("scroll", schedule, {
      capture: true,
      passive: true,
    });
    return () => {
      document.removeEventListener("scroll", schedule, true);
      cancelAnimationFrame(frame);
    };
  }, [content, versions, query]);
  return (
    <AppShell
      mobileTitle={article.title}
      preferCompactSidebar
      mainClassName={`v2-main ${styles.main}`}
      topbar={
        <Topbar titleSlot={<span className="title">{article.title}</span>} />
      }
    >
      <div className={styles.page}>
        <aside
          ref={sidebarRef}
          className={`${styles.sidebar} ${releaseStyles.sidebar}`}
        >
          <label
            className={releaseStyles.searchLabel}
            htmlFor="release-notes-search"
          >
            {t("search")}
          </label>
          <input
            id="release-notes-search"
            className={releaseStyles.search}
            type="search"
            placeholder={t("search_placeholder")}
            value={search}
            onChange={(event) => changeSearch(event.target.value)}
          />
          <nav
            ref={navigationRef}
            aria-label={t("navigation")}
            className={releaseStyles.navigation}
          >
            {matches.map((version, index) => {
              const measuring = !query && index >= visibleCount;
              const titleMatches = version.title.toLowerCase().includes(query);
              const sections = version.children.filter(
                (section) =>
                  !query ||
                  titleMatches ||
                  section.text.toLowerCase().includes(query),
              );
              return (
                <div
                  key={version.id}
                  className={releaseStyles.version}
                  data-release-version
                  data-measuring={measuring}
                  aria-hidden={measuring || undefined}
                >
                  <a
                    className={releaseStyles.versionLink}
                    href={`#${version.id}`}
                    aria-current={
                      activeId === version.id ? "location" : undefined
                    }
                  >
                    {version.title}
                  </a>
                  {article.releaseDates?.[version.title] ? (
                    <div className={releaseStyles.date}>
                      {releaseDate(version.title)}
                    </div>
                  ) : null}
                  {query && !titleMatches && sections.length === 0 ? (
                    <p className={releaseStyles.snippet}>
                      {searchSnippet(version.text, query)}
                    </p>
                  ) : null}
                  <ul>
                    {sections.map((section) => (
                      <li key={section.id}>
                        <a
                          href={`#${section.id}`}
                          aria-current={
                            activeId === section.id ? "location" : undefined
                          }
                        >
                          {section.title}
                          {query && !titleMatches ? (
                            <span className={releaseStyles.snippet}>
                              {searchSnippet(section.text, query)}
                            </span>
                          ) : null}
                        </a>
                      </li>
                    ))}
                  </ul>
                </div>
              );
            })}
            <Button
              data-measuring={!!query || visibleCount >= versions.length}
              aria-hidden={
                !!query || visibleCount >= versions.length || undefined
              }
              size="sm"
              variant="ghost"
              className={releaseStyles.loadMore}
              onClick={() => setLoadedCount(visibleCount + RELEASE_BATCH_SIZE)}
            >
              {t("load_more")}
            </Button>
          </nav>
          <p ref={statusRef} role="status" className={releaseStyles.status}>
            {query
              ? matches.length
                ? t("results", { count: matches.length })
                : t("no_results")
              : visibleCount >= versions.length
                ? t("all_loaded", { total: versions.length })
                : t("loaded", {
                    count: Math.min(visibleCount, versions.length),
                    total: versions.length,
                  })}
          </p>
        </aside>
        <article
          ref={articleRef}
          className={`${styles.article} ${releaseStyles.article}`}
        >
          {query && matches.length === 0 ? <p>{t("no_results")}</p> : null}
          <ReactMarkdown
            remarkPlugins={[remarkGfm, highlight]}
            components={markdownComponents}
          >
            {content}
          </ReactMarkdown>
        </article>
      </div>
    </AppShell>
  );
}
