import { useEffect, useState } from "react";
import type { DockyardClient } from "../client/dockyardClient";
import { ReadStateBanner } from "../components/ReadStateBanner";
import { StatusPill } from "../components/StatusPill";
import type { DockyardSemanticState } from "../design/tokens";
import type {
  DockyardOperationalOverviewProjection,
  DockyardRunListProjection,
  DockyardSseEventType,
  DockyardTaskListProjection,
} from "../types/api";

interface WorkbenchEvidence {
  readonly overview: DockyardOperationalOverviewProjection;
  readonly tasks: DockyardTaskListProjection;
  readonly runs: DockyardRunListProjection;
}

interface WorkbenchPageProps {
  readonly client?: DockyardClient;
  readonly projectId?: string;
  readonly onNavigate?: (path: string) => void;
}

export type WorkbenchStreamState = "unavailable" | "connecting" | "connected" | "reconnecting";

export const WORKBENCH_READY_STREAM_LABELS: Readonly<Record<WorkbenchStreamState, string>> = {
  unavailable: "安全投影已校验 · SSE 不可用",
  connecting: "安全投影已校验 · SSE 连接中",
  connected: "安全投影已校验 · SSE 已连接",
  reconnecting: "保留已校验快照 · SSE 重连中",
};

export function isWorkbenchStreamConnected(streamState: WorkbenchStreamState): boolean {
  return streamState === "connected";
}

const ACTIVE_RUN_PHASES = new Set([
  "RESERVED",
  "SUPERVISOR_READY",
  "WORKER_STARTED",
  "ACKNOWLEDGED",
  "FINALIZING",
  "RETRY_RESERVED",
  "RETRY_STARTED",
  "RETRY_FINALIZING",
]);

export const WORKBENCH_REFRESH_EVENT_TYPES = [
  "snapshot.changed",
  "task.changed",
  "run.changed",
  "approval.changed",
  "review.changed",
  "provider.changed",
  "project.changed",
  "health.changed",
] as const satisfies readonly DockyardSseEventType[];

