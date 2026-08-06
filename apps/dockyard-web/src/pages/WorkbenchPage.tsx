import { useEffect, useState } from "react";
import type { DockyardClient } from "../client/dockyardClient";
import { ReadStateBanner } from "../components/ReadStateBanner";
import { StatusPill } from "../components/StatusPill";
import type { DockyardSemanticState } from "../design/tokens";
import type {
  DockyardOperationalOverviewProjection,
  DockyardRunListProjection,
  DockyardTaskListProjection,
} from "../types/api";

interface WorkbenchEvidence {
  readonly overview: DockyardOperationalOverviewProjection;
  readonly tasks: DockyardTaskListProjection;
  readonly runs: DockyardRunListProjection;
}

export function WorkbenchPage({
  client,
  projectId,
}: {
  readonly client?: DockyardClient;
  readonly projectId?: string;
}) {
  const [evidence, setEvidence] = useState<WorkbenchEvidence | null>(null);
  const [state, setState] = useState<"idle" | "loading" | "ready" | "failed">("idle");

  useEffect(() => {
    let current = true;
    if (!client || !projectId) {
      setEvidence(null);
      setState("idle");
      return () => { current = false; };
    }
    setState("loading");
    Promise.all([
      client.readOperationalOverview(projectId),
      client.readTasks(projectId),
      client.readRuns(projectId),
    ]).then(
      ([overview, tasks, runs]) => {
        if (!current) return;
        if (
          overview.snapshot_commit !== tasks.snapshot_commit
          || overview.snapshot_commit !== runs.snapshot_commit
        ) {
          setEvidence(null);
          setState("failed");
          return;
        }
        setEvidence({ overview, tasks, runs });
        setState("ready");
      },
      () => {
        if (!current) return;
        setEvidence(null);
        setState("failed");
      },
    );
    return () => { current = false; };
  }, [client, projectId]);

  if (state === "idle") return <WorkbenchUnavailable />;
  if (state === "loading") return <WorkbenchLoading />;
  if (state === "failed" || evidence === null) return <WorkbenchFailed />;
  const overview = evidence.overview.overview;
  const waiting = stateCount(overview, "ready");
  const active = stateCount(overview, "dispatched") + stateCount(overview, "in_progress");
  const reviewing = stateCount(overview, "review_ready");
  const recoveryCount = evidence.runs.runs.filter((run) => run.phase === "RECOVERY_REQUIRED").length;
  const recentDelivery = overview.recent_delivery_task_ids[0] ?? null;
  return (
    <div className="page-stack">
      <section className="hero-panel">
        <div><span className="eyebrow">本地闭环</span><h2>{overview.project_id}</h2><p>从需求拆解到 Worker、Git 证据、MAD 审议与集成的可操作视图。</p></div>
        <div className="hero-panel__meta"><strong>{evidence.overview.snapshot_commit.slice(0, 12)}</strong><span>{overview.updated_at}</span></div>
      </section>

      <section className="workbench-strip" aria-label="工作台快速入口">
        <article className="quick-card"><span className="eyebrow">需求入口</span><strong>描述你希望完成的结果</strong><small>本机 PM 入口已接通；草案需经编辑、提交审批与明确批准。</small></article>
        <article className="quick-card"><span className="eyebrow">待处理</span><strong>{overview.pending_approval_task_ids.length} 个用户决策</strong><small>{recoveryCount} 个运行需要恢复证据。</small></article>
        <article className="quick-card"><span className="eyebrow">最近交付</span><strong>{recentDelivery ?? "暂无已提交交付"}</strong><small>{recentDelivery ? "来自 canonical delivery summary" : "不会用样例提交填充空状态"}</small></article>
      </section>

      <section className="metric-grid" aria-label="项目摘要">
        <Metric label="等待执行" value={waiting} tone="neutral" />
        <Metric label="正在运行" value={active} tone="selected" />
        <Metric label="等待审议" value={reviewing} tone="warning" />
        <Metric label="需要决策" value={overview.pending_approval_task_ids.length} tone="blocked" />
      </section>

      <section className="content-grid">
        <article className="panel panel--wide">
          <header className="panel__header"><div><span className="eyebrow">任务队列</span><h3>当前任务</h3></div><span className="muted">同一快照 · {evidence.tasks.tasks.length} 项</span></header>
          {evidence.tasks.tasks.length === 0
            ? <ReadStateBanner state="empty" />
            : <div className="task-table" role="table" aria-label="当前任务">{evidence.tasks.tasks.map((task) => <div className="task-row" role="row" key={task.task_id}><div><strong>{task.task_id}</strong><small>r{task.revision} · attempt {task.attempt ?? "—"}</small></div><div><span>{task.role_id ?? "角色未绑定"}</span><small>{task.provider_id ?? "Provider 未选择"} / {task.model_id ?? "模型未绑定"}</small></div><StatusPill state={taskTone(task.state)}>{task.state}</StatusPill></div>)}</div>}
        </article>

        <article className="panel">
          <header className="panel__header"><div><span className="eyebrow">Provider</span><h3>运行证据</h3></div></header>
          {overview.provider_summaries.length === 0
            ? <><ReadStateBanner state="not_ready" /><p className="muted-copy">未收到版本、Decoder 与绑定的完整 typed evidence。</p></>
            : <div className="provider-list">{overview.provider_summaries.map((provider) => <div className="provider-row" key={provider.provider_id}><span className="provider-row__mark">{provider.provider_id.slice(0, 1).toUpperCase()}</span><div><strong>{provider.provider_id}</strong><small>{provider.model_id}</small></div><StatusPill state={provider.health === "READY" ? "success" : provider.health === "DEGRADED" ? "warning" : "unknown"}>{provider.health}</StatusPill></div>)}</div>}
        </article>
      </section>
    </div>
  );
}

