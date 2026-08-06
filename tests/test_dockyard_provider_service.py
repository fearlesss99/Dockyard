"""TC-13.29g3 — Dockyard provider health/config service tests."""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import threading
import unittest
from dataclasses import fields, is_dataclass
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_SCRIPTS = _ROOT / "skills" / "agentdesk" / "scripts"
sys.path.insert(0, str(_SCRIPTS))

from dockyard_provider_service import (  # noqa: E402
    DockyardProviderConflictError,
    DockyardProviderFilesystemError,
    DockyardProviderInputError,
    ModelBindingUpdateRequest,
    ProviderEvidenceState,
    ProviderHealthStatus,
    ProviderRuntimeEvidence,
    project_provider_health,
    update_model_binding,
)

sys.path.pop(0)


def _binding(
    provider: str,
    model: str,
    *,
    tier: str = "standard",
    capabilities: list[str] | None = None,
    enabled: bool = True,
) -> dict[str, object]:
    return {
        "provider": provider,
        "model_id": model,
        "tier": tier,
        "deliberation_tier": "balanced",
        "context_window_tokens": 128000,
        "capabilities": capabilities or ["structured_output"],
        "enabled": enabled,
    }


def _document() -> dict[str, object]:
    return {
        "schema_version": "agentdesk.model-bindings/v2",
        "updated_at": "2026-08-02T00:00:00Z",
        "bindings": {
            "claude-standard": _binding("claude", "claude-sonnet"),
            "codex-standard": _binding("codex", "gpt-5.6-sol"),
            "reasonix-basic": _binding(
                "reasonix",
                "deepseek-v4-flash",
                tier="basic",
            ),
        },
    }


def _evidence(
    provider: str,
    *,
    state: ProviderEvidenceState = ProviderEvidenceState.PRESENT,
    capabilities: tuple[str, ...] | None = None,
    cli_version: str | None = None,
    decoder_version: str | None = None,
) -> ProviderRuntimeEvidence:
    versions = {
        "claude": "2.1.214",
        "codex": "0.144.6",
        "reasonix": "1.19.1",
    }
    if capabilities is None:
        capabilities = (
            ("real_api_evidence", "structured_output")
            if provider == "reasonix"
            else ("structured_output",)
        )
    version = versions[provider]
    return ProviderRuntimeEvidence(
        provider_id=provider,
        executable_state=state,
        executable_version_state=state,
        executable_version=version if state is ProviderEvidenceState.PRESENT else None,
        decoder_state=state,
        decoder_version=version if state is ProviderEvidenceState.PRESENT else None,
        capability_state=state,
        capabilities=capabilities if state is ProviderEvidenceState.PRESENT else (),
    )


