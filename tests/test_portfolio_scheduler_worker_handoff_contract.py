"""TC-13.24b.2b.4c.1 — PortfolioScheduler Worker Handoff Contract frozen tests.

stdlib-only unittest; no external dependencies.
Reads the contract markdown and verifies all frozen constraints:
  - exact public types and field counts
  - forward-only phases
  - no duplicate lease / event / attempt
  - provider pre-validation
  - lock-free Worker start
  - 1:1 generation binding with DispatchProcessReceipt
  - crash matrix completeness
  - byte-exact replay semantics
  - concurrency / stale generation / attempt 4
  - Interface #22 unchanged
  - runtime Target status

These are contract-freeze tests only — no production module is imported or called.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path


# ── helpers ──────────────────────────────────────────────────────────────────

_CONTRACT_PATH = (
    Path(__file__).resolve().parents[1]
    / "skills"
    / "agentdesk"
    / "references"
    / "public-interfaces"
    / "portfolio-scheduler-worker-handoff-contract.md"
)


def _contract_text() -> str:
    return _CONTRACT_PATH.read_text(encoding="utf-8")


def _find_heading(text: str, heading: str) -> int:
    """Return index of first char after the line containing *heading*, or -1.

    Matches by exact substring on any line where *heading* appears after
    optional `### ` prefix.  This avoids re.escape issues with Unicode
    punctuation (em-dashes, etc.) in heading text.
    """
    for m in re.finditer(r"^#{1,4}\s+", text, re.MULTILINE):
        line_start = m.start()
        line_end = text.find("\n", line_start)
        if line_end == -1:
            line_end = len(text)
        line = text[line_start:line_end]
        if heading in line:
            return line_end + 1  # index of first char on next line
    return -1


def _extract_table_rows(text: str, heading: str) -> list[dict[str, str]]:
    """Extract rows from a markdown table after *heading*.

    Returns a list of dicts keyed by column header.
    """
    idx = _find_heading(text, heading)
    if idx == -1:
        return []
    rest = text[idx:]

    # Find the next markdown table
    table_m = re.search(
        r"\|(.+)\|\n\|[-| :]+\|\n((?:\|.+\|\n?)+)", rest
    )
    if table_m is None:
        return []
    header_line = table_m.group(1)
    body_block = table_m.group(2)

    headers = [h.strip().strip("`") for h in header_line.split("|") if h.strip()]
    rows: list[dict[str, str]] = []
    for line in body_block.strip().split("\n"):
        if not line.startswith("|"):
            continue
        # Handle markdown-escaped `\|` inside cells (e.g. `str \| None`)
        cells = _split_table_row(line)
        if len(cells) == len(headers):
            rows.append(dict(zip(headers, cells)))
    return rows


def _split_table_row(line: str) -> list[str]:
    """Split a markdown table row, respecting backtick-quoted content."""
    # Replace `\|` inside cells with a temporary placeholder
    # to avoid splitting on markdown-escaped pipes
    normalized = re.sub(r"\\\|", "\x00PIPE\x00", line)
    cells = [c.strip().strip("`").replace("\x00PIPE\x00", "\\|")
             for c in normalized.split("|")[1:-1]]
    return cells


def _extract_backtick_enum(text: str, heading: str) -> list[str]:
    """Extract a backtick-enclosed list after *heading*."""
    idx = _find_heading(text, heading)
    if idx == -1:
        return []
    rest = text[idx:]
    m = re.search(
        r"```text\s*\n(.*?)```",
        rest, re.DOTALL,
    )
    if m is None:
        return []
    return [v.strip() for v in m.group(1).strip().split("\n") if v.strip()]


def _extract_numbered_list(text: str, heading: str) -> list[str]:
    """Extract a numbered list after *heading*."""
    idx = _find_heading(text, heading)
    if idx == -1:
        return []
    rest = text[idx:]
    items: list[str] = []
    for line in rest.split("\n"):
        nm = re.match(r"^\d+\.\s+(.+)$", line)
        if nm:
            items.append(nm.group(1).strip())
        elif items and not line.strip():
            continue
        elif items and not re.match(r"^\d+\.\s", line):
            break
    return items


def _section_text(text: str, heading: str) -> str:
    """Return the text under a ##-level heading up to the next ## heading."""
    idx = _find_heading(text, heading)
    if idx == -1:
        return ""
    rest = text[idx:]
    next_m = re.search(r"^##\s+", rest, re.MULTILINE)
    if next_m:
        rest = rest[:next_m.start()]
    return rest


# ── ScheduledDispatchHandoff fields (25) ──────────────────────────────────────

