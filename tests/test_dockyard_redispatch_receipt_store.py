from __future__ import annotations

import dataclasses
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills/agentdesk/scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from dockyard_redispatch_receipt_store import (
    SCHEMA_VERSION,
    DockyardRedispatchConflictError,
    DockyardRedispatchReceipt,
    DockyardRedispatchReceiptStore,
    DockyardRedispatchStoreError,
    with_content_digest,
)


def _receipt() -> DockyardRedispatchReceipt:
    return with_content_digest(DockyardRedispatchReceipt(
        SCHEMA_VERSION,
        "CMD-REDISPATCH-1",
        "REV-SOURCE-1",
        "sha256:" + "1" * 64,
        "TC-001",
        1,
        2,
        "DSP-REDISPATCH-1",
        "GEN-REDISPATCH-1",
        "EVT-DISPATCH-2",
        "sha256:" + "2" * 64,
        "EVT-ACK-2",
        "sha256:" + "3" * 64,
        "EVT-DELIVERY-2",
        "sha256:" + "4" * 64,
        "sha256:" + "5" * 64,
        "sha256:" + "6" * 64,
        "2026-08-04T00:00:00.000000Z",
        "sha256:" + "0" * 64,
    ))


class DockyardRedispatchReceiptStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        (self.root / ".agentdesk/runtime").mkdir(parents=True)
        self.store = DockyardRedispatchReceiptStore(self.root)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_receipt_shape_is_frozen_and_exact(self) -> None:
        self.assertEqual(
            tuple(field.name for field in dataclasses.fields(DockyardRedispatchReceipt)),
            (
                "schema_version", "command_id", "source_review_id",
                "source_review_receipt_digest", "task_id", "revision", "attempt",
                "dispatch_id", "generation_id", "dispatch_event_id",
                "dispatch_event_digest", "acknowledge_event_id",
                "acknowledge_event_digest", "delivery_event_id",
                "delivery_event_digest", "delivery_receipt_digest", "prompt_digest",
                "finalized_at", "content_digest",
            ),
        )
        self.assertTrue(DockyardRedispatchReceipt.__dataclass_params__.frozen)
        self.assertTrue(hasattr(DockyardRedispatchReceipt, "__slots__"))

    def test_round_trip_and_byte_exact_replay(self) -> None:
        receipt = _receipt()
        self.assertEqual(self.store.save(receipt), receipt)
        path = self.root / ".agentdesk/runtime/dockyard-redispatch/DSP-REDISPATCH-1.json"
        before = path.read_bytes()
        self.assertEqual(self.store.save(receipt), receipt)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(self.store.read(receipt.dispatch_id), receipt)

    def test_divergent_replay_is_conflict(self) -> None:
        receipt = _receipt()
        self.store.save(receipt)
        divergent = with_content_digest(dataclasses.replace(
            receipt, prompt_digest="sha256:" + "7" * 64
        ))
        with self.assertRaises(DockyardRedispatchConflictError):
            self.store.save(divergent)

    def test_attempt_four_and_bad_digest_are_rejected(self) -> None:
        with self.assertRaises(DockyardRedispatchStoreError):
            dataclasses.replace(_receipt(), attempt=4)
        with self.assertRaises(DockyardRedispatchStoreError):
            self.store.save(dataclasses.replace(
                _receipt(), content_digest="sha256:" + "f" * 64
            ))

    def test_extra_or_link_entry_fails_closed(self) -> None:
        self.store.save(_receipt())
        directory = self.root / ".agentdesk/runtime/dockyard-redispatch"
        (directory / "unexpected.txt").write_text("x", encoding="utf-8")
        with self.assertRaises(DockyardRedispatchStoreError):
            self.store.read("DSP-REDISPATCH-1")

    def test_concurrent_same_content_has_one_durable_result(self) -> None:
        receipt = _receipt()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = tuple(pool.map(lambda _: self.store.save(receipt), (1, 2)))
        self.assertEqual(results, (receipt, receipt))
        self.assertEqual(self.store.read(receipt.dispatch_id), receipt)
        directory = self.root / ".agentdesk/runtime/dockyard-redispatch"
        self.assertEqual(tuple(path.name for path in directory.iterdir()), (
            "DSP-REDISPATCH-1.json",
        ))

    def test_concurrent_divergent_content_has_one_winner(self) -> None:
        first = _receipt()
        second = with_content_digest(dataclasses.replace(
            first, prompt_digest="sha256:" + "8" * 64
        ))
        barrier = threading.Barrier(2)

        def save(receipt: DockyardRedispatchReceipt) -> str:
            barrier.wait(timeout=5)
            try:
                self.store.save(receipt)
                return "saved"
            except DockyardRedispatchConflictError:
                return "conflict"

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = tuple(pool.map(save, (first, second)))
        self.assertEqual(sorted(results), ["conflict", "saved"])
        self.assertIn(self.store.read(first.dispatch_id), (first, second))


if __name__ == "__main__":
    unittest.main()
