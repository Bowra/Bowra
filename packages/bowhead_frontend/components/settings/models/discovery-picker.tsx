"use client";

import { useMemo, useRef, useState, type KeyboardEvent } from "react";
import { useTranslations } from "next-intl";

import { Check, Search, XSm } from "@/components/icons";
import { DialogShell } from "@/components/ui/dialog";
import type { DiscoveryCandidate } from "@/lib/llm-config-v2-api";

export function DiscoveryPicker({
  candidates,
  truncated,
  onAdd,
  onClose,
}: {
  candidates: DiscoveryCandidate[];
  truncated: boolean;
  onAdd: (modelIds: string[]) => void;
  onClose: () => void;
}) {
  const t = useTranslations("settings.models.v2");
  const [query, setQuery] = useState("");
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const visible = useMemo(() => {
    const needle = query.trim().toLocaleLowerCase();
    if (!needle) return candidates;
    return candidates.filter((candidate) =>
      candidate.model_id.toLocaleLowerCase().includes(needle));
  }, [candidates, query]);

  // Follow listbox keyboard conventions: Tab stops on one item (roving tabIndex); Up/Down and Home/End move
  // among selectable items, Space or Enter selects; Down from the search box enters the list.
  const listRef = useRef<HTMLDivElement | null>(null);
  const [activeId, setActiveId] = useState<string | null>(null);
  const selectable = visible.filter((candidate) => !candidate.already_added);
  const tabStopId = selectable.some((candidate) => candidate.model_id === activeId)
    ? activeId
    : (selectable[0]?.model_id ?? null);

  const moveFocus = (key: string): boolean => {
    const options = Array.from(
      listRef.current?.querySelectorAll<HTMLButtonElement>('[role="option"]:not(:disabled)') ?? [],
    );
    if (!options.length) return false;
    const current = options.indexOf(document.activeElement as HTMLButtonElement);
    const last = options.length - 1;
    const next =
      key === "ArrowDown" ? (current < 0 ? 0 : Math.min(current + 1, last))
      : key === "ArrowUp" ? (current < 0 ? last : Math.max(current - 1, 0))
      : key === "Home" ? 0
      : key === "End" ? last
      : null;
    if (next === null) return false;
    options[next].focus();
    return true;
  };
  const onListKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (moveFocus(event.key)) event.preventDefault();
  };
  const onSearchKeyDown = (event: KeyboardEvent<HTMLInputElement>) => {
    if (event.key !== "ArrowDown") return;
    const target = listRef.current?.querySelector<HTMLButtonElement>('[role="option"][tabindex="0"]');
    if (!target) return;
    event.preventDefault();
    target.focus();
  };

  const toggle = (modelId: string) => {
    setSelected((current) => {
      const next = new Set(current);
      if (next.has(modelId)) next.delete(modelId);
      else next.add(modelId);
      return next;
    });
  };

  return (
    <DialogShell
      onClose={onClose}
      mount="inline"
      labelledBy="mset-discovery-title"
      backdropClassName="mset-scrim discovery-scrim"
      panelClassName="discovery-picker"
    >
      <header className="discovery-head">
        <div>
          <span>{t("discovery_kicker")}</span>
          <h2 id="mset-discovery-title">{t("discovery_title")}</h2>
          <p>{t("discovery_body")}</p>
        </div>
        <button type="button" className="iconbtn" aria-label={t("close")} onClick={onClose}>
          <XSm size={16} />
        </button>
      </header>
      <label className="discovery-search">
        <Search size={14} />
        <input
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          placeholder={t("discovery_search")}
          name="model_discovery_search"
          autoComplete="off"
          spellCheck={false}
          onKeyDown={onSearchKeyDown}
        />
      </label>
      {truncated ? <div className="discovery-note">{t("discovery_truncated")}</div> : null}
      <div
        ref={listRef}
        className="discovery-list"
        role="listbox"
        aria-multiselectable="true"
        aria-labelledby="mset-discovery-title"
        onKeyDown={onListKeyDown}
      >
        {visible.length ? visible.map((candidate) => {
          const disabled = candidate.already_added;
          const checked = selected.has(candidate.model_id);
          return (
            <button
              type="button"
              key={candidate.model_id}
              className={`discovery-row${checked ? " selected" : ""}`}
              role="option"
              aria-selected={checked}
              disabled={disabled}
              tabIndex={candidate.model_id === tabStopId ? 0 : -1}
              onFocus={() => setActiveId(candidate.model_id)}
              onClick={() => toggle(candidate.model_id)}
            >
              <span className="discovery-check">{checked ? <Check size={12} /> : null}</span>
              <span className="discovery-model">{candidate.model_id}</span>
              <span className="discovery-state">
                {disabled ? t("already_added") : t("capabilities_unknown")}
              </span>
            </button>
          );
        }) : <div className="discovery-empty">{t("discovery_empty")}</div>}
      </div>
      <footer className="discovery-foot">
        <span>{t("discovery_selected", { count: selected.size })}</span>
        <div>
          <button type="button" className="btn ghost sm" onClick={onClose}>{t("cancel")}</button>
          <button
            type="button"
            className="btn pri sm"
            disabled={!selected.size}
            onClick={() => onAdd([...selected])}
          >
            {t("add_selected")}
          </button>
        </div>
      </footer>
    </DialogShell>
  );
}