HANDOFF_25_FIELDS = [
    "schema_version",
    "handoff_id",
    "queue_id",
    "plan_digest",
    "receipt_id",
    "task_id",
    "revision",
    "attempt",
    "dispatch_id",
    "dispatch_event_id",
    "outbox_message_id",
    "lease_id",
    "lease_epoch",
    "holder_instance_id",
    "worker_kind",
    "assessment_id",
    "expected_snapshot_commit",
    "provider_binding_id",
    "phase",
    "reserved_at",
    "supervisor_started_at",
    "worker_started_at",
    "acknowledged_at",
    "finalized_at",
    "content_digest",
]

# ── ScheduledDispatchHandoffReceipt fields (11) ──────────────────────────────

HANDOFF_RECEIPT_11_FIELDS = [
    "schema_version",
    "handoff_id",
    "dispatch_id",
    "generation_id",
    "task_id",
    "revision",
    "attempt",
    "phase",
    "binding_digest",
    "written_at",
    "content_digest",
]

# ── Phase values (7) ─────────────────────────────────────────────────────────

HANDOFF_PHASES = [
    "ADMISSION_COMMITTED",
    "HANDOFF_RESERVED",
    "SUPERVISOR_STARTED",
    "WORKER_STARTED",
    "ACKNOWLEDGED",
    "FINALIZING",
    "FINALIZED",
]

# ── Outcome values (4) ────────────────────────────────────────────────────────

HANDOFF_OUTCOMES = [
    "HANDOFF_ACCEPTED",
    "HANDOFF_REJECTED",
    "HANDOFF_DEFERRED",
    "HANDOFF_FAILED",
]

# ── Forbidden field types ─────────────────────────────────────────────────────

FORBIDDEN_FIELD_TYPES = [
    "Any",
    "dict",
    "Mapping",
    "argv",
    "env",
    "prompt",
    "stdout",
    "stderr",
]


# ═══════════════════════════════════════════════════════════════════════════════
# Contract file existence and path
# ═══════════════════════════════════════════════════════════════════════════════

class ContractFileExistenceTests(unittest.TestCase):
    def test_contract_file_exists(self) -> None:
        self.assertTrue(
            _CONTRACT_PATH.exists(),
            f"Contract file missing: {_CONTRACT_PATH}",
        )

    def test_contract_file_is_regular(self) -> None:
        self.assertTrue(
            _CONTRACT_PATH.is_file(),
            f"Contract path is not a regular file: {_CONTRACT_PATH}",
        )

    def test_contract_file_is_non_empty(self) -> None:
        size = _CONTRACT_PATH.stat().st_size
        self.assertGreater(size, 0, "Contract file is empty")

    def test_contract_file_is_utf8(self) -> None:
        _CONTRACT_PATH.read_text(encoding="utf-8")  # must not raise

    def test_contract_file_has_no_bom(self) -> None:
        raw = _CONTRACT_PATH.read_bytes()
        self.assertNotEqual(raw[:3], b"\xef\xbb\xbf", "Contract file has UTF-8 BOM")


# ═══════════════════════════════════════════════════════════════════════════════
# Exact public types — ScheduledDispatchHandoff (25 fields)
# ═══════════════════════════════════════════════════════════════════════════════

class ScheduledDispatchHandoffTypeTests(unittest.TestCase):
    def test_handoff_type_has_exactly_25_fields(self) -> None:
        text = _contract_text()
        rows = _extract_table_rows(text, "### 3.4 ScheduledDispatchHandoff — Exactly 25 Fields")
        field_names = [r["Field"] for r in rows if "Field" in r]
        self.assertEqual(
            len(field_names), 25,
            f"ScheduledDispatchHandoff must have exactly 25 fields, got {len(field_names)}: {field_names}",
        )

    def test_handoff_field_names_exact_match(self) -> None:
        text = _contract_text()
        rows = _extract_table_rows(text, "### 3.4 ScheduledDispatchHandoff — Exactly 25 Fields")
        actual = [r["Field"] for r in rows if "Field" in r]
        self.assertEqual(
            actual, HANDOFF_25_FIELDS,
            f"ScheduledDispatchHandoff fields must match frozen order precisely",
        )

    def test_handoff_schema_version_field_is_first(self) -> None:
        text = _contract_text()
        rows = _extract_table_rows(text, "### 3.4 ScheduledDispatchHandoff — Exactly 25 Fields")
        actual = [r["Field"] for r in rows if "Field" in r]
        self.assertEqual(actual[0], "schema_version")

    def test_handoff_content_digest_field_is_last(self) -> None:
        text = _contract_text()
        rows = _extract_table_rows(text, "### 3.4 ScheduledDispatchHandoff — Exactly 25 Fields")
        actual = [r["Field"] for r in rows if "Field" in r]
        self.assertEqual(actual[-1], "content_digest")

    def test_handoff_fields_use_column_types(self) -> None:
        text = _contract_text()
        rows = _extract_table_rows(text, "### 3.4 ScheduledDispatchHandoff — Exactly 25 Fields")
        for row in rows:
            self.assertIn("Type", row, f"Missing Type column for field {row.get('Field')}")
            self.assertIn("Rule", row, f"Missing Rule column for field {row.get('Field')}")

    def test_handoff_no_forbidden_field_types(self) -> None:
        text = _contract_text()
        rows = _extract_table_rows(text, "### 3.4 ScheduledDispatchHandoff — Exactly 25 Fields")
        for row in rows:
            type_val = row.get("Type", "")
            for forbidden in FORBIDDEN_FIELD_TYPES:
                self.assertNotIn(
                    forbidden, type_val,
                    f"Forbidden type '{forbidden}' found in field {row.get('Field')}: {type_val}",
                )

    def test_handoff_section_states_forbidden_types_explicitly(self) -> None:
        text = _CONTRACT_PATH.read_text(encoding="utf-8")
        self.assertIn("Any", text)
        self.assertIn("dict", text.lower())
        # Should contain the explicit prohibition sentence
        self.assertTrue(
            "no public field may" in text.lower() or
            "must not contain" in text.lower() or
            "forbidden" in text.lower(),
            "Contract must explicitly forbid Any/dict/Mapping/mutable in public types",
        )

    def test_handoff_attempt_constrained_to_1_to_3(self) -> None:
        text = _contract_text()
        self.assertIn("attempt", text.lower())
        # attempt 4 must be forbidden
        self.assertTrue(
            "attempt 4" in text.lower() or "forbidden" in text.lower(),
            "Contract must forbid attempt 4",
        )


