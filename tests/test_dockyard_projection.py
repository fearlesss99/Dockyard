from __future__ import annotations

import dataclasses
import inspect
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "agentdesk" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from dockyard_projection import (  # noqa: E402
    DockyardApprovalSummary,
    DockyardOverview,
    DockyardProjectHealth,
    DockyardProjectionBundle,
    DockyardProjectionConflictError,
    DockyardProjectionInputError,
    DockyardProjectionRequest,
    DockyardProviderEvidence,
    DockyardProviderHealth,
    DockyardProviderSummary,
    DockyardRunEvidence,
    DockyardRunSummary,
    DockyardStateCount,
    DockyardTaskDetail,
    DockyardTaskSummary,
    DockyardWorktreeEvidence,
    DockyardWorktreeSummary,
    build_dockyard_projection,
)
from state_provider import (  # noqa: E402
    AcceptanceEntry,
    DispatchInfo,
    MadRefEntry,
    ModelSelectionSnapshot,
    StateSnapshot,
    TaskEntry,
    TaskTimestamps,
)


SHA_A = "a" * 40
SHA_B = "b" * 40
SHA_C = "c" * 40
NOW = "2026-08-02T12:00:00Z"


def _model(provider: str = "claude", model: str = "claude-model") -> ModelSelectionSnapshot:
    return ModelSelectionSnapshot(
        required_model_tier="standard",
        required_model_capabilities=("coding", "testing"),
        model_binding_id=f"binding-{provider}",
        selected_model_provider=provider,
        selected_model_id=model,
        selected_model_tier="standard",
        selected_deliberation_tier="balanced",
        selected_context_window_tokens=128000,
        selected_model_capabilities=("coding", "testing"),
        model_degradation_approval_id=None,
    )


def _timestamps(updated: str = NOW) -> TaskTimestamps:
    return TaskTimestamps(
        created_at="2026-08-02T10:00:00Z",
        ready_at="2026-08-02T10:05:00Z",
        dispatched_at="2026-08-02T10:10:00Z",
        started_at="2026-08-02T10:11:00Z",
        delivered_at=None,
        blocked_at=None,
        accepted_at=None,
        integrated_at=None,
        updated_at=updated,
    )


def _active_task() -> TaskEntry:
    return TaskEntry(
        task_id="TC-ACTIVE",
        revision=1,
        task_card_path="docs/pm/tasks/TC-ACTIVE-r1.md",
        task_card_commit=SHA_A,
        state="in_progress",
        attempt=1,
        current_dispatch=DispatchInfo(
            dispatch_id="DSP-ACTIVE",
            attempt_id="ATT-ACTIVE-1",
            role_id="DEV",
            base_commit=SHA_B,
            branch="feat/active",
            dispatched_at="2026-08-02T10:10:00Z",
            model_selection=_model(),
        ),
        report_path="docs/pm/reports/TC-ACTIVE-r1-a1.md",
        granted_approval_ids=("APR-1",),
        delivery_state="none",
        integration_state="not_integrated",
        implementation_commit=None,
        report_commit=None,
        accepted_commit=None,
        acceptance_path=None,
        integrated_commit=None,
        blocked_reason="SECRET BLOCKED REASON",
        blocked_kind=None,
        blocked_owner=None,
        unblock_condition=None,
        review_after=None,
        blocked_attempt_valid=None,
        resume_state=None,
        timestamps=_timestamps(),
        superseded_by=None,
    )


def _review_task() -> TaskEntry:
    return TaskEntry(
        task_id="TC-REVIEW",
        revision=2,
        task_card_path="docs/pm/tasks/TC-REVIEW-r2.md",
        task_card_commit=SHA_B,
        state="review_ready",
        attempt=1,
        current_dispatch=None,
        report_path="docs/pm/reports/TC-REVIEW-r2-a1.md",
        granted_approval_ids=(),
        delivery_state="delivered",
        integration_state="not_integrated",
        implementation_commit=SHA_A,
        report_commit=SHA_B,
        accepted_commit=None,
        acceptance_path=None,
        integrated_commit=None,
        blocked_reason=None,
        blocked_kind=None,
        blocked_owner=None,
        unblock_condition=None,
        review_after=None,
        blocked_attempt_valid=None,
        resume_state=None,
        timestamps=_timestamps("2026-08-02T12:05:00Z"),
        superseded_by=None,
    )


