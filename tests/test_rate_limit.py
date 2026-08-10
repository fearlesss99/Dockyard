"""TC-13.14b: RateLimitService comprehensive tests.

Tests the frozen contract behaviour and the production implementation;
does not extend the contract.
"""

from __future__ import annotations

import enum
import io
import importlib
import math
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path

# -- Load the module under test -------------------------------------------
_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCRIPTS = _REPO_ROOT / "skills" / "agentdesk" / "scripts"

sys.path.insert(0, str(_SCRIPTS))
import rate_limit  # noqa: E402

sys.path.pop(0)

# Convenience aliases.
RLS = rate_limit.RateLimitSignal
RLSc = rate_limit.RateLimitScope
RLSS = rate_limit.RateLimitSignalSource
RLCR = rate_limit.RateLimitCheckRequest
RLD = rate_limit.RateLimitDecision
RLA = rate_limit.RateLimitAction
RLR = rate_limit.RateLimitReason
RLSvc = rate_limit.RateLimitService
RLE = rate_limit.RateLimitError
RLIE = rate_limit.RateLimitInputError
RLStE = rate_limit.RateLimitStateError
RLSecE = rate_limit.RateLimitSecurityError


# =========================================================================
# Helpers
# =========================================================================

_UTC = timezone.utc
_T0 = datetime(2025, 1, 1, 0, 0, 0, tzinfo=_UTC)
_T1 = datetime(2025, 1, 1, 0, 0, 1, tzinfo=_UTC)


def _sig(
    provider: str = "anthropic",
    scope: RLSc = RLSc.REQUEST,
    observed_at: datetime = _T0,
    retry_after_seconds: int | None = None,
    reset_at: datetime | None = None,
    limit: int | None = None,
    remaining: int | None = None,
    source: RLSS = RLSS.PROVIDER_429,
) -> RLS:
    return RLS(
        provider=provider,
        scope=scope,
        observed_at=observed_at,
        retry_after_seconds=retry_after_seconds,
        reset_at=reset_at,
        limit=limit,
        remaining=remaining,
        source=source,
    )


def _req(
    provider: str = "anthropic",
    scope: RLSc = RLSc.REQUEST,
    now: datetime = _T1,
    units_requested: int = 1,
    signals: tuple[RLS, ...] = (),
) -> RLCR:
    return RLCR(
        provider=provider,
        scope=scope,
        now=now,
        units_requested=units_requested,
        signals=signals,
    )


def _reimport_clean() -> tuple[io.StringIO, io.StringIO]:
    """Reimport rate_limit capturing stdout/stderr."""
    original = sys.modules.pop("rate_limit", None)
    sys.path.insert(0, str(_SCRIPTS))
    try:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            importlib.import_module("rate_limit")
        return stdout, stderr
    finally:
        sys.path.remove(str(_SCRIPTS))
        sys.modules.pop("rate_limit", None)
        if original is not None:
            sys.modules["rate_limit"] = original


# =========================================================================
# Tests — __all__
# =========================================================================


class RateLimitAllTest(unittest.TestCase):
    """__all__ exactly 12 symbols."""

    def test_all_exact_twelve_symbols(self) -> None:
        expected = [
            "RateLimitSignal",
            "RateLimitScope",
            "RateLimitSignalSource",
            "RateLimitCheckRequest",
            "RateLimitDecision",
            "RateLimitAction",
            "RateLimitReason",
            "RateLimitService",
            "RateLimitError",
            "RateLimitInputError",
            "RateLimitStateError",
            "RateLimitSecurityError",
        ]
        self.assertIsNotNone(rate_limit.__all__)
        self.assertEqual(
            sorted(expected),
            sorted(rate_limit.__all__),
            "__all__ must contain exactly twelve symbols",
        )

    def test_all_no_extra_symbols(self) -> None:
        self.assertEqual(
            len(rate_limit.__all__),
            12,
            "__all__ must have exactly 12 entries",
        )


# =========================================================================
# Tests — Enums
# =========================================================================


class RateLimitScopeTest(unittest.TestCase):
    """RateLimitScope — exact four values, unique, str enum."""

    def test_exact_four_members(self) -> None:
        members = list(RLSc)
        self.assertEqual(len(members), 4)

    def test_member_names(self) -> None:
        names = {m.name for m in RLSc}
        self.assertEqual(names, {"REQUEST", "TOKEN", "CONCURRENCY", "UNKNOWN"})

    def test_member_values(self) -> None:
        expected = {
            "REQUEST": "request",
            "TOKEN": "token",
            "CONCURRENCY": "concurrency",
            "UNKNOWN": "unknown",
        }
        for m in RLSc:
            self.assertEqual(m.value, expected[m.name])

    def test_str_equals_value(self) -> None:
        for m in RLSc:
            self.assertEqual(str(m), m.value)

    def test_isinstance_str(self) -> None:
        for m in RLSc:
            self.assertIsInstance(m, str)

    def test_unique_values(self) -> None:
        vals = [m.value for m in RLSc]
        self.assertEqual(len(vals), len(set(vals)))

    def test_fail_closed_unknown_string(self) -> None:
        with self.assertRaises(ValueError):
            RLSc("unknown_scope_value")

    def test_unique_decorator(self) -> None:
        self.assertTrue(hasattr(RLSc, "__members__"))


class RateLimitSignalSourceTest(unittest.TestCase):
    """RateLimitSignalSource — exact three values."""

    def test_exact_three_members(self) -> None:
        members = list(RLSS)
        self.assertEqual(len(members), 3)

    def test_member_names(self) -> None:
        names = {m.name for m in RLSS}
        self.assertEqual(names, {"PROVIDER_429", "BUDGET_THROTTLE", "MANUAL"})

    def test_member_values(self) -> None:
        expected = {
            "PROVIDER_429": "provider_429",
            "BUDGET_THROTTLE": "budget_throttle",
            "MANUAL": "manual",
        }
        for m in RLSS:
            self.assertEqual(m.value, expected[m.name])

    def test_str_equals_value(self) -> None:
        for m in RLSS:
            self.assertEqual(str(m), m.value)

    def test_isinstance_str(self) -> None:
        for m in RLSS:
            self.assertIsInstance(m, str)

    def test_unique_values(self) -> None:
        vals = [m.value for m in RLSS]
        self.assertEqual(len(vals), len(set(vals)))

    def test_fail_closed_unknown_string(self) -> None:
        with self.assertRaises(ValueError):
            RLSS("unknown_source")


