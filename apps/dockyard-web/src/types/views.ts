export type ReadViewState =
  | "fresh"
  | "loading"
  | "empty"
  | "stale"
  | "reconnecting"
  | "offline"
  | "not_ready"
  | "unknown"
  | "forbidden"
  | "failed";

export interface RunView {
  readonly run_id: string;
  readonly task_id: string;
  readonly title: string;
  readonly phase: "SCHEDULED" | "ACKNOWLEDGED" | "RUNNING" | "FINALIZING" | "FINALIZED" | "RECOVERY_REQUIRED";
  readonly provider: string;
  readonly model: string;
  readonly attempt: number;
  readonly heartbeat: "HEALTHY" | "STALE" | "UNKNOWN";
  readonly updated_at: string;
}

export interface ReviewView {
  readonly review_id: string;
  readonly task_id: string;
  readonly verdict: "PASS" | "RETURN" | "BLOCKED" | "PENDING";
  readonly depth: "fast" | "balanced" | "deep";
  readonly implementation_commit: string | null;
  readonly report_commit: string | null;
  readonly summary: string;
}

export interface DeliveryView {
  readonly delivery_id: string;
  readonly task_id: string;
  readonly title: string;
  readonly outcome: "INTEGRATED" | "REVIEW_READY" | "RETURNED";
  readonly implementation_commit: string;
  readonly report_commit: string;
  readonly occurred_at: string;
}

export interface DiagnosticView {
  readonly check_id: string;
  readonly label: string;
  readonly outcome: "PASS" | "WARN" | "FAIL" | "UNKNOWN";
  readonly safe_message: string;
  readonly correlation_digest: string;
}
