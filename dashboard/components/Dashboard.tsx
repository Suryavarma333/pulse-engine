"use client";

import { useCallback, useState } from "react";

import { fetchDashboard } from "../lib/api";
import type { DashboardData, DemoResult, JsonObject } from "../lib/types";
import { usePolling } from "../lib/usePolling";
import { LineChart } from "./LineChart";

const WINDOWS = [
  { label: "15m", seconds: 900 },
  { label: "1h", seconds: 3_600 },
  { label: "6h", seconds: 21_600 },
] as const;

export interface DashboardProps {
  apiBaseUrl?: string;
  pollIntervalMs?: number;
  loader?: (signal: AbortSignal, windowSeconds: number) => Promise<DashboardData>;
}

export function Dashboard({
  apiBaseUrl = process.env.NEXT_PUBLIC_PULSE_API_URL ?? "http://localhost:8100",
  pollIntervalMs = Number(process.env.NEXT_PUBLIC_PULSE_POLL_MS ?? 2_000),
  loader,
}: DashboardProps) {
  const [windowSeconds, setWindowSeconds] = useState(3_600);
  const load = useCallback(
    (signal: AbortSignal) =>
      loader
        ? loader(signal, windowSeconds)
        : fetchDashboard(apiBaseUrl, windowSeconds, signal),
    [apiBaseUrl, loader, windowSeconds],
  );
  const polling = usePolling(load, pollIntervalMs);
  const data = polling.data;
  const status = data?.status;
  const latest = data?.snapshots[0] ?? status?.latest_snapshot ?? null;
  const degraded = !status || data?.warnings.length || !status.database_ready || !status.demo_app_ready;

  return (
    <main className="shell">
      <header className="topbar">
        <div className="brand-block">
          <div className="pulse-mark" aria-hidden="true"><span /></div>
          <div>
            <p className="eyebrow">Predictive traffic control</p>
            <h1>Pulse <span>Command Center</span></h1>
          </div>
        </div>
        <div className="top-actions">
          <div className="window-picker" aria-label="Chart window">
            {WINDOWS.map((option) => (
              <button
                className={option.seconds === windowSeconds ? "active" : ""}
                key={option.seconds}
                onClick={() => setWindowSeconds(option.seconds)}
                type="button"
              >
                {option.label}
              </button>
            ))}
          </div>
          <button className="refresh" type="button" onClick={polling.refresh}>
            Refresh
          </button>
        </div>
      </header>

      <section className={`system-strip ${degraded ? "degraded" : "healthy"}`}>
        <div>
          <span className="status-dot" />
          <strong>{polling.loading ? "Connecting" : degraded ? "Degraded visibility" : "All systems nominal"}</strong>
          <span>{status?.environment ?? "environment unavailable"}</span>
        </div>
        <div className="system-facts">
          <span>Mode <strong>{status?.execution_mode ?? "unknown"}</strong></span>
          <span>Control state <strong>{status?.state ?? "unavailable"}</strong></span>
          <span>Last refresh <strong>{data ? clock(data.fetchedAt) : "—"}</strong></span>
        </div>
      </section>

      {polling.error && <Notice tone="error" title="Dashboard API unavailable" detail={polling.error} />}
      {data?.warnings.map((warning) => (
        <Notice key={warning} tone="warning" title="Partial data" detail={warning} />
      ))}

      <section className="metric-grid" aria-label="Current platform metrics">
        <Metric label="Traffic now" value={metric(latest, "origin_request_rate_rps", " rps")} detail="Origin request rate" accent="cyan" />
        <Metric label="Checkout p99" value={metric(latest, "checkout_p99_latency_ms", " ms")} detail={successDetail(latest)} accent="green" />
        <Metric label="Capacity" value={number(status?.capacity?.desired)} detail={`${number(status?.capacity?.in_service)} in service · ${number(status?.capacity?.pending)} pending`} accent="violet" />
        <Metric label="Protection tier" value={`${status?.shedding_level ?? 0} · ${tierName(status?.shedding_level ?? 0)}`} detail="Checkout remains normal" accent="amber" />
      </section>

      <section className="grid-main">
        <Panel title="Actual vs predicted traffic" kicker={`Bounded ${windowLabel(windowSeconds)} window`} wide>
          <LineChart
            series={trafficSeries(data)}
            empty={<Empty title="No traffic evidence yet" detail="Run a scenario or wait for the collector to persist snapshots." />}
          />
        </Panel>
        <Panel title="Provider health" kicker="Leading signals">
          <ProviderList providers={status?.providers ?? {}} workers={status?.workers ?? []} />
        </Panel>
        <Panel title="Capacity timeline" kicker="Desired · in service · pending" wide>
          <LineChart
            series={capacitySeries(data)}
            empty={<Empty title="Capacity unavailable" detail={status?.capacity_error ?? "No capacity observations in this window."} />}
          />
        </Panel>
        <Panel title="Schedule & ramp" kicker="Next planned event">
          <Schedule status={status} events={data?.scheduledEvents ?? []} />
        </Panel>
      </section>

      <section className="two-column">
        <Panel title="Recent control decisions" kicker="Immutable action audit">
          <ActionTable actions={data?.actions ?? []} />
        </Panel>
        <Panel title="Endpoint policy" kicker="Current reversible tier">
          <PolicyList policy={status?.endpoint_policy ?? {}} />
        </Panel>
      </section>

      <section className="results-section">
        <div className="section-heading">
          <div>
            <p className="eyebrow">Measured evidence</p>
            <h2>Run results</h2>
          </div>
          <p>Values appear only after persisted evaluation. Missing evidence stays unavailable.</p>
        </div>
        <ResultCards results={data?.results ?? []} />
      </section>

      <footer>
        <span>Browser access is read-only</span>
        <span>{data ? `${data.snapshots.length} bounded snapshots loaded` : "Awaiting data"}</span>
      </footer>
    </main>
  );
}

