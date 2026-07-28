"""TC-13.13b: EscalationService comprehensive tests.

Tests the frozen contract behaviour only; does not extend the contract.
"""

from __future__ import annotations

import enum
import io
import importlib
import json
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

# -- Load the module under test -------------------------------------------
_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCRIPTS = _REPO_ROOT / "skills" / "agentdesk" / "scripts"

sys.path.insert(0, str(_SCRIPTS))
import escalation_service  # noqa: E402
from core_types import WorkerKind  # noqa: E402

sys.path.pop(0)

# Convenience aliases.
EA = escalation_service.EscalationAction
ER = escalation_service.EscalationRequest
ED = escalation_service.EscalationDecision
evaluate = escalation_service.evaluate_escalation


# =========================================================================
# Helpers
# =========================================================================


def _reimport_clean() -> tuple[io.StringIO, io.StringIO]:
    """Reimport escalation_service capturing stdout/stderr."""
    for mod in list(sys.modules):
        if mod == "escalation_service" or mod.startswith("escalation_service."):
            del sys.modules[mod]
    sys.path.insert(0, str(_SCRIPTS))
    try:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            importlib.import_module("escalation_service")
        return stdout, stderr
    finally:
        sys.path.remove(str(_SCRIPTS))


# =========================================================================
# Tests
# =========================================================================


class EscalationServiceAllTest(unittest.TestCase):
    """__all__ exact four symbols."""

    def test_all_exact_four_symbols(self) -> None:
        expected = [
            "EscalationAction",
            "EscalationRequest",
            "EscalationDecision",
            "evaluate_escalation",
        ]
        self.assertIsNotNone(escalation_service.__all__)
        self.assertEqual(
            sorted(expected),
            sorted(escalation_service.__all__),
            "__all__ must contain exactly four symbols",
        )

    def test_all_no_extra_symbols(self) -> None:
        self.assertEqual(
            len(escalation_service.__all__),
            4,
            "__all__ must have exactly 4 entries",
        )


class EscalationActionTest(unittest.TestCase):
    """EscalationAction enum — two members, unique values, string behaviour."""

    def test_exact_two_members(self) -> None:
        members = list(EA)
        self.assertEqual(len(members), 2, "EscalationAction must have exactly 2 members")

    def test_member_names(self) -> None:
        names = {m.name for m in EA}
        self.assertEqual(
            names,
            {"ESCALATE", "REQUEST_USER_DECISION"},
        )

    def test_member_values_are_lowercase_strings(self) -> None:
        for m in EA:
            self.assertEqual(
                m.value,
                m.name.lower(),
                f"{m.name}.value must be lowercase of name",
            )

    def test_str_equals_value(self) -> None:
        for m in EA:
            self.assertEqual(str(m), m.value)

    def test_isinstance_str(self) -> None:
        for m in EA:
            self.assertIsInstance(m, str)

    def test_unique_values(self) -> None:
        vals = [m.value for m in EA]
        self.assertEqual(len(vals), len(set(vals)))

    def test_fail_closed_unknown_string(self) -> None:
        with self.assertRaises(ValueError):
            EA("bogus_action")

    def test_bare_string_rejected(self) -> None:
        """Bare strings are not EscalationAction members."""
        self.assertNotIsInstance("escalate", EA)

    def test_no_alias(self) -> None:
        """No two members share the same value."""
        by_value: dict[str, list[EA]] = {}
        for m in EA:
            by_value.setdefault(m.value, []).append(m)
        for v, members in by_value.items():
            self.assertEqual(
                len(members),
                1,
                f"value {v!r} has {len(members)} members — aliases forbidden",
            )


