"""TC-13.19i — Quiescent Task Supersession Closed-Loop E2E.

Deterministic E2E that proves a task with no active dispatch can be
superseded through the full quiescent supersession pathway:

    StateProvider.snapshot()
    → WorkflowOrchestrator.supersede_quiescent_task()
    → ControlPlaneTransitionService.apply_transition(TASK_SUPERSEDED, lease=None)
    → canonical task enters ``superseded``
    → final StateProvider.snapshot() re-reads the state

Also validates three fail-closed reverse scenarios:
1. active dispatch present → WorkflowInputError, zero writes
2. CAS revision / expected_state mismatch → rejected before transition, zero writes
3. self-supersession (task_id == superseded_by) → WorkflowInputError, zero writes

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
    SupersededPayload,
    TransitionCAS,
    TransitionEventContext,
    TransitionRequest,
    TransitionResult,
)
from state_provider import StateProvider, StateSnapshot
from workflow_orchestrator import (
    TaskSupersessionRequest,
    TaskSupersessionResult,
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
    tasks: list[dict[str, Any]] | None = None,
) -> None:
    """Write a minimal tasks.yaml with the given task list.

    If *tasks* is None, writes a single ready TC-400 and TC-401 tasks.
    """
    state_dir = project_root / "docs" / "pm" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)

    if tasks is None:
        tasks = []

    tasks_doc: dict[str, Any] = {
        "schema_version": "agentdesk.tasks/v2",
        "project_id": "e2e-quiescent-supersede",
        "adoption_level": "standard",
        "updated_at": "2026-07-29T12:00:00Z",
        "pm_control": {
            "holder_id": "pm-e2e",
            "lease_epoch": 1,
            "mode": "manual",
        },
        "tasks": tasks,
    }
    path = state_dir / "tasks.yaml"
    path.write_text(json.dumps(tasks_doc, indent=2) + "\n", encoding="utf-8")


def _make_task_spec(
    task_id: str,
    state: str = "ready",
    revision: int = 1,
    task_card_path: str | None = None,
    task_card_commit: str = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    current_dispatch: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a single canonical task dict for tasks.yaml."""
    if task_card_path is None:
        task_card_path = f"tasks/{task_id}/task.md"
    return {
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
    }


# ═══════════════════════════════════════════════════════════════════════════════
# E2E Quiescent Supersession Tests
# ═══════════════════════════════════════════════════════════════════════════════


