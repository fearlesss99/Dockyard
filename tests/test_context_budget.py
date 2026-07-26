"""TC-13.5.1: ContextBudgetPolicy — comprehensive tests.

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
    """The four frozen percentages must be exactly 20 / 35 / 50 / 65."""

    def test_basic_is_20(self) -> None:
        self.assertEqual(
            context_budget._BUDGET_PERCENT[TD.BASIC], 20
        )

    def test_standard_is_35(self) -> None:
        self.assertEqual(
            context_budget._BUDGET_PERCENT[TD.STANDARD], 35
        )

    def test_advanced_is_50(self) -> None:
        self.assertEqual(
            context_budget._BUDGET_PERCENT[TD.ADVANCED], 50
        )

    def test_expert_is_65(self) -> None:
        self.assertEqual(
            context_budget._BUDGET_PERCENT[TD.EXPERT], 65
        )

    def test_no_other_difficulty_keys_in_percent_table(self) -> None:
        self.assertEqual(
            set(context_budget._BUDGET_PERCENT.keys()),
            {TD.BASIC, TD.STANDARD, TD.ADVANCED, TD.EXPERT},
        )

    def test_cap_table_contains_all_difficulties(self) -> None:
        self.assertEqual(
            set(context_budget._BUDGET_CAP.keys()),
            {TD.BASIC, TD.STANDARD, TD.ADVANCED, TD.EXPERT},
        )

    def test_basic_cap_is_64000(self) -> None:
        self.assertEqual(
            context_budget._BUDGET_CAP[TD.BASIC], 64_000
        )

    def test_standard_cap_is_128000(self) -> None:
        self.assertEqual(
            context_budget._BUDGET_CAP[TD.STANDARD], 128_000
        )

    def test_advanced_cap_is_256000(self) -> None:
        self.assertEqual(
            context_budget._BUDGET_CAP[TD.ADVANCED], 256_000
        )

    def test_expert_cap_is_512000(self) -> None:
        self.assertEqual(
            context_budget._BUDGET_CAP[TD.EXPERT], 512_000
        )


# =========================================================================
# 2 — Full combination result matrix
# =========================================================================

_WINDOWS = (
    1, 2, 3, 4, 5, 7, 10, 99, 100, 101,
    199999, 200000, 319999, 320000, 320001,
    511999, 512000, 512001, 1000000, 100000000,
)


class ResultMatrixTests(unittest.TestCase):
    """Every (difficulty, window) combination validated in detail."""

    # (window, percent, cap) → (expected_budget, cap_active)
    _EXPECTED = {
        # Basic 20% cap=64000
        (1, 20, 64000): (0, False),       # → ValueError
        (2, 20, 64000): (0, False),       # → ValueError
        (3, 20, 64000): (0, False),       # → ValueError
        (4, 20, 64000): (0, False),       # → ValueError
        (5, 20, 64000): (1, False),
        (7, 20, 64000): (1, False),
        (10, 20, 64000): (2, False),
        (99, 20, 64000): (19, False),
        (100, 20, 64000): (20, False),
        (101, 20, 64000): (20, False),
        (199999, 20, 64000): (39999, False),
        (200000, 20, 64000): (40000, False),
        (319999, 20, 64000): (63999, False),
        (320000, 20, 64000): (64000, True),
        (320001, 20, 64000): (64000, True),
        (511999, 20, 64000): (64000, True),
        (512000, 20, 64000): (64000, True),
        (512001, 20, 64000): (64000, True),
        (1000000, 20, 64000): (64000, True),
        (100000000, 20, 64000): (64000, True),
        # Standard 35% cap=128000
        (1, 35, 128000): (0, False),      # → ValueError
        (2, 35, 128000): (0, False),      # → ValueError
        (3, 35, 128000): (1, False),
        (4, 35, 128000): (1, False),
        (5, 35, 128000): (1, False),
        (7, 35, 128000): (2, False),
        (10, 35, 128000): (3, False),
        (99, 35, 128000): (34, False),
        (100, 35, 128000): (35, False),
        (101, 35, 128000): (35, False),
        (199999, 35, 128000): (69999, False),
        (200000, 35, 128000): (70000, False),
        (319999, 35, 128000): (111999, False),
        (320000, 35, 128000): (112000, False),
        (320001, 35, 128000): (112000, False),
        (511999, 35, 128000): (128000, True),  # 511999*35//100=179199, capped
        (512000, 35, 128000): (128000, True),
        (512001, 35, 128000): (128000, True),
        (1000000, 35, 128000): (128000, True),
        (100000000, 35, 128000): (128000, True),
        # Advanced 50% cap=256000
        (1, 50, 256000): (0, False),       # → ValueError
        (2, 50, 256000): (1, False),
        (3, 50, 256000): (1, False),
        (4, 50, 256000): (2, False),
        (5, 50, 256000): (2, False),
        (7, 50, 256000): (3, False),
        (10, 50, 256000): (5, False),
        (99, 50, 256000): (49, False),
        (100, 50, 256000): (50, False),
        (101, 50, 256000): (50, False),
        (199999, 50, 256000): (99999, False),
        (200000, 50, 256000): (100000, False),
        (319999, 50, 256000): (159999, False),
        (320000, 50, 256000): (160000, False),
        (320001, 50, 256000): (160000, False),
        (511999, 50, 256000): (255999, False),
        (512000, 50, 256000): (256000, True),
        (512001, 50, 256000): (256000, True),
        (1000000, 50, 256000): (256000, True),
        (100000000, 50, 256000): (256000, True),
        # Expert 65% cap=512000
        (1, 65, 512000): (0, False),       # → ValueError
        (2, 65, 512000): (1, False),
        (3, 65, 512000): (1, False),
        (4, 65, 512000): (2, False),
        (5, 65, 512000): (3, False),
        (7, 65, 512000): (4, False),
        (10, 65, 512000): (6, False),
        (99, 65, 512000): (64, False),
        (100, 65, 512000): (65, False),
        (101, 65, 512000): (65, False),
        (199999, 65, 512000): (129999, False),
        (200000, 65, 512000): (130000, False),
        (319999, 65, 512000): (207999, False),
        (320000, 65, 512000): (208000, False),
        (320001, 65, 512000): (208000, False),
        (511999, 65, 512000): (332799, False),
        (512000, 65, 512000): (332800, False),
        (512001, 65, 512000): (332800, False),
        (1000000, 65, 512000): (512000, True),
        (100000000, 65, 512000): (512000, True),
    }

    _ZERO_BUDGET = frozenset({
        (TD.BASIC, 1), (TD.BASIC, 2), (TD.BASIC, 3), (TD.BASIC, 4),
        (TD.STANDARD, 1), (TD.STANDARD, 2),
        (TD.ADVANCED, 1),
        (TD.EXPERT, 1),
    })

    def test_all_combinations(self) -> None:
        for difficulty in TD:
            percent = context_budget._BUDGET_PERCENT[difficulty]
            cap = context_budget._BUDGET_CAP[difficulty]
            for c in _WINDOWS:
                key = (c, percent, cap)
                expected_info = self._EXPECTED[key]
                if (difficulty, c) in self._ZERO_BUDGET:
                    with self.subTest(
                        difficulty=difficulty.name, window=c, percent=percent
                    ):
                        with self.assertRaises(
                            ValueError,
                            msg=f"({difficulty.value}, {c}) must raise ValueError",
                        ):
                            context_budget.compute_budget(c, difficulty)
                    continue

                expected_budget = expected_info[0]
                with self.subTest(
                    difficulty=difficulty.name, window=c, percent=percent
                ):
                    result = context_budget.compute_budget(c, difficulty)
                    # six fields
                    self.assertEqual(result.context_window_tokens, c)
                    self.assertIs(result.difficulty, difficulty)
                    self.assertEqual(result.budget_percent, percent)
                    self.assertEqual(result.budget_cap_tokens, cap)
                    self.assertEqual(result.budget_tokens, expected_budget)
                    self.assertEqual(
                        result.reserved_tokens, c - expected_budget
                    )

    def test_budget_tokens_equals_min_floor_percent_cap(self) -> None:
        """budget_tokens == min(c * percent // 100, cap)."""
        for difficulty in TD:
            percent = context_budget._BUDGET_PERCENT[difficulty]
            cap = context_budget._BUDGET_CAP[difficulty]
            for c in _WINDOWS:
                if (difficulty, c) in self._ZERO_BUDGET:
                    continue
                with self.subTest(difficulty=difficulty.name, window=c):
                    result = context_budget.compute_budget(c, difficulty)
                    expected = min(c * percent // 100, cap)
                    self.assertEqual(result.budget_tokens, expected)

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

    def test_budget_never_exceeds_cap(self) -> None:
        """budget_tokens <= cap_tokens for every valid combination."""
        for difficulty in TD:
            cap = context_budget._BUDGET_CAP[difficulty]
            for c in _WINDOWS:
                if (difficulty, c) in self._ZERO_BUDGET:
                    continue
                with self.subTest(difficulty=difficulty.name, window=c):
                    result = context_budget.compute_budget(c, difficulty)
                    self.assertLessEqual(
                        result.budget_tokens, cap,
                        f"budget {result.budget_tokens} > cap {cap}"
                    )

    def test_budget_never_exceeds_percentage_budget(self) -> None:
        """budget_tokens <= c * percent // 100 for every valid combination."""
        for difficulty in TD:
            percent = context_budget._BUDGET_PERCENT[difficulty]
            for c in _WINDOWS:
                if (difficulty, c) in self._ZERO_BUDGET:
                    continue
                with self.subTest(difficulty=difficulty.name, window=c):
                    result = context_budget.compute_budget(c, difficulty)
                    pct_budget = c * percent // 100
                    self.assertLessEqual(
                        result.budget_tokens, pct_budget,
                    )

    # ── Exact-sample regression tests ─────────────────────────────────

    def test_200k_exact_values(self) -> None:
        """Explicit regression: 200,000 window for each difficulty."""
        c = 200000
        cases = [
            (TD.BASIC, 20, 64000, 40000, 160000),
            (TD.STANDARD, 35, 128000, 70000, 130000),
            (TD.ADVANCED, 50, 256000, 100000, 100000),
            (TD.EXPERT, 65, 512000, 130000, 70000),
        ]
        for difficulty, percent, cap, expected_budget, expected_reserved in cases:
            with self.subTest(difficulty=difficulty.name):
                result = context_budget.compute_budget(c, difficulty)
                self.assertEqual(result.budget_percent, percent)
                self.assertEqual(result.budget_cap_tokens, cap)
                self.assertEqual(result.budget_tokens, expected_budget)
                self.assertEqual(result.reserved_tokens, expected_reserved)

    def test_1m_exact_values(self) -> None:
        """Explicit regression: 1,000,000 window for each difficulty."""
        c = 1000000
        cases = [
            (TD.BASIC, 20, 64000, 64000, 936000),
            (TD.STANDARD, 35, 128000, 128000, 872000),
            (TD.ADVANCED, 50, 256000, 256000, 744000),
            (TD.EXPERT, 65, 512000, 512000, 488000),
        ]
        for difficulty, percent, cap, expected_budget, expected_reserved in cases:
            with self.subTest(difficulty=difficulty.name):
                result = context_budget.compute_budget(c, difficulty)
                self.assertEqual(result.budget_percent, percent)
                self.assertEqual(result.budget_cap_tokens, cap)
                self.assertEqual(result.budget_tokens, expected_budget)
                self.assertEqual(result.reserved_tokens, expected_reserved)

    def test_100m_exact_values(self) -> None:
        """Explicit regression: 100,000,000 window for each difficulty."""
        c = 100000000
        cases = [
            (TD.BASIC, 20, 64000, 64000, 99936000),
            (TD.STANDARD, 35, 128000, 128000, 99872000),
            (TD.ADVANCED, 50, 256000, 256000, 99744000),
            (TD.EXPERT, 65, 512000, 512000, 99488000),
        ]
        for difficulty, percent, cap, expected_budget, expected_reserved in cases:
            with self.subTest(difficulty=difficulty.name):
                result = context_budget.compute_budget(c, difficulty)
                self.assertEqual(result.budget_percent, percent)
                self.assertEqual(result.budget_cap_tokens, cap)
                self.assertEqual(result.budget_tokens, expected_budget)
                self.assertEqual(result.reserved_tokens, expected_reserved)

    def test_199999_exact_values(self) -> None:
        """Explicit regression: 199,999 window for each difficulty."""
        c = 199999
        cases = [
            (TD.BASIC, 20, 64000, 39999, 160000),
            (TD.STANDARD, 35, 128000, 69999, 130000),
            (TD.ADVANCED, 50, 256000, 99999, 100000),
            (TD.EXPERT, 65, 512000, 129999, 70000),
        ]
        for difficulty, percent, cap, expected_budget, expected_reserved in cases:
            with self.subTest(difficulty=difficulty.name):
                result = context_budget.compute_budget(c, difficulty)
                self.assertEqual(result.budget_percent, percent)
                self.assertEqual(result.budget_cap_tokens, cap)
                self.assertEqual(result.budget_tokens, expected_budget)
                self.assertEqual(result.reserved_tokens, expected_reserved)

    # ── Cap boundary tests ─────────────────────────────────────────────

    def test_basic_cap_boundary(self) -> None:
        """Basic cap boundary: 319999→63999, 320000→64000, 320001→64000."""
        r1 = context_budget.compute_budget(319999, TD.BASIC)
        self.assertEqual(r1.budget_tokens, 63999)

        r2 = context_budget.compute_budget(320000, TD.BASIC)
        self.assertEqual(r2.budget_tokens, 64000)
        self.assertEqual(r2.budget_tokens, r2.budget_cap_tokens)

        r3 = context_budget.compute_budget(320001, TD.BASIC)
        self.assertEqual(r3.budget_tokens, 64000)
        self.assertEqual(r3.budget_tokens, r3.budget_cap_tokens)

    def test_standard_cap_boundary(self) -> None:
        """Standard cap boundary: 365714→127999, 365715→128000, 365716→128000."""
        r1 = context_budget.compute_budget(365714, TD.STANDARD)
        self.assertEqual(r1.budget_tokens, 127999)

        r2 = context_budget.compute_budget(365715, TD.STANDARD)
        self.assertEqual(r2.budget_tokens, 128000)
        self.assertEqual(r2.budget_tokens, r2.budget_cap_tokens)

        r3 = context_budget.compute_budget(365716, TD.STANDARD)
        self.assertEqual(r3.budget_tokens, 128000)
        self.assertEqual(r3.budget_tokens, r3.budget_cap_tokens)

    def test_advanced_cap_boundary(self) -> None:
        """Advanced cap boundary: 511999→255999, 512000→256000, 512001→256000."""
        r1 = context_budget.compute_budget(511999, TD.ADVANCED)
        self.assertEqual(r1.budget_tokens, 255999)

        r2 = context_budget.compute_budget(512000, TD.ADVANCED)
        self.assertEqual(r2.budget_tokens, 256000)
        self.assertEqual(r2.budget_tokens, r2.budget_cap_tokens)

        r3 = context_budget.compute_budget(512001, TD.ADVANCED)
        self.assertEqual(r3.budget_tokens, 256000)
        self.assertEqual(r3.budget_tokens, r3.budget_cap_tokens)

    def test_expert_cap_boundary(self) -> None:
        """Expert cap boundary: 787692→511999, 787693→512000, 787694→512000."""
        r1 = context_budget.compute_budget(787692, TD.EXPERT)
        self.assertEqual(r1.budget_tokens, 511999)

        r2 = context_budget.compute_budget(787693, TD.EXPERT)
        self.assertEqual(r2.budget_tokens, 512000)
        self.assertEqual(r2.budget_tokens, r2.budget_cap_tokens)

        r3 = context_budget.compute_budget(787694, TD.EXPERT)
        self.assertEqual(r3.budget_tokens, 512000)
        self.assertEqual(r3.budget_tokens, r3.budget_cap_tokens)

    def test_expert_reserved_exactly_ceil_35_percent_for_small_windows(self) -> None:
        """For Expert on small windows (uncapped), reserved == ceil(35% × window)."""
        for c in (1, 2, 3, 7, 10, 99, 100, 101, 199999, 200000):
            if (TD.EXPERT, c) in self._ZERO_BUDGET:
                continue
            with self.subTest(window=c):
                result = context_budget.compute_budget(c, TD.EXPERT)
                pct_budget = c * 65 // 100
                cap = 512000
                expected_budget = min(pct_budget, cap)
                expected_reserved = c - expected_budget
                self.assertEqual(result.reserved_tokens, expected_reserved)


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
        self.assertIn("20%", msg)

    def test_basic_c2_zero_budget_raises_valueerror(self) -> None:
        with self.assertRaises(ValueError):
            context_budget.compute_budget(2, TD.BASIC)

    def test_basic_c3_zero_budget_raises_valueerror(self) -> None:
        with self.assertRaises(ValueError):
            context_budget.compute_budget(3, TD.BASIC)

    def test_basic_c4_zero_budget_raises_valueerror(self) -> None:
        with self.assertRaises(ValueError):
            context_budget.compute_budget(4, TD.BASIC)

    def test_standard_c1_zero_budget_raises_valueerror(self) -> None:
        with self.assertRaises(ValueError):
            context_budget.compute_budget(1, TD.STANDARD)

    def test_standard_c2_zero_budget_raises_valueerror(self) -> None:
        with self.assertRaises(ValueError):
            context_budget.compute_budget(2, TD.STANDARD)

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

    def test_field_names_exact_six_fields(self) -> None:
        result = context_budget.compute_budget(200000, TD.BASIC)
        expected = (
            "context_window_tokens",
            "difficulty",
            "budget_percent",
            "budget_cap_tokens",
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
        self.assertEqual(d["budget_cap_tokens"], 256000)
        self.assertEqual(d["budget_tokens"], 100000)
        self.assertEqual(d["reserved_tokens"], 100000)

    def test_asdict_includes_budget_cap_tokens(self) -> None:
        result = context_budget.compute_budget(1000000, TD.BASIC)
        d = result._asdict()
        self.assertEqual(d["budget_cap_tokens"], 64000)
        self.assertIn("budget_cap_tokens", d)

    def test_two_identical_results_are_equal(self) -> None:
        a = context_budget.compute_budget(200000, TD.BASIC)
        b = context_budget.compute_budget(200000, TD.BASIC)
        self.assertEqual(a, b)

    def test_different_difficulty_not_equal(self) -> None:
        a = context_budget.compute_budget(200000, TD.BASIC)
        b = context_budget.compute_budget(200000, TD.STANDARD)
        self.assertNotEqual(a, b)

    def test_cap_recorded_even_when_not_active(self) -> None:
        """budget_cap_tokens must always record the frozen cap value."""
        result = context_budget.compute_budget(200000, TD.BASIC)
        self.assertEqual(result.budget_cap_tokens, 64000)
        self.assertLess(result.budget_tokens, result.budget_cap_tokens)

    def test_cap_recorded_when_active(self) -> None:
        result = context_budget.compute_budget(1000000, TD.BASIC)
        self.assertEqual(result.budget_cap_tokens, 64000)
        self.assertEqual(result.budget_tokens, result.budget_cap_tokens)


# =========================================================================
# 7 — JSON serialisation round-trip
# =========================================================================


class JsonSerialisationTests(unittest.TestCase):
    """BudgetResult can be serialised to JSON via _asdict()."""

    def test_asdict_produces_valid_json(self) -> None:
        result = context_budget.compute_budget(200000, TD.ADVANCED)
        d = result._asdict()
        d_serialisable = {
            "context_window_tokens": d["context_window_tokens"],
            "difficulty": d["difficulty"].value,
            "budget_percent": d["budget_percent"],
            "budget_cap_tokens": d["budget_cap_tokens"],
            "budget_tokens": d["budget_tokens"],
            "reserved_tokens": d["reserved_tokens"],
        }
        output = json.dumps(d_serialisable, ensure_ascii=False)
        parsed = json.loads(output)
        self.assertEqual(parsed["context_window_tokens"], 200000)
        self.assertEqual(parsed["difficulty"], "advanced")
        self.assertEqual(parsed["budget_percent"], 50)
        self.assertEqual(parsed["budget_cap_tokens"], 256000)
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
                percent = context_budget._BUDGET_PERCENT[difficulty]
                cap = context_budget._BUDGET_CAP[difficulty]
                expected_budget = min(c * percent // 100, cap)
                self.assertEqual(parsed["budget_tokens"], expected_budget)
                self.assertEqual(
                    parsed["reserved_tokens"],
                    c - parsed["budget_tokens"],
                )
                self.assertEqual(parsed["budget_cap_tokens"], cap)


# =========================================================================
# 8 — Large window does not overflow
# =========================================================================


class LargeWindowTests(unittest.TestCase):
    """Very large context_window_tokens must compute without overflow."""

    def test_billion_token_window(self) -> None:
        result = context_budget.compute_budget(10**9, TD.EXPERT)
        self.assertEqual(result.budget_tokens, 512_000)
        self.assertEqual(result.reserved_tokens, 10**9 - 512_000)
        self.assertEqual(
            result.budget_tokens + result.reserved_tokens,
            10**9,
        )

    def test_max_python_int_window(self) -> None:
        """No OverflowError — must compute cleanly."""
        c = 2**63 - 1  # typical 64-bit signed max
        result = context_budget.compute_budget(c, TD.BASIC)
        self.assertEqual(result.budget_tokens, 64_000)  # capped
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

    def test_percent_table_not_in_all(self) -> None:
        self.assertNotIn("_BUDGET_PERCENT", context_budget.__all__)

    def test_cap_table_not_in_all(self) -> None:
        self.assertNotIn("_BUDGET_CAP", context_budget.__all__)


# =========================================================================
# 10 — Import has no side effects
# =========================================================================


class ImportSideEffectTests(unittest.TestCase):
    """Importing context_budget produces no stdout/stderr."""

    def test_import_produces_no_output(self) -> None:
        name = "context_budget"
        if name in sys.modules:
            del sys.modules[name]
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
# 11 — No custom percent or cap override
# =========================================================================


class NoCustomPercentOverrideTests(unittest.TestCase):
    """compute_budget must not accept a custom percent or cap argument."""

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

    def test_cap_table_is_not_in_all(self) -> None:
        self.assertNotIn("_BUDGET_CAP", context_budget.__all__)


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


# =========================================================================
# 14 — Reverse / adversarial tests
# =========================================================================


class ReverseAdversarialTests(unittest.TestCase):
    """Tests that verify invariants cannot be silently violated."""

    def test_extra_key_in_percent_table_detected(self) -> None:
        """If _BUDGET_PERCENT contains an extra key, tests must catch it."""
        actual_len = len(context_budget._BUDGET_PERCENT)
        self.assertEqual(actual_len, 4,
                         f"percent table must have exactly 4 keys, has {actual_len}")

    def test_extra_key_in_cap_table_detected(self) -> None:
        """If _BUDGET_CAP contains an extra key, tests must catch it."""
        actual_len = len(context_budget._BUDGET_CAP)
        self.assertEqual(actual_len, 4,
                         f"cap table must have exactly 4 keys, has {actual_len}")

    def test_missing_difficulty_in_percent_table_detected(self) -> None:
        """Every TaskDifficulty must have an entry in _BUDGET_PERCENT."""
        for d in TD:
            self.assertIn(d, context_budget._BUDGET_PERCENT,
                          f"{d} missing from percent table")

    def test_missing_difficulty_in_cap_table_detected(self) -> None:
        """Every TaskDifficulty must have an entry in _BUDGET_CAP."""
        for d in TD:
            self.assertIn(d, context_budget._BUDGET_CAP,
                          f"{d} missing from cap table")

    def test_fields_exactly_six(self) -> None:
        """BudgetResult._fields must be exactly six."""
        self.assertEqual(
            context_budget.BudgetResult._fields,
            (
                "context_window_tokens",
                "difficulty",
                "budget_percent",
                "budget_cap_tokens",
                "budget_tokens",
                "reserved_tokens",
            ),
        )
        self.assertEqual(len(context_budget.BudgetResult._fields), 6)

    def test_no_third_parameter_accepted(self) -> None:
        """compute_budget must not accept a third positional argument."""
        import inspect
        sig = inspect.signature(context_budget.compute_budget)
        params = list(sig.parameters.keys())
        self.assertEqual(len(params), 2)
        with self.assertRaises(TypeError):
            context_budget.compute_budget(200000, TD.BASIC, 99)  # type: ignore[misc]


class CorruptPercentTableTests(unittest.TestCase):
    """Verify that a corrupt percent table triggers assertion or
    ValueError — never silently computes wrong values.

    These tests temporarily mutate the module-private tables and
    restore them afterwards so no test pollution occurs.
    """

    def setUp(self) -> None:
        self._saved_percent = dict(context_budget._BUDGET_PERCENT)
        self._saved_cap = dict(context_budget._BUDGET_CAP)

    def tearDown(self) -> None:
        context_budget._BUDGET_PERCENT.clear()
        context_budget._BUDGET_PERCENT.update(self._saved_percent)
        context_budget._BUDGET_CAP.clear()
        context_budget._BUDGET_CAP.update(self._saved_cap)

    def test_percent_gt_65_rejected_by_invariant(self) -> None:
        """A percent > 65 would push reserved below 35 % —
        reserved invariant must fire.  Must also lift caps so they
        don't absorb the violation."""
        context_budget._BUDGET_PERCENT = {d: 70 for d in TD}
        # caps must be high enough not to intervene
        context_budget._BUDGET_CAP = {d: 10**9 for d in TD}
        with self.assertRaises(AssertionError,
                               msg="percent 70% must trigger reserved invariant"):
            context_budget.compute_budget(200000, TD.BASIC)

    def test_percent_100_rejected_by_invariant(self) -> None:
        """percent=100 leaves reserved=0 — invariant must fire."""
        context_budget._BUDGET_PERCENT = {d: 100 for d in TD}
        context_budget._BUDGET_CAP = {d: 10**9 for d in TD}
        with self.assertRaises(AssertionError):
            context_budget.compute_budget(200000, TD.BASIC)


class CorruptCapTableTests(unittest.TestCase):
    """Verify that a corrupt cap table triggers an error —
    never silently computes wrong values.

    These tests temporarily mutate the module-private table and
    restore it afterwards so no test pollution occurs.
    """

    def setUp(self) -> None:
        self._saved_cap = dict(context_budget._BUDGET_CAP)

    def tearDown(self) -> None:
        context_budget._BUDGET_CAP.clear()
        context_budget._BUDGET_CAP.update(self._saved_cap)

    def test_zero_cap_is_acceptable(self) -> None:
        """A cap of 0 triggers ValueError because budget rounds to 0."""
        context_budget._BUDGET_CAP = {d: 0 for d in TD}
        with self.assertRaises(ValueError,
                               msg="cap=0 must cause zero-budget ValueError"):
            context_budget.compute_budget(200000, TD.BASIC)

    def test_negative_cap_triggers_assertion_error(self) -> None:
        """A negative cap must trigger AssertionError."""
        context_budget._BUDGET_CAP = {d: -1 for d in TD}
        with self.assertRaises(AssertionError,
                               msg="negative cap must trigger AssertionError"):
            context_budget.compute_budget(200000, TD.BASIC)

    def test_bool_cap_triggers_assertion_error(self) -> None:
        """A bool cap must trigger AssertionError."""
        context_budget._BUDGET_CAP = {d: True for d in TD}  # type: ignore[dict-item]
        with self.assertRaises(AssertionError,
                               msg="bool cap must trigger AssertionError"):
            context_budget.compute_budget(200000, TD.BASIC)

    def test_float_cap_triggers_assertion_error(self) -> None:
        """A float cap must trigger AssertionError."""
        context_budget._BUDGET_CAP = {d: 3.14 for d in TD}  # type: ignore[dict-item]
        with self.assertRaises(AssertionError,
                               msg="float cap must trigger AssertionError"):
            context_budget.compute_budget(200000, TD.BASIC)

    def test_string_cap_triggers_assertion_error(self) -> None:
        """A str cap must trigger AssertionError."""
        context_budget._BUDGET_CAP = {d: "64000" for d in TD}  # type: ignore[dict-item]
        with self.assertRaises(AssertionError,
                               msg="string cap must trigger AssertionError"):
            context_budget.compute_budget(200000, TD.BASIC)


if __name__ == "__main__":
    unittest.main()
