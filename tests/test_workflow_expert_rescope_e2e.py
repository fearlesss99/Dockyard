"""TC-13.19g — Expert User-Decision Rescope Closed-Loop E2E.

Deterministic E2E that validates the complete escalation chain through
all four WorkerKind tiers followed by caller-initiated rescope after
Expert returns REQUEST_USER_DECISION:

    Basic blocked → Standard blocked → Advanced blocked → Expert blocked
    → REQUEST_USER_DECISION
    → illegal rescope fail-closed (zero writes)
    → BLOCKER_RESCOPED
    → draft revision+1

Uses real production modules.  Only mocks ``asyncio.create_subprocess_exec``
(single router for both Claude and MAD subprocess boundaries) and the clock.
"""

from __future__ import annotations

import asyncio
import hashlib
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
    BlockerCancelledPayload,
    BlockerRescopedPayload,
    BlockerResolvedPayload,
    CancelledPayload,
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
    BlockedCancellationRequest,
    BlockedCancellationResult,
    BlockedRescopeRequest,
    BlockedRescopeResult,
    DispatchCycleRequest,
    DispatchCycleResult,
    EscalatedRedispatchRequest,
    EscalatedRedispatchResult,
    WorkflowClock,
    WorkflowInputError,
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


# ── MAD audit stdout builder (all blocked) ───────────────────────────────────


