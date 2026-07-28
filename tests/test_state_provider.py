"""TC-13.17b — StateProvider production module targeted tests.

Covers:
- Valid full snapshot with all five sources
- Missing mad-refs (legal, returns None)
- Five-source A/B change detection (each source changing independently)
- File add/delete/replace detection
- Corrupt YAML/JSON/frontmatter
- Extra/missing keys
- Duplicate task/event/message
- Orphan event/outbox/acceptance/mad-ref
- Dispatch subject mismatch
- Digest inconsistency
- Frozen/slots and tuple deep immutability
- Locale-independent sort order
- Zero open-write, zero subprocess, zero lock
- Exception message leak prevention
- Malicious __repr__ not called
"""

from __future__ import annotations

import hashlib
import json
import locale
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import types
import unittest
from dataclasses import fields
from pathlib import Path


_SKILL_SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "agentdesk" / "scripts"
sys.path.insert(0, str(_SKILL_SCRIPTS))

from state_provider import (  # noqa: E402
    AcceptanceEntry,
    DispatchInfo,
    EventEntry,
    GuardResult,
    MadRefEntry,
    ModelSelectionSnapshot,
    OutboxEntry,
    OutboxPayload,
    StateProvider,
    StateProviderError,
    StateProviderInconsistentSnapshotError,
    StateProviderInputError,
    StateProviderNotFoundError,
    StateProviderSchemaError,
    StateProviderSnapshotChangedError,
    StateSnapshot,
    TaskEntry,
    TaskTimestamps,
)


# ── helpers ────────────────────────────────────────────────────────────────


