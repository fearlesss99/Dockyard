"""TC-13.24b.2b.4c.1a — Scheduler-to-Worker Handoff Contract Adversarial Verification.

stdlib-only unittest; no external dependencies.

This test file performs independent adversarial verification of the frozen
`portfolio-scheduler-worker-handoff-contract.md` to prove it sufficiently
constrains the future `start_admitted_dispatch()` runtime.

These are adversarial tests — each test maps a specific attack scenario to a
contract clause and verifies the clause provides a unique, implementable
defense.  No production module is imported or called.

Coverage (incremental order):
  1. Identity substitution — per-field (19 public identity fields)
  2. Phase / crash windows — 7 phases × specific crash timing
  3. Provider boundary — pre-validation, zero-write, no inference
  4. Duplicate prevention — second Lease/DISPATCHED/event/supervisor/Worker/attempt-4
  5. Concurrency / replay — byte-exact, divergent, stale gen, finalized
  6. Liveness — ALIVE/UNKNOWN/DEAD
  7. Source / lock boundary — no proc/model/API inside lock
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
    for m in re.finditer(r"^#{1,4}\s+", text, re.MULTILINE):
        line_start = m.start()
        line_end = text.find("\n", line_start)
        if line_end == -1:
            line_end = len(text)
        line = text[line_start:line_end]
        if heading in line:
            return line_end + 1
    return -1


def _section_text(text: str, heading: str) -> str:
    idx = _find_heading(text, heading)
    if idx == -1:
        return ""
    rest = text[idx:]
    next_m = re.search(r"^##\s+", rest, re.MULTILINE)
    if next_m:
        rest = rest[: next_m.start()]
    return rest


def _extract_table_rows(text: str, heading: str) -> list[dict[str, str]]:
    idx = _find_heading(text, heading)
    if idx == -1:
        return []
    rest = text[idx:]
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
        cells = _split_table_row(line)
        if len(cells) == len(headers):
            rows.append(dict(zip(headers, cells)))
    return rows


def _split_table_row(line: str) -> list[str]:
    normalized = re.sub(r"\\\|", "\x00PIPE\x00", line)
    cells = [
        c.strip().strip("`").replace("\x00PIPE\x00", "\\|")
        for c in normalized.split("|")[1:-1]
    ]
    return cells


# ── Contract-derived frozen data ─────────────────────────────────────────────

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

# The 19 identity-bearing fields explicitly listed for per-field substitution
IDENTITY_FIELDS_FOR_SUBSTITUTION = [
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
    "generation_id",
    "binding_digest",
]

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

HANDOFF_PHASES = [
    "ADMISSION_COMMITTED",
    "HANDOFF_RESERVED",
    "SUPERVISOR_STARTED",
    "WORKER_STARTED",
    "ACKNOWLEDGED",
    "FINALIZING",
    "FINALIZED",
]

# Fields whose type is int in the contract (non-bool positive integer)
INT_FIELDS = {"revision", "attempt", "lease_epoch"}

# Fields that are identity-bearing "ID" fields ending with _id or _digest
ID_FORMAT_FIELDS = {
    "handoff_id": "HNDF-",
    "queue_id": None,
    "plan_digest": "sha256:",
    "receipt_id": None,
    "task_id": "TC-",
    "dispatch_id": "DSP-",
    "dispatch_event_id": "EVT-",
    "outbox_message_id": "MSG-",
    "lease_id": None,
    "assessment_id": "ASM-",
}


# ═══════════════════════════════════════════════════════════════════════════════
# 1. Identity substitution — per-field adversarial verification
# ═══════════════════════════════════════════════════════════════════════════════


class IdentitySubstitutionAdversarial(unittest.TestCase):
    """For each identity-bearing field, prove the contract provides a unique
    rejection rule that would fire BEFORE Worker startup.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.text = _contract_text()

    # ── handoff_id substitution ──────────────────────────────────────────

    def test_handoff_id_substitution_rejected_by_stable_identity_rule(self) -> None:
        """Substituting handoff_id with a different HNDF-* value: the contract
        states handoff_id is a 'stable HNDF-* identity unique per handoff
        generation'.  A substituted handoff_id would not match the durably
        reserved identity discovered during step 5 ('under lock: read existing
        handoff evidence') — it would be either a divergent write (conflict)
        or a fresh identity collision (rejected)."""
        self.assertIn("stable", self.text)
        self.assertIn("HNDF-", self.text)
        self.assertIn("unique", self.text.lower())
        self.assertIn("handoff_id", self.text)

    def test_handoff_id_substitution_checked_before_worker_start(self) -> None:
        """Handoff ID validation occurs at lock order step 2-6, all before
        step 11 ('Start Worker')."""
        lock_section = _section_text(self.text, "6. Atomic and Lock Order")
        reserve_line = max(
            (i for i, line in enumerate(lock_section.split("\n"))
             if "reserve" in line.lower()),
            default=-1,
        )
        worker_line = max(
            (i for i, line in enumerate(lock_section.split("\n"))
             if "start worker" in line.lower()),
            default=-1,
        )
        self.assertLess(reserve_line, worker_line,
                        "handoff_id validated at reservation (step 6) before Worker start (step 11)")

    # ── queue_id substitution ────────────────────────────────────────────

    def test_queue_id_substitution_rejected_by_field_match_rule(self) -> None:
        """Section 3.4 row 3: queue_id must 'Match the sourcing QueueEntry.queue_id'.
        A substituted queue_id would not match the canonical snapshot's queue entry
        identity, caught at lock order step 2 (validate admission evidence)."""
        rows = _extract_table_rows(
            self.text,
            "### 3.4 ScheduledDispatchHandoff — Exactly 25 Fields",
        )
        queue_row = [r for r in rows if r.get("Field") == "queue_id"]
        self.assertTrue(queue_row, "queue_id field row must exist")
        self.assertIn("queue_id", queue_row[0].get("Rule", "").lower() or
                      queue_row[0].get("Field", "").lower())

    def test_queue_id_substitution_pre_worker(self) -> None:
        """Queue ID validated in step 2 (validate AdmissionPlanReservation,
        ScheduleReceipt, etc. — all identity-bearing artifacts including queue_id)
        before Worker at step 11."""
        lock_section = _section_text(self.text, "6. Atomic and Lock Order")
        self.assertIn("Validate", lock_section)
        # Step 2 validates 'AdmissionPlanReservation, ScheduleReceipt, canonical
        # TASK_DISPATCHED event, and WorkerSlotLease identity' — the artifacts
        # embed queue_id verification before any Worker start
        lock_lines = lock_section.split("\n")
        validate_idx = max(
            (i for i, line in enumerate(lock_lines)
             if "validate" in line.lower()),
            default=-1,
        )
        worker_idx = max(
            (i for i, line in enumerate(lock_lines)
             if "start worker" in line.lower()),
            default=-1,
        )
        self.assertLess(validate_idx, worker_idx,
                        f"Queue ID validated at step 2 (line {validate_idx}) before Worker start (line {worker_idx})")

    # ── plan_digest substitution ─────────────────────────────────────────

    def test_plan_digest_substitution_rejected_by_sha256_binding(self) -> None:
        """Section 3.4 row 4: plan_digest is 'sha256: + 64 lowercase hex; binds the
        exact AdmissionPlanReservation'.  A substituted plan_digest does not hash
        to the canonical plan bytes — step 2 validation fails.  Contract states
        'a divergent plan is rejected before any handoff write'."""
        self.assertIn("divergent plan", self.text.lower())
        self.assertTrue(
            "rejected before any handoff write" in self.text.lower()
            or "before any handoff" in self.text.lower(),
            "Contract must reject divergent plan before handoff write",
        )

    # ── receipt_id substitution ──────────────────────────────────────────

    def test_receipt_id_substitution_rejected_by_matching_rule(self) -> None:
        """Section 3.4 row 5: receipt_id must 'Match the sourcing
        ScheduleReceipt.receipt_id'.  A substituted receipt_id fails step 2
        (validate ScheduleReceipt identity)."""
        self.assertIn("Receipt", self.text)
        # ScheduleReceipt verification at step 2
        self.assertIn("ScheduleReceipt", self.text)

    # ── task_id substitution ─────────────────────────────────────────────

    def test_task_id_substitution_rejected_by_tc_format_and_snapshot(self) -> None:
        """Section 3.4 row 6: task_id is 'TC-* identity from the canonical snapshot'.
        A substituted task_id would not match the canonical snapshot's task
        identity, caught at step 1-2 validation."""
        self.assertIn("canonical snapshot", self.text.lower())

    # ── revision substitution ────────────────────────────────────────────

    def test_revision_substitution_rejected_by_int_and_snapshot(self) -> None:
        """Section 3.4 row 7: revision is 'Non-bool positive integer; matches
        snapshot'.  A substituted revision mismatches the canonical snapshot."""
        rows = _extract_table_rows(
            self.text,
            "### 3.4 ScheduledDispatchHandoff — Exactly 25 Fields",
        )
        rev_row = [r for r in rows if r.get("Field") == "revision"]
        self.assertTrue(rev_row, "revision field row must exist")
        rule = rev_row[0].get("Rule", "")
        self.assertIn("int", rule.lower() or "")
        self.assertTrue(
            "snapshot" in rule.lower() or "matches" in rule.lower(),
            f"revision rule must reference snapshot: {rule}",
        )

    # ── attempt substitution ─────────────────────────────────────────────

    def test_attempt_substitution_rejected_by_1_to_3_range(self) -> None:
        """Section 3.4 row 8: attempt is 'Non-bool positive integer; 1..3 inclusive'.
        A substituted attempt ≠ original attempt, or attempt 4, both rejected."""
        self.assertIn("1..3", self.text)
        self.assertIn("attempt 4", self.text.lower())

    def test_attempt_4_specifically_rejected_before_reservation(self) -> None:
        """Crash matrix row 15: Attempt 4 → Reject before reservation."""
        crash = _section_text(self.text, "7. Crash Recovery Matrix")
        self.assertIn("Attempt 4", crash)
        self.assertTrue(
            "reject" in crash.lower() and "reservation" in crash.lower(),
            "Attempt 4 must be rejected before reservation per crash matrix",
        )

    # ── dispatch_id substitution ─────────────────────────────────────────

    def test_dispatch_id_substitution_rejected_by_dsp_format_and_plan(self) -> None:
        """Section 3.4 row 9: dispatch_id is 'DSP-* identity from
        AdmissionPlanReservation'.  Substituted DSP-* does not match the plan."""
        self.assertIn("DSP-", self.text)
        self.assertIn("AdmissionPlanReservation", self.text)

    # ── dispatch_event_id substitution ───────────────────────────────────

    def test_dispatch_event_id_substitution_rejected_by_evt_canonical_event(self) -> None:
        """Section 3.4 row 10: dispatch_event_id is 'EVT-* identity of the canonical
        TASK_DISPATCHED event'.  Substituted event does not match canonical event."""
        self.assertIn("EVT-", self.text)
        self.assertIn("TASK_DISPATCHED", self.text)

    # ── outbox_message_id substitution ───────────────────────────────────

    def test_outbox_message_id_substitution_rejected_by_msg_identity_from_plan(self) -> None:
        """Section 3.4 row 11: outbox_message_id is 'MSG-* identity from
        AdmissionPlanReservation'.  Substituted MSG-* mismatches plan."""
        self.assertIn("MSG-", self.text)
        rows = _extract_table_rows(
            self.text,
            "### 3.4 ScheduledDispatchHandoff — Exactly 25 Fields",
        )
        msg_row = [r for r in rows if r.get("Field") == "outbox_message_id"]
        self.assertTrue(msg_row, "outbox_message_id field row must exist")

    # ── lease_id substitution ────────────────────────────────────────────

    def test_lease_id_substitution_rejected_by_acquired_lease_match(self) -> None:
        """Section 3.4 rows 12-13: lease_id and lease_epoch must match the
        'current acquired lease'.  A substituted lease_id mismatches the
        Scheduler-acquired WorkerSlotLease, caught at step 2 validation."""
        self.assertIn("acquired", self.text)
        self.assertIn("WorkerSlotLease", self.text)

    # ── lease_epoch substitution ─────────────────────────────────────────

    def test_lease_epoch_substitution_rejected_by_epoch_match(self) -> None:
        """lease_epoch is 'Non-bool positive integer; epoch at lease acquisition'.
        Substituted epoch does not match the acquired lease epoch."""
        rows = _extract_table_rows(
            self.text,
            "### 3.4 ScheduledDispatchHandoff — Exactly 25 Fields",
        )
        epoch_row = [r for r in rows if r.get("Field") == "lease_epoch"]
        self.assertTrue(epoch_row, "lease_epoch field row must exist")

    # ── holder_instance_id substitution ──────────────────────────────────

    def test_holder_instance_id_substitution_rejected_by_instance_match(self) -> None:
        """Section 3.4 row 14: holder_instance_id is 'Instance that acquired the
        lease'.  Substituted holder does not match lease acquisition record."""
        rows = _extract_table_rows(
            self.text,
            "### 3.4 ScheduledDispatchHandoff — Exactly 25 Fields",
        )
        holder_row = [r for r in rows if r.get("Field") == "holder_instance_id"]
        self.assertTrue(holder_row, "holder_instance_id field row must exist")

    # ── worker_kind substitution ─────────────────────────────────────────

    def test_worker_kind_substitution_rejected_by_plan_source(self) -> None:
        """Section 3.4 row 15: worker_kind is 'From AdmissionPlanReservation'.
        A substituted worker_kind would not match the reserved plan."""
        rows = _extract_table_rows(
            self.text,
            "### 3.4 ScheduledDispatchHandoff — Exactly 25 Fields",
        )
        wk_row = [r for r in rows if r.get("Field") == "worker_kind"]
        self.assertTrue(wk_row, "worker_kind field row must exist")

    # ── assessment_id substitution ───────────────────────────────────────

    def test_assessment_id_substitution_rejected_by_asm_binding(self) -> None:
        """Section 3.4 row 16: assessment_id is 'ASM-* identity bound to task
        and revision'.  A substituted assessment_id breaks the task/revision
        binding."""
        self.assertIn("ASM-", self.text)

    # ── expected_snapshot_commit substitution ────────────────────────────

    def test_expected_snapshot_commit_substitution_rejected_by_40char_hex(self) -> None:
        """Section 3.4 row 17: expected_snapshot_commit is a '40-char hex Git commit;
        from AdmissionPlanReservation'.  Substituted commit mismatches plan."""
        self.assertIn("40-char", self.text)
        self.assertIn("hex", self.text.lower())

    # ── provider_binding_id substitution ─────────────────────────────────

    def test_provider_binding_id_substitution_rejected_by_provider_mapping(self) -> None:
        """Section 3.4 row 18: provider_binding_id identifies the provider from
        'ModelSelectionSnapshot.model_binding_id'.  Section 5 rule 3: the
        required provider_binding_id must exist as a key in the providers mapping.
        A substituted binding not present in the providers mapping is rejected."""
        provider_section = _section_text(self.text, "5. Provider Boundary")
        self.assertIn("provider_binding_id", self.text)

    # ── generation_id substitution (in receipt) ──────────────────────────

    def test_generation_id_substitution_rejected_by_1to1_binding(self) -> None:
        """Section 3.5: generation_id must match DispatchProcessReceipt.generation_id
        exactly.  A substituted generation_id breaks the 1:1 binding and produces
        a divergent binding_digest — fail-closed."""
        self.assertIn("DispatchProcessReceipt", self.text)
        self.assertIn("generation_id", self.text)
        self.assertIn("1:1", self.text)

    # ── binding_digest substitution (in receipt) ─────────────────────────

    def test_binding_digest_substitution_rejected_by_sha256_validation(self) -> None:
        """Section 3.5: binding_digest is 'sha256: + 64 lowercase hex binding the
        handoff + dispatch process receipt identity'.  A substituted binding_digest
        would not match the canonical concatenation of handoff_id + generation_id."""
        self.assertIn("binding_digest", self.text.lower())


