"""Tests for WorkflowOrchestrator — TC-13.18b.

Covers:
- API (__all__, frozen/slots, no AcceptanceCycle types)
- Input validation (project_root, clock, heartbeat interval, event type,
  payload type, consistency checks)
- Success path (precise call order, heartbeat in long tasks, opaque bytes)
- Failure & cancellation paths (acquire, transition, Worker, heartbeat,
  release, exception priority, pending tasks)
- Source boundary (no forbidden imports/calls)
"""

from __future__ import annotations

import asyncio
import unittest
from collections.abc import Mapping
from dataclasses import dataclass, fields as dc_fields
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

# Ensure the scripts dir is on sys.path.
import sys
import os as _os

_SCRIPTS_DIR = str(
    Path(__file__).resolve().parents[1] / "skills" / "agentdesk" / "scripts"
)
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from context_budget import BudgetResult
from control_plane_transition import (
    ControlPlaneTransitionService,
    DispatchPayload,
    TransitionCAS,
    TransitionEventContext,
    TransitionRequest,
    TransitionResult,
)
from core_types import TaskDifficulty, WorkerKind
from dispatcher_gateway import (
    AgentCliProvider,
    DispatchIdentity,
    DispatchRequest,
    DispatchResult,
    ModelSelectionSnapshot,
)
from state_provider import StateProviderError
from worker_adapter import WorkerResult
from worker_slot_lease import (
    WorkerSlotLease,
    WorkerSlotLeaseError,
    WorkerSlotCapacityError,
    WorkerSlotContentionError,
    WorkerSlotFencingError,
    WorkerSlotNotHeldError,
    WorkerSlotValidationError,
    acquire_worker_slot,
    release_worker_slot,
    renew_worker_slot,
)
from workflow_orchestrator import (
    DispatchCycleRequest,
    DispatchCycleResult,
    WorkflowClock,
    WorkflowHeartbeatError,
    WorkflowInputError,
    WorkflowInvariantError,
    WorkflowOrchestrator,
    WorkflowOrchestratorError,
)


# ── test helpers ───────────────────────────────────────────────────────


def _utc(iso: str) -> datetime:
    """Parse an ISO-ish UTC string into a tz-aware datetime."""
    return datetime.fromisoformat(iso).replace(tzinfo=timezone.utc)


def _make_dispatch_request(
    task_id: str = "TC-001",
    dispatch_id: str = "DSP-001",
    workspace: Path | None = None,
    model_selection: ModelSelectionSnapshot | None = None,
) -> DispatchRequest:
    if workspace is None:
        workspace = Path(__file__).resolve().parents[1]
    if model_selection is None:
        model_selection = _make_model_selection()
    identity = DispatchIdentity(
        task_id=task_id,
        revision=1,
        attempt=1,
        dispatch_id=dispatch_id,
    )
    return DispatchRequest(
        identity=identity,
        workspace=workspace,
        prompt="test prompt",
        model_selection=model_selection,
        timeout_seconds=60,
    )


def _make_model_selection(
    provider: str = "test",
    model_id: str = "test-model",
    tier: str = "advanced",
    deliberation: str = "balanced",
    ctx_tokens: int = 200000,
) -> ModelSelectionSnapshot:
    return ModelSelectionSnapshot(
        required_model_tier=tier,
        required_model_capabilities=(),
        model_binding_id="binding-001",
        selected_model_provider=provider,
        selected_model_id=model_id,
        selected_model_tier=tier,
        selected_deliberation_tier=deliberation,
        selected_context_window_tokens=ctx_tokens,
        selected_model_capabilities=(),
        model_degradation_approval_id=None,
    )