def _snapshot(reverse: bool = False) -> StateSnapshot:
    tasks = (_active_task(), _review_task())
    if reverse:
        tasks = tuple(reversed(tasks))
    return StateSnapshot(
        project_root=Path("C:/SECRET/ABSOLUTE/PROJECT"),
        schema_version="agentdesk.tasks/v2",
        project_id="dockyard-test",
        adoption_level="standard",
        updated_at=NOW,
        pm_holder_id="SECRET-PM-ROUTE",
        pm_lease_epoch=1,
        pm_mode="timed",
        tasks=tasks,
        events=(),
        outbox=(),
        acceptances=(
            AcceptanceEntry(
                schema_version="agentdesk.acceptance/v1",
                task_id="TC-REVIEW",
                revision=2,
                attempt=1,
                review_n=1,
                decision="accepted",
                implementation_commit=SHA_A,
                report_commit=SHA_B,
                base_commit=SHA_C,
                accepted_commit=SHA_B,
                reviewed_dispatch_id="DSP-HISTORICAL",
                type="acceptance",
                owner_approval_gate="none",
                owner_approval_ids=(),
                raw_body="SECRET RAW ACCEPTANCE BODY",
                filename="TC-REVIEW-r2-a1-review1.md",
            ),
        ),
        mad_refs=(
            MadRefEntry(
                task_id="TC-REVIEW",
                dispatch_id="DSP-HISTORICAL",
                purpose="audit",
                deliberation_id="MAD-1",
                depth="balanced",
                stdout_sha256=SHA_A + "a" * 24,
                report_sha256=SHA_B + "b" * 24,
                status="complete",
                archive_path="C:/SECRET/MAD/ARCHIVE",
                created_at=NOW,
            ),
        ),
        read_hexsha=SHA_C,
    )


def _run() -> DockyardRunEvidence:
    return DockyardRunEvidence(
        task_id="TC-ACTIVE",
        revision=1,
        attempt=1,
        dispatch_id="DSP-ACTIVE",
        phase="ACKNOWLEDGED",
        supervisor_state="ALIVE",
        worker_state="ALIVE",
        heartbeat_state="ACTIVE",
        lease_state="HELD",
        started_at="2026-08-02T10:11:00Z",
        updated_at=NOW,
        retry_count=0,
    )


def _provider(
    provider_id: str = "claude",
    health: DockyardProviderHealth = DockyardProviderHealth.READY,
) -> DockyardProviderEvidence:
    return DockyardProviderEvidence(
        provider_id=provider_id,
        model_id=f"{provider_id}-model",
        health=health,
        reason_code=None if health is DockyardProviderHealth.READY else "NOT_READY_EVIDENCE",
        executable_version="1.0.0",
        decoder_version="v1",
        binding_id=f"binding-{provider_id}",
        required_capabilities=("coding", "testing"),
        checked_at=NOW,
    )


def _worktree() -> DockyardWorktreeEvidence:
    return DockyardWorktreeEvidence(
        task_id="TC-ACTIVE",
        revision=1,
        attempt=1,
        worktree_id="WT-ACTIVE",
        lifecycle_state="READY",
        branch="feat/active",
        head_commit=SHA_B,
        dirty=False,
        checked_at=NOW,
    )


def _request(reverse: bool = False) -> DockyardProjectionRequest:
    providers = (_provider("reasonix"), _provider("claude"))
    if reverse:
        providers = tuple(reversed(providers))
    return DockyardProjectionRequest(
        snapshot=_snapshot(reverse),
        run_evidence=(_run(),),
        provider_evidence=providers,
        worktree_evidence=(_worktree(),),
        generated_at=NOW,
    )