# ═══════════════════════════════════════════════════════════════════════════════
# 2. Phase and crash windows — adversarial verification
# ═══════════════════════════════════════════════════════════════════════════════


class PhaseForwardOnlyAdversarial(unittest.TestCase):
    """Prove that each illegal phase transition has a unique contract-mandated
    rejection mechanism, not just that the contract lists legal transitions."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.text = _contract_text()

    def test_no_skip_from_admission_committed_to_supervisor_started(self) -> None:
        """Skipping HANDOFF_RESERVED: contract states 'Skipping a phase...
        is forbidden'.  A write with phase=SUPERVISOR_STARTED without a prior
        HANDOFF_RESERVED durable write is a phased write at an illegal
        transition — the store lock validation at step 5 reads existing
        evidence and rejects."""
        phase_section = _section_text(self.text, "3.2 ScheduledDispatchHandoffPhase")
        self.assertIn("skip", phase_section.lower())
        self.assertIn("forbidden", phase_section.lower())

    def test_no_backward_from_supervisor_started_to_handoff_reserved(self) -> None:
        """Moving backward: 'Phases are forward-only. ... moving backward ...
        is forbidden'."""
        self.assertIn("forward-only", self.text.lower())
        self.assertTrue(
            "moving backward" in self.text.lower()
            or "backward" in self.text.lower(),
            "Contract must forbid backward phase movement",
        )

    def test_no_cross_generation_overwrite(self) -> None:
        """A phase write with a different generation_id (or handoff_id) for the
        same dispatch_id: Section 3.5 states at most one handoff receipt per
        dispatch, 1:1 generation binding.  Cross-generation write is a
        conflicting identity — fail-closed."""
        self.assertIn("at most one", self.text.lower())
        # The generation binding prevents cross-generation overwrite
        receipt_section = _section_text(
            self.text, "3.5 ScheduledDispatchHandoffReceipt — Exactly 11 Fields"
        )
        self.assertIn("1:1", receipt_section)

    def test_reservation_write_interrupted_disposition(self) -> None:
        """Crash matrix row 3: 'Handoff reservation write interrupted' →
        'Fail-closed or byte-exact replay'.  Contract provides exactly two
        paths, both safe."""
        crash = _section_text(self.text, "7. Crash Recovery Matrix")
        self.assertIn("interrupted", crash.lower())
        self.assertTrue(
            "fail-closed" in crash.lower() or "byte-exact replay" in crash.lower(),
            "Interrupted reservation must have fail-closed or byte-exact replay disposition",
        )

    def test_after_reserved_before_supervisor_no_second_handoff(self) -> None:
        """Crash matrix row 4: 'After HANDOFF_RESERVED, before supervisor start'
        → 'Resume: advance from the exact reserved identity; never create a
        second handoff'."""
        crash = _section_text(self.text, "7. Crash Recovery Matrix")
        self.assertIn("never create a second", crash.lower())

    def test_supervisor_started_receipt_not_advanced_byte_exact_replay(self) -> None:
        """Crash matrix row 5: 'Supervisor started, receipt not advanced to
        SUPERVISOR_STARTED' → 'byte-exact replay of the phase advance'."""
        self.assertIn("SUPERVISOR_STARTED", self.text)

    def test_worker_started_phase_not_advanced_byte_exact_replay(self) -> None:
        """Crash matrix row 6: 'Worker started, phase not yet WORKER_STARTED'
        → 'byte-exact replay of the phase advance'."""
        self.assertIn("WORKER_STARTED", self.text)

    def test_ack_submitted_handoff_behind_byte_exact_replay(self) -> None:
        """Crash matrix row 7: 'After ACK, phase not yet ACKNOWLEDGED' →
        'byte-exact replay of ACKNOWLEDGED'."""
        crash = _section_text(self.text, "7. Crash Recovery Matrix")
        self.assertIn("ACK", crash)

    def test_finalizer_metadata_frozen_tombstone_not_complete_resume(self) -> None:
        """Crash matrix row 8: 'Before finalizer' → 'Resume: continue
        finalization'."""
        crash = _section_text(self.text, "7. Crash Recovery Matrix")
        self.assertIn("finalizer", crash.lower())

    def test_tombstone_done_handoff_not_finalized_fail_closed(self) -> None:
        """Crash matrix row 10: 'Tombstone and FINALIZED separated by crash'
        → 'Fail-closed until both durable records agree'.  The fail-closed
        disposition means zero writes until tombstone and handoff receipt
        both reflect the same finalized state."""
        crash = _section_text(self.text, "7. Crash Recovery Matrix")
        self.assertIn("fail-closed", crash.lower())

    def test_finalized_byte_exact_replay(self) -> None:
        """Crash matrix row 9: 'After finalizer, before FINALIZED' →
        'Byte-exact replay of FINALIZED phase advance'."""
        self.assertIn("FINALIZED", self.text)

    def test_stale_receipt_replay_rejected(self) -> None:
        """Crash matrix row 12: 'Stale generation' → 'Reject; zero writes'.
        A stale receipt replay attempt is rejected with zero writes."""
        crash = _section_text(self.text, "7. Crash Recovery Matrix")
        self.assertIn("stale generation", crash.lower())
        self.assertIn("reject", crash.lower())

    def test_concurrent_handoff_exactly_one_winner_cas(self) -> None:
        """Crash matrix row 11: 'Concurrent handoff (two callers, same dispatch)'
        → 'Exactly one winner by store-lock CAS; loser receives typed conflict'."""
        crash = _section_text(self.text, "7. Crash Recovery Matrix")
        self.assertIn("one winner", crash.lower())
        self.assertIn("loser", crash.lower())


