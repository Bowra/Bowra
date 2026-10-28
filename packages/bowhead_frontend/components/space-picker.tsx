"use client";

/**
 * Space picker — Topbar's "# name" chip opens a dropdown to select a space.
 *
 * Dropdown behavior (portal, positioning, arrows, Esc/outside dismissal, focus return) uses ui/menu: prefer opening below,
 * flip above if needed; near the right edge, shift left to stay on screen (measured on 116).
 */

import { useEffect, useMemo, useState, type KeyboardEvent, type ReactNode } from "react";
import type { Placement } from "@floating-ui/react";
import { Building2, ChevronDown } from "lucide-react";
import { useTranslations } from "next-intl";

import { useCopyDiagnosticsToastAction } from "@/components/error-notice";

import {
  MENU_DESCRIPTION_ATTR,
  MenuFloating,
  MenuKeyHints,
  useMenu,
  useMenuItem,
  type MenuMiddlewareOptions,
} from "@/components/ui/menu";
import { useSpace } from "@/lib/use-space";
import { confirmCanvasNavigation } from "@/lib/canvas-navigation-guard";
import { renderRefreshSvg } from "@/components/icon-refresh";

interface Props {
  /** compact: inline composer use, omit desc; defaults to true. */
  compact?: boolean;
}

export const SPACE_PICKER_PLACEMENT: Placement = "bottom-start";
export const SPACE_PICKER_MIN_WIDTH = 260;

export function spacePickerMenuOptions(compact: boolean): MenuMiddlewareOptions {
  return { maxHeight: 360, maxWidth: compact ? 320 : 360 };
}

/**
 * Menu's “复制诊断”: a real menuitem, reachable with arrows and executable with Enter (no ordinary buttons
 * or collapsible areas inside role=menu). Toast reports “已复制 / 复制失败”; omit if diagnostics are unrecognized (e.g. request never sent).
 */
function CopyDiagnosticsMenuItem({ error }: { error: unknown }) {
  const action = useCopyDiagnosticsToastAction()(error, "global");
  if (!action) return null;
  return <CopyDiagnosticsMenuItemButton label={action.label} onSelect={() => void action.onAction()} />;
}

function CopyDiagnosticsMenuItemButton({ label, onSelect }: { label: string; onSelect: () => void }) {
  const itemProps = useMenuItem({ label, onSelect });
  return (
    <button type="button" className="legacy-menu-space-item" {...itemProps}>
      {label}
    </button>
  );
}

function SpaceMenuItem({
  active,
  onSelect,
  children,
}: {
  active: boolean;
  onSelect: () => void;
  children: ReactNode;
}) {
  const itemProps = useMenuItem({ role: "menuitemradio", checked: active, onSelect });
  // Original inline styles and hover-time inline background changes moved to styles/ui-controls-legacy.css (appearance refresh batch 3).
  return (
    <button type="button" className="legacy-menu-space-item" data-active={active ? "true" : undefined} {...itemProps}>
      {children}
    </button>
  );
}

/** Leave search keys to the input; only up/down navigate menu items, while Esc and Tab close as usual. */
function keepTypingInSearch(event: KeyboardEvent<HTMLInputElement>) {
  if (["ArrowDown", "ArrowUp", "Escape", "Tab"].includes(event.key)) return;
  event.stopPropagation();
}

