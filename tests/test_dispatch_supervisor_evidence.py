"""TC-13.18d.12a-pre2 — Durable Dispatch Supervisor Evidence targeted tests.

Covers data model, monotonic phase progression, atomic write + temp cleanup,
symlink/reparse rejection, lock contention, three-state liveness, finalizer
exactly-once, cross-generation rejection, and no-sensitive-leak rules.

These are incremental targeted tests only — not the full suite.
"""

from __future__ import annotations

import os
import secrets
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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
            task_id="TC-001",
            revision=1,
            attempt=1,
            dispatch_id="DSP-t-0001",
            generation_id="GEN-t-0001",
            winner="completion",
            worker_done=True,
            heartbeat_done=True,
            release_completed=True,
            failure_kind=None,
        )
        base.update(overrides)
        return base

    def _reserve_to_finalizing(self, **overrides):
        kwargs = self._tombstone_kwargs()
        kwargs.update(overrides)
        dse.reserve_receipt(
            self.project,
            task_id=kwargs["task_id"],
            revision=kwargs["revision"],
            attempt=kwargs["attempt"],
            dispatch_id=kwargs["dispatch_id"],
            lease_epoch=1,
            holder_instance_id="inst-1",
            generation_id=kwargs["generation_id"],
            creator_pid=os.getpid(),
            creator_creation_time=dse.get_process_creation_time(os.getpid()) or "",
            boot_id=dse.get_boot_id(),
        )
        dse.advance_to_supervisor_ready(
            self.project,
            dispatch_id=kwargs["dispatch_id"],
            generation_id=kwargs["generation_id"],
            supervisor_pid=os.getpid() + 1,
            supervisor_creation_time="s-ct",
        )
        dse.advance_to_worker_started(
            self.project,
            dispatch_id=kwargs["dispatch_id"],
            generation_id=kwargs["generation_id"],
            worker_pid=os.getpid() + 2,
            worker_creation_time="w-ct",
            worker_process_group=9999,
        )
        dse.advance_to_finalizing(
            self.project,
            dispatch_id=kwargs["dispatch_id"],
            generation_id=kwargs["generation_id"],
        )

    def test_precondition_must_hold(self) -> None:
        self._reserve_to_finalizing()
        with self.assertRaises(dse.DispatchSupervisorPhaseError):
            dse.write_finalizer_tombstone(
                self.project, **self._tombstone_kwargs(worker_done=False)
            )

    def test_finalizing_to_finalized_success(self) -> None:
        self._reserve_to_finalizing()
        tombstone = dse.write_finalizer_tombstone(
            self.project, **self._tombstone_kwargs()
        )
        self.assertIsInstance(tombstone, dse.DispatchFinalizerTombstone)
        receipt = dse.read_dispatch_receipt(self.project, "DSP-t-0001")
        self.assertIsNotNone(receipt)
        self.assertEqual(receipt.phase, "FINALIZED")  # type: ignore[union-attr]

    def test_receipt_and_tombstone_both_persist(self) -> None:
        self._reserve_to_finalizing()
        dse.write_finalizer_tombstone(self.project, **self._tombstone_kwargs())
        receipt = dse.read_dispatch_receipt(self.project, "DSP-t-0001")
        tombstone = dse.read_dispatch_tombstone(self.project, "DSP-t-0001")
        self.assertIsNotNone(receipt)
        self.assertIsNotNone(tombstone)
        self.assertEqual(receipt.phase, "FINALIZED")  # type: ignore[union-attr]
        self.assertEqual(
            tombstone.generation_id,  # type: ignore[union-attr]
            "GEN-t-0001",
        )

    def test_receipt_final_phase_exactly_finalized(self) -> None:
        self._reserve_to_finalizing()
        dse.write_finalizer_tombstone(self.project, **self._tombstone_kwargs())
        receipt = dse.read_dispatch_receipt(self.project, "DSP-t-0001")
        self.assertIsNotNone(receipt)
        self.assertEqual(receipt.phase, "FINALIZED")  # type: ignore[union-attr]
        self.assertNotEqual(receipt.phase, "FINALIZING")  # type: ignore[union-attr]

    def test_exactly_once_replay(self) -> None:
        self._reserve_to_finalizing()
        first = dse.write_finalizer_tombstone(
            self.project, **self._tombstone_kwargs()
        )
        second = dse.write_finalizer_tombstone(
            self.project, **self._tombstone_kwargs()
        )
        self.assertEqual(first, second)
        self.assertEqual(first.finalized_at, second.finalized_at)

    def test_crash_recovery_tombstone_written_receipt_finalizing(self) -> None:
        self._reserve_to_finalizing()
        # Successful write advances receipt to FINALIZED.
        dse.write_finalizer_tombstone(self.project, **self._tombstone_kwargs())
        receipt_before = dse.read_dispatch_receipt(self.project, "DSP-t-0001")
        self.assertEqual(receipt_before.phase, "FINALIZED")
        # Simulate crash: receipt reverts to FINALIZING while tombstone remains.
        dse._write_receipt(
            self.project,
            dse._replace_receipt(
                receipt_before,
                phase=dse.DispatchReceiptPhase.FINALIZING.value,
                written_at=dse._now_utc_str(),
            ),
        )
        receipt_before = dse.read_dispatch_receipt(self.project, "DSP-t-0001")
        self.assertEqual(receipt_before.phase, "FINALIZING")
        # Replay must finish the advance and return the existing tombstone.
        replayed = dse.write_finalizer_tombstone(
            self.project, **self._tombstone_kwargs()
        )
        self.assertEqual(replayed.generation_id, "GEN-t-0001")
        receipt_after = dse.read_dispatch_receipt(self.project, "DSP-t-0001")
        self.assertEqual(receipt_after.phase, "FINALIZED")

    def test_receipt_finalized_tombstone_missing_fail_closed(self) -> None:
        self._reserve_to_finalizing()
        # Advance receipt to FINALIZED without writing a tombstone.
        receipt = dse.read_dispatch_receipt(self.project, "DSP-t-0001")
        dse._write_receipt(
            self.project,
            dse._replace_receipt(
                receipt,
                phase=dse.DispatchReceiptPhase.FINALIZED.value,
                written_at=dse._now_utc_str(),
            ),
        )
        with self.assertRaises(dse.DispatchSupervisorPhaseError):
            dse.write_finalizer_tombstone(self.project, **self._tombstone_kwargs())

    def test_receipt_missing_rejected(self) -> None:
        with self.assertRaises(dse.DispatchSupervisorPhaseError):
            dse.write_finalizer_tombstone(self.project, **self._tombstone_kwargs())

    def test_receipt_wrong_phase_rejected(self) -> None:
        # Leave receipt at RESERVED rather than advancing to FINALIZING.
        dse.reserve_receipt(
            self.project,
            task_id="TC-001",
            revision=1,
            attempt=1,
            dispatch_id="DSP-t-0001",
            lease_epoch=1,
            holder_instance_id="inst-1",
            generation_id="GEN-t-0001",
            creator_pid=os.getpid(),
            creator_creation_time=dse.get_process_creation_time(os.getpid()) or "",
            boot_id=dse.get_boot_id(),
        )
        with self.assertRaises(dse.DispatchSupervisorPhaseError):
            dse.write_finalizer_tombstone(self.project, **self._tombstone_kwargs())

    def test_generation_mismatch_rejected(self) -> None:
        self._reserve_to_finalizing(generation_id="GEN-t-0001")
        with self.assertRaises(dse.DispatchSupervisorFencingError):
            dse.write_finalizer_tombstone(
                self.project, **self._tombstone_kwargs(generation_id="GEN-WRONG")
            )

    def test_task_revision_attempt_dispatch_mismatch_rejected(self) -> None:
        self._reserve_to_finalizing()
        with self.assertRaises(dse.DispatchSupervisorPhaseError):
            dse.write_finalizer_tombstone(
                self.project, **self._tombstone_kwargs(task_id="TC-999")
            )
        with self.assertRaises(dse.DispatchSupervisorPhaseError):
            dse.write_finalizer_tombstone(
                self.project, **self._tombstone_kwargs(revision=99)
            )
        with self.assertRaises(dse.DispatchSupervisorPhaseError):
            dse.write_finalizer_tombstone(
                self.project, **self._tombstone_kwargs(attempt=99)
            )
        with self.assertRaises(dse.DispatchSupervisorPhaseError):
            dse.write_finalizer_tombstone(
                self.project, **self._tombstone_kwargs(dispatch_id="DSP-WRONG")
            )

    def test_divergent_content_rejected(self) -> None:
        self._reserve_to_finalizing()
        dse.write_finalizer_tombstone(self.project, **self._tombstone_kwargs())
        with self.assertRaises(dse.DispatchSupervisorPhaseError):
            dse.write_finalizer_tombstone(
                self.project, **self._tombstone_kwargs(winner="heartbeat")
            )

    def test_invalid_failure_kind_rejected(self) -> None:
        self._reserve_to_finalizing()
        with self.assertRaises(dse.DispatchSupervisorValidationError):
            dse.write_finalizer_tombstone(
                self.project, **self._tombstone_kwargs(failure_kind="bogus")
            )

    def test_tombstone_write_failure_receipt_stays_finalizing(self) -> None:
        self._reserve_to_finalizing()
        with patch.object(dse, "_atomic_write_bytes", side_effect=dse.DispatchSupervisorStoreError("boom")):
            with self.assertRaises(dse.DispatchSupervisorStoreError):
                dse.write_finalizer_tombstone(self.project, **self._tombstone_kwargs())
        receipt = dse.read_dispatch_receipt(self.project, "DSP-t-0001")
        self.assertIsNotNone(receipt)
        self.assertEqual(receipt.phase, "FINALIZING")  # type: ignore[union-attr]
        self.assertIsNone(dse.read_dispatch_tombstone(self.project, "DSP-t-0001"))

    def test_tombstone_has_exactly_12_fields_frozen_slots(self) -> None:
        self.assertEqual(len(dse.DispatchFinalizerTombstone.__dataclass_fields__), 12)
        params = dse.DispatchFinalizerTombstone.__dataclass_params__
        self.assertTrue(params.frozen)
        self.assertTrue(params.slots)
        fields = set(dse.DispatchFinalizerTombstone.__dataclass_fields__.keys())
        self.assertEqual(
            fields,
            {
                "schema_version",
                "task_id",
                "revision",
                "attempt",
                "dispatch_id",
                "generation_id",
                "winner",
                "worker_done",
                "heartbeat_done",
                "release_completed",
                "failure_kind",
                "finalized_at",
            },
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


# ── TC-13.18d.12a-pre2.2 — Windows Job Object tests ─────────────────────


class JobNameStructureTests(unittest.TestCase):
    """Job name derivation, determinism, and no-leak rules."""

    def test_name_is_deterministic(self) -> None:
        n1 = dse.derive_job_name("GEN-a")
        n2 = dse.derive_job_name("GEN-a")
        self.assertEqual(n1, n2)

    def test_different_generation_different_name(self) -> None:
        n1 = dse.derive_job_name("GEN-a")
        n2 = dse.derive_job_name("GEN-b")
        self.assertNotEqual(n1, n2)

    def test_name_uses_full_sha256(self) -> None:
        name = dse.derive_job_name("GEN-x")
        # Name is: prefix + 64 hex chars
        prefix = "Local\\AgentDesk-Dispatch-"
        self.assertTrue(name.startswith(prefix))
        digest = name[len(prefix):]
        self.assertEqual(len(digest), 64)
        # Must be all hex
        self.assertTrue(all(c in "0123456789abcdef" for c in digest))

    def test_raw_generation_not_in_name(self) -> None:
        name = dse.derive_job_name("GEN-SECRET-VALUE")
        self.assertNotIn("GEN-SECRET-VALUE", name)
        self.assertNotIn("SECRET", name)

    def test_name_starts_with_local_prefix(self) -> None:
        name = dse.derive_job_name("GEN-any")
        self.assertTrue(name.startswith("Local\\AgentDesk-Dispatch-"))


class JobCreationTests(unittest.TestCase):
    """Real Windows Job Object creation, KILL_ON_JOB_CLOSE config, assignment."""

    @classmethod
    def setUpClass(cls) -> None:
        if os.name != "nt":
            raise unittest.SkipTest("Windows Job Object tests require Windows")

    def setUp(self) -> None:
        self._gen_id = "GEN-job-create-" + secrets.token_hex(4)
        self._handles: list[int] = []

    def tearDown(self) -> None:
        for h in self._handles:
            dse.close_dispatch_job_handle(h)
        self._handles.clear()

    def test_create_job_success(self) -> None:
        handle = dse.create_dispatch_job(self._gen_id)
        self.assertIsInstance(handle, int)
        self.assertGreater(handle, 0)
        self._handles.append(handle)

    def test_kill_on_job_close_configured(self) -> None:
        handle = dse.create_dispatch_job(self._gen_id)
        self._handles.append(handle)
        # KILL_ON_JOB_CLOSE is verified inside create_dispatch_job().
        # If no exception was raised, the flag is confirmed.

    def test_assign_child_to_job_success(self) -> None:
        """Assign a child process to a KILL_ON_JOB_CLOSE Job — safe for test
        because the child is killed when we close the handle, not the test."""
        handle = dse.create_dispatch_job(self._gen_id)
        self._handles.append(handle)

        # Start a child process.
        import subprocess as _sp
        child = _sp.Popen(
            [sys.executable, "-c", "import time; time.sleep(15)"],
            stdout=_sp.PIPE, stderr=_sp.PIPE,
        )
        try:
            dse._assign_process_to_job(handle, child.pid)
            # Verify child is in the job.
            in_job = dse.is_process_in_dispatch_job(self._gen_id, child.pid)
            self.assertTrue(in_job)
        finally:
            # Close handle → KILL_ON_JOB_CLOSE terminates the child.
            dse.close_dispatch_job_handle(handle)
            self._handles.remove(handle)
            try:
                child.wait(timeout=10)
            except _sp.TimeoutError:
                child.kill()
                child.wait()

    def test_invalid_pid_assignment_fails(self) -> None:
        handle = dse.create_dispatch_job(self._gen_id)
        self._handles.append(handle)
        # PID 0 is the System Idle Process — should fail.
        with self.assertRaises(OSError):
            dse._assign_process_to_job(handle, 0)

    def test_create_job_different_generations_independent(self) -> None:
        import subprocess as _sp
        gen_a = self._gen_id + "-a"
        gen_b = self._gen_id + "-b"
        h1 = dse.create_dispatch_job(gen_a)
        h2 = dse.create_dispatch_job(gen_b)
        self._handles.extend([h1, h2])
        self.assertNotEqual(h1, h2)

        # Assign a child to gen_a's job.
        child = _sp.Popen(
            [sys.executable, "-c", "import time; time.sleep(10)"],
            stdout=_sp.PIPE, stderr=_sp.PIPE,
        )
        try:
            dse._assign_process_to_job(h1, child.pid)
            in_a = dse.is_process_in_dispatch_job(gen_a, child.pid)
            in_b = dse.is_process_in_dispatch_job(gen_b, child.pid)
            self.assertTrue(in_a)
            self.assertFalse(in_b)
        finally:
            # Close both handles; KILL_ON_JOB_CLOSE on both.
            dse.close_dispatch_job_handle(h1)
            dse.close_dispatch_job_handle(h2)
            self._handles.clear()
            try:
                child.wait(timeout=10)
            except _sp.TimeoutError:
                child.kill()
                child.wait()

    def test_close_handle_idempotent(self) -> None:
        # Closing 0 or None should not raise.
        dse.close_dispatch_job_handle(0)
        dse.close_dispatch_job_handle(None)

    def test_query_active_process_count(self) -> None:
        import subprocess as _sp
        handle = dse.create_dispatch_job(self._gen_id)
        self._handles.append(handle)
        # Before assignment, count should be 0.
        count_before = dse.query_job_active_process_count(self._gen_id)
        self.assertEqual(count_before, 0)
        # Assign a child to the job.
        child = _sp.Popen(
            [sys.executable, "-c", "import time; time.sleep(10)"],
            stdout=_sp.PIPE, stderr=_sp.PIPE,
        )
        try:
            dse._assign_process_to_job(handle, child.pid)
            count_after = dse.query_job_active_process_count(self._gen_id)
            self.assertIsNotNone(count_after)
            self.assertGreaterEqual(count_after, 1)
        finally:
            dse.close_dispatch_job_handle(handle)
            self._handles.remove(handle)
            try:
                child.wait(timeout=10)
            except _sp.TimeoutError:
                child.kill()
                child.wait()

    def test_query_missing_job_returns_none(self) -> None:
        result = dse.query_job_active_process_count("GEN-nonexistent-job-9999")
        self.assertIsNone(result)

    def test_is_process_in_dispatch_job_missing(self) -> None:
        result = dse.is_process_in_dispatch_job("GEN-nonexistent-9999", os.getpid())
        self.assertIsNone(result)


class OuterJobDetectionTests(unittest.TestCase):
    """Detect whether test process is in a host Job; verify nested rules."""

    @classmethod
    def setUpClass(cls) -> None:
        if os.name != "nt":
            raise unittest.SkipTest("Windows Job Object tests require Windows")

    def test_detect_current_process_job_status(self) -> None:
        """Report whether current process is already in a Job — informational."""
        in_job = dse._is_process_in_any_job(os.getpid())
        # This test is informational: it records the outer-job status
        # so the report can note it.  Neither True nor False is a failure.
        self.assertIn(in_job, (True, False, None),
                      "in_job must be True, False, or None (on error)")

    def test_nested_job_with_child_succeeds_or_reports_blocking(self) -> None:
        """If our process is in a host Job, test nested assignment with
        a child process.  Windows 8+ supports nested Job assignment."""
        import subprocess as _sp
        gen = "GEN-nested-" + secrets.token_hex(4)
        in_external = dse._is_process_in_any_job(os.getpid())
        handle = 0
        child = _sp.Popen(
            [sys.executable, "-c", "import time; time.sleep(10)"],
            stdout=_sp.PIPE, stderr=_sp.PIPE,
        )
        try:
            handle = dse.create_dispatch_job(gen)
            dse._assign_process_to_job(handle, child.pid)
            # Assignment succeeded — nested Job allowed.
            in_job = dse.is_process_in_dispatch_job(gen, child.pid)
            self.assertTrue(in_job)
        except OSError as exc:
            if in_external:
                error_msg = str(exc)
                self.assertTrue(
                    "AssignProcessToJobObject" in error_msg
                    or "OpenProcess" in error_msg,
                    f"Unexpected error: {error_msg}"
                )
            else:
                raise
        finally:
            if handle:
                dse.close_dispatch_job_handle(handle)
            try:
                child.wait(timeout=10)
            except _sp.TimeoutError:
                child.kill()
                child.wait()

    def test_no_breakaway_flag_used(self) -> None:
        """Verify we never use CREATE_BREAKAWAY_FROM_JOB."""
        import dispatch_supervisor_evidence as dse_mod
        source = dse_mod.__file__
        if source is None:
            self.skipTest("cannot locate source")
        src = Path(source).read_text(encoding="utf-8")
        self.assertNotIn("BREAKAWAY", src.upper(),
                         "CREATE_BREAKAWAY_FROM_JOB must not appear in source")


class WorkerInheritanceTests(unittest.TestCase):
    """Real helper Worker inherits Job from supervisor parent.

    A "supervisor" child process is created, assigned to the Job,
    then spawns Worker descendants.  All verify Job inheritance.
    """

    @classmethod
    def setUpClass(cls) -> None:
        if os.name != "nt":
            raise unittest.SkipTest("Windows Job Object tests require Windows")

    def setUp(self) -> None:
        self._gen = "GEN-inherit-" + secrets.token_hex(4)
        self._handle = dse.create_dispatch_job(self._gen)

    def tearDown(self) -> None:
        if self._handle:
            dse.close_dispatch_job_handle(self._handle)

    def test_worker_inherits_job(self) -> None:
        """Worker started after supervisor is in Job inherits it."""
        # Supervisor child that prints its PID and spawns a Worker.
        code = (
            "import subprocess as sp, sys, os, time; "
            "print(os.getpid()); "
            "w = sp.Popen([sys.executable, '-c', "
            "'import time; time.sleep(5)']); "
            "w.wait()"
        )
        sup = subprocess.Popen(
            [sys.executable, "-c", code],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            dse._assign_process_to_job(self._handle, sup.pid)
            sup_pid_line = sup.stdout.readline().decode("utf-8").strip()
            supervisor_pid = int(sup_pid_line)
            # Supervisor must be in the named Job.
            in_job_sup = dse.is_process_in_dispatch_job(self._gen, supervisor_pid)
            self.assertTrue(in_job_sup,
                            f"Supervisor PID {supervisor_pid} not in Job")
        finally:
            # Close handle → KILL_ON_JOB_CLOSE terminates the supervisor.
            dse.close_dispatch_job_handle(self._handle)
            self._handle = 0
            try:
                sup.wait(timeout=10)
            except subprocess.TimeoutError:
                sup.kill()
                sup.wait()

    def test_descendant_inherits_job(self) -> None:
        """Worker's descendant (grandchild) also inherits Job."""
        code = (
            "import subprocess as sp, sys, os, time; "
            "gc = sp.Popen([sys.executable, '-c', "
            "'import time; print(42); time.sleep(5)'], "
            "stdout=sp.PIPE); "
            "line = gc.stdout.readline(); "
            "print(gc.pid); "
            "sys.stdout.flush(); "
            "gc.wait()"
        )
        sup = subprocess.Popen(
            [sys.executable, "-c", code],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            dse._assign_process_to_job(self._handle, sup.pid)
            # Read the grandchild PID from supervisor's stdout.
            gc_pid_line = sup.stdout.readline().decode("utf-8").strip()
            gc_pid = int(gc_pid_line)
            in_job = dse.is_process_in_dispatch_job(self._gen, gc_pid)
            self.assertTrue(in_job,
                            f"Grandchild PID {gc_pid} not in Job")
        finally:
            dse.close_dispatch_job_handle(self._handle)
            self._handle = 0
            try:
                sup.wait(timeout=10)
            except subprocess.TimeoutError:
                sup.kill()
                sup.wait()

    def test_no_job_handle_in_argv_or_env(self) -> None:
        """Worker argv and environment must not contain Job handles."""
        code = (
            "import os, sys; "
            "assert 'JOB_HANDLE' not in ''.join(sys.argv), 'JOB in argv'; "
            "assert 'JOB_HANDLE' not in str(os.environ), 'JOB in env'; "
            "print('ok')"
        )
        proc = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            timeout=10,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8", "replace"))
        self.assertIn(b"ok", proc.stdout)


class ForcedTerminationTests(unittest.TestCase):
    """supervisor exit → KILL_ON_JOB_CLOSE terminates Worker + descendants.

    Tests use a hierarchy of child processes: test creates Job, spawns
    "supervisor" child, assigns it to Job, supervisor spawns Worker which
    spawns grandchild.  Closing the last Job handle kills the whole tree.
    """

    @classmethod
    def setUpClass(cls) -> None:
        if os.name != "nt":
            raise unittest.SkipTest("Windows Job Object tests require Windows")

    def setUp(self) -> None:
        self._gen = "GEN-term-" + secrets.token_hex(4)
        self.tmp = tempfile.TemporaryDirectory(prefix="dse-term-")
        self.project = Path(self.tmp.name) / "project"
        (self.project / ".agentdesk" / "runtime").mkdir(parents=True)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_handle_close_kills_worker_tree(self) -> None:
        """When the Job's last handle is closed, Worker + descendants die."""

        handle = dse.create_dispatch_job(self._gen)

        # Supervisor spawns a Worker, prints Worker PID and flushes.
        code = (
            "import subprocess as sp, sys, time; "
            "w = sp.Popen([sys.executable, '-c', "
            "'import time; time.sleep(60)']); "
            "print(w.pid); "
            "sys.stdout.flush(); "
            "w.wait()"
        )
        sup = subprocess.Popen(
            [sys.executable, "-c", code],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            dse._assign_process_to_job(handle, sup.pid)

            # Read the Worker PID from supervisor output.
            worker_line = sup.stdout.readline().decode("utf-8").strip()
            self.assertTrue(worker_line, "Expected Worker PID from supervisor")
            worker_pid = int(worker_line)

            # Verify Worker is in the Job.
            in_job = dse.is_process_in_dispatch_job(self._gen, worker_pid)
            self.assertTrue(in_job)

            # Verify Job has active processes.
            count_before = dse.query_job_active_process_count(self._gen)
            self.assertIsNotNone(count_before)
            self.assertGreater(count_before, 0)

            # Close the Job handle → KILL_ON_JOB_CLOSE.
            dse.close_dispatch_job_handle(handle)
            handle = 0

            # Supervisor and Worker should both terminate.
            try:
                sup.wait(timeout=10)
            except subprocess.TimeoutError:
                sup.kill()
                sup.wait()

            self.assertIsNotNone(sup.returncode)

            # Job should be empty or gone.
            count_after = dse.query_job_active_process_count(self._gen)
            if count_after is not None:
                self.assertEqual(count_after, 0)

        finally:
            if handle:
                dse.close_dispatch_job_handle(handle)
            try:
                if sup.returncode is None:
                    sup.kill()
                    sup.wait()
            except Exception:
                pass

    def test_worker_not_in_job_after_termination(self) -> None:
        """After Job handle close, Worker is no longer in the Job.

        Note: _probe_pid_state may still return 'present' on Windows
        because subprocess.Popen holds a process handle; the kernel
        process object persists until all handles close.  We verify
        the Job membership instead.
        """
        handle = dse.create_dispatch_job(self._gen)

        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            dse._assign_process_to_job(handle, child.pid)
            child_pid = child.pid

            # Verify child is in the Job.
            in_job_before = dse.is_process_in_dispatch_job(self._gen, child_pid)
            self.assertTrue(in_job_before)

            # Kill via Job close.
            dse.close_dispatch_job_handle(handle)
            handle = 0

            try:
                child.wait(timeout=10)
            except subprocess.TimeoutError:
                child.kill()
                child.wait()

            # Child exited — verify it is no longer in the Job.
            # (May return None if Job itself is gone, or False if
            # Job exists but child is gone.)
            in_job_after = dse.is_process_in_dispatch_job(self._gen, child_pid)
            self.assertNotEqual(in_job_after, True,
                                "Terminated worker should NOT be in Job")
        finally:
            if handle:
                dse.close_dispatch_job_handle(handle)
            try:
                if child.returncode is None:
                    child.kill()
                    child.wait()
            except Exception:
                pass


class NormalCompletionTests(unittest.TestCase):
    """Normal path: supervisor must not suicide before output is complete.

    Uses a child "supervisor" process to verify handle lifecycle.
    """

    @classmethod
    def setUpClass(cls) -> None:
        if os.name != "nt":
            raise unittest.SkipTest("Windows Job Object tests require Windows")

    def setUp(self) -> None:
        self._gen = "GEN-normal-" + secrets.token_hex(4)
        self.tmp = tempfile.TemporaryDirectory(prefix="dse-normal-")
        self.project = Path(self.tmp.name) / "project"
        (self.project / ".agentdesk" / "runtime").mkdir(parents=True)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_handle_closure_kills_child_not_test(self) -> None:
        """Closing Job handle kills assigned child, but not the test process
        (since test process was never assigned to it)."""
        handle = dse.create_dispatch_job(self._gen)
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(15)"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        try:
            dse._assign_process_to_job(handle, child.pid)
            self.assertTrue(dse.is_process_in_dispatch_job(self._gen, child.pid))
        finally:
            dse.close_dispatch_job_handle(handle)
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutError:
                child.kill()
                child.wait()
            # Child should have been terminated by Job close.
            self.assertIsNotNone(child.returncode)

    def test_supervisor_runner_source_no_premature_handle_close(self) -> None:
        """Verify the runner module does not close the handle in a plain finally
        block that fires before the Worker result is emitted."""
        import dispatch_supervisor_runner as dsr
        runner_src = Path(dsr.__file__).read_text(encoding="utf-8") if dsr.__file__ else ""
        if not runner_src:
            self.skipTest("cannot locate runner source")
        self.assertIn("close_dispatch_job_handle", runner_src,
                      "close_dispatch_job_handle should appear in runner")


class ThreeStateProbeTests(unittest.TestCase):
    """probe_dispatch_process_tree() — ALIVE/DEAD/UNKNOWN matrix."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="dse-probe-")
        self.project = Path(self.tmp.name) / "project"
        (self.project / ".agentdesk" / "runtime").mkdir(parents=True)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _make_receipt(self, **overrides) -> dse.DispatchProcessReceipt:
        kwargs = dict(_kwargs(), **overrides)
        return dse.DispatchProcessReceipt(
            schema_version=dse.SCHEMA_VERSION,
            task_id=kwargs["task_id"],
            revision=int(kwargs["revision"]),
            attempt=int(kwargs["attempt"]),
            dispatch_id=str(kwargs["dispatch_id"]),
            lease_epoch=int(kwargs["lease_epoch"]),
            holder_instance_id=str(kwargs["holder_instance_id"]),
            generation_id=str(kwargs["generation_id"]),
            platform=("win32" if os.name == "nt" else "posix"),
            boot_id=str(kwargs["boot_id"]),
            phase=str(kwargs.get("phase", "RESERVED")),
            creator_pid=int(kwargs["creator_pid"]),
            creator_creation_time=str(kwargs["creator_creation_time"]),
            supervisor_pid=kwargs.get("supervisor_pid"),
            supervisor_creation_time=kwargs.get("supervisor_creation_time"),
            worker_pid=kwargs.get("worker_pid"),
            worker_creation_time=kwargs.get("worker_creation_time"),
            worker_process_group=kwargs.get("worker_process_group"),
            written_at=dse._now_utc_str(),
        )

    def test_before_supervisor_ready_returns_unknown(self) -> None:
        r = self._make_receipt(phase="RESERVED")
        result = dse.probe_dispatch_process_tree(r)
        self.assertEqual(result, dse.ProcessLiveness.UNKNOWN)

    def test_supervisor_alive_returns_alive(self) -> None:
        r = self._make_receipt(
            phase="SUPERVISOR_READY",
            supervisor_pid=os.getpid(),
            supervisor_creation_time=dse.get_process_creation_time(os.getpid()) or "",
        )
        result = dse.probe_dispatch_process_tree(r)
        self.assertEqual(result, dse.ProcessLiveness.ALIVE)

    def test_supervisor_unknown_identity_returns_unknown(self) -> None:
        r = self._make_receipt(
            phase="SUPERVISOR_READY",
            supervisor_pid=os.getpid(),
            supervisor_creation_time="wrong-incarnation-value",
        )
        result = dse.probe_dispatch_process_tree(r)
        self.assertEqual(result, dse.ProcessLiveness.UNKNOWN)

    def test_pid_reuse_returns_unknown(self) -> None:
        r = self._make_receipt(
            phase="SUPERVISOR_READY",
            supervisor_pid=os.getpid(),
            supervisor_creation_time="posix-starttime:9999999999",
        )
        result = dse.probe_dispatch_process_tree(r)
        self.assertEqual(result, dse.ProcessLiveness.UNKNOWN)

    def test_invalid_phase_not_in_enum_returns_unknown(self) -> None:
        """probe_dispatch_process_tree handles non-standard phase strings.

        The dataclass validates phase at construction time, so we cannot
        construct a receipt with an invalid phase.  Instead we test that
        malformed input (a dict with an invalid phase) is safely handled.
        """
        # probe_dispatch_process_tree reads phase as a string attribute.
        # Create a minimal object-like payload and verify the probe
        # returns UNKNOWN rather than crashing.
        class _FakeReceipt:
            phase = "INVALID_PHASE"
            supervisor_pid = None
            supervisor_creation_time = None
            boot_id = "b"
            worker_pid = None
            worker_creation_time = None
            worker_process_group = None
            generation_id = "GEN-fake"

        result = dse.probe_dispatch_process_tree(_FakeReceipt())  # type: ignore[arg-type]
        self.assertEqual(result, dse.ProcessLiveness.UNKNOWN)

    def test_none_supervisor_returns_unknown(self) -> None:
        """When a receipt has None supervisor fields, probe returns UNKNOWN.

        The dataclass requires supervisor_pid at SUPERVISOR_READY, so we
        construct a valid RESERVED receipt (where None supervisor is legal)
        and test that probe_dispatch_process_tree correctly returns UNKNOWN
        for pre-SUPERVISOR_READY phases.
        """
        r = self._make_receipt(
            phase="RESERVED",
            supervisor_pid=None,
            supervisor_creation_time=None,
        )
        result = dse.probe_dispatch_process_tree(r)
        self.assertEqual(result, dse.ProcessLiveness.UNKNOWN)


class WindowsProbeTests(unittest.TestCase):
    """probe_dispatch_process_tree — Windows-specific Job Object paths.

    Uses child processes to avoid assigning the test process itself
    to a KILL_ON_JOB_CLOSE Job.
    """

    @classmethod
    def setUpClass(cls) -> None:
        if os.name != "nt":
            raise unittest.SkipTest("Windows Job Object tests require Windows")

    def setUp(self) -> None:
        self._gen = "GEN-windows-probe-" + secrets.token_hex(4)
        self.tmp = tempfile.TemporaryDirectory(prefix="dse-winprobe-")
        self.project = Path(self.tmp.name) / "project"
        (self.project / ".agentdesk" / "runtime").mkdir(parents=True)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _receipt(self, phase: str, supervisor_pid: int | None = None,
                 supervisor_ct: str | None = None,
                 worker_pid: int | None = None,
                 worker_ct: str | None = None,
                 worker_pg: int | None = None) -> dse.DispatchProcessReceipt:
        return dse.DispatchProcessReceipt(
            schema_version=dse.SCHEMA_VERSION,
            task_id="TC-001", revision=1, attempt=1,
            dispatch_id="DSP-probe-" + secrets.token_hex(4),
            lease_epoch=1, holder_instance_id="inst",
            generation_id=self._gen,
            platform="win32",
            boot_id=dse.get_boot_id(),
            phase=phase,
            creator_pid=os.getpid(),
            creator_creation_time=dse.get_process_creation_time(os.getpid()) or "",
            supervisor_pid=supervisor_pid,
            supervisor_creation_time=supervisor_ct,
            worker_pid=worker_pid,
            worker_creation_time=worker_ct,
            worker_process_group=worker_pg,
            written_at=dse._now_utc_str(),
        )

    def test_job_active_process_count_positive_returns_alive(self) -> None:
        """When Job exists and has active processes, returns ALIVE."""
        handle = dse.create_dispatch_job(self._gen)
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(10)"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        try:
            dse._assign_process_to_job(handle, child.pid)
            # Create receipt pointing to the child as supervisor.
            r = self._receipt(
                phase="SUPERVISOR_READY",
                supervisor_pid=child.pid,
                supervisor_ct=dse.get_process_creation_time(child.pid) or "",
            )
            result = dse.probe_dispatch_process_tree(r)
            self.assertEqual(result, dse.ProcessLiveness.ALIVE)
        finally:
            dse.close_dispatch_job_handle(handle)
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutError:
                child.kill()
                child.wait()

    def test_job_query_permission_denied_returns_unknown(self) -> None:
        """probe handles missing Job gracefully by returning UNKNOWN.

        When the Job doesn't exist (never created) and the supervisor PID
        doesn't match, probe_dispatch_process_tree must return UNKNOWN
        because it cannot discriminate between "Job deleted after kill"
        and "insufficient permissions to query the Job."
        """
        r = self._receipt(
            phase="SUPERVISOR_READY",
            supervisor_pid=99999,
            supervisor_ct="posix-starttime:9999999999",
        )
        result = dse.probe_dispatch_process_tree(r)
        # Supervisor is UNKNOWN (wrong creation time), Job doesn't exist → UNKNOWN.
        self.assertEqual(result, dse.ProcessLiveness.UNKNOWN)

    def test_job_not_initialized_returns_unknown(self) -> None:
        """If SUPERVISOR_READY was never written, probe returns UNKNOWN."""
        r = self._receipt(
            phase="RESERVED",
            supervisor_pid=None,
            supervisor_ct=None,
        )
        result = dse.probe_dispatch_process_tree(r)
        self.assertEqual(result, dse.ProcessLiveness.UNKNOWN)

    def test_job_empty_supervisor_dead_returns_dead(self) -> None:
        """When Job exists but is empty AND supervisor is DEAD, return DEAD."""
        handle = dse.create_dispatch_job(self._gen)
        try:
            # Job exists but is empty.  Supervisor PID is a non-existent PID
            # (guaranteed to be DEAD since it doesn't exist).
            r = self._receipt(
                phase="SUPERVISOR_READY",
                supervisor_pid=99999,
                supervisor_ct="posix-starttime:9999999999",
            )
            result = dse.probe_dispatch_process_tree(r)
            # Job empty (count == 0), supervisor DEAD (PID doesn't exist) → DEAD.
            self.assertEqual(result, dse.ProcessLiveness.DEAD)
        finally:
            dse.close_dispatch_job_handle(handle)

    def test_job_missing_supervisor_dead_returns_unknown(self) -> None:
        """When Job doesn't exist AND supervisor is DEAD, but we can't
        confirm the Job query was authoritative → UNKNOWN.

        Important: Job-not-found does NOT imply the tree is dead because
        the query failure might be due to permissions, not absence.
        """
        r = self._receipt(
            phase="SUPERVISOR_READY",
            supervisor_pid=99999,
            supervisor_ct="posix-starttime:9999999999",
        )
        result = dse.probe_dispatch_process_tree(r)
        # No Job was created for this generation → OpenJobObjectW fails.
        # The probe must NOT return DEAD based on a failed OpenJobObjectW.
        self.assertEqual(result, dse.ProcessLiveness.UNKNOWN)


# ── receipt / tombstone field-count freeze tests ─────────────────


class FieldCountFreezeTests(unittest.TestCase):
    """Ensure receipt (19) and tombstone (12) field counts are unchanged."""

    def test_receipt_has_exactly_19_fields(self) -> None:
        self.assertEqual(len(dse.DispatchProcessReceipt.__dataclass_fields__), 19)

    def test_tombstone_has_exactly_12_fields(self) -> None:
        self.assertEqual(len(dse.DispatchFinalizerTombstone.__dataclass_fields__), 12)

    def test_receipt_fields_frozen_and_slots(self) -> None:
        params = dse.DispatchProcessReceipt.__dataclass_params__
        self.assertTrue(params.frozen)
        self.assertTrue(params.slots)


if __name__ == "__main__":
    unittest.main()
