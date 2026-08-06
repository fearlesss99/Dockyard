"""TC-13.9b: comprehensive mock-based tests for ``worker_adapter.py``.

stdlib-only unittest with async support; zero real CLI, model, API,
network, subprocess, or file I/O.
"""

from __future__ import annotations

import asyncio
import ast
import dataclasses
import os
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Mapping
from pathlib import Path
from unittest import mock

# ── Load modules under test ──────────────────────────────────────────────
_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCRIPTS = _REPO_ROOT / "skills" / "agentdesk" / "scripts"

sys.path.insert(0, str(_SCRIPTS))

import context_budget as _cb
import core_types as _ct
import dispatcher_gateway as _dg
import worker_adapter as _wa

sys.path.pop(0)

# Convenience re-exports
BudgetResult = _cb.BudgetResult
compute_budget = _cb.compute_budget

TaskDifficulty = _ct.TaskDifficulty
WorkerKind = _ct.WorkerKind

DispatchIdentity = _dg.DispatchIdentity
ModelSelectionSnapshot = _dg.ModelSelectionSnapshot
DispatchRequest = _dg.DispatchRequest
DispatchResult = _dg.DispatchResult
AgentCliProvider = _dg.AgentCliProvider
run_dispatch = _dg.run_dispatch
DispatchGatewayError = _dg.DispatchGatewayError
ProviderNotSupportedError = _dg.ProviderNotSupportedError
DispatchInvocationError = _dg.DispatchInvocationError
DispatchTimeoutError = _dg.DispatchTimeoutError
DispatchCancelledError = _dg.DispatchCancelledError
DispatchNonZeroExitError = _dg.DispatchNonZeroExitError

WorkerResult = _wa.WorkerResult
run_worker = _wa.run_worker
run_worker_observed = _wa.run_worker_observed
wa_module = _wa


# ═══════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════


def _make_snapshot(
    *,
    selected_context_window_tokens: int = 200_000,
    selected_model_provider: str = "fake",
    selected_model_id: str = "fake-model",
    **overrides: object,
) -> ModelSelectionSnapshot:
    kwargs: dict[str, object] = {
        "required_model_tier": "standard",
        "required_model_capabilities": (),
        "model_binding_id": "bind-01",
        "selected_model_provider": selected_model_provider,
        "selected_model_id": selected_model_id,
        "selected_model_tier": "standard",
        "selected_deliberation_tier": "balanced",
        "selected_context_window_tokens": selected_context_window_tokens,
        "selected_model_capabilities": (),
        "model_degradation_approval_id": None,
    }
    kwargs.update(overrides)
    return ModelSelectionSnapshot(**kwargs)  # type: ignore[arg-type]


def _make_request(
    *,
    workspace: Path | None = None,
    prompt: str = "test prompt",
    timeout_seconds: int = 300,
    identity: DispatchIdentity | None = None,
    snapshot: ModelSelectionSnapshot | None = None,
) -> DispatchRequest:
    if workspace is None:
        workspace = Path(tempfile.gettempdir())
    if identity is None:
        identity = DispatchIdentity(
            task_id="TC-TEST",
            revision=1,
            attempt=1,
            dispatch_id="DSP-TEST-0001",
        )
    if snapshot is None:
        snapshot = _make_snapshot()
    return DispatchRequest(
        identity=identity,
        workspace=workspace,
        prompt=prompt,
        model_selection=snapshot,
        timeout_seconds=timeout_seconds,
    )


def _make_dispatch_result(
    *,
    identity: DispatchIdentity | None = None,
) -> DispatchResult:
    if identity is None:
        identity = DispatchIdentity(
            task_id="TC-TEST",
            revision=1,
            attempt=1,
            dispatch_id="DSP-TEST-0001",
        )
    return DispatchResult(
        identity=identity,
        provider="fake",
        model_id="fake-model",
        duration_seconds=1.0,
        stdout=b"fake stdout",
        stderr=b"fake stderr",
        stdout_sha256="a" * 64,
        stderr_sha256="b" * 64,
    )


# ═══════════════════════════════════════════════════════════════════════════
# API & Immutability
# ═══════════════════════════════════════════════════════════════════════════


class WorkerResultAPITests(unittest.TestCase):
    """WorkerResult shape, immutability, and module surface."""

    def test_all_exports_exactly_two_symbols(self) -> None:
        self.assertEqual(
            set(wa_module.__all__),
            {"WorkerResult", "run_worker", "run_worker_observed"},
        )

    def test_worker_result_exact_four_fields(self) -> None:
        field_names = {f.name for f in dataclasses.fields(WorkerResult)}
        self.assertEqual(
            field_names,
            {"worker_kind", "task_difficulty", "budget", "dispatch_result"},
        )

    def test_worker_result_is_frozen(self) -> None:
        wr = WorkerResult(
            worker_kind=WorkerKind.BASIC_AGENT,
            task_difficulty=TaskDifficulty.BASIC,
            budget=compute_budget(200_000, TaskDifficulty.BASIC),
            dispatch_result=_make_dispatch_result(),
        )
        for f in dataclasses.fields(WorkerResult):
            with self.assertRaises(dataclasses.FrozenInstanceError):
                setattr(wr, f.name, getattr(wr, f.name))

    def test_worker_result_has_slots_no_dict(self) -> None:
        wr = WorkerResult(
            worker_kind=WorkerKind.BASIC_AGENT,
            task_difficulty=TaskDifficulty.BASIC,
            budget=compute_budget(200_000, TaskDifficulty.BASIC),
            dispatch_result=_make_dispatch_result(),
        )
        with self.assertRaises(AttributeError):
            wr.__dict__  # type: ignore[attr-defined]

    def test_worker_result_field_types(self) -> None:
        budget = compute_budget(200_000, TaskDifficulty.BASIC)
        dr = _make_dispatch_result()
        wr = WorkerResult(
            worker_kind=WorkerKind.EXPERT_AGENT,
            task_difficulty=TaskDifficulty.BASIC,
            budget=budget,
            dispatch_result=dr,
        )
        self.assertIsInstance(wr.worker_kind, WorkerKind)
        self.assertIsInstance(wr.task_difficulty, TaskDifficulty)
        self.assertIsInstance(wr.budget, BudgetResult)
        self.assertIsInstance(wr.dispatch_result, DispatchResult)

    def test_worker_result_field_identity(self) -> None:
        budget = compute_budget(200_000, TaskDifficulty.BASIC)
        dr = _make_dispatch_result()
        wr = WorkerResult(
            worker_kind=WorkerKind.EXPERT_AGENT,
            task_difficulty=TaskDifficulty.BASIC,
            budget=budget,
            dispatch_result=dr,
        )
        self.assertIs(wr.budget, budget)
        self.assertIs(wr.dispatch_result, dr)

    def test_module_import_no_side_effects(self) -> None:
        """Importing worker_adapter must not produce stdout/stderr or I/O."""
        result = subprocess.run(
            [sys.executable, "-c",
             f"import sys; sys.path.insert(0, {str(_SCRIPTS)!r}); "
             "import worker_adapter"],
            capture_output=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0,
                         f"stderr: {result.stderr.decode('utf-8', errors='replace')}")
        self.assertEqual(result.stdout, b"")
        self.assertEqual(result.stderr, b"")


# ═══════════════════════════════════════════════════════════════════════════
# Input Validation — fail-closed rejection tests
# ═══════════════════════════════════════════════════════════════════════════


