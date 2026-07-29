"""TC-13.17b.1 — StateProvider YAML format compatibility targeted tests.

Covers:
- Real _to_yaml_str serialization for events/outbox produced by
  the canonical writer (control_plane_transition)
- json.loads() FAILS on canonical YAML output (proves JSON-only
  parsing is wrong)
- guard_results.inputs parsed as list[{"key":.., "value":..}]
- Event with real approval_gate guard
- TASK_DISPATCHED without matching outbox → InconsistentSnapshotError
- TASK_DISPATCHED + outbox with mismatched digest → InconsistentSnapshotError
- Symlink / reparse point rejection
- All public collections are tuple or frozen dataclass (no Mapping exposed)
- File disappearance → safe StateProvider exception
- Retains all original TC-13.17b coverage via helper-based test data
"""

from __future__ import annotations

import hashlib
import json
import locale
import os
import re
import stat
import subprocess
import sys
import tempfile
import types
import unittest
from dataclasses import fields
from pathlib import Path

_SKILL_ROOT = Path(__file__).resolve().parents[1]
_SKILL_SCRIPTS = _SKILL_ROOT / "skills" / "agentdesk" / "scripts"
sys.path.insert(0, str(_SKILL_SCRIPTS))

from state_provider import (  # noqa: E402
    AcceptanceEntry, DispatchInfo, EventEntry,
    GuardInput, GuardResult, MadRefEntry,
    ModelSelectionSnapshot, OutboxEntry, OutboxPayload,
    StateProvider,
    StateProviderError, StateProviderInconsistentSnapshotError,
    StateProviderInputError, StateProviderNotFoundError,
    StateProviderSchemaError, StateProviderSnapshotChangedError,
    StateSnapshot, TaskEntry, TaskTimestamps,
)

# Import the canonical YAML writer to produce real output
# We import the specific helper functions, NOT the write-end API.
# _to_yaml_str, _emit_yaml, _needs_quoting are pure serializers with
# no file I/O, no locks, no subprocess — importing them is safe.
from control_plane_transition import (  # noqa: E402
    _to_yaml_str, _needs_quoting,
)


# ── test helpers that use real writer serialization ──────────────────────


def _write_event_yaml(doc: dict, filename: str, dir_path: Path) -> bytes:
    """Serialize *doc* using the canonical YAML writer, write to disk,
    and return the raw bytes (for SHA-256 / digest computation)."""
    yaml_str = _to_yaml_str(doc)
    raw = yaml_str.encode("utf-8")
    (dir_path / filename).write_bytes(raw)
    return raw


def _write_outbox_yaml(doc: dict, filename: str, dir_path: Path) -> bytes:
    """Same as _write_event_yaml but for outbox."""
    yaml_str = _to_yaml_str(doc)
    raw = yaml_str.encode("utf-8")
    (dir_path / filename).write_bytes(raw)
    return raw


