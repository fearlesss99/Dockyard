from __future__ import annotations

import dataclasses
import inspect
import sys
import tempfile
import threading
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "agentdesk" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from dockyard_pairing import (  # noqa: E402
    DockyardAuthenticateRequest,
    DockyardDeviceRecord,
    DockyardDeviceScope,
    DockyardPairingAuthenticationError,
    DockyardPairingCodeRecord,
    DockyardPairingConflictError,
    DockyardPairingConsumeRequest,
    DockyardPairingInputError,
    DockyardPairingIssueRequest,
    DockyardPairingStore,
    DockyardPairingStoreError,
    DockyardRevokeDeviceRequest,
    decode_device_record,
    decode_pairing_code_record,
    encode_device_record,
    encode_pairing_code_record,
)


CREATED = "2026-08-02T12:00:00Z"
EXPIRES = "2026-08-02T12:05:00Z"
CONSUMED = "2026-08-02T12:01:00Z"
AUTHED = "2026-08-02T12:02:00Z"
SCOPES = (
    DockyardDeviceScope.APPROVE,
    DockyardDeviceScope.READ,
    DockyardDeviceScope.RETRY,
    DockyardDeviceScope.TERMINATE,
)


class PairingFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.store = DockyardPairingStore(self.root / "pairing")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def issue(self, pairing_id: str = "PAIR-1"):
        return self.store.issue(
            DockyardPairingIssueRequest(
                pairing_id=pairing_id,
                created_at=CREATED,
                expires_at=EXPIRES,
                max_attempts=3,
                operation_id=f"OP-ISSUE-{pairing_id}",
            )
        )

    def consume(self, code: str, device_id: str = "DEVICE-1"):
        return self.store.consume(
            DockyardPairingConsumeRequest(
                pairing_id="PAIR-1",
                pairing_code=code,
                device_id=device_id,
                display_name="My Browser",
                scopes=SCOPES,
                consumed_at=CONSUMED,
                operation_id=f"OP-CONSUME-{device_id}",
            )
        )

    def paired(self):
        issued = self.issue()
        consumed = self.consume(issued.pairing_code)
        return issued, consumed


class DockyardPairingTypeTests(PairingFixture):
    def test_record_fields_are_exact(self) -> None:
        self.assertEqual(
            tuple(field.name for field in dataclasses.fields(DockyardPairingCodeRecord)),
            (
                "schema_version", "pairing_id", "code_verifier", "created_at",
                "expires_at", "consumed_at", "failed_attempts", "max_attempts",
                "last_operation_id", "content_digest",
            ),
        )
        self.assertEqual(
            tuple(field.name for field in dataclasses.fields(DockyardDeviceRecord)),
            (
                "schema_version", "device_id", "display_name", "pairing_id",
                "token_verifier", "scopes", "issued_at", "last_used_at",
                "revoked_at", "generation", "last_operation_id",
                "last_request_digest", "content_digest",
            ),
        )

    def test_public_values_are_frozen_and_slotted(self) -> None:
        module = sys.modules[DockyardPairingStore.__module__]
        for name in (
            "DockyardPairingCodeRecord", "DockyardDeviceRecord",
            "DockyardPairingIssueRequest", "DockyardPairingIssueResult",
            "DockyardPairingConsumeRequest", "DockyardPairingConsumeResult",
            "DockyardAuthenticateRequest", "DockyardAuthenticationResult",
            "DockyardRevokeDeviceRequest",
        ):
            value_type = getattr(module, name)
            self.assertTrue(value_type.__dataclass_params__.frozen)
            self.assertTrue(hasattr(value_type, "__slots__"))

    def test_scopes_are_exact(self) -> None:
        self.assertEqual(
            tuple(scope.value for scope in DockyardDeviceScope),
            ("READ", "APPROVE", "RETRY", "TERMINATE"),
        )

    def test_bool_is_not_integer(self) -> None:
        with self.assertRaisesRegex(DockyardPairingInputError, "issue_max_attempts"):
            DockyardPairingIssueRequest("PAIR", CREATED, EXPIRES, True, "OP")

    def test_ttl_over_ten_minutes_is_rejected(self) -> None:
        with self.assertRaisesRegex(DockyardPairingInputError, "issue_ttl"):
            DockyardPairingIssueRequest(
                "PAIR", CREATED, "2026-08-02T12:11:00Z", 3, "OP"
            )