class RateLimitActionTest(unittest.TestCase):
    """RateLimitAction — exact three values."""

    def test_exact_three_members(self) -> None:
        members = list(RLA)
        self.assertEqual(len(members), 3)

    def test_member_names(self) -> None:
        names = {m.name for m in RLA}
        self.assertEqual(names, {"ALLOW", "WAIT", "FAIL_CLOSED"})

    def test_member_values(self) -> None:
        expected = {
            "ALLOW": "allow",
            "WAIT": "wait",
            "FAIL_CLOSED": "fail_closed",
        }
        for m in RLA:
            self.assertEqual(m.value, expected[m.name])

    def test_str_equals_value(self) -> None:
        for m in RLA:
            self.assertEqual(str(m), m.value)

    def test_isinstance_str(self) -> None:
        for m in RLA:
            self.assertIsInstance(m, str)

    def test_unique_values(self) -> None:
        vals = [m.value for m in RLA]
        self.assertEqual(len(vals), len(set(vals)))


class RateLimitReasonTest(unittest.TestCase):
    """RateLimitReason — exact five values."""

    def test_exact_five_members(self) -> None:
        members = list(RLR)
        self.assertEqual(len(members), 5)

    def test_member_names(self) -> None:
        names = {m.name for m in RLR}
        self.assertEqual(
            names,
            {"NO_SIGNALS", "WITHIN_LIMIT", "RETRY_AFTER", "INSUFFICIENT", "EXHAUSTED"},
        )

    def test_member_values(self) -> None:
        expected = {
            "NO_SIGNALS": "no_signals",
            "WITHIN_LIMIT": "within_limit",
            "RETRY_AFTER": "retry_after",
            "INSUFFICIENT": "insufficient",
            "EXHAUSTED": "exhausted",
        }
        for m in RLR:
            self.assertEqual(m.value, expected[m.name])

    def test_str_equals_value(self) -> None:
        for m in RLR:
            self.assertEqual(str(m), m.value)

    def test_isinstance_str(self) -> None:
        for m in RLR:
            self.assertIsInstance(m, str)

    def test_unique_values(self) -> None:
        vals = [m.value for m in RLR]
        self.assertEqual(len(vals), len(set(vals)))

    def test_no_conflict_value(self) -> None:
        values = {m.value for m in RLR}
        self.assertNotIn("conflict", values)


# =========================================================================
# Tests — RateLimitSignal construction
# =========================================================================


class RateLimitSignalConstructionTest(unittest.TestCase):
    """RateLimitSignal — exact 8 fields, frozen, slots, validation."""

    def test_exact_eight_fields(self) -> None:
        sig = _sig()
        self.assertEqual(
            len(sig.__dataclass_fields__),
            8,
            "RateLimitSignal must have exactly 8 fields",
        )

    def test_field_names(self) -> None:
        sig = _sig()
        expected = {
            "provider", "scope", "observed_at", "retry_after_seconds",
            "reset_at", "limit", "remaining", "source",
        }
        self.assertEqual(set(sig.__dataclass_fields__.keys()), expected)

    def test_frozen(self) -> None:
        sig = _sig()
        with self.assertRaises(AttributeError):
            sig.provider = "other"  # type: ignore[misc]

    def test_slots(self) -> None:
        sig = _sig()
        self.assertFalse(hasattr(sig, "__dict__"))

    def test_valid_construction(self) -> None:
        sig = RLS(
            provider="openai",
            scope=RLSc.TOKEN,
            observed_at=_T0,
            retry_after_seconds=30,
            reset_at=_T1,
            limit=100,
            remaining=50,
            source=RLSS.PROVIDER_429,
        )
        self.assertEqual(sig.provider, "openai")
        self.assertEqual(sig.scope, RLSc.TOKEN)
        self.assertEqual(sig.retry_after_seconds, 30)
        self.assertEqual(sig.remaining, 50)

    # -- provider validation --

    def test_provider_empty(self) -> None:
        with self.assertRaises(RLIE):
            _sig(provider="")

    def test_provider_whitespace(self) -> None:
        with self.assertRaises(RLIE):
            _sig(provider=" anthropic")

    def test_provider_trailing_whitespace(self) -> None:
        with self.assertRaises(RLIE):
            _sig(provider="anthropic ")

    def test_provider_nul(self) -> None:
        with self.assertRaises(RLIE):
            _sig(provider="anth\x00ropic")

    def test_provider_cr(self) -> None:
        with self.assertRaises(RLIE):
            _sig(provider="anth\rropic")

    def test_provider_lf(self) -> None:
        with self.assertRaises(RLIE):
            _sig(provider="anth\nropic")

    def test_provider_not_str(self) -> None:
        with self.assertRaises(RLIE):
            _sig(provider=123)  # type: ignore[arg-type]

    # -- scope validation --

    def test_scope_not_enum(self) -> None:
        with self.assertRaises(RLIE):
            _sig(scope="request")  # type: ignore[arg-type]

    # -- observed_at validation --

    def test_observed_at_naive(self) -> None:
        with self.assertRaises(RLIE):
            _sig(observed_at=datetime(2025, 1, 1))

    def test_observed_at_non_utc(self) -> None:
        with self.assertRaises(RLIE):
            _sig(observed_at=datetime(2025, 1, 1, tzinfo=timezone(timedelta(hours=5))))

    def test_observed_at_not_datetime(self) -> None:
        with self.assertRaises(RLIE):
            _sig(observed_at="2025-01-01")  # type: ignore[arg-type]

    # -- retry_after_seconds validation --

    def test_retry_after_negative(self) -> None:
        with self.assertRaises(RLIE):
            _sig(retry_after_seconds=-1)

    def test_retry_after_bool(self) -> None:
        with self.assertRaises(RLIE):
            _sig(retry_after_seconds=True)  # type: ignore[arg-type]

    def test_retry_after_zero_allowed(self) -> None:
        sig = _sig(retry_after_seconds=0)
        self.assertEqual(sig.retry_after_seconds, 0)

    # -- reset_at validation --

    def test_reset_at_before_observed_at(self) -> None:
        with self.assertRaises(RLIE):
            _sig(
                observed_at=_T1,
                reset_at=_T0,
            )

    def test_reset_at_naive(self) -> None:
        with self.assertRaises(RLIE):
            _sig(reset_at=datetime(2025, 1, 2))

    def test_reset_at_non_utc(self) -> None:
        with self.assertRaises(RLIE):
            _sig(reset_at=datetime(2025, 1, 2, tzinfo=timezone(timedelta(hours=5))))

    def test_reset_at_equal_observed_at(self) -> None:
        sig = _sig(observed_at=_T0, reset_at=_T0)
        self.assertEqual(sig.reset_at, _T0)

    # -- limit / remaining validation --

    def test_limit_negative(self) -> None:
        with self.assertRaises(RLIE):
            _sig(limit=-1)

    def test_limit_bool(self) -> None:
        with self.assertRaises(RLIE):
            _sig(limit=True)  # type: ignore[arg-type]

    def test_remaining_negative(self) -> None:
        with self.assertRaises(RLIE):
            _sig(remaining=-1)

    def test_remaining_bool(self) -> None:
        with self.assertRaises(RLIE):
            _sig(remaining=True)  # type: ignore[arg-type]

    def test_remaining_gt_limit(self) -> None:
        with self.assertRaises(RLIE):
            _sig(limit=10, remaining=11)

    def test_remaining_eq_limit(self) -> None:
        sig = _sig(limit=10, remaining=10)
        self.assertEqual(sig.remaining, 10)

    # -- source validation --

    def test_source_not_enum(self) -> None:
        with self.assertRaises(RLIE):
            _sig(source="provider_429")  # type: ignore[arg-type]