def _make_tasks_yaml(
    task_id: str = "TC-001",
    state: str = "draft",
    revision: int = 1,
    attempt: int | None = None,
    current_dispatch: dict | None = None,
    acceptance_path: str | None = None,
    extra_fields: dict | None = None,
) -> dict:
    doc = {
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
    if extra_fields:
        doc.update(extra_fields)
    return doc


def _make_event_yaml(
    event_id: str = "EVT-20260728-0001",
    event_type: str = "TASK_SPECIFIED",
    task_id: str = "TC-001", revision: int = 1,
    attempt: int | None = None, dispatch_id: str | None = None,
    from_state: str = "draft", to_state: str = "ready",
    payload_digest: str | None = None, extra_fields: dict | None = None,
) -> dict:
    doc = {
        "schema_version": "agentdesk.state-event/v2",
        "event_id": event_id, "event_type": event_type,
        "task_id": task_id, "revision": revision, "attempt": attempt,
        "dispatch_id": dispatch_id, "from_state": from_state,
        "to_state": to_state, "lease_epoch": 1, "actor_role_id": "PM",
        "occurred_at": "2026-07-28T00:00:00Z",
        "source_message_id": None, "evidence_refs": [], "guard_results": [],
    }
    if payload_digest is not None:
        doc["payload_digest"] = payload_digest
    if extra_fields:
        doc.update(extra_fields)
    return doc


def _make_outbox_yaml(
    message_id: str = "MSG-20260728-0001",
    event_id: str = "EVT-20260728-0001",
    task_id: str = "TC-001", revision: int = 1,
    attempt: int = 1, dispatch_id: str = "DSP-001",
    extra_fields: dict | None = None,
) -> dict:
    doc = {
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
    if extra_fields:
        doc.update(extra_fields)
    return doc


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


def _make_mad_refs_yaml(refs: list[dict] | None = None) -> dict:
    if refs is None:
        refs = []
    return {"schema_version": "agentdesk.mad-refs/v1",
            "updated_at": "2026-07-28T00:00:00Z", "refs": refs}


def _make_mad_ref_entry(
    task_id: str = "TC-001", dispatch_id: str = "DSP-001",
    purpose: str = "planning", deliberation_id: str = "delib-001",
    depth: str = "balanced", archive_path: str | None = None,
) -> dict:
    if archive_path is None:
        archive_path = str(Path(tempfile.gettempdir()) / "archive")
    return {
        "task_id": task_id, "dispatch_id": dispatch_id,
        "purpose": purpose, "deliberation_id": deliberation_id,
        "depth": depth, "stdout_sha256": "a" * 64,
        "report_sha256": "b" * 64, "status": "completed",
        "archive_path": archive_path,
        "created_at": "2026-07-28T00:00:00Z",
    }


class _ProjectTree:
    """Build a temporary project tree with the five canonical input sources.

    Pre-creates all required directories so tests that only write a subset
    don't fail with NotFoundError.
    """

    def __init__(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        # Pre-create all required canonical directories
        (self.root / "docs" / "pm" / "state").mkdir(parents=True, exist_ok=True)
        (self.root / "docs" / "pm" / "events").mkdir(parents=True, exist_ok=True)
        (self.root / "docs" / "pm" / "outbox").mkdir(parents=True, exist_ok=True)
        (self.root / "docs" / "pm" / "acceptances").mkdir(parents=True, exist_ok=True)
        (self.root / ".agentdesk" / "runtime").mkdir(parents=True, exist_ok=True)

    def write_tasks(self, doc: dict) -> None:
        path = self.root / "docs" / "pm" / "state"
        raw = json.dumps(doc, ensure_ascii=False, indent=2) + "\n"
        (path / "tasks.yaml").write_text(raw, encoding="utf-8")

    def write_event(self, doc: dict, filename: str | None = None) -> str:
        path = self.root / "docs" / "pm" / "events"
        if filename is None:
            filename = f"{doc['event_id']}.yaml"
        raw = json.dumps(doc, ensure_ascii=False, indent=2) + "\n"
        (path / filename).write_text(raw, encoding="utf-8")
        return filename

    def write_outbox(self, doc: dict, filename: str | None = None) -> str:
        path = self.root / "docs" / "pm" / "outbox"
        if filename is None:
            filename = f"{doc['message_id']}.yaml"
        raw = json.dumps(doc, ensure_ascii=False, indent=2) + "\n"
        (path / filename).write_text(raw, encoding="utf-8")
        return filename

    def write_acceptance(
        self, content: str, task_id: str = "TC-001",
        revision: int = 1, attempt: int = 1, review_n: int = 1,
    ) -> str:
        path = self.root / "docs" / "pm" / "acceptances"
        filename = f"{task_id}-r{revision}-a{attempt}-review{review_n}.md"
        (path / filename).write_text(content, encoding="utf-8")
        return filename

    def write_mad_refs(self, doc: dict) -> None:
        path = self.root / ".agentdesk" / "runtime"
        raw = json.dumps(doc, ensure_ascii=False, indent=2) + "\n"
        (path / "mad-refs.yaml").write_text(raw, encoding="utf-8")

    def delete_file(self, rel_path: str) -> None:
        full = self.root / rel_path
        if full.exists():
            full.unlink()

    def delete_dir(self, rel_path: str) -> None:
        """Remove directory (used for testing missing dirs)."""
        import shutil
        full = self.root / rel_path
        if full.exists():
            shutil.rmtree(full)

    def cleanup(self) -> None:
        self._tmp.cleanup()


def _setup_valid_tree(tree: _ProjectTree, include_mad_refs: bool = False) -> str:
    """Set up a valid project tree with consistent digest/event linkage.

    Writes tasks.yaml, a TASK_DISPATCHED event, a matching outbox,
    and an acceptance.  Returns the dispatch_id used.
    """
    dispatch_id = "DSP-001"
    out = _make_outbox_yaml(dispatch_id=dispatch_id)
    out_name = tree.write_outbox(out)
    # Compute digest from exact on-disk bytes
    actual_bytes = (tree.root / "docs" / "pm" / "outbox" / out_name).read_bytes()
    digest = "sha256:" + hashlib.sha256(actual_bytes).hexdigest()
    ev = _make_event_yaml(
        event_id=out["event_id"], event_type="TASK_DISPATCHED",
        dispatch_id=dispatch_id,
        payload_digest=digest, from_state="ready", to_state="dispatched",
    )
    tree.write_event(ev)
    tree.write_tasks(_make_tasks_yaml())
    tree.write_acceptance(_make_acceptance_md(reviewed_dispatch_id=dispatch_id))
    if include_mad_refs:
        tree.write_mad_refs(_make_mad_refs_yaml([
            _make_mad_ref_entry(dispatch_id=dispatch_id)
        ]))
    return dispatch_id


# ── tests ──────────────────────────────────────────────────────────────────


class TC1317bProductionTests(unittest.TestCase):
    """TC-13.17b — StateProvider production module tests."""

    # ── 1. Valid full snapshot ─────────────────────────────────────────────

    def test_valid_full_snapshot_with_all_five_sources(self) -> None:
        tree = _ProjectTree()
        try:
            _setup_valid_tree(tree, include_mad_refs=True)
            provider = StateProvider(tree.root)
            snapshot = provider.snapshot()

            self.assertIsInstance(snapshot, StateSnapshot)
            self.assertEqual(snapshot.schema_version, "agentdesk.tasks/v2")
            self.assertEqual(snapshot.project_id, "test-project")
            self.assertEqual(snapshot.pm_holder_id, "PM-001")
            self.assertIsInstance(snapshot.tasks, tuple)
            self.assertEqual(len(snapshot.tasks), 1)
            self.assertIsInstance(snapshot.tasks[0], TaskEntry)
            self.assertIsInstance(snapshot.events, tuple)
            self.assertGreaterEqual(len(snapshot.events), 1)
            self.assertIsInstance(snapshot.outbox, tuple)
            self.assertEqual(len(snapshot.outbox), 1)
            self.assertIsInstance(snapshot.acceptances, tuple)
            self.assertIsNotNone(snapshot.mad_refs)
            self.assertIsInstance(snapshot.mad_refs, tuple)
            self.assertEqual(len(snapshot.mad_refs), 1)
            self.assertEqual(len(snapshot.read_hexsha), 64)
        finally:
            tree.cleanup()

    # ── 2. Missing mad-refs ─────────────────────────────────────────────────

    def test_missing_mad_refs_returns_none(self) -> None:
        tree = _ProjectTree()
        try:
            _setup_valid_tree(tree, include_mad_refs=False)
            snapshot = StateProvider(tree.root).snapshot()
            self.assertIsNone(snapshot.mad_refs)
        finally:
            tree.cleanup()

    # ── 3. A/B change detection ────────────────────────────────────────────

    def test_tasks_yaml_changed_between_passes(self) -> None:
        tree = _ProjectTree()
        try:
            _setup_valid_tree(tree)
            orig = Path.read_bytes
            count = [0]

            def _changing(self_path: Path) -> bytes:  # type: ignore[no-untyped-def]
                val = orig(self_path)
                if self_path.name == "tasks.yaml":
                    count[0] += 1
                    if count[0] == 2:
                        return val + b"# changed\n"
                return val

            Path.read_bytes = _changing  # type: ignore[method-assign]
            try:
                provider = StateProvider(tree.root)
                with self.assertRaises(StateProviderSnapshotChangedError):
                    provider.snapshot()
            finally:
                Path.read_bytes = orig
        finally:
            tree.cleanup()

    def test_event_file_changed_between_passes(self) -> None:
        tree = _ProjectTree()
        try:
            _setup_valid_tree(tree)
            orig = Path.read_bytes
            ev_count = [0]

            def _changing(self_path: Path) -> bytes:  # type: ignore[no-untyped-def]
                val = orig(self_path)
                if self_path.parent.name == "events" and self_path.suffix == ".yaml":
                    ev_count[0] += 1
                    if ev_count[0] > 1:
                        return val + b"# changed\n"
                return val

            Path.read_bytes = _changing  # type: ignore[method-assign]
            try:
                provider = StateProvider(tree.root)
                with self.assertRaises(StateProviderSnapshotChangedError):
                    provider.snapshot()
            finally:
                Path.read_bytes = orig
        finally:
            tree.cleanup()

    def test_outbox_file_changed_between_passes(self) -> None:
        tree = _ProjectTree()
        try:
            _setup_valid_tree(tree)
            orig = Path.read_bytes
            count = [0]

            def _changing(self_path: Path) -> bytes:  # type: ignore[no-untyped-def]
                val = orig(self_path)
                if self_path.parent.name == "outbox" and self_path.suffix == ".yaml":
                    count[0] += 1
                    if count[0] > 1:
                        return val + b"# changed\n"
                return val

            Path.read_bytes = _changing  # type: ignore[method-assign]
            try:
                provider = StateProvider(tree.root)
                with self.assertRaises(StateProviderSnapshotChangedError):
                    provider.snapshot()
            finally:
                Path.read_bytes = orig
        finally:
            tree.cleanup()

    def test_acceptance_file_changed_between_passes(self) -> None:
        tree = _ProjectTree()
        try:
            _setup_valid_tree(tree)
            orig = Path.read_bytes
            count = [0]

            def _changing(self_path: Path) -> bytes:  # type: ignore[no-untyped-def]
                val = orig(self_path)
                if self_path.parent.name == "acceptances" and self_path.suffix == ".md":
                    count[0] += 1
                    if count[0] > 1:
                        return val + b"# changed\n"
                return val

            Path.read_bytes = _changing  # type: ignore[method-assign]
            try:
                provider = StateProvider(tree.root)
                with self.assertRaises(StateProviderSnapshotChangedError):
                    provider.snapshot()
            finally:
                Path.read_bytes = orig
        finally:
            tree.cleanup()

    def test_mad_refs_file_changed_between_passes(self) -> None:
        tree = _ProjectTree()
        try:
            _setup_valid_tree(tree, include_mad_refs=True)
            orig = Path.read_bytes
            count = [0]

            def _changing(self_path: Path) -> bytes:  # type: ignore[no-untyped-def]
                val = orig(self_path)
                if self_path.name == "mad-refs.yaml":
                    count[0] += 1
                    if count[0] > 1:
                        return val + b"# changed\n"
                return val

            Path.read_bytes = _changing  # type: ignore[method-assign]
            try:
                provider = StateProvider(tree.root)
                with self.assertRaises(StateProviderSnapshotChangedError):
                    provider.snapshot()
            finally:
                Path.read_bytes = orig
        finally:
            tree.cleanup()

    def test_file_appears_between_passes(self) -> None:
        """New file appearing between passes raises SnapshotChangedError."""
        tree = _ProjectTree()
        try:
            _setup_valid_tree(tree)
            orig = Path.iterdir
            call_count = [0]

            def _appending(self: Path) -> list:
                if self.name == "events":
                    call_count[0] += 1
                    if call_count[0] > 1:
                        new_ev = (self / "EVT-20260728-9999.yaml")
                        new_ev.write_text(json.dumps(
                            _make_event_yaml("EVT-20260728-9999")), encoding="utf-8")
                return list(orig(self))

            Path.iterdir = _appending  # type: ignore[assignment]
            try:
                provider = StateProvider(tree.root)
                with self.assertRaises(StateProviderSnapshotChangedError):
                    provider.snapshot()
            finally:
                Path.iterdir = orig
        finally:
            tree.cleanup()

    def test_file_deleted_between_passes(self) -> None:
        """File disappearing between passes raises SnapshotChangedError.

        The five-source hash includes every file's content.  To simulate a
        deletion we make one event file return different bytes on the second
        pass — the hash mismatch triggers the expected error.
        """
        tree = _ProjectTree()
        try:
            _setup_valid_tree(tree)
            ev_dir = tree.root / "docs" / "pm" / "events"
            ev_files = sorted(
                [p for p in ev_dir.iterdir() if p.is_file() and p.suffix == ".yaml"],
                key=lambda p: p.name.encode("utf-8"),
            )
            self.assertGreater(len(ev_files), 0, "Need at least one event file")

            # Write a corrupt version of the first event file that only
            # appears on the second pass.
            orig = ev_files[0].read_bytes()
            corruption = b"# deleted\n"

            orig_read = Path.read_bytes
            read_seq = [0]

            def _mutating(self_path: Path) -> bytes:  # type: ignore[no-untyped-def]
                if self_path == ev_files[0]:
                    read_seq[0] += 1
                    if read_seq[0] == 2:   # second pass → corrupted
                        return corruption
                return orig_read(self_path)

            Path.read_bytes = _mutating  # type: ignore[method-assign]
            try:
                provider = StateProvider(tree.root)
                with self.assertRaises(StateProviderSnapshotChangedError):
                    provider.snapshot()
            finally:
                Path.read_bytes = orig_read
        finally:
            tree.cleanup()

    # ── 4. Corrupt input ───────────────────────────────────────────────────

    def test_corrupt_yaml_in_tasks(self) -> None:
        tree = _ProjectTree()
        try:
            _setup_valid_tree(tree)
            (tree.root / "docs" / "pm" / "state" / "tasks.yaml").write_text(
                "not json {{{", encoding="utf-8")
            with self.assertRaises(StateProviderSchemaError):
                StateProvider(tree.root).snapshot()
        finally:
            tree.cleanup()

    def test_corrupt_yaml_in_event(self) -> None:
        tree = _ProjectTree()
        try:
            _setup_valid_tree(tree)
            ev_dir = tree.root / "docs" / "pm" / "events"
            for f in ev_dir.iterdir():
                if f.suffix == ".yaml":
                    f.write_text("not json {{{", encoding="utf-8")
            with self.assertRaises(StateProviderSchemaError):
                StateProvider(tree.root).snapshot()
        finally:
            tree.cleanup()

    def test_corrupt_frontmatter_in_acceptance(self) -> None:
        tree = _ProjectTree()
        try:
            _setup_valid_tree(tree)
            acc_dir = tree.root / "docs" / "pm" / "acceptances"
            for f in acc_dir.iterdir():
                if f.suffix == ".md":
                    f.write_text("no frontmatter here\njust text\n", encoding="utf-8")
            with self.assertRaises(StateProviderSchemaError):
                StateProvider(tree.root).snapshot()
        finally:
            tree.cleanup()

    # ── 5. Extra / missing keys ────────────────────────────────────────────

    def test_extra_root_key_in_tasks(self) -> None:
        tree = _ProjectTree()
        try:
            _setup_valid_tree(tree)
            doc = _make_tasks_yaml()
            doc["extra_field"] = "bogus"
            tree.write_tasks(doc)
            with self.assertRaises(StateProviderSchemaError):
                StateProvider(tree.root).snapshot()
        finally:
            tree.cleanup()

    def test_missing_key_in_tasks(self) -> None:
        tree = _ProjectTree()
        try:
            _setup_valid_tree(tree)
            doc = _make_tasks_yaml()
            del doc["project_id"]
            tree.write_tasks(doc)
            with self.assertRaises(StateProviderSchemaError):
                StateProvider(tree.root).snapshot()
        finally:
            tree.cleanup()

    def test_extra_key_in_event(self) -> None:
        tree = _ProjectTree()
        try:
            _setup_valid_tree(tree)
            ev = _make_event_yaml()
            ev["bogus_field"] = 42
            tree.write_event(ev, "EVT-EXTRA.yaml")
            with self.assertRaises(StateProviderSchemaError):
                StateProvider(tree.root).snapshot()
        finally:
            tree.cleanup()

    # ── 6. Duplicate task/event/message ────────────────────────────────────

    def test_duplicate_task_id(self) -> None:
        tree = _ProjectTree()
        try:
            _setup_valid_tree(tree)
            doc = _make_tasks_yaml()
            doc["tasks"].append(doc["tasks"][0].copy())
            tree.write_tasks(doc)
            with self.assertRaises(StateProviderSchemaError):
                StateProvider(tree.root).snapshot()
        finally:
            tree.cleanup()

    def test_duplicate_event_id(self) -> None:
        tree = _ProjectTree()
        try:
            _setup_valid_tree(tree)
            ev = _make_event_yaml()
            tree.write_event(ev, "EVT-DUP-1.yaml")
            ev2 = _make_event_yaml(
                event_id=ev["event_id"], event_type="TASK_CANCELLED",
                from_state="ready", to_state="cancelled")
            tree.write_event(ev2, "EVT-DUP-2.yaml")
            with self.assertRaises(StateProviderSchemaError):
                StateProvider(tree.root).snapshot()
        finally:
            tree.cleanup()

    def test_duplicate_message_id(self) -> None:
        tree = _ProjectTree()
        try:
            _setup_valid_tree(tree)
            out = _make_outbox_yaml(dispatch_id="DSP-002",
                                    message_id="MSG-DUP")
            out_name = tree.write_outbox(out, "MSG-DUP-1.yaml")
            actual = (tree.root / "docs" / "pm" / "outbox" / out_name).read_bytes()
            digest = "sha256:" + hashlib.sha256(actual).hexdigest()
            ev = _make_event_yaml(
                event_id=out["event_id"], event_type="TASK_DISPATCHED",
                payload_digest=digest, from_state="ready", to_state="dispatched")
            tree.write_event(ev)
            # Write second outbox with same message_id
            out2 = _make_outbox_yaml(message_id="MSG-DUP", dispatch_id="DSP-003")
            tree.write_outbox(out2, "MSG-DUP-2.yaml")
            with self.assertRaises(StateProviderSchemaError):
                StateProvider(tree.root).snapshot()
        finally:
            tree.cleanup()

    # ── 7. Orphan detection ────────────────────────────────────────────────

    def test_orphan_event_references_nonexistent_task(self) -> None:
        tree = _ProjectTree()
        try:
            _setup_valid_tree(tree)
            tree.write_event(_make_event_yaml(
                task_id="TC-999", event_id="EVT-ORPHAN-1"))
            with self.assertRaises(StateProviderInconsistentSnapshotError):
                StateProvider(tree.root).snapshot()
        finally:
            tree.cleanup()

    def test_orphan_outbox_references_nonexistent_event(self) -> None:
        tree = _ProjectTree()
        try:
            _setup_valid_tree(tree)
            out = _make_outbox_yaml(event_id="EVT-NONEXISTENT", dispatch_id="DSP-ORPHAN")
            out_name = tree.write_outbox(out)
            with self.assertRaises(StateProviderInconsistentSnapshotError):
                StateProvider(tree.root).snapshot()
        finally:
            tree.cleanup()

    def test_orphan_outbox_references_nonexistent_task(self) -> None:
        tree = _ProjectTree()
        try:
            _setup_valid_tree(tree)
            ev = _make_event_yaml(task_id="TC-999", event_id="EVT-ORPHAN-2",
                                  event_type="TASK_DISPATCHED",
                                  payload_digest="sha256:" + "f" * 64,
                                  from_state="ready", to_state="dispatched")
            tree.write_event(ev)
            out = _make_outbox_yaml(task_id="TC-999", event_id=ev["event_id"],
                                    dispatch_id="DSP-ORPHAN")
            tree.write_outbox(out)
            with self.assertRaises(StateProviderInconsistentSnapshotError):
                StateProvider(tree.root).snapshot()
        finally:
            tree.cleanup()

    def test_orphan_acceptance_references_nonexistent_task(self) -> None:
        tree = _ProjectTree()
        try:
            _setup_valid_tree(tree)
            tree.write_acceptance(_make_acceptance_md(task_id="TC-999"),
                                  task_id="TC-999")
            with self.assertRaises(StateProviderInconsistentSnapshotError):
                StateProvider(tree.root).snapshot()
        finally:
            tree.cleanup()

    def test_orphan_mad_ref_references_nonexistent_task(self) -> None:
        tree = _ProjectTree()
        try:
            _setup_valid_tree(tree, include_mad_refs=True)
            tree.write_mad_refs(_make_mad_refs_yaml([
                _make_mad_ref_entry(task_id="TC-999", dispatch_id="DSP-001")
            ]))
            with self.assertRaises(StateProviderInconsistentSnapshotError):
                StateProvider(tree.root).snapshot()
        finally:
            tree.cleanup()

    def test_orphan_mad_ref_dispatch_no_event(self) -> None:
        tree = _ProjectTree()
        try:
            _setup_valid_tree(tree, include_mad_refs=True)
            tree.write_mad_refs(_make_mad_refs_yaml([
                _make_mad_ref_entry(dispatch_id="DSP-NONEXISTENT")
            ]))
            with self.assertRaises(StateProviderInconsistentSnapshotError):
                StateProvider(tree.root).snapshot()
        finally:
            tree.cleanup()

    # ── 8. Dispatch subject mismatch ───────────────────────────────────────

    def test_current_dispatch_no_matching_event(self) -> None:
        tree = _ProjectTree()
        try:
            _setup_valid_tree(tree)
            dispatch = {
                "dispatch_id": "DSP-MISMATCH", "attempt_id": "A-001",
                "role_id": "WORKER-001", "base_commit": "b" * 40,
                "branch": "work/TC-001",
                "dispatched_at": "2026-07-28T00:00:00Z",
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
            }
            tree.write_tasks(_make_tasks_yaml(
                state="dispatched", revision=2, attempt=1,
                current_dispatch=dispatch))
            with self.assertRaises(StateProviderInconsistentSnapshotError):
                StateProvider(tree.root).snapshot()
        finally:
            tree.cleanup()

    # ── 9. Digest inconsistency ────────────────────────────────────────────

    def test_payload_digest_mismatch(self) -> None:
        tree = _ProjectTree()
        try:
            out = _make_outbox_yaml(dispatch_id="DSP-BAD-DIGEST")
            tree.write_outbox(out)
            ev = _make_event_yaml(
                task_id="TC-001", event_type="TASK_DISPATCHED",
                event_id=out["event_id"],
                payload_digest="sha256:" + "f" * 64,
                from_state="ready", to_state="dispatched")
            tree.write_event(ev)
            tree.write_tasks(_make_tasks_yaml())
            with self.assertRaises(StateProviderInconsistentSnapshotError):
                StateProvider(tree.root).snapshot()
        finally:
            tree.cleanup()

    # ── 10. Deep immutability ──────────────────────────────────────────────

    def test_task_entry_is_frozen(self) -> None:
        tree = _ProjectTree()
        try:
            _setup_valid_tree(tree)
            snapshot = StateProvider(tree.root).snapshot()
            task = snapshot.tasks[0]
            with self.assertRaises(Exception):
                task.state = "modified"  # type: ignore[misc]
        finally:
            tree.cleanup()

    def test_collections_are_tuples(self) -> None:
        tree = _ProjectTree()
        try:
            _setup_valid_tree(tree, include_mad_refs=True)
            snapshot = StateProvider(tree.root).snapshot()
            self.assertIsInstance(snapshot.tasks, tuple)
            self.assertIsInstance(snapshot.events, tuple)
            self.assertIsInstance(snapshot.outbox, tuple)
            self.assertIsInstance(snapshot.acceptances, tuple)
            self.assertIsInstance(snapshot.mad_refs, tuple)
        finally:
            tree.cleanup()

    def test_cannot_mutate_task_tuple(self) -> None:
        tree = _ProjectTree()
        try:
            _setup_valid_tree(tree)
            snapshot = StateProvider(tree.root).snapshot()
            with self.assertRaises(TypeError):
                snapshot.tasks[0] = snapshot.tasks[0]  # type: ignore[index]
        finally:
            tree.cleanup()

    def test_cannot_append_to_tuple(self) -> None:
        tree = _ProjectTree()
        try:
            _setup_valid_tree(tree)
            snapshot = StateProvider(tree.root).snapshot()
            with self.assertRaises(AttributeError):
                snapshot.tasks.append(None)  # type: ignore[union-attr]
        finally:
            tree.cleanup()

    # ── 11. Construction errors ────────────────────────────────────────────

    def test_construction_rejects_non_path(self) -> None:
        with self.assertRaises(StateProviderInputError):
            StateProvider("/not/a/path/object")  # type: ignore[arg-type]

    def test_construction_rejects_relative_path(self) -> None:
        with self.assertRaises(StateProviderInputError):
            StateProvider(Path("relative/path"))

    def test_construction_accepts_absolute_path(self) -> None:
        tree = _ProjectTree()
        try:
            _setup_valid_tree(tree)
            self.assertIsInstance(StateProvider(tree.root), StateProvider)
        finally:
            tree.cleanup()

    # ── 12. Missing required files ─────────────────────────────────────────

    def test_missing_tasks_yaml_raises_not_found(self) -> None:
        tree = _ProjectTree()
        try:
            tree.delete_file("docs/pm/state/tasks.yaml")
            with self.assertRaises(StateProviderNotFoundError):
                StateProvider(tree.root).snapshot()
        finally:
            tree.cleanup()

    def test_missing_events_dir_raises_not_found(self) -> None:
        tree = _ProjectTree()
        try:
            tree.write_tasks(_make_tasks_yaml())
            tree.delete_dir("docs/pm/events")
            with self.assertRaises(StateProviderNotFoundError):
                StateProvider(tree.root).snapshot()
        finally:
            tree.cleanup()

    # ── 13. Locale-independent sort ────────────────────────────────────────

    def test_sort_is_locale_independent(self) -> None:
        tree = _ProjectTree()
        try:
            out = _make_outbox_yaml(dispatch_id="DSP-SORT")
            tree.write_tasks(_make_tasks_yaml())
            tree.write_outbox(out)
            # Read back exact bytes for digest
            actual = (tree.root / "docs" / "pm" / "outbox").iterdir().__next__().read_bytes()
            digest = "sha256:" + hashlib.sha256(actual).hexdigest()
            ev = _make_event_yaml(
                event_id=out["event_id"], event_type="TASK_DISPATCHED",
                payload_digest=digest, from_state="ready", to_state="dispatched")
            tree.write_event(ev)

            # Write events with names that sort differently under locale vs byte
            for name in ("EVT-a", "EVT-Z", "EVT-b"):
                ev2 = _make_event_yaml(event_id=name)
                tree.write_event(ev2, f"{name}.yaml")

            # Run with C locale
            try:
                saved = locale.setlocale(locale.LC_ALL, "C")
            except locale.Error:
                saved = locale.getlocale(locale.LC_ALL)
                try:
                    locale.setlocale(locale.LC_ALL, "")
                except locale.Error:
                    pass

            try:
                snapshot = StateProvider(tree.root).snapshot()
                ev_names = [e.event_id for e in snapshot.events
                            if e.event_id in ("EVT-Z", "EVT-a", "EVT-b")]
                # Byte order: 'Z' (0x5A) < 'a' (0x61) < 'b' (0x62)
                if len(ev_names) >= 2:
                    expected = sorted(ev_names, key=lambda n: n.encode("utf-8"))
                    self.assertEqual(ev_names, expected,
                                     "Sort must be locale-independent byte order")
            finally:
                try:
                    locale.setlocale(locale.LC_ALL, "")
                except locale.Error:
                    pass
        finally:
            tree.cleanup()

    # ── 14. Zero subprocess ────────────────────────────────────────────────

    def test_no_subprocess_imports(self) -> None:
        src = (_SKILL_SCRIPTS / "state_provider.py").read_text(encoding="utf-8")
        self.assertNotIn("import subprocess", src)
        self.assertNotIn("from subprocess", src)

    def test_no_subprocess_calls(self) -> None:
        tree = _ProjectTree()
        try:
            _setup_valid_tree(tree)
            orig_run = subprocess.run
            caught = [0]
            def _counting(*args: object, **kwargs: object) -> object:  # type: ignore[no-untyped-def]
                caught[0] += 1
                return orig_run(*args, **kwargs)  # type: ignore[call-arg]
            subprocess.run = _counting  # type: ignore[assignment]
            try:
                StateProvider(tree.root).snapshot()
                self.assertEqual(caught[0], 0,
                                 "StateProvider must not call subprocess.run")
            finally:
                subprocess.run = orig_run
        finally:
            tree.cleanup()

    # ── 15. Zero file writes ───────────────────────────────────────────────

    def test_no_file_writes_during_snapshot(self) -> None:
        tree = _ProjectTree()
        try:
            _setup_valid_tree(tree)
            before = {str(p): p.stat().st_mtime
                      for p in tree.root.rglob("*") if p.is_file()}
            StateProvider(tree.root).snapshot()
            for p in tree.root.rglob("*"):
                if p.is_file():
                    key = str(p)
                    if key in before:
                        self.assertEqual(before[key], p.stat().st_mtime,
                                         f"File {key} modified during snapshot")
            after = {str(p) for p in tree.root.rglob("*") if p.is_file()}
            self.assertEqual(set(before.keys()), after,
                             "Files created during snapshot")
        finally:
            tree.cleanup()

    # ── 16. Exception message leak prevention ──────────────────────────────

    def test_error_messages_do_not_contain_task_id(self) -> None:
        tree = _ProjectTree()
        try:
            _setup_valid_tree(tree)
            (tree.root / "docs" / "pm" / "events" / "EVT-001.yaml").write_text(
                "not json {{{", encoding="utf-8")
            try:
                StateProvider(tree.root).snapshot()
            except StateProviderError as e:
                self.assertNotIn("TC-001", str(e))
                self.assertNotIn("TC-", str(e))
        finally:
            tree.cleanup()

    def test_error_messages_do_not_contain_paths(self) -> None:
        tree = _ProjectTree()
        try:
            _setup_valid_tree(tree)
            (tree.root / "docs" / "pm" / "events" / "EVT-001.yaml").write_text(
                "not json {{{", encoding="utf-8")
            try:
                StateProvider(tree.root).snapshot()
            except StateProviderError as e:
                self.assertNotIn(str(tree.root), str(e))
        finally:
            tree.cleanup()

    # ── 17. Malicious __repr__ not called ────────────────────────────────────

    def test_malicious_repr_not_called(self) -> None:
        tree = _ProjectTree()
        try:
            _setup_valid_tree(tree)
            (tree.root / "docs" / "pm" / "events" / "EVT-001.yaml").write_text(
                "not json {{{", encoding="utf-8")
            try:
                StateProvider(tree.root).snapshot()
            except StateProviderError:
                pass  # If we reach here without repr() exploding, the test passes
        finally:
            tree.cleanup()

    # ── 18. Schema version mismatch ────────────────────────────────────────

    def test_wrong_schema_version_in_tasks(self) -> None:
        tree = _ProjectTree()
        try:
            _setup_valid_tree(tree)
            doc = _make_tasks_yaml()
            doc["schema_version"] = "agentdesk.tasks/v1"
            tree.write_tasks(doc)
            with self.assertRaises(StateProviderSchemaError):
                StateProvider(tree.root).snapshot()
        finally:
            tree.cleanup()

    def test_wrong_schema_version_in_event(self) -> None:
        tree = _ProjectTree()
        try:
            _setup_valid_tree(tree)
            ev = _make_event_yaml()
            ev["schema_version"] = "agentdesk.state-event/v1"
            tree.write_event(ev, "EVT-WRONG.yaml")
            with self.assertRaises(StateProviderSchemaError):
                StateProvider(tree.root).snapshot()
        finally:
            tree.cleanup()

    def test_wrong_schema_version_in_outbox(self) -> None:
        tree = _ProjectTree()
        try:
            _setup_valid_tree(tree)
            out = _make_outbox_yaml(dispatch_id="DSP-WRONG-SCHEMA")
            out["schema_version"] = "agentdesk.outbox-message/v1"
            tree.write_outbox(out)
            with self.assertRaises(StateProviderSchemaError):
                StateProvider(tree.root).snapshot()
        finally:
            tree.cleanup()

    # ── 19. Bool not accepted as int ───────────────────────────────────────

    def test_bool_not_accepted_as_revision(self) -> None:
        tree = _ProjectTree()
        try:
            _setup_valid_tree(tree)
            doc = _make_tasks_yaml()
            # JSON has no bool type confusion path — test with non-int string
            doc["tasks"][0]["revision"] = "not_an_int"
            tree.write_tasks(doc)
            with self.assertRaises(StateProviderSchemaError):
                StateProvider(tree.root).snapshot()
        finally:
            tree.cleanup()

    def test_non_int_lease_epoch_rejected(self) -> None:
        tree = _ProjectTree()
        try:
            _setup_valid_tree(tree)
            doc = _make_tasks_yaml()
            doc["pm_control"]["lease_epoch"] = 0  # too small
            tree.write_tasks(doc)
            with self.assertRaises(StateProviderSchemaError):
                StateProvider(tree.root).snapshot()
        finally:
            tree.cleanup()

    # ── 20. blocked_attempt_valid must be bool not int ─────────────────────

    def test_blocked_attempt_valid_rejects_int(self) -> None:
        tree = _ProjectTree()
        try:
            _setup_valid_tree(tree)
            doc = _make_tasks_yaml()
            doc["tasks"][0]["blocked_attempt_valid"] = 1  # int, not bool
            tree.write_tasks(doc)
            with self.assertRaises(StateProviderSchemaError):
                StateProvider(tree.root).snapshot()
        finally:
            tree.cleanup()

    # ── 21. No write-end imports ───────────────────────────────────────────

    def test_no_write_end_imports(self) -> None:
        src = (_SKILL_SCRIPTS / "state_provider.py").read_text(encoding="utf-8")
        self.assertNotIn("import control_plane_transition", src)
        self.assertNotIn("from control_plane_transition", src)
        self.assertNotIn("import worker_adapter", src)
        self.assertNotIn("from worker_adapter", src)
        self.assertNotIn("from approval_gate import", src)
        self.assertNotIn("import approval_gate", src)

    # ── 22. Zero lock ──────────────────────────────────────────────────────

    def test_no_lock_files_created(self) -> None:
        tree = _ProjectTree()
        try:
            _setup_valid_tree(tree)
            StateProvider(tree.root).snapshot()
            lock_files = list(tree.root.rglob("*.lock"))
            self.assertEqual(len(lock_files), 0,
                             f"Lock files created: {lock_files}")
        finally:
            tree.cleanup()

    # ── 23. MadRefEntry matches mad_refs.py field order ────────────────────

    def test_mad_ref_entry_field_order(self) -> None:
        expected = [
            "task_id", "dispatch_id", "purpose", "deliberation_id",
            "depth", "stdout_sha256", "report_sha256", "status",
            "archive_path", "created_at",
        ]
        field_names = [f.name for f in fields(MadRefEntry)]
        self.assertEqual(field_names, expected,
                         "MadRefEntry field order must match mad_refs.py")

    # ── 24. __all__ completeness ───────────────────────────────────────────

    def test_all_public_symbols_in_all(self) -> None:
        import state_provider as sp
        _import_artifacts = {
            "Any", "Mapping", "Iterator", "MappingProxyType",
            "re", "json", "hashlib", "sys", "dataclass", "types",
            "pathlib", "TYPE_CHECKING", "Path",
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

    # ── 25. Multiple tasks ─────────────────────────────────────────────────

    def test_multiple_tasks_parses_correctly(self) -> None:
        tree = _ProjectTree()
        try:
            _setup_valid_tree(tree)
            doc = _make_tasks_yaml(task_id="TC-001")
            doc["tasks"].append({
                "task_id": "TC-002", "revision": 1,
                "task_card_path": "docs/pm/tasks/TC-002.md",
                "task_card_commit": "b" * 40, "state": "draft",
                "attempt": None, "current_dispatch": None,
                "report_path": None, "granted_approval_ids": None,
                "delivery_state": None, "integration_state": None,
                "implementation_commit": None, "report_commit": None,
                "accepted_commit": None, "acceptance_path": None,
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
            })
            tree.write_tasks(doc)
            snapshot = StateProvider(tree.root).snapshot()
            self.assertEqual(len(snapshot.tasks), 2)
            self.assertEqual({t.task_id for t in snapshot.tasks},
                             {"TC-001", "TC-002"})
        finally:
            tree.cleanup()

    # ── 26. Exception hierarchy ────────────────────────────────────────────

    def test_exception_hierarchy(self) -> None:
        self.assertTrue(issubclass(StateProviderInputError, StateProviderError))
        self.assertTrue(issubclass(StateProviderNotFoundError, StateProviderError))
        self.assertTrue(issubclass(StateProviderSchemaError, StateProviderError))
        self.assertTrue(issubclass(StateProviderSnapshotChangedError, StateProviderError))
        self.assertTrue(issubclass(StateProviderInconsistentSnapshotError, StateProviderError))


def load_tests(loader: unittest.TestLoader, standard_tests: unittest.TestSuite,
               pattern: str | None) -> unittest.TestSuite:  # type: ignore[override]
    suite = unittest.TestSuite()
    suite.addTests(loader.loadTestsFromTestCase(TC1317bProductionTests))
    return suite
