from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

_SCRIPTS_DIR = str(
    Path(__file__).resolve().parents[1]
    / "skills"
    / "agentdesk"
    / "scripts"
)
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from approval_gate import ApprovalScope, ApprovalSubject, write_grant  # noqa: E402
from control_plane_transition import (  # noqa: E402
    ControlPlaneTransitionService,
    DispatchPayload,
    TransitionCAS,
    TransitionEventContext,
    TransitionRequest,
)
from core_types import WorkerKind  # noqa: E402
from dispatcher_gateway import ModelSelectionSnapshot  # noqa: E402
import portfolio_scheduler_admission as pa  # noqa: E402
import portfolio_scheduler_store as ps  # noqa: E402
from worker_slot_lease import (  # noqa: E402
    WorkerSlotCapacityError,
    WorkerSlotContentionError,
    acquire_worker_slot,
    read_worker_slot_leases,
    release_worker_slot,
)


STAMP = "2026-07-29T12:00:00.000000Z"
NOW = datetime(2026, 7, 29, 12, 0, 1, tzinfo=UTC)
TASK_ID = "TC-2401"


def _run(cmd: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
    )


def _git_head(root: Path) -> str:
    return _run(["git", "rev-parse", "HEAD"], root).stdout.strip()


def _init_git(root: Path) -> None:
    _run(["git", "init", "-b", "main"], root)
    _run(["git", "config", "user.email", "test@test.test"], root)
    _run(["git", "config", "user.name", "Test"], root)
    _run(["git", "commit", "--allow-empty", "-m", "init"], root)


def _make_model_selection() -> ModelSelectionSnapshot:
    return ModelSelectionSnapshot(
        required_model_tier="standard",
        required_model_capabilities=(),
        model_binding_id="binding-001",
        selected_model_provider="claude",
        selected_model_id="test-model",
        selected_model_tier="standard",
        selected_deliberation_tier="balanced",
        selected_context_window_tokens=200000,
        selected_model_capabilities=(),
        model_degradation_approval_id=None,
    )


def _write_tasks_yaml(
    root: Path,
    task_id: str = TASK_ID,
    revision: int = 1,
    state: str = "ready",
    attempt: int | None = None,
    task_card_path: str | None = None,
    task_card_commit: str = "b" * 40,
    base_commit: str = "c" * 40,
) -> None:
    if task_card_path is None:
        task_card_path = f"tasks/{task_id}/task.md"
    state_dir = root / "docs" / "pm" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    for directory in (
        "docs/pm/events",
        "docs/pm/outbox",
        "docs/pm/acceptances",
        "docs/pm/approvals",
    ):
        (root / directory.replace("/", os.sep)).mkdir(parents=True, exist_ok=True)

    current_dispatch = None
    if state in ("dispatched", "in_progress", "review_ready"):
        current_dispatch = {
            "dispatch_id": "DSP-001",
            "attempt_id": f"attempt-{attempt}",
            "role_id": "agent",
            "base_commit": base_commit,
            "branch": "feat/admit",
            "dispatched_at": STAMP,
            "model_selection": dataclasses.asdict(_make_model_selection()),
        }

    tasks_doc = {
        "schema_version": "agentdesk.tasks/v2",
        "project_id": "admission-test",
        "adoption_level": "standard",
        "updated_at": STAMP,
        "pm_control": {
            "holder_id": "pm-1",
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
                "attempt": attempt,
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
                    "created_at": STAMP,
                    "ready_at": STAMP,
                    "dispatched_at": None,
                    "started_at": None,
                    "delivered_at": None,
                    "blocked_at": None,
                    "accepted_at": None,
                    "integrated_at": None,
                    "updated_at": STAMP,
                },
            }
        ],
    }
    (state_dir / "tasks.yaml").write_text(
        json.dumps(tasks_doc, indent=2) + "\n",
        encoding="utf-8",
    )
    task_card = root / task_card_path
    task_card.parent.mkdir(parents=True, exist_ok=True)
    task_card.write_text(
        "---\n"
        "type: implementation\n"
        "role_id: worker-standard\n"
        "base_commit: " + base_commit + "\n"
        "owner_approval:\n"
        "  gate: none\n"
        "  approval_ids: []\n"
        "---\n\n# Task Card\n",
        encoding="utf-8",
    )


