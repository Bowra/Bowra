"use client";

/**
 * UserMenu — account menu for the unified shell.
 *
 * Contents: identity, personal / space / team settings, role-filtered administration group (each
 * destination individually clickable, rather than a single entry), sign out.
 * Small screens (56px sidebar) lack room → floating popup implementation.
 */

import {
  useEffect,
  useRef,
  useState,
  type MouseEvent as ReactMouseEvent,
  type ReactNode,
} from "react";
import type { Placement } from "@floating-ui/react";
import { ChevronsUpDown, Gauge, ShieldCheck, Stamp } from "lucide-react";
import { useTranslations } from "next-intl";
import {
  BookOpen,
  History,
  LogOut,
  Sparkles,
  Terminal,
  UserIcon,
} from "@/components/icons";
import { useBrand } from "@/components/brand-provider";
import { lucideIcon } from "@/components/icon-refresh";
import {
  AnswerLanguageLinkItem,
  LanguageMenuEntry,
  useInterfaceLanguageSwitch,
  useSwitchingLabel,
} from "@/components/locale-switcher";
import { effectiveAnswerValue, useAnswerLanguageName } from "@/components/settings/answer-language-menu";
import { MenuFloating, useMenu, useMenuItem } from "@/components/ui/menu";
import {
  formatVersionDetails,
  formatVersionDetailsInline,
  useAppVersionInfo,
  versionLabel,
} from "@/lib/app-version";
import { appPath } from "@/lib/base-path";
import { copyTextWithFallback } from "@/lib/clipboard";
import { toast } from "@/lib/use-toast";
import { fetchMe, logoutUser, type MeUser } from "@/lib/auth-api";
import { useOptionalAuthSession } from "@/lib/auth/auth-session-context";
import {
  confirmCanvasNavigation,
  hasCanvasUnsavedChanges,
} from "@/lib/canvas-navigation-guard";
import { shouldHandleAppLinkClick } from "@/lib/navigation-click";
import { trackInteraction as track } from "@/lib/metrics";
import { useAppRouter } from "@/lib/use-app-router";
import { readLocaleCookie } from "@/lib/locale-names";
import { useLanguagePreferences } from "@/lib/use-language-preferences";
import { administrationDestinationsForUser } from "@/lib/navigation-centers";
import {
  TEAM_SETTINGS_ROLES,
  teamRoleForUser,
  type AdministrationDestinationId,
} from "@/lib/settings-navigation";

interface Props {
  initials?: string;
  user?: MeUser | null;
  showIdentity?: boolean;
  onBeforePersonalSettingsNavigate?: () => void;
}

function userDisplayName(me: MeUser): string {
  return me.display_name?.trim() || me.email.split("@")[0] || me.email;
}

/**
 * Second line of the sidebar identity row (P2 batch 3): "team · role" in system type; the team part is
 * left out when `/me` has no team name (older gateways), and the email stays in the account menu header and
 * the tooltip. `teamRoleForUser` falls back to "member".
 */
function identitySecondLine(
  me: MeUser | null | undefined,
  roleLabel: (role: NonNullable<ReturnType<typeof teamRoleForUser>>) => string,
): string {
  if (!me) return "";
  const role = teamRoleForUser(me);
  const team = me.team_name?.trim();
  return [team, role ? roleLabel(role) : ""].filter(Boolean).join(" · ") || me.email;
}

function userInitial(me: MeUser | null, fallback: string): string {
  const source = me ? userDisplayName(me) : fallback.trim();
  return Array.from(source)[0]?.toUpperCase() || "?";
}

/**
 * Open to sidebar's right, aligned with trigger bottom. If it cannot fit, useMenu common flip tries left,
 * then vertical sides (mobile drawer trigger nearly fills width, so neither horizontal side fits; open upward, UI redesign P0-5).
 */
export const USER_MENU_PLACEMENT: Placement = "right-end";
/** Settings › Account, scrolled to the answer-language row (deep link from the Language submenu). */
const ANSWER_LANGUAGE_ROUTE = "/settings/profile/#answer-language";
export const USER_MENU_OFFSET = 8;

