export const DOCKYARD_API_NAMESPACE = "/api/dockyard/v1" as const;

export type ProviderHealth = "READY" | "DEGRADED" | "NOT_READY" | "UNKNOWN";

export interface DockyardErrorEnvelope {
  readonly schema_version: "dockyard.error/v1";
  readonly request_id: string;
  readonly error_code: string;
  readonly category: string;
  readonly retryable: boolean;
  readonly safe_message: string;
  readonly correlation_digest: string;
}

export interface DockyardPlanApprovalProjection {
  readonly schema_version: "dockyard.plan-approval-projection/v1";
  readonly project_id: string;
  readonly snapshot_commit: string;
  readonly project_generation: number;
  readonly plan_id: string;
  readonly revision: number;
  readonly plan_digest: string;
  readonly task_count: number;
  readonly phase: "APPROVAL_PENDING";
  readonly content_digest: string;
}

export interface DockyardProjectPlanningProjection {
  readonly schema_version: "dockyard.project-planning/v1";
  readonly project_id: string;
  readonly snapshot_commit: string;
  readonly project_generation: number;
  readonly plan_id: string;
}

export interface DockyardPlanTaskProjection {
  readonly task_id: string;
  readonly title: string;
  readonly description: string;
  readonly dependencies: readonly string[];
  readonly execution_mode: "parallel" | "serial";
  readonly role_id: string;
  readonly provider_id: "claude" | "codex" | "reasonix";
  readonly model_id: string;
  readonly budget_tokens: number;
  readonly max_attempts: 1 | 2 | 3;
}

export interface DockyardPlanProjection {
  readonly schema_version: "dockyard.plan-projection/v1";
  readonly project_id: string;
  readonly snapshot_commit: string;
  readonly project_generation: number;
  readonly plan_id: string;
  readonly revision: number;
  readonly phase: "DRAFTED" | "EDITED" | "APPROVAL_PENDING";
  readonly requirement: string;
  readonly tasks: readonly DockyardPlanTaskProjection[];
  readonly plan_digest: string;
  readonly content_digest: string;
}

export interface DockyardReviewProjection {
  readonly schema_version: "dockyard.review-projection/v1";
  readonly project_id: string;
  readonly snapshot_commit: string;
  readonly task_id: string;
  readonly revision: number;
  readonly attempt: number;
  readonly dispatch_id: string;
  readonly implementation_commit: string;
  readonly report_commit: string;
  readonly audit_verdict: "pass" | "fail" | "blocked";
  readonly review_phase: "MAD_COMPLETED";
  readonly content_digest: string;
}

export interface DockyardReviewListProjection {
  readonly schema_version: "dockyard.review-list/v1";
  readonly project_id: string;
  readonly snapshot_commit: string;
  readonly reviews: readonly DockyardReviewProjection[];
}

export interface DockyardStateCountProjection {
  readonly state: string;
  readonly count: number;
}

export interface DockyardProjectHealthProjection {
  readonly snapshot_state: string;
  readonly pm_state: string;
  readonly scheduler_state: string;
  readonly runner_state: string;
  readonly provider_state: ProviderHealth;
  readonly issue_codes: readonly string[];
  readonly checked_at: string;
  readonly content_digest: string;
}

export interface DockyardLiveProviderProjection {
  readonly provider_id: string;
  readonly model_id: string;
  readonly health: ProviderHealth;
  readonly reason_code: string | null;
  readonly executable_version: string | null;
  readonly decoder_version: string | null;
  readonly binding_id: string | null;
  readonly required_capabilities: readonly string[];
  readonly checked_at: string;
  readonly content_digest: string;
}

export interface DockyardLiveOverviewProjection {
  readonly schema_version: "agentdesk.dockyard-projection/v1";
  readonly project_id: string;
  readonly adoption_level: string;
  readonly snapshot_commit: string;
  readonly updated_at: string;
  readonly generated_at: string;
  readonly pm_mode: string;
  readonly state_counts: readonly DockyardStateCountProjection[];
  readonly active_task_ids: readonly string[];
  readonly pending_approval_task_ids: readonly string[];
  readonly recent_delivery_task_ids: readonly string[];
  readonly provider_summaries: readonly DockyardLiveProviderProjection[];
  readonly health: DockyardProjectHealthProjection;
  readonly content_digest: string;
}

export interface DockyardOperationalOverviewProjection {
  readonly schema_version: "dockyard.operational-overview/v1";
  readonly project_id: string;
  readonly snapshot_commit: string;
  readonly overview: DockyardLiveOverviewProjection;
  readonly plan_approval: DockyardPlanApprovalProjection | null;
  readonly content_digest: string;
}