export function WorkbenchPage({ client, projectId, onNavigate }: WorkbenchPageProps) {
  const [evidence, setEvidence] = useState<WorkbenchEvidence | null>(null);
  const [state, setState] = useState<"idle" | "loading" | "ready" | "failed">("idle");
  const [refresh, setRefresh] = useState(0);
  const [streamState, setStreamState] = useState<WorkbenchStreamState>("unavailable");

  useEffect(() => {
    let current = true;
    if (!client || !projectId) {
      setEvidence(null);
      setState("idle");
      return () => { current = false; };
    }
    setState((current) => current === "ready" ? "ready" : "loading");
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
  }, [client, projectId, refresh]);

  useEffect(() => {
    if (!client || !projectId || typeof EventSource === "undefined") {
      setStreamState("unavailable");
      return;
    }
    setStreamState("connecting");
    const source = new EventSource(client.eventsUrl(projectId));
    let reconnectTimer: ReturnType<typeof setTimeout> | undefined;
    const refreshEvidence = () => setRefresh((value) => value + 1);
    source.onopen = () => {
      if (reconnectTimer !== undefined) clearTimeout(reconnectTimer);
      reconnectTimer = undefined;
      setStreamState("connected");
    };
    source.onerror = () => {
      if (reconnectTimer !== undefined) clearTimeout(reconnectTimer);
      reconnectTimer = setTimeout(() => setStreamState("reconnecting"), 5000);
    };
    for (const eventType of WORKBENCH_REFRESH_EVENT_TYPES) {
      source.addEventListener(eventType, refreshEvidence);
    }
    return () => {
      if (reconnectTimer !== undefined) clearTimeout(reconnectTimer);
      source.close();
    };
  }, [client, projectId]);

  if (state === "idle") return <WorkbenchUnavailable onNavigate={onNavigate} />;
  if (state === "loading") return <WorkbenchLoading onNavigate={onNavigate} />;
  if (state === "failed" || evidence === null) return <WorkbenchFailed onNavigate={onNavigate} />;

  const overview = evidence.overview.overview;
  const waiting = stateCount(overview, "ready");
  const active = evidence.runs.runs.filter(isActiveRun).length;
  const reviewing = stateCount(overview, "review_ready");
  const recoveryCount = evidence.runs.runs.filter((run) => run.phase === "RECOVERY_REQUIRED").length;
  const recentDelivery = overview.recent_delivery_task_ids[0] ?? null;

  return (
    <div className="page-stack page-stack--workbench">
      <WorkbenchHero
        state="ready"
        projectId={overview.project_id}
        snapshot={evidence.overview.snapshot_commit.slice(0, 12)}
        updatedAt={overview.updated_at}
        waiting={waiting}
        active={active}
        reviewing={reviewing}
        decisions={overview.pending_approval_task_ids.length}
        streamState={streamState}
        onNavigate={onNavigate}
      />

      <section className="workbench-summary" aria-labelledby="workbench-summary-title">
        <header className="section-heading">
          <div>
            <span className="eyebrow">已验证快照</span>
            <h2 id="workbench-summary-title">当前工作流</h2>
          </div>
          <span className="muted">所有摘要来自同一 Git 快照</span>
        </header>

        <div className="workbench-strip" aria-label="工作台快速入口">
          <article className="quick-card">
            <span className="eyebrow">需求入口</span>
            <strong>描述下一项希望完成的工作</strong>
            <small>生成可编辑计划后，再提交审批与明确批准。</small>
            <button className="text-link" type="button" onClick={() => onNavigate?.("/requirements")}>前往计划</button>
          </article>
          <article className="quick-card">
            <span className="eyebrow">待处理</span>
            <strong>{overview.pending_approval_task_ids.length} 个用户决策</strong>
            <small>{recoveryCount} 个运行需要恢复证据。</small>
            <button className="text-link" type="button" onClick={() => onNavigate?.("/runs")}>查看运行</button>
          </article>
          <article className="quick-card">
            <span className="eyebrow">最近交付</span>
            <strong>{recentDelivery ?? "暂无已提交交付"}</strong>
            <small>{recentDelivery ? "来自规范交付摘要" : "不会用样例提交填充空状态"}</small>
            <button className="text-link" type="button" onClick={() => onNavigate?.("/reviews")}>查看审议</button>
          </article>
        </div>

        <div className="metric-grid" aria-label="项目摘要">
          <Metric label="等待执行" value={waiting} tone="neutral" />
          <Metric label="正在运行" value={active} tone="selected" />
          <Metric label="等待审议" value={reviewing} tone="warning" />
          <Metric label="需要决策" value={overview.pending_approval_task_ids.length} tone="blocked" />
        </div>

        <div className="content-grid">
          <article className="panel panel--wide">
            <header className="panel__header">
              <div><span className="eyebrow">任务队列</span><h3>当前任务</h3></div>
              <span className="muted">同一快照 · {evidence.tasks.tasks.length} 项</span>
            </header>
            {evidence.tasks.tasks.length === 0
              ? <ReadStateBanner state="empty" />
              : (
                <div className="task-table" role="table" aria-label="当前任务">
                  {evidence.tasks.tasks.map((task) => (
                    <div className="task-row" role="row" key={task.task_id}>
                      <div><strong>{task.task_id}</strong><small>r{task.revision} · 尝试 {task.attempt ?? "—"}</small></div>
                      <div><span>{task.role_id ?? "角色未绑定"}</span><small>{task.provider_id ?? "Provider 未选择"} / {task.model_id ?? "模型未绑定"}</small></div>
                      <StatusPill state={taskTone(task.state)}>{task.state}</StatusPill>
                    </div>
                  ))}
                </div>
              )}
          </article>

          <article className="panel">
            <header className="panel__header"><div><span className="eyebrow">运行资源</span><h3>Provider 证据</h3></div></header>
            {overview.provider_summaries.length === 0
              ? <><ReadStateBanner state="not_ready" /><p className="muted-copy">未收到版本、Decoder 与绑定的完整 typed evidence。</p></>
              : (
                <div className="provider-list">
                  {overview.provider_summaries.map((provider) => (
                    <div className="provider-row" key={provider.provider_id}>
                      <span className="provider-row__mark">{provider.provider_id.slice(0, 1).toUpperCase()}</span>
                      <div><strong>{provider.provider_id}</strong><small>{provider.model_id}</small></div>
                      <StatusPill state={provider.health === "READY" ? "success" : provider.health === "DEGRADED" ? "warning" : "unknown"}>{provider.health}</StatusPill>
                    </div>
                  ))}
                </div>
              )}
          </article>
        </div>
      </section>
    </div>
  );
}