class DockyardProjectionTypeTests(unittest.TestCase):
    def test_required_output_fields_are_exact(self) -> None:
        expected = {
            DockyardTaskSummary: (
                "task_id", "revision", "state", "attempt", "role_id", "provider_id",
                "model_id", "deliberation_tier", "delivery_state", "integration_state",
                "updated_at", "blocked_kind", "has_report", "content_digest",
            ),
            DockyardTaskDetail: (
                "summary", "task_card_commit", "current_dispatch_id", "branch",
                "implementation_commit", "report_commit", "accepted_commit",
                "integrated_commit", "approval_count", "content_digest",
            ),
            DockyardRunSummary: (
                "task_id", "revision", "attempt", "dispatch_id", "phase",
                "supervisor_state", "worker_state", "heartbeat_state", "lease_state",
                "started_at", "updated_at", "retry_count", "content_digest",
                "generation_id", "termination_available", "termination_event_id",
                "retry_available", "retry_event_id", "retry_failure_kind",
                "retry_provider_id", "retry_model_id",
            ),
            DockyardApprovalSummary: (
                "task_id", "revision", "granted_approval_count",
                "latest_acceptance_decision", "owner_approval_gate",
                "pending_user_decision", "updated_at", "content_digest",
            ),
            DockyardProviderSummary: (
                "provider_id", "model_id", "health", "reason_code",
                "executable_version", "decoder_version", "binding_id",
                "required_capabilities", "checked_at", "content_digest",
            ),
            DockyardProjectHealth: (
                "snapshot_state", "pm_state", "scheduler_state", "runner_state",
                "provider_state", "issue_codes", "checked_at", "content_digest",
            ),
        }
        for value_type, field_names in expected.items():
            self.assertEqual(tuple(field.name for field in dataclasses.fields(value_type)), field_names)

    def test_public_dataclasses_are_frozen_and_slotted(self) -> None:
        for value_type in (
            DockyardProjectionRequest, DockyardRunEvidence, DockyardProviderEvidence,
            DockyardWorktreeEvidence, DockyardStateCount, DockyardTaskSummary,
            DockyardTaskDetail, DockyardRunSummary, DockyardApprovalSummary,
            DockyardProviderSummary, DockyardWorktreeSummary, DockyardProjectHealth,
            DockyardOverview, DockyardProjectionBundle,
        ):
            self.assertTrue(value_type.__dataclass_params__.frozen)
            self.assertTrue(hasattr(value_type, "__slots__"))

    def test_public_annotations_have_no_dynamic_or_mutable_collections(self) -> None:
        forbidden = ("Any", "object", "dict", "Mapping", "list", "set")
        for value_type in (
            DockyardProjectionRequest, DockyardRunEvidence, DockyardProviderEvidence,
            DockyardWorktreeEvidence, DockyardStateCount, DockyardTaskSummary,
            DockyardTaskDetail, DockyardRunSummary, DockyardApprovalSummary,
            DockyardProviderSummary, DockyardWorktreeSummary, DockyardProjectHealth,
            DockyardOverview, DockyardProjectionBundle,
        ):
            rendered = " ".join(str(value) for value in value_type.__annotations__.values())
            for name in forbidden:
                self.assertNotIn(name, rendered)

    def test_bool_is_not_accepted_as_integer(self) -> None:
        with self.assertRaisesRegex(DockyardProjectionInputError, "run_revision"):
            DockyardRunEvidence(
                "TC", True, 1, "DSP", "RESERVED", "ALIVE", "ALIVE", "ACTIVE",
                "HELD", None, NOW, 0,
            )