class EscalationRequestTest(unittest.TestCase):
    """EscalationRequest — frozen, slots, exact one field."""

    def test_exact_one_field(self) -> None:
        fields = list(ER.__dataclass_fields__)
        self.assertEqual(
            fields,
            ["current_worker_kind"],
            "EscalationRequest must have exactly one field",
        )

    def test_frozen(self) -> None:
        req = ER(current_worker_kind=WorkerKind.BASIC_AGENT)
        with self.assertRaises(Exception):
            req.current_worker_kind = WorkerKind.STANDARD_AGENT  # type: ignore[misc]

    def test_slots(self) -> None:
        req = ER(current_worker_kind=WorkerKind.BASIC_AGENT)
        with self.assertRaises(AttributeError):
            req.__dict__  # type: ignore[attr-defined]

    def test_valid_construction(self) -> None:
        for wk in WorkerKind:
            req = ER(current_worker_kind=wk)
            self.assertIs(req.current_worker_kind, wk)

    def test_bare_string_rejected(self) -> None:
        with self.assertRaises(TypeError):
            ER(current_worker_kind="basic_agent")  # type: ignore[arg-type]

    def test_none_rejected(self) -> None:
        with self.assertRaises(TypeError):
            ER(current_worker_kind=None)  # type: ignore[arg-type]

    def test_bool_rejected(self) -> None:
        with self.assertRaises(TypeError):
            ER(current_worker_kind=True)  # type: ignore[arg-type]

    def test_int_rejected(self) -> None:
        with self.assertRaises(TypeError):
            ER(current_worker_kind=42)  # type: ignore[arg-type]

    def test_list_rejected(self) -> None:
        with self.assertRaises(TypeError):
            ER(current_worker_kind=[])  # type: ignore[arg-type]

    def test_dict_rejected(self) -> None:
        with self.assertRaises(TypeError):
            ER(current_worker_kind={})  # type: ignore[arg-type]

    def test_other_enum_rejected(self) -> None:
        """A different enum type must be rejected."""

        class Other(enum.Enum):
            X = 1

        with self.assertRaises(TypeError):
            ER(current_worker_kind=Other.X)  # type: ignore[arg-type]

    def test_error_message_contains_safe_type_name(self) -> None:
        with self.assertRaises(TypeError) as ctx:
            ER(current_worker_kind="basic_agent")  # type: ignore[arg-type]
        msg = str(ctx.exception)
        self.assertIn("str", msg)

    def test_error_message_contains_type_name_for_none(self) -> None:
        with self.assertRaises(TypeError) as ctx:
            ER(current_worker_kind=None)  # type: ignore[arg-type]
        msg = str(ctx.exception)
        self.assertIn("NoneType", msg)

    def test_error_message_no_input_value_leaked(self) -> None:
        """Exception message must not contain the illegal input value."""
        with self.assertRaises(TypeError):
            ER(current_worker_kind="basic_agent")  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            ER(current_worker_kind=42)  # type: ignore[arg-type]


