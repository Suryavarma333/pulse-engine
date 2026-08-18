import type {
  ApiPage,
  DashboardData,
  DemoResult,
  JsonObject,
  OperationalStatus,
  Prediction,
  ScalingAction,
  ScheduledEvent,
  Snapshot,
} from "./types";

export const READ_ONLY_PATHS = [
  "/api/v1/status",
  "/api/v1/snapshots",
  "/api/v1/predictions",
  "/api/v1/scaling-actions",
  "/api/v1/load-shedding-events",
  "/api/v1/scheduled-events",
  "/api/v1/results/summary",
] as const;

const MIN_WINDOW_SECONDS = 60;
const MAX_WINDOW_SECONDS = 86_400;
const ROW_LIMIT = 240;

export function boundedWindow(now: Date, requestedSeconds: number) {
  const seconds = Math.min(
    MAX_WINDOW_SECONDS,
    Math.max(MIN_WINDOW_SECONDS, Math.floor(requestedSeconds)),
  );
  return {
    from: new Date(now.getTime() - seconds * 1_000).toISOString(),
    to: now.toISOString(),
    seconds,
  };
}

async function getJson<T>(url: URL, signal: AbortSignal): Promise<T> {
  const response = await fetch(url, {
    method: "GET",
    cache: "no-store",
    credentials: "omit",
    signal,
  });
  if (!response.ok) {
    throw new Error(`${url.pathname} returned ${response.status}`);
  }
  return (await response.json()) as T;
}

export async function fetchDashboard(
  baseUrl: string,
  requestedWindowSeconds: number,
  signal: AbortSignal,
  now = new Date(),
): Promise<DashboardData> {
  const window = boundedWindow(now, requestedWindowSeconds);
  const base = baseUrl.replace(/\/$/, "");
  const url = (path: (typeof READ_ONLY_PATHS)[number], withWindow = true) => {
    const value = new URL(`${base}${path}`);
    if (withWindow) {
      value.searchParams.set("from", window.from);
      value.searchParams.set("to", window.to);
      value.searchParams.set("limit", String(ROW_LIMIT));
    }
    return value;
  };
  const requests = [
    getJson<OperationalStatus>(url("/api/v1/status", false), signal),
    getJson<ApiPage<Snapshot>>(url("/api/v1/snapshots"), signal),
    getJson<ApiPage<Prediction>>(url("/api/v1/predictions"), signal),
    getJson<ApiPage<ScalingAction>>(url("/api/v1/scaling-actions"), signal),
    getJson<ApiPage<JsonObject>>(url("/api/v1/load-shedding-events"), signal),
    getJson<ApiPage<ScheduledEvent>>(url("/api/v1/scheduled-events"), signal),
    getJson<ApiPage<DemoResult>>(url("/api/v1/results/summary"), signal),
  ] as const;
  const settled = await Promise.allSettled(requests);
  if (signal.aborted) {
    throw new DOMException("Polling request was cancelled", "AbortError");
  }
  const warnings = settled.flatMap((result, index) =>
    result.status === "rejected"
      ? [`${READ_ONLY_PATHS[index]} unavailable: ${safeMessage(result.reason)}`]
      : [],
  );
  return {
    status: value(settled[0], null),
    snapshots: value(settled[1], emptyPage<Snapshot>()).items,
    predictions: value(settled[2], emptyPage<Prediction>()).items,
    actions: value(settled[3], emptyPage<ScalingAction>()).items,
    sheddingEvents: value(settled[4], emptyPage<JsonObject>()).items,
    scheduledEvents: value(settled[5], emptyPage<ScheduledEvent>()).items,
    results: value(settled[6], emptyPage<DemoResult>()).items,
    warnings,
    fetchedAt: now.toISOString(),
    window,
  };
}

function value<T>(result: PromiseSettledResult<T>, fallback: T): T {
  return result.status === "fulfilled" ? result.value : fallback;
}

function emptyPage<T extends JsonObject>(): ApiPage<T> {
  return { items: [], next_cursor: null, from: "", to: "" };
}

function safeMessage(reason: unknown): string {
  return reason instanceof Error ? reason.message : "request failed";
}
