from __future__ import annotations

import dataclasses
import ctypes
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

SCRIPT_ROOT = Path(__file__).resolve().parents[1] / "skills" / "agentdesk" / "scripts"
if str(SCRIPT_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPT_ROOT))

import portfolio_scheduler_store as ps  # noqa: E402
from core_types import WorkerKind  # noqa: E402


STAMP = "2026-01-01T00:00:00.000000Z"
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


class _FakeWin32Function:
    def __init__(self, implementation):
        self._implementation = implementation
        self.argtypes = None
        self.restype = None

    def __call__(self, *args):
        return self._implementation(*args)


class _FakeWin32Handle:
    def __init__(self, value: int):
        self.value = value


class _FakeWin32Kernel32:
    def __init__(self, path_kind: str):
        self.path_kind = path_kind
        self.last_error = 0
        self.created_handles = 0
        self.create_arguments: list[tuple[int, int]] = []
        self.CreateFileW = _FakeWin32Function(self._create_file)
        self.GetLastError = _FakeWin32Function(lambda: self.last_error)
        self.GetFileAttributesW = _FakeWin32Function(self._get_attributes)
        self.CloseHandle = _FakeWin32Function(lambda handle: True)

    def _create_file(self, path, desired_access, share_mode, security, disposition, flags, template):
        self.create_arguments.append((int(share_mode), int(disposition)))
        if self.path_kind == "vanish_then_success":
            self.path_kind = "vanished_missing"
            self.last_error = 5
            return _FakeWin32Handle(_INVALID_HANDLE_VALUE)
        if self.path_kind == "success_pending":
            self.created_handles += 1
            self.last_error = 0
            self.path_kind = "regular"
            return _FakeWin32Handle(100 + self.created_handles)
        self.last_error = 5
        return _FakeWin32Handle(_INVALID_HANDLE_VALUE)

    def _get_attributes(self, path):
        if self.path_kind == "missing":
            self.last_error = 2
            return 0xFFFFFFFF
        if self.path_kind == "vanished_missing":
            self.last_error = 2
            self.path_kind = "success_pending"
            return 0xFFFFFFFF
        if self.path_kind == "directory":
            self.last_error = 0
            return 0x00000010
        if self.path_kind == "reparse":
            self.last_error = 0
            return 0x00000400
        if self.path_kind in ("regular", "success_pending"):
            self.last_error = 0
            return 0
        self.last_error = 5
        return 0xFFFFFFFF


def make_entry(
    sequence: int,
    *,
    queue_id: str = "Q-1",
    task_id: str = "TC-2401",
    revision: int = 1,
    state: ps.QueuePhase = ps.QueuePhase.QUEUED,
    generation: int = 1,
    worker: WorkerKind = WorkerKind.STANDARD_AGENT,
) -> ps.QueueEntry:
    provisional = ps.QueueEntry(
        schema_version=ps.SCHEMA_VERSION,
        queue_id=queue_id,
        task_id=task_id,
        revision=revision,
        enqueue_sequence=sequence,
        enqueued_at=STAMP,
        business_priority=ps.BusinessPriority.P1,
        aging_basis_at=STAMP,
        worker_kind_request=worker,
        assessment_id="ASM-2401",
        state=state,
        conflict_keys=(
            ps.ConflictKey("repo:agentdesk", ps.ConflictKeyClass.ADVISORY, True),
        ),
        retry_budget_used=0,
        content_digest="sha256:" + "0" * 64,
        selection_generation=generation,
    )
    return dataclasses.replace(provisional, content_digest=ps._entry_digest(provisional))