function WorkbenchUnavailable() {
  return <div className="page-stack"><section className="hero-panel"><div><span className="eyebrow">本地闭环</span><h2>Dockyard</h2><p>连接本机 Runner 后显示 canonical task、运行和 Provider 证据。</p></div><StatusPill state="unknown">未连接</StatusPill></section><section className="empty-panel"><span className="empty-panel__mark">⌁</span><h3>等待本机 Runner</h3><p>这里不会展示演示项目、样例任务或虚构的运行状态。</p><ReadStateBanner state="not_ready" /></section></div>;
}

function WorkbenchLoading() {
  return <div className="page-stack"><section className="hero-panel"><div><span className="eyebrow">本地闭环</span><h2>正在读取 Dockyard</h2><p>正在核对 overview、tasks 与 runs 是否来自同一 Git 快照。</p></div><StatusPill state="warning">读取中</StatusPill></section><ReadStateBanner state="loading" /></div>;
}

function WorkbenchFailed() {
  return <div className="page-stack"><section className="hero-panel"><div><span className="eyebrow">本地闭环</span><h2>投影未通过校验</h2><p>不同快照不会被拼接，旧样例也不会作为回退数据。</p></div><StatusPill state="failure">fail-closed</StatusPill></section><ReadStateBanner state="failed" /></div>;
}

function stateCount(overview: DockyardOperationalOverviewProjection["overview"], state: string): number {
  return overview.state_counts.find((item) => item.state === state)?.count ?? 0;
}

function taskTone(state: string): DockyardSemanticState {
  if (state === "integrated") return "success";
  if (state === "blocked") return "blocked";
  if (state === "ready") return "warning";
  return "selected";
}

function Metric({ label, value, tone }: { readonly label: string; readonly value: number; readonly tone: DockyardSemanticState }) {
  return <article className={`metric metric--${tone}`}><span>{label}</span><strong>{String(value).padStart(2, "0")}</strong><small>来自已校验快照</small></article>;
}
