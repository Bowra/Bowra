"use client";

/**
 * Form parts for public pages: shell (`PublicShell`), field, password input, password rules, form-level errors.
 *
 * No AdminShell (no sidebar or top bar); for signed-out pages. Styles live in styles/public-pages.css.
 */

import {
  type FocusEventHandler,
  type ReactNode,
  type Ref,
  useId,
  useState,
} from "react";
import { useTranslations } from "next-intl";

import { ErrorNotice } from "./error-notice";
import { AlertCircle, Check } from "./icons";
import { PublicBanner, PublicShell, type PublicShellProps } from "./public-shell";
import type {
  ErrorDiagnostics,
  ErrorSurface,
} from "@/lib/error-presentation";
import { useErrorDiagnosticsCapability } from "@/lib/use-error-diagnostics-capability";

export function AuthShell(props: PublicShellProps) {
  return <PublicShell {...props} />;
}

/** Accessibility attributes a field control needs: `<label for>` association; hints and errors via `aria-describedby`. */
export interface FieldControlProps {
  id: string;
  "aria-describedby"?: string;
  "aria-invalid"?: true;
}

export function AuthField({
  label, children, hint, error, aside, rules,
}: {
  label: string;
  children: ReactNode | ((control: FieldControlProps) => ReactNode);
  hint?: ReactNode;
  /** Error under the field (12px error color + icon); when set, the control gets `aria-invalid`. */
  error?: string | null;
  /** Right side of the label row ("Forgot password?", "Optional"). Placed after the control in the DOM so it follows it in Tab order. */
  aside?: ReactNode;
  /** Password rules (checked live), associated via `aria-describedby`. */
  rules?: ReactNode;
}) {
  const id = useId();
  const hintId = hint ? `${id}-hint` : undefined;
  const rulesId = rules ? `${id}-rules` : undefined;
  const errorId = error ? `${id}-error` : undefined;
  const describedBy = [errorId, rulesId, hintId].filter(Boolean).join(" ") || undefined;
  const control: FieldControlProps = {
    id,
    ...(describedBy ? { "aria-describedby": describedBy } : null),
    ...(error ? { "aria-invalid": true as const } : null),
  };
  return (
    <div className="pub-field">
      <label className="pub-field__label" htmlFor={id}>
        {label}
      </label>
      {typeof children === "function" ? children(control) : children}
      {aside ? <span className="pub-field__aside">{aside}</span> : null}
      {rules ? <div id={rulesId}>{rules}</div> : null}
      {hint ? (
        <p className="pub-help" id={hintId}>
          {hint}
        </p>
      ) : null}
      {error ? (
        <p className="pub-field-error" id={errorId}>
          <AlertCircle size={12} aria-hidden="true" />
          <span>{error}</span>
        </p>
      ) : null}
    </div>
  );
}

/** Password rules matching the server (at least 8 characters, a letter, a digit); each one ticks as the user types. */
export function passwordRuleState(value: string) {
  return {
    length: value.length >= 8,
    letter: /\p{L}/u.test(value),
    digit: /\p{Nd}/u.test(value),
  };
}

export function passwordMeetsRules(value: string): boolean {
  const state = passwordRuleState(value);
  return state.length && state.letter && state.digit;
}

export function PasswordRules({ value }: { value: string }) {
  const t = useTranslations("auth.password_rules");
  const state = passwordRuleState(value);
  const rules: Array<[keyof typeof state, string]> = [
    ["length", t("length")],
    ["letter", t("letter")],
    ["digit", t("digit")],
  ];
  return (
    <ul className="pub-rules" aria-label={t("aria")}>
      {rules.map(([key, label]) => (
        <li key={key} data-met={state[key] ? "true" : "false"}>
          <span className="pub-rules__mark" aria-hidden="true">
            {state[key] ? <Check size={12} /> : <span className="pub-rules__dot" />}
          </span>
          <span>{label}</span>
          <span className="pub-sr-only">
            {state[key] ? t("met") : t("unmet")}
          </span>
        </li>
      ))}
    </ul>
  );
}

export interface AuthErrorDiagnostics {
  status?: number;
  code?: string;
  technicalMessage?: string;
  requestId?: string;
}

interface AuthErrorDiagnosticLabels {
  summary: string;
  status: string;
  code: string;
  technicalMessage: string;
  requestId: string;
}

/**
 * Errors not tied to a field: a banner at the top of the form, icon plus text. Technical details (status, error code, request ID, raw server text)
 * are shown collapsed only when error diagnostics are on; otherwise only the user-facing sentence is shown.
 */
export function AuthError({
  message,
  diagnostics,
  diagnosticLabels,
  publishedDiagnostics,
  surface = "authentication",
  tone = "error",
  focusKey,
}: {
  message: string | null;
  diagnostics?: AuthErrorDiagnostics | null;
  diagnosticLabels?: AuthErrorDiagnosticLabels;
  publishedDiagnostics?: ErrorDiagnostics | null;
  surface?: ErrorSurface;
  tone?: "error" | "warning" | "info";
  focusKey?: unknown;
}) {
  if (!message) return null;
  const wantsDiagnostics = Boolean(
    publishedDiagnostics
    || (diagnostics
      && diagnosticLabels
      && (
        diagnostics.status !== undefined
        || diagnostics.code
        || diagnostics.technicalMessage
        || diagnostics.requestId
      )),
  );
  if (!wantsDiagnostics) {
    return <AuthErrorBanner message={message} tone={tone} focusKey={focusKey} />;
  }
  return (
    <DiagnosableAuthError
      message={message}
      diagnostics={diagnostics}
      diagnosticLabels={diagnosticLabels}
      publishedDiagnostics={publishedDiagnostics ?? null}
      surface={surface}
      tone={tone}
      focusKey={focusKey}
    />
  );
}

