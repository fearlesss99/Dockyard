from __future__ import annotations

import dataclasses
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills/agentdesk/scripts"
sys.path.insert(0, str(SCRIPTS))

import dockyard_post_delivery_review_store as store

STAMP = "2026-08-03T10:00:00.000000Z"


def _request(*, integration: bool = True, routing: str = "pass"):
    return store.with_request_digest(store.DockyardPostDeliveryReviewRequest(
        store.SCHEMA_VERSION, "OP-REVIEW-1", "PRJ-1", "TC-001", 1, 1,
        "DSP-1", "GEN-1", "Q-1", "SR-1", "sha256:" + "1" * 64,
        "HNDF-1", "sha256:" + "2" * 64, "EVT-DISPATCH",
        "sha256:" + "3" * 64, "EVT-ACK", "sha256:" + "4" * 64,
        "EVT-DELIVERY", "sha256:" + "5" * 64, "sha256:" + "6" * 64,
        "a" * 40, "b" * 40, "WT-1", "sha256:" + "7" * 64,
        "agentdesk/TC-001/r1/a1/DSP-1", "c" * 40,
        "sha256:" + "8" * 64, "AUDIT-CONFIG-1", "EVT-ACCEPT",
        "sha256:" + "9" * 64,
        "EVT-INTEGRATE" if integration else None,
        "sha256:" + "a" * 64 if integration else None,
        "EVT-RETURN" if routing == "fail" else None,
        "EVT-REQUEUE" if routing == "fail" else None,
        "EVT-BLOCK" if routing == "blocked" else None,
        "sha256:" + "b" * 64, STAMP, "sha256:" + "0" * 64,
    ))


class DockyardReviewStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        (self.root / ".agentdesk/runtime").mkdir(parents=True)
        self.store = store.DockyardPostDeliveryReviewStore(self.root)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_public_types_are_exact_frozen_and_slotted(self) -> None:
        self.assertEqual(len(dataclasses.fields(store.DockyardPostDeliveryReviewRequest)), 38)
        self.assertEqual(len(dataclasses.fields(store.DockyardPostDeliveryReviewReceipt)), 27)
        for cls in (store.DockyardPostDeliveryReviewRequest, store.DockyardPostDeliveryReviewReceipt):
            self.assertTrue(cls.__dataclass_params__.frozen)
            self.assertTrue(hasattr(cls, "__slots__"))

    def test_codec_round_trip_is_byte_exact(self) -> None:
        request = _request()
        encoded = store.encode_request(request)
        self.assertEqual(store.decode_request(encoded), request)
        receipt = self.store.reserve("REV-1", request)
        receipt_bytes = store.encode_receipt(receipt)
        self.assertEqual(store.decode_receipt(receipt_bytes), receipt)

    def test_noncanonical_and_malicious_scalar_are_rejected(self) -> None:
        encoded = store.encode_request(_request())
        with self.assertRaises(store.DockyardReviewSecurityError):
            store.decode_request(encoded + b"\n")
        with self.assertRaises(store.DockyardReviewSecurityError):
            store.decode_request(encoded.replace(b'operation_id: "OP-REVIEW-1"', b'operation_id: !!python/object "x"'))

    def test_reserve_read_replay_and_divergence(self) -> None:
        request = _request()
        first = self.store.reserve("REV-1", request)
        self.assertEqual(self.store.reserve("REV-1", request), first)
        self.assertEqual(self.store.read_request("REV-1"), request)
        self.assertEqual(self.store.read_receipt("REV-1"), first)
        divergent = store.with_request_digest(dataclasses.replace(request, operation_id="OP-REVIEW-2", content_digest="sha256:" + "0" * 64))
        with self.assertRaises(store.DockyardReviewConflictError):
            self.store.reserve("REV-1", divergent)

    def test_pass_with_integration_advances_all_phases(self) -> None:
        self.store.reserve("REV-1", _request())
        steps = (
            (store.DockyardPostDeliveryReviewPhase.REVIEW_RESERVED, store.DockyardPostDeliveryReviewOutcome.RESERVED, {}),
            (store.DockyardPostDeliveryReviewPhase.MAD_STARTED, store.DockyardPostDeliveryReviewOutcome.RESERVED, {}),
            (store.DockyardPostDeliveryReviewPhase.MAD_COMPLETED, store.DockyardPostDeliveryReviewOutcome.AUDIT_PASS, {"audit_result_id": "AUDIT-1", "audit_result_digest": "sha256:" + "c" * 64, "audit_verdict": "pass"}),
            (store.DockyardPostDeliveryReviewPhase.REVIEW_APPLIED, store.DockyardPostDeliveryReviewOutcome.ACCEPTED, {"applied_event_id": "EVT-ACCEPT"}),
            (store.DockyardPostDeliveryReviewPhase.INTEGRATION_APPLIED, store.DockyardPostDeliveryReviewOutcome.INTEGRATED, {"applied_event_id": "EVT-INTEGRATE"}),
            (store.DockyardPostDeliveryReviewPhase.FINALIZED, store.DockyardPostDeliveryReviewOutcome.FINALIZED, {}),
        )
        receipt = None
        for index, (phase, outcome, extras) in enumerate(steps, 1):
            receipt = self.store.advance("REV-1", phase, outcome, f"2026-08-03T10:00:0{index}.000000Z", **extras)
        assert receipt is not None
        self.assertIs(receipt.phase, store.DockyardPostDeliveryReviewPhase.FINALIZED)
        self.assertEqual(receipt.integration_event_id, "EVT-INTEGRATE")

    def test_pass_without_integration_skips_only_optional_phase(self) -> None:
        self.store.reserve("REV-1", _request(integration=False))
        self.store.advance("REV-1", store.DockyardPostDeliveryReviewPhase.REVIEW_RESERVED, store.DockyardPostDeliveryReviewOutcome.RESERVED, "2026-08-03T10:00:01.000000Z")
        self.store.advance("REV-1", store.DockyardPostDeliveryReviewPhase.MAD_STARTED, store.DockyardPostDeliveryReviewOutcome.RESERVED, "2026-08-03T10:00:02.000000Z")
        self.store.advance("REV-1", store.DockyardPostDeliveryReviewPhase.MAD_COMPLETED, store.DockyardPostDeliveryReviewOutcome.AUDIT_PASS, "2026-08-03T10:00:03.000000Z", audit_result_id="AUDIT-1", audit_result_digest="sha256:" + "c" * 64, audit_verdict="pass")
        self.store.advance("REV-1", store.DockyardPostDeliveryReviewPhase.REVIEW_APPLIED, store.DockyardPostDeliveryReviewOutcome.ACCEPTED, "2026-08-03T10:00:04.000000Z", applied_event_id="EVT-ACCEPT")
        final = self.store.advance("REV-1", store.DockyardPostDeliveryReviewPhase.FINALIZED, store.DockyardPostDeliveryReviewOutcome.FINALIZED, "2026-08-03T10:00:05.000000Z")
        self.assertIs(final.phase, store.DockyardPostDeliveryReviewPhase.FINALIZED)

    def test_fail_route_finalizes_without_consuming_dormant_integration_intent(self) -> None:
        self.store.reserve("REV-1", _request(integration=True, routing="fail"))
        self.store.advance("REV-1", store.DockyardPostDeliveryReviewPhase.REVIEW_RESERVED, store.DockyardPostDeliveryReviewOutcome.RESERVED, "2026-08-03T10:00:01.000000Z")
        self.store.advance("REV-1", store.DockyardPostDeliveryReviewPhase.MAD_STARTED, store.DockyardPostDeliveryReviewOutcome.RESERVED, "2026-08-03T10:00:02.000000Z")
        self.store.advance("REV-1", store.DockyardPostDeliveryReviewPhase.MAD_COMPLETED, store.DockyardPostDeliveryReviewOutcome.AUDIT_FAIL, "2026-08-03T10:00:03.000000Z", audit_result_id="AUDIT-1", audit_result_digest="sha256:" + "c" * 64, audit_verdict="fail")
        applied = self.store.advance("REV-1", store.DockyardPostDeliveryReviewPhase.REVIEW_APPLIED, store.DockyardPostDeliveryReviewOutcome.RETURNED, "2026-08-03T10:00:04.000000Z", applied_event_id="EVT-RETURN")
        self.assertEqual((applied.return_event_id, applied.requeue_event_id), ("EVT-RETURN", "EVT-REQUEUE"))
        with self.assertRaises(store.DockyardReviewInputError):
            self.store.advance("REV-1", store.DockyardPostDeliveryReviewPhase.INTEGRATION_APPLIED, store.DockyardPostDeliveryReviewOutcome.INTEGRATED, "2026-08-03T10:00:05.000000Z", applied_event_id="EVT-INTEGRATE")
        final = self.store.advance("REV-1", store.DockyardPostDeliveryReviewPhase.FINALIZED, store.DockyardPostDeliveryReviewOutcome.FINALIZED, "2026-08-03T10:00:06.000000Z")
        self.assertIs(final.phase, store.DockyardPostDeliveryReviewPhase.FINALIZED)
        self.assertIsNone(final.integration_event_id)

    def test_pass_route_cannot_skip_reserved_integration(self) -> None:
        self.store.reserve("REV-1", _request(integration=True))
        self.store.advance("REV-1", store.DockyardPostDeliveryReviewPhase.REVIEW_RESERVED, store.DockyardPostDeliveryReviewOutcome.RESERVED, "2026-08-03T10:00:01.000000Z")
        self.store.advance("REV-1", store.DockyardPostDeliveryReviewPhase.MAD_STARTED, store.DockyardPostDeliveryReviewOutcome.RESERVED, "2026-08-03T10:00:02.000000Z")
        self.store.advance("REV-1", store.DockyardPostDeliveryReviewPhase.MAD_COMPLETED, store.DockyardPostDeliveryReviewOutcome.AUDIT_PASS, "2026-08-03T10:00:03.000000Z", audit_result_id="AUDIT-1", audit_result_digest="sha256:" + "c" * 64, audit_verdict="pass")
        self.store.advance("REV-1", store.DockyardPostDeliveryReviewPhase.REVIEW_APPLIED, store.DockyardPostDeliveryReviewOutcome.ACCEPTED, "2026-08-03T10:00:04.000000Z", applied_event_id="EVT-ACCEPT")
        with self.assertRaises(store.DockyardReviewPhaseError):
            self.store.advance("REV-1", store.DockyardPostDeliveryReviewPhase.FINALIZED, store.DockyardPostDeliveryReviewOutcome.FINALIZED, "2026-08-03T10:00:05.000000Z")

    def test_skip_backward_and_finalized_reopen_are_rejected(self) -> None:
        self.store.reserve("REV-1", _request())
        with self.assertRaises(store.DockyardReviewPhaseError):
            self.store.advance("REV-1", store.DockyardPostDeliveryReviewPhase.MAD_STARTED, store.DockyardPostDeliveryReviewOutcome.RESERVED, STAMP)
        self.store.advance("REV-1", store.DockyardPostDeliveryReviewPhase.REVIEW_RESERVED, store.DockyardPostDeliveryReviewOutcome.RESERVED, "2026-08-03T10:00:01.000000Z")
        with self.assertRaises(store.DockyardReviewPhaseError):
            self.store.advance("REV-1", store.DockyardPostDeliveryReviewPhase.DELIVERY_BOUND, store.DockyardPostDeliveryReviewOutcome.RESERVED, STAMP)

    def test_attempt_four_rejects_before_store_write(self) -> None:
        with self.assertRaises(store.DockyardReviewInputError):
            dataclasses.replace(_request(), attempt=4)
        self.assertFalse((self.root / ".agentdesk/runtime/dockyard-review").exists())

    def test_extra_file_and_symlink_are_fail_closed(self) -> None:
        self.store.reserve("REV-1", _request())
        extra = self.root / ".agentdesk/runtime/dockyard-review/requests/REV-1.evil"
        extra.write_text("x", encoding="utf-8")
        with self.assertRaises(store.DockyardReviewSecurityError):
            self.store.read_request("REV-1")
        extra.unlink()
        target = self.root / "target"
        target.write_text("x", encoding="utf-8")
        link = self.root / ".agentdesk/runtime/dockyard-review/requests/REV-LINK.yaml"
        try:
            link.symlink_to(target)
        except OSError:
            self.skipTest("symlink creation unavailable")
        with self.assertRaises(store.DockyardReviewSecurityError):
            self.store.read_request("REV-LINK")

    def test_two_threads_converge_on_one_receipt(self) -> None:
        request = _request()
        barrier = threading.Barrier(2)
        results = []
        errors = []

        def run():
            try:
                barrier.wait(timeout=5)
                results.append(self.store.reserve("REV-1", request))
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=run) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        self.assertFalse(errors)
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0], results[1])
        self.assertEqual(len(tuple((self.root / ".agentdesk/runtime/dockyard-review/requests").glob("*.yaml"))), 1)

    def test_source_has_no_owner_process_provider_or_network_import(self) -> None:
        source = (SCRIPTS / "dockyard_post_delivery_review_store.py").read_text(encoding="utf-8")
        for forbidden in ("workflow_orchestrator", "import mad", "from mad", "control_plane_transition", "worker_slot_lease", "subprocess", "import socket", "from socket", "import requests", "from requests", "provider"):
            self.assertNotIn(forbidden, source.lower())


if __name__ == "__main__":
    unittest.main()
