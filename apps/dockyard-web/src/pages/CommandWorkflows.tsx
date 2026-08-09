import { useEffect, useMemo, useRef, useState } from "react";
import { DockyardClient, DockyardClientError } from "../client/dockyardClient";
import { ConfirmationDialog } from "../components/ConfirmationDialog";
import { StatusPill } from "../components/StatusPill";
import type { DockyardRouteId } from "../navigation/routes";
import type {
  DockyardPlanApprovalProjection,
  DockyardPlanProjection,
  DockyardProjectPlanningProjection,
  DockyardReviewProjection,
  DockyardRunListProjection,
  DockyardTaskListProjection,
  DockyardLiveTaskSummary,
  DockyardLiveRunSummary,
} from "../types/api";
import type {
  CommandConfirmation,
  CommandUiState,
  DockyardCommandDraft,
  PlanEditorTask,
} from "../types/commands";

const STALE_PROJECTION_MESSAGE = "状态已变化。正在刷新当前投影，请重新核对后确认。";

export function verifiedProjectionState(state: CommandUiState): CommandUiState {
  return state === "stale" ? "idle" : state;
}

export function verifiedProjectionMessage(message: string): string {
  return message === STALE_PROJECTION_MESSAGE || message.startsWith("状态已变化") ? "" : message;
}

export function commandSuccessMessage(commandType: string, snapshotCommit: string): string {
  if (commandType === "plan.discard") {
    return "草案已放弃；历史证据保留，现可生成一份新的 PM-Codex 草案。";
  }
  if (commandType === "delivery.accept") {
    return "已验收交付并完成 Git 集成。";
  }
  if (commandType === "delivery.return") {
    return "已退回返修；任务已恢复为待派发状态。";
  }
  return `已提交：${commandType} · ${snapshotCommit.slice(0, 8)}`;
}

export function shouldShowCommandUnavailable(
  routeId: DockyardRouteId,
  actionCount: number,
  state: CommandUiState,
): boolean {
  return actionCount === 0 && routeId !== "requirements" && state !== "committed";
}

export function invalidatedProjectionFeedback(terminalFeedback: string): {
  readonly state: CommandUiState;
  readonly message: string;
} {
  return terminalFeedback
    ? { state: "committed", message: terminalFeedback }
    : { state: "stale", message: STALE_PROJECTION_MESSAGE };
}

export function commandFailureFeedback(error: unknown): {
  readonly state: CommandUiState;
  readonly message: string;
} {
  if (
    error instanceof DockyardClientError
    && error.envelope?.error_code === "PROVIDER_NOT_READY"
  ) {
    return {
      state: "failed",
      message: "所选 Provider 尚未就绪；未创建 dispatch，也未启动 Worker。",
    };
  }
  if (
    error instanceof DockyardClientError
    && error.envelope?.error_code === "AGENT_ROUTING_UNAVAILABLE"
  ) {
    return {
      state: "failed",
      message: "PM 没有找到满足该任务难度的 Agent；请打开高级路由覆盖，指定已配置的 Agent，或先补齐本机 Provider 绑定。",
    };
  }
  if (
    error instanceof DockyardClientError
    && error.envelope?.error_code === "PM_AGENT_UNAVAILABLE"
  ) {
    return {
      state: "failed",
      message: "PM-Codex Agent 未就绪；为避免本地规则冒充 PM，系统没有创建计划或任务卡。",
    };
  }
  if (
    error instanceof DockyardClientError
    && error.envelope?.error_code === "PM_CODEX_PROXY_CERTIFICATE_UNTRUSTED"
  ) {
    return {
      state: "failed",
      message: "PM-Codex 无法信任当前代理的 TLS 证书；未创建计划或任务卡。请在 Windows“当前用户 > 受信任的根证书颁发机构”安装代理根证书后重试；不要关闭代理或跳过证书校验。",
    };
  }
  if (
    error instanceof DockyardClientError
    && error.envelope?.error_code === "PM_CODEX_DISPATCH_FAILED"
  ) {
    return {
      state: "failed",
      message: "PM-Codex CLI 调用失败；未创建计划或任务卡。请确认 Codex 登录、模型与网络可用后重试。",
    };
  }
  if (
    error instanceof DockyardClientError
    && error.envelope?.error_code === "PM_CODEX_OUTPUT_INVALID"
  ) {
    return {
      state: "failed",
      message: "PM-Codex 返回的计划格式未通过校验；未创建计划或任务卡。请重试生成。",
    };
  }
  if (
    error instanceof DockyardClientError
    && error.envelope?.error_code === "PM_CODEX_PLAN_REJECTED"
  ) {
    return {
      state: "failed",
      message: "PM-Codex 计划不符合本地任务与路由策略；未创建计划或任务卡。请调整需求或 Provider 能力后重试。",
    };
  }
  if (
    error instanceof DockyardClientError
    && error.envelope?.error_code === "UPSTREAM_TIMEOUT"
  ) {
    return {
      state: "failed",
      message: "本机 Runner 等待上游操作超时；未收到命令回执，请核对诊断后重试。",
    };
  }
  if (
    error instanceof DockyardClientError
    && error.envelope?.error_code === "UPSTREAM_UNAVAILABLE"
  ) {
    return {
      state: "failed",
      message: "本机 Runner 上游暂不可用；未收到命令回执，请核对诊断后重试。",
    };
  }
  if (error instanceof DockyardClientError && error.status === 409) {
    return {
      state: "stale",
      message: "状态已变化。已刷新快照；请重新核对并确认，未覆盖旧状态。",
    };
  }
  return { state: "failed", message: "命令未提交。请查看安全诊断摘要。" };
}

