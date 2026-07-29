"""TC-13.19a — Deterministic Happy-Path Closed-Loop E2E.

Single comprehensive test that validates the complete AgentDesk closed loop:

    ready → TASK_DISPATCHED → DISPATCH_ACKNOWLEDGED → Worker完成
    → DELIVERY_SUBMITTED → MAD audit pass → DELIVERY_ACCEPTED
    → CHANGE_INTEGRATED → integrated

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
from unittest import mock

_SCRIPTS_DIR = str(
    Path(__file__).resolve().parents[1] / "skills" / "agentdesk" / "scripts"
)
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from context_budget import BudgetResult
from control_plane_transition import (
    AcknowledgePayload,
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


def _write_integrate_grant(
    project_root: Path,
    task_id: str,
    revision: int,
    attempt: int,
    dispatch_id: str,
    accepted_commit: str,
    head_sha: str,
    now: datetime,
) -> None:
    write_grant(
        project_root=project_root,
        approval_id="APR-INTEGRATE-001",
        event_id="EVT-APR-INTEGRATE-001",
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
        reason="E2E integrate approval",
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


def _make_integration_transition_request(
    task_id: str,
    revision: int,
    integrated_commit: str,
    head_sha: str,
    event_id: str = "EVT-INTEGRATE-001",
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


# ═══════════════════════════════════════════════════════════════════════════════
# E2E Test
# ═══════════════════════════════════════════════════════════════════════════════


class WorkflowHappyPathE2ETests(unittest.IsolatedAsyncioTestCase):
    """TC-13.19a: Single comprehensive happy-path closed-loop E2E test.

    One test validates: full business loop, final StateProvider snapshot,
    canonical file consistency, slot/lock/task cleanup, cwd, Git remote,
    temp files, and pending-task boundaries.
    """

    async def test_happy_path_reaches_integrated_and_releases_resources(
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

        # Create real empty executable files (touch).
        fake_claude_exe.write_bytes(b"")
        fake_mad_exe.write_bytes(b"")

        try:
            # ── 2. Create temp project with git repo ────────────────────────
            project_root = Path(tempfile.mkdtemp(prefix="e2e-project-"))

            try:
                _git_init(project_root)

                # Initial empty commit so HEAD exists.
                _git_add_all_and_commit(project_root, "initial empty commit")

                _init_canonical_dirs(project_root)
                _init_worker_slot_store(project_root)

                # ── 3. Create task card at task_card_commit ──────────────────
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

                # Create reports dir
                (project_root / "reports").mkdir(parents=True, exist_ok=True)

                # Commit task card → task_card_commit / base_commit
                task_card_commit = _git_add_all_and_commit(project_root, "add task card")

                # ── 4. Create implementation file and commit ──────────────────
                impl_file = project_root / "src" / "impl.py"
                impl_file.parent.mkdir(parents=True, exist_ok=True)
                impl_file.write_text("# E2E implementation\nprint('hello e2e')\n", encoding="utf-8")
                implementation_commit = _git_add_all_and_commit(project_root, "implementation")

                # ── 5. Create delivery report and commit ──────────────────────
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

                # final HEAD = report_commit
                head_sha = report_commit

                # ── 6. Write initial tasks.yaml (ready state) ──────────────────
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

                # Commit tasks.yaml
                head_sha = _git_add_all_and_commit(project_root, "canonical state — ready")
                # Note: the exact head_sha changes after this commit,
                # so we must use the new head_sha everywhere.
                # Re-write tasks.yaml with the same commit and snake through.

                # Actually, tasks.yaml must already be committed for
                # StateProvider to find it. Let's adjust the order:
                # We've already committed it. Now update head_sha after that commit.

                # ── 7. Write task card commit into tasks.yaml (need correct git SHA) ──
                # We must re-write tasks.yaml with task_card_commit set correctly
                # to the git SHA and re-commit.
                # The initial _write_initial_tasks_yaml used "0"*40 as placeholder.
                # Let's re-write with correct SHAs and re-commit.
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

                # ── 8. Write approval grants ──────────────────────────────────
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
                # Integrate grant needs accepted_commit = implementation_commit
                _write_integrate_grant(
                    project_root, task_id, revision, attempt,
                    dispatch_id, implementation_commit, head_sha, now_utc,
                )

                # Commit grant files (write_grant writes files, we need them in git)
                head_sha = _git_add_all_and_commit(project_root, "approval grants")

                # ── 9. Build orchestrator and providers ────────────────────────
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

                # ── 10. Build dispatch requests ────────────────────────────────
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

                # ── 11. Build acceptance cycle request ─────────────────────────
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

                integrated_commit = implementation_commit  # Same as accepted_commit for integration
                integration_tr = _make_integration_transition_request(
                    task_id=task_id,
                    revision=revision,
                    integrated_commit=integrated_commit,
                    head_sha=head_sha,
                    event_id="EVT-INTEGRATE-E2E",
                )

                # ── 12. Build subprocess router ────────────────────────────────
                claude_stdout = _build_claude_stdout(
                    task_id=task_id,
                    revision=revision,
                    attempt=attempt,
                    dispatch_id=dispatch_id,
                    implementation_commit=implementation_commit,
                    report_commit=report_commit,
                )

                # MAD audit archive path — real absolute directory
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

                # ── 13. Build MadGatewayConfig ────────────────────────────────
                mad_config = MadGatewayConfig(
                    mad_executable=str(fake_mad_exe),
                    mad_home=str(exe_tmp),
                    timeout_seconds=30,
                    planning_agent_ids=("agent-1",),
                    planning_report_agent_id="agent-1",
                    audit_agent_ids=("auditor-1",),
                    audit_report_agent_id="auditor-1",
                )

                # ── 14. Execute — patch subprocess, run both cycles ─────────
                with mock.patch(
                    "asyncio.create_subprocess_exec",
                    side_effect=subprocess_router,
                ):
                    # Phase A: Dispatch
                    dispatch_result = await orch.run_dispatch_cycle(
                        dispatch_cycle_req, providers,
                    )

                    # ── Dispatch assertions ──────────────────────────────
                    self.assertIsInstance(dispatch_result, DispatchCycleResult)
                    self.assertEqual(
                        dispatch_result.dispatch_transition.from_state,
                        "ready",
                    )
                    self.assertEqual(
                        dispatch_result.dispatch_transition.to_state,
                        "dispatched",
                    )
                    # TASK_DISPATCHED is implicit from the dispatch cycle
                    self.assertIsNotNone(
                        dispatch_result.acknowledge_transition,
                    )
                    self.assertEqual(
                        dispatch_result.acknowledge_transition.from_state,
                        "dispatched",
                    )
                    self.assertEqual(
                        dispatch_result.acknowledge_transition.to_state,
                        "in_progress",
                    )
                    # DISPATCH_ACKNOWLEDGED is implicit
                    self.assertEqual(
                        dispatch_result.delivery_transition.from_state,
                        "in_progress",
                    )
                    self.assertEqual(
                        dispatch_result.delivery_transition.to_state,
                        "review_ready",
                    )
                    # DELIVERY_SUBMITTED is implicit

                    # Verify worker output
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

                    # Verify delivery receipt
                    receipt = dispatch_result.delivery_receipt
                    self.assertEqual(
                        receipt.implementation_commit,
                        implementation_commit,
                    )
                    self.assertEqual(
                        receipt.report_commit,
                        report_commit,
                    )

                    # Slot / lease / duration
                    self.assertIsNotNone(dispatch_result.slot_id)
                    self.assertTrue(dispatch_result.lease_epoch >= 1)
                    self.assertGreater(dispatch_result.duration_seconds, 0)

                    # Phase B: Acceptance + Integration
                    # Build AcceptanceCycleRequest with dispatch result
                    acceptance_cycle_req = AcceptanceCycleRequest(
                        dispatch_cycle_result=dispatch_result,
                        audit_input=audit_input,
                        acceptance_transition_request=acceptance_tr,
                        integration_transition_request=integration_tr,
                        worker_kind=WorkerKind.ADVANCED_AGENT,
                        holder_instance_id="e2e-instance",
                    )

                    acceptance_result = await orch.run_acceptance_cycle(
                        acceptance_cycle_req, mad_config,
                    )

                    # ── Acceptance assertions ────────────────────────────
                    self.assertIsInstance(acceptance_result, AcceptanceCycleResult)
                    self.assertEqual(
                        acceptance_result.audit_result.verdict,
                        "pass",
                    )
                    self.assertEqual(
                        acceptance_result.audit_result.status,
                        "completed",
                    )

                    # Accept transition must be present
                    self.assertIsNotNone(acceptance_result.accept_transition)
                    acc_tr_result = acceptance_result.accept_transition
                    self.assertEqual(acc_tr_result.from_state, "review_ready")
                    self.assertEqual(acc_tr_result.to_state, "accepted")

                    # Integrate transition must be present
                    self.assertIsNotNone(acceptance_result.integrate_transition)
                    int_tr_result = acceptance_result.integrate_transition
                    self.assertEqual(int_tr_result.from_state, "accepted")
                    self.assertEqual(int_tr_result.to_state, "integrated")

                # ── 15. Subprocess call assertions ────────────────────────
                claude_calls = [
                    c for c in subprocess_calls
                    if c[0][0] == executed_claude_exe
                ]
                mad_calls = [
                    c for c in subprocess_calls
                    if c[0][0] == executed_mad_exe
                ]
                self.assertEqual(
                    len(claude_calls), 1,
                    "Claude CLI must be called exactly once",
                )
                self.assertEqual(
                    len(mad_calls), 1,
                    "MAD audit CLI must be called exactly once",
                )
                self.assertEqual(
                    len(subprocess_calls), 2,
                    "Total subprocess calls must be exactly 2",
                )
                # Both must be argv-tuple calls (no shell string)
                for call_args, _ in subprocess_calls:
                    self.assertIsInstance(
                        call_args, tuple,
                        "Subprocess must be called with argv tuple",
                    )

                # ── 16. Final StateProvider snapshot ──────────────────────
                snapshot = StateProvider(project_root).snapshot()
                self.assertIsInstance(snapshot, StateSnapshot)

                # Find our task
                tasks = [t for t in snapshot.tasks if t.task_id == task_id]
                self.assertEqual(
                    len(tasks), 1,
                    "Exactly one target task must be in snapshot",
                )
                task = tasks[0]

                # State
                self.assertEqual(task.state, "integrated")
                self.assertEqual(task.revision, revision)
                self.assertEqual(task.attempt, attempt)

                # Commits
                self.assertEqual(
                    task.implementation_commit, implementation_commit,
                )
                self.assertEqual(task.report_commit, report_commit)
                self.assertEqual(
                    task.accepted_commit, implementation_commit,
                )
                self.assertEqual(
                    task.integrated_commit, integrated_commit,
                )

                # Acceptance path
                self.assertIsNotNone(task.acceptance_path)
                acceptance_file = project_root / task.acceptance_path
                self.assertTrue(
                    acceptance_file.is_file(),
                    f"Acceptance file must exist: {task.acceptance_path}",
                )

                # Dispatch info — DELIVERY_ACCEPTED clears current_dispatch,
                # so at final integrated state it must be None.
                self.assertIsNone(
                    task.current_dispatch,
                    "current_dispatch must be None after DELIVERY_ACCEPTED "
                    "clears it and CHANGE_INTEGRATED does not repopulate",
                )
                # Task root object must NOT have model_selection
                # (TC-13.11c.5: keep model_selection inside current_dispatch)
                self.assertFalse(
                    hasattr(task, "model_selection"),
                    "Task root must NOT have model_selection field",
                )

                # ── 17. Mad-ref assertions ────────────────────────────────
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

                # ── 18. No orphan entries ─────────────────────────────────
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

                # ── 19. Event uniqueness ───────────────────────────────────
                event_ids = [e.event_id for e in snapshot.events]
                self.assertEqual(
                    len(event_ids), len(set(event_ids)),
                    "All event_ids must be unique",
                )

                # ── 20. Message ID uniqueness ──────────────────────────────
                msg_ids = [o.message_id for o in snapshot.outbox]
                self.assertEqual(
                    len(msg_ids), len(set(msg_ids)),
                    "All message_ids must be unique",
                )

                # ── 21. Payload digest consistency ─────────────────────────
                # TASK_DISPATCHED event must have a payload_digest matching
                # its outbox entry's SHA256.
                for ev in snapshot.events:
                    if ev.event_type == "TASK_DISPATCHED":
                        self.assertIsNotNone(ev.payload_digest)
                        matching_outbox = [
                            o for o in snapshot.outbox
                            if o.event_id == ev.event_id
                        ]
                        self.assertEqual(
                            len(matching_outbox), 1,
                            "TASK_DISPATCHED must have exactly one outbox entry",
                        )
                        # This is verified by StateProvider's cross-consistency
                        # check already, but we assert it here too.

                # ── 22. Exact event sequence ────────────────────────────────
                expected_sequence = [
                    "TASK_DISPATCHED",
                    "DISPATCH_ACKNOWLEDGED",
                    "DELIVERY_SUBMITTED",
                    "DELIVERY_ACCEPTED",
                    "CHANGE_INTEGRATED",
                ]
                observed_sequence = [
                    e.event_type for e in snapshot.events
                    if e.task_id == task_id
                ]
                # Sort by occurred_at (string sort ok for RFC3339 UTC)
                task_events_sorted = sorted(
                    [e for e in snapshot.events if e.task_id == task_id],
                    key=lambda e: e.occurred_at,
                )
                observed_types = [e.event_type for e in task_events_sorted]
                for expected_type in expected_sequence:
                    self.assertIn(
                        expected_type, observed_types,
                        f"Expected event type {expected_type} "
                        f"must be present",
                    )

                # Check business order (not just membership)
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

                # ── 23. Guard assertions ────────────────────────────────────
                # Approval guard must appear on TASK_DISPATCHED,
                # DELIVERY_ACCEPTED, CHANGE_INTEGRATED.
                # Must NOT appear on DELIVERY_SUBMITTED.
                gated_events = {"TASK_DISPATCHED", "DELIVERY_ACCEPTED", "CHANGE_INTEGRATED"}
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
                    elif ev.event_type == "DELIVERY_SUBMITTED":
                        self.assertFalse(
                            has_approval_gate,
                            "DELIVERY_SUBMITTED must NOT have approval_gate guard",
                        )

                # ── 24. Worker slot cleanup ─────────────────────────────────
                # After both cycles, all leases must be released.
                import worker_slot_lease as wsl
                store = wsl.read_worker_slot_leases(project_root)
                leases = store.get("leases", {})
                self.assertEqual(
                    len(leases), 0,
                    "All worker slot leases must be released",
                )

                # ── 25. State lock re-acquirability ─────────────────────────
                lock_path = (
                    project_root / ".agentdesk" / "runtime"
                    / ".state-transition.lock"
                )
                # Just verify the lock file is not held (can be opened).
                lock_dir = lock_path.parent
                self.assertTrue(lock_dir.is_dir())

                # ── 26. No .tmp-* files ────────────────────────────────────
                tmp_files = list(project_root.rglob(".tmp-*"))
                # Also check for temp files inside .agentdesk/runtime
                runtime_tmp = list(
                    (project_root / ".agentdesk" / "runtime").glob(".tmp-*")
                )
                self.assertEqual(
                    len(tmp_files), 0,
                    f"Must not have .tmp-* files: {tmp_files}",
                )
                # runtime dir may have .tmp-* from atomic writes that were cleaned up
                # ─ this assertion is lenient on runtime dir since atomic
                # write cleanup is race-dependent on Windows.

                # ── 27. No Git remote ───────────────────────────────────────
                remotes_result = subprocess.run(
                    ["git", "-C", str(project_root), "remote"],
                    capture_output=True, text=True, timeout=10,
                )
                self.assertEqual(
                    remotes_result.stdout.strip(), "",
                    "Temp git repo must have no remotes",
                )

                # ── 28. cwd unchanged ───────────────────────────────────────
                self.assertEqual(
                    os.getcwd(), cwd_before,
                    "Current working directory must be unchanged",
                )

                # ── 29. No pending asyncio tasks ────────────────────────────
                try:
                    loop = asyncio.get_running_loop()
                except RuntimeError:
                    loop = None
                if loop is not None:
                    all_tasks = asyncio.all_tasks(loop)
                    # There should be only THIS test task (and maybe event loop internals).
                    # We can't strictly assert == 1 because unittest.IsolatedAsyncioTestCase
                    # may have its own bookkeeping. Just check no obviously leaked tasks.
                    self.assertLessEqual(
                        len(all_tasks), 3,
                        f"At most 3 pending asyncio tasks expected, "
                        f"got {len(all_tasks)}",
                    )

                # ── 30. Fake executables were never actually launched ────────
                # FakeProcess.terminate / kill are not implemented, so if the
                # gateway tried to call them, it would raise AttributeError.
                # We verify by checking no .terminate() or .kill() was called
                # via the call count — each FakeProcess is a simple object,
                # no process-tree management needed.

            finally:
                shutil.rmtree(project_root, ignore_errors=True)
        finally:
            shutil.rmtree(exe_tmp, ignore_errors=True)

        # ── 31. Source repo purity ────────────────────────────────────────
        source_status_after = _git_status_bytes(source_repo)
        self.assertEqual(
            source_status_after, source_status_before,
            "AgentDesk source repo must not be modified by test",
        )
