import type {
  DockyardErrorEnvelope,
  DockyardApprovalListProjection,
  DockyardLiveApprovalSummary,
  DockyardLiveOverviewProjection,
  DockyardLiveProviderProjection,
  DockyardLiveRunSummary,
  DockyardLiveTaskSummary,
  DockyardOperationalOverviewProjection,
  DockyardPlanApprovalProjection,
  DockyardPlanProjection,
  DockyardPlanTaskProjection,
  DockyardProjectPlanningProjection,
  DockyardReviewProjection,
  DockyardReviewListProjection,
  DockyardProviderListProjection,
  DockyardRunListProjection,
  DockyardTaskListProjection,
} from "../types/api";
import type {
  DockyardCommandDraft,
  DockyardCommandEnvelope,
  DockyardCommandReceipt,
  PlanEditorTask,
} from "../types/commands";

export interface DockyardClientCredentials {
  readonly deviceId: string;
  readonly bearerToken: string;
  readonly csrfValue: string;
}

export class DockyardClientError extends Error {
  constructor(
    readonly status: number,
    readonly envelope: DockyardErrorEnvelope | null,
  ) {
    super(status === 409 ? "Dockyard snapshot conflict" : "Dockyard command failed");
  }
}

type IdentityFactory = () => string;

function canonicalJson(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(",")}]`;
  if (value !== null && typeof value === "object") {
    const record = value as Record<string, unknown>;
    return `{${Object.keys(record).sort().map((key) => `${JSON.stringify(key)}:${canonicalJson(record[key])}`).join(",")}}`;
  }
  return JSON.stringify(value);
}

async function sha256(text: string): Promise<string> {
  const bytes = new TextEncoder().encode(text);
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  return Array.from(new Uint8Array(digest), (value) => value.toString(16).padStart(2, "0")).join("");
}

export class DockyardClient {
  constructor(
    private readonly baseUrl: string,
    private readonly credentials: DockyardClientCredentials,
    private readonly identityFactory: IdentityFactory = () => crypto.randomUUID(),
    private readonly fetcher: typeof fetch = (input, init) => globalThis.fetch(input, init),
  ) {}

  private requestUrl(path: string): string {
    if (typeof location !== "undefined" && this.baseUrl === location.origin) return path;
    return `${this.baseUrl}${path}`;
  }

  async readProjectPlanning(projectId: string): Promise<DockyardProjectPlanningProjection> {
    const project = strictText(projectId, "projectId");
    const response = await this.fetcher(
      this.requestUrl(`/api/dockyard/v1/projects/${encodeURIComponent(project)}`),
      { method: "GET", headers: { Accept: "application/json" } },
    );
    const value: unknown = await response.json();
    if (!response.ok) throw new DockyardClientError(response.status, null);
    return decodeProjectPlanningProjection(value, project);
  }

  async readPlan(projectId: string, planId: string): Promise<DockyardPlanProjection> {
    const project = strictText(projectId, "projectId");
    const plan = strictText(planId, "planId");
    const response = await this.fetcher(
      this.requestUrl(`/api/dockyard/v1/projects/${encodeURIComponent(project)}/plans/${encodeURIComponent(plan)}`),
      { method: "GET", headers: { Accept: "application/json" } },
    );
    const value: unknown = await response.json();
    if (!response.ok) throw new DockyardClientError(response.status, null);
    const projection = decodePlanProjection(value);
    if (projection.project_id !== project || projection.plan_id !== plan) {
      throw new DockyardClientError(500, null);
    }
    if (await editablePlanProjectionDigest(projection) !== projection.content_digest) {
      throw new DockyardClientError(500, null);
    }
    return projection;
  }

  async createPlan(
    project: DockyardProjectPlanningProjection,
    requirement: string,
  ): Promise<DockyardPlanProjection> {
    await this.execute({
      projectId: project.project_id,
      commandType: "plan.create",
      endpoint: `/api/dockyard/v1/projects/${encodeURIComponent(project.project_id)}/plans`,
      method: "POST",
      expectedSnapshotCommit: project.snapshot_commit,
      expectedRevision: project.project_generation,
      confirmationId: null,
      payload: { plan_id: project.plan_id, requirement },
    });
    // The PM may advance to a new immutable plan identity after the prior
    // plan was materialized.  Re-read the project pointer before projecting
    // the newly drafted plan instead of retrying the retired plan ID.
    const current = await this.readProjectPlanning(project.project_id);
    return this.readPlan(project.project_id, current.plan_id);
  }

  async submitPlan(
    projection: DockyardPlanProjection,
    requirement: string,
    tasks: readonly PlanEditorTask[],
  ): Promise<DockyardPlanProjection> {
    await this.execute({
      projectId: projection.project_id,
      commandType: "plan.update",
      endpoint: `/api/dockyard/v1/projects/${encodeURIComponent(projection.project_id)}/plans/${encodeURIComponent(projection.plan_id)}`,
      method: "PATCH",
      expectedSnapshotCommit: projection.snapshot_commit,
      expectedRevision: projection.revision,
      confirmationId: null,
      payload: {
        plan_id: projection.plan_id,
        requirement,
        tasks: tasks.map((task) => ({
          task_id: task.taskId,
          title: task.title,
          description: task.description,
          dependencies: [...task.dependencies],
          execution_mode: task.executionMode,
          role_id: task.agentId,
          provider_id: task.providerId,
          model_id: task.modelId,
          budget_tokens: task.budgetTokens,
          max_attempts: task.maxAttempts,
        })),
        submit_for_approval: true,
      },
    });
    return this.readPlan(projection.project_id, projection.plan_id);
  }

  async readPlanApproval(projectId: string): Promise<DockyardPlanApprovalProjection> {
    const overview = await this.readOperationalOverview(projectId);
    if (overview.plan_approval === null) throw new DockyardClientError(409, null);
    return overview.plan_approval;
  }

  async readOperationalOverview(projectId: string): Promise<DockyardOperationalOverviewProjection> {
    const project = strictText(projectId, "projectId");
    const response = await this.fetcher(
      this.requestUrl(`/api/dockyard/v1/projects/${encodeURIComponent(project)}/overview`),
      { method: "GET", headers: { Accept: "application/json" } },
    );
    const value: unknown = await response.json();
    if (!response.ok) throw new DockyardClientError(response.status, null);
    const result = decodeOperationalOverview(value, project);
    if (
      result.plan_approval !== null
      && await planProjectionDigest(result.plan_approval) !== result.plan_approval.content_digest
    ) throw new DockyardClientError(500, null);
    return result;
  }

  async readReview(projectId: string, taskId: string): Promise<DockyardReviewProjection> {
    const project = strictText(projectId, "projectId");
    const task = strictText(taskId, "taskId");
    const response = await this.fetcher(
      this.requestUrl(`/api/dockyard/v1/projects/${encodeURIComponent(project)}/tasks/${encodeURIComponent(task)}`),
      { method: "GET", headers: { Accept: "application/json" } },
    );
    const value: unknown = await response.json();
    if (!response.ok) throw new DockyardClientError(response.status, null);
    const record = exactRecord(value, [
      "schema_version", "project_id", "snapshot_commit", "task", "review", "content_digest",
    ]);
    if (record.schema_version !== "dockyard.task-detail/v1" || record.review === null) {
      throw new DockyardClientError(404, null);
    }
    const projection = decodeReviewProjection(record.review);
    if (projection.project_id !== project || projection.task_id !== task) {
      throw new DockyardClientError(500, null);
    }
    const values = [
      projection.project_id, projection.snapshot_commit, projection.task_id,
      String(projection.revision), String(projection.attempt), projection.dispatch_id,
      projection.implementation_commit, projection.report_commit,
      projection.audit_verdict, projection.review_phase,
    ];
    const digest = await sha256(values.map((item) => `${item.length}:${item}`).join(""));
    if (digest !== projection.content_digest) throw new DockyardClientError(500, null);
    return projection;
  }

  async readReviews(projectId: string): Promise<DockyardReviewListProjection> {
    const value = await this.readTasks(projectId);
    return {
      schema_version: "dockyard.review-list/v1",
      project_id: value.project_id,
      snapshot_commit: value.snapshot_commit,
      reviews: value.reviews,
    };
  }

  async readTasks(projectId: string): Promise<DockyardTaskListProjection> {
    const project = strictText(projectId, "projectId");
    const response = await this.fetcher(
      this.requestUrl(`/api/dockyard/v1/projects/${encodeURIComponent(project)}/tasks`),
      { method: "GET", headers: { Accept: "application/json" } },
    );
    const value: unknown = await response.json();
    if (!response.ok || value === null || typeof value !== "object" || Array.isArray(value)) {
      throw new DockyardClientError(response.status || 500, null);
    }
    const record = value as Record<string, unknown>;
    const fields = ["schema_version", "project_id", "snapshot_commit", "tasks", "reviews", "content_digest"];
    if (
      Object.keys(record).length !== fields.length
      || !fields.every((field) => field in record)
      || record.schema_version !== "dockyard.task-list/v1"
      || record.project_id !== project
      || !Array.isArray(record.tasks)
      || !Array.isArray(record.reviews)
    ) throw new DockyardClientError(500, null);
    const snapshotCommit = strictHex(record.snapshot_commit, 40);
    const tasks = record.tasks.map(decodeLiveTaskSummary);
    const reviews = record.reviews.map(decodeReviewProjection);
    for (const review of reviews) {
      if (review.project_id !== project || review.snapshot_commit !== snapshotCommit) {
        throw new DockyardClientError(500, null);
      }
      const values = [
        review.project_id, review.snapshot_commit, review.task_id,
        String(review.revision), String(review.attempt), review.dispatch_id,
        review.implementation_commit, review.report_commit,
        review.audit_verdict, review.review_phase,
      ];
      const digest = await sha256(values.map((item) => `${item.length}:${item}`).join(""));
      if (digest !== review.content_digest) throw new DockyardClientError(500, null);
    }
    return {
      schema_version: "dockyard.task-list/v1",
      project_id: project,
      snapshot_commit: snapshotCommit,
      tasks,
      reviews,
      content_digest: strictHex(record.content_digest, 64),
    };
  }

  async readRuns(projectId: string): Promise<DockyardRunListProjection> {
    const value = await this.readOperationalCollection(projectId, "runs", "dockyard.run-list/v1");
    return {
      schema_version: "dockyard.run-list/v1",
      project_id: value.projectId,
      snapshot_commit: value.snapshotCommit,
      runs: value.items.map(decodeLiveRunSummary),
      content_digest: value.contentDigest,
    };
  }

  async readApprovals(projectId: string): Promise<DockyardApprovalListProjection> {
    const value = await this.readOperationalCollection(projectId, "approvals", "dockyard.approval-list/v1");
    return {
      schema_version: "dockyard.approval-list/v1",
      project_id: value.projectId,
      snapshot_commit: value.snapshotCommit,
      approvals: value.items.map(decodeLiveApprovalSummary),
      content_digest: value.contentDigest,
    };
  }

  async readProviders(projectId: string): Promise<DockyardProviderListProjection> {
    const value = await this.readOperationalCollection(projectId, "providers", "dockyard.provider-list/v1");
    return {
      schema_version: "dockyard.provider-list/v1",
      project_id: value.projectId,
      snapshot_commit: value.snapshotCommit,
      providers: value.items.map(decodeLiveProviderProjection),
      content_digest: value.contentDigest,
    };
  }

  private async readOperationalCollection(
    projectId: string,
    key: "runs" | "approvals" | "providers",
    schema: "dockyard.run-list/v1" | "dockyard.approval-list/v1" | "dockyard.provider-list/v1",
  ): Promise<{
    readonly projectId: string;
    readonly snapshotCommit: string;
    readonly items: readonly unknown[];
    readonly contentDigest: string;
  }> {
    const project = strictText(projectId, "projectId");
    const response = await this.fetcher(
      this.requestUrl(`/api/dockyard/v1/projects/${encodeURIComponent(project)}/${key}`),
      { method: "GET", headers: { Accept: "application/json" } },
    );
    const value: unknown = await response.json();
    if (!response.ok) throw new DockyardClientError(response.status, null);
    const record = exactRecord(value, ["schema_version", "project_id", "snapshot_commit", key, "content_digest"]);
    if (record.schema_version !== schema || record.project_id !== project || !Array.isArray(record[key])) {
      throw new DockyardClientError(500, null);
    }
    return {
      projectId: project,
      snapshotCommit: strictHex(record.snapshot_commit, 40),
      items: record[key],
      contentDigest: strictHex(record.content_digest, 64),
    };
  }

  eventsUrl(projectId: string): string {
    return this.requestUrl(`/api/dockyard/v1/projects/${encodeURIComponent(strictText(projectId, "projectId"))}/events`);
  }

  async execute(draft: DockyardCommandDraft): Promise<DockyardCommandReceipt> {
    const commandId = `CMD-${this.identityFactory()}`;
    const idempotencyKey = `IDEMP-${this.identityFactory()}`;
    const payloadJson = canonicalJson(draft.payload);
    const envelope: DockyardCommandEnvelope = {
      schema_version: "dockyard.command/v1",
      command_id: commandId,
      project_id: draft.projectId,
      command_type: draft.commandType,
      idempotency_key: idempotencyKey,
      expected_snapshot_commit: draft.expectedSnapshotCommit,
      expected_revision: draft.expectedRevision,
      confirmation_id: draft.confirmationId,
      payload_digest: await sha256(payloadJson),
    };
    const response = await this.fetcher(this.requestUrl(draft.endpoint), {
      method: draft.method,
      headers: {
        "Content-Type": "application/json",
        "X-Dockyard-Device-Id": this.credentials.deviceId,
        "X-Dockyard-CSRF": this.credentials.csrfValue,
        "X-Dockyard-Idempotency-Key": idempotencyKey,
        Authorization: `Bearer ${this.credentials.bearerToken}`,
      },
      body: `{"envelope":${canonicalJson(envelope)},"payload":${payloadJson}}`,
    });
    const value = await response.json() as DockyardCommandReceipt | DockyardErrorEnvelope;
    if (!response.ok) {
      throw new DockyardClientError(
        response.status,
        value.schema_version === "dockyard.error/v1" ? value : null,
      );
    }
    if (value.schema_version !== "dockyard.command-receipt/v1") {
      throw new DockyardClientError(500, null);
    }
    return value;
  }
}

function strictText(value: unknown, field: string): string {
  if (typeof value !== "string" || value.length === 0 || value !== value.trim() || /[\u0000-\u001f]/.test(value)) {
    throw new DockyardClientError(500, null);
  }
  return value;
}

function strictInteger(value: unknown): number {
  if (typeof value !== "number" || !Number.isInteger(value) || value < 1) throw new DockyardClientError(500, null);
  return value;
}

function strictNonNegativeInteger(value: unknown): number {
  if (typeof value !== "number" || !Number.isInteger(value) || value < 0) {
    throw new DockyardClientError(500, null);
  }
  return value;
}

function nullableText(value: unknown): string | null {
  return value === null ? null : strictText(value, "optional");
}

function exactRecord(value: unknown, fields: readonly string[]): Record<string, unknown> {
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    throw new DockyardClientError(500, null);
  }
  const record = value as Record<string, unknown>;
  if (Object.keys(record).length !== fields.length || !fields.every((field) => field in record)) {
    throw new DockyardClientError(500, null);
  }
  return record;
}

function strictHex(value: unknown, length: number): string {
  const text = strictText(value, "digest");
  if (text.length !== length || !/^[0-9a-f]+$/.test(text)) throw new DockyardClientError(500, null);
  return text;
}

function strictStringArray(value: unknown): readonly string[] {
  if (!Array.isArray(value)) throw new DockyardClientError(500, null);
  return value.map((item) => strictText(item, "list_item"));
}

function decodeLiveProviderProjection(value: unknown): DockyardLiveProviderProjection {
  const record = exactRecord(value, [
    "provider_id", "model_id", "health", "reason_code", "executable_version",
    "decoder_version", "binding_id", "required_capabilities", "checked_at", "content_digest",
  ]);
  if (!["READY", "DEGRADED", "NOT_READY", "UNKNOWN"].includes(String(record.health))) {
    throw new DockyardClientError(500, null);
  }
  return {
    provider_id: strictText(record.provider_id, "provider_id"),
    model_id: strictText(record.model_id, "model_id"),
    health: record.health as DockyardLiveProviderProjection["health"],
    reason_code: nullableText(record.reason_code),
    executable_version: nullableText(record.executable_version),
    decoder_version: nullableText(record.decoder_version),
    binding_id: nullableText(record.binding_id),
    required_capabilities: strictStringArray(record.required_capabilities),
    checked_at: strictText(record.checked_at, "checked_at"),
    content_digest: strictHex(record.content_digest, 64),
  };
}

function decodeLiveOverviewProjection(value: unknown): DockyardLiveOverviewProjection {
  const record = exactRecord(value, [
    "schema_version", "project_id", "adoption_level", "snapshot_commit", "updated_at",
    "generated_at", "pm_mode", "state_counts", "active_task_ids",
    "pending_approval_task_ids", "recent_delivery_task_ids", "provider_summaries",
    "health", "content_digest",
  ]);
  if (record.schema_version !== "agentdesk.dockyard-projection/v1" || !Array.isArray(record.state_counts) || !Array.isArray(record.provider_summaries)) {
    throw new DockyardClientError(500, null);
  }
  const health = exactRecord(record.health, [
    "snapshot_state", "pm_state", "scheduler_state", "runner_state", "provider_state",
    "issue_codes", "checked_at", "content_digest",
  ]);
  if (!["READY", "DEGRADED", "NOT_READY", "UNKNOWN"].includes(String(health.provider_state))) {
    throw new DockyardClientError(500, null);
  }
  return {
    schema_version: "agentdesk.dockyard-projection/v1",
    project_id: strictText(record.project_id, "project_id"),
    adoption_level: strictText(record.adoption_level, "adoption_level"),
    snapshot_commit: strictHex(record.snapshot_commit, 64),
    updated_at: strictText(record.updated_at, "updated_at"),
    generated_at: strictText(record.generated_at, "generated_at"),
    pm_mode: strictText(record.pm_mode, "pm_mode"),
    state_counts: record.state_counts.map((item) => {
      const count = exactRecord(item, ["state", "count"]);
      return { state: strictText(count.state, "state"), count: strictNonNegativeInteger(count.count) };
    }),
    active_task_ids: strictStringArray(record.active_task_ids),
    pending_approval_task_ids: strictStringArray(record.pending_approval_task_ids),
    recent_delivery_task_ids: strictStringArray(record.recent_delivery_task_ids),
    provider_summaries: record.provider_summaries.map(decodeLiveProviderProjection),
    health: {
      snapshot_state: strictText(health.snapshot_state, "snapshot_state"),
      pm_state: strictText(health.pm_state, "pm_state"),
      scheduler_state: strictText(health.scheduler_state, "scheduler_state"),
      runner_state: strictText(health.runner_state, "runner_state"),
      provider_state: health.provider_state as DockyardLiveProviderProjection["health"],
      issue_codes: strictStringArray(health.issue_codes),
      checked_at: strictText(health.checked_at, "checked_at"),
      content_digest: strictHex(health.content_digest, 64),
    },
    content_digest: strictHex(record.content_digest, 64),
  };
}

function decodeOperationalOverview(value: unknown, expectedProject: string): DockyardOperationalOverviewProjection {
  const record = exactRecord(value, [
    "schema_version", "project_id", "snapshot_commit", "overview", "plan_approval", "content_digest",
  ]);
  if (record.schema_version !== "dockyard.operational-overview/v1" || record.project_id !== expectedProject) {
    throw new DockyardClientError(500, null);
  }
  const overview = decodeLiveOverviewProjection(record.overview);
  const snapshotCommit = strictHex(record.snapshot_commit, 40);
  if (overview.project_id !== expectedProject) {
    throw new DockyardClientError(500, null);
  }
  return {
    schema_version: "dockyard.operational-overview/v1",
    project_id: expectedProject,
    snapshot_commit: snapshotCommit,
    overview,
    plan_approval: record.plan_approval === null ? null : decodePlanApprovalProjection(record.plan_approval),
    content_digest: strictHex(record.content_digest, 64),
  };
}

function decodeLiveTaskSummary(value: unknown): DockyardLiveTaskSummary {
  const record = exactRecord(value, [
    "task_id", "revision", "state", "attempt", "role_id", "provider_id", "model_id",
    "deliberation_tier", "delivery_state", "integration_state", "updated_at",
    "blocked_kind", "has_report", "content_digest",
  ]);
  if (typeof record.has_report !== "boolean") throw new DockyardClientError(500, null);
  return {
    task_id: strictText(record.task_id, "task_id"),
    revision: strictInteger(record.revision),
    state: strictText(record.state, "state"),
    attempt: record.attempt === null ? null : strictInteger(record.attempt),
    role_id: nullableText(record.role_id),
    provider_id: nullableText(record.provider_id),
    model_id: nullableText(record.model_id),
    deliberation_tier: nullableText(record.deliberation_tier),
    delivery_state: nullableText(record.delivery_state),
    integration_state: nullableText(record.integration_state),
    updated_at: strictText(record.updated_at, "updated_at"),
    blocked_kind: nullableText(record.blocked_kind),
    has_report: record.has_report,
    content_digest: strictHex(record.content_digest, 64),
  };
}

function decodeLiveRunSummary(value: unknown): DockyardLiveRunSummary {
  const record = exactRecord(value, [
    "task_id", "revision", "attempt", "dispatch_id", "phase", "supervisor_state",
    "worker_state", "heartbeat_state", "lease_state", "started_at", "updated_at",
    "retry_count", "generation_id", "termination_available", "termination_event_id",
    "retry_available", "retry_event_id", "retry_failure_kind", "retry_provider_id", "retry_model_id",
    "content_digest",
  ]);
  if (typeof record.termination_available !== "boolean") throw new DockyardClientError(500, null);
  if (typeof record.retry_available !== "boolean") throw new DockyardClientError(500, null);
  return {
    task_id: strictText(record.task_id, "task_id"),
    revision: strictInteger(record.revision),
    attempt: strictInteger(record.attempt),
    dispatch_id: strictText(record.dispatch_id, "dispatch_id"),
    phase: strictText(record.phase, "phase"),
    supervisor_state: strictText(record.supervisor_state, "supervisor_state"),
    worker_state: strictText(record.worker_state, "worker_state"),
    heartbeat_state: strictText(record.heartbeat_state, "heartbeat_state"),
    lease_state: strictText(record.lease_state, "lease_state"),
    started_at: nullableText(record.started_at),
    updated_at: strictText(record.updated_at, "updated_at"),
    retry_count: strictNonNegativeInteger(record.retry_count),
    generation_id: nullableText(record.generation_id),
    termination_available: record.termination_available,
    termination_event_id: nullableText(record.termination_event_id),
    retry_available: record.retry_available,
    retry_event_id: nullableText(record.retry_event_id),
    retry_failure_kind: nullableText(record.retry_failure_kind),
    retry_provider_id: nullableText(record.retry_provider_id),
    retry_model_id: nullableText(record.retry_model_id),
    content_digest: strictHex(record.content_digest, 64),
  };
}

function decodeLiveApprovalSummary(value: unknown): DockyardLiveApprovalSummary {
  const record = exactRecord(value, [
    "task_id", "revision", "granted_approval_count", "latest_acceptance_decision",
    "owner_approval_gate", "pending_user_decision", "updated_at", "content_digest",
  ]);
  if (typeof record.pending_user_decision !== "boolean") throw new DockyardClientError(500, null);
  return {
    task_id: strictText(record.task_id, "task_id"),
    revision: strictInteger(record.revision),
    granted_approval_count: strictNonNegativeInteger(record.granted_approval_count),
    latest_acceptance_decision: nullableText(record.latest_acceptance_decision),
    owner_approval_gate: nullableText(record.owner_approval_gate),
    pending_user_decision: record.pending_user_decision,
    updated_at: strictText(record.updated_at, "updated_at"),
    content_digest: strictHex(record.content_digest, 64),
  };
}

export function decodePlanApprovalProjection(value: unknown): DockyardPlanApprovalProjection {
  if (value === null || typeof value !== "object" || Array.isArray(value)) throw new DockyardClientError(500, null);
  const record = value as Record<string, unknown>;
  const fields = [
    "schema_version", "project_id", "snapshot_commit", "project_generation", "plan_id",
    "revision", "plan_digest", "task_count", "phase", "content_digest",
  ];
  if (Object.keys(record).length !== fields.length || !fields.every((field) => field in record)) {
    throw new DockyardClientError(500, null);
  }
  if (record.schema_version !== "dockyard.plan-approval-projection/v1" || record.phase !== "APPROVAL_PENDING") {
    throw new DockyardClientError(500, null);
  }
  return {
    schema_version: "dockyard.plan-approval-projection/v1",
    project_id: strictText(record.project_id, "project_id"),
    snapshot_commit: strictHex(record.snapshot_commit, 40),
    project_generation: strictInteger(record.project_generation),
    plan_id: strictText(record.plan_id, "plan_id"),
    revision: strictInteger(record.revision),
    plan_digest: strictHex(record.plan_digest, 64),
    task_count: strictInteger(record.task_count),
    phase: "APPROVAL_PENDING",
    content_digest: strictHex(record.content_digest, 64),
  };
}

export function decodeReviewProjection(value: unknown): DockyardReviewProjection {
  if (value === null || typeof value !== "object" || Array.isArray(value)) throw new DockyardClientError(500, null);
  const record = value as Record<string, unknown>;
  const fields = [
    "schema_version", "project_id", "snapshot_commit", "task_id", "revision",
    "attempt", "dispatch_id", "implementation_commit", "report_commit",
    "audit_verdict", "review_phase", "content_digest",
  ];
  if (Object.keys(record).length !== fields.length || !fields.every((field) => field in record)) {
    throw new DockyardClientError(500, null);
  }
  if (
    record.schema_version !== "dockyard.review-projection/v1"
    || !["pass", "fail", "blocked"].includes(String(record.audit_verdict))
    || record.review_phase !== "MAD_COMPLETED"
  ) throw new DockyardClientError(500, null);
  return {
    schema_version: "dockyard.review-projection/v1",
    project_id: strictText(record.project_id, "project_id"),
    snapshot_commit: strictHex(record.snapshot_commit, 40),
    task_id: strictText(record.task_id, "task_id"),
    revision: strictInteger(record.revision),
    attempt: strictInteger(record.attempt),
    dispatch_id: strictText(record.dispatch_id, "dispatch_id"),
    implementation_commit: strictHex(record.implementation_commit, 40),
    report_commit: strictHex(record.report_commit, 40),
    audit_verdict: record.audit_verdict as DockyardReviewProjection["audit_verdict"],
    review_phase: "MAD_COMPLETED",
    content_digest: strictHex(record.content_digest, 64),
  };
}

function strictDocumentText(value: unknown, field: string): string {
  if (
    typeof value !== "string"
    || value.length === 0
    || value !== value.trim()
    || /[\u0000-\u0008\u000b\u000c\u000e-\u001f]/.test(value)
  ) {
    throw new DockyardClientError(500, null);
  }
  return value;
}

export function decodeProjectPlanningProjection(
  value: unknown,
  expectedProject: string,
): DockyardProjectPlanningProjection {
  if (value === null || typeof value !== "object" || Array.isArray(value)) throw new DockyardClientError(500, null);
  const record = value as Record<string, unknown>;
  const fields = ["schema_version", "project_id", "snapshot_commit", "project_generation", "plan_id"];
  if (Object.keys(record).length !== fields.length || !fields.every((field) => field in record)) {
    throw new DockyardClientError(500, null);
  }
  if (record.schema_version !== "dockyard.project-planning/v1") throw new DockyardClientError(500, null);
  const result: DockyardProjectPlanningProjection = {
    schema_version: "dockyard.project-planning/v1",
    project_id: strictText(record.project_id, "project_id"),
    snapshot_commit: strictHex(record.snapshot_commit, 40),
    project_generation: strictInteger(record.project_generation),
    plan_id: strictText(record.plan_id, "plan_id"),
  };
  if (result.project_id !== expectedProject) throw new DockyardClientError(500, null);
  return result;
}

function decodePlanTask(value: unknown): DockyardPlanTaskProjection {
  if (value === null || typeof value !== "object" || Array.isArray(value)) throw new DockyardClientError(500, null);
  const record = value as Record<string, unknown>;
  const fields = [
    "task_id", "title", "description", "dependencies", "execution_mode",
    "role_id", "provider_id", "model_id", "budget_tokens", "max_attempts",
  ];
  if (Object.keys(record).length !== fields.length || !fields.every((field) => field in record)) {
    throw new DockyardClientError(500, null);
  }
  if (!Array.isArray(record.dependencies) || !record.dependencies.every((item) => typeof item === "string")) {
    throw new DockyardClientError(500, null);
  }
  if (!["parallel", "serial"].includes(String(record.execution_mode))) throw new DockyardClientError(500, null);
  if (!["claude", "codex", "reasonix"].includes(String(record.provider_id))) throw new DockyardClientError(500, null);
  const attempts = strictInteger(record.max_attempts);
  if (attempts > 3) throw new DockyardClientError(500, null);
  return {
    task_id: strictText(record.task_id, "task_id"),
    title: strictText(record.title, "title"),
    description: strictDocumentText(record.description, "description"),
    dependencies: record.dependencies.map((item) => strictText(item, "dependency")),
    execution_mode: record.execution_mode as "parallel" | "serial",
    role_id: strictText(record.role_id, "role_id"),
    provider_id: record.provider_id as "claude" | "codex" | "reasonix",
    model_id: strictText(record.model_id, "model_id"),
    budget_tokens: strictInteger(record.budget_tokens),
    max_attempts: attempts as 1 | 2 | 3,
  };
}

export function decodePlanProjection(value: unknown): DockyardPlanProjection {
  if (value === null || typeof value !== "object" || Array.isArray(value)) throw new DockyardClientError(500, null);
  const record = value as Record<string, unknown>;
  const fields = [
    "schema_version", "project_id", "snapshot_commit", "project_generation", "plan_id",
    "revision", "phase", "requirement", "tasks", "plan_digest", "content_digest",
  ];
  if (Object.keys(record).length !== fields.length || !fields.every((field) => field in record)) {
    throw new DockyardClientError(500, null);
  }
  if (record.schema_version !== "dockyard.plan-projection/v1") throw new DockyardClientError(500, null);
  if (!["DRAFTED", "EDITED", "APPROVAL_PENDING"].includes(String(record.phase))) {
    throw new DockyardClientError(500, null);
  }
  if (!Array.isArray(record.tasks) || record.tasks.length === 0) throw new DockyardClientError(500, null);
  return {
    schema_version: "dockyard.plan-projection/v1",
    project_id: strictText(record.project_id, "project_id"),
    snapshot_commit: strictHex(record.snapshot_commit, 40),
    project_generation: strictInteger(record.project_generation),
    plan_id: strictText(record.plan_id, "plan_id"),
    revision: strictInteger(record.revision),
    phase: record.phase as DockyardPlanProjection["phase"],
    requirement: strictDocumentText(record.requirement, "requirement"),
    tasks: record.tasks.map(decodePlanTask),
    plan_digest: strictHex(record.plan_digest, 64),
    content_digest: strictHex(record.content_digest, 64),
  };
}

async function planProjectionDigest(projection: DockyardPlanApprovalProjection): Promise<string> {
  const values = [
    projection.project_id,
    projection.snapshot_commit,
    String(projection.project_generation),
    projection.plan_id,
    String(projection.revision),
    projection.plan_digest,
    String(projection.task_count),
    projection.phase,
  ];
  return sha256(values.map((value) => `${value.length}:${value}`).join(""));
}

async function editablePlanProjectionDigest(projection: DockyardPlanProjection): Promise<string> {
  const values = [
    projection.project_id,
    projection.snapshot_commit,
    String(projection.project_generation),
    projection.plan_id,
    String(projection.revision),
    projection.phase,
    projection.requirement,
  ];
  for (const task of projection.tasks) {
    values.push(
      task.task_id,
      task.title,
      task.description,
      ...task.dependencies,
      task.execution_mode,
      task.role_id,
      task.provider_id,
      task.model_id,
      String(task.budget_tokens),
      String(task.max_attempts),
    );
  }
  values.push(projection.plan_digest);
  return sha256(values.map((item) => `${item.length}:${item}`).join(""));
}

export { canonicalJson, sha256 };