function DiagnosableAuthError({
  message,
  diagnostics,
  diagnosticLabels,
  publishedDiagnostics,
  surface,
  tone,
  focusKey,
}: {
  message: string;
  diagnostics?: AuthErrorDiagnostics | null;
  diagnosticLabels?: AuthErrorDiagnosticLabels;
  publishedDiagnostics: ErrorDiagnostics | null;
  surface: ErrorSurface;
  tone: "error" | "warning" | "info";
  focusKey?: unknown;
}) {
  const enabled = useErrorDiagnosticsCapability();
  if (enabled && publishedDiagnostics) {
    return (
      <ErrorNotice
        message={message}
        diagnostics={publishedDiagnostics}
        surface={surface}
        diagnosticsEnabled
      />
    );
  }
  return (
    <AuthErrorBanner
      message={message}
      tone={tone}
      focusKey={focusKey}
      diagnostics={enabled ? diagnostics : null}
      diagnosticLabels={diagnosticLabels}
    />
  );
}

function AuthErrorBanner({
  message,
  tone,
  focusKey,
  diagnostics,
  diagnosticLabels,
}: {
  message: string;
  tone: "error" | "warning" | "info";
  focusKey?: unknown;
  diagnostics?: AuthErrorDiagnostics | null;
  diagnosticLabels?: AuthErrorDiagnosticLabels;
}) {
  const hasDiagnostics = Boolean(
    diagnostics
    && diagnosticLabels
    && (
      diagnostics.status !== undefined
      || diagnostics.code
      || diagnostics.technicalMessage
      || diagnostics.requestId
    ),
  );
  return (
    <PublicBanner tone={tone} focusKey={focusKey}>
      <span data-testid="auth-error-message">{message}</span>
      {hasDiagnostics && diagnostics && diagnosticLabels ? (
        <details
          data-testid="auth-error-diagnostics"
          className="pub-diagnostics"
        >
          <summary>{diagnosticLabels.summary}</summary>
          <dl>
            {diagnostics.status !== undefined ? (
              <DiagnosticRow
                label={diagnosticLabels.status}
                value={String(diagnostics.status)}
              />
            ) : null}
            {diagnostics.code ? (
              <DiagnosticRow label={diagnosticLabels.code} value={diagnostics.code} />
            ) : null}
            {diagnostics.requestId ? (
              <DiagnosticRow
                label={diagnosticLabels.requestId}
                value={diagnostics.requestId}
              />
            ) : null}
            {diagnostics.technicalMessage ? (
              <DiagnosticRow
                label={diagnosticLabels.technicalMessage}
                value={diagnostics.technicalMessage}
              />
            ) : null}
          </dl>
        </details>
      ) : null}
    </PublicBanner>
  );
}

function DiagnosticRow({ label, value }: { label: string; value: string }) {
  return (
    <>
      <dt>{label}</dt>
      <dd>
        <code>{value}</code>
      </dd>
    </>
  );
}

function EyeIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor"
      strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7-10-7-10-7Z" />
      <circle cx="12" cy="12" r="3" />
    </svg>
  );
}

function EyeOffIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor"
      strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M9.9 4.24A9.1 9.1 0 0 1 12 4c6.5 0 10 7 10 7a13.2 13.2 0 0 1-1.67 2.68M6.6 6.6C3.9 8.24 2 12 2 12s3.5 7 10 7a9.1 9.1 0 0 0 4.9-1.44" />
      <path d="M3 3l18 18" />
      <path d="M9.9 9.9a3 3 0 0 0 4.24 4.24" />
    </svg>
  );
}

/**
 * Password input with a show/hide toggle. The toggle uses `aria-pressed` and is named "Show password / Hide password"; toggling neither moves focus nor clears the value.
 * The field's id / aria attributes come from `AuthField` (`control`).
 */
export function PasswordInput({
  value, onChange, autoComplete, minLength, required, placeholder, ariaLabel, showLabel, hideLabel,
  control, readOnly, onBlur, inputRef, name,
}: {
  value: string;
  onChange: (v: string) => void;
  autoComplete?: string;
  minLength?: number;
  required?: boolean;
  placeholder?: string;
  ariaLabel?: string;
  showLabel?: string;
  hideLabel?: string;
  control?: FieldControlProps;
  readOnly?: boolean;
  onBlur?: FocusEventHandler<HTMLInputElement>;
  inputRef?: Ref<HTMLInputElement>;
  name?: string;
}) {
  const [show, setShow] = useState(false);
  const label = show ? (hideLabel ?? "Hide password") : (showLabel ?? "Show password");
  return (
    <div className="pub-input-wrap">
      <input
        {...control}
        ref={inputRef}
        name={name}
        type={show ? "text" : "password"}
        aria-label={control ? undefined : ariaLabel}
        value={value}
        required={required}
        autoComplete={autoComplete}
        minLength={minLength}
        placeholder={placeholder}
        readOnly={readOnly}
        onBlur={onBlur}
        autoCapitalize="off"
        autoCorrect="off"
        spellCheck={false}
        onChange={(e) => onChange(e.target.value)}
        className="pub-input"
      />
      <button
        type="button"
        onClick={() => setShow((s) => !s)}
        aria-label={label}
        aria-pressed={show}
        title={label}
        className="pub-eye"
      >
        {show ? <EyeOffIcon /> : <EyeIcon />}
      </button>
    </div>
  );
}