class DockyardPairingCodecTests(PairingFixture):
    def test_code_and_device_codec_roundtrip(self) -> None:
        issued, consumed = self.paired()
        code_path = self.store.root / "codes" / f"{issued.pairing_id}.json"
        device_path = self.store.root / "devices" / "DEVICE-1.json"
        code_bytes = code_path.read_bytes()
        device_bytes = device_path.read_bytes()
        self.assertEqual(encode_pairing_code_record(decode_pairing_code_record(code_bytes)), code_bytes)
        self.assertEqual(encode_device_record(decode_device_record(device_bytes)), device_bytes)
        self.assertEqual(decode_device_record(device_bytes), consumed.device)

    def test_plaintext_code_and_token_are_never_persisted(self) -> None:
        issued, consumed = self.paired()
        durable = b"".join(path.read_bytes() for path in self.store.root.rglob("*.json"))
        self.assertNotIn(issued.pairing_code.encode(), durable)
        self.assertNotIn(consumed.device_token.encode(), durable)
        self.assertIn(b"sha256:", durable)

    def test_plaintext_code_and_token_are_excluded_from_repr(self) -> None:
        issued, consumed = self.paired()
        self.assertNotIn(issued.pairing_code, repr(issued))
        self.assertNotIn(consumed.device_token, repr(consumed))

    def test_tampered_device_digest_fails_closed(self) -> None:
        _, consumed = self.paired()
        data = encode_device_record(consumed.device).replace(b"My Browser", b"Evil Browser")
        with self.assertRaises(DockyardPairingStoreError):
            decode_device_record(data)


class DockyardPairingLifecycleTests(PairingFixture):
    def test_issue_returns_high_entropy_single_use_code(self) -> None:
        issued = self.issue()
        self.assertEqual(issued.pairing_id, "PAIR-1")
        self.assertGreaterEqual(len(issued.pairing_code), 40)
        record = decode_pairing_code_record(
            (self.store.root / "codes" / "PAIR-1.json").read_bytes()
        )
        self.assertNotEqual(record.code_verifier, issued.pairing_code)
        self.assertIsNone(record.consumed_at)

    def test_consume_returns_token_and_freezes_device(self) -> None:
        issued = self.issue()
        result = self.consume(issued.pairing_code)
        self.assertGreaterEqual(len(result.device_token), 40)
        self.assertEqual(result.device.scopes, SCOPES)
        self.assertEqual(result.device.generation, 1)
        code = decode_pairing_code_record(
            (self.store.root / "codes" / "PAIR-1.json").read_bytes()
        )
        self.assertEqual(code.consumed_at, CONSUMED)

    def test_consumed_code_cannot_be_reused(self) -> None:
        issued = self.issue()
        self.consume(issued.pairing_code)
        with self.assertRaisesRegex(DockyardPairingConflictError, "pairing_consumed"):
            self.consume(issued.pairing_code, "DEVICE-2")

    def test_expired_code_fails_closed(self) -> None:
        issued = self.issue()
        request = DockyardPairingConsumeRequest(
            "PAIR-1", issued.pairing_code, "DEVICE-1", "Browser", SCOPES,
            "2026-08-02T12:06:00Z", "OP-LATE",
        )
        with self.assertRaisesRegex(DockyardPairingAuthenticationError, "pairing_expired"):
            self.store.consume(request)

    def test_wrong_code_increments_attempts_and_exhausts(self) -> None:
        self.issue()
        for index in range(3):
            with self.assertRaisesRegex(DockyardPairingAuthenticationError, "pairing_code"):
                self.store.consume(
                    DockyardPairingConsumeRequest(
                        "PAIR-1", f"wrong-{index}", "DEVICE-1", "Browser", SCOPES,
                        CONSUMED, f"OP-WRONG-{index}",
                    )
                )
        with self.assertRaisesRegex(DockyardPairingAuthenticationError, "pairing_exhausted"):
            self.store.consume(
                DockyardPairingConsumeRequest(
                    "PAIR-1", "wrong-final", "DEVICE-1", "Browser", SCOPES,
                    CONSUMED, "OP-WRONG-FINAL",
                )
            )