function Panel({ title, kicker, wide = false, children }: { title: string; kicker: string; wide?: boolean; children: React.ReactNode }) {
  return (
    <article className={`panel ${wide ? "wide" : ""}`}>
      <header><div><p>{kicker}</p><h2>{title}</h2></div></header>
      {children}
    </article>
  );
}

function Metric({ label, value, detail, accent }: { label: string; value: string; detail: string; accent: string }) {
  return (
    <article className={`metric ${accent}`}>
      <p>{label}</p><strong>{value}</strong><span>{detail}</span>
    </article>
  );
}

function Notice({ tone, title, detail }: { tone: "error" | "warning"; title: string; detail: string }) {
  return <div className={`notice ${tone}`} role="status"><strong>{title}</strong><span>{detail}</span></div>;
}

function Empty({ title, detail }: { title: string; detail: string }) {
  return <div className="empty"><strong>{title}</strong><span>{detail}</span></div>;
}

function ProviderList({ providers, workers }: { providers: Record<string, { status?: string; freshness_seconds?: number; detail?: string }>; workers: Array<{ name: string; status: string; detail?: string | null }> }) {
  const items = [
    ...Object.entries(providers).map(([name, value]) => ({ name, status: value.status ?? "unknown", detail: value.freshness_seconds === undefined ? value.detail : `${value.freshness_seconds.toFixed(1)}s fresh` })),
    ...workers.map((worker) => ({ name: worker.name, status: worker.status, detail: worker.detail ?? "worker heartbeat" })),
  ];
  if (!items.length) return <Empty title="Provider state unavailable" detail="No sanitized health signal has been observed." />;
  return <div className="health-list">{items.slice(0, 8).map((item) => <div key={`${item.name}:${item.status}`}><span className={`health-icon ${item.status}`} /><p><strong>{label(item.name)}</strong><small>{item.detail ?? "No detail"}</small></p><b>{item.status}</b></div>)}</div>;
}

function Schedule({
  status,
  events,
}: {
  status: DashboardData["status"] | undefined;
  events: JsonObject[];
}) {
  const event = status?.scheduled?.event ?? events[0];
  if (!event) return <Empty title="No event in view" detail="Seed the timed Diwali scenario to see its prewarm ramp." />;
  const start = text(event, "starts_at");
  return <div className="schedule"><div className="schedule-date"><span>{start ? dateDay(start) : "—"}</span><small>{start ? dateMonth(start) : "TBD"}</small></div><div><h3>{text(event, "name") ?? "Scheduled event"}</h3><p>{start ? dateTime(start) : "Start unavailable"}</p><div className="ramp"><i /><i /><i /><i /></div><small>Peak target {text(event, "peak_desired_capacity") ?? "—"} · next point {status?.scheduled?.next_due_at ? dateTime(status.scheduled.next_due_at) : "complete or unavailable"}</small></div></div>;
}

function ActionTable({ actions }: { actions: DashboardData["actions"] }) {
  if (!actions.length) return <Empty title="No decisions in this window" detail="Dry-run, capped, skipped, and live outcomes will appear here." />;
  return <div className="table-wrap"><table><thead><tr><th>Time</th><th>Decision</th><th>Capacity</th><th>Outcome</th></tr></thead><tbody>{actions.slice(0, 7).map((action, index) => <tr key={action.id ?? index}><td>{action.requested_at ? clock(action.requested_at) : "—"}</td><td><strong>{label(action.reason_code ?? "control action")}</strong><small>{action.execution_mode ?? "unknown mode"}</small></td><td>{number(action.requested_desired_capacity)} → {number(action.applied_desired_capacity)}</td><td><span className={`pill ${action.status ?? "unknown"}`}>{action.status ?? "unknown"}</span></td></tr>)}</tbody></table></div>;
}