export function SpacePicker({ compact = true }: Props) {
  const t = useTranslations("workspace");
  const ws = useSpace();
  const [open, setOpen] = useState(false);
  // U12: filter box for teams with many workspaces (33+ scrolling is painful).
  const [query, setQuery] = useState("");

  // U12: reset the filter each time the menu closes so it reopens clean.
  useEffect(() => { if (!open) setQuery(""); }, [open]);

  // U12: filter by name / desc / id; only surface the search box for long lists.
  const q = query.trim().toLowerCase();
  const visibleItems = useMemo(
    () =>
      q
        ? ws.items.filter(
            (w) =>
              (w.name || "").toLowerCase().includes(q) ||
              (w.desc || "").toLowerCase().includes(q) ||
              w.id.toLowerCase().includes(q),
          )
        : ws.items,
    [q, ws.items],
  );
  const showSearch = ws.items.length > 8;
  const listed = ws.loading ? [] : visibleItems;
  // Use the unfiltered list: search is empty on opening, so both match. Following filtered results would, on every keystroke,
  // make list navigation refocus the current space, pulling focus out of the search field.
  const activeIndex = ws.loading ? -1 : ws.items.findIndex((w) => w.id === ws.activeId);

  const menu = useMenu({
    open,
    onOpenChange: setOpen,
    placement: SPACE_PICKER_PLACEMENT,
    ...spacePickerMenuOptions(compact),
    selectedIndex: activeIndex < 0 ? null : activeIndex,
  });

  return (
    <div
      className="v2-ws-picker"
      style={{
        position: "relative",
        cursor: ws.loading ? "progress" : "pointer",
        userSelect: "none",
      }}
    >
      <button
        ref={menu.refs.setReference}
        type="button"
        disabled={ws.loading}
        title={ws.error ? t("load_failed_notice") : ws.activeName}
        aria-label={ws.activeName ? t("picker_aria", { name: ws.activeName }) : t("picker_aria_empty")}
        {...menu.getReferenceProps()}
        // P2 batch 3: the button is reset in styles/ui-refresh-chrome.css instead of inline `all: unset`,
        // which would also wipe the keyboard focus ring.
        className="v2-ws-picker-button"
      >
        {/* P2 batch 3 (topbar space chip): space icon + name + chevron, 14px icons; the chip width cap is CSS. */}
        {renderRefreshSvg(Building2, 14, 14, { "aria-hidden": true })}
        <span suppressHydrationWarning className="v2-ws-picker-name">
          {ws.activeName}
        </span>
        {renderRefreshSvg(ChevronDown, 14, 14, { "aria-hidden": true })}
      </button>

      <MenuFloating menu={menu}>
        <div
          ref={menu.refs.setFloating}
          {...menu.getFloatingProps({
            className: "v2-ws-picker-menu legacy-menu-space",
            style: {
              minWidth: SPACE_PICKER_MIN_WIDTH,
              maxWidth: spacePickerMenuOptions(compact).maxWidth,
              maxHeight: spacePickerMenuOptions(compact).maxHeight,
            },
          })}
        >
          {ws.loading && (
            <div className="legacy-menu-space-note">
              {t("loading")}
            </div>
          )}
          {!ws.loading && ws.items.length === 0 && (
            <div className="legacy-menu-space-note">
              {ws.error ? t("load_failed_notice") : t("empty")}
            </div>
          )}
          {!ws.loading && ws.items.length === 0 && ws.error ? (
            <CopyDiagnosticsMenuItem error={ws.errorCause} />
          ) : null}
          {!ws.loading && showSearch && (
            <input
              type="search"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder={t("search_placeholder")}
              aria-label={t("search_placeholder")}
              onKeyDown={keepTypingInSearch}
              className="legacy-menu-space-search"
            />
          )}
          {!ws.loading && ws.items.length > 0 && visibleItems.length === 0 && (
            <div className="legacy-menu-space-note">
              {t("no_matches")}
            </div>
          )}
          {listed.map((w) => {
            const active = w.id === ws.activeId;
            return (
              <SpaceMenuItem key={w.id} active={active} onSelect={() => {
                if (active) return;
                void confirmCanvasNavigation().then((allowed) => { if (allowed) ws.setActiveId(w.id); });
              }}>
                <div className="legacy-menu-space-title">
                  <span className="legacy-menu-space-name">
                    {w.name || `Space ${w.id}`}
                  </span>
                  {/* U13: de-emphasize the raw workspace id — short faint suffix, full id on hover */}
                  <span title={w.id} className="legacy-menu-space-id">
                    {w.id.length > 8 ? `#…${w.id.slice(-6)}` : `#${w.id}`}
                  </span>
                </div>
                {w.desc ? (
                  <div
                    className="legacy-menu-space-desc"
                    // Behavior batch A: descriptions on spaces make narrow menus use a bottom sheet.
                    {...{ [MENU_DESCRIPTION_ATTR]: "" }}
                  >
                    {w.desc}
                  </div>
                ) : null}
              </SpaceMenuItem>
            );
          })}
          {listed.length > 0 ? (
            <MenuKeyHints
              hints={[
                { keys: "↑↓", label: t("hint_select") },
                { keys: "↵", label: t("hint_switch") },
                { keys: "esc", label: t("hint_close") },
              ]}
            />
          ) : null}
        </div>
      </MenuFloating>
    </div>
  );
}