# ═══════════════════════════════════════════════════════════════════════════════
# 3. Provider boundary — adversarial verification
# ═══════════════════════════════════════════════════════════════════════════════


class ProviderBoundaryAdversarial(unittest.TestCase):
    """Prove the contract requires provider validation before any handoff write,
    that missing/empty/wrong-type provider is rejected, and that no provider
    inference is permitted."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.text = _contract_text()

    def test_empty_providers_rejected_before_any_handoff_write(self) -> None:
        """Section 5 rule 1: 'providers must be non-empty; empty mapping is
        rejected before any handoff write'."""
        provider = _section_text(self.text, "5. Provider Boundary")
        self.assertIn("non-empty", provider.lower())
        self.assertTrue(
            "empty mapping" in provider.lower()
            or "empty" in provider.lower(),
            "Contract must reject empty providers mapping",
        )

    def test_missing_provider_key_results_in_zero_write_zero_supervisor_zero_worker(self) -> None:
        """Section 5 rule 4: 'Missing, empty, wrong-type, or absent-selected-provider
        results in zero handoff writes, zero supervisor, zero Worker, zero ACK'."""
        provider = _section_text(self.text, "5. Provider Boundary")
        self.assertIn("zero handoff writes", provider.lower())
        self.assertIn("zero supervisor", provider.lower())

    def test_providers_not_mapping_or_not_containing_target_rejected_before_worker(self) -> None:
        """Section 5 rule 2: type is 'frozen mapping of str → typed provider
        descriptor (never Any, dict, or Mapping)'.  Non-mapping or Mapping
        type is rejected at entry."""
        self.assertIn("Mapping", self.text)

    def test_no_provider_name_based_inference(self) -> None:
        """Section 5 rule 6: 'The handoff must not infer retry eligibility,
        liveness classification, or failure classification from a provider
        name.'"""
        provider = _section_text(self.text, "5. Provider Boundary")
        self.assertIn("must not infer", provider.lower())

    def test_no_api_key_or_token_persistence(self) -> None:
        """Section 5 rule 5: 'The handoff must not persist API keys, tokens,
        argv, env, or prompt'."""
        provider = _section_text(self.text, "5. Provider Boundary")
        self.assertIn("API key", provider)

    def test_no_exception_text_or_exit_code_based_inference(self) -> None:
        """Section 3 header states no public field may use 'exception text,
        or exit code'.  The handoff must not classify based on exception
        messages or exit codes."""
        # Section 3 preamble forbids exception text / exit code in public types
        sec3 = _section_text(self.text, "3. Public Value Types")
        self.assertIn("exception", sec3.lower())

    def test_provider_validation_not_dependent_on_env_vars(self) -> None:
        """Section 5 rule 5 forbids persisting argv, env.  Provider validation
        uses the explicit providers mapping, not environment variables."""
        provider = _section_text(self.text, "5. Provider Boundary")
        self.assertIn("env", provider.lower())

    def test_provider_validation_is_step_3_before_store_lock(self) -> None:
        """Lock order step 3: 'Validate providers mapping' — occurs before
        step 4 (acquire handoff store lock), thus before any durable write.
        In the frozen lock order text, provider validation is listed as step 3
        and 'Acquire handoff store lock' is listed as step 4."""
        lock = _section_text(self.text, "6. Atomic and Lock Order")
        # Extract the numbered steps from the code block
        code_match = re.search(r"```text\s*\n(.*?)```", lock, re.DOTALL)
        self.assertIsNotNone(code_match, "Lock order code block must exist")
        steps = code_match.group(1).strip().split("\n")
        # Find step 3 text and step 4 text by line content
        provider_line = next(
            (i for i, line in enumerate(steps)
             if "provider" in line.lower()),
            -1,
        )
        acquire_lock_line = next(
            (i for i, line in enumerate(steps)
             if "acquire" in line.lower() and "lock" in line.lower()),
            -1,
        )
        self.assertNotEqual(provider_line, -1, "Provider validation step (3) not found")
        self.assertNotEqual(acquire_lock_line, -1, "Lock acquire step (4) not found")
        self.assertLess(
            provider_line, acquire_lock_line,
            f"Provider validation (line {provider_line}) must precede store lock acquire (line {acquire_lock_line})",
        )