class _ProjectMixin:
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        runtime = self.root / ".agentdesk" / "runtime"
        runtime.mkdir(parents=True)
        self.path = runtime / "model-bindings.yaml"
        self.path.write_text(
            json.dumps(_document(), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def evidence(self) -> tuple[ProviderRuntimeEvidence, ...]:
        return tuple(_evidence(provider) for provider in ("claude", "codex", "reasonix"))

    def digest(self) -> str:
        return hashlib.sha256(self.path.read_bytes()).hexdigest()

    def request(self, **changes: object) -> ModelBindingUpdateRequest:
        values: dict[str, object] = {
            "expected_digest": self.digest(),
            "operation_id": "OP-001",
            "updated_at": "2026-08-02T01:00:00Z",
            "binding_id": "reasonix-basic",
            "provider_id": "reasonix",
            "model_id": "deepseek-v4-flash",
            "tier": "basic",
            "deliberation_tier": "efficient",
            "context_window_tokens": 128000,
            "capabilities": ("structured_output",),
            "enabled": True,
        }
        values.update(changes)
        return ModelBindingUpdateRequest(**values)  # type: ignore[arg-type]


class ProviderProjectionTests(_ProjectMixin, unittest.TestCase):
    def test_ready_claude_and_codex(self) -> None:
        catalog = project_provider_health(self.root, self.evidence())
        by_id = {item.provider_id: item for item in catalog.providers}
        self.assertEqual(by_id["claude"].status, ProviderHealthStatus.READY)
        self.assertEqual(by_id["codex"].status, ProviderHealthStatus.READY)

    def test_reasonix_requires_real_api_evidence(self) -> None:
        evidence = (
            _evidence("claude"),
            _evidence("codex"),
            _evidence("reasonix", capabilities=("structured_output",)),
        )
        health = project_provider_health(self.root, evidence).providers[2]
        self.assertEqual(health.status, ProviderHealthStatus.NOT_READY)
        self.assertEqual(health.missing_capabilities, ("real_api_evidence",))

    def test_reasonix_ready_when_all_evidence_is_present(self) -> None:
        health = project_provider_health(
            self.root,
            self.evidence(),
            preferred_tier="basic",
        ).providers[2]
        self.assertEqual(health.status, ProviderHealthStatus.READY)

    def test_unknown_is_fail_closed(self) -> None:
        evidence = (_evidence("claude"), _evidence("codex", state=ProviderEvidenceState.UNKNOWN))
        health = project_provider_health(self.root, evidence).providers[1]
        self.assertEqual(health.status, ProviderHealthStatus.UNKNOWN)
        self.assertFalse(health.requires_approval)

    def test_missing_executable_is_not_ready(self) -> None:
        evidence = (_evidence("claude", state=ProviderEvidenceState.MISSING),)
        health = project_provider_health(self.root, evidence).providers[0]
        self.assertEqual(health.status, ProviderHealthStatus.NOT_READY)

    def test_version_mismatch_is_not_ready(self) -> None:
        item = _evidence("codex")
        item = ProviderRuntimeEvidence(
            provider_id=item.provider_id,
            executable_state=item.executable_state,
            executable_version_state=item.executable_version_state,
            executable_version="0.144.5",
            decoder_state=item.decoder_state,
            decoder_version=item.decoder_version,
            capability_state=item.capability_state,
            capabilities=item.capabilities,
        )
        health = project_provider_health(self.root, (item,)).providers[1]
        self.assertEqual(health.reason_code, "CLI_VERSION_UNSUPPORTED")

    def test_basic_tier_is_degraded_and_requires_approval(self) -> None:
        health = project_provider_health(self.root, self.evidence()).providers[2]
        self.assertEqual(health.status, ProviderHealthStatus.DEGRADED)
        self.assertTrue(health.requires_approval)

    def test_no_enabled_binding_is_not_ready(self) -> None:
        document = _document()
        bindings = document["bindings"]
        assert isinstance(bindings, dict)
        binding = bindings["codex-standard"]
        assert isinstance(binding, dict)
        binding["enabled"] = False
        self.path.write_text(json.dumps(document), encoding="utf-8")
        health = project_provider_health(self.root, self.evidence()).providers[1]
        self.assertEqual(health.reason_code, "NO_ENABLED_BINDING")

    def test_duplicate_evidence_is_rejected(self) -> None:
        with self.assertRaises(DockyardProviderInputError):
            project_provider_health(self.root, (_evidence("codex"), _evidence("codex")))

    def test_catalog_exposes_no_credentials_or_absolute_paths(self) -> None:
        rendered = repr(project_provider_health(self.root, self.evidence())).lower()
        for forbidden in ("api_key", "credential", "password", str(self.root).lower()):
            self.assertNotIn(forbidden, rendered)


class ProviderConfigurationTests(_ProjectMixin, unittest.TestCase):
    def test_atomic_binding_update(self) -> None:
        result = update_model_binding(self.root, self.request())
        self.assertFalse(result.replayed)
        self.assertNotEqual(result.previous_digest, result.current_digest)
        document = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(document["bindings"]["reasonix-basic"]["deliberation_tier"], "efficient")

    def test_byte_exact_replay_returns_same_digest(self) -> None:
        request = self.request()
        first = update_model_binding(self.root, request)
        second = update_model_binding(self.root, request)
        self.assertTrue(second.replayed)
        self.assertEqual(first.current_digest, second.current_digest)

    def test_divergent_stale_cas_is_rejected(self) -> None:
        request = self.request()
        update_model_binding(self.root, request)
        divergent = self.request(
            expected_digest=request.expected_digest,
            operation_id="OP-002",
            model_id="deepseek-v4-pro",
        )
        with self.assertRaises(DockyardProviderConflictError):
            update_model_binding(self.root, divergent)

    def test_other_bindings_are_preserved(self) -> None:
        update_model_binding(self.root, self.request())
        document = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(document["bindings"]["codex-standard"]["model_id"], "gpt-5.6-sol")

    def test_operation_path_injection_is_rejected(self) -> None:
        with self.assertRaises(DockyardProviderInputError):
            self.request(operation_id="../escape")

    def test_stale_temp_file_fails_closed(self) -> None:
        temporary = self.path.with_name(".model-bindings.yaml.OP-001.tmp")
        temporary.write_text("owned", encoding="utf-8")
        with self.assertRaises(DockyardProviderConflictError):
            update_model_binding(self.root, self.request())

    def test_non_file_binding_path_is_rejected(self) -> None:
        self.path.unlink()
        self.path.mkdir()
        with self.assertRaises(DockyardProviderFilesystemError):
            project_provider_health(self.root, self.evidence())

    def test_concurrent_divergent_updates_have_one_winner(self) -> None:
        expected = self.digest()
        requests = (
            self.request(expected_digest=expected, operation_id="OP-A"),
            self.request(
                expected_digest=expected,
                operation_id="OP-B",
                model_id="deepseek-v4-pro",
            ),
        )
        barrier = threading.Barrier(2)
        outcomes: list[str] = []
        guard = threading.Lock()

        def run(request: ModelBindingUpdateRequest) -> None:
            barrier.wait(timeout=2)
            try:
                update_model_binding(self.root, request)
                outcome = "winner"
            except DockyardProviderConflictError:
                outcome = "conflict"
            with guard:
                outcomes.append(outcome)

        threads = [threading.Thread(target=run, args=(request,)) for request in requests]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=3)
            self.assertFalse(thread.is_alive())
        self.assertEqual(sorted(outcomes), ["conflict", "winner"])


class ProviderSourceBoundaryTests(unittest.TestCase):
    def test_public_types_are_frozen_and_slotted(self) -> None:
        import dockyard_provider_service as service

        for name in (
            "ProviderRuntimeEvidence",
            "ProviderBinding",
            "ProviderHealth",
            "ProviderCatalog",
            "ModelBindingUpdateRequest",
            "ModelBindingUpdateResult",
        ):
            cls = getattr(service, name)
            self.assertTrue(is_dataclass(cls))
            self.assertTrue(hasattr(cls, "__slots__"))
            self.assertGreater(len(fields(cls)), 0)

    def test_source_has_no_model_network_or_credential_access(self) -> None:
        source = (_SCRIPTS / "dockyard_provider_service.py").read_text(encoding="utf-8")
        for forbidden in (
            "subprocess",
            "socket",
            "requests",
            "httpx",
            "urllib",
            "os.getenv",
            "os.environ",
            "api_key",
            "password",
        ):
            self.assertNotIn(forbidden, source.lower())


if __name__ == "__main__":
    unittest.main()