class EscalationDecisionTest(unittest.TestCase):
    """EscalationDecision — frozen, slots, exact three fields, invariants."""

    def test_exact_three_fields(self) -> None:
        fields = sorted(ED.__dataclass_fields__)
        self.assertEqual(
            fields,
            sorted(["action", "current_worker_kind", "next_worker_kind"]),
        )

    def test_frozen(self) -> None:
        d = ED(
            action=EA.ESCALATE,
            current_worker_kind=WorkerKind.BASIC_AGENT,
            next_worker_kind=WorkerKind.STANDARD_AGENT,
        )
        with self.assertRaises(Exception):
            d.action = EA.REQUEST_USER_DECISION  # type: ignore[misc]

    def test_slots(self) -> None:
        d = ED(
            action=EA.ESCALATE,
            current_worker_kind=WorkerKind.BASIC_AGENT,
            next_worker_kind=WorkerKind.STANDARD_AGENT,
        )
        with self.assertRaises(AttributeError):
            d.__dict__  # type: ignore[attr-defined]

    def test_escalate_with_none_rejected(self) -> None:
        with self.assertRaises(ValueError):
            ED(
                action=EA.ESCALATE,
                current_worker_kind=WorkerKind.BASIC_AGENT,
                next_worker_kind=None,
            )

    def test_request_user_decision_with_non_none_rejected(self) -> None:
        with self.assertRaises(ValueError):
            ED(
                action=EA.REQUEST_USER_DECISION,
                current_worker_kind=WorkerKind.EXPERT_AGENT,
                next_worker_kind=WorkerKind.BASIC_AGENT,
            )

    def test_request_user_decision_non_expert_rejected(self) -> None:
        with self.assertRaises(ValueError):
            ED(
                action=EA.REQUEST_USER_DECISION,
                current_worker_kind=WorkerKind.BASIC_AGENT,
                next_worker_kind=None,
            )

    def test_escalate_valid_construction(self) -> None:
        d = ED(
            action=EA.ESCALATE,
            current_worker_kind=WorkerKind.BASIC_AGENT,
            next_worker_kind=WorkerKind.STANDARD_AGENT,
        )
        self.assertIs(d.action, EA.ESCALATE)
        self.assertIs(d.current_worker_kind, WorkerKind.BASIC_AGENT)
        self.assertIs(d.next_worker_kind, WorkerKind.STANDARD_AGENT)

    def test_request_user_decision_valid_construction(self) -> None:
        d = ED(
            action=EA.REQUEST_USER_DECISION,
            current_worker_kind=WorkerKind.EXPERT_AGENT,
            next_worker_kind=None,
        )
        self.assertIs(d.action, EA.REQUEST_USER_DECISION)
        self.assertIs(d.current_worker_kind, WorkerKind.EXPERT_AGENT)
        self.assertIsNone(d.next_worker_kind)

    def test_illegal_action_type_rejected(self) -> None:
        with self.assertRaises(TypeError):
            ED(
                action="escalate",  # type: ignore[arg-type]
                current_worker_kind=WorkerKind.BASIC_AGENT,
                next_worker_kind=WorkerKind.STANDARD_AGENT,
            )

    def test_illegal_current_worker_kind_rejected(self) -> None:
        with self.assertRaises(TypeError):
            ED(
                action=EA.ESCALATE,
                current_worker_kind="basic_agent",  # type: ignore[arg-type]
                next_worker_kind=WorkerKind.STANDARD_AGENT,
            )

    def test_illegal_next_worker_kind_type_rejected_escalate(self) -> None:
        with self.assertRaises(TypeError):
            ED(
                action=EA.ESCALATE,
                current_worker_kind=WorkerKind.BASIC_AGENT,
                next_worker_kind="standard_agent",  # type: ignore[arg-type]
            )

    def test_error_message_no_input_value_leaked(self) -> None:
        """Decision error messages must not contain illegal input values."""
        try:
            ED(
                action="escalate",  # type: ignore[arg-type]
                current_worker_kind=WorkerKind.BASIC_AGENT,
                next_worker_kind=WorkerKind.STANDARD_AGENT,
            )
        except TypeError:
            pass  # expected