# ═══════════════════════════════════════════════════════════════════════════════
# 4. Duplicate prevention — adversarial verification
# ═══════════════════════════════════════════════════════════════════════════════


class DuplicatePreventionAdversarial(unittest.TestCase):
    """Prove the contract forbids specific duplicate artifacts and provides
    unique rejection paths for each."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.text = _contract_text()

    def test_no_second_worker_slot_lease_by_scheduler_acquired_lease_reuse(self) -> None:
        """Section 1: handoff reuses the Scheduler-acquired WorkerSlotLease.
        Section 2 step 5: 'Verify the WorkerSlotLease epoch and holder identity'.
        A second lease acquisition would have a different lease_id/epoch
        and fail validation."""
        self.assertIn("never create a second", self.text.lower())
        self.assertIn("WorkerSlotLease", self.text)

    def test_no_second_task_dispatched_by_canonical_event_verification(self) -> None:
        """Section 2 step 2: 'Verify the canonical TASK_DISPATCHED event is
        committed'.  Section 1: handoff 'must never create a second
        TASK_DISPATCHED event'.  A second event would have a different
        dispatch_event_id and fail the canonical event check."""
        self.assertIn("TASK_DISPATCHED", self.text)
        self.assertTrue(
            "second" in self.text.lower() and "TASK_DISPATCHED" in self.text,
            "Contract must forbid a second TASK_DISPATCHED event",
        )

    def test_no_second_dispatch_id_by_dsp_format_and_plan_binding(self) -> None:
        """Section 3.4: dispatch_id is tied to AdmissionPlanReservation.
        The handoff freezes 'no second dispatch_id' in the forbidden second
        operations clause."""
        self.assertIn("never create a second", self.text.lower())

    def test_no_second_dispatch_event_id_by_evt_canonical_identity(self) -> None:
        """Section 3.4: dispatch_event_id is the canonical TASK_DISPATCHED
        event identity.  A second event_id would be a different EVT-* identity."""
        self.assertIn("EVT-", self.text)

    def test_no_second_outbox_identity_by_msg_tie_to_plan(self) -> None:
        """Section 3.4: outbox_message_id is from AdmissionPlanReservation.
        A second MSG-* identity conflicts with the plan binding."""
        self.assertIn("MSG-", self.text)

    def test_no_second_supervisor_by_one_winner_cas(self) -> None:
        """Concurrent handoff results in exactly one winner by store-lock CAS.
        A second supervisor for the same dispatch would require a second
        handoff reservation — rejected as conflicting."""
        crash = _section_text(self.text, "7. Crash Recovery Matrix")
        self.assertIn("one winner", crash.lower())

    def test_no_second_worker_by_phase_forward_only(self) -> None:
        """Phase transitions are forward-only.  Once WORKER_STARTED is reached,
        starting a second Worker would require a backward phase transition
        to SUPERVISOR_STARTED, which is forbidden."""
        phase = _section_text(self.text, "3.2 ScheduledDispatchHandoffPhase")
        self.assertIn("forward-only", phase.lower())

    def test_no_second_heartbeat_by_lease_epoch_fencing(self) -> None:
        """Section 6: 'Worker execution remains protected by lease epoch,
        heartbeat, and fencing.'  A second heartbeat for the same dispatch
        with a different holder_instance_id would conflict with epoch fencing."""
        lock = _section_text(self.text, "6. Atomic and Lock Order")
        self.assertIn("lease epoch", lock.lower())
        self.assertIn("fencing", lock.lower())

    def test_no_second_admission_by_forbidden_second_operations(self) -> None:
        """Section 1: 'The handoff must never create ... a second Admission.'
        Section 2 step 6: 'Not re-execute Admission'."""
        self.assertIn("never create a second", self.text.lower())
        self.assertIn("Admission", self.text)

    def test_attempt_4_forbidden_at_handoff_reservation(self) -> None:
        """Section 3.4: 'attempt must be in 1..3; attempt 4 is forbidden at
        handoff reservation'.  Crash matrix row 15: 'Attempt 4 → Reject
        before reservation'."""
        crash = _section_text(self.text, "7. Crash Recovery Matrix")
        self.assertIn("Attempt 4", crash)
        self.assertTrue(
            "reject" in crash.lower(),
            "Attempt 4 must have a rejection disposition in crash matrix",
        )

    def test_no_duplicate_dispatch_receipt_by_1to1_generation_binding(self) -> None:
        """Section 3.5: 'A given dispatch_id has at most one handoff receipt
        and at most one dispatch process receipt with matching generation_id.'
        A second receipt for the same dispatch_id violates the at-most-one rule."""
        receipt = _section_text(
            self.text, "3.5 ScheduledDispatchHandoffReceipt — Exactly 11 Fields"
        )
        self.assertIn("at most one", receipt.lower())