function UserMenuLink({
  href,
  onSelect,
  children,
}: {
  href: string;
  onSelect: (event: ReactMouseEvent<HTMLElement>) => void;
  children: ReactNode;
}) {
  const itemProps = useMenuItem({ onSelect });
  return (
    <a
      href={href}
      className="app-floating-menu-item v2-user-menu-item"
      {...itemProps}
    >
      {children}
    </a>
  );
}

function SignOutItem({
  signingOut,
  onSignOut,
  children,
}: {
  signingOut: boolean;
  onSignOut: () => void;
  children: ReactNode;
}) {
  // Keep menu open during logout, show “正在退出”, and disable item.
  const itemProps = useMenuItem({
    onSelect: onSignOut,
    disabled: signingOut,
    closeOnSelect: false,
  });
  return (
    <button
      type="button"
      disabled={signingOut}
      className="app-floating-menu-item v2-user-menu-item is-danger"
      {...itemProps}
    >
      {children}
    </button>
  );
}

/**
 * Last line of the account menu (P2 batch 6, user decision 2026-10-01): replaces the
 * former sidebar version row. Shows "<application name> <version>" in small secondary text; activating
 * it copies the full version details (web bundle, locale build, server) and confirms with a Toast.
 * The visible label stays the start of the accessible name, followed by the action.
 */
function VersionMenuItem({ open }: { open: boolean }) {
  const t = useTranslations("sidebar.user_menu");
  const { brand } = useBrand();
  // Load the server version only while the menu is open, so copying has it ready.
  const info = useAppVersionInfo(open);
  const label = versionLabel(brand.applicationName, info.web);
  const itemProps = useMenuItem({
    onSelect: () => {
      void copyTextWithFallback(
        formatVersionDetails(brand.applicationName, info),
        t("version_copy_prompt"),
        formatVersionDetailsInline(brand.applicationName, info),
      ).then((copied) => {
        if (copied) toast({ kind: "success", text: t("version_copied") });
      });
    },
  });
  return (
    <button
      type="button"
      className="app-floating-menu-item v2-user-menu-item v2-user-menu-version"
      title={t("version_copy_hint")}
      {...itemProps}
    >
      <span translate="no">{label}</span>
      <span className="unified-work-sr-only">{`, ${t("version_copy_hint")}`}</span>
    </button>
  );
}

/**
 * Administration destination icons follow the design navigation vocabulary (P2 batch 3):
 * approvals = stamp, credits = gauge, access = shield-check.
 */
const ApprovalsIcon = lucideIcon(Stamp, 16);
const CreditsIcon = lucideIcon(Gauge, 16, { sized: false });
const AccessIcon = lucideIcon(ShieldCheck, 11);
/** Identity row chevron: up/down chevrons (14). */
const IdentityCaret = lucideIcon(ChevronsUpDown, 14, { sized: false });

/**
 * Administration destination icons. Deliberately exhaustive switch, rather than lookup with default:
 * adding ADMINISTRATION_DESTINATIONS without an icon fails compilation,
 * rather than silently rendering an empty icon box.
 */
export function administrationIcon(id: AdministrationDestinationId) {
  switch (id) {
    case "traces":
      return <Terminal size={17} />;
    case "approvals":
      return <ApprovalsIcon size={17} />;
    case "credits":
      return <CreditsIcon />;
    case "audit":
      return <History size={17} />;
    case "access":
      return <AccessIcon size={17} />;
  }
}

