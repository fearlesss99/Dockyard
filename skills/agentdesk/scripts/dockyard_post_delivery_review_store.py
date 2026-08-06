"""Durable local evidence store for Dockyard post-delivery review."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import threading
from contextlib import contextmanager
from dataclasses import dataclass, fields, replace
from enum import Enum
from pathlib import Path

SCHEMA_VERSION = "dockyard.post-delivery-review/v1"
_RELATIVE = Path(".agentdesk") / "runtime" / "dockyard-review"
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_SHA = re.compile(r"[0-9a-f]{40}")
_SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,254}")
_FILE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,126}")
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400


class DockyardReviewStoreError(ValueError):
    pass


class DockyardReviewInputError(DockyardReviewStoreError):
    pass


class DockyardReviewConflictError(DockyardReviewStoreError):
    pass


class DockyardReviewSecurityError(DockyardReviewStoreError):
    pass


class DockyardReviewNotFoundError(DockyardReviewStoreError):
    pass


class DockyardReviewPhaseError(DockyardReviewStoreError):
    pass


class DockyardPostDeliveryReviewPhase(str, Enum):
    DELIVERY_BOUND = "DELIVERY_BOUND"
    REVIEW_RESERVED = "REVIEW_RESERVED"
    MAD_STARTED = "MAD_STARTED"
    MAD_COMPLETED = "MAD_COMPLETED"
    REVIEW_APPLIED = "REVIEW_APPLIED"
    INTEGRATION_APPLIED = "INTEGRATION_APPLIED"
    FINALIZED = "FINALIZED"


class DockyardPostDeliveryReviewOutcome(str, Enum):
    RESERVED = "RESERVED"
    AUDIT_PASS = "AUDIT_PASS"
    AUDIT_FAIL = "AUDIT_FAIL"
    AUDIT_BLOCKED = "AUDIT_BLOCKED"
    ACCEPTED = "ACCEPTED"
    RETURNED = "RETURNED"
    BLOCKED = "BLOCKED"
    INTEGRATED = "INTEGRATED"
    RECOVERY_REQUIRED = "RECOVERY_REQUIRED"
    REJECTED = "REJECTED"
    FINALIZED = "FINALIZED"


def _text(value: object, name: str) -> str:
    if type(value) is not str or not value or len(value) > 4096:
        raise DockyardReviewInputError(f"review:{name}")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise DockyardReviewInputError(f"review:{name}")
    return value


def _identifier(value: object, name: str) -> str:
    value = _text(value, name)
    if _SAFE_ID.fullmatch(value) is None:
        raise DockyardReviewInputError(f"review:{name}")
    return value


def _optional_identifier(value: object, name: str) -> str | None:
    if value is None:
        return None
    return _identifier(value, name)


def _digest(value: object, name: str) -> str:
    if type(value) is not str or _DIGEST.fullmatch(value) is None:
        raise DockyardReviewInputError(f"review:{name}")
    return value


def _optional_digest(value: object, name: str) -> str | None:
    if value is None:
        return None
    return _digest(value, name)


def _sha(value: object, name: str) -> str:
    if type(value) is not str or _SHA.fullmatch(value) is None:
        raise DockyardReviewInputError(f"review:{name}")
    return value


def _positive(value: object, name: str, maximum: int | None = None) -> int:
    if type(value) is not int or value < 1 or (maximum is not None and value > maximum):
        raise DockyardReviewInputError(f"review:{name}")
    return value


def _timestamp(value: object, name: str) -> str:
    value = _text(value, name)
    if not value.endswith("Z") or "T" not in value:
        raise DockyardReviewInputError(f"review:{name}")
    return value


@dataclass(frozen=True, slots=True)
class DockyardPostDeliveryReviewRequest:
    schema_version: str
    operation_id: str
    project_id: str
    task_id: str
    revision: int
    attempt: int
    dispatch_id: str
    dispatch_generation_id: str
    queue_id: str
    schedule_receipt_id: str
    admission_plan_digest: str
    handoff_id: str
    external_worker_receipt_digest: str
    dispatch_event_id: str
    dispatch_event_digest: str
    acknowledge_event_id: str
    acknowledge_event_digest: str
    delivery_event_id: str
    delivery_event_digest: str
    delivery_receipt_digest: str
    implementation_commit: str
    report_commit: str
    worktree_id: str
    worktree_identity_digest: str
    branch: str
    base_commit: str
    audit_input_digest: str
    audit_config_id: str
    acceptance_event_id: str
    acceptance_request_digest: str
    integration_event_id: str | None
    integration_request_digest: str | None
    return_event_id: str | None
    requeue_event_id: str | None
    block_event_id: str | None
    routing_digest: str
    reserved_at: str
    content_digest: str

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise DockyardReviewInputError("review:schema_version")
        for name in (
            "operation_id", "project_id", "task_id", "dispatch_id",
            "dispatch_generation_id", "queue_id", "schedule_receipt_id",
            "handoff_id", "dispatch_event_id", "acknowledge_event_id",
            "delivery_event_id", "worktree_id", "audit_config_id",
            "acceptance_event_id",
        ):
            _identifier(getattr(self, name), name)
        _positive(self.revision, "revision")
        _positive(self.attempt, "attempt", 3)
        for name in (
            "admission_plan_digest", "external_worker_receipt_digest",
            "dispatch_event_digest", "acknowledge_event_digest",
            "delivery_event_digest", "delivery_receipt_digest",
            "worktree_identity_digest", "audit_input_digest",
            "acceptance_request_digest", "routing_digest", "content_digest",
        ):
            _digest(getattr(self, name), name)
        for name in ("implementation_commit", "report_commit", "base_commit"):
            _sha(getattr(self, name), name)
        if self.implementation_commit == self.report_commit:
            raise DockyardReviewInputError("review:double_commit")
        _text(self.branch, "branch")
        _optional_identifier(self.integration_event_id, "integration_event_id")
        _optional_digest(self.integration_request_digest, "integration_request_digest")
        if (self.integration_event_id is None) != (self.integration_request_digest is None):
            raise DockyardReviewInputError("review:integration_pair")
        for name in ("return_event_id", "requeue_event_id", "block_event_id"):
            _optional_identifier(getattr(self, name), name)
        routing = (self.return_event_id is not None, self.requeue_event_id is not None, self.block_event_id is not None)
        if routing not in ((False, False, False), (True, True, False), (False, False, True)):
            raise DockyardReviewInputError("review:routing")
        _timestamp(self.reserved_at, "reserved_at")


@dataclass(frozen=True, slots=True)
class DockyardPostDeliveryReviewReceipt:
    schema_version: str
    review_id: str
    operation_id: str
    request_content_digest: str
    task_id: str
    revision: int
    attempt: int
    dispatch_id: str
    dispatch_generation_id: str
    phase: DockyardPostDeliveryReviewPhase
    outcome: DockyardPostDeliveryReviewOutcome
    audit_result_id: str | None
    audit_result_digest: str | None
    audit_verdict: str | None
    acceptance_event_id: str | None
    integration_event_id: str | None
    return_event_id: str | None
    requeue_event_id: str | None
    block_event_id: str | None
    reserved_at: str
    mad_started_at: str | None
    mad_completed_at: str | None
    review_applied_at: str | None
    integration_applied_at: str | None
    finalized_at: str | None
    updated_at: str
    content_digest: str

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise DockyardReviewInputError("review:receipt_schema")
        for name in ("review_id", "operation_id", "task_id", "dispatch_id", "dispatch_generation_id"):
            _identifier(getattr(self, name), name)
        _positive(self.revision, "revision")
        _positive(self.attempt, "attempt", 3)
        if type(self.phase) is not DockyardPostDeliveryReviewPhase or type(self.outcome) is not DockyardPostDeliveryReviewOutcome:
            raise DockyardReviewInputError("review:receipt_enum")
        for name in ("audit_result_id", "acceptance_event_id", "integration_event_id", "return_event_id", "requeue_event_id", "block_event_id"):
            _optional_identifier(getattr(self, name), name)
        _optional_digest(self.audit_result_digest, "audit_result_digest")
        if self.audit_verdict is not None and self.audit_verdict not in ("pass", "fail", "blocked"):
            raise DockyardReviewInputError("review:audit_verdict")
        audit_values = (self.audit_result_id, self.audit_result_digest, self.audit_verdict)
        if any(value is None for value in audit_values) and any(value is not None for value in audit_values):
            raise DockyardReviewInputError("review:audit_result")
        _timestamp(self.reserved_at, "reserved_at")
        _timestamp(self.updated_at, "updated_at")
        for name in ("mad_started_at", "mad_completed_at", "review_applied_at", "integration_applied_at", "finalized_at"):
            value = getattr(self, name)
            if value is not None:
                _timestamp(value, name)
        _digest(self.request_content_digest, "request_content_digest")
        _digest(self.content_digest, "content_digest")


_REQUEST_FIELDS = tuple(field.name for field in fields(DockyardPostDeliveryReviewRequest))
_RECEIPT_FIELDS = tuple(field.name for field in fields(DockyardPostDeliveryReviewReceipt))


def _plain_value(value: object) -> object:
    return value.value if isinstance(value, Enum) else value


def _canonical(fields_: tuple[str, ...], values: tuple[object, ...]) -> bytes:
    lines = [name + ": " + json.dumps(_plain_value(value), ensure_ascii=False, separators=(",", ":")) for name, value in zip(fields_, values)]
    return ("\n".join(lines) + "\n").encode("utf-8")


def _content_digest(fields_: tuple[str, ...], values: tuple[object, ...]) -> str:
    return "sha256:" + hashlib.sha256(_canonical(fields_[:-1], values[:-1])).hexdigest()


def with_request_digest(value: DockyardPostDeliveryReviewRequest) -> DockyardPostDeliveryReviewRequest:
    if type(value) is not DockyardPostDeliveryReviewRequest:
        raise DockyardReviewInputError("review:request_type")
    values = tuple(getattr(value, name) for name in _REQUEST_FIELDS)
    return replace(value, content_digest=_content_digest(_REQUEST_FIELDS, values))


def with_receipt_digest(value: DockyardPostDeliveryReviewReceipt) -> DockyardPostDeliveryReviewReceipt:
    if type(value) is not DockyardPostDeliveryReviewReceipt:
        raise DockyardReviewInputError("review:receipt_type")
    values = tuple(getattr(value, name) for name in _RECEIPT_FIELDS)
    return replace(value, content_digest=_content_digest(_RECEIPT_FIELDS, values))


def encode_request(value: DockyardPostDeliveryReviewRequest) -> bytes:
    values = tuple(getattr(value, name) for name in _REQUEST_FIELDS)
    if value.content_digest != _content_digest(_REQUEST_FIELDS, values):
        raise DockyardReviewConflictError("review:request_digest")
    return _canonical(_REQUEST_FIELDS, values)


def encode_receipt(value: DockyardPostDeliveryReviewReceipt) -> bytes:
    values = tuple(getattr(value, name) for name in _RECEIPT_FIELDS)
    if value.content_digest != _content_digest(_RECEIPT_FIELDS, values):
        raise DockyardReviewConflictError("review:receipt_digest")
    return _canonical(_RECEIPT_FIELDS, values)


def _decode(data: bytes, expected: tuple[str, ...]) -> dict[str, object]:
    try:
        text = data.decode("utf-8")
    except UnicodeError as exc:
        raise DockyardReviewSecurityError("review:utf8") from exc
    if not text.endswith("\n") or text.endswith("\n\n"):
        raise DockyardReviewSecurityError("review:canonical_lf")
    result: dict[str, object] = {}
    names: list[str] = []
    for line in text[:-1].split("\n"):
        if ": " not in line:
            raise DockyardReviewSecurityError("review:canonical_line")
        name, token = line.split(": ", 1)
        if name in result:
            raise DockyardReviewSecurityError("review:duplicate_field")
        try:
            result[name] = json.loads(token)
        except json.JSONDecodeError as exc:
            raise DockyardReviewSecurityError("review:scalar") from exc
        names.append(name)
    if tuple(names) != expected:
        raise DockyardReviewSecurityError("review:field_order")
    return result


def decode_request(data: bytes) -> DockyardPostDeliveryReviewRequest:
    raw = _decode(data, _REQUEST_FIELDS)
    try:
        value = DockyardPostDeliveryReviewRequest(**raw)
    except (TypeError, ValueError) as exc:
        raise DockyardReviewSecurityError("review:request_decode") from exc
    if encode_request(value) != data:
        raise DockyardReviewSecurityError("review:request_noncanonical")
    return value


def decode_receipt(data: bytes) -> DockyardPostDeliveryReviewReceipt:
    raw = _decode(data, _RECEIPT_FIELDS)
    try:
        raw["phase"] = DockyardPostDeliveryReviewPhase(raw["phase"])
        raw["outcome"] = DockyardPostDeliveryReviewOutcome(raw["outcome"])
        value = DockyardPostDeliveryReviewReceipt(**raw)
    except (TypeError, ValueError) as exc:
        raise DockyardReviewSecurityError("review:receipt_decode") from exc
    if encode_receipt(value) != data:
        raise DockyardReviewSecurityError("review:receipt_noncanonical")
    return value


def _is_link(path: Path) -> bool:
    if path.is_symlink():
        return True
    try:
        metadata = os.lstat(path)
    except OSError:
        return False
    return bool(getattr(metadata, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT)


def _directory(path: Path, create: bool) -> Path:
    if path.exists():
        if _is_link(path) or not path.is_dir():
            raise DockyardReviewSecurityError("review:directory")
    elif create:
        path.mkdir()
    else:
        raise DockyardReviewNotFoundError("review:directory")
    return path


def _root(project_root: Path, create: bool) -> tuple[Path, Path]:
    if not isinstance(project_root, Path) or not project_root.is_absolute() or not project_root.is_dir():
        raise DockyardReviewInputError("review:project_root")
    current = project_root
    for part in _RELATIVE.parts:
        current = _directory(current / part, create)
    return _directory(current / "requests", create), _directory(current / "receipts", create)


def _file(directory: Path, review_id: str) -> Path:
    if type(review_id) is not str or _FILE_ID.fullmatch(review_id) is None:
        raise DockyardReviewInputError("review:review_id")
    return directory / (review_id + ".yaml")


def _reject_extra(directory: Path, review_id: str) -> None:
    expected = review_id + ".yaml"
    try:
        entries = tuple(directory.iterdir())
    except OSError as exc:
        raise DockyardReviewSecurityError("review:directory_read") from exc
    for entry in entries:
        if entry.name.startswith(review_id + ".") and entry.name != expected:
            raise DockyardReviewSecurityError("review:extra_file")


def _read(path: Path) -> bytes | None:
    if not path.exists():
        return None
    if _is_link(path) or not path.is_file():
        raise DockyardReviewSecurityError("review:file")
    try:
        return path.read_bytes()
    except OSError as exc:
        raise DockyardReviewSecurityError("review:read") from exc


def _write(path: Path, data: bytes) -> None:
    temporary = path.with_name(path.name + ".tmp-" + secrets.token_hex(8))
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except OSError as exc:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise DockyardReviewConflictError("review:write") from exc


_GUARD = threading.Lock()
_LOCKS: dict[str, threading.Lock] = {}


@contextmanager
def _store_lock(project_root: Path):
    key = os.path.normcase(str(project_root.resolve()))
    with _GUARD:
        lock = _LOCKS.setdefault(key, threading.Lock())
    with lock:
        yield


class DockyardPostDeliveryReviewStore:
    def __init__(self, project_root: Path) -> None:
        if not isinstance(project_root, Path) or not project_root.is_absolute() or not project_root.is_dir():
            raise DockyardReviewInputError("review:project_root")
        self.project_root = project_root

    def reserve(self, review_id: str, request: DockyardPostDeliveryReviewRequest) -> DockyardPostDeliveryReviewReceipt:
        if type(request) is not DockyardPostDeliveryReviewRequest:
            raise DockyardReviewInputError("review:request_type")
        request_bytes = encode_request(request)
        with _store_lock(self.project_root):
            requests, receipts = _root(self.project_root, True)
            request_path = _file(requests, review_id)
            receipt_path = _file(receipts, review_id)
            _reject_extra(requests, review_id)
            _reject_extra(receipts, review_id)
            existing_request, existing_receipt = _read(request_path), _read(receipt_path)
            if existing_request is not None:
                if existing_request != request_bytes or existing_receipt is None:
                    raise DockyardReviewConflictError("review:reservation_replay")
                return decode_receipt(existing_receipt)
            if existing_receipt is not None:
                raise DockyardReviewConflictError("review:orphan_receipt")
            receipt = with_receipt_digest(DockyardPostDeliveryReviewReceipt(
                SCHEMA_VERSION, review_id, request.operation_id,
                request.content_digest, request.task_id, request.revision,
                request.attempt, request.dispatch_id,
                request.dispatch_generation_id,
                DockyardPostDeliveryReviewPhase.DELIVERY_BOUND,
                DockyardPostDeliveryReviewOutcome.RESERVED,
                None, None, None, None, None, None, None, None,
                request.reserved_at, None, None, None, None, None,
                request.reserved_at, "sha256:" + "0" * 64,
            ))
            _write(request_path, request_bytes)
            _write(receipt_path, encode_receipt(receipt))
            return receipt

    def read_request(self, review_id: str) -> DockyardPostDeliveryReviewRequest:
        requests, _ = _root(self.project_root, False)
        _reject_extra(requests, review_id)
        data = _read(_file(requests, review_id))
        if data is None:
            raise DockyardReviewNotFoundError("review:request")
        return decode_request(data)

    def read_receipt(self, review_id: str) -> DockyardPostDeliveryReviewReceipt:
        _, receipts = _root(self.project_root, False)
        _reject_extra(receipts, review_id)
        data = _read(_file(receipts, review_id))
        if data is None:
            raise DockyardReviewNotFoundError("review:receipt")
        return decode_receipt(data)

    def enumerate_receipts(self) -> tuple[DockyardPostDeliveryReviewReceipt, ...]:
        """Return a stable validated snapshot without exposing Store paths."""
        _, receipts = _root(self.project_root, False)
        entries = []
        try:
            paths = tuple(sorted(receipts.iterdir(), key=lambda item: item.name))
        except OSError as exc:
            raise DockyardReviewSecurityError("review:enumerate") from exc
        for path in paths:
            if path.is_symlink() or not path.is_file() or path.suffix != ".yaml":
                raise DockyardReviewSecurityError("review:enumerate_entry")
            review_id = path.stem
            _file(receipts, review_id)
            data = _read(path)
            if data is None:
                raise DockyardReviewConflictError("review:enumerate_missing")
            entries.append(decode_receipt(data))
        return tuple(entries)

    def advance(
        self,
        review_id: str,
        target_phase: DockyardPostDeliveryReviewPhase,
        outcome: DockyardPostDeliveryReviewOutcome,
        updated_at: str,
        *,
        audit_result_id: str | None = None,
        audit_result_digest: str | None = None,
        audit_verdict: str | None = None,
        applied_event_id: str | None = None,
    ) -> DockyardPostDeliveryReviewReceipt:
        if type(target_phase) is not DockyardPostDeliveryReviewPhase or type(outcome) is not DockyardPostDeliveryReviewOutcome:
            raise DockyardReviewInputError("review:advance_enum")
        _timestamp(updated_at, "updated_at")
        requests, receipts = _root(self.project_root, False)
        request_path, receipt_path = _file(requests, review_id), _file(receipts, review_id)
        with _store_lock(self.project_root):
            _reject_extra(requests, review_id)
            _reject_extra(receipts, review_id)
            request_data, receipt_data = _read(request_path), _read(receipt_path)
            if request_data is None or receipt_data is None:
                raise DockyardReviewNotFoundError("review:pair")
            request, current = decode_request(request_data), decode_receipt(receipt_data)
            order = tuple(DockyardPostDeliveryReviewPhase)
            current_index, target_index = order.index(current.phase), order.index(target_phase)
            integration = request.integration_event_id is not None
            legal = target_index == current_index + 1
            terminal_without_integration = current.audit_verdict in ("fail", "blocked")
            if (
                current.phase is DockyardPostDeliveryReviewPhase.REVIEW_APPLIED
                and target_phase is DockyardPostDeliveryReviewPhase.FINALIZED
                and (not integration or terminal_without_integration)
            ):
                legal = True
            if target_phase is current.phase:
                if (
                    outcome is not current.outcome
                    or audit_result_id not in (None, current.audit_result_id)
                    or audit_result_digest not in (None, current.audit_result_digest)
                    or audit_verdict not in (None, current.audit_verdict)
                    or applied_event_id is not None
                ):
                    raise DockyardReviewConflictError("review:phase_replay")
                candidate = current
            elif not legal or current.phase is DockyardPostDeliveryReviewPhase.FINALIZED:
                raise DockyardReviewPhaseError("review:phase")
            else:
                changes: dict[str, object] = {"phase": target_phase, "outcome": outcome, "updated_at": updated_at}
                if target_phase is DockyardPostDeliveryReviewPhase.MAD_STARTED:
                    changes["mad_started_at"] = updated_at
                elif target_phase is DockyardPostDeliveryReviewPhase.MAD_COMPLETED:
                    if audit_result_id is None or audit_result_digest is None or audit_verdict is None:
                        raise DockyardReviewInputError("review:audit_result")
                    changes.update(audit_result_id=audit_result_id, audit_result_digest=audit_result_digest, audit_verdict=audit_verdict, mad_completed_at=updated_at)
                elif target_phase is DockyardPostDeliveryReviewPhase.REVIEW_APPLIED:
                    changes["review_applied_at"] = updated_at
                    if current.audit_verdict == "pass":
                        changes["acceptance_event_id"] = applied_event_id
                    elif current.audit_verdict == "fail":
                        changes["return_event_id"] = applied_event_id
                        changes["requeue_event_id"] = request.requeue_event_id
                    elif current.audit_verdict == "blocked":
                        changes["block_event_id"] = applied_event_id
                    else:
                        raise DockyardReviewInputError("review:missing_verdict")
                elif target_phase is DockyardPostDeliveryReviewPhase.INTEGRATION_APPLIED:
                    if (
                        not integration
                        or current.audit_verdict != "pass"
                        or applied_event_id != request.integration_event_id
                    ):
                        raise DockyardReviewInputError("review:integration")
                    changes.update(integration_event_id=applied_event_id, integration_applied_at=updated_at)
                elif target_phase is DockyardPostDeliveryReviewPhase.FINALIZED:
                    changes["finalized_at"] = updated_at
                candidate = with_receipt_digest(replace(current, content_digest="sha256:" + "0" * 64, **changes))
            candidate_bytes = encode_receipt(candidate)
            if target_phase is current.phase:
                if candidate_bytes != receipt_data:
                    raise DockyardReviewConflictError("review:phase_replay")
                return current
            _write(receipt_path, candidate_bytes)
            return candidate


__all__ = [
    "SCHEMA_VERSION", "DockyardPostDeliveryReviewPhase",
    "DockyardPostDeliveryReviewOutcome", "DockyardPostDeliveryReviewRequest",
    "DockyardPostDeliveryReviewReceipt", "DockyardPostDeliveryReviewStore",
    "DockyardReviewStoreError", "DockyardReviewInputError",
    "DockyardReviewConflictError", "DockyardReviewSecurityError",
    "DockyardReviewNotFoundError", "DockyardReviewPhaseError",
    "with_request_digest", "with_receipt_digest", "encode_request",
    "decode_request", "encode_receipt", "decode_receipt",
]
