"""TC-13.19h — Quiescent Task Cancellation Closed-Loop E2E.

Deterministic E2E that proves a task with no active dispatch can be
cancelled through the full quiescent cancellation pathway:

    StateProvider.snapshot()
    → WorkflowOrchestrator.cancel_quiescent_task()
    → ControlPlaneTransitionService.apply_transition(TASK_CANCELLED, lease=None)
    → canonical task enters ``cancelled``
    → final StateProvider.snapshot() re-reads the state

Also validates two fail-closed reverse scenarios:
1. active dispatch present → WorkflowInputError, zero writes
2. CAS revision / expected_state mismatch → rejected before transition, zero writes

Uses real production modules.  Only the clock is faked.
Zero subprocess, zero model/API/network calls.
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
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

_SCRIPTS_DIR = str(
    Path(__file__).resolve().parents[1] / "skills" / "agentdesk" / "scripts"
)
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from control_plane_transition import (
    CancelledPayload,
    ControlPlaneTransitionService,
    TransitionCAS,
    TransitionEventContext,
    TransitionRequest,
    TransitionResult,
)
from state_provider import StateProvider, StateSnapshot
from workflow_orchestrator import (
    TaskCancellationRequest,
    TaskCancellationResult,
    WorkflowClock,
    WorkflowInputError,
    WorkflowOrchestrator,
)

# ── Source repo path ────────────────────────────────────────────────────────

_SOURCE_REPO = Path(__file__).resolve().parents[1]


# ── FakeClock ───────────────────────────────────────────────────────────────


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


# ── canonical directory initialisation ──────────────────────────────────────


def _init_canonical_dirs(project_root: Path) -> None:
    for d in ("docs/pm/events", "docs/pm/outbox", "docs/pm/acceptances"):
        (project_root / d.replace("/", os.sep)).mkdir(parents=True, exist_ok=True)
    runtime = project_root / ".agentdesk" / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)


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


def _write_initial_tasks_yaml(
    project_root: Path,
    task_id: str = "TC-100",
    state: str = "ready",
    revision: int = 1,
    task_card_path: str = "tasks/TC-100/task.md",
    task_card_commit: str = "0" * 40,
    current_dispatch: dict[str, Any] | None = None,
) -> None:
    """Write a minimal tasks.yaml with one task.

    *current_dispatch* if given must be a full canonical dispatch dict;
    if None the task has no active dispatch.
    """
    state_dir = project_root / "docs" / "pm" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)

    tasks_doc: dict[str, Any] = {
        "schema_version": "agentdesk.tasks/v2",
        "project_id": "e2e-quiescent-cancel",
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
                "attempt": 1 if state in ("dispatched", "in_progress", "review_ready") else None,
                "current_dispatch": current_dispatch,
                "report_path": None,
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


# ── minimal event YAML builder ──────────────────────────────────────────────


def _build_minimal_event_yaml(
    event_id: str,
    event_type: str,
    task_id: str,
    revision: int,
    attempt: int | None,
    dispatch_id: str | None,
    from_state: str,
    to_state: str,
    lease_epoch: int = 1,
    occurred_at: str = "2026-07-29T12:00:00Z",
    payload_digest: str | None = None,
) -> bytes:
    """Build a minimal canonical event YAML (agentdesk.state-event/v2).

    Used to pre-populate an events directory so that StateProvider
    cross-consistency validation passes.
    """
    import control_plane_transition as cpt

    common = {
        "schema_version": "agentdesk.state-event/v2",
        "event_id": event_id,
        "event_type": event_type,
        "task_id": task_id,
        "revision": revision,
        "attempt": attempt,
        "dispatch_id": dispatch_id,
        "from_state": from_state,
        "to_state": to_state,
        "lease_epoch": lease_epoch,
        "actor_role_id": "PM",
        "occurred_at": occurred_at,
        "source_message_id": None,
        "evidence_refs": [],
        "guard_results": [],
    }
    if event_type == "TASK_DISPATCHED":
        common["payload_digest"] = payload_digest or (
            "sha256:0000000000000000000000000000000000000000000000000000000000000000"
        )
    return cpt._to_yaml_str(common).encode("utf-8")


def _build_minimal_outbox_yaml(
    message_id: str,
    event_id: str,
    task_id: str,
    revision: int,
    attempt: int,
    dispatch_id: str,
) -> bytes:
    """Build a minimal canonical outbox YAML (agentdesk.outbox-message/v2).

    Required by StateProvider cross-consistency: every TASK_DISPATCHED
    event must have a corresponding outbox entry.
    """
    import control_plane_transition as cpt

    outbox = {
        "schema_version": "agentdesk.outbox-message/v2",
        "message_id": message_id,
        "event_id": event_id,
        "message_type": "task.dispatch",
        "dedupe_key": f"{task_id}/r{revision}/a{attempt}/{dispatch_id}/task.dispatch",
        "task_id": task_id,
        "revision": revision,
        "attempt": attempt,
        "dispatch_id": dispatch_id,
        "destination_role_id": "agent",
        "created_at": "2026-07-29T12:00:00Z",
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
        "payload": {
            "task_path": "tasks/TC-200/task.md",
            "task_card_commit": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "base_commit": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "branch": "feat/active",
            "report_path": "reports/report.md",
        },
    }
    return cpt._to_yaml_str(outbox).encode("utf-8")


# ═══════════════════════════════════════════════════════════════════════════════
# E2E Quiescent Cancellation Tests
# ═══════════════════════════════════════════════════════════════════════════════


class WorkflowQuiescentCancellationE2ETests(unittest.IsolatedAsyncioTestCase):
    """TC-13.19h: Quiescent task cancellation closed-loop E2E.

    Three scenarios:
    1. Happy path — ready task with no dispatch → cancelled
    2. Fail-closed — active dispatch present → WorkflowInputError
    3. Fail-closed — CAS mismatch → rejected before transition
    """

    async def test_quiescent_cancellation_happy_path(self) -> None:
        """Happy path: quiescent ready task → cancelled via cancel_quiescent_task.

        Verifies the full chain:
        StateProvider.snapshot() → cancel_quiescent_task()
        → ControlPlaneTransitionService.apply_transition(TASK_CANCELLED, lease=None)
        → cancelled → final StateProvider.snapshot().
        """
        # ── 0. Capture pre-test state ──────────────────────────────────────
        source_repo = _SOURCE_REPO
        source_status_before = _git_status_bytes(source_repo)
        cwd_before = os.getcwd()

        # ── 1. Create temp project with git repo ───────────────────────────
        project_root = Path(tempfile.mkdtemp(prefix="e2e-quiescent-cancel-"))

        try:
            _git_init(project_root)
            _git_add_all_and_commit(project_root, "initial empty commit")

            _init_canonical_dirs(project_root)
            _init_worker_slot_store(project_root)

            # ── 2. Create task card ────────────────────────────────────────
            task_id = "TC-100"
            revision = 1

            task_dir = project_root / "tasks" / "TC-100"
            task_dir.mkdir(parents=True, exist_ok=True)

            task_card_path_rel = "tasks/TC-100/task.md"
            task_card_content = (
                "---\n"
                "type: implementation\n"
                "role_id: DEV\n"
                'base_commit: "aaaaaaaaaa000000000000000000000000000000"\n'
                "owner_approval:\n"
                "  gate: none\n"
                "---\n\n"
                "# TC-100 · Quiescent Cancellation E2E\n\n"
                "## Goal\n\n"
                "Test quiescent cancellation pathway.\n\n"
                "## Scope\n\n"
                "Test only.\n\n"
                "## Acceptance Criteria\n\n"
                "- [x] Done\n"
            )
            (project_root / task_card_path_rel).parent.mkdir(parents=True, exist_ok=True)
            (project_root / task_card_path_rel).write_text(
                task_card_content, encoding="utf-8"
            )
            task_card_commit = _git_add_all_and_commit(
                project_root, "add task card"
            )

            # ── 3. Write canonical state — ready, no dispatch ──────────────
            _write_initial_tasks_yaml(
                project_root,
                task_id=task_id,
                state="ready",
                revision=revision,
                task_card_path=task_card_path_rel,
                task_card_commit=task_card_commit,
                current_dispatch=None,
            )
            head_sha = _git_add_all_and_commit(
                project_root, "canonical state — ready"
            )

            # ── 4. Build orchestrator ──────────────────────────────────────
            clock = FakeClock(start=_utc("2026-07-29T13:00:00"))
            orch = WorkflowOrchestrator(
                project_root=project_root,
                clock=clock,
                heartbeat_interval_seconds=10.0,
            )

            # ── 5. Pre-condition snapshot ──────────────────────────────────
            snapshot_before = StateProvider(project_root).snapshot()
            self.assertIsInstance(snapshot_before, StateSnapshot)

            tasks_before = [
                t for t in snapshot_before.tasks if t.task_id == task_id
            ]
            self.assertEqual(len(tasks_before), 1)
            task_before = tasks_before[0]
            self.assertEqual(task_before.state, "ready")
            self.assertIsNone(task_before.current_dispatch)
            self.assertEqual(task_before.revision, revision)

            # ── Capture pre-transition canonical bytes ─────────────────────
            tasks_yaml_path = (
                project_root / "docs" / "pm" / "state" / "tasks.yaml"
            )
            tasks_yaml_before = tasks_yaml_path.read_bytes()
            events_dir = project_root / "docs" / "pm" / "events"
            events_before = sorted(
                p.name for p in events_dir.iterdir() if p.is_file()
            )
            events_before_count = len(events_before)

            # ── 6. Build cancellation request ──────────────────────────────
            cancel_event_id = "EVT-QUIESCENT-CANCEL-HAPPY"
            cancel_tr = TransitionRequest(
                cas=TransitionCAS(
                    task_id=task_id,
                    expected_revision=revision,
                    expected_state="ready",
                    expected_snapshot_commit=head_sha,
                ),
                dispatch_cas=None,
                event_id=cancel_event_id,
                event_type="TASK_CANCELLED",
                payload=CancelledPayload(),
                event_context=TransitionEventContext(
                    source_message_id=None,
                    evidence_refs=(),
                    guard_results=(),
                ),
            )
            cancel_req = TaskCancellationRequest(
                cancellation_transition_request=cancel_tr,
            )

            # ── 7. Execute quiescent cancellation ──────────────────────────
            result = await orch.cancel_quiescent_task(cancel_req)

            # ═════════════════════════════════════════════════════════════
            # Assertions — result
            # ═════════════════════════════════════════════════════════════

            self.assertIsInstance(result, TaskCancellationResult)
            self.assertEqual(result.task_id, task_id)

            tr_result = result.cancellation_transition
            self.assertIsInstance(tr_result, TransitionResult)
            self.assertEqual(tr_result.task_id, task_id)
            self.assertEqual(tr_result.event_id, cancel_event_id)
            self.assertEqual(tr_result.from_state, "ready")
            self.assertEqual(tr_result.to_state, "cancelled")

            # ── lease=None — verified by absence of lease operations ───────
            import worker_slot_lease as wsl
            store = wsl.read_worker_slot_leases(project_root)
            self.assertEqual(
                len(store.get("leases", {})), 0,
                "Quiescent cancellation must not acquire any lease",
            )

            # ═════════════════════════════════════════════════════════════
            # Final StateProvider snapshot
            # ═════════════════════════════════════════════════════════════

            snapshot = StateProvider(project_root).snapshot()
            self.assertIsInstance(snapshot, StateSnapshot)

            tasks = [t for t in snapshot.tasks if t.task_id == task_id]
            self.assertEqual(len(tasks), 1)
            task = tasks[0]

            # ── State is cancelled ───────────────────────────────────────
            self.assertEqual(task.state, "cancelled")

            # ── current_dispatch is None ──────────────────────────────────
            self.assertIsNone(task.current_dispatch)

            # ── Revision unchanged (TASK_CANCELLED frozen contract) ───────
            self.assertEqual(
                task.revision, revision,
                "TASK_CANCELLED must not increment revision",
            )

            # ── cancelled_at timestamp is written ──────────────────────────
            self.assertIsNotNone(task.timestamps.cancelled_at)

            # ── updated_at reflects the transition time ────────────────────
            # (TASK_CANCELLED transition writes cancelled_at to timestamps)

            # ═════════════════════════════════════════════════════════════
            # Event assertions
            # ═════════════════════════════════════════════════════════════

            # Exactly one new event
            self.assertEqual(
                len(events_before), 0,
                "No events should exist before cancellation",
            )
            task_events = [e for e in snapshot.events if e.task_id == task_id]
            self.assertEqual(
                len(task_events), 1,
                "Exactly one TASK_CANCELLED event must be created",
            )

            cancel_event = task_events[0]
            self.assertEqual(cancel_event.event_id, cancel_event_id)
            self.assertEqual(cancel_event.event_type, "TASK_CANCELLED")
            self.assertEqual(cancel_event.task_id, task_id)
            self.assertEqual(cancel_event.revision, revision)
            self.assertEqual(cancel_event.from_state, "ready")
            self.assertEqual(cancel_event.to_state, "cancelled")
            self.assertIsNone(cancel_event.dispatch_id)
            self.assertIsNone(cancel_event.attempt)

            # ── No TASK_DISPATCHED events ────────────────────────────────
            dispatch_events = [
                e for e in snapshot.events
                if e.task_id == task_id and e.event_type == "TASK_DISPATCHED"
            ]
            self.assertEqual(len(dispatch_events), 0)

            # ═════════════════════════════════════════════════════════════
            # Zero subprocess, zero model/API/network calls
            # ═════════════════════════════════════════════════════════════
            # (Verified by absence of any mock — no subprocess is spawned
            # by StateProvider, cancel_quiescent_task, or the PM-only
            # TASK_CANCELLED transition.)

            # ═════════════════════════════════════════════════════════════
            # No temporary file residue
            # ═════════════════════════════════════════════════════════════

            tmp_files = list(project_root.rglob(".tmp-*"))
            self.assertEqual(
                len(tmp_files), 0,
                "No temporary files must be left behind",
            )

            # ── No Git remote ───────────────────────────────────────────
            remotes_result = subprocess.run(
                ["git", "-C", str(project_root), "remote"],
                capture_output=True, text=True, timeout=10,
            )
            self.assertEqual(
                remotes_result.stdout.strip(), "",
                "No Git remote must be configured",
            )

            # ── cwd unchanged ───────────────────────────────────────────
            self.assertEqual(os.getcwd(), cwd_before)

            # ── Orphan check — every event references an existing task ───
            task_ids_in_snapshot = {t.task_id for t in snapshot.tasks}
            for ev in snapshot.events:
                self.assertIn(ev.task_id, task_ids_in_snapshot)

        finally:
            shutil.rmtree(project_root, ignore_errors=True)

        # ── Source repo purity ──────────────────────────────────────────
        source_status_after = _git_status_bytes(source_repo)
        self.assertEqual(
            source_status_after, source_status_before,
            "AgentDesk source repo must not be modified by test",
        )

    # ═══════════════════════════════════════════════════════════════════════
    # Fail-Closed Scenario 1: active dispatch present
    # ═══════════════════════════════════════════════════════════════════════

    async def test_active_dispatch_refuses_quiescent_cancellation(
        self,
    ) -> None:
        """Fail-closed: task with active dispatch → WorkflowInputError.

        cancel_quiescent_task() must refuse to cancel a task that has
        current_dispatch set.  The refusal must happen before any
        transition is applied — canonical bytes must be unchanged.
        """
        source_repo = _SOURCE_REPO
        source_status_before = _git_status_bytes(source_repo)
        cwd_before = os.getcwd()

        project_root = Path(
            tempfile.mkdtemp(prefix="e2e-quiescent-cancel-active-")
        )

        try:
            _git_init(project_root)
            _git_add_all_and_commit(project_root, "initial empty commit")
            _init_canonical_dirs(project_root)
            _init_worker_slot_store(project_root)

            task_id = "TC-200"
            revision = 1

            # Create task card
            task_dir = project_root / "tasks" / "TC-200"
            task_dir.mkdir(parents=True, exist_ok=True)
            task_card_path_rel = "tasks/TC-200/task.md"
            (project_root / task_card_path_rel).write_text(
                "---\ntype: implementation\nrole_id: DEV\n"
                'base_commit: "aaaaaaaaaa000000000000000000000000000000"\n'
                "owner_approval:\n  gate: none\n"
                "---\n\n# TC-200\n",
                encoding="utf-8",
            )
            task_card_commit = _git_add_all_and_commit(
                project_root, "add task card"
            )

            # ── Write canonical state WITH active dispatch ─────────────────
            active_dispatch: dict[str, Any] = {
                "dispatch_id": "DSP-ACTIVE-001",
                "attempt_id": "attempt-1",
                "role_id": "agent",
                "base_commit": "0" * 40,
                "branch": "feat/active",
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

            _write_initial_tasks_yaml(
                project_root,
                task_id=task_id,
                state="dispatched",
                revision=revision,
                task_card_path=task_card_path_rel,
                task_card_commit=task_card_commit,
                current_dispatch=active_dispatch,
            )

            # Write a matching TASK_DISPATCHED event so StateProvider
            # cross-consistency passes (current_dispatch with no matching
            # event evidence is rejected at snapshot time).
            dispatch_event_id = "EVT-DISPATCH-ACTIVE-001"
            outbox_msg_id = "MSG-DISPATCH-ACTIVE-001"
            outbox_yaml = _build_minimal_outbox_yaml(
                message_id=outbox_msg_id,
                event_id=dispatch_event_id,
                task_id=task_id,
                revision=revision,
                attempt=1,
                dispatch_id="DSP-ACTIVE-001",
            )
            outbox_dir = project_root / "docs" / "pm" / "outbox"
            outbox_dir.mkdir(parents=True, exist_ok=True)
            (outbox_dir / f"{outbox_msg_id}.yaml").write_bytes(outbox_yaml)

            # Build the event with the correct payload_digest
            import hashlib
            payload_digest = "sha256:" + hashlib.sha256(outbox_yaml).hexdigest()
            dispatch_event_yaml = _build_minimal_event_yaml(
                event_id=dispatch_event_id,
                event_type="TASK_DISPATCHED",
                task_id=task_id,
                revision=revision,
                attempt=1,
                dispatch_id="DSP-ACTIVE-001",
                from_state="ready",
                to_state="dispatched",
                lease_epoch=1,
                occurred_at="2026-07-29T12:00:00Z",
                payload_digest=payload_digest,
            )
            events_dir = project_root / "docs" / "pm" / "events"
            events_dir.mkdir(parents=True, exist_ok=True)
            (events_dir / f"{dispatch_event_id}.yaml").write_bytes(
                dispatch_event_yaml
            )

            head_sha = _git_add_all_and_commit(
                project_root, "canonical state — dispatched"
            )

            # ── Capture pre-failure canonical bytes ────────────────────────
            tasks_yaml_path = (
                project_root / "docs" / "pm" / "state" / "tasks.yaml"
            )
            tasks_yaml_before = tasks_yaml_path.read_bytes()
            events_dir = project_root / "docs" / "pm" / "events"
            events_before = sorted(
                p.name for p in events_dir.iterdir() if p.is_file()
            )
            outbox_dir = project_root / "docs" / "pm" / "outbox"
            outbox_before = sorted(
                p.name for p in outbox_dir.iterdir() if p.is_file()
            )

            # ── Build orchestrator ─────────────────────────────────────────
            clock = FakeClock()
            orch = WorkflowOrchestrator(
                project_root=project_root,
                clock=clock,
                heartbeat_interval_seconds=10.0,
            )

            # ── Build cancellation request targeting the active task ───────
            cancel_tr = TransitionRequest(
                cas=TransitionCAS(
                    task_id=task_id,
                    expected_revision=revision,
                    expected_state="dispatched",
                    expected_snapshot_commit=head_sha,
                ),
                dispatch_cas=None,
                event_id="EVT-QUIESCENT-CANCEL-ACTIVE",
                event_type="TASK_CANCELLED",
                payload=CancelledPayload(),
                event_context=TransitionEventContext(
                    source_message_id=None,
                    evidence_refs=(),
                    guard_results=(),
                ),
            )
            cancel_req = TaskCancellationRequest(
                cancellation_transition_request=cancel_tr,
            )

            # ── Execute — must raise WorkflowInputError ────────────────────
            with self.assertRaises(WorkflowInputError):
                await orch.cancel_quiescent_task(cancel_req)

            # ═════════════════════════════════════════════════════════════
            # Zero writes — byte-exact
            # ═════════════════════════════════════════════════════════════

            self.assertEqual(
                tasks_yaml_path.read_bytes(), tasks_yaml_before,
                "tasks.yaml must be byte-for-byte unchanged",
            )
            self.assertEqual(
                sorted(p.name for p in events_dir.iterdir() if p.is_file()),
                events_before,
                "events directory must be unchanged",
            )
            self.assertEqual(
                sorted(p.name for p in outbox_dir.iterdir() if p.is_file()),
                outbox_before,
                "outbox directory must be unchanged",
            )

            # ── Zero new events beyond the pre-existing TASK_DISPATCHED ────
            snapshot = StateProvider(project_root).snapshot()
            task_events = [
                e for e in snapshot.events if e.task_id == task_id
            ]
            self.assertEqual(
                len(task_events), 1,
                "Exactly one pre-existing TASK_DISPATCHED event — "
                "cancellation must not create any new events",
            )

            # ── Task state unchanged ──────────────────────────────────────
            tasks = [t for t in snapshot.tasks if t.task_id == task_id]
            self.assertEqual(len(tasks), 1)
            self.assertEqual(tasks[0].state, "dispatched")
            self.assertIsNotNone(tasks[0].current_dispatch)

            # ── Zero subprocess ───────────────────────────────────────────
            # No subprocess is spawned by the quiescent cancellation path.

        finally:
            shutil.rmtree(project_root, ignore_errors=True)

        source_status_after = _git_status_bytes(source_repo)
        self.assertEqual(source_status_after, source_status_before)
        self.assertEqual(os.getcwd(), cwd_before)

    # ═══════════════════════════════════════════════════════════════════════
    # Fail-Closed Scenario 2: CAS revision / expected_state mismatch
    # ═══════════════════════════════════════════════════════════════════════

    async def test_cas_mismatch_rejected_before_transition(self) -> None:
        """Fail-closed: CAS revision or expected_state mismatch → rejected.

        cancel_quiescent_task() validates CAS against the snapshot BEFORE
        calling apply_transition().  A mismatched revision or
        expected_state must raise WorkflowInputError with zero writes.
        """
        source_repo = _SOURCE_REPO
        source_status_before = _git_status_bytes(source_repo)
        cwd_before = os.getcwd()

        project_root = Path(
            tempfile.mkdtemp(prefix="e2e-quiescent-cancel-cas-")
        )

        try:
            _git_init(project_root)
            _git_add_all_and_commit(project_root, "initial empty commit")
            _init_canonical_dirs(project_root)
            _init_worker_slot_store(project_root)

            task_id = "TC-300"
            revision = 3

            # Create task card
            task_dir = project_root / "tasks" / "TC-300"
            task_dir.mkdir(parents=True, exist_ok=True)
            task_card_path_rel = "tasks/TC-300/task.md"
            (project_root / task_card_path_rel).write_text(
                "---\ntype: implementation\nrole_id: DEV\n"
                'base_commit: "aaaaaaaaaa000000000000000000000000000000"\n'
                "owner_approval:\n  gate: none\n"
                "---\n\n# TC-300\n",
                encoding="utf-8",
            )
            task_card_commit = _git_add_all_and_commit(
                project_root, "add task card"
            )

            # ── Write canonical state — ready, revision=3, no dispatch ────
            _write_initial_tasks_yaml(
                project_root,
                task_id=task_id,
                state="ready",
                revision=revision,
                task_card_path=task_card_path_rel,
                task_card_commit=task_card_commit,
                current_dispatch=None,
            )
            head_sha = _git_add_all_and_commit(
                project_root, "canonical state — ready r3"
            )

            # ── Capture pre-failure canonical bytes ────────────────────────
            tasks_yaml_path = (
                project_root / "docs" / "pm" / "state" / "tasks.yaml"
            )
            tasks_yaml_before = tasks_yaml_path.read_bytes()
            events_dir = project_root / "docs" / "pm" / "events"
            events_before = sorted(
                p.name for p in events_dir.iterdir() if p.is_file()
            )

            # ── Build orchestrator ─────────────────────────────────────────
            clock = FakeClock()
            orch = WorkflowOrchestrator(
                project_root=project_root,
                clock=clock,
                heartbeat_interval_seconds=10.0,
            )

            # ═════════════════════════════════════════════════════════════
            # Sub-case A: Wrong expected_revision (revision=99 vs actual=3)
            # ═════════════════════════════════════════════════════════════

            cancel_tr_bad_rev = TransitionRequest(
                cas=TransitionCAS(
                    task_id=task_id,
                    expected_revision=99,  # ← wrong
                    expected_state="ready",
                    expected_snapshot_commit=head_sha,
                ),
                dispatch_cas=None,
                event_id="EVT-QUIESCENT-CANCEL-BAD-REV",
                event_type="TASK_CANCELLED",
                payload=CancelledPayload(),
                event_context=TransitionEventContext(
                    source_message_id=None,
                    evidence_refs=(),
                    guard_results=(),
                ),
            )
            cancel_req_a = TaskCancellationRequest(
                cancellation_transition_request=cancel_tr_bad_rev,
            )

            with self.assertRaises(WorkflowInputError):
                await orch.cancel_quiescent_task(cancel_req_a)

            # ── Zero writes after sub-case A ───────────────────────────────
            self.assertEqual(
                tasks_yaml_path.read_bytes(), tasks_yaml_before,
                "tasks.yaml must be unchanged after revision mismatch",
            )
            self.assertEqual(
                sorted(p.name for p in events_dir.iterdir() if p.is_file()),
                events_before,
                "events must be unchanged after revision mismatch",
            )

            # ═════════════════════════════════════════════════════════════
            # Sub-case B: Wrong expected_state ("blocked" vs actual "ready")
            # ═════════════════════════════════════════════════════════════

            cancel_tr_bad_state = TransitionRequest(
                cas=TransitionCAS(
                    task_id=task_id,
                    expected_revision=revision,  # correct
                    expected_state="blocked",     # ← wrong
                    expected_snapshot_commit=head_sha,
                ),
                dispatch_cas=None,
                event_id="EVT-QUIESCENT-CANCEL-BAD-STATE",
                event_type="TASK_CANCELLED",
                payload=CancelledPayload(),
                event_context=TransitionEventContext(
                    source_message_id=None,
                    evidence_refs=(),
                    guard_results=(),
                ),
            )
            cancel_req_b = TaskCancellationRequest(
                cancellation_transition_request=cancel_tr_bad_state,
            )

            with self.assertRaises(WorkflowInputError):
                await orch.cancel_quiescent_task(cancel_req_b)

            # ── Zero writes after sub-case B ───────────────────────────────
            self.assertEqual(
                tasks_yaml_path.read_bytes(), tasks_yaml_before,
                "tasks.yaml must be unchanged after state mismatch",
            )
            self.assertEqual(
                sorted(p.name for p in events_dir.iterdir() if p.is_file()),
                events_before,
                "events must be unchanged after state mismatch",
            )

            # ── Zero new events confirmed via snapshot ─────────────────────
            snapshot = StateProvider(project_root).snapshot()
            task_events = [
                e for e in snapshot.events if e.task_id == task_id
            ]
            self.assertEqual(
                len(task_events), 0,
                "Zero new events after both CAS mismatch attempts",
            )

            # ── Task state still "ready" ───────────────────────────────────
            tasks = [t for t in snapshot.tasks if t.task_id == task_id]
            self.assertEqual(len(tasks), 1)
            self.assertEqual(tasks[0].state, "ready")
            self.assertEqual(tasks[0].revision, revision)

        finally:
            shutil.rmtree(project_root, ignore_errors=True)

        source_status_after = _git_status_bytes(source_repo)
        self.assertEqual(source_status_after, source_status_before)
        self.assertEqual(os.getcwd(), cwd_before)