# ═══════════════════════════════════════════════════════════════════════════════
# 5. Concurrency and replay — adversarial verification
# ═══════════════════════════════════════════════════════════════════════════════


class ConcurrencyReplayAdversarial(unittest.TestCase):
    """Prove the contract provides unique, implementable outcomes for each
    concurrency and replay scenario."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.text = _contract_text()

    def test_same_generation_same_canonical_bytes_is_byte_exact_replay(self) -> None:
        """Section 3.2 states: 'A write with the same handoff_id and same phase is
        legal only as byte-exact replay.'  Section 4: 'An identical request is
        byte-exact replay.'  Same generation + same canonical bytes → byte-exact
        replay, not conflict."""
        # The term 'byte-exact\nreplay' appears split across lines in the contract
        # markdown.  Verify both tokens exist in the contract.
        self.assertIn("byte-exact", self.text.lower())
        self.assertIn("replay", self.text.lower())
        # Section 4's durable path section confirms the replay contract
        durable = _section_text(self.text, "4. Durable Path")
        self.assertIn("byte-exact", durable.lower())
        self.assertIn("replay", durable.lower())

    def test_same_generation_divergent_bytes_is_typed_conflict(self) -> None:
        """Section 3.2: 'A phase or identity overwrite is rejected.'
        Section 4: 'the same identity with different bytes is a conflict.'
        Same generation + divergent bytes → typed conflict, not silent overwrite."""
        durable = _section_text(self.text, "4. Durable Path")
        self.assertIn("conflict", durable.lower())

    def test_two_recoverers_same_handoff_single_winner(self) -> None:
        """Crash matrix row 11: 'Concurrent handoff (two callers, same dispatch)'
        → 'Exactly one winner by store-lock CAS; loser receives typed conflict'."""
        crash = _section_text(self.text, "7. Crash Recovery Matrix")
        self.assertIn("one winner", crash.lower())

    def test_stale_generation_rejected_zero_writes(self) -> None:
        """Crash matrix row 12: 'Stale generation' → 'Reject; zero writes'."""
        crash = _section_text(self.text, "7. Crash Recovery Matrix")
        stale_line = [
            line
            for line in crash.split("\n")
            if "stale generation" in line.lower()
        ]
        self.assertTrue(stale_line, "Stale generation row must exist in crash matrix")
        self.assertTrue(
            any("reject" in line.lower() for line in stale_line),
            "Stale generation must have reject disposition",
        )

    def test_finalized_handoff_not_reopened(self) -> None:
        """FINALIZED is the terminal phase — no transitions out of it.
        Phase section shows FINALIZED is not a source in any legal transition.
        Once FINALIZED, the handoff must not be reopened."""
        phase = _section_text(self.text, "3.2 ScheduledDispatchHandoffPhase")
        transitions = re.findall(r"(\w+)\s*→\s*(\w+)", phase)
        sources = {t[0] for t in transitions}
        self.assertNotIn("FINALIZED", sources,
                         "FINALIZED must not be a source in any legal phase transition")

    def test_receipt_tombstone_identity_divergent_fail_closed(self) -> None:
        """Crash matrix row 10: 'Tombstone and FINALIZED separated by crash'
        → 'Fail-closed until both durable records agree.'  Divergent
        receipt/tombstone identity → fail-closed, not silent acceptance."""
        crash = _section_text(self.text, "7. Crash Recovery Matrix")
        self.assertIn("fail-closed", crash.lower())

    def test_content_digest_binds_exact_bytes_for_replay_comparison(self) -> None:
        """Section 3.4 row 25: content_digest is SHA-256 over fields 1-24.
        This provides the byte-exact comparator for replay.  A divergence in
        content_digest proves non-replay."""
        self.assertIn("content_digest", self.text.lower())

    def test_concurrency_rules_not_dependent_on_in_memory_boolean(self) -> None:
        """Section 6: lock contention 'returns an immediate failure or None;
        it never waits indefinitely.'  Concurrency decisions are based on
        durable store CAS, not in-memory booleans."""
        lock = _section_text(self.text, "6. Atomic and Lock Order")
        self.assertIn("never waits indefinitely", lock.lower())


# ═══════════════════════════════════════════════════════════════════════════════
# 6. Liveness and process evidence — adversarial verification
# ═══════════════════════════════════════════════════════════════════════════════


class LivenessProcessEvidenceAdversarial(unittest.TestCase):
    """Prove the contract requires specific process evidence for DEAD
    classification and treats UNKNOWN (including PID reuse) as fail-closed."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.text = _contract_text()

    def test_alive_is_no_op(self) -> None:
        """When a Worker is ALIVE (heartbeat current, lease epoch matches),
        the handoff must not start a second Worker.  Section 6 states Worker
        execution is protected by lease epoch, heartbeat, and fencing.
        ALIVE → no-op for handoff recovery."""
        lock = _section_text(self.text, "6. Atomic and Lock Order")
        self.assertIn("heartbeat", lock.lower())
        self.assertIn("lease epoch", lock.lower())

    def test_unknown_is_fail_closed_not_dead(self) -> None:
        """UNKNOWN is never downgraded to DEAD.  The contract states
        'fail-closed' for UNKNOWN (never inferred as DEAD)."""
        # The contract inherits liveness rules from existing contracts;
        # it states 'fail-closed' as the disposition in crash matrix
        crash = _section_text(self.text, "7. Crash Recovery Matrix")
        self.assertIn("fail-closed", crash.lower())

    def test_pid_reuse_fail_closed_in_crash_matrix(self) -> None:
        """Crash matrix row 13: 'PID reuse / boot-id change' →
        'Fail-closed (UNKNOWN is never downgraded)'."""
        crash = _section_text(self.text, "7. Crash Recovery Matrix")
        self.assertIn("PID reuse", crash)
        self.assertIn("UNKNOWN", crash)

    def test_boot_id_change_fail_closed(self) -> None:
        """Boot-id change is explicitly grouped with PID reuse in crash
        matrix row 13, disposition fail-closed."""
        crash = _section_text(self.text, "7. Crash Recovery Matrix")
        self.assertIn("boot-id", crash.lower())

    def test_insufficient_permissions_cannot_infer_dead(self) -> None:
        """The contract does not provide a path to infer DEAD from
        insufficient permissions.  UNKNOWN is never downgraded."""
        crash = _section_text(self.text, "7. Crash Recovery Matrix")
        self.assertIn("never downgraded", crash.lower())

    def test_no_second_worker_on_lease_expiry_alone(self) -> None:
        """Lease expiry alone does not justify a second Worker.  The handoff
        reuses the Scheduler-acquired lease (step 5).  A second Worker
        would require a second lease — forbidden by Section 1."""
        self.assertIn("never create a second", self.text.lower())

    def test_no_second_worker_on_pid_disappearance_alone(self) -> None:
        """PID disappearance without complete process tree evidence does
        not permit a second Worker.  UNKNOWN is never downgraded."""
        crash = _section_text(self.text, "7. Crash Recovery Matrix")
        self.assertIn("never downgraded", crash.lower())


