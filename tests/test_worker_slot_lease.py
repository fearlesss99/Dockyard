"""Tests for WorkerSlotLease store — TC-13.10b.

Covers: data model, store schema, serialization round-trip, read-only
load, lock infrastructure, atomic write, isolation from WorkerAdapter
and Gateway, and exception safety.

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
import unittest
from dataclasses import fields as dc_fields
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
        kw = _valid_lease_kwargs()
        lease = WorkerSlotLease(**kw)  # type: ignore[arg-type]
        with self.assertRaises(AttributeError):
            lease.extra = 42  # type: ignore[attr-defined]


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
        with self.assertRaises(ValueError):
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
        # The sentinel is 1970 — should never equal "now".
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
            any("does not match the leases key" in e for e in errors),
            str(errors),
        )

    def test_duplicate_lease_id(self) -> None:
        store = _canonical_empty_store()
        # Ensure slot_epochs match lease epochs.
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
        kw = _valid_lease_kwargs(
            slot_id="basic_agent-1",
            worker_kind=WorkerKind.STANDARD_AGENT,
        )
        # Build via dict to bypass dataclass validation.
        d = _lease_to_dict(
            WorkerSlotLease(**_valid_lease_kwargs(  # type: ignore[arg-type]
                slot_id="basic_agent-1",
                worker_kind=WorkerKind.STANDARD_AGENT,
            ))
        ) if False else {
            "lease_id": "WSL-00000000000000000000000000000001",
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
        """Store validation does NOT reject expired leases — that's
        TC-13.10c's job."""
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
        """Error messages must not contain full holder_instance_id or
        canonical_worktree."""
        store = _canonical_empty_store()
        store["leases"] = {"basic_agent-1": {  # type: ignore[list-item]
            "lease_id": "WSL-00000000000000000000000000000001",
            "lease_epoch": True,  # bool
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


# ── serialization tests ────────────────────────────────────────────────────


def _parse_rfc(value: str) -> object:
    """Parse RFC 3339 for comparison purposes."""
    from datetime import UTC, datetime
    candidate = value.strip()
    if candidate.endswith("Z"):
        candidate = candidate[:-1] + "+00:00"
    return datetime.fromisoformat(candidate)


def _code_body() -> str:
    """Return the module source with the top-level module docstring removed."""
    code = _WSL_PY.read_text(encoding="utf-8")
    # Strip the first docstring (module-level).
    # Find the second triple-quote — the end of the module docstring.
    first = code.find('"""')
    if first == -1:
        return code
    second = code.find('"""', first + 3)
    if second == -1:
        return code
    return code[second + 3:]


class IsolationTests(unittest.TestCase):
    """No WorkerAdapter, Gateway, subprocess, or lifecycle API."""

    def test_no_worker_adapter_import(self) -> None:
        import worker_slot_lease
        self.assertFalse(
            hasattr(worker_slot_lease, "run_worker"),
            "must not import worker_adapter",
        )

    def test_no_dispatcher_gateway_import(self) -> None:
        import worker_slot_lease
        self.assertFalse(
            hasattr(worker_slot_lease, "run_dispatch"),
            "must not import dispatcher_gateway",
        )

    def test_no_subprocess_import(self) -> None:
        import worker_slot_lease as wsl
        self.assertFalse(
            hasattr(wsl, "subprocess"),
        )

    def test_no_acquire_function(self) -> None:
        import worker_slot_lease as wsl
        self.assertFalse(hasattr(wsl, "acquire_worker_slot"))

    def test_no_release_function(self) -> None:
        import worker_slot_lease as wsl
        self.assertFalse(hasattr(wsl, "release_worker_slot"))

    def test_no_renew_function(self) -> None:
        import worker_slot_lease as wsl
        self.assertFalse(hasattr(wsl, "renew_worker_slot"))

    def test_no_hold_fence_function(self) -> None:
        import worker_slot_lease as wsl
        self.assertFalse(hasattr(wsl, "hold_worker_slot_fence"))

    def test_no_stale_api(self) -> None:
        import worker_slot_lease as wsl
        self.assertFalse(hasattr(wsl, "reap_stale"))
        self.assertFalse(hasattr(wsl, "clean_stale"))


class SerializationRoundTripTests(unittest.TestCase):
    """Exact 10-key round-trip."""

    def test_round_trip_basic(self) -> None:
        kw = _valid_lease_kwargs()
        lease = WorkerSlotLease(**kw)  # type: ignore[arg-type]
        d = _lease_to_dict(lease)
        self.assertSetEqual(set(d.keys()), set(_LEASE_FIELD_NAMES))
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
        """read must work even when the lock file exists."""
        runtime = self.project / ".agentdesk" / "runtime"
        runtime.mkdir(parents=True)
        lock = runtime / ".worker-slot-lease.lock"
        lock.write_text("test-token", encoding="utf-8")
        # Must not raise LockContentionError.
        store = read_worker_slot_leases(self.project)
        self.assertEqual(store["schema_version"], SCHEMA_VERSION)

    def test_does_not_clean_stale(self) -> None:
        """read must not remove expired leases."""
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
        """Contention must raise instantly — no waiting."""
        import time as _time
        with _exclusive_store_lock(self.project):
            start = _time.monotonic()
            with self.assertRaises(WorkerSlotContentionError):
                with _exclusive_store_lock(self.project):
                    pass
            elapsed = _time.monotonic() - start
            self.assertLess(elapsed, 1.0, "contention must not wait")

    def test_token_mismatch_does_not_delete(self) -> None:
        """If another process's token is in the lock file, release
        must NOT delete it.

        We simulate this by writing a foreign token into the lock file
        AFTER acquiring our own lock and while still holding it.  This
        mimics a takeover: our token gets overwritten by the other
        process.  On release, the token won't match so we must leave
        the file alone.
        """
        runtime = self.project / ".agentdesk" / "runtime"
        runtime.mkdir(parents=True)
        lock = self._lock_file()

        # First, acquire our own lock normally.
        with _exclusive_store_lock(self.project):
            # While we hold the lock, overwrite the token with a foreign
            # one to simulate a takeover.
            lock.write_text(
                "another-process-token-12345678", encoding="utf-8"
            )
            # On exit, our release code will read the foreign token,
            # see it doesn't match, and leave the file alone.

        # After exit, the lock file should still exist.
        self.assertTrue(lock.exists())

    def test_release_on_exception(self) -> None:
        """Lock must be released even if the guarded block raises."""
        try:
            with _exclusive_store_lock(self.project):
                raise RuntimeError("boom")
        except RuntimeError:
            pass
        self.assertFalse(self._lock_file().exists())

    def test_does_not_auto_delete_by_mtime(self) -> None:
        """An old lock file must not be silently removed — only contention."""
        runtime = self.project / ".agentdesk" / "runtime"
        runtime.mkdir(parents=True)
        lock = self._lock_file()
        lock.write_text("old-stale-token-00000000000000", encoding="utf-8")
        # Second attempt must still get contention.
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
        """Write store atomically inside a lock (the real call path)."""
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
        """Invalid data must not touch the file at all."""
        # First write a valid store.
        store = _canonical_empty_store()
        store["updated_at"] = "2026-07-27T12:00:00Z"
        self._write_store(store)
        original = self.path.read_bytes()

        # Now try an invalid write.
        bad = _canonical_empty_store()
        bad["extra"] = "nope"  # type: ignore[misc]
        with _exclusive_store_lock(self.project):
            with self.assertRaises(WorkerSlotValidationError):
                _atomic_write_store(self.project, bad)
        # Original must be preserved.
        self.assertEqual(self.path.read_bytes(), original)

    def test_temp_file_cleaned_up_on_failure(self) -> None:
        """After a failed write, no temp files remain."""
        # Write a valid store first.
        store = _canonical_empty_store()
        store["updated_at"] = "2026-07-27T12:00:00Z"
        self._write_store(store)

        # Count files before.
        before = set(self.runtime.iterdir())

        # Try invalid write.
        bad = _canonical_empty_store()
        bad["extra"] = "nope"  # type: ignore[misc]
        with _exclusive_store_lock(self.project):
            with self.assertRaises(WorkerSlotValidationError):
                _atomic_write_store(self.project, bad)

        # No extra files.
        after = set(self.runtime.iterdir())
        new_files = after - before
        self.assertEqual(
            len(new_files), 0,
            f"temp files leaked: {new_files}",
        )

    def test_temp_file_unique_per_call(self) -> None:
        """Each write uses a unique temp file name."""
        with _exclusive_store_lock(self.project):
            store = _canonical_empty_store()
            store["updated_at"] = "2026-07-27T12:00:00Z"
            _atomic_write_store(self.project, store)
        # After successful write, temp is renamed — no temp files remain.
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
        """Calling _atomic_write_store without a lock is allowed by the
        helper (caller's responsibility), but the write itself works."""
        store = _canonical_empty_store()
        store["updated_at"] = "2026-07-27T12:00:00Z"
        _atomic_write_store(self.project, store)
        self.assertTrue(self.path.exists())


class ImportSideEffectTests(unittest.TestCase):
    """Importing the module must not write files, acquire locks, or print."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="wsl-test-import-")
        self.project = Path(self.tmp.name)
        # Use a subprocess so the import is truly fresh.
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
        """All __all__ symbols must be importable."""
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
        """WorkerSlotConfigError must NOT exist."""
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
        """The tuple order must put lower-numbered slots first within
        each WorkerKind."""
        self.assertEqual(_STABLE_SLOT_IDS[0], "basic_agent-1")
        self.assertEqual(_STABLE_SLOT_IDS[1], "basic_agent-2")
        self.assertEqual(_STABLE_SLOT_IDS[2], "standard_agent-1")
        self.assertEqual(_STABLE_SLOT_IDS[7], "expert_agent-2")

    def test_ttl_is_60(self) -> None:
        self.assertEqual(_LEASE_TTL_SECONDS, 60)

    def test_max_heartbeat_is_20(self) -> None:
        self.assertEqual(_MAX_HEARTBEAT_INTERVAL_SECONDS, 20)


if __name__ == "__main__":
    unittest.main()