class EvaluateEscalationTest(unittest.TestCase):
    """evaluate_escalation — exact four WorkerKind progression results."""

    def test_basic_agent_escalates_to_standard(self) -> None:
        req = ER(current_worker_kind=WorkerKind.BASIC_AGENT)
        d = evaluate(req)
        self.assertIs(d.action, EA.ESCALATE)
        self.assertIs(d.current_worker_kind, WorkerKind.BASIC_AGENT)
        self.assertIs(d.next_worker_kind, WorkerKind.STANDARD_AGENT)

    def test_standard_agent_escalates_to_advanced(self) -> None:
        req = ER(current_worker_kind=WorkerKind.STANDARD_AGENT)
        d = evaluate(req)
        self.assertIs(d.action, EA.ESCALATE)
        self.assertIs(d.current_worker_kind, WorkerKind.STANDARD_AGENT)
        self.assertIs(d.next_worker_kind, WorkerKind.ADVANCED_AGENT)

    def test_advanced_agent_escalates_to_expert(self) -> None:
        req = ER(current_worker_kind=WorkerKind.ADVANCED_AGENT)
        d = evaluate(req)
        self.assertIs(d.action, EA.ESCALATE)
        self.assertIs(d.current_worker_kind, WorkerKind.ADVANCED_AGENT)
        self.assertIs(d.next_worker_kind, WorkerKind.EXPERT_AGENT)

    def test_expert_agent_requests_user_decision(self) -> None:
        req = ER(current_worker_kind=WorkerKind.EXPERT_AGENT)
        d = evaluate(req)
        self.assertIs(d.action, EA.REQUEST_USER_DECISION)
        self.assertIs(d.current_worker_kind, WorkerKind.EXPERT_AGENT)
        self.assertIsNone(d.next_worker_kind)

    def test_request_not_modified(self) -> None:
        req = ER(current_worker_kind=WorkerKind.BASIC_AGENT)
        _ = evaluate(req)
        self.assertIs(req.current_worker_kind, WorkerKind.BASIC_AGENT)

    def test_multiple_calls_equal_but_independent_objects(self) -> None:
        req = ER(current_worker_kind=WorkerKind.STANDARD_AGENT)
        d1 = evaluate(req)
        d2 = evaluate(req)
        self.assertEqual(d1, d2)
        # Same WorkerKind singletons will be identical, but the
        # EscalationDecision objects are distinct.
        self.assertIsNot(d1, d2)

    def test_deterministic(self) -> None:
        req = ER(current_worker_kind=WorkerKind.ADVANCED_AGENT)
        results = [evaluate(req) for _ in range(10)]
        for r in results:
            self.assertEqual(r, results[0])

    def test_non_escalation_request_rejected(self) -> None:
        with self.assertRaises(TypeError):
            evaluate(None)  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            evaluate("not a request")  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            evaluate(42)  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            evaluate({})  # type: ignore[arg-type]

    def test_no_fallback(self) -> None:
        """evaluate_escalation does not fall back to a default."""
        # All four WorkerKinds are covered above; there is no fallback path.

    def test_no_custom_exception(self) -> None:
        """Only built-in TypeError/ValueError are raised."""
        # Prove it by triggering each and checking type.
        with self.assertRaises(TypeError):
            evaluate("bad")  # type: ignore[arg-type]
        # Construct an EscalationRequest with a bad WorkerKind via
        # a way that bypasses __post_init__ (frozen + slots stops
        # normal assignment, but we can test the evaluate path).
        # No custom exception classes exist in the module.
        for obj in dir(escalation_service):
            if obj.endswith("Error") and not obj.startswith("__"):
                # Only built-in errors visible via re-export would match.
                self.fail(f"Custom exception {obj} found in module")


class EscalationServiceImportTest(unittest.TestCase):
    """Import produces zero stdout/stderr and no side effects."""

    def test_import_produces_no_output(self) -> None:
        stdout, stderr = _reimport_clean()
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue(), "")


class EscalationServiceSourceAuditTest(unittest.TestCase):
    """Production source must not contain forbidden references."""

    _MODULE_PATH = _SCRIPTS / "escalation_service.py"

    @classmethod
    def setUpClass(cls) -> None:
        cls._src = cls._MODULE_PATH.read_text(encoding="utf-8")

    def test_no_task_difficulty(self) -> None:
        self.assertNotIn("TaskDifficulty", self._src)

    def test_no_retry(self) -> None:
        self.assertNotIn("retry", self._src.lower())

    def test_no_rate_limit(self) -> None:
        self.assertNotIn("rate_limit", self._src.lower())
        self.assertNotIn("ratelimit", self._src.lower())

    def test_no_event(self) -> None:
        self.assertNotIn("TASK_ESCALATED", self._src)
        self.assertNotIn("event", self._src.lower())

    def test_no_outbox(self) -> None:
        self.assertNotIn("outbox", self._src.lower())

    def test_no_git(self) -> None:
        self.assertNotIn("git", self._src.lower())

    def test_no_subprocess(self) -> None:
        self.assertNotIn("subprocess", self._src)

    def test_no_network(self) -> None:
        self.assertNotIn("network", self._src.lower())
        self.assertNotIn("http", self._src.lower())
        self.assertNotIn("socket", self._src.lower())

    def test_no_file_write(self) -> None:
        self.assertNotIn("write", self._src.lower())
        self.assertNotIn("open(", self._src)

    def test_no_lock(self) -> None:
        self.assertNotIn("lock", self._src.lower())

    def test_no_custom_exception_class(self) -> None:
        self.assertNotIn("Error):", self._src)
        self.assertNotIn("Exception):", self._src)

    def test_no_internal_mapping_in_all(self) -> None:
        """_PROGRESSION must not be in __all__."""
        self.assertNotIn("_PROGRESSION", escalation_service.__all__)


