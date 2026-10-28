"use client";

import { spaceSettingsPath } from "@/lib/space-settings-paths";

import {
  autoUpdate,
  flip,
  FloatingFocusManager,
  FloatingPortal,
  offset,
  shift,
  size,
  useClick,
  useDismiss,
  useFloating,
  useInteractions,
  useRole,
} from "@floating-ui/react";
import { useCallback, useEffect, useId, useRef, useState } from "react";
import { useTranslations } from "next-intl";

import { Info } from "@/components/icons";
import { Button } from "@/components/ui/button";
import { Dialog } from "@/components/ui/dialog";
import { ListTime } from "@/components/ui/list-time";
import { useNarrowViewport } from "@/components/ui/sheet";
import { getSpaceInstructions, type SpaceInstructions } from "@/lib/api";
import { useAppRouter } from "@/lib/use-app-router";
import { useSpace } from "@/lib/use-space";

import styles from "./space-about.module.css";

/** Characters shown before "Show full text" (mockup E2: the body fades out past its height). */
const PREVIEW_CHARS = 360;

function AboutBody({ workspaceId, onEdit }: { workspaceId: string; onEdit: () => void }) {
  const t = useTranslations("space_about");
  const [value, setValue] = useState<SpaceInstructions | null>(null);
  const [failed, setFailed] = useState(false);
  const [expanded, setExpanded] = useState(false);

  const request = useRef(0);
  const load = useCallback(async () => {
    // Only the latest request may fill the body, so a slow answer for another Space never shows here.
    const id = ++request.current;
    setFailed(false);
    setValue(null);
    try {
      const next = await getSpaceInstructions(workspaceId);
      if (request.current === id) setValue(next);
    } catch {
      if (request.current === id) setFailed(true);
    }
  }, [workspaceId]);

  useEffect(() => {
    void load();
    return () => {
      request.current += 1;
    };
  }, [load]);

  if (failed) {
    return (
      <div className={styles.body} role="alert">
        <p className={styles.hint}>{t("load_failed")}</p>
        <div>
          <Button size="sm" variant="secondary" onClick={() => void load()}>
            {t("retry")}
          </Button>
        </div>
      </div>
    );
  }
  if (!value) return <p className={styles.hint} role="status">{t("loading")}</p>;

  const long = value.content.length > PREVIEW_CHARS;
  const names = value.administrators.join(t("name_separator"));
  return (
    <div className={styles.body}>
      {value.content ? (
        <>
          <p className={styles.text} data-clipped={long && !expanded ? "true" : undefined}>
            {long && !expanded ? `${value.content.slice(0, PREVIEW_CHARS)}…` : value.content}
          </p>
          {long ? (
            <div>
              <button type="button" className={styles.link} aria-expanded={expanded} onClick={() => setExpanded((open) => !open)}>
                {expanded ? t("collapse") : t("expand")}
              </button>
            </div>
          ) : null}
          <p className={styles.meta}>
            {t("meta", { version: value.version, name: value.updated_by_name ?? "—" })} · <ListTime value={value.updated_at} />
          </p>
        </>
      ) : (
        <p className={styles.text}>{t("empty")}</p>
      )}
      <p className={styles.hint}>{names ? t("hint", { names }) : t("hint_no_names")}</p>
      {value.can_edit ? (
        <div className={styles.actions}>
          <Button size="sm" variant="secondary" onClick={onEdit}>
            {t("edit")}
          </Button>
        </div>
      ) : null}
    </div>
  );
}

/**
 * "About this Space" next to the Space switcher (mockup Part 2, E2): members can read the instructions every
 * conversation in the Space follows and see whom to ask for a change. A popover on wide screens, a sheet on phones.
 */
export function SpaceAboutButton() {
  const t = useTranslations("space_about");
  const space = useSpace();
  const router = useAppRouter();
  const narrow = useNarrowViewport(true);
  const [open, setOpen] = useState(false);
  const titleId = useId();
  const { refs, floatingStyles, context } = useFloating({
    open: open && !narrow,
    onOpenChange: setOpen,
    placement: "bottom-end",
    whileElementsMounted: autoUpdate,
    middleware: [
      offset(8),
      flip(),
      shift({ padding: 12 }),
      size({
        padding: 12,
        apply({ availableHeight, elements }) {
          elements.floating.style.maxHeight = `${Math.max(240, availableHeight)}px`;
        },
      }),
    ],
  });
  const { getReferenceProps, getFloatingProps } = useInteractions([useClick(context), useDismiss(context), useRole(context, { role: "dialog" })]);
  const workspaceId = space.activeId;
  if (!workspaceId) return null;

  const title = t("title", { name: space.activeName });
  const edit = () => {
    setOpen(false);
    router.push(spaceSettingsPath("instructions"));
  };
  return (
    <>
      <button
        ref={refs.setReference}
        type="button"
        className={styles.trigger}
        aria-label={t("button")}
        title={t("button")}
        aria-expanded={open}
        {...getReferenceProps()}
      >
        <Info size={16} aria-hidden />
      </button>
      {open && narrow ? (
        <Dialog title={title} presentation="sheet" onClose={() => setOpen(false)}>
          <AboutBody key={workspaceId} workspaceId={workspaceId} onEdit={edit} />
        </Dialog>
      ) : null}
      {open && !narrow ? (
        <FloatingPortal>
          <FloatingFocusManager context={context} modal={false} initialFocus={-1}>
            <div ref={refs.setFloating} className={styles.popover} style={floatingStyles} aria-labelledby={titleId} {...getFloatingProps()}>
              <h2 id={titleId} className={styles.title}>
                <Info size={14} aria-hidden />
                {title}
              </h2>
              <AboutBody key={workspaceId} workspaceId={workspaceId} onEdit={edit} />
            </div>
          </FloatingFocusManager>
        </FloatingPortal>
      ) : null}
    </>
  );
}