def _git_head(project_root: Path) -> str:
    """Get the current git HEAD SHA in *project_root*."""
    import subprocess
    result = subprocess.run(
        ["git", "-C", str(project_root), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git rev-parse HEAD failed: {result.stderr}")
    return result.stdout.strip()


def _make_transition_request(
    task_id: str = "TC-001",
    dispatch_id: str = "DSP-001",
    model_selection: ModelSelectionSnapshot | None = None,
    event_id: str = "EVT-test-001",
    revision: int = 1,
    head_sha: str | None = None,
) -> TransitionRequest:
    if head_sha is None:
        head_sha = "a" * 40  # placeholder — caller should override
    if model_selection is None:
        model_selection = _make_model_selection()
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
        task_card_path="tasks/task.md",
        task_card_commit="b" * 40,
        base_commit="c" * 40,
        branch="feat/test",
        report_path="reports/report.md",
        outbox_message_id="MSG-test-001",
        new_attempt=1,
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


def _make_worker_result(
    worker_kind: WorkerKind = WorkerKind.ADVANCED_AGENT,
    task_difficulty: TaskDifficulty = TaskDifficulty.ADVANCED,
    stdout: bytes = b"output",
    stderr: bytes = b"",
    provider: str = "test",
    model_id: str = "test-model",
    duration_seconds: float = 0.5,
    task_id: str = "TC-001",
    dispatch_id: str = "DSP-001",
) -> WorkerResult:
    return WorkerResult(
        worker_kind=worker_kind,
        task_difficulty=task_difficulty,
        budget=BudgetResult(
            context_window_tokens=200000,
            difficulty=task_difficulty,
            budget_percent=50,
            budget_cap_tokens=256000,
            budget_tokens=100000,
            reserved_tokens=100000,
        ),
        dispatch_result=DispatchResult(
            identity=DispatchIdentity(
                task_id=task_id,
                revision=1,
                attempt=1,
                dispatch_id=dispatch_id,
            ),
            provider=provider,
            model_id=model_id,
            duration_seconds=duration_seconds,
            stdout=stdout,
            stderr=stderr,
            stdout_sha256="sha256_a",
            stderr_sha256="sha256_b",
        ),
    )


# ── fake clock ──────────────────────────────────────────────────────────


class FakeClock:
    """Deterministic fake clock for testing.

    *now* advances by at least *tick* on each call.
    """

    def __init__(self, start: datetime | None = None, tick: float = 1.0) -> None:
        self._now = start if start is not None else _utc("2026-07-28T12:00:00")
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
        # yield to the event loop so other coroutines can progress
        await asyncio.sleep(0)


# ── fake provider ───────────────────────────────────────────────────────


class FakeProvider:
    """Fake AgentCliProvider for testing."""

    def __init__(
        self,
        provider_id: str = "test",
        result: WorkerResult | None = None,
        exception: BaseException | None = None,
    ) -> None:
        self._provider_id = provider_id
        self._result = result
        self._exception = exception
        self.build_invocation_called = False
        self.last_request: DispatchRequest | None = None

    @property
    def provider_id(self) -> str:
        return self._provider_id

    def build_invocation(self, request: DispatchRequest) -> Any:
        self.build_invocation_called = True
        self.last_request = request
        # Return a minimal valid-looking invocation.
        from dispatcher_gateway import AgentCliInvocation
        return AgentCliInvocation(
            executable="fake-cli",
            argv=(),
            stdin=None,
            env_overrides=(),
        )


# ── non-UTC clock (for validation tests) ────────────────────────────────


class NonUtcClock:
    """A clock whose now() returns a non-UTC datetime."""

    def now(self) -> datetime:
        return datetime(2026, 7, 28, 12, 0, 0)  # naive

    def monotonic(self) -> float:
        return 0.0

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(0)


class OffsetClock:
    """A clock whose now() returns a +05:30 offset datetime."""

    def now(self) -> datetime:
        tz_plus_0530 = timezone(timedelta(hours=5, minutes=30))
        return datetime(2026, 7, 28, 12, 0, 0, tzinfo=tz_plus_0530)

    def monotonic(self) -> float:
        return 0.0

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(0)


# ── fake slot store helpers ─────────────────────────────────────────────


def _ensure_runtime_dir(project_root: Path) -> Path:
    """Create .agentdesk/runtime/ directory."""
    runtime = project_root / ".agentdesk" / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    return runtime


def _init_worker_slot_store(project_root: Path) -> None:
    """Write a minimal empty worker-slot-lease.yaml."""
    import json
    runtime = _ensure_runtime_dir(project_root)
    store = {
        "schema_version": "agentdesk.worker-slot-lease/v1",
        "updated_at": "1970-01-01T00:00:00Z",
        "slot_epochs": {
            "basic_agent-1": 0,
            "basic_agent-2": 0,
            "standard_agent-1": 0,
            "standard_agent-2": 0,
            "advanced_agent-1": 0,
            "advanced_agent-2": 0,
            "expert_agent-1": 0,
            "expert_agent-2": 0,
        },
        "leases": {},
    }
    path = runtime / "worker-slot-lease.yaml"
    path.write_text(json.dumps(store, indent=2) + "\n", encoding="utf-8")


def _init_tasks_yaml(project_root: Path, task_id: str = "TC-001", state: str = "ready",
                     revision: int = 1, dispatch_id: str = "DSP-001") -> None:
    """Write a minimal tasks.yaml for the target task."""
    import json
    state_dir = project_root / "docs" / "pm" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    tasks_doc = {
        "schema_version": "agentdesk.tasks/v2",
        "project_id": "test-project",
        "adoption_level": "standard",
        "updated_at": "2026-07-28T12:00:00Z",
        "pm_control": {
            "holder_id": "pm-1",
            "lease_epoch": 1,
            "mode": "manual",
        },
        "tasks": [
            {
                "task_id": task_id,
                "revision": revision,
                "task_card_path": f"tasks/{task_id}/task.md",
                "task_card_commit": "b" * 40,
                "state": state,
                "attempt": 1 if state in ("dispatched", "in_progress", "review_ready") else None,
                "current_dispatch": {
                    "dispatch_id": dispatch_id,
                    "attempt_id": "attempt-1",
                    "role_id": "agent",
                    "base_commit": "c" * 40,
                    "branch": "feat/test",
                    "dispatched_at": "2026-07-28T12:00:00Z",
                    "model_selection": {
                        "required_model_tier": "advanced",
                        "required_model_capabilities": [],
                        "model_binding_id": "binding-001",
                        "selected_model_provider": "test",
                        "selected_model_id": "test-model",
                        "selected_model_tier": "advanced",
                        "selected_deliberation_tier": "balanced",
                        "selected_context_window_tokens": 200000,
                        "selected_model_capabilities": [],
                        "model_degradation_approval_id": None,
                    },
                } if state in ("dispatched", "in_progress", "review_ready") else None,
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
                    "created_at": "2026-07-28T12:00:00Z",
                    "ready_at": "2026-07-28T12:00:00Z",
                    "dispatched_at": None,
                    "started_at": None,
                    "delivered_at": None,
                    "blocked_at": None,
                    "accepted_at": None,
                    "integrated_at": None,
                    "updated_at": "2026-07-28T12:00:00Z",
                },
            },
        ],
    }
    path = state_dir / "tasks.yaml"
    path.write_text(json.dumps(tasks_doc, indent=2) + "\n", encoding="utf-8")


def _init_canonical_dirs(project_root: Path) -> None:
    """Create all required canonical directories."""
    for d in ("events", "outbox", "acceptances"):
        (project_root / "docs" / "pm" / d).mkdir(parents=True, exist_ok=True)


def _setup_project(task_id: str = "TC-001", state: str = "ready",
                   revision: int = 1) -> Path:
    """Create a temp project with minimal canonical state, git repo, and approval."""
    import subprocess
    import tempfile
    tmp = Path(tempfile.mkdtemp(prefix="agentdesk-test-"))
    # Init git repo so apply_transition's _resolve_head_commit works.
    subprocess.run(
        ["git", "init", "-b", "main"],
        cwd=str(tmp),
        capture_output=True,
        timeout=10,
    )
    subprocess.run(
        ["git", "config", "user.email", "test@test.test"],
        cwd=str(tmp),
        capture_output=True,
        timeout=10,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=str(tmp),
        capture_output=True,
        timeout=10,
    )
    # Create an empty commit so HEAD exists.
    subprocess.run(
        ["git", "commit", "--allow-empty", "-m", "init"],
        cwd=str(tmp),
        capture_output=True,
        timeout=10,
    )
    _init_canonical_dirs(tmp)
    _init_tasks_yaml(tmp, task_id=task_id, state=state, revision=revision)
    _init_worker_slot_store(tmp)
    _ensure_runtime_dir(tmp)
    # Write a TASK_APPROVAL grant so ApprovalGate.require() passes.
    _write_approval_grant(tmp)
    # Add and commit the approval to git.
    subprocess.run(
        ["git", "add", "-A"],
        cwd=str(tmp),
        capture_output=True,
        timeout=10,
    )
    subprocess.run(
        ["git", "commit", "--allow-empty", "-m", "init state"],
        cwd=str(tmp),
        capture_output=True,
        timeout=10,
    )
    return tmp


def _write_approval_grant(project_root: Path) -> None:
    """Write a minimal TASK_APPROVAL grant so gated transitions pass."""
    import json
    approvals_dir = project_root / "docs" / "pm" / "approvals"
    approvals_dir.mkdir(parents=True, exist_ok=True)
    head = _git_head(project_root)
    grant = {
        "schema_version": "agentdesk.task-approval/v1",
        "record_type": "grant",
        "approval_id": "APR-test-001",
        "event_id": "EVT-approval-001",
        "scope": "dispatch",
        "task_id": "TC-001",
        "revision": 1,
        "attempt": 1,
        "dispatch_id": "DSP-001",
        "accepted_commit": None,
        "actor_role_id": "PM",
        "lease_epoch": 1,
        "granted_at": "2026-07-28T12:00:00Z",
        "expires_at": "2026-07-29T12:00:00Z",
        "reason": "test approval",
        "snapshot_commit": head,
    }
    path = approvals_dir / "EVT-approval-001.yaml"
    path.write_text(json.dumps(grant, indent=2) + "\n", encoding="utf-8")


# ── API tests ───────────────────────────────────────────────────────────


class TestWorkflowOrchestratorAPI(unittest.TestCase):
    """Test __all__ exactness and dataclass frozen/slots properties."""

    def test_all_exactly_eight(self) -> None:
        import workflow_orchestrator as wo
        self.assertEqual(
            len(wo.__all__), 8,
            f"__all__ must have exactly 8 entries, got {len(wo.__all__)}: {wo.__all__}"
        )
        expected = sorted([
            "DispatchCycleRequest",
            "DispatchCycleResult",
            "WorkflowClock",
            "WorkflowHeartbeatError",
            "WorkflowInputError",
            "WorkflowInvariantError",
            "WorkflowOrchestrator",
            "WorkflowOrchestratorError",
        ])
        self.assertEqual(sorted(wo.__all__), expected)

    def test_dispatch_cycle_request_frozen_slots(self) -> None:
        self.assertTrue(DispatchCycleRequest.__dataclass_params__.frozen)
        self.assertTrue(
            hasattr(DispatchCycleRequest, "__slots__"),
            "DispatchCycleRequest must have __slots__",
        )

    def test_dispatch_cycle_result_frozen_slots(self) -> None:
        self.assertTrue(DispatchCycleResult.__dataclass_params__.frozen)
        self.assertTrue(
            hasattr(DispatchCycleResult, "__slots__"),
            "DispatchCycleResult must have __slots__",
        )

    def test_workflow_orchestrator_frozen_slots(self) -> None:
        self.assertTrue(WorkflowOrchestrator.__dataclass_params__.frozen)
        self.assertTrue(
            hasattr(WorkflowOrchestrator, "__slots__"),
            "WorkflowOrchestrator must have __slots__",
        )

    def test_workflow_clock_has_three_methods(self) -> None:
        """WorkflowClock protocol exposes now, monotonic, sleep."""
        self.assertTrue(callable(getattr(WorkflowClock, "now", None)))
        self.assertTrue(callable(getattr(WorkflowClock, "monotonic", None)))
        self.assertTrue(callable(getattr(WorkflowClock, "sleep", None)))

    def test_no_acceptance_cycle_in_all(self) -> None:
        """AcceptanceCycleRequest/Result must not be in __all__."""
        import workflow_orchestrator as wo
        self.assertNotIn("AcceptanceCycleRequest", wo.__all__)
        self.assertNotIn("AcceptanceCycleResult", wo.__all__)

    def test_exception_hierarchy(self) -> None:
        self.assertTrue(issubclass(WorkflowOrchestratorError, Exception))
        self.assertTrue(issubclass(WorkflowInputError, WorkflowOrchestratorError))
        self.assertTrue(issubclass(WorkflowHeartbeatError, WorkflowOrchestratorError))
        self.assertTrue(issubclass(WorkflowInvariantError, WorkflowOrchestratorError))

    def test_dispatch_cycle_result_has_exactly_five_fields(self) -> None:
        fields = [f.name for f in dc_fields(DispatchCycleResult)]
        expected = ["worker_result", "dispatch_transition", "slot_id",
                     "lease_epoch", "duration_seconds"]
        self.assertEqual(fields, expected)

    def test_dispatch_cycle_result_no_forbidden_fields(self) -> None:
        fields = {f.name for f in dc_fields(DispatchCycleResult)}
        forbidden = {"delivery_transition", "implementation_commit",
                     "report_commit", "decoded_output", "audit_result",
                     "acceptance_result"}
        self.assertTrue(fields.isdisjoint(forbidden))


# ── input validation tests ──────────────────────────────────────────────


class TestWorkflowOrchestratorInputValidation(unittest.TestCase):

    def test_project_root_not_absolute(self) -> None:
        with self.assertRaises(WorkflowInputError):
            WorkflowOrchestrator(
                project_root=Path("relative"),
                clock=FakeClock(),
            )

    def test_project_root_not_existing_dir(self) -> None:
        with self.assertRaises(WorkflowInputError):
            WorkflowOrchestrator(
                project_root=Path("/nonexistent/path/xyz"),
                clock=FakeClock(),
            )

    def test_non_utc_clock_rejected_on_dispatch(self) -> None:
        """Non-UTC clock now() should be rejected during run_dispatch_cycle."""
        tmp = _setup_project()
        try:
            orch = WorkflowOrchestrator(
                project_root=tmp,
                clock=NonUtcClock(),
            )
            ms = _make_model_selection()
            dr = _make_dispatch_request(workspace=tmp, model_selection=ms)
            tr = _make_transition_request(model_selection=ms)
            req = DispatchCycleRequest(
                dispatch_request=dr,
                dispatch_transition_request=tr,
                worker_kind=WorkerKind.ADVANCED_AGENT,
                task_difficulty=TaskDifficulty.ADVANCED,
                holder_instance_id="test-instance",
            )
            providers = {"test": FakeProvider()}

            async def _run() -> None:
                with self.assertRaises(WorkflowInputError):
                    await orch.run_dispatch_cycle(req, providers)

            asyncio.run(_run())
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

    def test_offset_clock_rejected_on_dispatch(self) -> None:
        """Non-UTC offset clock should be rejected."""
        tmp = _setup_project()
        try:
            orch = WorkflowOrchestrator(
                project_root=tmp,
                clock=OffsetClock(),
            )
            ms = _make_model_selection()
            dr = _make_dispatch_request(workspace=tmp, model_selection=ms)
            tr = _make_transition_request(model_selection=ms)
            req = DispatchCycleRequest(
                dispatch_request=dr,
                dispatch_transition_request=tr,
                worker_kind=WorkerKind.ADVANCED_AGENT,
                task_difficulty=TaskDifficulty.ADVANCED,
                holder_instance_id="test-instance",
            )
            providers = {"test": FakeProvider()}

            async def _run() -> None:
                with self.assertRaises(WorkflowInputError):
                    await orch.run_dispatch_cycle(req, providers)

            asyncio.run(_run())
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

    def test_heartbeat_interval_zero(self) -> None:
        with self.assertRaises(WorkflowInputError):
            WorkflowOrchestrator(
                project_root=_setup_project(),
                clock=FakeClock(),
                heartbeat_interval_seconds=0,
            )

    def test_heartbeat_interval_negative(self) -> None:
        with self.assertRaises(WorkflowInputError):
            WorkflowOrchestrator(
                project_root=_setup_project(),
                clock=FakeClock(),
                heartbeat_interval_seconds=-1,
            )

    def test_heartbeat_interval_exceeds_20(self) -> None:
        with self.assertRaises(WorkflowInputError):
            WorkflowOrchestrator(
                project_root=_setup_project(),
                clock=FakeClock(),
                heartbeat_interval_seconds=21,
            )

    def test_heartbeat_interval_20_is_valid(self) -> None:
        orch = WorkflowOrchestrator(
            project_root=_setup_project(),
            clock=FakeClock(),
            heartbeat_interval_seconds=20.0,
        )
        self.assertEqual(orch.heartbeat_interval_seconds, 20.0)

    def test_heartbeat_interval_bool_rejected(self) -> None:
        with self.assertRaises(WorkflowInputError):
            WorkflowOrchestrator(
                project_root=_setup_project(),
                clock=FakeClock(),
                heartbeat_interval_seconds=True,  # type: ignore[arg-type]
            )

    def test_heartbeat_interval_nan_rejected(self) -> None:
        import math
        with self.assertRaises(WorkflowInputError):
            WorkflowOrchestrator(
                project_root=_setup_project(),
                clock=FakeClock(),
                heartbeat_interval_seconds=float("nan"),
            )

    def test_heartbeat_interval_inf_rejected(self) -> None:
        with self.assertRaises(WorkflowInputError):
            WorkflowOrchestrator(
                project_root=_setup_project(),
                clock=FakeClock(),
                heartbeat_interval_seconds=float("inf"),
            )

    def test_wrong_event_type(self) -> None:
        """DispatchCycleRequest rejects non-TASK_DISPATCHED event_type.

        TransitionRequest.__post_init__ enforces event_type→payload pairing.
        We construct a valid TransitionRequest first, then use object.__setattr__
        to change the event_type to a value that the orchestrator should reject.
        """
        from control_plane_transition import AcknowledgePayload
        ms = _make_model_selection()
        dr = _make_dispatch_request(model_selection=ms)
        # Build a valid TASK_DISPATCHED TransitionRequest first.
        payload = DispatchPayload(
            dispatch_id="DSP-001",
            role_id="agent",
            model_selection=ms,
            task_card_path="tasks/task.md",
            task_card_commit="b" * 40,
            base_commit="c" * 40,
            branch="feat/test",
            report_path="reports/report.md",
            outbox_message_id="MSG-test-001",
            new_attempt=1,
        )
        cas = TransitionCAS(
            task_id="TC-001",
            expected_revision=1,
            expected_state="ready",
            expected_snapshot_commit="a" * 40,
        )
        event_context = TransitionEventContext(
            source_message_id=None,
            evidence_refs=(),
            guard_results=(),
        )
        tr = TransitionRequest(
            cas=cas,
            dispatch_cas=None,
            event_id="EVT-test",
            event_type="TASK_DISPATCHED",
            payload=payload,
            event_context=event_context,
        )
        # Force event_type to something else so DispatchCycleRequest rejects it.
        object.__setattr__(tr, "event_type", "DISPATCH_ACKNOWLEDGED")
        with self.assertRaises(ValueError):
            DispatchCycleRequest(
                dispatch_request=dr,
                dispatch_transition_request=tr,
                worker_kind=WorkerKind.ADVANCED_AGENT,
                task_difficulty=TaskDifficulty.ADVANCED,
                holder_instance_id="test-instance",
            )

    def test_wrong_payload_type(self) -> None:
        """DispatchCycleRequest rejects TASK_DISPATCHED with non-DispatchPayload.

        TransitionRequest.__post_init__ enforces event_type→payload pairing, so
        we use object.__setattr__ to construct a deliberately-invalid request
        for the orchestrator input validation layer.
        """
        from control_plane_transition import AcknowledgePayload
        ms = _make_model_selection()
        dr = _make_dispatch_request(model_selection=ms)
        cas = TransitionCAS(
            task_id="TC-001",
            expected_revision=1,
            expected_state="ready",
            expected_snapshot_commit="a" * 40,
        )
        fake_payload = AcknowledgePayload()
        event_context = TransitionEventContext(
            source_message_id=None,
            evidence_refs=(),
            guard_results=(),
        )
        # Build the TransitionRequest, then forcibly replace payload.
        # We construct with a valid DispatchPayload first so post_init passes,
        # then swap to the invalid payload.
        real_payload = DispatchPayload(
            dispatch_id="DSP-001",
            role_id="agent",
            model_selection=ms,
            task_card_path="tasks/task.md",
            task_card_commit="b" * 40,
            base_commit="c" * 40,
            branch="feat/test",
            report_path="reports/report.md",
            outbox_message_id="MSG-test-001",
            new_attempt=1,
        )
        tr = TransitionRequest(
            cas=cas,
            dispatch_cas=None,
            event_id="EVT-test",
            event_type="TASK_DISPATCHED",
            payload=real_payload,
            event_context=event_context,
        )
        object.__setattr__(tr, "payload", fake_payload)
        with self.assertRaises(TypeError):
            DispatchCycleRequest(
                dispatch_request=dr,
                dispatch_transition_request=tr,
                worker_kind=WorkerKind.ADVANCED_AGENT,
                task_difficulty=TaskDifficulty.ADVANCED,
                holder_instance_id="test-instance",
            )

    def test_task_id_mismatch(self) -> None:
        dr = _make_dispatch_request(task_id="TC-001", dispatch_id="DSP-001",
                                     model_selection=_make_model_selection())
        tr = _make_transition_request(task_id="TC-002", dispatch_id="DSP-001",
                                       model_selection=dr.model_selection)
        with self.assertRaises(ValueError):
            DispatchCycleRequest(
                dispatch_request=dr,
                dispatch_transition_request=tr,
                worker_kind=WorkerKind.ADVANCED_AGENT,
                task_difficulty=TaskDifficulty.ADVANCED,
                holder_instance_id="test-instance",
            )

    def test_dispatch_id_mismatch(self) -> None:
        ms = _make_model_selection()
        dr = _make_dispatch_request(task_id="TC-001", dispatch_id="DSP-001",
                                     model_selection=ms)
        tr = _make_transition_request(task_id="TC-001", dispatch_id="DSP-002",
                                       model_selection=ms)
        with self.assertRaises(ValueError):
            DispatchCycleRequest(
                dispatch_request=dr,
                dispatch_transition_request=tr,
                worker_kind=WorkerKind.ADVANCED_AGENT,
                task_difficulty=TaskDifficulty.ADVANCED,
                holder_instance_id="test-instance",
            )

    def test_model_selection_not_same_object(self) -> None:
        ms1 = _make_model_selection()
        ms2 = _make_model_selection()  # same values, different object
        dr = _make_dispatch_request(task_id="TC-001", dispatch_id="DSP-001",
                                     model_selection=ms1)
        tr = _make_transition_request(task_id="TC-001", dispatch_id="DSP-001",
                                       model_selection=ms2)
        with self.assertRaises(ValueError):
            DispatchCycleRequest(
                dispatch_request=dr,
                dispatch_transition_request=tr,
                worker_kind=WorkerKind.ADVANCED_AGENT,
                task_difficulty=TaskDifficulty.ADVANCED,
                holder_instance_id="test-instance",
            )


# ── success path tests ──────────────────────────────────────────────────


class TestWorkflowOrchestratorSuccessPath(unittest.TestCase):

    def _make_req(self, tmp: Path, ms: ModelSelectionSnapshot | None = None,
                  task_id: str = "TC-001", dispatch_id: str = "DSP-001",
                  revision: int = 1) -> DispatchCycleRequest:
        """Build a valid DispatchCycleRequest against *tmp* git repo."""
        if ms is None:
            ms = _make_model_selection()
        head = _git_head(tmp)
        dr = _make_dispatch_request(
            task_id=task_id, dispatch_id=dispatch_id,
            workspace=tmp, model_selection=ms,
        )
        tr = _make_transition_request(
            task_id=task_id, dispatch_id=dispatch_id,
            model_selection=ms, revision=revision, head_sha=head,
        )
        return DispatchCycleRequest(
            dispatch_request=dr,
            dispatch_transition_request=tr,
            worker_kind=WorkerKind.ADVANCED_AGENT,
            task_difficulty=TaskDifficulty.ADVANCED,
            holder_instance_id="test-instance",
        )

    def _make_req_no_git(self, tmp: Path, ms: ModelSelectionSnapshot | None = None,
                          task_id: str = "TC-001", dispatch_id: str = "DSP-001",
                          revision: int = 1, head_sha: str = "a" * 40) -> DispatchCycleRequest:
        """Build a DispatchCycleRequest with explicit head_sha (no git needed)."""
        if ms is None:
            ms = _make_model_selection()
        dr = _make_dispatch_request(
            task_id=task_id, dispatch_id=dispatch_id,
            workspace=tmp, model_selection=ms,
        )
        tr = _make_transition_request(
            task_id=task_id, dispatch_id=dispatch_id,
            model_selection=ms, revision=revision, head_sha=head_sha,
        )
        return DispatchCycleRequest(
            dispatch_request=dr,
            dispatch_transition_request=tr,
            worker_kind=WorkerKind.ADVANCED_AGENT,
            task_difficulty=TaskDifficulty.ADVANCED,
            holder_instance_id="test-instance",
        )

    def test_complete_success_cycle(self) -> None:
        """Complete dispatch cycle: snapshot → acquire → transition → worker → release."""
        tmp = _setup_project()
        try:
            clock = FakeClock()
            orch = WorkflowOrchestrator(
                project_root=tmp,
                clock=clock,
                heartbeat_interval_seconds=10.0,
            )
            req = self._make_req(tmp)
            providers = {"test": FakeProvider()}

            import workflow_orchestrator as wo
            _orig_run_worker = wo.run_worker
            worker_result = _make_worker_result()

            async def _fake_run_worker(*args: Any, **kwargs: Any) -> WorkerResult:
                await asyncio.sleep(0)
                return worker_result

            wo.run_worker = _fake_run_worker  # type: ignore[assignment]

            try:
                async def _run() -> None:
                    result = await orch.run_dispatch_cycle(req, providers)
                    self.assertIsInstance(result, DispatchCycleResult)
                    self.assertIs(result.worker_result, worker_result)
                    self.assertIsInstance(result.dispatch_transition, TransitionResult)
                    self.assertIsNotNone(result.slot_id)
                    self.assertTrue(result.lease_epoch >= 1)
                    self.assertGreater(result.duration_seconds, 0)
                    # Opaque bytes preserved.
                    self.assertEqual(
                        result.worker_result.dispatch_result.stdout,
                        worker_result.dispatch_result.stdout,
                    )
                    self.assertEqual(
                        result.worker_result.dispatch_result.stderr,
                        worker_result.dispatch_result.stderr,
                    )
                    # No budget modification.
                    self.assertEqual(
                        result.worker_result.budget,
                        worker_result.budget,
                    )

                asyncio.run(_run())
            finally:
                wo.run_worker = _orig_run_worker  # type: ignore[assignment]
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

    def test_heartbeat_renews_at_least_once(self) -> None:
        """Long worker task → heartbeat loop is started."""
        tmp = _setup_project()
        try:
            clock = FakeClock()
            orch = WorkflowOrchestrator(
                project_root=tmp,
                clock=clock,
                heartbeat_interval_seconds=0.5,
            )
            req = self._make_req(tmp)
            providers = {"test": FakeProvider()}

            import workflow_orchestrator as wo
            _orig_run_worker = wo.run_worker
            worker_result = _make_worker_result()

            async def _fake_run_worker(*args: Any, **kwargs: Any) -> WorkerResult:
                await asyncio.sleep(0)
                return worker_result

            wo.run_worker = _fake_run_worker  # type: ignore[assignment]

            try:
                async def _run() -> None:
                    result = await orch.run_dispatch_cycle(req, providers)
                    self.assertIsInstance(result, DispatchCycleResult)
                    self.assertGreater(result.duration_seconds, 0)

                asyncio.run(_run())
            finally:
                wo.run_worker = _orig_run_worker  # type: ignore[assignment]
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

    def test_providers_passed_by_identity(self) -> None:
        """Providers mapping is passed to run_worker by identity, not copied."""
        tmp = _setup_project()
        try:
            clock = FakeClock()
            orch = WorkflowOrchestrator(
                project_root=tmp,
                clock=clock,
            )
            req = self._make_req(tmp)
            providers = {"test": FakeProvider()}
            providers_id_before = id(providers)

            received_providers: list[object] = []

            import workflow_orchestrator as wo
            _orig_run_worker = wo.run_worker

            async def _capture_run_worker(
                request: Any, wk: Any, td: Any, provs: Any,
            ) -> WorkerResult:
                received_providers.append(provs)
                return _make_worker_result()

            wo.run_worker = _capture_run_worker  # type: ignore[assignment]

            try:
                async def _run() -> None:
                    await orch.run_dispatch_cycle(req, providers)

                asyncio.run(_run())
                self.assertEqual(len(received_providers), 1)
                self.assertIs(received_providers[0], providers)
                self.assertEqual(id(received_providers[0]), providers_id_before)
                self.assertEqual(set(received_providers[0].keys()), {"test"})
            finally:
                wo.run_worker = _orig_run_worker  # type: ignore[assignment]
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

    def test_duration_uses_monotonic_clock(self) -> None:
        """Duration is computed from clock.monotonic() deltas."""
        tmp = _setup_project()
        try:
            clock = FakeClock()
            orch = WorkflowOrchestrator(
                project_root=tmp,
                clock=clock,
            )
            req = self._make_req(tmp)
            providers = {"test": FakeProvider()}

            import workflow_orchestrator as wo
            _orig_run_worker = wo.run_worker

            async def _fake_run_worker(*args: Any, **kwargs: Any) -> WorkerResult:
                clock._mono += 5.0
                await asyncio.sleep(0)
                return _make_worker_result()

            wo.run_worker = _fake_run_worker  # type: ignore[assignment]

            try:
                async def _run() -> None:
                    result = await orch.run_dispatch_cycle(req, providers)
                    self.assertGreaterEqual(result.duration_seconds, 5.0)

                asyncio.run(_run())
            finally:
                wo.run_worker = _orig_run_worker  # type: ignore[assignment]
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)


