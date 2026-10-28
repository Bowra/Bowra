"use client";

import { useId, useState } from "react";
import { useTranslations } from "next-intl";

import { CircleCheck, Copy, Info } from "@/components/icons";
import { SpaceInstructionsDialog, type SpaceInstructionsPrefill } from "@/components/settings/space-instructions";
import { Button } from "@/components/ui/button";
import { ListTime } from "@/components/ui/list-time";
import type { SpaceInstructions } from "@/lib/api";
import { copyTextWithFallback } from "@/lib/clipboard";
import type { FeedbackSpaceInstructionTarget } from "@/lib/feedback-candidates-api";

import styles from "./space-instruction-targets.module.css";

/** "Added to 1 of 3 Spaces" once any source Space saved the wording; null before that. */
export function addedProgress(spaces: FeedbackSpaceInstructionTarget[]): { done: number; total: number } | null {
  const done = spaces.filter((space) => space.added).length;
  return done ? { done, total: spaces.length } : null;
}

/**
 * Suggestion inbox › adopted Business-definition suggestion › "Add to Space instructions" (mockup Part 2, C1–C3 and
 * C6): one row per source Space, most conversations first. Each row opens that Space's instructions with the review
 * step; after saving, the row shows the version the wording landed in, so it is not added twice.
 */
export function SpaceInstructionTargets({
  text,
  spaces,
  prefill,
  onOpenSpaces,
  onAdded,
}: {
  text: string;
  spaces: FeedbackSpaceInstructionTarget[];
  prefill: Omit<SpaceInstructionsPrefill, "text">;
  onOpenSpaces: () => void;
  onAdded: (workspaceId: string, value: SpaceInstructions) => void;
}) {
  const t = useTranslations("settings.feedbackInbox");
  const [target, setTarget] = useState<{ space: FeedbackSpaceInstructionTarget; review: boolean } | null>(null);
  const [copied, setCopied] = useState(false);
  const titleId = useId();

  return (
    <section className={styles.block} aria-labelledby={titleId}>
      <h3 id={titleId} className={styles.title}>
        {t("target_title")}
      </h3>
      {spaces.length ? (
        <>
          <p className={styles.hint}>{spaces.length === 1 ? t("target_hint_one") : t("target_hint")}</p>
          <ul className={styles.rows}>
            {spaces.map((space, index) => (
              <li key={space.workspace_id} className={styles.row}>
                <span className={styles.name}>{space.name}</span>
                {space.added ? (
                  <span className={styles.done}>
                    <CircleCheck size={14} aria-hidden />
                    {t("added_to", { version: space.added.version })} ·{" "}
                    <button type="button" className={styles.link} onClick={() => setTarget({ space, review: false })}>
                      {t("added_view")}
                    </button>
                  </span>
                ) : (
                  <Button size="sm" variant={index === 0 ? "primary" : "secondary"} onClick={() => setTarget({ space, review: true })}>
                    {t("prefill_add_to_space")}
                  </Button>
                )}
                <span className={styles.meta}>
                  {space.added ? (
                    <>
                      {space.added.by_name} · <ListTime value={space.added.at} />
                    </>
                  ) : space.instructions_version > 0 ? (
                    t("target_meta", { count: space.conversation_count, version: space.instructions_version })
                  ) : (
                    t("target_meta_empty", { count: space.conversation_count })
                  )}
                </span>
              </li>
            ))}
          </ul>
        </>
      ) : (
        <>
          <div className={styles.warn} role="status">
            <Info size={14} aria-hidden />
            <span>{t("prefill_space_unknown")}</span>
          </div>
          <div className={styles.actions}>
            <Button size="sm" variant="secondary" onClick={() => void copyTextWithFallback(text, t("copy_prompt")).then((ok) => setCopied(ok))}>
              <Copy size={14} aria-hidden />
              {copied ? t("copied") : t("copy_text")}
            </Button>
            <Button size="sm" variant="ghost" onClick={onOpenSpaces}>
              {t("prefill_open_spaces")}
            </Button>
          </div>
        </>
      )}
      {target ? (
        <SpaceInstructionsDialog
          key={target.space.workspace_id}
          workspaceId={target.space.workspace_id}
          name={target.space.name}
          prefill={target.review ? { ...prefill, text } : undefined}
          onClose={() => setTarget(null)}
          onSaved={(value, { fromPrefill }) => {
            if (!fromPrefill) return;
            onAdded(target.space.workspace_id, value);
            setTarget(null);
          }}
        />
      ) : null}
    </section>
  );
}
