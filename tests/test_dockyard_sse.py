"""TC-13.29f bounded SSE projection tests."""

from __future__ import annotations

import json
import sys
import threading
import unittest
from dataclasses import fields
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "agentdesk" / "scripts"
sys.path.insert(0, str(_SCRIPTS))
from dockyard_sse import (  # noqa: E402
    DockyardSseCapacityError,
    DockyardSseCursorError,
    DockyardSseEvent,
    DockyardSseHub,
    DockyardSseInputError,
    encode_sse_event,
)
sys.path.pop(0)


class DockyardSseTests(unittest.TestCase):
    def event(self, hub: DockyardSseHub, resource: str = "TASK-1") -> DockyardSseEvent:
        return hub.publish(
            project_id="PRJ-1",
            event_type="task.changed",
            snapshot_commit="a" * 40,
            resource_id=resource,
            occurred_at="2026-08-02T00:00:00Z",
        )

    def test_exact_eight_fields_and_frozen_slots(self) -> None:
        self.assertEqual(
            tuple(field.name for field in fields(DockyardSseEvent)),
            (
                "schema_version",
                "event_cursor",
                "project_id",
                "event_type",
                "snapshot_commit",
                "resource_id",
                "occurred_at",
                "content_digest",
            ),
        )
        self.assertTrue(hasattr(DockyardSseEvent, "__slots__"))

    def test_publish_is_monotonic(self) -> None:
        hub = DockyardSseHub()
        first = self.event(hub, "TASK-1")
        second = self.event(hub, "TASK-2")
        self.assertEqual((first.event_cursor, second.event_cursor), (1, 2))
        self.assertNotEqual(first.content_digest, second.content_digest)

    def test_replay_after_cursor(self) -> None:
        hub = DockyardSseHub()
        self.event(hub, "TASK-1")
        second = self.event(hub, "TASK-2")
        self.assertEqual(hub.replay("PRJ-1", 1), (second,))

    def test_ahead_cursor_is_rejected(self) -> None:
        hub = DockyardSseHub()
        self.event(hub)
        with self.assertRaises(DockyardSseCursorError):
            hub.replay("PRJ-1", 2)

    def test_cursor_behind_backlog_is_rejected(self) -> None:
        hub = DockyardSseHub(max_backlog=1)
        self.event(hub, "TASK-1")
        self.event(hub, "TASK-2")
        with self.assertRaises(DockyardSseCursorError):
            hub.replay("PRJ-1", 0)

    def test_stream_capacity_is_typed(self) -> None:
        hub = DockyardSseHub(max_streams=1)
        hub.acquire_stream()
        try:
            with self.assertRaises(DockyardSseCapacityError):
                hub.acquire_stream()
        finally:
            hub.release_stream()

    def test_wait_wakes_on_publish_without_polling(self) -> None:
        hub = DockyardSseHub()
        result: list[tuple[DockyardSseEvent, ...]] = []
        started = threading.Event()

        def waiter() -> None:
            started.set()
            result.append(hub.wait_for_events("PRJ-1", 0, 1.0))

        thread = threading.Thread(target=waiter)
        thread.start()
        self.assertTrue(started.wait(timeout=1))
        event = self.event(hub)
        thread.join(timeout=2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result, [(event,)])

    def test_encode_is_valid_sse_and_json(self) -> None:
        encoded = encode_sse_event(self.event(DockyardSseHub()))
        lines = encoded.decode("utf-8").splitlines()
        self.assertEqual(lines[0], "id: 1")
        self.assertEqual(lines[1], "event: task.changed")
        self.assertEqual(json.loads(lines[2][6:])["project_id"], "PRJ-1")

    def test_unknown_event_type_is_rejected(self) -> None:
        with self.assertRaises(DockyardSseInputError):
            DockyardSseHub().publish(
                project_id="PRJ-1",
                event_type="shell.changed",
                snapshot_commit="a" * 40,
                resource_id="R-1",
                occurred_at="2026-08-02T00:00:00Z",
            )

    def test_bool_cursor_is_rejected(self) -> None:
        with self.assertRaises(DockyardSseInputError):
            DockyardSseHub().replay("PRJ-1", True)


if __name__ == "__main__":
    unittest.main()