export function UserMenu({
  initials = "N",
  user,
  showIdentity = false,
  onBeforePersonalSettingsNavigate,
}: Props) {
  const t = useTranslations("sidebar");
  const roleT = useTranslations("settings.access_boundary.roles");
  const router = useAppRouter();
  const authSession = useOptionalAuthSession();
  const [open, setOpen] = useState(false);
  const [me, setMe] = useState<MeUser | null>(null);
  const [signingOut, setSigningOut] = useState(false);
  const identityAttemptedRef = useRef(false);
  const resolvedMe =
    user === undefined ? (authSession?.me ?? me) : user;

  const menu = useMenu({
    open,
    onOpenChange: (next) => {
      // After identity-load failure, retry only on reopening; closing makes no request.
      if (next && user === undefined && !me) {
        identityAttemptedRef.current = false;
      }
      setOpen(next);
    },
    placement: USER_MENU_PLACEMENT,
    offset: USER_MENU_OFFSET,
  });

  // The closed trigger also renders account identity, so load once on mount. After a
  // failure, retry only on a fresh open transition; closing the menu must not issue I/O.
  useEffect(() => {
    if (
      authSession ||
      user !== undefined ||
      me ||
      identityAttemptedRef.current
    ) {
      return;
    }
    identityAttemptedRef.current = true;
    let cancelled = false;
    void fetchMe()
      .then((u) => {
        if (!cancelled) setMe(u);
      })
      .catch(() => {
        // Unauthenticated/failure → leave me null, show guidance in popup.
      });
    return () => {
      cancelled = true;
    };
  }, [authSession, open, me, user]);

  const handleSignOut = async () => {
    setSigningOut(true);
    try {
      await logoutUser();
    } catch {
      // Even if backend logout fails, frontend tries login navigation (refresh fallback if local cookie cannot be cleared server-side).
    }
    if (typeof window !== "undefined") {
      window.location.href = appPath("/login/");
    }
  };

  const handleRoute = async (
    event: ReactMouseEvent<HTMLElement>,
    route: string,
    scope?: "personal" | "space" | "team",
  ) => {
    if (!shouldHandleAppLinkClick(event)) return;
    event.preventDefault();
    if (
      hasCanvasUnsavedChanges() &&
      !(await confirmCanvasNavigation())
    ) {
      return;
    }
    if (scope) {
      track("bowhead_settings_opened_total", {
        surface: "user_menu",
        scope,
      });
    }
    router.push(route);
  };

  // Language (Header, Language & Paging Spec §1a): the saved interface preference drives the check; until the
  // account settings load, a NEXT_LOCALE cookie means a concrete choice and its absence means "Automatic".
  const languagePrefs = useLanguagePreferences(open);
  const savedUiLanguage =
    languagePrefs.status === "ready" ? languagePrefs.ui : (readLocaleCookie() ?? "auto");
  const { switching, switchTo } = useInterfaceLanguageSwitch({
    saved: savedUiLanguage,
    onSaved: languagePrefs.rememberUiLanguage,
  });
  const switchingLabel = useSwitchingLabel(switching);
  const answerLanguageName = useAnswerLanguageName();
  const answerLanguageValue =
    languagePrefs.status === "ready"
      ? answerLanguageName(effectiveAnswerValue(languagePrefs.output, languagePrefs.teamLock))
      : "";

  const administrationDestinations = administrationDestinationsForUser(resolvedMe);
  const teamRole = teamRoleForUser(resolvedMe);
  const canManageTeam = Boolean(
    teamRole && TEAM_SETTINGS_ROLES.includes(teamRole),
  );
  const showTeamOnboarding =
    canManageTeam && Boolean(resolvedMe?.capabilities?.team_onboarding);

  return (
    <>
      <button
        ref={menu.refs.setReference}
        type="button"
        className={
          showIdentity ? "unified-sidebar-user" : "v2-sb-avatar"
        }
        title={resolvedMe?.email ?? "User"}
        aria-label={t("account")}
        {...menu.getReferenceProps()}
      >
        {showIdentity ? (
          <>
            <span className="v2-sb-avatar" aria-hidden="true">
              {userInitial(resolvedMe, initials)}
            </span>
            <span className="unified-sidebar-user-copy">
              <strong>
                {resolvedMe
                  ? userDisplayName(resolvedMe)
                  : t("user_menu.loading_user")}
              </strong>
              <small>
                {switchingLabel ??
                  (identitySecondLine(resolvedMe, roleT) ||
                    t("user_menu.loading_user"))}
              </small>
            </span>
            <IdentityCaret className="unified-sidebar-caret" aria-hidden="true" />
          </>
        ) : (
          userInitial(resolvedMe, initials)
        )}
      </button>
      {/* Announced once while the interface language switches (the menu has already closed). */}
      <span role="status" aria-live="polite" className="unified-work-sr-only">
        {switchingLabel ?? ""}
      </span>
      <MenuFloating menu={menu}>
          <div
            ref={menu.refs.setFloating}
            data-user-menu="true"
            {...menu.getFloatingProps({
              "aria-label": t("account"),
              className: "app-floating-menu v2-user-menu",
            })}
          >
            <div className="v2-user-menu-header">
              {resolvedMe ? (
                <>
                  <span className="v2-user-menu-avatar" aria-hidden="true">
                    {userInitial(resolvedMe, initials)}
                  </span>
                  <span className="v2-user-menu-identity">
                    <strong>{userDisplayName(resolvedMe)}</strong>
                    <small>
                      {teamRole ? roleT(teamRole) : resolvedMe.email}
                    </small>
                  </span>
                </>
              ) : (
                <div className="v2-user-menu-loading">
                  {t("user_menu.loading_user")}
                </div>
              )}
            </div>
            <div className="v2-user-menu-section">
              <UserMenuLink
                href={appPath("/settings/profile/")}
                onSelect={(event) => {
                  onBeforePersonalSettingsNavigate?.();
                  void handleRoute(event, "/settings/profile/", "personal");
                }}
              >
                <span className="v2-user-menu-icon" aria-hidden="true">
                  <UserIcon size={17} />
                </span>
                <span>{t("user_menu.settings")}</span>
              </UserMenuLink>
              {showTeamOnboarding ? (
                <UserMenuLink
                  href={appPath("/setup/")}
                  onSelect={(event) =>
                    void handleRoute(event, "/setup/")
                  }
                >
                  <span className="v2-user-menu-icon" aria-hidden="true">
                    <Sparkles size={17} />
                  </span>
                  <span>{t("user_menu.team_onboarding")}</span>
                </UserMenuLink>
              ) : null}
            </div>
            {administrationDestinations.length ? (
              <div className="v2-user-menu-section">
                <UserMenuLink href={appPath("/admin/")} onSelect={(event) => void handleRoute(event, "/admin/")}>
                  <span className="v2-user-menu-icon" aria-hidden="true"><AccessIcon size={17} /></span>
                  <span>{t("user_menu.administration")}</span>
                </UserMenuLink>
              </div>
            ) : null}
            <div className="v2-user-menu-section">
              <UserMenuLink
                href={appPath("/help/")}
                onSelect={(event) => void handleRoute(event, "/help/")}
              >
                <span className="v2-user-menu-icon" aria-hidden="true">
                  <BookOpen size={17} />
                </span>
                <span>{t("user_menu.help_center")}</span>
              </UserMenuLink>
              <UserMenuLink
                href={appPath("/release-notes/")}
                onSelect={(event) => void handleRoute(event, "/release-notes/")}
              >
                <span className="v2-user-menu-icon" aria-hidden="true">
                  <History size={17} />
                </span>
                <span>{t("user_menu.release_notes")}</span>
              </UserMenuLink>
              <LanguageMenuEntry
                saved={savedUiLanguage}
                onPick={(next) => void switchTo(next)}
                answerLink={
                  <AnswerLanguageLinkItem
                    href={appPath(ANSWER_LANGUAGE_ROUTE)}
                    value={answerLanguageValue}
                    onSelect={() => {
                      onBeforePersonalSettingsNavigate?.();
                      router.push(ANSWER_LANGUAGE_ROUTE);
                    }}
                  />
                }
              />
            </div>
            <SignOutItem
              signingOut={signingOut}
              onSignOut={() => void handleSignOut()}
            >
              <span className="v2-user-menu-icon" aria-hidden="true">
                <LogOut />
              </span>
              {signingOut
                ? t("user_menu.signing_out")
                : t("user_menu.sign_out")}
            </SignOutItem>
            <VersionMenuItem open={open} />
          </div>
      </MenuFloating>
    </>
  );
}