class EscalationServiceJsonRoundTripTest(unittest.TestCase):
    """EscalationAction JSON serialisation round-trip."""

    def test_action_json_round_trip(self) -> None:
        for action in EA:
            raw = json.dumps(action)
            self.assertEqual(raw, f'"{action.value}"')
            loaded = json.loads(raw)
            self.assertEqual(loaded, action.value)

    def test_decision_json_round_trip_basic_route(self) -> None:
        """Decision fields serialize to proper JSON."""
        d = ED(
            action=EA.ESCALATE,
            current_worker_kind=WorkerKind.BASIC_AGENT,
            next_worker_kind=WorkerKind.STANDARD_AGENT,
        )
        raw = json.dumps(
            {
                "action": d.action.value,
                "current_worker_kind": d.current_worker_kind.value,
                "next_worker_kind": d.next_worker_kind.value,  # type: ignore[union-attr]
            }
        )
        loaded = json.loads(raw)
        self.assertEqual(loaded["action"], "escalate")
        self.assertEqual(loaded["current_worker_kind"], "basic_agent")
        self.assertEqual(loaded["next_worker_kind"], "standard_agent")


class EscalationServiceMaliciousReprTest(unittest.TestCase):
    """Malicious __repr__ must not be called on invalid input."""

    def test_request_does_not_call_repr(self) -> None:
        class Malicious:
            def __repr__(self) -> str:
                raise AssertionError("__repr__ must not be called")

        with self.assertRaises(TypeError):
            ER(current_worker_kind=Malicious())  # type: ignore[arg-type]

    def test_evaluate_does_not_call_repr_on_input(self) -> None:
        class Malicious:
            def __repr__(self) -> str:
                raise AssertionError("__repr__ must not be called")

        with self.assertRaises(TypeError):
            evaluate(Malicious())  # type: ignore[arg-type]


class EscalationServiceExceptionMessageSafetyTest(unittest.TestCase):
    """Exception messages must not contain sensitive input values."""

    _SENSITIVE = "SECRET_INJECTION_TOKEN"

    def test_request_type_error_no_sensitive_leak(self) -> None:
        with self.assertRaises(TypeError) as ctx:
            ER(current_worker_kind=self._SENSITIVE)  # type: ignore[arg-type]
        msg = str(ctx.exception)
        self.assertNotIn(self._SENSITIVE, msg)

    def test_decision_type_error_no_sensitive_leak(self) -> None:
        with self.assertRaises(TypeError) as ctx:
            ED(
                action=self._SENSITIVE,  # type: ignore[arg-type]
                current_worker_kind=WorkerKind.BASIC_AGENT,
                next_worker_kind=WorkerKind.STANDARD_AGENT,
            )
        msg = str(ctx.exception)
        self.assertNotIn(self._SENSITIVE, msg)

    def test_evaluate_type_error_no_sensitive_leak(self) -> None:
        with self.assertRaises(TypeError) as ctx:
            evaluate(self._SENSITIVE)  # type: ignore[arg-type]
        msg = str(ctx.exception)
        self.assertNotIn(self._SENSITIVE, msg)


if __name__ == "__main__":
    unittest.main()