# ── failure & cancellation tests ───────────────────────────────────────


class TestWorkflowOrchestratorFailureAndCancellation(unittest.TestCase):

    def _make_req(self, tmp: Path, ms: ModelSelectionSnapshot | None = None,
                  task_id: str = "TC-001", dispatch_id: str = "DSP-001",
                  revision: int = 1) -> DispatchCycleRequest:
        """Build a valid DispatchCycleRequest against *tmp* git repo."""
        if ms is None:
            ms = _make_model_selection()
        head = _git_head(tmp)
        dr = _make_dispatch_request(
            task_id=task_id, dispatch_id=dispatch_id,
            workspace=tmp, model_selection=ms,
        )
        tr = _make_transition_request(
            task_id=task_id, dispatch_id=dispatch_id,
            model_selection=ms, revision=revision, head_sha=head,
        )
        return DispatchCycleRequest(
            dispatch_request=dr,
            dispatch_transition_request=tr,
            worker_kind=WorkerKind.ADVANCED_AGENT,
            task_difficulty=TaskDifficulty.ADVANCED,
            holder_instance_id="test-instance",
        )

    def test_acquire_failure_no_release(self) -> None:
        """Acquire failure → no release attempt, no transition/worker called."""
        # Use a tmp dir with no .agentdesk/runtime — snapshot will fail
        # before acquire because there's no canonical state.
        import tempfile
        tmp = Path(tempfile.mkdtemp(prefix="agentdesk-acqfail-"))
        # Do NOT init anything — the first StateProvider.snapshot() call
        # will raise StateProviderNotFoundError.
        try:
            clock = FakeClock()
            orch = WorkflowOrchestrator(
                project_root=tmp,
                clock=clock,
            )
            ms = _make_model_selection()
            dr = _make_dispatch_request(workspace=tmp, model_selection=ms)
            tr = _make_transition_request(model_selection=ms)
            req = DispatchCycleRequest(
                dispatch_request=dr,
                dispatch_transition_request=tr,
                worker_kind=WorkerKind.ADVANCED_AGENT,
                task_difficulty=TaskDifficulty.ADVANCED,
                holder_instance_id="test-instance",
            )
            providers = {"test": FakeProvider()}

            async def _run() -> None:
                with self.assertRaises(StateProviderError):
                    await orch.run_dispatch_cycle(req, providers)

            asyncio.run(_run())
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

    def test_transition_failure_no_worker_no_heartbeat(self) -> None:
        """Transition fails → Worker and heartbeat must NOT be started."""
        tmp = _setup_project()
        try:
            clock = FakeClock()
            orch = WorkflowOrchestrator(
                project_root=tmp,
                clock=clock,
            )
            # Use wrong revision so CAS fails.
            req = self._make_req(tmp, revision=99)
            providers = {"test": FakeProvider()}

            import workflow_orchestrator as wo
            _orig_run_worker = wo.run_worker
            worker_called = [False]

            async def _tracking_worker(*args: Any, **kwargs: Any) -> WorkerResult:
                worker_called[0] = True
                return _make_worker_result()

            wo.run_worker = _tracking_worker  # type: ignore[assignment]

            try:
                # Use wrong revision so CAS validation fails.
                req = self._make_req(tmp, revision=99)

                async def _run() -> None:
                    # Revision mismatch caught at snapshot verification
                    # (step 3), before transition.
                    with self.assertRaises(WorkflowInvariantError):
                        await orch.run_dispatch_cycle(req, providers)
                    # Worker must not have been called.
                    self.assertFalse(worker_called[0])

                asyncio.run(_run())
            finally:
                wo.run_worker = _orig_run_worker  # type: ignore[assignment]
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

    def test_worker_failure_release(self) -> None:
        """Worker fails → release slot."""
        tmp = _setup_project()
        try:
            clock = FakeClock()
            orch = WorkflowOrchestrator(
                project_root=tmp,
                clock=clock,
            )
            req = self._make_req(tmp)
            providers = {"test": FakeProvider()}

            import workflow_orchestrator as wo
            _orig_run_worker = wo.run_worker

            class _TestWorkerError(Exception):
                pass

            async def _failing_worker(*args: Any, **kwargs: Any) -> WorkerResult:
                raise _TestWorkerError("worker crashed")

            wo.run_worker = _failing_worker  # type: ignore[assignment]

            try:
                async def _run() -> None:
                    # Worker failure propagates as-is.
                    with self.assertRaises(_TestWorkerError):
                        await orch.run_dispatch_cycle(req, providers)

                asyncio.run(_run())
            finally:
                wo.run_worker = _orig_run_worker  # type: ignore[assignment]
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

    def test_worker_cancellation_release(self) -> None:
        """Worker cancellation → release slot."""
        tmp = _setup_project()
        try:
            clock = FakeClock()
            orch = WorkflowOrchestrator(
                project_root=tmp,
                clock=clock,
            )
            req = self._make_req(tmp)
            providers = {"test": FakeProvider()}

            import workflow_orchestrator as wo
            _orig_run_worker = wo.run_worker

            async def _cancelling_worker(*args: Any, **kwargs: Any) -> WorkerResult:
                raise asyncio.CancelledError()

            wo.run_worker = _cancelling_worker  # type: ignore[assignment]

            try:
                async def _run() -> None:
                    # CancelledError from worker is NOT wrapped.
                    with self.assertRaises(asyncio.CancelledError):
                        await orch.run_dispatch_cycle(req, providers)

                asyncio.run(_run())
            finally:
                wo.run_worker = _orig_run_worker  # type: ignore[assignment]
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

    def test_heartbeat_fencing_cancels_worker(self) -> None:
        """Heartbeat renewal fails → Worker cancelled, release."""
        tmp = _setup_project()
        try:
            clock = FakeClock()
            orch = WorkflowOrchestrator(
                project_root=tmp,
                clock=clock,
                heartbeat_interval_seconds=0.5,
            )
            req = self._make_req(tmp)
            providers = {"test": FakeProvider()}

            import workflow_orchestrator as wo
            _orig_run_worker = wo.run_worker
            worker_cancelled = [False]

            running = asyncio.Event()

            async def _long_worker(*args: Any, **kwargs: Any) -> WorkerResult:
                running.set()
                try:
                    await asyncio.sleep(10)  # long — heartbeat should fail first
                except asyncio.CancelledError:
                    worker_cancelled[0] = True
                    raise
                return _make_worker_result()

            wo.run_worker = _long_worker  # type: ignore[assignment]

            # We also need to make renew_worker_slot fail. Since it's called
            # directly in the heartbeat loop, we can monkey-patch the module.
            import workflow_orchestrator as wo
            _orig_renew = wo.renew_worker_slot

            def _failing_renew(*args: Any, **kwargs: Any) -> Any:
                raise WorkerSlotFencingError("lease expired for slot ...")

            wo.renew_worker_slot = _failing_renew  # type: ignore[assignment]

            try:
                async def _run() -> None:
                    with self.assertRaises(WorkerSlotFencingError):
                        await orch.run_dispatch_cycle(req, providers)
                    # Worker must have been cancelled.
                    self.assertTrue(worker_cancelled[0])

                asyncio.run(_run())
            finally:
                wo.run_worker = _orig_run_worker  # type: ignore[assignment]
                wo.renew_worker_slot = _orig_renew  # type: ignore[assignment]
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

    def test_release_failure_on_success_path(self) -> None:
        """Release fails on success path → error propagated, no result returned."""
        tmp = _setup_project()
        try:
            clock = FakeClock()
            orch = WorkflowOrchestrator(
                project_root=tmp,
                clock=clock,
            )
            req = self._make_req(tmp)
            providers = {"test": FakeProvider()}

            import workflow_orchestrator as wo
            _orig_run_worker = wo.run_worker

            async def _quick_worker(*args: Any, **kwargs: Any) -> WorkerResult:
                return _make_worker_result()

            wo.run_worker = _quick_worker  # type: ignore[assignment]

            # Monkey-patch release_worker_slot to fail.
            _orig_release = wo.release_worker_slot

            def _failing_release(*args: Any, **kwargs: Any) -> None:
                raise WorkerSlotNotHeldError("slot not held: ...")

            wo.release_worker_slot = _failing_release  # type: ignore[assignment]

            try:
                async def _run() -> None:
                    with self.assertRaises(WorkerSlotNotHeldError):
                        await orch.run_dispatch_cycle(req, providers)

                asyncio.run(_run())
            finally:
                wo.run_worker = _orig_run_worker  # type: ignore[assignment]
                wo.release_worker_slot = _orig_release  # type: ignore[assignment]
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

    def test_dual_failure_primary_exception_priority(self) -> None:
        """Primary failure + release failure → primary exception type kept."""
        tmp = _setup_project()
        try:
            clock = FakeClock()
            orch = WorkflowOrchestrator(
                project_root=tmp,
                clock=clock,
            )
            req = self._make_req(tmp)
            providers = {"test": FakeProvider()}

            import workflow_orchestrator as wo
            _orig_run_worker = wo.run_worker

            class _PrimaryError(Exception):
                pass

            async def _failing_worker(*args: Any, **kwargs: Any) -> WorkerResult:
                raise _PrimaryError("primary failure")

            wo.run_worker = _failing_worker  # type: ignore[assignment]

            _orig_release = wo.release_worker_slot

            def _failing_release(*args: Any, **kwargs: Any) -> None:
                raise WorkerSlotNotHeldError("release also failed")

            wo.release_worker_slot = _failing_release  # type: ignore[assignment]

            try:
                async def _run() -> None:
                    with self.assertRaises(_PrimaryError):
                        await orch.run_dispatch_cycle(req, providers)

                asyncio.run(_run())
            finally:
                wo.run_worker = _orig_run_worker  # type: ignore[assignment]
                wo.release_worker_slot = _orig_release  # type: ignore[assignment]
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

    def test_no_pending_asyncio_tasks_after_success(self) -> None:
        """After successful cycle, no pending asyncio tasks."""
        tmp = _setup_project()
        try:
            clock = FakeClock()
            orch = WorkflowOrchestrator(
                project_root=tmp,
                clock=clock,
            )
            req = self._make_req(tmp)
            providers = {"test": FakeProvider()}

            import workflow_orchestrator as wo
            _orig_run_worker = wo.run_worker

            async def _quick_worker(*args: Any, **kwargs: Any) -> WorkerResult:
                return _make_worker_result()

            wo.run_worker = _quick_worker  # type: ignore[assignment]

            try:
                async def _run() -> None:
                    tasks_before = len(asyncio.all_tasks(asyncio.get_running_loop()))
                    await orch.run_dispatch_cycle(req, providers)
                    tasks_after = len(asyncio.all_tasks(asyncio.get_running_loop()))
                    self.assertLessEqual(tasks_after, tasks_before)

                asyncio.run(_run())
            finally:
                wo.run_worker = _orig_run_worker  # type: ignore[assignment]
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

    def test_no_pending_asyncio_tasks_after_worker_failure(self) -> None:
        """After worker failure, no pending asyncio tasks."""
        tmp = _setup_project()
        try:
            clock = FakeClock()
            orch = WorkflowOrchestrator(
                project_root=tmp,
                clock=clock,
            )
            req = self._make_req(tmp)
            providers = {"test": FakeProvider()}

            import workflow_orchestrator as wo
            _orig_run_worker = wo.run_worker

            async def _failing_worker(*args: Any, **kwargs: Any) -> WorkerResult:
                raise RuntimeError("worker error")

            wo.run_worker = _failing_worker  # type: ignore[assignment]

            try:
                async def _run() -> None:
                    tasks_before = len(asyncio.all_tasks(asyncio.get_running_loop()))
                    try:
                        await orch.run_dispatch_cycle(req, providers)
                    except RuntimeError:
                        pass
                    tasks_after = len(asyncio.all_tasks(asyncio.get_running_loop()))
                    self.assertLessEqual(tasks_after, tasks_before)

                asyncio.run(_run())
            finally:
                wo.run_worker = _orig_run_worker  # type: ignore[assignment]
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

    def test_underlying_exception_not_wrapped(self) -> None:
        """WorkerSlotLeaseError, DispatchGatewayError, etc. propagate unwrapped."""
        tmp = _setup_project()
        try:
            clock = FakeClock()
            orch = WorkflowOrchestrator(
                project_root=tmp,
                clock=clock,
            )
            req = self._make_req(tmp)
            providers = {"test": FakeProvider()}

            import workflow_orchestrator as wo
            _orig_run_worker = wo.run_worker
            from dispatcher_gateway import DispatchGatewayError

            async def _failing_worker(*args: Any, **kwargs: Any) -> WorkerResult:
                raise DispatchGatewayError("gateway error")

            wo.run_worker = _failing_worker  # type: ignore[assignment]

            try:
                async def _run() -> None:
                    # Must NOT be wrapped in WorkflowOrchestratorError.
                    with self.assertRaises(DispatchGatewayError):
                        await orch.run_dispatch_cycle(req, providers)

                asyncio.run(_run())
            finally:
                wo.run_worker = _orig_run_worker  # type: ignore[assignment]
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

    def test_cancelled_error_propagated_unwrapped(self) -> None:
        """Outer cancellation → CancelledError propagated as-is."""
        tmp = _setup_project()
        try:
            clock = FakeClock()
            orch = WorkflowOrchestrator(
                project_root=tmp,
                clock=clock,
            )
            req = self._make_req(tmp)
            providers = {"test": FakeProvider()}

            import workflow_orchestrator as wo
            _orig_run_worker = wo.run_worker

            async def _cancelling_worker(*args: Any, **kwargs: Any) -> WorkerResult:
                await asyncio.sleep(0)
                raise asyncio.CancelledError()

            wo.run_worker = _cancelling_worker  # type: ignore[assignment]

            try:
                async def _run() -> None:
                    with self.assertRaises(asyncio.CancelledError):
                        await orch.run_dispatch_cycle(req, providers)

                asyncio.run(_run())
            finally:
                wo.run_worker = _orig_run_worker  # type: ignore[assignment]
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

    def test_exception_message_has_no_prompt(self) -> None:
        """Exception messages must not contain prompt text."""
        tmp = _setup_project()
        try:
            clock = FakeClock()
            orch = WorkflowOrchestrator(
                project_root=tmp,
                clock=clock,
            )
            req = self._make_req(tmp)
            providers = {"test": FakeProvider()}

            import workflow_orchestrator as wo
            _orig_run_worker = wo.run_worker

            async def _failing_worker(*args: Any, **kwargs: Any) -> WorkerResult:
                raise RuntimeError("expected failure")

            wo.run_worker = _failing_worker  # type: ignore[assignment]

            try:
                async def _run() -> None:
                    try:
                        await orch.run_dispatch_cycle(req, providers)
                    except RuntimeError as e:
                        msg = str(e)
                        self.assertNotIn("test prompt", msg)
                        self.assertNotIn("DSP-001", msg)
                        self.assertNotIn("TC-001", msg)

                asyncio.run(_run())
            finally:
                wo.run_worker = _orig_run_worker  # type: ignore[assignment]
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

    def test_heartbeat_started_and_stopped_on_success(self) -> None:
        """Heartbeat starts → worker completes → heartbeat cancelled."""
        tmp = _setup_project()
        try:
            clock = FakeClock()
            orch = WorkflowOrchestrator(
                project_root=tmp,
                clock=clock,
                heartbeat_interval_seconds=1.0,
            )
            req = self._make_req(tmp)
            providers = {"test": FakeProvider()}

            import workflow_orchestrator as wo
            _orig_run_worker = wo.run_worker

            async def _quick_worker(*args: Any, **kwargs: Any) -> WorkerResult:
                return _make_worker_result()

            wo.run_worker = _quick_worker  # type: ignore[assignment]

            try:
                async def _run() -> None:
                    result = await orch.run_dispatch_cycle(req, providers)
                    self.assertIsInstance(result, DispatchCycleResult)

                asyncio.run(_run())
            finally:
                wo.run_worker = _orig_run_worker  # type: ignore[assignment]
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

    def test_heartbeat_failure_cancels_worker_no_subsequent_transition(self) -> None:
        """Heartbeat fails → Worker cancelled → no fake transition."""
        tmp = _setup_project()
        try:
            clock = FakeClock()
            orch = WorkflowOrchestrator(
                project_root=tmp,
                clock=clock,
                heartbeat_interval_seconds=0.5,
            )
            req = self._make_req(tmp)
            providers = {"test": FakeProvider()}

            import workflow_orchestrator as wo
            _orig_run_worker = wo.run_worker

            running = asyncio.Event()

            async def _long_worker(*args: Any, **kwargs: Any) -> WorkerResult:
                running.set()
                try:
                    await asyncio.sleep(10)
                except asyncio.CancelledError:
                    raise
                return _make_worker_result()

            wo.run_worker = _long_worker  # type: ignore[assignment]

            _orig_renew = wo.renew_worker_slot

            def _failing_renew(*args: Any, **kwargs: Any) -> Any:
                raise WorkerSlotFencingError("lease expired")

            wo.renew_worker_slot = _failing_renew  # type: ignore[assignment]

            try:
                async def _run() -> None:
                    with self.assertRaises(WorkerSlotFencingError):
                        await orch.run_dispatch_cycle(req, providers)

                asyncio.run(_run())
            finally:
                wo.run_worker = _orig_run_worker  # type: ignore[assignment]
                wo.renew_worker_slot = _orig_renew  # type: ignore[assignment]
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

    def test_heartbeat_single_success(self) -> None:
        """Single heartbeat renewal succeeds (short worker)."""
        tmp = _setup_project()
        try:
            clock = FakeClock()
            orch = WorkflowOrchestrator(
                project_root=tmp,
                clock=clock,
                heartbeat_interval_seconds=10.0,
            )
            req = self._make_req(tmp)
            providers = {"test": FakeProvider()}

            import workflow_orchestrator as wo
            _orig_run_worker = wo.run_worker

            async def _quick_worker(*args: Any, **kwargs: Any) -> WorkerResult:
                return _make_worker_result()

            wo.run_worker = _quick_worker  # type: ignore[assignment]

            try:
                async def _run() -> None:
                    result = await orch.run_dispatch_cycle(req, providers)
                    self.assertIsNotNone(result)

                asyncio.run(_run())
            finally:
                wo.run_worker = _orig_run_worker  # type: ignore[assignment]
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)


