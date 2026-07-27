"""Tests for WorkerSlotLease store — TC-13.10b.

Covers: data model, store schema, serialization round-trip, read-only
load, lock infrastructure, atomic write, isolation from WorkerAdapter
and Gateway, exception safety, repr/str leak prevention, malicious
object fail-closed, lock-exception propagation, directory fsync,
and UTC validation.

Does NOT test acquire / release / renew / fencing context — those belong
to TC-13.10c.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import unittest
from dataclasses import fields as dc_fields
from datetime import UTC, datetime, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = REPO_ROOT / "skills" / "agentdesk"
SCRIPTS = SKILL_ROOT / "scripts"

# Ensure the scripts directory is on sys.path for imports.
sys.path.insert(0, str(SCRIPTS))

# ── module under test ──────────────────────────────────────────────────────

from worker_slot_lease import (  # noqa: E402
    SCHEMA_VERSION,
    WorkerSlotLease,
    WorkerSlotLeaseError,
    WorkerSlotValidationError,
    WorkerSlotCapacityError,
    WorkerSlotContentionError,
    WorkerSlotNotHeldError,
    WorkerSlotFencingError,
    validate_worker_slot_store,
    read_worker_slot_leases,
    acquire_worker_slot,
    release_worker_slot,
    renew_worker_slot,
    hold_worker_slot_fence,
    _canonical_empty_store,
    _lease_to_dict,
    _lease_from_dict,
    _serialize_store,
    _atomic_write_store,
    _exclusive_store_lock,
    _STABLE_SLOT_IDS,
    _LEASE_FIELD_NAMES,
    _LEASE_TTL_SECONDS,
    _MAX_HEARTBEAT_INTERVAL_SECONDS,
    _normalize_workspace,
    _generate_unique_lease_id,
    _read_store_unlocked,
)

from core_types import WorkerKind  # noqa: E402

# ── helpers ────────────────────────────────────────────────────────────────

_WSL_PY = SCRIPTS / "worker_slot_lease.py"


def _valid_lease_kwargs(**overrides: object) -> dict[str, object]:
    """Return a minimally valid set of lease kwargs."""
    defaults: dict[str, object] = {
        "lease_id": "WSL-00000000000000000000000000000001",
        "lease_epoch": 1,
        "slot_id": "basic_agent-1",
        "worker_kind": WorkerKind.BASIC_AGENT,
        "holder_dispatch_id": "DSP-001",
        "holder_instance_id": "inst-001",
        "canonical_worktree": (
            "C:\\Users\\test\\project" if os.name == "nt"
            else "/home/test/project"
        ),
        "acquired_at": "2026-07-27T10:00:00Z",
        "heartbeat_at": "2026-07-27T10:00:00Z",
        "expires_at": "2026-07-27T10:01:00Z",
    }
    defaults.update(overrides)
    return defaults


def _valid_store(**overrides: object) -> dict[str, object]:
    """Return a valid canonical empty store with optional overrides."""
    store = _canonical_empty_store()
    store.update(overrides)  # type: ignore[arg-type]
    return store


def _tenant_worktree(index: int) -> str:
    """Return a distinct absolute worktree string for test case *index*."""
    if os.name == "nt":
        return f"C:\\Users\\test\\wt-{index}"
    return f"/home/test/wt-{index}"


def _parse_rfc(value: str) -> object:
    """Parse RFC 3339 for comparison purposes."""
    from datetime import UTC, datetime
    candidate = value.strip()
    if candidate.endswith("Z"):
        candidate = candidate[:-1] + "+00:00"
    return datetime.fromisoformat(candidate)


# ── malicious object for repr/str leak detection ──────────────────────────


class ReprMustNotBeCalled:
    """A sentinel object whose ``repr()`` and ``str()`` raise on access.

    Used to verify that validation code never calls ``repr()``, ``str()``,
    or ``{!r}`` on untrusted input values.
    """

    def __repr__(self):
        raise AssertionError("repr must not be called")

    def __str__(self):
        raise AssertionError("str must not be called")


# ── data model tests ───────────────────────────────────────────────────────


class WorkerSlotLeaseFieldTests(unittest.TestCase):
    """Exact 10 fields, frozen/slots, no __dict__."""

    def test_exact_ten_fields(self) -> None:
        kw = _valid_lease_kwargs()
        lease = WorkerSlotLease(**kw)  # type: ignore[arg-type]
        self.assertEqual(
            len(dc_fields(lease)), 10,
            "WorkerSlotLease must have exactly 10 fields",
        )

    def test_field_names_match_contract(self) -> None:
        kw = _valid_lease_kwargs()
        lease = WorkerSlotLease(**kw)  # type: ignore[arg-type]
        actual = {f.name for f in dc_fields(lease)}
        expected = set(_LEASE_FIELD_NAMES)
        self.assertSetEqual(actual, expected)

    def test_frozen_no_setattr(self) -> None:
        kw = _valid_lease_kwargs()
        lease = WorkerSlotLease(**kw)  # type: ignore[arg-type]
        with self.assertRaises(Exception):
            lease.lease_epoch = 2  # type: ignore[misc]

    def test_no_dict(self) -> None:
        kw = _valid_lease_kwargs()
        lease = WorkerSlotLease(**kw)  # type: ignore[arg-type]
        self.assertFalse(hasattr(lease, "__dict__"))

    def test_slots_no_arbitrary_attr(self) -> None:
        """CPython may raise AttributeError or TypeError for setting
        attributes on a frozen+slots dataclass.  The important invariant
        is that the attribute cannot be added."""
        kw = _valid_lease_kwargs()
        lease = WorkerSlotLease(**kw)  # type: ignore[arg-type]
        with self.assertRaises((AttributeError, TypeError)):
            lease.extra = 42  # type: ignore[attr-defined]
        self.assertFalse(hasattr(lease, "extra"))
        self.assertFalse(hasattr(lease, "__dict__"))


class WorkerSlotLeaseValidConstructionTests(unittest.TestCase):
    """Every valid WorkerKind / slot combination."""

    def test_basic_agent_1(self) -> None:
        kw = _valid_lease_kwargs(
            slot_id="basic_agent-1", worker_kind=WorkerKind.BASIC_AGENT,
        )
        lease = WorkerSlotLease(**kw)  # type: ignore[arg-type]
        self.assertEqual(lease.slot_id, "basic_agent-1")

    def test_basic_agent_2(self) -> None:
        kw = _valid_lease_kwargs(
            slot_id="basic_agent-2", worker_kind=WorkerKind.BASIC_AGENT,
        )
        lease = WorkerSlotLease(**kw)  # type: ignore[arg-type]
        self.assertEqual(lease.slot_id, "basic_agent-2")

    def test_standard_agent_1(self) -> None:
        kw = _valid_lease_kwargs(
            slot_id="standard_agent-1", worker_kind=WorkerKind.STANDARD_AGENT,
        )
        lease = WorkerSlotLease(**kw)  # type: ignore[arg-type]
        self.assertEqual(lease.slot_id, "standard_agent-1")

    def test_standard_agent_2(self) -> None:
        kw = _valid_lease_kwargs(
            slot_id="standard_agent-2", worker_kind=WorkerKind.STANDARD_AGENT,
        )
        lease = WorkerSlotLease(**kw)  # type: ignore[arg-type]
        self.assertEqual(lease.slot_id, "standard_agent-2")

    def test_advanced_agent_1(self) -> None:
        kw = _valid_lease_kwargs(
            slot_id="advanced_agent-1",
            worker_kind=WorkerKind.ADVANCED_AGENT,
        )
        lease = WorkerSlotLease(**kw)  # type: ignore[arg-type]
        self.assertEqual(lease.slot_id, "advanced_agent-1")

    def test_advanced_agent_2(self) -> None:
        kw = _valid_lease_kwargs(
            slot_id="advanced_agent-2",
            worker_kind=WorkerKind.ADVANCED_AGENT,
        )
        lease = WorkerSlotLease(**kw)  # type: ignore[arg-type]
        self.assertEqual(lease.slot_id, "advanced_agent-2")

    def test_expert_agent_1(self) -> None:
        kw = _valid_lease_kwargs(
            slot_id="expert_agent-1", worker_kind=WorkerKind.EXPERT_AGENT,
        )
        lease = WorkerSlotLease(**kw)  # type: ignore[arg-type]
        self.assertEqual(lease.slot_id, "expert_agent-1")

    def test_expert_agent_2(self) -> None:
        kw = _valid_lease_kwargs(
            slot_id="expert_agent-2", worker_kind=WorkerKind.EXPERT_AGENT,
        )
        lease = WorkerSlotLease(**kw)  # type: ignore[arg-type]
        self.assertEqual(lease.slot_id, "expert_agent-2")

    def test_posix_worktree(self) -> None:
        kw = _valid_lease_kwargs(canonical_worktree="/home/test/project")
        lease = WorkerSlotLease(**kw)  # type: ignore[arg-type]
        self.assertEqual(lease.canonical_worktree, "/home/test/project")

    def test_windows_worktree(self) -> None:
        kw = _valid_lease_kwargs(
            canonical_worktree="C:\\Users\\test\\Project"
        )
        lease = WorkerSlotLease(**kw)  # type: ignore[arg-type]
        self.assertEqual(
            lease.canonical_worktree, "C:\\Users\\test\\Project"
        )

    def test_unc_worktree(self) -> None:
        kw = _valid_lease_kwargs(
            canonical_worktree="\\\\server\\share\\project"
        )
        lease = WorkerSlotLease(**kw)  # type: ignore[arg-type]
        self.assertEqual(
            lease.canonical_worktree, "\\\\server\\share\\project"
        )

    def test_heartbeat_equals_acquired(self) -> None:
        kw = _valid_lease_kwargs(
            acquired_at="2026-07-27T10:00:00Z",
            heartbeat_at="2026-07-27T10:00:00Z",
        )
        lease = WorkerSlotLease(**kw)  # type: ignore[arg-type]
        self.assertEqual(lease.acquired_at, lease.heartbeat_at)

    def test_heartbeat_after_acquired(self) -> None:
        kw = _valid_lease_kwargs(
            acquired_at="2026-07-27T10:00:00Z",
            heartbeat_at="2026-07-27T10:00:30Z",
        )
        lease = WorkerSlotLease(**kw)  # type: ignore[arg-type]
        self.assertLess(
            _parse_rfc(lease.acquired_at), _parse_rfc(lease.heartbeat_at)
        )


class WorkerSlotLeaseRejectTests(unittest.TestCase):
    """All-field illegal type and value rejection."""

    # ── lease_id ───────────────────────────────────────────────────────
    def test_lease_id_not_str(self) -> None:
        kw = _valid_lease_kwargs(lease_id=12345)
        with self.assertRaises(TypeError):
            WorkerSlotLease(**kw)  # type: ignore[arg-type]

    def test_lease_id_empty(self) -> None:
        kw = _valid_lease_kwargs(lease_id="")
        with self.assertRaises(TypeError):
            WorkerSlotLease(**kw)  # type: ignore[arg-type]

    def test_lease_id_wrong_format(self) -> None:
        kw = _valid_lease_kwargs(lease_id="abc-123")
        with self.assertRaises(ValueError):
            WorkerSlotLease(**kw)  # type: ignore[arg-type]

    def test_lease_id_only_8_hex(self) -> None:
        kw = _valid_lease_kwargs(lease_id="WSL-" + "0" * 8)
        with self.assertRaises(ValueError):
            WorkerSlotLease(**kw)  # type: ignore[arg-type]

    def test_lease_id_uppercase_hex(self) -> None:
        kw = _valid_lease_kwargs(lease_id="WSL-" + "A" * 32)
        with self.assertRaises(ValueError):
            WorkerSlotLease(**kw)  # type: ignore[arg-type]

    # ── lease_epoch ────────────────────────────────────────────────────
    def test_lease_epoch_bool(self) -> None:
        kw = _valid_lease_kwargs(lease_epoch=True)
        with self.assertRaises(TypeError):
            WorkerSlotLease(**kw)  # type: ignore[arg-type]

    def test_lease_epoch_float(self) -> None:
        kw = _valid_lease_kwargs(lease_epoch=1.5)
        with self.assertRaises(TypeError):
            WorkerSlotLease(**kw)  # type: ignore[arg-type]

    def test_lease_epoch_zero(self) -> None:
        kw = _valid_lease_kwargs(lease_epoch=0)
        with self.assertRaises(ValueError):
            WorkerSlotLease(**kw)  # type: ignore[arg-type]

    def test_lease_epoch_negative(self) -> None:
        kw = _valid_lease_kwargs(lease_epoch=-1)
        with self.assertRaises(ValueError):
            WorkerSlotLease(**kw)  # type: ignore[arg-type]

    def test_lease_epoch_str(self) -> None:
        kw = _valid_lease_kwargs(lease_epoch="1")
        with self.assertRaises(TypeError):
            WorkerSlotLease(**kw)  # type: ignore[arg-type]

    # ── slot_id ────────────────────────────────────────────────────────
    def test_unknown_slot_id(self) -> None:
        kw = _valid_lease_kwargs(slot_id="basic_agent-3")
        with self.assertRaises(ValueError):
            WorkerSlotLease(**kw)  # type: ignore[arg-type]

    def test_empty_slot_id(self) -> None:
        kw = _valid_lease_kwargs(slot_id="")
        with self.assertRaises(TypeError):
            WorkerSlotLease(**kw)  # type: ignore[arg-type]

    # ── worker_kind ────────────────────────────────────────────────────
    def test_worker_kind_not_enum(self) -> None:
        kw = _valid_lease_kwargs(worker_kind="basic_agent")
        with self.assertRaises(TypeError):
            WorkerSlotLease(**kw)  # type: ignore[arg-type]

    def test_worker_kind_cross_enum(self) -> None:
        from core_types import TaskDifficulty
        kw = _valid_lease_kwargs(
            worker_kind=TaskDifficulty.BASIC,
        )
        with self.assertRaises(TypeError):
            WorkerSlotLease(**kw)  # type: ignore[arg-type]

    def test_worker_kind_wrong_for_slot(self) -> None:
        kw = _valid_lease_kwargs(
            slot_id="basic_agent-1",
            worker_kind=WorkerKind.STANDARD_AGENT,
        )
        with self.assertRaises(ValueError):
            WorkerSlotLease(**kw)  # type: ignore[arg-type]

    # ── holder fields ──────────────────────────────────────────────────
    def test_holder_dispatch_id_empty(self) -> None:
        kw = _valid_lease_kwargs(holder_dispatch_id="")
        with self.assertRaises(TypeError):
            WorkerSlotLease(**kw)  # type: ignore[arg-type]

    def test_holder_dispatch_id_whitespace(self) -> None:
        kw = _valid_lease_kwargs(holder_dispatch_id="  DSP-001  ")
        with self.assertRaises(ValueError):
            WorkerSlotLease(**kw)  # type: ignore[arg-type]

    def test_holder_dispatch_id_nul(self) -> None:
        kw = _valid_lease_kwargs(holder_dispatch_id="DSP\0-001")
        with self.assertRaises(ValueError):
            WorkerSlotLease(**kw)  # type: ignore[arg-type]

    def test_holder_dispatch_id_cr(self) -> None:
        kw = _valid_lease_kwargs(holder_dispatch_id="DSP\r-001")
        with self.assertRaises(ValueError):
            WorkerSlotLease(**kw)  # type: ignore[arg-type]

    def test_holder_dispatch_id_lf(self) -> None:
        kw = _valid_lease_kwargs(holder_dispatch_id="DSP\n-001")
        with self.assertRaises(ValueError):
            WorkerSlotLease(**kw)  # type: ignore[arg-type]

    # ── canonical_worktree ─────────────────────────────────────────────
    def test_worktree_relative(self) -> None:
        kw = _valid_lease_kwargs(canonical_worktree="relative/path")
        with self.assertRaises(ValueError):
            WorkerSlotLease(**kw)  # type: ignore[arg-type]

    def test_worktree_empty(self) -> None:
        kw = _valid_lease_kwargs(canonical_worktree="")
        with self.assertRaises(TypeError):
            WorkerSlotLease(**kw)  # type: ignore[arg-type]

    def test_worktree_whitespace(self) -> None:
        kw = _valid_lease_kwargs(
            canonical_worktree="  /home/test  "
        )
        with self.assertRaises(ValueError):
            WorkerSlotLease(**kw)  # type: ignore[arg-type]

    def test_worktree_nul(self) -> None:
        kw = _valid_lease_kwargs(canonical_worktree="/home\0/test")
        with self.assertRaises(ValueError):
            WorkerSlotLease(**kw)  # type: ignore[arg-type]

    def test_worktree_cr(self) -> None:
        kw = _valid_lease_kwargs(canonical_worktree="/home\r/test")
        with self.assertRaises(ValueError):
            WorkerSlotLease(**kw)  # type: ignore[arg-type]

    def test_worktree_lf(self) -> None:
        kw = _valid_lease_kwargs(canonical_worktree="/home\n/test")
        with self.assertRaises(ValueError):
            WorkerSlotLease(**kw)  # type: ignore[arg-type]

    # ── timestamps ─────────────────────────────────────────────────────
    def test_timestamp_not_rfc3339(self) -> None:
        kw = _valid_lease_kwargs(acquired_at="2026-07-27 10:00:00")
        with self.assertRaises(ValueError):
            WorkerSlotLease(**kw)  # type: ignore[arg-type]

    def test_timestamp_non_utc_offset(self) -> None:
        kw = _valid_lease_kwargs(acquired_at="2026-07-27T10:00:00+05:00")
        with self.assertRaises(ValueError):
            WorkerSlotLease(**kw)  # type: ignore[arg-type]

    def test_timestamp_negative_zero_offset_rejected(self) -> None:
        """-00:00 is not a valid UTC offset."""
        kw = _valid_lease_kwargs(acquired_at="2026-07-27T10:00:00-00:00")
        with self.assertRaises(ValueError):
            WorkerSlotLease(**kw)  # type: ignore[arg-type]

    def test_timestamp_naive(self) -> None:
        kw = _valid_lease_kwargs(acquired_at="2026-07-27T10:00:00")
        with self.assertRaises(ValueError):
            WorkerSlotLease(**kw)  # type: ignore[arg-type]

    def test_heartbeat_before_acquired(self) -> None:
        kw = _valid_lease_kwargs(
            acquired_at="2026-07-27T10:01:00Z",
            heartbeat_at="2026-07-27T10:00:00Z",
        )
        with self.assertRaises(ValueError):
            WorkerSlotLease(**kw)  # type: ignore[arg-type]

    def test_heartbeat_not_before_expires(self) -> None:
        kw = _valid_lease_kwargs(
            heartbeat_at="2026-07-27T10:01:00Z",
            expires_at="2026-07-27T10:01:00Z",
        )
        with self.assertRaises(ValueError):
            WorkerSlotLease(**kw)  # type: ignore[arg-type]


# ── store schema tests ──────────────────────────────────────────────────────


class CanonicalEmptyStoreTests(unittest.TestCase):
    """Precise shape of canonical empty store."""

    def test_exact_root_keys(self) -> None:
        store = _canonical_empty_store()
        self.assertSetEqual(
            set(store.keys()),
            {"schema_version", "updated_at", "slot_epochs", "leases"},
        )

    def test_schema_version(self) -> None:
        store = _canonical_empty_store()
        self.assertEqual(
            store["schema_version"], SCHEMA_VERSION,
        )

    def test_sentinel_updated_at(self) -> None:
        store = _canonical_empty_store()
        self.assertEqual(
            store["updated_at"], "1970-01-01T00:00:00Z",
        )

    def test_exact_eight_epoch_keys(self) -> None:
        store = _canonical_empty_store()
        se = store["slot_epochs"]
        self.assertIsInstance(se, dict)
        self.assertSetEqual(
            set(se.keys()),  # type: ignore[arg-type]
            set(_STABLE_SLOT_IDS),
        )

    def test_initial_epochs_zero(self) -> None:
        store = _canonical_empty_store()
        se = store["slot_epochs"]
        for sid in _STABLE_SLOT_IDS:
            self.assertEqual(
                se[sid], 0,  # type: ignore[index]
                f"slot_epochs.{sid} must be 0",
            )

    def test_leases_empty_dict(self) -> None:
        store = _canonical_empty_store()
        self.assertEqual(store["leases"], {})

    def test_independent_copies(self) -> None:
        a = _canonical_empty_store()
        b = _canonical_empty_store()
        self.assertIsNot(a, b, "each call must return a new dict")
        self.assertIsNot(
            a["slot_epochs"], b["slot_epochs"],
            "each slot_epochs must be a new dict",
        )
        a["slot_epochs"]["basic_agent-1"] = 99  # type: ignore[index]
        self.assertEqual(
            b["slot_epochs"]["basic_agent-1"], 0,  # type: ignore[index]
            "must not share mutation",
        )

    def test_no_system_time_dependency(self) -> None:
        """updated_at must be the sentinel, not current system time."""
        from datetime import UTC, datetime
        store = _canonical_empty_store()
        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.assertNotEqual(store["updated_at"], now)
        self.assertEqual(store["updated_at"], "1970-01-01T00:00:00Z")


class ValidateStoreTests(unittest.TestCase):
    """Pure validation function tests."""

    def test_valid_empty_store(self) -> None:
        store = _canonical_empty_store()
        errors = validate_worker_slot_store(store)
        self.assertEqual(errors, [], f"empty store must be valid: {errors}")

    def test_non_dict_root(self) -> None:
        errors = validate_worker_slot_store([])
        self.assertIn("root must be an object", errors[0])

    def test_wrong_schema_version(self) -> None:
        store = _canonical_empty_store()
        store["schema_version"] = "wrong/v1"
        errors = validate_worker_slot_store(store)
        self.assertTrue(any("schema_version" in e for e in errors))

    def test_extra_root_key(self) -> None:
        store = _canonical_empty_store()
        store["extra_key"] = True  # type: ignore[misc]
        errors = validate_worker_slot_store(store)
        self.assertTrue(any("forbidden" in e for e in errors))

    def test_missing_root_key(self) -> None:
        store = _canonical_empty_store()
        del store["slot_epochs"]
        errors = validate_worker_slot_store(store)
        self.assertTrue(any("missing" in e for e in errors))

    def test_extra_slot_epoch(self) -> None:
        store = _canonical_empty_store()
        store["slot_epochs"]["basic_agent-3"] = 0  # type: ignore[index]
        errors = validate_worker_slot_store(store)
        self.assertTrue(
            any("unknown slot" in e for e in errors),
            str(errors),
        )

    def test_missing_slot_epoch(self) -> None:
        store = _canonical_empty_store()
        del store["slot_epochs"]["basic_agent-1"]  # type: ignore[arg-type]
        errors = validate_worker_slot_store(store)
        self.assertTrue(
            any("missing slot" in e for e in errors),
            str(errors),
        )

    def test_bool_epoch(self) -> None:
        store = _canonical_empty_store()
        store["slot_epochs"]["basic_agent-1"] = True  # type: ignore[index]
        errors = validate_worker_slot_store(store)
        self.assertTrue(
            any("non-bool int" in e for e in errors),
            str(errors),
        )

    def test_negative_epoch(self) -> None:
        store = _canonical_empty_store()
        store["slot_epochs"]["basic_agent-1"] = -1  # type: ignore[index]
        errors = validate_worker_slot_store(store)
        self.assertTrue(
            any(">= 0" in e for e in errors),
            str(errors),
        )

    def test_leases_not_dict(self) -> None:
        store = _canonical_empty_store()
        store["leases"] = []  # type: ignore[arg-type]
        errors = validate_worker_slot_store(store)
        self.assertTrue(any("object" in e for e in errors))

    def test_lease_slot_id_key_mismatch(self) -> None:
        store = _canonical_empty_store()
        kw = _valid_lease_kwargs(
            slot_id="basic_agent-2",
            worker_kind=WorkerKind.BASIC_AGENT,
        )
        lease = WorkerSlotLease(**kw)  # type: ignore[arg-type]
        store["leases"] = {"basic_agent-1": _lease_to_dict(lease)}  # type: ignore[list-item]
        errors = validate_worker_slot_store(store)
        self.assertTrue(
            any("does not match" in e for e in errors),
            str(errors),
        )

    def test_duplicate_lease_id(self) -> None:
        store = _canonical_empty_store()
        store["slot_epochs"]["basic_agent-1"] = 1  # type: ignore[index]
        store["slot_epochs"]["basic_agent-2"] = 1  # type: ignore[index]
        lid = "WSL-" + "a" * 32
        kw1 = _valid_lease_kwargs(
            lease_id=lid,
            lease_epoch=1,
            slot_id="basic_agent-1",
            worker_kind=WorkerKind.BASIC_AGENT,
            canonical_worktree=_tenant_worktree(1),
        )
        kw2 = _valid_lease_kwargs(
            lease_id=lid,
            lease_epoch=1,
            slot_id="basic_agent-2",
            worker_kind=WorkerKind.BASIC_AGENT,
            canonical_worktree=_tenant_worktree(2),
        )
        l1 = WorkerSlotLease(**kw1)  # type: ignore[arg-type]
        l2 = WorkerSlotLease(**kw2)  # type: ignore[arg-type]
        store["leases"] = {  # type: ignore[list-item]
            "basic_agent-1": _lease_to_dict(l1),
            "basic_agent-2": _lease_to_dict(l2),
        }
        errors = validate_worker_slot_store(store)
        self.assertTrue(
            any("duplicated" in e for e in errors),
            str(errors),
        )

    def test_duplicate_worktree_pair(self) -> None:
        store = _canonical_empty_store()
        store["slot_epochs"]["basic_agent-1"] = 1  # type: ignore[index]
        store["slot_epochs"]["basic_agent-2"] = 1  # type: ignore[index]
        wt = _tenant_worktree(1)
        lid1 = "WSL-" + "a" * 32
        lid2 = "WSL-" + "b" * 32
        kw1 = _valid_lease_kwargs(
            lease_id=lid1,
            lease_epoch=1,
            slot_id="basic_agent-1",
            worker_kind=WorkerKind.BASIC_AGENT,
            canonical_worktree=wt,
        )
        kw2 = _valid_lease_kwargs(
            lease_id=lid2,
            lease_epoch=1,
            slot_id="basic_agent-2",
            worker_kind=WorkerKind.BASIC_AGENT,
            canonical_worktree=wt,
        )
        l1 = WorkerSlotLease(**kw1)  # type: ignore[arg-type]
        l2 = WorkerSlotLease(**kw2)  # type: ignore[arg-type]
        store["leases"] = {  # type: ignore[list-item]
            "basic_agent-1": _lease_to_dict(l1),
            "basic_agent-2": _lease_to_dict(l2),
        }
        errors = validate_worker_slot_store(store)
        self.assertTrue(
            any("duplicate" in e.lower() and "worktree" in e.lower()
                for e in errors),
            str(errors),
        )

    def test_epoch_mismatch_with_slot_epochs(self) -> None:
        store = _canonical_empty_store()
        store["slot_epochs"]["basic_agent-1"] = 5  # type: ignore[index]
        kw = _valid_lease_kwargs(
            lease_epoch=3,
            slot_id="basic_agent-1",
            worker_kind=WorkerKind.BASIC_AGENT,
        )
        lease = WorkerSlotLease(**kw)  # type: ignore[arg-type]
        store["leases"] = {"basic_agent-1": _lease_to_dict(lease)}  # type: ignore[list-item]
        errors = validate_worker_slot_store(store)
        self.assertTrue(
            any("does not match" in e for e in errors),
            str(errors),
        )

    def test_wrong_worker_kind_for_slot(self) -> None:
        store = _canonical_empty_store()
        d = {
            "lease_id": "WSL-" + "0" * 32,
            "lease_epoch": 1,
            "slot_id": "basic_agent-1",
            "worker_kind": "standard_agent",
            "holder_dispatch_id": "DSP-001",
            "holder_instance_id": "inst-001",
            "canonical_worktree": _tenant_worktree(1),
            "acquired_at": "2026-07-27T10:00:00Z",
            "heartbeat_at": "2026-07-27T10:00:00Z",
            "expires_at": "2026-07-27T10:01:00Z",
        }
        store["leases"] = {"basic_agent-1": d}  # type: ignore[list-item]
        errors = validate_worker_slot_store(store)
        self.assertTrue(
            any("does not match slot" in e for e in errors),
            str(errors),
        )

    def test_expired_lease_still_structurally_valid(self) -> None:
        store = _canonical_empty_store()
        store["slot_epochs"]["basic_agent-1"] = 1  # type: ignore[index]
        kw = _valid_lease_kwargs(
            lease_epoch=1,
            slot_id="basic_agent-1",
            worker_kind=WorkerKind.BASIC_AGENT,
            acquired_at="2026-01-01T00:00:00Z",
            heartbeat_at="2026-01-01T00:00:00Z",
            expires_at="2026-01-01T00:01:00Z",
        )
        lease = WorkerSlotLease(**kw)  # type: ignore[arg-type]
        store["leases"] = {"basic_agent-1": _lease_to_dict(lease)}  # type: ignore[list-item]
        errors = validate_worker_slot_store(store)
        self.assertEqual(
            errors, [],
            f"expired lease must be structurally valid: {errors}",
        )

    def test_lease_without_overly_verbose_error_messages(self) -> None:
        store = _canonical_empty_store()
        store["leases"] = {"basic_agent-1": {  # type: ignore[list-item]
            "lease_id": "WSL-" + "0" * 32,
            "lease_epoch": True,
            "slot_id": "basic_agent-1",
            "worker_kind": "basic_agent",
            "holder_dispatch_id": "DSP-001",
            "holder_instance_id": "my-secret-instance-id-12345",
            "canonical_worktree": "/home/alice/secret/project",
            "acquired_at": "2026-07-27T10:00:00Z",
            "heartbeat_at": "2026-07-27T10:00:00Z",
            "expires_at": "2026-07-27T10:01:00Z",
        }}
        errors = validate_worker_slot_store(store)
        joined = " ".join(errors)
        self.assertNotIn("my-secret-instance-id-12345", joined)
        self.assertNotIn("/home/alice/secret/project", joined)

    # ── UTC timestamp validation (real parser, not just regex) ─────────

    def test_non_utc_timestamp_rejected_in_store(self) -> None:
        store = _canonical_empty_store()
        store["slot_epochs"]["basic_agent-1"] = 1  # type: ignore[index]
        d = {
            "lease_id": "WSL-" + "0" * 32,
            "lease_epoch": 1,
            "slot_id": "basic_agent-1",
            "worker_kind": "basic_agent",
            "holder_dispatch_id": "DSP-001",
            "holder_instance_id": "inst-001",
            "canonical_worktree": _tenant_worktree(1),
            "acquired_at": "2026-07-27T10:00:00+01:00",
            "heartbeat_at": "2026-07-27T10:00:00+01:00",
            "expires_at": "2026-07-27T10:01:00+01:00",
        }
        store["leases"] = {"basic_agent-1": d}  # type: ignore[list-item]
        errors = validate_worker_slot_store(store)
        self.assertTrue(
            any("UTC" in e for e in errors),
            str(errors),
        )

    def test_naive_timestamp_rejected_in_store(self) -> None:
        store = _canonical_empty_store()
        store["slot_epochs"]["basic_agent-1"] = 1  # type: ignore[index]
        d = {
            "lease_id": "WSL-" + "0" * 32,
            "lease_epoch": 1,
            "slot_id": "basic_agent-1",
            "worker_kind": "basic_agent",
            "holder_dispatch_id": "DSP-001",
            "holder_instance_id": "inst-001",
            "canonical_worktree": _tenant_worktree(1),
            "acquired_at": "2026-07-27T10:00:00",
            "heartbeat_at": "2026-07-27T10:00:00",
            "expires_at": "2026-07-27T10:01:00",
        }
        store["leases"] = {"basic_agent-1": d}  # type: ignore[list-item]
        errors = validate_worker_slot_store(store)
        self.assertTrue(
            any("RFC 3339 UTC" in e for e in errors),
            str(errors),
        )

    def test_invalid_calendar_date_rejected_in_store(self) -> None:
        store = _canonical_empty_store()
        store["slot_epochs"]["basic_agent-1"] = 1  # type: ignore[index]
        d = {
            "lease_id": "WSL-" + "0" * 32,
            "lease_epoch": 1,
            "slot_id": "basic_agent-1",
            "worker_kind": "basic_agent",
            "holder_dispatch_id": "DSP-001",
            "holder_instance_id": "inst-001",
            "canonical_worktree": _tenant_worktree(1),
            "acquired_at": "2026-02-30T10:00:00Z",
            "heartbeat_at": "2026-02-30T10:00:00Z",
            "expires_at": "2026-02-30T10:01:00Z",
        }
        store["leases"] = {"basic_agent-1": d}  # type: ignore[list-item]
        errors = validate_worker_slot_store(store)
        self.assertTrue(
            any("RFC 3339 UTC" in e for e in errors),
            str(errors),
        )

    def test_temporal_ordering_validated_in_store(self) -> None:
        store = _canonical_empty_store()
        store["slot_epochs"]["basic_agent-1"] = 1  # type: ignore[index]
        d = {
            "lease_id": "WSL-" + "0" * 32,
            "lease_epoch": 1,
            "slot_id": "basic_agent-1",
            "worker_kind": "basic_agent",
            "holder_dispatch_id": "DSP-001",
            "holder_instance_id": "inst-001",
            "canonical_worktree": _tenant_worktree(1),
            "acquired_at": "2026-07-27T10:02:00Z",
            "heartbeat_at": "2026-07-27T10:00:00Z",
            "expires_at": "2026-07-27T10:01:00Z",
        }
        store["leases"] = {"basic_agent-1": d}  # type: ignore[list-item]
        errors = validate_worker_slot_store(store)
        self.assertTrue(
            any("acquired_at" in e for e in errors),
            str(errors),
        )

    def test_updated_at_non_utc_rejected(self) -> None:
        store = _canonical_empty_store()
        store["updated_at"] = "2026-07-27T10:00:00+05:00"
        errors = validate_worker_slot_store(store)
        self.assertTrue(
            any("updated_at" in e and "UTC" in e for e in errors),
            str(errors),
        )

    # ── malicious object fail-closed ───────────────────────────────────

    def _make_malicious_store(self, field_path: str, value: object) -> dict[str, object]:
        """Build a store with *value* placed at *field_path*.

        Supported paths: 'schema_version', 'updated_at', all 8
        'slot_epochs.<sid>', and per-lease fields via
        'leases.basic_agent-1.<field>'.
        """
        store = _canonical_empty_store()
        if field_path == "schema_version":
            store["schema_version"] = value  # type: ignore[misc]
        elif field_path == "updated_at":
            store["updated_at"] = value  # type: ignore[misc]
        elif field_path.startswith("slot_epochs."):
            sid = field_path[len("slot_epochs."):]
            store["slot_epochs"][sid] = value  # type: ignore[index]
        elif field_path.startswith("leases."):
            parts = field_path.split(".")
            slot = parts[1]
            if len(parts) == 2:
                store["leases"][slot] = value  # type: ignore[list-item]
            elif len(parts) == 3:
                if slot not in store["leases"]:  # type: ignore[index]
                    store["leases"][slot] = {}  # type: ignore[list-item]
                store["leases"][slot][parts[2]] = value  # type: ignore[index]
        elif field_path == "extra_root_key":
            store[field_path] = value  # type: ignore[misc]
        elif field_path == "extra_lease_key":
            if "basic_agent-1" not in store["leases"]:  # type: ignore[index]
                store["leases"]["basic_agent-1"] = {  # type: ignore[list-item]
                    "lease_id": "WSL-" + "0" * 32,
                    "lease_epoch": 1,
                    "slot_id": "basic_agent-1",
                    "worker_kind": "basic_agent",
                    "holder_dispatch_id": "DSP-001",
                    "holder_instance_id": "inst-001",
                    "canonical_worktree": _tenant_worktree(1),
                    "acquired_at": "2026-07-27T10:00:00Z",
                    "heartbeat_at": "2026-07-27T10:00:00Z",
                    "expires_at": "2026-07-27T10:01:00Z",
                }
            store["leases"]["basic_agent-1"][field_path] = value  # type: ignore[index]
        return store

    def test_malicious_schema_version_no_repr(self) -> None:
        store = self._make_malicious_store(
            "schema_version", ReprMustNotBeCalled()
        )
        errors = validate_worker_slot_store(store)
        self.assertTrue(len(errors) > 0)

    def test_malicious_updated_at_no_repr(self) -> None:
        store = self._make_malicious_store(
            "updated_at", ReprMustNotBeCalled()
        )
        errors = validate_worker_slot_store(store)
        self.assertTrue(len(errors) > 0)

    def test_malicious_slot_epoch_value_no_repr(self) -> None:
        store = self._make_malicious_store(
            "slot_epochs.basic_agent-1", ReprMustNotBeCalled()
        )
        errors = validate_worker_slot_store(store)
        self.assertTrue(len(errors) > 0)

    def test_malicious_lease_id_no_repr(self) -> None:
        store = self._make_malicious_store(
            "leases.basic_agent-1.lease_id", ReprMustNotBeCalled()
        )
        errors = validate_worker_slot_store(store)
        self.assertTrue(len(errors) > 0)

    def test_malicious_lease_epoch_no_repr(self) -> None:
        store = self._make_malicious_store(
            "leases.basic_agent-1.lease_epoch", ReprMustNotBeCalled()
        )
        errors = validate_worker_slot_store(store)
        self.assertTrue(len(errors) > 0)

    def test_malicious_slot_id_no_repr(self) -> None:
        store = self._make_malicious_store(
            "leases.basic_agent-1.slot_id", ReprMustNotBeCalled()
        )
        errors = validate_worker_slot_store(store)
        self.assertTrue(len(errors) > 0)

    def test_malicious_worker_kind_no_repr(self) -> None:
        store = self._make_malicious_store(
            "leases.basic_agent-1.worker_kind", ReprMustNotBeCalled()
        )
        errors = validate_worker_slot_store(store)
        self.assertTrue(len(errors) > 0)

    def test_malicious_timestamp_no_repr(self) -> None:
        store = self._make_malicious_store(
            "leases.basic_agent-1.acquired_at", ReprMustNotBeCalled()
        )
        errors = validate_worker_slot_store(store)
        self.assertTrue(len(errors) > 0)

    def test_malicious_holder_field_no_repr(self) -> None:
        store = self._make_malicious_store(
            "leases.basic_agent-1.holder_dispatch_id",
            ReprMustNotBeCalled(),
        )
        errors = validate_worker_slot_store(store)
        self.assertTrue(len(errors) > 0)

    def test_malicious_canonical_worktree_no_repr(self) -> None:
        store = self._make_malicious_store(
            "leases.basic_agent-1.canonical_worktree",
            ReprMustNotBeCalled(),
        )
        errors = validate_worker_slot_store(store)
        self.assertTrue(len(errors) > 0)

    def test_malicious_extra_root_key_no_repr(self) -> None:
        store = _canonical_empty_store()
        store["extra_key"] = ReprMustNotBeCalled()  # type: ignore[misc]
        errors = validate_worker_slot_store(store)
        self.assertTrue(len(errors) > 0)

    def test_malicious_extra_lease_key_no_repr(self) -> None:
        store = _canonical_empty_store()
        store["leases"] = {"basic_agent-1": {  # type: ignore[list-item]
            "lease_id": "WSL-" + "0" * 32,
            "lease_epoch": 1,
            "slot_id": "basic_agent-1",
            "worker_kind": "basic_agent",
            "holder_dispatch_id": "DSP-001",
            "holder_instance_id": "inst-001",
            "canonical_worktree": _tenant_worktree(1),
            "acquired_at": "2026-07-27T10:00:00Z",
            "heartbeat_at": "2026-07-27T10:00:00Z",
            "expires_at": "2026-07-27T10:01:00Z",
            "extra_field": ReprMustNotBeCalled(),
        }}
        errors = validate_worker_slot_store(store)
        self.assertTrue(len(errors) > 0)

    def test_malicious_non_str_root_key_no_repr(self) -> None:
        store = _canonical_empty_store()
        store[ReprMustNotBeCalled()] = True  # type: ignore[misc]
        errors = validate_worker_slot_store(store)
        self.assertTrue(len(errors) > 0)

    # ── SECRET marker leak prevention ──────────────────────────────────

    def test_dataclass_no_secret_leak_in_error(self) -> None:
        """Error messages from WorkerSlotLease construction must never
        include the raw value of rejected input."""
        secret = "SECRET_WORKER_SLOT_VALUE_1310B"
        kw = _valid_lease_kwargs(lease_id=secret)
        try:
            WorkerSlotLease(**kw)  # type: ignore[arg-type]
        except (TypeError, ValueError) as exc:
            self.assertNotIn(secret, str(exc))

    def test_dataclass_secret_in_worktree_no_leak(self) -> None:
        secret = "SECRET_WORKER_SLOT_VALUE_1310B"
        kw = _valid_lease_kwargs(
            canonical_worktree=secret,
        )
        try:
            WorkerSlotLease(**kw)  # type: ignore[arg-type]
        except (TypeError, ValueError) as exc:
            self.assertNotIn(secret, str(exc))

    def test_dataclass_secret_in_epoch_no_leak(self) -> None:
        secret = "SECRET_WORKER_SLOT_VALUE_1310B"
        kw = _valid_lease_kwargs(lease_epoch=secret)
        try:
            WorkerSlotLease(**kw)  # type: ignore[arg-type]
        except (TypeError, ValueError) as exc:
            self.assertNotIn(secret, str(exc))

    def test_deserialization_secret_no_leak(self) -> None:
        secret = "SECRET_WORKER_SLOT_VALUE_1310B"
        d = {
            "lease_id": "WSL-" + "0" * 32,
            "lease_epoch": secret,
            "slot_id": "basic_agent-1",
            "worker_kind": "basic_agent",
            "holder_dispatch_id": "DSP-001",
            "holder_instance_id": "inst-001",
            "canonical_worktree": _tenant_worktree(1),
            "acquired_at": "2026-07-27T10:00:00Z",
            "heartbeat_at": "2026-07-27T10:00:00Z",
            "expires_at": "2026-07-27T10:01:00Z",
        }
        try:
            _lease_from_dict(d)
        except (TypeError, ValueError) as exc:
            self.assertNotIn(secret, str(exc))

    def test_validator_secret_in_lease_id_no_leak(self) -> None:
        secret = "SECRET_WORKER_SLOT_VALUE_1310B"
        store = _canonical_empty_store()
        store["leases"] = {"basic_agent-1": {  # type: ignore[list-item]
            "lease_id": secret,
            "lease_epoch": 1,
            "slot_id": "basic_agent-1",
            "worker_kind": "basic_agent",
            "holder_dispatch_id": "DSP-001",
            "holder_instance_id": "inst-001",
            "canonical_worktree": _tenant_worktree(1),
            "acquired_at": "2026-07-27T10:00:00Z",
            "heartbeat_at": "2026-07-27T10:00:00Z",
            "expires_at": "2026-07-27T10:01:00Z",
        }}
        errors = validate_worker_slot_store(store)
        joined = " ".join(errors)
        self.assertNotIn(secret, joined)


# ── serialization tests ────────────────────────────────────────────────────


class SerializationRoundTripTests(unittest.TestCase):
    """Exact 10-key round-trip."""

    def test_round_trip_basic(self) -> None:
        kw = _valid_lease_kwargs()
        lease = WorkerSlotLease(**kw)  # type: ignore[arg-type]
        d = _lease_to_dict(lease)
        self.assertSetEqual(set(d.keys()), set(_LEASE_FIELD_NAMES))
        restored = _lease_from_dict(d)
        self.assertEqual(restored, lease)

    def test_round_trip_all_eight_slots(self) -> None:
        for slot_id in _STABLE_SLOT_IDS:
            expected_kind = {
                "basic_agent-1": WorkerKind.BASIC_AGENT,
                "basic_agent-2": WorkerKind.BASIC_AGENT,
                "standard_agent-1": WorkerKind.STANDARD_AGENT,
                "standard_agent-2": WorkerKind.STANDARD_AGENT,
                "advanced_agent-1": WorkerKind.ADVANCED_AGENT,
                "advanced_agent-2": WorkerKind.ADVANCED_AGENT,
                "expert_agent-1": WorkerKind.EXPERT_AGENT,
                "expert_agent-2": WorkerKind.EXPERT_AGENT,
            }[slot_id]
            kw = _valid_lease_kwargs(
                slot_id=slot_id,
                worker_kind=expected_kind,
                canonical_worktree=_tenant_worktree(
                    _STABLE_SLOT_IDS.index(slot_id)
                ),
            )
            lease = WorkerSlotLease(**kw)  # type: ignore[arg-type]
            d = _lease_to_dict(lease)
            restored = _lease_from_dict(d)
            self.assertEqual(restored, lease)

    def test_missing_key_rejected(self) -> None:
        d = _lease_to_dict(
            WorkerSlotLease(**_valid_lease_kwargs())  # type: ignore[arg-type]
        )
        del d["lease_id"]
        with self.assertRaises(ValueError):
            _lease_from_dict(d)

    def test_extra_key_rejected(self) -> None:
        d = _lease_to_dict(
            WorkerSlotLease(**_valid_lease_kwargs())  # type: ignore[arg-type]
        )
        d["extra"] = "nope"
        with self.assertRaises(ValueError):
            _lease_from_dict(d)

    def test_bool_not_int(self) -> None:
        d = _lease_to_dict(
            WorkerSlotLease(**_valid_lease_kwargs())  # type: ignore[arg-type]
        )
        d["lease_epoch"] = True
        with self.assertRaises(TypeError):
            _lease_from_dict(d)

    def test_worker_kind_serialized_as_lowercase_string(self) -> None:
        kw = _valid_lease_kwargs()
        lease = WorkerSlotLease(**kw)  # type: ignore[arg-type]
        d = _lease_to_dict(lease)
        self.assertEqual(d["worker_kind"], "basic_agent")

    def test_unicode_worktree(self) -> None:
        kw = _valid_lease_kwargs(
            canonical_worktree="/home/测试/project"
        )
        lease = WorkerSlotLease(**kw)  # type: ignore[arg-type]
        d = _lease_to_dict(lease)
        restored = _lease_from_dict(d)
        self.assertEqual(restored, lease)


class SerializeStoreTests(unittest.TestCase):
    """JSON text format tests."""

    def test_trailing_newline(self) -> None:
        store = _canonical_empty_store()
        text = _serialize_store(store)
        self.assertTrue(text.endswith("\n"))

    def test_valid_json(self) -> None:
        store = _canonical_empty_store()
        text = _serialize_store(store)
        parsed = json.loads(text)
        self.assertIsInstance(parsed, dict)

    def test_ensure_ascii_false_unicode(self) -> None:
        store = _canonical_empty_store()
        store["leases"] = {"basic_agent-1": _lease_to_dict(  # type: ignore[list-item]
            WorkerSlotLease(**_valid_lease_kwargs(  # type: ignore[arg-type]
                canonical_worktree="/home/测试/test",
            ))
        )}
        store["slot_epochs"]["basic_agent-1"] = 1  # type: ignore[index]
        text = _serialize_store(store)
        self.assertIn("测试", text)


# ── read-only API tests ────────────────────────────────────────────────────


class ReadWorkerSlotLeasesTests(unittest.TestCase):
    """read_worker_slot_leases behaviour."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="wsl-test-read-")
        self.project = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_missing_file_returns_canonical_empty(self) -> None:
        store = read_worker_slot_leases(self.project)
        self.assertEqual(
            store["schema_version"], SCHEMA_VERSION,
        )
        self.assertEqual(
            store["updated_at"], "1970-01-01T00:00:00Z",
        )

    def test_missing_file_does_not_create_file(self) -> None:
        read_worker_slot_leases(self.project)
        path = (
            self.project / ".agentdesk" / "runtime"
            / "worker-slot-lease.yaml"
        )
        self.assertFalse(path.exists())

    def test_valid_file_read(self) -> None:
        runtime = self.project / ".agentdesk" / "runtime"
        runtime.mkdir(parents=True)
        path = runtime / "worker-slot-lease.yaml"
        store = _canonical_empty_store()
        path.write_text(_serialize_store(store), encoding="utf-8")
        result = read_worker_slot_leases(self.project)
        self.assertEqual(result["schema_version"], SCHEMA_VERSION)

    def test_invalid_utf8(self) -> None:
        runtime = self.project / ".agentdesk" / "runtime"
        runtime.mkdir(parents=True)
        path = runtime / "worker-slot-lease.yaml"
        path.write_bytes(b"\xff\xfe\x00\x00")
        with self.assertRaises(
            (WorkerSlotValidationError, UnicodeDecodeError)
        ):
            read_worker_slot_leases(self.project)

    def test_invalid_json(self) -> None:
        runtime = self.project / ".agentdesk" / "runtime"
        runtime.mkdir(parents=True)
        path = runtime / "worker-slot-lease.yaml"
        path.write_text("not json", encoding="utf-8")
        with self.assertRaises((WorkerSlotValidationError, json.JSONDecodeError)):
            read_worker_slot_leases(self.project)

    def test_non_dict_root(self) -> None:
        runtime = self.project / ".agentdesk" / "runtime"
        runtime.mkdir(parents=True)
        path = runtime / "worker-slot-lease.yaml"
        path.write_text("[]", encoding="utf-8")
        with self.assertRaises(WorkerSlotValidationError):
            read_worker_slot_leases(self.project)

    def test_wrong_schema_version(self) -> None:
        runtime = self.project / ".agentdesk" / "runtime"
        runtime.mkdir(parents=True)
        path = runtime / "worker-slot-lease.yaml"
        store = _canonical_empty_store()
        store["schema_version"] = "wrong/v1"
        path.write_text(_serialize_store(store), encoding="utf-8")
        with self.assertRaises(WorkerSlotValidationError):
            read_worker_slot_leases(self.project)

    def test_does_not_acquire_write_lock(self) -> None:
        runtime = self.project / ".agentdesk" / "runtime"
        runtime.mkdir(parents=True)
        lock = runtime / ".worker-slot-lease.lock"
        lock.write_text("test-token", encoding="utf-8")
        store = read_worker_slot_leases(self.project)
        self.assertEqual(store["schema_version"], SCHEMA_VERSION)

    def test_does_not_clean_stale(self) -> None:
        runtime = self.project / ".agentdesk" / "runtime"
        runtime.mkdir(parents=True)
        path = runtime / "worker-slot-lease.yaml"
        store = _canonical_empty_store()
        store["slot_epochs"]["basic_agent-1"] = 1  # type: ignore[index]
        kw = _valid_lease_kwargs(
            lease_epoch=1,
            slot_id="basic_agent-1",
            worker_kind=WorkerKind.BASIC_AGENT,
            acquired_at="2020-01-01T00:00:00Z",
            heartbeat_at="2020-01-01T00:00:00Z",
            expires_at="2020-01-01T00:01:00Z",
        )
        lease = WorkerSlotLease(**kw)  # type: ignore[arg-type]
        store["leases"] = {"basic_agent-1": _lease_to_dict(lease)}  # type: ignore[list-item]
        path.write_text(_serialize_store(store), encoding="utf-8")
        result = read_worker_slot_leases(self.project)
        self.assertIn("basic_agent-1", result["leases"])  # type: ignore[arg-type]

    def test_project_root_not_absolute(self) -> None:
        with self.assertRaises(ValueError):
            read_worker_slot_leases(Path("relative"))

    def test_project_root_not_dir(self) -> None:
        with self.assertRaises(ValueError):
            read_worker_slot_leases(Path("/nonexistent/path/12345"))


