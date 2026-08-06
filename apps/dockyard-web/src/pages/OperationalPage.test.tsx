import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { DockyardApp } from "../App";
import { readStateLabels } from "../components/ReadStateBanner";
import {
  liveCommandEvidence,
  runEvidenceSummary,
  sseConnectionBannerState,
} from "./OperationalPage";
import type { DockyardProviderListProjection, DockyardRunListProjection, DockyardTaskListProjection } from "../types/api";

describe("Dockyard read-only operational pages", () => {
  it("keeps the workbench honest before the Runner is connected", () => {
    const html = renderToStaticMarkup(<DockyardApp initialPath="/" />);
    expect(html).toContain("等待本机 Runner");
    expect(html).toContain("不会展示演示项目、样例任务或虚构的运行状态");
    expect(html).not.toContain("Dockyard Web 基础层");
  });

  it("does not render sample tasks before a live client is connected", () => {
    const html = renderToStaticMarkup(<DockyardApp initialPath="/tasks" />);
    expect(html).toContain("尚未连接本机 Runner");
    expect(html).not.toContain("Dockyard Web 基础层");
    expect(html).not.toContain("TC-001");
    expect(html).toContain("Runner 未连接");
    expect(html).not.toContain("当前页面已连接真实证据");
  });

  it("does not invent run phases before durable evidence is loaded", () => {
    const html = renderToStaticMarkup(<DockyardApp initialPath="/runs" />);
    expect(html).toContain("尚未连接本机 Runner");
    expect(html).not.toContain("RUNNING");
    expect(html).not.toContain("RECOVERY_REQUIRED");
  });

  it("does not render sample provider health or an unavailable binding command", () => {
    const html = renderToStaticMarkup(<DockyardApp initialPath="/workers" />);
    expect(html).toContain("尚未连接本机 Runner");
    expect(html).not.toContain("保存模型配置");
    expect(html).not.toContain("派发 Worker");
  });

  it("does not claim live review evidence before the Runner is connected", () => {
    const html = renderToStaticMarkup(<DockyardApp initialPath="/reviews" />);
    expect(html).toContain("等待证据");
    expect(html).not.toContain("实时证据");
    expect(html).toContain("尚未建立本机 Control API 会话");
  });

  it("describes only live task, run and provider command targets", () => {
    const snapshot = "a".repeat(40);
    const tasks: DockyardTaskListProjection = {
      schema_version: "dockyard.task-list/v1", project_id: "PRJ-LIVE", snapshot_commit: snapshot,
      tasks: [{ task_id: "TC-LIVE", revision: 7, state: "blocked", attempt: 2, role_id: "R1", provider_id: "codex", model_id: "gpt", deliberation_tier: "balanced", delivery_state: null, integration_state: null, updated_at: "2026-08-04T00:00:00Z", blocked_kind: "delivery", has_report: false, content_digest: "b".repeat(64) }],
      reviews: [], content_digest: "c".repeat(64),
    };
    const runs: DockyardRunListProjection = {
      schema_version: "dockyard.run-list/v1", project_id: "PRJ-LIVE", snapshot_commit: snapshot,
      runs: [{ task_id: "TC-LIVE", revision: 7, attempt: 2, dispatch_id: "DSP-LIVE", phase: "ACKNOWLEDGED", supervisor_state: "UNKNOWN", worker_state: "UNKNOWN", heartbeat_state: "UNKNOWN", lease_state: "UNKNOWN", started_at: null, updated_at: "2026-08-04T00:00:00Z", retry_count: 1, generation_id: "GEN-LIVE", termination_available: false, termination_event_id: null, retry_available: false, retry_event_id: null, retry_failure_kind: null, retry_provider_id: null, retry_model_id: null, content_digest: "d".repeat(64) }],
      content_digest: "e".repeat(64),
    };
    const providers: DockyardProviderListProjection = {
      schema_version: "dockyard.provider-list/v1", project_id: "PRJ-LIVE", snapshot_commit: snapshot,
      providers: [{ provider_id: "reasonix", model_id: "deepseek-v4-flash", health: "UNKNOWN", reason_code: "EVIDENCE_UNKNOWN", executable_version: "1.19.1", decoder_version: null, binding_id: "basic", required_capabilities: [], checked_at: "2026-08-04T00:00:00Z", content_digest: "f".repeat(64) }],
      content_digest: "1".repeat(64),
    };
    expect(liveCommandEvidence({ kind: "tasks", value: tasks })[0]).toEqual({
      identity: "TC-LIVE · r7", summary: "blocked · attempt 2 · 操作以 durable run 证据为准",
    });
    expect(liveCommandEvidence({ kind: "runs", value: runs })[0]?.identity).toBe("DSP-LIVE");
    expect(liveCommandEvidence({ kind: "providers", value: providers })[0]?.identity).toBe("reasonix / deepseek-v4-flash");
    expect(JSON.stringify([
      liveCommandEvidence({ kind: "tasks", value: tasks }),
      liveCommandEvidence({ kind: "runs", value: runs }),
      liveCommandEvidence({ kind: "providers", value: providers }),
    ])).not.toContain("TC-001");
  });

  it("describes recovery and SSE reconnect without inventing progress", () => {
    const run: DockyardRunListProjection["runs"][number] = {
      task_id: "TC-LIVE", revision: 7, attempt: 2, dispatch_id: "DSP-LIVE",
      phase: "RECOVERY_REQUIRED", supervisor_state: "DEAD", worker_state: "DEAD",
      heartbeat_state: "UNKNOWN", lease_state: "UNKNOWN", started_at: null,
      updated_at: "2026-08-04T00:00:00Z", retry_count: 1,
      generation_id: "GEN-LIVE", termination_available: false,
      termination_event_id: null, retry_available: false, retry_event_id: null,
      retry_failure_kind: null, retry_provider_id: null, retry_model_id: null,
      content_digest: "d".repeat(64),
    };
    expect(runEvidenceSummary(run)).toBe("TC-LIVE · 需要恢复确认 · DEAD/DEAD");
    expect(sseConnectionBannerState("reconnecting", true)).toBe("reconnecting");
    expect(sseConnectionBannerState("reconnecting", false)).toBeNull();
    expect(sseConnectionBannerState("connected", true)).toBeNull();
  });

  it("defines Chinese UI text for every frozen read state", () => {
    expect(Object.keys(readStateLabels)).toHaveLength(10);
    for (const [title, message] of Object.values(readStateLabels)) {
      expect(title.length).toBeGreaterThan(0);
      expect(message.length).toBeGreaterThan(0);
    }
    expect(readStateLabels.offline[0]).toBe("本机 Runner 离线");
    expect(readStateLabels.failed[0]).toBe("读取失败");
  });

  it("never renders forbidden evidence classes", () => {
    const html = ["/", "/tasks", "/runs", "/reviews", "/workers", "/projects", "/diagnostics"]
      .map((path) => renderToStaticMarkup(<DockyardApp initialPath={path} />))
      .join("\n");
    expect(html).not.toMatch(/sk-[A-Za-z0-9_-]{12,}/);
    expect(html).not.toMatch(/[A-Za-z]:[\\/]/);
    expect(html).not.toContain("source_thread_id");
    expect(html).not.toContain("raw prompt");
  });
});
