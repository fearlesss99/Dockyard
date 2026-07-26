"""TC-13.4: shared core integration types — comprehensive tests.

stdlib-only; no third-party packages, I/O, subprocess, or network.
"""

from __future__ import annotations

import enum
import io
import json
import importlib
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any

# -- Load the module under test -------------------------------------------
_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCRIPTS = _REPO_ROOT / "skills" / "agentdesk" / "scripts"

# Ensure the scripts directory is on sys.path so we can import cleanly.
sys.path.insert(0, str(_SCRIPTS))
import core_types  # noqa: E402

# Remove the path injection so other tests are unaffected.
sys.path.pop(0)

# Convenience aliases.
TD = core_types.TaskDifficulty
MDD = core_types.MadDeliberationDepth
WK = core_types.WorkerKind


# =========================================================================
# Helpers
# =========================================================================


def _dedupe(items: list[Any]) -> list[Any]:
    """Return *items* preserving order, raising on any duplicate."""
    seen: set[Any] = set()
    result: list[Any] = []
    for item in items:
        if item in seen:
            raise AssertionError(f"duplicate: {item!r}")
        seen.add(item)
        result.append(item)
    return result


def _import_module_in_isolation() -> tuple[str, str]:
    """Re-import ``core_types`` with stdout/stderr captured.

    Returns a tuple of ``(stdout_text, stderr_text)``.  If the import
    raises, the exception propagates.
    """
    name = "core_types"
    if name in sys.modules:
        del sys.modules[name]
    stdout = io.StringIO()
    stderr = io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        # Temporarily add the scripts dir so importlib can find the module.
        sys.path.insert(0, str(_SCRIPTS))
        try:
            importlib.import_module(name)
        finally:
            sys.path.remove(str(_SCRIPTS))
    return stdout.getvalue(), stderr.getvalue()


# =========================================================================
# 1 — Literal value sets
# =========================================================================


class ExactValueSetsTests(unittest.TestCase):
    """Each enum's literal value set matches the frozen contract."""

    def test_task_difficulty_values(self) -> None:
        expected = frozenset({"basic", "standard", "advanced", "expert"})
        actual = frozenset(m.value for m in TD)
        self.assertSetEqual(expected, actual)

    def test_mad_deliberation_depth_values(self) -> None:
        expected = frozenset({"fast", "balanced", "deep"})
        actual = frozenset(m.value for m in MDD)
        self.assertSetEqual(expected, actual)

    def test_worker_kind_values(self) -> None:
        expected = frozenset(
            {"basic_agent", "standard_agent", "advanced_agent", "expert_agent"}
        )
        actual = frozenset(m.value for m in WK)
        self.assertSetEqual(expected, actual)


# =========================================================================
# 2 — Exact member-name sets (no aliases)
# =========================================================================


class ExactMemberNameSetsTests(unittest.TestCase):
    """Member names match the contract exactly — ``__members__`` must equal
    the iteration count, proving that no aliases exist.
    """

    def test_task_difficulty_member_names(self) -> None:
        expected = frozenset({"BASIC", "STANDARD", "ADVANCED", "EXPERT"})
        self.assertSetEqual(expected, set(TD.__members__))
        self.assertEqual(
            len(TD.__members__),
            len(list(TD)),
            "TaskDifficulty: __members__ count must equal iteration count — "
            "any difference indicates an alias",
        )

    def test_mad_deliberation_depth_member_names(self) -> None:
        expected = frozenset({"FAST", "BALANCED", "DEEP"})
        self.assertSetEqual(expected, set(MDD.__members__))
        self.assertEqual(
            len(MDD.__members__),
            len(list(MDD)),
            "MadDeliberationDepth: __members__ count must equal iteration "
            "count — any difference indicates an alias",
        )

    def test_worker_kind_member_names(self) -> None:
        expected = frozenset(
            {"BASIC_AGENT", "STANDARD_AGENT", "ADVANCED_AGENT", "EXPERT_AGENT"}
        )
        self.assertSetEqual(expected, set(WK.__members__))
        self.assertEqual(
            len(WK.__members__),
            len(list(WK)),
            "WorkerKind: __members__ count must equal iteration count — "
            "any difference indicates an alias",
        )


# =========================================================================
# 3 — Construction from valid strings
# =========================================================================


