import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { canConfirm } from "../components/ConfirmationDialog";
import { DockyardApp } from "../App";
import {
  CommandWorkflows,
  approvalActionFromProjection,
  acceptanceActionFromProjection,
  commandSuccessMessage,
  commandFailureFeedback,
  confirmationForAction,
  editorTasksFromProjection,
  invalidatedProjectionFeedback,
  movePlanTask,
  removalActionFromProjection,
  returnActionFromProjection,
  reviewActionsFromProjections,
  retryActionFromProjection,
  terminationActionFromProjection,
  shouldShowCommandUnavailable,
  verifiedProjectionMessage,
  verifiedProjectionState,
} from "./CommandWorkflows";
import type { DockyardLiveRunSummary, DockyardPlanApprovalProjection, DockyardPlanProjection, DockyardReviewProjection, DockyardRunListProjection } from "../types/api";
import { DockyardClient, DockyardClientError } from "../client/dockyardClient";
import type { PlanEditorTask } from "../types/commands";

describe("Dockyard command confirmation workflows", () => {
  const projection: DockyardPlanApprovalProjection = {
    schema_version: "dockyard.plan-approval-projection/v1",
    project_id: "PRJ-REAL",
    snapshot_commit: "a".repeat(40),
    project_generation: 4,
    plan_id: "PLAN-REAL",
    revision: 3,
    plan_digest: "b".repeat(64),
    task_count: 2,
    phase: "APPROVAL_PENDING",
    content_digest: "c".repeat(64),
  };

  it("clears only SSE stale feedback after a verified projection refresh", () => {
    expect(verifiedProjectionState("stale")).toBe("idle");
    expect(verifiedProjectionMessage(
      "状态已变化。正在刷新当前投影，请重新核对后确认。",
    )).toBe("");
    expect(verifiedProjectionMessage("状态已变化，请刷新后重新提交需求。")).toBe("");
    expect(verifiedProjectionState("failed")).toBe("failed");
    expect(verifiedProjectionState("committed")).toBe("committed");
    expect(verifiedProjectionMessage("命令未提交。请查看安全诊断摘要。")).toBe(
      "命令未提交。请查看安全诊断摘要。",
    );
  });

  it("preserves a verified terminal success after its pending action disappears", () => {
    expect(commandSuccessMessage("delivery.accept", "a".repeat(40))).toBe(
      "已验收交付并完成 Git 集成。",
    );
    expect(shouldShowCommandUnavailable("reviews", 0, "committed")).toBe(false);
    expect(shouldShowCommandUnavailable("reviews", 0, "idle")).toBe(true);
    expect(shouldShowCommandUnavailable("reviews", 1, "idle")).toBe(false);
    expect(commandSuccessMessage("plan.approve", "12345678" + "a".repeat(32))).toBe(
      "已提交：plan.approve · 12345678",
    );
  });

  it("fences a real terminal receipt from late SSE invalidation", () => {
    expect(invalidatedProjectionFeedback("已验收交付并完成 Git 集成。")).toEqual({
      state: "committed",
      message: "已验收交付并完成 Git 集成。",
    });
    expect(invalidatedProjectionFeedback("")).toEqual({
      state: "stale",
      message: "状态已变化。正在刷新当前投影，请重新核对后确认。",
    });
  });

  it("reports Provider NOT_READY without pretending the snapshot is stale", () => {
    const error = new DockyardClientError(409, {
      schema_version: "dockyard.error/v1",
      request_id: "REQ-1",
      error_code: "PROVIDER_NOT_READY",
      category: "not_ready",
      retryable: false,
      safe_message: "请求未被 Dockyard 接受。",
      correlation_digest: "a".repeat(64),
    });
    expect(commandFailureFeedback(error)).toEqual({
      state: "failed",
      message: "所选 Provider 尚未就绪；未创建 dispatch，也未启动 Worker。",
    });
  });

  it("binds plan approval to revision digest and task count", () => {
    const confirmation = confirmationForAction("approve-plan");
    expect(confirmation.facts.map((fact) => fact.label)).toEqual([
      "Plan", "Revision", "Digest", "任务数量",
    ]);
    expect(confirmation.mobileAllowed).toBe(true);
  });

  it("builds approval and removal only from the displayed projection", () => {
    const approval = approvalActionFromProjection(projection);
    expect(approval.draft.projectId).toBe("PRJ-REAL");
    expect(approval.draft.expectedSnapshotCommit).toBe("a".repeat(40));
    expect(approval.draft.expectedRevision).toBe(3);
    expect(approval.draft.payload).toEqual({
      plan_id: "PLAN-REAL", plan_digest: "b".repeat(64), task_count: 2,
    });
    const removal = removalActionFromProjection(projection);
    expect(canConfirm(removal.confirmation, true, "不会删除 Git 仓库")).toBe(true);
    expect(removal.confirmation.mobileAllowed).toBe(false);
    expect(removal.draft.expectedRevision).toBe(4);
    expect(removal.draft.payload).toEqual({
      project_id: "PRJ-REAL", acknowledgement: "不会删除 Git 仓库",
    });
  });

  it("binds explicit acceptance to the displayed MAD-completed delivery", () => {
    const review: DockyardReviewProjection = {
      schema_version: "dockyard.review-projection/v1",
      project_id: "PRJ-REAL",
      snapshot_commit: "a".repeat(40),
      task_id: "TC-001",
      revision: 1,
      attempt: 2,
      dispatch_id: "DSP-002",
      implementation_commit: "b".repeat(40),
      report_commit: "c".repeat(40),
      audit_verdict: "pass",
      review_phase: "MAD_COMPLETED",
      content_digest: "d".repeat(64),
    };
    const action = acceptanceActionFromProjection(review);
    expect(action.label).toBe("验收并集成交付");
    expect(action.confirmation.consequence).toContain("不会重跑 Worker 或审议");
    expect(action.draft.commandType).toBe("delivery.accept");
    expect(action.draft.expectedSnapshotCommit).toBe(review.snapshot_commit);
    expect(action.draft.payload).toEqual({
      task_id: "TC-001",
      implementation_commit: "b".repeat(40),
      report_commit: "c".repeat(40),
    });
  });

  it("binds an explicit return only to displayed fail-review evidence", () => {
    const review: DockyardReviewProjection = {
      schema_version: "dockyard.review-projection/v1",
      project_id: "PRJ-REAL",
      snapshot_commit: "a".repeat(40),
      task_id: "TC-FAIL",
      revision: 2,
      attempt: 3,
      dispatch_id: "DSP-FAIL",
      implementation_commit: "b".repeat(40),
      report_commit: "c".repeat(40),
      audit_verdict: "fail",
      review_phase: "MAD_COMPLETED",
      content_digest: "d".repeat(64),
    };
    const action = returnActionFromProjection(review);
    expect(action.label).toBe("退回返修");
    expect(action.confirmation.consequence).toContain("不会启动 Worker");
    expect(action.confirmation.requiredPhrase).toBe("退回 TC-FAIL");
    expect(action.confirmation.mobileAllowed).toBe(false);
    expect(action.draft.commandType).toBe("delivery.return");
    expect(action.draft.endpoint).toContain("/deliveries/TC-FAIL/return");
    expect(action.draft.payload).toEqual({
      task_id: "TC-FAIL",
      implementation_commit: "b".repeat(40),
      report_commit: "c".repeat(40),
    });
    expect(commandSuccessMessage("delivery.return", "a".repeat(40))).toBe(
      "已退回返修；任务已恢复为待派发状态。",
    );
    expect(reviewActionsFromProjections([review])).toEqual([action]);
    expect(reviewActionsFromProjections([{ ...review, audit_verdict: "blocked" }])).toEqual([]);
  });

  it("creates a destructive terminate command only from exact active ownership evidence", () => {
    const run: DockyardLiveRunSummary = {
      task_id: "TC-ACTIVE",
      revision: 2,
      attempt: 1,
      dispatch_id: "DSP-ACTIVE",
      phase: "ACKNOWLEDGED",
      supervisor_state: "UNKNOWN",
      worker_state: "UNKNOWN",
      heartbeat_state: "UNKNOWN",
      lease_state: "UNKNOWN",
      started_at: null,
      updated_at: "2026-08-04T00:00:00Z",
      retry_count: 0,
      generation_id: "GEN-ACTIVE",
      termination_available: true,
      termination_event_id: "EVT-CANCEL-123456789012345678901234",
      retry_available: false, retry_event_id: null, retry_failure_kind: null,
      retry_provider_id: null, retry_model_id: null,
      content_digest: "e".repeat(64),
    };
    const action = terminationActionFromProjection(run, "PRJ-REAL", "a".repeat(40));
    expect(action?.draft.commandType).toBe("dispatch.terminate");
    expect(action?.draft.payload).toEqual({
      task_id: "TC-ACTIVE",
      attempt: 1,
      dispatch_id: "DSP-ACTIVE",
      generation_id: "GEN-ACTIVE",
      event_id: "EVT-CANCEL-123456789012345678901234",
    });
    expect(action?.confirmation.requiredPhrase).toBe("终止 DSP-ACTIVE");
    expect(terminationActionFromProjection({ ...run, termination_available: false }, "PRJ-REAL", "a".repeat(40))).toBeNull();
    const runs: DockyardRunListProjection = {
      schema_version: "dockyard.run-list/v1",
      project_id: "PRJ-REAL",
      snapshot_commit: "a".repeat(40),
      runs: [run],
      content_digest: "f".repeat(64),
    };
    const html = renderToStaticMarkup(<CommandWorkflows
      routeId="runs"
      projectId="PRJ-REAL"
      client={{} as DockyardClient}
      runProjection={runs}
    />);
    expect(html).toContain("终止进程");
    expect(html).toContain('data-mobile="true"');
  });

  it("creates a retry command only from finalized retry evidence", () => {
    const run: DockyardLiveRunSummary = {
      task_id: "TC-1", revision: 2, attempt: 1, dispatch_id: "DSP-1",
      phase: "FAILED", supervisor_state: "DONE", worker_state: "DONE",
      heartbeat_state: "DONE", lease_state: "RELEASED", started_at: null,
      updated_at: "2026-08-04T00:00:00Z", retry_count: 0, generation_id: "GEN-1",
      termination_available: false, termination_event_id: null,
      retry_available: true, retry_event_id: "EVT-FAILED-1",
      retry_failure_kind: "worker_failed", retry_provider_id: "claude",
      retry_model_id: "claude-sonnet", content_digest: "a".repeat(64),
    };
    const action = retryActionFromProjection(run, "PRJ-1", "b".repeat(40));
    expect(action?.draft.commandType).toBe("task.retry");
    expect(action?.draft.payload).toMatchObject({
      task_id: "TC-1", failed_attempt: 1, dispatch_id: "DSP-1",
      generation_id: "GEN-1", event_id: "EVT-FAILED-1", upgrade: false,
    });
    expect(action?.confirmation.requiredPhrase).toBe("重试 TC-1");
    expect(retryActionFromProjection({ ...run, retry_available: false }, "PRJ-1", "b".repeat(40))).toBeNull();
  });

  it("creates a desktop-only returned-delivery redispatch from durable evidence", () => {
    const run: DockyardLiveRunSummary = {
      task_id: "TC-RETURNED", revision: 3, attempt: 1, dispatch_id: "DSP-RETURNED",
      phase: "RETURNED", supervisor_state: "NOT_APPLICABLE", worker_state: "NOT_APPLICABLE",
      heartbeat_state: "DONE", lease_state: "RELEASED", started_at: null,
      updated_at: "2026-08-04T00:00:00Z", retry_count: 0, generation_id: "GEN-RETURNED",
      termination_available: false, termination_event_id: null,
      retry_available: true, retry_event_id: "EVT-REQUEUED-RETURNED",
      retry_failure_kind: "delivery_returned", retry_provider_id: "claude",
      retry_model_id: "claude-sonnet", content_digest: "c".repeat(64),
    };
    const action = retryActionFromProjection(run, "PRJ-1", "d".repeat(40));
    expect(action?.id).toBe("redispatch-DSP-RETURNED");
    expect(action?.label).toBe("开始返修");
    expect(action?.confirmation.requiredPhrase).toBe("返修 TC-RETURNED");
    expect(action?.confirmation.mobileAllowed).toBe(false);
    expect(action?.confirmation.consequence).toContain("同一 revision");
    expect(action?.confirmation.consequence).toContain("MAD 失败反馈");
    expect(action?.draft.commandType).toBe("task.retry");
    expect(action?.draft.payload).toMatchObject({
      task_id: "TC-RETURNED", failed_attempt: 1, dispatch_id: "DSP-RETURNED",
      generation_id: "GEN-RETURNED", event_id: "EVT-REQUEUED-RETURNED",
      provider_id: "claude", model_id: "claude-sonnet", upgrade: false,
    });
  });

  it("renders no executable command without runtime evidence", () => {
    const pages = ["/requirements", "/tasks", "/runs", "/reviews", "/workers", "/projects"];
    const html = pages.map((path) => renderToStaticMarkup(<DockyardApp initialPath={path} />)).join("\n");
    expect(html).not.toContain('data-mobile="true"');
    expect(html).not.toContain('data-mobile="false"');
    expect(html).toContain("not_ready");
  });

  it("shows a live target as disabled evidence without an executable control", () => {
    const html = renderToStaticMarkup(<CommandWorkflows
      routeId="runs"
      projectId="PRJ-LIVE"
      client={{} as DockyardClient}
      liveEvidence={[{ identity: "DSP-LIVE", summary: "TC-LIVE · ACKNOWLEDGED · verified process identity 缺失" }]}
    />);
    expect(html).toContain("DSP-LIVE");
    expect(html).toContain("不可操作");
    expect(html).toContain("生产 owner 尚未接入");
    expect(html).not.toContain("<button");
    expect(html).not.toContain("confirmation");
  });

  it("renders a real requirement entry without mock task rows", () => {
    const html = renderToStaticMarkup(<DockyardApp initialPath="/requirements" />);
    expect(html).toContain("需求文本");
    expect(html).toContain("生成计划草案");
    expect(html).not.toContain("TC-001 · 控制 API");
    expect(html).not.toContain("输入运行时未就绪");
    expect(html).not.toContain('value="4"');
  });

  it("projects only server-owned editable task evidence", () => {
    const plan: DockyardPlanProjection = {
      schema_version: "dockyard.plan-projection/v1",
      project_id: "PRJ-REAL",
      snapshot_commit: "a".repeat(40),
      project_generation: 1,
      plan_id: "PLAN-REAL",
      revision: 1,
      phase: "DRAFTED",
      requirement: "实现需求入口",
      tasks: [{
        task_id: "TC-SERVER",
        title: "实现需求入口",
        description: "实现需求入口",
        dependencies: [],
        execution_mode: "serial",
        role_id: "R1",
        provider_id: "codex",
        model_id: "gpt-5.6-sol",
        budget_tokens: 32000,
        max_attempts: 3,
      }],
      plan_digest: "b".repeat(64),
      content_digest: "c".repeat(64),
    };
    expect(editorTasksFromProjection(plan)[0]).toMatchObject({
      taskId: "TC-SERVER", description: "实现需求入口", maxAttempts: 3,
    });
  });

  it("moves task order deterministically", () => {
    const tasks: readonly PlanEditorTask[] = [
      { taskId: "A", title: "A", description: "A", executionMode: "parallel", dependencies: [], agentId: "R1", providerId: "codex", modelId: "m", budgetTokens: 1, maxAttempts: 1 },
      { taskId: "B", title: "B", description: "B", executionMode: "serial", dependencies: ["A"], agentId: "R1", providerId: "codex", modelId: "m", budgetTokens: 1, maxAttempts: 1 },
    ];
    expect(movePlanTask(tasks, "B", -1).map((task) => task.taskId)).toEqual(["B", "A"]);
    expect(movePlanTask(tasks, "A", -1)).toBe(tasks);
  });
});