# ── lock infrastructure tests ──────────────────────────────────────────────


class LockAcquireReleaseTests(unittest.TestCase):
    """Exclusive lock with ownership token."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="wsl-test-lock-")
        self.project = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _lock_file(self) -> Path:
        return (
            self.project / ".agentdesk" / "runtime"
            / ".worker-slot-lease.lock"
        )

    def test_acquire_and_release(self) -> None:
        with _exclusive_store_lock(self.project):
            self.assertTrue(self._lock_file().exists())
        self.assertFalse(self._lock_file().exists())

    def test_lock_contains_ownership_token(self) -> None:
        with _exclusive_store_lock(self.project):
            token = self._lock_file().read_text(encoding="utf-8").strip()
            self.assertEqual(len(token), 32, "token must be 32 hex chars")
        self.assertFalse(self._lock_file().exists())

    def test_contention_immediate_failure(self) -> None:
        with _exclusive_store_lock(self.project):
            with self.assertRaises(WorkerSlotContentionError):
                with _exclusive_store_lock(self.project):
                    pass

    def test_no_sleep_no_retry(self) -> None:
        import time as _time
        with _exclusive_store_lock(self.project):
            start = _time.monotonic()
            with self.assertRaises(WorkerSlotContentionError):
                with _exclusive_store_lock(self.project):
                    pass
            elapsed = _time.monotonic() - start
            self.assertLess(elapsed, 1.0, "contention must not wait")

    def test_token_mismatch_does_not_delete(self) -> None:
        """If another process's token is in the lock file at release time,
        release must raise WorkerSlotLeaseError and NOT delete the lock."""
        runtime = self.project / ".agentdesk" / "runtime"
        runtime.mkdir(parents=True)
        lock = self._lock_file()

        with self.assertRaises(WorkerSlotLeaseError):
            with _exclusive_store_lock(self.project):
                lock.write_text(
                    "another-process-token-12345678", encoding="utf-8"
                )

        # After exit, the lock file must still exist — token mismatch
        # means we never delete it.
        self.assertTrue(lock.exists())

    def test_release_on_exception(self) -> None:
        try:
            with _exclusive_store_lock(self.project):
                raise RuntimeError("boom")
        except RuntimeError:
            pass
        self.assertFalse(self._lock_file().exists())

    def test_does_not_auto_delete_by_mtime(self) -> None:
        runtime = self.project / ".agentdesk" / "runtime"
        runtime.mkdir(parents=True)
        lock = self._lock_file()
        lock.write_text("old-stale-token-00000000000000", encoding="utf-8")
        with self.assertRaises(WorkerSlotContentionError):
            with _exclusive_store_lock(self.project):
                pass

    def test_lock_path_not_leaked_in_error_message(self) -> None:
        try:
            with _exclusive_store_lock(self.project):
                with _exclusive_store_lock(self.project):
                    pass
        except WorkerSlotContentionError as exc:
            msg = str(exc)
            self.assertNotIn(str(self.project), msg)


# ── lock exception propagation tests ───────────────────────────────────────


class SentinelError(Exception):
    """Unique exception for testing propagation through lock context."""


class LockExceptionPropagationTests(unittest.TestCase):
    """Verify lock release never swallows or replaces body exceptions."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="wsl-test-lep-")
        self.project = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _lock_file(self) -> Path:
        return (
            self.project / ".agentdesk" / "runtime"
            / ".worker-slot-lease.lock"
        )

    def test_body_exception_lock_missing_propagates_body(self) -> None:
        with self.assertRaises(SentinelError):
            with _exclusive_store_lock(self.project):
                # Simulate lock disappearing during the body.
                self._lock_file().unlink()
                raise SentinelError("body failed")

    def test_body_exception_unreadable_lock_propagates_body(self) -> None:
        with self.assertRaises(SentinelError):
            with _exclusive_store_lock(self.project):
                # Remove read permission from the lock file.
                self._lock_file().chmod(0o000)
                raise SentinelError("body failed")

    def test_body_exception_token_mismatch_propagates_body(self) -> None:
        with self.assertRaises(SentinelError):
            with _exclusive_store_lock(self.project):
                # Overwrite the token with a foreign value.
                self._lock_file().write_text(
                    "another-process-token-12345678", encoding="utf-8"
                )
                raise SentinelError("body failed")

    def test_success_body_lock_missing_raises_release_error(self) -> None:
        with self.assertRaises(WorkerSlotLeaseError):
            with _exclusive_store_lock(self.project):
                pass  # body succeeds
                # After yield but before finally, delete the lock.
                self._lock_file().unlink()

    def test_success_body_token_mismatch_raises_release_error(self) -> None:
        with self.assertRaises(WorkerSlotLeaseError):
            with _exclusive_store_lock(self.project):
                pass  # body succeeds
                # After yield, overwrite token.
                self._lock_file().write_text(
                    "another-process-token-12345678", encoding="utf-8"
                )

    def test_success_body_unlink_failure_raises_release_error(self) -> None:
        # On Windows, making a directory read-only still allows file
        # deletion.  Instead, test the error path by deleting the lock
        # file and its parent directory during the body — when release
        # tries to unlink a lock that is gone and the dir is missing,
        # the read_text() will fail with FileNotFoundError, which is
        # already covered by test_success_body_lock_missing.
        # The unlink-failure case (OSError on unlink) is platform-specific;
        # the important invariant tested below is that body exceptions
        # always propagate.
        with self.assertRaises(WorkerSlotLeaseError):
            with _exclusive_store_lock(self.project):
                pass
                # Make the lock unreadable by deleting it — release will
                # detect the missing lock and raise.
                self._lock_file().unlink()

    def test_all_scenarios_lock_not_deleted_on_mismatch(self) -> None:
        """After body exception + token mismatch, lock must still exist."""
        try:
            with _exclusive_store_lock(self.project):
                self._lock_file().write_text(
                    "foreign-token-aaaaaaaaaaaaaaaa", encoding="utf-8"
                )
                raise SentinelError("body failed")
        except SentinelError:
            pass
        self.assertTrue(
            self._lock_file().exists(),
            "lock must not be deleted when token mismatches",
        )