def make_receipt(entry: ps.QueueEntry, receipt_id: str = "SR-1") -> ps.ScheduleReceipt:
    provisional = ps.ScheduleReceipt(
        schema_version=ps.SCHEMA_VERSION,
        receipt_id=receipt_id,
        queue_id=entry.queue_id,
        task_id=entry.task_id,
        revision=entry.revision,
        enqueue_sequence=entry.enqueue_sequence,
        selected_at=STAMP,
        order_key=(1, 0, entry.enqueue_sequence, entry.queue_id),
        selection_reason="priority_order",
        worker_kind=entry.worker_kind_request,
        dispatch_event_id=None,
        phase=ps.ReceiptPhase.SELECTED,
        content_digest="sha256:" + "0" * 64,
        selection_generation=entry.selection_generation,
    )
    return dataclasses.replace(provisional, content_digest=ps._receipt_digest(provisional))


class StoreTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = ps.PortfolioSchedulerStore(self.root)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def queue_one(self, queue_id: str = "Q-1", task_id: str = "TC-2401") -> ps.QueueEntry:
        sequence = self.store.reserve_enqueue_sequence()
        entry = make_entry(sequence, queue_id=queue_id, task_id=task_id)
        self.store.create_queue_entry(entry)
        return entry

    def selected_one(self) -> ps.QueueEntry:
        entry = self.queue_one()
        return self.store.advance_queue_phase(
            entry.queue_id, entry.selection_generation, ps.QueuePhase.SELECTED
        )

    def dispatched_receipt(self) -> tuple[ps.QueueEntry, ps.ScheduleReceipt]:
        entry = self.selected_one()
        receipt = make_receipt(entry)
        self.store.create_schedule_receipt(receipt)
        receipt = self.store.advance_schedule_receipt(
            receipt.receipt_id, receipt.selection_generation, ps.ReceiptPhase.DISPATCHED, "EVT-1"
        )
        self.store.advance_queue_phase(
            entry.queue_id, entry.selection_generation, ps.QueuePhase.DISPATCHED, "EVT-1"
        )
        return entry, receipt