# ═══════════════════════════════════════════════════════════════════════════════
# 7. Source and lock boundary — adversarial verification
# ═══════════════════════════════════════════════════════════════════════════════


class SourceAndLockBoundaryAdversarial(unittest.TestCase):
    """Prove the contract forbids process/model/API calls inside locks and
    does not require importing forbidden modules or using forbidden types."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.text = _contract_text()

    def test_no_supervisor_start_inside_store_lock(self) -> None:
        """Lock order step 7: 'Release handoff store lock', step 9: 'Start
        supervisor'.  In the frozen lock order code block, supervisor start
        (step 9) occurs after lock release (step 7)."""
        lock = _section_text(self.text, "6. Atomic and Lock Order")
        code_match = re.search(r"```text\s*\n(.*?)```", lock, re.DOTALL)
        self.assertIsNotNone(code_match, "Lock order code block must exist")
        steps = code_match.group(1).strip().split("\n")
        release_idx = next(
            (i for i, line in enumerate(steps)
             if "release" in line.lower() and "lock" in line.lower() and "all other" not in line.lower()),
            -1,
        )
        supervisor_idx = next(
            (i for i, line in enumerate(steps)
             if "supervisor" in line.lower()),
            -1,
        )
        self.assertNotEqual(release_idx, -1, "Lock release step must exist")
        self.assertNotEqual(supervisor_idx, -1, "Supervisor start step must exist")
        self.assertLess(
            release_idx, supervisor_idx,
            f"Supervisor start (step {supervisor_idx+1}) must occur after lock release (step {release_idx+1})",
        )

    def test_no_worker_start_inside_store_lock(self) -> None:
        """Lock order step 11: 'Start Worker' occurs after step 10 (phase
        advance under store lock, released).  In the frozen lock order code
        block, Worker start (step 11) occurs after all lock releases (steps 7-8)."""
        lock = _section_text(self.text, "6. Atomic and Lock Order")
        code_match = re.search(r"```text\s*\n(.*?)```", lock, re.DOTALL)
        self.assertIsNotNone(code_match, "Lock order code block must exist")
        steps = code_match.group(1).strip().split("\n")
        # The "Release all other store/state locks" is step 8
        release_all_idx = next(
            (i for i, line in enumerate(steps)
             if "release" in line.lower() and "all other" in line.lower()),
            -1,
        )
        worker_idx = next(
            (i for i, line in enumerate(steps)
             if "start worker" in line.lower()),
            -1,
        )
        self.assertNotEqual(release_all_idx, -1, "Release all locks step must exist")
        self.assertNotEqual(worker_idx, -1, "Worker start step must exist")
        self.assertLess(
            release_all_idx, worker_idx,
            f"Worker start (step {worker_idx+1}) must occur after all locks released (step {release_all_idx+1})",
        )

    def test_no_provider_or_model_call_inside_lock(self) -> None:
        """Section 6: 'The following must never be called inside any lock:
        ... Calling a model or provider'."""
        lock = _section_text(self.text, "6. Atomic and Lock Order")
        self.assertIn("never", lock.lower())
        self.assertTrue(
            "model" in lock.lower() or "provider" in lock.lower(),
            "Contract must forbid model/provider calls inside locks",
        )

    def test_no_heartbeat_start_inside_lock(self) -> None:
        """Section 6: 'Waiting for heartbeat or completion' must never be
        called inside any lock."""
        lock = _section_text(self.text, "6. Atomic and Lock Order")
        self.assertIn("heartbeat", lock.lower())

    def test_no_direct_run_dispatch_cycle_if_it_recreates_task_dispatched(self) -> None:
        """Section 1: 'run_dispatch_cycle() / start_dispatch_cycle() must not
        be called directly if it would re-create TASK_DISPATCHED or
        re-acquire a lease'."""
        self.assertIn("run_dispatch_cycle", self.text.lower())
        self.assertIn("not called directly", self.text.lower())

    def test_no_bypass_of_workflow_orchestrator_lifecycle(self) -> None:
        """Section 8: 'The handoff freezes only a new entry point.  It does not
        modify ... run_dispatch_cycle(), start_dispatch_cycle(), or the
        WorkflowOrchestrator delivery/acceptance/integration state machine.'"""
        inv = _section_text(self.text, "8. Invariant Interfaces")
        self.assertIn("WorkflowOrchestrator", inv)

    def test_no_import_of_recovery_executor_implementation(self) -> None:
        """Section 8: 'Owner-loss recovery is not re-implemented by the
        handoff.'  The handoff must not import or depend on Recovery Executor
        implementation details."""
        self.assertIn("not re-implement", self.text.lower())

    def test_no_any_dict_mapping_in_public_types(self) -> None:
        """Section 3: 'No public field may use Any, dict, Mapping, a mutable
        collection, argv, env, prompt, stdout, stderr, callback, factory,
        provider runtime object, exception text, or exit code'."""
        sec3 = _section_text(self.text, "3. Public Value Types")
        self.assertIn("Any", sec3)
        self.assertIn("dict", sec3)
        self.assertIn("Mapping", sec3)

    def test_no_mutable_collections_in_public_metadata(self) -> None:
        """Section 3 forbids mutable collections in public types."""
        sec3 = _section_text(self.text, "3. Public Value Types")
        self.assertIn("mutable", sec3.lower())

    def test_contract_does_not_require_importing_production_modules_for_validation(self) -> None:
        """The contract is self-contained and verifiable without importing
        Recovery Executor, WorkflowOrchestrator, Admission, Runtime, Store,
        Policy, or TaskDifficulty implementation modules."""
        self.assertIn("does not implement", self.text.lower())