class DockyardAuthenticationTests(PairingFixture):
    def auth_request(self, token: str, scope=DockyardDeviceScope.READ, operation="OP-AUTH"):
        return DockyardAuthenticateRequest("DEVICE-1", token, scope, AUTHED, operation)

    def test_authentication_updates_last_used_and_generation(self) -> None:
        _, consumed = self.paired()
        result = self.store.authenticate(self.auth_request(consumed.device_token))
        self.assertEqual(result.generation, 2)
        self.assertFalse(result.replayed)
        device = self.store.read_device("DEVICE-1")
        self.assertEqual(device.last_used_at, AUTHED)  # type: ignore[union-attr]

    def test_byte_exact_auth_replay(self) -> None:
        _, consumed = self.paired()
        first = self.store.authenticate(self.auth_request(consumed.device_token))
        second = self.store.authenticate(self.auth_request(consumed.device_token))
        self.assertEqual(first.generation, second.generation)
        self.assertTrue(second.replayed)

    def test_replay_does_not_bypass_token_check(self) -> None:
        _, consumed = self.paired()
        self.store.authenticate(self.auth_request(consumed.device_token))
        with self.assertRaisesRegex(DockyardPairingAuthenticationError, "device_token"):
            self.store.authenticate(self.auth_request("evil-token"))

    def test_wrong_token_fails(self) -> None:
        self.paired()
        with self.assertRaisesRegex(DockyardPairingAuthenticationError, "device_token"):
            self.store.authenticate(self.auth_request("wrong"))

    def test_missing_scope_fails(self) -> None:
        issued = self.issue()
        consumed = self.store.consume(
            DockyardPairingConsumeRequest(
                "PAIR-1", issued.pairing_code, "DEVICE-1", "Browser",
                (DockyardDeviceScope.READ,), CONSUMED, "OP-CONSUME",
            )
        )
        with self.assertRaisesRegex(DockyardPairingAuthenticationError, "device_scope"):
            self.store.authenticate(
                self.auth_request(consumed.device_token, DockyardDeviceScope.TERMINATE)
            )

    def test_clock_rollback_fails(self) -> None:
        _, consumed = self.paired()
        request = dataclasses.replace(
            self.auth_request(consumed.device_token),
            authenticated_at="2026-08-02T11:59:00Z",
        )
        with self.assertRaisesRegex(DockyardPairingAuthenticationError, "clock_rollback"):
            self.store.authenticate(request)


class DockyardRevocationTests(PairingFixture):
    def test_revoke_device_blocks_future_authentication(self) -> None:
        _, consumed = self.paired()
        revoked = self.store.revoke_device(
            DockyardRevokeDeviceRequest(
                "DEVICE-1", 1, "2026-08-02T12:03:00Z", "OP-REVOKE"
            )
        )
        self.assertEqual(revoked.generation, 2)
        self.assertIsNotNone(revoked.revoked_at)
        with self.assertRaisesRegex(DockyardPairingAuthenticationError, "device_revoked"):
            self.store.authenticate(
                DockyardAuthenticateRequest(
                    "DEVICE-1", consumed.device_token, DockyardDeviceScope.READ,
                    "2026-08-02T12:04:00Z", "OP-AUTH-AFTER-REVOKE",
                )
            )

    def test_stale_revoke_generation_is_rejected(self) -> None:
        self.paired()
        with self.assertRaisesRegex(DockyardPairingConflictError, "stale_generation"):
            self.store.revoke_device(
                DockyardRevokeDeviceRequest(
                    "DEVICE-1", 2, "2026-08-02T12:03:00Z", "OP-REVOKE"
                )
            )

    def test_revoke_all_revokes_every_device(self) -> None:
        first = self.issue("PAIR-1")
        self.consume(first.pairing_code, "DEVICE-1")
        second = self.issue("PAIR-2")
        self.store.consume(
            DockyardPairingConsumeRequest(
                "PAIR-2", second.pairing_code, "DEVICE-2", "Second", SCOPES,
                CONSUMED, "OP-CONSUME-2",
            )
        )
        records = self.store.revoke_all("2026-08-02T12:03:00Z", "OP-REVOKE-ALL")
        self.assertEqual(tuple(record.device_id for record in records), ("DEVICE-1", "DEVICE-2"))
        self.assertTrue(all(record.revoked_at is not None for record in records))


class DockyardPairingConcurrencyTests(PairingFixture):
    def test_two_consumers_have_one_winner(self) -> None:
        issued = self.issue()
        barrier = threading.Barrier(2)
        successes = []
        failures = []

        def consume(device_id: str) -> None:
            try:
                barrier.wait(timeout=5)
                successes.append(self.consume(issued.pairing_code, device_id))
            except BaseException as exc:  # test capture only
                failures.append(exc)

        threads = [
            threading.Thread(target=consume, args=("DEVICE-1",)),
            threading.Thread(target=consume, args=("DEVICE-2",)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(len(successes), 1)
        self.assertEqual(len(failures), 1)
        self.assertIsInstance(failures[0], DockyardPairingConflictError)


class DockyardPairingSourceBoundaryTests(PairingFixture):
    def test_source_has_no_network_provider_model_or_logging(self) -> None:
        source = inspect.getsource(sys.modules[DockyardPairingStore.__module__])
        for forbidden in (
            "import socket", "import requests", "import urllib", "subprocess",
            "from worker_adapter", "from workflow_orchestrator",
            "from claude_code_provider", "from codex_cli_provider",
            "from reasonix_cli_provider", "logging.", "print(", "API_KEY",
            "os.environ",
        ):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
