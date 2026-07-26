"""TC-13.5: ContextBudgetPolicy — comprehensive tests.

stdlib-only unittest; no third-party packages, I/O, subprocess, or network.
"""

from __future__ import annotations

import enum
import importlib
import io
import json
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

# -- Load the modules under test ------------------------------------------
_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCRIPTS = _REPO_ROOT / "skills" / "agentdesk" / "scripts"

sys.path.insert(0, str(_SCRIPTS))
import core_types  # noqa: E402
import context_budget  # noqa: E402

sys.path.pop(0)

TD = core_types.TaskDifficulty
WK = core_types.WorkerKind
MDD = core_types.MadDeliberationDepth


# =========================================================================
# 1 — Fixed percentage table (module-private verification)
# =========================================================================


class FixedPercentagesTests(unittest.TestCase):
    """The four frozen percentages must be exactly 15 / 30 / 50 / 65."""

    def test_basic_is_15(self) -> None:
        self.assertEqual(
            context_budget._BUDGET_PERCENT[TD.BASIC], 15
        )

    def test_standard_is_30(self) -> None:
        self.assertEqual(
            context_budget._BUDGET_PERCENT[TD.STANDARD], 30
        )

    def test_advanced_is_50(self) -> None:
        self.assertEqual(
            context_budget._BUDGET_PERCENT[TD.ADVANCED], 50
        )

    def test_expert_is_65(self) -> None:
        self.assertEqual(
            context_budget._BUDGET_PERCENT[TD.EXPERT], 65
        )

    def test_no_other_difficulty_keys(self) -> None:
        self.assertEqual(
            set(context_budget._BUDGET_PERCENT.keys()),
            {TD.BASIC, TD.STANDARD, TD.ADVANCED, TD.EXPERT},
        )


# =========================================================================
# 2 — Full 44-combination result matrix
# =========================================================================

_WINDOWS = (1, 2, 3, 7, 10, 99, 100, 101, 199999, 200000, 1000000)


