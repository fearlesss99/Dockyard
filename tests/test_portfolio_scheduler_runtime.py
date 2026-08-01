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
from portfolio_scheduler_policy import AdmissionContext, select_next  # noqa: E402
from portfolio_scheduler_recovery import RecoveryAction  # noqa: E402
import portfolio_scheduler_runtime as pr  # noqa: E402
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
        reason="runtime test",
        expires_at=None,
        expected_snapshot_commit=head,
    )


def _setup_project(
    task_id: str = TASK_ID,
    state: str = "ready",
    attempt: int | None = None,
) -> tuple[Path, str]:
    root = Path(tempfile.mkdtemp(prefix="agentdesk-runtime-"))
    _init_git(root)
    _write_tasks_yaml(root, task_id=task_id, state=state, attempt=attempt)
    _run(["git", "add", "-A"], root)
    _run(["git", "commit", "-m", "state"], root)
    _write_worker_slot_store(root)
    head = _git_head(root)
    _write_dispatch_grant(root, head, task_id=task_id, attempt=attempt or 1)
    return root, head


def _make_entry(
    sequence: int,
    queue_id: str = "Q-2401",
    task_id: str = TASK_ID,
    revision: int = 1,
    generation: int = 1,
    worker: WorkerKind = WorkerKind.STANDARD_AGENT,
    business_priority: ps.BusinessPriority = ps.BusinessPriority.P1,
) -> ps.QueueEntry:
    provisional = ps.QueueEntry(
        schema_version=ps.SCHEMA_VERSION,
        queue_id=queue_id,
        task_id=task_id,
        revision=revision,
        enqueue_sequence=sequence,
        enqueued_at=STAMP,
        business_priority=business_priority,
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
        selection_reason="priority_p1:oldest_in_band:fifo",
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


def _setup_queued(
    root: Path,
    queue_id: str = "Q-2401",
    task_id: str = TASK_ID,
    worker: WorkerKind = WorkerKind.STANDARD_AGENT,
    generation: int = 1,
) -> tuple[ps.PortfolioSchedulerStore, ps.QueueEntry]:
    store = ps.PortfolioSchedulerStore(root)
    sequence = store.reserve_enqueue_sequence()
    entry = _make_entry(
        sequence,
        queue_id=queue_id,
        task_id=task_id,
        generation=generation,
        worker=worker,
    )
    store.create_queue_entry(entry)
    return store, entry


def _setup_selected(
    root: Path,
    queue_id: str = "Q-2401",
    task_id: str = TASK_ID,
    receipt_id: str = "SR-2401",
) -> tuple[ps.PortfolioSchedulerStore, ps.QueueEntry, ps.ScheduleReceipt]:
    store, entry = _setup_queued(root, queue_id=queue_id, task_id=task_id)
    entry = store.advance_queue_phase(
        entry.queue_id,
        entry.selection_generation,
        ps.QueuePhase.SELECTED,
    )
    receipt = _make_receipt(entry, receipt_id=receipt_id)
    store.create_schedule_receipt(receipt)
    return store, entry, receipt


def _admission_context() -> AdmissionContext:
    return AdmissionContext(
        policy_version=ps.SCHEMA_VERSION,
        evaluated_at=STAMP,
        active_hard_conflict_keys=(),
        advisory_conflict_keys=(),
        advisory_authorizations=(),
        available_worker_kinds=(WorkerKind.STANDARD_AGENT,),
    )


def _make_plan(
    root: Path,
    entry: ps.QueueEntry,
    receipt: ps.ScheduleReceipt | None = None,
    **overrides: object,
) -> pr.PortfolioAdmissionPlan:
    head = _git_head(root)
    receipt_id = (
        receipt.receipt_id
        if receipt is not None
        else f"SR-{entry.queue_id[2:]}-{entry.enqueue_sequence}-g{entry.selection_generation}"
    )
    plan = pr.PortfolioAdmissionPlan(
        schema_version=ps.SCHEMA_VERSION,
        queue_id=entry.queue_id,
        receipt_id=receipt_id,
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
        policy_version=ps.SCHEMA_VERSION,
        now=NOW,
        holder_instance_id="inst-001",
        canonical_worktree=str(root),
    )
    return dataclasses.replace(plan, **overrides)


def _make_tick_request(
    root: Path,
    entry: ps.QueueEntry,
    receipt: ps.ScheduleReceipt | None = None,
    **plan_overrides: object,
) -> pr.PortfolioSchedulerTickRequest:
    plan = _make_plan(root, entry, receipt, **plan_overrides)
    return pr.PortfolioSchedulerTickRequest(
        project_root=root,
        plan=plan,
        admission_context=_admission_context(),
    )


def _admission_request(
    plan: pr.PortfolioAdmissionPlan,
    entry: ps.QueueEntry,
    receipt: ps.ScheduleReceipt,
) -> pa.PortfolioAdmissionRequest:
    expected_queue_phase = (
        entry.state
        if entry.state in (ps.QueuePhase.DISPATCHED, ps.QueuePhase.RETIRED)
        else ps.QueuePhase.SELECTED
    )
    return pa.PortfolioAdmissionRequest(
        schema_version=ps.SCHEMA_VERSION,
        queue_id=entry.queue_id,
        receipt_id=receipt.receipt_id,
        task_id=entry.task_id,
        revision=entry.revision,
        enqueue_sequence=entry.enqueue_sequence,
        selection_generation=entry.selection_generation,
        worker_kind=entry.worker_kind_request,
        assessment_id=entry.assessment_id,
        dispatch_id=plan.dispatch_id,
        event_id=plan.event_id,
        outbox_message_id=plan.outbox_message_id,
        role_id=plan.role_id,
        task_card_path=plan.task_card_path,
        task_card_commit=plan.task_card_commit,
        base_commit=plan.base_commit,
        branch=plan.branch,
        report_path=plan.report_path,
        model_selection=plan.model_selection,
        expected_task_state=plan.expected_task_state,
        expected_task_attempt=plan.expected_task_attempt,
        new_attempt=plan.new_attempt,
        expected_snapshot_commit=plan.expected_snapshot_commit,
        expected_queue_phase=expected_queue_phase,
        expected_receipt_phase=receipt.phase,
        expected_queue_digest=entry.content_digest,
        expected_receipt_digest=receipt.content_digest,
        policy_version=plan.policy_version,
        now=plan.now,
        holder_instance_id=plan.holder_instance_id,
        canonical_worktree=plan.canonical_worktree,
    )


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


def _append_second_task(root: Path, second_task: str) -> str:
    state_path = root / "docs" / "pm" / "state" / "tasks.yaml"
    doc = json.loads(state_path.read_text(encoding="utf-8"))
    second = dict(doc["tasks"][0])
    second["task_id"] = second_task
    second["task_card_path"] = f"tasks/{second_task}/task.md"
    doc["tasks"].append(second)
    state_path.write_text(
        json.dumps(doc, indent=2) + "\n",
        encoding="utf-8",
    )
    second_dir = root / "tasks" / second_task
    second_dir.mkdir(parents=True, exist_ok=True)
    (second_dir / "task.md").write_text("# Second\n", encoding="utf-8")
    _run(["git", "add", "-A"], root)
    _run(["git", "commit", "-m", "second"], root)
    return _git_head(root)


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


def _receipt_ids(root: Path) -> list[str]:
    receipts_dir = root / "docs" / "pm" / "portfolio-scheduler" / "receipts"
    if not receipts_dir.is_dir():
        return []
    return sorted(path.stem for path in receipts_dir.glob("*.yaml"))


def _remove_tree(root: Path) -> None:
    import shutil

    def _onerror(function: object, path: str, exc_info: object) -> None:
        try:
            os.chmod(path, stat.S_IWRITE)
            function(path)
        except OSError:
            pass

    shutil.rmtree(root, onerror=_onerror)


class PortfolioSchedulerRuntimeTickTests(unittest.TestCase):
    def test_empty_queue_idle_and_locks_released_after_failure(self) -> None:
        root, head = _setup_project()
        try:
            store = ps.PortfolioSchedulerStore(root)
            store.reserve_enqueue_sequence()
            dummy = _make_entry(1, queue_id="Q-EMPTY")
            request = _make_tick_request(root, dummy)
            result = pr.PortfolioSchedulerRuntime(root).tick(request)
            self.assertEqual(
                result.outcome.kind,
                pr.PortfolioSchedulerTickOutcomeKind.IDLE,
            )
            self.assertIsNone(result.admission_result)
            self.assertEqual(_event_ids(root), [])
            self.assertEqual(_outbox_ids(root), [])
            self.assertEqual(_lease_count(root), 0)
            self.assertEqual(_receipt_ids(root), [])
            self.assertEqual(
                store.enumerate_queue_snapshot(),
                (),
            )
            store, entry = _setup_queued(root)
            request = _make_tick_request(
                root,
                entry,
                receipt_id="SR-2401-3-g1",
            )
            with self.assertRaises(pr.PortfolioSchedulerRuntimeConflictError):
                pr.PortfolioSchedulerRuntime(root).tick(request)
            self.assertEqual(store.enumerate_queue_snapshot()[0].queue_id, entry.queue_id)
            self.assertEqual(store.reserve_enqueue_sequence(), 3)
        finally:
            _remove_tree(root)

    def test_single_candidate_happy_path(self) -> None:
        root, head = _setup_project()
        try:
            store, entry = _setup_queued(root)
            request = _make_tick_request(root, entry)
            result = pr.PortfolioSchedulerRuntime(root).tick(request)
            self.assertEqual(
                result.outcome.kind,
                pr.PortfolioSchedulerTickOutcomeKind.ADMITTED,
            )
            self.assertEqual(result.admission_result.event_id, "EVT-ADMIT-001")
            self.assertEqual(_event_ids(root), ["EVT-ADMIT-001"])
            self.assertEqual(_outbox_ids(root), ["MSG-ADMIT-001"])
            self.assertEqual(_lease_count(root), 1)
            self.assertEqual(_receipt_ids(root), ["SR-2401-1-g1"])
            queue = store.read_queue_entry(entry.queue_id)
            receipt = store.read_schedule_receipt("SR-2401-1-g1")
            self.assertEqual(queue.state, ps.QueuePhase.DISPATCHED)
            self.assertEqual(receipt.phase, ps.ReceiptPhase.DISPATCHED)
        finally:
            _remove_tree(root)

    def test_multi_candidate_policy_selects_exact_plan(self) -> None:
        root, head = _setup_project()
        try:
            new_head = _append_second_task(root, "TC-2402")
            store = ps.PortfolioSchedulerStore(root)
            sequence = store.reserve_enqueue_sequence()
            first = _make_entry(
                sequence,
                queue_id="Q-2401",
                task_id=TASK_ID,
                business_priority=ps.BusinessPriority.P0,
            )
            store.create_queue_entry(first)
            sequence = store.reserve_enqueue_sequence()
            second = _make_entry(
                sequence,
                queue_id="Q-2402",
                task_id="TC-2402",
            )
            store.create_queue_entry(second)
            plan = _make_plan(
                root,
                first,
                expected_snapshot_commit=new_head,
                task_card_path="tasks/TC-2401/task.md",
            )
            request = pr.PortfolioSchedulerTickRequest(
                project_root=root,
                plan=plan,
                admission_context=_admission_context(),
            )
            result = pr.PortfolioSchedulerRuntime(root).tick(request)
            self.assertEqual(
                result.outcome.kind,
                pr.PortfolioSchedulerTickOutcomeKind.ADMITTED,
            )
            self.assertEqual(store.read_queue_entry(first.queue_id).state, ps.QueuePhase.DISPATCHED)
            self.assertEqual(store.read_queue_entry(second.queue_id).state, ps.QueuePhase.QUEUED)
            self.assertEqual(_receipt_ids(root), ["SR-2401-1-g1"])
        finally:
            _remove_tree(root)

    def test_receipt_written_queue_not_selected_replay(self) -> None:
        root, head = _setup_project()
        try:
            store, entry = _setup_queued(root)
            runtime = pr.PortfolioSchedulerRuntime(root)
            snapshot = store.enumerate_queue_snapshot()
            policy_receipt = select_next(snapshot, _admission_context())
            self.assertIsNotNone(policy_receipt)
            runtime._persist_reservation(root, policy_receipt)
            self.assertEqual(store.read_queue_entry(entry.queue_id).state, ps.QueuePhase.QUEUED)
            request = _make_tick_request(root, entry)
            result = runtime.tick(request)
            self.assertEqual(
                result.outcome.kind,
                pr.PortfolioSchedulerTickOutcomeKind.ADMITTED,
            )
            self.assertEqual(_event_ids(root), ["EVT-ADMIT-001"])
            self.assertEqual(store.read_queue_entry(entry.queue_id).state, ps.QueuePhase.DISPATCHED)
        finally:
            _remove_tree(root)

    def test_queue_selected_admission_not_called_replay(self) -> None:
        root, head = _setup_project()
        try:
            store, entry, receipt = _setup_selected(root)
            request = _make_tick_request(root, entry, receipt)
            result = pr.PortfolioSchedulerRuntime(root).tick(request)
            self.assertEqual(
                result.outcome.kind,
                pr.PortfolioSchedulerTickOutcomeKind.ADMITTED,
            )
            self.assertEqual(_event_ids(root), ["EVT-ADMIT-001"])
            self.assertEqual(store.read_queue_entry(entry.queue_id).state, ps.QueuePhase.DISPATCHED)
        finally:
            _remove_tree(root)

    def test_task_dispatched_committed_scheduler_evidence_lag_replay(self) -> None:
        root, head = _setup_project()
        try:
            store, entry, receipt = _setup_selected(root)
            plan = _make_plan(root, entry, receipt)
            admission_request = _admission_request(plan, entry, receipt)
            _apply_dispatch_direct(root, admission_request)
            self.assertEqual(store.read_queue_entry(entry.queue_id).state, ps.QueuePhase.SELECTED)
            request = _make_tick_request(root, entry, receipt)
            result = pr.PortfolioSchedulerRuntime(root).tick(request)
            self.assertEqual(
                result.outcome.kind,
                pr.PortfolioSchedulerTickOutcomeKind.ADMITTED,
            )
            self.assertEqual(_event_ids(root), ["EVT-ADMIT-001"])
            self.assertEqual(store.read_queue_entry(entry.queue_id).state, ps.QueuePhase.DISPATCHED)
            self.assertEqual(_lease_count(root), 1)
        finally:
            _remove_tree(root)

    def test_same_tick_byte_exact_replay(self) -> None:
        root, head = _setup_project()
        try:
            store, entry = _setup_queued(root)
            request = _make_tick_request(root, entry)
            runtime = pr.PortfolioSchedulerRuntime(root)
            first = runtime.tick(request)
            second = runtime.tick(request)
            self.assertEqual(first, second)
            self.assertEqual(_event_ids(root), ["EVT-ADMIT-001"])
            self.assertEqual(_outbox_ids(root), ["MSG-ADMIT-001"])
            self.assertEqual(_lease_count(root), 1)
            self.assertEqual(_receipt_ids(root), ["SR-2401-1-g1"])
        finally:
            _remove_tree(root)

    def test_divergent_plan_and_stale_generation_rejected(self) -> None:
        root, head = _setup_project()
        try:
            store, first = _setup_queued(root)
            sequence = store.reserve_enqueue_sequence()
            second = _make_entry(sequence, queue_id="Q-2402", task_id="TC-2402")
            store.create_queue_entry(second)
            divergent = _make_tick_request(
                root,
                first,
                receipt_id="SR-2401-2-g1",
            )
            with self.assertRaises(pr.PortfolioSchedulerRuntimeConflictError):
                pr.PortfolioSchedulerRuntime(root).tick(divergent)
            stale = _make_tick_request(
                root,
                second,
                selection_generation=2,
            )
            with self.assertRaises(pr.PortfolioSchedulerRuntimeConflictError):
                pr.PortfolioSchedulerRuntime(root).tick(stale)
            self.assertEqual(_event_ids(root), [])
            self.assertEqual(_lease_count(root), 0)
            self.assertEqual(_receipt_ids(root), [])
            self.assertEqual(store.read_queue_entry(first.queue_id).state, ps.QueuePhase.QUEUED)
            self.assertEqual(store.read_queue_entry(second.queue_id).state, ps.QueuePhase.QUEUED)
        finally:
            _remove_tree(root)

    def test_stale_git_cas_rejected_before_lease(self) -> None:
        root, head = _setup_project()
        try:
            store, entry = _setup_queued(root)
            request = _make_tick_request(
                root,
                entry,
                expected_snapshot_commit="0" * 40,
            )
            with self.assertRaises(pa.PortfolioAdmissionConflictError):
                pr.PortfolioSchedulerRuntime(root).tick(request)
            self.assertEqual(_event_ids(root), [])
            self.assertEqual(_lease_count(root), 0)
            self.assertEqual(store.read_queue_entry(entry.queue_id).state, ps.QueuePhase.SELECTED)
            self.assertEqual(
                store.read_schedule_receipt("SR-2401-1-g1").phase,
                ps.ReceiptPhase.SELECTED,
            )
        finally:
            _remove_tree(root)

    def test_two_coroutines_same_entry_have_one_winner(self) -> None:
        root, head = _setup_project()
        try:
            store, entry = _setup_queued(root)
            request = _make_tick_request(root, entry)
            runtime = pr.PortfolioSchedulerRuntime(root)

            async def run() -> tuple[object, object]:
                return await asyncio.gather(
                    asyncio.to_thread(runtime.tick, request),
                    asyncio.to_thread(runtime.tick, request),
                    return_exceptions=True,
                )

            results = asyncio.run(run())
            successes = [
                item
                for item in results
                if isinstance(item, pr.PortfolioSchedulerTickResult)
            ]
            failures = [item for item in results if isinstance(item, BaseException)]
            self.assertEqual(len(successes), 1)
            self.assertEqual(len(failures), 1)
            self.assertIsInstance(
                failures[0],
                (
                    WorkerSlotCapacityError,
                    WorkerSlotContentionError,
                    ps.PortfolioSchedulerLockConflictError,
                ),
            )
            self.assertEqual(_event_ids(root), ["EVT-ADMIT-001"])
            self.assertEqual(_outbox_ids(root), ["MSG-ADMIT-001"])
            self.assertEqual(_lease_count(root), 1)
            self.assertEqual(_receipt_ids(root), ["SR-2401-1-g1"])
        finally:
            _remove_tree(root)

    def test_two_entries_same_slot_capacity_error(self) -> None:
        root, head = _setup_project()
        try:
            new_head = _append_second_task(root, "TC-2402")
            store = ps.PortfolioSchedulerStore(root)
            sequence = store.reserve_enqueue_sequence()
            first = _make_entry(sequence, queue_id="Q-2401", task_id=TASK_ID)
            store.create_queue_entry(first)
            sequence = store.reserve_enqueue_sequence()
            second = _make_entry(sequence, queue_id="Q-2402", task_id="TC-2402")
            store.create_queue_entry(second)
            _write_dispatch_grant(
                root,
                new_head,
                task_id="TC-2402",
                dispatch_id="DSP-002",
                approval_id="APR-ADMIT-002",
                approval_event_id="EVT-APR-ADMIT-002",
            )
            first_plan = _make_plan(
                root,
                first,
                expected_snapshot_commit=new_head,
            )
            first_request = pr.PortfolioSchedulerTickRequest(
                project_root=root,
                plan=first_plan,
                admission_context=_admission_context(),
            )
            pr.PortfolioSchedulerRuntime(root).tick(first_request)
            second_plan = _make_plan(
                root,
                second,
                expected_snapshot_commit=new_head,
                dispatch_id="DSP-002",
                event_id="EVT-ADMIT-002",
                outbox_message_id="MSG-ADMIT-002",
                task_card_path="tasks/TC-2402/task.md",
                holder_instance_id="inst-002",
            )
            second_request = pr.PortfolioSchedulerTickRequest(
                project_root=root,
                plan=second_plan,
                admission_context=_admission_context(),
            )
            with self.assertRaises(WorkerSlotCapacityError):
                pr.PortfolioSchedulerRuntime(root).tick(second_request)
            self.assertEqual(_event_ids(root), ["EVT-ADMIT-001"])
            self.assertEqual(_lease_count(root), 1)
        finally:
            _remove_tree(root)

    def test_attempt_three_allowed(self) -> None:
        root, head = _setup_project(state="ready", attempt=2)
        try:
            _write_dispatch_grant(
                root,
                head,
                attempt=3,
                approval_id="APR-ADMIT-003",
                approval_event_id="EVT-APR-ADMIT-003",
            )
            store, entry = _setup_queued(root)
            request = _make_tick_request(
                root,
                entry,
                expected_task_attempt=2,
                new_attempt=3,
            )
            result = pr.PortfolioSchedulerRuntime(root).tick(request)
            self.assertEqual(
                result.outcome.kind,
                pr.PortfolioSchedulerTickOutcomeKind.ADMITTED,
            )
            self.assertEqual(_event_ids(root), ["EVT-ADMIT-001"])
            self.assertEqual(_lease_count(root), 1)
        finally:
            _remove_tree(root)

    def test_attempt_four_zero_side_effects(self) -> None:
        root, head = _setup_project()
        try:
            store, entry = _setup_queued(root)
            with self.assertRaises(pr.PortfolioSchedulerRuntimeInputError):
                _make_tick_request(
                    root,
                    entry,
                    expected_task_attempt=3,
                    new_attempt=4,
                )
            self.assertEqual(_event_ids(root), [])
            self.assertEqual(_outbox_ids(root), [])
            self.assertEqual(_lease_count(root), 0)
            self.assertEqual(_receipt_ids(root), [])
            self.assertEqual(store.read_queue_entry(entry.queue_id).state, ps.QueuePhase.QUEUED)
        finally:
            _remove_tree(root)

    def test_recovery_required_for_selected_without_reservation(self) -> None:
        root, head = _setup_project()
        try:
            store, entry = _setup_queued(root)
            entry = store.advance_queue_phase(
                entry.queue_id,
                entry.selection_generation,
                ps.QueuePhase.SELECTED,
            )
            request = _make_tick_request(root, entry)
            result = pr.PortfolioSchedulerRuntime(root).tick(request)
            self.assertEqual(
                result.outcome.kind,
                pr.PortfolioSchedulerTickOutcomeKind.RECOVERY_REQUIRED,
            )
            self.assertEqual(
                result.outcome.recovery_decision.recovery_action,
                RecoveryAction.INVALIDATE_SELECTION,
            )
            self.assertEqual(_event_ids(root), [])
            self.assertEqual(_lease_count(root), 0)
            self.assertEqual(_receipt_ids(root), [])
        finally:
            _remove_tree(root)

if __name__ == "__main__":
    unittest.main()