export interface CommandAction {
  readonly id: string;
  readonly label: string;
  readonly confirmation: CommandConfirmation;
  readonly draft: DockyardCommandDraft;
}

export function clearedCommandConfirmation(): {
  readonly active: null;
  readonly acknowledged: false;
  readonly phrase: "";
} {
  return { active: null, acknowledged: false, phrase: "" };
}

export const MOBILE_COMMAND_BREAKPOINT = 900;
const MOBILE_COMMAND_RESTRICTION_MESSAGE = "该操作仅允许在桌面端核对并提交，移动端保持只读。";

export function canOpenCommandAtViewport(action: CommandAction, viewportWidth: number): boolean {
  return action.confirmation.mobileAllowed || viewportWidth > MOBILE_COMMAND_BREAKPOINT;
}

export function commandViewportRestrictionMessage(
  action: CommandAction,
  viewportWidth: number,
): string | null {
  return canOpenCommandAtViewport(action, viewportWidth)
    ? null
    : MOBILE_COMMAND_RESTRICTION_MESSAGE;
}

export function taskCommandProjectionsMatch(
  tasks: DockyardTaskListProjection | undefined,
  runs: DockyardRunListProjection | null | undefined,
): boolean {
  return tasks !== undefined && runs != null && tasks.snapshot_commit === runs.snapshot_commit;
}

export interface LiveCommandEvidence {
  readonly identity: string;
  readonly summary: string;
}

export function confirmationForAction(id: string): CommandConfirmation {
  if (id === "approve-plan") {
    return {
      title: "批准当前计划",
      consequence: "批准后才允许生成版本化任务卡；本操作本身不会启动 Worker。",
      facts: [
        { label: "Plan", value: "等待真实投影" },
        { label: "Revision", value: "等待真实投影" },
        { label: "Digest", value: "等待真实投影" },
        { label: "任务数量", value: "等待真实投影" },
      ],
      requiredPhrase: null,
      destructive: false,
      mobileAllowed: true,
    };
  }
  throw new Error("Unknown Dockyard action");
}

export function approvalActionFromProjection(projection: DockyardPlanApprovalProjection): CommandAction {
  return {
    id: "approve-plan",
    label: "批准计划",
    confirmation: {
      title: `批准计划 revision ${projection.revision}`,
      consequence: "批准后才允许生成版本化任务卡；本操作本身不会启动 Worker。",
      facts: [
        { label: "Plan", value: projection.plan_id },
        { label: "Revision", value: String(projection.revision) },
        { label: "Digest", value: projection.plan_digest },
        { label: "任务数量", value: String(projection.task_count) },
      ],
      requiredPhrase: null,
      destructive: false,
      mobileAllowed: true,
    },
    draft: {
      projectId: projection.project_id,
      commandType: "plan.approve",
      endpoint: `/api/dockyard/v1/projects/${encodeURIComponent(projection.project_id)}/plans/${encodeURIComponent(projection.plan_id)}/approve`,
      method: "POST",
      expectedSnapshotCommit: projection.snapshot_commit,
      expectedRevision: projection.revision,
      confirmationId: `CONF-PLAN-${projection.revision}`,
      payload: {
        plan_id: projection.plan_id,
        plan_digest: projection.plan_digest,
        task_count: projection.task_count,
      },
    },
  };
}

export function acceptanceActionFromProjection(projection: DockyardReviewProjection): CommandAction {
  return {
    id: `accept-${projection.task_id}`,
    label: "验收并集成交付",
    confirmation: {
      title: `验收 ${projection.task_id} 的交付`,
      consequence: "确认后将使用已经完成的 MAD 审议结果，不会重跑 Worker 或审议；验收通过后继续执行本机 Git 集成。",
      facts: [
        { label: "任务", value: `${projection.task_id} · r${projection.revision} · attempt ${projection.attempt}` },
        { label: "审议结论", value: projection.audit_verdict === "pass" ? "通过" : projection.audit_verdict },
        { label: "实现提交", value: projection.implementation_commit },
        { label: "交付报告", value: projection.report_commit },
      ],
      requiredPhrase: null,
      destructive: false,
      mobileAllowed: true,
    },
    draft: {
      projectId: projection.project_id,
      commandType: "delivery.accept",
      endpoint: `/api/dockyard/v1/projects/${encodeURIComponent(projection.project_id)}/deliveries/${encodeURIComponent(projection.task_id)}/accept`,
      method: "POST",
      expectedSnapshotCommit: projection.snapshot_commit,
      expectedRevision: projection.revision,
      confirmationId: `CONF-ACCEPT-${projection.dispatch_id}`,
      payload: {
        task_id: projection.task_id,
        implementation_commit: projection.implementation_commit,
        report_commit: projection.report_commit,
      },
    },
  };
}