class InputRejectionTests(unittest.IsolatedAsyncioTestCase):
    """Each test verifies that a single invalid input is rejected and that
    neither compute_budget nor run_dispatch is called."""

    async def _assert_rejects(
        self,
        kwargs: dict[str, object],
        exc_type: type[BaseException],
    ) -> None:
        with mock.patch.object(wa_module, "compute_budget") as mock_budget, \
             mock.patch.object(wa_module, "run_dispatch") as mock_dispatch:
            with self.assertRaises(exc_type):
                await run_worker(**kwargs)  # type: ignore[arg-type]
            mock_budget.assert_not_called()
            mock_dispatch.assert_not_called()

    # ── request type ────────────────────────────────────────────────────

    async def test_rejects_request_none(self) -> None:
        await self._assert_rejects(
            {"request": None, "worker_kind": WorkerKind.BASIC_AGENT,
             "task_difficulty": TaskDifficulty.BASIC, "providers": {"fake": mock.Mock()}},
            TypeError,
        )

    async def test_rejects_request_str(self) -> None:
        await self._assert_rejects(
            {"request": "not-a-request", "worker_kind": WorkerKind.BASIC_AGENT,
             "task_difficulty": TaskDifficulty.BASIC, "providers": {"fake": mock.Mock()}},
            TypeError,
        )

    async def test_rejects_request_bool(self) -> None:
        await self._assert_rejects(
            {"request": True, "worker_kind": WorkerKind.BASIC_AGENT,
             "task_difficulty": TaskDifficulty.BASIC, "providers": {"fake": mock.Mock()}},
            TypeError,
        )

    async def test_rejects_request_int(self) -> None:
        await self._assert_rejects(
            {"request": 42, "worker_kind": WorkerKind.BASIC_AGENT,
             "task_difficulty": TaskDifficulty.BASIC, "providers": {"fake": mock.Mock()}},
            TypeError,
        )

    async def test_rejects_request_list(self) -> None:
        await self._assert_rejects(
            {"request": [], "worker_kind": WorkerKind.BASIC_AGENT,
             "task_difficulty": TaskDifficulty.BASIC, "providers": {"fake": mock.Mock()}},
            TypeError,
        )

    # ── worker_kind type ────────────────────────────────────────────────

    async def test_rejects_worker_kind_bare_str(self) -> None:
        await self._assert_rejects(
            {"request": _make_request(), "worker_kind": "basic_agent",
             "task_difficulty": TaskDifficulty.BASIC, "providers": {"fake": mock.Mock()}},
            TypeError,
        )

    async def test_rejects_worker_kind_task_difficulty_enum(self) -> None:
        await self._assert_rejects(
            {"request": _make_request(), "worker_kind": TaskDifficulty.BASIC,
             "task_difficulty": TaskDifficulty.BASIC, "providers": {"fake": mock.Mock()}},
            TypeError,
        )

    async def test_rejects_worker_kind_none(self) -> None:
        await self._assert_rejects(
            {"request": _make_request(), "worker_kind": None,
             "task_difficulty": TaskDifficulty.BASIC, "providers": {"fake": mock.Mock()}},
            TypeError,
        )

    async def test_rejects_worker_kind_bool(self) -> None:
        await self._assert_rejects(
            {"request": _make_request(), "worker_kind": True,
             "task_difficulty": TaskDifficulty.BASIC, "providers": {"fake": mock.Mock()}},
            TypeError,
        )

    async def test_rejects_worker_kind_int(self) -> None:
        await self._assert_rejects(
            {"request": _make_request(), "worker_kind": 1,
             "task_difficulty": TaskDifficulty.BASIC, "providers": {"fake": mock.Mock()}},
            TypeError,
        )

    async def test_rejects_worker_kind_other_str_enum(self) -> None:
        """WorkerKind rejects a MadDeliberationDepth member."""
        await self._assert_rejects(
            {"request": _make_request(), "worker_kind": _ct.MadDeliberationDepth.DEEP,
             "task_difficulty": TaskDifficulty.BASIC, "providers": {"fake": mock.Mock()}},
            TypeError,
        )

    # ── task_difficulty type ────────────────────────────────────────────

    async def test_rejects_task_difficulty_bare_str(self) -> None:
        await self._assert_rejects(
            {"request": _make_request(), "worker_kind": WorkerKind.BASIC_AGENT,
             "task_difficulty": "basic", "providers": {"fake": mock.Mock()}},
            TypeError,
        )

    async def test_rejects_task_difficulty_worker_kind_enum(self) -> None:
        await self._assert_rejects(
            {"request": _make_request(), "worker_kind": WorkerKind.BASIC_AGENT,
             "task_difficulty": WorkerKind.BASIC_AGENT, "providers": {"fake": mock.Mock()}},
            TypeError,
        )

    async def test_rejects_task_difficulty_none(self) -> None:
        await self._assert_rejects(
            {"request": _make_request(), "worker_kind": WorkerKind.BASIC_AGENT,
             "task_difficulty": None, "providers": {"fake": mock.Mock()}},
            TypeError,
        )

    async def test_rejects_task_difficulty_bool(self) -> None:
        await self._assert_rejects(
            {"request": _make_request(), "worker_kind": WorkerKind.BASIC_AGENT,
             "task_difficulty": False, "providers": {"fake": mock.Mock()}},
            TypeError,
        )

    async def test_rejects_task_difficulty_int(self) -> None:
        await self._assert_rejects(
            {"request": _make_request(), "worker_kind": WorkerKind.BASIC_AGENT,
             "task_difficulty": 0, "providers": {"fake": mock.Mock()}},
            TypeError,
        )

    async def test_rejects_task_difficulty_other_str_enum(self) -> None:
        """TaskDifficulty rejects a MadDeliberationDepth member."""
        await self._assert_rejects(
            {"request": _make_request(), "worker_kind": WorkerKind.BASIC_AGENT,
             "task_difficulty": _ct.MadDeliberationDepth.FAST,
             "providers": {"fake": mock.Mock()}},
            TypeError,
        )

    # ── providers type ──────────────────────────────────────────────────

    async def test_rejects_providers_none(self) -> None:
        await self._assert_rejects(
            {"request": _make_request(), "worker_kind": WorkerKind.BASIC_AGENT,
             "task_difficulty": TaskDifficulty.BASIC, "providers": None},
            TypeError,
        )

    async def test_rejects_providers_str(self) -> None:
        await self._assert_rejects(
            {"request": _make_request(), "worker_kind": WorkerKind.BASIC_AGENT,
             "task_difficulty": TaskDifficulty.BASIC, "providers": "not-a-mapping"},
            TypeError,
        )

    async def test_rejects_providers_list(self) -> None:
        await self._assert_rejects(
            {"request": _make_request(), "worker_kind": WorkerKind.BASIC_AGENT,
             "task_difficulty": TaskDifficulty.BASIC, "providers": []},
            TypeError,
        )

    async def test_rejects_providers_empty_dict(self) -> None:
        """Empty mapping must raise ValueError — budget/dispatch NOT called."""
        with mock.patch.object(wa_module, "compute_budget") as mock_budget, \
             mock.patch.object(wa_module, "run_dispatch") as mock_dispatch:
            with self.assertRaises(ValueError):
                await run_worker(
                    request=_make_request(),
                    worker_kind=WorkerKind.BASIC_AGENT,
                    task_difficulty=TaskDifficulty.BASIC,
                    providers={},
                )
            mock_budget.assert_not_called()
            mock_dispatch.assert_not_called()


# ═══════════════════════════════════════════════════════════════════════════
# Independent Inputs — WorkerKind / TaskDifficulty cross-tests
# ═══════════════════════════════════════════════════════════════════════════


