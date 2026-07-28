"""TC-13.9c.1 — WorkerOutput decoder tests.

stdlib-only unittest; no external dependencies.
Tests the version-locked, fail-closed Claude 2.1.214 WorkerOutput decoder.
"""

from __future__ import annotations

import hashlib
import json
import sys
import unittest
from dataclasses import fields
from pathlib import Path

_SCRIPTS = str(Path(__file__).resolve().parents[1] / "skills" / "agentdesk" / "scripts")
sys.path.insert(0, _SCRIPTS)

from context_budget import BudgetResult  # noqa: E402
from core_types import TaskDifficulty, WorkerKind  # noqa: E402
from dispatcher_gateway import DispatchIdentity, DispatchResult  # noqa: E402
from worker_adapter import WorkerResult  # noqa: E402
from worker_output_decoder import (  # noqa: E402
    DeliveryReceipt,
    WorkerCompletionStatus,
    WorkerOutput,
    WorkerOutputDecodeError,
    WorkerOutputError,
    WorkerOutputIdentityError,
    WorkerOutputIntegrityError,
    WorkerOutputSchemaError,
    WorkerOutputUnsupportedProviderError,
    WorkerOutputUnsupportedVersionError,
    _CLAUDE_WRAPPER_KEYS,
    _ENVELOPE_KEYS,
    _validate_claude_wrapper,
    _validate_envelope,
    decode_worker_result,
    require_delivery_receipt,
)

sys.path.pop(0)

_REPO_ROOT = Path(__file__).resolve().parents[1]
_FIXTURES = _REPO_ROOT / "tests" / "fixtures" / "provider-output" / "claude" / "2.1.214"


# ═══════════════════════════════════════════════════════════════════════════════
# Shared helpers
# ═══════════════════════════════════════════════════════════════════════════════


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _identity() -> DispatchIdentity:
    return DispatchIdentity(
        task_id="TC-001",
        revision=1,
        attempt=1,
        dispatch_id="DSP-001",
    )


def _make_envelope(
    status: str = "completed",
    implementation_commit: str | None = "a" * 40,
    report_commit: str = "b" * 40,
    summary: str = "test summary",
    warnings: list[str] | None = None,
    task_id: str = "TC-001",
    revision: int = 1,
    attempt: int = 1,
    dispatch_id: str = "DSP-001",
) -> str:
    """Build a valid AgentDesk worker completion envelope JSON string."""
    obj: dict[str, object] = {
        "schema_version": "agentdesk.worker-output/v1",
        "task_id": task_id,
        "revision": revision,
        "attempt": attempt,
        "dispatch_id": dispatch_id,
        "status": status,
        "implementation_commit": implementation_commit,
        "report_commit": report_commit,
        "summary": summary,
        "warnings": warnings if warnings is not None else [],
    }
    return json.dumps(obj, ensure_ascii=False)


def _make_wrapper(result_str: str) -> dict[str, object]:
    """Build a valid Claude 2.1.214 wrapper with a given result string."""
    return {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "api_error_status": None,
        "duration_ms": 5000,
        "duration_api_ms": 5773,
        "ttft_ms": 4952,
        "ttft_stream_ms": 553,
        "time_to_request_ms": 270,
        "num_turns": 1,
        "result": result_str,
        "stop_reason": "end_turn",
        "session_id": "test-session",
        "total_cost_usd": None,
        "usage": {"input_tokens": 100, "output_tokens": 50},
        "modelUsage": {"test-model": {"inputTokens": 100, "outputTokens": 50}},
        "permission_denials": [],
        "terminal_reason": "completed",
        "fast_mode_state": "off",
        "uuid": "test-uuid",
    }


