"use client";

import { PRODUCT_NAME } from "@/lib/product-identity";

import {
  Children,
  isValidElement,
  type MouseEvent as ReactMouseEvent,
  type ReactNode,
} from "react";
import { useSearchParams } from "next/navigation";
import { useTranslations } from "next-intl";
import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";

import {
  ArrowRight,
  Book,
  BookOpen,
  Bot,
  Chat,
  Database,
  Shield,
  Spark,
  Users,
} from "@/components/icons";
import { useBrand } from "@/components/brand-provider";
import { AppShell } from "@/components/shell/AppShell";
import { Topbar } from "@/components/shell/Topbar";
import { withUiHooks } from "@/components/settings/ui-hook-classes";
import { Button } from "@/components/ui/button";
import {
  formatVersionDetails,
  formatVersionDetailsInline,
  useAppVersionInfo,
  versionLabel,
} from "@/lib/app-version";
import { useOptionalAuthSession } from "@/lib/auth/auth-session-context";
import type { MeUser } from "@/lib/auth-api";
import { appPath } from "@/lib/base-path";
import { copyTextWithFallback } from "@/lib/clipboard";
import type { HelpArticle, HelpArticleGroup } from "@/lib/help-content";
import { shouldHandleAppLinkClick } from "@/lib/navigation-click";
import { useAppRouter } from "@/lib/use-app-router";
import { toast } from "@/lib/use-toast";

import moduleStyles from "./help-center-page.module.css";
import { MermaidDiagram } from "./mermaid-diagram";

// UI refresh (page-by-page acceptance cleanup): append stable global ui-help-* hooks to module classes; new appearance lives in
// styles/ui-refresh-pages-main.css, “tail:help”.
const styles = withUiHooks(moduleStyles, "ui-help");

interface HelpCenterPageProps {
  articles: HelpArticle[];
}

const GROUPS: HelpArticleGroup[] = [
  "getting_started",
  "interface",
  "team_access",
  "agents_skills",
  "security_data",
  "reference",
];

const HOME_ROUTES = ["getting-started", "space-access", "publish-agent", "security-sandbox"];

/** Help › About: the version card replaces this marker between the article's introduction and its sections. */
const ABOUT_ARTICLE_ID = "about";
const ABOUT_VERSION_MARKER = "<!-- about-version -->";
/** Generated at build time from the repository's THIRD_PARTY_NOTICES.md (scripts/copy-legal-notices.mjs). */
export const THIRD_PARTY_NOTICES_PATH = "/legal/third-party-notices.txt";

/**
 * Version card on Help › About (P2 batch 6): application name and version, web bundle and server
 * versions, a copy button (same text as the account menu's version line), and links to the release notes
 * and the third-party licenses.
 */
function AboutVersionCard({
  onNavigate,
}: {
  onNavigate: (event: ReactMouseEvent<HTMLAnchorElement>, href: string) => void;
}) {
  const t = useTranslations("help_center.about");
  const tMenu = useTranslations("sidebar.user_menu");
  const { brand } = useBrand();
  const info = useAppVersionInfo();
  const releaseNotesHref = "/release-notes/";
  const copy = () => {
    void copyTextWithFallback(
      formatVersionDetails(brand.applicationName, info),
      tMenu("version_copy_prompt"),
      formatVersionDetailsInline(brand.applicationName, info),
    ).then((copied) => {
      if (copied) toast({ kind: "success", text: tMenu("version_copied") });
    });
  };
  const serverText =
    info.server ?? (info.serverUnavailable ? t("server_unavailable") : t("server_loading"));
  return (
    <section className="ui-help-about" aria-label={t("label")}>
      <dl>
        <dt>{t("version")}</dt>
        <dd translate="no">{versionLabel(brand.applicationName, info.web)}</dd>
        <dt>{t("web")}</dt>
        <dd translate="no">
          {info.web}
          {info.build ? ` · ${t("build", { locale: info.build })}` : ""}
        </dd>
        <dt>{t("server")}</dt>
        <dd translate="no" aria-live="polite">{serverText}</dd>
      </dl>
      {info.server && info.server !== info.web ? (
        <p className="ui-help-about-note">{t("mismatch")}</p>
      ) : null}
      <div className="ui-help-about-actions">
        <Button size="sm" onClick={copy}>
          {t("copy")}
        </Button>
        <a
          href={appPath(releaseNotesHref)}
          onClick={(event) => onNavigate(event, releaseNotesHref)}
        >
          {t("release_notes")}
        </a>
        <a href={appPath(THIRD_PARTY_NOTICES_PATH)} target="_blank" rel="noreferrer">
          {t("third_party")}
        </a>
      </div>
    </section>
  );
}

const APPLICATION_NAME_PLACEHOLDER = "{applicationName}";

/**
 * Fills the `{applicationName}` placeholder (used by Help › About) with the team's application name, so the
 * product is named the way the team brands it. Articles without the placeholder are returned unchanged.
 */