class IndependentInputTests(unittest.IsolatedAsyncioTestCase):
    """Budget is driven by task_difficulty, never by worker_kind."""

    async def _run_and_assert_budget(
        self,
        worker_kind: WorkerKind,
        task_difficulty: TaskDifficulty,
        expected_percent: int,
        expected_cap: int,
    ) -> None:
        window = 200_000
        snapshot = _make_snapshot(selected_context_window_tokens=window)
        req = _make_request(snapshot=snapshot)
        provider = mock.Mock(spec=AgentCliProvider)
        providers: Mapping[str, AgentCliProvider] = {"fake": provider}

        # We let run_dispatch proceed via mock — we only verify budget.
        dr_out = _make_dispatch_result()
        with mock.patch.object(
            wa_module, "run_dispatch", return_value=dr_out
        ) as mock_rd:
            result = await run_worker(
                request=req,
                worker_kind=worker_kind,
                task_difficulty=task_difficulty,
                providers=providers,
            )

        # Budget must come from task_difficulty
        self.assertEqual(result.budget.budget_percent, expected_percent)
        self.assertEqual(result.budget.budget_cap_tokens, expected_cap)
        # worker_kind echoed as-is
        self.assertIs(result.worker_kind, worker_kind)
        # task_difficulty echoed as-is
        self.assertIs(result.task_difficulty, task_difficulty)

    async def test_basic_task_expert_worker(self) -> None:
        await self._run_and_assert_budget(
            worker_kind=WorkerKind.EXPERT_AGENT,
            task_difficulty=TaskDifficulty.BASIC,
            expected_percent=20,
            expected_cap=64_000,
        )

    async def test_expert_task_basic_worker(self) -> None:
        await self._run_and_assert_budget(
            worker_kind=WorkerKind.BASIC_AGENT,
            task_difficulty=TaskDifficulty.EXPERT,
            expected_percent=65,
            expected_cap=512_000,
        )

    async def test_standard_task_advanced_worker(self) -> None:
        await self._run_and_assert_budget(
            worker_kind=WorkerKind.ADVANCED_AGENT,
            task_difficulty=TaskDifficulty.STANDARD,
            expected_percent=35,
            expected_cap=128_000,
        )

    async def test_advanced_task_standard_worker(self) -> None:
        await self._run_and_assert_budget(
            worker_kind=WorkerKind.STANDARD_AGENT,
            task_difficulty=TaskDifficulty.ADVANCED,
            expected_percent=50,
            expected_cap=256_000,
        )

    async def test_basic_task_basic_worker(self) -> None:
        await self._run_and_assert_budget(
            worker_kind=WorkerKind.BASIC_AGENT,
            task_difficulty=TaskDifficulty.BASIC,
            expected_percent=20,
            expected_cap=64_000,
        )

    async def test_expert_task_expert_worker(self) -> None:
        await self._run_and_assert_budget(
            worker_kind=WorkerKind.EXPERT_AGENT,
            task_difficulty=TaskDifficulty.EXPERT,
            expected_percent=65,
            expected_cap=512_000,
        )

    # ── critical reverse case: Expert worker + Basic difficulty ─────────

    async def test_critical_reverse_expert_worker_basic_difficulty_budget(self) -> None:
        """Even with EXPERT_AGENT worker_kind, budget must use BASIC (20%/64k)."""
        window = 200_000
        snapshot = _make_snapshot(selected_context_window_tokens=window)
        req = _make_request(snapshot=snapshot)
        provider = mock.Mock(spec=AgentCliProvider)
        dr_out = _make_dispatch_result()

        with mock.patch.object(wa_module, "run_dispatch", return_value=dr_out):
            result = await run_worker(
                request=req,
                worker_kind=WorkerKind.EXPERT_AGENT,
                task_difficulty=TaskDifficulty.BASIC,
                providers={"fake": provider},
            )

        self.assertEqual(result.budget.budget_percent, 20)
        self.assertEqual(result.budget.budget_cap_tokens, 64_000)
        self.assertIs(result.worker_kind, WorkerKind.EXPERT_AGENT)
        self.assertIs(result.task_difficulty, TaskDifficulty.BASIC)


# ═══════════════════════════════════════════════════════════════════════════
# Execution Order
# ═══════════════════════════════════════════════════════════════════════════


class ExecutionOrderTests(unittest.IsolatedAsyncioTestCase):
    """Verify validate → compute_budget → run_dispatch → result."""

    async def test_call_order_via_side_effects(self) -> None:
        events: list[str] = []

        real_cb = compute_budget

        def _budget_side_effect(
            context_window_tokens: int,
            difficulty: TaskDifficulty,
        ) -> BudgetResult:
            events.append("budget")
            return real_cb(context_window_tokens, difficulty)

        async def _dispatch_side_effect(
            request: DispatchRequest,
            providers: Mapping[str, AgentCliProvider],
        ) -> DispatchResult:
            events.append("dispatch")
            return _make_dispatch_result()

        with mock.patch.object(
            wa_module, "compute_budget", side_effect=_budget_side_effect
        ), mock.patch.object(
            wa_module, "run_dispatch", side_effect=_dispatch_side_effect
        ):
            result = await run_worker(
                request=_make_request(),
                worker_kind=WorkerKind.BASIC_AGENT,
                task_difficulty=TaskDifficulty.BASIC,
                providers={"fake": mock.Mock()},
            )

        self.assertEqual(events, ["budget", "dispatch"])
        self.assertIsInstance(result, WorkerResult)

    async def test_compute_budget_called_exactly_once(self) -> None:
        dr_out = _make_dispatch_result()
        with mock.patch.object(
            wa_module, "compute_budget", wraps=wa_module.compute_budget
        ) as mock_budget, \
             mock.patch.object(
            wa_module, "run_dispatch", return_value=dr_out
        ) as mock_rd:
            await run_worker(
                request=_make_request(),
                worker_kind=WorkerKind.BASIC_AGENT,
                task_difficulty=TaskDifficulty.BASIC,
                providers={"fake": mock.Mock()},
            )
        mock_budget.assert_called_once()
        mock_rd.assert_called_once()

    async def test_budget_failure_no_dispatch(self) -> None:
        """When compute_budget raises, run_dispatch must NOT be called."""
        with mock.patch.object(
            wa_module, "compute_budget", side_effect=ValueError("bad budget")
        ) as mock_budget, \
             mock.patch.object(
            wa_module, "run_dispatch", return_value=_make_dispatch_result()
        ) as mock_rd:
            with self.assertRaises(ValueError):
                await run_worker(
                    request=_make_request(),
                    worker_kind=WorkerKind.BASIC_AGENT,
                    task_difficulty=TaskDifficulty.BASIC,
                    providers={"fake": mock.Mock()},
                )
        mock_budget.assert_called_once()
        mock_rd.assert_not_called()

    async def test_dispatch_failure_no_worker_result(self) -> None:
        """When run_dispatch raises, no WorkerResult is returned."""
        with mock.patch.object(
            wa_module, "compute_budget", wraps=wa_module.compute_budget
        ), mock.patch.object(
            wa_module, "run_dispatch",
            side_effect=ProviderNotSupportedError("unknown"),
        ):
            with self.assertRaises(ProviderNotSupportedError):
                await run_worker(
                    request=_make_request(),
                    worker_kind=WorkerKind.BASIC_AGENT,
                    task_difficulty=TaskDifficulty.BASIC,
                    providers={"fake": mock.Mock()},
                )

    async def test_providers_passed_by_identity(self) -> None:
        """providers mapping must be passed to run_dispatch as-is."""
        providers: dict[str, AgentCliProvider] = {"fake": mock.Mock()}
        dr_out = _make_dispatch_result()

        captured_providers = None

        async def _capture_dispatch(
            req: DispatchRequest,
            prov: Mapping[str, AgentCliProvider],
        ) -> DispatchResult:
            nonlocal captured_providers
            captured_providers = prov
            return dr_out

        with mock.patch.object(
            wa_module, "compute_budget", wraps=wa_module.compute_budget
        ), mock.patch.object(
            wa_module, "run_dispatch", side_effect=_capture_dispatch
        ):
            await run_worker(
                request=_make_request(),
                worker_kind=WorkerKind.BASIC_AGENT,
                task_difficulty=TaskDifficulty.BASIC,
                providers=providers,
            )

        self.assertIs(captured_providers, providers)
        # providers dict must not have been modified
        self.assertEqual(list(providers.keys()), ["fake"])

    async def test_request_passed_by_identity(self) -> None:
        """request must be passed to run_dispatch as-is."""
        req = _make_request()
        dr_out = _make_dispatch_result()

        captured_request = None

        async def _capture_dispatch(
            request: DispatchRequest,
            providers: Mapping[str, AgentCliProvider],
        ) -> DispatchResult:
            nonlocal captured_request
            captured_request = request
            return dr_out

        with mock.patch.object(
            wa_module, "compute_budget", wraps=wa_module.compute_budget
        ), mock.patch.object(
            wa_module, "run_dispatch", side_effect=_capture_dispatch
        ):
            await run_worker(
                request=req,
                worker_kind=WorkerKind.BASIC_AGENT,
                task_difficulty=TaskDifficulty.BASIC,
                providers={"fake": mock.Mock()},
            )

        self.assertIs(captured_request, req)


