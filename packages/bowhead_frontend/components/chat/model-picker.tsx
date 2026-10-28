"use client";

/**
 * Composer Model Picker.
 *
 * Uses the team-scoped server catalog. It never reads local provider secrets.
 * The selected model affects the next turn via preferred_provider/model.
 */

import {
  useEffect,
  useId,
  useRef,
  useState,
  type ReactNode,
  type RefObject,
} from "react";
import { useTranslations } from "next-intl";
import { useOptionalAuthSession } from "@/lib/auth/auth-session-context";
import { TEAM_SETTINGS_ROLES, type SettingsTeamRole } from "@/lib/settings-navigation";

import { MenuFloating, useMenu, useMenuItem } from "@/components/ui/menu";
import { COMPOSER_MENU_MAX_HEIGHT, useComposerFocusFallback } from "@/lib/composer-menu";
import { fetchLlmCatalog, type LlmCatalog } from "@/lib/llm-config-api";
import { usePreferredModel } from "@/lib/use-preferred-model";
import { trackComposerEvent } from "@/lib/metrics";
import { CharIcon } from "@/components/char-icon";

/** Menu width (px); useMenu narrows to available width on narrow screens. */
const MODEL_MENU_WIDTH = 252;

/** API types supported by team model configuration (backend `team_llm_config.SUPPORTED_PROVIDER_TYPES`). */
const KNOWN_PROVIDER_TYPES = new Set([
  "openai_compatible",
  "anthropic_compatible",
  "azure_openai",
  "custom_http",
]);

/** Small API-type labels beside group headings (Menus Spec section 1:「OpenAI 兼容」「Anthropic 兼容」).
 *  Catalog `type` is raw configuration data, never user-facing; unknown values use neutral「其他接口」. */
function providerTypeLabelKey(providerType: string): string {
  return KNOWN_PROVIDER_TYPES.has(providerType) ? providerType : "other";
}

interface PickEntry {
  providerId: string;
  providerName: string;
  model: string;
  providerType?: string;
}

function ModelMenuItem({
  checked,
  onSelect,
  hoverBackground,
  className,
  children,
}: {
  checked: boolean;
  onSelect: () => void;
  hoverBackground: boolean;
  className?: string;
  children: ReactNode;
}) {
  const itemProps = useMenuItem({ role: "menuitemradio", checked, onSelect });
  // Move original inline styles and hover-time background mutations to styles/ui-controls-legacy.css (appearance-refresh batch 3),
  // base-layer values item by item; the .ui-menu item rules restyle them.
  return (
    <button
      type="button"
      data-active={checked ? "true" : undefined}
      data-hover-bg={hoverBackground ? "true" : undefined}
      className={`composer-v2__model-menu-item legacy-menu-model-item${className ? ` ${className}` : ""}`}
      {...itemProps}
    >
      {children}
    </button>
  );
}

