"""Immutable durable store for Dockyard PM plan revisions."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import threading
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Iterator

from core_types import TaskDifficulty
from difficulty_assessor import DifficultyAssessmentError, assess_task_difficulty
from portfolio_scheduler_store import BusinessPriority

__all__ = [
    "DockyardPlanPhase",
    "DockyardPlanTask",
    "DockyardPlanRecord",
    "DockyardPlanStoreError",
    "DockyardPlanInputError",
    "DockyardPlanConflictError",
    "DockyardPlanFilesystemError",
    "DockyardPlanStore",
]

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SHA = frozenset("0123456789abcdef")
_PROVIDERS = frozenset({"claude", "codex", "reasonix"})
_EXECUTION = frozenset({"parallel", "serial"})
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400
_TASK_FIELDS = frozenset({
    "task_id", "title", "description", "dependencies", "execution_mode",
    "role_id", "task_type", "provider_id", "model_id", "tier",
    "budget_tokens", "max_attempts", "base_commit", "business_priority",
    "difficulty_rationale_keys", "selected_difficulty",
    "difficulty_override_reason", "difficulty_approval_id", "risk",
    "task_capabilities", "degradation_approval_id", "expected_task_attempt",
    "new_attempt",
})
_RECORD_FIELDS = frozenset({
    "schema_version", "plan_id", "project_id", "revision", "phase",
    "requirement", "requirement_digest", "tasks", "plan_digest",
    "previous_revision_digest", "pm_owner_id", "created_at", "updated_at",
    "approved_at", "materialized_at", "last_operation_id", "request_digest",
    "content_digest",
})
_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


class DockyardPlanPhase(str, Enum):
    DRAFTED = "DRAFTED"
    EDITED = "EDITED"
    APPROVAL_PENDING = "APPROVAL_PENDING"
    APPROVED = "APPROVED"
    MATERIALIZED = "MATERIALIZED"


_ALLOWED_PHASE_TRANSITIONS = {
    DockyardPlanPhase.DRAFTED: frozenset({
        DockyardPlanPhase.EDITED,
        DockyardPlanPhase.APPROVAL_PENDING,
    }),
    DockyardPlanPhase.EDITED: frozenset({
        DockyardPlanPhase.EDITED,
        DockyardPlanPhase.APPROVAL_PENDING,
    }),
    DockyardPlanPhase.APPROVAL_PENDING: frozenset({DockyardPlanPhase.APPROVED}),
    DockyardPlanPhase.APPROVED: frozenset({
        DockyardPlanPhase.EDITED,
        DockyardPlanPhase.MATERIALIZED,
    }),
    DockyardPlanPhase.MATERIALIZED: frozenset(),
}


class DockyardPlanStoreError(Exception):
    pass


class DockyardPlanInputError(DockyardPlanStoreError):
    pass


class DockyardPlanConflictError(DockyardPlanStoreError):
    pass


class DockyardPlanFilesystemError(DockyardPlanStoreError):
    pass


def _text(value: object, field: str, maximum: int = 8192) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or len(value) > maximum:
        raise DockyardPlanInputError(f"{field} is invalid")
    if "\x00" in value:
        raise DockyardPlanInputError(f"{field} contains NUL")
    return value


def _identifier(value: object, field: str) -> str:
    text = _text(value, field, 128)
    if not _ID.fullmatch(text):
        raise DockyardPlanInputError(f"{field} is invalid")
    return text


def _sha(value: object, length: int, field: str) -> str:
    if not isinstance(value, str) or len(value) != length or any(char not in _SHA for char in value):
        raise DockyardPlanInputError(f"{field} is invalid")
    return value


def _utc(value: object, field: str) -> str:
    text = _text(value, field, 64)
    candidate = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise DockyardPlanInputError(f"{field} is invalid") from exc
    if parsed.utcoffset() is None or parsed.utcoffset().total_seconds() != 0:
        raise DockyardPlanInputError(f"{field} is invalid")
    return text


def _tuple_text(value: object, field: str) -> tuple[str, ...]:
    if not isinstance(value, tuple):
        raise DockyardPlanInputError(f"{field} must be tuple")
    result = tuple(_identifier(item, field) for item in value)
    if len(set(result)) != len(result):
        raise DockyardPlanInputError(f"{field} contains duplicates")
    return result


@dataclass(frozen=True, slots=True)
class DockyardPlanTask:
    task_id: str
    title: str
    description: str
    dependencies: tuple[str, ...]
    execution_mode: str
    role_id: str
    task_type: str
    provider_id: str
    model_id: str
    tier: str
    budget_tokens: int
    max_attempts: int
    base_commit: str
    business_priority: BusinessPriority
    difficulty_rationale_keys: tuple[str, ...]
    selected_difficulty: TaskDifficulty | None
    difficulty_override_reason: str | None
    difficulty_approval_id: str | None
    risk: str
    task_capabilities: tuple[str, ...]
    degradation_approval_id: str | None
    expected_task_attempt: int
    new_attempt: int

    def __post_init__(self) -> None:
        _identifier(self.task_id, "task_id")
        _text(self.title, "title", 256)
        _text(self.description, "description")
        dependencies = _tuple_text(self.dependencies, "dependencies")
        if self.task_id in dependencies:
            raise DockyardPlanInputError("task cannot depend on itself")
        if self.execution_mode not in _EXECUTION:
            raise DockyardPlanInputError("execution_mode is invalid")
        _identifier(self.role_id, "role_id")
        _identifier(self.task_type, "task_type")
        if self.provider_id not in _PROVIDERS:
            raise DockyardPlanInputError("provider_id is invalid")
        _text(self.model_id, "model_id", 256)
        if "\r" in self.model_id or "\n" in self.model_id:
            raise DockyardPlanInputError("model_id must be one line")
        if self.tier not in ("basic", "standard", "advanced", "expert"):
            raise DockyardPlanInputError("tier is invalid")
        if isinstance(self.budget_tokens, bool) or not isinstance(self.budget_tokens, int) or self.budget_tokens < 1:
            raise DockyardPlanInputError("budget_tokens is invalid")
        if isinstance(self.max_attempts, bool) or not isinstance(self.max_attempts, int) or not 1 <= self.max_attempts <= 3:
            raise DockyardPlanInputError("max_attempts is invalid")
        _sha(self.base_commit, 40, "base_commit")
        if type(self.business_priority) is not BusinessPriority:
            raise DockyardPlanInputError("business_priority is invalid")
        try:
            assess_task_difficulty(
                assessment_id="ASM-DOCKYARD-PLAN-VALIDATION",
                task_id=self.task_id,
                revision=1,
                rationale_keys=self.difficulty_rationale_keys,
                selected_difficulty=self.selected_difficulty,
                override_reason=self.difficulty_override_reason,
                approval_id=self.difficulty_approval_id,
            )
        except DifficultyAssessmentError as exc:
            raise DockyardPlanInputError("difficulty evidence is invalid") from exc
        _text(self.risk, "risk", 128)
        capabilities = _tuple_text(self.task_capabilities, "task_capabilities")
        if not capabilities:
            raise DockyardPlanInputError("task_capabilities must not be empty")
        if self.degradation_approval_id is not None:
            _identifier(self.degradation_approval_id, "degradation_approval_id")
        if (
            type(self.expected_task_attempt) is not int
            or type(self.new_attempt) is not int
            or not 0 <= self.expected_task_attempt <= 2
            or not 1 <= self.new_attempt <= 3
            or self.new_attempt != self.expected_task_attempt + 1
        ):
            raise DockyardPlanInputError("attempt identity is invalid")


@dataclass(frozen=True, slots=True)
class DockyardPlanRecord:
    schema_version: str
    plan_id: str
    project_id: str
    revision: int
    phase: DockyardPlanPhase
    requirement: str
    requirement_digest: str
    tasks: tuple[DockyardPlanTask, ...]
    plan_digest: str
    previous_revision_digest: str | None
    pm_owner_id: str
    created_at: str
    updated_at: str
    approved_at: str | None
    materialized_at: str | None
    last_operation_id: str
    request_digest: str
    content_digest: str

    def __post_init__(self) -> None:
        if self.schema_version != "dockyard.pm-plan/v1":
            raise DockyardPlanInputError("schema_version is invalid")
        _identifier(self.plan_id, "plan_id")
        _identifier(self.project_id, "project_id")
        if isinstance(self.revision, bool) or not isinstance(self.revision, int) or self.revision < 1:
            raise DockyardPlanInputError("revision is invalid")
        if not isinstance(self.phase, DockyardPlanPhase):
            raise DockyardPlanInputError("phase is invalid")
        _text(self.requirement, "requirement", 32768)
        _sha(self.requirement_digest, 64, "requirement_digest")
        if not isinstance(self.tasks, tuple) or not self.tasks:
            raise DockyardPlanInputError("tasks must be a non-empty tuple")
        if any(not isinstance(task, DockyardPlanTask) for task in self.tasks):
            raise DockyardPlanInputError("tasks contain an invalid item")
        ids = tuple(task.task_id for task in self.tasks)
        if len(set(ids)) != len(ids):
            raise DockyardPlanInputError("task IDs must be unique")
        known = set(ids)
        if any(set(task.dependencies) - known for task in self.tasks):
            raise DockyardPlanInputError("task dependency is unknown")
        _sha(self.plan_digest, 64, "plan_digest")
        if self.previous_revision_digest is not None:
            _sha(self.previous_revision_digest, 64, "previous_revision_digest")
        _identifier(self.pm_owner_id, "pm_owner_id")
        _utc(self.created_at, "created_at")
        _utc(self.updated_at, "updated_at")
        if self.approved_at is not None:
            _utc(self.approved_at, "approved_at")
        if self.materialized_at is not None:
            _utc(self.materialized_at, "materialized_at")
        _identifier(self.last_operation_id, "last_operation_id")
        _sha(self.request_digest, 64, "request_digest")
        _sha(self.content_digest, 64, "content_digest")


def _task_object(task: DockyardPlanTask) -> dict[str, object]:
    return {
        "task_id": task.task_id,
        "title": task.title,
        "description": task.description,
        "dependencies": list(task.dependencies),
        "execution_mode": task.execution_mode,
        "role_id": task.role_id,
        "task_type": task.task_type,
        "provider_id": task.provider_id,
        "model_id": task.model_id,
        "tier": task.tier,
        "budget_tokens": task.budget_tokens,
        "max_attempts": task.max_attempts,
        "base_commit": task.base_commit,
        "business_priority": task.business_priority.value,
        "difficulty_rationale_keys": list(task.difficulty_rationale_keys),
        "selected_difficulty": (
            None if task.selected_difficulty is None else task.selected_difficulty.value
        ),
        "difficulty_override_reason": task.difficulty_override_reason,
        "difficulty_approval_id": task.difficulty_approval_id,
        "risk": task.risk,
        "task_capabilities": list(task.task_capabilities),
        "degradation_approval_id": task.degradation_approval_id,
        "expected_task_attempt": task.expected_task_attempt,
        "new_attempt": task.new_attempt,
    }


def _record_object(record: DockyardPlanRecord, include_digest: bool) -> dict[str, object]:
    result: dict[str, object] = {
        "schema_version": record.schema_version,
        "plan_id": record.plan_id,
        "project_id": record.project_id,
        "revision": record.revision,
        "phase": record.phase.value,
        "requirement": record.requirement,
        "requirement_digest": record.requirement_digest,
        "tasks": [_task_object(task) for task in record.tasks],
        "plan_digest": record.plan_digest,
        "previous_revision_digest": record.previous_revision_digest,
        "pm_owner_id": record.pm_owner_id,
        "created_at": record.created_at,
        "updated_at": record.updated_at,
        "approved_at": record.approved_at,
        "materialized_at": record.materialized_at,
        "last_operation_id": record.last_operation_id,
        "request_digest": record.request_digest,
    }
    if include_digest:
        result["content_digest"] = record.content_digest
    return result


def _encode_object(value: dict[str, object]) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def _with_digest(record: DockyardPlanRecord) -> DockyardPlanRecord:
    digest = hashlib.sha256(_encode_object(_record_object(record, False))).hexdigest()
    return replace(record, content_digest=digest)


def _decode(raw: bytes) -> DockyardPlanRecord:
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DockyardPlanFilesystemError("plan record cannot be decoded") from exc
    if not isinstance(value, dict):
        raise DockyardPlanFilesystemError("plan record root is invalid")
    try:
        if set(value) != _RECORD_FIELDS:
            raise TypeError
        tasks_raw = value["tasks"]
        if not isinstance(tasks_raw, list):
            raise TypeError
        if any(not isinstance(item, dict) or set(item) != _TASK_FIELDS for item in tasks_raw):
            raise TypeError
        tasks = tuple(DockyardPlanTask(
            task_id=item["task_id"],
            title=item["title"],
            description=item["description"],
            dependencies=tuple(item["dependencies"]),
            execution_mode=item["execution_mode"],
            role_id=item["role_id"],
            task_type=item["task_type"],
            provider_id=item["provider_id"],
            model_id=item["model_id"],
            tier=item["tier"],
            budget_tokens=item["budget_tokens"],
            max_attempts=item["max_attempts"],
            base_commit=item["base_commit"],
            business_priority=BusinessPriority(item["business_priority"]),
            difficulty_rationale_keys=tuple(item["difficulty_rationale_keys"]),
            selected_difficulty=(
                None
                if item["selected_difficulty"] is None
                else TaskDifficulty(item["selected_difficulty"])
            ),
            difficulty_override_reason=item["difficulty_override_reason"],
            difficulty_approval_id=item["difficulty_approval_id"],
            risk=item["risk"],
            task_capabilities=tuple(item["task_capabilities"]),
            degradation_approval_id=item["degradation_approval_id"],
            expected_task_attempt=item["expected_task_attempt"],
            new_attempt=item["new_attempt"],
        ) for item in tasks_raw)
        record = DockyardPlanRecord(
            schema_version=value["schema_version"],
            plan_id=value["plan_id"],
            project_id=value["project_id"],
            revision=value["revision"],
            phase=DockyardPlanPhase(value["phase"]),
            requirement=value["requirement"],
            requirement_digest=value["requirement_digest"],
            tasks=tasks,
            plan_digest=value["plan_digest"],
            previous_revision_digest=value["previous_revision_digest"],
            pm_owner_id=value["pm_owner_id"],
            created_at=value["created_at"],
            updated_at=value["updated_at"],
            approved_at=value["approved_at"],
            materialized_at=value["materialized_at"],
            last_operation_id=value["last_operation_id"],
            request_digest=value["request_digest"],
            content_digest=value["content_digest"],
        )
    except (KeyError, TypeError, ValueError, DockyardPlanInputError) as exc:
        raise DockyardPlanFilesystemError("plan record schema is invalid") from exc
    if _with_digest(replace(record, content_digest="0" * 64)).content_digest != record.content_digest:
        raise DockyardPlanFilesystemError("plan record digest is invalid")
    return record


def _reparse(path: Path) -> bool:
    metadata = path.lstat()
    attributes = getattr(metadata, "st_file_attributes", 0)
    return stat.S_ISLNK(metadata.st_mode) or bool(attributes & _FILE_ATTRIBUTE_REPARSE_POINT)


def _thread_lock(root: Path) -> threading.Lock:
    key = str(root)
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(key, threading.Lock())


@contextmanager
def _store_lock(root: Path) -> Iterator[None]:
    with _thread_lock(root):
        path = root / ".plan-store.lock"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_BINARY"):
            flags |= os.O_BINARY
        try:
            descriptor = os.open(path, flags, 0o600)
        except FileExistsError as exc:
            raise DockyardPlanConflictError("plan store lock is held") from exc
        payload = b"DOCKYARD-PLAN-STORE"
        try:
            os.write(descriptor, payload)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        try:
            yield
        finally:
            try:
                if path.is_file() and not _reparse(path) and path.read_bytes() == payload:
                    path.unlink()
            except OSError:
                pass


class DockyardPlanStore:
    def __init__(self, root: Path) -> None:
        if not isinstance(root, Path) or not root.is_absolute():
            raise DockyardPlanInputError("root must be an absolute Path")
        root.mkdir(parents=True, exist_ok=True)
        if not root.is_dir() or _reparse(root):
            raise DockyardPlanFilesystemError("plan store root is not plain")
        self._root = root

    @property
    def root(self) -> Path:
        return self._root

    def _directory(self, plan_id: str) -> Path:
        return self._root / _identifier(plan_id, "plan_id")

    def _path(self, plan_id: str, revision: int) -> Path:
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
            raise DockyardPlanInputError("revision is invalid")
        return self._directory(plan_id) / f"r{revision}.json"

    def read(self, plan_id: str, revision: int) -> DockyardPlanRecord | None:
        path = self._path(plan_id, revision)
        if not path.exists():
            return None
        if not path.is_file() or _reparse(path):
            raise DockyardPlanFilesystemError("plan record path is not plain")
        return _decode(path.read_bytes())

    def latest(self, plan_id: str) -> DockyardPlanRecord | None:
        directory = self._directory(plan_id)
        if not directory.exists():
            return None
        if not directory.is_dir() or _reparse(directory):
            raise DockyardPlanFilesystemError("plan directory is not plain")
        revisions: list[int] = []
        for path in directory.iterdir():
            match = re.fullmatch(r"r([1-9][0-9]*)\.json", path.name)
            if match is None or not path.is_file() or _reparse(path):
                raise DockyardPlanFilesystemError("plan directory contains an invalid entry")
            revisions.append(int(match.group(1)))
        if not revisions:
            return None
        if sorted(revisions) != list(range(1, max(revisions) + 1)):
            raise DockyardPlanFilesystemError("plan revisions are not contiguous")
        return self.read(plan_id, max(revisions))

    def current_session(self, base_plan_id: str) -> DockyardPlanRecord | None:
        """Recover the unique non-terminal session descended from a base ID."""
        base = _identifier(base_plan_id, "plan_id")
        candidates: list[DockyardPlanRecord] = []
        for path in self._root.iterdir():
            if path.name != base and not path.name.startswith(base + "-r"):
                continue
            if not path.is_dir() or _reparse(path):
                raise DockyardPlanFilesystemError("plan session path is not plain")
            record = self.latest(path.name)
            if record is not None and record.phase is not DockyardPlanPhase.MATERIALIZED:
                candidates.append(record)
        if len(candidates) > 1:
            raise DockyardPlanConflictError("multiple active plan sessions")
        return None if not candidates else candidates[0]

    def save(self, record: DockyardPlanRecord) -> DockyardPlanRecord:
        if not isinstance(record, DockyardPlanRecord):
            raise DockyardPlanInputError("record has an invalid type")
        sealed = _with_digest(record)
        data = _encode_object(_record_object(sealed, True))
        with _store_lock(self._root):
            directory = self._directory(sealed.plan_id)
            directory.mkdir(exist_ok=True)
            if _reparse(directory):
                raise DockyardPlanFilesystemError("plan directory is not plain")
            path = self._path(sealed.plan_id, sealed.revision)
            if path.exists():
                if not path.is_file() or _reparse(path):
                    raise DockyardPlanFilesystemError("plan record path is not plain")
                existing = path.read_bytes()
                if existing == data:
                    return _decode(existing)
                raise DockyardPlanConflictError("divergent plan revision replay")
            latest = self.latest(sealed.plan_id)
            if latest is None and sealed.revision != 1:
                raise DockyardPlanConflictError("first plan revision must be one")
            if latest is not None:
                if sealed.revision != latest.revision + 1:
                    raise DockyardPlanConflictError("plan revision CAS failed")
                if sealed.previous_revision_digest != latest.content_digest:
                    raise DockyardPlanConflictError("previous revision digest mismatch")
                if sealed.pm_owner_id != latest.pm_owner_id:
                    raise DockyardPlanConflictError("PM owner identity changed")
                if sealed.phase not in _ALLOWED_PHASE_TRANSITIONS[latest.phase]:
                    raise DockyardPlanConflictError("plan phase transition is not allowed")
            temporary = path.with_suffix(".tmp")
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            if hasattr(os, "O_BINARY"):
                flags |= os.O_BINARY
            try:
                descriptor = os.open(temporary, flags, 0o600)
            except FileExistsError as exc:
                raise DockyardPlanConflictError("plan temp file exists") from exc
            try:
                offset = 0
                while offset < len(data):
                    offset += os.write(descriptor, data[offset:])
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            os.replace(temporary, path)
            return sealed