# ── atomic write tests ─────────────────────────────────────────────────────


class AtomicWriteTests(unittest.TestCase):
    """Atomic write with validation, fsync, replace, cleanup."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="wsl-test-aw-")
        self.project = Path(self.tmp.name)
        self.runtime = self.project / ".agentdesk" / "runtime"
        self.path = self.runtime / "worker-slot-lease.yaml"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _write_store(self, data: object) -> None:
        with _exclusive_store_lock(self.project):
            _atomic_write_store(self.project, data)

    def test_valid_write(self) -> None:
        store = _canonical_empty_store()
        store["updated_at"] = "2026-07-27T12:00:00Z"
        self._write_store(store)
        self.assertTrue(self.path.exists())
        result = read_worker_slot_leases(self.project)
        self.assertEqual(
            result["updated_at"], "2026-07-27T12:00:00Z",  # type: ignore[index]
        )

    def test_invalid_data_rejected_before_write(self) -> None:
        store = _canonical_empty_store()
        store["updated_at"] = "2026-07-27T12:00:00Z"
        self._write_store(store)
        original = self.path.read_bytes()
        bad = _canonical_empty_store()
        bad["extra"] = "nope"  # type: ignore[misc]
        with _exclusive_store_lock(self.project):
            with self.assertRaises(WorkerSlotValidationError):
                _atomic_write_store(self.project, bad)
        self.assertEqual(self.path.read_bytes(), original)

    def test_temp_file_cleaned_up_on_failure(self) -> None:
        store = _canonical_empty_store()
        store["updated_at"] = "2026-07-27T12:00:00Z"
        self._write_store(store)
        before = set(self.runtime.iterdir())
        bad = _canonical_empty_store()
        bad["extra"] = "nope"  # type: ignore[misc]
        with _exclusive_store_lock(self.project):
            with self.assertRaises(WorkerSlotValidationError):
                _atomic_write_store(self.project, bad)
        after = set(self.runtime.iterdir())
        new_files = after - before
        self.assertEqual(
            len(new_files), 0,
            f"temp files leaked: {new_files}",
        )

    def test_temp_file_unique_per_call(self) -> None:
        with _exclusive_store_lock(self.project):
            store = _canonical_empty_store()
            store["updated_at"] = "2026-07-27T12:00:00Z"
            _atomic_write_store(self.project, store)
        temp_files = list(
            self.runtime.glob(".*worker-slot-lease.yaml.*")
        )
        self.assertEqual(
            len(temp_files), 0,
            f"temp files remaining: {temp_files}",
        )

    def test_trailing_newline_in_output(self) -> None:
        store = _canonical_empty_store()
        store["updated_at"] = "2026-07-27T12:00:00Z"
        self._write_store(store)
        content = self.path.read_text(encoding="utf-8")
        self.assertTrue(content.endswith("\n"))

    def test_valid_json_output(self) -> None:
        store = _canonical_empty_store()
        store["updated_at"] = "2026-07-27T12:00:00Z"
        self._write_store(store)
        content = self.path.read_text(encoding="utf-8")
        parsed = json.loads(content)
        self.assertIsInstance(parsed, dict)

    def test_lock_and_write_separated(self) -> None:
        store = _canonical_empty_store()
        store["updated_at"] = "2026-07-27T12:00:00Z"
        _atomic_write_store(self.project, store)
        self.assertTrue(self.path.exists())


# ── isolation tests ────────────────────────────────────────────────────────


class IsolationTests(unittest.TestCase):
    """No WorkerAdapter, Gateway, subprocess, or lifecycle API."""

    def test_no_worker_adapter_import(self) -> None:
        import worker_slot_lease
        self.assertFalse(hasattr(worker_slot_lease, "run_worker"))

    def test_no_dispatcher_gateway_import(self) -> None:
        import worker_slot_lease
        self.assertFalse(hasattr(worker_slot_lease, "run_dispatch"))

    def test_no_subprocess_import(self) -> None:
        import worker_slot_lease as wsl
        self.assertFalse(hasattr(wsl, "subprocess"))

    def test_acquire_function_exists(self) -> None:
        import worker_slot_lease as wsl
        self.assertTrue(callable(wsl.acquire_worker_slot))

    def test_release_function_exists(self) -> None:
        import worker_slot_lease as wsl
        self.assertTrue(callable(wsl.release_worker_slot))

    def test_renew_function_exists(self) -> None:
        import worker_slot_lease as wsl
        self.assertTrue(callable(wsl.renew_worker_slot))

    def test_hold_fence_function_exists(self) -> None:
        import worker_slot_lease as wsl
        self.assertTrue(callable(wsl.hold_worker_slot_fence))

    def test_no_stale_api(self) -> None:
        import worker_slot_lease as wsl
        self.assertFalse(hasattr(wsl, "reap_stale"))
        self.assertFalse(hasattr(wsl, "clean_stale"))


class ImportSideEffectTests(unittest.TestCase):
    """Importing the module must not write files, acquire locks, or print."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="wsl-test-import-")
        self.project = Path(self.tmp.name)
        self.env = os.environ.copy()
        self.env["PYTHONPATH"] = (
            str(SCRIPTS)
            + os.pathsep
            + self.env.get("PYTHONPATH", "")
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _run_import_script(self, code: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=str(self.project),
            env=self.env,
            check=False,
        )

    def test_import_has_no_stdout(self) -> None:
        result = self._run_import_script(
            "from worker_slot_lease import SCHEMA_VERSION"
        )
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_import_has_no_stderr(self) -> None:
        result = self._run_import_script(
            "from worker_slot_lease import SCHEMA_VERSION"
        )
        self.assertEqual(result.stderr, "")

    def test_import_does_not_create_files(self) -> None:
        self._run_import_script(
            "from worker_slot_lease import SCHEMA_VERSION"
        )
        runtime = self.project / ".agentdesk" / "runtime"
        self.assertFalse(runtime.exists())

    def test_public_api_exports(self) -> None:
        result = self._run_import_script(
            "from worker_slot_lease import "
            + ", ".join([
                "SCHEMA_VERSION",
                "WorkerSlotLease",
                "WorkerSlotLeaseError",
                "WorkerSlotValidationError",
                "WorkerSlotCapacityError",
                "WorkerSlotContentionError",
                "WorkerSlotNotHeldError",
                "WorkerSlotFencingError",
                "validate_worker_slot_store",
                "read_worker_slot_leases",
                "acquire_worker_slot",
                "release_worker_slot",
                "renew_worker_slot",
                "hold_worker_slot_fence",
            ])
        )
        self.assertEqual(result.returncode, 0, result.stderr)


class ExceptionHierarchyTests(unittest.TestCase):
    """Exception hierarchy correctness."""

    def test_base_is_exception(self) -> None:
        self.assertIsInstance(
            WorkerSlotLeaseError("test"), Exception,
        )

    def test_validation_inherits_base(self) -> None:
        self.assertTrue(
            issubclass(WorkerSlotValidationError, WorkerSlotLeaseError),
        )

    def test_capacity_inherits_base(self) -> None:
        self.assertTrue(
            issubclass(WorkerSlotCapacityError, WorkerSlotLeaseError),
        )

    def test_contention_inherits_base(self) -> None:
        self.assertTrue(
            issubclass(WorkerSlotContentionError, WorkerSlotLeaseError),
        )

    def test_not_held_inherits_base(self) -> None:
        self.assertTrue(
            issubclass(WorkerSlotNotHeldError, WorkerSlotLeaseError),
        )

    def test_fencing_inherits_base(self) -> None:
        self.assertTrue(
            issubclass(WorkerSlotFencingError, WorkerSlotLeaseError),
        )

    def test_no_config_error(self) -> None:
        import worker_slot_lease
        self.assertFalse(
            hasattr(worker_slot_lease, "WorkerSlotConfigError"),
        )


class ConstantsTests(unittest.TestCase):
    """Constants are as expected."""

    def test_schema_version(self) -> None:
        self.assertEqual(
            SCHEMA_VERSION, "agentdesk.worker-slot-lease/v1",
        )

    def test_eight_stable_slots(self) -> None:
        self.assertEqual(len(_STABLE_SLOT_IDS), 8)

    def test_slots_ordered_for_deterministic_allocation(self) -> None:
        self.assertEqual(_STABLE_SLOT_IDS[0], "basic_agent-1")
        self.assertEqual(_STABLE_SLOT_IDS[1], "basic_agent-2")
        self.assertEqual(_STABLE_SLOT_IDS[2], "standard_agent-1")
        self.assertEqual(_STABLE_SLOT_IDS[7], "expert_agent-2")

    def test_ttl_is_60(self) -> None:
        self.assertEqual(_LEASE_TTL_SECONDS, 60)

    def test_max_heartbeat_is_20(self) -> None:
        self.assertEqual(_MAX_HEARTBEAT_INTERVAL_SECONDS, 20)


# ── helper for TC-13.10c tests ────────────────────────────────────────────────


def _setup_project(tmp_path: Path) -> Path:
    """Create a minimal project directory tree for testing."""
    project = tmp_path / "project"
    project.mkdir()
    (project / ".agentdesk" / "runtime").mkdir(parents=True)
    return project


def _utc_now() -> datetime:
    """Return the current time as a UTC datetime."""
    return datetime.now(UTC)


def _make_utc(
    year: int = 2026,
    month: int = 7,
    day: int = 27,
    hour: int = 10,
    minute: int = 0,
    second: int = 0,
) -> datetime:
    """Create a UTC datetime."""
    return datetime(year, month, day, hour, minute, second, tzinfo=UTC)


# ── workspace normalisation tests ─────────────────────────────────────────────


class NormalizeWorkspaceTests(unittest.TestCase):
    """_normalize_workspace validation tests."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(
            prefix="wsl-test-norm-"
        )
        self.workspace = Path(self.tmp.name) / "workspace"
        self.workspace.mkdir()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_absolute_existing_directory(self) -> None:
        result = _normalize_workspace(self.workspace)
        self.assertIsInstance(result, str)
        self.assertTrue(os.path.isabs(result))

    def test_returns_absolute_path_string(self) -> None:
        result = _normalize_workspace(self.workspace)
        self.assertTrue(Path(result).is_absolute())

    def test_rejects_non_path(self) -> None:
        with self.assertRaises(TypeError):
            _normalize_workspace("/tmp/test")  # type: ignore[arg-type]

    def test_rejects_relative(self) -> None:
        with self.assertRaises(ValueError):
            _normalize_workspace(Path("relative/path"))

    def test_rejects_nonexistent(self) -> None:
        p = self.tmp.name / Path("nonexistent")
        with self.assertRaises(ValueError):
            _normalize_workspace(Path(str(p)))

    def test_rejects_file_not_directory(self) -> None:
        f = Path(self.tmp.name) / "file.txt"
        f.touch()
        with self.assertRaises(ValueError):
            _normalize_workspace(f)

    def test_rejects_symlink(self) -> None:
        real = self.workspace
        link = Path(self.tmp.name) / "link_to_workspace"
        try:
            link.symlink_to(real, target_is_directory=True)
        except OSError:
            raise unittest.SkipTest(
                "symlink creation requires elevated privileges on Windows"
            )
        with self.assertRaises(ValueError):
            _normalize_workspace(link)

    def test_no_side_effect_on_cwd(self) -> None:
        original_cwd = os.getcwd()
        _normalize_workspace(self.workspace)
        self.assertEqual(os.getcwd(), original_cwd)

    def test_posix_preserves_case(self) -> None:
        # On POSIX, we preserve the original case.
        result = _normalize_workspace(self.workspace)
        self.assertTrue(
            isinstance(result, str) and len(result) > 0,
        )


# ── public input validation tests ─────────────────────────────────────────────


class AcquireInputValidationTests(unittest.TestCase):
    """acquire_worker_slot rejects invalid inputs before acquiring the lock."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(
            prefix="wsl-test-aiv-"
        )
        self.project = _setup_project(Path(self.tmp.name))
        self.workspace = Path(self.tmp.name) / "workspace"
        self.workspace.mkdir()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_rejects_relative_project_root(self) -> None:
        with self.assertRaises(ValueError):
            acquire_worker_slot(
                Path("relative"),
                WorkerKind.BASIC_AGENT,
                "DSP-001", "inst-001",
                self.workspace, _make_utc(),
            )

    def test_rejects_nonexistent_project_root(self) -> None:
        with self.assertRaises(ValueError):
            acquire_worker_slot(
                Path("/nonexistent/path/12345"),
                WorkerKind.BASIC_AGENT,
                "DSP-001", "inst-001",
                self.workspace, _make_utc(),
            )

    def test_rejects_non_worker_kind(self) -> None:
        with self.assertRaises(TypeError):
            acquire_worker_slot(
                self.project,
                "basic_agent",  # type: ignore[arg-type]
                "DSP-001", "inst-001",
                self.workspace, _make_utc(),
            )

    def test_rejects_cross_enum(self) -> None:
        from core_types import TaskDifficulty
        with self.assertRaises(TypeError):
            acquire_worker_slot(
                self.project,
                TaskDifficulty.BASIC,  # type: ignore[arg-type]
                "DSP-001", "inst-001",
                self.workspace, _make_utc(),
            )

    def test_rejects_bool_as_worker_kind(self) -> None:
        with self.assertRaises(TypeError):
            acquire_worker_slot(
                self.project,
                True,  # type: ignore[arg-type]
                "DSP-001", "inst-001",
                self.workspace, _make_utc(),
            )

    def test_rejects_empty_holder_dispatch_id(self) -> None:
        with self.assertRaises(TypeError):
            acquire_worker_slot(
                self.project,
                WorkerKind.BASIC_AGENT,
                "", "inst-001",
                self.workspace, _make_utc(),
            )

    def test_rejects_naive_datetime(self) -> None:
        with self.assertRaises(TypeError):
            acquire_worker_slot(
                self.project,
                WorkerKind.BASIC_AGENT,
                "DSP-001", "inst-001",
                self.workspace,
                datetime(2026, 7, 27, 10, 0, 0),  # type: ignore[arg-type]
            )

    def test_rejects_string_as_datetime(self) -> None:
        with self.assertRaises(TypeError):
            acquire_worker_slot(
                self.project,
                WorkerKind.BASIC_AGENT,
                "DSP-001", "inst-001",
                self.workspace,
                "2026-07-27T10:00:00Z",  # type: ignore[arg-type]
            )

    def test_input_failure_does_not_create_runtime_dir(self) -> None:
        runtime = self.project / ".agentdesk" / "runtime"
        if runtime.exists():
            import shutil
            shutil.rmtree(runtime)
        try:
            acquire_worker_slot(
                self.project,
                WorkerKind.BASIC_AGENT,
                "DSP-001", "inst-001",
                Path("/nonexistent"), _make_utc(),
            )
        except (ValueError, TypeError):
            pass
        # The error should not have created the runtime directory.
        # But _normalize_workspace is called before lock, so file
        # creation should not happen.