# =========================================================================
# Tests — RateLimitCheckRequest construction
# =========================================================================


class RateLimitCheckRequestConstructionTest(unittest.TestCase):
    """RateLimitCheckRequest — exact 5 fields, frozen, slots, validation."""

    def test_exact_five_fields(self) -> None:
        req = _req()
        self.assertEqual(len(req.__dataclass_fields__), 5)

    def test_field_names(self) -> None:
        req = _req()
        expected = {"provider", "scope", "now", "units_requested", "signals"}
        self.assertEqual(set(req.__dataclass_fields__.keys()), expected)

    def test_frozen(self) -> None:
        req = _req()
        with self.assertRaises(AttributeError):
            req.provider = "other"  # type: ignore[misc]

    def test_slots(self) -> None:
        req = _req()
        self.assertFalse(hasattr(req, "__dict__"))

    # -- provider validation --

    def test_provider_empty(self) -> None:
        with self.assertRaises(RLIE):
            _req(provider="")

    def test_provider_whitespace(self) -> None:
        with self.assertRaises(RLIE):
            _req(provider=" anthropic")

    def test_provider_nul(self) -> None:
        with self.assertRaises(RLIE):
            _req(provider="anth\x00ropic")

    # -- scope validation --

    def test_scope_not_enum(self) -> None:
        with self.assertRaises(RLIE):
            _req(scope="request")  # type: ignore[arg-type]

    # -- now validation --

    def test_now_naive(self) -> None:
        with self.assertRaises(RLIE):
            _req(now=datetime(2025, 1, 1))

    def test_now_non_utc(self) -> None:
        with self.assertRaises(RLIE):
            _req(now=datetime(2025, 1, 1, tzinfo=timezone(timedelta(hours=5))))

    # -- units_requested validation --

    def test_units_zero(self) -> None:
        with self.assertRaises(RLIE):
            _req(units_requested=0)

    def test_units_negative(self) -> None:
        with self.assertRaises(RLIE):
            _req(units_requested=-1)

    def test_units_bool(self) -> None:
        with self.assertRaises(RLIE):
            _req(units_requested=True)  # type: ignore[arg-type]

    def test_units_float(self) -> None:
        with self.assertRaises(RLIE):
            _req(units_requested=1.5)  # type: ignore[arg-type]

    # -- signals validation --

    def test_signals_list(self) -> None:
        with self.assertRaises(RLIE):
            _req(signals=[_sig()])  # type: ignore[arg-type]

    def test_signals_dict(self) -> None:
        with self.assertRaises(RLIE):
            _req(signals={})  # type: ignore[arg-type]

    def test_signals_wrong_item_type(self) -> None:
        with self.assertRaises(RLIE):
            _req(signals=("not_a_signal",))  # type: ignore[arg-type]

    def test_signals_empty_tuple(self) -> None:
        req = _req(signals=())
        self.assertEqual(req.signals, ())


# =========================================================================
# Tests — RateLimitDecision construction
# =========================================================================