class ResultMatrixTests(unittest.TestCase):
    """Every (difficulty, window) combination validated in detail."""

    # (window, percent) → expected budget
    _EXPECTED = {
        # basic 15%
        (1, 15): 0,  # → ValueError
        (2, 15): 0,  # → ValueError
        (3, 15): 0,  # → ValueError
        (7, 15): 1,
        (10, 15): 1,
        (99, 15): 14,
        (100, 15): 15,
        (101, 15): 15,
        (199999, 15): 29999,
        (200000, 15): 30000,
        (1000000, 15): 150000,
        # standard 30%
        (1, 30): 0,  # → ValueError
        (2, 30): 0,  # → ValueError
        (3, 30): 0,  # → ValueError
        (7, 30): 2,
        (10, 30): 3,
        (99, 30): 29,
        (100, 30): 30,
        (101, 30): 30,
        (199999, 30): 59999,
        (200000, 30): 60000,
        (1000000, 30): 300000,
        # advanced 50%
        (1, 50): 0,  # → ValueError
        (2, 50): 1,
        (3, 50): 1,
        (7, 50): 3,
        (10, 50): 5,
        (99, 50): 49,
        (100, 50): 50,
        (101, 50): 50,
        (199999, 50): 99999,
        (200000, 50): 100000,
        (1000000, 50): 500000,
        # expert 65%
        (1, 65): 0,  # → ValueError
        (2, 65): 1,
        (3, 65): 1,
        (7, 65): 4,
        (10, 65): 6,
        (99, 65): 64,
        (100, 65): 65,
        (101, 65): 65,
        (199999, 65): 129999,
        (200000, 65): 130000,
        (1000000, 65): 650000,
    }

    _ZERO_BUDGET = frozenset({
        (TD.BASIC, 1), (TD.BASIC, 2), (TD.BASIC, 3),
        (TD.STANDARD, 1), (TD.STANDARD, 2), (TD.STANDARD, 3),
        (TD.ADVANCED, 1),
        (TD.EXPERT, 1),
    })

    def test_all_valid_combinations_return_five_fields(self) -> None:
        for difficulty in TD:
            percent = context_budget._BUDGET_PERCENT[difficulty]
            for c in _WINDOWS:
                key = (c, percent)
                expected_budget = self._EXPECTED[key]
                if (difficulty, c) in self._ZERO_BUDGET:
                    # Must raise ValueError
                    with self.subTest(
                        difficulty=difficulty.name, window=c, percent=percent
                    ):
                        with self.assertRaises(
                            ValueError,
                            msg=f"({difficulty.value}, {c}) must raise ValueError",
                        ):
                            context_budget.compute_budget(c, difficulty)
                    continue

                with self.subTest(
                    difficulty=difficulty.name, window=c, percent=percent
                ):
                    result = context_budget.compute_budget(c, difficulty)
                    # five fields
                    self.assertEqual(result.context_window_tokens, c)
                    self.assertIs(result.difficulty, difficulty)
                    self.assertEqual(result.budget_percent, percent)
                    self.assertEqual(result.budget_tokens, expected_budget)
                    self.assertEqual(
                        result.reserved_tokens, c - expected_budget
                    )

    def test_floor_integer_formula(self) -> None:
        """budget_tokens == context_window_tokens * percent // 100."""
        for difficulty in TD:
            percent = context_budget._BUDGET_PERCENT[difficulty]
            for c in _WINDOWS:
                if (difficulty, c) in self._ZERO_BUDGET:
                    continue
                with self.subTest(difficulty=difficulty.name, window=c):
                    result = context_budget.compute_budget(c, difficulty)
                    self.assertEqual(
                        result.budget_tokens, c * percent // 100
                    )

    def test_total_conservation(self) -> None:
        """budget_tokens + reserved_tokens == context_window_tokens."""
        for difficulty in TD:
            for c in _WINDOWS:
                if (difficulty, c) in self._ZERO_BUDGET:
                    continue
                with self.subTest(difficulty=difficulty.name, window=c):
                    result = context_budget.compute_budget(c, difficulty)
                    self.assertEqual(
                        result.budget_tokens + result.reserved_tokens,
                        result.context_window_tokens,
                    )

    def test_at_least_35_percent_reserved(self) -> None:
        """reserved_tokens >= ceil(context_window_tokens * 35 / 100)."""
        for difficulty in TD:
            for c in _WINDOWS:
                if (difficulty, c) in self._ZERO_BUDGET:
                    continue
                with self.subTest(difficulty=difficulty.name, window=c):
                    result = context_budget.compute_budget(c, difficulty)
                    min_reserved = (c * 35 + 99) // 100  # ceil(35%)
                    self.assertGreaterEqual(result.reserved_tokens, min_reserved)

    def test_199999_exact_values(self) -> None:
        """Explicit regression: 199999 window for each difficulty."""
        c = 199999
        cases = [
            (TD.BASIC, 15, 29999, 170000),
            (TD.STANDARD, 30, 59999, 140000),
            (TD.ADVANCED, 50, 99999, 100000),
            (TD.EXPERT, 65, 129999, 70000),
        ]
        for difficulty, percent, expected_budget, expected_reserved in cases:
            with self.subTest(difficulty=difficulty.name):
                result = context_budget.compute_budget(c, difficulty)
                self.assertEqual(result.budget_percent, percent)
                self.assertEqual(result.budget_tokens, expected_budget)
                self.assertEqual(
                    result.reserved_tokens, expected_reserved
                )

    def test_expert_reserved_exactly_ceil_35_percent(self) -> None:
        """For Expert, reserved_tokens == ceil(35% × context_window_tokens)."""
        for c in _WINDOWS:
            if (TD.EXPERT, c) in self._ZERO_BUDGET:
                continue
            with self.subTest(window=c):
                result = context_budget.compute_budget(c, TD.EXPERT)
                expected = (c * 35 + 99) // 100
                self.assertEqual(result.reserved_tokens, expected)


# =========================================================================
# 3 — Zero-budget ValueError rejection
# =========================================================================