class DockyardProjectionDeterminismTests(unittest.TestCase):
    def test_build_returns_frozen_bundle(self) -> None:
        result = build_dockyard_projection(_request())
        self.assertIs(type(result), DockyardProjectionBundle)
        self.assertEqual(result.overview.snapshot_commit, SHA_C)

    def test_termination_evidence_is_typed_and_digest_bound(self) -> None:
        evidence = dataclasses.replace(
            _run(),
            generation_id="GEN-ACTIVE",
            termination_available=True,
            termination_event_id="EVT-CANCEL-123456789012345678901234",
        )
        request = dataclasses.replace(_request(), run_evidence=(evidence,))
        result = build_dockyard_projection(request)
        run = result.runs[0]
        self.assertTrue(run.termination_available)
        self.assertEqual(run.generation_id, "GEN-ACTIVE")
        self.assertEqual(run.termination_event_id, "EVT-CANCEL-123456789012345678901234")
        with self.assertRaisesRegex(DockyardProjectionInputError, "run_termination_identity"):
            dataclasses.replace(evidence, generation_id=None)

    def test_input_order_does_not_change_output(self) -> None:
        first = build_dockyard_projection(_request(False))
        second = build_dockyard_projection(_request(True))
        self.assertEqual(first, second)
        self.assertEqual(first.content_digest, second.content_digest)

    def test_task_and_provider_order_is_stable(self) -> None:
        result = build_dockyard_projection(_request(True))
        self.assertEqual(tuple(item.task_id for item in result.tasks), ("TC-ACTIVE", "TC-REVIEW"))
        self.assertEqual(tuple(item.provider_id for item in result.providers), ("claude", "reasonix"))

    def test_provider_health_change_changes_digest(self) -> None:
        first = build_dockyard_projection(_request())
        request = dataclasses.replace(
            _request(),
            provider_evidence=(
                _provider("claude"),
                _provider("reasonix", DockyardProviderHealth.NOT_READY),
            ),
        )
        second = build_dockyard_projection(request)
        self.assertNotEqual(first.content_digest, second.content_digest)
        self.assertIs(second.overview.health.provider_state, DockyardProviderHealth.NOT_READY)

    def test_retry_evidence_requires_finalizer_identity(self) -> None:
        base = DockyardRunEvidence(
            "TC-RETRY", 1, 1, "DSP-RETRY", "FINALIZED", "DEAD", "DEAD",
            "DONE", "RELEASED", None, NOW, 0, "GEN-RETRY", False, None,
        )
        with self.assertRaises(DockyardProjectionInputError):
            dataclasses.replace(base, retry_available=True)
        ready = dataclasses.replace(
            base,
            retry_available=True,
            retry_event_id="EVT-FAILED-RETRY",
            retry_failure_kind="worker_failed",
            retry_provider_id="claude",
            retry_model_id="claude-sonnet",
        )
        self.assertTrue(ready.retry_available)
        self.assertEqual(ready.retry_failure_kind, "worker_failed")

    def test_returned_run_retains_prior_dispatch_identity_without_current_dispatch(self) -> None:
        returned = dataclasses.replace(
            _run(),
            task_id="TC-REVIEW",
            revision=2,
            attempt=1,
            dispatch_id="DSP-RETURNED",
            phase="RETURNED",
            generation_id="GEN-RETURNED",
            termination_available=False,
            termination_event_id=None,
            retry_available=True,
            retry_event_id="EVT-REQUEUED-RETURNED",
            retry_failure_kind="delivery_returned",
            retry_provider_id="claude",
            retry_model_id="claude-sonnet",
        )
        result = build_dockyard_projection(
            dataclasses.replace(_request(), run_evidence=(returned,))
        )
        self.assertEqual(result.runs[0].phase, "RETURNED")
        self.assertEqual(result.runs[0].dispatch_id, "DSP-RETURNED")
        self.assertEqual(result.runs[0].retry_failure_kind, "delivery_returned")

    def test_snapshot_commit_change_changes_digest(self) -> None:
        first = build_dockyard_projection(_request())
        snapshot = dataclasses.replace(_snapshot(), read_hexsha="d" * 40)
        second = build_dockyard_projection(dataclasses.replace(_request(), snapshot=snapshot))
        self.assertNotEqual(first.content_digest, second.content_digest)