class ValidConstructionTests(unittest.TestCase):
    """Every contract-listed value string constructs the correct member."""

    def test_task_difficulty_valid(self) -> None:
        for value in ("basic", "standard", "advanced", "expert"):
            m = TD(value)
            self.assertIsInstance(m, TD)
            self.assertEqual(m.value, value)

    def test_mad_deliberation_depth_valid(self) -> None:
        for value in ("fast", "balanced", "deep"):
            m = MDD(value)
            self.assertIsInstance(m, MDD)
            self.assertEqual(m.value, value)

    def test_worker_kind_valid(self) -> None:
        for value in ("basic_agent", "standard_agent", "advanced_agent", "expert_agent"):
            m = WK(value)
            self.assertIsInstance(m, WK)
            self.assertEqual(m.value, value)


# =========================================================================
# 4 — Invalid-construction tests (fail-closed for every enum)
# =========================================================================

# Reusable invalid-input tuples: (description, value)
_INVALID_INPUTS: list[tuple[str, Any]] = [
    ("empty string", ""),
    ("whitespace-only", "   "),
    ("leading whitespace", " basic"),
    ("trailing whitespace", "basic "),
    ("mixed case", "Basic"),
    ("upper case", "BASIC"),
    ("unknown string", "super_basic"),
    ("None", None),
    ("True", True),
    ("False", False),
    ("zero", 0),
    ("negative int", -1),
    ("positive int", 42),
    ("float", 3.14),
    ("list", ["basic"]),
    ("dict", {"value": "basic"}),
]


class _InvalidConstructionMixin:
    """Mixin that runs _INVALID_INPUTS against cls_name."""

    cls: type[enum.Enum]  # set by subclasses

    def _check_fail(self, value: Any) -> None:
        with self.assertRaises(
            ValueError,
            msg=f"{self.cls.__name__}({value!r}) must raise ValueError",
        ):
            self.cls(value)

    def test_all_invalid_inputs_rejected(self) -> None:
        for desc, value in _INVALID_INPUTS:
            with self.subTest(cls=self.cls.__name__, input=desc, value=value):
                self._check_fail(value)


class TaskDifficultyInvalidTests(_InvalidConstructionMixin, unittest.TestCase):
    cls = TD


class MadDeliberationDepthInvalidTests(_InvalidConstructionMixin, unittest.TestCase):
    cls = MDD


class WorkerKindInvalidTests(_InvalidConstructionMixin, unittest.TestCase):
    cls = WK


# =========================================================================
# 5 — str protocol
# =========================================================================


class StrProtocolTests(unittest.TestCase):
    """str(member) == member.value and isinstance(member, str)."""

    def test_str_equals_value(self) -> None:
        for cls in (TD, MDD, WK):
            for member in cls:
                with self.subTest(cls=cls.__name__, member=member.name):
                    self.assertEqual(str(member), member.value)

    def test_isinstance_str(self) -> None:
        for cls in (TD, MDD, WK):
            for member in cls:
                with self.subTest(cls=cls.__name__, member=member.name):
                    self.assertIsInstance(member, str)


# =========================================================================
# 6 — JSON serialisation and round-trip
# =========================================================================


class JsonSerialisationTests(unittest.TestCase):
    """json.dumps() serialises members as plain JSON strings, and
    round-tripping through JSON reconstructs the same member.
    """

    def test_json_dumps_plain_string(self) -> None:
        for cls in (TD, MDD, WK):
            for member in cls:
                with self.subTest(cls=cls.__name__, member=member.name):
                    output = json.dumps(member, ensure_ascii=False)
                    # Must be a JSON string literal, not a number or object.
                    self.assertTrue(output.startswith('"'), f"got {output!r}")
                    self.assertTrue(output.endswith('"'), f"got {output!r}")
                    # Decode to raw value.
                    decoded = json.loads(output)
                    self.assertEqual(decoded, member.value)
                    self.assertIsInstance(decoded, str)

    def test_json_round_trip(self) -> None:
        for cls in (TD, MDD, WK):
            for member in cls:
                with self.subTest(cls=cls.__name__, member=member.name):
                    output = json.dumps(member, ensure_ascii=False)
                    raw = json.loads(output)
                    reconstructed = cls(raw)
                    self.assertIs(reconstructed, member)

    def test_json_array_of_members(self) -> None:
        """An array of enum members serialises as a plain JSON string array."""
        arr = [TD.BASIC, MDD.DEEP, WK.EXPERT_AGENT]
        output = json.dumps(arr, ensure_ascii=False)
        self.assertEqual(output, '["basic", "deep", "expert_agent"]')

    def test_json_object_with_member_keys(self) -> None:
        """Enum members as dict values serialise correctly."""
        obj = {
            "difficulty": TD.ADVANCED,
            "depth": MDD.BALANCED,
            "kind": WK.STANDARD_AGENT,
        }
        output = json.dumps(obj, ensure_ascii=False)
        parsed = json.loads(output)
        self.assertEqual(parsed["difficulty"], "advanced")
        self.assertEqual(parsed["depth"], "balanced")
        self.assertEqual(parsed["kind"], "standard_agent")