export function withApplicationName(articles: HelpArticle[], applicationName: string): HelpArticle[] {
  const name = applicationName.trim() || PRODUCT_NAME;
  const fill = (text: string) => text.split(APPLICATION_NAME_PLACEHOLDER).join(name);
  return articles.map((article) =>
    [article.title, article.description, article.content].some((text) =>
      text.includes(APPLICATION_NAME_PLACEHOLDER),
    )
      ? {
          ...article,
          title: fill(article.title),
          description: fill(article.description),
          content: fill(article.content),
        }
      : article,
  );
}

/**
 * Drops articles whose required `/me` capability is not reported as true, so a gated
 * article is absent from navigation, home cards, and in-article link targets, and a deep
 * link to it falls through to the same state as an unknown article id.
 */
export function visibleHelpArticles(
  articles: HelpArticle[],
  capabilities: MeUser["capabilities"] | null | undefined,
): HelpArticle[] {
  return articles.filter(
    ({ requiresCapability }) =>
      !requiresCapability || capabilities?.[requiresCapability] === true,
  );
}

export function HelpCenterPage({ articles: allArticles }: HelpCenterPageProps) {
  const t = useTranslations("help_center");
  const capabilities = useOptionalAuthSession()?.me?.capabilities;
  const { brand } = useBrand();
  const articles = withApplicationName(
    visibleHelpArticles(allArticles, capabilities),
    brand.applicationName,
  );
  const searchParams = useSearchParams();
  const router = useAppRouter();
  const requestedArticle = searchParams.get("article") ?? "home";
  const activeArticle =
    articles.find((article) => article.id === requestedArticle) ?? articles[0];

  if (!activeArticle) return null;

  const homeArticles = HOME_ROUTES.map((id) =>
    articles.find((article) => article.id === id),
  ).filter((article): article is HelpArticle => Boolean(article));
  const articleById = new Map(articles.map((article) => [article.id, article]));

  const navigateInsideHelp = (
    event: ReactMouseEvent<HTMLAnchorElement>,
    href: string,
  ) => {
    if (!shouldHandleAppLinkClick(event)) return;
    event.preventDefault();
    router.push(href);
  };

  const markdownComponents: Components = {
    a({ href = "", children, ...props }) {
      const isHelpLink = href.startsWith("/help/") || href.startsWith("/release-notes/");
      const isExternal = /^https?:\/\//i.test(href);
      return (
        <a
          {...props}
          href={isHelpLink ? appPath(href) : href}
          target={isExternal ? "_blank" : undefined}
          rel={isExternal ? "noreferrer" : undefined}
          onClick={
            isHelpLink
              ? (event) => navigateInsideHelp(event, href)
              : undefined
          }
        >
          {children}
        </a>
      );
    },
    img({ src = "", alt = "", ...props }) {
      const imageSource = typeof src === "string" ? src : "";
      return (
        // Help images are build-owned content and keep their authored alt text.
        // eslint-disable-next-line @next/next/no-img-element
        <img
          {...props}
          src={
            imageSource.startsWith("/")
              ? appPath(imageSource)
              : imageSource
          }
          alt={alt}
        />
      );
    },
    pre({ children }) {
      const child = Children.toArray(children)[0];
      if (
        isValidElement<{
          className?: string;
          children?: ReactNode;
        }>(child) &&
        child.props.className === "language-mermaid"
      ) {
        return (
          <MermaidDiagram
            source={String(child.props.children ?? "").trim()}
            diagramLabel={t("diagram_label")}
            loadingLabel={t("diagram_loading")}
            errorLabel={t("diagram_error")}
          />
        );
      }
      return <pre>{children}</pre>;
    },
    table({ children, ...props }) {
      return (
        <div className={styles.tableScroll} tabIndex={0}>
          <table {...props}>{children}</table>
        </div>
      );
    },
  };
  const renderMarkdown = (content: string) => (
    <ReactMarkdown remarkPlugins={[remarkGfm]} components={markdownComponents}>
      {content}
    </ReactMarkdown>
  );
  // Help › About: the version card sits between the introduction and the sections (flag-gated article).
  const aboutParts =
    activeArticle.id === ABOUT_ARTICLE_ID
      ? activeArticle.content.split(ABOUT_VERSION_MARKER)
      : null;
  return (
    <AppShell
      mobileTitle={t("title")}
      preferCompactSidebar
      mainClassName={`v2-main ${styles.main}`}
      topbar={
        <Topbar
          titleSlot={<span className="title">{t("title")}</span>}
        />
      }
    >
      <div className={styles.page}>
        <aside className={styles.sidebar}>
          <div className={styles.sidebarIntro}>
            <span className={styles.bookmark} aria-hidden="true">
              <BookOpen size={18} />
            </span>
            <div>
              <p className={styles.eyebrow}>{t("permissions_guide")}</p>
              <p className={styles.sidebarLead}>{t("sidebar_lead")}</p>
            </div>
          </div>
          <nav aria-label={t("article_navigation")}>
            <a
              href={appPath("/help/")}
              aria-current={activeArticle.id === "home" ? "page" : undefined}
              className={`${styles.homeLink} ${activeArticle.id === "home" ? styles.navItemActive : ""}`}
              onClick={(event) => navigateInsideHelp(event, "/help/")}
            >
              <span>{t("home")}</span>
              <small>{t("home_description")}</small>
            </a>
            {GROUPS.map((group) => {
              const groupArticles = articles.filter(
                (article) => article.group === group,
              );
              return (
                <section className={styles.navGroup} key={group}>
                  <h2>{t(`groups.${group}`)}</h2>
                  <div className={styles.navItems}>
                    {groupArticles.map((article) => {
                      const href = `/help/?article=${article.id}`;
                      const isActive = article.id === activeArticle.id;
                      return (
                        <a
                          key={article.id}
                          href={appPath(href)}
                          aria-current={isActive ? "page" : undefined}
                          className={`${styles.navItem} ${isActive ? styles.navItemActive : ""}`}
                          onClick={(event) =>
                            navigateInsideHelp(event, href)
                          }
                        >
                          <span>{article.title}</span>
                          <small>{article.description}</small>
                        </a>
                      );
                    })}
                  </div>
                </section>
              );
            })}
          </nav>
        </aside>

        <article className={styles.article}>
          {activeArticle.id === "home" ? (
            <>
              <section className={styles.homeHero} aria-labelledby="help-home-title">
                <div className={styles.heroCopy}>
                  <p className={styles.homeEyebrow}>{t("home_eyebrow")}</p>
                  {/* The H1 repeats the top-bar title so the large-title pattern shows it once. */}
                  <h1 id="help-home-title">{t("title")}</h1>
                  <p className={styles.homeTagline}>{t("home_tagline")}</p>
                  <p>{t("home_lead")}</p>
                  <a
                    className={styles.heroAction}
                    href={appPath("/help/?article=getting-started")}
                    onClick={(event) =>
                      navigateInsideHelp(event, "/help/?article=getting-started")
                    }
                  >
                    <Spark size={15} aria-hidden="true" />
                    {t("home_primary_action")}
                    <ArrowRight size={15} aria-hidden="true" />
                  </a>
                </div>
                <aside className={styles.heroPath} aria-label={t("home_path_label")}>
                  <p>{t("home_path_label")}</p>
                  {["home_path_one", "home_path_two", "home_path_three"].map(
                    (key, index) => (
                      <div className={styles.pathStep} key={key}>
                        <span>{String(index + 1).padStart(2, "0")}</span>
                        <p>{t(key)}</p>
                      </div>
                    ),
                  )}
                </aside>
              </section>

              <section className={styles.homeSection} aria-labelledby="help-goals-title">
                <div className={styles.sectionHeading}>
                  <p>{t("home_goals_eyebrow")}</p>
                  <h2 id="help-goals-title">{t("home_goals_title")}</h2>
                </div>
                <div className={styles.goalGrid}>
                  {homeArticles.map((article, index) => {
                    const href = `/help/?article=${article.id}`;
                    const Icon = [Chat, Users, Bot, Shield][index] ?? Book;
                    return (
                      <a
                        className={styles.goalCard}
                        href={appPath(href)}
                        key={article.id}
                        onClick={(event) => navigateInsideHelp(event, href)}
                      >
                        <span className={styles.goalIcon} aria-hidden="true">
                          <Icon size={17} />
                        </span>
                        <span className={styles.goalTitle}>{article.title}</span>
                        <small>{article.description}</small>
                        <ArrowRight size={15} aria-hidden="true" />
                      </a>
                    );
                  })}
                </div>
              </section>

              <section className={styles.homeSection} aria-labelledby="help-map-title">
                <div className={styles.sectionHeading}>
                  <p>{t("home_map_eyebrow")}</p>
                  <h2 id="help-map-title">{t("home_map_title")}</h2>
                </div>
                <div className={styles.mapGrid}>
                  {[
                    [Users, "home_map_team", "overview"],
                    [Bot, "home_map_agents", "publish-agent"],
                    [Database, "home_map_data", "troubleshoot-access"],
                    [Shield, "home_map_security", "security-sandbox"],
                  ].map(([Icon, key, id]) => {
                    const article = articleById.get(String(id));
                    const href = article ? `/help/?article=${article.id}` : "/help/";
                    const MapIcon = Icon as typeof Users;
                    return (
                      <a
                        className={styles.mapCard}
                        href={appPath(href)}
                        key={String(key)}
                        onClick={(event) => navigateInsideHelp(event, href)}
                      >
                        <MapIcon size={18} aria-hidden="true" />
                        <span>{t(`${key}.title`)}</span>
                        <small>{t(`${key}.description`)}</small>
                      </a>
                    );
                  })}
                </div>
              </section>
            </>
          ) : (
            <div className={styles.articleMeta}>
              <span>{t(`groups.${activeArticle.group ?? "reference"}`)}</span>
              <span aria-hidden="true">·</span>
              <span>{t("current_implementation")}</span>
            </div>
          )}
          {aboutParts ? (
            <>
              {renderMarkdown(aboutParts[0])}
              <AboutVersionCard onNavigate={navigateInsideHelp} />
              {renderMarkdown(aboutParts.slice(1).join(""))}
            </>
          ) : (
            renderMarkdown(activeArticle.content)
          )}
        </article>
      </div>
    </AppShell>
  );
}