# ═══════════════════════════════════════════════════════════════════════════
# Budget Semantics — informational only
# ═══════════════════════════════════════════════════════════════════════════


class BudgetSemanticsTests(unittest.IsolatedAsyncioTestCase):
    """Budget is informational — full six-field BudgetResult, never enforced."""

    async def test_budget_result_is_complete_six_field(self) -> None:
        dr_out = _make_dispatch_result()
        with mock.patch.object(wa_module, "run_dispatch", return_value=dr_out):
            result = await run_worker(
                request=_make_request(),
                worker_kind=WorkerKind.STANDARD_AGENT,
                task_difficulty=TaskDifficulty.STANDARD,
                providers={"fake": mock.Mock()},
            )
        self.assertIsInstance(result.budget, BudgetResult)
        self.assertEqual(result.budget.context_window_tokens, 200_000)
        self.assertEqual(result.budget.difficulty, TaskDifficulty.STANDARD)
        self.assertEqual(result.budget.budget_percent, 35)
        self.assertEqual(result.budget.budget_cap_tokens, 128_000)
        self.assertEqual(result.budget.budget_tokens, 70_000)  # min(70k, 128k)
        self.assertEqual(
            result.budget.reserved_tokens,
            200_000 - result.budget.budget_tokens,
        )

    async def test_budget_uses_task_difficulty_not_worker_kind(self) -> None:
        """Cross-check: budget percent comes from task_difficulty only."""
        window = 200_000
        snapshot = _make_snapshot(selected_context_window_tokens=window)
        req = _make_request(snapshot=snapshot)
        dr_out = _make_dispatch_result()

        with mock.patch.object(wa_module, "run_dispatch", return_value=dr_out):
            result = await run_worker(
                request=req,
                worker_kind=WorkerKind.BASIC_AGENT,
                task_difficulty=TaskDifficulty.EXPERT,
                providers={"fake": mock.Mock()},
            )
        # Expert → 65%, 512k cap
        self.assertEqual(result.budget.budget_percent, 65)
        self.assertEqual(result.budget.budget_cap_tokens, 512_000)
        # min(200k * 65% = 130k, 512k) = 130k
        self.assertEqual(result.budget.budget_tokens, 130_000)

    async def test_prompt_not_modified(self) -> None:
        """WorkerAdapter must not modify the request in any way."""
        prompt = "test prompt content"
        req = _make_request(prompt=prompt)
        dr_out = _make_dispatch_result()

        captured_request = None

        async def _capture(request: DispatchRequest,
                           providers: Mapping[str, AgentCliProvider]) -> DispatchResult:
            nonlocal captured_request
            captured_request = request
            return dr_out

        with mock.patch.object(wa_module, "run_dispatch", side_effect=_capture):
            await run_worker(
                request=req,
                worker_kind=WorkerKind.BASIC_AGENT,
                task_difficulty=TaskDifficulty.BASIC,
                providers={"fake": mock.Mock()},
            )

        self.assertIsNotNone(captured_request)
        self.assertEqual(captured_request.prompt, prompt)
        self.assertIs(captured_request, req)


# ═══════════════════════════════════════════════════════════════════════════
# Exception Propagation
# ═══════════════════════════════════════════════════════════════════════════


class ExceptionPropagationTests(unittest.IsolatedAsyncioTestCase):
    """All exceptions propagate without wrapping."""

    async def test_compute_budget_typeerror(self) -> None:
        with mock.patch.object(
            wa_module, "compute_budget",
            side_effect=TypeError("bad type"),
        ):
            with self.assertRaises(TypeError) as ctx:
                await run_worker(
                    request=_make_request(),
                    worker_kind=WorkerKind.BASIC_AGENT,
                    task_difficulty=TaskDifficulty.BASIC,
                    providers={"fake": mock.Mock()},
                )
        self.assertIn("bad type", str(ctx.exception))

    async def test_compute_budget_valueerror(self) -> None:
        with mock.patch.object(
            wa_module, "compute_budget",
            side_effect=ValueError("too small"),
        ):
            with self.assertRaises(ValueError) as ctx:
                await run_worker(
                    request=_make_request(),
                    worker_kind=WorkerKind.BASIC_AGENT,
                    task_difficulty=TaskDifficulty.BASIC,
                    providers={"fake": mock.Mock()},
                )
        self.assertIn("too small", str(ctx.exception))

    async def test_provider_not_supported_error(self) -> None:
        with mock.patch.object(
            wa_module, "run_dispatch",
            side_effect=ProviderNotSupportedError("unknown"),
        ):
            with self.assertRaises(ProviderNotSupportedError):
                await run_worker(
                    request=_make_request(),
                    worker_kind=WorkerKind.BASIC_AGENT,
                    task_difficulty=TaskDifficulty.BASIC,
                    providers={"fake": mock.Mock()},
                )

    async def test_dispatch_invocation_error(self) -> None:
        with mock.patch.object(
            wa_module, "run_dispatch",
            side_effect=DispatchInvocationError("bad invocation"),
        ):
            with self.assertRaises(DispatchInvocationError):
                await run_worker(
                    request=_make_request(),
                    worker_kind=WorkerKind.BASIC_AGENT,
                    task_difficulty=TaskDifficulty.BASIC,
                    providers={"fake": mock.Mock()},
                )

    async def test_dispatch_timeout_error(self) -> None:
        with mock.patch.object(
            wa_module, "run_dispatch",
            side_effect=DispatchTimeoutError(300),
        ):
            with self.assertRaises(DispatchTimeoutError):
                await run_worker(
                    request=_make_request(),
                    worker_kind=WorkerKind.BASIC_AGENT,
                    task_difficulty=TaskDifficulty.BASIC,
                    providers={"fake": mock.Mock()},
                )

    async def test_dispatch_cancelled_error(self) -> None:
        with mock.patch.object(
            wa_module, "run_dispatch",
            side_effect=DispatchCancelledError(),
        ):
            with self.assertRaises(DispatchCancelledError):
                await run_worker(
                    request=_make_request(),
                    worker_kind=WorkerKind.BASIC_AGENT,
                    task_difficulty=TaskDifficulty.BASIC,
                    providers={"fake": mock.Mock()},
                )

    async def test_dispatch_non_zero_exit_error(self) -> None:
        with mock.patch.object(
            wa_module, "run_dispatch",
            side_effect=DispatchNonZeroExitError(
                exit_code=1,
                stdout_sha256="a" * 64,
                stderr_sha256="b" * 64,
                stderr_bytes=b"error",
            ),
        ):
            with self.assertRaises(DispatchNonZeroExitError):
                await run_worker(
                    request=_make_request(),
                    worker_kind=WorkerKind.BASIC_AGENT,
                    task_difficulty=TaskDifficulty.BASIC,
                    providers={"fake": mock.Mock()},
                )

    async def test_asyncio_cancelled_error(self) -> None:
        with mock.patch.object(
            wa_module, "run_dispatch",
            side_effect=asyncio.CancelledError(),
        ):
            with self.assertRaises(asyncio.CancelledError):
                await run_worker(
                    request=_make_request(),
                    worker_kind=WorkerKind.BASIC_AGENT,
                    task_difficulty=TaskDifficulty.BASIC,
                    providers={"fake": mock.Mock()},
                )

    async def test_no_wrapping_to_custom_exception(self) -> None:
        """Verify that DispatchGatewayError is not wrapped in a new type."""
        with mock.patch.object(
            wa_module, "run_dispatch",
            side_effect=ProviderNotSupportedError("x"),
        ):
            with self.assertRaises(ProviderNotSupportedError) as ctx:
                await run_worker(
                    request=_make_request(),
                    worker_kind=WorkerKind.BASIC_AGENT,
                    task_difficulty=TaskDifficulty.BASIC,
                    providers={"fake": mock.Mock()},
                )
            # Must be the exact same exception type, not wrapped
            self.assertIs(type(ctx.exception), ProviderNotSupportedError)