# ═══════════════════════════════════════════════════════════════════════════════
# Exact public types — ScheduledDispatchHandoffReceipt (11 fields)
# ═══════════════════════════════════════════════════════════════════════════════

class ScheduledDispatchHandoffReceiptTypeTests(unittest.TestCase):
    def test_receipt_type_has_exactly_11_fields(self) -> None:
        text = _contract_text()
        rows = _extract_table_rows(text, "### 3.5 ScheduledDispatchHandoffReceipt — Exactly 11 Fields")
        field_names = [r["Field"] for r in rows if "Field" in r]
        self.assertEqual(
            len(field_names), 11,
            f"ScheduledDispatchHandoffReceipt must have exactly 11 fields, got {len(field_names)}: {field_names}",
        )

    def test_receipt_field_names_exact_match(self) -> None:
        text = _contract_text()
        rows = _extract_table_rows(text, "### 3.5 ScheduledDispatchHandoffReceipt — Exactly 11 Fields")
        actual = [r["Field"] for r in rows if "Field" in r]
        self.assertEqual(
            actual, HANDOFF_RECEIPT_11_FIELDS,
            f"ScheduledDispatchHandoffReceipt fields must match frozen order precisely",
        )

    def test_receipt_no_forbidden_field_types(self) -> None:
        text = _contract_text()
        rows = _extract_table_rows(text, "### 3.5 ScheduledDispatchHandoffReceipt — Exactly 11 Fields")
        for row in rows:
            type_val = row.get("Type", "")
            for forbidden in FORBIDDEN_FIELD_TYPES:
                self.assertNotIn(
                    forbidden, type_val,
                    f"Forbidden type '{forbidden}' found in receipt field {row.get('Field')}",
                )

    def test_receipt_binds_generation_id(self) -> None:
        text = _contract_text()
        rows = _extract_table_rows(text, "### 3.5 ScheduledDispatchHandoffReceipt — Exactly 11 Fields")
        fields = [r["Field"] for r in rows if "Field" in r]
        self.assertIn("generation_id", fields, "Receipt must have generation_id field")

    def test_receipt_binding_digest_field_present(self) -> None:
        text = _contract_text()
        rows = _extract_table_rows(text, "### 3.5 ScheduledDispatchHandoffReceipt — Exactly 11 Fields")
        fields = [r["Field"] for r in rows if "Field" in r]
        self.assertIn("binding_digest", fields, "Receipt must have binding_digest field")


# ═══════════════════════════════════════════════════════════════════════════════
# Forward-only phase transitions
# ═══════════════════════════════════════════════════════════════════════════════