export function ModelPicker({
  disabled = false,
  fallbackFocusRef,
  telemetry,
}: {
  disabled?: boolean;
  fallbackFocusRef?: RefObject<HTMLElement | null>;
  telemetry?: {
    surface: "home" | "workbench";
    composerMode: "comfortable" | "compact" | "ultra";
  };
}) {
  const t = useTranslations("model_picker");
  const session = useOptionalAuthSession();
  // Share API-type copy with team model configuration (settings.llm.provider_types).
  const tProviderType = useTranslations("settings.llm.provider_types");
  const tooltipId = `composer-model-${useId().replaceAll(":", "")}`;
  const { parsed, select, clear } = usePreferredModel();
  const [catalog, setCatalog] = useState<LlmCatalog | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadFailed, setLoadFailed] = useState(false);
  const [staleNotice, setStaleNotice] = useState(false);
  const [open, setOpen] = useState(false);
  const wrapRef = useRef<HTMLDivElement | null>(null);
  const returnFocusFallback = useComposerFocusFallback(wrapRef, fallbackFocusRef);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setLoadFailed(false);
    void fetchLlmCatalog()
      .then((d) => {
        if (!cancelled) setCatalog(d);
      })
      .catch(() => {
        if (!cancelled) setLoadFailed(true);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => { cancelled = true; };
  }, []);

  const entries: PickEntry[] = (catalog?.providers ?? []).flatMap((p) =>
    (p.models ?? []).map((m) => ({
      providerId: p.id,
      providerName: p.display_name || p.id,
      model: m,
      providerType: p.type,
    })),
  );
  const hasAny = entries.length > 0;
  const selectedAllowed = Boolean(
    parsed && entries.some((e) => e.providerId === parsed.providerId && e.model === parsed.model),
  );

  useEffect(() => {
    if (!loading && parsed && hasAny && !selectedAllowed) {
      clear();
      setStaleNotice(true);
    }
  }, [clear, hasAny, loading, parsed, selectedAllowed]);

  const groups = groupByProvider(entries);
  //「使用团队默认」is item 0, followed by models in group order.
  const selectedModelIndex = parsed
    ? groups
        .flatMap((group) => group.models.map((model) => [group.providerId, model] as const))
        .findIndex(([providerId, model]) => providerId === parsed.providerId && model === parsed.model)
    : -1;
  const selectedIndex = !parsed ? 0 : selectedModelIndex < 0 ? null : selectedModelIndex + 1;
  const triggerDisabled = disabled || !hasAny || loading || loadFailed;

  // ui/menu handles opening, outside click/Esc, arrows, positioning, and return focus: prefer upward, flip if it does not fit;
  // clamp to visible area when the keyboard opens; return focus to trigger, falling back to composer if disabled/unmounted.
  const menu = useMenu({
    open: open && hasAny,
    onOpenChange: (next) => {
      if (next && triggerDisabled) return;
      if (next && !open && telemetry) {
        trackComposerEvent({
          event_name: "composer_model_panel_opened",
          carrier: "popover",
          surface: telemetry.surface,
          composer_mode: telemetry.composerMode,
        });
      }
      setOpen(next);
    },
    placement: "top-start",
    maxHeight: COMPOSER_MENU_MAX_HEIGHT,
    selectedIndex,
    returnFocusFallback,
    // Appearance refresh + narrow screen + threshold met (usually >6 items): bottom sheet titled「模型」(Menus Spec section 6).
    sheetTitle: t("title"),
  });

  // Close the menu when the control is disabled (sending/read-only).
  useEffect(() => {
    if (disabled && open) setOpen(false);
  }, [disabled, open]);

  const role = session?.me?.team_role || (session?.me?.is_team_admin ? "team_admin" : "member");
  const canManageModels = TEAM_SETTINGS_ROLES.includes(role as SettingsTeamRole);
  let currentLabel: string;
  let currentTitle: string;
  if (loading) {
    currentLabel = t("loading");
    currentTitle = t("loading");
  } else if (loadFailed) {
    currentLabel = t("load_failed");
    currentTitle = t("load_failed");
  } else if (parsed && selectedAllowed) {
    // The trigger and its hint name the model only (116: the hint read "deepseek-flash · deepseek_anthropic", an
    // internal provider id); providers appear as the menu's group headings.
    currentLabel = parsed.model;
    currentTitle = parsed.model;
  } else if (catalog?.default_model) {
    currentLabel = catalog.default_model.model;
    currentTitle = `${catalog.default_model.model} (${t("team_default")})`;
  } else if (entries.length > 0) {
    currentLabel = t("team_default");
    currentTitle = t("team_default");
  } else {
    currentLabel = t("no_providers");
    // A team owner or administrator can fix this themselves; only others are sent to them.
    currentTitle = t(canManageModels ? "no_providers_hint_manage" : "no_providers_hint");
  }

  return (
    <div ref={wrapRef} className="composer-v2__model-wrap" style={{
      position: "relative",
      isolation: "isolate",
      zIndex: open ? 10000 : 1,
    }}>
      <button
        ref={menu.refs.setReference}
        className={`composer-v2__model-trigger${hasAny ? "" : " is-unavailable"}`}
        type="button"
        title={currentTitle}
        aria-describedby={tooltipId}
        {...menu.getReferenceProps()}
        disabled={triggerDisabled}
      >
        <span className="composer-v2__model-trigger-label">
          {currentLabel}
        </span>
        <span className="composer-v2__model-chevron" aria-hidden="true"><CharIcon ch="▾" aria-hidden /></span>
      </button>
      <span id={tooltipId} className="composer-v2__tooltip" role="tooltip">
        {currentTitle}
      </span>

      {/* MenuFloating portals to body: outer frozen-chat-glass card has backdrop-filter (globals.css
          ~4419), making it a position:fixed containing block; otherwise the menu positions against the glass card and is clipped. */}
      <MenuFloating menu={menu}>
        <div
          ref={menu.refs.setFloating}
          {...menu.getFloatingProps({
            className: "composer-v2__model-menu legacy-menu-model",
            style: { width: MODEL_MENU_WIDTH, maxHeight: COMPOSER_MENU_MAX_HEIGHT },
          })}
        >
          {staleNotice ? (
            <div className="legacy-menu-model-stale">
              {t("stale_selection")}
            </div>
          ) : null}

          <ModelMenuItem
            checked={!parsed}
            hoverBackground={false}
            onSelect={() => {
              clear();
              setStaleNotice(false);
              if (telemetry) {
                trackComposerEvent({
                  event_name: "composer_model_preference_changed",
                  source: "team_default",
                  surface: telemetry.surface,
                  composer_mode: telemetry.composerMode,
                });
              }
            }}
            className="legacy-menu-model-item--default"
          >
            <span className="legacy-menu-model-mark">{parsed ? "" : t("active")}</span>
            <span>{t("clear")}</span>
          </ModelMenuItem>

          {/* Render groups by provider, with one header each. */}
          {groups.map(({ providerId, providerName, providerType, models }) => (
            <div key={providerId} className="legacy-menu-model-group">
              {/* The base layer shows uppercase names; the refreshed styles show original names per spec. Render both, hiding one by the hook attribute
                  (display: none); screen readers read only the visible version, preserving legacy screen-reader content. */}
              <div className="legacy-menu-model-group-title">
                <span className="legacy-menu-model-provider-upper">{providerName.toUpperCase()}</span>
                <span className="legacy-menu-model-provider-name">{providerName}</span>
                {providerType ? (
                  <span className="legacy-menu-model-tag">
                    {tProviderType(providerTypeLabelKey(providerType))}
                  </span>
                ) : null}
              </div>
              {models.map((m) => {
                const active = parsed?.providerId === providerId && parsed?.model === m;
                return (
                  <ModelMenuItem
                    key={m}
                    checked={active}
                    hoverBackground
                    onSelect={() => {
                      select(providerId, m);
                      setStaleNotice(false);
                      if (telemetry) {
                        trackComposerEvent({
                          event_name: "composer_model_preference_changed",
                          source: "personal",
                          surface: telemetry.surface,
                          composer_mode: telemetry.composerMode,
                        });
                      }
                    }}
                  >
                    <span className="legacy-menu-model-mark">{active ? t("active") : ""}</span>
                    <span>{m}</span>
                  </ModelMenuItem>
                );
              })}
            </div>
          ))}
        </div>
      </MenuFloating>
    </div>
  );
}

function groupByProvider(entries: PickEntry[]): Array<{
  providerId: string; providerName: string; providerType?: string; models: string[];
}> {
  const map = new Map<string, { providerName: string; providerType?: string; models: string[] }>();
  for (const e of entries) {
    const cur = map.get(e.providerId);
    if (cur) cur.models.push(e.model);
    else map.set(e.providerId, {
      providerName: e.providerName,
      providerType: e.providerType,
      models: [e.model],
    });
  }
  return Array.from(map.entries()).map(([providerId, v]) => ({ providerId, ...v }));
}
