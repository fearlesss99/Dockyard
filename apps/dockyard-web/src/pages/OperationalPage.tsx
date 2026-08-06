import { useEffect, useState, type ReactElement } from "react";
import { ReadStateBanner } from "../components/ReadStateBanner";
import { StatusPill } from "../components/StatusPill";
import type { DockyardClient } from "../client/dockyardClient";
import type {
  DockyardLiveOverviewProjection,
  DockyardLiveTaskSummary,
  DockyardOperationalOverviewProjection,
  DockyardProviderListProjection,
  DockyardRunListProjection,
  DockyardTaskListProjection,
} from "../types/api";
import type { DockyardRoute, DockyardRouteId } from "../navigation/routes";
import { CommandWorkflows, type LiveCommandEvidence } from "./CommandWorkflows";

type LiveData =
  | { readonly kind: "tasks"; readonly value: DockyardTaskListProjection }
  | { readonly kind: "runs"; readonly value: DockyardRunListProjection }
  | { readonly kind: "providers"; readonly value: DockyardProviderListProjection }
  | { readonly kind: "overview"; readonly value: DockyardOperationalOverviewProjection };

type LoadState = "idle" | "loading" | "ready" | "failed";
export type SseConnectionState = "connected" | "reconnecting";

export function OperationalPage({
  route,
  client,
  projectId,
}: {
  readonly route: DockyardRoute;
  readonly client?: DockyardClient;
  readonly projectId?: string;
}) {
  const routeId = route.id;
  const [data, setData] = useState<LiveData | null>(null);
  const [loadState, setLoadState] = useState<LoadState>("idle");
  const [refresh, setRefresh] = useState(0);
  const [sseState, setSseState] = useState<SseConnectionState>("connected");

  useEffect(() => {
    let current = true;
    setData(null);
    if (!client || !projectId || !needsOperationalProjection(routeId)) {
      setLoadState("idle");
      return () => { current = false; };
    }
    setLoadState("loading");
    const request = routeId === "tasks"
      ? client.readTasks(projectId).then((value): LiveData => ({ kind: "tasks", value }))
      : routeId === "runs"
        ? client.readRuns(projectId).then((value): LiveData => ({ kind: "runs", value }))
        : routeId === "workers"
          ? client.readProviders(projectId).then((value): LiveData => ({ kind: "providers", value }))
          : client.readOperationalOverview(projectId).then((value): LiveData => ({ kind: "overview", value }));
    request.then(
      (value) => {
        if (!current) return;
        setData(value);
        setLoadState("ready");
      },
      () => {
        if (!current) return;
        setData(null);
        setLoadState("failed");
      },
    );
    return () => { current = false; };
  }, [client, projectId, routeId, refresh]);

  useEffect(() => {
    if (!client || !projectId || routeId !== "runs" || typeof EventSource === "undefined") {
      setSseState("connected");
      return;
    }
    const source = new EventSource(client.eventsUrl(projectId));
    const refreshRuns = () => setRefresh((value) => value + 1);
    source.onopen = () => setSseState("connected");
    source.onerror = () => setSseState("reconnecting");
    source.addEventListener("run.changed", refreshRuns);
    source.addEventListener("snapshot.changed", refreshRuns);
    return () => source.close();
  }, [client, projectId, routeId]);

  if (routeId === "workbench") return null;
  return (
    <div className="page-stack">
      <section className="hero-panel hero-panel--compact">
        <div><span className="eyebrow">{route.eyebrow}</span><h2>{route.label}</h2><p>{route.description}</p></div>
        <StatusPill state={loadState === "failed" ? "failure" : loadState === "loading" ? "warning" : loadState === "ready" ? "selected" : "unknown"}>
          {loadState === "failed" ? "投影不可用" : loadState === "loading" ? "读取中" : loadState === "ready" ? "已校验证据" : "Runner 未连接"}
        </StatusPill>
      </section>
      {sseConnectionBannerState(sseState, data !== null) === "reconnecting"
        ? <ReadStateBanner state="reconnecting" />
        : null}
      {renderPage(routeId, loadState, data)}
      <CommandWorkflows
        routeId={routeId}
        client={client}
        projectId={projectId}
        liveEvidence={routeId === "runs" ? [] : liveCommandEvidence(data)}
        runProjection={data?.kind === "runs" ? data.value : undefined}
      />
    </div>
  );
}