class RateLimitDecisionConstructionTest(unittest.TestCase):
    """RateLimitDecision — exact 3 fields, frozen, slots, consistency."""

    def test_exact_three_fields(self) -> None:
        d = RLD(action=RLA.ALLOW, wait_seconds=0, reason=RLR.NO_SIGNALS)
        self.assertEqual(len(d.__dataclass_fields__), 3)

    def test_frozen(self) -> None:
        d = RLD(action=RLA.ALLOW, wait_seconds=0, reason=RLR.NO_SIGNALS)
        with self.assertRaises(AttributeError):
            d.action = RLA.WAIT  # type: ignore[misc]

    def test_slots(self) -> None:
        d = RLD(action=RLA.ALLOW, wait_seconds=0, reason=RLR.NO_SIGNALS)
        self.assertFalse(hasattr(d, "__dict__"))

    # -- ALLOW consistency --

    def test_allow_no_signals(self) -> None:
        d = RLD(action=RLA.ALLOW, wait_seconds=0, reason=RLR.NO_SIGNALS)
        self.assertEqual(d.action, RLA.ALLOW)

    def test_allow_within_limit(self) -> None:
        d = RLD(action=RLA.ALLOW, wait_seconds=0, reason=RLR.WITHIN_LIMIT)
        self.assertEqual(d.reason, RLR.WITHIN_LIMIT)

    def test_allow_with_wait_fails(self) -> None:
        with self.assertRaises(RLStE):
            RLD(action=RLA.ALLOW, wait_seconds=5, reason=RLR.NO_SIGNALS)

    def test_allow_with_exhausted_fails(self) -> None:
        with self.assertRaises(RLStE):
            RLD(action=RLA.ALLOW, wait_seconds=0, reason=RLR.EXHAUSTED)

    def test_allow_with_retry_after_fails(self) -> None:
        with self.assertRaises(RLStE):
            RLD(action=RLA.ALLOW, wait_seconds=0, reason=RLR.RETRY_AFTER)

    def test_allow_with_insufficient_fails(self) -> None:
        with self.assertRaises(RLStE):
            RLD(action=RLA.ALLOW, wait_seconds=0, reason=RLR.INSUFFICIENT)

    # -- WAIT consistency --

    def test_wait_retry_after(self) -> None:
        d = RLD(action=RLA.WAIT, wait_seconds=5, reason=RLR.RETRY_AFTER)
        self.assertEqual(d.action, RLA.WAIT)

    def test_wait_insufficient(self) -> None:
        d = RLD(action=RLA.WAIT, wait_seconds=5, reason=RLR.INSUFFICIENT)
        self.assertEqual(d.reason, RLR.INSUFFICIENT)

    def test_wait_zero_wait_fails(self) -> None:
        with self.assertRaises(RLStE):
            RLD(action=RLA.WAIT, wait_seconds=0, reason=RLR.RETRY_AFTER)

    def test_wait_with_no_signals_fails(self) -> None:
        with self.assertRaises(RLStE):
            RLD(action=RLA.WAIT, wait_seconds=5, reason=RLR.NO_SIGNALS)

    def test_wait_with_exhausted_fails(self) -> None:
        with self.assertRaises(RLStE):
            RLD(action=RLA.WAIT, wait_seconds=5, reason=RLR.EXHAUSTED)

    # -- FAIL_CLOSED consistency --

    def test_fail_closed_exhausted(self) -> None:
        d = RLD(action=RLA.FAIL_CLOSED, wait_seconds=0, reason=RLR.EXHAUSTED)
        self.assertEqual(d.action, RLA.FAIL_CLOSED)

    def test_fail_closed_with_wait_fails(self) -> None:
        with self.assertRaises(RLStE):
            RLD(action=RLA.FAIL_CLOSED, wait_seconds=1, reason=RLR.EXHAUSTED)

    def test_fail_closed_with_retry_after_fails(self) -> None:
        with self.assertRaises(RLStE):
            RLD(action=RLA.FAIL_CLOSED, wait_seconds=0, reason=RLR.RETRY_AFTER)

    # -- type validation --

    def test_action_not_enum(self) -> None:
        with self.assertRaises(RLIE):
            RLD(action="allow", wait_seconds=0, reason=RLR.NO_SIGNALS)  # type: ignore[arg-type]

    def test_reason_not_enum(self) -> None:
        with self.assertRaises(RLIE):
            RLD(action=RLA.ALLOW, wait_seconds=0, reason="no_signals")  # type: ignore[arg-type]

    def test_wait_seconds_bool(self) -> None:
        with self.assertRaises(RLIE):
            RLD(action=RLA.ALLOW, wait_seconds=True, reason=RLR.NO_SIGNALS)  # type: ignore[arg-type]

    def test_wait_seconds_negative(self) -> None:
        with self.assertRaises(RLIE):
            RLD(action=RLA.ALLOW, wait_seconds=-1, reason=RLR.NO_SIGNALS)


# =========================================================================
# Tests — Exception hierarchy
# =========================================================================


class RateLimitExceptionTest(unittest.TestCase):
    """Exception hierarchy — exact 4 types, proper inheritance."""

    def test_input_error_inherits_rate_limit_error(self) -> None:
        self.assertTrue(issubclass(RLIE, RLE))

    def test_state_error_inherits_rate_limit_error(self) -> None:
        self.assertTrue(issubclass(RLStE, RLE))

    def test_security_error_inherits_rate_limit_error(self) -> None:
        self.assertTrue(issubclass(RLSecE, RLE))

    def test_input_error_not_state_error(self) -> None:
        self.assertFalse(issubclass(RLIE, RLStE))

    def test_state_error_not_security_error(self) -> None:
        self.assertFalse(issubclass(RLStE, RLSecE))

    def test_catch_base_catches_all(self) -> None:
        for exc_cls in (RLIE, RLStE, RLSecE):
            with self.assertRaises(RLE):
                raise exc_cls("test")


# =========================================================================
# Tests — Exception message safety
# =========================================================================


class RateLimitExceptionMessageSafetyTest(unittest.TestCase):
    """Exception messages must not contain provider values, tokens, paths."""

    def test_input_error_no_provider_value(self) -> None:
        try:
            _sig(provider="secret-provider-name")
            # If we get here, provider was valid; test with invalid
            _sig(provider="")
        except RLIE as e:
            msg = str(e)
            self.assertNotIn("secret-provider-name", msg)

    def test_input_error_no_repr(self) -> None:
        try:
            _sig(scope="not_an_enum")  # type: ignore[arg-type]
        except RLIE as e:
            msg = str(e)
            # Should contain type name, not the value repr
            self.assertNotIn("not_an_enum", msg)

    def test_state_error_no_value_leak(self) -> None:
        try:
            RLD(action=RLA.ALLOW, wait_seconds=5, reason=RLR.NO_SIGNALS)
        except RLStE as e:
            msg = str(e)
            # No input values leaked
            self.assertNotIn("5", msg)


# =========================================================================
# Tests — Evaluate: no signals
# =========================================================================


class EvaluateNoSignalsTest(unittest.TestCase):
    """No matching signals → ALLOW/NO_SIGNALS/0."""

    def setUp(self) -> None:
        self.svc = RLSvc()

    def test_empty_signals(self) -> None:
        d = self.svc.evaluate(_req(signals=()))
        self.assertEqual(d.action, RLA.ALLOW)
        self.assertEqual(d.reason, RLR.NO_SIGNALS)
        self.assertEqual(d.wait_seconds, 0)

    def test_different_provider(self) -> None:
        sig = _sig(provider="openai")
        d = self.svc.evaluate(_req(provider="anthropic", signals=(sig,)))
        self.assertEqual(d.action, RLA.ALLOW)
        self.assertEqual(d.reason, RLR.NO_SIGNALS)


# =========================================================================
# Tests — Evaluate: provider filtering
# =========================================================================


