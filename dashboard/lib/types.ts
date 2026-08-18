export type JsonObject = Record<string, unknown>;

export interface ApiPage<T extends JsonObject> {
  items: T[];
  next_cursor: string | null;
  from: string;
  to: string;
}

export interface ProviderHealth {
  status?: string;
  freshness_seconds?: number;
  detail?: string;
}

export interface Capacity {
  desired?: number;
  in_service?: number;
  pending?: number;
  provider_status?: string;
}

export interface OperationalStatus extends JsonObject {
  environment: string;
  execution_mode: string;
  global_ceiling: number;
  state: string;
  database_ready: boolean;
  demo_app_ready: boolean;
  capacity: Capacity | null;
  capacity_error: string | null;
  shedding_level: number;
  endpoint_policy: Record<string, string>;
  cooldown_until: string | null;
  providers: Record<string, ProviderHealth>;
  workers: Array<{ name: string; status: string; detail?: string | null }>;
  scheduled: { next_due_at?: string | null; event?: JsonObject | null };
  latest_snapshot?: Snapshot | null;
}

export interface Snapshot extends JsonObject {
  id?: number;
  observed_at?: string;
  origin_request_rate_rps?: number;
  baseline_request_rate_rps?: number;
  checkout_p99_latency_ms?: number | null;
  checkout_success_rate?: number | null;
  desired_capacity?: number | null;
  in_service_capacity?: number | null;
  pending_capacity?: number | null;
  load_shedding_level?: number;
}

export interface Prediction extends JsonObject {
  id?: string;
  created_at?: string;
  predicted_peak_at?: string;
  predicted_peak_rps?: number;
  confidence?: number;
  status?: string;
  mode?: string;
  reasoning?: string;
}

export interface ScalingAction extends JsonObject {
  id?: string;
  requested_at?: string;
  requested_desired_capacity?: number;
  applied_desired_capacity?: number | null;
  status?: string;
  reason_code?: string;
  reasoning?: string;
  execution_mode?: string;
}

export interface ScheduledEvent extends JsonObject {
  id?: string;
  name?: string;
  starts_at?: string;
  ends_at?: string;
  status?: string;
  ramp_profile?: JsonObject;
  peak_desired_capacity?: number;
}

export interface DemoResult extends JsonObject {
  id?: string;
  scenario_name?: string;
  baseline_type?: string;
  started_at?: string;
  checkout_p99_latency_ms?: number | null;
  checkout_success_rate?: number | null;
  detection_lead_seconds?: number | null;
  provisioning_efficiency_pct?: number | null;
  warnings?: { items?: string[] };
}

export interface DashboardData {
  status: OperationalStatus | null;
  snapshots: Snapshot[];
  predictions: Prediction[];
  actions: ScalingAction[];
  sheddingEvents: JsonObject[];
  scheduledEvents: ScheduledEvent[];
  results: DemoResult[];
  warnings: string[];
  fetchedAt: string;
  window: { from: string; to: string; seconds: number };
}
