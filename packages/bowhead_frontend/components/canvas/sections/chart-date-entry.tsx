"use client";

import { useEffect, useId, useRef, useState } from "react";
import { useTranslations } from "next-intl";
import { Button } from "@/components/ui/button";
import { Field } from "@/components/ui/field";
import { exactTimeIndex, type TimeRange } from "@/lib/chart/time-window";

/** Optional direct entry beside existing time controls, using only prepared keys. */
export function ChartDateEntry({ domain, range, single, onApply }: {
  domain: Array<string | number>; range: TimeRange; single?: boolean; onApply: (range: TimeRange) => void;
}) {
  const t = useTranslations("chart_block");
  const id = useId();
  const [open, setOpen] = useState(false);
  const [start, setStart] = useState("");
  const [end, setEnd] = useState("");
  const [error, setError] = useState(false);
  const trigger = useRef<HTMLButtonElement>(null);
  const input = useRef<HTMLInputElement>(null);
  useEffect(() => { setOpen(false); }, [range.start, range.end]);
  useEffect(() => { if (open) input.current?.focus(); }, [open]);
  function close() { setOpen(false); trigger.current?.focus(); }
  const toggle = <Button variant="plain" size="sm" onClick={(event) => { trigger.current = event.currentTarget; setStart(String(domain[range.start])); setEnd(String(domain[range.end])); setError(false); setOpen(!open); }}>{t("date_entry")}</Button>;
  if (!open) return toggle;
  function apply() {
    const lower = exactTimeIndex(start, domain), upper = single ? lower : exactTimeIndex(end, domain);
    if (lower < 0 || upper < lower) { setError(true); return; }
    onApply({ start: lower, end: upper }); close();
  }
  return <>{toggle}<div className="flex basis-full flex-wrap items-end gap-2" data-chart-date-entry onKeyDown={(event) => {
    if (event.key === "Enter") { event.preventDefault(); event.stopPropagation(); apply(); }
    if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); close(); }
  }}>
    <Field id={`${id}-start`} label={t(single ? "frame_label" : "window_start")}>
      <input ref={input} id={`${id}-start`} className="ui-input" style={{ width: "10rem" }} value={start} list={`${id}-keys`} onChange={(event) => setStart(event.target.value)} aria-invalid={error} aria-describedby={`${id}-hint`} />
    </Field>
    {!single ? <Field id={`${id}-end`} label={t("window_end")}>
      <input id={`${id}-end`} className="ui-input" style={{ width: "10rem" }} value={end} list={`${id}-keys`} onChange={(event) => setEnd(event.target.value)} aria-invalid={error} aria-describedby={`${id}-hint`} />
    </Field> : null}
    <datalist id={`${id}-keys`}>{domain.map((value) => <option key={value} value={value} />)}</datalist>
    <Button variant="plain" size="sm" onClick={apply}>{t("date_apply")}</Button>
    <Button variant="plain" size="sm" onClick={close}>{t("date_cancel")}</Button>
    <p id={`${id}-hint`} role={error ? "alert" : undefined} className="basis-full text-xs text-muted-foreground">{t(error ? "date_invalid" : "date_hint", { example: domain[0] })}</p>
  </div></>;
}