def _build_mad_audit_blocked_stdout(
    archive_path: str,
    deliberation_id: str,
    issue_id: str,
    depth_value: str = "balanced",
    worker_name: str = "worker",
) -> bytes:
    """Build a valid ``mad.audit-result/v1`` JSON with verdict=blocked."""
    result = {
        "schema_version": "mad.audit-result/v1",
        "deliberation_id": deliberation_id,
        "status": "completed",
        "verdict": "blocked",
        "issues": [
            {
                "id": issue_id,
                "severity": "critical",
                "category": "process",
                "title": f"Dependency not available in {worker_name} Worker environment",
                "description": f"The implementation requires capabilities beyond the {worker_name} worker tier.",
                "recommendation": f"Escalate to next tier above {worker_name}.",
                "location": {
                    "file": "src/impl.py",
                    "line": 1,
                    "commit": "0" * 40,
                },
            },
        ],
        "evidence": [],
        "warnings": [f"Worker tier insufficient for {worker_name}"],
        "report": f"Delivery blocked: {worker_name} tier cannot resolve required dependencies.",
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
            "branch": "feat/e2e-expert-cancel",
            "dispatched_at": "2026-07-29T12:00:00Z",
            "model_selection": {
                "required_model_tier": "basic",
                "required_model_capabilities": [],
                "model_binding_id": "binding-001",
                "selected_model_provider": "claude",
                "selected_model_id": "claude-haiku-4-5",
                "selected_model_tier": "basic",
                "selected_deliberation_tier": "efficient",
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
        reason=f"E2E expert cancellation dispatch approval ({approval_id})",
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
        reason=f"E2E expert cancellation accept approval ({approval_id})",
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
        reason=f"E2E expert cancellation integrate approval ({approval_id})",
        expires_at=None,
        expected_snapshot_commit=head_sha,
    )


# ── Model selection builders (one per tier) ──────────────────────────────────


def _make_model_selection_basic() -> Any:
    from dispatcher_gateway import ModelSelectionSnapshot as DPModelSelection
    return DPModelSelection(
        required_model_tier="basic",
        required_model_capabilities=(),
        model_binding_id="binding-basic-001",
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
        model_binding_id="binding-standard-002",
        selected_model_provider="claude",
        selected_model_id="claude-sonnet-4-5",
        selected_model_tier="standard",
        selected_deliberation_tier="balanced",
        selected_context_window_tokens=200000,
        selected_model_capabilities=(),
        model_degradation_approval_id=None,
    )


def _make_model_selection_advanced() -> Any:
    from dispatcher_gateway import ModelSelectionSnapshot as DPModelSelection
    return DPModelSelection(
        required_model_tier="advanced",
        required_model_capabilities=(),
        model_binding_id="binding-advanced-003",
        selected_model_provider="claude",
        selected_model_id="claude-sonnet-4-5",
        selected_model_tier="advanced",
        selected_deliberation_tier="balanced",
        selected_context_window_tokens=200000,
        selected_model_capabilities=(),
        model_degradation_approval_id=None,
    )


def _make_model_selection_expert() -> Any:
    from dispatcher_gateway import ModelSelectionSnapshot as DPModelSelection
    return DPModelSelection(
        required_model_tier="expert",
        required_model_capabilities=(),
        model_binding_id="binding-expert-004",
        selected_model_provider="claude",
        selected_model_id="claude-opus-4-8",
        selected_model_tier="expert",
        selected_deliberation_tier="deep",
        selected_context_window_tokens=200000,
        selected_model_capabilities=(),
        model_degradation_approval_id=None,
    )


# ── Transition request builders ──────────────────────────────────────────────


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
        criteria_evidence=("E2E expert cancellation acceptance criteria satisfied",),
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
    blocked_reason: str,
    unblock_condition: str,
    resume_state: str = "ready",
) -> TransitionRequest:
    cas = TransitionCAS(
        task_id=task_id,
        expected_revision=revision,
        expected_state="review_ready",
        expected_snapshot_commit=head_sha,
    )
    payload = BlockedPayload(
        blocked_reason=blocked_reason,
        blocked_kind="dependency",
        blocked_owner="pm",
        unblock_condition=unblock_condition,
        resume_state=resume_state,
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


def _make_cancellation_transition_request(
    task_id: str,
    revision: int,
    head_sha: str,
    event_id: str,
) -> TransitionRequest:
    """Build a BLOCKER_CANCELLED TransitionRequest with BlockerCancelledPayload."""
    cas = TransitionCAS(
        task_id=task_id,
        expected_revision=revision,
        expected_state="blocked",
        expected_snapshot_commit=head_sha,
    )
    payload = BlockerCancelledPayload()
    event_context = TransitionEventContext(
        source_message_id=None,
        evidence_refs=(),
        guard_results=(),
    )
    return TransitionRequest(
        cas=cas,
        dispatch_cas=None,
        event_id=event_id,
        event_type="BLOCKER_CANCELLED",
        payload=payload,
        event_context=event_context,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# E2E Expert Cancellation Test
# ═══════════════════════════════════════════════════════════════════════════════


class WorkflowExpertRescopeE2ETests(unittest.IsolatedAsyncioTestCase):
    """TC-13.19g: Expert user-decision rescope closed-loop E2E.

    One test validates: Basic → Standard → Advanced → Expert all blocked,
    Expert returns REQUEST_USER_DECISION, then caller-initiated rescope
    via BLOCKER_RESCOPED → draft revision+1.  Also validates illegal rescope
    requests fail-closed with zero writes.
    """

    async def test_expert_user_decision_rescopes_blocked_task_to_new_draft_fail_closed(
        self,
    ) -> None:
        # ── 0. Capture pre-test state ──────────────────────────────────────
        source_repo = _SOURCE_REPO
        source_status_before = _git_status_bytes(source_repo)
        cwd_before = os.getcwd()

        # ── 1. Create fake executables in a temp dir ────────────────────────
        exe_tmp = Path(tempfile.mkdtemp(prefix="e2e-expert-cancel-exes-"))
        fake_claude_exe = exe_tmp / "fake-claude.exe"
        fake_mad_exe = exe_tmp / "fake-mad.exe"

        fake_claude_exe.write_bytes(b"")
        fake_mad_exe.write_bytes(b"")
        fake_claude_exe.chmod(0o700)
        fake_mad_exe.chmod(0o700)

        try:
            # ── 2. Create temp project with git repo ────────────────────────
            project_root = Path(tempfile.mkdtemp(prefix="e2e-expert-cancel-project-"))

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
                    "# TC-001 · E2E Expert Cancellation Test Task\n\n"
                    "## Goal\n\n"
                    "Implement the E2E expert cancellation fixture.\n\n"
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

                report_path_rel = "reports/report.md"
                task_id = "TC-001"
                revision = 1

                # ── 4. Define identities for all four attempts ───────────────
                dispatch_id_a1 = "DSP-EXPERT-CANCEL-001"
                attempt_a1 = 1
                dispatch_id_a2 = "DSP-EXPERT-CANCEL-002"
                attempt_a2 = 2
                dispatch_id_a3 = "DSP-EXPERT-CANCEL-003"
                attempt_a3 = 3
                dispatch_id_a4 = "DSP-EXPERT-CANCEL-004"
                attempt_a4 = 4

                # ── 5. Create attempt-1 (Basic) impl + report ────────────────
                impl_file = project_root / "src" / "impl.py"
                impl_file.parent.mkdir(parents=True, exist_ok=True)
                impl_file.write_text(
                    "# E2E expert cancellation implementation A1\n"
                    "import fancy_parser  # unavailable in Basic\n"
                    "print('hello')\n",
                    encoding="utf-8",
                )
                impl_a1 = _git_add_all_and_commit(project_root, "implementation attempt 1 (Basic)")

                report_file = project_root / report_path_rel
                report_file.write_text(
                    "# Delivery Report (Attempt 1 — Basic)\n\n"
                    "## Summary\n\nE2E delivery attempt 1 (Basic).\n\n"
                    f"## Implementation Commit\n\n{impl_a1}\n",
                    encoding="utf-8",
                )
                report_a1 = _git_add_all_and_commit(project_root, "delivery report attempt 1")

                # ── 6. Create attempt-2 (Standard) impl + report ────────────
                impl_file.write_text(
                    "# E2E expert cancellation implementation A2\n"
                    "import compiled_native  # unavailable in Standard\n"
                    "print('hello a2')\n",
                    encoding="utf-8",
                )
                impl_a2 = _git_add_all_and_commit(project_root, "implementation attempt 2 (Standard)")

                report_file.write_text(
                    "# Delivery Report (Attempt 2 — Standard)\n\n"
                    "## Summary\n\nE2E delivery attempt 2 (Standard).\n\n"
                    f"## Implementation Commit\n\n{impl_a2}\n",
                    encoding="utf-8",
                )
                report_a2 = _git_add_all_and_commit(project_root, "delivery report attempt 2")

                # ── 7. Create attempt-3 (Advanced) impl + report ────────────
                impl_file.write_text(
                    "# E2E expert cancellation implementation A3\n"
                    "import distributed_cluster  # unavailable in Advanced\n"
                    "print('hello a3')\n",
                    encoding="utf-8",
                )
                impl_a3 = _git_add_all_and_commit(project_root, "implementation attempt 3 (Advanced)")

                report_file.write_text(
                    "# Delivery Report (Attempt 3 — Advanced)\n\n"
                    "## Summary\n\nE2E delivery attempt 3 (Advanced).\n\n"
                    f"## Implementation Commit\n\n{impl_a3}\n",
                    encoding="utf-8",
                )
                report_a3 = _git_add_all_and_commit(project_root, "delivery report attempt 3")

                # ── 8. Create attempt-4 (Expert) impl + report ──────────────
                impl_file.write_text(
                    "# E2E expert cancellation implementation A4\n"
                    "import theoretical_dep  # unavailable in Expert\n"
                    "print('hello a4')\n",
                    encoding="utf-8",
                )
                impl_a4 = _git_add_all_and_commit(project_root, "implementation attempt 4 (Expert)")

                report_file.write_text(
                    "# Delivery Report (Attempt 4 — Expert)\n\n"
                    "## Summary\n\nE2E delivery attempt 4 (Expert).\n\n"
                    f"## Implementation Commit\n\n{impl_a4}\n",
                    encoding="utf-8",
                )
                report_a4 = _git_add_all_and_commit(project_root, "delivery report attempt 4")

                head_sha = report_a4

                # ── 9. Write initial tasks.yaml (ready state) ────────────────
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

                # ── 10. Write all approval grants ───────────────────────────
                now_utc = _utc("2026-07-29T12:00:00Z")

                # Attempt 1 (Basic) grants
                _write_dispatch_grant(
                    project_root, task_id, revision, attempt_a1,
                    dispatch_id_a1, head_sha, now_utc,
                    approval_id="APR-DISPATCH-EXP-CANCEL-A1",
                    event_id="EVT-APR-DISPATCH-EXP-CANCEL-A1",
                )
                _write_accept_grant(
                    project_root, task_id, revision, attempt_a1,
                    dispatch_id_a1, head_sha, now_utc,
                    approval_id="APR-ACCEPT-EXP-CANCEL-A1",
                    event_id="EVT-APR-ACCEPT-EXP-CANCEL-A1",
                )
                _write_integrate_grant(
                    project_root, task_id, revision, attempt_a1,
                    dispatch_id_a1, impl_a1, head_sha, now_utc,
                    approval_id="APR-INTEGRATE-EXP-CANCEL-A1",
                    event_id="EVT-APR-INTEGRATE-EXP-CANCEL-A1",
                )

                # Attempt 2 (Standard) grants
                _write_dispatch_grant(
                    project_root, task_id, revision, attempt_a2,
                    dispatch_id_a2, head_sha, now_utc,
                    approval_id="APR-DISPATCH-EXP-CANCEL-A2",
                    event_id="EVT-APR-DISPATCH-EXP-CANCEL-A2",
                )
                _write_accept_grant(
                    project_root, task_id, revision, attempt_a2,
                    dispatch_id_a2, head_sha, now_utc,
                    approval_id="APR-ACCEPT-EXP-CANCEL-A2",
                    event_id="EVT-APR-ACCEPT-EXP-CANCEL-A2",
                )
                _write_integrate_grant(
                    project_root, task_id, revision, attempt_a2,
                    dispatch_id_a2, impl_a2, head_sha, now_utc,
                    approval_id="APR-INTEGRATE-EXP-CANCEL-A2",
                    event_id="EVT-APR-INTEGRATE-EXP-CANCEL-A2",
                )

                # Attempt 3 (Advanced) grants
                _write_dispatch_grant(
                    project_root, task_id, revision, attempt_a3,
                    dispatch_id_a3, head_sha, now_utc,
                    approval_id="APR-DISPATCH-EXP-CANCEL-A3",
                    event_id="EVT-APR-DISPATCH-EXP-CANCEL-A3",
                )
                _write_accept_grant(
                    project_root, task_id, revision, attempt_a3,
                    dispatch_id_a3, head_sha, now_utc,
                    approval_id="APR-ACCEPT-EXP-CANCEL-A3",
                    event_id="EVT-APR-ACCEPT-EXP-CANCEL-A3",
                )
                _write_integrate_grant(
                    project_root, task_id, revision, attempt_a3,
                    dispatch_id_a3, impl_a3, head_sha, now_utc,
                    approval_id="APR-INTEGRATE-EXP-CANCEL-A3",
                    event_id="EVT-APR-INTEGRATE-EXP-CANCEL-A3",
                )

                # Attempt 4 (Expert) grants
                _write_dispatch_grant(
                    project_root, task_id, revision, attempt_a4,
                    dispatch_id_a4, head_sha, now_utc,
                    approval_id="APR-DISPATCH-EXP-CANCEL-A4",
                    event_id="EVT-APR-DISPATCH-EXP-CANCEL-A4",
                )
                _write_accept_grant(
                    project_root, task_id, revision, attempt_a4,
                    dispatch_id_a4, head_sha, now_utc,
                    approval_id="APR-ACCEPT-EXP-CANCEL-A4",
                    event_id="EVT-APR-ACCEPT-EXP-CANCEL-A4",
                )
                _write_integrate_grant(
                    project_root, task_id, revision, attempt_a4,
                    dispatch_id_a4, impl_a4, head_sha, now_utc,
                    approval_id="APR-INTEGRATE-EXP-CANCEL-A4",
                    event_id="EVT-APR-INTEGRATE-EXP-CANCEL-A4",
                )

                head_sha = _git_add_all_and_commit(project_root, "all approval grants")
                from tests.test_workflow_orchestrator import _write_difficulty_assessment
                _write_difficulty_assessment(
                    project_root, task_id=task_id, revision=revision,
                    difficulty="basic",
                )

                # ── 11. Build orchestrator and providers ────────────────────
                clock = FakeClock()
                orch = WorkflowOrchestrator(
                    project_root=project_root,
                    clock=clock,
                    heartbeat_interval_seconds=10.0,
                )

                ms_basic = _make_model_selection_basic()
                ms_standard = _make_model_selection_standard()
                ms_advanced = _make_model_selection_advanced()
                ms_expert = _make_model_selection_expert()

                providers: Mapping[str, AgentCliProvider] = {
                    "claude": ClaudeCodeProvider(
                        provider_id="claude",
                        executable=str(fake_claude_exe),
                        permission_mode="default",
                        allowed_tools=(),
                        disallowed_tools=(),
                    ),
                }

                # ── 12. Build subprocess router (8 calls: 4 Claude + 4 MAD) ──
                claude_stdout_a1 = _build_claude_stdout(
                    task_id=task_id, revision=revision, attempt=attempt_a1,
                    dispatch_id=dispatch_id_a1,
                    implementation_commit=impl_a1,
                    report_commit=report_a1,
                    summary="Basic worker implementation.",
                )
                claude_stdout_a2 = _build_claude_stdout(
                    task_id=task_id, revision=revision, attempt=attempt_a2,
                    dispatch_id=dispatch_id_a2,
                    implementation_commit=impl_a2,
                    report_commit=report_a2,
                    summary="Standard worker implementation.",
                )
                claude_stdout_a3 = _build_claude_stdout(
                    task_id=task_id, revision=revision, attempt=attempt_a3,
                    dispatch_id=dispatch_id_a3,
                    implementation_commit=impl_a3,
                    report_commit=report_a3,
                    summary="Advanced worker implementation.",
                )
                claude_stdout_a4 = _build_claude_stdout(
                    task_id=task_id, revision=revision, attempt=attempt_a4,
                    dispatch_id=dispatch_id_a4,
                    implementation_commit=impl_a4,
                    report_commit=report_a4,
                    summary="Expert worker implementation.",
                )

                mad_archive_dir = project_root / ".agentdesk" / "mad-archives"
                mad_archive_dir.mkdir(parents=True, exist_ok=True)

                mad_archive_a1 = mad_archive_dir / "e2e-expert-cancel-a1"
                mad_archive_a1.mkdir(parents=True, exist_ok=True)
                mad_stdout_a1 = _build_mad_audit_blocked_stdout(
                    archive_path=str(mad_archive_a1),
                    deliberation_id="DELIB-EXPERT-CANCEL-A1",
                    issue_id="BLOCK-BASIC-001",
                    depth_value="fast",
                    worker_name="Basic",
                )

                mad_archive_a2 = mad_archive_dir / "e2e-expert-cancel-a2"
                mad_archive_a2.mkdir(parents=True, exist_ok=True)
                mad_stdout_a2 = _build_mad_audit_blocked_stdout(
                    archive_path=str(mad_archive_a2),
                    deliberation_id="DELIB-EXPERT-CANCEL-A2",
                    issue_id="BLOCK-STANDARD-001",
                    depth_value="balanced",
                    worker_name="Standard",
                )

                mad_archive_a3 = mad_archive_dir / "e2e-expert-cancel-a3"
                mad_archive_a3.mkdir(parents=True, exist_ok=True)
                mad_stdout_a3 = _build_mad_audit_blocked_stdout(
                    archive_path=str(mad_archive_a3),
                    deliberation_id="DELIB-EXPERT-CANCEL-A3",
                    issue_id="BLOCK-ADVANCED-001",
                    depth_value="balanced",
                    worker_name="Advanced",
                )

                mad_archive_a4 = mad_archive_dir / "e2e-expert-cancel-a4"
                mad_archive_a4.mkdir(parents=True, exist_ok=True)
                mad_stdout_a4 = _build_mad_audit_blocked_stdout(
                    archive_path=str(mad_archive_a4),
                    deliberation_id="DELIB-EXPERT-CANCEL-A4",
                    issue_id="BLOCK-EXPERT-001",
                    depth_value="deep",
                    worker_name="Expert",
                )

                claude_proc_a1 = FakeProcess(pid=8001, returncode=0, stdout=claude_stdout_a1)
                mad_proc_a1 = FakeProcess(pid=8002, returncode=0, stdout=mad_stdout_a1)
                claude_proc_a2 = FakeProcess(pid=8003, returncode=0, stdout=claude_stdout_a2)
                mad_proc_a2 = FakeProcess(pid=8004, returncode=0, stdout=mad_stdout_a2)
                claude_proc_a3 = FakeProcess(pid=8005, returncode=0, stdout=claude_stdout_a3)
                mad_proc_a3 = FakeProcess(pid=8006, returncode=0, stdout=mad_stdout_a3)
                claude_proc_a4 = FakeProcess(pid=8007, returncode=0, stdout=claude_stdout_a4)
                mad_proc_a4 = FakeProcess(pid=8008, returncode=0, stdout=mad_stdout_a4)

                subprocess_calls: list[tuple] = []
                executed_claude_exe = str(fake_claude_exe)
                executed_mad_exe = str(fake_mad_exe)

                _claude_seq = [claude_proc_a1, claude_proc_a2, claude_proc_a3, claude_proc_a4]
                _mad_seq = [mad_proc_a1, mad_proc_a2, mad_proc_a3, mad_proc_a4]

                def subprocess_router(*argv: str, **kwargs: Any) -> FakeProcess:
                    subprocess_calls.append((argv, kwargs))
                    exe = argv[0] if argv else ""
                    if exe == executed_claude_exe:
                        if not _claude_seq:
                            raise AssertionError("Unexpected extra Claude subprocess call")
                        return _claude_seq.pop(0)
                    elif exe == executed_mad_exe:
                        if not _mad_seq:
                            raise AssertionError("Unexpected extra MAD subprocess call")
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
                # Build all dispatch/acceptance/etc requests for all 4 attempts
                # ═══════════════════════════════════════════════════════════

                # -- Attempt 1 (Basic) --
                dr_a1 = DispatchRequest(
                    identity=DispatchIdentity(
                        task_id=task_id, revision=revision,
                        attempt=attempt_a1, dispatch_id=dispatch_id_a1,
                    ),
                    workspace=project_root,
                    prompt="Implement the E2E expert cancellation task (Basic).",
                    model_selection=ms_basic,
                    timeout_seconds=60,
                )
                dispatch_tr_a1 = _make_dispatch_transition_request(
                    task_id=task_id, dispatch_id=dispatch_id_a1, revision=revision,
                    model_selection=ms_basic, task_card_path=task_card_path_rel,
                    task_card_commit=task_card_commit, base_commit=task_card_commit,
                    branch="feat/e2e-expert-cancel", report_path=report_path_rel,
                    outbox_message_id="MSG-EXPERT-CANCEL-OUTBOX-A1",
                    attempt=attempt_a1, head_sha=head_sha,
                    event_id="EVT-DISPATCH-EXPERT-CANCEL-A1",
                )
                ack_tr_a1 = _make_ack_transition_request(
                    task_id=task_id, dispatch_id=dispatch_id_a1, revision=revision,
                    attempt=attempt_a1, head_sha=head_sha,
                    dispatch_event_id="EVT-DISPATCH-EXPERT-CANCEL-A1",
                )
                dcr_a1 = DispatchCycleRequest(
                    dispatch_request=dr_a1,
                    dispatch_transition_request=dispatch_tr_a1,
                    acknowledge_transition_request=ack_tr_a1,
                    delivery_event_id="EVT-DELIVERY-EXPERT-CANCEL-A1",
                    delivery_event_context=TransitionEventContext(
                        source_message_id=None, evidence_refs=(), guard_results=(),
                    ),
                    provider_cli_version="2.1.214",
                    worker_kind=WorkerKind.BASIC_AGENT,
                    task_difficulty=TaskDifficulty.BASIC,
                    holder_instance_id="e2e-expert-cancel-instance",
                )
                audit_a1 = MadAuditGatewayInput(
                    project_root=project_root, task_id=task_id,
                    dispatch_id=dispatch_id_a1,
                    question="Audit the E2E expert cancellation delivery (Basic).",
                    workspace=project_root, task_card_commit=task_card_commit,
                    task_card_path=task_card_path_rel,
                    delivery_report_path=report_path_rel,
                    report_commit=report_a1, base_commit=task_card_commit,
                    implementation_commit=impl_a1,
                    depth=MadDeliberationDepth.FAST,
                )
                acceptance_path_a1 = (
                    f"docs/pm/acceptances/{task_id}-r{revision}"
                    f"-a{attempt_a1}-review1.md"
                )
                acceptance_tr_a1 = _make_acceptance_transition_request(
                    task_id=task_id, dispatch_id=dispatch_id_a1, revision=revision,
                    attempt=attempt_a1, accepted_commit=impl_a1,
                    acceptance_path=acceptance_path_a1, head_sha=head_sha,
                    event_id="EVT-ACCEPT-EXPERT-CANCEL-A1",
                )
                integration_tr_a1 = _make_integration_transition_request(
                    task_id=task_id, revision=revision,
                    integrated_commit=impl_a1, head_sha=head_sha,
                    event_id="EVT-INTEGRATE-EXPERT-CANCEL-A1",
                )
                block_tr_a1 = _make_block_transition_request(
                    task_id=task_id, revision=revision, head_sha=head_sha,
                    event_id="EVT-BLOCK-EXPERT-CANCEL-A1",
                    blocked_reason="Basic worker cannot resolve required dependencies",
                    unblock_condition="Escalate to Standard or higher worker tier",
                )
                resolve_tr_a1 = _make_resolve_transition_request(
                    task_id=task_id, revision=revision, head_sha=head_sha,
                    event_id="EVT-RESOLVE-EXPERT-CANCEL-A1",
                )

                # -- Attempt 2 (Standard) --
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
                    branch="feat/e2e-expert-cancel", report_path=report_path_rel,
                    outbox_message_id="MSG-EXPERT-CANCEL-OUTBOX-A2",
                    attempt=attempt_a2, head_sha=head_sha,
                    event_id="EVT-DISPATCH-EXPERT-CANCEL-A2",
                )
                ack_tr_a2 = _make_ack_transition_request(
                    task_id=task_id, dispatch_id=dispatch_id_a2, revision=revision,
                    attempt=attempt_a2, head_sha=head_sha,
                    dispatch_event_id="EVT-DISPATCH-EXPERT-CANCEL-A2",
                )
                dcr_a2 = DispatchCycleRequest(
                    dispatch_request=dr_a2,
                    dispatch_transition_request=dispatch_tr_a2,
                    acknowledge_transition_request=ack_tr_a2,
                    delivery_event_id="EVT-DELIVERY-EXPERT-CANCEL-A2",
                    delivery_event_context=TransitionEventContext(
                        source_message_id=None, evidence_refs=(), guard_results=(),
                    ),
                    provider_cli_version="2.1.214",
                    worker_kind=WorkerKind.STANDARD_AGENT,
                    task_difficulty=TaskDifficulty.BASIC,
                    holder_instance_id="e2e-expert-cancel-instance",
                )
                audit_a2 = MadAuditGatewayInput(
                    project_root=project_root, task_id=task_id,
                    dispatch_id=dispatch_id_a2,
                    question="Audit the E2E expert cancellation delivery (Standard).",
                    workspace=project_root, task_card_commit=task_card_commit,
                    task_card_path=task_card_path_rel,
                    delivery_report_path=report_path_rel,
                    report_commit=report_a2, base_commit=task_card_commit,
                    implementation_commit=impl_a2,
                    depth=MadDeliberationDepth.BALANCED,
                )
                acceptance_path_a2 = (
                    f"docs/pm/acceptances/{task_id}-r{revision}"
                    f"-a{attempt_a2}-review1.md"
                )
                acceptance_tr_a2 = _make_acceptance_transition_request(
                    task_id=task_id, dispatch_id=dispatch_id_a2, revision=revision,
                    attempt=attempt_a2, accepted_commit=impl_a2,
                    acceptance_path=acceptance_path_a2, head_sha=head_sha,
                    event_id="EVT-ACCEPT-EXPERT-CANCEL-A2",
                )
                integration_tr_a2 = _make_integration_transition_request(
                    task_id=task_id, revision=revision,
                    integrated_commit=impl_a2, head_sha=head_sha,
                    event_id="EVT-INTEGRATE-EXPERT-CANCEL-A2",
                )
                block_tr_a2 = _make_block_transition_request(
                    task_id=task_id, revision=revision, head_sha=head_sha,
                    event_id="EVT-BLOCK-EXPERT-CANCEL-A2",
                    blocked_reason="Standard worker cannot resolve compiled native dependencies",
                    unblock_condition="Escalate to Advanced or higher worker tier",
                )
                resolve_tr_a2 = _make_resolve_transition_request(
                    task_id=task_id, revision=revision, head_sha=head_sha,
                    event_id="EVT-RESOLVE-EXPERT-CANCEL-A2",
                )

                # -- Attempt 3 (Advanced) --
                dr_a3 = DispatchRequest(
                    identity=DispatchIdentity(
                        task_id=task_id, revision=revision,
                        attempt=attempt_a3, dispatch_id=dispatch_id_a3,
                    ),
                    workspace=project_root,
                    prompt="Fix dependencies and re-deliver in Advanced worker.",
                    model_selection=ms_advanced,
                    timeout_seconds=60,
                )
                dispatch_tr_a3 = _make_dispatch_transition_request(
                    task_id=task_id, dispatch_id=dispatch_id_a3, revision=revision,
                    model_selection=ms_advanced, task_card_path=task_card_path_rel,
                    task_card_commit=task_card_commit, base_commit=task_card_commit,
                    branch="feat/e2e-expert-cancel", report_path=report_path_rel,
                    outbox_message_id="MSG-EXPERT-CANCEL-OUTBOX-A3",
                    attempt=attempt_a3, head_sha=head_sha,
                    event_id="EVT-DISPATCH-EXPERT-CANCEL-A3",
                )
                ack_tr_a3 = _make_ack_transition_request(
                    task_id=task_id, dispatch_id=dispatch_id_a3, revision=revision,
                    attempt=attempt_a3, head_sha=head_sha,
                    dispatch_event_id="EVT-DISPATCH-EXPERT-CANCEL-A3",
                )
                dcr_a3 = DispatchCycleRequest(
                    dispatch_request=dr_a3,
                    dispatch_transition_request=dispatch_tr_a3,
                    acknowledge_transition_request=ack_tr_a3,
                    delivery_event_id="EVT-DELIVERY-EXPERT-CANCEL-A3",
                    delivery_event_context=TransitionEventContext(
                        source_message_id=None, evidence_refs=(), guard_results=(),
                    ),
                    provider_cli_version="2.1.214",
                    worker_kind=WorkerKind.ADVANCED_AGENT,
                    task_difficulty=TaskDifficulty.BASIC,
                    holder_instance_id="e2e-expert-cancel-instance",
                )
                audit_a3 = MadAuditGatewayInput(
                    project_root=project_root, task_id=task_id,
                    dispatch_id=dispatch_id_a3,
                    question="Audit the E2E expert cancellation delivery (Advanced).",
                    workspace=project_root, task_card_commit=task_card_commit,
                    task_card_path=task_card_path_rel,
                    delivery_report_path=report_path_rel,
                    report_commit=report_a3, base_commit=task_card_commit,
                    implementation_commit=impl_a3,
                    depth=MadDeliberationDepth.BALANCED,
                )
                acceptance_path_a3 = (
                    f"docs/pm/acceptances/{task_id}-r{revision}"
                    f"-a{attempt_a3}-review1.md"
                )
                acceptance_tr_a3 = _make_acceptance_transition_request(
                    task_id=task_id, dispatch_id=dispatch_id_a3, revision=revision,
                    attempt=attempt_a3, accepted_commit=impl_a3,
                    acceptance_path=acceptance_path_a3, head_sha=head_sha,
                    event_id="EVT-ACCEPT-EXPERT-CANCEL-A3",
                )
                integration_tr_a3 = _make_integration_transition_request(
                    task_id=task_id, revision=revision,
                    integrated_commit=impl_a3, head_sha=head_sha,
                    event_id="EVT-INTEGRATE-EXPERT-CANCEL-A3",
                )
                block_tr_a3 = _make_block_transition_request(
                    task_id=task_id, revision=revision, head_sha=head_sha,
                    event_id="EVT-BLOCK-EXPERT-CANCEL-A3",
                    blocked_reason="Advanced worker cannot resolve distributed cluster dependencies",
                    unblock_condition="Escalate to Expert or higher worker tier",
                )
                resolve_tr_a3 = _make_resolve_transition_request(
                    task_id=task_id, revision=revision, head_sha=head_sha,
                    event_id="EVT-RESOLVE-EXPERT-CANCEL-A3",
                )

                # -- Attempt 4 (Expert) --
                dr_a4 = DispatchRequest(
                    identity=DispatchIdentity(
                        task_id=task_id, revision=revision,
                        attempt=attempt_a4, dispatch_id=dispatch_id_a4,
                    ),
                    workspace=project_root,
                    prompt="Fix dependencies and re-deliver in Expert worker.",
                    model_selection=ms_expert,
                    timeout_seconds=60,
                )
                dispatch_tr_a4 = _make_dispatch_transition_request(
                    task_id=task_id, dispatch_id=dispatch_id_a4, revision=revision,
                    model_selection=ms_expert, task_card_path=task_card_path_rel,
                    task_card_commit=task_card_commit, base_commit=task_card_commit,
                    branch="feat/e2e-expert-cancel", report_path=report_path_rel,
                    outbox_message_id="MSG-EXPERT-CANCEL-OUTBOX-A4",
                    attempt=attempt_a4, head_sha=head_sha,
                    event_id="EVT-DISPATCH-EXPERT-CANCEL-A4",
                )
                ack_tr_a4 = _make_ack_transition_request(
                    task_id=task_id, dispatch_id=dispatch_id_a4, revision=revision,
                    attempt=attempt_a4, head_sha=head_sha,
                    dispatch_event_id="EVT-DISPATCH-EXPERT-CANCEL-A4",
                )
                dcr_a4 = DispatchCycleRequest(
                    dispatch_request=dr_a4,
                    dispatch_transition_request=dispatch_tr_a4,
                    acknowledge_transition_request=ack_tr_a4,
                    delivery_event_id="EVT-DELIVERY-EXPERT-CANCEL-A4",
                    delivery_event_context=TransitionEventContext(
                        source_message_id=None, evidence_refs=(), guard_results=(),
                    ),
                    provider_cli_version="2.1.214",
                    worker_kind=WorkerKind.EXPERT_AGENT,
                    task_difficulty=TaskDifficulty.BASIC,
                    holder_instance_id="e2e-expert-cancel-instance",
                )
                audit_a4 = MadAuditGatewayInput(
                    project_root=project_root, task_id=task_id,
                    dispatch_id=dispatch_id_a4,
                    question="Audit the E2E expert cancellation delivery (Expert).",
                    workspace=project_root, task_card_commit=task_card_commit,
                    task_card_path=task_card_path_rel,
                    delivery_report_path=report_path_rel,
                    report_commit=report_a4, base_commit=task_card_commit,
                    implementation_commit=impl_a4,
                    depth=MadDeliberationDepth.DEEP,
                )
                acceptance_path_a4 = (
                    f"docs/pm/acceptances/{task_id}-r{revision}"
                    f"-a{attempt_a4}-review1.md"
                )
                acceptance_tr_a4 = _make_acceptance_transition_request(
                    task_id=task_id, dispatch_id=dispatch_id_a4, revision=revision,
                    attempt=attempt_a4, accepted_commit=impl_a4,
                    acceptance_path=acceptance_path_a4, head_sha=head_sha,
                    event_id="EVT-ACCEPT-EXPERT-CANCEL-A4",
                )
                integration_tr_a4 = _make_integration_transition_request(
                    task_id=task_id, revision=revision,
                    integrated_commit=impl_a4, head_sha=head_sha,
                    event_id="EVT-INTEGRATE-EXPERT-CANCEL-A4",
                )
                block_tr_a4 = _make_block_transition_request(
                    task_id=task_id, revision=revision, head_sha=head_sha,
                    event_id="EVT-BLOCK-EXPERT-CANCEL-A4",
                    blocked_reason="Expert worker cannot resolve theoretical dependencies",
                    unblock_condition="User decision required — no higher tier available",
                    resume_state="draft",
                )

                # ═══════════════════════════════════════════════════════════
                # Execute all phases through Expert REQUEST_USER_DECISION
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
                    # ───────────────────────────────────────────────────────
                    # Phase A: Attempt 1 — Basic → dispatch → MAD blocked
                    # ───────────────────────────────────────────────────────

                    dispatch_result_a1 = await orch.run_dispatch_cycle(
                        dcr_a1, providers,
                    )
                    self.assertIsInstance(dispatch_result_a1, DispatchCycleResult)
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

                    acceptance_cycle_req_a1 = AcceptanceCycleRequest(
                        dispatch_cycle_result=dispatch_result_a1,
                        audit_input=audit_a1,
                        acceptance_transition_request=acceptance_tr_a1,
                        integration_transition_request=integration_tr_a1,
                        worker_kind=WorkerKind.BASIC_AGENT,
                        holder_instance_id="e2e-expert-cancel-instance",
                    )
                    acceptance_result_a1 = await orch.run_acceptance_cycle(
                        acceptance_cycle_req_a1, mad_config,
                    )
                    self.assertEqual(
                        acceptance_result_a1.audit_result.verdict, "blocked",
                    )
                    self.assertIsNone(acceptance_result_a1.accept_transition)
                    self.assertIsNone(acceptance_result_a1.integrate_transition)

                    blocked_audit_req_a1 = BlockedAuditRequest(
                        acceptance_cycle_result=acceptance_result_a1,
                        dispatch_cycle_result=dispatch_result_a1,
                        block_transition_request=block_tr_a1,
                        current_worker_kind=WorkerKind.BASIC_AGENT,
                    )
                    blocked_audit_result_a1 = await orch.run_blocked_audit_cycle(
                        blocked_audit_req_a1,
                    )
                    self.assertEqual(blocked_audit_result_a1.task_id, task_id)

                    from escalation_service import EscalationAction
                    ed_a1 = blocked_audit_result_a1.escalation_decision
                    self.assertEqual(ed_a1.action, EscalationAction.ESCALATE)
                    self.assertEqual(ed_a1.current_worker_kind, WorkerKind.BASIC_AGENT)
                    self.assertEqual(ed_a1.next_worker_kind, WorkerKind.STANDARD_AGENT)
                    self.assertEqual(
                        blocked_audit_result_a1.block_transition.to_state, "blocked",
                    )

                    # ── Phase A: Escalated redispatch Basic → Standard ─────
                    escalated_req_a1 = EscalatedRedispatchRequest(
                        blocked_audit_request=blocked_audit_req_a1,
                        blocked_audit_result=blocked_audit_result_a1,
                        resolve_transition_request=resolve_tr_a1,
                        next_dispatch_cycle_request=dcr_a2,
                    )
                    escalated_result_a1 = await orch.run_escalated_redispatch(
                        escalated_req_a1, providers,
                    )
                    self.assertEqual(
                        escalated_result_a1.resolve_transition.from_state, "blocked",
                    )
                    self.assertEqual(
                        escalated_result_a1.resolve_transition.to_state, "ready",
                    )

                    dcr_a2_result = escalated_result_a1.dispatch_cycle_result
                    self.assertEqual(
                        dcr_a2_result.delivery_transition.to_state, "review_ready",
                    )

                    receipt_a2 = dcr_a2_result.delivery_receipt
                    self.assertEqual(receipt_a2.identity.dispatch_id, dispatch_id_a2)
                    self.assertEqual(receipt_a2.identity.attempt, attempt_a2)

                    # ───────────────────────────────────────────────────────
                    # Phase B: Attempt 2 — Standard dispatch → MAD blocked
                    # ───────────────────────────────────────────────────────

                    acceptance_cycle_req_a2 = AcceptanceCycleRequest(
                        dispatch_cycle_result=dcr_a2_result,
                        audit_input=audit_a2,
                        acceptance_transition_request=acceptance_tr_a2,
                        integration_transition_request=integration_tr_a2,
                        worker_kind=WorkerKind.STANDARD_AGENT,
                        holder_instance_id="e2e-expert-cancel-instance",
                    )
                    acceptance_result_a2 = await orch.run_acceptance_cycle(
                        acceptance_cycle_req_a2, mad_config,
                    )
                    self.assertEqual(
                        acceptance_result_a2.audit_result.verdict, "blocked",
                    )
                    self.assertIsNone(acceptance_result_a2.accept_transition)
                    self.assertIsNone(acceptance_result_a2.integrate_transition)

                    blocked_audit_req_a2 = BlockedAuditRequest(
                        acceptance_cycle_result=acceptance_result_a2,
                        dispatch_cycle_result=dcr_a2_result,
                        block_transition_request=block_tr_a2,
                        current_worker_kind=WorkerKind.STANDARD_AGENT,
                    )
                    blocked_audit_result_a2 = await orch.run_blocked_audit_cycle(
                        blocked_audit_req_a2,
                    )
                    ed_a2 = blocked_audit_result_a2.escalation_decision
                    self.assertEqual(ed_a2.action, EscalationAction.ESCALATE)
                    self.assertEqual(ed_a2.current_worker_kind, WorkerKind.STANDARD_AGENT)
                    self.assertEqual(ed_a2.next_worker_kind, WorkerKind.ADVANCED_AGENT)

                    # ── Phase B: Escalated redispatch Standard → Advanced ──
                    escalated_req_a2 = EscalatedRedispatchRequest(
                        blocked_audit_request=blocked_audit_req_a2,
                        blocked_audit_result=blocked_audit_result_a2,
                        resolve_transition_request=resolve_tr_a2,
                        next_dispatch_cycle_request=dcr_a3,
                    )
                    escalated_result_a2 = await orch.run_escalated_redispatch(
                        escalated_req_a2, providers,
                    )
                    self.assertEqual(
                        escalated_result_a2.resolve_transition.to_state, "ready",
                    )

                    dcr_a3_result = escalated_result_a2.dispatch_cycle_result
                    self.assertEqual(
                        dcr_a3_result.delivery_transition.to_state, "review_ready",
                    )

                    receipt_a3 = dcr_a3_result.delivery_receipt
                    self.assertEqual(receipt_a3.identity.dispatch_id, dispatch_id_a3)
                    self.assertEqual(receipt_a3.identity.attempt, attempt_a3)

                    # ───────────────────────────────────────────────────────
                    # Phase C: Attempt 3 — Advanced dispatch → MAD blocked
                    # ───────────────────────────────────────────────────────

                    acceptance_cycle_req_a3 = AcceptanceCycleRequest(
                        dispatch_cycle_result=dcr_a3_result,
                        audit_input=audit_a3,
                        acceptance_transition_request=acceptance_tr_a3,
                        integration_transition_request=integration_tr_a3,
                        worker_kind=WorkerKind.ADVANCED_AGENT,
                        holder_instance_id="e2e-expert-cancel-instance",
                    )
                    acceptance_result_a3 = await orch.run_acceptance_cycle(
                        acceptance_cycle_req_a3, mad_config,
                    )
                    self.assertEqual(
                        acceptance_result_a3.audit_result.verdict, "blocked",
                    )
                    self.assertIsNone(acceptance_result_a3.accept_transition)
                    self.assertIsNone(acceptance_result_a3.integrate_transition)

                    blocked_audit_req_a3 = BlockedAuditRequest(
                        acceptance_cycle_result=acceptance_result_a3,
                        dispatch_cycle_result=dcr_a3_result,
                        block_transition_request=block_tr_a3,
                        current_worker_kind=WorkerKind.ADVANCED_AGENT,
                    )
                    blocked_audit_result_a3 = await orch.run_blocked_audit_cycle(
                        blocked_audit_req_a3,
                    )
                    ed_a3 = blocked_audit_result_a3.escalation_decision
                    self.assertEqual(ed_a3.action, EscalationAction.ESCALATE)
                    self.assertEqual(ed_a3.current_worker_kind, WorkerKind.ADVANCED_AGENT)
                    self.assertEqual(ed_a3.next_worker_kind, WorkerKind.EXPERT_AGENT)

                    # ── Phase C: Escalated redispatch Advanced → Expert ───
                    escalated_req_a3 = EscalatedRedispatchRequest(
                        blocked_audit_request=blocked_audit_req_a3,
                        blocked_audit_result=blocked_audit_result_a3,
                        resolve_transition_request=resolve_tr_a3,
                        next_dispatch_cycle_request=dcr_a4,
                    )
                    escalated_result_a3 = await orch.run_escalated_redispatch(
                        escalated_req_a3, providers,
                    )
                    self.assertEqual(
                        escalated_result_a3.resolve_transition.to_state, "ready",
                    )

                    dcr_a4_result = escalated_result_a3.dispatch_cycle_result
                    self.assertEqual(
                        dcr_a4_result.delivery_transition.to_state, "review_ready",
                    )

                    receipt_a4 = dcr_a4_result.delivery_receipt
                    self.assertEqual(receipt_a4.identity.dispatch_id, dispatch_id_a4)
                    self.assertEqual(receipt_a4.identity.attempt, attempt_a4)

                    # ───────────────────────────────────────────────────────
                    # Phase D: Attempt 4 — Expert → REQUEST_USER_DECISION
                    # ───────────────────────────────────────────────────────

                    acceptance_cycle_req_a4 = AcceptanceCycleRequest(
                        dispatch_cycle_result=dcr_a4_result,
                        audit_input=audit_a4,
                        acceptance_transition_request=acceptance_tr_a4,
                        integration_transition_request=integration_tr_a4,
                        worker_kind=WorkerKind.EXPERT_AGENT,
                        holder_instance_id="e2e-expert-cancel-instance",
                    )
                    acceptance_result_a4 = await orch.run_acceptance_cycle(
                        acceptance_cycle_req_a4, mad_config,
                    )
                    self.assertEqual(
                        acceptance_result_a4.audit_result.verdict, "blocked",
                    )
                    self.assertIsNone(acceptance_result_a4.accept_transition)
                    self.assertIsNone(acceptance_result_a4.integrate_transition)

                    blocked_audit_req_a4 = BlockedAuditRequest(
                        acceptance_cycle_result=acceptance_result_a4,
                        dispatch_cycle_result=dcr_a4_result,
                        block_transition_request=block_tr_a4,
                        current_worker_kind=WorkerKind.EXPERT_AGENT,
                    )
                    blocked_audit_result_a4 = await orch.run_blocked_audit_cycle(
                        blocked_audit_req_a4,
                    )

                    # ── Expert escalation: REQUEST_USER_DECISION ──────────
                    ed_a4 = blocked_audit_result_a4.escalation_decision
                    self.assertEqual(ed_a4.action, EscalationAction.REQUEST_USER_DECISION)
                    self.assertEqual(ed_a4.current_worker_kind, WorkerKind.EXPERT_AGENT)
                    self.assertIsNone(ed_a4.next_worker_kind,
                                      "Expert next_worker_kind must be None")
                    self.assertEqual(
                        blocked_audit_result_a4.block_transition.to_state, "blocked",
                    )

                # ── Subprocess count = 8 (4 Claude + 4 MAD) ─────────────────
                self.assertEqual(len(subprocess_calls), 8,
                                 "Total subprocess calls must be exactly 8")

                # ── Exact call order ───────────────────────────────────────
                all_exes = [c[0][0] for c in subprocess_calls]
                expected_order = [
                    executed_claude_exe,  # Claude Basic
                    executed_mad_exe,     # MAD blocked Basic
                    executed_claude_exe,  # Claude Standard
                    executed_mad_exe,     # MAD blocked Standard
                    executed_claude_exe,  # Claude Advanced
                    executed_mad_exe,     # MAD blocked Advanced
                    executed_claude_exe,  # Claude Expert
                    executed_mad_exe,     # MAD blocked Expert
                ]
                self.assertEqual(all_exes, expected_order)

                # ═════════════════════════════════════════════════════════
                # Phase E: Expert pre-rescope state verification
                # ═════════════════════════════════════════════════════════

                snapshot_pre = StateProvider(project_root).snapshot()
                tasks_pre = [t for t in snapshot_pre.tasks if t.task_id == task_id]
                self.assertEqual(len(tasks_pre), 1)
                task_pre = tasks_pre[0]

                self.assertEqual(task_pre.state, "blocked")
                self.assertEqual(task_pre.revision, 1)
                self.assertIsNone(task_pre.current_dispatch)
                self.assertEqual(task_pre.blocked_attempt_valid, False)
                self.assertIsNone(task_pre.accepted_commit)
                self.assertIsNone(task_pre.acceptance_path)
                self.assertIsNone(task_pre.integrated_commit)

                # ═════════════════════════════════════════════════════════
                # Phase F: Illegal rescope request — fail-closed,
                #          zero writes
                # ═════════════════════════════════════════════════════════

                # Capture pre-failure canonical state
                tasks_yaml_path = project_root / "docs" / "pm" / "state" / "tasks.yaml"
                tasks_yaml_before = tasks_yaml_path.read_bytes()
                events_dir = project_root / "docs" / "pm" / "events"
                events_before = sorted(
                    p.name for p in events_dir.iterdir() if p.is_file()
                )
                subprocess_count_before = len(subprocess_calls)

                import worker_slot_lease as wsl
                lease_store_before = wsl.read_worker_slot_leases(project_root)
                leases_before = dict(lease_store_before.get("leases", {}))

                # Illegal 1: event_type is not BLOCKER_RESCOPED
                illegal_rescope_cas = TransitionCAS(
                    task_id=task_id,
                    expected_revision=revision,
                    expected_state="blocked",
                    expected_snapshot_commit=head_sha,
                )
                illegal_rescope_tr = TransitionRequest(
                    cas=illegal_rescope_cas,
                    dispatch_cas=None,
                    event_id="EVT-RESCOPE-ILLEGAL-1",
                    event_type="BLOCKER_CANCELLED",  # ← wrong event type
                    payload=BlockerCancelledPayload(),
                    event_context=TransitionEventContext(
                        source_message_id=None,
                        evidence_refs=(),
                        guard_results=(),
                    ),
                )
                illegal_req1 = BlockedRescopeRequest(
                    blocked_audit_request=blocked_audit_req_a4,
                    blocked_audit_result=blocked_audit_result_a4,
                    rescope_transition_request=illegal_rescope_tr,
                )

                with self.assertRaises(WorkflowInputError):
                    await orch.record_blocked_rescope(illegal_req1)

                # Zero writes
                self.assertEqual(
                    tasks_yaml_path.read_bytes(), tasks_yaml_before,
                    "tasks.yaml must be unchanged after illegal rescope request",
                )
                self.assertEqual(
                    sorted(p.name for p in events_dir.iterdir() if p.is_file()),
                    events_before,
                    "events must be unchanged after illegal rescope request",
                )
                self.assertEqual(
                    len(subprocess_calls), subprocess_count_before,
                    "subprocess count must be unchanged",
                )
                lease_store_after = wsl.read_worker_slot_leases(project_root)
                leases_after = dict(lease_store_after.get("leases", {}))
                self.assertEqual(
                    leases_after, leases_before,
                    "leases must be unchanged",
                )

                # Illegal 2: new_revision is N (not N+1)
                illegal_rescope_cas2 = TransitionCAS(
                    task_id=task_id,
                    expected_revision=revision,
                    expected_state="blocked",
                    expected_snapshot_commit=head_sha,
                )
                illegal_rescope_tr2 = TransitionRequest(
                    cas=illegal_rescope_cas2,
                    dispatch_cas=None,
                    event_id="EVT-RESCOPE-ILLEGAL-2",
                    event_type="BLOCKER_RESCOPED",
                    payload=BlockerRescopedPayload(new_revision=1),  # ← should be 2
                    event_context=TransitionEventContext(
                        source_message_id=None,
                        evidence_refs=(),
                        guard_results=(),
                    ),
                )
                illegal_req2 = BlockedRescopeRequest(
                    blocked_audit_request=blocked_audit_req_a4,
                    blocked_audit_result=blocked_audit_result_a4,
                    rescope_transition_request=illegal_rescope_tr2,
                )

                with self.assertRaises(WorkflowInputError):
                    await orch.record_blocked_rescope(illegal_req2)

                # Still zero new subprocess calls
                self.assertEqual(
                    len(subprocess_calls), subprocess_count_before,
                    "subprocess count must be unchanged after second illegal request",
                )

                # ═════════════════════════════════════════════════════════
                # Phase G: Legal rescope request
                # ═════════════════════════════════════════════════════════

                rescope_event_id = "EVT-RESCOPE-EXPERT-E2E"
                rescope_cas = TransitionCAS(
                    task_id=task_id,
                    expected_revision=revision,
                    expected_state="blocked",
                    expected_snapshot_commit=head_sha,
                )
                rescope_tr = TransitionRequest(
                    cas=rescope_cas,
                    dispatch_cas=None,
                    event_id=rescope_event_id,
                    event_type="BLOCKER_RESCOPED",
                    payload=BlockerRescopedPayload(new_revision=revision + 1),
                    event_context=TransitionEventContext(
                        source_message_id=None,
                        evidence_refs=(),
                        guard_results=(),
                    ),
                )

                rescope_req = BlockedRescopeRequest(
                    blocked_audit_request=blocked_audit_req_a4,
                    blocked_audit_result=blocked_audit_result_a4,
                    rescope_transition_request=rescope_tr,
                )

                rescope_result = await orch.record_blocked_rescope(rescope_req)

                # Result assertions
                self.assertIsInstance(rescope_result, BlockedRescopeResult)
                self.assertEqual(rescope_result.task_id, task_id)
                self.assertEqual(rescope_result.previous_revision, 1)
                self.assertEqual(rescope_result.new_revision, 2)
                self.assertIsInstance(rescope_result.rescope_transition, TransitionResult)
                self.assertEqual(
                    rescope_result.rescope_transition.from_state, "blocked",
                )
                self.assertEqual(
                    rescope_result.rescope_transition.to_state, "draft",
                )

                # No additional subprocess calls
                self.assertEqual(
                    len(subprocess_calls), 8,
                    "Subprocess calls must remain exactly 8 after legal rescope",
                )

                # ═════════════════════════════════════════════════════════
                # Phase H: Final state verification
                # ═════════════════════════════════════════════════════════

                snapshot = StateProvider(project_root).snapshot()
                self.assertIsInstance(snapshot, StateSnapshot)

                tasks = [t for t in snapshot.tasks if t.task_id == task_id]
                self.assertEqual(len(tasks), 1)
                task = tasks[0]

                # ── Final state must be draft ───────────────────────────
                self.assertEqual(task.state, "draft")

                # ── Revision must be incremented ────────────────────────
                self.assertEqual(task.revision, 2)

                # ── current_dispatch must be None ───────────────────────
                self.assertIsNone(task.current_dispatch)

                # ── Blocked fields must be cleared ──────────────────────
                self.assertIsNone(task.blocked_reason)
                self.assertIsNone(task.blocked_kind)
                self.assertIsNone(task.blocked_owner)
                self.assertIsNone(task.unblock_condition)
                self.assertIsNone(task.resume_state)
                self.assertIsNone(task.blocked_attempt_valid)
                self.assertIsNone(task.review_after)

                # ── Delivery fields must be cleared ─────────────────────
                self.assertIsNone(task.delivery_state)
                self.assertIsNone(task.implementation_commit)
                self.assertIsNone(task.report_commit)
                self.assertIsNone(task.accepted_commit)
                self.assertIsNone(task.acceptance_path)
                self.assertIsNone(task.report_path)

                # ── No integration ──────────────────────────────────────
                self.assertIsNone(task.integrated_commit)

                # ── No cancelled_at ─────────────────────────────────────
                self.assertIsNone(task.timestamps.cancelled_at)
                self.assertIsNone(task.timestamps.superseded_at)

                # ═════════════════════════════════════════════════════════
                # Event sequence assertions
                # ═════════════════════════════════════════════════════════

                task_events_sorted = sorted(
                    [e for e in snapshot.events if e.task_id == task_id],
                    key=lambda e: e.occurred_at,
                )
                observed_types = [e.event_type for e in task_events_sorted]

                def count_event(event_type: str) -> int:
                    return sum(1 for e in task_events_sorted if e.event_type == event_type)

                # Expected counts
                self.assertEqual(count_event("TASK_DISPATCHED"), 4)
                self.assertEqual(count_event("DISPATCH_ACKNOWLEDGED"), 4)
                self.assertEqual(count_event("DELIVERY_SUBMITTED"), 4)
                self.assertEqual(count_event("TASK_BLOCKED"), 4)
                self.assertEqual(count_event("BLOCKER_RESOLVED"), 3)
                self.assertEqual(count_event("BLOCKER_RESCOPED"), 1)

                # Zero forbidden events
                self.assertEqual(count_event("BLOCKER_CANCELLED"), 0)
                self.assertEqual(count_event("TASK_CANCELLED"), 0)
                self.assertEqual(count_event("TASK_SUPERSEDED"), 0)
                self.assertEqual(count_event("DELIVERY_ACCEPTED"), 0)
                self.assertEqual(count_event("CHANGE_INTEGRATED"), 0)
                self.assertEqual(count_event("DELIVERY_RETURNED"), 0)
                self.assertEqual(count_event("TASK_REQUEUED"), 0)

                # Total events = 4+4+4+4+3+1 = 20
                self.assertEqual(len(task_events_sorted), 20)

                # Verify business order
                expected_sequence = [
                    "TASK_DISPATCHED",       # A1 (Basic)
                    "DISPATCH_ACKNOWLEDGED",
                    "DELIVERY_SUBMITTED",
                    "TASK_BLOCKED",
                    "BLOCKER_RESOLVED",
                    "TASK_DISPATCHED",       # A2 (Standard)
                    "DISPATCH_ACKNOWLEDGED",
                    "DELIVERY_SUBMITTED",
                    "TASK_BLOCKED",
                    "BLOCKER_RESOLVED",
                    "TASK_DISPATCHED",       # A3 (Advanced)
                    "DISPATCH_ACKNOWLEDGED",
                    "DELIVERY_SUBMITTED",
                    "TASK_BLOCKED",
                    "BLOCKER_RESOLVED",
                    "TASK_DISPATCHED",       # A4 (Expert)
                    "DISPATCH_ACKNOWLEDGED",
                    "DELIVERY_SUBMITTED",
                    "TASK_BLOCKED",
                    "BLOCKER_RESCOPED",      # Terminal rescope
                ]

                self.assertEqual(observed_types, expected_sequence)

                # BLOCKER_RESCOPED event properties
                rescope_events = [e for e in task_events_sorted if e.event_type == "BLOCKER_RESCOPED"]
                self.assertEqual(len(rescope_events), 1)
                re_ev = rescope_events[0]
                self.assertEqual(re_ev.from_state, "blocked")
                self.assertEqual(re_ev.to_state, "draft")
                self.assertEqual(re_ev.revision, 1)  # records OLD revision
                self.assertIsNone(re_ev.dispatch_id)

                # ── No attempt 5 ───────────────────────────────────────
                attempt_values = set()
                for ev in task_events_sorted:
                    if ev.attempt is not None:
                        attempt_values.add(ev.attempt)
                self.assertEqual(attempt_values, {1, 2, 3, 4})
                self.assertNotIn(5, attempt_values)

                # ═════════════════════════════════════════════════════════
                # Identity isolation
                # ═════════════════════════════════════════════════════════

                all_dispatch_ids = {dispatch_id_a1, dispatch_id_a2, dispatch_id_a3, dispatch_id_a4}
                self.assertEqual(len(all_dispatch_ids), 4)

                # ═════════════════════════════════════════════════════════
                # Guard assertions: BLOCKER_RESCOPED has no approval_gate
                # ═════════════════════════════════════════════════════════

                guard_names = {gr.guard for gr in re_ev.guard_results}
                self.assertNotIn("approval_gate", guard_names)

                # ═════════════════════════════════════════════════════════
                # Worker slot cleanup
                # ═════════════════════════════════════════════════════════

                store = wsl.read_worker_slot_leases(project_root)
                self.assertEqual(len(store.get("leases", {})), 0,
                                 "All worker slot leases must be released")

                # ═════════════════════════════════════════════════════════
                # No .tmp-* files, no Git remote, cwd unchanged
                # ═════════════════════════════════════════════════════════

                tmp_files = list(project_root.rglob(".tmp-*"))
                self.assertEqual(len(tmp_files), 0)

                remotes_result = subprocess.run(
                    ["git", "-C", str(project_root), "remote"],
                    capture_output=True, text=True, timeout=10,
                )
                self.assertEqual(remotes_result.stdout.strip(), "")

                self.assertEqual(os.getcwd(), cwd_before)

                try:
                    loop = asyncio.get_running_loop()
                except RuntimeError:
                    loop = None
                if loop is not None:
                    all_tasks = asyncio.all_tasks(loop)
                    self.assertLessEqual(len(all_tasks), 3)

                # ═════════════════════════════════════════════════════════
                # No orphan entries
                # ═════════════════════════════════════════════════════════

                task_ids_in_snapshot = {t.task_id for t in snapshot.tasks}
                for ev in snapshot.events:
                    self.assertIn(ev.task_id, task_ids_in_snapshot)
                for ob in snapshot.outbox:
                    self.assertIn(ob.task_id, task_ids_in_snapshot)
                for acc in snapshot.acceptances:
                    self.assertIn(acc.task_id, task_ids_in_snapshot)

                # ═════════════════════════════════════════════════════════
                # Mad-ref assertions
                # ═════════════════════════════════════════════════════════

                self.assertIsNotNone(snapshot.mad_refs)
                audit_refs = [r for r in snapshot.mad_refs if r.purpose == "audit"]
                self.assertGreaterEqual(len(audit_refs), 4)
                audit_ref_dispatch_ids = {r.dispatch_id for r in audit_refs}
                for did in (dispatch_id_a1, dispatch_id_a2, dispatch_id_a3, dispatch_id_a4):
                    self.assertIn(did, audit_ref_dispatch_ids)

                # ═════════════════════════════════════════════════════════
                # Historical events preserved
                # ═════════════════════════════════════════════════════════

                self.assertEqual(count_event("TASK_DISPATCHED"), 4)
                self.assertEqual(count_event("DELIVERY_SUBMITTED"), 4)
                self.assertEqual(count_event("TASK_BLOCKED"), 4)
                self.assertEqual(count_event("BLOCKER_RESOLVED"), 3)

                # ═════════════════════════════════════════════════════════
                # Payload digest consistency for TASK_DISPATCHED
                # ═════════════════════════════════════════════════════════

                for ev in snapshot.events:
                    if ev.event_type == "TASK_DISPATCHED" and ev.task_id == task_id:
                        self.assertIsNotNone(ev.payload_digest)

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