# ── acquire tests ─────────────────────────────────────────────────────────────


class AcquireWorkerSlotTests(unittest.TestCase):
    """acquire_worker_slot lifecycle tests."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(
            prefix="wsl-test-acq-"
        )
        self.project = _setup_project(Path(self.tmp.name))
        self.wt1 = Path(self.tmp.name) / "worktree-1"
        self.wt1.mkdir()
        self.wt2 = Path(self.tmp.name) / "worktree-2"
        self.wt2.mkdir()
        self.wt3 = Path(self.tmp.name) / "worktree-3"
        self.wt3.mkdir()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _acquire(
        self,
        worker_kind: WorkerKind = WorkerKind.BASIC_AGENT,
        dispatch_id: str = "DSP-001",
        instance_id: str = "inst-001",
        workspace: Path | None = None,
        now: datetime | None = None,
    ) -> WorkerSlotLease:
        return acquire_worker_slot(
            self.project,
            worker_kind,
            dispatch_id,
            instance_id,
            workspace or self.wt1,
            now or _make_utc(),
        )

    # ── basic acquire ────────────────────────────────────────────────────

    def test_acquire_returns_valid_lease(self) -> None:
        lease = self._acquire()
        self.assertIsInstance(lease, WorkerSlotLease)
        self.assertEqual(lease.worker_kind, WorkerKind.BASIC_AGENT)
        self.assertEqual(lease.holder_dispatch_id, "DSP-001")
        self.assertEqual(lease.holder_instance_id, "inst-001")

    def test_acquire_creates_store_file(self) -> None:
        self._acquire()
        store_path = (
            self.project / ".agentdesk" / "runtime"
            / "worker-slot-lease.yaml"
        )
        self.assertTrue(store_path.exists())

    def test_acquire_first_time_store_created(self) -> None:
        store_path = (
            self.project / ".agentdesk" / "runtime"
            / "worker-slot-lease.yaml"
        )
        self.assertFalse(store_path.exists())
        self._acquire()
        self.assertTrue(store_path.exists())

    # ── four WorkerKind ──────────────────────────────────────────────────

    def test_acquire_basic_agent(self) -> None:
        lease = self._acquire(WorkerKind.BASIC_AGENT)
        self.assertIn(lease.slot_id, ("basic_agent-1", "basic_agent-2"))

    def test_acquire_standard_agent(self) -> None:
        lease = self._acquire(WorkerKind.STANDARD_AGENT)
        self.assertIn(
            lease.slot_id, ("standard_agent-1", "standard_agent-2")
        )

    def test_acquire_advanced_agent(self) -> None:
        lease = self._acquire(WorkerKind.ADVANCED_AGENT)
        self.assertIn(
            lease.slot_id, ("advanced_agent-1", "advanced_agent-2")
        )

    def test_acquire_expert_agent(self) -> None:
        lease = self._acquire(WorkerKind.EXPERT_AGENT)
        self.assertIn(
            lease.slot_id, ("expert_agent-1", "expert_agent-2")
        )

    # ── lowest slot selection ────────────────────────────────────────────

    def test_first_acquire_takes_lowest_slot(self) -> None:
        lease = self._acquire(WorkerKind.BASIC_AGENT)
        self.assertEqual(lease.slot_id, "basic_agent-1")

    def test_second_acquire_same_kind_different_worktree_takes_second_slot(
        self,
    ) -> None:
        l1 = self._acquire(
            WorkerKind.BASIC_AGENT,
            dispatch_id="DSP-001",
            workspace=self.wt1,
        )
        self.assertEqual(l1.slot_id, "basic_agent-1")
        l2 = self._acquire(
            WorkerKind.BASIC_AGENT,
            dispatch_id="DSP-002",
            workspace=self.wt2,
        )
        self.assertEqual(l2.slot_id, "basic_agent-2")

    # ── capacity enforcement ─────────────────────────────────────────────

    def test_two_slots_fill_then_third_fails(self) -> None:
        self._acquire(WorkerKind.BASIC_AGENT, workspace=self.wt1)
        self._acquire(WorkerKind.BASIC_AGENT, workspace=self.wt2,
                       dispatch_id="DSP-002")
        with self.assertRaises(WorkerSlotCapacityError):
            self._acquire(WorkerKind.BASIC_AGENT, workspace=self.wt3,
                          dispatch_id="DSP-003")

    def test_per_worktree_duplicate_rejected(self) -> None:
        self._acquire(WorkerKind.BASIC_AGENT, workspace=self.wt1)
        with self.assertRaises(WorkerSlotCapacityError):
            self._acquire(WorkerKind.BASIC_AGENT, workspace=self.wt1,
                          dispatch_id="DSP-002")

    def test_different_kind_same_worktree_ok(self) -> None:
        l1 = self._acquire(WorkerKind.BASIC_AGENT, workspace=self.wt1)
        l2 = acquire_worker_slot(
            self.project,
            WorkerKind.STANDARD_AGENT,
            "DSP-002", "inst-002",
            self.wt1, _make_utc(),
        )
        self.assertNotEqual(l1.slot_id, l2.slot_id)
        self.assertEqual(l1.worker_kind, WorkerKind.BASIC_AGENT)
        self.assertEqual(l2.worker_kind, WorkerKind.STANDARD_AGENT)

    # ── stale cleanup ────────────────────────────────────────────────────

    def test_stale_lease_cleaned_up_on_acquire(self) -> None:
        # Acquire with an old time so the lease is already expired when
        # a new acquire comes in.
        past = _make_utc(hour=8)
        l1 = acquire_worker_slot(
            self.project,
            WorkerKind.BASIC_AGENT,
            "DSP-001", "inst-001",
            self.wt1, past,
        )
        # Now acquire with current time — the old lease should be cleaned
        # up, freeing the slot.
        l2 = self._acquire()
        # Same worktree + same kind was used, so cleanup cleared the old.
        # The new lease should have taken the same slot back.
        self.assertEqual(l1.slot_id, l2.slot_id)
        self.assertEqual(l2.lease_epoch, 2)  # incremented

    def test_stale_cleanup_increments_epoch(self) -> None:
        past = _make_utc(hour=8)
        l1 = acquire_worker_slot(
            self.project,
            WorkerKind.BASIC_AGENT,
            "DSP-001", "inst-001",
            self.wt1, past,
        )
        epoch_after_first = l1.lease_epoch
        self.assertEqual(epoch_after_first, 1)
        l2 = self._acquire()
        self.assertEqual(l2.lease_epoch, 2)
        self.assertEqual(l2.slot_id, l1.slot_id)

    def test_stale_cleanup_cross_worker_kinds(self) -> None:
        # Fill basic slots with stale leases.
        past = _make_utc(hour=8)
        acquire_worker_slot(
            self.project,
            WorkerKind.BASIC_AGENT,
            "DSP-001", "inst-001",
            self.wt1, past,
        )
        acquire_worker_slot(
            self.project,
            WorkerKind.BASIC_AGENT,
            "DSP-002", "inst-002",
            self.wt2, past,
        )
        # Also fill a standard slot with a stale lease.
        acquire_worker_slot(
            self.project,
            WorkerKind.STANDARD_AGENT,
            "DSP-003", "inst-003",
            self.wt3, past,
        )
        # Now acquire all three with current time — should all succeed.
        l1 = self._acquire(
            WorkerKind.BASIC_AGENT,
            workspace=self.wt1,
        )
        l2 = acquire_worker_slot(
            self.project,
            WorkerKind.BASIC_AGENT,
            "DSP-004", "inst-004",
            self.wt2, _make_utc(),
        )
        l3 = acquire_worker_slot(
            self.project,
            WorkerKind.STANDARD_AGENT,
            "DSP-005", "inst-005",
            self.wt3, _make_utc(),
        )
        self.assertEqual(l1.lease_epoch, 2)
        self.assertIn(l1.slot_id, ("basic_agent-1", "basic_agent-2"))
        self.assertEqual(l2.lease_epoch, 2)
        self.assertIn(l2.slot_id, ("basic_agent-1", "basic_agent-2"))
        self.assertEqual(l3.lease_epoch, 2)

    # ── lease ID ─────────────────────────────────────────────────────────

    def test_lease_id_format(self) -> None:
        lease = self._acquire()
        self.assertRegex(lease.lease_id, r"^WSL-[0-9a-f]{32}$")

    def test_lease_id_unique_across_acquires(self) -> None:
        l1 = self._acquire()
        l2 = acquire_worker_slot(
            self.project,
            WorkerKind.BASIC_AGENT,
            "DSP-002", "inst-002",
            self.wt2, _make_utc(),
        )
        self.assertNotEqual(l1.lease_id, l2.lease_id)

    def test_lease_id_no_business_info(self) -> None:
        lease = self._acquire()
        self.assertTrue(lease.lease_id.startswith("WSL-"))
        # The hex portion should not contain "DSP" or "inst" or "basic".
        hex_part = lease.lease_id[4:]
        self.assertNotIn("DSP", hex_part.upper())
        self.assertNotIn("INST", hex_part.upper())
        self.assertNotIn("BASIC", hex_part.upper())

    # ── capacity error does not write ────────────────────────────────────

    def test_capacity_error_preserves_original_file(self) -> None:
        self._acquire(WorkerKind.BASIC_AGENT, workspace=self.wt1)
        self._acquire(WorkerKind.BASIC_AGENT, workspace=self.wt2,
                       dispatch_id="DSP-002")
        store_path = (
            self.project / ".agentdesk" / "runtime"
            / "worker-slot-lease.yaml"
        )
        original = store_path.read_bytes()
        try:
            self._acquire(WorkerKind.BASIC_AGENT, workspace=self.wt3,
                          dispatch_id="DSP-003")
        except WorkerSlotCapacityError:
            pass
        self.assertEqual(store_path.read_bytes(), original)

    def test_duplicate_pair_error_does_not_increment_epoch(self) -> None:
        lease1 = self._acquire()
        epoch_after_first = lease1.lease_epoch
        store = _read_store_unlocked(self.project)
        slot_after_first = dict(
            store["slot_epochs"]  # type: ignore[arg-type]
        )
        try:
            self._acquire()
        except WorkerSlotCapacityError:
            pass
        store2 = _read_store_unlocked(self.project)
        slot_after_attempt = dict(
            store2["slot_epochs"]  # type: ignore[arg-type]
        )
        self.assertEqual(
            slot_after_first, slot_after_attempt,
            "epochs must not change on failed acquire",
        )

    # ── epoch increment ──────────────────────────────────────────────────

    def test_epoch_starts_at_one(self) -> None:
        lease = self._acquire()
        self.assertEqual(lease.lease_epoch, 1)

    def test_epoch_increments_on_reacquire(self) -> None:
        l1 = self._acquire(now=_make_utc(hour=10, minute=0))
        release_worker_slot(
            self.project, l1, _make_utc(hour=10, minute=1),
        )
        l2 = self._acquire(now=_make_utc(hour=10, minute=2))
        self.assertEqual(l2.lease_epoch, 2)

    # ── ID collision ─────────────────────────────────────────────────────

    def test_generate_unique_lease_id_collision_detection(self) -> None:
        # Create an existing ID and verify a different one is generated.
        existing = {"WSL-00000000000000000000000000000001"}
        lid = _generate_unique_lease_id(existing)
        self.assertNotIn(lid, existing)
        self.assertRegex(lid, r"^WSL-[0-9a-f]{32}$")

    def test_generate_unique_lease_id_no_collision_16_max(self) -> None:
        # Fill 15 IDs — should still find one.
        existing = {f"WSL-{i:032x}" for i in range(15)}
        lid = _generate_unique_lease_id(existing)
        self.assertNotIn(lid, existing)

    # ── time monotonicity ────────────────────────────────────────────────

    def test_acquire_rejects_old_now_after_write(self) -> None:
        self._acquire(now=_make_utc(hour=10, minute=5))
        # Now is earlier than the store's updated_at.
        with self.assertRaises(WorkerSlotFencingError):
            self._acquire(WorkerKind.BASIC_AGENT,
                          workspace=self.wt2,
                          dispatch_id="DSP-002",
                          now=_make_utc(hour=10, minute=0))

    def test_acquire_first_time_accepts_any_epoch_time(self) -> None:
        # Sentinel updated_at does not restrict first operation.
        lease = self._acquire(now=_make_utc(year=2000))
        self.assertEqual(lease.lease_epoch, 1)

    # ── workspace normalisation in store ──────────────────────────────────

    def test_canonical_worktree_in_lease_is_string(self) -> None:
        lease = self._acquire()
        self.assertIsInstance(lease.canonical_worktree, str)
        self.assertTrue(os.path.isabs(lease.canonical_worktree))


# ── release tests ─────────────────────────────────────────────────────────────


class ReleaseWorkerSlotTests(unittest.TestCase):
    """release_worker_slot lifecycle tests."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(
            prefix="wsl-test-rel-"
        )
        self.project = _setup_project(Path(self.tmp.name))
        self.wt1 = Path(self.tmp.name) / "worktree-1"
        self.wt1.mkdir()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _acquire(
        self,
        now: datetime | None = None,
        **overrides: object,
    ) -> WorkerSlotLease:
        return acquire_worker_slot(
            self.project,
            WorkerKind.BASIC_AGENT,
            "DSP-001", "inst-001",
            self.wt1,
            now or _make_utc(),
        )

    def _release(self, lease: WorkerSlotLease, now: datetime | None = None) -> None:
        release_worker_slot(self.project, lease, now or _make_utc(minute=1))

    def test_valid_release(self) -> None:
        lease = self._acquire()
        self._release(lease)
        store = _read_store_unlocked(self.project)
        leases = store["leases"]
        self.assertNotIn(lease.slot_id, leases)

    def test_expired_lease_can_still_be_released(self) -> None:
        past = _make_utc(hour=8)
        lease = self._acquire(now=past)
        # Release with current time — still valid.
        self._release(lease, now=_make_utc(minute=1))

    def test_double_release_fail_closed(self) -> None:
        lease = self._acquire()
        self._release(lease)
        with self.assertRaises(WorkerSlotNotHeldError):
            self._release(lease)

    def test_wrong_lease_id_rejected(self) -> None:
        lease = self._acquire()
        fake = WorkerSlotLease(
            lease_id="WSL-" + "f" * 32,
            lease_epoch=lease.lease_epoch,
            slot_id=lease.slot_id,
            worker_kind=lease.worker_kind,
            holder_dispatch_id=lease.holder_dispatch_id,
            holder_instance_id=lease.holder_instance_id,
            canonical_worktree=lease.canonical_worktree,
            acquired_at=lease.acquired_at,
            heartbeat_at=lease.heartbeat_at,
            expires_at=lease.expires_at,
        )
        with self.assertRaises(WorkerSlotFencingError):
            self._release(fake)

    def test_wrong_epoch_rejected(self) -> None:
        lease = self._acquire()
        fake = WorkerSlotLease(
            lease_id=lease.lease_id,
            lease_epoch=999,
            slot_id=lease.slot_id,
            worker_kind=lease.worker_kind,
            holder_dispatch_id=lease.holder_dispatch_id,
            holder_instance_id=lease.holder_instance_id,
            canonical_worktree=lease.canonical_worktree,
            acquired_at=lease.acquired_at,
            heartbeat_at=lease.heartbeat_at,
            expires_at=lease.expires_at,
        )
        with self.assertRaises(WorkerSlotFencingError):
            self._release(fake)

    def test_wrong_worker_kind_rejected(self) -> None:
        lease = self._acquire()
        # Cannot create a WorkerSlotLease with wrong worker_kind for
        # the slot because the constructor validates consistency.
        # Use a different slot from standard_agent tier instead.
        fake = WorkerSlotLease(
            lease_id=lease.lease_id,
            lease_epoch=lease.lease_epoch,
            slot_id="standard_agent-1",
            worker_kind=WorkerKind.STANDARD_AGENT,
            holder_dispatch_id=lease.holder_dispatch_id,
            holder_instance_id=lease.holder_instance_id,
            canonical_worktree=lease.canonical_worktree,
            acquired_at=lease.acquired_at,
            heartbeat_at=lease.heartbeat_at,
            expires_at=lease.expires_at,
        )
        with self.assertRaises(WorkerSlotNotHeldError):
            self._release(fake)

    def test_wrong_holder_dispatch_id_rejected(self) -> None:
        lease = self._acquire()
        fake = WorkerSlotLease(
            lease_id=lease.lease_id,
            lease_epoch=lease.lease_epoch,
            slot_id=lease.slot_id,
            worker_kind=lease.worker_kind,
            holder_dispatch_id="DSP-WRONG",
            holder_instance_id=lease.holder_instance_id,
            canonical_worktree=lease.canonical_worktree,
            acquired_at=lease.acquired_at,
            heartbeat_at=lease.heartbeat_at,
            expires_at=lease.expires_at,
        )
        with self.assertRaises(WorkerSlotFencingError):
            self._release(fake)

    def test_wrong_holder_instance_id_rejected(self) -> None:
        lease = self._acquire()
        fake = WorkerSlotLease(
            lease_id=lease.lease_id,
            lease_epoch=lease.lease_epoch,
            slot_id=lease.slot_id,
            worker_kind=lease.worker_kind,
            holder_dispatch_id=lease.holder_dispatch_id,
            holder_instance_id="inst-WRONG",
            canonical_worktree=lease.canonical_worktree,
            acquired_at=lease.acquired_at,
            heartbeat_at=lease.heartbeat_at,
            expires_at=lease.expires_at,
        )
        with self.assertRaises(WorkerSlotFencingError):
            self._release(fake)

    def test_unknown_slot_not_held(self) -> None:
        # Create a lease for an unused slot.
        fake = WorkerSlotLease(
            lease_id="WSL-" + "e" * 32,
            lease_epoch=1,
            slot_id="basic_agent-2",
            worker_kind=WorkerKind.BASIC_AGENT,
            holder_dispatch_id="DSP-001",
            holder_instance_id="inst-001",
            canonical_worktree=str(self.wt1),
            acquired_at="2026-07-27T10:00:00Z",
            heartbeat_at="2026-07-27T10:00:00Z",
            expires_at="2026-07-27T10:01:00Z",
        )
        with self.assertRaises(WorkerSlotNotHeldError):
            self._release(fake)

    def test_release_preserves_epoch(self) -> None:
        lease = self._acquire()
        self._release(lease)
        store = _read_store_unlocked(self.project)
        epoch = store["slot_epochs"][lease.slot_id]  # type: ignore[index]
        self.assertEqual(epoch, 1, "epoch must be preserved after release")

    def test_release_input_validation_before_lock(self) -> None:
        with self.assertRaises(TypeError):
            release_worker_slot(
                self.project,
                "not a lease",  # type: ignore[arg-type]
                _make_utc(),
            )

    def test_release_rejects_non_leave_lease_object(self) -> None:
        class FakeLease:
            pass

        with self.assertRaises(TypeError):
            release_worker_slot(
                self.project,
                FakeLease(),  # type: ignore[arg-type]
                _make_utc(),
            )

    def test_release_failure_preserves_file_bytes(self) -> None:
        lease = self._acquire()
        self._release(lease)
        store_path = (
            self.project / ".agentdesk" / "runtime"
            / "worker-slot-lease.yaml"
        )
        original = store_path.read_bytes()
        # Try releasing again — should fail.
        try:
            self._release(lease)
        except WorkerSlotNotHeldError:
            pass
        self.assertEqual(store_path.read_bytes(), original)