class WorkflowQuiescentSupersessionE2ETests(unittest.IsolatedAsyncioTestCase):
    """TC-13.19i: Quiescent task supersession closed-loop E2E.

    Four scenarios:
    1. Happy path — ready task superseded by another task → superseded
    2. Fail-closed — active dispatch present → WorkflowInputError
    3. Fail-closed — CAS mismatch → rejected before transition
    4. Fail-closed — self-supersession → WorkflowInputError
    """

    async def test_quiescent_supersession_happy_path(self) -> None:
        """Happy path: quiescent ready task superseded via supersede_quiescent_task.

        Verifies the full chain:
        StateProvider.snapshot() → supersede_quiescent_task()
        → ControlPlaneTransitionService.apply_transition(TASK_SUPERSEDED, lease=None)
        → superseded → final StateProvider.snapshot().
        """
        # ── 0. Capture pre-test state ──────────────────────────────────────
        source_repo = _SOURCE_REPO
        source_status_before = _git_status_bytes(source_repo)
        cwd_before = os.getcwd()

        # ── 1. Create temp project with git repo ───────────────────────────
        project_root = Path(tempfile.mkdtemp(prefix="e2e-quiescent-supersede-"))

        try:
            _git_init(project_root)
            _git_add_all_and_commit(project_root, "initial empty commit")

            _init_canonical_dirs(project_root)
            _init_worker_slot_store(project_root)

            # ── 2. Create task cards for both tasks ────────────────────────
            source_task_id = "TC-400"
            replacement_task_id = "TC-401"
            source_revision = 2

            for tid in (source_task_id, replacement_task_id):
                task_dir = project_root / "tasks" / tid
                task_dir.mkdir(parents=True, exist_ok=True)
                task_card_path = task_dir / "task.md"
                task_card_path.write_text(
                    "---\ntype: implementation\nrole_id: DEV\n"
                    'base_commit: "aaaaaaaaaa000000000000000000000000000000"\n'
                    "owner_approval:\n  gate: none\n"
                    f"---\n\n# {tid} · Supersession E2E\n\n"
                    "## Goal\nTest supersession pathway.\n\n"
                    "## Scope\nTest only.\n\n"
                    "## Acceptance Criteria\n- [x] Done\n",
                    encoding="utf-8",
                )
            task_card_commit = _git_add_all_and_commit(
                project_root, "add task cards"
            )

            # ── 3. Write canonical state — both tasks ready, no dispatch ───
            source_task_spec = _make_task_spec(
                task_id=source_task_id,
                state="ready",
                revision=source_revision,
                task_card_path=f"tasks/{source_task_id}/task.md",
                task_card_commit=task_card_commit,
                current_dispatch=None,
            )
            replacement_task_spec = _make_task_spec(
                task_id=replacement_task_id,
                state="ready",
                revision=1,
                task_card_path=f"tasks/{replacement_task_id}/task.md",
                task_card_commit=task_card_commit,
                current_dispatch=None,
            )
            _write_initial_tasks_yaml(
                project_root,
                tasks=[source_task_spec, replacement_task_spec],
            )
            head_sha = _git_add_all_and_commit(
                project_root, "canonical state — two ready tasks"
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
                t for t in snapshot_before.tasks
                if t.task_id in (source_task_id, replacement_task_id)
            ]
            self.assertEqual(len(tasks_before), 2)

            source_before = [
                t for t in tasks_before if t.task_id == source_task_id
            ][0]
            self.assertEqual(source_before.state, "ready")
            self.assertIsNone(source_before.current_dispatch)
            self.assertEqual(source_before.revision, source_revision)

            replacement_before = [
                t for t in tasks_before if t.task_id == replacement_task_id
            ][0]
            self.assertEqual(replacement_before.state, "ready")

            # ── Capture pre-transition canonical bytes ─────────────────────
            tasks_yaml_path = (
                project_root / "docs" / "pm" / "state" / "tasks.yaml"
            )
            tasks_yaml_before = tasks_yaml_path.read_bytes()
            events_dir = project_root / "docs" / "pm" / "events"
            events_before = sorted(
                p.name for p in events_dir.iterdir() if p.is_file()
            )

            # ── 6. Build supersession request ──────────────────────────────
            supersede_event_id = "EVT-QUIESCENT-SUPERSEDE-HAPPY"
            supersede_tr = TransitionRequest(
                cas=TransitionCAS(
                    task_id=source_task_id,
                    expected_revision=source_revision,
                    expected_state="ready",
                    expected_snapshot_commit=head_sha,
                ),
                dispatch_cas=None,
                event_id=supersede_event_id,
                event_type="TASK_SUPERSEDED",
                payload=SupersededPayload(superseded_by=replacement_task_id),
                event_context=TransitionEventContext(
                    source_message_id=None,
                    evidence_refs=(),
                    guard_results=(),
                ),
            )
            supersede_req = TaskSupersessionRequest(
                supersession_transition_request=supersede_tr,
            )

            # ── 7. Execute quiescent supersession ──────────────────────────
            result = await orch.supersede_quiescent_task(supersede_req)

            # ═════════════════════════════════════════════════════════════
            # Assertions — result
            # ═════════════════════════════════════════════════════════════

            self.assertIsInstance(result, TaskSupersessionResult)
            self.assertEqual(result.task_id, source_task_id)
            self.assertEqual(result.superseded_by, replacement_task_id)

            tr_result = result.supersession_transition
            self.assertIsInstance(tr_result, TransitionResult)
            self.assertEqual(tr_result.task_id, source_task_id)
            self.assertEqual(tr_result.event_id, supersede_event_id)
            self.assertEqual(tr_result.from_state, "ready")
            self.assertEqual(tr_result.to_state, "superseded")

            # ── lease=None — verified by absence of lease operations ───────
            import worker_slot_lease as wsl
            store = wsl.read_worker_slot_leases(project_root)
            self.assertEqual(
                len(store.get("leases", {})), 0,
                "Quiescent supersession must not acquire any lease",
            )

            # ═════════════════════════════════════════════════════════════
            # Final StateProvider snapshot
            # ═════════════════════════════════════════════════════════════

            snapshot = StateProvider(project_root).snapshot()
            self.assertIsInstance(snapshot, StateSnapshot)

            # ── Source task is superseded ─────────────────────────────────
            source_tasks = [
                t for t in snapshot.tasks if t.task_id == source_task_id
            ]
            self.assertEqual(len(source_tasks), 1)
            source_task = source_tasks[0]

            self.assertEqual(source_task.state, "superseded")
            self.assertIsNone(source_task.current_dispatch)

            # ── Revision unchanged (TASK_SUPERSEDED frozen contract) ──────
            self.assertEqual(
                source_task.revision, source_revision,
                "TASK_SUPERSEDED must not increment revision",
            )

            # ── superseded_at timestamp is written ────────────────────────
            self.assertIsNotNone(source_task.timestamps.superseded_at)

            # ── Replacement task is untouched ─────────────────────────────
            replacement_tasks = [
                t for t in snapshot.tasks if t.task_id == replacement_task_id
            ]
            self.assertEqual(len(replacement_tasks), 1)
            replacement_task = replacement_tasks[0]
            self.assertEqual(replacement_task.state, "ready")
            self.assertIsNone(replacement_task.current_dispatch)
            self.assertEqual(replacement_task.revision, 1)

            # ═════════════════════════════════════════════════════════════
            # Event assertions
            # ═════════════════════════════════════════════════════════════

            self.assertEqual(
                len(events_before), 0,
                "No events should exist before supersession",
            )
            task_events = [
                e for e in snapshot.events if e.task_id == source_task_id
            ]
            self.assertEqual(
                len(task_events), 1,
                "Exactly one TASK_SUPERSEDED event must be created",
            )

            supersede_event = task_events[0]
            self.assertEqual(supersede_event.event_id, supersede_event_id)
            self.assertEqual(supersede_event.event_type, "TASK_SUPERSEDED")
            self.assertEqual(supersede_event.task_id, source_task_id)
            self.assertEqual(supersede_event.revision, source_revision)
            self.assertEqual(supersede_event.from_state, "ready")
            self.assertEqual(supersede_event.to_state, "superseded")
            self.assertIsNone(supersede_event.dispatch_id)
            self.assertIsNone(supersede_event.attempt)

            # ── Replacement task has zero new events ─────────────────────
            repl_events = [
                e for e in snapshot.events if e.task_id == replacement_task_id
            ]
            self.assertEqual(len(repl_events), 0)

            # ═════════════════════════════════════════════════════════════
            # No temporary file residue, no Git remote, cwd unchanged
            # ═════════════════════════════════════════════════════════════

            tmp_files = list(project_root.rglob(".tmp-*"))
            self.assertEqual(len(tmp_files), 0)

            remotes_result = subprocess.run(
                ["git", "-C", str(project_root), "remote"],
                capture_output=True, text=True, timeout=10,
            )
            self.assertEqual(remotes_result.stdout.strip(), "")

            self.assertEqual(os.getcwd(), cwd_before)

            # ── Orphan check ────────────────────────────────────────────
            task_ids_in_snapshot = {t.task_id for t in snapshot.tasks}
            for ev in snapshot.events:
                self.assertIn(ev.task_id, task_ids_in_snapshot)

        finally:
            shutil.rmtree(project_root, ignore_errors=True)

        source_status_after = _git_status_bytes(source_repo)
        self.assertEqual(source_status_after, source_status_before)
        self.assertEqual(os.getcwd(), cwd_before)

    # ═══════════════════════════════════════════════════════════════════════
    # Fail-Closed Scenario 1: active dispatch present
    # ═══════════════════════════════════════════════════════════════════════

    async def test_active_dispatch_refuses_quiescent_supersession(
        self,
    ) -> None:
        """Fail-closed: task with active dispatch → WorkflowInputError.

        supersede_quiescent_task() must refuse to supersede a task that has
        current_dispatch set.  The refusal must happen before any
        transition is applied — canonical bytes must be unchanged.
        """
        source_repo = _SOURCE_REPO
        source_status_before = _git_status_bytes(source_repo)
        cwd_before = os.getcwd()

        project_root = Path(
            tempfile.mkdtemp(prefix="e2e-quiescent-supersede-active-")
        )

        try:
            _git_init(project_root)
            _git_add_all_and_commit(project_root, "initial empty commit")
            _init_canonical_dirs(project_root)
            _init_worker_slot_store(project_root)

            source_task_id = "TC-410"
            replacement_task_id = "TC-411"
            source_revision = 1

            for tid in (source_task_id, replacement_task_id):
                task_dir = project_root / "tasks" / tid
                task_dir.mkdir(parents=True, exist_ok=True)
                (task_dir / "task.md").write_text(
                    f"---\ntype: implementation\nrole_id: DEV\n"
                    f'base_commit: "aaaaaaaaaa000000000000000000000000000000"\n'
                    f"owner_approval:\n  gate: none\n"
                    f"---\n\n# {tid}\n",
                    encoding="utf-8",
                )
            task_card_commit = _git_add_all_and_commit(
                project_root, "add task cards"
            )

            # ── Source task has active dispatch ───────────────────────────
            active_dispatch: dict[str, Any] = {
                "dispatch_id": "DSP-SUPERSEDE-ACTIVE-001",
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

            source_task_spec = _make_task_spec(
                task_id=source_task_id,
                state="dispatched",
                revision=source_revision,
                current_dispatch=active_dispatch,
            )
            replacement_task_spec = _make_task_spec(
                task_id=replacement_task_id,
                state="ready",
                revision=1,
            )
            _write_initial_tasks_yaml(
                project_root,
                tasks=[source_task_spec, replacement_task_spec],
            )

            # Write TASK_DISPATCHED event + outbox for cross-consistency
            _write_dispatch_event_and_outbox(
                project_root,
                event_id="EVT-DISPATCH-SUPERSEDE-ACTIVE-001",
                outbox_msg_id="MSG-DISPATCH-SUPERSEDE-ACTIVE-001",
                task_id=source_task_id,
                revision=source_revision,
                attempt=1,
                dispatch_id="DSP-SUPERSEDE-ACTIVE-001",
                task_card_commit=task_card_commit,
            )

            head_sha = _git_add_all_and_commit(
                project_root, "canonical state — source dispatched"
            )

            tasks_yaml_path = (
                project_root / "docs" / "pm" / "state" / "tasks.yaml"
            )
            tasks_yaml_before = tasks_yaml_path.read_bytes()
            events_dir = project_root / "docs" / "pm" / "events"
            events_before = sorted(
                p.name for p in events_dir.iterdir() if p.is_file()
            )

            # ── Build orchestrator and supersession request ───────────────
            clock = FakeClock()
            orch = WorkflowOrchestrator(
                project_root=project_root,
                clock=clock,
                heartbeat_interval_seconds=10.0,
            )

            supersede_tr = TransitionRequest(
                cas=TransitionCAS(
                    task_id=source_task_id,
                    expected_revision=source_revision,
                    expected_state="dispatched",
                    expected_snapshot_commit=head_sha,
                ),
                dispatch_cas=None,
                event_id="EVT-QUIESCENT-SUPERSEDE-ACTIVE",
                event_type="TASK_SUPERSEDED",
                payload=SupersededPayload(superseded_by=replacement_task_id),
                event_context=TransitionEventContext(
                    source_message_id=None,
                    evidence_refs=(),
                    guard_results=(),
                ),
            )
            supersede_req = TaskSupersessionRequest(
                supersession_transition_request=supersede_tr,
            )

            with self.assertRaises(WorkflowInputError):
                await orch.supersede_quiescent_task(supersede_req)

            # ── Zero writes — byte-exact ──────────────────────────────────
            self.assertEqual(
                tasks_yaml_path.read_bytes(), tasks_yaml_before,
                "tasks.yaml must be byte-for-byte unchanged",
            )
            self.assertEqual(
                sorted(p.name for p in events_dir.iterdir() if p.is_file()),
                events_before,
                "events directory must be unchanged",
            )

            # ── Source task still dispatched ──────────────────────────────
            snapshot = StateProvider(project_root).snapshot()
            source_tasks = [
                t for t in snapshot.tasks if t.task_id == source_task_id
            ]
            self.assertEqual(len(source_tasks), 1)
            self.assertEqual(source_tasks[0].state, "dispatched")
            self.assertIsNotNone(source_tasks[0].current_dispatch)

            # ── Only the pre-existing TASK_DISPATCHED event ───────────────
            task_events = [
                e for e in snapshot.events if e.task_id == source_task_id
            ]
            dispatch_events = [
                e for e in task_events if e.event_type == "TASK_DISPATCHED"
            ]
            self.assertEqual(len(dispatch_events), 1)
            self.assertEqual(
                len([e for e in task_events if e.event_type == "TASK_SUPERSEDED"]),
                0,
            )

        finally:
            shutil.rmtree(project_root, ignore_errors=True)

        source_status_after = _git_status_bytes(source_repo)
        self.assertEqual(source_status_after, source_status_before)
        self.assertEqual(os.getcwd(), cwd_before)

    # ═══════════════════════════════════════════════════════════════════════
    # Fail-Closed Scenario 2: CAS mismatch
    # ═══════════════════════════════════════════════════════════════════════

    async def test_cas_mismatch_rejected_before_transition(self) -> None:
        """Fail-closed: CAS revision or expected_state mismatch → rejected.

        supersede_quiescent_task() validates CAS against the snapshot BEFORE
        calling apply_transition().  A mismatched revision or expected_state
        must raise WorkflowInputError with zero writes.
        """
        source_repo = _SOURCE_REPO
        source_status_before = _git_status_bytes(source_repo)
        cwd_before = os.getcwd()

        project_root = Path(
            tempfile.mkdtemp(prefix="e2e-quiescent-supersede-cas-")
        )

        try:
            _git_init(project_root)
            _git_add_all_and_commit(project_root, "initial empty commit")
            _init_canonical_dirs(project_root)
            _init_worker_slot_store(project_root)

            source_task_id = "TC-420"
            replacement_task_id = "TC-421"
            source_revision = 5

            for tid in (source_task_id, replacement_task_id):
                task_dir = project_root / "tasks" / tid
                task_dir.mkdir(parents=True, exist_ok=True)
                (task_dir / "task.md").write_text(
                    f"---\ntype: implementation\nrole_id: DEV\n"
                    f'base_commit: "aaaaaaaaaa000000000000000000000000000000"\n'
                    f"owner_approval:\n  gate: none\n"
                    f"---\n\n# {tid}\n",
                    encoding="utf-8",
                )
            task_card_commit = _git_add_all_and_commit(
                project_root, "add task cards"
            )

            _write_initial_tasks_yaml(
                project_root,
                tasks=[
                    _make_task_spec(
                        task_id=source_task_id, state="draft",
                        revision=source_revision,
                    ),
                    _make_task_spec(
                        task_id=replacement_task_id, state="ready",
                        revision=1,
                    ),
                ],
            )
            head_sha = _git_add_all_and_commit(
                project_root, "canonical state — source draft r5"
            )

            tasks_yaml_path = (
                project_root / "docs" / "pm" / "state" / "tasks.yaml"
            )
            tasks_yaml_before = tasks_yaml_path.read_bytes()
            events_dir = project_root / "docs" / "pm" / "events"
            events_before = sorted(
                p.name for p in events_dir.iterdir() if p.is_file()
            )

            clock = FakeClock()
            orch = WorkflowOrchestrator(
                project_root=project_root,
                clock=clock,
                heartbeat_interval_seconds=10.0,
            )

            # ═════════════════════════════════════════════════════════════
            # Sub-case A: Wrong expected_revision (99 vs actual 5)
            # ═════════════════════════════════════════════════════════════

            supersede_tr_bad_rev = TransitionRequest(
                cas=TransitionCAS(
                    task_id=source_task_id,
                    expected_revision=99,  # ← wrong
                    expected_state="draft",
                    expected_snapshot_commit=head_sha,
                ),
                dispatch_cas=None,
                event_id="EVT-QUIESCENT-SUPERSEDE-BAD-REV",
                event_type="TASK_SUPERSEDED",
                payload=SupersededPayload(superseded_by=replacement_task_id),
                event_context=TransitionEventContext(
                    source_message_id=None,
                    evidence_refs=(),
                    guard_results=(),
                ),
            )
            supersede_req_a = TaskSupersessionRequest(
                supersession_transition_request=supersede_tr_bad_rev,
            )

            with self.assertRaises(WorkflowInputError):
                await orch.supersede_quiescent_task(supersede_req_a)

            self.assertEqual(
                tasks_yaml_path.read_bytes(), tasks_yaml_before,
                "tasks.yaml unchanged after revision mismatch",
            )
            self.assertEqual(
                sorted(p.name for p in events_dir.iterdir() if p.is_file()),
                events_before,
                "events unchanged after revision mismatch",
            )

            # ═════════════════════════════════════════════════════════════
            # Sub-case B: Wrong expected_state ("blocked" vs actual "draft")
            # ═════════════════════════════════════════════════════════════

            supersede_tr_bad_state = TransitionRequest(
                cas=TransitionCAS(
                    task_id=source_task_id,
                    expected_revision=source_revision,  # correct
                    expected_state="blocked",             # ← wrong
                    expected_snapshot_commit=head_sha,
                ),
                dispatch_cas=None,
                event_id="EVT-QUIESCENT-SUPERSEDE-BAD-STATE",
                event_type="TASK_SUPERSEDED",
                payload=SupersededPayload(superseded_by=replacement_task_id),
                event_context=TransitionEventContext(
                    source_message_id=None,
                    evidence_refs=(),
                    guard_results=(),
                ),
            )
            supersede_req_b = TaskSupersessionRequest(
                supersession_transition_request=supersede_tr_bad_state,
            )

            with self.assertRaises(WorkflowInputError):
                await orch.supersede_quiescent_task(supersede_req_b)

            self.assertEqual(
                tasks_yaml_path.read_bytes(), tasks_yaml_before,
                "tasks.yaml unchanged after state mismatch",
            )
            self.assertEqual(
                sorted(p.name for p in events_dir.iterdir() if p.is_file()),
                events_before,
                "events unchanged after state mismatch",
            )

            # ── Zero new events ───────────────────────────────────────────
            snapshot = StateProvider(project_root).snapshot()
            task_events = [
                e for e in snapshot.events if e.task_id == source_task_id
            ]
            self.assertEqual(len(task_events), 0)

            # ── Source task still draft ───────────────────────────────────
            source_tasks = [
                t for t in snapshot.tasks if t.task_id == source_task_id
            ]
            self.assertEqual(len(source_tasks), 1)
            self.assertEqual(source_tasks[0].state, "draft")
            self.assertEqual(source_tasks[0].revision, source_revision)

        finally:
            shutil.rmtree(project_root, ignore_errors=True)

        source_status_after = _git_status_bytes(source_repo)
        self.assertEqual(source_status_after, source_status_before)
        self.assertEqual(os.getcwd(), cwd_before)

    # ═══════════════════════════════════════════════════════════════════════
    # Fail-Closed Scenario 3: self-supersession
    # ═══════════════════════════════════════════════════════════════════════

    async def test_self_supersession_rejected(self) -> None:
        """Fail-closed: task superseding itself → WorkflowInputError.

        supersede_quiescent_task() must reject a request where
        superseeded_by == task_id (source task == replacement task).
        """
        source_repo = _SOURCE_REPO
        source_status_before = _git_status_bytes(source_repo)
        cwd_before = os.getcwd()

        project_root = Path(
            tempfile.mkdtemp(prefix="e2e-quiescent-supersede-self-")
        )

        try:
            _git_init(project_root)
            _git_add_all_and_commit(project_root, "initial empty commit")
            _init_canonical_dirs(project_root)
            _init_worker_slot_store(project_root)

            task_id = "TC-430"
            revision = 1

            task_dir = project_root / "tasks" / task_id
            task_dir.mkdir(parents=True, exist_ok=True)
            (task_dir / "task.md").write_text(
                f"---\ntype: implementation\nrole_id: DEV\n"
                f'base_commit: "aaaaaaaaaa000000000000000000000000000000"\n'
                f"owner_approval:\n  gate: none\n"
                f"---\n\n# {task_id}\n",
                encoding="utf-8",
            )
            task_card_commit = _git_add_all_and_commit(
                project_root, "add task card"
            )

            _write_initial_tasks_yaml(
                project_root,
                tasks=[
                    _make_task_spec(
                        task_id=task_id, state="ready",
                        revision=revision,
                        task_card_commit=task_card_commit,
                    ),
                ],
            )
            head_sha = _git_add_all_and_commit(
                project_root, "canonical state — single task"
            )

            tasks_yaml_path = (
                project_root / "docs" / "pm" / "state" / "tasks.yaml"
            )
            tasks_yaml_before = tasks_yaml_path.read_bytes()
            events_dir = project_root / "docs" / "pm" / "events"
            events_before = sorted(
                p.name for p in events_dir.iterdir() if p.is_file()
            )

            clock = FakeClock()
            orch = WorkflowOrchestrator(
                project_root=project_root,
                clock=clock,
                heartbeat_interval_seconds=10.0,
            )

            # ── Self-supersession: superseded_by == task_id ────────────────
            supersede_tr = TransitionRequest(
                cas=TransitionCAS(
                    task_id=task_id,
                    expected_revision=revision,
                    expected_state="ready",
                    expected_snapshot_commit=head_sha,
                ),
                dispatch_cas=None,
                event_id="EVT-QUIESCENT-SUPERSEDE-SELF",
                event_type="TASK_SUPERSEDED",
                payload=SupersededPayload(superseded_by=task_id),
                event_context=TransitionEventContext(
                    source_message_id=None,
                    evidence_refs=(),
                    guard_results=(),
                ),
            )
            supersede_req = TaskSupersessionRequest(
                supersession_transition_request=supersede_tr,
            )

            with self.assertRaises(WorkflowInputError):
                await orch.supersede_quiescent_task(supersede_req)

            # ── Zero writes ───────────────────────────────────────────────
            self.assertEqual(
                tasks_yaml_path.read_bytes(), tasks_yaml_before,
                "tasks.yaml must be unchanged after self-supersession attempt",
            )
            self.assertEqual(
                sorted(p.name for p in events_dir.iterdir() if p.is_file()),
                events_before,
                "events must be unchanged",
            )

            # ── Task state unchanged ──────────────────────────────────────
            snapshot = StateProvider(project_root).snapshot()
            tasks = [t for t in snapshot.tasks if t.task_id == task_id]
            self.assertEqual(len(tasks), 1)
            self.assertEqual(tasks[0].state, "ready")
            self.assertEqual(tasks[0].revision, revision)

            task_events = [
                e for e in snapshot.events if e.task_id == task_id
            ]
            self.assertEqual(len(task_events), 0)

        finally:
            shutil.rmtree(project_root, ignore_errors=True)

        source_status_after = _git_status_bytes(source_repo)
        self.assertEqual(source_status_after, source_status_before)
        self.assertEqual(os.getcwd(), cwd_before)


# ── dispatch event + outbox helpers ──────────────────────────────────────────


def _write_dispatch_event_and_outbox(
    project_root: Path,
    event_id: str,
    outbox_msg_id: str,
    task_id: str,
    revision: int,
    attempt: int,
    dispatch_id: str,
    task_card_commit: str,
) -> None:
    """Write a TASK_DISPATCHED event + matching outbox for cross-consistency."""
    import control_plane_transition as cpt
    import hashlib

    outbox = {
        "schema_version": "agentdesk.outbox-message/v2",
        "message_id": outbox_msg_id,
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
            "task_path": f"tasks/{task_id}/task.md",
            "task_card_commit": task_card_commit,
            "base_commit": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "branch": "feat/active",
            "report_path": "reports/report.md",
        },
    }
    outbox_yaml = cpt._to_yaml_str(outbox).encode("utf-8")
    outbox_dir = project_root / "docs" / "pm" / "outbox"
    outbox_dir.mkdir(parents=True, exist_ok=True)
    (outbox_dir / f"{outbox_msg_id}.yaml").write_bytes(outbox_yaml)

    payload_digest = "sha256:" + hashlib.sha256(outbox_yaml).hexdigest()

    event = {
        "schema_version": "agentdesk.state-event/v2",
        "event_id": event_id,
        "event_type": "TASK_DISPATCHED",
        "task_id": task_id,
        "revision": revision,
        "attempt": attempt,
        "dispatch_id": dispatch_id,
        "from_state": "ready",
        "to_state": "dispatched",
        "lease_epoch": 1,
        "actor_role_id": "PM",
        "occurred_at": "2026-07-29T12:00:00Z",
        "source_message_id": None,
        "evidence_refs": [],
        "guard_results": [],
        "payload_digest": payload_digest,
    }
    event_yaml = cpt._to_yaml_str(event).encode("utf-8")
    events_dir = project_root / "docs" / "pm" / "events"
    events_dir.mkdir(parents=True, exist_ok=True)
    (events_dir / f"{event_id}.yaml").write_bytes(event_yaml)
