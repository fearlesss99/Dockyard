"""Safe deterministic read projection for the Dockyard visual control surface.

This module consumes already validated, frozen AgentDesk evidence.  It performs
no filesystem, Git, process, provider, model, API, network, or canonical-state
operation.  In particular it never exposes ``StateSnapshot.project_root``, raw
acceptance bodies, MAD archive paths, outbox payload paths, prompt/output bytes,
or runtime routing identities.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from state_provider import AcceptanceEntry, StateSnapshot, TaskEntry


SCHEMA_VERSION = "agentdesk.dockyard-projection/v1"
MAX_TEXT = 512


class DockyardProjectionError(ValueError):
    """Base error for safe projection failures."""


class DockyardProjectionInputError(DockyardProjectionError):
    """Raised when caller-supplied typed evidence is invalid."""


class DockyardProjectionConflictError(DockyardProjectionError):
    """Raised when independently supplied evidence diverges."""


class DockyardProviderHealth(str, Enum):
    READY = "READY"
    DEGRADED = "DEGRADED"
    NOT_READY = "NOT_READY"
    UNKNOWN = "UNKNOWN"


_RUN_PHASES = frozenset(
    {
        "RESERVED",
        "SUPERVISOR_READY",
        "WORKER_STARTED",
        "ACKNOWLEDGED",
        "FINALIZING",
        "FINALIZED",
        "FAILED",
        "RETURNED",
        "RECOVERY_REQUIRED",
    }
)
_PROCESS_STATES = frozenset({"ALIVE", "DEAD", "UNKNOWN", "NOT_APPLICABLE"})
_HEARTBEAT_STATES = frozenset({"ACTIVE", "DONE", "FAILED", "UNKNOWN"})
_LEASE_STATES = frozenset({"HELD", "RELEASED", "UNKNOWN"})
_WORKTREE_STATES = frozenset(
    {"RESERVED", "CREATING", "READY", "RELEASING", "RELEASED", "UNKNOWN"}
)


def _fail(error_type: type[DockyardProjectionError], code: str) -> None:
    raise error_type(code)


def _text(value: object, code: str, *, maximum: int = MAX_TEXT) -> str:
    if type(value) is not str:
        _fail(DockyardProjectionInputError, code)
    if not value or len(value) > maximum:
        _fail(DockyardProjectionInputError, code)
    if value != value.strip() or any(ord(char) < 0x20 for char in value):
        _fail(DockyardProjectionInputError, code)
    return value


def _optional_text(value: object, code: str, *, maximum: int = MAX_TEXT) -> str | None:
    if value is None:
        return None
    return _text(value, code, maximum=maximum)


def _positive(value: object, code: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        _fail(DockyardProjectionInputError, code)
    return value


def _timestamp(value: object, code: str) -> str:
    text = _text(value, code, maximum=64)
    if not text.endswith("Z"):
        _fail(DockyardProjectionInputError, code)
    try:
        parsed = datetime.fromisoformat(text[:-1] + "+00:00")
    except ValueError:
        _fail(DockyardProjectionInputError, code)
    if parsed.utcoffset() is None or parsed.utcoffset().total_seconds() != 0:
        _fail(DockyardProjectionInputError, code)
    return text


def _sha40(value: object, code: str) -> str:
    text = _text(value, code, maximum=40)
    if len(text) != 40 or any(char not in "0123456789abcdef" for char in text):
        _fail(DockyardProjectionInputError, code)
    return text


def _choice(value: object, choices: frozenset[str], code: str) -> str:
    text = _text(value, code, maximum=64)
    if text not in choices:
        _fail(DockyardProjectionInputError, code)
    return text


def _segments(values: tuple[str, ...]) -> bytes:
    return "".join(f"{len(value)}:{value}" for value in values).encode("utf-8")


def _digest(values: tuple[str, ...]) -> str:
    return hashlib.sha256(_segments(values)).hexdigest()


def _termination_event_id(dispatch_id: str) -> str:
    token = hashlib.sha256(("CANCEL\0" + dispatch_id).encode("utf-8")).hexdigest()[:24]
    return f"EVT-CANCEL-{token}"


def _bool_text(value: bool) -> str:
    return "true" if value else "false"


@dataclass(frozen=True, slots=True)
class DockyardRunEvidence:
    task_id: str
    revision: int
    attempt: int
    dispatch_id: str
    phase: str
    supervisor_state: str
    worker_state: str
    heartbeat_state: str
    lease_state: str
    started_at: str | None
    updated_at: str
    retry_count: int
    generation_id: str | None = None
    termination_available: bool = False
    termination_event_id: str | None = None
    retry_available: bool = False
    retry_event_id: str | None = None
    retry_failure_kind: str | None = None
    retry_provider_id: str | None = None
    retry_model_id: str | None = None

    def __post_init__(self) -> None:
        _text(self.task_id, "run_task_id")
        _positive(self.revision, "run_revision", minimum=1)
        _positive(self.attempt, "run_attempt", minimum=1)
        _text(self.dispatch_id, "run_dispatch_id")
        _choice(self.phase, _RUN_PHASES, "run_phase")
        _choice(self.supervisor_state, _PROCESS_STATES, "run_supervisor_state")
        _choice(self.worker_state, _PROCESS_STATES, "run_worker_state")
        _choice(self.heartbeat_state, _HEARTBEAT_STATES, "run_heartbeat_state")
        _choice(self.lease_state, _LEASE_STATES, "run_lease_state")
        if self.started_at is not None:
            _timestamp(self.started_at, "run_started_at")
        _timestamp(self.updated_at, "run_updated_at")
        _positive(self.retry_count, "run_retry_count")
        _optional_text(self.generation_id, "run_generation_id")
        if type(self.termination_available) is not bool:
            _fail(DockyardProjectionInputError, "run_termination_available")
        _optional_text(self.termination_event_id, "run_termination_event_id")
        if self.termination_available and (
            self.generation_id is None or self.termination_event_id is None
        ):
            _fail(DockyardProjectionInputError, "run_termination_identity")
        if not self.termination_available and self.termination_event_id is not None:
            _fail(DockyardProjectionInputError, "run_termination_identity")
        if type(self.retry_available) is not bool:
            _fail(DockyardProjectionInputError, "run_retry_available")
        _optional_text(self.retry_event_id, "run_retry_event_id")
        _optional_text(self.retry_failure_kind, "run_retry_failure_kind")
        _optional_text(self.retry_provider_id, "run_retry_provider_id")
        _optional_text(self.retry_model_id, "run_retry_model_id")
        if self.retry_available and (self.retry_event_id is None or self.retry_failure_kind is None):
            _fail(DockyardProjectionInputError, "run_retry_identity")
        if not self.retry_available and any(
            value is not None
            for value in (
                self.retry_event_id,
                self.retry_failure_kind,
                self.retry_provider_id,
                self.retry_model_id,
            )
        ):
            _fail(DockyardProjectionInputError, "run_retry_identity")


@dataclass(frozen=True, slots=True)
class DockyardProviderEvidence:
    provider_id: str
    model_id: str
    health: DockyardProviderHealth
    reason_code: str | None
    executable_version: str | None
    decoder_version: str | None
    binding_id: str | None
    required_capabilities: tuple[str, ...]
    checked_at: str

    def __post_init__(self) -> None:
        _text(self.provider_id, "provider_id")
        _text(self.model_id, "provider_model_id")
        if type(self.health) is not DockyardProviderHealth:
            _fail(DockyardProjectionInputError, "provider_health")
        _optional_text(self.reason_code, "provider_reason_code")
        _optional_text(self.executable_version, "provider_executable_version")
        _optional_text(self.decoder_version, "provider_decoder_version")
        _optional_text(self.binding_id, "provider_binding_id")
        if type(self.required_capabilities) is not tuple:
            _fail(DockyardProjectionInputError, "provider_capabilities")
        if tuple(sorted(self.required_capabilities)) != self.required_capabilities:
            _fail(DockyardProjectionInputError, "provider_capability_order")
        if len(set(self.required_capabilities)) != len(self.required_capabilities):
            _fail(DockyardProjectionInputError, "provider_capability_duplicate")
        for capability in self.required_capabilities:
            _text(capability, "provider_capability")
        _timestamp(self.checked_at, "provider_checked_at")


@dataclass(frozen=True, slots=True)
class DockyardWorktreeEvidence:
    task_id: str
    revision: int
    attempt: int
    worktree_id: str
    lifecycle_state: str
    branch: str
    head_commit: str
    dirty: bool
    checked_at: str

    def __post_init__(self) -> None:
        _text(self.task_id, "worktree_task_id")
        _positive(self.revision, "worktree_revision", minimum=1)
        _positive(self.attempt, "worktree_attempt", minimum=1)
        _text(self.worktree_id, "worktree_id")
        _choice(self.lifecycle_state, _WORKTREE_STATES, "worktree_state")
        _text(self.branch, "worktree_branch")
        _sha40(self.head_commit, "worktree_head")
        if type(self.dirty) is not bool:
            _fail(DockyardProjectionInputError, "worktree_dirty")
        _timestamp(self.checked_at, "worktree_checked_at")


@dataclass(frozen=True, slots=True)
class DockyardProjectionRequest:
    snapshot: StateSnapshot
    run_evidence: tuple[DockyardRunEvidence, ...]
    provider_evidence: tuple[DockyardProviderEvidence, ...]
    worktree_evidence: tuple[DockyardWorktreeEvidence, ...]
    generated_at: str

    def __post_init__(self) -> None:
        if type(self.snapshot) is not StateSnapshot:
            _fail(DockyardProjectionInputError, "snapshot_type")
        if type(self.run_evidence) is not tuple:
            _fail(DockyardProjectionInputError, "run_evidence_type")
        if type(self.provider_evidence) is not tuple:
            _fail(DockyardProjectionInputError, "provider_evidence_type")
        if type(self.worktree_evidence) is not tuple:
            _fail(DockyardProjectionInputError, "worktree_evidence_type")
        if any(type(item) is not DockyardRunEvidence for item in self.run_evidence):
            _fail(DockyardProjectionInputError, "run_evidence_item")
        if any(type(item) is not DockyardProviderEvidence for item in self.provider_evidence):
            _fail(DockyardProjectionInputError, "provider_evidence_item")
        if any(type(item) is not DockyardWorktreeEvidence for item in self.worktree_evidence):
            _fail(DockyardProjectionInputError, "worktree_evidence_item")
        _timestamp(self.generated_at, "generated_at")


@dataclass(frozen=True, slots=True)
class DockyardStateCount:
    state: str
    count: int


@dataclass(frozen=True, slots=True)
class DockyardTaskSummary:
    task_id: str
    revision: int
    state: str
    attempt: int | None
    role_id: str | None
    provider_id: str | None
    model_id: str | None
    deliberation_tier: str | None
    delivery_state: str | None
    integration_state: str | None
    updated_at: str
    blocked_kind: str | None
    has_report: bool
    content_digest: str


@dataclass(frozen=True, slots=True)
class DockyardTaskDetail:
    summary: DockyardTaskSummary
    task_card_commit: str
    current_dispatch_id: str | None
    branch: str | None
    implementation_commit: str | None
    report_commit: str | None
    accepted_commit: str | None
    integrated_commit: str | None
    approval_count: int
    content_digest: str


@dataclass(frozen=True, slots=True)
class DockyardRunSummary:
    task_id: str
    revision: int
    attempt: int
    dispatch_id: str
    phase: str
    supervisor_state: str
    worker_state: str
    heartbeat_state: str
    lease_state: str
    started_at: str | None
    updated_at: str
    retry_count: int
    content_digest: str
    generation_id: str | None = None
    termination_available: bool = False
    termination_event_id: str | None = None
    retry_available: bool = False
    retry_event_id: str | None = None
    retry_failure_kind: str | None = None
    retry_provider_id: str | None = None
    retry_model_id: str | None = None


@dataclass(frozen=True, slots=True)
class DockyardApprovalSummary:
    task_id: str
    revision: int
    granted_approval_count: int
    latest_acceptance_decision: str | None
    owner_approval_gate: str | None
    pending_user_decision: bool
    updated_at: str
    content_digest: str


@dataclass(frozen=True, slots=True)
class DockyardProviderSummary:
    provider_id: str
    model_id: str
    health: DockyardProviderHealth
    reason_code: str | None
    executable_version: str | None
    decoder_version: str | None
    binding_id: str | None
    required_capabilities: tuple[str, ...]
    checked_at: str
    content_digest: str


@dataclass(frozen=True, slots=True)
class DockyardWorktreeSummary:
    task_id: str
    revision: int
    attempt: int
    worktree_id: str
    lifecycle_state: str
    branch: str
    head_commit: str
    dirty: bool
    checked_at: str
    content_digest: str


@dataclass(frozen=True, slots=True)
class DockyardProjectHealth:
    snapshot_state: str
    pm_state: str
    scheduler_state: str
    runner_state: str
    provider_state: DockyardProviderHealth
    issue_codes: tuple[str, ...]
    checked_at: str
    content_digest: str


@dataclass(frozen=True, slots=True)
class DockyardOverview:
    schema_version: str
    project_id: str
    adoption_level: str
    snapshot_commit: str
    updated_at: str
    generated_at: str
    pm_mode: str
    state_counts: tuple[DockyardStateCount, ...]
    active_task_ids: tuple[str, ...]
    pending_approval_task_ids: tuple[str, ...]
    recent_delivery_task_ids: tuple[str, ...]
    provider_summaries: tuple[DockyardProviderSummary, ...]
    health: DockyardProjectHealth
    content_digest: str


@dataclass(frozen=True, slots=True)
class DockyardProjectionBundle:
    overview: DockyardOverview
    tasks: tuple[DockyardTaskSummary, ...]
    task_details: tuple[DockyardTaskDetail, ...]
    runs: tuple[DockyardRunSummary, ...]
    approvals: tuple[DockyardApprovalSummary, ...]
    providers: tuple[DockyardProviderSummary, ...]
    worktrees: tuple[DockyardWorktreeSummary, ...]
    content_digest: str


def _task_values(task: TaskEntry) -> tuple[str, ...]:
    dispatch = task.current_dispatch
    model = dispatch.model_selection if dispatch is not None else None
    return (
        task.task_id,
        str(task.revision),
        task.state,
        "" if task.attempt is None else str(task.attempt),
        "" if dispatch is None else dispatch.role_id,
        "" if model is None else model.selected_model_provider,
        "" if model is None else model.selected_model_id,
        "" if model is None else model.selected_deliberation_tier,
        "" if task.delivery_state is None else task.delivery_state,
        "" if task.integration_state is None else task.integration_state,
        task.timestamps.updated_at,
        "" if task.blocked_kind is None else task.blocked_kind,
        _bool_text(task.report_commit is not None),
    )


def _task_summary(task: TaskEntry) -> DockyardTaskSummary:
    dispatch = task.current_dispatch
    model = dispatch.model_selection if dispatch is not None else None
    values = _task_values(task)
    return DockyardTaskSummary(
        task_id=task.task_id,
        revision=task.revision,
        state=task.state,
        attempt=task.attempt,
        role_id=None if dispatch is None else dispatch.role_id,
        provider_id=None if model is None else model.selected_model_provider,
        model_id=None if model is None else model.selected_model_id,
        deliberation_tier=None if model is None else model.selected_deliberation_tier,
        delivery_state=task.delivery_state,
        integration_state=task.integration_state,
        updated_at=task.timestamps.updated_at,
        blocked_kind=task.blocked_kind,
        has_report=task.report_commit is not None,
        content_digest=_digest(values),
    )


def _task_detail(task: TaskEntry, summary: DockyardTaskSummary) -> DockyardTaskDetail:
    dispatch = task.current_dispatch
    values = (
        summary.content_digest,
        task.task_card_commit,
        "" if dispatch is None else dispatch.dispatch_id,
        "" if dispatch is None else dispatch.branch,
        "" if task.implementation_commit is None else task.implementation_commit,
        "" if task.report_commit is None else task.report_commit,
        "" if task.accepted_commit is None else task.accepted_commit,
        "" if task.integrated_commit is None else task.integrated_commit,
        str(len(task.granted_approval_ids or ())),
    )
    return DockyardTaskDetail(
        summary=summary,
        task_card_commit=task.task_card_commit,
        current_dispatch_id=None if dispatch is None else dispatch.dispatch_id,
        branch=None if dispatch is None else dispatch.branch,
        implementation_commit=task.implementation_commit,
        report_commit=task.report_commit,
        accepted_commit=task.accepted_commit,
        integrated_commit=task.integrated_commit,
        approval_count=len(task.granted_approval_ids or ()),
        content_digest=_digest(values),
    )


def _run_summary(evidence: DockyardRunEvidence) -> DockyardRunSummary:
    values = tuple(
        "" if value is None else str(value)
        for value in (
            evidence.task_id,
            evidence.revision,
            evidence.attempt,
            evidence.dispatch_id,
            evidence.phase,
            evidence.supervisor_state,
            evidence.worker_state,
            evidence.heartbeat_state,
            evidence.lease_state,
            evidence.started_at,
            evidence.updated_at,
            evidence.retry_count,
            evidence.generation_id,
            _bool_text(evidence.termination_available),
            evidence.termination_event_id,
            _bool_text(evidence.retry_available),
            evidence.retry_event_id,
            evidence.retry_failure_kind,
            evidence.retry_provider_id,
            evidence.retry_model_id,
        )
    )
    return DockyardRunSummary(task_id=evidence.task_id, revision=evidence.revision,
        attempt=evidence.attempt, dispatch_id=evidence.dispatch_id,
        phase=evidence.phase, supervisor_state=evidence.supervisor_state,
        worker_state=evidence.worker_state, heartbeat_state=evidence.heartbeat_state,
        lease_state=evidence.lease_state, started_at=evidence.started_at,
        updated_at=evidence.updated_at, retry_count=evidence.retry_count,
        generation_id=evidence.generation_id,
        termination_available=evidence.termination_available,
        termination_event_id=evidence.termination_event_id,
        retry_available=evidence.retry_available,
        retry_event_id=evidence.retry_event_id,
        retry_failure_kind=evidence.retry_failure_kind,
        retry_provider_id=evidence.retry_provider_id,
        retry_model_id=evidence.retry_model_id,
        content_digest=_digest(values))


def _provider_summary(evidence: DockyardProviderEvidence) -> DockyardProviderSummary:
    values = (
        evidence.provider_id,
        evidence.model_id,
        evidence.health.value,
        "" if evidence.reason_code is None else evidence.reason_code,
        "" if evidence.executable_version is None else evidence.executable_version,
        "" if evidence.decoder_version is None else evidence.decoder_version,
        "" if evidence.binding_id is None else evidence.binding_id,
        *evidence.required_capabilities,
        evidence.checked_at,
    )
    return DockyardProviderSummary(
        provider_id=evidence.provider_id,
        model_id=evidence.model_id,
        health=evidence.health,
        reason_code=evidence.reason_code,
        executable_version=evidence.executable_version,
        decoder_version=evidence.decoder_version,
        binding_id=evidence.binding_id,
        required_capabilities=evidence.required_capabilities,
        checked_at=evidence.checked_at,
        content_digest=_digest(values),
    )


def _worktree_summary(evidence: DockyardWorktreeEvidence) -> DockyardWorktreeSummary:
    values = (
        evidence.task_id,
        str(evidence.revision),
        str(evidence.attempt),
        evidence.worktree_id,
        evidence.lifecycle_state,
        evidence.branch,
        evidence.head_commit,
        _bool_text(evidence.dirty),
        evidence.checked_at,
    )
    return DockyardWorktreeSummary(
        task_id=evidence.task_id,
        revision=evidence.revision,
        attempt=evidence.attempt,
        worktree_id=evidence.worktree_id,
        lifecycle_state=evidence.lifecycle_state,
        branch=evidence.branch,
        head_commit=evidence.head_commit,
        dirty=evidence.dirty,
        checked_at=evidence.checked_at,
        content_digest=_digest(values),
    )


def _latest_acceptance(snapshot: StateSnapshot, task: TaskEntry) -> AcceptanceEntry | None:
    matches = tuple(
        entry
        for entry in snapshot.acceptances
        if entry.task_id == task.task_id and entry.revision == task.revision
    )
    if not matches:
        return None
    return max(matches, key=lambda entry: (entry.attempt, entry.review_n, entry.filename))


def _approval_summary(snapshot: StateSnapshot, task: TaskEntry) -> DockyardApprovalSummary:
    latest = _latest_acceptance(snapshot, task)
    pending = task.state == "review_ready" or (
        task.state == "blocked" and task.blocked_owner in {"owner", "user", "PM"}
    )
    decision = None if latest is None else latest.decision
    gate = None if latest is None else latest.owner_approval_gate
    values = (
        task.task_id,
        str(task.revision),
        str(len(task.granted_approval_ids or ())),
        "" if decision is None else decision,
        "" if gate is None else gate,
        _bool_text(pending),
        task.timestamps.updated_at,
    )
    return DockyardApprovalSummary(
        task_id=task.task_id,
        revision=task.revision,
        granted_approval_count=len(task.granted_approval_ids or ()),
        latest_acceptance_decision=decision,
        owner_approval_gate=gate,
        pending_user_decision=pending,
        updated_at=task.timestamps.updated_at,
        content_digest=_digest(values),
    )


def _validate_identities(request: DockyardProjectionRequest) -> None:
    snapshot = request.snapshot
    tasks = {task.task_id: task for task in snapshot.tasks}
    if len(tasks) != len(snapshot.tasks):
        _fail(DockyardProjectionConflictError, "duplicate_task")

    run_keys: set[tuple[str, int, int, str]] = set()
    for run in request.run_evidence:
        key = (run.task_id, run.revision, run.attempt, run.dispatch_id)
        if key in run_keys:
            _fail(DockyardProjectionConflictError, "duplicate_run")
        run_keys.add(key)
        task = tasks.get(run.task_id)
        if task is None or task.revision != run.revision or task.attempt != run.attempt:
            _fail(DockyardProjectionConflictError, "run_task_identity")
        if task.current_dispatch is None:
            if run.phase not in {"FAILED", "RETURNED"}:
                _fail(DockyardProjectionConflictError, "run_dispatch_identity")
        elif task.current_dispatch.dispatch_id != run.dispatch_id:
            _fail(DockyardProjectionConflictError, "run_dispatch_identity")

    provider_ids: set[str] = set()
    for provider in request.provider_evidence:
        if provider.provider_id in provider_ids:
            _fail(DockyardProjectionConflictError, "duplicate_provider")
        provider_ids.add(provider.provider_id)

    worktree_keys: set[tuple[str, int, int]] = set()
    for worktree in request.worktree_evidence:
        key = (worktree.task_id, worktree.revision, worktree.attempt)
        if key in worktree_keys:
            _fail(DockyardProjectionConflictError, "duplicate_worktree")
        worktree_keys.add(key)
        task = tasks.get(worktree.task_id)
        if task is None or task.revision != worktree.revision or task.attempt != worktree.attempt:
            _fail(DockyardProjectionConflictError, "worktree_task_identity")
        if task.current_dispatch is not None and task.current_dispatch.branch != worktree.branch:
            _fail(DockyardProjectionConflictError, "worktree_branch_identity")


def _project_health(
    snapshot: StateSnapshot,
    runs: tuple[DockyardRunSummary, ...],
    providers: tuple[DockyardProviderSummary, ...],
    worktrees: tuple[DockyardWorktreeSummary, ...],
    checked_at: str,
) -> DockyardProjectHealth:
    issues: list[str] = []
    if not providers:
        provider_state = DockyardProviderHealth.UNKNOWN
        issues.append("NO_PROVIDER_EVIDENCE")
    elif any(item.health is DockyardProviderHealth.UNKNOWN for item in providers):
        provider_state = DockyardProviderHealth.UNKNOWN
        issues.append("PROVIDER_UNKNOWN")
    elif any(item.health is DockyardProviderHealth.NOT_READY for item in providers):
        provider_state = DockyardProviderHealth.NOT_READY
        issues.append("PROVIDER_NOT_READY")
    elif any(item.health is DockyardProviderHealth.DEGRADED for item in providers):
        provider_state = DockyardProviderHealth.DEGRADED
        issues.append("PROVIDER_DEGRADED")
    else:
        provider_state = DockyardProviderHealth.READY

    if any(item.lifecycle_state == "UNKNOWN" for item in worktrees):
        issues.append("WORKTREE_UNKNOWN")
    if any(
        item.supervisor_state == "UNKNOWN"
        or item.worker_state == "UNKNOWN"
        or item.heartbeat_state == "UNKNOWN"
        or item.lease_state == "UNKNOWN"
        for item in runs
    ):
        issues.append("RUN_EVIDENCE_UNKNOWN")
    issues_tuple = tuple(sorted(issues))
    values = (
        "VERIFIED",
        "ACTIVE",
        "AVAILABLE",
        "ACTIVE" if runs else "IDLE",
        provider_state.value,
        *issues_tuple,
        checked_at,
        snapshot.read_hexsha,
    )
    return DockyardProjectHealth(
        snapshot_state="VERIFIED",
        pm_state="ACTIVE",
        scheduler_state="AVAILABLE",
        runner_state="ACTIVE" if runs else "IDLE",
        provider_state=provider_state,
        issue_codes=issues_tuple,
        checked_at=checked_at,
        content_digest=_digest(values),
    )


def build_dockyard_projection(request: DockyardProjectionRequest) -> DockyardProjectionBundle:
    """Build one deterministic, sensitive-data-free Dockyard projection."""

    if type(request) is not DockyardProjectionRequest:
        _fail(DockyardProjectionInputError, "request_type")
    _validate_identities(request)
    snapshot = request.snapshot

    tasks = tuple(_task_summary(task) for task in sorted(snapshot.tasks, key=lambda item: item.task_id))
    summary_by_id = {summary.task_id: summary for summary in tasks}
    details = tuple(
        _task_detail(task, summary_by_id[task.task_id])
        for task in sorted(snapshot.tasks, key=lambda item: item.task_id)
    )
    runs = tuple(
        _run_summary(item)
        for item in sorted(
            request.run_evidence,
            key=lambda item: (item.task_id, item.revision, item.attempt, item.dispatch_id),
        )
    )
    providers = tuple(
        _provider_summary(item)
        for item in sorted(request.provider_evidence, key=lambda item: item.provider_id)
    )
    worktrees = tuple(
        _worktree_summary(item)
        for item in sorted(
            request.worktree_evidence,
            key=lambda item: (item.task_id, item.revision, item.attempt),
        )
    )
    approvals = tuple(
        _approval_summary(snapshot, task)
        for task in sorted(snapshot.tasks, key=lambda item: item.task_id)
    )
    health = _project_health(snapshot, runs, providers, worktrees, request.generated_at)

    states = sorted({task.state for task in snapshot.tasks})
    state_counts = tuple(
        DockyardStateCount(state, sum(task.state == state for task in snapshot.tasks))
        for state in states
    )
    active_ids = tuple(
        task.task_id
        for task in sorted(snapshot.tasks, key=lambda item: item.task_id)
        if task.state in {"dispatched", "in_progress"}
    )
    pending_ids = tuple(item.task_id for item in approvals if item.pending_user_decision)
    recent_ids = tuple(
        task.task_id
        for task in sorted(
            snapshot.tasks,
            key=lambda item: (item.timestamps.updated_at, item.task_id),
            reverse=True,
        )
        if task.report_commit is not None or task.delivery_state not in {None, "none"}
    )[:20]
    overview_values = (
        SCHEMA_VERSION,
        snapshot.project_id,
        snapshot.adoption_level,
        snapshot.read_hexsha,
        snapshot.updated_at,
        request.generated_at,
        snapshot.pm_mode,
        *tuple(f"{item.state}:{item.count}" for item in state_counts),
        *active_ids,
        *pending_ids,
        *recent_ids,
        *tuple(item.content_digest for item in providers),
        health.content_digest,
    )
    overview = DockyardOverview(
        schema_version=SCHEMA_VERSION,
        project_id=snapshot.project_id,
        adoption_level=snapshot.adoption_level,
        snapshot_commit=snapshot.read_hexsha,
        updated_at=snapshot.updated_at,
        generated_at=request.generated_at,
        pm_mode=snapshot.pm_mode,
        state_counts=state_counts,
        active_task_ids=active_ids,
        pending_approval_task_ids=pending_ids,
        recent_delivery_task_ids=recent_ids,
        provider_summaries=providers,
        health=health,
        content_digest=_digest(overview_values),
    )
    bundle_values = (
        overview.content_digest,
        *tuple(item.content_digest for item in tasks),
        *tuple(item.content_digest for item in details),
        *tuple(item.content_digest for item in runs),
        *tuple(item.content_digest for item in approvals),
        *tuple(item.content_digest for item in providers),
        *tuple(item.content_digest for item in worktrees),
    )
    return DockyardProjectionBundle(
        overview=overview,
        tasks=tasks,
        task_details=details,
        runs=runs,
        approvals=approvals,
        providers=providers,
        worktrees=worktrees,
        content_digest=_digest(bundle_values),
    )


__all__ = [
    "DockyardApprovalSummary",
    "DockyardOverview",
    "DockyardProjectHealth",
    "DockyardProjectionBundle",
    "DockyardProjectionConflictError",
    "DockyardProjectionError",
    "DockyardProjectionInputError",
    "DockyardProjectionRequest",
    "DockyardProviderEvidence",
    "DockyardProviderHealth",
    "DockyardProviderSummary",
    "DockyardRunEvidence",
    "DockyardRunSummary",
    "DockyardStateCount",
    "DockyardTaskDetail",
    "DockyardTaskSummary",
    "DockyardWorktreeEvidence",
    "DockyardWorktreeSummary",
    "SCHEMA_VERSION",
    "build_dockyard_projection",
]