def _write_worker_slot_store(root: Path) -> None:
    runtime = root / ".agentdesk" / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
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
    (runtime / "worker-slot-lease.yaml").write_text(
        json.dumps(store, indent=2) + "\n",
        encoding="utf-8",
    )


def _write_dispatch_grant(
    root: Path,
    head: str,
    task_id: str = TASK_ID,
    revision: int = 1,
    attempt: int = 1,
    dispatch_id: str = "DSP-001",
    approval_id: str = "APR-ADMIT-001",
    approval_event_id: str = "EVT-APR-ADMIT-001",
) -> None:
    write_grant(
        project_root=root,
        approval_id=approval_id,
        event_id=approval_event_id,
        scope=ApprovalScope.DISPATCH,
        subject=ApprovalSubject(
            task_id=task_id,
            revision=revision,
            attempt=attempt,
            dispatch_id=dispatch_id,
            accepted_commit=None,
        ),
        lease_epoch=1,
        now=NOW,
        reason="admission test",
        expires_at=None,
        expected_snapshot_commit=head,
    )


def _setup_project(
    task_id: str = TASK_ID,
    state: str = "ready",
    attempt: int | None = None,
) -> tuple[Path, str]:
    root = Path(tempfile.mkdtemp(prefix="agentdesk-admission-"))
    _init_git(root)
    _write_tasks_yaml(root, task_id=task_id, state=state, attempt=attempt)
    _run(["git", "add", "-A"], root)
    _run(["git", "commit", "-m", "state"], root)
    _write_worker_slot_store(root)
    head = _git_head(root)
    _write_dispatch_grant(root, head, task_id=task_id)
    return root, head


def _make_entry(
    sequence: int,
    queue_id: str = "Q-2401",
    task_id: str = TASK_ID,
    revision: int = 1,
    generation: int = 1,
    worker: WorkerKind = WorkerKind.STANDARD_AGENT,
) -> ps.QueueEntry:
    provisional = ps.QueueEntry(
        schema_version=ps.SCHEMA_VERSION,
        queue_id=queue_id,
        task_id=task_id,
        revision=revision,
        enqueue_sequence=sequence,
        enqueued_at=STAMP,
        business_priority=ps.BusinessPriority.P1,
        aging_basis_at=STAMP,
        worker_kind_request=worker,
        assessment_id=f"ASM-{task_id}-r{revision}",
        state=ps.QueuePhase.QUEUED,
        conflict_keys=(),
        retry_budget_used=0,
        content_digest="sha256:" + "0" * 64,
        selection_generation=generation,
    )
    return dataclasses.replace(
        provisional,
        content_digest=ps._entry_digest(provisional),
    )


def _make_receipt(
    entry: ps.QueueEntry,
    receipt_id: str = "SR-2401",
) -> ps.ScheduleReceipt:
    provisional = ps.ScheduleReceipt(
        schema_version=ps.SCHEMA_VERSION,
        receipt_id=receipt_id,
        queue_id=entry.queue_id,
        task_id=entry.task_id,
        revision=entry.revision,
        enqueue_sequence=entry.enqueue_sequence,
        selected_at=STAMP,
        order_key=(1, 0, entry.enqueue_sequence, entry.queue_id),
        selection_reason="priority_order",
        worker_kind=entry.worker_kind_request,
        dispatch_event_id=None,
        phase=ps.ReceiptPhase.SELECTED,
        content_digest="sha256:" + "0" * 64,
        selection_generation=entry.selection_generation,
    )
    return dataclasses.replace(
        provisional,
        content_digest=ps._receipt_digest(provisional),
    )


def _setup_selected(
    root: Path,
    queue_id: str = "Q-2401",
    task_id: str = TASK_ID,
    receipt_id: str = "SR-2401",
) -> tuple[ps.PortfolioSchedulerStore, ps.QueueEntry, ps.ScheduleReceipt]:
    store = ps.PortfolioSchedulerStore(root)
    sequence = store.reserve_enqueue_sequence()
    entry = _make_entry(sequence, queue_id=queue_id, task_id=task_id)
    store.create_queue_entry(entry)
    entry = store.advance_queue_phase(
        entry.queue_id,
        entry.selection_generation,
        ps.QueuePhase.SELECTED,
    )
    receipt = _make_receipt(entry, receipt_id=receipt_id)
    store.create_schedule_receipt(receipt)
    return store, entry, receipt