class PortfolioSchedulerCodecTests(StoreTestCase):
    def test_public_types_are_exact_frozen_slots(self) -> None:
        self.assertTrue(dataclasses.is_dataclass(ps.QueueEntry))
        self.assertTrue(dataclasses.is_dataclass(ps.ScheduleReceipt))
        self.assertTrue(dataclasses.is_dataclass(ps.ConflictKey))
        self.assertEqual(tuple(field.name for field in dataclasses.fields(ps.QueueEntry)), ps._QUEUE_FIELDS)
        self.assertEqual(tuple(field.name for field in dataclasses.fields(ps.ScheduleReceipt)), ps._RECEIPT_FIELDS)
        for cls in (ps.QueueEntry, ps.ScheduleReceipt, ps.ConflictKey):
            self.assertTrue(cls.__dataclass_params__.frozen)
            self.assertTrue(hasattr(cls, "__slots__"))

    def test_enums_and_public_annotations_have_no_mutable_or_any_fields(self) -> None:
        self.assertEqual([item.value for item in ps.BusinessPriority], ["P0", "P1", "P2", "P3"])
        self.assertEqual([item.value for item in ps.QueuePhase], ["queued", "selected", "dispatched", "retired"])
        self.assertEqual([item.value for item in ps.ReceiptPhase], ["selected", "dispatched"])
        self.assertEqual([item.value for item in ps.ConflictKeyClass], ["hard_exclusive", "advisory", "unknown"])
        annotations = {**ps.QueueEntry.__annotations__, **ps.ScheduleReceipt.__annotations__}
        public_text = repr(annotations)
        for forbidden in ("Any", "Mapping", "dict", "list", "set"):
            self.assertNotIn(forbidden, public_text)

    def test_canonical_byte_exact_round_trip(self) -> None:
        entry = make_entry(1)
        encoded = ps.encode_queue_entry(entry)
        self.assertEqual(ps.encode_queue_entry(ps.decode_queue_entry(encoded)), encoded)
        self.assertEqual(encoded, encoded.decode("utf-8").encode("utf-8"))
        selected = dataclasses.replace(entry, state=ps.QueuePhase.SELECTED)
        selected = dataclasses.replace(selected, content_digest=ps._entry_digest(selected))
        receipt = make_receipt(selected)
        receipt_bytes = ps.encode_schedule_receipt(receipt)
        self.assertEqual(ps.encode_schedule_receipt(ps.decode_schedule_receipt(receipt_bytes)), receipt_bytes)

    def test_codec_rejects_unknown_missing_extra_and_digest(self) -> None:
        entry = make_entry(1)
        encoded = ps.encode_queue_entry(entry)
        self.assertRaises(ps.PortfolioSchedulerSchemaError, ps.decode_queue_entry, encoded.replace(b"selection_generation", b"unknown_field", 1))
        self.assertRaises(ps.PortfolioSchedulerSchemaError, ps.decode_queue_entry, encoded.replace(b"retry_budget_used: 0\n", b"", 1))
        self.assertRaises(ps.PortfolioSchedulerSchemaError, ps.decode_queue_entry, encoded + b"extra: \"x\"\n")
        self.assertRaises(
            ps.PortfolioSchedulerSchemaError,
            ps.decode_queue_entry,
            encoded.replace(entry.content_digest.encode("ascii"), (b"sha256:" + b"1" * 64), 1),
        )

    def test_codec_rejects_bool_as_int_and_malicious_object_without_stringifying(self) -> None:
        bad = make_entry(1)
        with self.assertRaises(ps.PortfolioSchedulerInputError):
            ps.QueueEntry(
                **{**dataclasses.asdict(bad), "revision": True, "content_digest": bad.content_digest}
            )

        class Malicious:
            def __str__(self) -> str:
                raise AssertionError("must not stringify")

            def __repr__(self) -> str:
                raise AssertionError("must not repr")

        with self.assertRaises(ps.PortfolioSchedulerInputError):
            ps.QueueEntry(
                schema_version=ps.SCHEMA_VERSION,
                queue_id=Malicious(),
                task_id="TC-2401",
                revision=1,
                enqueue_sequence=1,
                enqueued_at=STAMP,
                business_priority=ps.BusinessPriority.P1,
                aging_basis_at=STAMP,
                worker_kind_request=WorkerKind.STANDARD_AGENT,
                assessment_id="ASM-2401",
                state=ps.QueuePhase.QUEUED,
                conflict_keys=(),
                retry_budget_used=0,
                content_digest="sha256:" + "0" * 64,
                selection_generation=1,
            )

    def test_conflict_key_unknown_cannot_be_canonical(self) -> None:
        with self.assertRaises(ps.PortfolioSchedulerInputError):
            ps.ConflictKey("repo:x", ps.ConflictKeyClass.UNKNOWN, True)