export interface DockyardLiveTaskSummary {
  readonly task_id: string;
  readonly revision: number;
  readonly state: string;
  readonly attempt: number | null;
  readonly role_id: string | null;
  readonly provider_id: string | null;
  readonly model_id: string | null;
  readonly deliberation_tier: string | null;
  readonly delivery_state: string | null;
  readonly integration_state: string | null;
  readonly updated_at: string;
  readonly blocked_kind: string | null;
  readonly has_report: boolean;
  readonly content_digest: string;
}

export interface DockyardTaskListProjection {
  readonly schema_version: "dockyard.task-list/v1";
  readonly project_id: string;
  readonly snapshot_commit: string;
  readonly tasks: readonly DockyardLiveTaskSummary[];
  readonly reviews: readonly DockyardReviewProjection[];
  readonly content_digest: string;
}

export interface DockyardLiveRunSummary {
  readonly task_id: string;
  readonly revision: number;
  readonly attempt: number;
  readonly dispatch_id: string;
  readonly phase: string;
  readonly supervisor_state: string;
  readonly worker_state: string;
  readonly heartbeat_state: string;
  readonly lease_state: string;
  readonly started_at: string | null;
  readonly updated_at: string;
  readonly retry_count: number;
  readonly generation_id: string | null;
  readonly termination_available: boolean;
  readonly termination_event_id: string | null;
  readonly retry_available: boolean;
  readonly retry_event_id: string | null;
  readonly retry_failure_kind: string | null;
  readonly retry_provider_id: string | null;
  readonly retry_model_id: string | null;
  readonly content_digest: string;
}

export interface DockyardLiveApprovalSummary {
  readonly task_id: string;
  readonly revision: number;
  readonly granted_approval_count: number;
  readonly latest_acceptance_decision: string | null;
  readonly owner_approval_gate: string | null;
  readonly pending_user_decision: boolean;
  readonly updated_at: string;
  readonly content_digest: string;
}

export interface DockyardRunListProjection {
  readonly schema_version: "dockyard.run-list/v1";
  readonly project_id: string;
  readonly snapshot_commit: string;
  readonly runs: readonly DockyardLiveRunSummary[];
  readonly content_digest: string;
}

export interface DockyardApprovalListProjection {
  readonly schema_version: "dockyard.approval-list/v1";
  readonly project_id: string;
  readonly snapshot_commit: string;
  readonly approvals: readonly DockyardLiveApprovalSummary[];
  readonly content_digest: string;
}

export interface DockyardProviderListProjection {
  readonly schema_version: "dockyard.provider-list/v1";
  readonly project_id: string;
  readonly snapshot_commit: string;
  readonly providers: readonly DockyardLiveProviderProjection[];
  readonly content_digest: string;
}

export type DockyardSseEventType =
  | "snapshot.changed"
  | "task.changed"
  | "run.changed"
  | "approval.changed"
  | "review.changed"
  | "provider.changed"
  | "project.changed"
  | "health.changed";

export interface DockyardSseEvent {
  readonly schema_version: "dockyard.sse-event/v1";
  readonly event_cursor: string;
  readonly project_id: string;
  readonly event_type: DockyardSseEventType;
  readonly snapshot_commit: string;
  readonly resource_id: string;
  readonly occurred_at: string;
  readonly content_digest: string;
}

export interface DockyardTaskSummary {
  readonly task_id: string;
  readonly title: string;
  readonly state: string;
  readonly revision: number;
  readonly attempt: number;
  readonly role_name: string;
  readonly provider: string | null;
  readonly model: string | null;
  readonly updated_at: string;
}

export interface DockyardProviderSummary {
  readonly provider_id: "claude" | "codex" | "reasonix";
  readonly label: string;
  readonly health: ProviderHealth;
  readonly model_id: string | null;
  readonly evidence_label: string;
}

export interface DockyardOverviewFixture {
  readonly schema_version: "dockyard.overview/v1";
  readonly project_id: string;
  readonly project_name: string;
  readonly snapshot_commit: string;
  readonly updated_at: string;
  readonly task_counts: Readonly<{
    ready: number;
    active: number;
    review: number;
    blocked: number;
  }>;
  readonly pending_approvals: number;
  readonly tasks: readonly DockyardTaskSummary[];
  readonly providers: readonly DockyardProviderSummary[];
}