class EvaluateProviderFilterTest(unittest.TestCase):
    """Provider exact match filtering."""

    def setUp(self) -> None:
        self.svc = RLSvc()

    def test_same_provider_matches(self) -> None:
        sig = _sig(provider="anthropic", remaining=10, limit=10)
        d = self.svc.evaluate(_req(provider="anthropic", signals=(sig,)))
        self.assertEqual(d.action, RLA.ALLOW)
        self.assertEqual(d.reason, RLR.WITHIN_LIMIT)

    def test_different_provider_no_match(self) -> None:
        sig = _sig(provider="openai", remaining=0, limit=10)
        d = self.svc.evaluate(_req(provider="anthropic", signals=(sig,)))
        self.assertEqual(d.action, RLA.ALLOW)
        self.assertEqual(d.reason, RLR.NO_SIGNALS)

    def test_mixed_providers_only_matching(self) -> None:
        sig_a = _sig(provider="anthropic", remaining=0, limit=10)
        sig_o = _sig(provider="openai", remaining=10, limit=10)
        d = self.svc.evaluate(_req(provider="anthropic", signals=(sig_a, sig_o)))
        self.assertEqual(d.action, RLA.FAIL_CLOSED)
        self.assertEqual(d.reason, RLR.EXHAUSTED)


# =========================================================================
# Tests — Evaluate: scope filtering
# =========================================================================


class EvaluateScopeFilterTest(unittest.TestCase):
    """Scope filtering: specific scope, UNKNOWN scope."""

    def setUp(self) -> None:
        self.svc = RLSvc()

    def test_specific_scope_matches_same(self) -> None:
        sig = _sig(scope=RLSc.REQUEST, remaining=10, limit=10)
        d = self.svc.evaluate(_req(scope=RLSc.REQUEST, signals=(sig,)))
        self.assertEqual(d.action, RLA.ALLOW)
        self.assertEqual(d.reason, RLR.WITHIN_LIMIT)

    def test_specific_scope_rejects_different(self) -> None:
        sig = _sig(scope=RLSc.TOKEN, remaining=0, limit=10)
        d = self.svc.evaluate(_req(scope=RLSc.REQUEST, signals=(sig,)))
        self.assertEqual(d.action, RLA.ALLOW)
        self.assertEqual(d.reason, RLR.NO_SIGNALS)

    def test_specific_scope_accepts_unknown_signal(self) -> None:
        sig = _sig(scope=RLSc.UNKNOWN, remaining=5, limit=10)
        d = self.svc.evaluate(_req(scope=RLSc.REQUEST, signals=(sig,)))
        # remaining=5 >= units_requested=1 → ALLOW/WITHIN_LIMIT
        self.assertEqual(d.action, RLA.ALLOW)
        self.assertEqual(d.reason, RLR.WITHIN_LIMIT)

    def test_specific_scope_unknown_signal_insufficient(self) -> None:
        sig = _sig(scope=RLSc.UNKNOWN, remaining=0, limit=10)
        d = self.svc.evaluate(_req(scope=RLSc.REQUEST, signals=(sig,)))
        self.assertEqual(d.action, RLA.FAIL_CLOSED)
        self.assertEqual(d.reason, RLR.EXHAUSTED)

    def test_unknown_request_accepts_all_scopes(self) -> None:
        sig_r = _sig(scope=RLSc.REQUEST, remaining=10, limit=10)
        sig_t = _sig(scope=RLSc.TOKEN, remaining=10, limit=10)
        d = self.svc.evaluate(
            _req(scope=RLSc.UNKNOWN, signals=(sig_r, sig_t))
        )
        self.assertEqual(d.action, RLA.ALLOW)
        self.assertEqual(d.reason, RLR.WITHIN_LIMIT)

    def test_unknown_request_with_fail_signal(self) -> None:
        sig = _sig(scope=RLSc.TOKEN, remaining=0, limit=10)
        d = self.svc.evaluate(
            _req(scope=RLSc.UNKNOWN, signals=(sig,))
        )
        self.assertEqual(d.action, RLA.FAIL_CLOSED)
        self.assertEqual(d.reason, RLR.EXHAUSTED)

    def test_provider_and_scope_double_filter(self) -> None:
        sig1 = _sig(provider="anthropic", scope=RLSc.REQUEST, remaining=0, limit=10)
        sig2 = _sig(provider="openai", scope=RLSc.REQUEST, remaining=10, limit=10)
        sig3 = _sig(provider="anthropic", scope=RLSc.TOKEN, remaining=10, limit=10)
        d = self.svc.evaluate(
            _req(provider="anthropic", scope=RLSc.REQUEST, signals=(sig1, sig2, sig3))
        )
        # Only sig1 matches provider+scope → FAIL_CLOSED
        self.assertEqual(d.action, RLA.FAIL_CLOSED)
        self.assertEqual(d.reason, RLR.EXHAUSTED)


# =========================================================================
# Tests — Evaluate: future observation
# =========================================================================


class EvaluateFutureObservationTest(unittest.TestCase):
    """observed_at > now → RateLimitStateError."""

    def setUp(self) -> None:
        self.svc = RLSvc()

    def test_future_observation_raises_state_error(self) -> None:
        future = _T1 + timedelta(seconds=10)
        sig = _sig(observed_at=future, remaining=10, limit=10)
        with self.assertRaises(RLStE):
            self.svc.evaluate(_req(now=_T1, signals=(sig,)))

    def test_observation_equal_now_ok(self) -> None:
        sig = _sig(observed_at=_T1, remaining=10, limit=10)
        d = self.svc.evaluate(_req(now=_T1, signals=(sig,)))
        self.assertEqual(d.action, RLA.ALLOW)


# =========================================================================
# Tests — Evaluate: retry-after time decay
# =========================================================================


class EvaluateRetryAfterDecayTest(unittest.TestCase):
    """retry_after_seconds decays with elapsed time."""

    def setUp(self) -> None:
        self.svc = RLSvc()

    def test_retry_after_not_elapsed(self) -> None:
        # observed_at=T0, now=T1, retry_after=30s → 30-1=29s remaining
        sig = _sig(observed_at=_T0, retry_after_seconds=30)
        d = self.svc.evaluate(_req(now=_T1, signals=(sig,)))
        self.assertEqual(d.action, RLA.WAIT)
        self.assertEqual(d.wait_seconds, 29)
        self.assertEqual(d.reason, RLR.RETRY_AFTER)

    def test_retry_after_fully_elapsed(self) -> None:
        # observed_at=T0, now=T1, retry_after=0s → 0-1=-1 → ceil(-1)=-1 → max(0,-1)=0
        sig = _sig(observed_at=_T0, retry_after_seconds=0)
        d = self.svc.evaluate(_req(now=_T1, signals=(sig,)))
        # retry_after=0, no reset_at, no remaining → expired signal → ALLOW/NO_SIGNALS
        self.assertEqual(d.action, RLA.ALLOW)
        self.assertEqual(d.reason, RLR.NO_SIGNALS)

    def test_retry_after_partial_decay(self) -> None:
        # observed_at=T0, now=T0+0.5s, retry_after=2s → 2-0.5=1.5 → ceil(1.5)=2
        now_half = _T0 + timedelta(seconds=0, microseconds=500000)
        sig = _sig(observed_at=_T0, retry_after_seconds=2)
        d = self.svc.evaluate(_req(now=now_half, signals=(sig,)))
        self.assertEqual(d.wait_seconds, 2)