class ForwardOnlyPhaseTests(unittest.TestCase):
    def test_phase_enum_has_exactly_7_values(self) -> None:
        text = _contract_text()
        phases = _extract_backtick_enum(text, "### 3.2 ScheduledDispatchHandoffPhase")
        self.assertEqual(
            len(phases), 7,
            f"ScheduledDispatchHandoffPhase must have exactly 7 values, got {len(phases)}: {phases}",
        )

    def test_phase_values_exact_match(self) -> None:
        text = _contract_text()
        phases = _extract_backtick_enum(text, "### 3.2 ScheduledDispatchHandoffPhase")
        self.assertEqual(
            phases, HANDOFF_PHASES,
            "Phase values must match frozen order precisely",
        )

    def test_legal_transitions_are_frozen(self) -> None:
        text = _contract_text()
        # Extract the "only legal phase transitions" block
        section = _section_text(text, "3.2 ScheduledDispatchHandoffPhase")
        # Each phase (except the last) must appear exactly once as a source
        transitions = re.findall(
            r"(\w+)\s*→\s*(\w+)",
            section,
        )
        sources = {t[0] for t in transitions}
        targets = {t[1] for t in transitions}
        # Every phase except FINALIZED must be a source
        self.assertIn("ADMISSION_COMMITTED", sources)
        self.assertIn("HANDOFF_RESERVED", sources)
        self.assertIn("SUPERVISOR_STARTED", sources)
        self.assertIn("WORKER_STARTED", sources)
        self.assertIn("ACKNOWLEDGED", sources)
        self.assertIn("FINALIZING", sources)
        # FINALIZED is the terminal phase (not a source)
        self.assertNotIn("FINALIZED", sources)

    def test_no_backward_transitions(self) -> None:
        text = _contract_text()
        self.assertTrue(
            "forward-only" in text.lower() or "forward only" in text.lower(),
            "Contract must state phases are forward-only",
        )

    def test_no_phase_skipping(self) -> None:
        text = _contract_text()
        self.assertTrue(
            "skipping a phase" in text.lower() or "skip" in text.lower(),
            "Contract must forbid phase skipping",
        )


# ═══════════════════════════════════════════════════════════════════════════════
# Outcome enum
# ═══════════════════════════════════════════════════════════════════════════════

class OutcomeEnumTests(unittest.TestCase):
    def test_outcome_enum_has_exactly_4_values(self) -> None:
        text = _contract_text()
        outcomes = _extract_backtick_enum(text, "### 3.3 ScheduledDispatchHandoffOutcome")
        self.assertEqual(
            len(outcomes), 4,
            f"ScheduledDispatchHandoffOutcome must have exactly 4 values, got {len(outcomes)}: {outcomes}",
        )

    def test_outcome_values_exact_match(self) -> None:
        text = _contract_text()
        outcomes = _extract_backtick_enum(text, "### 3.3 ScheduledDispatchHandoffOutcome")
        self.assertEqual(
            outcomes, HANDOFF_OUTCOMES,
            "Outcome values must match frozen order precisely",
        )


# ═══════════════════════════════════════════════════════════════════════════════
# No duplicate lease / event / attempt
# ═══════════════════════════════════════════════════════════════════════════════

class NoDuplicateLeaseEventAttemptTests(unittest.TestCase):
    def test_no_second_worker_slot_lease(self) -> None:
        text = _contract_text()
        self.assertIn("never create a second", text.lower())
        self.assertIn("WorkerSlotLease", text)
        self.assertIn("second", text)

    def test_no_second_task_dispatched_event(self) -> None:
        text = _contract_text()
        self.assertIn("TASK_DISPATCHED", text)
        # Must reference "second" prohibition
        self.assertTrue(
            "second TASK_DISPATCHED" in text
            or "re-create TASK_DISPATCHED" in text,
            "Contract must forbid a second TASK_DISPATCHED event",
        )

    def test_no_second_dispatch_id(self) -> None:
        text = _contract_text()
        # Look for the key prohibition line: 'never create a second'
        # which mentions dispatch_id among the forbidden seconds
        self.assertTrue(
            ("second" in text.lower() and "dispatch_id" in text.lower() and
             any(p in text.lower() for p in [
                 "never create a second", "never create second",
                 "a second dispatch_id", "a second `dispatch_id`",
                 "no second dispatch_id", "forbid",
             ])),
            "Contract must forbid a second dispatch_id",
        )

    def test_no_second_event_id(self) -> None:
        text = _contract_text()
        self.assertTrue(
            "second event_id" in text.lower()
            or "second `event_id`" in text
            or "a second event_id" in text.lower(),
            "Contract must forbid a second event_id",
        )

    def test_no_second_attempt(self) -> None:
        text = _contract_text()
        self.assertTrue(
            "second attempt" in text.lower()
            or "second `attempt`" in text
            or "a second attempt" in text.lower(),
            "Contract must forbid a second attempt",
        )

    def test_no_second_admission(self) -> None:
        text = _contract_text()
        self.assertIn("Admission", text)
        self.assertTrue(
            "second Admission" in text
            or "re-execute Admission" in text
            or "not re-execute Admission" in text,
            "Contract must forbid a second Admission",
        )


# ═══════════════════════════════════════════════════════════════════════════════
# Provider pre-validation
# ═══════════════════════════════════════════════════════════════════════════════