def _write_tasks_json(doc: dict, dir_path: Path) -> bytes:
    """Serialize tasks.yaml using json.dumps (matching the real writer)."""
    raw = (json.dumps(doc, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    (dir_path / "tasks.yaml").write_bytes(raw)
    return raw


def _write_mad_refs_json(doc: dict, dir_path: Path) -> bytes:
    """Serialize mad-refs.yaml (uses json.dumps in real mad_refs.py)."""
    raw = (json.dumps(doc, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    (dir_path.parent / "mad-refs.yaml").write_bytes(raw)
    return raw


# ── fixture builders (real serialization) ────────────────────────────────


def _make_tasks_doc(task_id: str = "TC-001", state: str = "draft",
                    revision: int = 1, attempt: int | None = None,
                    current_dispatch: dict | None = None,
                    acceptance_path: str | None = None) -> dict:
    return {
        "schema_version": "agentdesk.tasks/v2",
        "project_id": "test-project",
        "adoption_level": "standard",
        "updated_at": "2026-07-28T00:00:00Z",
        "pm_control": {"holder_id": "PM-001", "lease_epoch": 1, "mode": "manual"},
        "tasks": [{
            "task_id": task_id, "revision": revision,
            "task_card_path": f"docs/pm/tasks/{task_id}.md",
            "task_card_commit": "a" * 40, "state": state,
            "attempt": attempt, "current_dispatch": current_dispatch,
            "report_path": None, "granted_approval_ids": None,
            "delivery_state": None, "integration_state": None,
            "implementation_commit": None, "report_commit": None,
            "accepted_commit": None, "acceptance_path": acceptance_path,
            "integrated_commit": None, "blocked_reason": None,
            "blocked_kind": None, "blocked_owner": None,
            "unblock_condition": None, "review_after": None,
            "blocked_attempt_valid": None, "resume_state": None,
            "timestamps": {
                "created_at": "2026-07-28T00:00:00Z", "ready_at": None,
                "dispatched_at": None, "started_at": None,
                "delivered_at": None, "blocked_at": None,
                "accepted_at": None, "integrated_at": None,
                "updated_at": "2026-07-28T00:00:00Z",
            },
        }],
    }


def _make_event_doc(
    event_id: str = "EVT-20260728-0001",
    event_type: str = "TASK_SPECIFIED",
    task_id: str = "TC-001", revision: int = 1,
    attempt: int | None = None, dispatch_id: str | None = None,
    from_state: str = "draft", to_state: str = "ready",
    payload_digest: str | None = None,
    guard_results: list[dict] | None = None,
) -> dict:
    return {
        "schema_version": "agentdesk.state-event/v2",
        "event_id": event_id, "event_type": event_type,
        "task_id": task_id, "revision": revision, "attempt": attempt,
        "dispatch_id": dispatch_id, "from_state": from_state,
        "to_state": to_state, "lease_epoch": 1, "actor_role_id": "PM",
        "occurred_at": "2026-07-28T00:00:00Z",
        "source_message_id": None, "evidence_refs": [], "guard_results": guard_results or [],
        **({"payload_digest": payload_digest} if payload_digest else {}),
    }


def _make_outbox_doc(
    message_id: str = "MSG-20260728-0001",
    event_id: str = "EVT-20260728-0001",
    task_id: str = "TC-001", revision: int = 1,
    attempt: int = 1, dispatch_id: str = "DSP-001",
) -> dict:
    return {
        "schema_version": "agentdesk.outbox-message/v2",
        "message_id": message_id, "event_id": event_id,
        "message_type": "task.dispatch",
        "dedupe_key": f"{task_id}/r{revision}/a{attempt}/{dispatch_id}/task.dispatch",
        "task_id": task_id, "revision": revision, "attempt": attempt,
        "dispatch_id": dispatch_id, "destination_role_id": "WORKER-001",
        "created_at": "2026-07-28T00:00:00Z",
        "model_selection": {
            "required_model_tier": "standard",
            "required_model_capabilities": ["code_generation"],
            "model_binding_id": "binding-001",
            "selected_model_provider": "claude",
            "selected_model_id": "claude-sonnet-5",
            "selected_model_tier": "standard",
            "selected_deliberation_tier": "balanced",
            "selected_context_window_tokens": 200000,
            "selected_model_capabilities": ["code_generation"],
            "model_degradation_approval_id": None,
        },
        "payload": {
            "task_path": f"docs/pm/tasks/{task_id}.md",
            "task_card_commit": "a" * 40,
            "base_commit": "b" * 40,
            "branch": f"work/{task_id}",
            "report_path": f"docs/pm/reports/{task_id}-r{revision}-a{attempt}.md",
        },
    }


def _make_approval_gate_guard(scope: str = "dispatch",
                              approval_id: str = "APR-test") -> dict:
    """Build a guard_result matching the real approval_gate guard format."""
    return {
        "guard": "approval_gate",
        "inputs": [
            {"key": "scope", "value": scope},
            {"key": "approval_id", "value": approval_id},
        ],
        "result": "passed",
        "checked_at": "2026-07-28T00:00:00Z",
        "evidence_ref": f"docs/pm/approvals/EVT-approval-{approval_id}.yaml",
    }


def _make_acceptance_md(
    task_id: str = "TC-001", revision: int = 1, attempt: int = 1,
    review_n: int = 1, decision: str = "accepted",
    reviewed_dispatch_id: str = "DSP-001",
) -> str:
    return f"""---
schema_version: agentdesk.acceptance/v2
task_id: {task_id}
revision: {revision}
attempt: {attempt}
implementation_commit: {'a' * 40}
report_commit: {'b' * 40}
base_commit: {'c' * 40}
decision: {decision}
accepted_commit: {'a' * 40}
reviewed_dispatch_id: {reviewed_dispatch_id}
type: implementation
owner_approval:
  gate: none
  approval_ids: []
---
# {task_id} — Revision {revision}, Attempt {attempt}, Review {review_n}

Acceptance body content.
"""


def _make_mad_refs_doc(refs: list[dict] | None = None) -> dict:
    if refs is None:
        refs = []
    return {"schema_version": "agentdesk.mad-refs/v1",
            "updated_at": "2026-07-28T00:00:00Z", "refs": refs}


def _make_mad_ref_entry(task_id: str = "TC-001",
                        dispatch_id: str = "DSP-001") -> dict:
    return {
        "task_id": task_id, "dispatch_id": dispatch_id,
        "purpose": "planning", "deliberation_id": "delib-001",
        "depth": "balanced", "stdout_sha256": "a" * 64,
        "report_sha256": "b" * 64, "status": "completed",
        "archive_path": str(Path(tempfile.gettempdir()) / "archive"),
        "created_at": "2026-07-28T00:00:00Z",
    }


class _ProjectTree:
    """Temporary project tree with pre-created canonical directories."""

    def __init__(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        for d in ["docs/pm/state", "docs/pm/events", "docs/pm/outbox",
                   "docs/pm/acceptances", ".agentdesk/runtime"]:
            (self.root / d).mkdir(parents=True, exist_ok=True)

    def cleanup(self) -> None:
        self._tmp.cleanup()


def _setup_minimal_valid(tree: _ProjectTree,
                         include_mad_refs: bool = False,
                         include_acceptance: bool = True) -> str:
    """Set up a minimal valid project with real serialization.
    Returns the dispatch_id used.
    """
    did = "DSP-001"
    tasks_doc = _make_tasks_doc()
    _write_tasks_json(tasks_doc, tree.root / "docs" / "pm" / "state")

    # Outbox + event with matching digest
    out_doc = _make_outbox_doc(dispatch_id=did)
    out_raw = _write_outbox_yaml(out_doc, "MSG-001.yaml",
                                  tree.root / "docs" / "pm" / "outbox")
    digest = "sha256:" + hashlib.sha256(out_raw).hexdigest()
    ev_doc = _make_event_doc(
        event_id=out_doc["event_id"], event_type="TASK_DISPATCHED",
        dispatch_id=did, payload_digest=digest,
        from_state="ready", to_state="dispatched",
    )
    _write_event_yaml(ev_doc, "EVT-001.yaml", tree.root / "docs" / "pm" / "events")

    if include_acceptance:
        acc = _make_acceptance_md(reviewed_dispatch_id=did)
        (tree.root / "docs" / "pm" / "acceptances" /
         "TC-001-r1-a1-review1.md").write_text(acc, encoding="utf-8")

    if include_mad_refs:
        md = _make_mad_refs_doc([_make_mad_ref_entry(dispatch_id=did)])
        # Write mad-refs as JSON to the .agentdesk/runtime directory
        rt = tree.root / ".agentdesk" / "runtime"
        rt.mkdir(parents=True, exist_ok=True)
        raw = (json.dumps(md, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        (rt / "mad-refs.yaml").write_bytes(raw)

    return did


# ── tests ──────────────────────────────────────────────────────────────────


class TC1317bProductionTests(unittest.TestCase):
    """TC-13.17b.1 — YAML format compatibility & original coverage."""

    # ── 1. json.loads FAILS on canonical YAML ─────────────────────────────

    def test_json_loads_fails_on_canonical_event_yaml(self) -> None:
        """json.loads() must fail on canonical _to_yaml_str output.

        This proves that StateProvider must use a YAML parser, not JSON.
        """
        doc = _make_event_doc()
        yaml_str = _to_yaml_str(doc)
        with self.assertRaises(json.JSONDecodeError):
            json.loads(yaml_str)

    def test_json_loads_fails_on_canonical_outbox_yaml(self) -> None:
        """json.loads() must fail on canonical outbox YAML."""
        doc = _make_outbox_doc()
        yaml_str = _to_yaml_str(doc)
        with self.assertRaises(json.JSONDecodeError):
            json.loads(yaml_str)

    # ── 2. Real YAML round-trip ──────────────────────────────────────────

    def test_full_snapshot_with_real_yaml_serialization(self) -> None:
        """Full snapshot succeeds with events/outbox written by _to_yaml_str."""
        tree = _ProjectTree()
        try:
            _setup_minimal_valid(tree, include_mad_refs=True)
            snapshot = StateProvider(tree.root).snapshot()
            self.assertIsInstance(snapshot, StateSnapshot)
            self.assertEqual(len(snapshot.tasks), 1)
            self.assertEqual(len(snapshot.events), 1)
            self.assertEqual(snapshot.events[0].event_type, "TASK_DISPATCHED")
            self.assertEqual(len(snapshot.outbox), 1)
            self.assertIsNotNone(snapshot.mad_refs)
            self.assertEqual(len(snapshot.mad_refs), 1)
        finally:
            tree.cleanup()

    # ── 3. guard_results.inputs parsed as list[{"key":., "value":.}] ─────

    def test_guard_inputs_parsed_as_key_value_list(self) -> None:
        """guard_results.inputs is list[{"key":..., "value":...}] not dict."""
        tree = _ProjectTree()
        try:
            tasks_doc = _make_tasks_doc()
            _write_tasks_json(tasks_doc, tree.root / "docs" / "pm" / "state")

            gr = _make_approval_gate_guard("dispatch", "APR-001")
            ev_doc = _make_event_doc(guard_results=[gr])
            _write_event_yaml(ev_doc, "EVT-001.yaml",
                              tree.root / "docs" / "pm" / "events")

            snapshot = StateProvider(tree.root).snapshot()
            gr_parsed = snapshot.events[0].guard_results[0]
            self.assertIsInstance(gr_parsed, GuardResult)
            self.assertEqual(gr_parsed.guard, "approval_gate")
            self.assertIsInstance(gr_parsed.inputs, tuple)
            self.assertEqual(len(gr_parsed.inputs), 2)
            self.assertIsInstance(gr_parsed.inputs[0], GuardInput)
            self.assertEqual(gr_parsed.inputs[0].key, "scope")
            self.assertEqual(gr_parsed.inputs[0].value, "dispatch")
            self.assertEqual(gr_parsed.inputs[1].key, "approval_id")
            self.assertEqual(gr_parsed.inputs[1].value, "APR-001")
            self.assertEqual(gr_parsed.result, "passed")
        finally:
            tree.cleanup()

    # ── 4. Event with real approval_gate guard ───────────────────────────

    def test_event_with_approval_gate_guard(self) -> None:
        """Event with approval_gate guard parses correctly."""
        tree = _ProjectTree()
        try:
            tasks_doc = _make_tasks_doc()
            _write_tasks_json(tasks_doc, tree.root / "docs" / "pm" / "state")

            gr = {
                "guard": "approval_gate",
                "inputs": [
                    {"key": "scope", "value": "accept"},
                    {"key": "approval_id", "value": "APR-accept-001"},
                    {"key": "revision", "value": 1},
                    {"key": "attempt", "value": 1},
                ],
                "result": "passed",
                "checked_at": "2026-07-28T01:00:00Z",
                "evidence_ref": "docs/pm/approvals/EVT-APR-accept-001.yaml",
            }
            ev = _make_event_doc(guard_results=[gr])
            _write_event_yaml(ev, "EVT-001.yaml",
                              tree.root / "docs" / "pm" / "events")

            snapshot = StateProvider(tree.root).snapshot()
            parsed = snapshot.events[0].guard_results[0]
            self.assertEqual(parsed.guard, "approval_gate")
            self.assertEqual(len(parsed.inputs), 4)
            self.assertEqual(parsed.inputs[0].key, "scope")
            self.assertEqual(parsed.inputs[0].value, "accept")
            self.assertEqual(parsed.inputs[2].key, "revision")
            self.assertEqual(parsed.inputs[2].value, 1)
            self.assertEqual(parsed.result, "passed")
        finally:
            tree.cleanup()

    # ── 5. TASK_DISPATCHED without outbox → InconsistentSnapshotError ─────

    def test_task_dispatched_without_outbox_rejected(self) -> None:
        """TASK_DISPATCHED event with no matching outbox must fail."""
        tree = _ProjectTree()
        try:
            tasks_doc = _make_tasks_doc()
            _write_tasks_json(tasks_doc, tree.root / "docs" / "pm" / "state")

            ev = _make_event_doc(
                event_type="TASK_DISPATCHED",
                dispatch_id="DSP-NO-OUTBOX",
                payload_digest="sha256:" + "f" * 64,
                from_state="ready", to_state="dispatched",
            )
            _write_event_yaml(ev, "EVT-001.yaml",
                              tree.root / "docs" / "pm" / "events")
            # No outbox written

            with self.assertRaises(StateProviderInconsistentSnapshotError):
                StateProvider(tree.root).snapshot()
        finally:
            tree.cleanup()

    # ── 6. Digest mismatch → InconsistentSnapshotError ───────────────────

    def test_digest_mismatch_event_vs_outbox(self) -> None:
        """TASK_DISPATCHED digest not matching outbox bytes must fail."""
        tree = _ProjectTree()
        try:
            tasks_doc = _make_tasks_doc()
            _write_tasks_json(tasks_doc, tree.root / "docs" / "pm" / "state")

            out_doc = _make_outbox_doc()
            _write_outbox_yaml(out_doc, "MSG-001.yaml",
                               tree.root / "docs" / "pm" / "outbox")
            # Write event with WRONG digest
            ev = _make_event_doc(
                event_id=out_doc["event_id"],
                event_type="TASK_DISPATCHED",
                dispatch_id="DSP-001",
                payload_digest="sha256:" + "0" * 64,  # wrong digest
                from_state="ready", to_state="dispatched",
            )
            _write_event_yaml(ev, "EVT-001.yaml",
                              tree.root / "docs" / "pm" / "events")

            with self.assertRaises(StateProviderInconsistentSnapshotError):
                StateProvider(tree.root).snapshot()
        finally:
            tree.cleanup()

    # ── 7. Symlink / reparse point rejection ────────────────────────────

    def test_event_symlink_rejected(self) -> None:
        """Symlinked event file must be rejected."""
        tree = _ProjectTree()
        try:
            _setup_minimal_valid(tree)
            ev_dir = tree.root / "docs" / "pm" / "events"
            # Create a regular file, then a symlink pointing to it
            real_file = ev_dir / "EVT-OK.yaml"
            # Real event file already exists from _setup_minimal_valid
            link_path = ev_dir / "EVT-LINK.yaml"
            try:
                os.symlink(str(real_file), str(link_path))
                with self.assertRaises(StateProviderSchemaError):
                    StateProvider(tree.root).snapshot()
            except OSError:
                # Symlinks may require admin on Windows — skip test gracefully
                self.skipTest("symlink creation requires elevated privileges")
        finally:
            tree.cleanup()

    def test_acceptance_symlink_rejected(self) -> None:
        """Symlinked acceptance file must be rejected."""
        tree = _ProjectTree()
        try:
            _setup_minimal_valid(tree)
            acc_dir = tree.root / "docs" / "pm" / "acceptances"
            real_file = list(acc_dir.glob("*.md"))[0]
            link_path = acc_dir / "TC-001-r1-a1-review99.md"
            try:
                os.symlink(str(real_file), str(link_path))
                with self.assertRaises(StateProviderSchemaError):
                    StateProvider(tree.root).snapshot()
            except OSError:
                self.skipTest("symlink creation requires elevated privileges")
        finally:
            tree.cleanup()

    # ── 8. No Mapping/MappingProxyType on public dataclasses ─────────────

    def test_no_mapping_fields_on_public_dataclasses(self) -> None:
        """No public dataclass field has a Mapping/MappingProxyType type."""
        tree = _ProjectTree()
        try:
            _setup_minimal_valid(tree, include_mad_refs=True)
            snapshot = StateProvider(tree.root).snapshot()

            # Check all dataclasses for Mapping fields
            def _check_dc(obj: object, name: str) -> None:
                for f in fields(obj):
                    origin = getattr(f.type, "__origin__", None)
                    type_name = str(f.type)
                    self.assertFalse(
                        "Mapping" in type_name or "dict" in type_name.lower(),
                        f"{name}.{f.name} has type {f.type} — "
                        "no Mapping/dict allowed on public dataclass"
                    )

            _check_dc(snapshot, "StateSnapshot")
            _check_dc(snapshot.tasks[0], "TaskEntry")
            _check_dc(snapshot.tasks[0].timestamps, "TaskTimestamps")
            _check_dc(snapshot.events[0], "EventEntry")
            if snapshot.events[0].guard_results:
                _check_dc(snapshot.events[0].guard_results[0], "GuardResult")
            _check_dc(snapshot.outbox[0], "OutboxEntry")
            _check_dc(snapshot.outbox[0].payload, "OutboxPayload")
            _check_dc(snapshot.outbox[0].model_selection, "ModelSelectionSnapshot")
            _check_dc(snapshot.acceptances[0], "AcceptanceEntry")
            self.assertIsNotNone(snapshot.mad_refs)
            if snapshot.mad_refs:
                _check_dc(snapshot.mad_refs[0], "MadRefEntry")
        finally:
            tree.cleanup()

    # ── 9. File disappearance during read → safe exception ───────────────

    def test_file_disappears_during_read(self) -> None:
        """If an event file disappears between directory scan and read,
        a safe StateProvider error is raised (no path in message)."""
        tree = _ProjectTree()
        try:
            _setup_minimal_valid(tree)
            ev_dir = tree.root / "docs" / "pm" / "events"
            # Write an extra event file that we will delete
            ev2 = _make_event_doc(event_id="EVT-VANISH")
            _write_event_yaml(ev2, "EVT-VANISH.yaml", ev_dir)

            # Monkey-patch: delete the file after iterdir but before read_bytes
            orig_read = Path.read_bytes
            deleted = [False]

            def _delete_then_read(self_path: Path) -> bytes:
                if self_path.name == "EVT-VANISH.yaml" and not deleted[0]:
                    deleted[0] = True
                    self_path.unlink()
                    raise FileNotFoundError("simulated disappearance")
                return orig_read(self_path)

            Path.read_bytes = _delete_then_read  # type: ignore[method-assign]
            try:
                with self.assertRaises(StateProviderSnapshotChangedError):
                    StateProvider(tree.root).snapshot()
            finally:
                Path.read_bytes = orig_read
        finally:
            tree.cleanup()

    # ── 10. Tasks.yaml uses JSON (json.loads works) ──────────────────────

    def test_tasks_yaml_is_json_parseable(self) -> None:
        """tasks.yaml is written as JSON, so json.loads works.

        This is the one exception: the canonical writer for tasks.yaml
        uses json.dumps, not _to_yaml_str.
        """
        doc = _make_tasks_doc()
        raw = (json.dumps(doc, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
        # json.loads must succeed
        parsed = json.loads(raw)
        self.assertEqual(parsed["schema_version"], "agentdesk.tasks/v2")

    # ── 11. Deep immutability ────────────────────────────────────────────

    def test_collections_are_tuples(self) -> None:
        tree = _ProjectTree()
        try:
            _setup_minimal_valid(tree, include_mad_refs=True)
            snapshot = StateProvider(tree.root).snapshot()
            self.assertIsInstance(snapshot.tasks, tuple)
            self.assertIsInstance(snapshot.events, tuple)
            self.assertIsInstance(snapshot.outbox, tuple)
            self.assertIsInstance(snapshot.acceptances, tuple)
            self.assertIsInstance(snapshot.mad_refs, tuple)
        finally:
            tree.cleanup()

    def test_task_entry_is_frozen(self) -> None:
        tree = _ProjectTree()
        try:
            _setup_minimal_valid(tree)
            snapshot = StateProvider(tree.root).snapshot()
            with self.assertRaises(Exception):
                snapshot.tasks[0].state = "modified"  # type: ignore[misc]
        finally:
            tree.cleanup()

    # ── 12. Construction errors ──────────────────────────────────────────

    def test_construction_rejects_non_path(self) -> None:
        with self.assertRaises(StateProviderInputError):
            StateProvider("/not/a/path/object")  # type: ignore[arg-type]

    def test_construction_rejects_relative_path(self) -> None:
        with self.assertRaises(StateProviderInputError):
            StateProvider(Path("relative/path"))

    # ── 13. Missing optional mad-refs → None ─────────────────────────────

    def test_missing_mad_refs_returns_none(self) -> None:
        tree = _ProjectTree()
        try:
            _setup_minimal_valid(tree, include_mad_refs=False)
            snapshot = StateProvider(tree.root).snapshot()
            self.assertIsNone(snapshot.mad_refs)
        finally:
            tree.cleanup()

    # ── 14. Orphan event → Inconsistent ──────────────────────────────────

    def test_orphan_event_references_nonexistent_task(self) -> None:
        tree = _ProjectTree()
        try:
            _setup_minimal_valid(tree)
            ev = _make_event_doc(event_id="EVT-ORPHAN", task_id="TC-999")
            _write_event_yaml(ev, "EVT-ORPHAN.yaml",
                              tree.root / "docs" / "pm" / "events")
            with self.assertRaises(StateProviderInconsistentSnapshotError):
                StateProvider(tree.root).snapshot()
        finally:
            tree.cleanup()

    # ── 15. Duplicate IDs ────────────────────────────────────────────────

    def test_duplicate_event_id_rejected(self) -> None:
        tree = _ProjectTree()
        try:
            _setup_minimal_valid(tree)
            # Write a second event with the same event_id
            ev_existing = list((tree.root / "docs" / "pm" / "events").glob("*.yaml"))[0]
            ev2 = _make_event_doc(event_id="EVT-20260728-0001",
                                   event_type="TASK_CANCELLED",
                                   from_state="ready", to_state="cancelled")
            _write_event_yaml(ev2, "EVT-DUP.yaml",
                              tree.root / "docs" / "pm" / "events")
            with self.assertRaises(StateProviderSchemaError):
                StateProvider(tree.root).snapshot()
        finally:
            tree.cleanup()

    # ── 16. Exception hierarchy ──────────────────────────────────────────

    def test_exception_hierarchy(self) -> None:
        self.assertTrue(issubclass(StateProviderInputError, StateProviderError))
        self.assertTrue(issubclass(StateProviderNotFoundError, StateProviderError))
        self.assertTrue(issubclass(StateProviderSchemaError, StateProviderError))
        self.assertTrue(issubclass(StateProviderSnapshotChangedError, StateProviderError))
        self.assertTrue(issubclass(StateProviderInconsistentSnapshotError, StateProviderError))

    # ── 17. __all__ completeness ─────────────────────────────────────────

    def test_all_public_symbols_in_all(self) -> None:
        import state_provider as sp
        _import_artifacts = {
            "Any", "Path", "re", "json", "hashlib", "sys",
            "dataclass", "types", "pathlib", "os", "stat",
            "TYPE_CHECKING",
        }
        for name in dir(sp):
            if name.startswith("_"):
                continue
            if name in _import_artifacts:
                continue
            obj = getattr(sp, name)
            if isinstance(obj, type) or (callable(obj) and
                                         not isinstance(obj, types.ModuleType)):
                self.assertIn(name, sp.__all__,
                              f"{name} is public but not in __all__")

    # ── 18. MadRefEntry field order ──────────────────────────────────────

    def test_mad_ref_entry_field_order(self) -> None:
        expected = [
            "task_id", "dispatch_id", "purpose", "deliberation_id",
            "depth", "stdout_sha256", "report_sha256", "status",
            "archive_path", "created_at",
        ]
        self.assertEqual([f.name for f in fields(MadRefEntry)], expected)

    # ── 19. Locale-independent sort ──────────────────────────────────────

    def test_sort_is_locale_independent(self) -> None:
        tree = _ProjectTree()
        try:
            tasks_doc = _make_tasks_doc()
            _write_tasks_json(tasks_doc, tree.root / "docs" / "pm" / "state")
            for name in ("EVT-a", "EVT-Z", "EVT-b"):
                ev = _make_event_doc(event_id=name)
                _write_event_yaml(ev, f"{name}.yaml",
                                  tree.root / "docs" / "pm" / "events")
            # Try C locale
            saved = None
            try:
                saved = locale.setlocale(locale.LC_ALL, "C")
            except locale.Error:
                try:
                    saved = locale.setlocale(locale.LC_ALL, "")
                except locale.Error:
                    pass
            try:
                snapshot = StateProvider(tree.root).snapshot()
                ev_names = [e.event_id for e in snapshot.events
                            if e.event_id in ("EVT-Z", "EVT-a", "EVT-b")]
                if len(ev_names) >= 2:
                    expected_order = sorted(ev_names, key=lambda n: n.encode("utf-8"))
                    self.assertEqual(ev_names, expected_order)
            finally:
                if saved is not None:
                    try:
                        locale.setlocale(locale.LC_ALL, "")
                    except locale.Error:
                        pass
        finally:
            tree.cleanup()

    # ── 20. No subprocess / no lock ─────────────────────────────────────

    def test_no_subprocess_imports(self) -> None:
        src = (_SKILL_SCRIPTS / "state_provider.py").read_text(encoding="utf-8")
        self.assertNotIn("import subprocess", src)
        self.assertNotIn("from subprocess", src)

    def test_no_write_end_imports(self) -> None:
        src = (_SKILL_SCRIPTS / "state_provider.py").read_text(encoding="utf-8")
        self.assertNotIn("import control_plane_transition", src)
        self.assertNotIn("from control_plane_transition import", src)
        self.assertNotIn("import worker_adapter", src)
        self.assertNotIn("from worker_adapter", src)

    # ── 21. acceptance_path must exist for accepted/integrated ────────────

    def test_acceptance_path_must_exist_for_accepted_task(self) -> None:
        tree = _ProjectTree()
        try:
            did = "DSP-ACC"
            tasks_doc = _make_tasks_doc(
                state="accepted",
                acceptance_path="docs/pm/acceptances/TC-001-r1-a1-review99.md",
            )
            _write_tasks_json(tasks_doc, tree.root / "docs" / "pm" / "state")
            out_doc = _make_outbox_doc(dispatch_id=did,
                                       event_id="EVT-ACC")
            out_raw = _write_outbox_yaml(out_doc, "MSG-ACC.yaml",
                                          tree.root / "docs" / "pm" / "outbox")
            digest = "sha256:" + hashlib.sha256(out_raw).hexdigest()
            ev = _make_event_doc(
                event_id=out_doc["event_id"], event_type="TASK_DISPATCHED",
                dispatch_id=did, payload_digest=digest,
                from_state="ready", to_state="dispatched",
            )
            _write_event_yaml(ev, "EVT-ACC.yaml",
                              tree.root / "docs" / "pm" / "events")
            # acceptance file missing
            with self.assertRaises(StateProviderInconsistentSnapshotError):
                StateProvider(tree.root).snapshot()
        finally:
            tree.cleanup()

    # ── 22. Cancelled timestamp read (TC-13.17b.3) ────────────────────────

    def test_snapshot_accepts_cancelled_at_and_defaults_superseded_at(self) -> None:
        """cancelled_at is read; superseded_at defaults to None when absent."""
        tree = _ProjectTree()
        try:
            did = "DSP-CANCEL"
            ts = {
                "created_at": "2026-07-28T00:00:00Z", "ready_at": None,
                "dispatched_at": "2026-07-28T00:00:01Z", "started_at": None,
                "delivered_at": None, "blocked_at": "2026-07-28T00:00:02Z",
                "accepted_at": None, "integrated_at": None,
                "cancelled_at": "2026-07-28T00:00:03Z",
                "updated_at": "2026-07-28T00:00:03Z",
            }
            tasks_doc = _make_tasks_doc(state="cancelled")
            tasks_doc["tasks"][0]["timestamps"] = ts
            _write_tasks_json(tasks_doc, tree.root / "docs" / "pm" / "state")

            out_doc = _make_outbox_doc(dispatch_id=did, event_id="EVT-CANCEL")
            out_raw = _write_outbox_yaml(out_doc, "MSG-CANCEL.yaml",
                                          tree.root / "docs" / "pm" / "outbox")
            digest = "sha256:" + hashlib.sha256(out_raw).hexdigest()
            ev = _make_event_doc(
                event_id=out_doc["event_id"], event_type="TASK_DISPATCHED",
                dispatch_id=did, payload_digest=digest,
                from_state="ready", to_state="dispatched",
            )
            _write_event_yaml(ev, "EVT-CANCEL.yaml",
                              tree.root / "docs" / "pm" / "events")

            snapshot = StateProvider(tree.root).snapshot()
            self.assertEqual(len(snapshot.tasks), 1)
            task = snapshot.tasks[0]
            self.assertEqual(task.state, "cancelled")
            self.assertEqual(task.timestamps.cancelled_at, "2026-07-28T00:00:03Z")
            self.assertIsNone(task.timestamps.superseded_at)

            # Verify frozen
            with self.assertRaises(Exception):
                task.timestamps.cancelled_at = "modified"  # type: ignore[misc]
        finally:
            tree.cleanup()

    # ── 23. Superseded timestamp read (TC-13.17b.3) ───────────────────────

    def test_snapshot_accepts_superseded_at_and_defaults_cancelled_at(self) -> None:
        """superseded_at is read; cancelled_at defaults to None when absent."""
        tree = _ProjectTree()
        try:
            did = "DSP-SUPERSEDE"
            ts = {
                "created_at": "2026-07-28T00:00:00Z", "ready_at": None,
                "dispatched_at": "2026-07-28T00:00:01Z", "started_at": None,
                "delivered_at": None, "blocked_at": None,
                "accepted_at": None, "integrated_at": None,
                "superseded_at": "2026-07-28T00:00:04Z",
                "updated_at": "2026-07-28T00:00:04Z",
            }
            tasks_doc = _make_tasks_doc(state="superseded")
            tasks_doc["tasks"][0]["timestamps"] = ts
            _write_tasks_json(tasks_doc, tree.root / "docs" / "pm" / "state")

            out_doc = _make_outbox_doc(dispatch_id=did, event_id="EVT-SUPERSEDE")
            out_raw = _write_outbox_yaml(out_doc, "MSG-SUPERSEDE.yaml",
                                          tree.root / "docs" / "pm" / "outbox")
            digest = "sha256:" + hashlib.sha256(out_raw).hexdigest()
            ev = _make_event_doc(
                event_id=out_doc["event_id"], event_type="TASK_DISPATCHED",
                dispatch_id=did, payload_digest=digest,
                from_state="ready", to_state="dispatched",
            )
            _write_event_yaml(ev, "EVT-SUPERSEDE.yaml",
                              tree.root / "docs" / "pm" / "events")

            snapshot = StateProvider(tree.root).snapshot()
            self.assertEqual(len(snapshot.tasks), 1)
            task = snapshot.tasks[0]
            self.assertEqual(task.state, "superseded")
            self.assertEqual(task.timestamps.superseded_at, "2026-07-28T00:00:04Z")
            self.assertIsNone(task.timestamps.cancelled_at)
        finally:
            tree.cleanup()

    # ── 24. Terminal timestamp type validation (TC-13.17b.3) ──────────────

    def test_terminal_timestamp_types_remain_fail_closed(self) -> None:
        """Non-string cancelled_at/superseded_at and unknown keys are rejected."""
        tree = _ProjectTree()
        try:
            did = "DSP-TERM-TYPE"

            # ── non-string cancelled_at ──
            ts_bad_cancelled = {
                "created_at": "2026-07-28T00:00:00Z", "ready_at": None,
                "dispatched_at": "2026-07-28T00:00:01Z", "started_at": None,
                "delivered_at": None, "blocked_at": "2026-07-28T00:00:02Z",
                "accepted_at": None, "integrated_at": None,
                "cancelled_at": 12345,
                "updated_at": "2026-07-28T00:00:03Z",
            }
            tasks_doc = _make_tasks_doc(state="cancelled")
            tasks_doc["tasks"][0]["timestamps"] = ts_bad_cancelled
            _write_tasks_json(tasks_doc, tree.root / "docs" / "pm" / "state")
            out_doc = _make_outbox_doc(dispatch_id=did, event_id="EVT-TYPE1")
            out_raw = _write_outbox_yaml(out_doc, "MSG-TYPE1.yaml",
                                          tree.root / "docs" / "pm" / "outbox")
            digest = "sha256:" + hashlib.sha256(out_raw).hexdigest()
            ev = _make_event_doc(
                event_id=out_doc["event_id"], event_type="TASK_DISPATCHED",
                dispatch_id=did, payload_digest=digest,
                from_state="ready", to_state="dispatched",
            )
            _write_event_yaml(ev, "EVT-TYPE1.yaml",
                              tree.root / "docs" / "pm" / "events")
            with self.assertRaises(StateProviderSchemaError):
                StateProvider(tree.root).snapshot()
            tree.cleanup()

            # ── non-string superseded_at ──
            tree = _ProjectTree()
            ts_bad_superseded = {
                "created_at": "2026-07-28T00:00:00Z", "ready_at": None,
                "dispatched_at": "2026-07-28T00:00:01Z", "started_at": None,
                "delivered_at": None, "blocked_at": None,
                "accepted_at": None, "integrated_at": None,
                "superseded_at": True,
                "updated_at": "2026-07-28T00:00:04Z",
            }
            tasks_doc = _make_tasks_doc(state="superseded")
            tasks_doc["tasks"][0]["timestamps"] = ts_bad_superseded
            _write_tasks_json(tasks_doc, tree.root / "docs" / "pm" / "state")
            out2 = _make_outbox_doc(dispatch_id=did, event_id="EVT-TYPE2")
            out_raw2 = _write_outbox_yaml(out2, "MSG-TYPE2.yaml",
                                           tree.root / "docs" / "pm" / "outbox")
            digest2 = "sha256:" + hashlib.sha256(out_raw2).hexdigest()
            ev2 = _make_event_doc(
                event_id=out2["event_id"], event_type="TASK_DISPATCHED",
                dispatch_id=did, payload_digest=digest2,
                from_state="ready", to_state="dispatched",
            )
            _write_event_yaml(ev2, "EVT-TYPE2.yaml",
                              tree.root / "docs" / "pm" / "events")
            with self.assertRaises(StateProviderSchemaError):
                StateProvider(tree.root).snapshot()
            tree.cleanup()

            # ── unknown timestamp key still rejected ──
            tree = _ProjectTree()
            ts_unknown_key = {
                "created_at": "2026-07-28T00:00:00Z", "ready_at": None,
                "dispatched_at": None, "started_at": None,
                "delivered_at": None, "blocked_at": None,
                "accepted_at": None, "integrated_at": None,
                "unknown_extra": "should be rejected",
                "updated_at": "2026-07-28T00:00:00Z",
            }
            tasks_doc = _make_tasks_doc(state="draft")
            tasks_doc["tasks"][0]["timestamps"] = ts_unknown_key
            _write_tasks_json(tasks_doc, tree.root / "docs" / "pm" / "state")
            # Minimal event to allow read — dispatch anyway
            out3 = _make_outbox_doc(dispatch_id=did, event_id="EVT-TYPE3")
            out_raw3 = _write_outbox_yaml(out3, "MSG-TYPE3.yaml",
                                           tree.root / "docs" / "pm" / "outbox")
            digest3 = "sha256:" + hashlib.sha256(out_raw3).hexdigest()
            ev3 = _make_event_doc(
                event_id=out3["event_id"], event_type="TASK_DISPATCHED",
                dispatch_id=did, payload_digest=digest3,
                from_state="ready", to_state="dispatched",
            )
            _write_event_yaml(ev3, "EVT-TYPE3.yaml",
                              tree.root / "docs" / "pm" / "events")
            with self.assertRaises(StateProviderSchemaError):
                StateProvider(tree.root).snapshot()
            tree.cleanup()

            # ── empty cancelled_at ──
            tree = _ProjectTree()
            ts_empty_cancelled = {
                "created_at": "2026-07-28T00:00:00Z", "ready_at": None,
                "dispatched_at": "2026-07-28T00:00:01Z", "started_at": None,
                "delivered_at": None, "blocked_at": "2026-07-28T00:00:02Z",
                "accepted_at": None, "integrated_at": None,
                "cancelled_at": "",
                "updated_at": "2026-07-28T00:00:03Z",
            }
            tasks_doc = _make_tasks_doc(state="cancelled")
            tasks_doc["tasks"][0]["timestamps"] = ts_empty_cancelled
            _write_tasks_json(tasks_doc, tree.root / "docs" / "pm" / "state")
            out4 = _make_outbox_doc(dispatch_id=did, event_id="EVT-TYPE4")
            out_raw4 = _write_outbox_yaml(out4, "MSG-TYPE4.yaml",
                                           tree.root / "docs" / "pm" / "outbox")
            digest4 = "sha256:" + hashlib.sha256(out_raw4).hexdigest()
            ev4 = _make_event_doc(
                event_id=out4["event_id"], event_type="TASK_DISPATCHED",
                dispatch_id=did, payload_digest=digest4,
                from_state="ready", to_state="dispatched",
            )
            _write_event_yaml(ev4, "EVT-TYPE4.yaml",
                              tree.root / "docs" / "pm" / "events")
            with self.assertRaises(StateProviderSchemaError):
                StateProvider(tree.root).snapshot()
            tree.cleanup()

            # ── empty superseded_at ──
            tree = _ProjectTree()
            ts_empty_superseded = {
                "created_at": "2026-07-28T00:00:00Z", "ready_at": None,
                "dispatched_at": "2026-07-28T00:00:01Z", "started_at": None,
                "delivered_at": None, "blocked_at": None,
                "accepted_at": None, "integrated_at": None,
                "superseded_at": "",
                "updated_at": "2026-07-28T00:00:04Z",
            }
            tasks_doc = _make_tasks_doc(state="superseded")
            tasks_doc["tasks"][0]["timestamps"] = ts_empty_superseded
            _write_tasks_json(tasks_doc, tree.root / "docs" / "pm" / "state")
            out5 = _make_outbox_doc(dispatch_id=did, event_id="EVT-TYPE5")
            out_raw5 = _write_outbox_yaml(out5, "MSG-TYPE5.yaml",
                                           tree.root / "docs" / "pm" / "outbox")
            digest5 = "sha256:" + hashlib.sha256(out_raw5).hexdigest()
            ev5 = _make_event_doc(
                event_id=out5["event_id"], event_type="TASK_DISPATCHED",
                dispatch_id=did, payload_digest=digest5,
                from_state="ready", to_state="dispatched",
            )
            _write_event_yaml(ev5, "EVT-TYPE5.yaml",
                              tree.root / "docs" / "pm" / "events")
            with self.assertRaises(StateProviderSchemaError):
                StateProvider(tree.root).snapshot()
        finally:
            tree.cleanup()


def load_tests(loader: unittest.TestLoader, standard_tests: unittest.TestSuite,
               pattern: str | None) -> unittest.TestSuite:  # type: ignore[override]
    suite = unittest.TestSuite()
    suite.addTests(loader.loadTestsFromTestCase(TC1317bProductionTests))
    return suite