def _make_request(
    root: Path,
    entry: ps.QueueEntry,
    receipt: ps.ScheduleReceipt,
    **overrides: object,
) -> pa.PortfolioAdmissionRequest:
    head = _git_head(root)
    request = pa.PortfolioAdmissionRequest(
        schema_version=ps.SCHEMA_VERSION,
        queue_id=entry.queue_id,
        receipt_id=receipt.receipt_id,
        task_id=entry.task_id,
        revision=entry.revision,
        enqueue_sequence=entry.enqueue_sequence,
        selection_generation=entry.selection_generation,
        worker_kind=entry.worker_kind_request,
        assessment_id=entry.assessment_id,
        dispatch_id="DSP-001",
        event_id="EVT-ADMIT-001",
        outbox_message_id="MSG-ADMIT-001",
        role_id="agent",
        task_card_path="tasks/TC-2401/task.md",
        task_card_commit="b" * 40,
        base_commit="c" * 40,
        branch="feat/admit",
        report_path="reports/report.md",
        model_selection=_make_model_selection(),
        expected_task_state="ready",
        expected_task_attempt=0,
        new_attempt=1,
        expected_snapshot_commit=head,
        expected_queue_phase=ps.QueuePhase.SELECTED,
        expected_receipt_phase=ps.ReceiptPhase.SELECTED,
        expected_queue_digest=entry.content_digest,
        expected_receipt_digest=receipt.content_digest,
        policy_version=ps.SCHEMA_VERSION,
        now=NOW,
        holder_instance_id="inst-001",
        canonical_worktree=str(root),
    )
    return dataclasses.replace(request, **overrides)


def _transition_request(
    request: pa.PortfolioAdmissionRequest,
) -> TransitionRequest:
    return TransitionRequest(
        cas=TransitionCAS(
            task_id=request.task_id,
            expected_revision=request.revision,
            expected_state=request.expected_task_state,
            expected_snapshot_commit=request.expected_snapshot_commit,
        ),
        dispatch_cas=None,
        event_id=request.event_id,
        event_type="TASK_DISPATCHED",
        payload=DispatchPayload(
            dispatch_id=request.dispatch_id,
            role_id=request.role_id,
            model_selection=request.model_selection,
            task_card_path=request.task_card_path,
            task_card_commit=request.task_card_commit,
            base_commit=request.base_commit,
            branch=request.branch,
            report_path=request.report_path,
            outbox_message_id=request.outbox_message_id,
            new_attempt=request.new_attempt,
        ),
        event_context=TransitionEventContext(
            source_message_id=None,
            evidence_refs=(),
            guard_results=(),
        ),
    )


def _apply_dispatch_direct(
    root: Path,
    request: pa.PortfolioAdmissionRequest,
) -> None:
    lease = acquire_worker_slot(
        root,
        request.worker_kind,
        request.dispatch_id,
        request.holder_instance_id,
        Path(request.canonical_worktree),
        request.now,
    )
    try:
        ControlPlaneTransitionService(root).apply_transition(
            _transition_request(request),
            lease,
            request.now,
        )
    except BaseException:
        release_worker_slot(root, lease, request.now)
        raise


def _event_ids(root: Path) -> list[str]:
    events_dir = root / "docs" / "pm" / "events"
    if not events_dir.is_dir():
        return []
    return sorted(path.stem for path in events_dir.glob("*.yaml"))


def _outbox_ids(root: Path) -> list[str]:
    outbox_dir = root / "docs" / "pm" / "outbox"
    if not outbox_dir.is_dir():
        return []
    return sorted(path.stem for path in outbox_dir.glob("*.yaml"))


def _lease_count(root: Path) -> int:
    return len(read_worker_slot_leases(root)["leases"])


def _remove_tree(root: Path) -> None:
    import shutil

    def _onerror(function: object, path: str, exc_info: object) -> None:
        try:
            os.chmod(path, stat.S_IWRITE)
            function(path)
        except OSError:
            pass

    shutil.rmtree(root, onerror=_onerror)