export function liveCommandEvidence(data: LiveData | null): readonly LiveCommandEvidence[] {
  if (data?.kind === "tasks") {
    return data.value.tasks.map((task) => ({
      identity: `${task.task_id} · r${task.revision}`,
      summary: taskEvidenceSummary(task),
    }));
  }
  if (data?.kind === "runs") {
    return data.value.runs.map((run) => ({
      identity: run.dispatch_id,
      summary: runEvidenceSummary(run),
    }));
  }
  if (data?.kind === "providers") {
    return data.value.providers.map((provider) => ({
      identity: `${provider.provider_id} / ${provider.model_id}`,
      summary: `${provider.health} · ${provider.reason_code ?? "证据完整"} · 配置写入 owner 尚未接入`,
    }));
  }
  return [];
}

export function sseConnectionBannerState(
  state: SseConnectionState,
  hasValidatedSnapshot: boolean,
): "reconnecting" | null {
  return state === "reconnecting" && hasValidatedSnapshot ? "reconnecting" : null;
}

export function taskEvidenceSummary(task: DockyardLiveTaskSummary): string {
  return `${task.state} · attempt ${task.attempt ?? "—"} · 操作以 durable run 证据为准`;
}

export function runEvidenceSummary(
  run: DockyardRunListProjection["runs"][number],
): string {
  if (run.phase === "RECOVERY_REQUIRED") {
    return `${run.task_id} · 需要恢复确认 · ${run.supervisor_state}/${run.worker_state}`;
  }
  if (run.termination_available) {
    return `${run.task_id} · ${run.phase} · 可安全终止`;
  }
  if (run.retry_available) {
    return `${run.task_id} · ${run.phase} · 可确认重试`;
  }
  return `${run.task_id} · ${run.phase} · 运行证据已校验`;
}

function needsOperationalProjection(routeId: DockyardRouteId): boolean {
  return ["requirements", "tasks", "runs", "reviews", "workers", "projects", "diagnostics"].includes(routeId);
}

function renderPage(routeId: Exclude<DockyardRouteId, "workbench">, state: LoadState, data: LiveData | null): ReactElement {
  if (routeId === "requirements") return <RequirementsView />;
  if (routeId === "reviews") return <ReviewsView connected={state === "ready"} />;
  if (routeId === "settings") return <SettingsView />;
  if (state === "loading") return <EvidenceState mark="···" title="正在读取本机证据" copy="页面只接受同一份已校验快照，不会先展示缓存样例。" banner="loading" />;
  if (state === "failed") return <EvidenceState mark="!" title="安全投影暂不可用" copy="没有展示旧数据，也没有根据异常文本推测状态。" banner="failed" />;
  if (data === null) return <EvidenceState mark="—" title="尚未连接本机 Runner" copy="连接建立后，这里会显示只读的 canonical evidence。" banner="not_ready" />;
  if (routeId === "tasks" && data.kind === "tasks") return <TasksView projection={data.value} />;
  if (routeId === "runs" && data.kind === "runs") return <RunsView projection={data.value} />;
  if (routeId === "workers" && data.kind === "providers") return <WorkersView projection={data.value} />;
  if (routeId === "projects" && data.kind === "overview") return <ProjectsView projection={data.value.overview} />;
  if (routeId === "diagnostics" && data.kind === "overview") return <DiagnosticsView projection={data.value.overview} />;
  return <EvidenceState mark="?" title="投影类型不一致" copy="为避免混合不同快照，当前页面已按 fail-closed 隐藏。" banner="failed" />;
}

function EvidenceState({
  mark,
  title,
  copy,
  banner,
}: {
  readonly mark: string;
  readonly title: string;
  readonly copy: string;
  readonly banner: "loading" | "empty" | "not_ready" | "failed";
}) {
  return (
    <section className="empty-panel" aria-live="polite">
      <span className="empty-panel__mark">{mark}</span>
      <h3>{title}</h3>
      <p>{copy}</p>
      <ReadStateBanner state={banner} />
    </section>
  );
}

function RequirementsView() {
  return (
    <section className="content-grid content-grid--balanced">
      <article className="panel requirement-placeholder">
        <header className="panel__header"><div><span className="eyebrow">需求入口</span><h3>描述你希望完成的结果</h3></div><StatusPill state="unknown">本机处理</StatusPill></header>
        <div className="readonly-input">在下方填写需求，先生成可编辑草案，再明确提交审批。</div>
        <p className="muted-copy">草案只存于本机；批准前不会创建 dispatch、lease 或 Worker。</p>
      </article>
      <article className="panel">
        <header className="panel__header"><div><span className="eyebrow">默认策略</span><h3>Standard + balanced</h3></div></header>
        <dl className="detail-list"><div><dt>执行模式</dt><dd>Standard</dd></div><div><dt>审议深度</dt><dd>balanced</dd></div><div><dt>计划批准</dt><dd>必须明确确认</dd></div><div><dt>Automated</dt><dd><StatusPill state="unknown">未就绪</StatusPill></dd></div></dl>
      </article>
    </section>
  );
}

