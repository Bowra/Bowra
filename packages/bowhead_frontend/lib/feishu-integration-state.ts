import type {
  FeishuDiagnostics,
  FeishuPublicHealth,
  FeishuSetupChecklistItem,
} from "./feishu-integration-api";

export type FeishuRuntimeProblemKey =
  | "app_id"
  | "app_secret"
  | "verification_token"
  | "encrypt_key"
  | "bot_open_id"
  | "event_store"
  | "long_restart"
  | "long_failed"
  | "long_not_running"
  | "provider_secret"
  | "dev_auth"
  | "unknown_runtime";

export type FeishuRuntimeState =
  | "stopped"
  | "normal"
  | "restart_required"
  | "long_failed"
  | "long_not_running"
  | "event_store"
  | "provider_secret"
  | "dev_auth"
  | "unknown_runtime";

const CREDENTIAL_KEYS = new Set<FeishuRuntimeProblemKey>([
  "app_id",
  "app_secret",
  "verification_token",
  "encrypt_key",
  "bot_open_id",
]);

export function feishuCredentialGaps(
  diagnostics: FeishuDiagnostics,
): FeishuRuntimeProblemKey[] {
  const source =
    diagnostics.missing_credentials ??
    diagnostics.missing_config.filter((key) => CREDENTIAL_KEYS.has(key as FeishuRuntimeProblemKey));
  return source.filter((key): key is FeishuRuntimeProblemKey =>
    CREDENTIAL_KEYS.has(key as FeishuRuntimeProblemKey),
  );
}

export function feishuRuntimeProblemKeys(
  diagnostics: FeishuDiagnostics,
  health: FeishuPublicHealth,
): FeishuRuntimeProblemKey[] {
  if (
    !diagnostics.enabled ||
    !health.enabled ||
    diagnostics.event_ingress === "disabled"
  ) {
    return [];
  }

  const problems = [...feishuCredentialGaps(diagnostics)];
  const add = (key: FeishuRuntimeProblemKey) => {
    if (!problems.includes(key)) problems.push(key);
  };

  if (diagnostics.missing_config.includes("event_store")) add("event_store");
  if (diagnostics.long_connection_restart_required) add("long_restart");
  if (diagnostics.event_ingress === "long_connection") {
    if (diagnostics.long_connection?.start_error) add("long_failed");
    else if (
      !diagnostics.long_connection?.running ||
      diagnostics.long_connection.thread_alive === false
    ) {
      add("long_not_running");
    }
  }
  if (diagnostics.missing_llm_provider_secrets?.length) add("provider_secret");
  if (!diagnostics.prod_auth_mode) add("dev_auth");
  if (health.degraded && problems.length === 0) add("unknown_runtime");
  return problems;
}

export function feishuRuntimeState(
  diagnostics: FeishuDiagnostics,
  health: FeishuPublicHealth,
): FeishuRuntimeState {
  if (
    !diagnostics.enabled ||
    !health.enabled ||
    diagnostics.event_ingress === "disabled"
  ) {
    return "stopped";
  }
  if (feishuCredentialGaps(diagnostics).length > 0) return "stopped";

  const problems = feishuRuntimeProblemKeys(diagnostics, health);
  if (problems.includes("long_restart")) return "restart_required";
  if (problems.includes("long_failed")) return "long_failed";
  if (problems.includes("long_not_running")) return "long_not_running";
  if (problems.includes("event_store")) return "event_store";
  if (problems.includes("provider_secret")) return "provider_secret";
  if (problems.includes("dev_auth")) return "dev_auth";
  if (problems.length > 0 || health.degraded) return "unknown_runtime";
  return "normal";
}

export function feishuAutomaticChecksPassed(
  diagnostics: FeishuDiagnostics,
  health: FeishuPublicHealth,
): boolean {
  return (
    diagnostics.enabled &&
    health.enabled &&
    diagnostics.event_ingress !== "disabled" &&
    diagnostics.ok &&
    health.ok &&
    !diagnostics.degraded &&
    !health.degraded &&
    feishuCredentialGaps(diagnostics).length === 0 &&
    feishuRuntimeProblemKeys(diagnostics, health).length === 0
  );
}

export function feishuAutomaticChecklist(
  diagnostics: FeishuDiagnostics,
): FeishuSetupChecklistItem[] {
  return (diagnostics.setup_checklist ?? []).filter(
    (item) => item.status !== "manual",
  );
}
