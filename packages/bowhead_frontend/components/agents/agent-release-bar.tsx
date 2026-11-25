"use client";

/**
 * The Agent workbench release bar (docs/sprints/agent-page-redesign-2026-11-24/design.md §3, tech-design.md §4):
 * one status sentence, at most one primary action, and the release path as a small second row. Presentational:
 * the state comes from `releaseBarState`, the words and handlers from the caller.
 */

import { useEffect, useId, useRef, useState } from "react";
import { Check, Clock, Info, Minus, TriangleAlert, X } from "lucide-react";
import { useTranslations } from "next-intl";

import { Button } from "@/components/ui/button";
import type {
  ReleaseBarAction,
  ReleaseBarState,
  ReleaseBarStep,
} from "./release-bar-state";

export interface ReleaseBarActionView {
  label: string;
  onSelect: () => void;
  loading?: boolean;
  disabled?: boolean;
}

const STEP_ICON: Partial<Record<ReleaseBarStep["tone"], typeof Check>> = {
  done: Check,
  warn: TriangleAlert,
  bad: X,
  wait: Clock,
  info: Info,
  off: Minus,
};

export function AgentReleaseBar({
  state,
  status,
  meta,
  results,
  actions,
  variant = "page",
}: {
  state: ReleaseBarState;
  status: string;
  meta: string | null;
  /** The meta line's link to the test result, when the state has one. */
  results?: { label: string; onSelect: () => void } | null;
  actions: Partial<Record<ReleaseBarAction, ReleaseBarActionView>>;
  /** "canvas": a band at the top of the Canvas-hosted editor. */
  variant?: "page" | "canvas";
}) {
  const t = useTranslations("agents_marketplace");
  const titleId = useId();
  const stepsId = useId();
  // Narrow screens show the status and actions; the steps open on request.
  const [stepsOpen, setStepsOpen] = useState(false);
  // Announce the status only when it changes, never on mount (the page already shows it).
  const [announcement, setAnnouncement] = useState("");
  const previous = useRef(status);
  useEffect(() => {
    if (previous.current === status) return;
    previous.current = status;
    setAnnouncement(status);
  }, [status]);

  const view = (action: ReleaseBarAction | null) =>
    action ? actions[action] : undefined;
  const primary = view(state.primary);
  const secondary = state.secondary
    .map((action) => [action, view(action)] as const)
    .filter(([, item]) => item);
  const quiet = state.quiet
    .map((action) => [action, view(action)] as const)
    .filter(([, item]) => item);
  const hasActions = Boolean(primary || secondary.length || quiet.length);

  return (
    <section
      className="am-rb"
      aria-labelledby={titleId}
      data-tone={state.tone}
      data-sticky={state.sticky || undefined}
      data-variant={variant}
      data-state={state.id}
    >
      <h2 id={titleId} className="sr-only">
        {t("release_bar_title")}
      </h2>
      <div className="am-rb-head">
        <div className="am-rb-text">
          <p className="am-rb-status">{status}</p>
          {meta || results ? (
            <p className="am-rb-meta">
              {meta}
              {meta && results ? t("version_list_separator") : null}
              {results ? (
                <button
                  type="button"
                  className="am-rb-link"
                  onClick={results.onSelect}
                >
                  {results.label}
                </button>
              ) : null}
            </p>
          ) : null}
        </div>
        {hasActions ? (
          <div className="am-rb-actions">
            {quiet.map(([action, item]) => (
              <Button
                key={action}
                variant="ghost"
                size="sm"
                disabled={item!.disabled}
                loading={item!.loading}
                onClick={item!.onSelect}
              >
                {item!.label}
              </Button>
            ))}
            {secondary.map(([action, item]) => (
              <Button
                key={action}
                variant="secondary"
                size="sm"
                disabled={item!.disabled}
                loading={item!.loading}
                onClick={item!.onSelect}
              >
                {item!.label}
              </Button>
            ))}
            {primary ? (
              <Button
                variant="primary"
                size="sm"
                disabled={primary.disabled}
                loading={primary.loading}
                onClick={primary.onSelect}
              >
                {primary.label}
              </Button>
            ) : null}
          </div>
        ) : null}
      </div>
      {state.steps.length ? (
        <button type="button" className="am-rb-steps-toggle" aria-expanded={stepsOpen} aria-controls={stepsId} onClick={() => setStepsOpen((open) => !open)}>
          {t(stepsOpen ? "release_bar_steps_hide" : "release_bar_steps_show")}
        </button>
      ) : null}
      {state.steps.length ? (
        <ol id={stepsId} className="am-rb-steps" data-open={stepsOpen || undefined}>
          {state.steps.map((step, index) => {
            const Icon = STEP_ICON[step.tone];
            const label = t(
              step.key === "test" && !state.shared
                ? "release_bar_step_test_optional"
                : (`release_bar_step_${step.key}` as never),
            );
            return (
              <li
                key={step.key}
                className="am-rb-step"
                data-tone={step.tone}
                aria-current={step.current ? "step" : undefined}
              >
                <span className="am-rb-dot" aria-hidden>
                  {Icon ? <Icon size={11} strokeWidth={2.6} /> : index + 1}
                </span>
                {label}
                {step.tone !== "now" ? (
                  <span className="sr-only">
                    {t("release_bar_step_state", {
                      state: t(`release_bar_step_tone_${step.tone}` as never),
                    })}
                  </span>
                ) : null}
              </li>
            );
          })}
        </ol>
      ) : null}
      <p className="sr-only" role="status" aria-live="polite">
        {announcement}
      </p>
    </section>
  );
}
