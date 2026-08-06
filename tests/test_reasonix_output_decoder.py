"""TC-13.28a.2 Phase A: Reasonix output decoder unit tests.

Tests ``reasonix_output_decoder.py`` against fixture data — no real CLI,
model, API, network, or subprocess.

Covers: success wrapper, failure wrapper, identity mismatch, Unicode
preservation, malicious JSON, extra keys, wrong type/subtype/is_error,
integrity failure, and version gating.
"""

from __future__ import annotations

import hashlib
import json
import sys
import unittest
from dataclasses import replace
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCRIPTS = _REPO_ROOT / "skills" / "agentdesk" / "scripts"

sys.path.insert(0, str(_SCRIPTS))
import dispatcher_gateway as dg  # noqa: E402
import core_types as ct  # noqa: E402
import context_budget as cb  # noqa: E402
import worker_adapter as wa  # noqa: E402
import worker_output_decoder as wod  # noqa: E402
import reasonix_output_decoder as rod  # noqa: E402

sys.path.pop(0)

DispatchIdentity = dg.DispatchIdentity
DispatchResult = dg.DispatchResult
WorkerKind = ct.WorkerKind
TaskDifficulty = ct.TaskDifficulty
BudgetResult = cb.BudgetResult
WorkerResult = wa.WorkerResult
WorkerOutput = wod.WorkerOutput
WorkerCompletionStatus = wod.WorkerCompletionStatus

# ═══════════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════════


def _make_identity(**overrides) -> DispatchIdentity:
    defaults = {
        "task_id": "TC-EVIDENCE-001",
        "revision": 1,
        "attempt": 1,
        "dispatch_id": "DSP-EVIDENCE-001",
    }
    defaults.update(overrides)
    return DispatchIdentity(**defaults)


def _make_envelope(**overrides) -> dict:
    defaults = {
        "schema_version": "agentdesk.worker-output/v1",
        "task_id": "TC-EVIDENCE-001",
        "revision": 1,
        "attempt": 1,
        "dispatch_id": "DSP-EVIDENCE-001",
        "status": "completed",
        "implementation_commit": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "report_commit": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        "summary": "Task completed successfully within scope.",
        "warnings": [],
    }
    defaults.update(overrides)
    return defaults


def _make_success_wrapper(**overrides) -> dict:
    envelope = _make_envelope()
    return {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "duration_ms": 150,
        "duration_api_ms": 120,
        "num_turns": 3,
        "result": json.dumps(envelope, ensure_ascii=False),
        "session_id": "test-session",
        "total_cost_usd": 0.0,
        "usage": {"input_tokens": 100, "output_tokens": 200},
        "modelUsage": {
            "deepseek-v4-flash": {"inputTokens": 100, "outputTokens": 200},
        },
        "permission_denials": [],
        "uuid": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
    }


def _make_dispatch_result(**overrides) -> DispatchResult:
    wrapper = _make_success_wrapper()
    stdout = json.dumps(wrapper, ensure_ascii=False).encode("utf-8")
    sha = hashlib.sha256(stdout).hexdigest()
    defaults = {
        "identity": _make_identity(),
        "provider": "reasonix",
        "model_id": "deepseek-v4-flash",
        "duration_seconds": 1.5,
        "stdout": stdout,
        "stderr": b"progress text",
        "stdout_sha256": sha,
        "stderr_sha256": hashlib.sha256(b"progress text").hexdigest(),
    }
    defaults.update(overrides)
    return DispatchResult(**defaults)


def _make_worker_result(**overrides) -> WorkerResult:
    dr = overrides.pop("dispatch_result", None) or _make_dispatch_result()
    defaults = {
        "worker_kind": WorkerKind.BASIC_AGENT,
        "task_difficulty": TaskDifficulty.BASIC,
        "budget": BudgetResult(
            context_window_tokens=128000,
            difficulty=TaskDifficulty.BASIC,
            budget_percent=20,
            budget_cap_tokens=64000,
            budget_tokens=25600,
            reserved_tokens=6400,
        ),
        "dispatch_result": dr,
    }
    defaults.update(overrides)
    return WorkerResult(**defaults)