class PortfolioAdmissionTypeTests(unittest.TestCase):
    def test_public_value_types_are_frozen_slots_without_forbidden_fields(
        self,
    ) -> None:
        for cls in (
            pa.PortfolioAdmissionRequest,
            pa.PortfolioAdmissionResult,
        ):
            self.assertTrue(dataclasses.is_dataclass(cls))
            self.assertTrue(cls.__dataclass_params__.frozen)
            self.assertTrue(hasattr(cls, "__slots__"))
        annotations = {
            **pa.PortfolioAdmissionRequest.__annotations__,
            **pa.PortfolioAdmissionResult.__annotations__,
        }
        text = repr(annotations)
        for forbidden in ("Any", "Mapping", "dict", "list", "set"):
            self.assertNotIn(forbidden, text)

    def test_attempt_four_is_rejected_before_any_side_effect(self) -> None:
        root, head = _setup_project()
        try:
            _, entry, receipt = _setup_selected(root)
            with self.assertRaises(pa.PortfolioAdmissionInputError):
                _make_request(
                    root,
                    entry,
                    receipt,
                    expected_task_attempt=3,
                    new_attempt=4,
                )
            self.assertEqual(_event_ids(root), [])
            self.assertEqual(_lease_count(root), 0)
        finally:
            _remove_tree(root)


class PortfolioAdmissionHappyPathTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root, self.head = _setup_project()
        self.store, self.entry, self.receipt = _setup_selected(self.root)
        self.request = _make_request(
            self.root, self.entry, self.receipt
        )

    def tearDown(self) -> None:
        _remove_tree(self.root)

    def test_happy_path_dispatches_and_aligns_receipt_and_queue(self) -> None:
        result = pa.PortfolioSchedulerAdmission(self.root).admit(
            self.request
        )
        self.assertEqual(result.queue_id, self.entry.queue_id)
        self.assertEqual(result.receipt_id, self.receipt.receipt_id)
        self.assertEqual(result.dispatch_id, "DSP-001")
        self.assertEqual(result.event_id, "EVT-ADMIT-001")
        self.assertEqual(result.queue_phase, ps.QueuePhase.DISPATCHED)
        self.assertEqual(result.receipt_phase, ps.ReceiptPhase.DISPATCHED)
        self.assertEqual(result.transition.from_state, "ready")
        self.assertEqual(result.transition.to_state, "dispatched")
        self.assertEqual(
            result.transition.outbox_message_id, "MSG-ADMIT-001"
        )

        queue = self.store.read_queue_entry(self.entry.queue_id)
        receipt = self.store.read_schedule_receipt(self.receipt.receipt_id)
        self.assertEqual(queue.state, ps.QueuePhase.DISPATCHED)
        self.assertEqual(receipt.phase, ps.ReceiptPhase.DISPATCHED)
        self.assertEqual(receipt.dispatch_event_id, "EVT-ADMIT-001")
        self.assertEqual(_event_ids(self.root), ["EVT-ADMIT-001"])
        self.assertEqual(_outbox_ids(self.root), ["MSG-ADMIT-001"])
        self.assertEqual(_lease_count(self.root), 1)

    def test_byte_exact_replay_does_not_create_duplicate_event_or_lease(
        self,
    ) -> None:
        first = pa.PortfolioSchedulerAdmission(self.root).admit(
            self.request
        )
        second = pa.PortfolioSchedulerAdmission(self.root).admit(
            self.request
        )
        self.assertEqual(first, second)
        self.assertEqual(_event_ids(self.root), ["EVT-ADMIT-001"])
        self.assertEqual(_outbox_ids(self.root), ["MSG-ADMIT-001"])
        self.assertEqual(_lease_count(self.root), 1)

    def test_same_identity_divergent_digest_is_rejected_before_lease(
        self,
    ) -> None:
        divergent = dataclasses.replace(
            self.request,
            expected_queue_digest="sha256:" + "1" * 64,
        )
        with self.assertRaises(pa.PortfolioAdmissionConflictError):
            pa.PortfolioSchedulerAdmission(self.root).admit(divergent)
        self.assertEqual(_event_ids(self.root), [])
        self.assertEqual(_lease_count(self.root), 0)


