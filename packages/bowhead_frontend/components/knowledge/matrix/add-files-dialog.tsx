"use client";

/**
 * Add files to an existing matrix (document matrix step 1, S2; mockup ⑦).
 *
 * Lists every file the viewer can read (personal, this Space, Team), searched by name on the server and grouped by
 * scope. Files that cannot be added stay visible with the reason: already in the matrix, a personal file (only
 * only-me matrices may hold them, step 1 S8: `personalFiles`), or not processed. Up to
 * `MATRIX_FILES_PER_CHANGE` files are added per request; the server checks each file again.
 */

import { useEffect, useId, useMemo, useState } from "react";
import { useTranslations } from "next-intl";

import { LucideGlyph } from "@/components/icon-refresh";
import { Button } from "@/components/ui/button";
import { Dialog } from "@/components/ui/dialog";
import { ListTime } from "@/components/ui/list-time";
import { fileTypeIcon } from "@/lib/file-type-icon";
import { type KbFileDTO, fetchKbFiles, formatBytes, isParseInProgress } from "@/lib/kb-api";
import { MATRIX_FILES_PER_CHANGE } from "@/lib/kb-matrix/run-api";

/** One server page; the search narrows longer lists. */
export const ADD_FILES_PAGE = 200;

type Group = "space" | "team" | "personal";
const GROUP_ORDER: Group[] = ["space", "team", "personal"];

type DisabledReason = "present" | "personal" | "personal_space" | "not_ready" | "failed";

/**
 * Personal files in this dialog (S8): "allowed" in the viewer's only-me matrix; otherwise refused, saying the
 * matrix is visible to Space members ("space_matrix") or, while only-me matrices are off, that they are coming.
 */
export type PersonalFilesMode = "allowed" | "space_matrix" | "unavailable";

function disabledReason(file: KbFileDTO, present: ReadonlySet<string>, personal: PersonalFilesMode): DisabledReason | null {
  if (present.has(file.file_id)) return "present";
  if (file.scope === "personal" && personal !== "allowed") return personal === "space_matrix" ? "personal_space" : "personal";
  if (file.parse_status === "done") return null;
  return isParseInProgress(file.parse_status) || file.parse_status === "queued" || file.parse_status === "awaiting_upload"
    ? "not_ready"
    : "failed";
}

const REASON_KEY: Record<DisabledReason, string> = {
  present: "add_files_reason_present",
  personal: "add_files_reason_personal",
  personal_space: "add_files_reason_personal_space",
  not_ready: "add_files_reason_not_ready",
  failed: "add_files_reason_failed",
};