export function returnActionFromProjection(projection: DockyardReviewProjection): CommandAction {
  return {
    id: `return-${projection.task_id}`,
    label: "退回返修",
    confirmation: {
      title: `退回 ${projection.task_id} 返修`,
      consequence: "确认后将使用已完成的失败审议证据，把任务退回并重新置为待派发；本操作不会启动 Worker。",
      facts: [
        { label: "任务", value: `${projection.task_id} · r${projection.revision} · attempt ${projection.attempt}` },
        { label: "审议结论", value: "未通过" },
        { label: "实现提交", value: projection.implementation_commit },
        { label: "交付报告", value: projection.report_commit },
      ],
      requiredPhrase: `退回 ${projection.task_id}`,
      destructive: true,
      mobileAllowed: false,
    },
    draft: {
      projectId: projection.project_id,
      commandType: "delivery.return",
      endpoint: `/api/dockyard/v1/projects/${encodeURIComponent(projection.project_id)}/deliveries/${encodeURIComponent(projection.task_id)}/return`,
      method: "POST",
      expectedSnapshotCommit: projection.snapshot_commit,
      expectedRevision: projection.revision,
      confirmationId: `CONF-RETURN-${projection.dispatch_id}`,
      payload: {
        task_id: projection.task_id,
        implementation_commit: projection.implementation_commit,
        report_commit: projection.report_commit,
      },
    },
  };
}

export function reviewActionsFromProjections(
  reviews: readonly DockyardReviewProjection[],
): readonly CommandAction[] {
  return reviews.flatMap((review) => {
    if (review.audit_verdict === "pass") return [acceptanceActionFromProjection(review)];
    if (review.audit_verdict === "fail") return [returnActionFromProjection(review)];
    return [];
  });
}

export function removalActionFromProjection(projection: DockyardPlanApprovalProjection): CommandAction {
  return {
    id: "remove-project",
    label: "移除项目",
    confirmation: {
      title: "从 Dockyard 移除项目",
      consequence: "仅执行软移除，不会删除 Git 仓库、分支、提交、worktree 或任务证据。",
      facts: [
        { label: "Project", value: projection.project_id },
        { label: "Repository", value: "保持原样" },
      ],
      requiredPhrase: "不会删除 Git 仓库",
      destructive: true,
      mobileAllowed: false,
    },
    draft: {
      projectId: projection.project_id,
      commandType: "project.remove",
      endpoint: `/api/dockyard/v1/projects/${encodeURIComponent(projection.project_id)}/remove`,
      method: "POST",
      expectedSnapshotCommit: projection.snapshot_commit,
      expectedRevision: projection.project_generation,
      confirmationId: `CONF-REMOVE-${projection.project_generation}`,
      payload: {
        project_id: projection.project_id,
        acknowledgement: "不会删除 Git 仓库",
      },
    },
  };
}

export function terminationActionFromProjection(
  run: DockyardLiveRunSummary,
  projectId: string,
  snapshotCommit: string,
): CommandAction | null {
  if (
    !run.termination_available
    || run.generation_id === null
    || run.termination_event_id === null
  ) return null;
  return {
    id: `terminate-${run.dispatch_id}`,
    label: "终止进程",
    confirmation: {
      title: `终止 ${run.dispatch_id}`,
      consequence: "仅终止当前已被 Runner registry 精确证明仍在运行的 dispatch；不会创建重试，也不会删除项目或 Git 数据。",
      facts: [
        { label: "Task", value: `${run.task_id} · r${run.revision}` },
        { label: "Attempt", value: String(run.attempt) },
        { label: "Dispatch", value: run.dispatch_id },
        { label: "Ownership", value: "ACTIVE · exact generation match" },
      ],
      requiredPhrase: `终止 ${run.dispatch_id}`,
      destructive: true,
      mobileAllowed: true,
    },
    draft: {
      projectId,
      commandType: "dispatch.terminate",
      endpoint: `/api/dockyard/v1/projects/${encodeURIComponent(projectId)}/dispatches/${encodeURIComponent(run.dispatch_id)}/terminate`,
      method: "POST",
      expectedSnapshotCommit: snapshotCommit,
      expectedRevision: run.revision,
      confirmationId: `CONF-TERM-${run.dispatch_id}`,
      payload: {
        task_id: run.task_id,
        attempt: run.attempt,
        dispatch_id: run.dispatch_id,
        generation_id: run.generation_id,
        event_id: run.termination_event_id,
      },
    },
  };
}

export function cancelActionFromTask(
  task: DockyardLiveTaskSummary,
  projectId: string,
  snapshotCommit: string,
): CommandAction | null {
  if (
    !(task.state === "draft" || task.state === "ready" || task.state === "blocked")
    || task.role_id !== null
  ) return null;
  return {
    id: `cancel-task-${task.task_id}`,
    label: "删除任务",
    confirmation: {
      title: `删除 ${task.task_id}`,
      consequence: "确认后通过 canonical TASK_CANCELLED 将任务软删除；不会删除 Git 仓库、提交或历史证据。正在运行的任务必须先走终止进程。",
      facts: [
        { label: "Task", value: `${task.task_id} · r${task.revision}` },
        { label: "当前状态", value: task.state },
        { label: "处理方式", value: "TASK_CANCELLED · durable soft delete" },
      ],
      requiredPhrase: `删除 ${task.task_id}`,
      destructive: true,
      mobileAllowed: false,
    },
    draft: {
      projectId,
      commandType: "task.cancel",
      endpoint: `/api/dockyard/v1/projects/${encodeURIComponent(projectId)}/tasks/${encodeURIComponent(task.task_id)}/cancel`,
      method: "POST",
      expectedSnapshotCommit: snapshotCommit,
      expectedRevision: task.revision,
      confirmationId: `CONF-CANCEL-${task.task_id}-${task.revision}`,
      payload: {
        task_id: task.task_id,
        event_id: `EVT-DOCKYARD-CANCEL-${task.task_id}-${task.revision}`,
      },
    },
  };
}

