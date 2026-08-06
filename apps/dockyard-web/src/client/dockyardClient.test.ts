import { describe, expect, it, vi } from "vitest";
import {
  DockyardClient,
  DockyardClientError,
  canonicalJson,
  decodePlanApprovalProjection,
  decodePlanProjection,
  decodeProjectPlanningProjection,
  decodeReviewProjection,
  sha256,
} from "./dockyardClient";
import type { DockyardCommandDraft } from "../types/commands";

const draft: DockyardCommandDraft = {
  projectId: "PRJ-1",
  commandType: "task.retry",
  endpoint: "/api/dockyard/v1/projects/PRJ-1/tasks/TC-1/retry",
  method: "POST",
  expectedSnapshotCommit: "a".repeat(40),
  expectedRevision: 1,
  confirmationId: "CONF-1",
  payload: { next_attempt: 2, task_id: "TC-1" },
};

describe("Dockyard typed command client", () => {
  it("canonicalizes payload keys", () => {
    expect(canonicalJson({ z: 1, a: 2 })).toBe('{"a":2,"z":1}');
  });

  it("sends the frozen command envelope and local device headers", async () => {
    const fetcher = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
      const request = JSON.parse(String(init?.body));
      expect(request.envelope.schema_version).toBe("dockyard.command/v1");
      expect(request.envelope.payload_digest).toMatch(/^[0-9a-f]{64}$/);
      expect(init?.headers).toMatchObject({
        "X-Dockyard-Device-Id": "DEV-1",
        "X-Dockyard-CSRF": "CSRF-1",
      });
      return new Response(JSON.stringify({
        schema_version: "dockyard.command-receipt/v1",
        command_id: request.envelope.command_id,
        project_id: "PRJ-1",
        command_type: "task.retry",
        outcome: "committed",
        canonical_event_id: "EVT-1",
        snapshot_commit: "b".repeat(40),
        replayed: false,
        completed_at: "2026-08-02T00:00:00Z",
        content_digest: "c".repeat(64),
      }), { status: 200, headers: { "Content-Type": "application/json" } });
    });
    const identities = ["1", "2"];
    const client = new DockyardClient(
      "http://127.0.0.1:7001",
      { deviceId: "DEV-1", bearerToken: "local-device-token", csrfValue: "CSRF-1" },
      () => identities.shift()!,
      fetcher as typeof fetch,
    );
    const receipt = await client.execute(draft);
    expect(receipt.outcome).toBe("committed");
    expect(fetcher).toHaveBeenCalledOnce();
  });

  it("preserves stale CAS as a typed conflict", async () => {
    const fetcher = vi.fn(async () => new Response(JSON.stringify({
      schema_version: "dockyard.error/v1",
      request_id: "REQ-1",
      error_code: "SNAPSHOT_STALE",
      category: "conflict",
      retryable: false,
      safe_message: "状态已变化",
      correlation_digest: "d".repeat(64),
    }), { status: 409 }));
    const client = new DockyardClient(
      "http://127.0.0.1:7001",
      { deviceId: "DEV-1", bearerToken: "token", csrfValue: "CSRF-1" },
      () => "1",
      fetcher as typeof fetch,
    );
    await expect(client.execute(draft)).rejects.toBeInstanceOf(DockyardClientError);
  });

  it("contains no provider API credential surface", () => {
    const source = String(DockyardClient);
    expect(source).not.toMatch(/DEEPSEEK_API_KEY|OPENAI_API_KEY|ANTHROPIC_API_KEY/);
  });

  it("strictly decodes the exact pending-plan projection", () => {
    const projection = {
      schema_version: "dockyard.plan-approval-projection/v1",
      project_id: "PRJ-1",
      snapshot_commit: "a".repeat(40),
      project_generation: 2,
      plan_id: "PLAN-1",
      revision: 3,
      plan_digest: "b".repeat(64),
      task_count: 2,
      phase: "APPROVAL_PENDING",
      content_digest: "c".repeat(64),
    };
    expect(decodePlanApprovalProjection(projection)).toEqual(projection);
    expect(() => decodePlanApprovalProjection({ ...projection, extra: true })).toThrow(DockyardClientError);
    expect(() => decodePlanApprovalProjection({ ...projection, plan_digest: "short" })).toThrow(DockyardClientError);
    expect(() => decodePlanApprovalProjection({ ...projection, project_generation: true })).toThrow(DockyardClientError);
    expect(() => decodePlanApprovalProjection({ ...projection, phase: "APPROVED" })).toThrow(DockyardClientError);
  });

  it("reads the current approval projection from the project overview", async () => {
    const base = {
      schema_version: "dockyard.plan-approval-projection/v1",
      project_id: "PRJ-1",
      snapshot_commit: "a".repeat(40),
      project_generation: 1,
      plan_id: "PLAN-1",
      revision: 2,
      plan_digest: "b".repeat(64),
      task_count: 1,
      phase: "APPROVAL_PENDING",
      content_digest: "",
    };
    const values = [
      base.project_id, base.snapshot_commit, String(base.project_generation), base.plan_id,
      String(base.revision), base.plan_digest, String(base.task_count), base.phase,
    ];
    const projection = {
      ...base,
      content_digest: await sha256(values.map((value) => `${value.length}:${value}`).join("")),
    };
    const overview = {
      schema_version: "agentdesk.dockyard-projection/v1",
      project_id: "PRJ-1",
      adoption_level: "standard",
      snapshot_commit: "9".repeat(64),
      updated_at: "2026-08-04T00:00:00Z",
      generated_at: "2026-08-04T00:00:00Z",
      pm_mode: "manual",
      state_counts: [],
      active_task_ids: [],
      pending_approval_task_ids: [],
      recent_delivery_task_ids: [],
      provider_summaries: [],
      health: {
        snapshot_state: "VERIFIED", pm_state: "ACTIVE", scheduler_state: "AVAILABLE",
        runner_state: "IDLE", provider_state: "UNKNOWN", issue_codes: ["provider_evidence_missing"],
        checked_at: "2026-08-04T00:00:00Z", content_digest: "d".repeat(64),
      },
      content_digest: "e".repeat(64),
    };
    const envelope = {
      schema_version: "dockyard.operational-overview/v1",
      project_id: "PRJ-1",
      snapshot_commit: "a".repeat(40),
      overview,
      plan_approval: projection,
      content_digest: "f".repeat(64),
    };
    const fetcher = vi.fn(async () => new Response(JSON.stringify(envelope), { status: 200 }));
    const client = new DockyardClient(
      "http://127.0.0.1:7001",
      { deviceId: "DEV-1", bearerToken: "token", csrfValue: "CSRF-1" },
      () => "1",
      fetcher as typeof fetch,
    );
    await expect(client.readPlanApproval("PRJ-1")).resolves.toEqual(projection);
    expect(fetcher).toHaveBeenCalledWith(
      "http://127.0.0.1:7001/api/dockyard/v1/projects/PRJ-1/overview",
      { method: "GET", headers: { Accept: "application/json" } },
    );
    expect(client.eventsUrl("PRJ-1")).toBe(
      "http://127.0.0.1:7001/api/dockyard/v1/projects/PRJ-1/events",
    );

    const substituted = { ...envelope, project_id: "PRJ-EVIL" };
    const substitutedFetcher = vi.fn(async () => new Response(JSON.stringify(substituted), { status: 200 }));
    const substitutedClient = new DockyardClient(
      "http://127.0.0.1:7001",
      { deviceId: "DEV-1", bearerToken: "token", csrfValue: "CSRF-1" },
      () => "1",
      substitutedFetcher as typeof fetch,
    );
    await expect(substitutedClient.readPlanApproval("PRJ-1")).rejects.toBeInstanceOf(DockyardClientError);
  });

  it("strictly decodes project and editable plan projections", async () => {
    const project = {
      schema_version: "dockyard.project-planning/v1",
      project_id: "PRJ-1",
      snapshot_commit: "a".repeat(40),
      project_generation: 1,
      plan_id: "PLAN-1",
    };
    expect(decodeProjectPlanningProjection(project, "PRJ-1")).toEqual(project);
    expect(() => decodeProjectPlanningProjection({ ...project, plan_id: "" }, "PRJ-1")).toThrow(DockyardClientError);
    const plan = {
      schema_version: "dockyard.plan-projection/v1",
      project_id: "PRJ-1",
      snapshot_commit: "a".repeat(40),
      project_generation: 1,
      plan_id: "PLAN-1",
      revision: 1,
      phase: "DRAFTED",
      requirement: "实现需求入口",
      tasks: [{
        task_id: "TC-1", title: "实现需求入口", description: "实现需求入口",
        dependencies: [], execution_mode: "serial", role_id: "R1", provider_id: "codex",
        model_id: "gpt-5.6-sol", budget_tokens: 32000, max_attempts: 3,
      }],
      plan_digest: "b".repeat(64),
      content_digest: "c".repeat(64),
    };
    expect(decodePlanProjection(plan)).toEqual(plan);
    expect(() => decodePlanProjection({ ...plan, tasks: [{ ...plan.tasks[0], max_attempts: 4 }] })).toThrow(DockyardClientError);
    expect(() => decodePlanProjection({ ...plan, tasks: [{ ...plan.tasks[0], base_commit: "a".repeat(40) }] })).toThrow(DockyardClientError);
  });

  it("reads and verifies the live MAD-completed review list", async () => {
    const base = {
      schema_version: "dockyard.review-projection/v1" as const,
      project_id: "PRJ-1",
      snapshot_commit: "a".repeat(40),
      task_id: "TC-001",
      revision: 1,
      attempt: 1,
      dispatch_id: "DSP-001",
      implementation_commit: "b".repeat(40),
      report_commit: "c".repeat(40),
      audit_verdict: "pass" as const,
      review_phase: "MAD_COMPLETED" as const,
      content_digest: "",
    };
    const values = [
      base.project_id, base.snapshot_commit, base.task_id, String(base.revision),
      String(base.attempt), base.dispatch_id, base.implementation_commit,
      base.report_commit, base.audit_verdict, base.review_phase,
    ];
    const review = { ...base, content_digest: await sha256(values.map((value) => `${value.length}:${value}`).join("")) };
    expect(decodeReviewProjection(review)).toEqual(review);
    const fetcher = vi.fn(async () => new Response(JSON.stringify({
      schema_version: "dockyard.task-list/v1",
      project_id: "PRJ-1",
      snapshot_commit: "a".repeat(40),
      tasks: [],
      reviews: [review],
      content_digest: "d".repeat(64),
    }), { status: 200 }));
    const client = new DockyardClient(
      "http://127.0.0.1:7001",
      { deviceId: "DEV-1", bearerToken: "token", csrfValue: "CSRF-1" },
      () => "1",
      fetcher as typeof fetch,
    );
    await expect(client.readReviews("PRJ-1")).resolves.toMatchObject({ reviews: [review] });
    const divergent = new DockyardClient(
      "http://127.0.0.1:7001",
      { deviceId: "DEV-1", bearerToken: "token", csrfValue: "CSRF-1" },
      () => "1",
      vi.fn(async () => new Response(JSON.stringify({
        schema_version: "dockyard.task-list/v1",
        project_id: "PRJ-1",
        snapshot_commit: "b".repeat(40),
        tasks: [],
        reviews: [review],
        content_digest: "d".repeat(64),
      }), { status: 200 })) as typeof fetch,
    );
    await expect(divergent.readReviews("PRJ-1")).rejects.toBeInstanceOf(DockyardClientError);
    expect(() => decodeReviewProjection({ ...review, dispatch_id: "" })).toThrow(DockyardClientError);
  });

  it("strictly decodes live task run approval and provider collections", async () => {
    const common = { project_id: "PRJ-1", snapshot_commit: "a".repeat(40), content_digest: "d".repeat(64) };
    const responses: Record<string, object> = {
      tasks: { ...common, schema_version: "dockyard.task-list/v1", tasks: [{
        task_id: "TC-1", revision: 1, state: "in_progress", attempt: 1,
        role_id: "R1", provider_id: "codex", model_id: "gpt-5.6-sol",
        deliberation_tier: "balanced", delivery_state: null, integration_state: null,
        updated_at: "2026-08-04T00:00:00Z", blocked_kind: null, has_report: false,
        content_digest: "1".repeat(64),
      }], reviews: [] },
      runs: { ...common, schema_version: "dockyard.run-list/v1", runs: [{
        task_id: "TC-1", revision: 1, attempt: 1, dispatch_id: "DSP-1",
        phase: "ACKNOWLEDGED", supervisor_state: "ALIVE", worker_state: "ALIVE",
        heartbeat_state: "ACTIVE", lease_state: "HELD", started_at: "2026-08-04T00:00:00Z",
        updated_at: "2026-08-04T00:00:01Z", retry_count: 0, generation_id: "GEN-1",
        termination_available: false, termination_event_id: null,
        retry_available: false, retry_event_id: null, retry_failure_kind: null,
        retry_provider_id: null, retry_model_id: null, content_digest: "2".repeat(64),
      }] },
      approvals: { ...common, schema_version: "dockyard.approval-list/v1", approvals: [{
        task_id: "TC-1", revision: 1, granted_approval_count: 1,
        latest_acceptance_decision: null, owner_approval_gate: "granted",
        pending_user_decision: false, updated_at: "2026-08-04T00:00:00Z",
        content_digest: "3".repeat(64),
      }] },
      providers: { ...common, schema_version: "dockyard.provider-list/v1", providers: [{
        provider_id: "codex", model_id: "gpt-5.6-sol", health: "READY",
        reason_code: null, executable_version: "1.0", decoder_version: "v1",
        binding_id: "BIND-1", required_capabilities: ["coding"],
        checked_at: "2026-08-04T00:00:00Z", content_digest: "4".repeat(64),
      }] },
    };
    const fetcher = vi.fn(async (input: RequestInfo | URL) => {
      const key = String(input).split("/").at(-1) as keyof typeof responses;
      return new Response(JSON.stringify(responses[key]), { status: 200 });
    });
    const client = new DockyardClient(
      "http://127.0.0.1:7001",
      { deviceId: "DEV-1", bearerToken: "token", csrfValue: "CSRF-1" },
      () => "1",
      fetcher as typeof fetch,
    );
    await expect(client.readTasks("PRJ-1")).resolves.toMatchObject({ tasks: [{ task_id: "TC-1" }] });
    await expect(client.readRuns("PRJ-1")).resolves.toMatchObject({ runs: [{ dispatch_id: "DSP-1" }] });
    await expect(client.readApprovals("PRJ-1")).resolves.toMatchObject({ approvals: [{ granted_approval_count: 1 }] });
    await expect(client.readProviders("PRJ-1")).resolves.toMatchObject({ providers: [{ health: "READY" }] });

    responses.tasks = {
      ...(responses.tasks as object),
      tasks: [{ ...((responses.tasks as { tasks: readonly object[] }).tasks[0]), secret: "must reject" }],
    };
    await expect(client.readTasks("PRJ-1")).rejects.toBeInstanceOf(DockyardClientError);
  });
});
