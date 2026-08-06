import type { DeliveryView, DiagnosticView, ReviewView, RunView } from "../types/views";

export const runFixtures: readonly RunView[] = Object.freeze([
  Object.freeze({
    run_id: "RUN-029-H-1",
    task_id: "TC-029-H",
    title: "Dockyard Web 基础层",
    phase: "RUNNING",
    provider: "codex",
    model: "gpt-5.6-sol",
    attempt: 1,
    heartbeat: "HEALTHY",
    updated_at: "16 秒前",
  }),
  Object.freeze({
    run_id: "RUN-028-A-1",
    task_id: "TC-028-A",
    title: "Reasonix Basic 合约",
    phase: "RECOVERY_REQUIRED",
    provider: "reasonix",
    model: "deepseek-v4-flash",
    attempt: 1,
    heartbeat: "UNKNOWN",
    updated_at: "等待真实 API 证据",
  }),
]);

export const reviewFixtures: readonly ReviewView[] = Object.freeze([
  Object.freeze({
    review_id: "REV-029-G1",
    task_id: "TC-029-G1",
    verdict: "PASS",
    depth: "balanced",
    implementation_commit: "a97e7d8",
    report_commit: "local-evidence",
    summary: "本地 Provider、Decoder 与 Workflow 证据通过；真实 API 仍为 Target。",
  }),
  Object.freeze({
    review_id: "REV-029-G2",
    task_id: "TC-029-G2",
    verdict: "BLOCKED",
    depth: "balanced",
    implementation_commit: null,
    report_commit: null,
    summary: "缺少稳定 Codex JSONL schema，按证据门 fail-closed。",
  }),
]);

export const deliveryFixtures: readonly DeliveryView[] = Object.freeze([
  Object.freeze({
    delivery_id: "DEL-029-H",
    task_id: "TC-029-H",
    title: "Dockyard Web console 骨架",
    outcome: "INTEGRATED",
    implementation_commit: "8a61fa8",
    report_commit: "8a61fa8",
    occurred_at: "刚刚",
  }),
  Object.freeze({
    delivery_id: "DEL-029-E",
    task_id: "TC-029-E",
    title: "设备配对核心",
    outcome: "REVIEW_READY",
    implementation_commit: "c0ee764",
    report_commit: "c0ee764",
    occurred_at: "较早",
  }),
]);

export const diagnosticFixtures: readonly DiagnosticView[] = Object.freeze([
  Object.freeze({ check_id: "D001", label: "本机 Runner", outcome: "PASS", safe_message: "loopback Runner 可达", correlation_digest: "sha256:runner-safe" }),
  Object.freeze({ check_id: "D004", label: "Canonical Snapshot", outcome: "PASS", safe_message: "任务快照与 Git HEAD 一致", correlation_digest: "sha256:snapshot-safe" }),
  Object.freeze({ check_id: "D009", label: "Provider Executables", outcome: "WARN", safe_message: "部分 Provider 缺少完整证据", correlation_digest: "sha256:provider-safe" }),
  Object.freeze({ check_id: "D010", label: "Decoder Eligibility", outcome: "UNKNOWN", safe_message: "Codex decoder 尚未取得稳定 schema", correlation_digest: "sha256:decoder-safe" }),
]);
