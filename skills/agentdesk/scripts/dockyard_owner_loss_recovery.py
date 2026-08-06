"""Compose Dockyard startup recovery into the existing owner-loss owner.

This module does not decide process death from text, exit codes, leases, or
timestamps.  It consumes the durable dispatch receipt and the existing
three-state probes, then delegates the lock-held second check, canonical
``DISPATCH_FAILED`` transition, durable retry reservation, and retry execution
to :class:`workflow_orchestrator.WorkflowOrchestrator`.
"""

from __future__ import annotations

import asyncio
import hashlib
import subprocess
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path

import dispatch_supervisor_evidence as dispatch_evidence
from approval_gate import (
    ApprovalCheckRequest,
    ApprovalGate,
    ApprovalScope,
    ApprovalSubject,
    write_grant,
)
from control_plane_transition import (
    AcknowledgePayload,
    DispatchCAS,
    DispatchFailedPayload,
    DispatchPayload,
    TransitionCAS,
    TransitionEventContext,
    TransitionRequest,
)
from difficulty_assessment_store import DifficultyAssessmentStore
from dispatcher_gateway import AgentCliProvider, DispatchIdentity, DispatchRequest
from portfolio_scheduler_store import AdmissionPlanReservation, PortfolioSchedulerStore
from portfolio_scheduler_worker_handoff_store import (
    PortfolioSchedulerWorkerHandoffStore,
    ScheduledDispatchHandoffPhase,
)
from state_provider import StateProvider
from workflow_orchestrator import (
    BoundedDispatchRetryRequest,
    DispatchCycleRequest,
    DispatchRetryAttempt,
    OwnerLossRecoveryRequest,
    WorkflowOrchestrator,
)

__all__ = [
    "DockyardOwnerLossRecoveryConflictError",
    "DockyardOwnerLossRecoveryError",
    "DockyardOwnerLossRecoveryInputError",
    "DockyardOwnerLossRecoveryOutcome",
    "DockyardOwnerLossRecoveryResult",
    "DockyardOwnerLossRecoveryRuntime",
]


class DockyardOwnerLossRecoveryError(ValueError):
    pass


class DockyardOwnerLossRecoveryInputError(DockyardOwnerLossRecoveryError):
    pass


class DockyardOwnerLossRecoveryConflictError(DockyardOwnerLossRecoveryError):
    pass


class DockyardOwnerLossRecoveryOutcome(str, Enum):
    ALIVE = "ALIVE"
    FAIL_CLOSED = "FAIL_CLOSED"
    RETRY_FINALIZED = "RETRY_FINALIZED"
    RETRY_FAILED = "RETRY_FAILED"
    ATTEMPT_LIMIT_REACHED = "ATTEMPT_LIMIT_REACHED"


def _text(value: object, field: str, maximum: int = 512) -> str:
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or len(value) > maximum
        or any(character in value for character in "\0\r\n")
    ):
        raise DockyardOwnerLossRecoveryInputError("owner_loss:" + field)
    return value


def _positive(value: object, field: str) -> int:
    if type(value) is not int or value < 1:
        raise DockyardOwnerLossRecoveryInputError("owner_loss:" + field)
    return value


def _timestamp(clock: object) -> str:
    value = getattr(clock, "now")()
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
        or value.utcoffset().total_seconds() != 0
    ):
        raise DockyardOwnerLossRecoveryInputError("owner_loss:clock")
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _git_head(root: Path) -> str:
    try:
        result = subprocess.run(
            ("git", "-C", str(root), "rev-parse", "--verify", "HEAD"),
            check=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="strict",
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError, UnicodeError) as exc:
        raise DockyardOwnerLossRecoveryConflictError(
            "owner_loss:git_head"
        ) from exc
    head = result.stdout.strip()
    if len(head) != 40 or any(character not in "0123456789abcdef" for character in head):
        raise DockyardOwnerLossRecoveryConflictError("owner_loss:git_head")
    return head


class _SystemClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)

    def monotonic(self) -> float:
        return time.monotonic()

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


