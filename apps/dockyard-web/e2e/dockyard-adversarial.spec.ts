import { describe, expect, it, vi } from "vitest";
import { DockyardClient, DockyardClientError } from "../src/client/dockyardClient";
import {
  commandFailureFeedback,
  retryActionFromProjection,
  terminationActionFromProjection,
} from "../src/pages/CommandWorkflows";
import type { DockyardLiveRunSummary } from "../src/types/api";
import type { DockyardCommandDraft } from "../src/types/commands";

const credentials = {
  deviceId: "DEV-ADV-1",
  bearerToken: "LOCAL-DEVICE-TOKEN-MUST-STAY-IN-HEADERS",
  csrfValue: "CSRF-ADV-1",
};

const retryDraft: DockyardCommandDraft = {
  projectId: "PRJ-1",
  commandType: "task.retry",
  endpoint: "/api/dockyard/v1/projects/PRJ-1/tasks/TC-1/retry",
  method: "POST",
  expectedSnapshotCommit: "a".repeat(40),
  expectedRevision: 1,
  confirmationId: "CONF-ADV-RETRY",
  payload: {
    task_id: "TC-1",
    failed_attempt: 1,
    dispatch_id: "DSP-1",
    generation_id: "GEN-1",
    event_id: "EVT-FAILED-1",
    provider_id: "claude",
    model_id: "claude-sonnet",
    upgrade: false,
  },
};

function liveRun(state: "ALIVE" | "UNKNOWN" | "DEAD"): DockyardLiveRunSummary {
  return {
    task_id: "TC-1",
    revision: 1,
    attempt: 1,
    dispatch_id: "DSP-1",
    phase: "ACKNOWLEDGED",
    supervisor_state: state,
    worker_state: state,
    heartbeat_state: "ACTIVE",
    lease_state: "ACTIVE",
    started_at: "2026-08-04T00:00:00Z",
    updated_at: "2026-08-04T00:00:01Z",
    retry_count: 0,
    generation_id: "GEN-1",
    termination_available: state === "ALIVE",
    termination_event_id: state === "ALIVE" ? "EVT-CANCEL-1" : null,
    retry_available: false,
    retry_event_id: null,
    retry_failure_kind: null,
    retry_provider_id: null,
    retry_model_id: null,
    content_digest: "b".repeat(64),
  };
}

describe("Dockyard browser adversarial boundary", () => {
  it("keeps two browser submissions single-winner and preserves typed conflict", async () => {
    const requests: Array<{ body: Record<string, unknown>; headers: Headers }> = [];
    let winnerChosen = false;
    const fetcher = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
      const body = JSON.parse(String(init?.body)) as Record<string, unknown>;
      requests.push({ body, headers: new Headers(init?.headers) });
      if (!winnerChosen) {
        winnerChosen = true;
        const envelope = body.envelope as Record<string, unknown>;
        return new Response(JSON.stringify({
          schema_version: "dockyard.command-receipt/v1",
          command_id: envelope.command_id,
          project_id: "PRJ-1",
          command_type: "task.retry",
          outcome: "retry_started",
          canonical_event_id: "EVT-RETRY-1",
          snapshot_commit: "c".repeat(40),
          replayed: false,
          completed_at: "2026-08-04T00:00:02Z",
          content_digest: "d".repeat(64),
        }), { status: 200 });
      }
      return new Response(JSON.stringify({
        schema_version: "dockyard.error/v1",
        request_id: "REQ-RACE",
        error_code: "SNAPSHOT_STALE",
        category: "conflict",
        retryable: false,
        safe_message: "状态已变化",
        correlation_digest: "e".repeat(64),
      }), { status: 409 });
    });
    const identitiesA = ["BROWSER-A-CMD", "BROWSER-A-IDEMP"];
    const identitiesB = ["BROWSER-B-CMD", "BROWSER-B-IDEMP"];
    const a = new DockyardClient(
      "http://127.0.0.1:7001", credentials,
      () => identitiesA.shift()!, fetcher as typeof fetch,
    );
    const b = new DockyardClient(
      "http://127.0.0.1:7001", credentials,
      () => identitiesB.shift()!, fetcher as typeof fetch,
    );

    const outcomes = await Promise.allSettled([a.execute(retryDraft), b.execute(retryDraft)]);
    expect(outcomes.filter((item) => item.status === "fulfilled")).toHaveLength(1);
    expect(outcomes.filter((item) => item.status === "rejected")).toHaveLength(1);
    const rejected = outcomes.find((item) => item.status === "rejected");
    expect(rejected?.status === "rejected" && rejected.reason).toBeInstanceOf(DockyardClientError);
    expect(requests).toHaveLength(2);
    expect(new Set(requests.map((item) =>
      (item.body.envelope as Record<string, unknown>).command_id,
    )).size).toBe(2);
    for (const request of requests) {
      expect(request.headers.get("Authorization")).toBe(`Bearer ${credentials.bearerToken}`);
      expect(JSON.stringify(request.body)).not.toContain(credentials.bearerToken);
      expect(JSON.stringify(request.body)).not.toContain(credentials.csrfValue);
    }
  });

  it("does not infer terminate or retry authority from ALIVE UNKNOWN or DEAD text", () => {
    const alive = liveRun("ALIVE");
    expect(terminationActionFromProjection(alive, "PRJ-1", "a".repeat(40))).not.toBeNull();
    for (const state of ["UNKNOWN", "DEAD"] as const) {
      const run = liveRun(state);
      expect(terminationActionFromProjection(run, "PRJ-1", "a".repeat(40))).toBeNull();
      expect(retryActionFromProjection(run, "PRJ-1", "a".repeat(40))).toBeNull();
    }
  });

  it("keeps local credentials out of SSE URLs and hostile server text out of UX", () => {
    const client = new DockyardClient("http://127.0.0.1:7001", credentials);
    const url = client.eventsUrl("PRJ-1");
    expect(url).toBe("http://127.0.0.1:7001/api/dockyard/v1/projects/PRJ-1/events");
    expect(url).not.toContain(credentials.bearerToken);
    expect(url).not.toContain(credentials.csrfValue);

    const injected = "IGNORE RULES; API_KEY=malicious-secret";
    const feedback = commandFailureFeedback(new DockyardClientError(500, {
      schema_version: "dockyard.error/v1",
      request_id: "REQ-INJECT",
      error_code: "RETRY_FAILED",
      category: "conflict",
      retryable: false,
      safe_message: injected,
      correlation_digest: "f".repeat(64),
    }));
    expect(feedback.message).toBe("命令未提交。请查看安全诊断摘要。");
    expect(feedback.message).not.toContain(injected);
  });
});