export function discardPlanAction(
  plan: DockyardPlanProjection,
): CommandAction | null {
  if (!(plan.phase === "DRAFTED" || plan.phase === "EDITED")) return null;
  return {
    id: `discard-plan-${plan.plan_id}-${plan.revision}`,
    label: "放弃草案",
    confirmation: {
      title: `放弃 ${plan.plan_id}`,
      consequence: "当前未批准草案将被终态放弃，历史证据会保留；随后可重新请求 PM-Codex 生成新草案。",
      facts: [
        { label: "Plan", value: `${plan.plan_id} · r${plan.revision}` },
        { label: "任务数量", value: String(plan.tasks.length) },
        { label: "处理方式", value: "DISCARDED · 保留历史，不派发任务" },
      ],
      requiredPhrase: `放弃 ${plan.plan_id}`,
      destructive: true,
      mobileAllowed: true,
    },
    draft: {
      projectId: plan.project_id,
      commandType: "plan.discard",
      endpoint: `/api/dockyard/v1/projects/${encodeURIComponent(plan.project_id)}/plans/${encodeURIComponent(plan.plan_id)}/discard`,
      method: "POST",
      expectedSnapshotCommit: plan.snapshot_commit,
      expectedRevision: plan.revision,
      confirmationId: `CONF-DISCARD-${plan.plan_id}-${plan.revision}`,
      payload: { plan_id: plan.plan_id, plan_digest: plan.plan_digest },
    },
  };
}

export function retryActionFromProjection(
  run: DockyardLiveRunSummary,
  projectId: string,
  snapshotCommit: string,
): CommandAction | null {
  if (
    !run.retry_available
    || run.generation_id === null
    || run.retry_event_id === null
    || run.retry_failure_kind === null
    || run.retry_provider_id === null
    || run.retry_model_id === null
  ) return null;
  const returnedDelivery = run.retry_failure_kind === "delivery_returned";
  return {
    id: `${returnedDelivery ? "redispatch" : "retry"}-${run.dispatch_id}`,
    label: returnedDelivery ? "开始返修" : "手动重试",
    confirmation: {
      title: `${returnedDelivery ? "返修" : "重试"} ${run.task_id}`,
      consequence: returnedDelivery
        ? "确认后使用原任务卡与已冻结的 MAD 失败反馈启动同一 revision 的下一 attempt；不会覆盖原任务卡。"
        : "确认后只启动下一次 bounded dispatch；不会覆盖失败证据，也不会降低 Provider 或审批门槛。",
      facts: [
        { label: "Task", value: `${run.task_id} · r${run.revision}` },
        { label: returnedDelivery ? "原 attempt" : "失败 attempt", value: String(run.attempt) },
        { label: "下一 attempt", value: String(run.attempt + 1) },
        { label: "Provider / Model", value: `${run.retry_provider_id} / ${run.retry_model_id}` },
        { label: returnedDelivery ? "返修来源" : "失败分类", value: returnedDelivery ? "MAD 审议未通过" : run.retry_failure_kind },
      ],
      requiredPhrase: `${returnedDelivery ? "返修" : "重试"} ${run.task_id}`,
      destructive: false,
      mobileAllowed: !returnedDelivery,
    },
    draft: {
      projectId,
      commandType: "task.retry",
      endpoint: `/api/dockyard/v1/projects/${encodeURIComponent(projectId)}/tasks/${encodeURIComponent(run.task_id)}/retry`,
      method: "POST",
      expectedSnapshotCommit: snapshotCommit,
      expectedRevision: run.revision,
      confirmationId: `${returnedDelivery ? "CONF-REDISPATCH" : "CONF-RETRY"}-${run.dispatch_id}`,
      payload: {
        task_id: run.task_id,
        failed_attempt: run.attempt,
        dispatch_id: run.dispatch_id,
        generation_id: run.generation_id,
        event_id: run.retry_event_id,
        provider_id: run.retry_provider_id,
        model_id: run.retry_model_id,
        upgrade: false,
      },
    },
  };
}

export function movePlanTask(
  tasks: readonly PlanEditorTask[],
  taskId: string,
  direction: -1 | 1,
): readonly PlanEditorTask[] {
  const index = tasks.findIndex((task) => task.taskId === taskId);
  const target = index + direction;
  if (index < 0 || target < 0 || target >= tasks.length) return tasks;
  const copy = [...tasks];
  [copy[index], copy[target]] = [copy[target], copy[index]];
  return copy;
}

/**
 * Remove one task from the local draft and detach dependencies pointing at it.
 * This is intentionally a draft-only edit; durable task evidence remains
 * immutable and can only be changed through the guarded command owners.
 */
export function removePlanTask(
  tasks: readonly PlanEditorTask[],
  taskId: string,
): readonly PlanEditorTask[] {
  if (tasks.length <= 1 || !tasks.some((task) => task.taskId === taskId)) return tasks;
  return tasks
    .filter((task) => task.taskId !== taskId)
    .map((task) => ({
      ...task,
      dependencies: task.dependencies.filter((dependency) => dependency !== taskId),
    }));
}

export function planSubmissionProblem(
  requirement: string,
  tasks: readonly PlanEditorTask[],
): string | null {
  if (!requirement.trim()) return "请输入非空需求后再提交审批。";
  if (tasks.length === 0) return "计划至少需要保留一个任务后才能提交审批。";
  return null;
}

