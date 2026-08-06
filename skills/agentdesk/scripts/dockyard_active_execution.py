"""Thread-safe in-process ownership for Dockyard active dispatches.

The registry deliberately stores no process identity and no durable state.  A
Runner restart therefore produces an empty registry; recovery remains owned by
the existing durable evidence path instead of guessing from a PID.
"""

from __future__ import annotations

import hashlib
import threading
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum

__all__ = [
    "DockyardActiveCancellationCommand",
    "DockyardActiveCancellationOutcome",
    "DockyardActiveExecutionCallbackError",
    "DockyardActiveExecutionConflictError",
    "DockyardActiveExecutionIdentity",
    "DockyardActiveExecutionInputError",
    "DockyardActiveExecutionNotFoundError",
    "DockyardActiveExecutionRegistry",
    "DockyardActiveExecutionState",
    "DockyardActiveRegistrationResult",
    "make_active_execution_identity",
]


SCHEMA_VERSION = "dockyard.active-execution/v1"


class DockyardActiveExecutionError(ValueError):
    pass


class DockyardActiveExecutionInputError(DockyardActiveExecutionError):
    pass


class DockyardActiveExecutionConflictError(DockyardActiveExecutionError):
    pass


class DockyardActiveExecutionNotFoundError(DockyardActiveExecutionError):
    pass


class DockyardActiveExecutionCallbackError(DockyardActiveExecutionError):
    pass


class DockyardActiveExecutionState(str, Enum):
    ACTIVE = "ACTIVE"
    CANCELLING = "CANCELLING"


def _text(value: object, field: str, maximum: int = 256) -> str:
    if type(value) is not str or not value or value != value.strip() or len(value) > maximum:
        raise DockyardActiveExecutionInputError(f"active_execution:{field}")
    if any(ord(character) < 32 for character in value):
        raise DockyardActiveExecutionInputError(f"active_execution:{field}")
    return value


def _positive(value: object, field: str) -> int:
    if type(value) is not int or value < 1:
        raise DockyardActiveExecutionInputError(f"active_execution:{field}")
    return value


def _sha(value: object, field: str) -> str:
    text = _text(value, field, 64)
    if len(text) != 64 or any(character not in "0123456789abcdef" for character in text):
        raise DockyardActiveExecutionInputError(f"active_execution:{field}")
    return text