@dataclass(frozen=True, slots=True)
class DockyardOwnerLossRecoveryResult:
    task_id: str
    revision: int
    failed_attempt: int
    failed_dispatch_id: str
    generation_id: str
    outcome: DockyardOwnerLossRecoveryOutcome
    recovery_event_id: str | None
    next_dispatch_id: str | None
    completed_at: str

    def __post_init__(self) -> None:
        _text(self.task_id, "result_task_id", 128)
        _positive(self.revision, "result_revision")
        _positive(self.failed_attempt, "result_failed_attempt")
        _text(self.failed_dispatch_id, "result_failed_dispatch_id", 128)
        _text(self.generation_id, "result_generation_id", 128)
        if type(self.outcome) is not DockyardOwnerLossRecoveryOutcome:
            raise DockyardOwnerLossRecoveryInputError("owner_loss:result_outcome")
        if self.recovery_event_id is not None:
            _text(self.recovery_event_id, "result_recovery_event_id", 128)
        if self.next_dispatch_id is not None:
            _text(self.next_dispatch_id, "result_next_dispatch_id", 128)
        _text(self.completed_at, "result_completed_at", 64)


_LOCKS_GUARD = threading.Lock()
_DISPATCH_LOCKS: dict[tuple[str, str], threading.Lock] = {}