function TasksView({ projection }: { readonly projection: DockyardTaskListProjection }) {
  if (projection.tasks.length === 0) {
    return <EvidenceState mark="0" title="还没有 canonical task" copy="提交并批准计划后，真实任务会从同一 Git 快照出现在这里。" banner="empty" />;
  }
  const selected = projection.tasks[0];
  return (
    <section className="content-grid content-grid--balanced">
      <article className="panel">
        <header className="panel__header"><div><span className="eyebrow">Lifecycle</span><h3>任务列表</h3></div><span className="muted">{projection.tasks.length} 项</span></header>
        <div className="record-list">{projection.tasks.map((task) => <TaskRow task={task} key={task.task_id} />)}</div>
      </article>
      <article className="panel">
        <header className="panel__header"><div><span className="eyebrow">任务详情</span><h3>{selected.task_id}</h3></div></header>
        <dl className="detail-list"><div><dt>Revision / Attempt</dt><dd>r{selected.revision} / {selected.attempt ?? "尚未派发"}</dd></div><div><dt>角色</dt><dd>{selected.role_id ?? "尚未绑定"}</dd></div><div><dt>Provider</dt><dd>{selected.provider_id ?? "尚未选择"}</dd></div><div><dt>Model</dt><dd>{selected.model_id ?? "尚未选择"}</dd></div><div><dt>状态</dt><dd>{selected.state}</dd></div><div><dt>最近更新</dt><dd>{selected.updated_at}</dd></div></dl>
      </article>
    </section>
  );
}

function TaskRow({ task }: { readonly task: DockyardLiveTaskSummary }) {
  return <div className="record-card"><div><strong>{task.task_id}</strong><small>r{task.revision} · attempt {task.attempt ?? "—"} · {task.provider_id ?? "未选择 Provider"}</small></div><StatusPill state={task.state === "integrated" ? "success" : task.state === "blocked" ? "blocked" : task.state === "ready" ? "warning" : "selected"}>{task.state}</StatusPill></div>;
}

function RunsView({ projection }: { readonly projection: DockyardRunListProjection }) {
  if (projection.runs.length === 0) {
    return <EvidenceState mark="∅" title="当前没有 durable run evidence" copy="Dockyard 不会用 canonical task 状态猜测 Worker、心跳或 lease 是否仍然存活。" banner="empty" />;
  }
  return (
    <section className="panel">
      <header className="panel__header"><div><span className="eyebrow">运行阶段</span><h3>调度与恢复</h3></div><span className="muted">{projection.runs.length} 条</span></header>
      <div className="timeline-list">{projection.runs.map((run) => <article className="timeline-row" key={run.dispatch_id}><span className={`timeline-dot timeline-dot--${run.phase === "RECOVERY_REQUIRED" ? "unknown" : "active"}`} /><div><strong>{run.task_id}</strong><small>attempt {run.attempt} · {run.dispatch_id}</small></div><div><StatusPill state={run.phase === "RECOVERY_REQUIRED" ? "unknown" : "selected"}>{run.phase}</StatusPill><small>{run.heartbeat_state} · {run.updated_at}</small></div></article>)}</div>
    </section>
  );
}

function ReviewsView({ connected }: { readonly connected: boolean }) {
  return (
    <section className="content-grid content-grid--balanced">
      <article className="panel"><header className="panel__header"><div><span className="eyebrow">MAD REVIEW</span><h3>待验收交付</h3></div><StatusPill state="unknown">等待证据</StatusPill></header><p className="muted-copy">连接 Runner 后，这里只展示已经持久化的审议结果。验收不会重跑 Worker 或 MAD。</p></article>
      <article className="panel"><header className="panel__header"><div><span className="eyebrow">INTEGRATION</span><h3>集成边界</h3></div></header><dl className="detail-list"><div><dt>验收</dt><dd>必须显式确认</dd></div><div><dt>Git</dt><dd>old-value CAS · tree merge</dd></div><div><dt>凭据</dt><dd>仅保留在本机 Runner</dd></div></dl></article>
    </section>
  );
}