# ═══════════════════════════════════════════════════════════════════════════
# Output Preservation — opaque bytes
# ═══════════════════════════════════════════════════════════════════════════


class OpaqueOutputTests(unittest.IsolatedAsyncioTestCase):
    """DispatchResult is carried as-is; bytes remain unchanged."""

    async def test_dispatch_result_preserved_identity(self) -> None:
        dr = _make_dispatch_result()
        with mock.patch.object(
            wa_module, "run_dispatch", return_value=dr
        ):
            result = await run_worker(
                request=_make_request(),
                worker_kind=WorkerKind.BASIC_AGENT,
                task_difficulty=TaskDifficulty.BASIC,
                providers={"fake": mock.Mock()},
            )
        self.assertIs(result.dispatch_result, dr)

    async def test_non_utf8_bytes_preserved(self) -> None:
        """stdout containing non-UTF-8 bytes must survive unchanged."""
        non_utf8_stdout = b"\xff\xfe\x00\x01" + b"hello"
        non_utf8_stderr = b"\x80\x81\x82" + b"error"

        dr = DispatchResult(
            identity=DispatchIdentity(
                task_id="T", revision=1, attempt=1, dispatch_id="D"
            ),
            provider="fake",
            model_id="fake-model",
            duration_seconds=0.5,
            stdout=non_utf8_stdout,
            stderr=non_utf8_stderr,
            stdout_sha256="s" * 64,
            stderr_sha256="e" * 64,
        )

        with mock.patch.object(wa_module, "run_dispatch", return_value=dr):
            result = await run_worker(
                request=_make_request(),
                worker_kind=WorkerKind.BASIC_AGENT,
                task_difficulty=TaskDifficulty.BASIC,
                providers={"fake": mock.Mock()},
            )

        self.assertIs(result.dispatch_result, dr)
        self.assertEqual(result.dispatch_result.stdout, non_utf8_stdout)
        self.assertEqual(result.dispatch_result.stderr, non_utf8_stderr)

    async def test_sha_fields_preserved(self) -> None:
        stdout_sha = "0" * 64
        stderr_sha = "f" * 64
        dr = DispatchResult(
            identity=DispatchIdentity(
                task_id="T", revision=1, attempt=1, dispatch_id="D"
            ),
            provider="p",
            model_id="m",
            duration_seconds=0.1,
            stdout=b"out",
            stderr=b"err",
            stdout_sha256=stdout_sha,
            stderr_sha256=stderr_sha,
        )

        with mock.patch.object(wa_module, "run_dispatch", return_value=dr):
            result = await run_worker(
                request=_make_request(),
                worker_kind=WorkerKind.BASIC_AGENT,
                task_difficulty=TaskDifficulty.BASIC,
                providers={"fake": mock.Mock()},
            )

        self.assertEqual(result.dispatch_result.stdout_sha256, stdout_sha)
        self.assertEqual(result.dispatch_result.stderr_sha256, stderr_sha)

    async def test_no_decode_attempted(self) -> None:
        """Even valid UTF-8 bytes should not be decoded by the adapter."""
        dr = DispatchResult(
            identity=DispatchIdentity(
                task_id="T", revision=1, attempt=1, dispatch_id="D"
            ),
            provider="p",
            model_id="m",
            duration_seconds=0.1,
            stdout=b'{"result": "ok"}',
            stderr=b"",
            stdout_sha256="a" * 64,
            stderr_sha256="b" * 64,
        )

        with mock.patch.object(wa_module, "run_dispatch", return_value=dr):
            result = await run_worker(
                request=_make_request(),
                worker_kind=WorkerKind.BASIC_AGENT,
                task_difficulty=TaskDifficulty.BASIC,
                providers={"fake": mock.Mock()},
            )

        # stdout remains bytes — no decode
        self.assertIsInstance(result.dispatch_result.stdout, bytes)
        self.assertEqual(result.dispatch_result.stdout, b'{"result": "ok"}')


# ═══════════════════════════════════════════════════════════════════════════
# Zero Side Effects
# ═══════════════════════════════════════════════════════════════════════════


