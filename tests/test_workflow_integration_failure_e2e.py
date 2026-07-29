"""TC-13.19e — Accepted-Delivery External Integration Failure Fail-Closed E2E.

Single comprehensive test that validates the fail-closed blocking loop after
an external integration failure observed post-acceptance:

    ready → TASK_DISPATCHED → DISPATCH_ACKNOWLEDGED → Worker completes
    → DELIVERY_SUBMITTED → MAD audit pass → DELIVERY_ACCEPTED
    → (external Git integration fails)
    → INTEGRATION_FAILED → blocked

Uses real production modules.  Only mocks ``asyncio.create_subprocess_exec``
(single router for both Claude and MAD subprocess boundaries) and the clock.

Also validates that an illegal IntegrationFailureRequest is rejected with
zero writes (fail-closed).
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
from unittest import mock

_SCRIPTS_DIR = str(
    Path(__file__).resolve().parents[1] / "skills" / "agentdesk" / "scripts"
)
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from context_budget import BudgetResult
from control_plane_transition import (
    AcknowledgePayload,
    BlockedPayload,
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
    AgentCliInvocation,
    DispatchIdentity,
    DispatchRequest,
    DispatchResult,
    DispatchStarted,
    DispatchStartedObserver,
)
from worker_adapter import WorkerResult
from worker_output_decoder import (
    DeliveryReceipt,
    WorkerCompletionStatus,
    WorkerOutput,
    decode_worker_result,
    require_delivery_receipt,
)
from worker_slot_lease import (
    WorkerSlotLease,
    acquire_worker_slot,
    release_worker_slot,
    renew_worker_slot,
)
from claude_code_provider import ClaudeCodeProvider
from approval_gate import (
    ApprovalGate,
    ApprovalScope,
    ApprovalSubject,
    write_grant,
)
from mad_gateway import MadGatewayConfig
from mad_audit_gateway import (
    MadAuditGatewayInput,
    MadAuditGatewayResult,
    MadAuditIssue,
    MadAuditIssueLocation,
    MadAuditEvidence,
    MadAuditPlan,
    run_audit_gateway,
)
from state_provider import StateProvider, StateSnapshot
from workflow_orchestrator import (
    AcceptanceCycleRequest,
    AcceptanceCycleResult,
    DeliveryReceipt as OrcDeliveryReceipt,
    DispatchCycleRequest,
    DispatchCycleResult,
    IntegrationFailureRequest,
    IntegrationFailureResult,
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


# ── MAD audit stdout builder ─────────────────────────────────────────────────


def _build_mad_audit_stdout(archive_path: str, depth_value: str = "balanced") -> bytes:
    """Build a valid ``mad.audit-result/v1`` JSON with exactly 11 root keys."""
    result = {
        "schema_version": "mad.audit-result/v1",
        "deliberation_id": "DELIB-e2e-001",
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
            "branch": "feat/e2e-test",
            "dispatched_at": "2026-07-29T12:00:00Z",
            "model_selection": {
                "required_model_tier": "advanced",
                "required_model_capabilities": [],
                "model_binding_id": "binding-001",
                "selected_model_provider": "claude",
                "selected_model_id": "claude-sonnet-4-5",
                "selected_model_tier": "advanced",
                "selected_deliberation_tier": "balanced",
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
) -> None:
    write_grant(
        project_root=project_root,
        approval_id="APR-DISPATCH-001",
        event_id="EVT-APR-DISPATCH-001",
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
        reason="E2E dispatch approval",
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
) -> None:
    write_grant(
        project_root=project_root,
        approval_id="APR-ACCEPT-001",
        event_id="EVT-APR-ACCEPT-001",
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
        reason="E2E accept approval",
        expires_at=None,
        expected_snapshot_commit=head_sha,
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
    event_id: str = "EVT-DISPATCH-001",
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
    dispatch_event_id: str = "EVT-DISPATCH-001",
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
    event_id: str = "EVT-ACCEPT-001",
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
        criteria_evidence=("E2E acceptance criteria satisfied",),
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


# ── Model selection helper ───────────────────────────────────────────────────


def _make_model_selection() -> Any:
    """Build a ModelSelectionSnapshot that DispatchPayload can reference."""
    from dispatcher_gateway import ModelSelectionSnapshot as DPModelSelection
    return DPModelSelection(
        required_model_tier="advanced",
        required_model_capabilities=(),
        model_binding_id="binding-001",
        selected_model_provider="claude",
        selected_model_id="claude-sonnet-4-5",
        selected_model_tier="advanced",
        selected_deliberation_tier="balanced",
        selected_context_window_tokens=200000,
        selected_model_capabilities=(),
        model_degradation_approval_id=None,
    )


# ── Integration failure helpers ──────────────────────────────────────────────


def _make_failure_transition_request(
    task_id: str,
    revision: int,
    head_sha: str,
    *,
    event_id: str = "EVT-INTFAIL-E2E",
    blocked_kind: str = "integration_conflict",
    blocked_reason: str = "external git merge conflict detected",
    blocked_owner: str = "PM",
    unblock_condition: str = "manual conflict resolution and re-integration",
    resume_state: str = "accepted",
    blocked_attempt_valid: bool = True,
) -> TransitionRequest:
    """Build a TransitionRequest for INTEGRATION_FAILED."""
    cas = TransitionCAS(
        task_id=task_id,
        expected_revision=revision,
        expected_state="accepted",
        expected_snapshot_commit=head_sha,
    )
    payload = BlockedPayload(
        blocked_reason=blocked_reason,
        blocked_kind=blocked_kind,
        blocked_owner=blocked_owner,
        unblock_condition=unblock_condition,
        resume_state=resume_state,
        blocked_attempt_valid=blocked_attempt_valid,
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
        event_type="INTEGRATION_FAILED",
        payload=payload,
        event_context=event_context,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# E2E Test
# ═══════════════════════════════════════════════════════════════════════════════


class WorkflowIntegrationFailureE2ETests(unittest.IsolatedAsyncioTestCase):
    """TC-13.19e: Accepted-delivery external integration failure fail-closed E2E.

    Validates:
    * Happy-path dispatch → acceptance (no integration request)
    * Illegal IntegrationFailureRequest → fail-closed, zero writes
    * Legal IntegrationFailureRequest → INTEGRATION_FAILED → blocked
    * Final state preservation and event order
    """

    async def test_accepted_delivery_records_external_integration_failure_fail_closed(
        self,
    ) -> None:
        # ── 0. Capture pre-test state ──────────────────────────────────────
        source_repo = _SOURCE_REPO
        source_status_before = _git_status_bytes(source_repo)
        cwd_before = os.getcwd()

        # ── 1. Create fake executables in a temp dir ────────────────────────
        exe_tmp = Path(tempfile.mkdtemp(prefix="e2e-exes-"))
        fake_claude_exe = exe_tmp / "fake-claude.exe"
        fake_mad_exe = exe_tmp / "fake-mad.exe"

        fake_claude_exe.write_bytes(b"")
        fake_mad_exe.write_bytes(b"")

        try:
            # ── 2. Create temp project with git repo ────────────────────────
            project_root = Path(tempfile.mkdtemp(prefix="e2e-project-"))

            try:
                _git_init(project_root)
                _git_add_all_and_commit(project_root, "initial empty commit")

                _init_canonical_dirs(project_root)
                _init_worker_slot_store(project_root)

                # ── 3. Create task card ────────────────────────────────────
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
                    "# TC-001 · E2E Test Task\n\n"
                    "## Goal\n\n"
                    "Implement the E2E test fixture.\n\n"
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

                # ── 4. Create implementation file and commit ────────────────
                impl_file = project_root / "src" / "impl.py"
                impl_file.parent.mkdir(parents=True, exist_ok=True)
                impl_file.write_text("# E2E implementation\nprint('hello e2e')\n", encoding="utf-8")
                implementation_commit = _git_add_all_and_commit(project_root, "implementation")

                # ── 5. Create delivery report and commit ────────────────────
                report_path_rel = "reports/report.md"
                report_file = project_root / report_path_rel
                report_file.write_text(
                    "# Delivery Report\n\n"
                    "## Summary\n\n"
                    "E2E delivery completed.\n\n"
                    f"## Implementation Commit\n\n{implementation_commit}\n",
                    encoding="utf-8",
                )
                report_commit = _git_add_all_and_commit(project_root, "delivery report")

                # ── 6. Write initial tasks.yaml (ready state) ──────────────
                _write_initial_tasks_yaml(
                    project_root,
                    task_id="TC-001",
                    state="ready",
                    revision=1,
                    attempt=None,
                    task_card_path=task_card_path_rel,
                    task_card_commit=task_card_commit,
                    base_commit=task_card_commit,
                )

                head_sha = _git_add_all_and_commit(project_root, "canonical state — ready")

                # Re-write with correct SHAs
                _write_initial_tasks_yaml(
                    project_root,
                    task_id="TC-001",
                    state="ready",
                    revision=1,
                    attempt=None,
                    task_card_path=task_card_path_rel,
                    task_card_commit=task_card_commit,
                    base_commit=task_card_commit,
                )
                head_sha = _git_add_all_and_commit(project_root, "canonical state — ready (corrected)")

                # ── 7. Write approval grants (dispatch + accept ONLY) ──────
                now_utc = _utc("2026-07-29T12:00:00Z")
                task_id = "TC-001"
                dispatch_id = "DSP-E2E-001"
                revision = 1
                attempt = 1

                _write_dispatch_grant(
                    project_root, task_id, revision, attempt,
                    dispatch_id, head_sha, now_utc,
                )
                _write_accept_grant(
                    project_root, task_id, revision, attempt,
                    dispatch_id, head_sha, now_utc,
                )
                # ⚠ No integrate grant — integration is NOT requested

                head_sha = _git_add_all_and_commit(project_root, "approval grants")

                # ── 8. Build orchestrator and providers ────────────────────
                clock = FakeClock()
                orch = WorkflowOrchestrator(
                    project_root=project_root,
                    clock=clock,
                    heartbeat_interval_seconds=10.0,
                )

                ms = _make_model_selection()
                providers: Mapping[str, AgentCliProvider] = {
                    "claude": ClaudeCodeProvider(
                        provider_id="claude",
                        executable=str(fake_claude_exe),
                        permission_mode="default",
                        allowed_tools=(),
                        disallowed_tools=(),
                    ),
                }

                # ── 9. Build dispatch requests ────────────────────────────
                dr = DispatchRequest(
                    identity=DispatchIdentity(
                        task_id=task_id,
                        revision=revision,
                        attempt=attempt,
                        dispatch_id=dispatch_id,
                    ),
                    workspace=project_root,
                    prompt="Implement the E2E task.",
                    model_selection=ms,
                    timeout_seconds=60,
                )

                dispatch_tr = _make_dispatch_transition_request(
                    task_id=task_id,
                    dispatch_id=dispatch_id,
                    revision=revision,
                    model_selection=ms,
                    task_card_path=task_card_path_rel,
                    task_card_commit=task_card_commit,
                    base_commit=task_card_commit,
                    branch="feat/e2e-test",
                    report_path=report_path_rel,
                    outbox_message_id="MSG-E2E-OUTBOX-001",
                    attempt=attempt,
                    head_sha=head_sha,
                    event_id="EVT-DISPATCH-E2E",
                )

                ack_tr = _make_ack_transition_request(
                    task_id=task_id,
                    dispatch_id=dispatch_id,
                    revision=revision,
                    attempt=attempt,
                    head_sha=head_sha,
                    dispatch_event_id="EVT-DISPATCH-E2E",
                )

                delivery_event_id = "EVT-DELIVERY-E2E"
                delivery_event_context = TransitionEventContext(
                    source_message_id=None,
                    evidence_refs=(),
                    guard_results=(),
                )

                dispatch_cycle_req = DispatchCycleRequest(
                    dispatch_request=dr,
                    dispatch_transition_request=dispatch_tr,
                    acknowledge_transition_request=ack_tr,
                    delivery_event_id=delivery_event_id,
                    delivery_event_context=delivery_event_context,
                    provider_cli_version="2.1.214",
                    worker_kind=WorkerKind.ADVANCED_AGENT,
                    task_difficulty=TaskDifficulty.ADVANCED,
                    holder_instance_id="e2e-instance",
                )

                # ── 10. Build acceptance cycle WITHOUT integration request ──
                audit_input = MadAuditGatewayInput(
                    project_root=project_root,
                    task_id=task_id,
                    dispatch_id=dispatch_id,
                    question="Audit the E2E delivery.",
                    workspace=project_root,
                    task_card_commit=task_card_commit,
                    task_card_path=task_card_path_rel,
                    delivery_report_path=report_path_rel,
                    report_commit=report_commit,
                    base_commit=task_card_commit,
                    implementation_commit=implementation_commit,
                    depth=MadDeliberationDepth.BALANCED,
                )

                acceptance_path = (
                    f"docs/pm/acceptances/{task_id}-r{revision}"
                    f"-a{attempt}-review1.md"
                )
                acceptance_tr = _make_acceptance_transition_request(
                    task_id=task_id,
                    dispatch_id=dispatch_id,
                    revision=revision,
                    attempt=attempt,
                    accepted_commit=implementation_commit,
                    acceptance_path=acceptance_path,
                    head_sha=head_sha,
                    event_id="EVT-ACCEPT-E2E",
                )

                # ⚠ integration_transition_request is None — acceptance
                # cycle must NOT request integration.

                # ── 11. Build subprocess router ────────────────────────────
                claude_stdout = _build_claude_stdout(
                    task_id=task_id,
                    revision=revision,
                    attempt=attempt,
                    dispatch_id=dispatch_id,
                    implementation_commit=implementation_commit,
                    report_commit=report_commit,
                )

                mad_archive = project_root / ".agentdesk" / "mad-archives" / "e2e-audit"
                mad_archive.parent.mkdir(parents=True, exist_ok=True)
                mad_archive.mkdir(parents=True, exist_ok=True)
                mad_audit_stdout = _build_mad_audit_stdout(
                    archive_path=str(mad_archive),
                    depth_value="balanced",
                )

                claude_proc = FakeProcess(pid=10001, returncode=0, stdout=claude_stdout)
                mad_proc = FakeProcess(pid=10002, returncode=0, stdout=mad_audit_stdout)

                subprocess_calls: list[tuple] = []
                executed_claude_exe = str(fake_claude_exe)
                executed_mad_exe = str(fake_mad_exe)

                def subprocess_router(*argv: str, **kwargs: Any) -> FakeProcess:
                    subprocess_calls.append((argv, kwargs))
                    exe = argv[0] if argv else ""
                    if exe == executed_claude_exe:
                        return claude_proc
                    elif exe == executed_mad_exe:
                        return mad_proc
                    else:
                        raise AssertionError(
                            f"Unexpected subprocess executable: {exe!r}"
                        )

                # ── 12. Build MadGatewayConfig ─────────────────────────────
                mad_config = MadGatewayConfig(
                    mad_executable=str(fake_mad_exe),
                    mad_home=str(exe_tmp),
                    timeout_seconds=30,
                    planning_agent_ids=("agent-1",),
                    planning_report_agent_id="agent-1",
                    audit_agent_ids=("auditor-1",),
                    audit_report_agent_id="auditor-1",
                )

                # ═════════════════════════════════════════════════════════
                # PHASE A: Dispatch + PHASE B: Acceptance
                # ═════════════════════════════════════════════════════════

                with mock.patch(
                    "asyncio.create_subprocess_exec",
                    side_effect=subprocess_router,
                ):
                    # ── Phase A: Dispatch ─────────────────────────────────
                    dispatch_result = await orch.run_dispatch_cycle(
                        dispatch_cycle_req, providers,
                    )

                    # Dispatch assertions
                    self.assertIsInstance(dispatch_result, DispatchCycleResult)
                    self.assertEqual(
                        dispatch_result.dispatch_transition.from_state,
                        "ready",
                    )
                    self.assertEqual(
                        dispatch_result.dispatch_transition.to_state,
                        "dispatched",
                    )
                    self.assertIsNotNone(dispatch_result.acknowledge_transition)
                    self.assertEqual(
                        dispatch_result.acknowledge_transition.from_state,
                        "dispatched",
                    )
                    self.assertEqual(
                        dispatch_result.acknowledge_transition.to_state,
                        "in_progress",
                    )
                    self.assertEqual(
                        dispatch_result.delivery_transition.from_state,
                        "in_progress",
                    )
                    self.assertEqual(
                        dispatch_result.delivery_transition.to_state,
                        "review_ready",
                    )

                    self.assertEqual(
                        dispatch_result.worker_output.status,
                        WorkerCompletionStatus.COMPLETED,
                    )
                    self.assertEqual(
                        dispatch_result.worker_output.implementation_commit,
                        implementation_commit,
                    )
                    self.assertEqual(
                        dispatch_result.worker_output.report_commit,
                        report_commit,
                    )

                    receipt = dispatch_result.delivery_receipt
                    self.assertEqual(
                        receipt.implementation_commit,
                        implementation_commit,
                    )
                    self.assertEqual(
                        receipt.report_commit,
                        report_commit,
                    )

                    # ── Phase B: Acceptance (no integration) ─────────────
                    acceptance_cycle_req = AcceptanceCycleRequest(
                        dispatch_cycle_result=dispatch_result,
                        audit_input=audit_input,
                        acceptance_transition_request=acceptance_tr,
                        integration_transition_request=None,
                        worker_kind=WorkerKind.ADVANCED_AGENT,
                        holder_instance_id="e2e-instance",
                    )

                    acceptance_result = await orch.run_acceptance_cycle(
                        acceptance_cycle_req, mad_config,
                    )

                    # Acceptance assertions
                    self.assertIsInstance(acceptance_result, AcceptanceCycleResult)
                    self.assertEqual(
                        acceptance_result.audit_result.verdict,
                        "pass",
                    )
                    self.assertEqual(
                        acceptance_result.audit_result.status,
                        "completed",
                    )

                    self.assertIsNotNone(acceptance_result.accept_transition)
                    acc_tr_result = acceptance_result.accept_transition
                    self.assertEqual(acc_tr_result.from_state, "review_ready")
                    self.assertEqual(acc_tr_result.to_state, "accepted")

                    # Integration was NOT requested
                    self.assertIsNone(acceptance_result.integrate_transition)

                # ── 13. Subprocess call assertions (after mock exits) ──────
                claude_calls = [
                    c for c in subprocess_calls
                    if c[0][0] == executed_claude_exe
                ]
                mad_calls = [
                    c for c in subprocess_calls
                    if c[0][0] == executed_mad_exe
                ]
                self.assertEqual(len(claude_calls), 1,
                                 "Claude CLI must be called exactly once")
                self.assertEqual(len(mad_calls), 1,
                                 "MAD audit CLI must be called exactly once")
                self.assertEqual(len(subprocess_calls), 2,
                                 "Total subprocess calls must be exactly 2")

                # ── 14. Post-acceptance state snapshot ────────────────────
                snapshot_post_accept = StateProvider(project_root).snapshot()
                tasks_accept = [t for t in snapshot_post_accept.tasks
                                if t.task_id == task_id]
                self.assertEqual(len(tasks_accept), 1)
                task_accept = tasks_accept[0]

                self.assertEqual(task_accept.state, "accepted")
                self.assertEqual(
                    task_accept.accepted_commit, implementation_commit,
                )
                self.assertIsNone(task_accept.integrated_commit)
                self.assertIsNone(task_accept.current_dispatch)
                self.assertIsNotNone(task_accept.acceptance_path)
                acceptance_file = project_root / task_accept.acceptance_path
                self.assertTrue(acceptance_file.is_file())

                # Capture pre-failure canonical state for zero-write check
                tasks_yaml_path = project_root / "docs" / "pm" / "state" / "tasks.yaml"
                tasks_yaml_before = tasks_yaml_path.read_bytes()
                events_dir = project_root / "docs" / "pm" / "events"
                acceptance_dir = project_root / "docs" / "pm" / "acceptances"
                events_before = sorted(
                    p.name for p in events_dir.iterdir() if p.is_file()
                )
                acceptances_before = sorted(
                    p.name for p in acceptance_dir.iterdir() if p.is_file()
                )
                subprocess_count_before = len(subprocess_calls)
                acceptance_file_before = (
                    acceptance_file.read_bytes()
                    if acceptance_file.is_file() else None
                )

                # Lease state before
                import worker_slot_lease as wsl
                lease_store_before = wsl.read_worker_slot_leases(project_root)
                leases_before = dict(lease_store_before.get("leases", {}))

                # ═════════════════════════════════════════════════════════
                # PHASE C: Illegal failure request — fail-closed, zero-write
                # ═════════════════════════════════════════════════════════

                # Illegal request: blocked_attempt_valid=False
                illegal_ftr = _make_failure_transition_request(
                    task_id=task_id,
                    revision=revision,
                    head_sha=head_sha,
                    event_id="EVT-INTFAIL-ILLEGAL",
                    blocked_attempt_valid=False,  # ← illegal: must be True
                )

                illegal_req = IntegrationFailureRequest(
                    acceptance_cycle_request=acceptance_cycle_req,
                    acceptance_cycle_result=acceptance_result,
                    dispatch_cycle_result=dispatch_result,
                    failure_transition_request=illegal_ftr,
                )

                with self.assertRaises(WorkflowInputError):
                    await orch.record_integration_failure(illegal_req)

                # ── Zero-write assertions ────────────────────────────────
                tasks_yaml_after = tasks_yaml_path.read_bytes()
                events_after = sorted(
                    p.name for p in events_dir.iterdir() if p.is_file()
                )
                acceptances_after = sorted(
                    p.name for p in acceptance_dir.iterdir() if p.is_file()
                )
                subprocess_count_after = len(subprocess_calls)

                # tasks.yaml unchanged
                self.assertEqual(
                    tasks_yaml_after, tasks_yaml_before,
                    "tasks.yaml must be unchanged after illegal failure request",
                )
                # events unchanged
                self.assertEqual(
                    events_after, events_before,
                    "events must be unchanged after illegal failure request",
                )
                # acceptances unchanged
                self.assertEqual(
                    acceptances_after, acceptances_before,
                    "acceptances must be unchanged after illegal failure request",
                )
                # subprocess count unchanged
                self.assertEqual(
                    subprocess_count_after, subprocess_count_before,
                    "subprocess count must be unchanged after illegal failure request",
                )
                # acceptance file unchanged
                if acceptance_file_before is not None:
                    self.assertEqual(
                        acceptance_file.read_bytes(), acceptance_file_before,
                        "acceptance file must be unchanged after illegal failure request",
                    )
                # lease state unchanged
                lease_store_after = wsl.read_worker_slot_leases(project_root)
                leases_after = dict(lease_store_after.get("leases", {}))
                self.assertEqual(
                    leases_after, leases_before,
                    "leases must be unchanged after illegal failure request",
                )

                # Also test: resume_state != "accepted"
                illegal_ftr2 = _make_failure_transition_request(
                    task_id=task_id,
                    revision=revision,
                    head_sha=head_sha,
                    event_id="EVT-INTFAIL-ILLEGAL2",
                    resume_state="ready",  # ← illegal
                )

                illegal_req2 = IntegrationFailureRequest(
                    acceptance_cycle_request=acceptance_cycle_req,
                    acceptance_cycle_result=acceptance_result,
                    dispatch_cycle_result=dispatch_result,
                    failure_transition_request=illegal_ftr2,
                )

                with self.assertRaises(WorkflowInputError):
                    await orch.record_integration_failure(illegal_req2)

                # Still zero new subprocess calls
                self.assertEqual(
                    len(subprocess_calls), subprocess_count_before,
                    "subprocess count must be unchanged after second illegal failure request",
                )

                # ═════════════════════════════════════════════════════════
                # PHASE D: Legal integration failure recording
                # ═════════════════════════════════════════════════════════

                legal_ftr = _make_failure_transition_request(
                    task_id=task_id,
                    revision=revision,
                    head_sha=head_sha,
                    event_id="EVT-INTFAIL-E2E",
                    blocked_kind="integration_conflict",
                    blocked_reason="external git merge conflict — target branch diverged",
                    blocked_owner="PM",
                    unblock_condition="manual conflict resolution and re-integration by PM",
                    resume_state="accepted",
                    blocked_attempt_valid=True,
                )

                legal_req = IntegrationFailureRequest(
                    acceptance_cycle_request=acceptance_cycle_req,
                    acceptance_cycle_result=acceptance_result,
                    dispatch_cycle_result=dispatch_result,
                    failure_transition_request=legal_ftr,
                )

                result = await orch.record_integration_failure(legal_req)

                # Result assertions
                self.assertIsInstance(result, IntegrationFailureResult)
                self.assertEqual(result.task_id, task_id)
                self.assertEqual(result.audit_result.verdict, "pass")
                self.assertIs(
                    result.audit_result,
                    acceptance_result.audit_result,
                    "audit result identity must be preserved",
                )
                self.assertEqual(
                    result.failure_transition.from_state, "accepted",
                )
                self.assertEqual(
                    result.failure_transition.to_state, "blocked",
                )

                # No additional subprocess calls
                self.assertEqual(
                    len(subprocess_calls), 2,
                    "Subprocess calls must remain exactly 2 after "
                    "integration failure recording",
                )

                # ═════════════════════════════════════════════════════════
                # PHASE E: Final state verification
                # ═════════════════════════════════════════════════════════

                snapshot = StateProvider(project_root).snapshot()
                self.assertIsInstance(snapshot, StateSnapshot)

                tasks = [t for t in snapshot.tasks if t.task_id == task_id]
                self.assertEqual(len(tasks), 1)
                task = tasks[0]

                # ── State ──────────────────────────────────────────────
                self.assertEqual(task.state, "blocked")
                self.assertEqual(task.revision, revision)
                self.assertEqual(task.attempt, attempt)

                # ── Delivery state ─────────────────────────────────────
                self.assertEqual(task.delivery_state, "accepted")

                # ── Accepted commit preserved ──────────────────────────
                self.assertEqual(
                    task.accepted_commit, implementation_commit,
                )

                # ── Acceptance path preserved ──────────────────────────
                self.assertIsNotNone(task.acceptance_path)
                acceptance_file_final = project_root / task.acceptance_path
                self.assertTrue(
                    acceptance_file_final.is_file(),
                    "Acceptance file must still exist after INTEGRATION_FAILED",
                )

                # ── integrated_commit is None ──────────────────────────
                self.assertIsNone(task.integrated_commit)

                # ── current_dispatch is None ───────────────────────────
                self.assertIsNone(task.current_dispatch)

                # ── Resume state ───────────────────────────────────────
                self.assertEqual(task.resume_state, "accepted")

                # ── Blocked attempt valid ─────────────────────────────
                self.assertTrue(task.blocked_attempt_valid)

                # ── Blocked kind ───────────────────────────────────────
                self.assertEqual(task.blocked_kind, "integration_conflict")

                # ── Blocked owner and unblock condition non-empty ─────
                self.assertIsNotNone(task.blocked_owner)
                self.assertNotEqual(task.blocked_owner, "")
                self.assertIsNotNone(task.unblock_condition)
                self.assertNotEqual(task.unblock_condition, "")

                # ── Implementation and report commits preserved ────────
                self.assertEqual(task.implementation_commit, implementation_commit)
                self.assertEqual(task.report_commit, report_commit)

                # ── No CHANGE_INTEGRATED ───────────────────────────────
                integrated_events = [
                    e for e in snapshot.events
                    if e.event_type == "CHANGE_INTEGRATED" and e.task_id == task_id
                ]
                self.assertEqual(
                    len(integrated_events), 0,
                    "CHANGE_INTEGRATED must never appear",
                )

                # ── Event order ────────────────────────────────────────
                expected_sequence = [
                    "TASK_DISPATCHED",
                    "DISPATCH_ACKNOWLEDGED",
                    "DELIVERY_SUBMITTED",
                    "DELIVERY_ACCEPTED",
                    "INTEGRATION_FAILED",
                ]

                task_events_sorted = sorted(
                    [e for e in snapshot.events if e.task_id == task_id],
                    key=lambda e: e.occurred_at,
                )
                observed_types = [e.event_type for e in task_events_sorted]

                for expected_type in expected_sequence:
                    self.assertIn(
                        expected_type, observed_types,
                        f"Expected event type {expected_type} must be present",
                    )

                # Check business order
                seq_indices = {
                    et: observed_types.index(et)
                    for et in expected_sequence
                    if et in observed_types
                }
                for i in range(len(expected_sequence) - 1):
                    a, b = expected_sequence[i], expected_sequence[i + 1]
                    if a in seq_indices and b in seq_indices:
                        self.assertLess(
                            seq_indices[a], seq_indices[b],
                            f"{a} must occur before {b}",
                        )

                # ── Forbidden event types ──────────────────────────────
                forbidden_types = {
                    "CHANGE_INTEGRATED", "DELIVERY_RETURNED",
                    "TASK_REQUEUED", "TASK_BLOCKED", "BLOCKER_RESOLVED",
                }
                for ft in forbidden_types:
                    forbidden = [
                        e for e in snapshot.events
                        if e.task_id == task_id and e.event_type == ft
                    ]
                    self.assertEqual(
                        len(forbidden), 0,
                        f"Forbidden event type {ft} must not appear",
                    )

                # ── Correct number of events (exactly 5) ───────────────
                self.assertEqual(
                    len(task_events_sorted), 5,
                    f"Expected exactly 5 events for task, "
                    f"got {len(task_events_sorted)}: {observed_types}",
                )

                # ── Guard assertions ────────────────────────────────────
                # TASK_DISPATCHED and DELIVERY_ACCEPTED must have
                # approval_gate guard.
                # INTEGRATION_FAILED must NOT have approval_gate guard.
                gated_events = {"TASK_DISPATCHED", "DELIVERY_ACCEPTED"}
                for ev in snapshot.events:
                    if ev.task_id != task_id:
                        continue
                    guard_names = {gr.guard for gr in ev.guard_results}
                    has_approval_gate = "approval_gate" in guard_names
                    if ev.event_type in gated_events:
                        self.assertTrue(
                            has_approval_gate,
                            f"{ev.event_type} must have approval_gate guard; "
                            f"guards present: {guard_names}",
                        )
                    elif ev.event_type == "INTEGRATION_FAILED":
                        self.assertFalse(
                            has_approval_gate,
                            "INTEGRATION_FAILED must NOT have approval_gate guard",
                        )

                # ── No second dispatch, no second MAD audit ──────────────
                # Already confirmed via subprocess + event count checks.

                # ── No auto-retry, redispatch, escalation ────────────────
                # The state is "blocked" with no further transitions.
                dispatch_events = [
                    e for e in snapshot.events
                    if e.task_id == task_id and e.event_type == "TASK_DISPATCHED"
                ]
                self.assertEqual(
                    len(dispatch_events), 1,
                    "Only one TASK_DISPATCHED event must exist",
                )

                # ── StateProvider reads final consistent state ─────────────
                self.assertIsNotNone(snapshot.tasks)
                self.assertIsNotNone(snapshot.events)

                # ── All worker slot leases released ────────────────────────
                final_lease_store = wsl.read_worker_slot_leases(project_root)
                final_leases = final_lease_store.get("leases", {})
                self.assertEqual(
                    len(final_leases), 0,
                    "All worker slot leases must be released",
                )

                # ── Event uniqueness ──────────────────────────────────────
                event_ids = [e.event_id for e in snapshot.events]
                self.assertEqual(
                    len(event_ids), len(set(event_ids)),
                    "All event_ids must be unique",
                )

                # ── No orphan entries ─────────────────────────────────────
                task_ids_in_snapshot = {t.task_id for t in snapshot.tasks}
                for ev in snapshot.events:
                    self.assertIn(
                        ev.task_id, task_ids_in_snapshot,
                        f"Event {ev.event_id} references non-existent task",
                    )
                for ob in snapshot.outbox:
                    self.assertIn(
                        ob.task_id, task_ids_in_snapshot,
                        f"Outbox {ob.message_id} references non-existent task",
                    )
                for acc in snapshot.acceptances:
                    self.assertIn(
                        acc.task_id, task_ids_in_snapshot,
                        f"Acceptance {acc.filename} references non-existent task",
                    )

                # ── No .tmp-* files ──────────────────────────────────────
                tmp_files = list(project_root.rglob(".tmp-*"))
                self.assertEqual(
                    len(tmp_files), 0,
                    f"Must not have .tmp-* files: {tmp_files}",
                )

                # ── No Git remote ─────────────────────────────────────────
                remotes_result = subprocess.run(
                    ["git", "-C", str(project_root), "remote"],
                    capture_output=True, text=True, timeout=10,
                )
                self.assertEqual(
                    remotes_result.stdout.strip(), "",
                    "Temp git repo must have no remotes",
                )

                # ── cwd unchanged ─────────────────────────────────────────
                self.assertEqual(
                    os.getcwd(), cwd_before,
                    "Current working directory must be unchanged",
                )

                # ── No pending asyncio tasks ──────────────────────────────
                try:
                    loop = asyncio.get_running_loop()
                except RuntimeError:
                    loop = None
                if loop is not None:
                    all_tasks = asyncio.all_tasks(loop)
                    self.assertLessEqual(
                        len(all_tasks), 3,
                        f"At most 3 pending asyncio tasks expected, "
                        f"got {len(all_tasks)}",
                    )

                # ── Mad-ref assertions ────────────────────────────────────
                self.assertIsNotNone(snapshot.mad_refs)
                audit_refs = [
                    r for r in snapshot.mad_refs
                    if r.purpose == "audit" and r.dispatch_id == dispatch_id
                ]
                self.assertEqual(
                    len(audit_refs), 1,
                    "Exactly one mad-ref with purpose=audit must exist",
                )
                mad_ref = audit_refs[0]
                self.assertEqual(mad_ref.purpose, "audit")
                self.assertEqual(mad_ref.task_id, task_id)
                self.assertTrue(
                    Path(mad_ref.archive_path).is_absolute(),
                    "mad-ref archive_path must be absolute",
                )

                # ── Task root must NOT have model_selection ────────────────
                self.assertFalse(
                    hasattr(task, "model_selection"),
                    "Task root must NOT have model_selection field",
                )

            finally:
                shutil.rmtree(project_root, ignore_errors=True)
        finally:
            shutil.rmtree(exe_tmp, ignore_errors=True)

        # ── Source repo purity ────────────────────────────────────────
        source_status_after = _git_status_bytes(source_repo)
        self.assertEqual(
            source_status_after, source_status_before,
            "AgentDesk source repo must not be modified by test",
        )
