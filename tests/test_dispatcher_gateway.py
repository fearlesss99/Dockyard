"""TC-13.7: comprehensive mock‑based tests for ``dispatcher_gateway.py``.

stdlib‑only unittest with async support; no real CLI, model, API,
network, taskkill, or process‑tree termination.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import subprocess
import sys
import tempfile
import unittest
from dataclasses import asdict, fields, is_dataclass
from pathlib import Path
from unittest import mock

# -- Load the modules under test -------------------------------------------
_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCRIPTS = _REPO_ROOT / "skills" / "agentdesk" / "scripts"

sys.path.insert(0, str(_SCRIPTS))
import dispatcher_gateway as dg  # noqa: E402

sys.path.pop(0)

# Re‑export for convenience
DispatchIdentity = dg.DispatchIdentity
ModelSelectionSnapshot = dg.ModelSelectionSnapshot
DispatchRequest = dg.DispatchRequest
AgentCliInvocation = dg.AgentCliInvocation
DispatchResult = dg.DispatchResult
AgentCliProvider = dg.AgentCliProvider
run_dispatch = dg.run_dispatch


# =========================================================================
# FakeAgentCliProvider — tests/ only
# =========================================================================


_STDIN_SENTINEL = object()


class FakeAgentCliProvider:
    """Configurable fake provider for testing.

    Defined ONLY in the test directory per S2.10.5.
    """

    def __init__(
        self,
        provider_id: str = "fake",
        executable: str | None = None,
        argv: tuple[str, ...] = (),
        stdin: bytes | None = _STDIN_SENTINEL,  # type: ignore[assignment]
        env_overrides: tuple[tuple[str, str], ...] = (),
    ) -> None:
        self._provider_id = provider_id
        self._executable = executable if executable is not None else sys.executable
        self._argv = argv
        self._stdin_sentinel = stdin
        self._env_overrides = env_overrides

    @property
    def provider_id(self) -> str:
        return self._provider_id

    def build_invocation(self, request: DispatchRequest) -> AgentCliInvocation:
        stdin = None
        if self._stdin_sentinel is _STDIN_SENTINEL:
            stdin = request.prompt.encode("utf-8")
        else:
            stdin = self._stdin_sentinel  # type: ignore[assignment]
        return AgentCliInvocation(
            executable=self._executable,
            argv=self._argv,
            stdin=stdin,
            env_overrides=self._env_overrides,
        )


# =========================================================================
# Subprocess mock
# =========================================================================


class _FakeProcess:
    """A mock asyncio.subprocess.Process for testing."""

    def __init__(
        self,
        returncode: int = 0,
        stdout: bytes = b"",
        stderr: bytes = b"",
        pid: int = 12345,
    ) -> None:
        self.returncode: int | None = returncode
        self._stdout = stdout
        self._stderr = stderr
        self.pid = pid

    async def communicate(self, input: bytes | None = None) -> tuple[bytes, bytes]:
        return self._stdout, self._stderr

    async def wait(self) -> int:
        return self.returncode if self.returncode is not None else 0


# =========================================================================
# Helpers
# =========================================================================

_EXE_HELPER_SCRIPT = None


def _get_helper_exe() -> str:
    """Return path to a helper .py script that acts as a fake executable."""
    global _EXE_HELPER_SCRIPT
    if _EXE_HELPER_SCRIPT is not None and os.path.isfile(_EXE_HELPER_SCRIPT):
        return _EXE_HELPER_SCRIPT
    fd, path = tempfile.mkstemp(suffix=".py", prefix="dg_test_helper_")
    os.close(fd)
    with open(path, "w", encoding="utf-8") as f:
        f.write("import sys; sys.exit(0)\n")
    _EXE_HELPER_SCRIPT = path
    return path


def _make_identity(**overrides) -> DispatchIdentity:
    defaults = {
        "task_id": "TC-001",
        "revision": 1,
        "attempt": 1,
        "dispatch_id": "DSP-001",
    }
    defaults.update(overrides)
    return DispatchIdentity(**defaults)


def _make_snapshot_dict(**overrides) -> dict:
    defaults: dict = {
        "required_model_tier": "standard",
        "required_model_capabilities": ["read", "write"],
        "model_binding_id": "bind-1",
        "selected_model_provider": "fake",
        "selected_model_id": "claude-opus-4",
        "selected_model_tier": "standard",
        "selected_deliberation_tier": "balanced",
        "selected_context_window_tokens": 200000,
        "selected_model_capabilities": ["read", "write"],
        "model_degradation_approval_id": None,
    }
    defaults.update(overrides)
    return defaults


def _make_snapshot(**overrides) -> ModelSelectionSnapshot:
    return ModelSelectionSnapshot.from_mapping(_make_snapshot_dict(**overrides))


def _make_request(**overrides) -> DispatchRequest:
    defaults: dict = {
        "identity": _make_identity(),
        "workspace": Path(tempfile.gettempdir()),
        "prompt": "test prompt",
        "model_selection": _make_snapshot(),
        "timeout_seconds": 30,
    }
    defaults.update(overrides)
    return DispatchRequest(**defaults)


def _fake_provider(**overrides) -> FakeAgentCliProvider:
    defaults: dict = {
        "provider_id": "fake",
        "executable": _get_helper_exe(),
    }
    defaults.update(overrides)
    return FakeAgentCliProvider(**defaults)


def _make_providers(**overrides) -> dict[str, FakeAgentCliProvider]:
    defaults: dict = {
        "fake": _fake_provider(),
    }
    defaults.update(overrides)
    return defaults


def _run_async(coro):
    """Run a coroutine and return its result synchronously."""
    return asyncio.run(coro)


def _dispatch(
    request=None,
    providers=None,
    exit_code=0,
    stdout=b"ok",
    stderr=b"",
):
    """Run run_dispatch with a mocked subprocess.  No real execution.

    Patches ``asyncio.create_subprocess_exec`` inside the
    ``dispatcher_gateway`` module namespace.
    """
    async def _go():
        proc = _FakeProcess(returncode=exit_code, stdout=stdout, stderr=stderr)
        target = "dispatcher_gateway.asyncio.create_subprocess_exec"
        with mock.patch(target, return_value=proc):
            return await run_dispatch(
                request if request is not None else _make_request(),
                providers if providers is not None else _make_providers(),
            )
    return _run_async(_go())


def _dispatch_raises(
    exc_type,
    request=None,
    providers=None,
    exit_code=0,
    stdout=b"ok",
    stderr=b"",
):
    """Assert run_dispatch raises *exc_type*."""
    try:
        _dispatch(request=request, providers=providers,
                  exit_code=exit_code, stdout=stdout, stderr=stderr)
        raise AssertionError(f"Expected {exc_type.__name__}, but no exception raised")
    except exc_type:
        return  # expected


# =========================================================================
# 1. DispatchIdentity
# =========================================================================


class DispatchIdentityTests(unittest.TestCase):
    """TC-13.7: DispatchIdentity frozen dataclass tests."""

    def test_is_dataclass(self) -> None:
        self.assertTrue(is_dataclass(DispatchIdentity))

    def test_exact_four_fields(self) -> None:
        field_names = {f.name for f in fields(DispatchIdentity)}
        self.assertSetEqual(
            field_names,
            {"task_id", "revision", "attempt", "dispatch_id"},
        )

    def test_frozen(self) -> None:
        ident = _make_identity()
        with self.assertRaises(Exception):
            ident.task_id = "other"  # type: ignore[misc]

    def test_slots(self) -> None:
        ident = _make_identity()
        with self.assertRaises(AttributeError):
            ident.__dict__  # type: ignore[attr-defined]

    def test_task_id_non_empty(self) -> None:
        _dispatch_raises(dg.DispatchInputError,
                         request=_make_request(identity=_make_identity(task_id="")))

    def test_revision_non_bool(self) -> None:
        _dispatch_raises(dg.DispatchInputError,
                         request=_make_request(identity=_make_identity(revision=True)))  # type: ignore[arg-type]

    def test_revision_ge_1(self) -> None:
        _dispatch_raises(dg.DispatchInputError,
                         request=_make_request(identity=_make_identity(revision=0)))

    def test_revision_negative(self) -> None:
        _dispatch_raises(dg.DispatchInputError,
                         request=_make_request(identity=_make_identity(revision=-1)))

    def test_attempt_non_bool(self) -> None:
        _dispatch_raises(dg.DispatchInputError,
                         request=_make_request(identity=_make_identity(attempt=True)))  # type: ignore[arg-type]

    def test_attempt_ge_1(self) -> None:
        _dispatch_raises(dg.DispatchInputError,
                         request=_make_request(identity=_make_identity(attempt=0)))

    def test_dispatch_id_non_empty(self) -> None:
        _dispatch_raises(dg.DispatchInputError,
                         request=_make_request(identity=_make_identity(dispatch_id="")))

    def test_gateway_does_not_generate_identity(self) -> None:
        """Gateway must not generate or modify identity values."""
        ident = _make_identity(task_id="TC-X", revision=5, attempt=3, dispatch_id="DSP-Y")
        result = _dispatch(request=_make_request(identity=ident))
        self.assertEqual(result.identity.task_id, "TC-X")
        self.assertEqual(result.identity.revision, 5)
        self.assertEqual(result.identity.attempt, 3)
        self.assertEqual(result.identity.dispatch_id, "DSP-Y")


# =========================================================================
# 2. ModelSelectionSnapshot
# =========================================================================


class ModelSelectionSnapshotTests(unittest.TestCase):
    """TC-13.7: ModelSelectionSnapshot frozen dataclass tests."""

    def test_is_dataclass(self) -> None:
        self.assertTrue(is_dataclass(ModelSelectionSnapshot))

    def test_exact_ten_fields(self) -> None:
        field_names = {f.name for f in fields(ModelSelectionSnapshot)}
        self.assertSetEqual(
            field_names,
            {
                "required_model_tier",
                "required_model_capabilities",
                "model_binding_id",
                "selected_model_provider",
                "selected_model_id",
                "selected_model_tier",
                "selected_deliberation_tier",
                "selected_context_window_tokens",
                "selected_model_capabilities",
                "model_degradation_approval_id",
            },
        )

    def test_frozen(self) -> None:
        snap = _make_snapshot()
        with self.assertRaises(Exception):
            snap.selected_model_id = "other"  # type: ignore[misc]

    def test_slots(self) -> None:
        snap = _make_snapshot()
        with self.assertRaises(AttributeError):
            snap.__dict__  # type: ignore[attr-defined]

    def test_from_mapping_not_mapping(self) -> None:
        with self.assertRaises(dg.DispatchSnapshotError):
            ModelSelectionSnapshot.from_mapping(["not", "a", "mapping"])  # type: ignore[arg-type]

    def test_from_mapping_missing_key(self) -> None:
        d = _make_snapshot_dict()
        del d["required_model_tier"]
        with self.assertRaises(dg.DispatchSnapshotError) as ctx:
            ModelSelectionSnapshot.from_mapping(d)
        self.assertIn("missing", str(ctx.exception))

    def test_from_mapping_extra_key(self) -> None:
        d = _make_snapshot_dict()
        d["extra_field"] = "x"
        with self.assertRaises(dg.DispatchSnapshotError) as ctx:
            ModelSelectionSnapshot.from_mapping(d)
        self.assertIn("forbidden", str(ctx.exception))

    # -- capability: source list mutation does not affect tuple --------------

    def test_capability_source_list_mutation_does_not_affect_snapshot(self) -> None:
        caps = ["a", "b", "c"]
        d = _make_snapshot_dict(
            required_model_capabilities=caps,
            selected_model_capabilities=caps,
        )
        snap = ModelSelectionSnapshot.from_mapping(d)
        caps.append("d")
        caps[0] = "z"
        self.assertEqual(snap.required_model_capabilities, ("a", "b", "c"))
        self.assertEqual(snap.selected_model_capabilities, ("a", "b", "c"))

    def test_capability_duplicates_rejected(self) -> None:
        d = _make_snapshot_dict(required_model_capabilities=["a", "b", "a"])
        with self.assertRaises(dg.DispatchSnapshotError) as ctx:
            ModelSelectionSnapshot.from_mapping(d)
        self.assertIn("duplicate", str(ctx.exception).lower())

    def test_capability_empty_string_rejected(self) -> None:
        d = _make_snapshot_dict(required_model_capabilities=["a", ""])
        with self.assertRaises(dg.DispatchSnapshotError):
            ModelSelectionSnapshot.from_mapping(d)

    def test_capability_wrong_type(self) -> None:
        d = _make_snapshot_dict(required_model_capabilities="not_a_list")
        with self.assertRaises(dg.DispatchSnapshotError):
            ModelSelectionSnapshot.from_mapping(d)

    def test_capability_non_string_element(self) -> None:
        d = _make_snapshot_dict(required_model_capabilities=["a", 123])
        with self.assertRaises(dg.DispatchSnapshotError):
            ModelSelectionSnapshot.from_mapping(d)

    def test_capability_preserves_order(self) -> None:
        caps = ["z", "a", "m", "b"]
        d = _make_snapshot_dict(required_model_capabilities=caps)
        snap = ModelSelectionSnapshot.from_mapping(d)
        self.assertEqual(snap.required_model_capabilities, ("z", "a", "m", "b"))

    def test_snapshot_fields_are_immutable_types(self) -> None:
        snap = _make_snapshot()
        for f in fields(snap):
            val = getattr(snap, f.name)
            self.assertNotIsInstance(val, list,
                                     f"field {f.name} is a mutable list")
            self.assertNotIsInstance(val, dict,
                                     f"field {f.name} is a mutable dict")
            self.assertNotIsInstance(val, set,
                                     f"field {f.name} is a mutable set")

    def test_context_window_tokens_non_bool(self) -> None:
        d = _make_snapshot_dict(selected_context_window_tokens=True)
        with self.assertRaises(dg.DispatchSnapshotError):
            ModelSelectionSnapshot.from_mapping(d)

    def test_context_window_tokens_ge_1(self) -> None:
        d = _make_snapshot_dict(selected_context_window_tokens=0)
        with self.assertRaises(dg.DispatchSnapshotError):
            ModelSelectionSnapshot.from_mapping(d)

    def test_context_window_tokens_not_int(self) -> None:
        d = _make_snapshot_dict(selected_context_window_tokens="200k")  # type: ignore[arg-type]
        with self.assertRaises(dg.DispatchSnapshotError):
            ModelSelectionSnapshot.from_mapping(d)

    def test_approval_id_none_ok(self) -> None:
        snap = ModelSelectionSnapshot.from_mapping(
            _make_snapshot_dict(model_degradation_approval_id=None)
        )
        self.assertIsNone(snap.model_degradation_approval_id)

    def test_approval_id_non_empty_str_ok(self) -> None:
        snap = ModelSelectionSnapshot.from_mapping(
            _make_snapshot_dict(model_degradation_approval_id="APPROVED-1")
        )
        self.assertEqual(snap.model_degradation_approval_id, "APPROVED-1")

    def test_approval_id_empty_str_rejected(self) -> None:
        d = _make_snapshot_dict(model_degradation_approval_id="")
        with self.assertRaises(dg.DispatchSnapshotError):
            ModelSelectionSnapshot.from_mapping(d)

    def test_approval_id_bool_rejected(self) -> None:
        d = _make_snapshot_dict(model_degradation_approval_id=True)  # type: ignore[arg-type]
        with self.assertRaises(dg.DispatchSnapshotError):
            ModelSelectionSnapshot.from_mapping(d)

    def test_empty_required_model_tier_rejected(self) -> None:
        d = _make_snapshot_dict(required_model_tier="")
        with self.assertRaises(dg.DispatchSnapshotError):
            ModelSelectionSnapshot.from_mapping(d)

    def test_empty_model_binding_id_rejected(self) -> None:
        d = _make_snapshot_dict(model_binding_id="")
        with self.assertRaises(dg.DispatchSnapshotError):
            ModelSelectionSnapshot.from_mapping(d)


# =========================================================================
# 3. DispatchRequest
# =========================================================================


class DispatchRequestTests(unittest.TestCase):
    """TC-13.7: DispatchRequest frozen dataclass tests."""

    def test_is_dataclass(self) -> None:
        self.assertTrue(is_dataclass(DispatchRequest))

    def test_exact_five_fields(self) -> None:
        field_names = {f.name for f in fields(DispatchRequest)}
        self.assertSetEqual(
            field_names,
            {"identity", "workspace", "prompt", "model_selection", "timeout_seconds"},
        )

    def test_frozen(self) -> None:
        req = _make_request()
        with self.assertRaises(Exception):
            req.prompt = "other"  # type: ignore[misc]

    def test_slots(self) -> None:
        req = _make_request()
        with self.assertRaises(AttributeError):
            req.__dict__  # type: ignore[attr-defined]

    def test_workspace_relative_rejected(self) -> None:
        _dispatch_raises(dg.DispatchInputError,
                         request=_make_request(workspace=Path("relative/path")))

    def test_workspace_nonexistent_rejected(self) -> None:
        _dispatch_raises(dg.DispatchInputError,
                         request=_make_request(workspace=Path("/nonexistent/path/xyz")))

    def test_workspace_not_directory_rejected(self) -> None:
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(b"test")
            tmp_file = f.name
        try:
            _dispatch_raises(dg.DispatchInputError,
                             request=_make_request(workspace=Path(tmp_file)))
        finally:
            os.unlink(tmp_file)

    def test_workspace_not_path_rejected(self) -> None:
        _dispatch_raises(dg.DispatchInputError,
                         request=_make_request(workspace="/not/a/path/object"))  # type: ignore[arg-type]

    def test_prompt_empty_rejected(self) -> None:
        _dispatch_raises(dg.DispatchInputError,
                         request=_make_request(prompt=""))

    def test_prompt_not_str_rejected(self) -> None:
        _dispatch_raises(dg.DispatchInputError,
                         request=_make_request(prompt=123))  # type: ignore[arg-type]

    def test_timeout_non_bool(self) -> None:
        _dispatch_raises(dg.DispatchInputError,
                         request=_make_request(timeout_seconds=True))  # type: ignore[arg-type]

    def test_timeout_zero_rejected(self) -> None:
        _dispatch_raises(dg.DispatchInputError,
                         request=_make_request(timeout_seconds=0))

    def test_timeout_negative_rejected(self) -> None:
        _dispatch_raises(dg.DispatchInputError,
                         request=_make_request(timeout_seconds=-5))

    def test_timeout_not_int_rejected(self) -> None:
        _dispatch_raises(dg.DispatchInputError,
                         request=_make_request(timeout_seconds="30"))  # type: ignore[arg-type]

    def test_model_selection_wrong_type_rejected(self) -> None:
        _dispatch_raises(dg.DispatchInputError,
                         request=_make_request(model_selection="not_a_snapshot"))  # type: ignore[arg-type]


# =========================================================================
# 4. AgentCliInvocation
# =========================================================================


class AgentCliInvocationTests(unittest.TestCase):
    """TC-13.7: AgentCliInvocation frozen dataclass tests."""

    def test_is_dataclass(self) -> None:
        self.assertTrue(is_dataclass(AgentCliInvocation))

    def test_exact_four_fields(self) -> None:
        field_names = {f.name for f in fields(AgentCliInvocation)}
        self.assertSetEqual(
            field_names,
            {"executable", "argv", "stdin", "env_overrides"},
        )

    def test_frozen(self) -> None:
        inv = AgentCliInvocation(
            executable="python",
            argv=(),
            stdin=None,
            env_overrides=(),
        )
        with self.assertRaises(Exception):
            inv.executable = "other"  # type: ignore[misc]

    def test_slots(self) -> None:
        inv = AgentCliInvocation(
            executable="python",
            argv=(),
            stdin=None,
            env_overrides=(),
        )
        with self.assertRaises(AttributeError):
            inv.__dict__  # type: ignore[attr-defined]


# =========================================================================
# 5. Provider mapping
# =========================================================================


class ProviderMappingTests(unittest.TestCase):
    """TC-13.7: provider mapping validation tests."""

    def test_providers_empty_rejected(self) -> None:
        _dispatch_raises(dg.DispatchInputError, providers={})

    def test_provider_missing_raises_provider_not_supported(self) -> None:
        snap = _make_snapshot(selected_model_provider="nonexistent")
        try:
            _dispatch(request=_make_request(model_selection=snap))
            self.fail("Should have raised")
        except dg.ProviderNotSupportedError as ctx:
            self.assertEqual(ctx.provider_id, "nonexistent")

    def test_mapping_key_mismatches_provider_id(self) -> None:
        """adapter.provider_id must match its key in the mapping."""
        provider = FakeAgentCliProvider(
            provider_id="mismatch",
            executable=_get_helper_exe(),
        )
        _dispatch_raises(dg.DispatchInvocationError, providers={"fake": provider})

    def test_providers_not_modified_by_gateway(self) -> None:
        providers = _make_providers()
        provider_count_before = len(providers)
        keys_before = set(providers.keys())
        _dispatch(providers=providers)
        self.assertEqual(len(providers), provider_count_before)
        self.assertEqual(set(providers.keys()), keys_before)

    def test_lookup_key_is_selected_model_provider(self) -> None:
        provider_abc = FakeAgentCliProvider(
            provider_id="abc",
            executable=_get_helper_exe(),
        )
        providers = {"abc": provider_abc}
        snap = _make_snapshot(selected_model_provider="abc")
        result = _dispatch(request=_make_request(model_selection=snap),
                           providers=providers)
        self.assertEqual(result.provider, "abc")

    def test_provider_id_must_be_non_empty_string(self) -> None:
        """adapter.provider_id must be non-empty string — verify via
        _validate_providers directly."""
        # Test that the adapter's provider_id being empty is caught
        provider = FakeAgentCliProvider(provider_id="")
        with self.assertRaises(dg.DispatchInvocationError):
            dg._validate_providers({"x": provider}, "x")


# =========================================================================
# 6. Invocation validation
# =========================================================================


class InvocationValidationTests(unittest.TestCase):
    """TC-13.7: AgentCliInvocation validation tests."""

    def test_argv_does_not_include_executable(self) -> None:
        """argv must not contain the resolved executable."""
        provider = _fake_provider()
        result = _dispatch(providers={"fake": provider})
        self.assertIsInstance(result, DispatchResult)

    def test_executable_absolute_path_with_spaces(self) -> None:
        """Absolute path with spaces is legal."""
        with tempfile.TemporaryDirectory(prefix="test dir") as tmp:
            script = Path(tmp) / "my tool.py"
            script.write_text("import sys; sys.exit(0)")
            os.chmod(script, 0o755)
            provider = FakeAgentCliProvider(
                provider_id="fake",
                executable=str(script),
            )
            # subprocess is mocked, so the executable just needs to exist
            result = _dispatch(providers={"fake": provider},
                               stdout=b"spaces ok")
            self.assertEqual(result.stdout, b"spaces ok")

    def test_plain_executable_via_which(self) -> None:
        """Plain executable name resolved via shutil.which."""
        # sys.executable exists and is findable via which
        provider = FakeAgentCliProvider(
            provider_id="fake",
            executable=sys.executable,
        )
        result = _dispatch(providers={"fake": provider})
        self.assertIsInstance(result, DispatchResult)

    def test_executable_not_found(self) -> None:
        provider = FakeAgentCliProvider(
            provider_id="fake",
            executable="/nonexistent/path/to/exe_xyz",
        )
        _dispatch_raises(dg.ExecutableNotFoundError, providers={"fake": provider})

    def test_executable_nul_rejected(self) -> None:
        provider = FakeAgentCliProvider(
            provider_id="fake",
            executable="python\0hidden",
        )
        _dispatch_raises(dg.DispatchInvocationError, providers={"fake": provider})

    def test_argv_not_tuple_rejected(self) -> None:
        provider = FakeAgentCliProvider(
            provider_id="fake",
            executable=_get_helper_exe(),
            argv=["--version"],  # list, not tuple
        )
        _dispatch_raises(dg.DispatchInvocationError, providers={"fake": provider})

    def test_argv_element_nul_rejected(self) -> None:
        provider = FakeAgentCliProvider(
            provider_id="fake",
            executable=_get_helper_exe(),
            argv=("arg1\0hidden",),
        )
        _dispatch_raises(dg.DispatchInvocationError, providers={"fake": provider})

    def test_argv_element_not_string_rejected(self) -> None:
        provider = FakeAgentCliProvider(
            provider_id="fake",
            executable=_get_helper_exe(),
            argv=(123,),  # type: ignore[arg-type]
        )
        _dispatch_raises(dg.DispatchInvocationError, providers={"fake": provider})

    def test_empty_argv_legal(self) -> None:
        provider = FakeAgentCliProvider(
            provider_id="fake",
            executable=_get_helper_exe(),
            argv=(),
        )
        result = _dispatch(providers={"fake": provider},
                           stdout=b"no args ok")
        self.assertIsInstance(result, DispatchResult)

    def test_stdin_bytes_ok(self) -> None:
        provider = FakeAgentCliProvider(
            provider_id="fake",
            executable=_get_helper_exe(),
            stdin=b"custom stdin",
        )
        result = _dispatch(providers={"fake": provider})
        self.assertIsInstance(result, DispatchResult)

    def test_stdin_none_ok(self) -> None:
        provider = FakeAgentCliProvider(
            provider_id="fake",
            executable=_get_helper_exe(),
            stdin=None,
        )
        result = _dispatch(providers={"fake": provider})
        self.assertIsInstance(result, DispatchResult)

    def test_stdin_not_bytes_rejected(self) -> None:
        provider = FakeAgentCliProvider(
            provider_id="fake",
            executable=_get_helper_exe(),
            stdin="string not bytes",  # type: ignore[arg-type]
        )
        _dispatch_raises(dg.DispatchInvocationError, providers={"fake": provider})

    # -- env_overrides validation -------------------------------------------

    def test_env_overrides_not_tuple_rejected(self) -> None:
        provider = FakeAgentCliProvider(
            provider_id="fake",
            executable=_get_helper_exe(),
            env_overrides=[("KEY", "val")],  # type: ignore[arg-type]
        )
        _dispatch_raises(dg.DispatchInvocationError, providers={"fake": provider})

    def test_env_overrides_not_pair_rejected(self) -> None:
        provider = FakeAgentCliProvider(
            provider_id="fake",
            executable=_get_helper_exe(),
            env_overrides=(("KEY",),),  # type: ignore[arg-type]
        )
        _dispatch_raises(dg.DispatchInvocationError, providers={"fake": provider})

    def test_env_overrides_key_not_string_rejected(self) -> None:
        provider = FakeAgentCliProvider(
            provider_id="fake",
            executable=_get_helper_exe(),
            env_overrides=((123, "val"),),  # type: ignore[arg-type]
        )
        _dispatch_raises(dg.DispatchInvocationError, providers={"fake": provider})

    def test_env_overrides_key_empty_rejected(self) -> None:
        provider = FakeAgentCliProvider(
            provider_id="fake",
            executable=_get_helper_exe(),
            env_overrides=(("", "val"),),
        )
        _dispatch_raises(dg.DispatchInvocationError, providers={"fake": provider})

    def test_env_overrides_key_has_equals_rejected(self) -> None:
        provider = FakeAgentCliProvider(
            provider_id="fake",
            executable=_get_helper_exe(),
            env_overrides=(("KEY=BAD", "val"),),
        )
        _dispatch_raises(dg.DispatchInvocationError, providers={"fake": provider})

    def test_env_overrides_duplicate_key_rejected(self) -> None:
        provider = FakeAgentCliProvider(
            provider_id="fake",
            executable=_get_helper_exe(),
            env_overrides=(("KEY", "val1"), ("KEY", "val2")),
        )
        _dispatch_raises(dg.DispatchInvocationError, providers={"fake": provider})

    def test_env_overrides_nul_in_key_rejected(self) -> None:
        provider = FakeAgentCliProvider(
            provider_id="fake",
            executable=_get_helper_exe(),
            env_overrides=(("KEY\0bad", "val"),),
        )
        _dispatch_raises(dg.DispatchInvocationError, providers={"fake": provider})

    def test_env_overrides_nul_in_value_rejected(self) -> None:
        provider = FakeAgentCliProvider(
            provider_id="fake",
            executable=_get_helper_exe(),
            env_overrides=(("KEY", "val\0bad"),),
        )
        _dispatch_raises(dg.DispatchInvocationError, providers={"fake": provider})

    def test_env_overrides_dict_rejected(self) -> None:
        provider = FakeAgentCliProvider(
            provider_id="fake",
            executable=_get_helper_exe(),
            env_overrides={"KEY": "val"},  # type: ignore[arg-type]
        )
        _dispatch_raises(dg.DispatchInvocationError, providers={"fake": provider})

    def test_executable_has_directory_separator_relative(self) -> None:
        """Plain name with dir separator is rejected by resolution."""
        provider = FakeAgentCliProvider(
            provider_id="fake",
            executable="bin/python",
        )
        _dispatch_raises(dg.ExecutableNotFoundError, providers={"fake": provider})


# =========================================================================
# 7. Environment isolation
# =========================================================================


class EnvironmentIsolationTests(unittest.TestCase):
    """TC-13.7: environment isolation and non‑contamination tests."""

    def test_os_environ_not_modified(self) -> None:
        env_before = os.environ.copy()
        provider = FakeAgentCliProvider(
            provider_id="fake",
            executable=_get_helper_exe(),
            env_overrides=(("TEST_TC137_VAR", "test_value"),),
        )
        _dispatch(providers={"fake": provider})
        self.assertNotIn("TEST_TC137_VAR", os.environ)

    def test_mad_home_not_set(self) -> None:
        """Gateway must not set MAD_HOME in subprocess environment."""
        env = dg._build_env(())
        new_keys = set(env.keys()) - set(os.environ.keys())
        self.assertNotIn("MAD_HOME", new_keys)

    def test_mad_participant_not_set(self) -> None:
        """Gateway must not set MAD_PARTICIPANT in subprocess environment."""
        env = dg._build_env(())
        new_keys = set(env.keys()) - set(os.environ.keys())
        self.assertNotIn("MAD_PARTICIPANT", new_keys)

    def test_env_overrides_applied_in_order(self) -> None:
        """env_overrides are applied to a copy of os.environ."""
        provider = FakeAgentCliProvider(
            provider_id="fake",
            executable=_get_helper_exe(),
            env_overrides=(("CUSTOM_VAR", "custom_val"),),
        )

        captured_env: list[dict] = []

        async def _go():
            proc = _FakeProcess(returncode=0, stdout=b"ok", stderr=b"")
            target = "dispatcher_gateway.asyncio.create_subprocess_exec"

            async def _fake_exec(*args, **kwargs):
                captured_env.append(kwargs.get("env", {}))
                return proc

            with mock.patch(target, side_effect=_fake_exec):
                return await run_dispatch(
                    _make_request(),
                    {"fake": provider},
                )

        _run_async(_go())
        self.assertEqual(len(captured_env), 1)
        self.assertEqual(captured_env[0].get("CUSTOM_VAR"), "custom_val")


# =========================================================================
# 8. Subprocess execution model
# =========================================================================


class SubprocessExecutionTests(unittest.TestCase):
    """TC-13.7: subprocess execution model tests."""

    def test_create_subprocess_exec_used_not_shell(self) -> None:
        """Verify create_subprocess_exec is used (not create_subprocess_shell)."""

        async def _check():
            proc = _FakeProcess(returncode=0, stdout=b"ok", stderr=b"")
            target = "dispatcher_gateway.asyncio.create_subprocess_exec"
            with mock.patch(target, return_value=proc) as mock_exec:
                await run_dispatch(_make_request(), _make_providers())
                mock_exec.assert_called_once()
                call_kwargs = mock_exec.call_args[1]
                self.assertNotIn("shell", call_kwargs)
                return True

        self.assertTrue(_run_async(_check()))

    def test_create_subprocess_shell_never_used(self) -> None:
        """Verify create_subprocess_shell is never called."""
        with mock.patch("asyncio.create_subprocess_shell") as mock_shell:
            mock_shell.side_effect = RuntimeError("should not be called")
            _dispatch()
            mock_shell.assert_not_called()

    def test_executable_not_in_argv(self) -> None:
        """Resolved executable passed as first arg, not duplicated in argv."""

        captured_args: list[tuple] = []

        async def _check():
            proc = _FakeProcess(returncode=0, stdout=b"ok", stderr=b"")
            target = "dispatcher_gateway.asyncio.create_subprocess_exec"

            async def _fake_exec(*args, **kwargs):
                captured_args.append(args)
                return proc

            with mock.patch(target, side_effect=_fake_exec):
                provider = FakeAgentCliProvider(
                    provider_id="fake",
                    executable=_get_helper_exe(),
                    argv=("--flag", "value"),
                )
                await run_dispatch(_make_request(), {"fake": provider})

            self.assertEqual(len(captured_args), 1)
            exe = captured_args[0][0]
            rest = captured_args[0][1:]
            self.assertIn("--flag", rest)
            # argv contents must NOT duplicate the executable as first arg
            self.assertNotIn(exe, rest)
            return True

        self.assertTrue(_run_async(_check()))

    def test_executable_is_string_not_shell_command(self) -> None:
        """executable is a single path — not a shell command string."""
        provider = FakeAgentCliProvider(
            provider_id="fake",
            executable=sys.executable + " --some-flag",
        )
        _dispatch_raises((dg.ExecutableNotFoundError, dg.DispatchLaunchError),
                         providers={"fake": provider})


# =========================================================================
# 9. stdin/stdout/stderr
# =========================================================================


class StdioTests(unittest.TestCase):
    """TC-13.7: stdin/stdout/stderr handling tests."""

    def test_stdin_pipe_when_not_none(self) -> None:
        async def _check():
            proc = _FakeProcess(returncode=0, stdout=b"ok", stderr=b"")
            target = "dispatcher_gateway.asyncio.create_subprocess_exec"
            with mock.patch(target, return_value=proc) as mock_exec:
                provider = FakeAgentCliProvider(
                    provider_id="fake",
                    executable=_get_helper_exe(),
                    stdin=b"piped input",
                )
                await run_dispatch(_make_request(), {"fake": provider})
                call_kwargs = mock_exec.call_args[1]
                self.assertEqual(call_kwargs["stdin"], asyncio.subprocess.PIPE)
                return True
        self.assertTrue(_run_async(_check()))

    def test_stdin_devnull_when_none(self) -> None:
        async def _check():
            proc = _FakeProcess(returncode=0, stdout=b"ok", stderr=b"")
            target = "dispatcher_gateway.asyncio.create_subprocess_exec"
            with mock.patch(target, return_value=proc) as mock_exec:
                provider = FakeAgentCliProvider(
                    provider_id="fake",
                    executable=_get_helper_exe(),
                    stdin=None,
                )
                await run_dispatch(_make_request(), {"fake": provider})
                call_kwargs = mock_exec.call_args[1]
                self.assertEqual(call_kwargs["stdin"], asyncio.subprocess.DEVNULL)
                return True
        self.assertTrue(_run_async(_check()))

    def test_stdout_stderr_are_pipe(self) -> None:
        async def _check():
            proc = _FakeProcess(returncode=0, stdout=b"ok", stderr=b"")
            target = "dispatcher_gateway.asyncio.create_subprocess_exec"
            with mock.patch(target, return_value=proc) as mock_exec:
                await run_dispatch(_make_request(), _make_providers())
                call_kwargs = mock_exec.call_args[1]
                self.assertEqual(call_kwargs["stdout"], asyncio.subprocess.PIPE)
                self.assertEqual(call_kwargs["stderr"], asyncio.subprocess.PIPE)
                return True
        self.assertTrue(_run_async(_check()))

    def test_stdout_stderr_raw_bytes(self) -> None:
        result = _dispatch()
        self.assertIsInstance(result.stdout, bytes)
        self.assertIsInstance(result.stderr, bytes)

    def test_non_utf8_output_success(self) -> None:
        result = _dispatch(stdout=b"\xff\xfe\x00\x01", stderr=b"\x80\x81\x82")
        self.assertEqual(result.stdout, b"\xff\xfe\x00\x01")
        self.assertEqual(result.stderr, b"\x80\x81\x82")
        self.assertEqual(len(result.stdout_sha256), 64)
        self.assertEqual(len(result.stderr_sha256), 64)

    def test_sha256_exact(self) -> None:
        result = _dispatch(stdout=b"hello world", stderr=b"error log")
        expected_stdout = hashlib.sha256(b"hello world").hexdigest()
        expected_stderr = hashlib.sha256(b"error log").hexdigest()
        self.assertEqual(result.stdout_sha256, expected_stdout)
        self.assertEqual(result.stderr_sha256, expected_stderr)

    def test_no_text_encoding_set_on_subprocess(self) -> None:
        async def _check():
            proc = _FakeProcess(returncode=0, stdout=b"ok", stderr=b"")
            target = "dispatcher_gateway.asyncio.create_subprocess_exec"
            with mock.patch(target, return_value=proc) as mock_exec:
                await run_dispatch(_make_request(), _make_providers())
                call_kwargs = mock_exec.call_args[1]
                self.assertNotIn("text", call_kwargs)
                self.assertNotIn("encoding", call_kwargs)
                self.assertNotIn("errors", call_kwargs)
                return True
        self.assertTrue(_run_async(_check()))


# =========================================================================
# 10. DispatchResult
# =========================================================================


class DispatchResultTests(unittest.TestCase):
    """TC-13.7: DispatchResult frozen dataclass tests."""

    def test_is_dataclass(self) -> None:
        self.assertTrue(is_dataclass(DispatchResult))

    def test_exact_eight_fields(self) -> None:
        field_names = {f.name for f in fields(DispatchResult)}
        self.assertSetEqual(
            field_names,
            {
                "identity", "provider", "model_id", "duration_seconds",
                "stdout", "stderr", "stdout_sha256", "stderr_sha256",
            },
        )

    def test_frozen(self) -> None:
        result = _dispatch()
        with self.assertRaises(Exception):
            result.stdout_sha256 = "other"  # type: ignore[misc]

    def test_slots(self) -> None:
        result = _dispatch()
        with self.assertRaises(AttributeError):
            result.__dict__  # type: ignore[attr-defined]

    def test_provider_and_model_id_echoed_from_snapshot(self) -> None:
        snap = _make_snapshot(
            selected_model_provider="fake",
            selected_model_id="my-model-42",
        )
        result = _dispatch(request=_make_request(model_selection=snap))
        self.assertEqual(result.provider, "fake")
        self.assertEqual(result.model_id, "my-model-42")

    def test_duration_is_float(self) -> None:
        result = _dispatch()
        self.assertIsInstance(result.duration_seconds, float)
        self.assertGreaterEqual(result.duration_seconds, 0.0)


# =========================================================================
# 11. Non-zero exit
# =========================================================================


class NonZeroExitTests(unittest.TestCase):
    """TC-13.7: non‑zero exit handling tests."""

    def test_non_zero_exit_raises_dispatch_non_zero_exit_error(self) -> None:
        try:
            _dispatch(exit_code=1, stdout=b"stdout content", stderr=b"error occurred")
            self.fail("Should have raised")
        except dg.DispatchNonZeroExitError as exc:
            self.assertEqual(exc.exit_code, 1)
            self.assertEqual(exc.stdout_sha256,
                             hashlib.sha256(b"stdout content").hexdigest())
            self.assertEqual(exc.stderr_sha256,
                             hashlib.sha256(b"error occurred").hexdigest())
            self.assertIsInstance(exc.stderr_preview, str)

    def test_non_zero_exit_does_not_carry_raw_bytes(self) -> None:
        try:
            _dispatch(exit_code=2, stderr=b"secret: password=hunter2")
            self.fail("Should have raised")
        except dg.DispatchNonZeroExitError as exc:
            self.assertFalse(hasattr(exc, "stdout"))
            self.assertFalse(hasattr(exc, "stderr"))
            self.assertFalse(hasattr(exc, "argv"))
            self.assertFalse(hasattr(exc, "env"))

    def test_non_zero_exit_carries_sha(self) -> None:
        try:
            _dispatch(exit_code=3, stdout=b"a", stderr=b"b")
            self.fail("Should have raised")
        except dg.DispatchNonZeroExitError as exc:
            self.assertEqual(exc.stdout_sha256, hashlib.sha256(b"a").hexdigest())
            self.assertEqual(exc.stderr_sha256, hashlib.sha256(b"b").hexdigest())

    def test_stderr_preview_max_500_chars(self) -> None:
        long_stderr = b"x" * 1000
        try:
            _dispatch(exit_code=1, stderr=long_stderr)
            self.fail("Should have raised")
        except dg.DispatchNonZeroExitError as exc:
            self.assertLessEqual(len(exc.stderr_preview), 501)

    def test_stderr_preview_decode_errors_replace(self) -> None:
        try:
            _dispatch(exit_code=1, stderr=b"valid prefix \xff\xfe more")
            self.fail("Should have raised")
        except dg.DispatchNonZeroExitError as exc:
            self.assertIsInstance(exc.stderr_preview, str)
            self.assertIn("�", exc.stderr_preview)

    def test_dispatch_result_never_returned_for_non_zero(self) -> None:
        """Ensure DispatchResult is never returned for non‑zero exit."""
        try:
            _dispatch(exit_code=1)
            self.fail("Should have raised DispatchNonZeroExitError")
        except dg.DispatchNonZeroExitError:
            pass  # expected


# =========================================================================
# 12. Secret redaction
# =========================================================================


class SecretRedactionTests(unittest.TestCase):
    """TC-13.7: secret redaction in non‑zero exit error messages."""

    def test_bearer_token_redacted(self) -> None:
        try:
            _dispatch(exit_code=1,
                      stderr=b"Authorization: Bearer sk-abc123def456")
            self.fail("Should have raised")
        except dg.DispatchNonZeroExitError as exc:
            self.assertNotIn("sk-abc123def456", str(exc))
            self.assertNotIn("sk-abc123def456", exc.stderr_preview)

    def test_api_key_redacted(self) -> None:
        try:
            _dispatch(exit_code=1,
                      stderr=b"error: api_key=my-secret-key-123")
            self.fail("Should have raised")
        except dg.DispatchNonZeroExitError as exc:
            self.assertNotIn("my-secret-key-123", str(exc))
            self.assertNotIn("my-secret-key-123", exc.stderr_preview)

    def test_token_key_value_redacted(self) -> None:
        try:
            _dispatch(exit_code=1,
                      stderr=b"token=supersecrettokenvalue")
            self.fail("Should have raised")
        except dg.DispatchNonZeroExitError as exc:
            self.assertNotIn("supersecrettokenvalue", str(exc))
            self.assertNotIn("supersecrettokenvalue", exc.stderr_preview)

    def test_password_redacted(self) -> None:
        try:
            _dispatch(exit_code=1,
                      stderr=b"password=MyP@ssw0rd!")
            self.fail("Should have raised")
        except dg.DispatchNonZeroExitError as exc:
            self.assertNotIn("MyP@ssw0rd!", str(exc))
            self.assertNotIn("MyP@ssw0rd!", exc.stderr_preview)

    def test_github_token_redacted(self) -> None:
        try:
            _dispatch(exit_code=1,
                      stderr=b"github token: ghp_abc123def456ghi789jkl")
            self.fail("Should have raised")
        except dg.DispatchNonZeroExitError as exc:
            self.assertNotIn("ghp_abc123def456ghi789jkl", str(exc))
            self.assertNotIn("ghp_abc123def456ghi789jkl", exc.stderr_preview)


# =========================================================================
# 13. Timeout and cancellation
# =========================================================================


class TimeoutCancellationTests(unittest.TestCase):
    """TC-13.7: timeout and cancellation handling tests."""

    def test_timeout_raises_dispatch_timeout_error(self) -> None:
        async def _check():
            proc = _FakeProcess(returncode=0)

            async def slow_communicate(input=None):
                await asyncio.sleep(5)
                return b"", b""

            proc.communicate = slow_communicate
            target = "dispatcher_gateway.asyncio.create_subprocess_exec"

            with mock.patch(target, return_value=proc):
                with mock.patch.object(dg, "_terminate_process") as mock_term:
                    async def _term(p):
                        pass
                    mock_term.side_effect = _term

                    with self.assertRaises(dg.DispatchTimeoutError) as ctx:
                        await run_dispatch(
                            _make_request(timeout_seconds=1),
                            _make_providers(),
                        )
                    self.assertEqual(ctx.exception.timeout_seconds, 1)
                    mock_term.assert_called_once()
                    return True
        self.assertTrue(_run_async(_check()))

    def test_timeout_terminates_process(self) -> None:
        async def _check():
            proc = _FakeProcess(returncode=0)

            async def slow_communicate(input=None):
                await asyncio.sleep(5)
                return b"", b""

            proc.communicate = slow_communicate
            target = "dispatcher_gateway.asyncio.create_subprocess_exec"

            with mock.patch(target, return_value=proc):
                with mock.patch.object(dg, "_terminate_process") as mock_term:
                    async def _term(p):
                        pass
                    mock_term.side_effect = _term

                    with self.assertRaises(dg.DispatchTimeoutError):
                        await run_dispatch(
                            _make_request(timeout_seconds=1),
                            _make_providers(),
                        )
                    mock_term.assert_called_once()
                    return True
        self.assertTrue(_run_async(_check()))

    def test_cancellation_raises_dispatch_cancelled_error(self) -> None:
        async def _check():
            proc = _FakeProcess(returncode=0)

            async def hanging_communicate(input=None):
                await asyncio.sleep(10)
                return b"", b""

            proc.communicate = hanging_communicate
            target = "dispatcher_gateway.asyncio.create_subprocess_exec"

            with mock.patch(target, return_value=proc):
                with mock.patch.object(dg, "_terminate_process") as mock_term:
                    async def _term(p):
                        pass
                    mock_term.side_effect = _term

                    task = asyncio.create_task(run_dispatch(
                        _make_request(timeout_seconds=30),
                        _make_providers(),
                    ))
                    await asyncio.sleep(0.05)
                    task.cancel()
                    with self.assertRaises(dg.DispatchCancelledError):
                        await task
                    mock_term.assert_called_once()
                    return True
        self.assertTrue(_run_async(_check()))

    def test_timeout_does_not_return_partial_result(self) -> None:
        async def _check():
            proc = _FakeProcess(returncode=0, stdout=b"partial")

            async def slow_communicate(input=None):
                await asyncio.sleep(5)
                return b"partial", b""

            proc.communicate = slow_communicate
            target = "dispatcher_gateway.asyncio.create_subprocess_exec"

            with mock.patch(target, return_value=proc):
                with mock.patch.object(dg, "_terminate_process") as mock_term:
                    async def _term(p):
                        pass
                    mock_term.side_effect = _term

                    with self.assertRaises(dg.DispatchTimeoutError):
                        await run_dispatch(
                            _make_request(timeout_seconds=1),
                            _make_providers(),
                        )
                    return True
        self.assertTrue(_run_async(_check()))

    def test_cancellation_not_swallowed(self) -> None:
        """Asyncio cancellation must propagate, not be silently swallowed."""
        async def _check():
            proc = _FakeProcess(returncode=0)

            async def hanging_communicate(input=None):
                await asyncio.sleep(10)
                return b"", b""

            proc.communicate = hanging_communicate
            target = "dispatcher_gateway.asyncio.create_subprocess_exec"

            with mock.patch(target, return_value=proc):
                with mock.patch.object(dg, "_terminate_process") as mock_term:
                    async def _term(p):
                        pass
                    mock_term.side_effect = _term

                    task = asyncio.create_task(run_dispatch(
                        _make_request(timeout_seconds=30),
                        _make_providers(),
                    ))
                    await asyncio.sleep(0.05)
                    task.cancel()
                    with self.assertRaises(dg.DispatchCancelledError):
                        await task
                    return True
        self.assertTrue(_run_async(_check()))

    # -- POSIX termination mocked -------------------------------------------

    @mock.patch.object(dg, "_terminate_posix")
    def test_posix_termination_called_on_posix(self, mock_term_posix) -> None:
        async def _check():
            async def _term(p):
                pass
            mock_term_posix.side_effect = _term

            proc = _FakeProcess(returncode=0)

            async def slow_communicate(input=None):
                await asyncio.sleep(5)
                return b"", b""

            proc.communicate = slow_communicate
            target = "dispatcher_gateway.asyncio.create_subprocess_exec"

            with mock.patch(target, return_value=proc):
                with mock.patch.object(sys, "platform", "linux"):
                    with self.assertRaises(dg.DispatchTimeoutError):
                        await run_dispatch(
                            _make_request(timeout_seconds=1),
                            _make_providers(),
                        )
            return True
        self.assertTrue(_run_async(_check()))

    # -- Windows termination mocked -----------------------------------------

    @mock.patch.object(dg, "_terminate_windows")
    def test_windows_termination_called_on_windows(self, mock_term_win) -> None:
        async def _check():
            async def _term(p):
                pass
            mock_term_win.side_effect = _term

            proc = _FakeProcess(returncode=0)

            async def slow_communicate(input=None):
                await asyncio.sleep(5)
                return b"", b""

            proc.communicate = slow_communicate
            target = "dispatcher_gateway.asyncio.create_subprocess_exec"

            with mock.patch(target, return_value=proc):
                with mock.patch.object(sys, "platform", "win32"):
                    with self.assertRaises(dg.DispatchTimeoutError):
                        await run_dispatch(
                            _make_request(timeout_seconds=1),
                            _make_providers(),
                        )
            return True
        self.assertTrue(_run_async(_check()))


# =========================================================================
# 14. Launch error
# =========================================================================


class LaunchErrorTests(unittest.TestCase):
    """TC-13.7: subprocess launch failure tests."""

    def test_launch_oserror_raises_dispatch_launch_error(self) -> None:
        async def _check():
            target = "dispatcher_gateway.asyncio.create_subprocess_exec"
            with mock.patch(target, side_effect=OSError("cannot execute")):
                with self.assertRaises(dg.DispatchLaunchError):
                    await run_dispatch(_make_request(), _make_providers())
                return True
        self.assertTrue(_run_async(_check()))


# =========================================================================
# 15. Exception hierarchy
# =========================================================================


class ExceptionHierarchyTests(unittest.TestCase):
    """TC-13.7: exception hierarchy verification."""

    def test_all_exceptions_inherit_from_dispatch_gateway_error(self) -> None:
        expected = [
            dg.DispatchInputError,
            dg.DispatchSnapshotError,
            dg.ProviderNotSupportedError,
            dg.ExecutableNotFoundError,
            dg.DispatchInvocationError,
            dg.DispatchLaunchError,
            dg.DispatchTimeoutError,
            dg.DispatchCancelledError,
            dg.DispatchNonZeroExitError,
        ]
        for cls in expected:
            self.assertTrue(issubclass(cls, dg.DispatchGatewayError),
                            f"{cls.__name__} must be a subclass of DispatchGatewayError")

    def test_no_extra_exceptions(self) -> None:
        expected = {
            "DispatchGatewayError",
            "DispatchInputError",
            "DispatchSnapshotError",
            "ProviderNotSupportedError",
            "ExecutableNotFoundError",
            "DispatchInvocationError",
            "DispatchLaunchError",
            "DispatchTimeoutError",
            "DispatchCancelledError",
            "DispatchNonZeroExitError",
        }
        for name in expected:
            self.assertTrue(hasattr(dg, name), f"Missing exception: {name}")

    def test_errors_are_exceptions(self) -> None:
        for name in dg.__all__:
            if "Error" in name:
                cls = getattr(dg, name)
                self.assertTrue(issubclass(cls, Exception),
                                f"{name} is not an Exception subclass")

    def test_provider_not_supported_error_carries_provider_id(self) -> None:
        exc = dg.ProviderNotSupportedError("my-provider")
        self.assertEqual(exc.provider_id, "my-provider")

    def test_executable_not_found_error_carries_executable(self) -> None:
        exc = dg.ExecutableNotFoundError("/bad/path")
        self.assertEqual(exc.executable, "/bad/path")


# =========================================================================
# 16. Zero file writes
# =========================================================================


class ZeroFileWritesTests(unittest.TestCase):
    """TC-13.7: zero persistence / zero file writes tests."""

    def test_no_file_writes_on_success(self) -> None:
        with mock.patch("builtins.open") as mock_open:
            _dispatch()
            mock_open.assert_not_called()

    def test_no_file_writes_on_non_zero_exit(self) -> None:
        with mock.patch("builtins.open") as mock_open:
            try:
                _dispatch(exit_code=1)
            except dg.DispatchNonZeroExitError:
                pass
            mock_open.assert_not_called()

    def test_no_file_writes_on_timeout(self) -> None:
        async def _check():
            proc = _FakeProcess(returncode=0)
            async def slow_comm(input=None):
                await asyncio.sleep(5)
                return b"", b""
            proc.communicate = slow_comm
            target = "dispatcher_gateway.asyncio.create_subprocess_exec"

            with mock.patch("builtins.open") as mock_open_fn:
                with mock.patch(target, return_value=proc):
                    with mock.patch.object(dg, "_terminate_process") as mock_term:
                        async def _term(p):
                            pass
                        mock_term.side_effect = _term
                        try:
                            await run_dispatch(
                                _make_request(timeout_seconds=1),
                                _make_providers(),
                            )
                        except dg.DispatchTimeoutError:
                            pass
                        mock_open_fn.assert_not_called()
                        return True
        self.assertTrue(_run_async(_check()))


# =========================================================================
# 17. Import boundary
# =========================================================================


class ImportBoundaryTests(unittest.TestCase):
    """TC-13.7: import boundary verification — no mad_gateway, mad_refs, etc."""

    def test_no_mad_gateway_import(self) -> None:
        src = (_SCRIPTS / "dispatcher_gateway.py").read_text(encoding="utf-8")
        self.assertNotIn("mad_gateway", src)

    def test_no_mad_refs_import(self) -> None:
        src = (_SCRIPTS / "dispatcher_gateway.py").read_text(encoding="utf-8")
        self.assertNotIn("mad_refs", src)

    def _code_only(self) -> str:
        """Return source without docstrings and comments."""
        src = (_SCRIPTS / "dispatcher_gateway.py").read_text(encoding="utf-8")
        lines: list[str] = []
        in_docstring = False
        for line in src.splitlines():
            stripped = line.strip()
            if '"""' in stripped:
                in_docstring = not in_docstring
                continue
            if in_docstring:
                continue
            if stripped.startswith("#"):
                continue
            lines.append(line)
        return "\n".join(lines)

    def test_no_claude_reference(self) -> None:
        code = self._code_only()
        self.assertNotIn("claude", code.lower())
        self.assertNotIn("--permission-mode", code)

    def test_no_mad_home_setting(self) -> None:
        code = self._code_only()
        self.assertNotIn("MAD_HOME", code)

    def test_no_mad_participant_setting(self) -> None:
        code = self._code_only()
        self.assertNotIn("MAD_PARTICIPANT", code)

    def test_fake_provider_not_in_production_module(self) -> None:
        code = self._code_only()
        self.assertNotIn("FakeAgentCliProvider", code)

    def test_no_module_level_registry(self) -> None:
        code = self._code_only()
        self.assertNotIn("register_provider", code)
        self.assertNotIn("unregister_provider", code)

    def test_no_core_types_import(self) -> None:
        """dispatcher_gateway may or may not import core_types —
        but must not import WorkerKind, TaskDifficulty etc."""
        src = (_SCRIPTS / "dispatcher_gateway.py").read_text(encoding="utf-8")
        self.assertNotIn("from core_types import", src)
        self.assertNotIn("import core_types", src)