export function AddFilesDialog({
  matrixName,
  presentFileIds,
  personalFiles = "unavailable",
  busy,
  error = null,
  onClose,
  onAdd,
}: {
  matrixName: string;
  /** Rows already in the matrix (snapshot rows, including ones hidden from this viewer). */
  presentFileIds: ReadonlySet<string>;
  /** S8: whether personal files may be added (the viewer's only-me matrix) and how a refusal reads. */
  personalFiles?: PersonalFilesMode;
  busy: boolean;
  /** The last add's failure (condition and next step), shown above the actions. */
  error?: string | null;
  onClose: () => void;
  /** Called with the checked ids in the order they were checked. */
  onAdd: (fileIds: string[]) => void;
}) {
  const t = useTranslations("kbMatrix");
  const [query, setQuery] = useState("");
  const [term, setTerm] = useState("");
  const [files, setFiles] = useState<KbFileDTO[]>([]);
  const [hasMore, setHasMore] = useState(false);
  const [loading, setLoading] = useState(true);
  const [failed, setFailed] = useState(false);
  const [retry, setRetry] = useState(0);
  const [checked, setChecked] = useState<string[]>([]);
  const noteId = useId();

  useEffect(() => {
    const timer = window.setTimeout(() => setTerm(query.trim()), query.trim() ? 220 : 0);
    return () => window.clearTimeout(timer);
  }, [query]);

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setFailed(false);
    fetchKbFiles({ scope: "all", q: term || undefined, limit: ADD_FILES_PAGE, order_by: "uploaded_at", order_dir: "desc" }, { signal: controller.signal })
      .then((page) => {
        if (controller.signal.aborted) return;
        setFiles(page.files);
        setHasMore(!!page.has_more);
      })
      .catch(() => {
        if (!controller.signal.aborted) setFailed(true);
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [term, retry]);

  const groups = useMemo(() => {
    const out: Record<Group, KbFileDTO[]> = { space: [], team: [], personal: [] };
    for (const file of files) out[file.scope === "personal" ? "personal" : file.scope === "team" ? "team" : "space"].push(file);
    return GROUP_ORDER.filter((g) => out[g].length > 0).map((g) => ({ group: g, files: out[g] }));
  }, [files]);

  const overLimit = checked.length > MATRIX_FILES_PER_CHANGE;
  const toggle = (fileId: string, on: boolean) =>
    setChecked((current) => (on ? (current.includes(fileId) ? current : [...current, fileId]) : current.filter((id) => id !== fileId)));

  return (
    <Dialog
      title={t("add_files_title", { name: matrixName })}
      width={560}
      busy={busy}
      onClose={onClose}
      footer={(
        <>
          <Button onClick={onClose} disabled={busy}>
            {t("cancel")}
          </Button>
          <Button
            variant="primary"
            loading={busy}
            disabled={busy || checked.length === 0 || overLimit}
            aria-describedby={overLimit ? noteId : undefined}
            data-testid="kb-matrix-addfiles-confirm"
            onClick={() => onAdd(checked)}
          >
            {t("add_files_confirm", { n: checked.length })}
          </Button>
        </>
      )}
    >
      <div className="mx-addfiles" data-testid="kb-matrix-addfiles">
        <input
          className="settings-input mx-addfiles-search"
          type="search"
          aria-label={t("add_files_search")}
          placeholder={t("add_files_search")}
          value={query}
          onChange={(e) => setQuery(e.target.value)}
        />
        <div className="mx-addfiles-list" aria-busy={loading}>
          {loading && files.length === 0 ? (
            <p className="mx-addfiles-state" role="status">{t("loading")}</p>
          ) : failed ? (
            <div className="mx-addfiles-state">
              <p role="alert">{t("add_files_load_failed")}</p>
              <Button size="sm" onClick={() => setRetry((n) => n + 1)}>
                {t("matrix_unavailable_retry")}
              </Button>
            </div>
          ) : groups.length === 0 ? (
            <p className="mx-addfiles-state">{term ? t("add_files_no_match", { q: term }) : t("add_files_none")}</p>
          ) : (
            groups.map(({ group, files: groupFiles }) => (
              <div key={group} role="group" aria-labelledby={`${noteId}-${group}`} className="mx-addfiles-group">
                <div id={`${noteId}-${group}`} className="mx-addfiles-grouphead">
                  {t(`add_files_group_${group}`)}
                </div>
                {groupFiles.map((file) => {
                  const reason = disabledReason(file, presentFileIds, personalFiles);
                  const Icon = fileTypeIcon(file.filename, file.mime_type);
                  const reasonId = `${noteId}-${file.file_id}`;
                  return (
                    <label
                      key={file.file_id}
                      className={"mx-addfiles-row" + (reason ? " is-disabled" : "")}
                      data-testid={`kb-matrix-addfiles-${file.file_id}`}
                    >
                      <input
                        type="checkbox"
                        checked={!reason && checked.includes(file.file_id)}
                        disabled={!!reason || busy}
                        aria-describedby={reasonId}
                        onChange={(e) => toggle(file.file_id, e.target.checked)}
                      />
                      <LucideGlyph icon={Icon} size={16} aria-hidden="true" className="mx-addfiles-icon" />
                      <span className="mx-addfiles-who">
                        <span className="mx-addfiles-name">{file.filename}</span>
                        <span id={reasonId} className="mx-addfiles-sub">
                          {reason ? (
                            t(REASON_KEY[reason])
                          ) : (
                            <>
                              {formatBytes(file.size_bytes)}
                              {" · "}
                              <ListTime value={file.uploaded_at} />
                            </>
                          )}
                        </span>
                      </span>
                    </label>
                  );
                })}
              </div>
            ))
          )}
          {!loading && !failed && hasMore && <p className="mx-addfiles-state">{t("add_files_more", { n: files.length })}</p>}
        </div>
        <p id={noteId} className={overLimit ? "mx-inlineerr" : "mx-addfiles-note"} role={overLimit ? "alert" : undefined}>
          {overLimit ? t("add_files_limit", { limit: MATRIX_FILES_PER_CHANGE }) : t("add_files_note")}
        </p>
        {error && (
          <p className="mx-inlineerr" role="alert" data-testid="kb-matrix-addfiles-error">
            {error}
          </p>
        )}
      </div>
    </Dialog>
  );
}
