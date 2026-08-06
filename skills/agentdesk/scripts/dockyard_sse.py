"""Bounded in-memory SSE projection stream for Dockyard loopback clients."""

from __future__ import annotations

import hashlib
import json
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime

__all__ = [
    "DockyardSseEvent",
    "DockyardSseError",
    "DockyardSseInputError",
    "DockyardSseCursorError",
    "DockyardSseCapacityError",
    "DockyardSseHub",
    "encode_sse_event",
]

_EVENT_TYPES = frozenset({
    "snapshot.changed",
    "task.changed",
    "run.changed",
    "approval.changed",
    "review.changed",
    "provider.changed",
    "project.changed",
    "health.changed",
})
_SHA40 = frozenset("0123456789abcdef")


class DockyardSseError(Exception):
    pass


class DockyardSseInputError(DockyardSseError):
    pass


class DockyardSseCursorError(DockyardSseError):
    pass


class DockyardSseCapacityError(DockyardSseError):
    pass


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise DockyardSseInputError(f"{field} must be a non-empty trimmed str")
    if any(ord(char) < 32 for char in value):
        raise DockyardSseInputError(f"{field} contains a control character")
    return value


def _timestamp(value: object) -> str:
    text = _text(value, "occurred_at")
    candidate = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise DockyardSseInputError("occurred_at must be RFC3339 UTC") from exc
    if parsed.utcoffset() is None or parsed.utcoffset().total_seconds() != 0:
        raise DockyardSseInputError("occurred_at must be RFC3339 UTC")
    return text


def _sha(value: object, length: int, field: str) -> str:
    if not isinstance(value, str) or len(value) != length or any(
        char not in _SHA40 for char in value
    ):
        raise DockyardSseInputError(f"{field} has an invalid digest")
    return value


@dataclass(frozen=True, slots=True)
class DockyardSseEvent:
    schema_version: str
    event_cursor: int
    project_id: str
    event_type: str
    snapshot_commit: str
    resource_id: str
    occurred_at: str
    content_digest: str

    def __post_init__(self) -> None:
        if self.schema_version != "dockyard.sse-event/v1":
            raise DockyardSseInputError("schema_version is invalid")
        if isinstance(self.event_cursor, bool) or not isinstance(self.event_cursor, int) or self.event_cursor < 1:
            raise DockyardSseInputError("event_cursor must be a positive int")
        _text(self.project_id, "project_id")
        if self.event_type not in _EVENT_TYPES:
            raise DockyardSseInputError("event_type is invalid")
        _sha(self.snapshot_commit, 40, "snapshot_commit")
        _text(self.resource_id, "resource_id")
        _timestamp(self.occurred_at)
        _sha(self.content_digest, 64, "content_digest")


def _content_digest(
    cursor: int,
    project_id: str,
    event_type: str,
    snapshot_commit: str,
    resource_id: str,
    occurred_at: str,
) -> str:
    payload = "\x1f".join((
        str(cursor), project_id, event_type, snapshot_commit, resource_id, occurred_at
    )).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def encode_sse_event(event: DockyardSseEvent) -> bytes:
    if not isinstance(event, DockyardSseEvent):
        raise DockyardSseInputError("event has an invalid type")
    data = json.dumps(asdict(event), ensure_ascii=False, separators=(",", ":"))
    return f"id: {event.event_cursor}\nevent: {event.event_type}\ndata: {data}\n\n".encode("utf-8")


class DockyardSseHub:
    def __init__(self, *, max_backlog: int = 256, max_streams: int = 8) -> None:
        if isinstance(max_backlog, bool) or not isinstance(max_backlog, int) or max_backlog < 1:
            raise DockyardSseInputError("max_backlog must be positive")
        if isinstance(max_streams, bool) or not isinstance(max_streams, int) or max_streams < 1:
            raise DockyardSseInputError("max_streams must be positive")
        self._max_backlog = max_backlog
        self._events: dict[str, tuple[DockyardSseEvent, ...]] = {}
        self._condition = threading.Condition()
        self._streams = threading.BoundedSemaphore(max_streams)

    def publish(
        self,
        *,
        project_id: str,
        event_type: str,
        snapshot_commit: str,
        resource_id: str,
        occurred_at: str,
    ) -> DockyardSseEvent:
        project_id = _text(project_id, "project_id")
        if event_type not in _EVENT_TYPES:
            raise DockyardSseInputError("event_type is invalid")
        snapshot_commit = _sha(snapshot_commit, 40, "snapshot_commit")
        resource_id = _text(resource_id, "resource_id")
        occurred_at = _timestamp(occurred_at)
        with self._condition:
            previous = self._events.get(project_id, ())
            cursor = previous[-1].event_cursor + 1 if previous else 1
            event = DockyardSseEvent(
                "dockyard.sse-event/v1",
                cursor,
                project_id,
                event_type,
                snapshot_commit,
                resource_id,
                occurred_at,
                _content_digest(
                    cursor,
                    project_id,
                    event_type,
                    snapshot_commit,
                    resource_id,
                    occurred_at,
                ),
            )
            self._events[project_id] = (previous + (event,))[-self._max_backlog:]
            self._condition.notify_all()
            return event

    def replay(self, project_id: str, after_cursor: int) -> tuple[DockyardSseEvent, ...]:
        project_id = _text(project_id, "project_id")
        if isinstance(after_cursor, bool) or not isinstance(after_cursor, int) or after_cursor < 0:
            raise DockyardSseInputError("after_cursor must be a non-negative int")
        with self._condition:
            events = self._events.get(project_id, ())
            if not events:
                if after_cursor:
                    raise DockyardSseCursorError("cursor is ahead of the stream")
                return ()
            if after_cursor > events[-1].event_cursor:
                raise DockyardSseCursorError("cursor is ahead of the stream")
            if after_cursor < events[0].event_cursor - 1:
                raise DockyardSseCursorError("cursor fell behind the bounded backlog")
            return tuple(event for event in events if event.event_cursor > after_cursor)

    def wait_for_events(
        self,
        project_id: str,
        after_cursor: int,
        timeout_seconds: float,
    ) -> tuple[DockyardSseEvent, ...]:
        if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)) or timeout_seconds <= 0:
            raise DockyardSseInputError("timeout_seconds must be positive")
        deadline = time.monotonic() + float(timeout_seconds)
        with self._condition:
            while True:
                events = self.replay(project_id, after_cursor)
                if events:
                    return events
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return ()
                self._condition.wait(remaining)

    def acquire_stream(self) -> None:
        if not self._streams.acquire(blocking=False):
            raise DockyardSseCapacityError("stream capacity is exhausted")

    def release_stream(self) -> None:
        self._streams.release()