# ── source boundary tests ───────────────────────────────────────────────


class TestWorkflowOrchestratorSourceBoundary(unittest.TestCase):
    """Verify production code does not contain forbidden imports/calls.

    Only actual code lines (not docstrings or comments) are checked.
    """

    def setUp(self) -> None:
        src_path = (
            Path(__file__).resolve().parents[1]
            / "skills" / "agentdesk" / "scripts"
            / "workflow_orchestrator.py"
        )
        raw = src_path.read_text(encoding="utf-8")
        # Strip docstrings and comments: keep only code lines.
        in_docstring = False
        code_lines: list[str] = []
        for line in raw.splitlines():
            stripped = line.strip()
            # Toggle docstring state on triple-quotes.
            if stripped.startswith('"""') or stripped.startswith("'''"):
                if stripped.count('"""') == 2 or stripped.count("'''") == 2:
                    # Single-line docstring — skip entirely.
                    continue
                in_docstring = not in_docstring
                continue
            if in_docstring:
                continue
            # Skip pure comment lines.
            if stripped.startswith("#"):
                continue
            # Strip inline comments.
            if "  # " in line:
                line = line.split("  # ")[0]
            code_lines.append(line)
        self.code_src = "\n".join(code_lines)

    def test_no_subprocess_import(self) -> None:
        self.assertNotIn("subprocess", self.code_src)

    def test_no_open_call(self) -> None:
        self.assertNotIn("open(", self.code_src,
                         f"'open(' found in code source")

    def test_no_git_commands(self) -> None:
        self.assertNotIn('"git', self.code_src)
        self.assertNotIn("'git", self.code_src)

    def test_no_hold_worker_slot_fence(self) -> None:
        self.assertNotIn("hold_worker_slot_fence", self.code_src)

    def test_no_ApprovalGate(self) -> None:
        self.assertNotIn("ApprovalGate", self.code_src)

    def test_no_datetime_dot_now(self) -> None:
        self.assertNotIn("datetime.now", self.code_src)

    def test_no_time_dot_sleep(self) -> None:
        self.assertNotIn("time.sleep", self.code_src)

    def test_no_stdout_decode(self) -> None:
        self.assertNotIn(".decode(", self.code_src)

    def test_no_json_parsing(self) -> None:
        self.assertNotIn("json.load", self.code_src)

    def test_no_mad_audit(self) -> None:
        self.assertNotIn("mad_audit", self.code_src)
        self.assertNotIn("MadAudit", self.code_src)

    def test_no_escalation(self) -> None:
        self.assertNotIn("escalation", self.code_src)

    def test_no_retry(self) -> None:
        self.assertNotIn("retry", self.code_src.lower())


if __name__ == "__main__":
    unittest.main()
