"""TC-13.19c — Deterministic Escalation E2E: Blocked Audit → Escalated Redispatch.

Single comprehensive test that validates the complete blocked-audit escalation
closed loop:

    ready → TASK_DISPATCHED (Basic, attempt 1) → DISPATCH_ACKNOWLEDGED (attempt 1)
    → DELIVERY_SUBMITTED (attempt 1) → MAD audit blocked
    → TASK_BLOCKED → Basic→Standard escalation
    → BLOCKER_RESOLVED → TASK_DISPATCHED (Standard, attempt 2)
    → DISPATCH_ACKNOWLEDGED (attempt 2) → DELIVERY_SUBMITTED (attempt 2)
    → MAD audit pass → DELIVERY_ACCEPTED (attempt 2)
    → CHANGE_INTEGRATED → integrated

Uses real production modules.  Only mocks ``asyncio.create_subprocess_exec``
(single router for both Claude and MAD subprocess boundaries) and the clock.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Mapping
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any
from unittest import mock

_SCRIPTS_DIR = str(
    Path(__file__).resolve().parents[1] / "skills" / "agentdesk" / "scripts"
)
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from control_plane_transition import (
    AcknowledgePayload,
    BlockedPayload,
    BlockerResolvedPayload,
    ControlPlaneTransitionService,
    DeliveryAcceptedPayload,
    DeliverySubmittedPayload,
    DispatchCAS,
    DispatchPayload,
    IntegrationPayload,
    TransitionCAS,
    TransitionEventContext,
    TransitionRequest,
    TransitionResult,
)
from core_types import MadDeliberationDepth, TaskDifficulty, WorkerKind
from dispatcher_gateway import (
    AgentCliProvider,
    DispatchIdentity,
    DispatchRequest,
)
from worker_adapter import run_dispatch_observed
from worker_output_decoder import (
    WorkerCompletionStatus,
)
from claude_code_provider import ClaudeCodeProvider
from approval_gate import (
    ApprovalScope,
    ApprovalSubject,
    write_grant,
)
from mad_gateway import MadGatewayConfig
from mad_audit_gateway import (
    MadAuditGatewayInput,
    MadAuditGatewayResult,
)
from state_provider import StateProvider, StateSnapshot
from workflow_orchestrator import (
    AcceptanceCycleRequest,
    AcceptanceCycleResult,
    BlockedAuditRequest,
    BlockedAuditResult,
    DispatchCycleRequest,
    DispatchCycleResult,
    EscalatedRedispatchRequest,
    EscalatedRedispatchResult,
    WorkflowClock,
    WorkflowOrchestrator,
)

# ── Source repo path ────────────────────────────────────────────────────────

_SOURCE_REPO = Path(__file__).resolve().parents[1]


# ── git helpers ─────────────────────────────────────────────────────────────


def _git_head(project_root: Path) -> str:
    result = subprocess.run(
        ["git", "-C", str(project_root), "rev-parse", "HEAD"],
        capture_output=True, text=True, timeout=10,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git rev-parse HEAD failed: {result.stderr}")
    return result.stdout.strip()


def _git_status_bytes(project_root: Path) -> bytes:
    result = subprocess.run(
        ["git", "-C", str(project_root), "status", "--porcelain"],
        capture_output=True, timeout=10,
    )
    return result.stdout


def _git_init(project_root: Path) -> None:
    subprocess.run(
        ["git", "init", "-b", "main"],
        cwd=str(project_root), capture_output=True, timeout=10,
    )
    subprocess.run(
        ["git", "config", "user.email", "test@e2e.test"],
        cwd=str(project_root), capture_output=True, timeout=10,
    )
    subprocess.run(
        ["git", "config", "user.name", "E2E Test"],
        cwd=str(project_root), capture_output=True, timeout=10,
    )


def _git_add_all_and_commit(project_root: Path, message: str) -> str:
    subprocess.run(
        ["git", "add", "-A"],
        cwd=str(project_root), capture_output=True, timeout=10,
    )
    subprocess.run(
        ["git", "commit", "--allow-empty", "-m", message],
        cwd=str(project_root), capture_output=True, timeout=10,
    )
    return _git_head(project_root)


# ── FakeClock ────────────────────────────────────────────────────────────────


class FakeClock:
    """Deterministic fake clock for testing — UTC, monotonic ticking."""

    def __init__(self, start: datetime | None = None, tick: float = 1.0) -> None:
        self._now = start if start is not None else _utc("2026-07-29T12:00:00")
        self._mono: float = 0.0
        self._tick = tick

    def now(self) -> datetime:
        result = self._now
        self._now = self._now + timedelta(seconds=self._tick)
        return result

    def monotonic(self) -> float:
        result = self._mono
        self._mono += self._tick
        return result

    async def sleep(self, seconds: float) -> None:
        self._now = self._now + timedelta(seconds=seconds)
        self._mono += seconds
        await asyncio.sleep(0)


def _utc(iso: str) -> datetime:
    return datetime.fromisoformat(iso).replace(tzinfo=timezone.utc)


# ── FakeProcess ──────────────────────────────────────────────────────────────


class FakeProcess:
    """Minimal fake asyncio subprocess for a successful exit-0 run."""

    def __init__(self, pid: int, returncode: int, stdout: bytes) -> None:
        self.pid = pid
        self.returncode = returncode
        self._stdout = stdout

    async def communicate(self, input: bytes | None = None) -> tuple[bytes, bytes]:
        return self._stdout, b""

    async def wait(self) -> int:
        return self.returncode


# ── Claude worker stdout builder ─────────────────────────────────────────────


def _build_claude_stdout(
    task_id: str,
    revision: int,
    attempt: int,
    dispatch_id: str,
    implementation_commit: str,
    report_commit: str,
    summary: str = "Implementation completed successfully.",
) -> bytes:
    """Build a Claude 2.1.214 wrapper stdout with a valid Worker envelope inside."""
    envelope = {
        "schema_version": "agentdesk.worker-output/v1",
        "task_id": task_id,
        "revision": revision,
        "attempt": attempt,
        "dispatch_id": dispatch_id,
        "status": "completed",
        "implementation_commit": implementation_commit,
        "report_commit": report_commit,
        "summary": summary,
        "warnings": [],
    }
    inner_json = json.dumps(envelope)
    wrapper = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "api_error_status": None,
        "duration_ms": 5000,
        "duration_api_ms": 5773,
        "ttft_ms": 4952,
        "ttft_stream_ms": 553,
        "time_to_request_ms": 270,
        "num_turns": 1,
        "result": inner_json,
        "stop_reason": "end_turn",
        "session_id": "test-session",
        "total_cost_usd": None,
        "usage": {
            "input_tokens": 100,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
            "output_tokens": 10,
            "server_tool_use": {"web_search_requests": 0, "web_fetch_requests": 0},
            "service_tier": "standard",
            "cache_creation": {"ephemeral_1h_input_tokens": 0, "ephemeral_5m_input_tokens": 0},
            "inference_geo": "",
            "iterations": [],
            "speed": "standard",
        },
        "modelUsage": {},
        "permission_denials": [],
        "terminal_reason": "completed",
        "fast_mode_state": "off",
        "uuid": "test-uuid",
    }
    return json.dumps(wrapper).encode("utf-8")


# ── MAD audit stdout builders ────────────────────────────────────────────────


def _build_mad_audit_blocked_stdout(archive_path: str, depth_value: str = "balanced") -> bytes:
    """Build a valid ``mad.audit-result/v1`` JSON with verdict=blocked."""
    result = {
        "schema_version": "mad.audit-result/v1",
        "deliberation_id": "DELIB-e2e-blocked-001",
        "status": "completed",
        "verdict": "blocked",
        "issues": [
            {
                "id": "BLOCK-001",
                "severity": "critical",
                "category": "process",
                "title": "Missing upstream library dependency",
                "description": "The implementation requires a library not available in the Basic worker environment.",
                "recommendation": "Upgrade to Standard or Advanced worker with full dependency support.",
                "location": {
                    "file": "src/impl.py",
                    "line": 1,
                    "commit": "0" * 40,
                },
            },
        ],
        "evidence": [],
        "warnings": ["Worker tier insufficient for complex dependency resolution"],
        "report": "Delivery blocked: worker tier cannot resolve required dependencies.",
        "archive_path": archive_path,
        "participants": ["auditor-1"],
        "plan": {"depth": depth_value},
    }
    return json.dumps(result).encode("utf-8")


def _build_mad_audit_pass_stdout(archive_path: str, depth_value: str = "balanced") -> bytes:
    """Build a valid ``mad.audit-result/v1`` JSON with verdict=pass."""
    result = {
        "schema_version": "mad.audit-result/v1",
        "deliberation_id": "DELIB-e2e-pass-001",
        "status": "completed",
        "verdict": "pass",
        "issues": [],
        "evidence": [],
        "warnings": [],
        "report": "All checks passed. No issues found.",
        "archive_path": archive_path,
        "participants": ["auditor-1"],
        "plan": {"depth": depth_value},
    }
    return json.dumps(result).encode("utf-8")


# ── canonical directory initialisation ───────────────────────────────────────


def _init_canonical_dirs(project_root: Path) -> None:
    for d in ("docs/pm/events", "docs/pm/outbox", "docs/pm/acceptances",
              "docs/pm/approvals"):
        (project_root / d.replace("/", os.sep)).mkdir(parents=True, exist_ok=True)
    runtime = project_root / ".agentdesk" / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)


def _write_initial_tasks_yaml(
    project_root: Path,
    task_id: str = "TC-001",
    state: str = "ready",
    revision: int = 1,
    attempt: int | None = None,
    dispatch_id: str | None = None,
    task_card_path: str = "tasks/TC-001/task.md",
    task_card_commit: str = "0" * 40,
    base_commit: str = "0" * 40,
) -> None:
    """Write a minimal tasks.yaml with one task."""
    state_dir = project_root / "docs" / "pm" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)

    current_dispatch = None
    if state in ("dispatched", "in_progress", "review_ready") and dispatch_id is not None:
        current_dispatch = {
            "dispatch_id": dispatch_id,
            "attempt_id": f"attempt-{attempt}",
            "role_id": "agent",
            "base_commit": base_commit,
            "branch": "feat/e2e-escalation",
            "dispatched_at": "2026-07-29T12:00:00Z",
            "model_selection": {
                "required_model_tier": "basic",
                "required_model_capabilities": [],
                "model_binding_id": "binding-001",
                "selected_model_provider": "claude",
                "selected_model_id": "claude-haiku-4-5",
                "selected_model_tier": "basic",
                "selected_deliberation_tier": "fast",
                "selected_context_window_tokens": 200000,
                "selected_model_capabilities": [],
                "model_degradation_approval_id": None,
            },
        }

    tasks_doc = {
        "schema_version": "agentdesk.tasks/v2",
        "project_id": "e2e-project",
        "adoption_level": "standard",
        "updated_at": "2026-07-29T12:00:00Z",
        "pm_control": {
            "holder_id": "pm-e2e",
            "lease_epoch": 1,
            "mode": "manual",
        },
        "tasks": [
            {
                "task_id": task_id,
                "revision": revision,
                "task_card_path": task_card_path,
                "task_card_commit": task_card_commit,
                "state": state,
                "attempt": attempt if state in ("dispatched", "in_progress", "review_ready") else None,
                "current_dispatch": current_dispatch,
                "report_path": "reports/report.md",
                "granted_approval_ids": None,
                "delivery_state": None,
                "integration_state": None,
                "implementation_commit": None,
                "report_commit": None,
                "accepted_commit": None,
                "acceptance_path": None,
                "integrated_commit": None,
                "blocked_reason": None,
                "blocked_kind": None,
                "blocked_owner": None,
                "unblock_condition": None,
                "review_after": None,
                "blocked_attempt_valid": None,
                "resume_state": None,
                "timestamps": {
                    "created_at": "2026-07-29T11:00:00Z",
                    "ready_at": "2026-07-29T12:00:00Z",
                    "dispatched_at": None,
                    "started_at": None,
                    "delivered_at": None,
                    "blocked_at": None,
                    "accepted_at": None,
                    "integrated_at": None,
                    "updated_at": "2026-07-29T12:00:00Z",
                },
            },
        ],
    }
    path = state_dir / "tasks.yaml"
    path.write_text(json.dumps(tasks_doc, indent=2) + "\n", encoding="utf-8")


def _init_worker_slot_store(project_root: Path) -> None:
    runtime = project_root / ".agentdesk" / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    store = {
        "schema_version": "agentdesk.worker-slot-lease/v1",
        "updated_at": "1970-01-01T00:00:00Z",
        "slot_epochs": {
            "basic_agent-1": 0, "basic_agent-2": 0,
            "standard_agent-1": 0, "standard_agent-2": 0,
            "advanced_agent-1": 0, "advanced_agent-2": 0,
            "expert_agent-1": 0, "expert_agent-2": 0,
        },
        "leases": {},
    }
    path = runtime / "worker-slot-lease.yaml"
    path.write_text(json.dumps(store, indent=2) + "\n", encoding="utf-8")


# ── Approval grant helpers ───────────────────────────────────────────────────


def _write_dispatch_grant(
    project_root: Path,
    task_id: str,
    revision: int,
    attempt: int,
    dispatch_id: str,
    head_sha: str,
    now: datetime,
    approval_id: str,
    event_id: str,
) -> None:
    write_grant(
        project_root=project_root,
        approval_id=approval_id,
        event_id=event_id,
        scope=ApprovalScope.DISPATCH,
        subject=ApprovalSubject(
            task_id=task_id,
            revision=revision,
            attempt=attempt,
            dispatch_id=dispatch_id,
            accepted_commit=None,
        ),
        lease_epoch=1,
        now=now,
        reason="E2E escalation dispatch approval",
        expires_at=None,
        expected_snapshot_commit=head_sha,
    )


def _write_accept_grant(
    project_root: Path,
    task_id: str,
    revision: int,
    attempt: int,
    dispatch_id: str,
    head_sha: str,
    now: datetime,
    approval_id: str,
    event_id: str,
) -> None:
    write_grant(
        project_root=project_root,
        approval_id=approval_id,
        event_id=event_id,
        scope=ApprovalScope.ACCEPT,
        subject=ApprovalSubject(
            task_id=task_id,
            revision=revision,
            attempt=attempt,
            dispatch_id=dispatch_id,
            accepted_commit=None,
        ),
        lease_epoch=1,
        now=now,
        reason="E2E escalation accept approval",
        expires_at=None,
        expected_snapshot_commit=head_sha,
    )


def _write_integrate_grant(
    project_root: Path,
    task_id: str,
    revision: int,
    attempt: int,
    dispatch_id: str,
    accepted_commit: str,
    head_sha: str,
    now: datetime,
    approval_id: str,
    event_id: str,
) -> None:
    write_grant(
        project_root=project_root,
        approval_id=approval_id,
        event_id=event_id,
        scope=ApprovalScope.INTEGRATE,
        subject=ApprovalSubject(
            task_id=task_id,
            revision=revision,
            attempt=attempt,
            dispatch_id=dispatch_id,
            accepted_commit=accepted_commit,
        ),
        lease_epoch=1,
        now=now,
        reason="E2E escalation integrate approval",
        expires_at=None,
        expected_snapshot_commit=head_sha,
    )


# ── Transition request builders ──────────────────────────────────────────────


def _make_model_selection_basic() -> Any:
    from dispatcher_gateway import ModelSelectionSnapshot as DPModelSelection
    return DPModelSelection(
        required_model_tier="basic",
        required_model_capabilities=(),
        model_binding_id="binding-001",
        selected_model_provider="claude",
        selected_model_id="claude-haiku-4-5",
        selected_model_tier="basic",
        selected_deliberation_tier="efficient",
        selected_context_window_tokens=200000,
        selected_model_capabilities=(),
        model_degradation_approval_id=None,
    )


def _make_model_selection_standard() -> Any:
    from dispatcher_gateway import ModelSelectionSnapshot as DPModelSelection
    return DPModelSelection(
        required_model_tier="standard",
        required_model_capabilities=(),
        model_binding_id="binding-002",
        selected_model_provider="claude",
        selected_model_id="claude-sonnet-4-5",
        selected_model_tier="standard",
        selected_deliberation_tier="balanced",
        selected_context_window_tokens=200000,
        selected_model_capabilities=(),
        model_degradation_approval_id=None,
    )


def _make_dispatch_transition_request(
    task_id: str,
    dispatch_id: str,
    revision: int,
    model_selection: Any,
    task_card_path: str,
    task_card_commit: str,
    base_commit: str,
    branch: str,
    report_path: str,
    outbox_message_id: str,
    attempt: int,
    head_sha: str,
    event_id: str,
) -> TransitionRequest:
    cas = TransitionCAS(
        task_id=task_id,
        expected_revision=revision,
        expected_state="ready",
        expected_snapshot_commit=head_sha,
    )
    payload = DispatchPayload(
        dispatch_id=dispatch_id,
        role_id="agent",
        model_selection=model_selection,
        task_card_path=task_card_path,
        task_card_commit=task_card_commit,
        base_commit=base_commit,
        branch=branch,
        report_path=report_path,
        outbox_message_id=outbox_message_id,
        new_attempt=attempt,
    )
    event_context = TransitionEventContext(
        source_message_id=None,
        evidence_refs=(),
        guard_results=(),
    )
    return TransitionRequest(
        cas=cas,
        dispatch_cas=None,
        event_id=event_id,
        event_type="TASK_DISPATCHED",
        payload=payload,
        event_context=event_context,
    )


def _make_ack_transition_request(
    task_id: str,
    dispatch_id: str,
    revision: int,
    attempt: int,
    head_sha: str,
    dispatch_event_id: str,
) -> TransitionRequest:
    cas = TransitionCAS(
        task_id=task_id,
        expected_revision=revision,
        expected_state="dispatched",
        expected_snapshot_commit=head_sha,
    )
    dispatch_cas = DispatchCAS(
        expected_dispatch_id=dispatch_id,
        expected_attempt=attempt,
    )
    payload = AcknowledgePayload()
    event_context = TransitionEventContext(
        source_message_id=None,
        evidence_refs=(),
        guard_results=(),
    )
    return TransitionRequest(
        cas=cas,
        dispatch_cas=dispatch_cas,
        event_id=dispatch_event_id + "-ACK",
        event_type="DISPATCH_ACKNOWLEDGED",
        payload=payload,
        event_context=event_context,
    )


def _make_acceptance_transition_request(
    task_id: str,
    dispatch_id: str,
    revision: int,
    attempt: int,
    accepted_commit: str,
    acceptance_path: str,
    head_sha: str,
    event_id: str,
) -> TransitionRequest:
    cas = TransitionCAS(
        task_id=task_id,
        expected_revision=revision,
        expected_state="review_ready",
        expected_snapshot_commit=head_sha,
    )
    dispatch_cas = DispatchCAS(
        expected_dispatch_id=dispatch_id,
        expected_attempt=attempt,
    )
    payload = DeliveryAcceptedPayload(
        accepted_commit=accepted_commit,
        acceptance_path=acceptance_path,
        residual_risks=(),
        criteria_evidence=("E2E escalation acceptance criteria satisfied",),
        rationale="E2E test acceptance",
    )
    event_context = TransitionEventContext(
        source_message_id=None,
        evidence_refs=(),
        guard_results=(),
    )
    return TransitionRequest(
        cas=cas,
        dispatch_cas=dispatch_cas,
        event_id=event_id,
        event_type="DELIVERY_ACCEPTED",
        payload=payload,
        event_context=event_context,
    )


def _make_integration_transition_request(
    task_id: str,
    revision: int,
    integrated_commit: str,
    head_sha: str,
    event_id: str,
) -> TransitionRequest:
    cas = TransitionCAS(
        task_id=task_id,
        expected_revision=revision,
        expected_state="accepted",
        expected_snapshot_commit=head_sha,
    )
    payload = IntegrationPayload(
        integrated_commit=integrated_commit,
        equivalence_method="patch_id",
        equivalence_evidence_ref=None,
    )
    event_context = TransitionEventContext(
        source_message_id=None,
        evidence_refs=(),
        guard_results=(),
    )
    return TransitionRequest(
        cas=cas,
        dispatch_cas=None,
        event_id=event_id,
        event_type="CHANGE_INTEGRATED",
        payload=payload,
        event_context=event_context,
    )


def _make_block_transition_request(
    task_id: str,
    revision: int,
    head_sha: str,
    event_id: str,
) -> TransitionRequest:
    cas = TransitionCAS(
        task_id=task_id,
        expected_revision=revision,
        expected_state="review_ready",
        expected_snapshot_commit=head_sha,
    )
    payload = BlockedPayload(
        blocked_reason="Basic worker cannot resolve required dependencies",
        blocked_kind="dependency",
        blocked_owner="pm",
        unblock_condition="Escalate to Standard or higher worker tier",
        resume_state="ready",
        blocked_attempt_valid=False,
    )
    event_context = TransitionEventContext(
        source_message_id=None,
        evidence_refs=(),
        guard_results=(),
    )
    return TransitionRequest(
        cas=cas,
        dispatch_cas=None,
        event_id=event_id,
        event_type="TASK_BLOCKED",
        payload=payload,
        event_context=event_context,
    )


def _make_resolve_transition_request(
    task_id: str,
    revision: int,
    head_sha: str,
    event_id: str,
) -> TransitionRequest:
    cas = TransitionCAS(
        task_id=task_id,
        expected_revision=revision,
        expected_state="blocked",
        expected_snapshot_commit=head_sha,
    )
    payload = BlockerResolvedPayload(resume_to_state="ready")
    event_context = TransitionEventContext(
        source_message_id=None,
        evidence_refs=(),
        guard_results=(),
    )
    return TransitionRequest(
        cas=cas,
        dispatch_cas=None,
        event_id=event_id,
        event_type="BLOCKER_RESOLVED",
        payload=payload,
        event_context=event_context,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# E2E Escalation Test
# ═══════════════════════════════════════════════════════════════════════════════


class WorkflowEscalationE2ETests(unittest.IsolatedAsyncioTestCase):
    """TC-13.19c: Deterministic blocked-audit escalation E2E test.

    One test validates: Basic Worker dispatch → MAD blocked →
    TASK_BLOCKED → Basic→Standard escalation → BLOCKER_RESOLVED →
    Standard redispatch → MAD pass → DELIVERY_ACCEPTED →
    CHANGE_INTEGRATED → integrated.
    """

    async def test_blocked_audit_escalates_and_redispatches_to_integration(
        self,
    ) -> None:
        # ── 0. Capture pre-test state ──────────────────────────────────────
        source_repo = _SOURCE_REPO
        source_status_before = _git_status_bytes(source_repo)
        cwd_before = os.getcwd()

        # ── 1. Create fake executables in a temp dir ────────────────────────
        exe_tmp = Path(tempfile.mkdtemp(prefix="e2e-escalation-exes-"))
        fake_claude_exe = exe_tmp / "fake-claude.exe"
        fake_mad_exe = exe_tmp / "fake-mad.exe"

        fake_claude_exe.write_bytes(b"")
        fake_mad_exe.write_bytes(b"")
        fake_claude_exe.chmod(0o700)
        fake_mad_exe.chmod(0o700)

        try:
            # ── 2. Create temp project with git repo ────────────────────────
            project_root = Path(tempfile.mkdtemp(prefix="e2e-escalation-project-"))

            try:
                _git_init(project_root)
                _git_add_all_and_commit(project_root, "initial empty commit")

                _init_canonical_dirs(project_root)
                _init_worker_slot_store(project_root)

                # ── 3. Create task card ──────────────────────────────────────
                task_dir = project_root / "tasks" / "TC-001"
                task_dir.mkdir(parents=True, exist_ok=True)
                task_card_path_rel = "tasks/TC-001/task.md"
                task_card_content = (
                    "---\n"
                    "type: implementation\n"
                    "role_id: DEV\n"
                    'base_commit: "aaaaaaaaaa000000000000000000000000000000"\n'
                    "owner_approval:\n"
                    "  gate: none\n"
                    "---\n\n"
                    "# TC-001 · E2E Escalation Test Task\n\n"
                    "## Goal\n\n"
                    "Implement the E2E escalation fixture.\n\n"
                    "## Context Budget\n\n"
                    "Minimal.\n\n"
                    "## Scope\n\n"
                    "Test only.\n\n"
                    "## Acceptance Criteria\n\n"
                    "- [x] Done\n\n"
                    "## Delivery\n\n"
                    "Commit implementation, then report.\n"
                )
                (project_root / task_card_path_rel).parent.mkdir(parents=True, exist_ok=True)
                (project_root / task_card_path_rel).write_text(task_card_content, encoding="utf-8")

                (project_root / "reports").mkdir(parents=True, exist_ok=True)

                task_card_commit = _git_add_all_and_commit(project_root, "add task card")

                # ── 4. Create attempt-1 (Basic) implementation and report ───
                impl_file = project_root / "src" / "impl.py"
                impl_file.parent.mkdir(parents=True, exist_ok=True)
                impl_file.write_text("# E2E escalation implementation\nimport fancy_parser  # unavailable in Basic\nprint('hello')\n", encoding="utf-8")
                implementation_commit_a1 = _git_add_all_and_commit(project_root, "implementation attempt 1 (Basic)")

                report_path_rel = "reports/report.md"
                report_file = project_root / report_path_rel
                report_file.write_text(
                    "# Delivery Report (Attempt 1 — Basic)\n\n"
                    "## Summary\n\n"
                    "E2E delivery attempt 1 completed.\n\n"
                    f"## Implementation Commit\n\n{implementation_commit_a1}\n",
                    encoding="utf-8",
                )
                report_commit_a1 = _git_add_all_and_commit(project_root, "delivery report attempt 1")

                head_sha = report_commit_a1

                # ── 5. Write initial tasks.yaml (ready state) ────────────────
                task_id = "TC-001"
                revision = 1

                _write_initial_tasks_yaml(
                    project_root,
                    task_id=task_id,
                    state="ready",
                    revision=revision,
                    attempt=None,
                    task_card_path=task_card_path_rel,
                    task_card_commit=task_card_commit,
                    base_commit=task_card_commit,
                )
                head_sha = _git_add_all_and_commit(project_root, "canonical state — ready")

                _write_initial_tasks_yaml(
                    project_root,
                    task_id=task_id,
                    state="ready",
                    revision=revision,
                    attempt=None,
                    task_card_path=task_card_path_rel,
                    task_card_commit=task_card_commit,
                    base_commit=task_card_commit,
                )
                head_sha = _git_add_all_and_commit(project_root, "canonical state — ready (corrected)")

                # ── 6. Define identities ────────────────────────────────────
                now_utc = _utc("2026-07-29T12:00:00Z")
                dispatch_id_a1 = "DSP-ESCALATION-001"
                attempt_a1 = 1
                dispatch_id_a2 = "DSP-ESCALATION-002"
                attempt_a2 = 2

                # ── 7. Write approval grants ─────────────────────────────────
                # Attempt 1 (Basic) grants
                _write_dispatch_grant(
                    project_root, task_id, revision, attempt_a1,
                    dispatch_id_a1, head_sha, now_utc,
                    approval_id="APR-DISPATCH-ESC-A1",
                    event_id="EVT-APR-DISPATCH-ESC-A1",
                )
                _write_accept_grant(
                    project_root, task_id, revision, attempt_a1,
                    dispatch_id_a1, head_sha, now_utc,
                    approval_id="APR-ACCEPT-ESC-A1",
                    event_id="EVT-APR-ACCEPT-ESC-A1",
                )
                _write_integrate_grant(
                    project_root, task_id, revision, attempt_a1,
                    dispatch_id_a1, implementation_commit_a1, head_sha, now_utc,
                    approval_id="APR-INTEGRATE-ESC-A1",
                    event_id="EVT-APR-INTEGRATE-ESC-A1",
                )

                # Attempt 2 (Standard) dispatch + accept grants
                # (integrate grant for A2 written after A2 implementation commit)
                _write_dispatch_grant(
                    project_root, task_id, revision, attempt_a2,
                    dispatch_id_a2, head_sha, now_utc,
                    approval_id="APR-DISPATCH-ESC-A2",
                    event_id="EVT-APR-DISPATCH-ESC-A2",
                )
                _write_accept_grant(
                    project_root, task_id, revision, attempt_a2,
                    dispatch_id_a2, head_sha, now_utc,
                    approval_id="APR-ACCEPT-ESC-A2",
                    event_id="EVT-APR-ACCEPT-ESC-A2",
                )

                head_sha = _git_add_all_and_commit(project_root, "approval grants")
                from tests.test_workflow_orchestrator import _write_difficulty_assessment
                _write_difficulty_assessment(
                    project_root, task_id=task_id, revision=revision,
                    difficulty="basic",
                )

                # ── 8. Create attempt-2 implementation (Standard) and report ─
                impl_file_v2 = project_root / "src" / "impl.py"
                impl_file_v2.write_text(
                    "# E2E escalation implementation v2 — proper deps\n"
                    "import json  # stdlib, available in Standard\n"
                    "def main():\n"
                    "    data = json.loads('{\"key\": \"value\"}')\n"
                    "    print(data['key'])\n",
                    encoding="utf-8",
                )
                implementation_commit_a2 = _git_add_all_and_commit(project_root, "implementation attempt 2 (Standard)")

                report_file_v2 = project_root / report_path_rel
                report_file_v2.write_text(
                    "# Delivery Report (Attempt 2 — Standard)\n\n"
                    "## Summary\n\n"
                    "E2E delivery attempt 2 completed — proper deps in Standard.\n\n"
                    f"## Implementation Commit\n\n{implementation_commit_a2}\n",
                    encoding="utf-8",
                )
                report_commit_a2 = _git_add_all_and_commit(project_root, "delivery report attempt 2")

                head_sha = report_commit_a2

                # Integrate grant for attempt 2
                _write_integrate_grant(
                    project_root, task_id, revision, attempt_a2,
                    dispatch_id_a2, implementation_commit_a2, head_sha, now_utc,
                    approval_id="APR-INTEGRATE-ESC-A2",
                    event_id="EVT-APR-INTEGRATE-ESC-A2",
                )
                head_sha = _git_add_all_and_commit(project_root, "integrate grant for attempt 2")

                # ── 9. Build orchestrator and providers ──────────────────────
                clock = FakeClock()
                orch = WorkflowOrchestrator(
                    project_root=project_root,
                    clock=clock,
                    heartbeat_interval_seconds=10.0,
                )

                ms_basic = _make_model_selection_basic()
                ms_standard = _make_model_selection_standard()
                providers: Mapping[str, AgentCliProvider] = {
                    "claude": ClaudeCodeProvider(
                        provider_id="claude",
                        executable=str(fake_claude_exe),
                        permission_mode="default",
                        allowed_tools=(),
                        disallowed_tools=(),
                    ),
                }

                # ── 10. Build subprocess router ───────────────────────────────
                claude_stdout_a1 = _build_claude_stdout(
                    task_id=task_id, revision=revision, attempt=attempt_a1,
                    dispatch_id=dispatch_id_a1,
                    implementation_commit=implementation_commit_a1,
                    report_commit=report_commit_a1,
                )

                mad_archive_a1 = project_root / ".agentdesk" / "mad-archives" / "e2e-escalation-a1"
                mad_archive_a1.parent.mkdir(parents=True, exist_ok=True)
                mad_archive_a1.mkdir(parents=True, exist_ok=True)
                mad_stdout_a1 = _build_mad_audit_blocked_stdout(
                    archive_path=str(mad_archive_a1), depth_value="fast",
                )

                claude_stdout_a2 = _build_claude_stdout(
                    task_id=task_id, revision=revision, attempt=attempt_a2,
                    dispatch_id=dispatch_id_a2,
                    implementation_commit=implementation_commit_a2,
                    report_commit=report_commit_a2,
                    summary="Fixed dependencies in Standard worker.",
                )

                mad_archive_a2 = project_root / ".agentdesk" / "mad-archives" / "e2e-escalation-a2"
                mad_archive_a2.parent.mkdir(parents=True, exist_ok=True)
                mad_archive_a2.mkdir(parents=True, exist_ok=True)
                mad_stdout_a2 = _build_mad_audit_pass_stdout(
                    archive_path=str(mad_archive_a2), depth_value="balanced",
                )

                claude_proc_a1 = FakeProcess(pid=10001, returncode=0, stdout=claude_stdout_a1)
                mad_proc_a1 = FakeProcess(pid=10002, returncode=0, stdout=mad_stdout_a1)
                claude_proc_a2 = FakeProcess(pid=10003, returncode=0, stdout=claude_stdout_a2)
                mad_proc_a2 = FakeProcess(pid=10004, returncode=0, stdout=mad_stdout_a2)

                subprocess_calls: list[tuple] = []
                executed_claude_exe = str(fake_claude_exe)
                executed_mad_exe = str(fake_mad_exe)

                _claude_seq = [claude_proc_a1, claude_proc_a2]
                _mad_seq = [mad_proc_a1, mad_proc_a2]

                def subprocess_router(*argv: str, **kwargs: Any) -> FakeProcess:
                    subprocess_calls.append((argv, kwargs))
                    exe = argv[0] if argv else ""
                    if exe == executed_claude_exe:
                        return _claude_seq.pop(0)
                    elif exe == executed_mad_exe:
                        return _mad_seq.pop(0)
                    else:
                        raise AssertionError(
                            f"Unexpected subprocess executable: {exe!r}"
                        )

                mad_config = MadGatewayConfig(
                    mad_executable=str(fake_mad_exe),
                    mad_home=str(exe_tmp),
                    timeout_seconds=30,
                    planning_agent_ids=("agent-1",),
                    planning_report_agent_id="agent-1",
                    audit_agent_ids=("auditor-1",),
                    audit_report_agent_id="auditor-1",
                )

                # ═══════════════════════════════════════════════════════════
                # Phase A: Attempt 1 — Basic dispatch → MAD blocked
                # ═══════════════════════════════════════════════════════════

                dr_a1 = DispatchRequest(
                    identity=DispatchIdentity(
                        task_id=task_id, revision=revision,
                        attempt=attempt_a1, dispatch_id=dispatch_id_a1,
                    ),
                    workspace=project_root,
                    prompt="Implement the E2E escalation task (Basic).",
                    model_selection=ms_basic,
                    timeout_seconds=60,
                )

                dispatch_tr_a1 = _make_dispatch_transition_request(
                    task_id=task_id, dispatch_id=dispatch_id_a1, revision=revision,
                    model_selection=ms_basic, task_card_path=task_card_path_rel,
                    task_card_commit=task_card_commit, base_commit=task_card_commit,
                    branch="feat/e2e-escalation", report_path=report_path_rel,
                    outbox_message_id="MSG-ESCALATION-OUTBOX-A1",
                    attempt=attempt_a1, head_sha=head_sha,
                    event_id="EVT-DISPATCH-ESCALATION-A1",
                )

                ack_tr_a1 = _make_ack_transition_request(
                    task_id=task_id, dispatch_id=dispatch_id_a1, revision=revision,
                    attempt=attempt_a1, head_sha=head_sha,
                    dispatch_event_id="EVT-DISPATCH-ESCALATION-A1",
                )

                dispatch_cycle_req_a1 = DispatchCycleRequest(
                    dispatch_request=dr_a1,
                    dispatch_transition_request=dispatch_tr_a1,
                    acknowledge_transition_request=ack_tr_a1,
                    delivery_event_id="EVT-DELIVERY-ESCALATION-A1",
                    delivery_event_context=TransitionEventContext(
                        source_message_id=None, evidence_refs=(), guard_results=(),
                    ),
                    provider_cli_version="2.1.214",
                    worker_kind=WorkerKind.BASIC_AGENT,
                    task_difficulty=TaskDifficulty.BASIC,
                    holder_instance_id="e2e-escalation-instance",
                )

                audit_input_a1 = MadAuditGatewayInput(
                    project_root=project_root, task_id=task_id,
                    dispatch_id=dispatch_id_a1,
                    question="Audit the E2E escalation delivery (Basic).",
                    workspace=project_root, task_card_commit=task_card_commit,
                    task_card_path=task_card_path_rel,
                    delivery_report_path=report_path_rel,
                    report_commit=report_commit_a1, base_commit=task_card_commit,
                    implementation_commit=implementation_commit_a1,
                    depth=MadDeliberationDepth.FAST,
                )

                acceptance_path_a1 = (
                    f"docs/pm/acceptances/{task_id}-r{revision}"
                    f"-a{attempt_a1}-review1.md"
                )
                acceptance_tr_a1 = _make_acceptance_transition_request(
                    task_id=task_id, dispatch_id=dispatch_id_a1, revision=revision,
                    attempt=attempt_a1, accepted_commit=implementation_commit_a1,
                    acceptance_path=acceptance_path_a1, head_sha=head_sha,
                    event_id="EVT-ACCEPT-ESCALATION-A1",
                )

                integration_tr_a1 = _make_integration_transition_request(
                    task_id=task_id, revision=revision,
                    integrated_commit=implementation_commit_a1, head_sha=head_sha,
                    event_id="EVT-INTEGRATE-ESCALATION-A1",
                )

                # ── Block transition request ──────────────────────────────────
                block_tr = _make_block_transition_request(
                    task_id=task_id, revision=revision, head_sha=head_sha,
                    event_id="EVT-BLOCK-ESCALATION-A1",
                )

                # ═══════════════════════════════════════════════════════════
                # Phase B: Attempt 2 (Standard) redispatch
                # ═══════════════════════════════════════════════════════════

                resolve_tr = _make_resolve_transition_request(
                    task_id=task_id, revision=revision, head_sha=head_sha,
                    event_id="EVT-RESOLVE-ESCALATION-A1",
                )

                dr_a2 = DispatchRequest(
                    identity=DispatchIdentity(
                        task_id=task_id, revision=revision,
                        attempt=attempt_a2, dispatch_id=dispatch_id_a2,
                    ),
                    workspace=project_root,
                    prompt="Fix dependencies and re-deliver in Standard worker.",
                    model_selection=ms_standard,
                    timeout_seconds=60,
                )

                dispatch_tr_a2 = _make_dispatch_transition_request(
                    task_id=task_id, dispatch_id=dispatch_id_a2, revision=revision,
                    model_selection=ms_standard, task_card_path=task_card_path_rel,
                    task_card_commit=task_card_commit, base_commit=task_card_commit,
                    branch="feat/e2e-escalation", report_path=report_path_rel,
                    outbox_message_id="MSG-ESCALATION-OUTBOX-A2",
                    attempt=attempt_a2, head_sha=head_sha,
                    event_id="EVT-DISPATCH-ESCALATION-A2",
                )

                ack_tr_a2 = _make_ack_transition_request(
                    task_id=task_id, dispatch_id=dispatch_id_a2, revision=revision,
                    attempt=attempt_a2, head_sha=head_sha,
                    dispatch_event_id="EVT-DISPATCH-ESCALATION-A2",
                )

                dispatch_cycle_req_a2 = DispatchCycleRequest(
                    dispatch_request=dr_a2,
                    dispatch_transition_request=dispatch_tr_a2,
                    acknowledge_transition_request=ack_tr_a2,
                    delivery_event_id="EVT-DELIVERY-ESCALATION-A2",
                    delivery_event_context=TransitionEventContext(
                        source_message_id=None, evidence_refs=(), guard_results=(),
                    ),
                    provider_cli_version="2.1.214",
                    worker_kind=WorkerKind.STANDARD_AGENT,
                    task_difficulty=TaskDifficulty.BASIC,
                    holder_instance_id="e2e-escalation-instance",
                )

                audit_input_a2 = MadAuditGatewayInput(
                    project_root=project_root, task_id=task_id,
                    dispatch_id=dispatch_id_a2,
                    question="Audit the E2E escalation delivery (Standard).",
                    workspace=project_root, task_card_commit=task_card_commit,
                    task_card_path=task_card_path_rel,
                    delivery_report_path=report_path_rel,
                    report_commit=report_commit_a2, base_commit=task_card_commit,
                    implementation_commit=implementation_commit_a2,
                    depth=MadDeliberationDepth.BALANCED,
                )

                acceptance_path_a2 = (
                    f"docs/pm/acceptances/{task_id}-r{revision}"
                    f"-a{attempt_a2}-review1.md"
                )
                acceptance_tr_a2 = _make_acceptance_transition_request(
                    task_id=task_id, dispatch_id=dispatch_id_a2, revision=revision,
                    attempt=attempt_a2, accepted_commit=implementation_commit_a2,
                    acceptance_path=acceptance_path_a2, head_sha=head_sha,
                    event_id="EVT-ACCEPT-ESCALATION-A2",
                )

                integration_tr_a2 = _make_integration_transition_request(
                    task_id=task_id, revision=revision,
                    integrated_commit=implementation_commit_a2, head_sha=head_sha,
                    event_id="EVT-INTEGRATE-ESCALATION-A2",
                )

                # ═══════════════════════════════════════════════════════════
                # Execute all phases
                # ═══════════════════════════════════════════════════════════

                with (
                    mock.patch(
                        "worker_adapter.run_supervised_dispatch",
                        new=run_dispatch_observed,
                    ),
                    mock.patch(
                        "asyncio.create_subprocess_exec",
                        side_effect=subprocess_router,
                    ),
                ):
                    # ── Phase A.1: Attempt-1 Basic dispatch ───────────────
                    dispatch_result_a1 = await orch.run_dispatch_cycle(
                        dispatch_cycle_req_a1, providers,
                    )

                    self.assertIsInstance(dispatch_result_a1, DispatchCycleResult)
                    self.assertEqual(
                        dispatch_result_a1.dispatch_transition.from_state, "ready",
                    )
                    self.assertEqual(
                        dispatch_result_a1.dispatch_transition.to_state, "dispatched",
                    )
                    self.assertIsNotNone(dispatch_result_a1.acknowledge_transition)
                    self.assertEqual(
                        dispatch_result_a1.delivery_transition.to_state, "review_ready",
                    )
                    self.assertEqual(
                        dispatch_result_a1.worker_output.status,
                        WorkerCompletionStatus.COMPLETED,
                    )

                    receipt_a1 = dispatch_result_a1.delivery_receipt
                    self.assertEqual(receipt_a1.identity.dispatch_id, dispatch_id_a1)
                    self.assertEqual(receipt_a1.identity.attempt, attempt_a1)

                    # ── Phase A.2: Acceptance cycle — must be blocked ──────
                    acceptance_cycle_req_a1 = AcceptanceCycleRequest(
                        dispatch_cycle_result=dispatch_result_a1,
                        audit_input=audit_input_a1,
                        acceptance_transition_request=acceptance_tr_a1,
                        integration_transition_request=integration_tr_a1,
                        worker_kind=WorkerKind.BASIC_AGENT,
                        holder_instance_id="e2e-escalation-instance",
                    )

                    acceptance_result_a1 = await orch.run_acceptance_cycle(
                        acceptance_cycle_req_a1, mad_config,
                    )

                    self.assertIsInstance(acceptance_result_a1, AcceptanceCycleResult)
                    self.assertEqual(
                        acceptance_result_a1.audit_result.verdict, "blocked",
                    )
                    self.assertEqual(
                        acceptance_result_a1.audit_result.status, "completed",
                    )
                    self.assertIsNone(
                        acceptance_result_a1.accept_transition,
                        "accept_transition must be None for blocked verdict",
                    )
                    self.assertIsNone(
                        acceptance_result_a1.integrate_transition,
                        "integrate_transition must be None for blocked verdict",
                    )

                    # ── Phase A.3: Blocked audit cycle ─────────────────────
                    blocked_audit_req = BlockedAuditRequest(
                        acceptance_cycle_result=acceptance_result_a1,
                        dispatch_cycle_result=dispatch_result_a1,
                        block_transition_request=block_tr,
                        current_worker_kind=WorkerKind.BASIC_AGENT,
                    )

                    blocked_audit_result = await orch.run_blocked_audit_cycle(
                        blocked_audit_req,
                    )

                    self.assertIsInstance(blocked_audit_result, BlockedAuditResult)
                    self.assertEqual(blocked_audit_result.task_id, task_id)

                    # ── Escalation decision: Basic → Standard ───────────────
                    ed = blocked_audit_result.escalation_decision
                    from escalation_service import EscalationAction
                    self.assertEqual(ed.action, EscalationAction.ESCALATE)
                    self.assertEqual(ed.current_worker_kind, WorkerKind.BASIC_AGENT)
                    self.assertEqual(ed.next_worker_kind, WorkerKind.STANDARD_AGENT)

                    # Block transition: review_ready → blocked
                    self.assertEqual(
                        blocked_audit_result.block_transition.from_state,
                        "review_ready",
                    )
                    self.assertEqual(
                        blocked_audit_result.block_transition.to_state, "blocked",
                    )

                    # ── Phase B: Escalated redispatch ──────────────────────
                    escalated_req = EscalatedRedispatchRequest(
                        blocked_audit_request=blocked_audit_req,
                        blocked_audit_result=blocked_audit_result,
                        resolve_transition_request=resolve_tr,
                        next_dispatch_cycle_request=dispatch_cycle_req_a2,
                    )

                    escalated_result = await orch.run_escalated_redispatch(
                        escalated_req, providers,
                    )

                    self.assertIsInstance(escalated_result, EscalatedRedispatchResult)
                    self.assertEqual(escalated_result.task_id, task_id)
                    self.assertEqual(
                        escalated_result.escalation_decision.next_worker_kind,
                        WorkerKind.STANDARD_AGENT,
                    )
                    self.assertEqual(
                        escalated_result.resolve_transition.from_state, "blocked",
                    )
                    self.assertEqual(
                        escalated_result.resolve_transition.to_state, "ready",
                    )

                    # Dispatch result from escalated redispatch (attempt 2)
                    dcr_a2 = escalated_result.dispatch_cycle_result
                    self.assertIsInstance(dcr_a2, DispatchCycleResult)
                    self.assertEqual(
                        dcr_a2.dispatch_transition.from_state, "ready",
                    )
                    self.assertEqual(
                        dcr_a2.dispatch_transition.to_state, "dispatched",
                    )
                    self.assertEqual(
                        dcr_a2.delivery_transition.to_state, "review_ready",
                    )
                    self.assertEqual(
                        dcr_a2.worker_output.status, WorkerCompletionStatus.COMPLETED,
                    )

                    receipt_a2 = dcr_a2.delivery_receipt
                    self.assertEqual(receipt_a2.identity.dispatch_id, dispatch_id_a2)
                    self.assertEqual(receipt_a2.identity.attempt, attempt_a2)
                    # Must not reuse attempt 1 identity
                    self.assertNotEqual(
                        receipt_a2.identity.dispatch_id, dispatch_id_a1,
                    )

                    # ── Phase B.2: Acceptance cycle for attempt 2 — pass ──
                    acceptance_cycle_req_a2 = AcceptanceCycleRequest(
                        dispatch_cycle_result=dcr_a2,
                        audit_input=audit_input_a2,
                        acceptance_transition_request=acceptance_tr_a2,
                        integration_transition_request=integration_tr_a2,
                        worker_kind=WorkerKind.STANDARD_AGENT,
                        holder_instance_id="e2e-escalation-instance",
                    )

                    acceptance_result_a2 = await orch.run_acceptance_cycle(
                        acceptance_cycle_req_a2, mad_config,
                    )

                    self.assertIsInstance(acceptance_result_a2, AcceptanceCycleResult)
                    self.assertEqual(
                        acceptance_result_a2.audit_result.verdict, "pass",
                    )
                    self.assertEqual(
                        acceptance_result_a2.audit_result.status, "completed",
                    )
                    self.assertIsNotNone(acceptance_result_a2.accept_transition)
                    self.assertEqual(
                        acceptance_result_a2.accept_transition.from_state,
                        "review_ready",
                    )
                    self.assertEqual(
                        acceptance_result_a2.accept_transition.to_state, "accepted",
                    )
                    self.assertIsNotNone(acceptance_result_a2.integrate_transition)
                    self.assertEqual(
                        acceptance_result_a2.integrate_transition.from_state,
                        "accepted",
                    )
                    self.assertEqual(
                        acceptance_result_a2.integrate_transition.to_state,
                        "integrated",
                    )

                # ═══════════════════════════════════════════════════════════
                # Subprocess call assertions
                # ═══════════════════════════════════════════════════════════

                claude_calls = [
                    c for c in subprocess_calls if c[0][0] == executed_claude_exe
                ]
                mad_calls = [
                    c for c in subprocess_calls if c[0][0] == executed_mad_exe
                ]
                self.assertEqual(len(claude_calls), 2)
                self.assertEqual(len(mad_calls), 2)
                self.assertEqual(len(subprocess_calls), 4)

                # Exact call order
                all_exes = [c[0][0] for c in subprocess_calls]
                self.assertEqual(all_exes[0], executed_claude_exe)  # Claude A1
                self.assertEqual(all_exes[1], executed_mad_exe)     # MAD blocked
                self.assertEqual(all_exes[2], executed_claude_exe)  # Claude A2
                self.assertEqual(all_exes[3], executed_mad_exe)     # MAD pass

                # All argv-tuple calls
                for call_args, _ in subprocess_calls:
                    self.assertIsInstance(call_args, tuple)

                # ═══════════════════════════════════════════════════════════
                # Final StateProvider snapshot
                # ═══════════════════════════════════════════════════════════

                snapshot = StateProvider(project_root).snapshot()
                self.assertIsInstance(snapshot, StateSnapshot)

                tasks = [t for t in snapshot.tasks if t.task_id == task_id]
                self.assertEqual(len(tasks), 1)
                task = tasks[0]

                self.assertEqual(task.state, "integrated")
                self.assertEqual(task.revision, revision)
                self.assertEqual(task.attempt, attempt_a2)

                # Commits must be from attempt 2
                self.assertEqual(task.implementation_commit, implementation_commit_a2)
                self.assertEqual(task.report_commit, report_commit_a2)
                self.assertEqual(task.accepted_commit, implementation_commit_a2)
                self.assertEqual(task.integrated_commit, implementation_commit_a2)
                self.assertNotEqual(task.implementation_commit, implementation_commit_a1)

                # current_dispatch must be None
                self.assertIsNone(task.current_dispatch)
                self.assertFalse(hasattr(task, "model_selection"))

                # Acceptance binds attempt 2
                self.assertIsNotNone(task.acceptance_path)
                self.assertIn(f"a{attempt_a2}", task.acceptance_path or "")
                self.assertNotIn(f"a{attempt_a1}", task.acceptance_path or "")

                # ═══════════════════════════════════════════════════════════
                # Event sequence assertions
                # ═══════════════════════════════════════════════════════════

                expected_sequence = [
                    "TASK_DISPATCHED",       # attempt 1 / Basic
                    "DISPATCH_ACKNOWLEDGED", # attempt 1
                    "DELIVERY_SUBMITTED",    # attempt 1
                    "TASK_BLOCKED",
                    "BLOCKER_RESOLVED",
                    "TASK_DISPATCHED",       # attempt 2 / Standard
                    "DISPATCH_ACKNOWLEDGED", # attempt 2
                    "DELIVERY_SUBMITTED",    # attempt 2
                    "DELIVERY_ACCEPTED",     # attempt 2
                    "CHANGE_INTEGRATED",
                ]

                task_events_sorted = sorted(
                    [e for e in snapshot.events if e.task_id == task_id],
                    key=lambda e: e.occurred_at,
                )
                observed_types = [e.event_type for e in task_events_sorted]

                for expected_type in set(expected_sequence):
                    self.assertIn(expected_type, observed_types)

                # Verify business order with duplicate-type handling
                for i in range(len(expected_sequence) - 1):
                    before, after = expected_sequence[i], expected_sequence[i + 1]
                    try:
                        idx_before = observed_types.index(before)
                        idx_after = observed_types.index(after)
                    except ValueError:
                        continue
                    if before == "TASK_BLOCKED" and after == "TASK_DISPATCHED":
                        # Second dispatch must follow block
                        try:
                            idx_after = [
                                j for j, t in enumerate(observed_types)
                                if t == "TASK_DISPATCHED" and j > idx_before
                            ][0]
                        except (IndexError, ValueError):
                            pass
                    elif before == "BLOCKER_RESOLVED" and after == "TASK_DISPATCHED":
                        # Second dispatch must follow resolve (skip first dispatch)
                        try:
                            idx_after = [
                                j for j, t in enumerate(observed_types)
                                if t == "TASK_DISPATCHED" and j > idx_before
                            ][0]
                        except (IndexError, ValueError):
                            pass
                    self.assertLess(idx_before, idx_after,
                                    f"{before} must occur before {after}")

                # Must NOT have DELIVERY_RETURNED, TASK_REQUEUED
                self.assertNotIn("DELIVERY_RETURNED", observed_types)
                self.assertNotIn("TASK_REQUEUED", observed_types)

                # Exactly two TASK_DISPATCHED
                dispatch_events = [e for e in task_events_sorted if e.event_type == "TASK_DISPATCHED"]
                self.assertEqual(len(dispatch_events), 2)

                # Exactly one TASK_BLOCKED, one BLOCKER_RESOLVED
                self.assertEqual(
                    len([e for e in task_events_sorted if e.event_type == "TASK_BLOCKED"]), 1,
                )
                self.assertEqual(
                    len([e for e in task_events_sorted if e.event_type == "BLOCKER_RESOLVED"]), 1,
                )

                # ── Must NOT have A1 acceptance or integration ──
                before_resolve = [
                    e for e in task_events_sorted
                    if e.event_type in ("DELIVERY_ACCEPTED", "CHANGE_INTEGRATED")
                    and e.occurred_at
                    < next(
                        (ev.occurred_at for ev in task_events_sorted
                         if ev.event_type == "BLOCKER_RESOLVED"),
                        "9999",
                    )
                ]
                self.assertEqual(len(before_resolve), 0,
                                 "No acceptance or integration before BLOCKER_RESOLVED")

                # ═══════════════════════════════════════════════════════════
                # Guard assertions
                # ═══════════════════════════════════════════════════════════

                gated_events = {"TASK_DISPATCHED", "DELIVERY_ACCEPTED", "CHANGE_INTEGRATED"}
                non_gated_events = {
                    "DELIVERY_SUBMITTED", "DISPATCH_ACKNOWLEDGED",
                    "TASK_BLOCKED", "BLOCKER_RESOLVED",
                }

                for ev in task_events_sorted:
                    guard_names = {gr.guard for gr in ev.guard_results}
                    has_approval_gate = "approval_gate" in guard_names
                    if ev.event_type in gated_events:
                        self.assertTrue(has_approval_gate,
                                        f"{ev.event_type} must have approval_gate")
                    elif ev.event_type in non_gated_events:
                        self.assertFalse(has_approval_gate,
                                         f"{ev.event_type} must NOT have approval_gate")

                # Both dispatch events have approval_gate
                for de in dispatch_events:
                    guard_names = {gr.guard for gr in de.guard_results}
                    self.assertIn("approval_gate", guard_names)

                # ═══════════════════════════════════════════════════════════
                # Worker slot cleanup
                # ═══════════════════════════════════════════════════════════

                import worker_slot_lease as wsl
                store = wsl.read_worker_slot_leases(project_root)
                self.assertEqual(len(store.get("leases", {})), 0)

                # ═══════════════════════════════════════════════════════════
                # No .tmp-* files
                # ═══════════════════════════════════════════════════════════

                tmp_files = list(project_root.rglob(".tmp-*"))
                self.assertEqual(len(tmp_files), 0)

                # ═══════════════════════════════════════════════════════════
                # No Git remote
                # ═══════════════════════════════════════════════════════════

                remotes_result = subprocess.run(
                    ["git", "-C", str(project_root), "remote"],
                    capture_output=True, text=True, timeout=10,
                )
                self.assertEqual(remotes_result.stdout.strip(), "")

                # ═══════════════════════════════════════════════════════════
                # cwd unchanged, no asyncio leak
                # ═══════════════════════════════════════════════════════════

                self.assertEqual(os.getcwd(), cwd_before)

                try:
                    loop = asyncio.get_running_loop()
                except RuntimeError:
                    loop = None
                if loop is not None:
                    all_tasks = asyncio.all_tasks(loop)
                    self.assertLessEqual(len(all_tasks), 3)

                # ═══════════════════════════════════════════════════════════
                # Event uniqueness & no orphans
                # ═══════════════════════════════════════════════════════════

                event_ids = [e.event_id for e in snapshot.events]
                self.assertEqual(len(event_ids), len(set(event_ids)))
                msg_ids = [o.message_id for o in snapshot.outbox]
                self.assertEqual(len(msg_ids), len(set(msg_ids)))

                task_ids_in_snapshot = {t.task_id for t in snapshot.tasks}
                for ev in snapshot.events:
                    self.assertIn(ev.task_id, task_ids_in_snapshot)
                for ob in snapshot.outbox:
                    self.assertIn(ob.task_id, task_ids_in_snapshot)
                for acc in snapshot.acceptances:
                    self.assertIn(acc.task_id, task_ids_in_snapshot)

                # ═══════════════════════════════════════════════════════════
                # Mad-ref assertions
                # ═══════════════════════════════════════════════════════════

                self.assertIsNotNone(snapshot.mad_refs)
                audit_refs = [r for r in snapshot.mad_refs if r.purpose == "audit"]
                self.assertGreaterEqual(len(audit_refs), 2)
                audit_ref_dispatch_ids = {r.dispatch_id for r in audit_refs}
                self.assertIn(dispatch_id_a1, audit_ref_dispatch_ids)
                self.assertIn(dispatch_id_a2, audit_ref_dispatch_ids)

                # ═══════════════════════════════════════════════════════════
                # Identity isolation
                # ═══════════════════════════════════════════════════════════

                self.assertNotEqual(dispatch_id_a1, dispatch_id_a2)
                self.assertNotEqual(attempt_a1, attempt_a2)
                self.assertNotEqual(
                    receipt_a1.identity.dispatch_id,
                    receipt_a2.identity.dispatch_id,
                )
                self.assertNotEqual(
                    receipt_a1.identity.attempt,
                    receipt_a2.identity.attempt,
                )

                outbox_msgs = [o.message_id for o in snapshot.outbox if o.task_id == task_id]
                self.assertEqual(len(outbox_msgs), 2)
                self.assertNotEqual(outbox_msgs[0], outbox_msgs[1])

            finally:
                shutil.rmtree(project_root, ignore_errors=True)
        finally:
            shutil.rmtree(exe_tmp, ignore_errors=True)

        # ═══════════════════════════════════════════════════════════════
        # Source repo purity
        # ═══════════════════════════════════════════════════════════════

        source_status_after = _git_status_bytes(source_repo)
        self.assertEqual(
            source_status_after, source_status_before,
            "AgentDesk source repo must not be modified by test",
        )