class ZeroSideEffectsTests(unittest.IsolatedAsyncioTestCase):
    """WorkerAdapter produces zero file writes, zero subprocess, zero I/O."""

    async def test_success_path_no_file_writes(self) -> None:
        dr = _make_dispatch_result()
        with mock.patch.object(wa_module, "run_dispatch", return_value=dr), \
             mock.patch("builtins.open") as mock_open, \
             mock.patch("pathlib.Path.write_text") as mock_write_text:
            await run_worker(
                request=_make_request(),
                worker_kind=WorkerKind.BASIC_AGENT,
                task_difficulty=TaskDifficulty.BASIC,
                providers={"fake": mock.Mock()},
            )
        mock_open.assert_not_called()
        mock_write_text.assert_not_called()

    async def test_success_path_no_subprocess(self) -> None:
        dr = _make_dispatch_result()
        with mock.patch.object(wa_module, "run_dispatch", return_value=dr), \
             mock.patch("subprocess.run") as mock_sp_run, \
             mock.patch("asyncio.create_subprocess_exec") as mock_asp:
            await run_worker(
                request=_make_request(),
                worker_kind=WorkerKind.BASIC_AGENT,
                task_difficulty=TaskDifficulty.BASIC,
                providers={"fake": mock.Mock()},
            )
        mock_sp_run.assert_not_called()
        mock_asp.assert_not_called()

    async def test_success_path_no_env_modification(self) -> None:
        dr = _make_dispatch_result()
        original_env = dict(os.environ)
        with mock.patch.object(wa_module, "run_dispatch", return_value=dr):
            await run_worker(
                request=_make_request(),
                worker_kind=WorkerKind.BASIC_AGENT,
                task_difficulty=TaskDifficulty.BASIC,
                providers={"fake": mock.Mock()},
            )
        self.assertEqual(dict(os.environ), original_env)

    async def test_success_path_no_git(self) -> None:
        dr = _make_dispatch_result()
        with mock.patch.object(wa_module, "run_dispatch", return_value=dr), \
             mock.patch("subprocess.run") as mock_sp:
            await run_worker(
                request=_make_request(),
                worker_kind=WorkerKind.BASIC_AGENT,
                task_difficulty=TaskDifficulty.BASIC,
                providers={"fake": mock.Mock()},
            )
        # No git-specific calls
        for call_args in mock_sp.call_args_list:
            args = call_args[0] if call_args[0] else call_args[1].get("args", [])
            if args and len(args) > 0:
                self.assertNotIn("git", str(args[0]).lower())

    async def test_success_path_providers_not_modified(self) -> None:
        providers: dict[str, AgentCliProvider] = {"fake": mock.Mock()}
        original_keys = list(providers.keys())

        dr = _make_dispatch_result()
        with mock.patch.object(wa_module, "run_dispatch", return_value=dr):
            await run_worker(
                request=_make_request(),
                worker_kind=WorkerKind.BASIC_AGENT,
                task_difficulty=TaskDifficulty.BASIC,
                providers=providers,
            )

        self.assertEqual(list(providers.keys()), original_keys)
        self.assertEqual(len(providers), 1)

    async def test_budget_failure_path_no_file_writes(self) -> None:
        with mock.patch.object(
            wa_module, "compute_budget",
            side_effect=ValueError("bad"),
        ), mock.patch("builtins.open") as mock_open:
            with self.assertRaises(ValueError):
                await run_worker(
                    request=_make_request(),
                    worker_kind=WorkerKind.BASIC_AGENT,
                    task_difficulty=TaskDifficulty.BASIC,
                    providers={"fake": mock.Mock()},
                )
        mock_open.assert_not_called()

    async def test_dispatch_failure_path_no_file_writes(self) -> None:
        with mock.patch.object(
            wa_module, "run_dispatch",
            side_effect=ProviderNotSupportedError("x"),
        ), mock.patch("builtins.open") as mock_open:
            with self.assertRaises(ProviderNotSupportedError):
                await run_worker(
                    request=_make_request(),
                    worker_kind=WorkerKind.BASIC_AGENT,
                    task_difficulty=TaskDifficulty.BASIC,
                    providers={"fake": mock.Mock()},
                )
        mock_open.assert_not_called()


# ═══════════════════════════════════════════════════════════════════════════
# Side-effect-free import (isolated subprocess)
# ═══════════════════════════════════════════════════════════════════════════


class IsolatedImportTests(unittest.TestCase):
    """Import must have zero I/O side effects — verified in subprocess."""

    def test_import_worker_adapter_no_io_side_effects(self) -> None:
        result = subprocess.run(
            [sys.executable, "-c",
             f"import sys; sys.path.insert(0, {str(_SCRIPTS)!r}); "
             "import worker_adapter"],
            capture_output=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0,
                         f"stderr: {result.stderr.decode('utf-8', errors='replace')}")
        self.assertEqual(result.stdout, b"")
        self.assertEqual(result.stderr, b"")


# ═══════════════════════════════════════════════════════════════════════════
# Happy Path — end-to-end with mocked Gateway
# ═══════════════════════════════════════════════════════════════════════════


class HappyPathTests(unittest.IsolatedAsyncioTestCase):
    """Full successful path with all four tiers."""

    async def test_basic_worker_returns_complete_result(self) -> None:
        dr = _make_dispatch_result()
        with mock.patch.object(wa_module, "run_dispatch", return_value=dr):
            result = await run_worker(
                request=_make_request(),
                worker_kind=WorkerKind.BASIC_AGENT,
                task_difficulty=TaskDifficulty.BASIC,
                providers={"fake": mock.Mock()},
            )
        self.assertIs(result.dispatch_result, dr)
        self.assertIs(result.worker_kind, WorkerKind.BASIC_AGENT)
        self.assertIs(result.task_difficulty, TaskDifficulty.BASIC)
        self.assertIsInstance(result.budget, BudgetResult)

    async def test_standard_worker_returns_complete_result(self) -> None:
        dr = _make_dispatch_result()
        with mock.patch.object(wa_module, "run_dispatch", return_value=dr):
            result = await run_worker(
                request=_make_request(),
                worker_kind=WorkerKind.STANDARD_AGENT,
                task_difficulty=TaskDifficulty.STANDARD,
                providers={"fake": mock.Mock()},
            )
        self.assertIsInstance(result, WorkerResult)
        self.assertEqual(result.budget.budget_percent, 35)

    async def test_advanced_worker_returns_complete_result(self) -> None:
        dr = _make_dispatch_result()
        with mock.patch.object(wa_module, "run_dispatch", return_value=dr):
            result = await run_worker(
                request=_make_request(),
                worker_kind=WorkerKind.ADVANCED_AGENT,
                task_difficulty=TaskDifficulty.ADVANCED,
                providers={"fake": mock.Mock()},
            )
        self.assertIsInstance(result, WorkerResult)
        self.assertEqual(result.budget.budget_percent, 50)

    async def test_expert_worker_returns_complete_result(self) -> None:
        dr = _make_dispatch_result()
        with mock.patch.object(wa_module, "run_dispatch", return_value=dr):
            result = await run_worker(
                request=_make_request(),
                worker_kind=WorkerKind.EXPERT_AGENT,
                task_difficulty=TaskDifficulty.EXPERT,
                providers={"fake": mock.Mock()},
            )
        self.assertIsInstance(result, WorkerResult)
        self.assertEqual(result.budget.budget_percent, 65)

    async def test_large_context_window_budget_capped(self) -> None:
        """With a 2M-token window, Expert budget should cap at 512k."""
        window = 2_000_000
        snapshot = _make_snapshot(selected_context_window_tokens=window)
        req = _make_request(snapshot=snapshot)
        dr = _make_dispatch_result()

        with mock.patch.object(wa_module, "run_dispatch", return_value=dr):
            result = await run_worker(
                request=req,
                worker_kind=WorkerKind.EXPERT_AGENT,
                task_difficulty=TaskDifficulty.EXPERT,
                providers={"fake": mock.Mock()},
            )

        # 2M * 65% = 1.3M, capped at 512k
        self.assertEqual(result.budget.budget_tokens, 512_000)
        self.assertEqual(result.budget.budget_percent, 65)
        self.assertEqual(result.budget.budget_cap_tokens, 512_000)


# ═══════════════════════════════════════════════════════════════════════════
# No WorkerKind derivation — explicit exclusion tests
# ═══════════════════════════════════════════════════════════════════════════


