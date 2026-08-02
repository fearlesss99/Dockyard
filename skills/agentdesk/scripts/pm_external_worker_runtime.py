"""PM-owned facade for durable external Worker execution.

This module composes the existing scheduler handoff runtime with a local,
gitignored endpoint registry. It never creates a Codex Worker thread and never
persists prompts or provider output. The synchronous return to the sole PM is
recorded as a compact receipt after the handoff runtime has finalized.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path

from dispatcher_gateway import AgentCliProvider
from mad_audit_gateway import MadAuditGatewayInput
from mad_gateway import MadGatewayConfig
from portfolio_scheduler_worker_handoff_runtime import (
    AdmittedDispatchStartRequest,
    AdmittedDispatchStartResult,
    start_admitted_dispatch_completion,
)
from workflow_orchestrator import (
    AcceptanceCycleRequest,
    AcceptanceCycleResult,
    BlockedAuditRequest,
    BlockedAuditResult,
    DeliveryRemediationRequest,
    DeliveryRemediationResult,
    DispatchCycleResult,
    WorkflowOrchestrator,
)
from control_plane_transition import TransitionRequest
from core_types import WorkerKind

CONFIG_SCHEMA = "agentdesk.external-workers/v1"
RECEIPT_SCHEMA = "agentdesk.pm-external-worker-receipt/v1"
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")


class PmExternalWorkerError(ValueError):
    """Base error for the PM external Worker facade."""


class PmExternalWorkerInputError(PmExternalWorkerError):
    """Configuration or request validation failed before execution."""


class PmExternalWorkerConflictError(PmExternalWorkerError):
    """Durable receipt or endpoint identity diverged."""


@dataclass(frozen=True, slots=True)
class ExternalWorkerEndpoint:
    endpoint_id: str
    role_id: str
    provider_id: str
    model_binding_id: str
    executable: str
    permission_mode: str
    status: str


@dataclass(frozen=True, slots=True)
class PmExternalWorkerRequest:
    operation_id: str
    project_root: str
    role_id: str
    pm_role_id: str
    returned_at: str
    handoff_request: AdmittedDispatchStartRequest


@dataclass(frozen=True, slots=True)
class PmExternalWorkerReceipt:
    schema_version: str
    operation_id: str
    task_id: str
    revision: int
    attempt: int
    dispatch_id: str
    role_id: str
    pm_role_id: str
    endpoint_id: str
    provider_id: str
    model_binding_id: str
    handoff_outcome: str
    handoff_phase: str
    handoff_content_digest: str
    delivery_result_available: bool
    implementation_commit: str | None
    report_commit: str | None
    return_channel: str
    returned_at: str
    content_digest: str


@dataclass(frozen=True, slots=True)
class PmExternalWorkerRunResult:
    receipt: PmExternalWorkerReceipt
    dispatch_cycle_result: DispatchCycleResult | None


@dataclass(frozen=True, slots=True)
class PmExternalAcceptancePlan:
    audit_input: MadAuditGatewayInput
    acceptance_transition_request: TransitionRequest
    integration_transition_request: TransitionRequest | None
    worker_kind: WorkerKind
    holder_instance_id: str


@dataclass(frozen=True, slots=True)
class PmExternalAcceptanceResult:
    worker_run: PmExternalWorkerRunResult
    acceptance_cycle: AcceptanceCycleResult


@dataclass(frozen=True, slots=True)
class PmExternalReviewRoutingPlan:
    return_transition_request: TransitionRequest | None
    requeue_transition_request: TransitionRequest | None
    block_transition_request: TransitionRequest | None


@dataclass(frozen=True, slots=True)
class PmExternalReviewResult:
    worker_run: PmExternalWorkerRunResult
    acceptance_cycle: AcceptanceCycleResult
    remediation: DeliveryRemediationResult | None
    blocked_audit: BlockedAuditResult | None


def _safe_text(value: object, field: str) -> str:
    if type(value) is not str or not _SAFE_ID.fullmatch(value):
        raise PmExternalWorkerInputError(f"external_worker:{field}")
    return value


def _project_root(value: object) -> Path:
    if type(value) is not str or not value:
        raise PmExternalWorkerInputError("external_worker:project_root")
    root = Path(value)
    if not root.is_absolute() or not root.is_dir():
        raise PmExternalWorkerInputError("external_worker:project_root")
    return root.resolve()


def _runtime_root(project_root: Path) -> Path:
    path = project_root / ".agentdesk" / "runtime"
    if path.is_symlink() or not path.is_dir():
        raise PmExternalWorkerInputError("external_worker:runtime_root")
    return path


def load_external_worker_endpoint(
    project_root: Path, role_id: str
) -> ExternalWorkerEndpoint:
    """Load one exact role endpoint from the gitignored registry."""
    role_id = _safe_text(role_id, "role_id")
    path = _runtime_root(project_root) / "external-workers.yaml"
    if path.is_symlink():
        raise PmExternalWorkerInputError("external_worker:config_link")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PmExternalWorkerInputError("external_worker:config") from exc
    if type(raw) is not dict or set(raw) != {
        "schema_version", "updated_at", "workers"
    }:
        raise PmExternalWorkerInputError("external_worker:config_schema")
    if raw.get("schema_version") != CONFIG_SCHEMA or type(raw.get("workers")) is not dict:
        raise PmExternalWorkerInputError("external_worker:config_schema")
    item = raw["workers"].get(role_id)
    expected = {
        "endpoint_id", "role_id", "provider_id", "model_binding_id",
        "executable", "permission_mode", "status",
    }
    if type(item) is not dict or set(item) != expected:
        raise PmExternalWorkerInputError("external_worker:endpoint_schema")
    endpoint = ExternalWorkerEndpoint(**item)
    for field in ("endpoint_id", "role_id", "provider_id", "model_binding_id", "permission_mode"):
        _safe_text(getattr(endpoint, field), field)
    if endpoint.role_id != role_id or endpoint.status != "verified":
        raise PmExternalWorkerInputError("external_worker:endpoint_status")
    executable = Path(endpoint.executable)
    if not executable.is_absolute() or not executable.is_file():
        raise PmExternalWorkerInputError("external_worker:executable")
    return endpoint


def _receipt_path(root: Path, dispatch_id: str) -> Path:
    dispatch_id = _safe_text(dispatch_id, "dispatch_id")
    directory = _runtime_root(root) / "external-worker-receipts"
    if directory.exists() and (directory.is_symlink() or not directory.is_dir()):
        raise PmExternalWorkerConflictError("external_worker:receipt_directory")
    directory.mkdir(parents=False, exist_ok=True)
    return directory / f"{dispatch_id}.json"


def _receipt_digest(values: tuple[str, ...]) -> str:
    return "sha256:" + hashlib.sha256("\0".join(values).encode("utf-8")).hexdigest()


def _make_receipt(
    request: PmExternalWorkerRequest,
    endpoint: ExternalWorkerEndpoint,
    result: AdmittedDispatchStartResult,
    dispatch_cycle_result: DispatchCycleResult | None,
) -> PmExternalWorkerReceipt:
    delivery = (
        dispatch_cycle_result.delivery_receipt
        if dispatch_cycle_result is not None
        else None
    )
    implementation_commit = (
        delivery.implementation_commit if delivery is not None else None
    )
    report_commit = delivery.report_commit if delivery is not None else None
    values = (
        request.operation_id, result.task_id, str(result.revision),
        str(result.attempt), result.dispatch_id, request.role_id,
        request.pm_role_id, endpoint.endpoint_id, endpoint.provider_id,
        endpoint.model_binding_id, result.outcome.value, result.phase.value,
        result.content_digest, str(delivery is not None).lower(),
        implementation_commit or "", report_commit or "",
        "synchronous_pm", request.returned_at,
    )
    return PmExternalWorkerReceipt(
        RECEIPT_SCHEMA, request.operation_id, result.task_id, result.revision,
        result.attempt, result.dispatch_id, request.role_id, request.pm_role_id,
        endpoint.endpoint_id, endpoint.provider_id, endpoint.model_binding_id,
        result.outcome.value, result.phase.value, result.content_digest,
        delivery is not None, implementation_commit, report_commit,
        "synchronous_pm", request.returned_at, _receipt_digest(values),
    )


def _load_receipt(path: Path) -> PmExternalWorkerReceipt | None:
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file():
        raise PmExternalWorkerConflictError("external_worker:receipt_type")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if type(raw) is not dict or set(raw) != set(PmExternalWorkerReceipt.__dataclass_fields__):
            raise ValueError
        receipt = PmExternalWorkerReceipt(**raw)
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise PmExternalWorkerConflictError("external_worker:receipt_parse") from exc
    values = (
        receipt.operation_id, receipt.task_id, str(receipt.revision),
        str(receipt.attempt), receipt.dispatch_id, receipt.role_id,
        receipt.pm_role_id, receipt.endpoint_id, receipt.provider_id,
        receipt.model_binding_id, receipt.handoff_outcome,
        receipt.handoff_phase, receipt.handoff_content_digest,
        str(receipt.delivery_result_available).lower(),
        receipt.implementation_commit or "", receipt.report_commit or "",
        receipt.return_channel, receipt.returned_at,
    )
    if receipt.schema_version != RECEIPT_SCHEMA or receipt.content_digest != _receipt_digest(values):
        raise PmExternalWorkerConflictError("external_worker:receipt_digest")
    return receipt


def _write_receipt(path: Path, receipt: PmExternalWorkerReceipt) -> None:
    payload = (json.dumps(asdict(receipt), ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    temp = path.with_name(
        path.name + f".tmp-{os.getpid()}-{secrets.token_hex(8)}"
    )
    try:
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    except OSError as exc:
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass
        raise PmExternalWorkerConflictError("external_worker:receipt_write") from exc


async def run_pm_external_worker(
    request: PmExternalWorkerRequest,
    providers: Mapping[str, AgentCliProvider],
    orchestrator: WorkflowOrchestrator,
) -> PmExternalWorkerRunResult:
    """Execute one admitted external Worker and return evidence to the PM."""
    if type(request) is not PmExternalWorkerRequest:
        raise PmExternalWorkerInputError("external_worker:request")
    root = _project_root(request.project_root)
    if request.pm_role_id != "PM" or request.role_id == "PM":
        raise PmExternalWorkerInputError("external_worker:role_boundary")
    _safe_text(request.operation_id, "operation_id")
    _safe_text(request.role_id, "role_id")
    _safe_text(request.returned_at, "returned_at")
    if type(request.handoff_request) is not AdmittedDispatchStartRequest:
        raise PmExternalWorkerInputError("external_worker:handoff_request")
    if Path(request.handoff_request.project_root).resolve() != root:
        raise PmExternalWorkerInputError("external_worker:handoff_root")
    if type(orchestrator) is not WorkflowOrchestrator or orchestrator.project_root != root:
        raise PmExternalWorkerInputError("external_worker:orchestrator")

    endpoint = load_external_worker_endpoint(root, request.role_id)
    dispatch = request.handoff_request.dispatch_cycle_request.dispatch_request
    snapshot = dispatch.model_selection
    if (
        snapshot.selected_model_provider != endpoint.provider_id
        or snapshot.model_binding_id != endpoint.model_binding_id
    ):
        raise PmExternalWorkerConflictError("external_worker:model_binding")
    provider = providers.get(endpoint.provider_id) if isinstance(providers, Mapping) else None
    if provider is None or getattr(provider, "provider_id", None) != endpoint.provider_id:
        raise PmExternalWorkerInputError("external_worker:provider")
    if getattr(provider, "executable", endpoint.executable) != endpoint.executable:
        raise PmExternalWorkerConflictError("external_worker:provider_executable")

    path = _receipt_path(root, request.handoff_request.dispatch_id)
    existing = _load_receipt(path)
    if existing is not None:
        if (
            existing.operation_id == request.operation_id
            and existing.task_id == request.handoff_request.task_id
            and existing.revision == request.handoff_request.revision
            and existing.attempt == request.handoff_request.attempt
            and existing.dispatch_id == request.handoff_request.dispatch_id
            and existing.role_id == request.role_id
            and existing.pm_role_id == request.pm_role_id
            and existing.endpoint_id == endpoint.endpoint_id
            and existing.provider_id == endpoint.provider_id
            and existing.model_binding_id == endpoint.model_binding_id
            and existing.returned_at == request.returned_at
        ):
            return PmExternalWorkerRunResult(existing, None)
        raise PmExternalWorkerConflictError("external_worker:receipt_divergent")

    completion = await start_admitted_dispatch_completion(
        request.handoff_request, providers, orchestrator
    )
    receipt = _make_receipt(
        request,
        endpoint,
        completion.start_result,
        completion.dispatch_cycle_result,
    )
    _write_receipt(path, receipt)
    return PmExternalWorkerRunResult(receipt, completion.dispatch_cycle_result)


async def run_pm_external_worker_acceptance(
    request: PmExternalWorkerRequest,
    plan: PmExternalAcceptancePlan,
    providers: Mapping[str, AgentCliProvider],
    orchestrator: WorkflowOrchestrator,
    audit_config: MadGatewayConfig,
) -> PmExternalAcceptanceResult:
    """Run one external Worker and enter the mandatory MAD audit gate."""
    if type(plan) is not PmExternalAcceptancePlan:
        raise PmExternalWorkerInputError("external_worker:acceptance_plan")
    worker_run = await run_pm_external_worker(request, providers, orchestrator)
    dispatch_cycle_result = worker_run.dispatch_cycle_result
    if dispatch_cycle_result is None:
        raise PmExternalWorkerConflictError(
            "external_worker:delivery_replay_requires_recovery"
        )
    acceptance_request = AcceptanceCycleRequest(
        dispatch_cycle_result=dispatch_cycle_result,
        audit_input=plan.audit_input,
        acceptance_transition_request=plan.acceptance_transition_request,
        integration_transition_request=plan.integration_transition_request,
        worker_kind=plan.worker_kind,
        holder_instance_id=plan.holder_instance_id,
    )
    acceptance_cycle = await orchestrator.run_acceptance_cycle(
        acceptance_request, audit_config
    )
    return PmExternalAcceptanceResult(worker_run, acceptance_cycle)


async def run_pm_external_worker_review(
    request: PmExternalWorkerRequest,
    acceptance_plan: PmExternalAcceptancePlan,
    routing_plan: PmExternalReviewRoutingPlan,
    providers: Mapping[str, AgentCliProvider],
    orchestrator: WorkflowOrchestrator,
    audit_config: MadGatewayConfig,
) -> PmExternalReviewResult:
    """Run the mandatory MAD gate and route its exact verdict fail-closed."""
    if type(routing_plan) is not PmExternalReviewRoutingPlan:
        raise PmExternalWorkerInputError("external_worker:routing_plan")
    accepted = await run_pm_external_worker_acceptance(
        request, acceptance_plan, providers, orchestrator, audit_config
    )
    verdict = accepted.acceptance_cycle.audit_result.verdict
    remediation = None
    blocked_audit = None
    if verdict == "pass":
        if any((
            routing_plan.return_transition_request,
            routing_plan.requeue_transition_request,
            routing_plan.block_transition_request,
        )):
            raise PmExternalWorkerInputError("external_worker:pass_routing")
    elif verdict == "fail":
        if (
            routing_plan.return_transition_request is None
            or routing_plan.requeue_transition_request is None
            or routing_plan.block_transition_request is not None
        ):
            raise PmExternalWorkerInputError("external_worker:fail_routing")
        dispatch_result = accepted.worker_run.dispatch_cycle_result
        if dispatch_result is None:
            raise PmExternalWorkerConflictError("external_worker:missing_delivery")
        remediation = await orchestrator.run_delivery_remediation(
            DeliveryRemediationRequest(
                acceptance_cycle_result=accepted.acceptance_cycle,
                dispatch_cycle_result=dispatch_result,
                return_transition_request=routing_plan.return_transition_request,
                requeue_transition_request=routing_plan.requeue_transition_request,
                worker_kind=acceptance_plan.worker_kind,
                holder_instance_id=acceptance_plan.holder_instance_id,
            )
        )
    elif verdict == "blocked":
        if (
            routing_plan.block_transition_request is None
            or routing_plan.return_transition_request is not None
            or routing_plan.requeue_transition_request is not None
        ):
            raise PmExternalWorkerInputError("external_worker:blocked_routing")
        dispatch_result = accepted.worker_run.dispatch_cycle_result
        if dispatch_result is None:
            raise PmExternalWorkerConflictError("external_worker:missing_delivery")
        blocked_audit = await orchestrator.run_blocked_audit_cycle(
            BlockedAuditRequest(
                acceptance_cycle_result=accepted.acceptance_cycle,
                dispatch_cycle_result=dispatch_result,
                block_transition_request=routing_plan.block_transition_request,
                current_worker_kind=acceptance_plan.worker_kind,
            )
        )
    else:
        raise PmExternalWorkerConflictError("external_worker:audit_verdict")
    return PmExternalReviewResult(
        accepted.worker_run,
        accepted.acceptance_cycle,
        remediation,
        blocked_audit,
    )


__all__ = [
    "CONFIG_SCHEMA", "RECEIPT_SCHEMA", "ExternalWorkerEndpoint",
    "PmExternalWorkerRequest", "PmExternalWorkerReceipt",
    "PmExternalWorkerRunResult",
    "PmExternalAcceptancePlan", "PmExternalAcceptanceResult",
    "PmExternalReviewRoutingPlan", "PmExternalReviewResult",
    "PmExternalWorkerError", "PmExternalWorkerInputError",
    "PmExternalWorkerConflictError", "load_external_worker_endpoint",
    "run_pm_external_worker", "run_pm_external_worker_acceptance",
    "run_pm_external_worker_review",
]
