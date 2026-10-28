
import { isNetworkFailure } from "@/lib/transport-error";
export type ResultsWorkspaceErrorClass =
  | "unavailable"
  | "unsupported"
  | "too_large"
  | "corrupt"
  | "network"
  | "unknown";

export class ResultsWorkspaceError extends Error {
  readonly errorClass: ResultsWorkspaceErrorClass;

  constructor(errorClass: ResultsWorkspaceErrorClass) {
    super(errorClass);
    this.name = "ResultsWorkspaceError";
    this.errorClass = errorClass;
  }
}

export function classifyResultsResponse(status: number): ResultsWorkspaceErrorClass {
  if (status === 401 || status === 403 || status === 404 || status === 410) {
    return "unavailable";
  }
  if (status === 413) return "too_large";
  if (status === 415) return "unsupported";
  if (status === 422) return "corrupt";
  if (status >= 500) return "unknown";
  return "unknown";
}

export function classifyResultsError(error: unknown): ResultsWorkspaceErrorClass {
  if (error instanceof ResultsWorkspaceError) return error.errorClass;
  if (isNetworkFailure(error)) return "network";
  if (error instanceof SyntaxError) return "corrupt";
  return "unknown";
}