class PortfolioSchedulerSequenceTests(StoreTestCase):
    def test_initial_contiguous_and_restart_reservation(self) -> None:
        self.assertEqual(self.store.reserve_enqueue_sequence(), 1)
        self.assertEqual(self.store.reserve_enqueue_sequence(), 2)
        self.assertEqual(ps.PortfolioSchedulerStore(self.root).reserve_enqueue_sequence(), 3)
        sequence = self.root / "docs" / "pm" / "portfolio-scheduler" / "sequence.yaml"
        self.assertIn(b"last_issued_enqueue_sequence: 3", sequence.read_bytes())

    def test_reserved_sequence_is_not_reused_without_queue_entry(self) -> None:
        self.assertEqual(self.store.reserve_enqueue_sequence(), 1)
        self.assertEqual(self.store.reserve_enqueue_sequence(), 2)
        self.assertEqual(self.store.reserve_enqueue_sequence(), 3)

    def test_counter_rollback_corruption_and_missing_counter_fail_closed(self) -> None:
        self.store.reserve_enqueue_sequence()
        entry = make_entry(1, queue_id="Q-2", task_id="TC-2402")
        self.store.create_queue_entry(entry)
        sequence = self.root / "docs" / "pm" / "portfolio-scheduler" / "sequence.yaml"
        sequence.write_bytes(ps._encode_sequence(0, ()))
        with self.assertRaises(ps.PortfolioSchedulerSequenceError):
            self.store.reserve_enqueue_sequence()
        sequence.unlink()
        with self.assertRaises(ps.PortfolioSchedulerSequenceError):
            self.store.enumerate_queue_snapshot()
        self.assertEqual(entry.enqueue_sequence, 1)

    def test_sequence_malformed_bool_duplicate_and_discontinuity_fail_closed(self) -> None:
        self.store.reserve_enqueue_sequence()
        sequence = self.root / "docs" / "pm" / "portfolio-scheduler" / "sequence.yaml"
        sequence.write_bytes(
            b'schema_version: "agentdesk.portfolio-scheduler/v1"\n'
            b"last_issued_enqueue_sequence: true\nreserved_sequences:\n  - 1\n"
        )
        with self.assertRaises(ps.PortfolioSchedulerSequenceError):
            self.store.reserve_enqueue_sequence()
        sequence.write_bytes(
            b'schema_version: "agentdesk.portfolio-scheduler/v1"\n'
            b"last_issued_enqueue_sequence: 2\nreserved_sequences:\n  - 1\n  - 1\n"
        )
        with self.assertRaises(ps.PortfolioSchedulerSequenceError):
            self.store.reserve_enqueue_sequence()


class PortfolioSchedulerQueueStoreTests(StoreTestCase):
    def test_create_read_replay_and_snapshot(self) -> None:
        entry = self.queue_one()
        self.assertEqual(self.store.read_queue_entry(entry.queue_id), entry)
        self.assertEqual(self.store.create_queue_entry(entry), entry)
        self.assertEqual(self.store.enumerate_queue_snapshot(), (entry,))

    def test_divergent_identity_and_duplicate_fencing(self) -> None:
        entry = self.queue_one()
        divergent = make_entry(1, queue_id=entry.queue_id, task_id=entry.task_id)
        divergent = dataclasses.replace(divergent, assessment_id="ASM-other")
        divergent = dataclasses.replace(divergent, content_digest=ps._entry_digest(divergent))
        with self.assertRaises(ps.PortfolioSchedulerConflictError):
            self.store.create_queue_entry(divergent)
        second = self.store.reserve_enqueue_sequence()
        duplicate_task = make_entry(second, queue_id="Q-2", task_id=entry.task_id)
        with self.assertRaises(ps.PortfolioSchedulerConflictError):
            self.store.create_queue_entry(duplicate_task)

    def test_forward_only_phase_and_stale_generation(self) -> None:
        entry = self.queue_one()
        selected = self.store.advance_queue_phase(entry.queue_id, 1, ps.QueuePhase.SELECTED)
        self.assertEqual(selected.state, ps.QueuePhase.SELECTED)
        with self.assertRaises(ps.PortfolioSchedulerTransitionError):
            self.store.advance_queue_phase(entry.queue_id, 1, ps.QueuePhase.QUEUED)
        with self.assertRaises(ps.PortfolioSchedulerConflictError):
            self.store.advance_queue_phase(entry.queue_id, 2, ps.QueuePhase.DISPATCHED, "EVT-1")
        dispatched = self.store.advance_queue_phase(entry.queue_id, 1, ps.QueuePhase.DISPATCHED, "EVT-1")
        retired = self.store.retire_queue_entry(entry.queue_id, 1)
        self.assertEqual((dispatched.state, retired.state), (ps.QueuePhase.DISPATCHED, ps.QueuePhase.RETIRED))
        with self.assertRaises(ps.PortfolioSchedulerTransitionError):
            self.store.advance_queue_phase(entry.queue_id, 1, ps.QueuePhase.SELECTED)

    def test_recovery_is_named_and_increments_generation(self) -> None:
        entry = self.selected_one()
        receipt = make_receipt(entry)
        self.store.create_schedule_receipt(receipt)
        recovered = self.store.recover_selected_without_dispatch(entry.queue_id, 1, receipt.receipt_id)
        self.assertEqual((recovered.state, recovered.selection_generation), (ps.QueuePhase.QUEUED, 2))
        with self.assertRaises(ps.PortfolioSchedulerTransitionError):
            self.store.read_schedule_receipt(receipt.receipt_id)
        self.assertEqual(
            self.store.recover_selected_without_dispatch(entry.queue_id, 1, receipt.receipt_id), recovered
        )