class ZeroBudgetRejectionTests(unittest.TestCase):
    """Every combination yielding budget_tokens < 1 must raise ValueError."""

    def test_basic_c1_zero_budget_raises_valueerror(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            context_budget.compute_budget(1, TD.BASIC)
        msg = str(ctx.exception)
        self.assertIn("1", msg)
        self.assertIn("basic", msg)
        self.assertIn("15%", msg)

    def test_basic_c2_zero_budget_raises_valueerror(self) -> None:
        with self.assertRaises(ValueError):
            context_budget.compute_budget(2, TD.BASIC)

    def test_basic_c3_zero_budget_raises_valueerror(self) -> None:
        with self.assertRaises(ValueError):
            context_budget.compute_budget(3, TD.BASIC)

    def test_standard_c1_zero_budget_raises_valueerror(self) -> None:
        with self.assertRaises(ValueError):
            context_budget.compute_budget(1, TD.STANDARD)

    def test_standard_c2_zero_budget_raises_valueerror(self) -> None:
        with self.assertRaises(ValueError):
            context_budget.compute_budget(2, TD.STANDARD)

    def test_standard_c3_zero_budget_raises_valueerror(self) -> None:
        with self.assertRaises(ValueError):
            context_budget.compute_budget(3, TD.STANDARD)

    def test_advanced_c1_zero_budget_raises_valueerror(self) -> None:
        with self.assertRaises(ValueError):
            context_budget.compute_budget(1, TD.ADVANCED)

    def test_expert_c1_zero_budget_raises_valueerror(self) -> None:
        with self.assertRaises(ValueError):
            context_budget.compute_budget(1, TD.EXPERT)


# =========================================================================
# 4 — Difficulty type errors (TypeError)
# =========================================================================

_DIFFICULTY_TYPE_ERROR_INPUTS: list[tuple[str, object]] = [
    ("bare string 'basic'", "basic"),
    ("bare string 'standard'", "standard"),
    ("bare string 'advanced'", "advanced"),
    ("bare string 'expert'", "expert"),
    ("WorkerKind BASIC_AGENT", WK.BASIC_AGENT),
    ("WorkerKind STANDARD_AGENT", WK.STANDARD_AGENT),
    ("WorkerKind ADVANCED_AGENT", WK.ADVANCED_AGENT),
    ("WorkerKind EXPERT_AGENT", WK.EXPERT_AGENT),
    ("MadDeliberationDepth FAST", MDD.FAST),
    ("MadDeliberationDepth BALANCED", MDD.BALANCED),
    ("MadDeliberationDepth DEEP", MDD.DEEP),
    ("None", None),
    ("True", True),
    ("False", False),
    ("int 0", 0),
    ("int -1", -1),
    ("int 42", 42),
    ("float 3.14", 3.14),
    ("list", ["basic"]),
    ("dict", {"value": "basic"}),
]


class DifficultyTypeErrorTests(unittest.TestCase):
    """Non-TaskDifficulty difficulty inputs must raise TypeError."""

    def test_all_invalid_difficulty_inputs_rejected(self) -> None:
        for desc, value in _DIFFICULTY_TYPE_ERROR_INPUTS:
            with self.subTest(desc=desc, value=value):
                with self.assertRaises(
                    TypeError,
                    msg=f"difficulty={value!r} must raise TypeError",
                ):
                    context_budget.compute_budget(200000, value)


# =========================================================================
# 5 — context_window_tokens validation errors
# =========================================================================


class ContextWindowTokensTypeErrorTests(unittest.TestCase):
    """Non-int (or bool) context_window_tokens must raise TypeError."""

    def test_true_rejected(self) -> None:
        with self.assertRaises(TypeError):
            context_budget.compute_budget(True, TD.BASIC)

    def test_false_rejected(self) -> None:
        with self.assertRaises(TypeError):
            context_budget.compute_budget(False, TD.EXPERT)

    def test_none_rejected(self) -> None:
        with self.assertRaises(TypeError):
            context_budget.compute_budget(None, TD.BASIC)

    def test_float_rejected(self) -> None:
        with self.assertRaises(TypeError):
            context_budget.compute_budget(3.14, TD.BASIC)

    def test_string_rejected(self) -> None:
        with self.assertRaises(TypeError):
            context_budget.compute_budget("100000", TD.BASIC)

    def test_empty_string_rejected(self) -> None:
        with self.assertRaises(TypeError):
            context_budget.compute_budget("", TD.BASIC)

    def test_list_rejected(self) -> None:
        with self.assertRaises(TypeError):
            context_budget.compute_budget([200000], TD.BASIC)

    def test_dict_rejected(self) -> None:
        with self.assertRaises(TypeError):
            context_budget.compute_budget({"tokens": 100}, TD.BASIC)


class ContextWindowTokensValueErrorTests(unittest.TestCase):
    """Non-positive context_window_tokens must raise ValueError."""

    def test_zero_rejected(self) -> None:
        with self.assertRaises(ValueError):
            context_budget.compute_budget(0, TD.BASIC)

    def test_negative_one_rejected(self) -> None:
        with self.assertRaises(ValueError):
            context_budget.compute_budget(-1, TD.BASIC)

    def test_large_negative_rejected(self) -> None:
        with self.assertRaises(ValueError):
            context_budget.compute_budget(-1000000, TD.BASIC)


# =========================================================================
# 6 — BudgetResult immutability and properties
# =========================================================================


class BudgetResultPropertiesTests(unittest.TestCase):
    """BudgetResult must be immutable, hashable, and have exact field names."""

    def test_is_namedtuple(self) -> None:
        result = context_budget.compute_budget(200000, TD.BASIC)
        self.assertIsInstance(result, tuple)

    def test_is_immutable(self) -> None:
        result = context_budget.compute_budget(200000, TD.BASIC)
        with self.assertRaises(AttributeError):
            result.budget_tokens = 99999  # type: ignore[misc]

    def test_is_hashable(self) -> None:
        result = context_budget.compute_budget(200000, TD.BASIC)
        # Must not raise TypeError
        hash(result)

    def test_field_names_exact(self) -> None:
        result = context_budget.compute_budget(200000, TD.BASIC)
        expected = (
            "context_window_tokens",
            "difficulty",
            "budget_percent",
            "budget_tokens",
            "reserved_tokens",
        )
        self.assertEqual(result._fields, expected)

    def test_asdict_round_trip(self) -> None:
        result = context_budget.compute_budget(200000, TD.ADVANCED)
        d = result._asdict()
        self.assertEqual(d["context_window_tokens"], 200000)
        self.assertIs(d["difficulty"], TD.ADVANCED)
        self.assertEqual(d["budget_percent"], 50)
        self.assertEqual(d["budget_tokens"], 100000)
        self.assertEqual(d["reserved_tokens"], 100000)

    def test_two_identical_results_are_equal(self) -> None:
        a = context_budget.compute_budget(200000, TD.BASIC)
        b = context_budget.compute_budget(200000, TD.BASIC)
        self.assertEqual(a, b)

    def test_different_difficulty_not_equal(self) -> None:
        a = context_budget.compute_budget(200000, TD.BASIC)
        b = context_budget.compute_budget(200000, TD.STANDARD)
        self.assertNotEqual(a, b)


# =========================================================================
# 7 — JSON serialisation round-trip
# =========================================================================


class JsonSerialisationTests(unittest.TestCase):
    """BudgetResult can be serialised to JSON via _asdict()."""

    def test_asdict_produces_valid_json(self) -> None:
        result = context_budget.compute_budget(200000, TD.ADVANCED)
        d = result._asdict()
        # difficulty is an enum member — asdict does not stringify it
        d_serialisable = {
            "context_window_tokens": d["context_window_tokens"],
            "difficulty": d["difficulty"].value,
            "budget_percent": d["budget_percent"],
            "budget_tokens": d["budget_tokens"],
            "reserved_tokens": d["reserved_tokens"],
        }
        output = json.dumps(d_serialisable, ensure_ascii=False)
        parsed = json.loads(output)
        self.assertEqual(parsed["context_window_tokens"], 200000)
        self.assertEqual(parsed["difficulty"], "advanced")
        self.assertEqual(parsed["budget_percent"], 50)
        self.assertEqual(parsed["budget_tokens"], 100000)
        self.assertEqual(parsed["reserved_tokens"], 100000)

    def test_full_matrix_json_round_trip(self) -> None:
        for difficulty in TD:
            for c in (100, 200000, 1000000):
                result = context_budget.compute_budget(c, difficulty)
                d = result._asdict()
                d_ser = {
                    k: (v.value if hasattr(v, "value") else v)
                    for k, v in d.items()
                }
                output = json.dumps(d_ser, ensure_ascii=False)
                parsed = json.loads(output)
                self.assertEqual(parsed["context_window_tokens"], c)
                self.assertEqual(parsed["difficulty"], difficulty.value)
                self.assertEqual(
                    parsed["budget_tokens"],
                    c * context_budget._BUDGET_PERCENT[difficulty] // 100,
                )
                self.assertEqual(
                    parsed["reserved_tokens"],
                    c - parsed["budget_tokens"],
                )


# =========================================================================
# 8 — Large window does not overflow
# =========================================================================


class LargeWindowTests(unittest.TestCase):
    """Very large context_window_tokens must compute without overflow."""

    def test_billion_token_window(self) -> None:
        result = context_budget.compute_budget(10**9, TD.EXPERT)
        self.assertEqual(result.budget_tokens, 650_000_000)
        self.assertEqual(result.reserved_tokens, 350_000_000)
        self.assertEqual(
            result.budget_tokens + result.reserved_tokens,
            10**9,
        )

    def test_max_python_int_window(self) -> None:
        """No OverflowError — must compute cleanly."""
        c = 2**63 - 1  # typical 64-bit signed max
        result = context_budget.compute_budget(c, TD.BASIC)
        self.assertEqual(result.budget_tokens, c * 15 // 100)
        self.assertEqual(
            result.budget_tokens + result.reserved_tokens, c
        )


# =========================================================================
# 9 — __all__ is exactly the two public names
# =========================================================================


class AllExportsTests(unittest.TestCase):
    """__all__ exposes exactly BudgetResult and compute_budget."""

    def test_all_exact(self) -> None:
        expected = frozenset({"BudgetResult", "compute_budget"})
        actual = frozenset(context_budget.__all__)
        self.assertSetEqual(expected, actual)

    def test_all_has_two_names(self) -> None:
        self.assertEqual(len(context_budget.__all__), 2)


# =========================================================================
# 10 — Import has no side effects
# =========================================================================


class ImportSideEffectTests(unittest.TestCase):
    """Importing context_budget produces no stdout/stderr."""

    def test_import_produces_no_output(self) -> None:
        name = "context_budget"
        if name in sys.modules:
            del sys.modules[name]
        # Also need to ensure core_types is re-imported cleanly.
        stdout_capture = io.StringIO()
        stderr_capture = io.StringIO()
        with redirect_stdout(stdout_capture), redirect_stderr(stderr_capture):
            sys.path.insert(0, str(_SCRIPTS))
            try:
                importlib.import_module(name)
            finally:
                sys.path.remove(str(_SCRIPTS))
        self.assertEqual(stdout_capture.getvalue(), "",
                         "import must not write to stdout")
        self.assertEqual(stderr_capture.getvalue(), "",
                         "import must not write to stderr")

    def test_module_has_no_io_attributes(self) -> None:
        names = set(dir(context_budget))
        for forbidden in ("subprocess", "socket", "urlopen", "requests",
                          "open", "environ", "argv", "pathlib"):
            self.assertNotIn(forbidden, names,
                             f"module must not expose {forbidden}")


# =========================================================================
# 11 — No custom percentage override
# =========================================================================


class NoCustomPercentOverrideTests(unittest.TestCase):
    """compute_budget must not accept a custom percent argument."""

    def test_no_percent_parameter(self) -> None:
        import inspect
        sig = inspect.signature(context_budget.compute_budget)
        params = list(sig.parameters.keys())
        self.assertEqual(
            params,
            ["context_window_tokens", "difficulty"],
            f"unexpected parameters: {params}",
        )

    def test_percent_table_is_not_in_all(self) -> None:
        self.assertNotIn("_BUDGET_PERCENT", context_budget.__all__)


# =========================================================================
# 12 — No I/O, no file writes
# =========================================================================


class NoIOTests(unittest.TestCase):
    """Module must not perform any I/O, subprocess, or filesystem access."""

    def test_no_builtin_open_in_module(self) -> None:
        self.assertNotIn("open", dir(context_budget))

    def test_no_os_in_module(self) -> None:
        self.assertNotIn("os", dir(context_budget))

    def test_no_sys_in_module(self) -> None:
        self.assertNotIn("sys", dir(context_budget))


# =========================================================================
# 13 — BudgetResult does not leak into persistent evidence paths
# =========================================================================


class NoPersistenceLeakTests(unittest.TestCase):
    """BudgetResult is a pure in-memory type — no write methods."""

    def test_budgetresult_has_no_write_methods(self) -> None:
        forbidden = ("write", "save", "dump", "persist", "serialize",
                     "to_file", "to_yaml")
        for name in forbidden:
            self.assertFalse(hasattr(context_budget.BudgetResult, name),
                             f"BudgetResult must not have {name}()")


if __name__ == "__main__":
    unittest.main()