class DockyardProjectionIdentityDefenseTests(unittest.TestCase):
    def test_duplicate_provider_is_conflict(self) -> None:
        request = dataclasses.replace(_request(), provider_evidence=(_provider(), _provider()))
        with self.assertRaisesRegex(DockyardProjectionConflictError, "duplicate_provider"):
            build_dockyard_projection(request)

    def test_duplicate_run_is_conflict(self) -> None:
        request = dataclasses.replace(_request(), run_evidence=(_run(), _run()))
        with self.assertRaisesRegex(DockyardProjectionConflictError, "duplicate_run"):
            build_dockyard_projection(request)

    def test_run_dispatch_divergence_is_conflict(self) -> None:
        request = dataclasses.replace(
            _request(), run_evidence=(dataclasses.replace(_run(), dispatch_id="DSP-EVIL"),)
        )
        with self.assertRaisesRegex(DockyardProjectionConflictError, "run_dispatch_identity"):
            build_dockyard_projection(request)

    def test_worktree_branch_divergence_is_conflict(self) -> None:
        request = dataclasses.replace(
            _request(), worktree_evidence=(dataclasses.replace(_worktree(), branch="evil"),)
        )
        with self.assertRaisesRegex(DockyardProjectionConflictError, "worktree_branch_identity"):
            build_dockyard_projection(request)

    def test_wrong_snapshot_type_is_rejected(self) -> None:
        with self.assertRaisesRegex(DockyardProjectionInputError, "snapshot_type"):
            DockyardProjectionRequest(None, (), (), (), NOW)  # type: ignore[arg-type]

    def test_wrong_request_type_is_rejected(self) -> None:
        with self.assertRaisesRegex(DockyardProjectionInputError, "request_type"):
            build_dockyard_projection(None)  # type: ignore[arg-type]

    def test_malicious_str_and_repr_are_not_called(self) -> None:
        class Evil:
            def __str__(self) -> str:
                raise AssertionError("str called")

            def __repr__(self) -> str:
                raise AssertionError("repr called")

        with self.assertRaisesRegex(DockyardProjectionInputError, "provider_id"):
            DockyardProviderEvidence(
                Evil(), "model", DockyardProviderHealth.READY, None, None, None, None,
                (), NOW,
            )  # type: ignore[arg-type]


class DockyardProjectionPrivacyTests(unittest.TestCase):
    def test_sensitive_snapshot_values_are_not_in_projection(self) -> None:
        rendered = repr(build_dockyard_projection(_request()))
        for secret in (
            "C:/SECRET/ABSOLUTE/PROJECT",
            "SECRET-PM-ROUTE",
            "SECRET RAW ACCEPTANCE BODY",
            "C:/SECRET/MAD/ARCHIVE",
            "SECRET BLOCKED REASON",
        ):
            self.assertNotIn(secret, rendered)

    def test_projection_source_has_no_io_or_process_imports(self) -> None:
        source = inspect.getsource(sys.modules[build_dockyard_projection.__module__])
        for value in (
            "import os", "import subprocess", "import socket", "import pathlib",
            "from pathlib", "open(", "Popen", "requests", "urllib",
        ):
            self.assertNotIn(value, source)

    def test_projection_does_not_reference_sensitive_snapshot_fields(self) -> None:
        source = inspect.getsource(build_dockyard_projection)
        for value in ("project_root", "raw_body", "archive_path", "evidence_refs"):
            self.assertNotIn(value, source)


class DockyardProjectionSemanticsTests(unittest.TestCase):
    def test_active_pending_and_recent_summaries(self) -> None:
        overview = build_dockyard_projection(_request()).overview
        self.assertEqual(overview.active_task_ids, ("TC-ACTIVE",))
        self.assertEqual(overview.pending_approval_task_ids, ("TC-REVIEW",))
        self.assertEqual(overview.recent_delivery_task_ids, ("TC-REVIEW",))

    def test_state_counts_are_exact(self) -> None:
        counts = build_dockyard_projection(_request()).overview.state_counts
        self.assertEqual(counts, (
            DockyardStateCount("in_progress", 1),
            DockyardStateCount("review_ready", 1),
        ))

    def test_acceptance_projection_exposes_only_safe_fields(self) -> None:
        approvals = build_dockyard_projection(_request()).approvals
        review = next(item for item in approvals if item.task_id == "TC-REVIEW")
        self.assertEqual(review.latest_acceptance_decision, "accepted")
        self.assertEqual(review.owner_approval_gate, "none")
        self.assertTrue(review.pending_user_decision)

    def test_no_provider_evidence_is_unknown(self) -> None:
        result = build_dockyard_projection(dataclasses.replace(_request(), provider_evidence=()))
        self.assertIs(result.overview.health.provider_state, DockyardProviderHealth.UNKNOWN)
        self.assertIn("NO_PROVIDER_EVIDENCE", result.overview.health.issue_codes)

    def test_unknown_run_evidence_is_fail_closed_health(self) -> None:
        run = dataclasses.replace(_run(), worker_state="UNKNOWN")
        result = build_dockyard_projection(dataclasses.replace(_request(), run_evidence=(run,)))
        self.assertIn("RUN_EVIDENCE_UNKNOWN", result.overview.health.issue_codes)


if __name__ == "__main__":
    unittest.main()