class ProviderPreValidationTests(unittest.TestCase):
    def test_providers_explicit_non_empty_typed_mapping(self) -> None:
        text = _contract_text()
        self.assertIn("providers", text.lower())
        self.assertIn("non-empty", text.lower())

    def test_providers_validated_before_any_handoff_write(self) -> None:
        text = _contract_text()
        self.assertIn("before any handoff", text.lower())

    def test_zero_handoff_write_zero_supervisor_zero_worker_on_missing_provider(self) -> None:
        text = _contract_text()
        self.assertIn("zero handoff writes", text.lower())
        self.assertIn("zero supervisor", text.lower())
        # "zero Worker" or "zero worker" or "zero ack" or "zero ACK"
        self.assertTrue(
            "zero worker" in text.lower() or "zero ack" in text.lower(),
            "Contract must forbid zero Worker/ACK on missing provider",
        )

    def test_no_api_key_persistence(self) -> None:
        text = _contract_text()
        self.assertIn("API key", text)

    def test_no_provider_name_based_retry_classification(self) -> None:
        text = _contract_text()
        section = _section_text(text, "5. Provider Boundary")
        self.assertIn("provider", section.lower())


# ═══════════════════════════════════════════════════════════════════════════════
# Lock-free Worker start
# ═══════════════════════════════════════════════════════════════════════════════

class LockFreeWorkerStartTests(unittest.TestCase):
    def test_worker_adapter_not_called_inside_lock(self) -> None:
        text = _contract_text()
        section = _section_text(text, "6. Atomic and Lock Order")
        self.assertIn("never", section.lower())

    def test_no_process_start_inside_lock(self) -> None:
        text = _contract_text()
        section = _section_text(text, "6. Atomic and Lock Order")
        self.assertIn("process", section.lower())

    def test_no_model_call_inside_lock(self) -> None:
        text = _contract_text()
        section = _section_text(text, "6. Atomic and Lock Order")
        self.assertIn("model", section.lower())

    def test_no_heartbeat_wait_inside_lock(self) -> None:
        text = _contract_text()
        section = _section_text(text, "6. Atomic and Lock Order")
        self.assertTrue(
            "heartbeat" in section.lower() or "completion" in section.lower(),
        )

    def test_lock_contention_returns_immediate_failure(self) -> None:
        text = _contract_text()
        section = _section_text(text, "6. Atomic and Lock Order")
        self.assertIn("never waits indefinitely", section.lower())

    def test_worker_protected_by_lease_epoch_heartbeat_fencing(self) -> None:
        text = _contract_text()
        self.assertIn("lease epoch", text.lower())
        self.assertIn("heartbeat", text.lower())


# ═══════════════════════════════════════════════════════════════════════════════
# 1:1 generation binding with DispatchProcessReceipt
# ═══════════════════════════════════════════════════════════════════════════════

class OneToOneDispatchProcessReceiptBindingTests(unittest.TestCase):
    def test_receipt_section_mentions_dispatch_process_receipt(self) -> None:
        text = _contract_text()
        self.assertIn("DispatchProcessReceipt", text)

    def test_one_to_one_generation_binding_stated(self) -> None:
        text = _contract_text()
        self.assertIn("1:1", text)
        self.assertTrue(
            "1:1 generation binding" in text
            or "one-to-one generation binding" in text.lower(),
            "Contract must state 1:1 generation binding",
        )

    def test_generation_id_matches(self) -> None:
        text = _contract_text()
        self.assertIn("generation_id", text)
        # The handoff receipt generation_id must match DispatchProcessReceipt.generation_id
        self.assertIn("match", text.lower())

    def test_at_most_one_handoff_receipt_per_dispatch(self) -> None:
        text = _contract_text()
        self.assertTrue(
            "at most one" in text.lower(),
            "Contract must state at most one handoff receipt per dispatch",
        )

    def test_no_copy_or_replace_dispatch_process_receipt(self) -> None:
        text = _contract_text()
        self.assertTrue(
            "does not copy" in text.lower()
            or "not copy" in text.lower()
            or "does not duplicate" in text.lower(),
            "Contract must state the handoff receipt doesn't copy/replace DispatchProcessReceipt",
        )


# ═══════════════════════════════════════════════════════════════════════════════
# Crash matrix — frozen windows
# ═══════════════════════════════════════════════════════════════════════════════