class PortfolioSchedulerReceiptStoreTests(StoreTestCase):
    def test_binding_create_read_and_forward_only(self) -> None:
        entry = self.selected_one()
        receipt = make_receipt(entry)
        self.assertEqual(self.store.create_schedule_receipt(receipt), receipt)
        self.assertEqual(self.store.read_schedule_receipt(receipt.receipt_id), receipt)
        dispatched = self.store.advance_schedule_receipt(
            receipt.receipt_id, 1, ps.ReceiptPhase.DISPATCHED, "EVT-1"
        )
        self.assertEqual(dispatched.dispatch_event_id, "EVT-1")
        with self.assertRaises(ps.PortfolioSchedulerTransitionError):
            self.store.advance_schedule_receipt(receipt.receipt_id, 1, ps.ReceiptPhase.SELECTED)

    def test_receipt_identity_mismatch_and_orphan_fail_closed(self) -> None:
        entry = self.selected_one()
        receipt = make_receipt(entry)
        wrong = dataclasses.replace(receipt, task_id="TC-other")
        wrong = dataclasses.replace(wrong, content_digest=ps._receipt_digest(wrong))
        with self.assertRaises(ps.PortfolioSchedulerConflictError):
            self.store.create_schedule_receipt(wrong)
        orphan = make_receipt(make_entry(1, queue_id="Q-orphan"), "SR-orphan")
        orphan_path = self.root / "docs" / "pm" / "portfolio-scheduler" / "receipts" / "SR-orphan.yaml"
        orphan_path.write_bytes(ps.encode_schedule_receipt(orphan))
        with self.assertRaises(ps.PortfolioSchedulerNotFoundError):
            self.store.read_schedule_receipt("SR-orphan")

    def test_receipt_enumeration_validates_cross_references(self) -> None:
        entry = self.selected_one()
        receipt = make_receipt(entry)
        self.store.create_schedule_receipt(receipt)
        self.assertEqual(self.store.enumerate_validated_receipts(), (receipt,))


class PortfolioSchedulerReplayTombstoneTests(StoreTestCase):
    def test_byte_exact_divergent_receipt_replay(self) -> None:
        entry = self.selected_one()
        receipt = make_receipt(entry)
        self.assertEqual(self.store.create_schedule_receipt(receipt), receipt)
        self.assertEqual(self.store.create_schedule_receipt(receipt), receipt)
        divergent = dataclasses.replace(receipt, selection_reason="different_reason")
        divergent = dataclasses.replace(divergent, content_digest=ps._receipt_digest(divergent))
        with self.assertRaises(ps.PortfolioSchedulerConflictError):
            self.store.create_schedule_receipt(divergent)

    def test_finalize_is_tombstone_first_and_idempotent(self) -> None:
        _, receipt = self.dispatched_receipt()
        self.assertEqual(self.store.finalize_schedule_receipt(receipt.receipt_id), receipt)
        self.assertEqual(self.store.finalize_schedule_receipt(receipt.receipt_id), receipt)
        tombstone = self.root / "docs" / "pm" / "portfolio-scheduler" / "tombstones" / "SR-1.yaml"
        self.assertTrue(tombstone.exists())
        with self.assertRaises(ps.PortfolioSchedulerTransitionError):
            self.store.advance_schedule_receipt(receipt.receipt_id, 1, ps.ReceiptPhase.DISPATCHED, "EVT-2")

    def test_tombstone_receipt_divergence_fails_closed(self) -> None:
        _, receipt = self.dispatched_receipt()
        self.store.finalize_schedule_receipt(receipt.receipt_id)
        receipt_path = self.root / "docs" / "pm" / "portfolio-scheduler" / "receipts" / "SR-1.yaml"
        altered = dataclasses.replace(receipt, selection_reason="changed_reason")
        altered = dataclasses.replace(altered, content_digest=ps._receipt_digest(altered))
        receipt_path.write_bytes(ps.encode_schedule_receipt(altered))
        with self.assertRaises(ps.PortfolioSchedulerConflictError):
            self.store.read_schedule_receipt(receipt.receipt_id)