function WorkbenchHero({
  state,
  projectId,
  snapshot,
  updatedAt,
  waiting,
  active,
  reviewing,
  decisions,
  streamState,
  onNavigate,
}: {
  readonly state: "ready" | "idle" | "loading" | "failed";
  readonly projectId: string;
  readonly snapshot: string;
  readonly updatedAt: string;
  readonly waiting: number | null;
  readonly active: number | null;
  readonly reviewing: number | null;
  readonly decisions: number | null;
  readonly streamState: WorkbenchStreamState;
  readonly onNavigate?: (path: string) => void;
}) {
  const ready = state === "ready";
  const statusText = state === "ready"
    ? WORKBENCH_READY_STREAM_LABELS[streamState]
    : state === "loading"
      ? "正在校验快照"
      : state === "failed"
        ? "投影校验失败"
        : "等待本机 Runner";

  return (
    <section className="workbench-hero" aria-labelledby="dockyard-title">
      <div className="workbench-hero__intro">
        <div className="update-badge"><strong>{ready ? "已校验" : "本机"}</strong><span>运行状态总览</span></div>
        <h1 id="dockyard-title">Dockyard</h1>
        <p>让需求、任务、运行、审议与交付沿着同一条可验证链路推进。</p>
      </div>

      <section className="overview-console" aria-label="运行状态总览">
        <header className="overview-console__header">
          <div>
            <span className={ready && isWorkbenchStreamConnected(streamState) ? "console-dot" : "console-dot console-dot--idle"} aria-hidden="true" />
            <strong>{statusText}</strong>
          </div>
          <div className="overview-console__snapshot">
            <span>{projectId}</span>
            <code>{snapshot}</code>
          </div>
        </header>

        <div className="overview-console__metrics">
          <HeroMetric label="等待执行" value={waiting} />
          <HeroMetric label="正在运行" value={active} />
          <HeroMetric label="等待审议" value={reviewing} />
          <HeroMetric label="需要决策" value={decisions} />
        </div>

        <div className="overview-console__action">
          <button type="button" onClick={() => onNavigate?.("/requirements")}>
            <span><strong>描述下一项工作</strong><small>进入需求与方案，生成可审阅的计划草案</small></span>
            <span className="submit-icon" aria-hidden="true">↑</span>
          </button>
          <span>{updatedAt}</span>
        </div>
      </section>
    </section>
  );
}

function WorkbenchUnavailable({ onNavigate }: { readonly onNavigate?: (path: string) => void }) {
  return (
    <div className="page-stack page-stack--workbench">
      <WorkbenchHero state="idle" projectId="本地工作区" snapshot="等待快照" updatedAt="Runner 未连接" waiting={null} active={null} reviewing={null} decisions={null} streamState="unavailable" onNavigate={onNavigate} />
      <section className="workbench-summary workbench-summary--empty">
        <div className="empty-panel"><span className="empty-panel__mark">⌁</span><h2>等待本机 Runner</h2><p>这里不会展示演示项目、样例任务或虚构的运行状态。连接后会显示真实项目、任务、运行、Provider 与交付状态。</p><ReadStateBanner state="not_ready" /></div>
      </section>
    </div>
  );
}

function WorkbenchLoading({ onNavigate }: { readonly onNavigate?: (path: string) => void }) {
  return (
    <div className="page-stack page-stack--workbench">
      <WorkbenchHero state="loading" projectId="Dockyard" snapshot="校验中" updatedAt="正在读取" waiting={null} active={null} reviewing={null} decisions={null} streamState="connecting" onNavigate={onNavigate} />
      <section className="workbench-summary workbench-summary--empty"><ReadStateBanner state="loading" /></section>
    </div>
  );
}

function WorkbenchFailed({ onNavigate }: { readonly onNavigate?: (path: string) => void }) {
  return (
    <div className="page-stack page-stack--workbench">
      <WorkbenchHero state="failed" projectId="Dockyard" snapshot="fail-closed" updatedAt="未展示旧数据" waiting={null} active={null} reviewing={null} decisions={null} streamState="unavailable" onNavigate={onNavigate} />
      <section className="workbench-summary workbench-summary--empty"><ReadStateBanner state="failed" /></section>
    </div>
  );
}

function HeroMetric({ label, value }: { readonly label: string; readonly value: number | null }) {
  return <div><span>{label}</span><strong>{value === null ? "—" : String(value).padStart(2, "0")}</strong></div>;
}

function stateCount(overview: DockyardOperationalOverviewProjection["overview"], state: string): number {
  return overview.state_counts.find((item) => item.state === state)?.count ?? 0;
}

function isActiveRun(run: DockyardRunListProjection["runs"][number]): boolean {
  return run.termination_available || ACTIVE_RUN_PHASES.has(run.phase);
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