class DockyardOwnerLossRecoveryRuntime:
    """Recover exact DEAD dispatches without becoming a lifecycle owner."""

    def __init__(
        self,
        project_root: Path,
        providers: Mapping[str, AgentCliProvider],
        provider_cli_versions: tuple[tuple[str, str], ...],
        *,
        clock: object | None = None,
    ) -> None:
        if not isinstance(project_root, Path) or not project_root.is_absolute():
            raise DockyardOwnerLossRecoveryInputError("owner_loss:project_root")
        try:
            root = project_root.resolve(strict=True)
        except OSError as exc:
            raise DockyardOwnerLossRecoveryInputError(
                "owner_loss:project_root"
            ) from exc
        if not root.is_dir() or root.is_symlink():
            raise DockyardOwnerLossRecoveryInputError("owner_loss:project_root")
        if not isinstance(providers, Mapping) or not providers:
            raise DockyardOwnerLossRecoveryInputError("owner_loss:providers")
        if type(provider_cli_versions) is not tuple or not provider_cli_versions:
            raise DockyardOwnerLossRecoveryInputError("owner_loss:provider_versions")
        versions: dict[str, str] = {}
        for item in provider_cli_versions:
            if type(item) is not tuple or len(item) != 2:
                raise DockyardOwnerLossRecoveryInputError("owner_loss:provider_versions")
            provider_id = _text(item[0], "provider_id", 128)
            version = _text(item[1], "provider_version", 128)
            if provider_id in versions:
                raise DockyardOwnerLossRecoveryInputError("owner_loss:provider_versions")
            versions[provider_id] = version
        if set(versions) != set(providers):
            raise DockyardOwnerLossRecoveryInputError("owner_loss:provider_versions")
        self.root = root
        self.providers = providers
        self.versions = versions
        self.clock = clock or _SystemClock()
        for method in ("now", "monotonic", "sleep"):
            if not callable(getattr(self.clock, method, None)):
                raise DockyardOwnerLossRecoveryInputError("owner_loss:clock")
        self._thread_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._results: tuple[DockyardOwnerLossRecoveryResult, ...] = ()
        self._error: BaseException | None = None

    def start_background(self) -> None:
        with self._thread_lock:
            if self._thread is not None:
                return
            thread = threading.Thread(
                target=self._background,
                name="dockyard-owner-loss-recovery",
                daemon=True,
            )
            self._thread = thread
            thread.start()

    def wait(self, timeout: float | None = None) -> tuple[DockyardOwnerLossRecoveryResult, ...]:
        with self._thread_lock:
            thread = self._thread
        if thread is None:
            return self._results
        thread.join(timeout)
        if thread.is_alive():
            raise TimeoutError("Dockyard owner-loss recovery did not finish")
        if self._error is not None:
            raise DockyardOwnerLossRecoveryConflictError(
                "owner_loss:background_failed"
            ) from self._error
        return self._results

    def results(self) -> tuple[DockyardOwnerLossRecoveryResult, ...]:
        return self._results

    def _background(self) -> None:
        try:
            self._results = self.recover_pending()
        except BaseException as exc:
            self._error = exc

    def recover_pending(self) -> tuple[DockyardOwnerLossRecoveryResult, ...]:
        snapshot = StateProvider(self.root).snapshot()
        identities = tuple(
            (
                task.task_id,
                task.revision,
                task.attempt,
                task.current_dispatch.dispatch_id,
            )
            for task in snapshot.tasks
            if task.state in {"dispatched", "in_progress"}
            and task.attempt is not None
            and task.current_dispatch is not None
        )
        results: list[DockyardOwnerLossRecoveryResult] = []
        for task_id, revision, attempt, dispatch_id in identities:
            result = self._recover_identity(task_id, revision, attempt, dispatch_id)
            if result is not None:
                results.append(result)
        return tuple(results)

    def _recover_identity(
        self,
        task_id: str,
        revision: int,
        failed_attempt: int,
        failed_dispatch_id: str,
    ) -> DockyardOwnerLossRecoveryResult | None:
        key = (str(self.root).casefold(), failed_dispatch_id)
        with _LOCKS_GUARD:
            lock = _DISPATCH_LOCKS.setdefault(key, threading.Lock())
        with lock:
            return self._recover_identity_locked(
                task_id, revision, failed_attempt, failed_dispatch_id
            )

    def _recover_identity_locked(
        self,
        task_id: str,
        revision: int,
        failed_attempt: int,
        failed_dispatch_id: str,
    ) -> DockyardOwnerLossRecoveryResult | None:
        snapshot = StateProvider(self.root).snapshot()
        task = next((item for item in snapshot.tasks if item.task_id == task_id), None)
        if task is None:
            raise DockyardOwnerLossRecoveryConflictError("owner_loss:task_missing")
        replay_event = self._recovery_event(snapshot, task_id, revision, failed_attempt, failed_dispatch_id)
        if replay_event is not None and task.revision == revision and (
            (task.state == "ready" and task.current_dispatch is None)
            or (task.attempt is not None and task.attempt > failed_attempt)
        ):
            return self._replay_result(
                task_id, revision, failed_attempt, failed_dispatch_id,
                replay_event.event_id, replay_event.occurred_at,
            )
        if (
            task.revision != revision
            or task.attempt != failed_attempt
            or task.state not in {"dispatched", "in_progress"}
            or task.current_dispatch is None
            or task.current_dispatch.dispatch_id != failed_dispatch_id
        ):
            return None
        receipt = dispatch_evidence.read_dispatch_receipt(self.root, failed_dispatch_id)
        tombstone = dispatch_evidence.read_dispatch_tombstone(self.root, failed_dispatch_id)
        if receipt is None:
            raise DockyardOwnerLossRecoveryConflictError("owner_loss:receipt_missing")
        if (
            receipt.task_id != task_id
            or receipt.revision != revision
            or receipt.attempt != failed_attempt
            or receipt.dispatch_id != failed_dispatch_id
            or receipt.phase not in {
                dispatch_evidence.DispatchReceiptPhase.SUPERVISOR_READY.value,
                dispatch_evidence.DispatchReceiptPhase.WORKER_STARTED.value,
            }
        ):
            raise DockyardOwnerLossRecoveryConflictError("owner_loss:receipt_identity")
        if tombstone is not None:
            return None
        creator = dispatch_evidence.probe_process(
            receipt.creator_pid,
            receipt.creator_creation_time,
            receipt.boot_id,
        )
        tree = dispatch_evidence.probe_dispatch_process_tree(receipt)
        completed_at = _timestamp(self.clock)
        if creator is dispatch_evidence.ProcessLiveness.ALIVE or tree is dispatch_evidence.ProcessLiveness.ALIVE:
            return DockyardOwnerLossRecoveryResult(
                task_id, revision, failed_attempt, failed_dispatch_id,
                receipt.generation_id, DockyardOwnerLossRecoveryOutcome.ALIVE,
                None, None, completed_at,
            )
        if creator is not dispatch_evidence.ProcessLiveness.DEAD or tree is not dispatch_evidence.ProcessLiveness.DEAD:
            return DockyardOwnerLossRecoveryResult(
                task_id, revision, failed_attempt, failed_dispatch_id,
                receipt.generation_id,
                DockyardOwnerLossRecoveryOutcome.FAIL_CLOSED,
                None, None, completed_at,
            )

        token = hashlib.sha256(
            ("dockyard-owner-loss\0" + failed_dispatch_id + "\0" + receipt.generation_id).encode("utf-8")
        ).hexdigest()[:32]
        recovery_event_id = "EVT-OWNER-LOSS-" + token
        evidence_refs = (
            ".agentdesk/runtime/dispatch-supervisor/" + failed_dispatch_id + ".receipt.yaml",
        )
        context = TransitionEventContext(None, evidence_refs, ())
        retry_plan = None
        next_dispatch_id = None
        if failed_attempt < 3:
            plan = self._source_plan(task_id, revision, failed_attempt, failed_dispatch_id)
            next_dispatch_id = "DSP-OWNER-RETRY-" + token
            self._ensure_automatic_approval(
                task_id, revision, failed_attempt + 1, next_dispatch_id,
                snapshot.pm_lease_epoch, receipt,
            )
            retry_plan = self._retry_plan(
                plan, failed_attempt, failed_dispatch_id, next_dispatch_id,
                token, context,
            )
        request = OwnerLossRecoveryRequest(
            task_id,
            revision,
            failed_attempt,
            failed_dispatch_id,
            receipt.generation_id,
            recovery_event_id,
            context,
            "worker_failed",
            evidence_refs,
            retry_plan,
        )
        orchestrator = WorkflowOrchestrator(self.root, self.clock)
        try:
            recovered = asyncio.run(
                orchestrator.recover_owner_lost_dispatch(
                    request,
                    self.providers if retry_plan is not None else None,
                )
            )
        except BaseException as exc:
            if retry_plan is None or next_dispatch_id is None:
                raise
            retry_receipt = dispatch_evidence.read_retry_receipt(
                self.root, next_dispatch_id
            )
            retry_tombstone = dispatch_evidence.read_dispatch_tombstone(
                self.root, next_dispatch_id
            )
            final = StateProvider(self.root).snapshot()
            final_task = next((item for item in final.tasks if item.task_id == task_id), None)
            if (
                retry_receipt is None
                or retry_receipt.phase != "RETRY_FINALIZED"
                or retry_tombstone is None
                or retry_tombstone.failure_kind is None
                or final_task is None
                or final_task.state != "ready"
                or final_task.current_dispatch is not None
                or final_task.attempt != failed_attempt + 1
            ):
                raise DockyardOwnerLossRecoveryConflictError(
                    "owner_loss:retry_failure_incomplete"
                ) from exc
            return DockyardOwnerLossRecoveryResult(
                task_id, revision, failed_attempt, failed_dispatch_id,
                receipt.generation_id,
                DockyardOwnerLossRecoveryOutcome.RETRY_FAILED,
                recovery_event_id, next_dispatch_id,
                retry_receipt.finalized_at or retry_tombstone.finalized_at,
            )
        if retry_plan is None:
            if recovered.retry_result is not None:
                raise DockyardOwnerLossRecoveryConflictError(
                    "owner_loss:attempt_limit_result"
                )
            outcome = DockyardOwnerLossRecoveryOutcome.ATTEMPT_LIMIT_REACHED
            completed_at = recovered.recovery_transition.occurred_at
        else:
            if recovered.retry_result is None:
                raise DockyardOwnerLossRecoveryConflictError(
                    "owner_loss:retry_result_missing"
                )
            retry_receipt = dispatch_evidence.read_retry_receipt(self.root, next_dispatch_id)
            if retry_receipt is None or retry_receipt.phase != "RETRY_FINALIZED":
                raise DockyardOwnerLossRecoveryConflictError(
                    "owner_loss:retry_receipt_incomplete"
                )
            outcome = DockyardOwnerLossRecoveryOutcome.RETRY_FINALIZED
            if retry_receipt.finalized_at is None:
                raise DockyardOwnerLossRecoveryConflictError(
                    "owner_loss:retry_finalized_at"
                )
            completed_at = retry_receipt.finalized_at
        return DockyardOwnerLossRecoveryResult(
            task_id, revision, failed_attempt, failed_dispatch_id,
            receipt.generation_id, outcome, recovery_event_id,
            next_dispatch_id, completed_at,
        )

    def _recovery_event(self, snapshot, task_id, revision, attempt, dispatch_id):
        matches = tuple(
            event for event in snapshot.events
            if event.event_type == "DISPATCH_FAILED"
            and event.task_id == task_id
            and event.revision == revision
            and event.attempt == attempt
            and event.dispatch_id == dispatch_id
        )
        if len(matches) > 1:
            raise DockyardOwnerLossRecoveryConflictError("owner_loss:recovery_duplicate")
        return matches[0] if matches else None

    def _replay_result(
        self, task_id, revision, attempt, dispatch_id, event_id, occurred_at
    ):
        receipt = dispatch_evidence.read_dispatch_receipt(self.root, dispatch_id)
        if receipt is None:
            raise DockyardOwnerLossRecoveryConflictError("owner_loss:replay_receipt")
        token = hashlib.sha256(
            ("dockyard-owner-loss\0" + dispatch_id + "\0" + receipt.generation_id).encode("utf-8")
        ).hexdigest()[:32]
        expected_event_id = "EVT-OWNER-LOSS-" + token
        if event_id != expected_event_id:
            raise DockyardOwnerLossRecoveryConflictError("owner_loss:replay_event")
        next_dispatch_id = "DSP-OWNER-RETRY-" + token if attempt < 3 else None
        retry = None if next_dispatch_id is None else dispatch_evidence.read_retry_receipt(
            self.root, next_dispatch_id
        )
        outcome = (
            DockyardOwnerLossRecoveryOutcome.ATTEMPT_LIMIT_REACHED
            if next_dispatch_id is None
            else DockyardOwnerLossRecoveryOutcome.RETRY_FINALIZED
            if retry is not None and retry.phase == "RETRY_FINALIZED"
            else DockyardOwnerLossRecoveryOutcome.RETRY_FAILED
        )
        completed_at = (
            occurred_at
            if retry is None or retry.finalized_at is None
            else retry.finalized_at
        )
        return DockyardOwnerLossRecoveryResult(
            task_id, revision, attempt, dispatch_id, receipt.generation_id,
            outcome, event_id, next_dispatch_id, completed_at,
        )

    def _source_plan(self, task_id, revision, attempt, dispatch_id) -> AdmissionPlanReservation:
        plans = tuple(
            plan for plan in PortfolioSchedulerStore(self.root).enumerate_validated_admission_plans()
            if plan.task_id == task_id
            and plan.revision == revision
            and plan.new_attempt == attempt
            and plan.dispatch_id == dispatch_id
        )
        if len(plans) != 1:
            raise DockyardOwnerLossRecoveryConflictError("owner_loss:plan_identity")
        plan = plans[0]
        snapshot = StateProvider(self.root).snapshot()
        task = next((item for item in snapshot.tasks if item.task_id == task_id), None)
        dispatched = tuple(
            event for event in snapshot.events
            if event.event_type == "TASK_DISPATCHED"
            and event.event_id == plan.event_id
            and event.task_id == task_id
            and event.revision == revision
            and event.attempt == attempt
            and event.dispatch_id == dispatch_id
        )
        outboxes = tuple(
            item for item in snapshot.outbox
            if item.message_id == plan.outbox_message_id
            and item.event_id == plan.event_id
            and item.task_id == task_id
            and item.revision == revision
            and item.attempt == attempt
            and item.dispatch_id == dispatch_id
        )
        if (
            task is None
            or task.current_dispatch is None
            or task.current_dispatch.dispatch_id != dispatch_id
            or task.task_card_path != plan.task_card_path
            or task.task_card_commit != plan.task_card_commit
            or task.current_dispatch.base_commit != plan.base_commit
            or task.current_dispatch.branch != plan.branch
            or len(dispatched) != 1
            or len(outboxes) != 1
        ):
            raise DockyardOwnerLossRecoveryConflictError(
                "owner_loss:canonical_plan_binding"
            )
        outbox = outboxes[0]
        if (
            outbox.destination_role_id != plan.role_id
            or outbox.payload.task_path != plan.task_card_path
            or outbox.payload.task_card_commit != plan.task_card_commit
            or outbox.payload.base_commit != plan.base_commit
            or outbox.payload.branch != plan.branch
            or outbox.payload.report_path != plan.report_path
            or outbox.model_selection.model_binding_id
            != plan.model_selection.model_binding_id
            or outbox.model_selection.selected_model_provider
            != plan.model_selection.selected_model_provider
            or outbox.model_selection.selected_model_id
            != plan.model_selection.selected_model_id
        ):
            raise DockyardOwnerLossRecoveryConflictError(
                "owner_loss:outbox_plan_binding"
            )
        handoff = PortfolioSchedulerWorkerHandoffStore(self.root).read_handoff(dispatch_id)
        if (
            handoff.task_id != task_id
            or handoff.revision != revision
            or handoff.attempt != attempt
            or handoff.dispatch_id != dispatch_id
            or handoff.plan_digest != plan.content_digest
            or handoff.receipt_id != plan.receipt_id
            or handoff.phase not in {
                ScheduledDispatchHandoffPhase.SUPERVISOR_STARTED,
                ScheduledDispatchHandoffPhase.WORKER_STARTED,
                ScheduledDispatchHandoffPhase.ACKNOWLEDGED,
            }
        ):
            raise DockyardOwnerLossRecoveryConflictError("owner_loss:handoff_identity")
        return plan

    def _ensure_automatic_approval(
        self,
        task_id: str,
        revision: int,
        attempt: int,
        dispatch_id: str,
        lease_epoch: int,
        failed_receipt,
    ) -> None:
        creator = dispatch_evidence.probe_process(
            failed_receipt.creator_pid,
            failed_receipt.creator_creation_time,
            failed_receipt.boot_id,
        )
        tree = dispatch_evidence.probe_dispatch_process_tree(failed_receipt)
        if (
            creator is not dispatch_evidence.ProcessLiveness.DEAD
            or tree is not dispatch_evidence.ProcessLiveness.DEAD
        ):
            raise DockyardOwnerLossRecoveryConflictError(
                "owner_loss:approval_liveness_changed"
            )
        current = _git_head(self.root)
        gate = ApprovalGate(self.root)
        source_subject = ApprovalSubject(
            task_id,
            revision,
            failed_receipt.attempt,
            failed_receipt.dispatch_id,
            None,
        )
        source_checked = gate.check(
            ApprovalCheckRequest(ApprovalScope.DISPATCH, source_subject, current),
            getattr(self.clock, "now")(),
        )
        if not source_checked.passed or source_checked.matched_evidence is None:
            raise DockyardOwnerLossRecoveryConflictError(
                "owner_loss:approved_plan_evidence"
            )
        subject = ApprovalSubject(task_id, revision, attempt, dispatch_id, None)
        checked = gate.check(
            ApprovalCheckRequest(ApprovalScope.DISPATCH, subject, current),
            getattr(self.clock, "now")(),
        )
        token = hashlib.sha256(
            ("dockyard-owner-loss-approval\0" + dispatch_id).encode("utf-8")
        ).hexdigest()[:32]
        approval_id = "APR-OWNER-LOSS-" + token
        event_id = "EVT-APR-OWNER-LOSS-" + token
        reason = "Dockyard approved-plan owner-loss automatic retry"
        if checked.passed:
            evidence = checked.matched_evidence
            if (
                evidence is None
                or evidence.approval_id != approval_id
                or evidence.event_id != event_id
                or evidence.subject != subject
                or evidence.reason != reason
            ):
                raise DockyardOwnerLossRecoveryConflictError(
                    "owner_loss:approval_divergence"
                )
            return
        if checked.failure_code not in {"not_found", "wrong_scope", "wrong_subject"}:
            raise DockyardOwnerLossRecoveryConflictError(
                "owner_loss:approval_unavailable"
            )
        try:
            write_grant(
                self.root,
                approval_id,
                event_id,
                ApprovalScope.DISPATCH,
                subject,
                lease_epoch,
                getattr(self.clock, "now")(),
                reason,
                None,
                current,
            )
        except Exception as exc:
            raise DockyardOwnerLossRecoveryConflictError(
                "owner_loss:approval_write"
            ) from exc

    def _retry_plan(
        self,
        plan: AdmissionPlanReservation,
        failed_attempt: int,
        failed_dispatch_id: str,
        next_dispatch_id: str,
        token: str,
        context: TransitionEventContext,
    ) -> BoundedDispatchRetryRequest:
        provider_id = plan.model_selection.selected_model_provider
        provider_version = self.versions.get(provider_id)
        if provider_id not in self.providers or provider_version is None:
            raise DockyardOwnerLossRecoveryConflictError("owner_loss:provider_not_ready")
        worktree = Path(plan.canonical_worktree).resolve(strict=True)
        task_card = (worktree / plan.task_card_path).resolve(strict=True)
        try:
            task_card.relative_to(worktree)
            prompt = task_card.read_text(encoding="utf-8", errors="strict")
        except (OSError, UnicodeError, ValueError) as exc:
            raise DockyardOwnerLossRecoveryConflictError(
                "owner_loss:task_card"
            ) from exc
        assessment = DifficultyAssessmentStore().read(
            self.root, plan.task_id, plan.revision, plan.assessment_id
        ).assessment
        next_attempt = failed_attempt + 1
        head = _git_head(self.root)
        dispatch_event_id = "EVT-OWNER-RETRY-DISPATCH-" + token
        outbox_id = "MSG-OWNER-RETRY-" + token
        dispatch_request = DispatchRequest(
            DispatchIdentity(plan.task_id, plan.revision, next_attempt, next_dispatch_id),
            worktree,
            prompt,
            plan.model_selection,
            1800,
        )
        dispatch_transition = TransitionRequest(
            TransitionCAS(plan.task_id, plan.revision, "ready", head),
            None,
            dispatch_event_id,
            "TASK_DISPATCHED",
            DispatchPayload(
                next_dispatch_id,
                plan.role_id,
                plan.model_selection,
                plan.task_card_path,
                plan.task_card_commit,
                plan.base_commit,
                plan.branch,
                plan.report_path,
                outbox_id,
                next_attempt,
            ),
            context,
        )
        acknowledge = TransitionRequest(
            TransitionCAS(plan.task_id, plan.revision, "dispatched", head),
            DispatchCAS(next_dispatch_id, next_attempt),
            "EVT-OWNER-RETRY-ACK-" + token,
            "DISPATCH_ACKNOWLEDGED",
            AcknowledgePayload(),
            context,
        )
        cycle = DispatchCycleRequest(
            dispatch_request,
            dispatch_transition,
            acknowledge,
            "EVT-OWNER-RETRY-DELIVERY-" + token,
            context,
            provider_version,
            plan.worker_kind,
            assessment.selected_difficulty,
            plan.holder_instance_id,
        )
        failure = TransitionRequest(
            TransitionCAS(plan.task_id, plan.revision, "in_progress", head),
            DispatchCAS(next_dispatch_id, next_attempt),
            "EVT-OWNER-RETRY-FAILED-" + token,
            "DISPATCH_FAILED",
            DispatchFailedPayload("worker_failed"),
            context,
        )
        return BoundedDispatchRetryRequest((DispatchRetryAttempt(cycle, failure),))
