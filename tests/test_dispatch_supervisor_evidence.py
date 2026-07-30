"""TC-13.18d.12a-pre2 — Durable Dispatch Supervisor Evidence targeted tests.

Covers data model, monotonic phase progression, atomic write + temp cleanup,
symlink/reparse rejection, lock contention, three-state liveness, finalizer
exactly-once, cross-generation rejection, and no-sensitive-leak rules.

These are incremental targeted tests only — not the full suite.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCRIPTS = _REPO_ROOT / "skills" / "agentdesk" / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import dispatch_supervisor_evidence as dse  # noqa: E402


class _ReprMustNotBeCalled:
    """A malicious non-str value whose repr must never be invoked."""

    _repr_called = False

    def __repr__(self) -> str:  # pragma: no cover - must not run
        type(self)._repr_called = True
        return "EVIL"

    def __str__(self) -> str:  # pragma: no cover - must not run
        type(self)._repr_called = True
        return "EVIL"


def _kwargs(**overrides):
    base = dict(
        task_id="TC-001",
        revision=1,
        attempt=1,
        dispatch_id="DSP-test-0001",
        lease_epoch=1,
        holder_instance_id="inst-1",
        generation_id="GEN-test-0001",
        creator_pid=os.getpid(),
        creator_creation_time=dse.get_process_creation_time(os.getpid()) or "",
        boot_id=dse.get_boot_id(),
    )
    base.update(overrides)
    return base


class ReceiptDataModelTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="dse-model-")
        self.project = Path(self.tmp.name) / "project"
        (self.project / ".agentdesk" / "runtime").mkdir(parents=True)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_reserve_writes_reserved_phase(self) -> None:
        r = dse.reserve_receipt(self.project, **_kwargs())
        self.assertEqual(r.phase, "RESERVED")
        self.assertIsNone(r.supervisor_pid)
        self.assertIsNone(r.worker_pid)
        loaded = dse.read_dispatch_receipt(self.project, "DSP-test-0001")
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.phase, "RESERVED")  # type: ignore[union-attr]

    def test_receipt_rejects_supervisor_fields_before_ready(self) -> None:
        with self.assertRaises(dse.DispatchSupervisorValidationError):
            dse.DispatchProcessReceipt(
                schema_version=dse.SCHEMA_VERSION,
                task_id="TC-001", revision=1, attempt=1,
                dispatch_id="DSP-x", lease_epoch=1,
                holder_instance_id="inst", generation_id="GEN-x",
                platform="posix", boot_id="b", phase="RESERVED",
                creator_pid=1, creator_creation_time="c",
                supervisor_pid=2, supervisor_creation_time="s",  # illegal at RESERVED
                worker_pid=None, worker_creation_time=None,
                worker_process_group=None, written_at="2026-01-01T00:00:00Z",
            )

    def test_worker_fields_required_at_worker_started(self) -> None:
        with self.assertRaises(dse.DispatchSupervisorValidationError):
            dse.DispatchProcessReceipt(
                schema_version=dse.SCHEMA_VERSION,
                task_id="TC-001", revision=1, attempt=1,
                dispatch_id="DSP-x", lease_epoch=1,
                holder_instance_id="inst", generation_id="GEN-x",
                platform="posix", boot_id="b", phase="WORKER_STARTED",
                creator_pid=1, creator_creation_time="c",
                supervisor_pid=2, supervisor_creation_time="s",
                worker_pid=None,  # illegal at WORKER_STARTED
                worker_creation_time=None, worker_process_group=None,
                written_at="2026-01-01T00:00:00Z",
            )

    def test_unsafe_dispatch_id_rejected(self) -> None:
        with self.assertRaises(dse.DispatchSupervisorValidationError):
            dse.reserve_receipt(self.project, **_kwargs(dispatch_id="../escape"))


class PhaseProgressionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="dse-phase-")
        self.project = Path(self.tmp.name) / "project"
        (self.project / ".agentdesk" / "runtime").mkdir(parents=True)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _full_to_ready(self) -> None:
        dse.reserve_receipt(self.project, **_kwargs())
        dse.advance_to_supervisor_ready(
            self.project, dispatch_id="DSP-test-0001",
            generation_id="GEN-test-0001",
            supervisor_pid=os.getpid() + 1,
            supervisor_creation_time=dse.get_process_creation_time(os.getpid()) or "s",
        )

    def test_legal_forward_progression(self) -> None:
        self._full_to_ready()
        dse.advance_to_worker_started(
            self.project, dispatch_id="DSP-test-0001",
            generation_id="GEN-test-0001", worker_pid=os.getpid() + 2,
            worker_creation_time="wc", worker_process_group=9999,
        )
        dse.advance_to_finalizing(
            self.project, dispatch_id="DSP-test-0001",
            generation_id="GEN-test-0001",
        )
        r = dse.read_dispatch_receipt(self.project, "DSP-test-0001")
        self.assertEqual(r.phase, "FINALIZING")  # type: ignore[union-attr]

    def test_reserve_twice_rejected(self) -> None:
        dse.reserve_receipt(self.project, **_kwargs())
        with self.assertRaises(dse.DispatchSupervisorPhaseError):
            dse.reserve_receipt(self.project, **_kwargs())

    def test_jump_rejected(self) -> None:
        dse.reserve_receipt(self.project, **_kwargs())
        with self.assertRaises(dse.DispatchSupervisorPhaseError):
            dse.advance_to_worker_started(
                self.project, dispatch_id="DSP-test-0001",
                generation_id="GEN-test-0001", worker_pid=42,
                worker_creation_time="wc", worker_process_group=99,
            )

    def test_backward_rejected(self) -> None:
        self._full_to_ready()
        with self.assertRaises(dse.DispatchSupervisorPhaseError):
            dse.advance_to_supervisor_ready(
                self.project, dispatch_id="DSP-test-0001",
                generation_id="GEN-test-0001", supervisor_pid=42,
                supervisor_creation_time="s",
            )

    def test_cross_generation_rejected(self) -> None:
        dse.reserve_receipt(self.project, **_kwargs())
        with self.assertRaises(dse.DispatchSupervisorFencingError):
            dse.advance_to_supervisor_ready(
                self.project, dispatch_id="DSP-test-0001",
                generation_id="GEN-WRONG", supervisor_pid=42,
                supervisor_creation_time="s",
            )


class AtomicWriteAndLockTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="dse-aw-")
        self.project = Path(self.tmp.name) / "project"
        (self.project / ".agentdesk" / "runtime").mkdir(parents=True)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_temp_file_cleaned_up_on_failure(self) -> None:
        store = self.project / ".agentdesk" / "runtime" / "dispatch-supervisor"
        dse.reserve_receipt(self.project, **_kwargs())
        before = set(store.iterdir())
        # Force a failure: reserve the same dispatch again (PhaseError) —
        # the lock + any temp must be cleaned up.
        with self.assertRaises(dse.DispatchSupervisorPhaseError):
            dse.reserve_receipt(self.project, **_kwargs())
        after = set(store.iterdir())
        self.assertEqual(after, before, "temp or lock file leaked")

    def test_no_tmp_files_remain(self) -> None:
        store = self.project / ".agentdesk" / "runtime" / "dispatch-supervisor"
        for i in range(3):
            dse.reserve_receipt(self.project, **_kwargs(dispatch_id=f"DSP-t-{i:04d}"))
        leftover = [p for p in store.iterdir() if ".tmp-" in p.name]
        self.assertEqual(leftover, [])

    def test_lock_contention_immediate(self) -> None:
        with dse._exclusive_store_lock(self.project):
            with self.assertRaises(dse.DispatchSupervisorContentionError):
                with dse._exclusive_store_lock(self.project):
                    pass

    def test_symlink_receipt_rejected(self) -> None:
        store = self.project / ".agentdesk" / "runtime" / "dispatch-supervisor"
        store.mkdir(parents=True, exist_ok=True)
        target = store / "real.yaml"
        target.write_text("{}", encoding="utf-8")
        link = store / "DSP-symlink-0001.receipt.yaml"
        try:
            link.symlink_to(target)
        except OSError:
            self.skipTest("symlink creation requires elevated privileges on Windows")
        with self.assertRaises(dse.DispatchSupervisorValidationError):
            dse.read_dispatch_receipt(self.project, "DSP-symlink-0001")


class LivenessProbeTests(unittest.TestCase):
    def test_alive_self(self) -> None:
        pid = os.getpid()
        creation = dse.get_process_creation_time(pid)
        boot = dse.get_boot_id()
        if creation is None:
            self.skipTest("creation-time probe unavailable on this platform")
        self.assertEqual(
            dse.probe_process(pid, creation, boot), dse.ProcessLiveness.ALIVE
        )

    def test_dead_absent_pid(self) -> None:
        self.assertEqual(
            dse.probe_process(0, "c", dse.get_boot_id()),
            dse.ProcessLiveness.DEAD,
        )

    def test_unknown_wrong_creation(self) -> None:
        pid = os.getpid()
        self.assertEqual(
            dse.probe_process(pid, "wrong-creation", dse.get_boot_id()),
            dse.ProcessLiveness.UNKNOWN,
        )

    def test_unknown_wrong_boot(self) -> None:
        pid = os.getpid()
        creation = dse.get_process_creation_time(pid)
        if creation is None:
            self.skipTest("creation-time probe unavailable")
        self.assertEqual(
            dse.probe_process(pid, creation, "wrong-boot"),
            dse.ProcessLiveness.UNKNOWN,
        )

    def test_unknown_none_inputs(self) -> None:
        self.assertEqual(
            dse.probe_process(None, "c", "b"), dse.ProcessLiveness.UNKNOWN
        )

    def test_probe_pid_state_tri_state(self) -> None:
        self.assertEqual(dse._probe_pid_state(0), "absent")
        self.assertEqual(dse._probe_pid_state(os.getpid()), "present")


class FinalizerTombstoneTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="dse-tomb-")
        self.project = Path(self.tmp.name) / "project"
        (self.project / ".agentdesk" / "runtime").mkdir(parents=True)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _tombstone_kwargs(self, **overrides):
        base = dict(
            task_id="TC-001", revision=1, attempt=1,
            dispatch_id="DSP-t-0001", generation_id="GEN-t-0001",
            winner="completion", worker_done=True, heartbeat_done=True,
            release_completed=True, failure_kind=None,
        )
        base.update(overrides)
        return base

    def test_precondition_must_hold(self) -> None:
        with self.assertRaises(dse.DispatchSupervisorPhaseError):
            dse.write_finalizer_tombstone(self.project, **self._tombstone_kwargs(worker_done=False))

    def test_exactly_once_replay(self) -> None:
        first = dse.write_finalizer_tombstone(self.project, **self._tombstone_kwargs())
        second = dse.write_finalizer_tombstone(self.project, **self._tombstone_kwargs())
        self.assertEqual(first, second)
        self.assertEqual(first.finalized_at, second.finalized_at)

    def test_cross_generation_rejected(self) -> None:
        dse.write_finalizer_tombstone(self.project, **self._tombstone_kwargs())
        with self.assertRaises(dse.DispatchSupervisorFencingError):
            dse.write_finalizer_tombstone(
                self.project, **self._tombstone_kwargs(generation_id="GEN-WRONG")
            )

    def test_divergent_content_rejected(self) -> None:
        dse.write_finalizer_tombstone(self.project, **self._tombstone_kwargs())
        with self.assertRaises(dse.DispatchSupervisorPhaseError):
            dse.write_finalizer_tombstone(
                self.project, **self._tombstone_kwargs(winner="heartbeat")
            )

    def test_invalid_failure_kind_rejected(self) -> None:
        with self.assertRaises(dse.DispatchSupervisorValidationError):
            dse.write_finalizer_tombstone(
                self.project, **self._tombstone_kwargs(failure_kind="bogus")
            )


class NoLeakageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="dse-leak-")
        self.project = Path(self.tmp.name) / "project"
        (self.project / ".agentdesk" / "runtime").mkdir(parents=True)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_generation_value_not_in_exception(self) -> None:
        leak_gen = "GEN-LEAK-MARKER-4242"
        dse.reserve_receipt(self.project, **_kwargs(generation_id=leak_gen))
        with self.assertRaises(dse.DispatchSupervisorFencingError) as cm:
            dse.advance_to_supervisor_ready(
                self.project, dispatch_id="DSP-test-0001",
                generation_id="GEN-WRONG", supervisor_pid=42,
                supervisor_creation_time="s",
            )
        self.assertNotIn(leak_gen, str(cm.exception))

    def test_malicious_repr_not_executed(self) -> None:
        _ReprMustNotBeCalled._repr_called = False
        with self.assertRaises(dse.DispatchSupervisorValidationError):
            dse.reserve_receipt(
                self.project,
                task_id="TC-001", revision=1, attempt=1,
                dispatch_id=_ReprMustNotBeCalled(),  # type: ignore[arg-type]
                lease_epoch=1, holder_instance_id="inst",
                generation_id="GEN-x", creator_pid=1,
                creator_creation_time="c", boot_id="b",
            )
        self.assertFalse(_ReprMustNotBeCalled._repr_called)


if __name__ == "__main__":
    unittest.main()
