"use client";

/**
 * Confirmation for removing rows from a matrix (document matrix step 1, S2).
 *
 * States what goes: the rows' cells, and how many of them were edited or verified. Rows with such cells need the
 * matrix creator or a Space/Team admin (server `can_manage`); for anyone else the dialog says who can remove them and
 * what to do instead, and offers no confirm. Counts come from the viewer's snapshot: cells of rows hidden from the
 * viewer are not counted, so the server still answers 403 `matrix_remove_requires_owner` with its own counts.
 */

import { useId } from "react";
import { useTranslations } from "next-intl";

import { Button } from "@/components/ui/button";
import { Dialog } from "@/components/ui/dialog";
import type { MatrixRowVM } from "@/lib/kb-matrix/types";
import { rowRemovalCounts } from "@/lib/kb-matrix/types";

export function RemoveFilesDialog({
  rows,
  canManage,
  hasActiveRun,
  busy,
  error = null,
  refused = false,
  onCancel,
  onConfirm,
}: {
  rows: MatrixRowVM[];
  canManage: boolean;
  hasActiveRun: boolean;
  busy: boolean;
  /** The last attempt's failure, stated in the dialog. */
  error?: string | null;
  /** The server refused the removal (403 `matrix_remove_requires_owner`): no retry is offered. */
  refused?: boolean;
  onCancel: () => void;
  onConfirm: () => void;
}) {
  const t = useTranslations("kbMatrix");
  const describedBy = useId();
  const { cells, edited, verified } = rowRemovalCounts(rows);
  const reviewed = edited + verified > 0;
  const blocked = (reviewed && !canManage) || refused;
  // A row hidden from the viewer has no name to show.
  const title =
    rows.length === 1 && !rows[0].missing
      ? t("remove_title_one", { name: rows[0].fileName })
      : t("remove_title_many", { n: rows.length });
  return (
    <Dialog
      title={title}
      role="alertdialog"
      describedBy={describedBy}
      layer="top"
      busy={busy}
      onClose={onCancel}
      footer={(
        <>
          <Button onClick={onCancel} disabled={busy} data-dialog-initial-focus="">
            {t("cancel")}
          </Button>
          {!blocked && (
            <Button variant="danger" loading={busy} disabled={busy} data-testid="kb-matrix-remove-confirm" onClick={onConfirm}>
              {t("remove_confirm")}
            </Button>
          )}
        </>
      )}
    >
      <div id={describedBy} className="settings-dialog__description" data-testid="kb-matrix-remove-dialog">
        <p>{t("remove_body", { cells })}</p>
        {reviewed && <p>{t("remove_reviewed", { edited, verified })}</p>}
        {hasActiveRun && <p>{t("remove_running")}</p>}
        {error ? (
          <p className="mx-inlineerr" role="alert">
            {error}
          </p>
        ) : blocked ? (
          <p className="mx-inlineerr" role="note">
            {t("remove_blocked")}
          </p>
        ) : null}
      </div>
    </Dialog>
  );
}