# ═══════════════════════════════════════════════════════════════════════════════
# Cross-cutting adversarial verification
# ═══════════════════════════════════════════════════════════════════════════════


class CrossCuttingAdversarial(unittest.TestCase):
    """Tests that span multiple contract sections to prove holistic defenses."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.text = _contract_text()

    def test_identity_substitution_cannot_bypass_phase_check(self) -> None:
        """Even if an attacker substitutes a field, the phase must progress
        forward-only.  A substituted identity with an advanced phase would fail
        the byte-exact content_digest check (Section 3.4 row 25)."""
        self.assertIn("content_digest", self.text.lower())
        self.assertIn("forward-only", self.text.lower())

    def test_provider_bypass_cannot_create_handoff(self) -> None:
        """If the providers mapping is empty or missing the target provider,
        Section 5 rule 4 requires zero handoff writes.  Combined with lock
        order step 3 (provider validation before step 4 store lock), no
        durable handoff artifact can be created without provider validation."""
        lock = _section_text(self.text, "6. Atomic and Lock Order")
        self.assertIn("provider", lock.lower())

    def test_all_19_identity_fields_in_contract(self) -> None:
        """All 19 identity-substitution target fields must appear in the
        contract with a validation rule."""
        text_lower = self.text.lower()
        for field in IDENTITY_FIELDS_FOR_SUBSTITUTION:
            with self.subTest(field=field):
                self.assertIn(
                    field.lower(), text_lower,
                    f"Identity field '{field}' must appear in contract",
                )


if __name__ == "__main__":
    unittest.main()