class NoWorkerKindDerivationTests(unittest.TestCase):
    """Verify the production module contains no prohibited patterns."""

    def test_no_worker_kind_to_difficulty_mapping(self) -> None:
        """Module must not contain _WORKER_KIND_TO_DIFFICULTY."""
        source = (_SCRIPTS / "worker_adapter.py").read_text(encoding="utf-8")
        self.assertNotIn("_WORKER_KIND_TO_DIFFICULTY", source)
        self.assertNotIn("removesuffix", source)
        self.assertNotIn("TaskDifficulty(worker_kind", source)

    def test_no_task_difficulty_derivation(self) -> None:
        """Module must not derive TaskDifficulty from WorkerKind."""
        source = (_SCRIPTS / "worker_adapter.py").read_text(encoding="utf-8")
        self.assertNotIn("worker_kind.value", source)

    def test_no_worker_output_type(self) -> None:
        """Module must not define WorkerOutput or final_text."""
        source = (_SCRIPTS / "worker_adapter.py").read_text(encoding="utf-8")
        self.assertNotIn("WorkerOutput", source)
        self.assertNotIn("final_text", source)

    def test_no_decoder_protocol(self) -> None:
        """Module must not define a decoder Protocol."""
        source = (_SCRIPTS / "worker_adapter.py").read_text(encoding="utf-8")
        self.assertNotIn("Decoder", source)
        self.assertNotIn("decode", source)

    def test_no_retry_loop(self) -> None:
        """Module must not contain retry logic."""
        source = (_SCRIPTS / "worker_adapter.py").read_text(encoding="utf-8")
        self.assertNotIn("retry", source.lower())
        self.assertNotIn("Retry", source)

    def test_no_file_write(self) -> None:
        """Module must not write files."""
        source = (_SCRIPTS / "worker_adapter.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        calls = tuple(
            ast.unparse(node.func)
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
        )
        self.assertNotIn("open", calls)
        self.assertFalse(
            any(name.endswith(("write_text", "write_bytes")) for name in calls),
            calls,
        )

    def test_no_subprocess(self) -> None:
        """Module must not launch subprocesses."""
        source = (_SCRIPTS / "worker_adapter.py").read_text(encoding="utf-8")
        self.assertNotIn("create_subprocess", source)

    def test_no_git(self) -> None:
        """Module must not run git."""
        source = (_SCRIPTS / "worker_adapter.py").read_text(encoding="utf-8")
        self.assertNotIn("git", source.lower())

    # ── Error message source checks ────────────────────────────────────

    def test_no_worker_kind_repr_in_source(self) -> None:
        """worker_kind validation must not use !r, repr(), or str()
        on the input value."""
        source = (_SCRIPTS / "worker_adapter.py").read_text(encoding="utf-8")
        # Extract the worker_kind validation block — lines between
        # "# Validate worker_kind" and the next "# ──" marker
        lines = source.splitlines()
        in_block = False
        block: list[str] = []
        for line in lines:
            if "validate worker_kind" in line.casefold():
                in_block = True
                continue
            if in_block:
                if line.strip().startswith("# ──"):
                    break
                block.append(line)
        block_text = "\n".join(block)
        self.assertNotIn("worker_kind!r", block_text)
        self.assertNotIn("repr(worker_kind)", block_text)
        self.assertNotIn("str(worker_kind)", block_text)

    def test_no_task_difficulty_repr_in_source(self) -> None:
        """task_difficulty validation must not use !r, repr(), or str()
        on the input value."""
        source = (_SCRIPTS / "worker_adapter.py").read_text(encoding="utf-8")
        lines = source.splitlines()
        in_block = False
        block: list[str] = []
        for line in lines:
            if "validate task_difficulty" in line.casefold():
                in_block = True
                continue
            if in_block:
                if line.strip().startswith("# ──"):
                    break
                block.append(line)
        block_text = "\n".join(block)
        self.assertNotIn("task_difficulty!r", block_text)
        self.assertNotIn("repr(task_difficulty)", block_text)
        self.assertNotIn("str(task_difficulty)", block_text)


# ═══════════════════════════════════════════════════════════════════════════
# Error Message Value Leakage — negative tests
# ═══════════════════════════════════════════════════════════════════════════


_SENSITIVE_MARKER = "SECRET_TASK_PROMPT_13_9B"


class ErrorMessageLeakageTests(unittest.IsolatedAsyncioTestCase):
    """TypeError messages must not include the rejected input value."""

    async def test_worker_kind_rejection_does_not_leak_value(self) -> None:
        with mock.patch.object(wa_module, "compute_budget") as mock_budget, \
             mock.patch.object(wa_module, "run_dispatch") as mock_dispatch:
            with self.assertRaises(TypeError) as ctx:
                await run_worker(
                    request=_make_request(),
                    worker_kind=_SENSITIVE_MARKER,
                    task_difficulty=TaskDifficulty.BASIC,
                    providers={"fake": mock.Mock()},
                )
            mock_budget.assert_not_called()
            mock_dispatch.assert_not_called()

        msg = str(ctx.exception)
        self.assertIn("worker_kind", msg,
                      "error must name the parameter")
        self.assertIn("WorkerKind", msg,
                      "error must name the expected type")
        self.assertNotIn(_SENSITIVE_MARKER, msg,
                         "error must not leak the rejected value")

    async def test_task_difficulty_rejection_does_not_leak_value(self) -> None:
        with mock.patch.object(wa_module, "compute_budget") as mock_budget, \
             mock.patch.object(wa_module, "run_dispatch") as mock_dispatch:
            with self.assertRaises(TypeError) as ctx:
                await run_worker(
                    request=_make_request(),
                    worker_kind=WorkerKind.BASIC_AGENT,
                    task_difficulty=_SENSITIVE_MARKER,
                    providers={"fake": mock.Mock()},
                )
            mock_budget.assert_not_called()
            mock_dispatch.assert_not_called()

        msg = str(ctx.exception)
        self.assertIn("task_difficulty", msg,
                      "error must name the parameter")
        self.assertIn("TaskDifficulty", msg,
                      "error must name the expected type")
        self.assertNotIn(_SENSITIVE_MARKER, msg,
                         "error must not leak the rejected value")


class ReprNotCalledTests(unittest.IsolatedAsyncioTestCase):
    """Passing a malicious object whose __repr__ raises must still produce
    a clean TypeError without calling __repr__."""

    class _ReprMustNotBeCalled:
        """An object whose __repr__ blows up — so we can prove it's unused."""
        def __repr__(self) -> str:
            raise AssertionError("repr must not be called")

    async def test_worker_kind_malicious_repr_not_called(self) -> None:
        bad = self._ReprMustNotBeCalled()
        with mock.patch.object(wa_module, "compute_budget") as mock_budget, \
             mock.patch.object(wa_module, "run_dispatch") as mock_dispatch:
            with self.assertRaises(TypeError) as ctx:
                await run_worker(
                    request=_make_request(),
                    worker_kind=bad,
                    task_difficulty=TaskDifficulty.BASIC,
                    providers={"fake": mock.Mock()},
                )
            mock_budget.assert_not_called()
            mock_dispatch.assert_not_called()
        msg = str(ctx.exception)
        self.assertIn("worker_kind", msg)
        self.assertIn("WorkerKind", msg)
        self.assertIn("ReprMustNotBeCalled", msg,
                      "error may include the type name")

    async def test_task_difficulty_malicious_repr_not_called(self) -> None:
        bad = self._ReprMustNotBeCalled()
        with mock.patch.object(wa_module, "compute_budget") as mock_budget, \
             mock.patch.object(wa_module, "run_dispatch") as mock_dispatch:
            with self.assertRaises(TypeError) as ctx:
                await run_worker(
                    request=_make_request(),
                    worker_kind=WorkerKind.BASIC_AGENT,
                    task_difficulty=bad,
                    providers={"fake": mock.Mock()},
                )
            mock_budget.assert_not_called()
            mock_dispatch.assert_not_called()
        msg = str(ctx.exception)
        self.assertIn("task_difficulty", msg)
        self.assertIn("TaskDifficulty", msg)
        self.assertIn("ReprMustNotBeCalled", msg,
                      "error may include the type name")

    async def test_same_malicious_object_both_params(self) -> None:
        """Pass the same malicious object to both worker_kind and
        task_difficulty — both must produce clean TypeErrors."""
        bad = self._ReprMustNotBeCalled()
        # First: as worker_kind (rejected first in validation order)
        with mock.patch.object(wa_module, "compute_budget") as mock_budget, \
             mock.patch.object(wa_module, "run_dispatch") as mock_dispatch:
            with self.assertRaises(TypeError) as ctx:
                await run_worker(
                    request=_make_request(),
                    worker_kind=bad,
                    task_difficulty=bad,
                    providers={"fake": mock.Mock()},
                )
            mock_budget.assert_not_called()
            mock_dispatch.assert_not_called()
        # Must be the worker_kind TypeError (first validation in order)
        msg = str(ctx.exception)
        self.assertIn("worker_kind", msg)
        self.assertNotIn("AssertionError", msg)
        self.assertNotIn("repr must not be called", msg)