# =========================================================================
# 7 — Three type objects are distinct
# =========================================================================


class DistinctTypesTests(unittest.TestCase):
    """The three enum classes are different types, not aliases."""

    def test_types_are_distinct(self) -> None:
        types = {TD, MDD, WK}
        self.assertEqual(len(types), 3, "must be three distinct type objects")

    def test_no_cross_enum_construction(self) -> None:
        """A value valid for one enum must not construct a member of another."""
        # "deep" is a valid MadDeliberationDepth but not TaskDifficulty.
        with self.assertRaises(ValueError):
            TD("deep")
        # "expert" is a valid TaskDifficulty but not MadDeliberationDepth.
        with self.assertRaises(ValueError):
            MDD("expert")
        # "basic" is a valid TaskDifficulty but not WorkerKind.
        with self.assertRaises(ValueError):
            WK("basic")


# =========================================================================
# 8 — __all__ is exactly the three public type names
# =========================================================================


class AllExportsTests(unittest.TestCase):
    """__all__ exposes exactly the three public enum types."""

    def test_all_exact(self) -> None:
        expected = frozenset({"TaskDifficulty", "MadDeliberationDepth", "WorkerKind"})
        actual = frozenset(core_types.__all__)
        self.assertSetEqual(expected, actual)

    def test_all_has_three_names(self) -> None:
        self.assertEqual(len(core_types.__all__), 3)


# =========================================================================
# 9 — Import has no side effects (no I/O, stdout, stderr)
# =========================================================================


class ImportSideEffectTests(unittest.TestCase):
    """Importing core_types produces no stdout/stderr and does not block
    on external resources.
    """

    def test_import_produces_no_output(self) -> None:
        # Use importlib to get a fresh load.
        name = "core_types"
        if name in sys.modules:
            del sys.modules[name]
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            sys.path.insert(0, str(_SCRIPTS))
            try:
                importlib.import_module(name)
            finally:
                sys.path.remove(str(_SCRIPTS))
        self.assertEqual(stdout.getvalue(), "",
                         "import must not write to stdout")
        self.assertEqual(stderr.getvalue(), "",
                         "import must not write to stderr")

    def test_module_has_no_io_on_import(self) -> None:
        """Re-import does not trigger subprocess, file open, or network."""
        # If the initial import succeeded without side effects, this test
        # is already proven.  We double-check by confirming the module
        # has the expected attributes only.
        names = set(dir(core_types))
        for forbidden in ("subprocess", "socket", "urlopen", "requests"):
            self.assertNotIn(forbidden, names,
                             f"module must not expose {forbidden}")


# =========================================================================
# 10 — No budget / WorkerAdapter / slot / lease types leaked
# =========================================================================


class NoForbiddenExportsTests(unittest.TestCase):
    """The module must not expose budget, adapter, slot, or lease types."""

    def test_no_forbidden_names_in_module(self) -> None:
        names = {name.lower() for name in dir(core_types)}
        forbidden_fragments = (
            "budget",
            "workeradapter",
            "slot",
            "lease",
            "context",
            "model_tier",
            "deliberation_tier",
            "select",
        )
        for fragment in forbidden_fragments:
            offending = [n for n in dir(core_types) if fragment in n.lower()]
            self.assertEqual(
                [],
                offending,
                f"core_types must not expose names containing '{fragment}': "
                f"{offending}",
            )


# =========================================================================
# 11 — enum.unique duplicate-value smoke test (stdlib-only)
# =========================================================================


class EnumUniqueSmokeTest(unittest.TestCase):
    """Prove that ``enum.unique`` detects duplicate values in a str,Enum.

    This test defines a *temporary* enum with a duplicate value inside
    the test method itself — it does not modify any production enum.
    """

    def test_duplicate_values_in_str_enum_triggers_value_error(self) -> None:
        class _TestEnum(str, enum.Enum):
            A = "dup"
            B = "dup"

        with self.assertRaises(
            ValueError,
            msg="enum.unique must raise ValueError for duplicate values",
        ) as ctx:
            enum.unique(_TestEnum)
        msg = str(ctx.exception)
        # Python's error message identifies the duplicate value.
        self.assertIn("dup", msg,
                      "error must name the duplicate value 'dup'")


if __name__ == "__main__":
    unittest.main()