function PolicyList({ policy }: { policy: Record<string, string> }) {
  const entries = Object.entries(policy);
  if (!entries.length) return <Empty title="Policy unavailable" detail="The tier is known, but no endpoint policy snapshot is loaded." />;
  return <div className="policy-list">{entries.map(([endpoint, mode]) => <div key={endpoint}><code>{endpoint}</code><span className={`pill ${mode}`}>{label(mode)}</span></div>)}</div>;
}

function ResultCards({ results }: { results: DemoResult[] }) {
  if (!results.length) return <div className="results-empty"><Empty title="No completed run results" detail="Smoke and benchmark runs remain visibly empty until formula-versioned evaluation completes." /></div>;
  return <div className="result-grid">{results.slice(0, 4).map((result, index) => <article className="result-card" key={result.id ?? index}><header><div><span className="pill completed">completed</span><h3>{label(result.scenario_name ?? "demo run")}</h3></div><small>{result.started_at ? dateTime(result.started_at) : "time unavailable"}</small></header><div className="result-metrics"><ResultMetric label="Checkout p99" value={unit(result.checkout_p99_latency_ms, "ms")} /><ResultMetric label="Success" value={percent(result.checkout_success_rate)} /><ResultMetric label="Detection lead" value={unit(result.detection_lead_seconds, "s")} /><ResultMetric label="Provisioning" value={unit(result.provisioning_efficiency_pct, "%")} /></div><footer><span>{result.baseline_type ?? "baseline unavailable"}</span><span>{result.warnings?.items?.length ? `${result.warnings.items.length} warnings` : "Evidence complete"}</span></footer></article>)}</div>;
}

function ResultMetric({ label: name, value }: { label: string; value: string }) { return <div><small>{name}</small><strong>{value}</strong></div>; }

function trafficSeries(data: DashboardData | null | undefined) {
  const snapshots = [...(data?.snapshots ?? [])].reverse();
  const predictions = [...(data?.predictions ?? [])].reverse();
  return [
    { label: "Actual RPS", color: "#37d7c2", points: snapshots.map((item) => ({ at: item.observed_at ?? "", value: item.origin_request_rate_rps })) },
    { label: "Baseline", color: "#6f82a0", points: snapshots.map((item) => ({ at: item.observed_at ?? "", value: item.baseline_request_rate_rps })) },
    { label: "Predicted peak", color: "#9b87ff", points: predictions.map((item) => ({ at: item.predicted_peak_at ?? item.created_at ?? "", value: item.predicted_peak_rps })) },
  ];
}

function capacitySeries(data: DashboardData | null | undefined) {
  const snapshots = [...(data?.snapshots ?? [])].reverse();
  return [
    { label: "Desired", color: "#9b87ff", points: snapshots.map((item) => ({ at: item.observed_at ?? "", value: item.desired_capacity })) },
    { label: "In service", color: "#37d7c2", points: snapshots.map((item) => ({ at: item.observed_at ?? "", value: item.in_service_capacity })) },
    { label: "Pending", color: "#f0b35f", points: snapshots.map((item) => ({ at: item.observed_at ?? "", value: item.pending_capacity })) },
  ];
}

function metric(value: JsonObject | null, key: string, suffix: string) { const raw = value?.[key]; return typeof raw === "number" ? `${raw.toFixed(raw >= 100 ? 0 : 1)}${suffix}` : "Unavailable"; }
function successDetail(value: JsonObject | null) { const raw = value?.checkout_success_rate; return typeof raw === "number" ? `${(raw * 100).toFixed(2)}% checkout success` : "Success rate unavailable"; }
function number(value: number | null | undefined) { return typeof value === "number" ? String(value) : "—"; }
function unit(value: number | null | undefined, suffix: string) { return typeof value === "number" ? `${value.toFixed(1)} ${suffix}` : "N/A"; }
function percent(value: number | null | undefined) { return typeof value === "number" ? `${(value * 100).toFixed(2)}%` : "N/A"; }
function text(value: JsonObject, key: string) { const raw = value[key]; return typeof raw === "string" || typeof raw === "number" ? String(raw) : null; }
function label(value: string) { return value.replaceAll("_", " ").replaceAll("-", " ").replace(/\b\w/g, (letter) => letter.toUpperCase()); }
function tierName(level: number) { return ["Normal", "Recommendations off", "Cached catalog", "Emergency"][level] ?? "Unknown"; }
function clock(value: string) { return new Intl.DateTimeFormat("en", { hour: "2-digit", minute: "2-digit", second: "2-digit" }).format(new Date(value)); }
function dateTime(value: string) { return new Intl.DateTimeFormat("en", { dateStyle: "medium", timeStyle: "short" }).format(new Date(value)); }
function dateDay(value: string) { return new Intl.DateTimeFormat("en", { day: "2-digit" }).format(new Date(value)); }
function dateMonth(value: string) { return new Intl.DateTimeFormat("en", { month: "short" }).format(new Date(value)); }
function windowLabel(seconds: number) { return WINDOWS.find((item) => item.seconds === seconds)?.label ?? `${seconds}s`; }
