"use client";

/**
 * Composer notice slot (Chat Cards Spec §9, D9): one line below the composer shows the single most important
 * notice; the rest wait behind "N more notices", a shared-menu trigger listing each notice with its fix.
 *
 * Tones: error (send failed or uncertain, draft unreadable) uses the error colour and `role="alert"`;
 * warning (a conflict that blocks sending) and neutral (guidance and state) use `role="status"`. Priority is
 * error > warning > neutral, then the order given. Resolving one lets the next take its place.
 */

import clsx from "clsx";
import { ChevronDown, CircleAlert, Info, TriangleAlert } from "lucide-react";
import { useTranslations } from "next-intl";
import { useState, type ReactNode } from "react";

import { LucideGlyph } from "@/components/icon-refresh";
import { ChatCardButton } from "@/components/ui/card";
import { MenuFloating, MENU_DESCRIPTION_ATTR, useMenu, useMenuItem } from "@/components/ui/menu";

export type ComposerNoticeTone = "error" | "warning" | "neutral";

export interface ComposerNoticeAction {
  label: string;
  onSelect: () => void;
  disabled?: boolean;
}

export interface ComposerNoticeItem {
  /** Stable key. */
  id: string;
  tone: ComposerNoticeTone;
  text: ReactNode;
  /** Fix actions (text buttons, 24 high, 44 on touch). */
  actions?: ComposerNoticeAction[];
  /** id of the sentence (send button `aria-describedby`). */
  textId?: string;
  /** Test hook on the sentence. */
  testId?: string;
}

const TONE_RANK: Record<ComposerNoticeTone, number> = { error: 0, warning: 1, neutral: 2 };
const TONE_ICON = { error: CircleAlert, warning: TriangleAlert, neutral: Info } as const;

/** The notices in display order: the first one is the one shown in the slot. */
export function orderComposerNotices(notices: ComposerNoticeItem[]): ComposerNoticeItem[] {
  return notices
    .map((notice, index) => ({ notice, index }))
    .sort((a, b) => TONE_RANK[a.notice.tone] - TONE_RANK[b.notice.tone] || a.index - b.index)
    .map(({ notice }) => notice);
}

export function ComposerNotices({ notices }: { notices: ComposerNoticeItem[] }) {
  const t = useTranslations("composer");
  const [open, setOpen] = useState(false);
  const menu = useMenu({ open, onOpenChange: setOpen, placement: "bottom-start", offset: 4 });
  const ordered = orderComposerNotices(notices);
  if (ordered.length === 0) return null;
  const [top, ...rest] = ordered;
  return (
    <div className="ui-card-notice" data-tone={top.tone} data-notice-id={top.id}>
      <span
        className="ui-card-notice__text"
        id={top.textId}
        role={top.tone === "error" ? "alert" : "status"}
        aria-live={top.tone === "error" ? "assertive" : "polite"}
        data-testid={top.testId}
      >
        <LucideGlyph icon={TONE_ICON[top.tone]} size={14} aria-hidden="true" />
        <span>{top.text}</span>
      </span>
      {top.actions?.map((action) => (
        <ChatCardButton key={action.label} variant="ghost" disabled={action.disabled} onClick={action.onSelect}>
          {action.label}
        </ChatCardButton>
      ))}
      {rest.length > 0 ? (
        <>
          <button
            ref={menu.refs.setReference}
            type="button"
            className={clsx("ui-card-notice__more", open && "is-open")}
            {...menu.getReferenceProps()}
          >
            {t("notices_more", { count: rest.length })}
            <LucideGlyph icon={ChevronDown} size={14} aria-hidden="true" />
          </button>
          <MenuFloating menu={menu}>
            <div
              ref={menu.refs.setFloating}
              {...menu.getFloatingProps({ className: "app-floating-menu ui-card-notice-menu is-portal" })}
            >
              {rest.map((notice) => (
                <NoticeMenuItem key={notice.id} notice={notice} />
              ))}
            </div>
          </MenuFloating>
        </>
      ) : null}
    </div>
  );
}

/** A hidden notice in the "N more notices" menu: the sentence, and its first fix as the second line. */
function NoticeMenuItem({ notice }: { notice: ComposerNoticeItem }) {
  const action = notice.actions?.find((candidate) => !candidate.disabled);
  const itemProps = useMenuItem({ onSelect: action ? () => action.onSelect() : undefined });
  return (
    <button type="button" className="app-floating-menu-item ui-card-notice-menu__item" data-tone={notice.tone} {...itemProps}>
      <LucideGlyph icon={TONE_ICON[notice.tone]} size={14} aria-hidden="true" />
      <span className="ui-card-notice-menu__text">
        {notice.text}
        {action ? <small {...{ [MENU_DESCRIPTION_ATTR]: "" }}>{action.label}</small> : null}
      </span>
    </button>
  );
}