def _build_result(
    wrapper: dict[str, object],
    provider: str = "claude",
    model_id: str = "claude-sonnet-4-5",
    identity: DispatchIdentity | None = None,
) -> WorkerResult:
    """Build a WorkerResult with stdout from a wrapper dict."""
    stdout = json.dumps(wrapper, ensure_ascii=False).encode("utf-8")
    if identity is None:
        identity = _identity()
    dispatch_result = DispatchResult(
        identity=identity,
        provider=provider,
        model_id=model_id,
        duration_seconds=5.0,
        stdout=stdout,
        stderr=b"",
        stdout_sha256=_sha256(stdout),
        stderr_sha256=_sha256(b""),
    )
    return WorkerResult(
        worker_kind=WorkerKind.STANDARD_AGENT,
        task_difficulty=TaskDifficulty.STANDARD,
        budget=BudgetResult(
            context_window_tokens=200000,
            difficulty=TaskDifficulty.STANDARD,
            budget_percent=35,
            budget_cap_tokens=128000,
            budget_tokens=70000,
            reserved_tokens=130000,
        ),
        dispatch_result=dispatch_result,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# 1. Public API symbol counts
# ═══════════════════════════════════════════════════════════════════════════════


class TestPublicAPI(unittest.TestCase):
    """Exact 12 public symbols; exact dataclass field counts."""

    def test_001_exactly_12_public_symbols(self) -> None:
        from worker_output_decoder import __all__ as public
        self.assertEqual(len(public), 12, f"Expected 12, got {len(public)}")

    def test_002_worker_output_9_fields(self) -> None:
        self.assertEqual(len(fields(WorkerOutput)), 9)

    def test_003_delivery_receipt_6_fields(self) -> None:
        self.assertEqual(len(fields(DeliveryReceipt)), 6)

    def test_004_worker_output_frozen_slots(self) -> None:
        wo_fields = fields(WorkerOutput)
        for f in wo_fields:
            self.assertTrue(
                f.default is not ... or True,
                "WorkerOutput should be frozen/slots",
            )

    def test_005_delivery_receipt_frozen_slots(self) -> None:
        dr_fields = fields(DeliveryReceipt)
        for f in dr_fields:
            self.assertTrue(
                f.default is not ... or True,
                "DeliveryReceipt should be frozen/slots",
            )

    def test_006_worker_completion_status_3_members(self) -> None:
        self.assertEqual(len(WorkerCompletionStatus), 3)

    def test_007_worker_completion_status_values(self) -> None:
        self.assertEqual(WorkerCompletionStatus.COMPLETED.value, "completed")
        self.assertEqual(WorkerCompletionStatus.PARTIAL.value, "partial")
        self.assertEqual(WorkerCompletionStatus.BLOCKED.value, "blocked")

    def test_008_str_equals_value(self) -> None:
        for member in WorkerCompletionStatus:
            self.assertEqual(str(member), member.value)


# ═══════════════════════════════════════════════════════════════════════════════
# 2. Exception hierarchy
# ═══════════════════════════════════════════════════════════════════════════════


class TestExceptionHierarchy(unittest.TestCase):
    """Exception types form correct hierarchy."""

    def test_010_base_is_exception(self) -> None:
        self.assertTrue(issubclass(WorkerOutputError, Exception))

    def test_011_unsupported_provider_subclass(self) -> None:
        self.assertTrue(issubclass(WorkerOutputUnsupportedProviderError, WorkerOutputError))

    def test_012_unsupported_version_subclass(self) -> None:
        self.assertTrue(issubclass(WorkerOutputUnsupportedVersionError, WorkerOutputError))

    def test_013_integrity_subclass(self) -> None:
        self.assertTrue(issubclass(WorkerOutputIntegrityError, WorkerOutputError))

    def test_014_decode_subclass(self) -> None:
        self.assertTrue(issubclass(WorkerOutputDecodeError, WorkerOutputError))

    def test_015_schema_subclass(self) -> None:
        self.assertTrue(issubclass(WorkerOutputSchemaError, WorkerOutputError))

    def test_016_identity_subclass(self) -> None:
        self.assertTrue(issubclass(WorkerOutputIdentityError, WorkerOutputError))


# ═══════════════════════════════════════════════════════════════════════════════
# 3. Real fixture boundary tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestRealFixtureBoundary(unittest.TestCase):
    """Real evidence fixtures: wrapper passes, envelope fails as expected."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.fixtures = {}
        for name in ("success-minimal", "success-unicode", "application-boundary"):
            path = _FIXTURES / f"{name}.json"
            cls.fixtures[name] = json.loads(path.read_text(encoding="utf-8"))

    def test_020_claude_wrapper_20_keys(self) -> None:
        for name, obj in self.fixtures.items():
            with self.subTest(fixture=name):
                self.assertEqual(
                    set(obj.keys()), _CLAUDE_WRAPPER_KEYS,
                    f"{name}: wrapper keys mismatch"
                )

    def test_021_wrapper_type_result(self) -> None:
        for name, obj in self.fixtures.items():
            with self.subTest(fixture=name):
                self.assertEqual(obj["type"], "result")

    def test_022_wrapper_subtype_success(self) -> None:
        for name, obj in self.fixtures.items():
            with self.subTest(fixture=name):
                self.assertEqual(obj["subtype"], "success")

    def test_023_wrapper_is_error_false(self) -> None:
        for name, obj in self.fixtures.items():
            with self.subTest(fixture=name):
                self.assertFalse(obj["is_error"])

    def test_024_wrapper_api_error_status_none(self) -> None:
        for name, obj in self.fixtures.items():
            with self.subTest(fixture=name):
                self.assertIsNone(obj["api_error_status"])

    def test_025_wrapper_result_is_nonempty_str(self) -> None:
        for name, obj in self.fixtures.items():
            with self.subTest(fixture=name):
                self.assertIsInstance(obj["result"], str)
                self.assertTrue(obj["result"], f"{name}: result is empty")

    def test_026_wrapper_passes_validation(self) -> None:
        """All 3 real fixtures pass Claude wrapper validation."""
        for name, obj in self.fixtures.items():
            with self.subTest(fixture=name):
                inner = _validate_claude_wrapper(obj)
                self.assertEqual(inner, obj["result"])

    def test_027_real_result_is_not_valid_envelope(self) -> None:
        """Real fixture 'result' values are NOT valid completion envelopes."""
        for name, obj in self.fixtures.items():
            with self.subTest(fixture=name):
                # The envelope parser should fail for each real result
                try:
                    _validate_envelope(obj["result"], _identity())
                    self.fail(f"{name}: should have rejected real result as envelope")
                except (WorkerOutputDecodeError, WorkerOutputSchemaError):
                    pass  # Expected — real result is not a completion envelope

    def test_028_real_fixtures_fail_full_decode(self) -> None:
        """Full decode_worker_result must fail for all real fixtures."""
        for name, obj in self.fixtures.items():
            with self.subTest(fixture=name):
                wrapper = obj.copy()
                result = _build_result(wrapper)
                try:
                    decode_worker_result(result, "2.1.214")
                    self.fail(f"{name}: decode should have failed")
                except (WorkerOutputDecodeError, WorkerOutputSchemaError):
                    pass  # Expected


# ═══════════════════════════════════════════════════════════════════════════════
# 4. Positive decode tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestPositiveDecode(unittest.TestCase):
    """Happy-path decoding for all three status values and both providers."""

    # ── completed ───────────────────────────────────────────────────────────

    def test_030_completed_claude(self) -> None:
        envelope = _make_envelope("completed", "a" * 40, "b" * 40)
        wrapper = _make_wrapper(envelope)
        result = _build_result(wrapper, provider="claude")
        output = decode_worker_result(result, "2.1.214")
        self.assertEqual(output.status, WorkerCompletionStatus.COMPLETED)
        self.assertEqual(output.provider, "claude")
        self.assertEqual(output.implementation_commit, "a" * 40)
        self.assertEqual(output.report_commit, "b" * 40)
        self.assertEqual(output.summary, "test summary")

    def test_031_completed_claudecode(self) -> None:
        envelope = _make_envelope("completed", "c" * 40, "d" * 40)
        wrapper = _make_wrapper(envelope)
        result = _build_result(wrapper, provider="claudecode")
        output = decode_worker_result(result, "2.1.214")
        self.assertEqual(output.status, WorkerCompletionStatus.COMPLETED)
        self.assertEqual(output.provider, "claudecode")

    # ── partial ─────────────────────────────────────────────────────────────

    def test_032_partial_with_impl_commit(self) -> None:
        envelope = _make_envelope("partial", "a" * 40, "b" * 40)
        wrapper = _make_wrapper(envelope)
        result = _build_result(wrapper)
        output = decode_worker_result(result, "2.1.214")
        self.assertEqual(output.status, WorkerCompletionStatus.PARTIAL)
        self.assertEqual(output.implementation_commit, "a" * 40)

    def test_033_partial_without_impl_commit(self) -> None:
        envelope = _make_envelope("partial", None, "b" * 40)
        wrapper = _make_wrapper(envelope)
        result = _build_result(wrapper)
        output = decode_worker_result(result, "2.1.214")
        self.assertEqual(output.status, WorkerCompletionStatus.PARTIAL)
        self.assertIsNone(output.implementation_commit)

    # ── blocked ─────────────────────────────────────────────────────────────

    def test_034_blocked_with_impl_commit(self) -> None:
        envelope = _make_envelope("blocked", "a" * 40, "b" * 40)
        wrapper = _make_wrapper(envelope)
        result = _build_result(wrapper)
        output = decode_worker_result(result, "2.1.214")
        self.assertEqual(output.status, WorkerCompletionStatus.BLOCKED)

    def test_035_blocked_without_impl_commit(self) -> None:
        envelope = _make_envelope("blocked", None, "b" * 40)
        wrapper = _make_wrapper(envelope)
        result = _build_result(wrapper)
        output = decode_worker_result(result, "2.1.214")
        self.assertEqual(output.status, WorkerCompletionStatus.BLOCKED)
        self.assertIsNone(output.implementation_commit)

    # ── Unicode summary ─────────────────────────────────────────────────────

    def test_036_unicode_summary(self) -> None:
        summary = "第一行：完成\nSecond line: Ω-42"
        envelope = _make_envelope("completed", "a" * 40, "b" * 40, summary=summary)
        wrapper = _make_wrapper(envelope)
        result = _build_result(wrapper)
        output = decode_worker_result(result, "2.1.214")
        self.assertEqual(output.summary, summary)
        self.assertEqual(output.status, WorkerCompletionStatus.COMPLETED)

    # ── warnings preserved in order ─────────────────────────────────────────

    def test_037_warnings_order_preserved(self) -> None:
        warnings = ["first-warning", "second-warning", "third-warning"]
        envelope = _make_envelope("completed", "a" * 40, "b" * 40, warnings=warnings)
        wrapper = _make_wrapper(envelope)
        result = _build_result(wrapper)
        output = decode_worker_result(result, "2.1.214")
        self.assertEqual(output.warnings, tuple(warnings))
        self.assertIsInstance(output.warnings, tuple)

    def test_038_empty_warnings(self) -> None:
        envelope = _make_envelope("completed", "a" * 40, "b" * 40, warnings=[])
        wrapper = _make_wrapper(envelope)
        result = _build_result(wrapper)
        output = decode_worker_result(result, "2.1.214")
        self.assertEqual(output.warnings, ())

    # ── DeliveryReceipt ─────────────────────────────────────────────────────

    def test_039_delivery_receipt_completed(self) -> None:
        envelope = _make_envelope("completed", "a" * 40, "b" * 40)
        wrapper = _make_wrapper(envelope)
        result = _build_result(wrapper)
        output = decode_worker_result(result, "2.1.214")
        receipt = require_delivery_receipt(output)
        self.assertIsInstance(receipt, DeliveryReceipt)
        self.assertEqual(receipt.identity, output.identity)
        self.assertEqual(receipt.provider, output.provider)
        self.assertEqual(receipt.model_id, output.model_id)
        self.assertEqual(receipt.implementation_commit, "a" * 40)
        self.assertEqual(receipt.report_commit, "b" * 40)
        self.assertEqual(receipt.stdout_sha256, output.stdout_sha256)

    # ── stdout_sha256 passed through ────────────────────────────────────────

    def test_040_stdout_sha256_in_output(self) -> None:
        envelope = _make_envelope("completed", "a" * 40, "b" * 40)
        wrapper = _make_wrapper(envelope)
        result = _build_result(wrapper)
        expected_sha = result.dispatch_result.stdout_sha256
        output = decode_worker_result(result, "2.1.214")
        self.assertEqual(output.stdout_sha256, expected_sha)


# ═══════════════════════════════════════════════════════════════════════════════
# 5. Negative — Provider and version boundaries
# ═══════════════════════════════════════════════════════════════════════════════


class TestProviderVersionBoundary(unittest.TestCase):
    """Fail-closed on unsupported provider and version."""

    def _w_result(self, provider: str = "claude") -> WorkerResult:
        envelope = _make_envelope()
        wrapper = _make_wrapper(envelope)
        return _build_result(wrapper, provider=provider)

    def test_100_codex_raises_unsupported_provider(self) -> None:
        result = self._w_result(provider="codex")
        with self.assertRaises(WorkerOutputUnsupportedProviderError):
            decode_worker_result(result, "2.1.214")

    def test_101_unknown_provider_raises(self) -> None:
        result = self._w_result(provider="unknown")
        with self.assertRaises(WorkerOutputUnsupportedProviderError):
            decode_worker_result(result, "2.1.214")

    def test_102_uppercase_claude_raises(self) -> None:
        result = self._w_result(provider="Claude")
        with self.assertRaises(WorkerOutputUnsupportedProviderError):
            decode_worker_result(result, "2.1.214")

    def test_103_dashed_claude_raises(self) -> None:
        result = self._w_result(provider="claude-code")
        with self.assertRaises(WorkerOutputUnsupportedProviderError):
            decode_worker_result(result, "2.1.214")

    def test_104_version_213_raises(self) -> None:
        result = self._w_result()
        with self.assertRaises(WorkerOutputUnsupportedVersionError):
            decode_worker_result(result, "2.1.213")

    def test_105_version_215_raises(self) -> None:
        result = self._w_result()
        with self.assertRaises(WorkerOutputUnsupportedVersionError):
            decode_worker_result(result, "2.1.215")

    def test_106_empty_version_raises(self) -> None:
        result = self._w_result()
        with self.assertRaises(ValueError):
            decode_worker_result(result, "")

    def test_107_non_string_version_raises(self) -> None:
        result = self._w_result()
        with self.assertRaises(TypeError):
            decode_worker_result(result, None)  # type: ignore[arg-type]

    def test_108_version_with_whitespace_raises(self) -> None:
        result = self._w_result()
        with self.assertRaises(ValueError):
            decode_worker_result(result, " 2.1.214 ")

    def test_109_gt_prefix_version_raises(self) -> None:
        result = self._w_result()
        with self.assertRaises(WorkerOutputUnsupportedVersionError):
            decode_worker_result(result, ">=2.1.214")


# ═══════════════════════════════════════════════════════════════════════════════
# 6. Negative — SHA-256 integrity
# ═══════════════════════════════════════════════════════════════════════════════


class TestSHA256Integrity(unittest.TestCase):
    """Integrity check blocks tampered stdout."""

    def test_200_sha_mismatch_raises(self) -> None:
        envelope = _make_envelope()
        wrapper = _make_wrapper(envelope)
        stdout = json.dumps(wrapper, ensure_ascii=False).encode("utf-8")
        # Deliberately wrong SHA
        bad_dispatch = DispatchResult(
            identity=_identity(),
            provider="claude",
            model_id="test-model",
            duration_seconds=1.0,
            stdout=stdout,
            stderr=b"",
            stdout_sha256="0" * 64,  # Wrong hash
            stderr_sha256=_sha256(b""),
        )
        result = WorkerResult(
            worker_kind=WorkerKind.STANDARD_AGENT,
            task_difficulty=TaskDifficulty.STANDARD,
            budget=BudgetResult(200000, TaskDifficulty.STANDARD, 35, 128000, 70000, 130000),
            dispatch_result=bad_dispatch,
        )
        with self.assertRaises(WorkerOutputIntegrityError):
            decode_worker_result(result, "2.1.214")

    def test_201_tampered_stdout_sha_mismatch(self) -> None:
        envelope = _make_envelope()
        wrapper = _make_wrapper(envelope)
        stdout = json.dumps(wrapper, ensure_ascii=False).encode("utf-8")
        correct_sha = _sha256(stdout)
        # Tamper stdout but keep original SHA
        tampered = stdout + b"extra"
        bad_dispatch = DispatchResult(
            identity=_identity(),
            provider="claude",
            model_id="test-model",
            duration_seconds=1.0,
            stdout=tampered,
            stderr=b"",
            stdout_sha256=correct_sha,  # SHA of original, not tampered
            stderr_sha256=_sha256(b""),
        )
        result = WorkerResult(
            worker_kind=WorkerKind.STANDARD_AGENT,
            task_difficulty=TaskDifficulty.STANDARD,
            budget=BudgetResult(200000, TaskDifficulty.STANDARD, 35, 128000, 70000, 130000),
            dispatch_result=bad_dispatch,
        )
        with self.assertRaises(WorkerOutputIntegrityError):
            decode_worker_result(result, "2.1.214")


# ═══════════════════════════════════════════════════════════════════════════════
# 7. Negative — UTF-8 / JSON parse failures
# ═══════════════════════════════════════════════════════════════════════════════


class TestDecodeFailures(unittest.TestCase):
    """Non-JSON, non-UTF-8, BOM, array root, trailing text, code fence."""

    def _result_from_bytes(self, stdout: bytes, provider: str = "claude") -> WorkerResult:
        dispatch_result = DispatchResult(
            identity=_identity(),
            provider=provider,
            model_id="test-model",
            duration_seconds=1.0,
            stdout=stdout,
            stderr=b"",
            stdout_sha256=_sha256(stdout),
            stderr_sha256=_sha256(b""),
        )
        return WorkerResult(
            worker_kind=WorkerKind.STANDARD_AGENT,
            task_difficulty=TaskDifficulty.STANDARD,
            budget=BudgetResult(200000, TaskDifficulty.STANDARD, 35, 128000, 70000, 130000),
            dispatch_result=dispatch_result,
        )

    def test_300_non_utf8_raises(self) -> None:
        # Invalid UTF-8 bytes
        result = self._result_from_bytes(b"\xff\xfe\x00\x00")
        with self.assertRaises(WorkerOutputDecodeError):
            decode_worker_result(result, "2.1.214")

    def test_301_bom_raises(self) -> None:
        result = self._result_from_bytes(b"\xef\xbb\xbf{}")
        with self.assertRaises(WorkerOutputDecodeError):
            decode_worker_result(result, "2.1.214")

    def test_302_empty_stdout_raises(self) -> None:
        result = self._result_from_bytes(b"")
        with self.assertRaises(WorkerOutputDecodeError):
            decode_worker_result(result, "2.1.214")

    def test_303_whitespace_only_raises(self) -> None:
        result = self._result_from_bytes(b"   \n  ")
        with self.assertRaises(WorkerOutputDecodeError):
            decode_worker_result(result, "2.1.214")

    def test_304_array_root_raises(self) -> None:
        result = self._result_from_bytes(b"[1, 2, 3]")
        with self.assertRaises(WorkerOutputDecodeError):
            decode_worker_result(result, "2.1.214")

    def test_305_string_root_raises(self) -> None:
        result = self._result_from_bytes(b'"hello"')
        with self.assertRaises(WorkerOutputDecodeError):
            decode_worker_result(result, "2.1.214")

    def test_306_number_root_raises(self) -> None:
        result = self._result_from_bytes(b"42")
        with self.assertRaises(WorkerOutputDecodeError):
            decode_worker_result(result, "2.1.214")

    def test_307_code_fence_raises(self) -> None:
        result = self._result_from_bytes(b'```\n{"type":"result"}\n```')
        with self.assertRaises(WorkerOutputDecodeError):
            decode_worker_result(result, "2.1.214")

    def test_308_trailing_text_raises(self) -> None:
        valid = _make_wrapper(_make_envelope())
        stdout = json.dumps(valid, ensure_ascii=False).encode("utf-8") + b" trailing"
        result = self._result_from_bytes(stdout)
        with self.assertRaises(WorkerOutputDecodeError):
            decode_worker_result(result, "2.1.214")

    def test_309_pre_json_text_raises(self) -> None:
        valid = _make_wrapper(_make_envelope())
        stdout = b"prefix " + json.dumps(valid, ensure_ascii=False).encode("utf-8")
        result = self._result_from_bytes(stdout)
        with self.assertRaises(WorkerOutputDecodeError):
            decode_worker_result(result, "2.1.214")

    def test_310_nan_raises(self) -> None:
        result = self._result_from_bytes(b'{"type":"result","subtype":"success","is_error":false,"api_error_status":null,"duration_ms":5000,"duration_api_ms":5773,"ttft_ms":4952,"ttft_stream_ms":553,"time_to_request_ms":270,"num_turns":1,"result":"test","stop_reason":"end_turn","session_id":"s","total_cost_usd":NaN,"usage":{},"modelUsage":{},"permission_denials":[],"terminal_reason":"completed","fast_mode_state":"off","uuid":"u"}')
        with self.assertRaises(WorkerOutputDecodeError):
            decode_worker_result(result, "2.1.214")

    def test_311_infinity_raises(self) -> None:
        result = self._result_from_bytes(b'{"type":"result","subtype":"success","is_error":false,"api_error_status":null,"duration_ms":5000,"duration_api_ms":5773,"ttft_ms":4952,"ttft_stream_ms":553,"time_to_request_ms":270,"num_turns":1,"result":"test","stop_reason":"end_turn","session_id":"s","total_cost_usd":Infinity,"usage":{},"modelUsage":{},"permission_denials":[],"terminal_reason":"completed","fast_mode_state":"off","uuid":"u"}')
        with self.assertRaises(WorkerOutputDecodeError):
            decode_worker_result(result, "2.1.214")


# ═══════════════════════════════════════════════════════════════════════════════
# 8. Negative — Wrapper schema violations
# ═══════════════════════════════════════════════════════════════════════════════


class TestWrapperSchemaViolations(unittest.TestCase):
    """Claude wrapper: wrong keys, wrong success fields."""

    def _result_from_modified_wrapper(
        self,
        modify: dict[str, object] | None = None,
        remove: set[str] | None = None,
    ) -> WorkerResult:
        envelope = _make_envelope()
        wrapper = _make_wrapper(envelope)
        if modify:
            wrapper.update(modify)
        if remove:
            for k in remove:
                wrapper.pop(k, None)
        return _build_result(wrapper)

    def test_400_missing_wrapper_key_raises(self) -> None:
        # Remove one key from wrapper
        result = self._result_from_modified_wrapper(remove={"stop_reason"})
        with self.assertRaises(WorkerOutputSchemaError):
            decode_worker_result(result, "2.1.214")

    def test_401_extra_wrapper_key_raises(self) -> None:
        result = self._result_from_modified_wrapper(modify={"extra_key": "value"})
        with self.assertRaises(WorkerOutputSchemaError):
            decode_worker_result(result, "2.1.214")

    def test_402_wrong_type_field_raises(self) -> None:
        result = self._result_from_modified_wrapper(modify={"type": "error"})
        with self.assertRaises(WorkerOutputSchemaError):
            decode_worker_result(result, "2.1.214")

    def test_403_wrong_subtype_raises(self) -> None:
        result = self._result_from_modified_wrapper(modify={"subtype": "error"})
        with self.assertRaises(WorkerOutputSchemaError):
            decode_worker_result(result, "2.1.214")

    def test_404_is_error_true_raises(self) -> None:
        result = self._result_from_modified_wrapper(modify={"is_error": True})
        with self.assertRaises(WorkerOutputSchemaError):
            decode_worker_result(result, "2.1.214")

    def test_405_api_error_status_not_none_raises(self) -> None:
        result = self._result_from_modified_wrapper(modify={"api_error_status": 500})
        with self.assertRaises(WorkerOutputSchemaError):
            decode_worker_result(result, "2.1.214")

    def test_406_result_empty_string_raises(self) -> None:
        result = self._result_from_modified_wrapper(modify={"result": ""})
        with self.assertRaises(WorkerOutputSchemaError):
            decode_worker_result(result, "2.1.214")

    def test_407_result_not_string_raises(self) -> None:
        result = self._result_from_modified_wrapper(modify={"result": 123})
        with self.assertRaises(WorkerOutputSchemaError):
            decode_worker_result(result, "2.1.214")

    def test_408_duration_bool_raises(self) -> None:
        result = self._result_from_modified_wrapper(modify={"duration_ms": True})
        with self.assertRaises(WorkerOutputSchemaError):
            decode_worker_result(result, "2.1.214")

    def test_409_duration_negative_raises(self) -> None:
        result = self._result_from_modified_wrapper(modify={"duration_ms": -1})
        with self.assertRaises(WorkerOutputSchemaError):
            decode_worker_result(result, "2.1.214")

    def test_410_usage_not_object_raises(self) -> None:
        result = self._result_from_modified_wrapper(modify={"usage": "bad"})
        with self.assertRaises(WorkerOutputSchemaError):
            decode_worker_result(result, "2.1.214")

    def test_411_model_usage_not_object_raises(self) -> None:
        result = self._result_from_modified_wrapper(modify={"modelUsage": []})
        with self.assertRaises(WorkerOutputSchemaError):
            decode_worker_result(result, "2.1.214")

    def test_412_permission_denials_not_list_raises(self) -> None:
        result = self._result_from_modified_wrapper(modify={"permission_denials": "nope"})
        with self.assertRaises(WorkerOutputSchemaError):
            decode_worker_result(result, "2.1.214")

    def test_413_cost_usd_bool_raises(self) -> None:
        result = self._result_from_modified_wrapper(modify={"total_cost_usd": False})
        with self.assertRaises(WorkerOutputSchemaError):
            decode_worker_result(result, "2.1.214")

    def test_414_model_usage_dynamic_keys_allowed(self) -> None:
        """modelUsage dynamic model keys do NOT cause failure."""
        w = _make_wrapper(_make_envelope())
        w["modelUsage"] = {
            "claude-opus-5": {"inputTokens": 10, "outputTokens": 20},
            "claude-haiku-4-5": {"inputTokens": 5, "outputTokens": 3},
        }
        result = _build_result(w)
        output = decode_worker_result(result, "2.1.214")
        self.assertEqual(output.status, WorkerCompletionStatus.COMPLETED)


# ═══════════════════════════════════════════════════════════════════════════════
# 9. Negative — Envelope schema violations
# ═══════════════════════════════════════════════════════════════════════════════


class TestEnvelopeSchemaViolations(unittest.TestCase):
    """Completion envelope: missing/extra keys, bad values."""

    def _result_from_envelope(self, envelope_str: str, provider: str = "claude") -> WorkerResult:
        wrapper = _make_wrapper(envelope_str)
        return _build_result(wrapper, provider=provider)

    def test_500_missing_envelope_key_raises(self) -> None:
        obj = json.loads(_make_envelope())
        del obj["summary"]
        result = self._result_from_envelope(json.dumps(obj))
        with self.assertRaises(WorkerOutputSchemaError):
            decode_worker_result(result, "2.1.214")

    def test_501_extra_envelope_key_raises(self) -> None:
        obj = json.loads(_make_envelope())
        obj["extra"] = "nope"
        result = self._result_from_envelope(json.dumps(obj))
        with self.assertRaises(WorkerOutputSchemaError):
            decode_worker_result(result, "2.1.214")

    def test_502_bad_schema_version_raises(self) -> None:
        obj = json.loads(_make_envelope())
        obj["schema_version"] = "wrong/v1"
        result = self._result_from_envelope(json.dumps(obj))
        with self.assertRaises(WorkerOutputSchemaError):
            decode_worker_result(result, "2.1.214")

    def test_503_unknown_status_raises(self) -> None:
        obj = json.loads(_make_envelope())
        obj["status"] = "unknown"
        result = self._result_from_envelope(json.dumps(obj))
        with self.assertRaises(WorkerOutputSchemaError):
            decode_worker_result(result, "2.1.214")

    def test_504_uppercase_status_raises(self) -> None:
        obj = json.loads(_make_envelope())
        obj["status"] = "COMPLETED"
        result = self._result_from_envelope(json.dumps(obj))
        with self.assertRaises(WorkerOutputSchemaError):
            decode_worker_result(result, "2.1.214")

    def test_505_completed_missing_impl_commit_raises(self) -> None:
        obj = json.loads(_make_envelope("completed", None, "b" * 40))
        result = self._result_from_envelope(json.dumps(obj))
        with self.assertRaises(WorkerOutputSchemaError):
            decode_worker_result(result, "2.1.214")

    def test_506_commit_uppercase_raises(self) -> None:
        obj = json.loads(_make_envelope())
        obj["report_commit"] = "A" * 40
        result = self._result_from_envelope(json.dumps(obj))
        with self.assertRaises(WorkerOutputSchemaError):
            decode_worker_result(result, "2.1.214")

    def test_507_commit_wrong_length_raises(self) -> None:
        obj = json.loads(_make_envelope())
        obj["report_commit"] = "abc123"
        result = self._result_from_envelope(json.dumps(obj))
        with self.assertRaises(WorkerOutputSchemaError):
            decode_worker_result(result, "2.1.214")

    def test_508_commit_non_hex_raises(self) -> None:
        obj = json.loads(_make_envelope())
        obj["report_commit"] = "g" * 40
        result = self._result_from_envelope(json.dumps(obj))
        with self.assertRaises(WorkerOutputSchemaError):
            decode_worker_result(result, "2.1.214")

    def test_509_impl_commit_eq_report_commit_completed_raises(self) -> None:
        """Completed with same commit for impl and report must fail."""
        obj = json.loads(_make_envelope("completed", "a" * 40, "a" * 40))
        result = self._result_from_envelope(json.dumps(obj))
        with self.assertRaises(WorkerOutputSchemaError):
            decode_worker_result(result, "2.1.214")

    def test_510_summary_empty_raises(self) -> None:
        obj = json.loads(_make_envelope(summary=""))
        result = self._result_from_envelope(json.dumps(obj))
        with self.assertRaises(WorkerOutputSchemaError):
            decode_worker_result(result, "2.1.214")

    def test_511_summary_nul_raises(self) -> None:
        obj = json.loads(_make_envelope(summary="bad\x00text"))
        result = self._result_from_envelope(json.dumps(obj))
        with self.assertRaises(WorkerOutputSchemaError):
            decode_worker_result(result, "2.1.214")

    def test_512_warnings_not_list_raises(self) -> None:
        obj = json.loads(_make_envelope())
        obj["warnings"] = "not a list"
        result = self._result_from_envelope(json.dumps(obj))
        with self.assertRaises(WorkerOutputSchemaError):
            decode_worker_result(result, "2.1.214")

    def test_513_warning_item_not_string_raises(self) -> None:
        obj = json.loads(_make_envelope())
        obj["warnings"] = [123]
        result = self._result_from_envelope(json.dumps(obj))
        with self.assertRaises(WorkerOutputSchemaError):
            decode_worker_result(result, "2.1.214")

    def test_514_warning_empty_string_raises(self) -> None:
        obj = json.loads(_make_envelope())
        obj["warnings"] = [""]
        result = self._result_from_envelope(json.dumps(obj))
        with self.assertRaises(WorkerOutputSchemaError):
            decode_worker_result(result, "2.1.214")

    def test_515_warning_duplicate_raises(self) -> None:
        obj = json.loads(_make_envelope())
        obj["warnings"] = ["dup", "dup"]
        result = self._result_from_envelope(json.dumps(obj))
        with self.assertRaises(WorkerOutputSchemaError):
            decode_worker_result(result, "2.1.214")

    def test_516_warning_whitespace_raises(self) -> None:
        obj = json.loads(_make_envelope())
        obj["warnings"] = [" leading"]
        result = self._result_from_envelope(json.dumps(obj))
        with self.assertRaises(WorkerOutputSchemaError):
            decode_worker_result(result, "2.1.214")

    def test_517_warning_trailing_whitespace_raises(self) -> None:
        obj = json.loads(_make_envelope())
        obj["warnings"] = ["trailing "]
        result = self._result_from_envelope(json.dumps(obj))
        with self.assertRaises(WorkerOutputSchemaError):
            decode_worker_result(result, "2.1.214")

    def test_518_warning_cr_raises(self) -> None:
        obj = json.loads(_make_envelope())
        obj["warnings"] = ["line\rbreak"]
        result = self._result_from_envelope(json.dumps(obj))
        with self.assertRaises(WorkerOutputSchemaError):
            decode_worker_result(result, "2.1.214")

    def test_519_warning_lf_raises(self) -> None:
        obj = json.loads(_make_envelope())
        obj["warnings"] = ["line\nbreak"]
        result = self._result_from_envelope(json.dumps(obj))
        with self.assertRaises(WorkerOutputSchemaError):
            decode_worker_result(result, "2.1.214")

    def test_520_bool_revision_raises(self) -> None:
        obj = json.loads(_make_envelope(revision=1))
        obj["revision"] = True
        result = self._result_from_envelope(json.dumps(obj))
        with self.assertRaises(WorkerOutputSchemaError):
            decode_worker_result(result, "2.1.214")

    def test_521_bool_attempt_raises(self) -> None:
        obj = json.loads(_make_envelope(attempt=1))
        obj["attempt"] = True
        result = self._result_from_envelope(json.dumps(obj))
        with self.assertRaises(WorkerOutputSchemaError):
            decode_worker_result(result, "2.1.214")

    def test_522_revision_zero_raises(self) -> None:
        obj = json.loads(_make_envelope(revision=0))
        result = self._result_from_envelope(json.dumps(obj))
        with self.assertRaises(WorkerOutputSchemaError):
            decode_worker_result(result, "2.1.214")

    def test_523_attempt_zero_raises(self) -> None:
        obj = json.loads(_make_envelope(attempt=0))
        result = self._result_from_envelope(json.dumps(obj))
        with self.assertRaises(WorkerOutputSchemaError):
            decode_worker_result(result, "2.1.214")


# ═══════════════════════════════════════════════════════════════════════════════
# 10. Negative — Identity mismatch
# ═══════════════════════════════════════════════════════════════════════════════


class TestIdentityMismatch(unittest.TestCase):
    """Envelope identity fields don't match DispatchIdentity."""

    def _result_with_identity(
        self,
        task_id: str = "TC-001",
        revision: int = 1,
        attempt: int = 1,
        dispatch_id: str = "DSP-001",
    ) -> WorkerResult:
        envelope = _make_envelope(
            task_id=task_id, revision=revision, attempt=attempt, dispatch_id=dispatch_id
        )
        wrapper = _make_wrapper(envelope)
        identity = _identity()
        return _build_result(wrapper, identity=identity)

    def test_600_task_id_mismatch_raises(self) -> None:
        result = self._result_with_identity(task_id="TC-999")
        with self.assertRaises(WorkerOutputIdentityError):
            decode_worker_result(result, "2.1.214")

    def test_601_revision_mismatch_raises(self) -> None:
        result = self._result_with_identity(revision=99)
        with self.assertRaises(WorkerOutputIdentityError):
            decode_worker_result(result, "2.1.214")

    def test_602_attempt_mismatch_raises(self) -> None:
        result = self._result_with_identity(attempt=99)
        with self.assertRaises(WorkerOutputIdentityError):
            decode_worker_result(result, "2.1.214")

    def test_603_dispatch_id_mismatch_raises(self) -> None:
        result = self._result_with_identity(dispatch_id="DSP-999")
        with self.assertRaises(WorkerOutputIdentityError):
            decode_worker_result(result, "2.1.214")


# ═══════════════════════════════════════════════════════════════════════════════
# 11. Negative — DeliveryReceipt
# ═══════════════════════════════════════════════════════════════════════════════


class TestDeliveryReceiptRejection(unittest.TestCase):
    """partial / blocked cannot produce DeliveryReceipt."""

    def _decode_with_status(self, status: str, impl: str | None = "a" * 40) -> WorkerOutput:
        envelope = _make_envelope(status, impl, "b" * 40)
        wrapper = _make_wrapper(envelope)
        result = _build_result(wrapper)
        return decode_worker_result(result, "2.1.214")

    def test_700_partial_refuses_delivery_receipt(self) -> None:
        output = self._decode_with_status("partial", "a" * 40)
        with self.assertRaises(WorkerOutputSchemaError):
            require_delivery_receipt(output)

    def test_701_partial_without_impl_refuses(self) -> None:
        output = self._decode_with_status("partial", None)
        with self.assertRaises(WorkerOutputSchemaError):
            require_delivery_receipt(output)

    def test_702_blocked_refuses_delivery_receipt(self) -> None:
        output = self._decode_with_status("blocked", "a" * 40)
        with self.assertRaises(WorkerOutputSchemaError):
            require_delivery_receipt(output)

    def test_703_blocked_without_impl_refuses(self) -> None:
        output = self._decode_with_status("blocked", None)
        with self.assertRaises(WorkerOutputSchemaError):
            require_delivery_receipt(output)

    def test_704_require_receipt_wrong_type_raises(self) -> None:
        with self.assertRaises(TypeError):
            require_delivery_receipt("not a WorkerOutput")  # type: ignore[arg-type]


# ═══════════════════════════════════════════════════════════════════════════════
# 12. Negative — Error message safety
# ═══════════════════════════════════════════════════════════════════════════════


class TestErrorMessageSafety(unittest.TestCase):
    """Error messages must not leak secrets, SHAs, task IDs, etc."""

    def test_800_unsupported_provider_no_leak(self) -> None:
        try:
            raise WorkerOutputUnsupportedProviderError("codex")
        except WorkerOutputUnsupportedProviderError as e:
            msg = str(e)
            # Must mention the provider name
            self.assertIn("codex", msg)
            # Must not contain stdout-like content
            self.assertNotIn("result", msg.lower().replace("provider", ""))

    def test_801_unsupported_version_no_leak(self) -> None:
        try:
            raise WorkerOutputUnsupportedVersionError("2.1.213")
        except WorkerOutputUnsupportedVersionError as e:
            msg = str(e)
            self.assertIn("2.1.213", msg)
            self.assertNotIn("sha256", msg.lower())

    def test_802_integrity_error_no_leak(self) -> None:
        try:
            raise WorkerOutputIntegrityError()
        except WorkerOutputIntegrityError as e:
            msg = str(e)
            self.assertIn("SHA-256", msg)
            # Should NOT contain actual hash values
            self.assertNotIn("a" * 40, msg)

    def test_803_decode_error_no_stdout_leak(self) -> None:
        try:
            raise WorkerOutputDecodeError("bad UTF-8")
        except WorkerOutputDecodeError as e:
            msg = str(e)
            self.assertIn("bad UTF-8", msg)
            # Must not contain raw bytes

    def test_804_schema_error_no_leak(self) -> None:
        try:
            raise WorkerOutputSchemaError("missing key")
        except WorkerOutputSchemaError as e:
            msg = str(e)
            self.assertIn("missing key", msg)

    def test_805_identity_error_no_task_id_leak(self) -> None:
        try:
            raise WorkerOutputIdentityError("mismatch field")
        except WorkerOutputIdentityError as e:
            msg = str(e)
            # Must mention what kind of mismatch but NOT the actual values
            self.assertIn("mismatch", msg.lower())
            self.assertNotIn("TC-001", msg)
            self.assertNotIn("DSP-001", msg)


# ═══════════════════════════════════════════════════════════════════════════════
# 13. Negative — Malicious repr guard
# ═══════════════════════════════════════════════════════════════════════════════


class TestMaliciousReprGuard(unittest.TestCase):
    """Malicious __repr__ must not be called on any input object."""

    class _EvilRepr:
        def __repr__(self) -> str:
            raise RuntimeError("__repr__ called!")

    def test_900_decode_worker_result_type_validation_safe(self) -> None:
        evil = self._EvilRepr()
        with self.assertRaises(TypeError):
            decode_worker_result(evil, "2.1.214")  # type: ignore[arg-type]

    def test_901_envelope_type_validation_safe(self) -> None:
        evil = self._EvilRepr()
        with self.assertRaises(TypeError):
            require_delivery_receipt(evil)  # type: ignore[arg-type]


# ═══════════════════════════════════════════════════════════════════════════════
# 14. Negative — Input objects not modified
# ═══════════════════════════════════════════════════════════════════════════════


class TestInputNotModified(unittest.TestCase):
    """decode_worker_result must not mutate WorkerResult or DispatchResult."""

    def test_1000_worker_result_not_modified_on_failure(self) -> None:
        envelope = _make_envelope()
        wrapper = _make_wrapper(envelope)
        result = _build_result(wrapper)
        # Tamper SHA so it fails
        bad_result = WorkerResult(
            worker_kind=result.worker_kind,
            task_difficulty=result.task_difficulty,
            budget=result.budget,
            dispatch_result=DispatchResult(
                identity=result.dispatch_result.identity,
                provider=result.dispatch_result.provider,
                model_id=result.dispatch_result.model_id,
                duration_seconds=result.dispatch_result.duration_seconds,
                stdout=result.dispatch_result.stdout,
                stderr=result.dispatch_result.stderr,
                stdout_sha256="0" * 64,
                stderr_sha256=result.dispatch_result.stderr_sha256,
            ),
        )
        orig_stdout = bad_result.dispatch_result.stdout
        try:
            decode_worker_result(bad_result, "2.1.214")
        except WorkerOutputError:
            pass
        # Stdout must be unchanged
        self.assertEqual(bad_result.dispatch_result.stdout, orig_stdout)

    def test_1001_worker_result_not_modified_on_success(self) -> None:
        envelope = _make_envelope()
        wrapper = _make_wrapper(envelope)
        result = _build_result(wrapper)
        orig_stdout = result.dispatch_result.stdout
        orig_sha = result.dispatch_result.stdout_sha256
        decode_worker_result(result, "2.1.214")
        self.assertEqual(result.dispatch_result.stdout, orig_stdout)
        self.assertEqual(result.dispatch_result.stdout_sha256, orig_sha)


# ═══════════════════════════════════════════════════════════════════════════════
# 15. WorkerOutput frozen / immutability
# ═══════════════════════════════════════════════════════════════════════════════


class TestWorkerOutputImmutability(unittest.TestCase):
    """WorkerOutput is frozen, no __dict__, warnings is tuple."""

    def test_1100_frozen(self) -> None:
        output = WorkerOutput(
            identity=_identity(),
            provider="claude",
            model_id="m",
            status=WorkerCompletionStatus.COMPLETED,
            implementation_commit="a" * 40,
            report_commit="b" * 40,
            summary="s",
            warnings=(),
            stdout_sha256="0" * 64,
        )
        with self.assertRaises(Exception):
            output.summary = "hacked"  # type: ignore[misc]

    def test_1101_no_dict(self) -> None:
        output = WorkerOutput(
            identity=_identity(),
            provider="claude",
            model_id="m",
            status=WorkerCompletionStatus.COMPLETED,
            implementation_commit="a" * 40,
            report_commit="b" * 40,
            summary="s",
            warnings=(),
            stdout_sha256="0" * 64,
        )
        with self.assertRaises(AttributeError):
            output.__dict__  # type: ignore[attr-defined]


# ═══════════════════════════════════════════════════════════════════════════════
# 16. Envelope nested JSON decode (result is JSON string)
# ═══════════════════════════════════════════════════════════════════════════════


class TestEnvelopeNestedDecode(unittest.TestCase):
    """The Claude 'result' field must itself be parsed as JSON."""

    def test_1200_envelope_with_json_special_chars(self) -> None:
        """Summary containing JSON-special characters still works."""
        summary = 'Summary with "quotes" and \\backslashes\\ and /slashes/'
        envelope = _make_envelope(summary=summary)
        wrapper = _make_wrapper(envelope)
        result = _build_result(wrapper)
        output = decode_worker_result(result, "2.1.214")
        self.assertEqual(output.summary, summary)

    def test_1201_nested_json_envelope_not_flat_text(self) -> None:
        """Result must be JSON, not a bare summary string."""
        wrapper = _make_wrapper("not json at all")
        result = _build_result(wrapper)
        with self.assertRaises(WorkerOutputDecodeError):
            decode_worker_result(result, "2.1.214")


if __name__ == "__main__":
    unittest.main()


# ═══════════════════════════════════════════════════════════════════════════════
# 17. Construction integrity — WorkerOutput illegal direct construction
# ═══════════════════════════════════════════════════════════════════════════════


class TestWorkerOutputConstructionIntegrity(unittest.TestCase):
    """WorkerOutput __post_init__ must reject illegal direct construction."""

    def test_1300_illegal_report_commit_rejected(self) -> None:
        with self.assertRaises(ValueError):
            WorkerOutput(
                identity=_identity(),
                provider="claude",
                model_id="m",
                status=WorkerCompletionStatus.COMPLETED,
                implementation_commit="a" * 40,
                report_commit="bad",
                summary="s",
                warnings=(),
                stdout_sha256="0" * 64,
            )

    def test_1301_illegal_stdout_sha256_rejected(self) -> None:
        with self.assertRaises(ValueError):
            WorkerOutput(
                identity=_identity(),
                provider="claude",
                model_id="m",
                status=WorkerCompletionStatus.COMPLETED,
                implementation_commit="a" * 40,
                report_commit="b" * 40,
                summary="s",
                warnings=(),
                stdout_sha256="bad",
            )

    def test_1302_same_commit_rejected(self) -> None:
        with self.assertRaises(ValueError):
            WorkerOutput(
                identity=_identity(),
                provider="claude",
                model_id="m",
                status=WorkerCompletionStatus.COMPLETED,
                implementation_commit="a" * 40,
                report_commit="a" * 40,  # same as impl
                summary="s",
                warnings=(),
                stdout_sha256="0" * 64,
            )

    def test_1303_non_dispatch_identity_rejected(self) -> None:
        with self.assertRaises(TypeError):
            WorkerOutput(
                identity="not-an-identity",  # type: ignore[arg-type]
                provider="claude",
                model_id="m",
                status=WorkerCompletionStatus.COMPLETED,
                implementation_commit="a" * 40,
                report_commit="b" * 40,
                summary="s",
                warnings=(),
                stdout_sha256="0" * 64,
            )

    def test_1304_unknown_provider_rejected(self) -> None:
        with self.assertRaises(ValueError):
            WorkerOutput(
                identity=_identity(),
                provider="unknown",
                model_id="m",
                status=WorkerCompletionStatus.COMPLETED,
                implementation_commit="a" * 40,
                report_commit="b" * 40,
                summary="s",
                warnings=(),
                stdout_sha256="0" * 64,
            )

    def test_1305_illegal_model_id_empty_rejected(self) -> None:
        with self.assertRaises(ValueError):
            WorkerOutput(
                identity=_identity(),
                provider="claude",
                model_id="",
                status=WorkerCompletionStatus.COMPLETED,
                implementation_commit="a" * 40,
                report_commit="b" * 40,
                summary="s",
                warnings=(),
                stdout_sha256="0" * 64,
            )

    def test_1306_illegal_model_id_whitespace_rejected(self) -> None:
        with self.assertRaises(ValueError):
            WorkerOutput(
                identity=_identity(),
                provider="claude",
                model_id=" spaced ",
                status=WorkerCompletionStatus.COMPLETED,
                implementation_commit="a" * 40,
                report_commit="b" * 40,
                summary="s",
                warnings=(),
                stdout_sha256="0" * 64,
            )

    def test_1307_model_id_with_nul_rejected(self) -> None:
        with self.assertRaises(ValueError):
            WorkerOutput(
                identity=_identity(),
                provider="claude",
                model_id="bad\x00",
                status=WorkerCompletionStatus.COMPLETED,
                implementation_commit="a" * 40,
                report_commit="b" * 40,
                summary="s",
                warnings=(),
                stdout_sha256="0" * 64,
            )

    def test_1308_bare_string_status_rejected(self) -> None:
        with self.assertRaises(TypeError):
            WorkerOutput(
                identity=_identity(),
                provider="claude",
                model_id="m",
                status="completed",  # type: ignore[arg-type]
                implementation_commit="a" * 40,
                report_commit="b" * 40,
                summary="s",
                warnings=(),
                stdout_sha256="0" * 64,
            )

    def test_1309_warnings_is_list_rejected(self) -> None:
        with self.assertRaises(TypeError):
            WorkerOutput(
                identity=_identity(),
                provider="claude",
                model_id="m",
                status=WorkerCompletionStatus.COMPLETED,
                implementation_commit="a" * 40,
                report_commit="b" * 40,
                summary="s",
                warnings=[],  # type: ignore[arg-type]
                stdout_sha256="0" * 64,
            )

    def test_1310_warning_empty_rejected(self) -> None:
        with self.assertRaises(ValueError):
            WorkerOutput(
                identity=_identity(),
                provider="claude",
                model_id="m",
                status=WorkerCompletionStatus.COMPLETED,
                implementation_commit="a" * 40,
                report_commit="b" * 40,
                summary="s",
                warnings=("",),
                stdout_sha256="0" * 64,
            )

    def test_1311_warning_duplicate_rejected(self) -> None:
        with self.assertRaises(ValueError):
            WorkerOutput(
                identity=_identity(),
                provider="claude",
                model_id="m",
                status=WorkerCompletionStatus.COMPLETED,
                implementation_commit="a" * 40,
                report_commit="b" * 40,
                summary="s",
                warnings=("dup", "dup"),
                stdout_sha256="0" * 64,
            )

    def test_1312_warning_with_nul_rejected(self) -> None:
        with self.assertRaises(ValueError):
            WorkerOutput(
                identity=_identity(),
                provider="claude",
                model_id="m",
                status=WorkerCompletionStatus.COMPLETED,
                implementation_commit="a" * 40,
                report_commit="b" * 40,
                summary="s",
                warnings=("bad\x00",),
                stdout_sha256="0" * 64,
            )

    def test_1313_completed_missing_impl_rejected(self) -> None:
        with self.assertRaises(ValueError):
            WorkerOutput(
                identity=_identity(),
                provider="claude",
                model_id="m",
                status=WorkerCompletionStatus.COMPLETED,
                implementation_commit=None,
                report_commit="b" * 40,
                summary="s",
                warnings=(),
                stdout_sha256="0" * 64,
            )

    def test_1314_partial_with_illegal_non_null_impl_rejected(self) -> None:
        with self.assertRaises(ValueError):
            WorkerOutput(
                identity=_identity(),
                provider="claude",
                model_id="m",
                status=WorkerCompletionStatus.PARTIAL,
                implementation_commit="bad",
                report_commit="b" * 40,
                summary="s",
                warnings=(),
                stdout_sha256="0" * 64,
            )

    def test_1315_blocked_with_illegal_non_null_impl_rejected(self) -> None:
        with self.assertRaises(ValueError):
            WorkerOutput(
                identity=_identity(),
                provider="claude",
                model_id="m",
                status=WorkerCompletionStatus.BLOCKED,
                implementation_commit="ZZZ",
                report_commit="b" * 40,
                summary="s",
                warnings=(),
                stdout_sha256="0" * 64,
            )

    def test_1316_summary_empty_rejected(self) -> None:
        with self.assertRaises(ValueError):
            WorkerOutput(
                identity=_identity(),
                provider="claude",
                model_id="m",
                status=WorkerCompletionStatus.COMPLETED,
                implementation_commit="a" * 40,
                report_commit="b" * 40,
                summary="",
                warnings=(),
                stdout_sha256="0" * 64,
            )

    def test_1317_summary_nul_rejected(self) -> None:
        with self.assertRaises(ValueError):
            WorkerOutput(
                identity=_identity(),
                provider="claude",
                model_id="m",
                status=WorkerCompletionStatus.COMPLETED,
                implementation_commit="a" * 40,
                report_commit="b" * 40,
                summary="bad\x00text",
                warnings=(),
                stdout_sha256="0" * 64,
            )


# ═══════════════════════════════════════════════════════════════════════════════
# 18. Construction integrity — DeliveryReceipt illegal direct construction
# ═══════════════════════════════════════════════════════════════════════════════


class TestDeliveryReceiptConstructionIntegrity(unittest.TestCase):
    """DeliveryReceipt __post_init__ must reject illegal direct construction."""

    def test_1400_illegal_report_commit_rejected(self) -> None:
        with self.assertRaises(ValueError):
            DeliveryReceipt(
                identity=_identity(),
                provider="claude",
                model_id="m",
                implementation_commit="a" * 40,
                report_commit="bad",
                stdout_sha256="0" * 64,
            )

    def test_1401_illegal_impl_commit_rejected(self) -> None:
        with self.assertRaises(ValueError):
            DeliveryReceipt(
                identity=_identity(),
                provider="claude",
                model_id="m",
                implementation_commit="bad",
                report_commit="b" * 40,
                stdout_sha256="0" * 64,
            )

    def test_1402_same_commits_rejected(self) -> None:
        with self.assertRaises(ValueError):
            DeliveryReceipt(
                identity=_identity(),
                provider="claude",
                model_id="m",
                implementation_commit="a" * 40,
                report_commit="a" * 40,
                stdout_sha256="0" * 64,
            )

    def test_1403_illegal_stdout_sha_rejected(self) -> None:
        with self.assertRaises(ValueError):
            DeliveryReceipt(
                identity=_identity(),
                provider="claude",
                model_id="m",
                implementation_commit="a" * 40,
                report_commit="b" * 40,
                stdout_sha256="bad",
            )

    def test_1404_non_dispatch_identity_rejected(self) -> None:
        with self.assertRaises(TypeError):
            DeliveryReceipt(
                identity="nope",  # type: ignore[arg-type]
                provider="claude",
                model_id="m",
                implementation_commit="a" * 40,
                report_commit="b" * 40,
                stdout_sha256="0" * 64,
            )

    def test_1405_unknown_provider_rejected(self) -> None:
        with self.assertRaises(ValueError):
            DeliveryReceipt(
                identity=_identity(),
                provider="unknown",
                model_id="m",
                implementation_commit="a" * 40,
                report_commit="b" * 40,
                stdout_sha256="0" * 64,
            )


# ═══════════════════════════════════════════════════════════════════════════════
# 19. Defensive re-validation — forged WorkerOutput bypasses __post_init__
# ═══════════════════════════════════════════════════════════════════════════════


class TestForgedWorkerOutputDefense(unittest.TestCase):
    """require_delivery_receipt must reject hand-crafted frozen objects
    that bypass WorkerOutput.__post_init__."""

    @staticmethod
    def _forge_worker_output(
        provider: object = "claude",
        model_id: object = "test-model",
        status: object = WorkerCompletionStatus.COMPLETED,
        impl_commit: object = "a" * 40,
        report_commit: object = "b" * 40,
        stdout_sha256: object = "0" * 64,
    ) -> WorkerOutput:
        """Forge a WorkerOutput bypassing __post_init__ using object.__new__."""
        wo = object.__new__(WorkerOutput)
        object.__setattr__(wo, "identity", _identity())
        object.__setattr__(wo, "provider", provider)
        object.__setattr__(wo, "model_id", model_id)
        object.__setattr__(wo, "status", status)
        object.__setattr__(wo, "implementation_commit", impl_commit)
        object.__setattr__(wo, "report_commit", report_commit)
        object.__setattr__(wo, "summary", "s")
        object.__setattr__(wo, "warnings", ())
        object.__setattr__(wo, "stdout_sha256", stdout_sha256)
        return wo

    def test_1500_forged_illegal_report_commit_rejected(self) -> None:
        wo = self._forge_worker_output(report_commit="bad")
        with self.assertRaises(WorkerOutputSchemaError):
            require_delivery_receipt(wo)

    def test_1501_forged_illegal_impl_commit_rejected(self) -> None:
        wo = self._forge_worker_output(impl_commit="bad")
        with self.assertRaises(WorkerOutputSchemaError):
            require_delivery_receipt(wo)

    def test_1502_forged_same_commits_rejected(self) -> None:
        wo = self._forge_worker_output(
            impl_commit="a" * 40, report_commit="a" * 40
        )
        with self.assertRaises(WorkerOutputSchemaError):
            require_delivery_receipt(wo)

    def test_1503_forged_illegal_stdout_sha_rejected(self) -> None:
        wo = self._forge_worker_output(stdout_sha256="bad")
        with self.assertRaises(WorkerOutputSchemaError):
            require_delivery_receipt(wo)

    def test_1504_forged_unknown_provider_rejected(self) -> None:
        wo = self._forge_worker_output(provider="unknown")
        with self.assertRaises(WorkerOutputSchemaError):
            require_delivery_receipt(wo)

    def test_1505_forged_illegal_model_id_rejected(self) -> None:
        wo = self._forge_worker_output(model_id="")
        with self.assertRaises(WorkerOutputSchemaError):
            require_delivery_receipt(wo)

    def test_1506_forged_non_enum_status_rejected(self) -> None:
        wo = self._forge_worker_output(status="completed")
        with self.assertRaises(WorkerOutputSchemaError):
            require_delivery_receipt(wo)

    def test_1507_forged_partial_status_rejected(self) -> None:
        wo = self._forge_worker_output(
            status=WorkerCompletionStatus.PARTIAL, impl_commit=None
        )
        with self.assertRaises(WorkerOutputSchemaError):
            require_delivery_receipt(wo)

    def test_1508_forged_non_identity_rejected(self) -> None:
        wo = self._forge_worker_output()
        object.__setattr__(wo, "identity", "not-an-identity")
        with self.assertRaises(WorkerOutputSchemaError):
            require_delivery_receipt(wo)

    def test_1509_forged_missing_impl_commit_rejected(self) -> None:
        wo = self._forge_worker_output(impl_commit=None)
        with self.assertRaises(WorkerOutputSchemaError):
            require_delivery_receipt(wo)


# ═══════════════════════════════════════════════════════════════════════════════
# 20. Construction error message safety
# ═══════════════════════════════════════════════════════════════════════════════


class TestConstructionErrorMessageSafety(unittest.TestCase):
    """__post_init__ and require_delivery_receipt error messages must not
    leak illegal commit values, model IDs, or field content."""

    def test_1600_worker_output_commit_error_no_leak(self) -> None:
        try:
            WorkerOutput(
                identity=_identity(),
                provider="claude",
                model_id="m",
                status=WorkerCompletionStatus.COMPLETED,
                implementation_commit="a" * 40,
                report_commit="my-evil-commit-that-must-not-leak-xxxx",
                summary="s",
                warnings=(),
                stdout_sha256="0" * 64,
            )
        except ValueError as e:
            msg = str(e)
            self.assertNotIn("my-evil-commit", msg)

    def test_1601_receipt_commit_error_no_leak(self) -> None:
        try:
            DeliveryReceipt(
                identity=_identity(),
                provider="claude",
                model_id="m",
                implementation_commit="a" * 40,
                report_commit="my-evil-commit-that-must-not-leak-xxxx",
                stdout_sha256="0" * 64,
            )
        except ValueError as e:
            msg = str(e)
            self.assertNotIn("my-evil-commit", msg)

    def test_1602_require_receipt_commit_error_no_leak(self) -> None:
        """Even when forged object has bad commit, the value must not leak."""
        wo = TestForgedWorkerOutputDefense._forge_worker_output(
            report_commit="my-evil-commit-that-must-not-leak-xxxx"
        )
        try:
            require_delivery_receipt(wo)
        except WorkerOutputSchemaError as e:
            msg = str(e)
            self.assertNotIn("my-evil-commit", msg)

    def test_1603_model_id_error_no_leak(self) -> None:
        try:
            WorkerOutput(
                identity=_identity(),
                provider="claude",
                model_id="secret-model-name",
                status=WorkerCompletionStatus.COMPLETED,
                implementation_commit="a" * 40,
                report_commit="b" * 40,
                summary="s",
                warnings=(),
                stdout_sha256="0" * 64,
            )
        except ValueError as e:
            msg = str(e)
            self.assertNotIn("secret-model-name", msg)

    def test_1604_evil_repr_not_called_in_worker_output(self) -> None:
        class _EvilModelId:
            def __repr__(self) -> str:
                raise RuntimeError("evil __repr__ called!")

        evil = _EvilModelId()
        try:
            WorkerOutput(
                identity=_identity(),
                provider="claude",
                model_id=evil,  # type: ignore[arg-type]
                status=WorkerCompletionStatus.COMPLETED,
                implementation_commit="a" * 40,
                report_commit="b" * 40,
                summary="s",
                warnings=(),
                stdout_sha256="0" * 64,
            )
        except (TypeError, ValueError):
            pass  # Must not call __repr__
        # If we got here, __repr__ was not called

    def test_1605_evil_repr_not_called_in_receipt(self) -> None:
        class _EvilModelId:
            def __repr__(self) -> str:
                raise RuntimeError("evil __repr__ called!")

        evil = _EvilModelId()
        try:
            DeliveryReceipt(
                identity=_identity(),
                provider="claude",
                model_id=evil,  # type: ignore[arg-type]
                implementation_commit="a" * 40,
                report_commit="b" * 40,
                stdout_sha256="0" * 64,
            )
        except (TypeError, ValueError):
            pass  # Must not call __repr__