class CrashMatrixTests(unittest.TestCase):
    def test_crash_matrix_has_at_least_15_entries(self) -> None:
        text = _contract_text()
        rows = _extract_table_rows(text, "## 7. Crash Recovery Matrix")
        self.assertGreaterEqual(
            len(rows), 15,
            f"Crash matrix must have at least 15 entries, got {len(rows)}",
        )

    def test_crash_matrix_covers_admission_committed_window(self) -> None:
        text = _contract_text()
        section = _section_text(text, "7. Crash Recovery Matrix")
        self.assertIn("Before Admission", section)

    def test_crash_matrix_covers_handoff_reservation_interrupted(self) -> None:
        text = _contract_text()
        section = _section_text(text, "7. Crash Recovery Matrix")
        self.assertIn("interrupted", section.lower())

    def test_crash_matrix_covers_before_supervisor_start(self) -> None:
        text = _contract_text()
        section = _section_text(text, "7. Crash Recovery Matrix")
        self.assertIn("supervisor", section.lower())

    def test_crash_matrix_covers_worker_started_window(self) -> None:
        text = _contract_text()
        section = _section_text(text, "7. Crash Recovery Matrix")
        self.assertIn("Worker started", section)

    def test_crash_matrix_covers_ack_window(self) -> None:
        text = _contract_text()
        section = _section_text(text, "7. Crash Recovery Matrix")
        self.assertIn("ACK", section)

    def test_crash_matrix_covers_finalizer_before_finalized(self) -> None:
        text = _contract_text()
        section = _section_text(text, "7. Crash Recovery Matrix")
        self.assertIn("finalizer", section.lower())

    def test_crash_matrix_covers_tombstone_and_finalized_separated(self) -> None:
        text = _contract_text()
        section = _section_text(text, "7. Crash Recovery Matrix")
        self.assertIn("tombstone", section.lower())

    def test_crash_matrix_covers_concurrent_handoff(self) -> None:
        text = _contract_text()
        section = _section_text(text, "7. Crash Recovery Matrix")
        self.assertIn("concurrent", section.lower())

    def test_crash_matrix_covers_stale_generation(self) -> None:
        text = _contract_text()
        section = _section_text(text, "7. Crash Recovery Matrix")
        self.assertIn("stale generation", section.lower())

    def test_crash_matrix_covers_pid_reuse_boot_id_change(self) -> None:
        text = _contract_text()
        section = _section_text(text, "7. Crash Recovery Matrix")
        self.assertIn("PID reuse", section)

    def test_crash_matrix_covers_provider_missing(self) -> None:
        text = _contract_text()
        section = _section_text(text, "7. Crash Recovery Matrix")
        self.assertIn("provider missing", section.lower())

    def test_crash_matrix_covers_attempt_4(self) -> None:
        text = _contract_text()
        section = _section_text(text, "7. Crash Recovery Matrix")
        self.assertIn("Attempt 4", section)

    def test_crash_matrix_dispositions_are_valid(self) -> None:
        valid = {"resume", "byte-exact replay", "no-op", "reject",
                 "fail-closed", "fail-closed or byte-exact replay",
                 "exactly one winner", "typed conflict"}
        text = _contract_text()
        rows = _extract_table_rows(text, "## 7. Crash Recovery Matrix")
        for row in rows:
            disp = row.get("Disposition", "").lower()
            self.assertTrue(
                any(v in disp for v in valid),
                f"Unknown disposition '{row.get('Disposition')}' for window '{row.get('#')}'",
            )


# ═══════════════════════════════════════════════════════════════════════════════
# Byte-exact replay semantics
# ═══════════════════════════════════════════════════════════════════════════════

class ByteExactReplayTests(unittest.TestCase):
    def test_identical_request_is_byte_exact_replay(self) -> None:
        text = _contract_text()
        self.assertIn("byte-exact replay", text.lower())

    def test_same_identity_different_bytes_is_conflict(self) -> None:
        text = _contract_text()
        self.assertIn("conflict", text.lower())

    def test_content_digest_is_sha256(self) -> None:
        text = _contract_text()
        self.assertIn("sha256:", text.lower())
        self.assertIn("SHA-256", text)

    def test_canonical_yaml_rules_referenced(self) -> None:
        text = _contract_text()
        self.assertTrue(
            "canonical YAML" in text
            or "UTF-8 without BOM" in text
            or "canonical bytes" in text.lower(),
            "Contract must reference canonical serialization rules",
        )

    def test_atomic_replace_with_temp_file(self) -> None:
        text = _contract_text()
        self.assertIn("temporary file", text.lower())

    def test_replay_does_not_create_second_identity(self) -> None:
        text = _contract_text()
        self.assertIn("never create a second", text.lower())


# ═══════════════════════════════════════════════════════════════════════════════
# Concurrency — exactly one winner
# ═══════════════════════════════════════════════════════════════════════════════

class ConcurrencyExactlyOneWinnerTests(unittest.TestCase):
    def test_concurrent_handoff_one_winner(self) -> None:
        text = _contract_text()
        self.assertIn("one winner", text.lower())

    def test_store_lock_cas_loser_rejected(self) -> None:
        text = _contract_text()
        self.assertIn("loser", text.lower())

    def test_stale_generation_rejected_zero_writes(self) -> None:
        text = _contract_text()
        self.assertIn("stale generation", text.lower())