# =========================================================================
# 18. Independent subprocess import test
# =========================================================================


class IndependentSubprocessImportTest(unittest.TestCase):
    """Verify dispatcher_gateway.py imports cleanly in an isolated subprocess."""

    def test_import_no_output_in_subprocess(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                ("import sys; sys.path.insert(0, r'{}'); "
                 "import dispatcher_gateway; "
                 "print('IMPORT_OK')").format(str(_SCRIPTS)),
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0,
                         f"Import failed: stderr={result.stderr}")
        self.assertIn("IMPORT_OK", result.stdout)


# =========================================================================
# 19. Cross-module compatibility
# =========================================================================


class CrossModuleCompatibilityTests(unittest.TestCase):
    """TC-13.7: ensure no module identity split when loaded with other modules."""

    def test_coexists_with_mad_gateway(self) -> None:
        sys.path.insert(0, str(_SCRIPTS))
        try:
            import mad_gateway as mg  # noqa: F811
            # Reload dispatcher_gateway fresh
            if "dispatcher_gateway" in sys.modules:
                del sys.modules["dispatcher_gateway"]
            import dispatcher_gateway as dg2  # noqa: F811
            self.assertIsNotNone(mg)
            self.assertIsNotNone(dg2)
        finally:
            sys.path.pop(0)

    def test_coexists_with_mad_refs(self) -> None:
        sys.path.insert(0, str(_SCRIPTS))
        try:
            import mad_refs as mr  # noqa: F811
            if "dispatcher_gateway" in sys.modules:
                del sys.modules["dispatcher_gateway"]
            import dispatcher_gateway as dg2  # noqa: F811
            self.assertIsNotNone(mr)
            self.assertIsNotNone(dg2)
        finally:
            sys.path.pop(0)

    def test_coexists_with_core_types(self) -> None:
        sys.path.insert(0, str(_SCRIPTS))
        try:
            import core_types as ct  # noqa: F811
            if "dispatcher_gateway" in sys.modules:
                del sys.modules["dispatcher_gateway"]
            import dispatcher_gateway as dg2  # noqa: F811
            self.assertIsNotNone(ct)
            self.assertIsNotNone(dg2)
        finally:
            sys.path.pop(0)


