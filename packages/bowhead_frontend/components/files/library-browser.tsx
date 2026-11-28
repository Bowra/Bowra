"use client";

import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { useTranslations } from "next-intl";
import { Tabs, TabList, Tab } from "@/components/ui/tabs";
import { FILE_CATEGORIES, fileCategoryLabel, type FileDTO } from "@/lib/api";
import { deriveArtifactViewerProfile } from "@/lib/artifact-viewer-profile";
import { appPath } from "@/lib/base-path";
import { fmtSize, MIME_FAMILIES, type FilesTab } from "@/lib/files-taxonomy";
import {
  libraryRead,
  libraryViews,
  type LibraryFile,
  type LibraryView,
  type LibraryPage,
  type LibrarySummary,
} from "@/lib/file-library";
import { formatDateTime, getCurrentLocale } from "@/lib/locale";
import { useSpaceSelection } from "@/lib/use-space";

const filtersDefault = {
  workspace_id: "",
  session_id: "",
  since: "",
  until: "",
  mime_family: "",
  category: "",
  in_kb: "",
  sort: "date",
};
type Props = {
  revision: number;
  onPage: (rows: FileDTO[]) => void;
  onTab: (tab: FilesTab) => void;
  onUpload: () => void;
  onSelection: (ids: string[]) => void;
  selected: string[];
  renderFile: (file: LibraryFile, close: () => void) => ReactNode;
};
const available = (f: LibraryFile) => deriveArtifactViewerProfile(f.mime, f.filename) !== "unsupported";
const date = (value: string) => formatDateTime(value, getCurrentLocale());

function updateFocusUrl(id: string | null, push = false) {
  const url = new URL(window.location.href);
  if (url.searchParams.get("focus") === id) return;
  if (id) url.searchParams.set("focus", id);
  else url.searchParams.delete("focus");
  if (push) window.history.pushState(window.history.state, "", url);
  else window.history.replaceState(window.history.state, "", url);
}