@unittest.skipUnless(os.name == "nt", "Win32 scheduler lock fixture")
class PortfolioSchedulerWindowsRaceTests(StoreTestCase):
    def _run_windows_create(self, path_kind: str):
        import ctypes

        fake = _FakeWin32Kernel32(path_kind)
        lock_path = self.root / ps.STORE_LOCK_FILENAME
        with mock.patch.object(ctypes, "WinDLL", return_value=fake):
            result = ps._windows_create_lock_handle(self.root, lock_path)
        return fake, result

    def test_access_denied_with_existing_regular_lock_is_conflict(self) -> None:
        fake = _FakeWin32Kernel32("regular")
        import ctypes

        with mock.patch.object(ctypes, "WinDLL", return_value=fake):
            with self.assertRaises(ps.PortfolioSchedulerLockConflictError):
                ps._windows_create_lock_handle(self.root, self.root / ps.STORE_LOCK_FILENAME)
        self.assertEqual(fake.create_arguments, [(3, 1)])
        self.assertEqual(fake.created_handles, 0)

    def test_access_denied_then_exact_missing_retries_once_and_succeeds(self) -> None:
        fake, result = self._run_windows_create("vanish_then_success")
        kernel32, handle = result
        self.assertIs(kernel32, fake)
        self.assertEqual(handle.value, 101)
        self.assertEqual(fake.create_arguments, [(3, 1), (3, 1)])
        self.assertEqual(fake.created_handles, 1)

    def test_access_denied_twice_with_missing_path_is_permission(self) -> None:
        fake = _FakeWin32Kernel32("missing")
        import ctypes

        with mock.patch.object(ctypes, "WinDLL", return_value=fake):
            with self.assertRaises(ps.PortfolioSchedulerPermissionError):
                ps._windows_create_lock_handle(self.root, self.root / ps.STORE_LOCK_FILENAME)
        self.assertEqual(fake.create_arguments, [(3, 1), (3, 1)])
        self.assertEqual(fake.created_handles, 0)

    def test_directory_and_reparse_are_not_contention(self) -> None:
        import ctypes

        for path_kind in ("directory", "reparse"):
            with self.subTest(path_kind=path_kind):
                fake = _FakeWin32Kernel32(path_kind)
                with mock.patch.object(ctypes, "WinDLL", return_value=fake):
                    with self.assertRaises(ps.PortfolioSchedulerSecurityError):
                        ps._windows_create_lock_handle(
                            self.root, self.root / ps.STORE_LOCK_FILENAME
                        )
                self.assertEqual(fake.create_arguments, [(3, 1)])
                self.assertEqual(fake.created_handles, 0)