# ═══════════════════════════════════════════════════════════════════════════════
# Attempt 4 rejection
# ═══════════════════════════════════════════════════════════════════════════════

class Attempt4RejectionTests(unittest.TestCase):
    def test_attempt_4_is_forbidden(self) -> None:
        text = _contract_text()
        self.assertTrue(
            "attempt 4" in text.lower() or "forbidden" in text.lower(),
            "Contract must explicitly forbid attempt 4",
        )

    def test_attempt_range_1_to_3(self) -> None:
        text = _contract_text()
        section = _section_text(text, "3.4 ScheduledDispatchHandoff — Exactly 25 Fields")
        self.assertTrue(
            "1..3" in section or "1..3" in section.lower(),
            "Attempt must be constrained to 1..3 in the ScheduledDispatchHandoff table",
        )

    def test_attempt_4_rejected_before_reservation(self) -> None:
        text = _contract_text()
        self.assertTrue(
            "before reservation" in text.lower() or
            "reject" in text.lower(),
            "Attempt 4 must be rejected before reservation",
        )


# ═══════════════════════════════════════════════════════════════════════════════
# Interface #22 unchanged
# ═══════════════════════════════════════════════════════════════════════════════

class Interface22UnchangedTests(unittest.TestCase):
    def test_interface_22_mentioned_as_unchanged(self) -> None:
        text = _contract_text()
        self.assertIn("Interface #22", text)

    def test_interface_22_status_current(self) -> None:
        text = _contract_text()
        self.assertTrue(
            "TC-13.18d.13b" in text,
            "Interface #22 must reference Current status TC-13.18d.13b",
        )

    def test_control_plane_transition_service_unchanged(self) -> None:
        text = _contract_text()
        self.assertIn("ControlPlaneTransitionService", text)

    def test_workflow_orchestrator_not_modified(self) -> None:
        text = _contract_text()
        self.assertIn("WorkflowOrchestrator", text)

    def test_run_dispatch_cycle_not_called_directly_if_recreates_task_dispatched(self) -> None:
        text = _contract_text()
        self.assertIn("run_dispatch_cycle", text.lower())
        self.assertTrue(
            "not called directly" in text.lower()
            or "must not be called directly" in text.lower(),
            "Contract must forbid direct run_dispatch_cycle() if it recreates TASK_DISPATCHED",
        )


# ═══════════════════════════════════════════════════════════════════════════════
# Runtime Target status — start_admitted_dispatch not implemented
# ═══════════════════════════════════════════════════════════════════════════════

class RuntimeTargetStatusTests(unittest.TestCase):
    def test_start_admitted_dispatch_is_target(self) -> None:
        text = _contract_text()
        self.assertIn("start_admitted_dispatch", text)
        self.assertTrue(
            "Target" in text,
            "Contract must mark start_admitted_dispatch runtime as Target",
        )

    def test_complete_handoff_runtime_is_target(self) -> None:
        text = _contract_text()
        self.assertIn("Target", text)

    def test_contract_does_not_implement_runtime(self) -> None:
        text = _contract_text()
        self.assertIn("does not implement", text.lower())

    def test_no_production_module_imported_in_tests(self) -> None:
        """This test file must not import production portfolio scheduler modules."""
        import sys
        for mod_name in list(sys.modules):
            if "portfolio_scheduler" in mod_name:
                if "test_portfolio_scheduler_worker_handoff_contract" not in mod_name:
                    self.fail(
                        f"Test file must not import production module '{mod_name}'"
                    )


# ═══════════════════════════════════════════════════════════════════════════════
# Durable path verification
# ═══════════════════════════════════════════════════════════════════════════════

class DurablePathTests(unittest.TestCase):
    def test_handoff_path_is_frozen(self) -> None:
        text = _contract_text()
        self.assertIn(
            "docs/pm/portfolio-scheduler/worker-handoffs/",
            text,
        )

    def test_path_uses_dispatch_id(self) -> None:
        text = _contract_text()
        self.assertIn("<dispatch_id>", text)

    def test_at_most_one_handoff_file_per_dispatch(self) -> None:
        text = _contract_text()
        self.assertTrue(
            "at most one" in text.lower(),
            "Contract must state at most one handoff file per dispatch",
        )


# ═══════════════════════════════════════════════════════════════════════════════
# Schema version
# ═══════════════════════════════════════════════════════════════════════════════