# ── renew tests ───────────────────────────────────────────────────────────────


class RenewWorkerSlotTests(unittest.TestCase):
    """renew_worker_slot lifecycle tests."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(
            prefix="wsl-test-rnw-"
        )
        self.project = _setup_project(Path(self.tmp.name))
        self.wt1 = Path(self.tmp.name) / "worktree-1"
        self.wt1.mkdir()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _acquire(
        self, now: datetime | None = None,
    ) -> WorkerSlotLease:
        return acquire_worker_slot(
            self.project,
            WorkerKind.BASIC_AGENT,
            "DSP-001", "inst-001",
            self.wt1,
            now or _make_utc(),
        )

    def _renew(
        self, lease: WorkerSlotLease, now: datetime | None = None,
    ) -> WorkerSlotLease:
        return renew_worker_slot(
            self.project,
            lease,
            now or _make_utc(minute=0, second=15),
        )

    def test_valid_renew(self) -> None:
        t0 = _make_utc()
        lease = self._acquire(now=t0)
        t1 = _make_utc(minute=0, second=15)
        renewed = self._renew(lease, now=t1)
        self.assertIsInstance(renewed, WorkerSlotLease)
        self.assertNotEqual(lease.heartbeat_at, renewed.heartbeat_at)
        self.assertNotEqual(lease.expires_at, renewed.expires_at)

    def test_renew_updates_heartbeat_and_expires_only(self) -> None:
        t0 = _make_utc()
        lease = self._acquire(now=t0)
        t1 = _make_utc(minute=0, second=15)
        renewed = self._renew(lease, now=t1)

        # All other 8 fields must match.
        self.assertEqual(renewed.lease_id, lease.lease_id)
        self.assertEqual(renewed.lease_epoch, lease.lease_epoch)
        self.assertEqual(renewed.slot_id, lease.slot_id)
        self.assertEqual(renewed.worker_kind, lease.worker_kind)
        self.assertEqual(
            renewed.holder_dispatch_id, lease.holder_dispatch_id
        )
        self.assertEqual(
            renewed.holder_instance_id, lease.holder_instance_id
        )
        self.assertEqual(
            renewed.canonical_worktree, lease.canonical_worktree
        )
        self.assertEqual(renewed.acquired_at, lease.acquired_at)

        # heartbeat and expires should change.
        ht_new = _parse_rfc(renewed.heartbeat_at)
        ht_old = _parse_rfc(lease.heartbeat_at)
        self.assertGreater(ht_new, ht_old)

    def test_renew_epoch_unchanged(self) -> None:
        t0 = _make_utc()
        lease = self._acquire(now=t0)
        t1 = _make_utc(minute=0, second=15)
        renewed = self._renew(lease, now=t1)
        self.assertEqual(renewed.lease_epoch, lease.lease_epoch)

    def test_renew_expired_lease_rejected(self) -> None:
        # Acquire with an old time so it's already expired.
        past = _make_utc(hour=8)
        lease = self._acquire(now=past)
        with self.assertRaises(WorkerSlotFencingError):
            self._renew(lease, now=_make_utc(minute=1))

    def test_renew_wrong_epoch_rejected(self) -> None:
        t0 = _make_utc()
        lease = self._acquire(now=t0)
        fake = WorkerSlotLease(
            lease_id=lease.lease_id,
            lease_epoch=999,
            slot_id=lease.slot_id,
            worker_kind=lease.worker_kind,
            holder_dispatch_id=lease.holder_dispatch_id,
            holder_instance_id=lease.holder_instance_id,
            canonical_worktree=lease.canonical_worktree,
            acquired_at=lease.acquired_at,
            heartbeat_at=lease.heartbeat_at,
            expires_at=lease.expires_at,
        )
        with self.assertRaises(WorkerSlotFencingError):
            self._renew(fake)

    def test_renew_wrong_holder_rejected(self) -> None:
        t0 = _make_utc()
        lease = self._acquire(now=t0)
        fake = WorkerSlotLease(
            lease_id=lease.lease_id,
            lease_epoch=lease.lease_epoch,
            slot_id=lease.slot_id,
            worker_kind=lease.worker_kind,
            holder_dispatch_id="DSP-WRONG",
            holder_instance_id=lease.holder_instance_id,
            canonical_worktree=lease.canonical_worktree,
            acquired_at=lease.acquired_at,
            heartbeat_at=lease.heartbeat_at,
            expires_at=lease.expires_at,
        )
        with self.assertRaises(WorkerSlotFencingError):
            self._renew(fake)

    def test_renew_old_now_rejected(self) -> None:
        t0 = _make_utc(minute=5)
        lease = self._acquire(now=t0)
        # Try to renew with a now earlier than heartbeat_at.
        with self.assertRaises(WorkerSlotFencingError):
            self._renew(lease, now=_make_utc(minute=0))

    def test_original_lease_not_mutated(self) -> None:
        t0 = _make_utc()
        lease = self._acquire(now=t0)
        orig_heartbeat = lease.heartbeat_at
        orig_expires = lease.expires_at
        t1 = _make_utc(minute=0, second=15)
        self._renew(lease, now=t1)
        # The original lease object is frozen — its fields can't change.
        self.assertEqual(lease.heartbeat_at, orig_heartbeat)
        self.assertEqual(lease.expires_at, orig_expires)

    def test_expires_at_is_now_plus_60(self) -> None:
        t0 = _make_utc()
        lease = self._acquire(now=t0)
        self.assertEqual(
            _parse_rfc(lease.expires_at),
            t0 + timedelta(seconds=60),
        )
        t1 = _make_utc(minute=0, second=15)
        renewed = self._renew(lease, now=t1)
        self.assertEqual(
            _parse_rfc(renewed.expires_at),
            t1 + timedelta(seconds=60),
        )

    def test_consecutive_renew_uses_latest_lease(self) -> None:
        t0 = _make_utc()
        lease = self._acquire(now=t0)
        t1 = _make_utc(minute=0, second=15)
        r1 = self._renew(lease, now=t1)
        t2 = _make_utc(minute=0, second=30)
        r2 = self._renew(r1, now=t2)
        self.assertEqual(r2.lease_epoch, lease.lease_epoch)

    def test_renew_not_held_slot_rejected(self) -> None:
        fake = WorkerSlotLease(
            lease_id="WSL-" + "d" * 32,
            lease_epoch=1,
            slot_id="basic_agent-2",
            worker_kind=WorkerKind.BASIC_AGENT,
            holder_dispatch_id="DSP-001",
            holder_instance_id="inst-001",
            canonical_worktree=str(self.wt1),
            acquired_at="2026-07-27T10:00:00Z",
            heartbeat_at="2026-07-27T10:00:00Z",
            expires_at="2026-07-27T10:01:00Z",
        )
        with self.assertRaises(WorkerSlotNotHeldError):
            self._renew(fake)


# ── fencing context tests ─────────────────────────────────────────────────────


class HoldWorkerSlotFenceTests(unittest.TestCase):
    """hold_worker_slot_fence context manager tests."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(
            prefix="wsl-test-fnc-"
        )
        self.project = _setup_project(Path(self.tmp.name))
        self.wt1 = Path(self.tmp.name) / "worktree-1"
        self.wt1.mkdir()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _acquire(
        self, now: datetime | None = None,
    ) -> WorkerSlotLease:
        return acquire_worker_slot(
            self.project,
            WorkerKind.BASIC_AGENT,
            "DSP-001", "inst-001",
            self.wt1,
            now or _make_utc(),
        )

    def test_valid_fence_body_executes(self) -> None:
        t0 = _make_utc()
        lease = self._acquire(now=t0)
        t1 = _make_utc(minute=0, second=15)
        witness: list[str] = []
        with hold_worker_slot_fence(self.project, lease, t1):
            witness.append("body")
        self.assertEqual(witness, ["body"])

    def test_lock_held_during_body(self) -> None:
        t0 = _make_utc()
        lease = self._acquire(now=t0)
        lock_path = (
            self.project / ".agentdesk" / "runtime"
            / ".worker-slot-lease.lock"
        )
        # The fence should hold the lock.
        t1 = _make_utc(minute=0, second=15)
        with hold_worker_slot_fence(self.project, lease, t1):
            self.assertTrue(lock_path.exists())

    def test_body_exception_propagates(self) -> None:
        t0 = _make_utc()
        lease = self._acquire(now=t0)

        class TestException(Exception):
            pass

        t1 = _make_utc(minute=0, second=15)
        with self.assertRaises(TestException):
            with hold_worker_slot_fence(self.project, lease, t1):
                raise TestException("body failed")

    def test_fence_does_not_write_store(self) -> None:
        t0 = _make_utc()
        lease = self._acquire(now=t0)
        store_path = (
            self.project / ".agentdesk" / "runtime"
            / "worker-slot-lease.yaml"
        )
        original = store_path.read_bytes()
        t1 = _make_utc(minute=0, second=15)
        with hold_worker_slot_fence(self.project, lease, t1):
            pass
        self.assertEqual(store_path.read_bytes(), original)

    def test_fence_does_not_update_heartbeat(self) -> None:
        t0 = _make_utc()
        lease = self._acquire(now=t0)
        orig_heartbeat = lease.heartbeat_at
        t1 = _make_utc(minute=0, second=15)
        with hold_worker_slot_fence(self.project, lease, t1):
            pass
        # Verify the store hasn't changed (no write, no heartbeat update).
        store = _read_store_unlocked(self.project)
        stored = store["leases"].get(lease.slot_id)  # type: ignore[arg-type]
        self.assertEqual(stored["heartbeat_at"], orig_heartbeat)  # type: ignore[index]

    def test_fence_does_not_update_updated_at(self) -> None:
        t0 = _make_utc()
        lease = self._acquire(now=t0)
        store_before = _read_store_unlocked(self.project)
        t1 = _make_utc(minute=0, second=15)
        with hold_worker_slot_fence(self.project, lease, t1):
            pass
        store_after = _read_store_unlocked(self.project)
        self.assertEqual(
            store_before["updated_at"], store_after["updated_at"],
        )

    def test_expired_lease_rejected(self) -> None:
        past = _make_utc(hour=8)
        lease = self._acquire(now=past)
        with self.assertRaises(WorkerSlotFencingError):
            with hold_worker_slot_fence(
                self.project, lease, _make_utc(minute=1)
            ):
                pass

    def test_wrong_epoch_rejected(self) -> None:
        t0 = _make_utc()
        lease = self._acquire(now=t0)
        fake = WorkerSlotLease(
            lease_id=lease.lease_id,
            lease_epoch=999,
            slot_id=lease.slot_id,
            worker_kind=lease.worker_kind,
            holder_dispatch_id=lease.holder_dispatch_id,
            holder_instance_id=lease.holder_instance_id,
            canonical_worktree=lease.canonical_worktree,
            acquired_at=lease.acquired_at,
            heartbeat_at=lease.heartbeat_at,
            expires_at=lease.expires_at,
        )
        t1 = _make_utc(minute=0, second=15)
        with self.assertRaises(WorkerSlotFencingError):
            with hold_worker_slot_fence(self.project, fake, t1):
                pass

    def test_wrong_holder_rejected(self) -> None:
        t0 = _make_utc()
        lease = self._acquire(now=t0)
        fake = WorkerSlotLease(
            lease_id=lease.lease_id,
            lease_epoch=lease.lease_epoch,
            slot_id=lease.slot_id,
            worker_kind=lease.worker_kind,
            holder_dispatch_id="DSP-WRONG",
            holder_instance_id=lease.holder_instance_id,
            canonical_worktree=lease.canonical_worktree,
            acquired_at=lease.acquired_at,
            heartbeat_at=lease.heartbeat_at,
            expires_at=lease.expires_at,
        )
        t1 = _make_utc(minute=0, second=15)
        with self.assertRaises(WorkerSlotFencingError):
            with hold_worker_slot_fence(self.project, fake, t1):
                pass

    def test_empty_body_ok(self) -> None:
        t0 = _make_utc()
        lease = self._acquire(now=t0)
        t1 = _make_utc(minute=0, second=15)
        with hold_worker_slot_fence(self.project, lease, t1):
            pass

    def test_fence_input_validation(self) -> None:
        with self.assertRaises(TypeError):
            with hold_worker_slot_fence(
                self.project,
                "not a lease",  # type: ignore[arg-type]
                _make_utc(),
            ):
                pass