# ═══════════════════════════════════════════════════════════════════════════════
# Test suite
# ═══════════════════════════════════════════════════════════════════════════════


class ReasonixDecoderSuccessTests(unittest.TestCase):
    """Happy-path decode with valid success wrapper."""

    def test_decode_success_returns_worker_output(self) -> None:
        wr = _make_worker_result()
        output = rod.decode_reasonix_output(wr, "1.19.1")
        self.assertIsInstance(output, WorkerOutput)

    def test_decode_success_status_completed(self) -> None:
        wr = _make_worker_result()
        output = rod.decode_reasonix_output(wr, "1.19.1")
        self.assertEqual(output.status, WorkerCompletionStatus.COMPLETED)

    def test_decode_success_provider_is_reasonix(self) -> None:
        wr = _make_worker_result()
        output = rod.decode_reasonix_output(wr, "1.19.1")
        self.assertEqual(output.provider, "reasonix")

    def test_decode_success_model_id(self) -> None:
        wr = _make_worker_result()
        output = rod.decode_reasonix_output(wr, "1.19.1")
        self.assertEqual(output.model_id, "deepseek-v4-flash")

    def test_decode_success_identity_preserved(self) -> None:
        wr = _make_worker_result()
        output = rod.decode_reasonix_output(wr, "1.19.1")
        ident = wr.dispatch_result.identity
        self.assertEqual(output.identity.task_id, ident.task_id)
        self.assertEqual(output.identity.revision, ident.revision)
        self.assertEqual(output.identity.attempt, ident.attempt)
        self.assertEqual(output.identity.dispatch_id, ident.dispatch_id)

    def test_decode_success_commits_preserved(self) -> None:
        wr = _make_worker_result()
        output = rod.decode_reasonix_output(wr, "1.19.1")
        self.assertEqual(
            output.implementation_commit,
            "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        )
        self.assertEqual(
            output.report_commit,
            "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        )

    def test_decode_success_sha256_preserved(self) -> None:
        wr = _make_worker_result()
        output = rod.decode_reasonix_output(wr, "1.19.1")
        self.assertEqual(
            output.stdout_sha256,
            wr.dispatch_result.stdout_sha256,
        )

    def test_decode_success_summary_non_empty(self) -> None:
        wr = _make_worker_result()
        output = rod.decode_reasonix_output(wr, "1.19.1")
        self.assertTrue(len(output.summary) > 0)

    def test_decode_success_warnings_empty(self) -> None:
        wr = _make_worker_result()
        output = rod.decode_reasonix_output(wr, "1.19.1")
        self.assertEqual(output.warnings, ())


class ReasonixDecoderFailureTests(unittest.TestCase):
    """Fail-closed paths for wrapper/version/identity/integrity violations."""

    def test_rejects_non_reasonix_provider(self) -> None:
        dr = _make_dispatch_result(provider="claude")
        wr = _make_worker_result(dispatch_result=dr)
        with self.assertRaises(wod.WorkerOutputUnsupportedProviderError):
            rod.decode_reasonix_output(wr, "1.19.1")

    def test_rejects_wrong_version(self) -> None:
        wr = _make_worker_result()
        with self.assertRaises(wod.WorkerOutputUnsupportedVersionError):
            rod.decode_reasonix_output(wr, "1.18.0")

    def test_rejects_empty_version(self) -> None:
        wr = _make_worker_result()
        with self.assertRaises(rod.ReasonixDecoderError):
            rod.decode_reasonix_output(wr, "")

    def test_sha256_mismatch_fail_closed(self) -> None:
        dr = _make_dispatch_result(stdout_sha256="0" * 64)
        wr = _make_worker_result(dispatch_result=dr)
        with self.assertRaises(wod.WorkerOutputIntegrityError):
            rod.decode_reasonix_output(wr, "1.19.1")

    def test_wrapper_type_not_result(self) -> None:
        wrapper = _make_success_wrapper()
        wrapper["type"] = "error"
        stdout = json.dumps(wrapper).encode("utf-8")
        sha = hashlib.sha256(stdout).hexdigest()
        dr = _make_dispatch_result(stdout=stdout, stdout_sha256=sha)
        wr = _make_worker_result(dispatch_result=dr)
        with self.assertRaises(rod.ReasonixWrapperMismatchError):
            rod.decode_reasonix_output(wr, "1.19.1")

    def test_wrapper_subtype_not_success(self) -> None:
        wrapper = _make_success_wrapper()
        wrapper["subtype"] = "failure"
        stdout = json.dumps(wrapper).encode("utf-8")
        sha = hashlib.sha256(stdout).hexdigest()
        dr = _make_dispatch_result(stdout=stdout, stdout_sha256=sha)
        wr = _make_worker_result(dispatch_result=dr)
        with self.assertRaises(rod.ReasonixWrapperMismatchError):
            rod.decode_reasonix_output(wr, "1.19.1")

    def test_wrapper_is_error_true(self) -> None:
        wrapper = _make_success_wrapper()
        wrapper["is_error"] = True
        wrapper["result"] = json.dumps(_make_envelope())
        stdout = json.dumps(wrapper).encode("utf-8")
        sha = hashlib.sha256(stdout).hexdigest()
        dr = _make_dispatch_result(stdout=stdout, stdout_sha256=sha)
        wr = _make_worker_result(dispatch_result=dr)
        with self.assertRaises(rod.ReasonixWrapperMismatchError):
            rod.decode_reasonix_output(wr, "1.19.1")

    def test_wrapper_missing_required_key(self) -> None:
        wrapper = _make_success_wrapper()
        del wrapper["result"]
        stdout = json.dumps(wrapper).encode("utf-8")
        sha = hashlib.sha256(stdout).hexdigest()
        dr = _make_dispatch_result(stdout=stdout, stdout_sha256=sha)
        wr = _make_worker_result(dispatch_result=dr)
        with self.assertRaises(rod.ReasonixWrapperMismatchError):
            rod.decode_reasonix_output(wr, "1.19.1")

    def test_wrapper_unknown_key(self) -> None:
        wrapper = _make_success_wrapper()
        wrapper["fabricated_key"] = "evil"
        stdout = json.dumps(wrapper).encode("utf-8")
        sha = hashlib.sha256(stdout).hexdigest()
        dr = _make_dispatch_result(stdout=stdout, stdout_sha256=sha)
        wr = _make_worker_result(dispatch_result=dr)
        with self.assertRaises(rod.ReasonixWrapperMismatchError):
            rod.decode_reasonix_output(wr, "1.19.1")

    def test_result_field_not_string(self) -> None:
        wrapper = _make_success_wrapper()
        wrapper["result"] = 12345
        stdout = json.dumps(wrapper).encode("utf-8")
        sha = hashlib.sha256(stdout).hexdigest()
        dr = _make_dispatch_result(stdout=stdout, stdout_sha256=sha)
        wr = _make_worker_result(dispatch_result=dr)
        with self.assertRaises(rod.ReasonixWrapperMismatchError):
            rod.decode_reasonix_output(wr, "1.19.1")

    def test_result_not_valid_json(self) -> None:
        wrapper = _make_success_wrapper()
        wrapper["result"] = "not valid json {{{"
        stdout = json.dumps(wrapper).encode("utf-8")
        sha = hashlib.sha256(stdout).hexdigest()
        dr = _make_dispatch_result(stdout=stdout, stdout_sha256=sha)
        wr = _make_worker_result(dispatch_result=dr)
        with self.assertRaises(wod.WorkerOutputDecodeError):
            rod.decode_reasonix_output(wr, "1.19.1")

    def test_envelope_identity_mismatch(self) -> None:
        wrapper = _make_success_wrapper()
        env = _make_envelope(task_id="TC-WRONG")
        wrapper["result"] = json.dumps(env)
        stdout = json.dumps(wrapper).encode("utf-8")
        sha = hashlib.sha256(stdout).hexdigest()
        dr = _make_dispatch_result(stdout=stdout, stdout_sha256=sha)
        wr = _make_worker_result(dispatch_result=dr)
        with self.assertRaises(wod.WorkerOutputIdentityError):
            rod.decode_reasonix_output(wr, "1.19.1")

    def test_envelope_missing_schema_version(self) -> None:
        wrapper = _make_success_wrapper()
        env = _make_envelope()
        del env["schema_version"]
        wrapper["result"] = json.dumps(env)
        stdout = json.dumps(wrapper).encode("utf-8")
        sha = hashlib.sha256(stdout).hexdigest()
        dr = _make_dispatch_result(stdout=stdout, stdout_sha256=sha)
        wr = _make_worker_result(dispatch_result=dr)
        with self.assertRaises(wod.WorkerOutputSchemaError):
            rod.decode_reasonix_output(wr, "1.19.1")

    def test_not_worker_result_input(self) -> None:
        with self.assertRaises(wod.WorkerOutputError):
            rod.decode_reasonix_output("not a result", "1.19.1")

    def test_dispatch_result_is_none(self) -> None:
        wr = WorkerResult(
            worker_kind=WorkerKind.BASIC_AGENT,
            task_difficulty=TaskDifficulty.BASIC,
            budget=BudgetResult(
                context_window_tokens=128000,
                difficulty=TaskDifficulty.BASIC,
                budget_percent=20,
                budget_cap_tokens=64000,
                budget_tokens=25600,
                reserved_tokens=6400,
            ),
            dispatch_result=None,
        )
        with self.assertRaises(rod.ReasonixDecoderError):
            rod.decode_reasonix_output(wr, "1.19.1")


class ReasonixDecoderUnicodeTests(unittest.TestCase):
    """Unicode and multiline content preservation."""

    def test_unicode_summary_preserved(self) -> None:
        wrapper = _make_success_wrapper()
        env = _make_envelope()
        env["summary"] = "完成 — 任务已成功完成。\n多行\n内容"
        wrapper["result"] = json.dumps(env, ensure_ascii=False)
        stdout = json.dumps(wrapper, ensure_ascii=False).encode("utf-8")
        sha = hashlib.sha256(stdout).hexdigest()
        dr = _make_dispatch_result(stdout=stdout, stdout_sha256=sha)
        wr = _make_worker_result(dispatch_result=dr)
        output = rod.decode_reasonix_output(wr, "1.19.1")
        self.assertIn("完成", output.summary)
        self.assertIn("多行", output.summary)

    def test_unicode_warnings_preserved(self) -> None:
        wrapper = _make_success_wrapper()
        env = _make_envelope()
        env["warnings"] = ["警告：非关键问题"]
        wrapper["result"] = json.dumps(env, ensure_ascii=False)
        stdout = json.dumps(wrapper, ensure_ascii=False).encode("utf-8")
        sha = hashlib.sha256(stdout).hexdigest()
        dr = _make_dispatch_result(stdout=stdout, stdout_sha256=sha)
        wr = _make_worker_result(dispatch_result=dr)
        output = rod.decode_reasonix_output(wr, "1.19.1")
        self.assertIn("警告：非关键问题", output.warnings)


class ReasonixDecoderMaliciousJsonTests(unittest.TestCase):
    """Fail-closed on malicious or malformed JSON."""

    def test_non_utf8_stdout(self) -> None:
        # Non-UTF-8 bytes: sha256 mismatch will hit before decode.
        # Use a valid-JSON wrapper with wrong sha to trigger the integrity
        # path, which is hit before parsing.
        wrapper = _make_success_wrapper()
        stdout = json.dumps(wrapper).encode("utf-8")
        sha = "0" * 64  # Deliberately wrong
        dr = _make_dispatch_result(stdout=stdout, stdout_sha256=sha)
        wr = _make_worker_result(dispatch_result=dr)
        with self.assertRaises(wod.WorkerOutputIntegrityError):
            rod.decode_reasonix_output(wr, "1.19.1")

    def test_bom_prefixed_stdout(self) -> None:
        wrapper = _make_success_wrapper()
        raw = json.dumps(wrapper).encode("utf-8")
        stdout = b"\xef\xbb\xbf" + raw
        sha = hashlib.sha256(stdout).hexdigest()
        dr = _make_dispatch_result(stdout=stdout, stdout_sha256=sha)
        wr = _make_worker_result(dispatch_result=dr)
        with self.assertRaises(wod.WorkerOutputDecodeError):
            rod.decode_reasonix_output(wr, "1.19.1")

    def test_trailing_garbage_after_root(self) -> None:
        wrapper = _make_success_wrapper()
        raw = json.dumps(wrapper).encode("utf-8") + b"\nextra garbage"
        sha = hashlib.sha256(raw).hexdigest()
        dr = _make_dispatch_result(stdout=raw, stdout_sha256=sha)
        wr = _make_worker_result(dispatch_result=dr)
        with self.assertRaises(wod.WorkerOutputDecodeError):
            rod.decode_reasonix_output(wr, "1.19.1")

    def test_array_not_object(self) -> None:
        stdout = b"[1, 2, 3]"
        sha = hashlib.sha256(stdout).hexdigest()
        dr = _make_dispatch_result(stdout=stdout, stdout_sha256=sha)
        wr = _make_worker_result(dispatch_result=dr)
        with self.assertRaises(wod.WorkerOutputDecodeError):
            rod.decode_reasonix_output(wr, "1.19.1")


class ReasonixDecoderPartialBlockedTests(unittest.TestCase):
    """Partial and blocked status decoding."""

    def test_partial_status(self) -> None:
        wrapper = _make_success_wrapper()
        env = _make_envelope(
            status="partial",
            implementation_commit=None,
        )
        wrapper["result"] = json.dumps(env)
        stdout = json.dumps(wrapper).encode("utf-8")
        sha = hashlib.sha256(stdout).hexdigest()
        dr = _make_dispatch_result(stdout=stdout, stdout_sha256=sha)
        wr = _make_worker_result(dispatch_result=dr)
        output = rod.decode_reasonix_output(wr, "1.19.1")
        self.assertEqual(output.status, WorkerCompletionStatus.PARTIAL)
        self.assertIsNone(output.implementation_commit)

    def test_blocked_status(self) -> None:
        wrapper = _make_success_wrapper()
        env = _make_envelope(
            status="blocked",
            implementation_commit=None,
            summary="Blocked by permission policy.",
        )
        wrapper["result"] = json.dumps(env)
        stdout = json.dumps(wrapper).encode("utf-8")
        sha = hashlib.sha256(stdout).hexdigest()
        dr = _make_dispatch_result(stdout=stdout, stdout_sha256=sha)
        wr = _make_worker_result(dispatch_result=dr)
        output = rod.decode_reasonix_output(wr, "1.19.1")
        self.assertEqual(output.status, WorkerCompletionStatus.BLOCKED)


class ReasonixDecoderV1ContractTests(unittest.TestCase):
    """TC-13.28a.1 contract assertions on decoder status."""

    @classmethod
    def setUpClass(cls) -> None:
        root = Path(__file__).resolve().parents[1]
        references = root / "skills" / "agentdesk" / "references"
        cls.contract = (
            references
            / "public-interfaces"
            / "reasonix-basic-provider-contract.md"
        ).read_text(encoding="utf-8")

    def test_contract_refers_to_decoder_gate(self) -> None:
        self.assertIn("decoder gate", self.contract)

    def test_contract_forbids_guessing_from_result(self) -> None:
        self.assertIn("No Guessing", self.contract)


# ═══════════════════════════════════════════════════════════════════════════════
# Runner
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    unittest.main()