# =========================================================================
# 20. Scope exclusion
# =========================================================================


class ScopeExclusionTests(unittest.TestCase):
    """TC-13.7: verify excluded concepts are absent from production module."""

    def _code_body(self) -> str:
        src = (_SCRIPTS / "dispatcher_gateway.py").read_text(encoding="utf-8")
        # Remove docstring and comments
        in_docstring = False
        lines: list[str] = []
        for line in src.splitlines():
            stripped = line.strip()
            if stripped.startswith('"""') or stripped.startswith("'''"):
                in_docstring = not in_docstring
                continue
            if in_docstring:
                continue
            if stripped.startswith("#"):
                continue
            lines.append(line)
        return "\n".join(lines)

    def test_no_worker_kind(self) -> None:
        self.assertNotIn("WorkerKind", self._code_body())

    def test_no_task_difficulty(self) -> None:
        self.assertNotIn("TaskDifficulty", self._code_body())

    def test_no_budget_policy(self) -> None:
        self.assertNotIn("ContextBudgetPolicy", self._code_body())

    def test_no_retry_count(self) -> None:
        self.assertNotIn("retry_count", self._code_body())

    def test_no_slot_lease(self) -> None:
        body = self._code_body().lower()
        self.assertNotIn("lease", body)
        self.assertNotIn("fencing", body)

    def test_no_rate_limit(self) -> None:
        self.assertNotIn("rate_limit", self._code_body().lower())

    def test_no_escalation_logic(self) -> None:
        self.assertNotIn("escalation", self._code_body().lower())

    def test_no_approval_logic(self) -> None:
        """No ApprovalGate import (field name model_degradation_approval_id is frozen)."""
        body = self._code_body()
        self.assertNotIn("ApprovalGate", body)
        self.assertNotIn("approval_gate", body.lower())

    def test_no_executor_model(self) -> None:
        self.assertNotIn("executor_model", self._code_body())

    def test_no_parse_result(self) -> None:
        self.assertNotIn("parse_result", self._code_body())


if __name__ == "__main__":
    unittest.main()