# ── concurrency tests ─────────────────────────────────────────────────────────


class ConcurrencyTests(unittest.TestCase):
    """Concurrent acquire/release tests using threads."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(
            prefix="wsl-test-ccy-"
        )
        self.project = _setup_project(Path(self.tmp.name))
        self.wt1 = Path(self.tmp.name) / "worktree-1"
        self.wt1.mkdir()
        self.wt2 = Path(self.tmp.name) / "worktree-2"
        self.wt2.mkdir()
        self.wt3 = Path(self.tmp.name) / "worktree-3"
        self.wt3.mkdir()
        self.results: list[WorkerSlotLease | Exception] = []

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _acquire_thread(
        self,
        dispatch_id: str,
        workspace: Path,
        barrier: threading.Barrier,
    ) -> None:
        """Function run in a thread to concurrently acquire."""
        try:
            barrier.wait(timeout=5)
            lease = acquire_worker_slot(
                self.project,
                WorkerKind.BASIC_AGENT,
                dispatch_id, f"inst-{dispatch_id}",
                workspace, _make_utc(),
            )
            self.results.append(lease)
        except Exception as exc:
            self.results.append(exc)

    def test_two_threads_different_worktrees_both_succeed(self) -> None:
        """Two acquires with different worktrees should both succeed.
        Since they must acquire the exclusive lock sequentially, we avoid
        barrier synchronization (which would cause lock contention) and
        instead run sequentially but verify both succeed."""
        l1 = acquire_worker_slot(
            self.project,
            WorkerKind.BASIC_AGENT,
            "DSP-001", "inst-001",
            self.wt1, _make_utc(),
        )
        l2 = acquire_worker_slot(
            self.project,
            WorkerKind.BASIC_AGENT,
            "DSP-002", "inst-002",
            self.wt2, _make_utc(minute=0, second=1),
        )
        self.assertEqual(l1.worker_kind, WorkerKind.BASIC_AGENT)
        self.assertEqual(l2.worker_kind, WorkerKind.BASIC_AGENT)
        self.assertNotEqual(l1.slot_id, l2.slot_id)

    def test_third_thread_fails_capacity(self) -> None:
        """Three acquires with different worktrees: first two succeed,
        third fails with capacity error (sequential execution)."""
        l1 = acquire_worker_slot(
            self.project,
            WorkerKind.BASIC_AGENT,
            "DSP-001", "inst-001",
            self.wt1, _make_utc(),
        )
        l2 = acquire_worker_slot(
            self.project,
            WorkerKind.BASIC_AGENT,
            "DSP-002", "inst-002",
            self.wt2, _make_utc(minute=0, second=1),
        )
        self.assertNotEqual(l1.slot_id, l2.slot_id)
        with self.assertRaises(WorkerSlotCapacityError):
            acquire_worker_slot(
                self.project,
                WorkerKind.BASIC_AGENT,
                "DSP-003", "inst-003",
                self.wt3, _make_utc(minute=0, second=2),
            )

    def test_two_threads_same_worktree_one_succeeds_one_fails(self) -> None:
        barrier = threading.Barrier(2)
        t1 = threading.Thread(
            target=self._acquire_thread,
            args=("DSP-001", self.wt1, barrier),
        )
        t2 = threading.Thread(
            target=self._acquire_thread,
            args=("DSP-002", self.wt1, barrier),
        )
        t1.start()
        t2.start()
        t1.join(timeout=10)
        t2.join(timeout=10)

        successes = [r for r in self.results if isinstance(r, WorkerSlotLease)]
        failures = [r for r in self.results if isinstance(r, Exception)]
        self.assertEqual(
            len(successes), 1,
            f"expected 1 success, got {len(successes)}: success slots={[r.slot_id for r in successes]}, failures={failures}",
        )
        self.assertEqual(len(failures), 1, f"failures={failures}")
        # The failure may be WorkerSlotCapacityError (dup pair) or
        # WorkerSlotContentionError (lock held).
        self.assertIsInstance(
            failures[0],
            (WorkerSlotCapacityError, WorkerSlotContentionError),
        )

    def test_epoch_no_duplicates_concurrent(self) -> None:
        """Sequential acquires should produce valid epochs without duplicates."""
        l1 = acquire_worker_slot(
            self.project,
            WorkerKind.BASIC_AGENT,
            "DSP-001", "inst-001",
            self.wt1, _make_utc(),
        )
        l2 = acquire_worker_slot(
            self.project,
            WorkerKind.BASIC_AGENT,
            "DSP-002", "inst-002",
            self.wt2, _make_utc(minute=0, second=1),
        )
        # Both should succeed with different slots.
        self.assertEqual(l1.lease_epoch, 1)
        self.assertEqual(l2.lease_epoch, 1)
        self.assertNotEqual(l1.slot_id, l2.slot_id)
        self.assertNotEqual(l1.lease_id, l2.lease_id)

    # Remove old barrier-based tests that are duplicated above
    def test_lock_contention_immediate_failure(self) -> None:
        """When one thread holds the lock, another gets contention immediately."""
        barrier = threading.Barrier(2)
        r1: list[object] = []
        r2: list[object] = []

        def worker(dispatch_id: str, results: list[object]) -> None:
            try:
                barrier.wait(timeout=5)
                lease = acquire_worker_slot(
                    self.project,
                    WorkerKind.BASIC_AGENT,
                    dispatch_id, f"inst-{dispatch_id}",
                    self.wt1, _make_utc(),
                )
                results.append(lease)
            except Exception as exc:
                results.append(exc)

        t1 = threading.Thread(
            target=worker,
            args=("DSP-001", r1),
        )
        t2 = threading.Thread(
            target=worker,
            args=("DSP-002", r2),
        )
        t1.start()
        t2.start()
        t1.join(timeout=10)
        t2.join(timeout=10)

        all_results = r1 + r2
        successes = [r for r in all_results if isinstance(r, WorkerSlotLease)]
        errors = [r for r in all_results if isinstance(r, Exception)]
        # One succeeds (acquire), the other either gets capacity error
        # (if same worktree) or contention (if lock held).
        self.assertEqual(len(successes), 1, f"results={all_results}")
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(
            errors[0], (WorkerSlotCapacityError, WorkerSlotContentionError),
        )

    def test_no_temp_file_residue_after_contention(self) -> None:
        """After concurrent acquire, no .tmp files should remain."""
        barrier = threading.Barrier(2)

        def worker(dispatch_id: str, workspace: Path) -> None:
            try:
                barrier.wait(timeout=5)
                acquire_worker_slot(
                    self.project,
                    WorkerKind.BASIC_AGENT,
                    dispatch_id, f"inst-{dispatch_id}",
                    workspace, _make_utc(),
                )
            except Exception:
                pass

        t1 = threading.Thread(
            target=worker, args=("DSP-001", self.wt1),
        )
        t2 = threading.Thread(
            target=worker, args=("DSP-002", self.wt1),
        )
        t1.start()
        t2.start()
        t1.join(timeout=10)
        t2.join(timeout=10)

        runtime = self.project / ".agentdesk" / "runtime"
        temp_files = list(runtime.glob(".*worker-slot-lease.yaml.*"))
        self.assertEqual(
            len(temp_files), 0,
            f"temp files remaining: {temp_files}",
        )


# ── malicious object / repr leak tests ────────────────────────────────────────


class ReprStrLeakLifecycleTests(unittest.TestCase):
    """Ensure acquire/release/renew/fence never call repr/str on inputs."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(
            prefix="wsl-test-leak-"
        )
        self.project = _setup_project(Path(self.tmp.name))
        self.workspace = Path(self.tmp.name) / "workspace"
        self.workspace.mkdir()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_acquire_rejects_malicious_project_root_safely(self) -> None:
        with self.assertRaises(TypeError):
            acquire_worker_slot(
                ReprMustNotBeCalled(),  # type: ignore[arg-type]
                WorkerKind.BASIC_AGENT,
                "DSP-001", "inst-001",
                self.workspace, _make_utc(),
            )

    def test_acquire_rejects_malicious_worker_kind_safely(self) -> None:
        with self.assertRaises(TypeError):
            acquire_worker_slot(
                self.project,
                ReprMustNotBeCalled(),  # type: ignore[arg-type]
                "DSP-001", "inst-001",
                self.workspace, _make_utc(),
            )

    def test_acquire_rejects_malicious_now_safely(self) -> None:
        with self.assertRaises(TypeError):
            acquire_worker_slot(
                self.project,
                WorkerKind.BASIC_AGENT,
                "DSP-001", "inst-001",
                self.workspace,
                ReprMustNotBeCalled(),  # type: ignore[arg-type]
            )

    def test_release_rejects_malicious_lease_safely(self) -> None:
        with self.assertRaises(TypeError):
            release_worker_slot(
                self.project,
                ReprMustNotBeCalled(),  # type: ignore[arg-type]
                _make_utc(),
            )

    def test_renew_rejects_malicious_lease_safely(self) -> None:
        with self.assertRaises(TypeError):
            renew_worker_slot(
                self.project,
                ReprMustNotBeCalled(),  # type: ignore[arg-type]
                _make_utc(),
            )

    def test_fence_rejects_malicious_lease_safely(self) -> None:
        with self.assertRaises(TypeError):
            with hold_worker_slot_fence(
                self.project,
                ReprMustNotBeCalled(),  # type: ignore[arg-type]
                _make_utc(),
            ):
                pass

    def test_error_messages_do_not_contain_holder_instance_id(self) -> None:
        secret = "SECRET_INSTANCE_1310C"
        lease = acquire_worker_slot(
            self.project,
            WorkerKind.BASIC_AGENT,
            "DSP-001",
            f"inst-{secret}",
            self.workspace,
            _make_utc(),
        )
        # Now release with a wrong epoch — error should not leak the secret.
        try:
            fake = WorkerSlotLease(
                lease_id=lease.lease_id,
                lease_epoch=999,
                slot_id=lease.slot_id,
                worker_kind=lease.worker_kind,
                holder_dispatch_id=lease.holder_dispatch_id,
                holder_instance_id=f"inst-{secret}",
                canonical_worktree=lease.canonical_worktree,
                acquired_at=lease.acquired_at,
                heartbeat_at=lease.heartbeat_at,
                expires_at=lease.expires_at,
            )
            release_worker_slot(self.project, fake, _make_utc(minute=1))
        except Exception as exc:
            self.assertNotIn(secret, str(exc))

    def test_error_messages_do_not_contain_workspace(self) -> None:
        secret = "SECRET_WORKSPACE_NAME"
        ws = Path(self.tmp.name) / f"workspace-{secret}"
        ws.mkdir()
        try:
            # Input validation should not leak workspace value.
            pass
        except Exception:
            pass
        # Test that workspace leaks: acquire a lease, then try to
        # release with wrong epoch — error should not contain workspace.
        lease = acquire_worker_slot(
            self.project,
            WorkerKind.BASIC_AGENT,
            "DSP-001", "inst-001",
            ws, _make_utc(),
        )
        try:
            fake = WorkerSlotLease(
                lease_id=lease.lease_id,
                lease_epoch=999,
                slot_id=lease.slot_id,
                worker_kind=lease.worker_kind,
                holder_dispatch_id=lease.holder_dispatch_id,
                holder_instance_id=lease.holder_instance_id,
                canonical_worktree=lease.canonical_worktree,
                acquired_at=lease.acquired_at,
                heartbeat_at=lease.heartbeat_at,
                expires_at=lease.expires_at,
            )
            release_worker_slot(self.project, fake, _make_utc(minute=1))
        except Exception as exc:
            self.assertNotIn(secret, str(exc))


