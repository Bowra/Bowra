"use client";

import { FileText, Search } from "lucide-react";
import { useFormatter, useTranslations } from "next-intl";
import { useEffect, useRef, useState, type RefObject } from "react";
import { Dialog } from "@/components/ui/dialog";
import { Tab, TabList, TabPanel, Tabs } from "@/components/ui/tabs";
import { ApiError } from "@/lib/api-client";
import { validateUpload } from "@/lib/api";
import {
  browseComposerLibrary,
  COMPOSER_LIBRARY_SCOPES,
  LIBRARY_PAGE_SIZE,
  libraryFileKey,
  readComposerLibraryFile,
  type ComposerLibraryFile,
  type LibraryScope,
} from "@/lib/composer-library";
import type { ComposerDraftScope } from "@/lib/composer-ordinary-draft";

interface ComposerLibraryPickerProps {
  context: ComposerDraftScope;
  onClose: () => void;
  onAdd: (source: ComposerLibraryFile, file: File) => boolean;
  atLimit: boolean;
  returnFocusRef: RefObject<HTMLElement | null>;
}

export function ComposerLibraryPicker({
  context,
  onClose,
  onAdd,
  atLimit,
  returnFocusRef,
}: ComposerLibraryPickerProps) {
  const t = useTranslations("composer.library");
  const format = useFormatter();
  const [scope, setScope] = useState<LibraryScope>("recent");
  const [query, setQuery] = useState("");
  const [offset, setOffset] = useState(0);
  const [files, setFiles] = useState<ComposerLibraryFile[]>([]);
  const [hasMore, setHasMore] = useState(false);
  const [loading, setLoading] = useState(true);
  const [listError, setListError] = useState(false);
  const [retry, setRetry] = useState(0);
  const [opening, setOpening] = useState<string | null>(null);
  const [selectionError, setSelectionError] = useState<string | null>(null);
  const download = useRef<AbortController | null>(null);
  useEffect(() => () => download.current?.abort(), []);

  useEffect(() => {
    const controller = new AbortController();
    const term = query.trim();
    const timer = window.setTimeout(
      () => {
        void browseComposerLibrary(context, scope, term, offset, controller.signal)
          .then((page) => {
            if (controller.signal.aborted) return;
            setFiles((previous) => {
              if (offset === 0) return page.files;
              const existing = new Set(previous.map(libraryFileKey));
              return [...previous, ...page.files.filter((file) => !existing.has(libraryFileKey(file)))];
            });
            setHasMore(page.hasMore);
          })
          .catch(() => {
            if (!controller.signal.aborted) setListError(true);
          })
          .finally(() => {
            if (!controller.signal.aborted) setLoading(false);
          });
      },
      term && offset === 0 ? 220 : 0,
    );
    return () => {
      window.clearTimeout(timer);
      controller.abort();
    };
  }, [context, offset, query, retry, scope]);

  const resetList = () => {
    setOffset(0);
    setFiles([]);
    setHasMore(false);
    setListError(false);
    setSelectionError(null);
    setLoading(true);
  };
  const changeScope = (next: LibraryScope) => {
    if (next === scope || download.current) return;
    resetList();
    setScope(next);
  };

  const add = async (source: ComposerLibraryFile) => {
    if (download.current) return;
    const issue = validateUpload({ name: source.filename, size: source.size, type: source.mime });
    if (issue) {
      setSelectionError(t("selection_error", { name: source.filename, reason: issue.message }));
      return;
    }
    const controller = new AbortController();
    download.current = controller;
    setSelectionError(null);
    setOpening(source.filename);
    try {
      const file = await readComposerLibraryFile(context, source, controller.signal);
      if (!controller.signal.aborted && onAdd(source, file)) onClose();
    } catch (error) {
      if (controller.signal.aborted) return;
      const lostAccess = error instanceof ApiError && [403, 404, 409, 410].includes(error.status);
      setSelectionError(t(lostAccess ? "file_unavailable" : "file_read_failed", { name: source.filename }));
    } finally {
      download.current = null;
      if (!controller.signal.aborted) setOpening(null);
    }
  };

  return (
    <Dialog
      title={t("choose")}
      width={520}
      onClose={() => {
        download.current?.abort();
        onClose();
      }}
      returnFocusRef={returnFocusRef}
    >
      <div className="composer-library-picker">
        <label className="composer-library-picker__search">
          <Search size={16} aria-hidden="true" />
          <input
            type="search"
            aria-label={t("search")}
            placeholder={t("search")}
            value={query}
            disabled={opening !== null}
            onChange={(event) => {
              resetList();
              setQuery(event.target.value);
            }}
          />
        </label>
        <Tabs value={scope} onValueChange={changeScope}>
          <TabList appearance="underline" className="composer-library-picker__scopes" aria-label={t("scopes")}>
            {COMPOSER_LIBRARY_SCOPES.map((value) => (
              <Tab key={value} value={value} disabled={opening !== null}>
                {t(value)}
              </Tab>
            ))}
          </TabList>
          <TabPanel value={scope} className="composer-library-picker__results" aria-busy={loading}>
            {files.length > 0 ? (
              <ul className="composer-library-picker__files">
                {files.map((file) => (
                  <li key={libraryFileKey(file)}>
                    <button
                      type="button"
                      className="composer-library-picker__file"
                      disabled={atLimit || opening !== null}
                      onClick={() => void add(file)}
                    >
                      <FileText size={17} aria-hidden="true" />
                      <span>
                        <span className="composer-library-picker__name">{file.filename}</span>
                        <span className="composer-library-picker__metadata">
                          {t(file.scope)}
                          {Number.isFinite(Date.parse(file.date))
                            ? ` · ${format.dateTime(new Date(file.date), { dateStyle: "medium" })}`
                            : null}
                        </span>
                      </span>
                    </button>
                  </li>
                ))}
              </ul>
            ) : null}
            {loading ? (
              <p className="composer-library-picker__hint" role="status">
                {t("loading")}
              </p>
            ) : null}
            {!loading && listError ? (
              <div className="composer-library-picker__state">
                <p role="alert">{t("list_error")}</p>
                <button
                  type="button"
                  className="btn sec"
                  onClick={() => {
                    setListError(false);
                    setLoading(true);
                    setRetry((value) => value + 1);
                  }}
                >
                  {t("retry")}
                </button>
              </div>
            ) : null}
            {!loading && !listError && files.length === 0 && !hasMore ? (
              <div className="composer-library-picker__state">
                <p>{t(scope === "recent" && !query.trim() ? "empty_recent" : "empty")}</p>
                {scope === "recent" && !query.trim() ? (
                  <button type="button" className="btn sec" onClick={() => changeScope("team")}>
                    {t("browse_team")}
                  </button>
                ) : null}
              </div>
            ) : null}
            {!loading && !listError && hasMore ? (
              <button
                type="button"
                className="btn sec composer-library-picker__more"
                disabled={opening !== null}
                onClick={() => {
                  setLoading(true);
                  setOffset((value) => value + LIBRARY_PAGE_SIZE);
                }}
              >
                {t("more")}
              </button>
            ) : null}
          </TabPanel>
        </Tabs>
        {atLimit || selectionError ? (
          <p className="composer-library-picker__error" role="alert">
            {atLimit ? t("limit") : selectionError}
          </p>
        ) : null}
        <p className="composer-library-picker__hint" role="status">
          {opening ? t("opening", { name: opening }) : t("copy_hint")}
        </p>
      </div>
    </Dialog>
  );
}
