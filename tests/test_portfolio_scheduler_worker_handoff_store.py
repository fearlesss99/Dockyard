"""Focused tests for the TC-13.24b.2b.4c.2 handoff evidence Store."""

from __future__ import annotations

import hashlib
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import fields, replace
from pathlib import Path
from unittest.mock import patch

SCRIPT_ROOT = Path(__file__).resolve().parents[1] / "skills" / "agentdesk" / "scripts"
if str(SCRIPT_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPT_ROOT))

from core_types import WorkerKind
from dispatch_supervisor_evidence import DispatchProcessReceipt
from portfolio_scheduler_worker_handoff_store import (
    HANDOFF_SCHEMA_VERSION,
    PortfolioSchedulerWorkerHandoffConflictError,
    PortfolioSchedulerWorkerHandoffSecurityError,
    PortfolioSchedulerWorkerHandoffStore,
    ScheduledDispatchHandoff,
    ScheduledDispatchHandoffPhase,
    ScheduledDispatchHandoffReceipt,
    decode_handoff,
    decode_handoff_receipt,
    encode_handoff,
    encode_handoff_receipt,
)

STAMP = "2026-01-01T00:00:00.000000Z"
SHA = "sha256:" + "1" * 64
COMMIT = "a" * 40


def _dispatch_receipt(dispatch_id: str = "DSP-1") -> DispatchProcessReceipt:
    return DispatchProcessReceipt(
        schema_version="agentdesk.dispatch-supervisor-evidence/v1",
        task_id="TC-001", revision=1, attempt=1, dispatch_id=dispatch_id,
        lease_epoch=1, holder_instance_id="holder-1", generation_id="GEN-1",
        platform="posix", boot_id="boot-1", phase="RESERVED",
        creator_pid=1, creator_creation_time="1", supervisor_pid=None,
        supervisor_creation_time=None, worker_pid=None, worker_creation_time=None,
        worker_process_group=None, written_at=STAMP,
    )


def _handoff(
    dispatch_id: str = "DSP-1",
    attempt: int = 1,
    phase: ScheduledDispatchHandoffPhase = ScheduledDispatchHandoffPhase.ADMISSION_COMMITTED,
) -> ScheduledDispatchHandoff:
    return ScheduledDispatchHandoff(
        schema_version=HANDOFF_SCHEMA_VERSION, handoff_id="HNDF-1",
        queue_id="Q-1", plan_digest=SHA, receipt_id="SR-1", task_id="TC-001",
        revision=1, attempt=attempt, dispatch_id=dispatch_id,
        dispatch_event_id="EVT-1", outbox_message_id="MSG-1", lease_id="lease-1",
        lease_epoch=1, holder_instance_id="holder-1",
        worker_kind=WorkerKind.STANDARD_AGENT, assessment_id="ASM-1",
        expected_snapshot_commit=COMMIT, provider_binding_id="binding-1",
        phase=phase, reserved_at=STAMP, supervisor_started_at=None,
        worker_started_at=None, acknowledged_at=None, finalized_at=None,
        content_digest=SHA,
    )


class PortfolioSchedulerWorkerHandoffStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = PortfolioSchedulerWorkerHandoffStore(self.root)
        self.handoff = _handoff()
        self.process = _dispatch_receipt()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _reserve(self) -> tuple[ScheduledDispatchHandoff, ScheduledDispatchHandoffReceipt]:
        return self.store.reserve_handoff(self.handoff, self.process)

    def test_public_types_have_exact_frozen_field_order(self) -> None:
        self.assertTrue(ScheduledDispatchHandoff.__dataclass_params__.frozen)
        self.assertTrue(ScheduledDispatchHandoffReceipt.__dataclass_params__.frozen)
        self.assertEqual(len(fields(ScheduledDispatchHandoff)), 25)
        self.assertEqual(len(fields(ScheduledDispatchHandoffReceipt)), 11)
        self.assertEqual(tuple(ScheduledDispatchHandoffPhase), tuple(ScheduledDispatchHandoffPhase.order()))

    def test_codec_exact_fields_digest_and_replay(self) -> None:
        reserved, receipt = self._reserve()
        handoff_bytes = encode_handoff(reserved)
        receipt_bytes = encode_handoff_receipt(receipt)
        self.assertEqual(decode_handoff(handoff_bytes), reserved)
        self.assertEqual(decode_handoff_receipt(receipt_bytes), receipt)
        self.assertNotIn(b"\xef\xbb\xbf", handoff_bytes)
        self.assertTrue(handoff_bytes.endswith(b"\n"))
        self.assertEqual(handoff_bytes.count(b"content_digest:"), 1)
        self.assertEqual(receipt_bytes.count(b"content_digest:"), 1)

    def test_create_read_path_and_generation_binding(self) -> None:
        reserved, receipt = self._reserve()
        path = self.root / "docs" / "pm" / "portfolio-scheduler" / "worker-handoffs"
        self.assertEqual(
            sorted(item.name for item in path.iterdir()),
            ["DSP-1.receipt.yaml", "DSP-1.yaml"],
        )
        self.assertEqual(self.store.read_handoff("DSP-1"), reserved)
        self.assertEqual(self.store.read_handoff_receipt("DSP-1"), receipt)
        self.assertEqual(receipt.generation_id, self.process.generation_id)
        self.assertEqual(
            receipt.binding_digest,
            "sha256:" + hashlib.sha256(b"HNDF-1GEN-1").hexdigest(),
        )

    def test_identical_replay_is_byte_exact_and_divergence_conflicts(self) -> None:
        first, first_receipt = self._reserve()
        second, second_receipt = self._reserve()
        self.assertEqual((first, first_receipt), (second, second_receipt))
        divergent = replace(self.handoff, plan_digest="sha256:" + "2" * 64)
        with self.assertRaises(PortfolioSchedulerWorkerHandoffConflictError):
            self.store.reserve_handoff(divergent, self.process)

    def test_forward_only_phase_progression_and_same_phase_replay(self) -> None:
        self._reserve()
        with self.assertRaises(Exception):
            self.store.advance_phase(
                "DSP-1", ScheduledDispatchHandoffPhase.WORKER_STARTED,
                STAMP, self.process,
            )
        supervisor_time = "2026-01-01T00:00:01.000000Z"
        advanced, _ = self.store.advance_phase(
            "DSP-1", ScheduledDispatchHandoffPhase.SUPERVISOR_STARTED,
            supervisor_time, self.process,
        )
        replay, _ = self.store.advance_phase(
            "DSP-1", ScheduledDispatchHandoffPhase.SUPERVISOR_STARTED,
            supervisor_time, self.process,
        )
        self.assertEqual(advanced, replay)
        with self.assertRaises(Exception):
            self.store.advance_phase(
                "DSP-1", ScheduledDispatchHandoffPhase.HANDOFF_RESERVED,
                supervisor_time, self.process,
            )

    def test_attempt_four_rejected_before_any_write(self) -> None:
        with self.assertRaises(Exception):
            self.store.reserve_handoff(_handoff(attempt=4), self.process)
        directory = self.root / "docs" / "pm" / "portfolio-scheduler" / "worker-handoffs"
        self.assertFalse(directory.exists())

    def test_path_and_extra_file_security(self) -> None:
        self._reserve()
        directory = self.root / "docs" / "pm" / "portfolio-scheduler" / "worker-handoffs"
        (directory / "DSP-1.extra.yaml").write_bytes(b"unexpected\n")
        with self.assertRaises(PortfolioSchedulerWorkerHandoffSecurityError):
            self.store.read_handoff("DSP-1")
        with self.assertRaises(Exception):
            self.store.read_handoff("DSP-1/escape")

    def test_crash_between_pair_writes_leaves_typed_partial_evidence(self) -> None:
        import portfolio_scheduler_worker_handoff_store as module

        original_write = module._write
        calls = {"count": 0}

        def fail_second(path: Path, content: bytes) -> None:
            calls["count"] += 1
            if calls["count"] == 2:
                raise OSError("simulated write interruption")
            original_write(path, content)

        with patch.object(module, "_write", side_effect=fail_second):
            with self.assertRaises(Exception):
                self._reserve()
        directory = self.root / "docs" / "pm" / "portfolio-scheduler" / "worker-handoffs"
        self.assertTrue((directory / "DSP-1.yaml").exists())
        with self.assertRaises(PortfolioSchedulerWorkerHandoffConflictError):
            self._reserve()

    def test_concurrent_same_bytes_has_one_durable_identity(self) -> None:
        def run_once(_: int) -> str:
            try:
                self.store.reserve_handoff(self.handoff, self.process)
                return "accepted"
            except PortfolioSchedulerWorkerHandoffConflictError:
                return "conflict"

        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = tuple(executor.map(run_once, (1, 2)))
        self.assertEqual(outcomes.count("accepted"), 1)
        self.assertEqual(outcomes.count("conflict"), 1)
        self.assertEqual(self.store.read_handoff("DSP-1").handoff_id, "HNDF-1")

    def test_no_worker_lease_or_canonical_event_boundary(self) -> None:
        reserved, _ = self._reserve()
        self.assertEqual(reserved.dispatch_event_id, "EVT-1")
        self.assertFalse((self.root / ".agentdesk").exists())
        self.assertFalse(
            (self.root / "docs" / "pm" / "portfolio-scheduler" / "events").exists()
        )


if __name__ == "__main__":
    unittest.main()