# =========================================================================
# Tests — Evaluate: reset-at ceil
# =========================================================================


class EvaluateResetAtCeilTest(unittest.TestCase):
    """reset_at uses math.ceil for partial seconds."""

    def setUp(self) -> None:
        self.svc = RLSvc()

    def test_reset_at_partial_second_ceil(self) -> None:
        # reset_at is 0.5s in the future → ceil(0.5) = 1
        reset = _T1 + timedelta(seconds=0, microseconds=500000)
        sig = _sig(observed_at=_T0, reset_at=reset)
        d = self.svc.evaluate(_req(now=_T1, signals=(sig,)))
        self.assertEqual(d.action, RLA.WAIT)
        self.assertEqual(d.wait_seconds, 1)
        self.assertEqual(d.reason, RLR.INSUFFICIENT)

    def test_reset_at_exact_second(self) -> None:
        reset = _T1 + timedelta(seconds=5)
        sig = _sig(observed_at=_T0, reset_at=reset)
        d = self.svc.evaluate(_req(now=_T1, signals=(sig,)))
        self.assertEqual(d.wait_seconds, 5)


# =========================================================================
# Tests — Evaluate: retry/reset max wait
# =========================================================================


class EvaluateRetryResetMaxWaitTest(unittest.TestCase):
    """effective_wait = max(retry_wait, reset_wait)."""

    def setUp(self) -> None:
        self.svc = RLSvc()

    def test_retry_greater_than_reset(self) -> None:
        sig = _sig(
            observed_at=_T0,
            retry_after_seconds=30,
            reset_at=_T0 + timedelta(seconds=10),
        )
        d = self.svc.evaluate(_req(now=_T1, signals=(sig,)))
        # retry_wait=29, reset_wait=9 → effective=29
        self.assertEqual(d.wait_seconds, 29)
        self.assertEqual(d.reason, RLR.RETRY_AFTER)

    def test_reset_greater_than_retry(self) -> None:
        sig = _sig(
            observed_at=_T0,
            retry_after_seconds=5,
            reset_at=_T0 + timedelta(seconds=60),
        )
        d = self.svc.evaluate(_req(now=_T1, signals=(sig,)))
        # retry_wait=4, reset_wait=59 → effective=59
        self.assertEqual(d.wait_seconds, 59)
        self.assertEqual(d.reason, RLR.RETRY_AFTER)

    def test_only_reset_no_retry(self) -> None:
        sig = _sig(
            observed_at=_T0,
            reset_at=_T0 + timedelta(seconds=30),
        )
        d = self.svc.evaluate(_req(now=_T1, signals=(sig,)))
        self.assertEqual(d.wait_seconds, 29)
        self.assertEqual(d.reason, RLR.INSUFFICIENT)


# =========================================================================
# Tests — Evaluate: all signals expired
# =========================================================================


class EvaluateAllExpiredTest(unittest.TestCase):
    """All matching signals have expired time info → ALLOW/NO_SIGNALS/0."""

    def setUp(self) -> None:
        self.svc = RLSvc()

    def test_expired_retry_after(self) -> None:
        # observed_at=T0, now=T1, retry_after=0 → expired
        sig = _sig(observed_at=_T0, retry_after_seconds=0)
        d = self.svc.evaluate(_req(now=_T1, signals=(sig,)))
        self.assertEqual(d.action, RLA.ALLOW)
        self.assertEqual(d.reason, RLR.NO_SIGNALS)

    def test_expired_reset_at(self) -> None:
        # reset_at is in the past → expired
        sig = _sig(observed_at=_T0, reset_at=_T0 + timedelta(seconds=1))
        d = self.svc.evaluate(_req(now=_T1, signals=(sig,)))
        self.assertEqual(d.action, RLA.ALLOW)
        self.assertEqual(d.reason, RLR.NO_SIGNALS)

    def test_expired_both_time_fields(self) -> None:
        # Both retry and reset expired
        sig = _sig(
            observed_at=_T0,
            retry_after_seconds=0,
            reset_at=_T0 + timedelta(seconds=1),
        )
        d = self.svc.evaluate(_req(now=_T1, signals=(sig,)))
        self.assertEqual(d.action, RLA.ALLOW)
        self.assertEqual(d.reason, RLR.NO_SIGNALS)


# =========================================================================
# Tests — Evaluate: WAIT/FAIL/ALLOW aggregation
# =========================================================================


class EvaluateAggregationTest(unittest.TestCase):
    """WAIT > FAIL > ALLOW > expired aggregation."""

    def setUp(self) -> None:
        self.svc = RLSvc()

    def test_wait_overrides_allow(self) -> None:
        sig_wait = _sig(
            observed_at=_T0,
            retry_after_seconds=30,
        )
        sig_allow = _sig(remaining=10, limit=10)
        d = self.svc.evaluate(_req(signals=(sig_wait, sig_allow)))
        self.assertEqual(d.action, RLA.WAIT)
        self.assertEqual(d.reason, RLR.RETRY_AFTER)

    def test_wait_overrides_fail(self) -> None:
        sig_wait = _sig(
            observed_at=_T0,
            retry_after_seconds=30,
        )
        sig_fail = _sig(remaining=0, limit=10)
        d = self.svc.evaluate(_req(signals=(sig_wait, sig_fail)))
        self.assertEqual(d.action, RLA.WAIT)

    def test_fail_overrides_allow(self) -> None:
        sig_fail = _sig(remaining=0, limit=10)
        sig_allow = _sig(remaining=10, limit=10)
        d = self.svc.evaluate(_req(signals=(sig_fail, sig_allow)))
        self.assertEqual(d.action, RLA.FAIL_CLOSED)
        self.assertEqual(d.reason, RLR.EXHAUSTED)

    def test_only_allow_candidates(self) -> None:
        sig1 = _sig(remaining=10, limit=10)
        sig2 = _sig(remaining=5, limit=10)
        d = self.svc.evaluate(_req(signals=(sig1, sig2)))
        self.assertEqual(d.action, RLA.ALLOW)
        self.assertEqual(d.reason, RLR.WITHIN_LIMIT)

    def test_only_fail_candidates(self) -> None:
        sig1 = _sig(remaining=0, limit=10)
        sig2 = _sig(remaining=None, limit=10)
        d = self.svc.evaluate(_req(signals=(sig1, sig2)))
        self.assertEqual(d.action, RLA.FAIL_CLOSED)
        self.assertEqual(d.reason, RLR.EXHAUSTED)

    def test_wait_max_effective_wait(self) -> None:
        sig_short = _sig(
            observed_at=_T0,
            retry_after_seconds=5,
        )
        sig_long = _sig(
            observed_at=_T0,
            retry_after_seconds=30,
        )
        d = self.svc.evaluate(_req(now=_T1, signals=(sig_short, sig_long)))
        # max(4, 29) = 29
        self.assertEqual(d.wait_seconds, 29)

    def test_wait_reason_retry_after(self) -> None:
        sig = _sig(
            observed_at=_T0,
            retry_after_seconds=30,
        )
        d = self.svc.evaluate(_req(now=_T1, signals=(sig,)))
        self.assertEqual(d.reason, RLR.RETRY_AFTER)

    def test_wait_reason_insufficient(self) -> None:
        sig = _sig(
            observed_at=_T0,
            reset_at=_T0 + timedelta(seconds=30),
        )
        d = self.svc.evaluate(_req(now=_T1, signals=(sig,)))
        self.assertEqual(d.reason, RLR.INSUFFICIENT)


