from __future__ import annotations

import dataclasses
import inspect
import sys
import unittest

from tests import test_portfolio_scheduler_recovery_adversarial as adversarial
from tests import test_portfolio_scheduler_runtime as runtime_tests


class PortfolioSchedulerRuntimeRecoveryIntegrationTests(unittest.TestCase):
    """限定的 PortfolioScheduler runtime/recovery 交叉边界验证。"""

    def test_all_scheduler_modules_import_in_one_interpreter(self) -> None:
        import control_plane_transition
        import portfolio_scheduler_admission
        import portfolio_scheduler_policy
        import portfolio_scheduler_recovery
        import portfolio_scheduler_runtime
        import portfolio_scheduler_store
        import worker_slot_lease

        modules = (
            portfolio_scheduler_store,
            portfolio_scheduler_policy,
            portfolio_scheduler_admission,
            portfolio_scheduler_runtime,
            portfolio_scheduler_recovery,
            control_plane_transition,
            worker_slot_lease,
        )
        self.assertEqual(len({module.__name__ for module in modules}), 7)
        self.assertTrue(callable(portfolio_scheduler_runtime.PortfolioSchedulerRuntime))
        self.assertTrue(callable(portfolio_scheduler_recovery.decide_reconciliation))

    def test_durable_plan_tick_emits_one_task_dispatched(self) -> None:
        root, _ = runtime_tests._setup_project()
        try:
            store, entry = runtime_tests._setup_queued(root)
            request = runtime_tests._make_tick_request(root, entry)
            result = runtime_tests.pr.PortfolioSchedulerRuntime(root).tick(request)
            self.assertIs(
                result.outcome.kind,
                runtime_tests.pr.PortfolioSchedulerTickOutcomeKind.ADMITTED,
            )
            self.assertEqual(runtime_tests._event_ids(root), ["EVT-ADMIT-001"])
            self.assertEqual(runtime_tests._lease_count(root), 1)
            self.assertEqual(runtime_tests._outbox_ids(root), ["MSG-ADMIT-001"])
            self.assertEqual(store.read_queue_entry(entry.queue_id).state.value, "dispatched")
        finally:
            runtime_tests._remove_tree(root)

    def test_dispatch_id_substitution_rejected_before_lease(self) -> None:
        self._assert_plan_substitution_rejected(dispatch_id="DSP-CHANGED")

    def test_event_id_substitution_rejected_before_lease(self) -> None:
        self._assert_plan_substitution_rejected(event_id="EVT-CHANGED")

    def _assert_plan_substitution_rejected(self, **override: object) -> None:
        root, _ = runtime_tests._setup_project()
        try:
            store, entry, receipt = runtime_tests._setup_selected(root)
            runtime = runtime_tests.pr.PortfolioSchedulerRuntime(root)
            plan = runtime_tests._make_plan(root, entry, receipt)
            store.reserve_admission_plan(runtime._durable_plan(plan))
            divergent = dataclasses.replace(plan, **override)
            request = runtime_tests.pr.PortfolioSchedulerTickRequest(
                project_root=root,
                plan=divergent,
                admission_context=runtime_tests._admission_context(),
            )
            with self.assertRaises(runtime_tests.pr.PortfolioSchedulerRuntimeConflictError):
                runtime.tick(request)
            self.assertEqual(runtime_tests._lease_count(root), 0)
            self.assertEqual(runtime_tests._event_ids(root), [])
            self.assertEqual(runtime_tests._outbox_ids(root), [])
        finally:
            runtime_tests._remove_tree(root)

    def test_selected_without_plan_returns_recovery_required(self) -> None:
        root, _ = runtime_tests._setup_project()
        try:
            store, entry = runtime_tests._setup_queued(root)
            selected = store.advance_queue_phase(
                entry.queue_id,
                entry.selection_generation,
                runtime_tests.ps.QueuePhase.SELECTED,
            )
            result = runtime_tests.pr.PortfolioSchedulerRuntime(root).tick(
                runtime_tests._make_tick_request(root, selected)
            )
            self.assertIs(
                result.outcome.kind,
                runtime_tests.pr.PortfolioSchedulerTickOutcomeKind.RECOVERY_REQUIRED,
            )
            self.assertEqual(runtime_tests._lease_count(root), 0)
            self.assertEqual(runtime_tests._event_ids(root), [])
            self.assertEqual(runtime_tests._outbox_ids(root), [])
        finally:
            runtime_tests._remove_tree(root)

    def test_recovery_required_carries_updated_typed_decision(self) -> None:
        root, _ = runtime_tests._setup_project()
        try:
            store, entry, receipt = runtime_tests._setup_selected(root)
            result = runtime_tests.pr.PortfolioSchedulerRuntime(root).tick(
                runtime_tests._make_tick_request(root, entry, receipt)
            )
            decision = result.outcome.recovery_decision
            self.assertIsNotNone(decision)
            self.assertIs(
                result.outcome.kind,
                runtime_tests.pr.PortfolioSchedulerTickOutcomeKind.RECOVERY_REQUIRED,
            )
            self.assertEqual(decision.queue_id, entry.queue_id)
            self.assertEqual(decision.expected_generation, entry.selection_generation)
            self.assertEqual(runtime_tests._lease_count(root), 0)
        finally:
            runtime_tests._remove_tree(root)

    def test_canonical_event_decision_uses_exact_event_id(self) -> None:
        entry = adversarial._entry(state=adversarial.ps.QueuePhase.QUEUED)
        event = adversarial._event(
            event_id="EVT-CANONICAL-EXACT",
            task_id=entry.task_id,
            revision=entry.revision,
            dispatch_id="DSP-CANONICAL",
        )
        task = adversarial._task(
            task_id=entry.task_id,
            revision=entry.revision,
            state="dispatched",
            attempt=1,
            dispatch_id="DSP-CANONICAL",
        )
        decision = adversarial.rec.decide_reconciliation(
            adversarial._req(entry, task, events=(event,))
        )
        self.assertEqual(decision.canonical_dispatch_event_id, event.event_id)
        self.assertNotEqual(decision.canonical_dispatch_event_id, event.dispatch_id)

    def test_task_dispatch_without_event_waits_for_execution_owner(self) -> None:
        entry = adversarial._entry(state=adversarial.ps.QueuePhase.QUEUED)
        task = adversarial._task(
            task_id=entry.task_id,
            revision=entry.revision,
            state="dispatched",
            attempt=1,
            dispatch_id="DSP-NO-EVENT",
        )
        decision = adversarial.rec.decide_reconciliation(
            adversarial._req(entry, task)
        )
        self.assertIs(
            decision.recovery_action,
            adversarial.rec.RecoveryAction.WAIT_FOR_EXECUTION_OWNER,
        )
        self.assertIsNone(decision.canonical_dispatch_event_id)

    def test_lease_dispatch_identity_divergence_fails_closed(self) -> None:
        entry = adversarial._entry(state=adversarial.ps.QueuePhase.QUEUED)
        event = adversarial._event(
            event_id="EVT-LEASE-BOUNDARY",
            task_id=entry.task_id,
            revision=entry.revision,
            dispatch_id="DSP-CANONICAL",
        )
        task = adversarial._task(
            task_id=entry.task_id,
            revision=entry.revision,
            state="dispatched",
            attempt=1,
            dispatch_id="DSP-CANONICAL",
        )
        lease = adversarial.rec.LeaseEvidence(
            lease_id="LEASE-001",
            slot_id="standard_agent-1",
            holder_dispatch_id="DSP-DIVERGENT",
            holder_instance_id="inst-001",
        )
        decision = adversarial.rec.decide_reconciliation(
            adversarial._req(entry, task, events=(event,), lease=lease)
        )
        self.assertIs(
            decision.recovery_action,
            adversarial.rec.RecoveryAction.FAIL_CLOSED,
        )
        self.assertIs(
            decision.reason,
            adversarial.rec.RecoveryReason.EVIDENCE_IDENTITY_DIVERGENCE,
        )

    def test_committed_event_with_lagging_scheduler_evidence_replays_once(self) -> None:
        root, _ = runtime_tests._setup_project()
        try:
            store, entry, receipt = runtime_tests._setup_selected(root)
            runtime = runtime_tests.pr.PortfolioSchedulerRuntime(root)
            plan = runtime_tests._make_plan(root, entry, receipt)
            store.reserve_admission_plan(runtime._durable_plan(plan))
            runtime_tests._apply_dispatch_direct(
                root,
                runtime_tests._admission_request(plan, entry, receipt),
            )
            result = runtime.tick(
                runtime_tests._make_tick_request(root, entry, receipt)
            )
            self.assertIs(
                result.outcome.kind,
                runtime_tests.pr.PortfolioSchedulerTickOutcomeKind.ADMITTED,
            )
            self.assertEqual(runtime_tests._event_ids(root), ["EVT-ADMIT-001"])
            self.assertEqual(runtime_tests._lease_count(root), 1)
            self.assertEqual(runtime_tests._outbox_ids(root), ["MSG-ADMIT-001"])
        finally:
            runtime_tests._remove_tree(root)

    def test_attempt_four_has_zero_plan_lease_and_event(self) -> None:
        root, _ = runtime_tests._setup_project()
        try:
            store, entry = runtime_tests._setup_queued(root)
            before_snapshot = store.enumerate_queue_snapshot()
            before_receipts = runtime_tests._receipt_ids(root)
            with self.assertRaises(runtime_tests.pr.PortfolioSchedulerRuntimeInputError):
                runtime_tests._make_tick_request(
                    root,
                    entry,
                    expected_task_attempt=3,
                    new_attempt=4,
                )
            plan_root = root / "docs" / "pm" / "portfolio-scheduler" / "admission-plans"
            plan_file = plan_root / entry.queue_id / f"g{entry.selection_generation}.yaml"
            self.assertFalse(plan_file.exists())
            self.assertEqual(store.enumerate_queue_snapshot(), before_snapshot)
            self.assertEqual(runtime_tests._receipt_ids(root), before_receipts)
            self.assertEqual(runtime_tests._lease_count(root), 0)
            self.assertEqual(runtime_tests._event_ids(root), [])
        finally:
            runtime_tests._remove_tree(root)

    def test_runtime_and_recovery_source_boundaries_are_explicit(self) -> None:
        runtime_source = inspect.getsource(runtime_tests.pr)
        recovery_source = inspect.getsource(adversarial.rec)
        self.assertIn("from portfolio_scheduler_recovery import", runtime_source)
        self.assertIn("recovery_decision=decision", runtime_source)
        self.assertNotIn("execute_recovery", runtime_source)
        self.assertNotIn("run_worker", runtime_source)
        self.assertNotIn("subprocess", runtime_source)
        self.assertNotIn("import requests", runtime_source)
        self.assertIn("evt.event_id.encode", recovery_source)
        self.assertIn("dispatched_evt.event_id", recovery_source)
        normalized_recovery = " ".join(recovery_source.split())
        self.assertIn("Never writes to Store", normalized_recovery)
        self.assertNotIn("portfolio_scheduler_runtime", recovery_source)
        self.assertNotIn("subprocess", recovery_source)
        self.assertNotIn("import requests", recovery_source)
        self.assertNotIn("from openai", recovery_source)
        self.assertIn(
            "PortfolioSchedulerRuntime",
            sys.modules["portfolio_scheduler_runtime"].__all__,
        )


if __name__ == "__main__":
    unittest.main()