def _identity_digest(values: tuple[str, ...]) -> str:
    framed = "".join(f"{len(value)}:{value}" for value in values)
    return hashlib.sha256(framed.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class DockyardActiveExecutionIdentity:
    schema_version: str
    task_id: str
    revision: int
    attempt: int
    dispatch_id: str
    generation_id: str
    registered_at: str
    content_digest: str

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise DockyardActiveExecutionInputError("active_execution:schema")
        _text(self.task_id, "task_id")
        _positive(self.revision, "revision")
        _positive(self.attempt, "attempt")
        _text(self.dispatch_id, "dispatch_id")
        _text(self.generation_id, "generation_id")
        _text(self.registered_at, "registered_at")
        digest = _sha(self.content_digest, "content_digest")
        expected = _identity_digest((
            self.schema_version, self.task_id, str(self.revision),
            str(self.attempt), self.dispatch_id, self.generation_id,
            self.registered_at,
        ))
        if digest != expected:
            raise DockyardActiveExecutionInputError("active_execution:digest")


def make_active_execution_identity(
    task_id: str,
    revision: int,
    attempt: int,
    dispatch_id: str,
    generation_id: str,
    registered_at: str,
) -> DockyardActiveExecutionIdentity:
    values = (
        SCHEMA_VERSION, task_id, str(revision), str(attempt), dispatch_id,
        generation_id, registered_at,
    )
    return DockyardActiveExecutionIdentity(
        SCHEMA_VERSION, task_id, revision, attempt, dispatch_id,
        generation_id, registered_at, _identity_digest(values),
    )


@dataclass(frozen=True, slots=True)
class DockyardActiveCancellationCommand:
    operation_id: str
    task_id: str
    revision: int
    attempt: int
    dispatch_id: str
    generation_id: str
    expected_snapshot_commit: str
    event_id: str
    requested_at: str

    def __post_init__(self) -> None:
        for field in ("operation_id", "task_id", "dispatch_id", "generation_id", "event_id", "requested_at"):
            _text(getattr(self, field), field)
        _positive(self.revision, "revision")
        _positive(self.attempt, "attempt")
        snapshot = _text(self.expected_snapshot_commit, "expected_snapshot_commit", 40)
        if len(snapshot) != 40 or any(character not in "0123456789abcdef" for character in snapshot):
            raise DockyardActiveExecutionInputError("active_execution:expected_snapshot_commit")


@dataclass(frozen=True, slots=True)
class DockyardActiveCancellationOutcome:
    task_id: str
    dispatch_id: str
    event_id: str
    completed_at: str

    def __post_init__(self) -> None:
        for field in ("task_id", "dispatch_id", "event_id", "completed_at"):
            _text(getattr(self, field), field)


@dataclass(frozen=True, slots=True)
class DockyardActiveRegistrationResult:
    identity: DockyardActiveExecutionIdentity
    replayed: bool

    def __post_init__(self) -> None:
        if type(self.identity) is not DockyardActiveExecutionIdentity or type(self.replayed) is not bool:
            raise DockyardActiveExecutionInputError("active_execution:registration_result")


CancellationCallback = Callable[
    [DockyardActiveCancellationCommand],
    DockyardActiveCancellationOutcome,
]


@dataclass(slots=True)
class _Registration:
    identity: DockyardActiveExecutionIdentity
    callback: CancellationCallback
    state: DockyardActiveExecutionState


class DockyardActiveExecutionRegistry:
    """Single-process registry with an exact one-winner cancellation gate."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: dict[str, _Registration] = {}

    def register(
        self,
        identity: DockyardActiveExecutionIdentity,
        callback: CancellationCallback,
    ) -> DockyardActiveRegistrationResult:
        if type(identity) is not DockyardActiveExecutionIdentity:
            raise DockyardActiveExecutionInputError("active_execution:identity")
        if not callable(callback):
            raise DockyardActiveExecutionInputError("active_execution:callback")
        with self._lock:
            existing = self._entries.get(identity.dispatch_id)
            if existing is not None:
                if existing.identity == identity:
                    return DockyardActiveRegistrationResult(existing.identity, True)
                raise DockyardActiveExecutionConflictError("active_execution:divergent_registration")
            self._entries[identity.dispatch_id] = _Registration(
                identity, callback, DockyardActiveExecutionState.ACTIVE,
            )
        return DockyardActiveRegistrationResult(identity, False)

    def snapshot(self) -> tuple[DockyardActiveExecutionIdentity, ...]:
        with self._lock:
            return tuple(
                self._entries[key].identity for key in sorted(self._entries)
            )

    def complete(self, dispatch_id: str, generation_id: str) -> bool:
        dispatch = _text(dispatch_id, "dispatch_id")
        generation = _text(generation_id, "generation_id")
        with self._lock:
            existing = self._entries.get(dispatch)
            if existing is None or existing.identity.generation_id != generation:
                return False
            del self._entries[dispatch]
            return True

    def cancel(
        self,
        command: DockyardActiveCancellationCommand,
    ) -> DockyardActiveCancellationOutcome:
        if type(command) is not DockyardActiveCancellationCommand:
            raise DockyardActiveExecutionInputError("active_execution:cancellation_command")
        with self._lock:
            registration = self._entries.get(command.dispatch_id)
            if registration is None:
                raise DockyardActiveExecutionNotFoundError("active_execution:not_found")
            identity = registration.identity
            if (
                command.task_id != identity.task_id
                or command.revision != identity.revision
                or command.attempt != identity.attempt
                or command.generation_id != identity.generation_id
            ):
                raise DockyardActiveExecutionConflictError("active_execution:identity_divergence")
            if registration.state is not DockyardActiveExecutionState.ACTIVE:
                raise DockyardActiveExecutionConflictError("active_execution:cancellation_in_progress")
            registration.state = DockyardActiveExecutionState.CANCELLING
            callback = registration.callback
        try:
            outcome = callback(command)
            if type(outcome) is not DockyardActiveCancellationOutcome:
                raise TypeError("callback returned the wrong outcome type")
            if outcome.task_id != identity.task_id or outcome.dispatch_id != identity.dispatch_id:
                raise ValueError("callback returned divergent identity")
        except Exception as exc:
            with self._lock:
                current = self._entries.get(command.dispatch_id)
                if current is registration:
                    current.state = DockyardActiveExecutionState.ACTIVE
            raise DockyardActiveExecutionCallbackError("active_execution:callback_failed") from exc
        with self._lock:
            current = self._entries.get(command.dispatch_id)
            if current is not registration:
                raise DockyardActiveExecutionConflictError("active_execution:registration_replaced")
            del self._entries[command.dispatch_id]
        return outcome