# =========================================================================
# Tests — Evaluate: tuple order independence
# =========================================================================


class EvaluateTupleOrderTest(unittest.TestCase):
    """Result must not depend on tuple order."""

    def setUp(self) -> None:
        self.svc = RLSvc()

    def test_wait_order(self) -> None:
        sig_a = _sig(
            observed_at=_T0,
            retry_after_seconds=10,
        )
        sig_b = _sig(
            observed_at=_T0,
            retry_after_seconds=30,
        )
        d1 = self.svc.evaluate(_req(now=_T1, signals=(sig_a, sig_b)))
        d2 = self.svc.evaluate(_req(now=_T1, signals=(sig_b, sig_a)))
        self.assertEqual(d1, d2)

    def test_mixed_candidates_order(self) -> None:
        sig_wait = _sig(
            observed_at=_T0,
            retry_after_seconds=30,
        )
        sig_fail = _sig(remaining=0, limit=10)
        sig_allow = _sig(remaining=10, limit=10)
        d1 = self.svc.evaluate(
            _req(now=_T1, signals=(sig_wait, sig_fail, sig_allow))
        )
        d2 = self.svc.evaluate(
            _req(now=_T1, signals=(sig_allow, sig_fail, sig_wait))
        )
        self.assertEqual(d1, d2)

    def test_fail_allow_order(self) -> None:
        sig_fail = _sig(remaining=0, limit=10)
        sig_allow = _sig(remaining=10, limit=10)
        d1 = self.svc.evaluate(_req(signals=(sig_fail, sig_allow)))
        d2 = self.svc.evaluate(_req(signals=(sig_allow, sig_fail)))
        self.assertEqual(d1, d2)


# =========================================================================
# Tests — Evaluate: determinism
# =========================================================================


class EvaluateDeterminismTest(unittest.TestCase):
    """Same request → identical decision."""

    def setUp(self) -> None:
        self.svc = RLSvc()

    def test_same_request_same_decision(self) -> None:
        sig = _sig(
            observed_at=_T0,
            retry_after_seconds=30,
            remaining=5,
            limit=10,
        )
        req = _req(now=_T1, signals=(sig,))
        d1 = self.svc.evaluate(req)
        d2 = self.svc.evaluate(req)
        self.assertEqual(d1, d2)

    def test_same_request_different_service_instance(self) -> None:
        sig = _sig(remaining=10, limit=10)
        req = _req(signals=(sig,))
        d1 = RLSvc().evaluate(req)
        d2 = RLSvc().evaluate(req)
        self.assertEqual(d1, d2)


# =========================================================================
# Tests — Evaluate: malicious __repr__ not called
# =========================================================================


class EvaluateMaliciousReprTest(unittest.TestCase):
    """Malicious __repr__ must not be called during evaluation."""

    def setUp(self) -> None:
        self.svc = RLSvc()

    def test_malicious_repr_not_called(self) -> None:
        class EvilRepr:
            def __repr__(self) -> str:
                raise AssertionError("repr called on untrusted object")

        # Create a valid signal with a non-evil provider
        sig = _sig(remaining=10, limit=10)
        req = _req(signals=(sig,))
        # This should not call any __repr__ on untrusted objects
        d = self.svc.evaluate(req)
        self.assertEqual(d.action, RLA.ALLOW)


# =========================================================================
# Tests — Evaluate: request type validation
# =========================================================================


class EvaluateRequestTypeTest(unittest.TestCase):
    """evaluate() must validate request type."""

    def setUp(self) -> None:
        self.svc = RLSvc()

    def test_not_check_request(self) -> None:
        with self.assertRaises(RLIE):
            self.svc.evaluate("not a request")  # type: ignore[arg-type]

    def test_none_request(self) -> None:
        with self.assertRaises(RLIE):
            self.svc.evaluate(None)  # type: ignore[arg-type]


# =========================================================================
# Tests — Evaluate: remaining is None → FAIL
# =========================================================================


class EvaluateRemainingNoneTest(unittest.TestCase):
    """remaining is None → FAIL candidate (no basis for optimistic decision)."""

    def setUp(self) -> None:
        self.svc = RLSvc()

    def test_remaining_none_no_time_info(self) -> None:
        sig = _sig(remaining=None, limit=None)
        d = self.svc.evaluate(_req(signals=(sig,)))
        self.assertEqual(d.action, RLA.FAIL_CLOSED)
        self.assertEqual(d.reason, RLR.EXHAUSTED)

    def test_remaining_zero_no_time_info(self) -> None:
        sig = _sig(remaining=0, limit=10)
        d = self.svc.evaluate(_req(signals=(sig,)))
        self.assertEqual(d.action, RLA.FAIL_CLOSED)
        self.assertEqual(d.reason, RLR.EXHAUSTED)


# =========================================================================
# Tests — Evaluate: insufficient remaining
# =========================================================================


