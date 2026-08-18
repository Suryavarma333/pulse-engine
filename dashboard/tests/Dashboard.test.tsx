import { act, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { Dashboard } from "../components/Dashboard";
import type { DashboardData, SnapshotV1 } from "../lib/types";
import snapshotPageV1 from "./fixtures/snapshot-page-v1.json";

const snapshotFixture = snapshotPageV1.items[0] as SnapshotV1;

afterEach(() => {
  vi.useRealTimers();
});

describe("Pulse dashboard", () => {
  it("renders healthy traffic, capacity, policy, audit, providers, schedule and results", async () => {
    render(<Dashboard loader={async () => healthyData()} pollIntervalMs={60_000} />);

    expect(await screen.findByText("All systems nominal")).toBeInTheDocument();
    expect(screen.getByText("42.5 rps")).toBeInTheDocument();
    expect(screen.getByText("84.0 ms")).toBeInTheDocument();
    expect(screen.getByText("Pulse Demo: Diwali")).toBeInTheDocument();
    expect(screen.getByText("Scale Ahead")).toBeInTheDocument();
    expect(screen.getByText("Cloudfront")).toBeInTheDocument();
    expect(screen.getByText("Sudden Spike")).toBeInTheDocument();
    expect(screen.getByText("18.0 s")).toBeInTheDocument();
    expect(screen.getByText("Browser access is read-only")).toBeInTheDocument();
  });

  it("renders empty and unavailable states without substituting metrics", async () => {
    const data = healthyData();
    data.status = null;
    data.snapshots = [];
    data.predictions = [];
    data.actions = [];
    data.scheduledEvents = [];
    data.results = [];
    data.warnings = ["/api/v1/status unavailable: connection refused"];
    render(<Dashboard loader={async () => data} pollIntervalMs={60_000} />);

    expect(await screen.findByText("Degraded visibility")).toBeInTheDocument();
    expect(screen.getAllByText("Unavailable").length).toBeGreaterThan(0);
    expect(screen.getByText("No traffic evidence yet")).toBeInTheDocument();
    expect(screen.getByText("No completed run results")).toBeInTheDocument();
    expect(screen.getByText("Partial data")).toBeInTheDocument();
  });

  it("cancels stale polls and the in-flight request on unmount", async () => {
    vi.useFakeTimers();
    const signals: AbortSignal[] = [];
    const loader = vi.fn(async (signal: AbortSignal) => {
      signals.push(signal);
      return healthyData();
    });
    const view = render(<Dashboard loader={loader} pollIntervalMs={1_000} />);

    await act(async () => Promise.resolve());
    expect(loader).toHaveBeenCalledTimes(1);
    await act(async () => vi.advanceTimersByTimeAsync(1_001));
    expect(loader).toHaveBeenCalledTimes(2);
    expect(signals[0].aborted).toBe(true);
    view.unmount();
    expect(signals[1].aborted).toBe(true);
  });

  it("lets operators select a bounded chart window", async () => {
    const loader = vi.fn(async (_signal: AbortSignal, seconds: number) => healthyData(seconds));
    render(<Dashboard loader={loader} pollIntervalMs={60_000} />);
    await waitFor(() => expect(loader).toHaveBeenCalledWith(expect.any(AbortSignal), 3_600));
    act(() => screen.getByRole("button", { name: "15m" }).click());
    await waitFor(() => expect(loader).toHaveBeenCalledWith(expect.any(AbortSignal), 900));
  });
});

function healthyData(seconds = 3_600): DashboardData {
  return {
    status: {
      environment: "local",
      execution_mode: "dry_run",
      global_ceiling: 3,
      state: "protect",
      database_ready: true,
      demo_app_ready: true,
      capacity: { desired: 3, in_service: 2, pending: 1, provider_status: "simulated" },
      capacity_error: null,
      shedding_level: 1,
      endpoint_policy: { checkout: "normal", catalog: "normal", recommendations: "disabled" },
      cooldown_until: null,
      providers: { cloudfront: { status: "healthy", freshness_seconds: 1.2 } },
      workers: [{ name: "realtime detector", status: "healthy" }],
      scheduled: {
        next_due_at: "2026-08-18T10:02:00Z",
        event: { name: "Pulse Demo: Diwali", starts_at: "2026-08-18T10:10:00Z", peak_desired_capacity: 3 },
      },
    },
    snapshots: [
      { ...snapshotFixture, observed_at: "2026-08-18T10:00:02Z", origin_request_rate_rps: 42.5, baseline_request_rate_rps: 8, checkout_p99_latency_ms: 84, checkout_success_rate: 1, asg_desired_capacity: 3, asg_in_service_capacity: 2, pending_capacity: 1 },
      { ...snapshotFixture, id: 2, observed_at: "2026-08-18T10:00:00Z", origin_request_rate_rps: 8, baseline_request_rate_rps: 8, checkout_p99_latency_ms: 60, checkout_success_rate: 1, asg_desired_capacity: 1, asg_in_service_capacity: 1, pending_capacity: 0 },
    ],
    predictions: [
      { id: "p1", created_at: "2026-08-18T10:00:01Z", predicted_peak_at: "2026-08-18T10:00:30Z", predicted_peak_rps: 70 },
      { id: "p0", created_at: "2026-08-18T10:00:00Z", predicted_peak_at: "2026-08-18T10:00:20Z", predicted_peak_rps: 50 },
    ],
    actions: [{ id: "a1", requested_at: "2026-08-18T10:00:01Z", reason_code: "scale_ahead", requested_desired_capacity: 3, applied_desired_capacity: 3, status: "dry_run", execution_mode: "dry_run" }],
    sheddingEvents: [],
    scheduledEvents: [],
    results: [{ id: "r1", scenario_name: "sudden-spike", baseline_type: "pulse", started_at: "2026-08-18T09:00:00Z", checkout_p99_latency_ms: 82, checkout_success_rate: 1, detection_lead_seconds: 18, provisioning_efficiency_pct: 91, warnings: { items: [] } }],
    warnings: [],
    fetchedAt: "2026-08-18T10:00:03Z",
    window: { from: "2026-08-18T09:00:03Z", to: "2026-08-18T10:00:03Z", seconds },
  };
}
