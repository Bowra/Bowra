"use client";

import { useTranslations } from "next-intl";
import { ChevronRight } from "@/components/icons";
import { record } from "@/lib/coach-operations";
import styles from "./coach-case-details.module.css";

const MAX_CASES = 8;
const MAX_TEXT = 1600;

/** Only readable point text belongs in this excerpt; contracts and Tool identifiers stay in the original review. */
function pointText(value: unknown): string | null {
  const points = record(value).points;
  if (!Array.isArray(points)) return null;
  return points.map((point) => record(point).text).filter((text): text is string => typeof text === "string").join("\n") || null;
}

function Excerpt({ value }: { value: unknown }) {
  const t = useTranslations("coach_operation.case_details");
  if (typeof value !== "string") return <p className={styles.value}>{t("unavailable")}</p>;
  return <>
    <p className={styles.value}>{value.slice(0, MAX_TEXT)}</p>
    {value.length > MAX_TEXT ? <p className="ui-card__text">{t("truncated")}</p> : null}
  </>;
}

/** Saved proposal content only: refreshing decisions never substitutes today's suite for the original change. */
export function CoachCaseDetails({ proposal }: { proposal: Record<string, unknown> }) {
  const t = useTranslations("coach_operation.case_details");
  const operation = useTranslations("coach_operation");
  const fields = useTranslations("agents_marketplace");
  const items = Array.isArray(proposal.suggested_cases) ? proposal.suggested_cases.map(record) : [];
  const visible = items.filter((item) => item.held_out !== true);
  if (!items.length) return null;
  return <details className={styles.details}>
    <summary className={`ui-card__details-toggle ${styles.toggle}`}>
      <ChevronRight size={14} aria-hidden />
      {t("open")}
    </summary>
    <div className={styles.body}>
      <p className="ui-card__text">{t("saved_note")}</p>
      {visible.slice(0, MAX_CASES).map((item, index) => {
        const edit = record(item.edit);
        const before = record(edit.before);
        const changed = Array.isArray(edit.changed_fields) ? edit.changed_fields : [];
        const edited = item.edit != null;
        const state = ["pending", "accepted", "dismissed"].includes(String(item.decision_status)) ? String(item.decision_status) : "unknown";
        const taskChanged = edited && changed.includes("task");
        const pointsChanged = edited && changed.includes("expected_contract");
        const points = pointText(record(item.case_spec).expected_contract);
        const otherFields = (["scorer_config", "valid_until"] as const).filter((field) => changed.includes(field));
        return <section className={styles.item} key={index} aria-label={t("item", { number: index + 1 })}>
          <p className={styles.heading}>{t("item", { number: index + 1 })} · {operation(`statuses.${state}`)}</p>
          {taskChanged ? <div>
            <p className={styles.label}>{t("before")}</p>
            <Excerpt value={before.task} />
            <p className={styles.label}>{t("after")}</p>
            <Excerpt value={item.task} />
          </div> : <div>
            <p className={styles.label}>{fields("version_suggested_case_field_task")}</p>
            <Excerpt value={item.task} />
          </div>}
          {pointsChanged ? <div>
            <p className={styles.label}>{t("points_before")}</p>
            <Excerpt value={pointText(record(before.case_spec).expected_contract)} />
            <p className={styles.label}>{t("points_after")}</p>
            <Excerpt value={points} />
          </div> : points ? <div>
            <p className={styles.label}>{fields("version_suggested_case_field_expected_contract")}</p>
            <Excerpt value={points} />
          </div> : null}
          {otherFields.length ? <p className="ui-card__text">{t("other_fields", { fields: otherFields.map((field) => fields(`version_suggested_case_field_${field}`)).join(", ") })}</p> : null}
        </section>;
      })}
      {visible.length > MAX_CASES ? <p className="ui-card__text">{t("more", { count: visible.length - MAX_CASES })}</p> : null}
      {items.some((item) => item.held_out === true) ? <p className="ui-card__text">{t("held_out")}</p> : null}
    </div>
  </details>;
}