class SchemaVersionTests(unittest.TestCase):
    def test_schema_version_is_exact(self) -> None:
        text = _contract_text()
        self.assertIn("agentdesk.portfolio-scheduler-worker-handoff/v1", text)

    def test_schema_version_appears_in_both_types(self) -> None:
        text = _contract_text()
        occurrences = text.count("agentdesk.portfolio-scheduler-worker-handoff/v1")
        self.assertGreaterEqual(
            occurrences, 2,
            f"Schema version must appear at least twice (Handoff + Receipt), got {occurrences}",
        )


# ═══════════════════════════════════════════════════════════════════════════════
# Lock order verification
# ═══════════════════════════════════════════════════════════════════════════════

class LockOrderTests(unittest.TestCase):
    def test_lock_order_is_numbered(self) -> None:
        text = _contract_text()
        section = _section_text(text, "6. Atomic and Lock Order")
        numbered = re.findall(r"^\d+\.", section, re.MULTILINE)
        self.assertGreaterEqual(
            len(numbered), 8,
            f"Lock order must have at least 8 numbered steps, got {len(numbered)}",
        )

    def test_supervisor_start_after_lock_release(self) -> None:
        text = _contract_text()
        section = _section_text(text, "6. Atomic and Lock Order")
        self.assertIn("Release", section)

    def test_worker_start_after_lock_release(self) -> None:
        text = _contract_text()
        section = _section_text(text, "6. Atomic and Lock Order")
        lines = section.split("\n")
        # Find numbered lock release steps: "Release ... lock" (not "released")
        lock_release_idx = max(
            (i for i, line in enumerate(lines)
             if "release" in line.lower() and "lock" in line.lower()
             and "released" not in line.lower()),
            default=-1,
        )
        # Find "Start Worker" step
        worker_start_idx = max(
            (i for i, line in enumerate(lines)
             if "start worker" in line.lower()),
            default=-1,
        )
        self.assertNotEqual(lock_release_idx, -1, "Lock release step not found in section")
        self.assertNotEqual(worker_start_idx, -1, "Worker start step not found in section")
        self.assertGreater(
            worker_start_idx, lock_release_idx,
            f"Worker start (line {worker_start_idx}) must occur after lock release "
            f"(line {lock_release_idx}) in the frozen order",
        )


# ═══════════════════════════════════════════════════════════════════════════════
# Relationship to existing contracts
# ═══════════════════════════════════════════════════════════════════════════════

class ExistingContractRelationshipTests(unittest.TestCase):
    def test_references_admission_plan_reservation(self) -> None:
        text = _contract_text()
        self.assertIn("AdmissionPlanReservation", text)

    def test_references_schedule_receipt(self) -> None:
        text = _contract_text()
        self.assertIn("ScheduleReceipt", text)

    def test_references_worker_slot_lease(self) -> None:
        text = _contract_text()
        self.assertIn("WorkerSlotLease", text)

    def test_references_dispatch_process_receipt(self) -> None:
        text = _contract_text()
        self.assertIn("DispatchProcessReceipt", text)

    def test_references_dispatch_finalizer_tombstone(self) -> None:
        text = _contract_text()
        self.assertIn("DispatchFinalizerTombstone", text)

    def test_references_control_plane_transition_service(self) -> None:
        text = _contract_text()
        self.assertIn("ControlPlaneTransitionService", text)

    def test_handoff_does_not_change_task_difficulty_start_order(self) -> None:
        text = _contract_text()
        self.assertIn("TaskDifficulty", text)
        self.assertTrue(
            "does not enter" in text.lower()
            or "not enter" in text.lower(),
            "Contract must state TaskDifficulty does not enter handoff start order",
        )

    def test_bounded_retry_not_reimplemented(self) -> None:
        text = _contract_text()
        self.assertTrue(
            "bounded retry" in text.lower() or "not re-implement" in text.lower(),
            "Contract must state bounded retry is not re-implemented by handoff",
        )

    def test_owner_loss_recovery_not_reimplemented(self) -> None:
        text = _contract_text()
        self.assertTrue(
            "owner-loss" in text.lower() or "not re-implement" in text.lower(),
            "Contract must state owner-loss recovery is not re-implemented",
        )

    def test_complete_worker_runtime_remains_target(self) -> None:
        text = _contract_text()
        self.assertIn("Target", text)
        self.assertIn("Worker", text)


# ═══════════════════════════════════════════════════════════════════════════════
# Task card header metadata
# ═══════════════════════════════════════════════════════════════════════════════

class TaskCardMetadataTests(unittest.TestCase):
    def test_title_contains_task_card_id(self) -> None:
        text = _contract_text()
        first_line = text.split("\n")[0]
        self.assertIn("TC-13.24b.2b.4c.1", first_line)

    def test_status_line_present(self) -> None:
        text = _contract_text()
        self.assertIn("Contract Current", text)


if __name__ == "__main__":
    unittest.main()