class PortfolioAdmissionFailClosedTests(unittest.TestCase):
    def _fresh(self) -> tuple[Path, str, ps.PortfolioSchedulerStore, ps.QueueEntry, ps.ScheduleReceipt]:
        root, head = _setup_project()
        store, entry, receipt = _setup_selected(root)
        return root, head, store, entry, receipt

    def _assert_no_side_effects(self, root: Path) -> None:
        self.assertEqual(_event_ids(root), [])
        self.assertEqual(_outbox_ids(root), [])
        self.assertEqual(_lease_count(root), 0)

    def test_missing_queue_entry_fails_closed(self) -> None:
        root, head, _, entry, receipt = self._fresh()
        try:
            request = _make_request(root, entry, receipt, queue_id="Q-MISSING")
            with self.assertRaises(ps.PortfolioSchedulerNotFoundError):
                pa.PortfolioSchedulerAdmission(root).admit(request)
            self._assert_no_side_effects(root)
        finally:
            _remove_tree(root)

    def test_missing_receipt_fails_closed(self) -> None:
        root, head, _, entry, receipt = self._fresh()
        try:
            request = _make_request(
                root, entry, receipt, receipt_id="SR-MISSING"
            )
            with self.assertRaises(ps.PortfolioSchedulerNotFoundError):
                pa.PortfolioSchedulerAdmission(root).admit(request)
            self._assert_no_side_effects(root)
        finally:
            _remove_tree(root)

    def test_identity_mismatch_fails_closed(self) -> None:
        root, head, _, entry, receipt = self._fresh()
        try:
            request = _make_request(
                root, entry, receipt, enqueue_sequence=999
            )
            with self.assertRaises(pa.PortfolioAdmissionConflictError):
                pa.PortfolioSchedulerAdmission(root).admit(request)
            self._assert_no_side_effects(root)
        finally:
            _remove_tree(root)

    def test_stale_queue_phase_fails_closed(self) -> None:
        root, head, store, entry, receipt = self._fresh()
        try:
            store.advance_queue_phase(
                entry.queue_id,
                entry.selection_generation,
                ps.QueuePhase.DISPATCHED,
                "EVT-OTHER-001",
            )
            request = _make_request(root, entry, receipt)
            with self.assertRaises(pa.PortfolioAdmissionConflictError):
                pa.PortfolioSchedulerAdmission(root).admit(request)
            self._assert_no_side_effects(root)
        finally:
            _remove_tree(root)

    def test_stale_receipt_phase_fails_closed(self) -> None:
        root, head, store, entry, receipt = self._fresh()
        try:
            store.advance_schedule_receipt(
                receipt.receipt_id,
                receipt.selection_generation,
                ps.ReceiptPhase.DISPATCHED,
                "EVT-OTHER-001",
            )
            request = _make_request(root, entry, receipt)
            with self.assertRaises(pa.PortfolioAdmissionConflictError):
                pa.PortfolioSchedulerAdmission(root).admit(request)
            self._assert_no_side_effects(root)
        finally:
            _remove_tree(root)

    def test_stale_policy_version_rejected_before_side_effects(self) -> None:
        root, head, _, entry, receipt = self._fresh()
        try:
            with self.assertRaises(pa.PortfolioAdmissionInputError):
                _make_request(
                    root,
                    entry,
                    receipt,
                    policy_version="agentdesk.portfolio-scheduler/v2",
                )
            self._assert_no_side_effects(root)
        finally:
            _remove_tree(root)

    def test_canonical_not_ready_fails_closed(self) -> None:
        root, head, _, entry, receipt = self._fresh()
        try:
            (root / "docs" / "pm" / "state" / "tasks.yaml").unlink()
            _write_tasks_yaml(root, state="draft")
            _run(["git", "add", "-A"], root)
            _run(["git", "commit", "-m", "draft"], root)
            new_head = _git_head(root)
            request = _make_request(
                root,
                entry,
                receipt,
                expected_snapshot_commit=new_head,
            )
            with self.assertRaises(pa.PortfolioAdmissionConflictError):
                pa.PortfolioSchedulerAdmission(root).admit(request)
            self._assert_no_side_effects(root)
        finally:
            _remove_tree(root)

    def test_stale_task_attempt_fails_closed(self) -> None:
        root, head, _, entry, receipt = self._fresh()
        try:
            (root / "docs" / "pm" / "state" / "tasks.yaml").unlink()
            _write_tasks_yaml(root, state="ready", attempt=1)
            _run(["git", "add", "-A"], root)
            _run(["git", "commit", "-m", "attempt"], root)
            new_head = _git_head(root)
            request = _make_request(
                root,
                entry,
                receipt,
                expected_snapshot_commit=new_head,
            )
            with self.assertRaises(pa.PortfolioAdmissionConflictError):
                pa.PortfolioSchedulerAdmission(root).admit(request)
            self._assert_no_side_effects(root)
        finally:
            _remove_tree(root)

    def test_task_card_identity_mismatch_fails_closed(self) -> None:
        root, head, _, entry, receipt = self._fresh()
        try:
            request = _make_request(
                root,
                entry,
                receipt,
                task_card_path="tasks/OTHER/task.md",
            )
            with self.assertRaises(pa.PortfolioAdmissionConflictError):
                pa.PortfolioSchedulerAdmission(root).admit(request)
            self._assert_no_side_effects(root)
        finally:
            _remove_tree(root)

    def test_worker_kind_mismatch_fails_closed(self) -> None:
        root, head, _, entry, receipt = self._fresh()
        try:
            request = _make_request(
                root,
                entry,
                receipt,
                worker_kind=WorkerKind.BASIC_AGENT,
            )
            with self.assertRaises(pa.PortfolioAdmissionConflictError):
                pa.PortfolioSchedulerAdmission(root).admit(request)
            self._assert_no_side_effects(root)
        finally:
            _remove_tree(root)

    def test_retired_entry_cannot_be_admitted(self) -> None:
        root, head, store, entry, receipt = self._fresh()
        try:
            store.advance_queue_phase(
                entry.queue_id,
                entry.selection_generation,
                ps.QueuePhase.DISPATCHED,
                "EVT-OTHER-001",
            )
            store.retire_queue_entry(
                entry.queue_id,
                entry.selection_generation,
            )
            request = _make_request(root, entry, receipt)
            with self.assertRaises(pa.PortfolioAdmissionConflictError):
                pa.PortfolioSchedulerAdmission(root).admit(request)
            self._assert_no_side_effects(root)
        finally:
            _remove_tree(root)

    def test_existing_different_dispatch_event_conflicts(self) -> None:
        root, head, _, entry, receipt = self._fresh()
        try:
            other = dataclasses.replace(
                _make_request(root, entry, receipt),
                event_id="EVT-OTHER-001",
                outbox_message_id="MSG-OTHER-001",
            )
            _apply_dispatch_direct(root, other)
            request = _make_request(root, entry, receipt)
            with self.assertRaises(pa.PortfolioAdmissionConflictError):
                pa.PortfolioSchedulerAdmission(root).admit(request)
            self.assertEqual(_lease_count(root), 1)
            self.assertEqual(_event_ids(root), ["EVT-OTHER-001"])
        finally:
            _remove_tree(root)


class PortfolioAdmissionCrashWindowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root, self.head = _setup_project()
        self.store, self.entry, self.receipt = _setup_selected(self.root)
        self.request = _make_request(
            self.root, self.entry, self.receipt
        )

    def tearDown(self) -> None:
        _remove_tree(self.root)

    def test_task_dispatched_before_scheduler_evidence_replay_aligns(self) -> None:
        _apply_dispatch_direct(self.root, self.request)
        self.assertEqual(self.entry.state, ps.QueuePhase.SELECTED)
        self.assertEqual(self.receipt.phase, ps.ReceiptPhase.SELECTED)

        result = pa.PortfolioSchedulerAdmission(self.root).admit(
            self.request
        )
        self.assertEqual(result.event_id, "EVT-ADMIT-001")
        self.assertEqual(result.queue_phase, ps.QueuePhase.DISPATCHED)
        self.assertEqual(result.receipt_phase, ps.ReceiptPhase.DISPATCHED)
        self.assertEqual(_event_ids(self.root), ["EVT-ADMIT-001"])
        self.assertEqual(_outbox_ids(self.root), ["MSG-ADMIT-001"])
        self.assertEqual(_lease_count(self.root), 1)

    def test_receipt_dispatched_before_queue_replay_aligns(self) -> None:
        _apply_dispatch_direct(self.root, self.request)
        self.store.advance_schedule_receipt(
            self.receipt.receipt_id,
            self.receipt.selection_generation,
            ps.ReceiptPhase.DISPATCHED,
            "EVT-ADMIT-001",
        )

        result = pa.PortfolioSchedulerAdmission(self.root).admit(
            self.request
        )
        self.assertEqual(result.queue_phase, ps.QueuePhase.DISPATCHED)
        self.assertEqual(result.receipt_phase, ps.ReceiptPhase.DISPATCHED)
        self.assertEqual(_event_ids(self.root), ["EVT-ADMIT-001"])
        self.assertEqual(_outbox_ids(self.root), ["MSG-ADMIT-001"])
        self.assertEqual(_lease_count(self.root), 1)


class PortfolioAdmissionConcurrencyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root, self.head = _setup_project()
        self.store, self.entry, self.receipt = _setup_selected(self.root)
        self.request = _make_request(
            self.root, self.entry, self.receipt
        )

    def tearDown(self) -> None:
        _remove_tree(self.root)

    def test_two_coroutines_for_one_entry_have_one_winner(self) -> None:
        async def run() -> tuple[object, object]:
            admission = pa.PortfolioSchedulerAdmission(self.root)
            results = await asyncio.gather(
                asyncio.to_thread(admission.admit, self.request),
                asyncio.to_thread(admission.admit, self.request),
                return_exceptions=True,
            )
            return results

        results = asyncio.run(run())
        successes = [item for item in results if isinstance(item, pa.PortfolioAdmissionResult)]
        failures = [item for item in results if isinstance(item, BaseException)]
        self.assertEqual(len(successes), 1)
        self.assertEqual(len(failures), 1)
        self.assertIsInstance(
            failures[0],
            (WorkerSlotCapacityError, WorkerSlotContentionError),
        )
        self.assertEqual(_event_ids(self.root), ["EVT-ADMIT-001"])
        self.assertEqual(_outbox_ids(self.root), ["MSG-ADMIT-001"])
        self.assertEqual(_lease_count(self.root), 1)

    def test_two_entries_same_slot_fail_with_capacity_error(self) -> None:
        second_task = "TC-2402"
        (self.root / "docs" / "pm" / "state" / "tasks.yaml").unlink()
        _write_tasks_yaml(
            self.root,
            task_id=self.entry.task_id,
            task_card_path="tasks/TC-2401/task.md",
        )
        # Append the second task manually to the same canonical document.
        state_path = self.root / "docs" / "pm" / "state" / "tasks.yaml"
        doc = json.loads(state_path.read_text(encoding="utf-8"))
        second = dict(doc["tasks"][0])
        second["task_id"] = second_task
        second["task_card_path"] = f"tasks/{second_task}/task.md"
        doc["tasks"].append(second)
        state_path.write_text(
            json.dumps(doc, indent=2) + "\n",
            encoding="utf-8",
        )
        second_task_dir = self.root / "tasks" / second_task
        second_task_dir.mkdir(parents=True, exist_ok=True)
        (second_task_dir / "task.md").write_text(
            "# Second\n",
            encoding="utf-8",
        )
        _run(["git", "add", "-A"], self.root)
        _run(["git", "commit", "-m", "second"], self.root)
        new_head = _git_head(self.root)
        first_request = _make_request(
            self.root,
            self.entry,
            self.receipt,
            expected_snapshot_commit=new_head,
        )
        _write_dispatch_grant(
            self.root,
            new_head,
            task_id=second_task,
            dispatch_id="DSP-002",
            approval_id="APR-ADMIT-002",
            approval_event_id="EVT-APR-ADMIT-002",
        )

        store = ps.PortfolioSchedulerStore(self.root)
        sequence = store.reserve_enqueue_sequence()
        second_entry = _make_entry(
            sequence,
            queue_id="Q-2402",
            task_id=second_task,
        )
        store.create_queue_entry(second_entry)
        second_entry = store.advance_queue_phase(
            second_entry.queue_id,
            second_entry.selection_generation,
            ps.QueuePhase.SELECTED,
        )
        second_receipt = _make_receipt(
            second_entry, receipt_id="SR-2402"
        )
        store.create_schedule_receipt(second_receipt)
        second_request = _make_request(
            self.root,
            second_entry,
            second_receipt,
            dispatch_id="DSP-002",
            event_id="EVT-ADMIT-002",
            outbox_message_id="MSG-ADMIT-002",
            task_card_path=f"tasks/{second_task}/task.md",
            expected_snapshot_commit=new_head,
            holder_instance_id="inst-002",
        )

        pa.PortfolioSchedulerAdmission(self.root).admit(first_request)
        with self.assertRaises(WorkerSlotCapacityError):
            pa.PortfolioSchedulerAdmission(self.root).admit(second_request)
        self.assertEqual(_event_ids(self.root), ["EVT-ADMIT-001"])
        self.assertEqual(_outbox_ids(self.root), ["MSG-ADMIT-001"])
        self.assertEqual(_lease_count(self.root), 1)


if __name__ == "__main__":
    unittest.main()
