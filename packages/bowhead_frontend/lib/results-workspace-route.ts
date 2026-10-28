export type ResultsViewerMode = "preview" | "raw" | "details";
export type ResultsRouteSurface = "viewer" | "explorer";

export type ResultsRoute =
  | { type: "explorer"; explicit: true; source: "canonical"; surface: "explorer" }
  | {
      type: "report";
      explicit: boolean;
      source: "canonical" | "legacy" | "default";
      surface: ResultsRouteSurface;
    }
  | {
      type: "artifact";
      artifactId: string;
      mode: ResultsViewerMode;
      explicit: true;
      source: "canonical" | "legacy";
      surface: ResultsRouteSurface;
    }
  | {
      type: "code";
      explicit: true;
      source: "canonical" | "legacy";
      surface: ResultsRouteSurface;
    }
  | { type: "legacy-data"; explicit: true; source: "legacy"; surface: "viewer" }
  | {
      type: "invalid";
      explicit: true;
      source: "canonical";
      surface: ResultsRouteSurface;
    };

export type ResultsRouteTarget =
  | { type: "report" }
  | { type: "artifact"; artifactId: string; mode?: ResultsViewerMode }
  | { type: "code" };

const RESULT_TOKEN = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$/;

export function isStableResultToken(value: string): boolean {
  return RESULT_TOKEN.test(value);
}

export function parseResultsRoute(
  input: URLSearchParams | string,
): ResultsRoute {
  const params =
    typeof input === "string" ? new URLSearchParams(input) : input;
  const surface: ResultsRouteSurface = params.get("results") === "all"
    ? "explorer"
    : "viewer";
  if (params.has("result")) {
    const result = params.get("result") ?? "";
    if (result === "report") {
      return { type: "report", explicit: true, source: "canonical", surface };
    }
    if (result === "code") {
      return { type: "code", explicit: true, source: "canonical", surface };
    }
    if (!isStableResultToken(result)) {
      return { type: "invalid", explicit: true, source: "canonical", surface };
    }
    return {
      type: "artifact",
      artifactId: result,
      mode: parseResultsViewerMode(params.get("mode")),
      explicit: true,
      source: "canonical",
      surface,
    };
  }

  if (surface === "explorer") {
    return { type: "explorer", explicit: true, source: "canonical", surface };
  }

  const legacyTab = params.get("tab");
  if (legacyTab === "data") {
    const artifactId = params.get("focus") ?? "";
    if (isStableResultToken(artifactId)) {
      return {
        type: "artifact",
        artifactId,
        mode: "preview",
        explicit: true,
        source: "legacy",
        surface: "viewer",
      };
    }
    return {
      type: "legacy-data",
      explicit: true,
      source: "legacy",
      surface: "viewer",
    };
  }
  if (legacyTab === "sql") {
    return {
      type: "code",
      explicit: true,
      source: "legacy",
      surface: "viewer",
    };
  }
  if (legacyTab === "report") {
    return {
      type: "report",
      explicit: true,
      source: "legacy",
      surface: "viewer",
    };
  }
  return {
    type: "report",
    explicit: false,
    source: "default",
    surface: "viewer",
  };
}

/**
 * Results routing only replaces an existing URL session with the results' session; **never add a session to a clean URL**. The page's session sync owns `?session=`.
 * Page writes sync into Next searchParams; adding `?session=` here after New Work based on an old session ID that has not yet
 * been cleared makes the guard treat it as a deep link and return the user to that session.
 */
function syncResultsSession(url: URL, sessionId: string | null | undefined): void {
  if (sessionId && url.searchParams.has("session")) url.searchParams.set("session", sessionId);
}

export function writeResultsRoute(
  currentUrl: URL,
  target: ResultsRouteTarget,
  sessionId?: string | null,
): URL {
  const next = new URL(currentUrl.toString());
  syncResultsSession(next, sessionId);
  next.searchParams.delete("tab");
  next.searchParams.delete("focus");
  next.searchParams.delete("results");

  if (target.type === "report") {
    next.searchParams.set("result", "report");
    next.searchParams.delete("mode");
    return next;
  }
  if (target.type === "code") {
    next.searchParams.set("result", "code");
    next.searchParams.delete("mode");
    return next;
  }

  if (!isStableResultToken(target.artifactId)) {
    throw new TypeError("Invalid result artifact id");
  }
  next.searchParams.set("result", target.artifactId);
  next.searchParams.set("mode", target.mode ?? "preview");
  return next;
}

export function writeResultsExplorerRoute(
  currentUrl: URL,
  target: ResultsRouteTarget | null,
  sessionId?: string | null,
): URL {
  const next = new URL(currentUrl.toString());
  syncResultsSession(next, sessionId);
  next.searchParams.set("results", "all");
  next.searchParams.delete("tab");
  next.searchParams.delete("focus");

  if (target === null) {
    next.searchParams.delete("result");
    next.searchParams.delete("mode");
    return next;
  }
  if (target.type === "report" || target.type === "code") {
    next.searchParams.set("result", target.type);
    next.searchParams.delete("mode");
    return next;
  }
  if (!isStableResultToken(target.artifactId)) {
    throw new TypeError("Invalid result artifact id");
  }
  next.searchParams.set("result", target.artifactId);
  next.searchParams.set("mode", target.mode ?? "preview");
  return next;
}

export function clearResultsRoute(
  currentUrl: URL,
  sessionId?: string | null,
): URL {
  const next = new URL(currentUrl.toString());
  syncResultsSession(next, sessionId);
  next.searchParams.delete("result");
  next.searchParams.delete("mode");
  next.searchParams.delete("tab");
  next.searchParams.delete("focus");
  next.searchParams.delete("results");
  return next;
}

/**
 * Normalize only canonical fields that have a single representation.
 * Legacy tab/focus parameters remain untouched on read; an explicit
 * navigation removes them through writeResultsRoute.
 */
export function normalizeCanonicalResultsUrl(currentUrl: URL): URL {
  const next = new URL(currentUrl.toString());
  const route = parseResultsRoute(next.searchParams);
  if (route.type === "explorer") {
    next.searchParams.delete("mode");
  } else if (
    route.type === "artifact"
    && route.source === "canonical"
    && next.searchParams.get("mode") !== route.mode
  ) {
    next.searchParams.set("mode", route.mode);
  } else if (
    (route.type === "report" || route.type === "code")
    && route.source === "canonical"
  ) {
    next.searchParams.delete("mode");
  }
  return next;
}

function parseResultsViewerMode(value: string | null): ResultsViewerMode {
  return value === "raw" || value === "details" ? value : "preview";
}