# ═══════════════════════════════════════════════════════════════════════════
# TC-13.18b.2 — WorkerAdapter Observed Tests
# ═══════════════════════════════════════════════════════════════════════════


class WorkerAdapterObservedApiTests(unittest.IsolatedAsyncioTestCase):
    """Smoke tests for run_worker_observed API.

    Uses IsolatedAsyncioTestCase so that ``async def`` test methods
    actually execute — not just return unawaited coroutines.
    """

    def test_all_exports_run_worker_observed(self) -> None:
        self.assertIn("run_worker_observed", wa_module.__all__)

    async def test_run_worker_observed_computes_budget_once(self) -> None:
        """Budget must be computed exactly once in run_worker_observed."""
        call_count = 0

        def _fake_compute_budget(ctx_tokens: int,
                                  difficulty: TaskDifficulty
                                  ) -> BudgetResult:
            nonlocal call_count
            call_count += 1
            return BudgetResult(
                context_window_tokens=ctx_tokens,
                difficulty=difficulty,
                budget_percent=50,
                budget_cap_tokens=256000,
                budget_tokens=100000,
                reserved_tokens=100000,
            )

        with mock.patch.object(wa_module, "compute_budget",
                               side_effect=_fake_compute_budget):
            with mock.patch.object(wa_module, "run_dispatch_observed",
                                   new_callable=mock.AsyncMock) as mock_rd:
                mock_rd.return_value = DispatchResult(
                    identity=_make_request().identity,
                    provider="fake",
                    model_id="fake-model",
                    duration_seconds=0.1,
                    stdout=b"ok",
                    stderr=b"",
                    stdout_sha256="aa",
                    stderr_sha256="bb",
                )
                await run_worker_observed(
                    request=_make_request(),
                    worker_kind=WorkerKind.STANDARD_AGENT,
                    task_difficulty=TaskDifficulty.STANDARD,
                    providers={"fake": mock.Mock()},
                    observer=mock.AsyncMock(),
                )
        self.assertEqual(call_count, 1, "Budget must be computed exactly once")

    async def test_run_worker_observed_observer_passed_by_identity(self) -> None:
        """Observer must be passed by object identity to run_dispatch_observed."""
        observer = mock.AsyncMock()

        with mock.patch.object(wa_module, "run_dispatch_observed",
                               new_callable=mock.AsyncMock) as mock_rd:
            mock_rd.return_value = DispatchResult(
                identity=_make_request().identity,
                provider="fake",
                model_id="fake-model",
                duration_seconds=0.1,
                stdout=b"ok",
                stderr=b"",
                stdout_sha256="aa",
                stderr_sha256="bb",
            )
            await run_worker_observed(
                request=_make_request(),
                worker_kind=WorkerKind.STANDARD_AGENT,
                task_difficulty=TaskDifficulty.STANDARD,
                providers={"fake": mock.Mock()},
                observer=observer,
            )
            # observer is 3rd positional arg to run_dispatch_observed
            args, _kwargs = mock_rd.call_args
            self.assertEqual(len(args), 3, f"Expected 3 positional args, got {len(args)}")
            self.assertIs(args[2], observer)

    async def test_run_worker_observed_request_passed_by_identity(self) -> None:
        """DispatchRequest must be passed by object identity."""
        request = _make_request()
        observer = mock.AsyncMock()

        with mock.patch.object(wa_module, "run_dispatch_observed") as mock_rd:
            mock_rd.return_value = DispatchResult(
                identity=request.identity,
                provider="fake",
                model_id="fake-model",
                duration_seconds=0.1,
                stdout=b"ok",
                stderr=b"",
                stdout_sha256="aa",
                stderr_sha256="bb",
            )
            await run_worker_observed(
                request=request,
                worker_kind=WorkerKind.STANDARD_AGENT,
                task_difficulty=TaskDifficulty.STANDARD,
                providers={"fake": mock.Mock()},
                observer=observer,
            )
            args, _ = mock_rd.call_args
            self.assertIs(args[0], request)

    async def test_run_worker_observed_providers_passed_by_identity(self) -> None:
        """Providers mapping must be passed by object identity."""
        providers: dict = {"fake": mock.Mock()}
        observer = mock.AsyncMock()

        with mock.patch.object(wa_module, "run_dispatch_observed") as mock_rd:
            mock_rd.return_value = DispatchResult(
                identity=_make_request().identity,
                provider="fake",
                model_id="fake-model",
                duration_seconds=0.1,
                stdout=b"ok",
                stderr=b"",
                stdout_sha256="aa",
                stderr_sha256="bb",
            )
            await run_worker_observed(
                request=_make_request(),
                worker_kind=WorkerKind.STANDARD_AGENT,
                task_difficulty=TaskDifficulty.STANDARD,
                providers=providers,
                observer=observer,
            )
            args, _ = mock_rd.call_args
            self.assertIs(args[1], providers)

    async def test_run_worker_observed_observer_exception_propagates(self) -> None:
        """Observer exception must propagate as-is through run_worker_observed."""

        class TestError(Exception):
            pass

        async def _failing_observed(*args: object, **kwargs: object) -> None:
            raise TestError("observer failure")

        with mock.patch.object(wa_module, "run_dispatch_observed",
                               side_effect=_failing_observed):
            with self.assertRaises(TestError):
                await run_worker_observed(
                    request=_make_request(),
                    worker_kind=WorkerKind.STANDARD_AGENT,
                    task_difficulty=TaskDifficulty.STANDARD,
                    providers={"fake": mock.Mock()},
                    observer=mock.AsyncMock(),
                )

    async def test_original_run_worker_unchanged(self) -> None:
        """run_worker() must still call run_dispatch (non-observed path)."""
        with mock.patch.object(wa_module, "run_dispatch") as mock_rd:
            mock_rd.return_value = DispatchResult(
                identity=_make_request().identity,
                provider="fake",
                model_id="fake-model",
                duration_seconds=0.1,
                stdout=b"ok",
                stderr=b"",
                stdout_sha256="aa",
                stderr_sha256="bb",
            )
            await run_worker(
                request=_make_request(),
                worker_kind=WorkerKind.STANDARD_AGENT,
                task_difficulty=TaskDifficulty.STANDARD,
                providers={"fake": mock.Mock()},
            )
        mock_rd.assert_called_once()