export function LibraryBrowser({ revision, onPage, onTab, selected, onSelection, onUpload, renderFile }: Props) {
  const t = useTranslations("file_library");
  const f = useTranslations("files_page");
  const { items: spaces, activeId } = useSpaceSelection();
  const [view, setView] = useState<LibraryView>("deliverables");
  const [storage, setStorage] = useState(false);
  const [summary, setSummary] = useState<LibrarySummary | null>(null);
  const [summaryError, setSummaryError] = useState(false);
  const [input, setInput] = useState("");
  const [q, setQ] = useState("");
  const [filters, setFilters] = useState(filtersDefault);
  const [cursors, setCursors] = useState<(string | null)[]>([null]);
  const [data, setData] = useState<LibraryPage | null>(null);
  const [error, setError] = useState(false);
  const [loading, setLoading] = useState(true);
  const [retry, setRetry] = useState(0);
  const [file, setFile] = useState<LibraryFile | null>(null);
  const [focus, setFocus] = useState(() =>
    typeof window !== "undefined" ? new URLSearchParams(window.location.search).get("focus") : null,
  );
  const [focusError, setFocusError] = useState(false);
  const [sources, setSources] = useState<Record<string, string>>({});
  const picked = useRef<string | null>(null);
  const selectedManually = useRef(false);
  const cursor = cursors[cursors.length - 1];
  useEffect(() => {
    function syncLocation() {
      const target = new URLSearchParams(window.location.search).get("focus");
      picked.current = null;
      selectedManually.current = false;
      setFile(null);
      setFocusError(false);
      setFocus(target);
      setRetry((n) => n + 1);
    }
    window.addEventListener("popstate", syncLocation);
    return () => window.removeEventListener("popstate", syncLocation);
  }, []);
  const changeView = useCallback(
    (next: LibraryView, manage = false) => {
      updateFocusUrl(null);
      setView(next);
      setStorage(manage);
      setFilters(filtersDefault);
      setInput("");
      setQ("");
      setCursors([null]);
      setFile(null);
      setFocus(null);
      setFocusError(false);
      picked.current = null;
      selectedManually.current = false;
      setData(null);
      onSelection([]);
      onTab(next === "trash" ? "trash" : next === "run" ? "run" : "assets");
    },
    [onSelection, onTab],
  );
  useEffect(() => {
    const handle = setTimeout(() => {
      setQ(input.trim());
    }, 250);
    return () => clearTimeout(handle);
  }, [input]);
  useEffect(() => {
    if (!focus) return;
    setFile(null);
    setFocusError(false);
    const ctrl = new AbortController();
    libraryRead<LibraryFile>("/items/" + encodeURIComponent(focus), ctrl.signal)
      .then((item) => {
        if (ctrl.signal.aborted) return;
        const next = libraryViews.find((v) => item.views.includes(v)) ?? "trash";
        setView(next);
        setStorage(next === "trash");
        onTab(next === "trash" ? "trash" : next === "run" ? "run" : "assets");
        setFile(item);
        picked.current = item.file_id;
        selectedManually.current = true;
      })
      .catch(() => {
        if (!ctrl.signal.aborted) {
          setFile(null);
          setFocusError(true);
        }
      });
    return () => ctrl.abort();
  }, [focus, activeId, revision, onTab]);
  useEffect(() => {
    if (!storage) {
      setSummary(null);
      return;
    }
    const ctrl = new AbortController();
    setSummaryError(false);
    libraryRead<LibrarySummary>("/summary", ctrl.signal)
      .then((s) => {
        if (!ctrl.signal.aborted) setSummary(s);
      })
      .catch(() => {
        if (!ctrl.signal.aborted) setSummaryError(true);
      });
    return () => ctrl.abort();
  }, [storage, revision, activeId, retry]);

  useEffect(() => {
    const ctrl = new AbortController();
    const params = new URLSearchParams({ view });
    if (storage) params.set("retention", "true");
    if (q) params.set("q", q);
    Object.entries(filters).forEach(([key, value]) => {
      if (!value) return;
      if (key === "until" || key === "since") {
        const d = new Date(value + "T00:00:00");
        if (key === "until") d.setDate(d.getDate() + 1);
        params.set(key, d.toISOString());
      } else params.set(key, value);
    });
    if (cursor) params.set("cursor", cursor);
    const desktop = window.matchMedia?.("(min-width: 768px)").matches;
    setLoading(true);
    setError(false);
    setSources({});
    setData(null);
    if (!focus) setFile(null);
    const load = async () => {
      let result = await libraryRead<LibraryPage>("?" + params, ctrl.signal);
      if (ctrl.signal.aborted) return;
      const first = result;
      let newest = result.items.find(available);
      if (
        view === "deliverables" &&
        desktop &&
        !focus &&
        !selectedManually.current &&
        !cursor &&
        !newest &&
        result.next_cursor
      ) {
        // Show the list while looking only at metadata for an eligible preview.
        setData(first);
        onPage(first.items);
        while (!newest && result.next_cursor && !selectedManually.current) {
          params.set("cursor", result.next_cursor);
          result = await libraryRead<LibraryPage>("?" + params, ctrl.signal);
          if (ctrl.signal.aborted) return;
          newest = result.items.find(available);
        }
      }
      if (ctrl.signal.aborted) return;
      setData(first);
      onPage(first.items);
      setSources(
        Object.fromEntries(
          first.items.filter((r) => r.session_id && r.session_title).map((r) => [r.session_id!, r.session_title!]),
        ),
      );
      if (!focus && !selectedManually.current && view === "deliverables" && desktop)
        setFile(newest ?? first.items[0] ?? null);
      if (!focus && selectedManually.current && picked.current)
        setFile(first.items.find((r) => r.file_id === picked.current) ?? null);
      setLoading(false);
    };
    void load().catch(() => {
      if (!ctrl.signal.aborted) {
        setError(true);
        setLoading(false);
        setFile(null);
        setData(null);
        onPage([]);
      }
    });
    return () => ctrl.abort();
  }, [storage, view, q, filters, cursor, focus, revision, retry, activeId, onPage]);

  function resetList() {
    updateFocusUrl(null);
    setCursors([null]);
    setFocus(null);
    setFocusError(false);
    setFile(null);
    setData(null);
    picked.current = null;
    selectedManually.current = false;
    onSelection([]);
  }
  const activeFilters = Object.entries(filters).filter(([key, value]) => value && key !== "sort");
  const filtered = Boolean(q || activeFilters.length);
  function choose(item: LibraryFile) {
    updateFocusUrl(item.file_id, true);
    setFocus(null);
    setFocusError(false);
    picked.current = item.file_id;
    selectedManually.current = true;
    setFile(item);
  }
  function field(key: keyof typeof filters, value: string) {
    resetList();
    setFilters((old) => ({ ...old, [key]: value }));
  }
  return (
    <section className="library-browser">
      <div className="library-nav">
        <Tabs
          value={storage ? "manage" : view}
          onValueChange={(id) => changeView(id === "manage" ? "all" : (id as LibraryView), id === "manage")}
        >
          <TabList appearance="underline">
            {[...libraryViews, "manage"].map((id) => (
              <Tab key={id} value={id}>
                {t(id)}
              </Tab>
            ))}
          </TabList>
        </Tabs>
      </div>
      {storage && (
        <section aria-label={t("manage")} className="library-storage">
          {summaryError ? (
            <button onClick={() => setRetry((n) => n + 1)}>{f("state.retry")}</button>
          ) : (
            summary && (
              <>
                <dl>
                  {[...libraryViews, "trash"].map((id) => (
                    <div key={id}>
                      <dt>{t(id)}</dt>
                      <dd>{summary.counts[id as LibraryView] ?? 0}</dd>
                    </div>
                  ))}
                </dl>
                <p>
                  {t("storage_bytes", {
                    assets: fmtSize(summary.bytes.assets ?? 0),
                    run: fmtSize(summary.bytes.run ?? 0),
                    trash: fmtSize(summary.bytes.trash ?? 0),
                  })}
                </p>
              </>
            )
          )}
          <button className="btn sec" onClick={() => changeView(view === "trash" ? "all" : "trash", true)}>
            {t(view === "trash" ? "all" : "trash")}
          </button>
        </section>
      )}
      <div className="library-tools">
        <input
          type="search"
          aria-label={f("search_placeholder")}
          placeholder={f("search_placeholder")}
          value={input}
          onChange={(e) => {
            resetList();
            setInput(e.target.value);
          }}
        />
        <details>
          <summary>
            {t("filters")}
            {activeFilters.length ? " (" + activeFilters.length + ")" : ""}
          </summary>
          <div className="library-filter-fields">
            <label>
              {t("space")}
              <select value={filters.workspace_id} onChange={(e) => field("workspace_id", e.target.value)}>
                <option value="">{f("filter.all")}</option>
                {spaces.map((s) => (
                  <option key={s.id} value={s.id}>
                    {s.name}
                  </option>
                ))}
              </select>
            </label>
            <label>
              {f("source_session")}
              <input
                list="library-conversations"
                value={filters.session_id}
                onChange={(e) => field("session_id", e.target.value)}
              />
            </label>
            <datalist id="library-conversations">
              {Object.entries(sources).map(([id, title]) => (
                <option key={id} value={title}>
                  {title}
                </option>
              ))}
            </datalist>
            {(["since", "until"] as const).map((k) => (
              <label key={k}>
                {t(k)}
                <input type="date" value={filters[k]} onChange={(e) => field(k, e.target.value)} />
              </label>
            ))}
            <label>
              {f("filter.type")}
              <select value={filters.mime_family} onChange={(e) => field("mime_family", e.target.value)}>
                <option value="">{f("filter.all")}</option>
                {MIME_FAMILIES.filter((v) => v !== "all").map((v) => (
                  <option key={v} value={v}>
                    {f("family." + v)}
                  </option>
                ))}
              </select>
            </label>
            <label>
              {f("filter.biz")}
              <select value={filters.category} onChange={(e) => field("category", e.target.value)}>
                <option value="">{f("filter.all")}</option>
                {FILE_CATEGORIES.map((v) => (
                  <option key={v} value={v}>
                    {fileCategoryLabel(v)}
                  </option>
                ))}
              </select>
            </label>
            <label>
              {f("table.kb")}
              <select value={filters.in_kb} onChange={(e) => field("in_kb", e.target.value)}>
                <option value="">{f("filter.all")}</option>
                <option value="true">{t("included")}</option>
                <option value="false">{t("excluded")}</option>
              </select>
            </label>
            {storage && (
              <label>
                {f("filter.sort")}
                <select value={filters.sort} onChange={(e) => field("sort", e.target.value)}>
                  <option value="date">{f("sort.created_desc")}</option>
                  <option value="size">{f("sort.size_desc")}</option>
                </select>
              </label>
            )}
          </div>
        </details>
        <button className="btn sec" onClick={onUpload}>
          {f("upload_btn")}
        </button>
      </div>
      {focusError && (
        <p role="alert">
          {t("unavailable")} <button onClick={() => changeView("all")}>{t("all")}</button>
        </p>
      )}
      <div className="library-split">
        <div className="library-list" aria-busy={loading}>
          {error ? (
            <p role="alert">
              {f("state.error_title")} <button onClick={() => setRetry((n) => n + 1)}>{f("state.retry")}</button>
            </p>
          ) : data && !data.items.length ? (
            <div className="f-state">
              {filtered ? (
                <>
                  <p>
                    {t("no_match", { conditions: [q, ...activeFilters.map(([, v]) => v)].filter(Boolean).join(", ") })}
                  </p>
                  <button
                    onClick={() => {
                      resetList();
                      setQ("");
                      setInput("");
                      setFilters(filtersDefault);
                    }}
                  >
                    {t("clear")}
                  </button>
                </>
              ) : (
                <>
                  <p>{t("empty", { view: t(view) })}</p>
                  {view === "deliverables" ? (
                    <>
                      {(["uploads", "all"] as const).map((v) => (
                        <button key={v} onClick={() => changeView(v)}>
                          {t(v)}
                        </button>
                      ))}
                    </>
                  ) : (
                    view !== "trash" && <button onClick={onUpload}>{f("upload_btn")}</button>
                  )}
                </>
              )}
            </div>
          ) : (
            data?.items.map((item) => (
              <div className="library-row" key={item.file_id} data-selected={file?.file_id === item.file_id}>
                <input
                  type="checkbox"
                  aria-label={f("select_file", { name: item.filename })}
                  checked={selected.includes(item.file_id)}
                  onChange={() =>
                    onSelection(
                      selected.includes(item.file_id)
                        ? selected.filter((x) => x !== item.file_id)
                        : [...selected, item.file_id],
                    )
                  }
                />
                <div>
                  <button className="library-name" onClick={() => choose(item)}>
                    {item.filename}
                  </button>
                  <small>
                    {f("family." + (item.mime_family ?? "other"))} ·{" "}
                    {date(view === "deliverables" && item.delivered_at ? item.delivered_at : item.created_at)}
                    {item.session_id && (
                      <>
                        {" "}
                        ·{" "}
                        <a href={appPath("/?session=" + encodeURIComponent(item.session_id))}>
                          {item.session_title || f("source_session")}
                        </a>
                      </>
                    )}
                    {item.uncertain_upload && <> · {t("uncertain_upload")}</>}
                    {item.pinned && <> · {t("pinned")}</>}
                  </small>
                  {item.recover_until && <small>{t("recover", { date: date(item.recover_until) })}</small>}
                  {(storage || view === "run") && (
                    <small>
                      {storage && fmtSize(item.size)}
                      {item.retention_days != null && (
                        <>
                          {storage ? " · " : ""}
                          {t("period")} · {item.retention_days ? t("days", { n: item.retention_days }) : t("off")}
                        </>
                      )}
                      {item.retention_reason &&
                        (storage || item.retention_days != null ? " · " : "") + t("reason." + item.retention_reason)}
                    </small>
                  )}
                </div>
              </div>
            ))
          )}
          {(cursors.length > 1 || data?.next_cursor) && (
            <nav className="library-pager">
              <button
                disabled={cursors.length < 2}
                onClick={() => {
                  setCursors((v) => v.slice(0, -1));
                  onSelection([]);
                }}
              >
                {t("previous")}
              </button>
              <button
                disabled={!data?.next_cursor}
                onClick={() => {
                  if (data?.next_cursor) setCursors((v) => [...v, data.next_cursor]);
                  onSelection([]);
                }}
              >
                {t("next")}
              </button>
            </nav>
          )}
        </div>
        {file && (
          <div className="library-reader" key={file.file_id}>
            {renderFile(file, () => {
              updateFocusUrl(null);
              selectedManually.current = true;
              picked.current = null;
              setFile(null);
              setFocus(null);
            })}
          </div>
        )}
      </div>
    </section>
  );
}