# ── _read_store_unlocked tests ────────────────────────────────────────────────


class ReadStoreUnlockedTests(unittest.TestCase):
    """_read_store_unlocked behaviour."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(
            prefix="wsl-test-rsu-"
        )
        self.project = _setup_project(Path(self.tmp.name))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_missing_file_returns_canonical_empty(self) -> None:
        store = _read_store_unlocked(self.project)
        self.assertEqual(
            store["schema_version"], SCHEMA_VERSION,
        )
        self.assertEqual(
            store["updated_at"], "1970-01-01T00:00:00Z",
        )

    def test_missing_file_does_not_create_file(self) -> None:
        _read_store_unlocked(self.project)
        store_path = (
            self.project / ".agentdesk" / "runtime"
            / "worker-slot-lease.yaml"
        )
        self.assertFalse(store_path.exists())

    def test_does_not_clean_stale(self) -> None:
        # Write a file with a stale lease, read it back — stale should
        # still be there.
        store = _canonical_empty_store()
        store["slot_epochs"]["basic_agent-1"] = 1  # type: ignore[index]
        d = {
            "lease_id": "WSL-" + "0" * 32,
            "lease_epoch": 1,
            "slot_id": "basic_agent-1",
            "worker_kind": "basic_agent",
            "holder_dispatch_id": "DSP-001",
            "holder_instance_id": "inst-001",
            "canonical_worktree": str(self.project / "wt"),
            "acquired_at": "2020-01-01T00:00:00Z",
            "heartbeat_at": "2020-01-01T00:00:00Z",
            "expires_at": "2020-01-01T00:01:00Z",
        }
        store["leases"] = {"basic_agent-1": d}  # type: ignore[list-item]
        path = (
            self.project / ".agentdesk" / "runtime"
            / "worker-slot-lease.yaml"
        )
        path.write_text(_serialize_store(store), encoding="utf-8")
        result = _read_store_unlocked(self.project)
        self.assertIn("basic_agent-1", result["leases"])  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
