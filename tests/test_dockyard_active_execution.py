from __future__ import annotations

import inspect
import sys
import threading
import unittest
from dataclasses import fields, is_dataclass, replace
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "agentdesk" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import dockyard_active_execution as active


class DockyardActiveExecutionRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = active.DockyardActiveExecutionRegistry()
        self.identity = active.make_active_execution_identity(
            "TC-001", 1, 2, "DSP-001", "GEN-001", "2026-08-04T00:00:00Z",
        )

    @staticmethod
    def command(**changes: object) -> active.DockyardActiveCancellationCommand:
        values: dict[str, object] = {
            "operation_id": "OP-CANCEL-001", "task_id": "TC-001",
            "revision": 1, "attempt": 2, "dispatch_id": "DSP-001",
            "generation_id": "GEN-001", "expected_snapshot_commit": "a" * 40,
            "event_id": "EVT-CANCEL-001", "requested_at": "2026-08-04T00:00:01Z",
        }
        values.update(changes)
        return active.DockyardActiveCancellationCommand(**values)  # type: ignore[arg-type]

    @staticmethod
    def outcome() -> active.DockyardActiveCancellationOutcome:
        return active.DockyardActiveCancellationOutcome(
            "TC-001", "DSP-001", "EVT-CANCEL-001", "2026-08-04T00:00:02Z",
        )

    def test_public_evidence_is_frozen_slotted_and_precise(self) -> None:
        expected = {
            active.DockyardActiveExecutionIdentity: 8,
            active.DockyardActiveCancellationCommand: 9,
            active.DockyardActiveCancellationOutcome: 4,
            active.DockyardActiveRegistrationResult: 2,
        }
        for value, count in expected.items():
            self.assertTrue(is_dataclass(value))
            self.assertEqual(len(fields(value)), count)
            self.assertTrue(value.__dataclass_params__.frozen)
            self.assertTrue(hasattr(value, "__slots__"))
            annotations = value.__annotations__.values()
            self.assertFalse(any(item in {object, dict, list, set} for item in annotations))

    def test_registration_and_byte_exact_replay_keep_first_callback(self) -> None:
        calls: list[str] = []
        first = lambda command: (calls.append("first"), self.outcome())[1]
        second = lambda command: (calls.append("second"), self.outcome())[1]
        self.assertFalse(self.registry.register(self.identity, first).replayed)
        self.assertTrue(self.registry.register(self.identity, second).replayed)
        self.registry.cancel(self.command())
        self.assertEqual(calls, ["first"])

    def test_divergent_registration_is_rejected(self) -> None:
        self.registry.register(self.identity, lambda command: self.outcome())
        divergent = active.make_active_execution_identity(
            "TC-001", 1, 2, "DSP-001", "GEN-002", "2026-08-04T00:00:00Z",
        )
        with self.assertRaises(active.DockyardActiveExecutionConflictError):
            self.registry.register(divergent, lambda command: self.outcome())

    def test_cancel_identity_substitution_is_rejected_before_callback(self) -> None:
        calls = 0
        def callback(command: active.DockyardActiveCancellationCommand) -> active.DockyardActiveCancellationOutcome:
            nonlocal calls
            calls += 1
            return self.outcome()
        self.registry.register(self.identity, callback)
        for change in ({"task_id": "TC-EVIL"}, {"revision": 2}, {"attempt": 3}, {"generation_id": "GEN-EVIL"}):
            with self.subTest(change=change):
                with self.assertRaises(active.DockyardActiveExecutionConflictError):
                    self.registry.cancel(self.command(**change))
        self.assertEqual(calls, 0)

    def test_callback_runs_outside_registry_lock(self) -> None:
        observed: list[tuple[active.DockyardActiveExecutionIdentity, ...]] = []
        def callback(command: active.DockyardActiveCancellationCommand) -> active.DockyardActiveCancellationOutcome:
            observed.append(self.registry.snapshot())
            return self.outcome()
        self.registry.register(self.identity, callback)
        self.registry.cancel(self.command())
        self.assertEqual(observed, [(self.identity,)])

    def test_callback_failure_restores_active_registration(self) -> None:
        attempts = 0
        def callback(command: active.DockyardActiveCancellationCommand) -> active.DockyardActiveCancellationOutcome:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("private detail")
            return self.outcome()
        self.registry.register(self.identity, callback)
        with self.assertRaises(active.DockyardActiveExecutionCallbackError) as raised:
            self.registry.cancel(self.command())
        self.assertNotIn("private detail", str(raised.exception))
        self.assertEqual(self.registry.cancel(self.command()), self.outcome())

    def test_success_removes_registration(self) -> None:
        self.registry.register(self.identity, lambda command: self.outcome())
        self.registry.cancel(self.command())
        self.assertEqual(self.registry.snapshot(), ())
        with self.assertRaises(active.DockyardActiveExecutionNotFoundError):
            self.registry.cancel(self.command())

    def test_concurrent_cancel_has_one_winner(self) -> None:
        entered = threading.Event()
        release = threading.Event()
        outcomes: list[str] = []
        def callback(command: active.DockyardActiveCancellationCommand) -> active.DockyardActiveCancellationOutcome:
            entered.set()
            self.assertTrue(release.wait(2))
            return self.outcome()
        self.registry.register(self.identity, callback)
        def run() -> None:
            try:
                self.registry.cancel(self.command())
                outcomes.append("winner")
            except active.DockyardActiveExecutionConflictError:
                outcomes.append("fenced")
        first = threading.Thread(target=run)
        second = threading.Thread(target=run)
        first.start()
        self.assertTrue(entered.wait(2))
        second.start()
        second.join(2)
        release.set()
        first.join(2)
        self.assertEqual(sorted(outcomes), ["fenced", "winner"])

    def test_stale_generation_completion_does_not_remove_current(self) -> None:
        self.registry.register(self.identity, lambda command: self.outcome())
        self.assertFalse(self.registry.complete("DSP-001", "GEN-OLD"))
        self.assertEqual(self.registry.snapshot(), (self.identity,))
        self.assertTrue(self.registry.complete("DSP-001", "GEN-001"))
        self.assertEqual(self.registry.snapshot(), ())

    def test_source_has_no_process_probe_or_external_io(self) -> None:
        source = inspect.getsource(active)
        for forbidden in (
            "subprocess", "socket", "requests", "probe_dispatch", "probe_process",
            "get_current_process_identity", "os.environ", "stdout", "stderr",
        ):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