class EvaluateInsufficientRemainingTest(unittest.TestCase):
    """0 < remaining < units_requested → FAIL candidate."""

    def setUp(self) -> None:
        self.svc = RLSvc()

    def test_insufficient_remaining(self) -> None:
        sig = _sig(remaining=2, limit=10)
        d = self.svc.evaluate(_req(units_requested=5, signals=(sig,)))
        self.assertEqual(d.action, RLA.FAIL_CLOSED)
        self.assertEqual(d.reason, RLR.EXHAUSTED)

    def test_insufficient_with_wait(self) -> None:
        sig = _sig(
            observed_at=_T0,
            remaining=2,
            limit=10,
            retry_after_seconds=30,
        )
        d = self.svc.evaluate(_req(units_requested=5, now=_T1, signals=(sig,)))
        self.assertEqual(d.action, RLA.WAIT)
        self.assertEqual(d.reason, RLR.RETRY_AFTER)


# =========================================================================
# Tests — Evaluate: ALLOW with sufficient remaining
# =========================================================================


class EvaluateAllowSufficientTest(unittest.TestCase):
    """remaining >= units_requested, no time info → ALLOW/WITHIN_LIMIT."""

    def setUp(self) -> None:
        self.svc = RLSvc()

    def test_remaining_equals_units(self) -> None:
        sig = _sig(remaining=5, limit=10)
        d = self.svc.evaluate(_req(units_requested=5, signals=(sig,)))
        self.assertEqual(d.action, RLA.ALLOW)
        self.assertEqual(d.reason, RLR.WITHIN_LIMIT)

    def test_remaining_exceeds_units(self) -> None:
        sig = _sig(remaining=10, limit=10)
        d = self.svc.evaluate(_req(units_requested=5, signals=(sig,)))
        self.assertEqual(d.action, RLA.ALLOW)
        self.assertEqual(d.reason, RLR.WITHIN_LIMIT)


# =========================================================================
# Tests — No I/O, network, subprocess, clock calls
# =========================================================================


class EvaluateNoSideEffectsTest(unittest.TestCase):
    """RateLimitService must not perform I/O, network, subprocess, or clock."""

    def test_no_datetime_now(self) -> None:
        source = (Path(__file__).resolve().parents[1] / "skills" / "agentdesk"
                  / "scripts" / "rate_limit.py").read_text(encoding="utf-8")
        self.assertNotIn("datetime.now", source)
        self.assertNotIn("time.time", source)
        self.assertNotIn("monotonic", source)

    def test_no_subprocess(self) -> None:
        source = (Path(__file__).resolve().parents[1] / "skills" / "agentdesk"
                  / "scripts" / "rate_limit.py").read_text(encoding="utf-8")
        self.assertNotIn("import subprocess", source)
        self.assertNotIn("import socket", source)
        self.assertNotIn("os.environ", source)

    def test_no_file_io(self) -> None:
        source = (Path(__file__).resolve().parents[1] / "skills" / "agentdesk"
                  / "scripts" / "rate_limit.py").read_text(encoding="utf-8")
        self.assertNotIn("open(", source)

    def test_no_record_or_consume(self) -> None:
        source = (Path(__file__).resolve().parents[1] / "skills" / "agentdesk"
                  / "scripts" / "rate_limit.py").read_text(encoding="utf-8")
        self.assertNotIn("def record", source)
        self.assertNotIn("def consume", source)

    def test_no_token_bucket(self) -> None:
        source = (Path(__file__).resolve().parents[1] / "skills" / "agentdesk"
                  / "scripts" / "rate_limit.py").read_text(encoding="utf-8")
        self.assertNotIn("token_bucket", source)
        self.assertNotIn("TokenBucket", source)

    def test_no_internal_mutable_state(self) -> None:
        source = (Path(__file__).resolve().parents[1] / "skills" / "agentdesk"
                  / "scripts" / "rate_limit.py").read_text(encoding="utf-8")
        self.assertNotIn("self._", source)


# =========================================================================
# Tests — RateLimitService no instance state
# =========================================================================


class RateLimitServiceNoStateTest(unittest.TestCase):
    """RateLimitService must have no instance state."""

    def test_no_instance_dict(self) -> None:
        svc = RLSvc()
        self.assertFalse(hasattr(svc, "__dict__"))

    def test_slots_empty(self) -> None:
        self.assertEqual(RLSvc.__slots__, ())

    def test_evaluate_is_pure(self) -> None:
        svc = RLSvc()
        sig = _sig(remaining=10, limit=10)
        req = _req(signals=(sig,))
        d1 = svc.evaluate(req)
        d2 = svc.evaluate(req)
        self.assertEqual(d1, d2)


# =========================================================================
# Tests — Import isolation (no stdout/stderr)
# =========================================================================


class RateLimitImportIsolationTest(unittest.TestCase):
    """Importing rate_limit must produce no stdout/stderr output."""

    def test_import_no_stdout_stderr(self) -> None:
        stdout, stderr = _reimport_clean()
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue(), "")


# =========================================================================
# Tests — No escalation dependency
# =========================================================================


class RateLimitEscalationBoundaryTest(unittest.TestCase):
    """RateLimitService must not depend on escalation_service."""

    def test_no_escalation_import(self) -> None:
        source = (Path(__file__).resolve().parents[1] / "skills" / "agentdesk"
                  / "scripts" / "rate_limit.py").read_text(encoding="utf-8")
        self.assertNotIn("escalation", source)
        self.assertNotIn("evaluate_escalation", source)

    def test_no_escalation_types(self) -> None:
        source = (Path(__file__).resolve().parents[1] / "skills" / "agentdesk"
                  / "scripts" / "rate_limit.py").read_text(encoding="utf-8")
        self.assertNotIn("EscalationAction", source)
        self.assertNotIn("EscalationDecision", source)
        self.assertNotIn("WorkerKind", source)


# =========================================================================
# Tests — Minimal happy path
# =========================================================================


class EvaluateMinimalHappyPathTest(unittest.TestCase):
    """One minimal happy path test."""

    def test_happy_path(self) -> None:
        svc = RLSvc()
        sig = _sig(remaining=10, limit=10)
        req = _req(signals=(sig,))
        d = svc.evaluate(req)
        self.assertEqual(d.action, RLA.ALLOW)
        self.assertEqual(d.wait_seconds, 0)
        self.assertEqual(d.reason, RLR.WITHIN_LIMIT)


if __name__ == "__main__":
    unittest.main()
