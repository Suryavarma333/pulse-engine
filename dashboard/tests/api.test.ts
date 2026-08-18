import { afterEach, describe, expect, it, vi } from "vitest";

import { boundedWindow, fetchDashboard, READ_ONLY_PATHS } from "../lib/api";
import snapshotPageV1 from "./fixtures/snapshot-page-v1.json";

afterEach(() => vi.unstubAllGlobals());

describe("read-only dashboard API", () => {
  it("clamps the selected chart window", () => {
    const now = new Date("2026-08-18T10:00:00Z");
    expect(boundedWindow(now, 1).seconds).toBe(60);
    expect(boundedWindow(now, 999_999).seconds).toBe(86_400);
  });

  it("uses only bounded GET requests and reports partial failures honestly", async () => {
    const calls: Array<{ url: URL; init: RequestInit }> = [];
    vi.stubGlobal("fetch", vi.fn(async (input: URL | RequestInfo, init: RequestInit) => {
      const url = new URL(String(input));
      calls.push({ url, init });
      if (url.pathname === "/api/v1/snapshots") {
        return response(503, { detail: "database warming" });
      }
      if (url.pathname === "/api/v1/status") {
        return response(200, {
          environment: "local", execution_mode: "dry_run", global_ceiling: 3,
          state: "watch", database_ready: true, demo_app_ready: true, capacity: null,
          capacity_error: null, shedding_level: 0, endpoint_policy: {}, providers: {},
          workers: [], scheduled: {},
        });
      }
      return response(200, { items: [], next_cursor: null, from: "", to: "" });
    }));

    const controller = new AbortController();
    const data = await fetchDashboard(
      "http://agent.example/",
      3_600,
      controller.signal,
      new Date("2026-08-18T10:00:00Z"),
    );

    expect(calls.map((call) => call.url.pathname)).toEqual(READ_ONLY_PATHS);
    expect(calls.every((call) => call.init.method === "GET")).toBe(true);
    expect(calls.every((call) => call.init.credentials === "omit")).toBe(true);
    expect(calls.filter((call) => call.url.pathname !== "/api/v1/status").every(
      (call) => call.url.searchParams.get("limit") === "240",
    )).toBe(true);
    expect(data.snapshots).toEqual([]);
    expect(data.warnings[0]).toContain("/api/v1/snapshots unavailable");
  });

  it("propagates cancellation instead of presenting stale data", async () => {
    const controller = new AbortController();
    controller.abort();
    vi.stubGlobal("fetch", vi.fn(async () => response(200, {})));

    await expect(fetchDashboard("http://agent", 900, controller.signal)).rejects.toMatchObject({
      name: "AbortError",
    });
  });

  it("consumes the production snapshot v1 capacity contract", async () => {
    vi.stubGlobal("fetch", vi.fn(async (input: URL | RequestInfo) => {
      const url = new URL(String(input));
      if (url.pathname === "/api/v1/snapshots") {
        return response(200, snapshotPageV1);
      }
      if (url.pathname === "/api/v1/status") {
        return response(200, {
          environment: "local", execution_mode: "dry_run", global_ceiling: 3,
          state: "watch", database_ready: true, demo_app_ready: true, capacity: null,
          capacity_error: null, shedding_level: 0, endpoint_policy: {}, providers: {},
          workers: [], scheduled: {},
        });
      }
      return response(200, { items: [], next_cursor: null, from: "", to: "" });
    }));

    const data = await fetchDashboard(
      "http://agent.example",
      3_600,
      new AbortController().signal,
      new Date("2026-08-18T12:01:00Z"),
    );

    expect(data.warnings).toEqual([]);
    expect(data.snapshots[0]).toMatchObject({
      schema_version: "pulse.snapshot.v1",
      asg_desired_capacity: 2,
      asg_in_service_capacity: 1,
      pending_capacity: 1,
    });
  });
});

function response(status: number, body: unknown): Response {
  return { ok: status >= 200 && status < 300, status, json: async () => body } as Response;
}