function WorkersView({ projection }: { readonly projection: DockyardProviderListProjection }) {
  if (projection.providers.length === 0) {
    return <EvidenceState mark="◇" title="尚无 Provider 健康证据" copy="已配置不等于 READY；版本、Decoder 与绑定证据完整后才会显示为可用。" banner="not_ready" />;
  }
  return <section className="provider-grid">{projection.providers.map((provider) => <article className="provider-card" key={provider.provider_id}><span className="provider-card__mark">{provider.provider_id.slice(0, 1).toUpperCase()}</span><div><span className="eyebrow">{provider.provider_id}</span><h3>{provider.model_id}</h3><p>{provider.reason_code ?? "证据完整"}</p></div><StatusPill state={provider.health === "READY" ? "success" : "unknown"}>{provider.health === "READY" ? "就绪" : "未就绪"}</StatusPill><dl className="detail-list"><div><dt>Executable</dt><dd>{provider.executable_version ?? "未验证"}</dd></div><div><dt>Decoder</dt><dd>{provider.decoder_version ?? "未验证"}</dd></div><div><dt>凭据</dt><dd>仅本机 Provider 管理</dd></div></dl></article>)}</section>;
}

function ProjectsView({ projection }: { readonly projection: DockyardLiveOverviewProjection }) {
  const taskCount = projection.state_counts.reduce((total, item) => total + item.count, 0);
  return (
    <section className="content-grid content-grid--balanced">
      <article className="panel"><header className="panel__header"><div><span className="eyebrow">项目健康</span><h3>{projection.project_id}</h3></div><StatusPill state={projection.health.snapshot_state === "VERIFIED" ? "success" : "unknown"}>{projection.health.snapshot_state === "VERIFIED" ? "快照已校验" : "未知"}</StatusPill></header><dl className="detail-list"><div><dt>Snapshot</dt><dd><code>{projection.snapshot_commit.slice(0, 12)}</code></dd></div><div><dt>采用等级</dt><dd>{projection.adoption_level}</dd></div><div><dt>PM 模式</dt><dd>{projection.pm_mode}</dd></div><div><dt>任务</dt><dd>{taskCount} 个 canonical summary</dd></div><div><dt>删除策略</dt><dd>只做软删除，不会删除 Git 仓库</dd></div></dl></article>
      <article className="panel"><header className="panel__header"><div><span className="eyebrow">证据时间</span><h3>同一快照</h3></div></header><ReadStateBanner state="fresh" /><p className="muted-copy">生成于 {projection.generated_at}，canonical 更新于 {projection.updated_at}。</p></article>
    </section>
  );
}

function DiagnosticsView({ projection }: { readonly projection: DockyardLiveOverviewProjection }) {
  const health = projection.health;
  return (
    <section className="content-grid content-grid--balanced">
      <article className="panel"><header className="panel__header"><div><span className="eyebrow">Doctor</span><h3>本机证据健康</h3></div><StatusPill state={health.issue_codes.length === 0 ? "success" : "warning"}>{health.issue_codes.length === 0 ? "无已知问题" : `${health.issue_codes.length} 项`}</StatusPill></header><dl className="detail-list"><div><dt>Snapshot</dt><dd>{health.snapshot_state}</dd></div><div><dt>PM</dt><dd>{health.pm_state}</dd></div><div><dt>Scheduler</dt><dd>{health.scheduler_state}</dd></div><div><dt>Runner</dt><dd>{health.runner_state}</dd></div><div><dt>Provider</dt><dd>{health.provider_state}</dd></div><div><dt>检查时间</dt><dd>{health.checked_at}</dd></div></dl></article>
      <article className="panel"><header className="panel__header"><div><span className="eyebrow">SAFE CODES</span><h3>诊断摘要</h3></div></header>{health.issue_codes.length === 0 ? <ReadStateBanner state="fresh" /> : <div className="record-list">{health.issue_codes.map((code) => <div className="record-card" key={code}><strong>{code}</strong><StatusPill state="warning">需核对</StatusPill></div>)}</div>}<p className="muted-copy">不显示异常文本、stdout/stderr、环境变量或本机绝对路径。</p></article>
    </section>
  );
}

function SettingsView() {
  return <section className="content-grid content-grid--balanced"><article className="panel"><header className="panel__header"><div><span className="eyebrow">界面</span><h3>显示设置</h3></div></header><dl className="detail-list"><div><dt>主题</dt><dd>Harbor Cyan 深色</dd></div><div><dt>语言</dt><dd>简体中文</dd></div><div><dt>动画</dt><dd>减少非证据动效</dd></div></dl></article><article className="panel"><header className="panel__header"><div><span className="eyebrow">安全</span><h3>本机边界</h3></div></header><dl className="detail-list"><div><dt>用户</dt><dd>本地单用户 · 无登录</dd></div><div><dt>Control API</dt><dd>loopback-only</dd></div><div><dt>云端</dt><dd><StatusPill state="unknown">未配置</StatusPill></dd></div></dl></article></section>;
}