class PortfolioSchedulerConcurrencyTests(StoreTestCase):
    def test_concurrent_sequence_reservations_are_unique(self) -> None:
        barrier = threading.Barrier(8)
        results: list[int] = []
        errors: list[BaseException] = []
        result_lock = threading.Lock()

        def reserve() -> None:
            try:
                barrier.wait(timeout=3)
                for _ in range(1000):
                    try:
                        value = self.store.reserve_enqueue_sequence()
                        break
                    except ps.PortfolioSchedulerLockConflictError:
                        continue
                else:
                    raise AssertionError("bounded retry exhausted")
                with result_lock:
                    results.append(value)
            except BaseException as exc:
                with result_lock:
                    errors.append(exc)

        threads = [threading.Thread(target=reserve) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=4)
            self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(sorted(results), list(range(1, 9)))

    def test_concurrent_divergent_phase_updates_have_one_winner(self) -> None:
        entry = self.queue_one()
        self.store.advance_queue_phase(entry.queue_id, 1, ps.QueuePhase.SELECTED)
        barrier = threading.Barrier(2)
        outcomes: list[object] = []
        result_lock = threading.Lock()

        def advance(event_id: str) -> None:
            try:
                barrier.wait(timeout=3)
                value = self.store.advance_queue_phase(entry.queue_id, 1, ps.QueuePhase.DISPATCHED, event_id)
                with result_lock:
                    outcomes.append(value)
            except BaseException as exc:
                with result_lock:
                    outcomes.append(exc)

        left = threading.Thread(target=advance, args=("EVT-left",))
        right = threading.Thread(target=advance, args=("EVT-right",))
        left.start()
        right.start()
        left.join(timeout=4)
        right.join(timeout=4)
        self.assertFalse(left.is_alive() or right.is_alive())
        self.assertEqual(sum(isinstance(value, ps.QueueEntry) for value in outcomes), 1)
        self.assertEqual(sum(isinstance(value, ps.PortfolioSchedulerError) for value in outcomes), 1)


class PortfolioSchedulerFilesystemTests(StoreTestCase):
    def test_lock_contention_and_token_mismatch_are_typed(self) -> None:
        self.store.reserve_enqueue_sequence()
        lock = self.root / ps.STORE_LOCK_FILENAME
        lock.write_bytes(b"foreign-token")
        with self.assertRaises(ps.PortfolioSchedulerLockConflictError):
            self.store.reserve_enqueue_sequence()
        lock.unlink()
        with self.assertRaises(ps.PortfolioSchedulerLockConflictError):
            with ps._store_lock(self.root):
                lock.write_bytes(b"replaced-token")
        self.assertTrue(lock.exists())
        lock.unlink()

    def test_path_traversal_symlink_and_permission_fail_closed(self) -> None:
        with self.assertRaises(ps.PortfolioSchedulerSecurityError):
            self.store.read_queue_entry("Q-../escape")
        self.store.reserve_enqueue_sequence()
        outside = Path(self.temp.name).parent / (Path(self.temp.name).name + "-outside")
        outside.mkdir()
        link = self.root / "docs" / "pm" / "portfolio-scheduler" / "receipts" / "SR-link.yaml"
        try:
            link.symlink_to(outside / "evidence.yaml")
        except (OSError, NotImplementedError):
            outside.rmdir()
            self.skipTest("symlink creation is unavailable")
        try:
            with self.assertRaises(ps.PortfolioSchedulerSecurityError):
                self.store.read_schedule_receipt("SR-link")
        finally:
            if link.is_symlink():
                link.unlink()
            outside.rmdir()

    def test_atomic_write_failure_maps_to_permission_and_temp_files_clean(self) -> None:
        with mock.patch.object(ps, "_atomic_write_bytes", side_effect=OSError("simulated write failure")):
            with self.assertRaises(ps.PortfolioSchedulerPermissionError):
                self.store.reserve_enqueue_sequence()
        self.assertEqual(list((self.root / "docs" / "pm" / "portfolio-scheduler").glob(".tmp-*")), [])
        self.store.reserve_enqueue_sequence()
        self.assertEqual(list((self.root / "docs" / "pm" / "portfolio-scheduler").glob(".tmp-*")), [])
        self.assertFalse((self.root / ".state-transition.lock").exists())


if __name__ == "__main__":
    unittest.main()
