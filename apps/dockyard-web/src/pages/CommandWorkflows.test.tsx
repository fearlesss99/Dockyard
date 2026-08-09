import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { canConfirm } from "../components/ConfirmationDialog";
import { DockyardApp } from "../App";
import {
  CommandWorkflows,
  approvalActionFromProjection,
  acceptanceActionFromProjection,
  canOpenCommandAtViewport,
  cancelActionFromTask,
  clearedCommandConfirmation,
  commandViewportRestrictionMessage,
  commandSuccessMessage,
  commandFailureFeedback,
  commandOwnerConnectedForRoute,
  discardPlanAction,
  confirmationForAction,
  editorTasksFromProjection,
  invalidatedProjectionFeedback,
  movePlanTask,
  planSubmissionProblem,
  removePlanTask,
  removalActionFromProjection,
  returnActionFromProjection,
  reviewActionsFromProjections,
  retryActionFromProjection,
  terminationActionFromProjection,
  taskCommandProjectionsMatch,
  shouldShowCommandUnavailable,
  verifiedProjectionMessage,
  verifiedProjectionState,
} from "./CommandWorkflows";
import type { DockyardLiveRunSummary, DockyardPlanApprovalProjection, DockyardPlanProjection, DockyardReviewProjection, DockyardRunListProjection, DockyardTaskListProjection } from "../types/api";
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

  it("treats an empty but successfully-read review projection as connected", () => {
    expect(commandOwnerConnectedForRoute("reviews", true, false)).toBe(true);
    expect(commandOwnerConnectedForRoute("reviews", false, true)).toBe(false);
    expect(commandOwnerConnectedForRoute("tasks", false, true)).toBe(true);
    expect(commandOwnerConnectedForRoute("runs", true, true)).toBe(false);
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

  it("reports a missing PM-Codex agent without pretending the plan is stale", () => {
    const error = new DockyardClientError(409, {
      schema_version: "dockyard.error/v1",
      request_id: "REQ-PM-1",
      error_code: "PM_AGENT_UNAVAILABLE",
      category: "not_ready",
      retryable: false,
      safe_message: "request was not accepted",
      correlation_digest: "b".repeat(64),
    });
    expect(commandFailureFeedback(error)).toEqual({
      state: "failed",
      message: "PM-Codex Agent 未就绪；为避免本地规则冒充 PM，系统没有创建计划或任务卡。",
    });
  });

  it.each([
    ["PM_CODEX_PROXY_CERTIFICATE_UNTRUSTED", "无法信任当前代理的 TLS 证书"],
    ["PM_CODEX_DISPATCH_FAILED", "CLI 调用失败"],
    ["PM_CODEX_OUTPUT_INVALID", "计划格式未通过校验"],
    ["PM_CODEX_PLAN_REJECTED", "不符合本地任务与路由策略"],
  ])("reports the safe PM-Codex failure branch %s", (errorCode, message) => {
    const error = new DockyardClientError(502, {
      schema_version: "dockyard.error/v1",
      request_id: "REQ-PM-SAFE-1",
      error_code: errorCode,
      category: "provider",
      retryable: false,
      safe_message: "request was not accepted",
      correlation_digest: "d".repeat(64),
    });
    expect(commandFailureFeedback(error)).toEqual({
      state: "failed",
      message: expect.stringContaining(message),
    });
  });

  it("reports a structured upstream timeout without claiming success", () => {
    const error = new DockyardClientError(504, {
      schema_version: "dockyard.error/v1",
      request_id: "REQ-UPSTREAM-1",
      error_code: "UPSTREAM_TIMEOUT",
      category: "transport",
      retryable: true,
      safe_message: "upstream request did not complete",
      correlation_digest: "c".repeat(64),
    });
    const feedback = commandFailureFeedback(error);
    expect(feedback.state).toBe("failed");
    expect(feedback.message).toContain("超时");
  });

  it("offers an explicit confirmed discard for an unapproved one-task draft", () => {
    const plan: DockyardPlanProjection = {
      schema_version: "dockyard.plan-projection/v1",
      project_id: "PRJ-REAL",
      snapshot_commit: "a".repeat(40),
      project_generation: 1,
      plan_id: "PLAN-REAL",
      revision: 2,
      phase: "DRAFTED",
      requirement: "旧草案",
      tasks: [{
        task_id: "TC-OLD", title: "旧任务", description: "旧任务", dependencies: [],
        execution_mode: "parallel", role_id: "R1", provider_id: "codex",
        model_id: "gpt-5.6-sol", budget_tokens: 1, max_attempts: 1,
      }],
      plan_digest: "b".repeat(64),
      content_digest: "c".repeat(64),
    };
    const action = discardPlanAction(plan);
    expect(action?.draft.commandType).toBe("plan.discard");
    expect(action?.draft.payload).toEqual({ plan_id: "PLAN-REAL", plan_digest: "b".repeat(64) });
    expect(action?.confirmation.requiredPhrase).toBe("放弃 PLAN-REAL");
    expect(discardPlanAction({ ...plan, phase: "APPROVAL_PENDING" })).toBeNull();
  });

  it("clears the entire confirmation context when navigation changes route", () => {
    expect(clearedCommandConfirmation()).toEqual({
      active: null,
      acknowledged: false,
      phrase: "",
    });
  });

  it("removes a draft task and detaches dependencies without mutating the source", () => {
    const tasks: readonly PlanEditorTask[] = [
      {
        taskId: "TC-001", title: "需求分析", description: "分析", executionMode: "parallel",
        dependencies: [], agentId: "pm", providerId: "codex", modelId: "gpt-5.6-sol", budgetTokens: 100,
        maxAttempts: 1,
      },
      {
        taskId: "TC-002", title: "实现", description: "实现", executionMode: "serial",
        dependencies: ["TC-001", "TC-KEEP"], agentId: "r1", providerId: "claude", modelId: "claude-sonnet",
        budgetTokens: 200, maxAttempts: 2,
      },
    ];
    const remaining = removePlanTask(tasks, "TC-001");
    expect(remaining).toHaveLength(1);
    expect(remaining[0]?.taskId).toBe("TC-002");
    expect(remaining[0]?.dependencies).toEqual(["TC-KEEP"]);
    expect(tasks).toHaveLength(2);
    expect(removePlanTask(tasks, "TC-MISSING")).toBe(tasks);
    const lastTask = remaining;
    expect(removePlanTask(lastTask, "TC-002")).toBe(lastTask);
  });

  it("keeps plan submission actionable when draft input is invalid", () => {
    expect(planSubmissionProblem("   ", [])).toBe("请输入非空需求后再提交审批。");
    expect(planSubmissionProblem("实现需求", [])).toBe("计划至少需要保留一个任务后才能提交审批。");
    expect(planSubmissionProblem("实现需求", [{
      taskId: "TC-001", title: "实现", description: "实现", executionMode: "serial",
      dependencies: [], agentId: "R1", providerId: "codex", modelId: "gpt-5.6-sol",
      budgetTokens: 100, maxAttempts: 1,
    }])).toBeNull();
  });

  it("builds a durable soft-delete action only for quiescent task states", () => {
    const task = {
      task_id: "TC-READY", revision: 2, state: "ready", attempt: null,
      role_id: null, provider_id: null, model_id: null, deliberation_tier: null,
      delivery_state: null, integration_state: null, updated_at: "2026-08-07T00:00:00Z",
      blocked_kind: null, has_report: false, content_digest: "d".repeat(64),
    } as const;
    const action = cancelActionFromTask(task, "PRJ-REAL", "a".repeat(40));
    expect(action?.label).toBe("删除任务");
    expect(action?.draft.commandType).toBe("task.cancel");
    expect(action?.draft.payload).toEqual({
      task_id: "TC-READY",
      event_id: "EVT-DOCKYARD-CANCEL-TC-READY-2",
    });
    expect(action?.confirmation.mobileAllowed).toBe(false);
    expect(canOpenCommandAtViewport(action!, 390)).toBe(false);
    expect(canOpenCommandAtViewport(action!, 1440)).toBe(true);
    expect(commandViewportRestrictionMessage(action!, 390)).toContain("移动端保持只读");
    expect(commandViewportRestrictionMessage(action!, 1440)).toBeNull();
    expect(cancelActionFromTask({ ...task, role_id: "R1" }, "PRJ-REAL", "a".repeat(40))).toBeNull();
    expect(cancelActionFromTask({ ...task, state: "in_progress" }, "PRJ-REAL", "a".repeat(40))).toBeNull();
    expect(cancelActionFromTask({ ...task, state: "cancelled" }, "PRJ-REAL", "a".repeat(40))).toBeNull();
  });

  it("combines task and run commands only from one verified snapshot", () => {
    const tasks = {
      schema_version: "dockyard.task-list/v1",
      project_id: "PRJ-REAL",
      snapshot_commit: "a".repeat(40),
      tasks: [],
      reviews: [],
      content_digest: "b".repeat(64),
    } satisfies DockyardTaskListProjection;
    const runs = {
      schema_version: "dockyard.run-list/v1",
      project_id: "PRJ-REAL",
      snapshot_commit: "a".repeat(40),
      runs: [],
      content_digest: "c".repeat(64),
    } satisfies DockyardRunListProjection;
    expect(taskCommandProjectionsMatch(tasks, runs)).toBe(true);
    expect(taskCommandProjectionsMatch(tasks, { ...runs, snapshot_commit: "d".repeat(40) })).toBe(false);
    expect(taskCommandProjectionsMatch(tasks, null)).toBe(false);
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