export function editorTasksFromProjection(
  projection: DockyardPlanProjection,
): readonly PlanEditorTask[] {
  return projection.tasks.map((task) => ({
    taskId: task.task_id,
    title: task.title,
    description: task.description,
    executionMode: task.execution_mode,
    dependencies: task.dependencies,
    agentId: task.role_id,
    providerId: task.provider_id,
    modelId: task.model_id,
    budgetTokens: task.budget_tokens,
    maxAttempts: task.max_attempts,
  }));
}

function replacePlanTask(
  tasks: readonly PlanEditorTask[],
  taskId: string,
  patch: Partial<PlanEditorTask>,
): readonly PlanEditorTask[] {
  return tasks.map((task) => task.taskId === taskId ? { ...task, ...patch } : task);
}

export function CommandWorkflows({
  routeId,
  client,
  projectId,
  liveEvidence = [],
  runProjection,
  taskProjection,
  requestedAction = null,
  onRequestedActionConsumed,
  onEvidenceInvalidated,
}: {
  readonly routeId: DockyardRouteId;
  readonly client?: DockyardClient;
  readonly projectId?: string;
  readonly liveEvidence?: readonly LiveCommandEvidence[];
  readonly runProjection?: DockyardRunListProjection;
  readonly taskProjection?: DockyardTaskListProjection;
  readonly requestedAction?: CommandAction | null;
  readonly onRequestedActionConsumed?: () => void;
  readonly onEvidenceInvalidated?: () => void;
}) {
  const [active, setActive] = useState<CommandAction | null>(null);
  const [acknowledged, setAcknowledged] = useState(false);
  const [phrase, setPhrase] = useState("");
  const [state, setState] = useState<CommandUiState>("idle");
  const [message, setMessage] = useState("");
  const [requirement, setRequirement] = useState("");
  const [tasks, setTasks] = useState<readonly PlanEditorTask[]>([]);
  const [advancedRouting, setAdvancedRouting] = useState(false);
  const [projectPlanning, setProjectPlanning] = useState<DockyardProjectPlanningProjection | null>(null);
  const [plan, setPlan] = useState<DockyardPlanProjection | null>(null);
  const [projection, setProjection] = useState<DockyardPlanApprovalProjection | null>(null);
  const [reviews, setReviews] = useState<readonly DockyardReviewProjection[]>([]);
  const [taskRunProjection, setTaskRunProjection] = useState<DockyardRunListProjection | null>(null);
  const [refresh, setRefresh] = useState(0);
  const terminalFeedbackRef = useRef("");
  const activeActionRef = useRef<CommandAction | null>(null);
  const routeRef = useRef(routeId);
  activeActionRef.current = active;
  const markProjectionVerified = () => {
    setState(verifiedProjectionState);
    setMessage(verifiedProjectionMessage);
  };

  useEffect(() => {
    let current = true;
    if (!client || !projectId || routeId !== "tasks" || runProjection !== undefined) {
      setTaskRunProjection(null);
      return () => { current = false; };
    }
    client.readRuns(projectId).then(
      (value) => { if (current) setTaskRunProjection(value); },
      () => { if (current) setTaskRunProjection(null); },
    );
    return () => { current = false; };
  }, [client, projectId, routeId, refresh, runProjection]);

  const effectiveTaskRunProjection = routeId === "tasks"
    ? runProjection ?? taskRunProjection
    : null;

  useEffect(() => {
    if (
      (routeId === "runs" && runProjection !== undefined)
      || (routeId === "tasks" && taskCommandProjectionsMatch(taskProjection, effectiveTaskRunProjection))
    ) {
      markProjectionVerified();
    }
  }, [effectiveTaskRunProjection, routeId, runProjection, taskProjection]);

  useEffect(() => {
    if (routeRef.current === routeId) return;
    routeRef.current = routeId;
    const cleared = clearedCommandConfirmation();
    terminalFeedbackRef.current = "";
    setActive(cleared.active);
    setAcknowledged(cleared.acknowledged);
    setPhrase(cleared.phrase);
    setState("idle");
    setMessage("");
  }, [routeId]);

  useEffect(() => {
    let current = true;
    if (!client || !projectId || routeId !== "requirements") {
      setProjectPlanning(null);
      setPlan(null);
      setTasks([]);
      return () => { current = false; };
    }
    client.readProjectPlanning(projectId).then(async (planning) => {
      if (!current) return;
      setProjectPlanning(planning);
      try {
        const observed = await client.readPlan(projectId, planning.plan_id);
        if (!current) return;
        setPlan(observed);
        setRequirement(observed.requirement);
        setTasks(editorTasksFromProjection(observed));
        markProjectionVerified();
      } catch (error) {
        if (!current) return;
        if (error instanceof DockyardClientError && error.status === 404) {
          setPlan(null);
          setTasks([]);
          markProjectionVerified();
        } else if (error instanceof DockyardClientError && error.status === 409) {
          // Once approval/materialization advances the plan, the editable
          // projection is intentionally no longer readable.  Do not report
          // that as a failed write: the command may already have committed
          // and the next owner is visible on the Tasks/Runs pages.
          setPlan(null);
          setTasks([]);
          if (terminalFeedbackRef.current) {
            setState("committed");
            setMessage(terminalFeedbackRef.current);
          } else {
            setState("idle");
            setMessage("计划已离开编辑阶段，请前往任务或运行页面查看后续状态。");
          }
        } else {
          setState("failed");
          setMessage("计划投影不可用。未写入任何计划或任务卡。");
        }
      }
    }, () => {
      if (current) {
        setProjectPlanning(null);
        setState("failed");
        setMessage("PM-Codex Agent 未就绪，无法生成计划草案。");
      }
    });
    return () => { current = false; };
  }, [client, projectId, routeId, refresh]);

  useEffect(() => {
    let current = true;
    if (!client || !projectId || routeId !== "reviews") {
      setReviews([]);
      return () => { current = false; };
    }
    client.readReviews(projectId).then(
      (value) => {
        if (current) {
          setReviews(value.reviews);
          markProjectionVerified();
        }
      },
      () => { if (current) setReviews([]); },
    );
    return () => { current = false; };
  }, [client, projectId, routeId, refresh]);

  useEffect(() => {
    let current = true;
    if (!client || !projectId || !["requirements", "projects"].includes(routeId)) {
      setProjection(null);
      return () => { current = false; };
    }
    client.readPlanApproval(projectId).then(
      (value) => {
        if (current) {
          setProjection(value);
          markProjectionVerified();
        }
      },
      () => { if (current) setProjection(null); },
    );
    return () => { current = false; };
  }, [client, projectId, routeId, refresh]);

  useEffect(() => {
    if (!client || !projectId || typeof EventSource === "undefined") return;
    if (!["requirements", "projects", "reviews", "runs", "tasks"].includes(routeId)) return;
    const source = new EventSource(client.eventsUrl(projectId));
    const invalidate = () => {
      const hasPendingConfirmation = activeActionRef.current !== null;
      const feedback = invalidatedProjectionFeedback(terminalFeedbackRef.current);
      setProjection(null);
      setActive(null);
      if (hasPendingConfirmation || terminalFeedbackRef.current) {
        setState(feedback.state);
        setMessage(feedback.message);
      } else {
        setState("idle");
        setMessage("");
      }
      setRefresh((value) => value + 1);
    };
    source.addEventListener("snapshot.changed", invalidate);
    source.addEventListener("approval.changed", invalidate);
    source.addEventListener("project.changed", invalidate);
    source.addEventListener("review.changed", invalidate);
    source.addEventListener("task.changed", invalidate);
    source.addEventListener("run.changed", invalidate);
    return () => source.close();
  }, [client, projectId, routeId]);

  const actions = useMemo(() => {
    if (routeId === "reviews") {
      return reviewActionsFromProjections(reviews);
    }
    const commandRuns = routeId === "tasks" ? effectiveTaskRunProjection : runProjection;
    const taskProjectionsReady = routeId !== "tasks" || taskCommandProjectionsMatch(taskProjection, commandRuns);
    if (!taskProjectionsReady) return [];
    const taskActions = routeId === "tasks" && projectId && taskProjection
      ? taskProjection.tasks.flatMap((task) => {
        const action = cancelActionFromTask(task, projectId, taskProjection.snapshot_commit);
        return action ? [action] : [];
      })
      : [];
    if ((routeId === "runs" || routeId === "tasks") && projectId && commandRuns) {
      return [...taskActions, ...commandRuns.runs
        .flatMap((run) => [
          terminationActionFromProjection(run, projectId, commandRuns.snapshot_commit),
          retryActionFromProjection(run, projectId, commandRuns.snapshot_commit),
        ])
        .filter((action): action is CommandAction => action !== null)];
    }
    if (taskActions.length > 0) return taskActions;
    if (routeId === "requirements" && projection) return [approvalActionFromProjection(projection)];
    if (routeId === "projects" && projection) return [removalActionFromProjection(projection)];
    return [];
  }, [effectiveTaskRunProjection, projectId, projection, reviews, routeId, runProjection, taskProjection]);

  const createDraft = async () => {
    if (!client || !projectPlanning || !requirement.trim()) {
      setState("failed");
      setMessage("请输入非空需求，并确认 PM-Codex Agent 已连接。");
      return;
    }
    setState("submitting");
    setMessage("");
    try {
      const created = await client.createPlan(projectPlanning, requirement.trim());
      setProjectPlanning(await client.readProjectPlanning(projectPlanning.project_id));
      setPlan(created);
      setRequirement(created.requirement);
      setTasks(editorTasksFromProjection(created));
      setState("committed");
      setMessage("计划草案已由 PM-Codex 生成，并由本地控制面校验和持久化；批准前不会派发任务。");
    } catch (error) {
      const feedback = commandFailureFeedback(error);
      setState(feedback.state);
      setMessage(feedback.message);
    }
  };

  const submitForApproval = async () => {
    if (!client || !plan || plan.phase === "APPROVAL_PENDING") return;
    const problem = planSubmissionProblem(requirement, tasks);
    if (problem !== null) {
      setState("failed");
      setMessage(problem);
      return;
    }
    setState("submitting");
    setMessage("");
    try {
      const pending = await client.submitPlan(plan, requirement.trim(), tasks);
      setPlan(pending);
      setRequirement(pending.requirement);
      setTasks(editorTasksFromProjection(pending));
      setState("committed");
      setMessage("计划已提交审批；仍未启动 Scheduler 或 Worker。");
      setRefresh((value) => value + 1);
    } catch (error) {
      setState(error instanceof DockyardClientError && error.status === 409 ? "stale" : "failed");
      setMessage(error instanceof DockyardClientError && error.status === 409
        ? "计划已变化，请重新核对后提交。"
        : "计划未提交审批；未启动 Scheduler 或 Worker。");
    }
  };

  const open = (action: CommandAction) => {
    const viewportWidth = typeof window === "undefined" ? Number.POSITIVE_INFINITY : window.innerWidth;
    const restriction = commandViewportRestrictionMessage(action, viewportWidth);
    if (restriction !== null) {
      setState("failed");
      setMessage(restriction);
      setActive(null);
      return;
    }
    terminalFeedbackRef.current = "";
    setActive(action);
    setAcknowledged(false);
    setPhrase("");
    setState("confirming");
    setMessage("");
  };

  useEffect(() => {
    if (!requestedAction) return;
    open(requestedAction);
    onRequestedActionConsumed?.();
  }, [onRequestedActionConsumed, requestedAction]);

  const submit = async () => {
    if (!active || !client) {
      setState("failed");
      setMessage("本机 Control API 会话尚未配对，命令未提交。");
      setActive(null);
      return;
    }
    const viewportWidth = typeof window === "undefined" ? Number.POSITIVE_INFINITY : window.innerWidth;
    const restriction = commandViewportRestrictionMessage(active, viewportWidth);
    if (restriction !== null) {
      setState("failed");
      setMessage(restriction);
      setActive(null);
      return;
    }
    setState("submitting");
    try {
      const receipt = await client.execute(active.draft);
      const successMessage = commandSuccessMessage(
        receipt.command_type,
        receipt.snapshot_commit,
      );
      terminalFeedbackRef.current = successMessage;
      if (active.draft.commandType === "plan.discard") {
        setPlan(null);
        setTasks([]);
        setRequirement("");
      }
      setState("committed");
      setMessage(successMessage);
      setProjection(null);
      setRefresh((value) => value + 1);
      onEvidenceInvalidated?.();
    } catch (error) {
      const feedback = commandFailureFeedback(error);
      setState(feedback.state);
      setMessage(feedback.message);
      if (feedback.state === "stale") {
        setProjection(null);
        setRefresh((value) => value + 1);
        onEvidenceInvalidated?.();
      }
    } finally {
      setActive(null);
    }
  };

  if (shouldShowCommandUnavailable(routeId, actions.length, state)) {
    const taskOwnerConnected = routeId === "tasks" && taskCommandProjectionsMatch(taskProjection, effectiveTaskRunProjection);
    return (
      <section className="panel command-panel">
        <header className="panel__header">
          <div><span className="eyebrow">操作</span><h3>受控命令</h3></div>
          <StatusPill state={taskOwnerConnected ? "success" : "unknown"}>{taskOwnerConnected ? "owner 已接入" : client ? "owner 未接入" : "Runner 未连接"}</StatusPill>
        </header>
        <p className="muted-copy">{taskOwnerConnected
          ? "生产命令 owner 已接入；当前任务没有满足精确代际、租约和进程证据要求的可执行操作。"
          : client
          ? "当前页面只展示已验证证据；对应生产 owner 尚未接入，操作保持禁用。"
          : "尚未建立本机 Control API 会话。这里不会生成草稿命令，也不会显示演示操作。"}</p>
        {liveEvidence.length > 0 && <div className="record-list" data-testid="live-command-evidence">
          {liveEvidence.map((item) => <article className="record-card record-card--command" key={item.identity}><div><strong>{item.identity}</strong><small>{item.summary}</small></div><StatusPill state="unknown">不可操作</StatusPill></article>)}
        </div>}
      </section>
    );
  }
  return (
    <section className="panel command-panel">
      <header className="panel__header">
        <div><span className="eyebrow">操作</span><h3>受控命令</h3></div>
        <StatusPill state={state === "committed" ? "success" : state === "stale" ? "warning" : state === "failed" ? "failure" : "selected"}>{state}</StatusPill>
      </header>

      {routeId === "requirements" && (
        <div className="plan-editor desktop-only">
          <label className="routing-mode-toggle"><input type="checkbox" checked={advancedRouting} onChange={(event) => setAdvancedRouting(event.target.checked)} disabled={plan?.phase === "APPROVAL_PENDING"} /><span>开启高级路由覆盖</span><small>{advancedRouting ? "PM 自动分配已暂时让位给你的逐任务设置" : "PM 会按任务难度、能力和并发上限自动分配 Agent"}</small></label>
          <label className="field-stack"><span>需求文本</span><textarea value={requirement} onChange={(event) => setRequirement(event.target.value)} placeholder="描述你希望完成的结果、边界与验收标准……" disabled={plan?.phase === "APPROVAL_PENDING"} /></label>
          <div className="inline-actions inline-actions--editor">
            <button className="button button--quiet" type="button" onClick={() => setRequirement("")} disabled={!requirement || plan?.phase === "APPROVAL_PENDING"}>清空需求输入</button>
          </div>
          {tasks.length > 0 && <div className="plan-task-list">
            {tasks.map((task) => (
              <article className="plan-task" key={task.taskId}>
                <div><strong>{task.taskId}</strong><small>{task.dependencies.length ? `依赖 ${task.dependencies.join(", ")}` : "无依赖"}</small></div>
                <input value={task.title} aria-label={`${task.taskId} 标题`} onChange={(event) => setTasks(replacePlanTask(tasks, task.taskId, { title: event.target.value }))} disabled={plan?.phase === "APPROVAL_PENDING"} />
                <select value={task.executionMode} aria-label={`${task.taskId} 执行模式`} onChange={(event) => setTasks(replacePlanTask(tasks, task.taskId, { executionMode: event.target.value as "parallel" | "serial" }))} disabled={plan?.phase === "APPROVAL_PENDING" || !advancedRouting}><option value="parallel">并行</option><option value="serial">串行</option></select>
                <select value={task.providerId} aria-label={`${task.taskId} Provider`} onChange={(event) => setTasks(replacePlanTask(tasks, task.taskId, { providerId: event.target.value as PlanEditorTask["providerId"] }))} disabled={plan?.phase === "APPROVAL_PENDING" || !advancedRouting}><option>codex</option><option>claude</option><option>reasonix</option></select>
                <input value={task.modelId} aria-label={`${task.taskId} 模型`} onChange={(event) => setTasks(replacePlanTask(tasks, task.taskId, { modelId: event.target.value }))} disabled={plan?.phase === "APPROVAL_PENDING" || !advancedRouting} />
                <input type="number" min="1" value={task.budgetTokens} aria-label={`${task.taskId} 预算`} onChange={(event) => setTasks(replacePlanTask(tasks, task.taskId, { budgetTokens: Number(event.target.value) }))} disabled={plan?.phase === "APPROVAL_PENDING" || !advancedRouting} />
                <select value={task.maxAttempts} aria-label={`${task.taskId} 最大 attempt`} onChange={(event) => setTasks(replacePlanTask(tasks, task.taskId, { maxAttempts: Number(event.target.value) as 1 | 2 | 3 }))} disabled={plan?.phase === "APPROVAL_PENDING" || !advancedRouting}><option value="1">1</option><option value="2">2</option><option value="3">3</option></select>
                <div className="inline-actions">
                  <button type="button" onClick={() => setTasks(movePlanTask(tasks, task.taskId, -1))} disabled={!advancedRouting || plan?.phase === "APPROVAL_PENDING"}>上移</button>
                  <button type="button" onClick={() => setTasks(movePlanTask(tasks, task.taskId, 1))} disabled={!advancedRouting || plan?.phase === "APPROVAL_PENDING"}>下移</button>
                  <button className="button button--danger" type="button" onClick={() => setTasks(removePlanTask(tasks, task.taskId))} disabled={plan?.phase === "APPROVAL_PENDING" || tasks.length <= 1}>删除任务</button>
                </div>
              </article>
            ))}
          </div>}
          <div className="command-actions">
            {!plan && <button className="button button--primary" type="button" onClick={createDraft} disabled={!client || !projectPlanning || state === "submitting"}>生成计划草案</button>}
            {plan && plan.phase !== "APPROVAL_PENDING" && <button className="button button--primary" type="button" onClick={submitForApproval} disabled={state === "submitting"}>提交审批</button>}
            {plan && discardPlanAction(plan) && <button className="button button--danger" type="button" onClick={() => open(discardPlanAction(plan)!)} disabled={state === "submitting"}>放弃草案</button>}
          </div>
          <p className="muted-copy">PM-Codex 负责拆解需求；本地控制面只负责校验、持久化与批准后的派发。提交审批和批准计划是两个独立步骤，批准前零派发。</p>
        </div>
      )}

      {routeId === "reviews" && reviews.length > 0 && (
        <div className="record-list">
          {reviews.map((review) => (
            <article className="review-card" key={review.dispatch_id}>
              <div className="review-card__title">
                <div><span className="eyebrow">{review.task_id} · attempt {review.attempt}</span><strong>{review.audit_verdict === "pass" ? "审议已完成，等待你的验收" : review.audit_verdict === "fail" ? "审议未通过，等待退回返修" : "审议被阻塞，需要人工处理"}</strong></div>
                <StatusPill state={review.audit_verdict === "pass" ? "success" : review.audit_verdict === "fail" ? "failure" : "blocked"}>{review.audit_verdict === "pass" ? "通过" : review.audit_verdict}</StatusPill>
              </div>
              <p>{review.audit_verdict === "pass" ? "实现与交付报告已由 MAD 审议。确认验收后才会进入 Git 集成，不会自动越过门禁。" : review.audit_verdict === "fail" ? "失败审议证据已经冻结。确认退回后任务会重新变为待派发，但不会自动启动下一次 Worker。" : "审议证据尚不能形成安全结论，Dockyard 不会自动继续。"}</p>
              <dl className="detail-list"><div><dt>Dispatch</dt><dd>{review.dispatch_id}</dd></div><div><dt>实现提交</dt><dd><code>{review.implementation_commit}</code></dd></div><div><dt>报告提交</dt><dd><code>{review.report_commit}</code></dd></div></dl>
            </article>
          ))}
        </div>
      )}

      <div className="command-actions">
        {actions
          .filter((action) => !(routeId === "tasks" && action.draft.commandType === "task.cancel"))
          .map((action) => <button className={action.confirmation.destructive ? "button button--danger" : "button button--primary"} type="button" key={action.id} onClick={() => open(action)} data-mobile={action.confirmation.mobileAllowed}>{action.label}</button>)}
      </div>
      {message && <p className={`command-message command-message--${state}`}>{message}</p>}
      {active && <ConfirmationDialog confirmation={active.confirmation} acknowledged={acknowledged} phrase={phrase} busy={state === "submitting"} onAcknowledge={setAcknowledged} onPhrase={setPhrase} onCancel={() => setActive(null)} onConfirm={submit} />}
    </section>
  );
}
