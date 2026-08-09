"""Production owner for the local Dockyard control-plane lifetime."""

from __future__ import annotations

import hashlib
import os
import subprocess
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from dockyard_composition import (
    DockyardCompositionGateway,
    DockyardPairingAuthorizer,
    DockyardPlanReadService,
)
from dockyard_active_execution import DockyardActiveExecutionRegistry
from dockyard_control_api import (
    DockyardApiConfig,
    DockyardControlServer,
    DockyardServerAddress,
)
from dockyard_pairing import (
    DockyardDeviceScope,
    DockyardPairingError,
    DockyardPairingStore,
)
from dockyard_plan_store import DockyardPlanStore
from dockyard_pm_service import DockyardPmService
from dockyard_project_registry import DockyardProjectRegistry
from dockyard_projection import DockyardProviderEvidence, DockyardProviderHealth
from dockyard_provider_service import (
    ProviderEvidenceState,
    ProviderHealthStatus,
    ProviderRuntimeEvidence,
    project_provider_health,
)
from dockyard_sse import DockyardSseHub
from dockyard_task_admission_composition import DockyardTaskAdmissionCompositionRuntime
from dockyard_post_admission_worker import DockyardPostAdmissionWorkerRuntime
from dockyard_owner_loss_recovery import DockyardOwnerLossRecoveryRuntime
from dockyard_provider_factory import (
    DockyardConfiguredProviders,
    build_configured_providers,
)
from dockyard_terminal_owner_composition import (
    DockyardTerminalOwnerCompositionRuntime,
)
from dispatcher_gateway import AgentCliProvider
from agent_capability_registry import AgentCapabilityRegistry
from mad_gateway import MadGatewayConfig
from dockyard_web_runtime import (
    DockyardBrowserBootstrap,
    DockyardWebRuntime,
    DockyardWebRuntimeConfig,
)

__all__ = [
    "DockyardBrowserBootstrap",
    "DockyardLocalRuntime",
    "DockyardLocalRuntimeConfig",
    "DockyardLocalRuntimeConflictError",
    "DockyardLocalRuntimeError",
    "DockyardLocalRuntimeInputError",
    "DockyardLocalRuntimePreconditionError",
    "DockyardLocalRuntimeResult",
]


class DockyardLocalRuntimeError(Exception):
    """Base error for local runtime composition."""


class DockyardLocalRuntimeInputError(DockyardLocalRuntimeError):
    """Runtime configuration is malformed."""


class DockyardLocalRuntimePreconditionError(DockyardLocalRuntimeError):
    """Required durable project or pairing evidence is unavailable."""


class DockyardLocalRuntimeConflictError(DockyardLocalRuntimeError):
    """Another runtime already owns the same local project."""


def _text(value: object, field_name: str, maximum: int = 512) -> str:
    if type(value) is not str or not value or value != value.strip() or len(value) > maximum:
        raise DockyardLocalRuntimeInputError(f"{field_name} is invalid")
    if any(ord(character) < 32 for character in value):
        raise DockyardLocalRuntimeInputError(f"{field_name} is invalid")
    return value


def _absolute_directory(value: object, field_name: str) -> Path:
    if not isinstance(value, Path) or not value.is_absolute():
        raise DockyardLocalRuntimeInputError(f"{field_name} must be an absolute Path")
    try:
        resolved = value.resolve(strict=True)
    except OSError as exc:
        raise DockyardLocalRuntimeInputError(f"{field_name} is unavailable") from exc
    if not resolved.is_dir() or resolved.is_symlink():
        raise DockyardLocalRuntimeInputError(f"{field_name} must be a plain directory")
    return resolved


def _timestamp(value: object) -> str:
    text = _text(value, "started_at", 64)
    candidate = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise DockyardLocalRuntimeInputError("started_at must be RFC3339 UTC") from exc
    if parsed.utcoffset() is None or parsed.utcoffset().total_seconds() != 0:
        raise DockyardLocalRuntimeInputError("started_at must be RFC3339 UTC")
    return text


def _runtime_now(clock: object | None) -> str:
    value = datetime.now(timezone.utc) if clock is None else getattr(clock, "now")()
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise DockyardLocalRuntimeInputError("runtime clock must return an aware datetime")
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _origin(value: object) -> str:
    text = _text(value, "browser_origin", 256)
    parsed = urlparse(text)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "::1", "localhost"}:
        raise DockyardLocalRuntimeInputError("browser_origin must be loopback HTTP")
    if parsed.path not in {"", "/"} or parsed.params or parsed.query or parsed.fragment:
        raise DockyardLocalRuntimeInputError("browser_origin must not contain a route")
    return text.rstrip("/")


def _git_head(project_root: Path) -> str:
    git_environment = os.environ.copy()
    for variable in (
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_COMMON_DIR",
        "GIT_DIR",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_WORK_TREE",
    ):
        git_environment.pop(variable, None)
    try:
        completed = subprocess.run(
            (
                "git",
                "-c",
                "core.longpaths=true",
                "-c",
                f"safe.directory={project_root}",
                "-C",
                str(project_root),
                "rev-parse",
                "--verify",
                "HEAD",
            ),
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="strict",
            timeout=10,
            env=git_environment,
        )
    except (OSError, subprocess.SubprocessError, UnicodeError) as exc:
        raise DockyardLocalRuntimePreconditionError("project HEAD is unavailable") from exc
    head = completed.stdout.strip()
    if len(head) != 40 or any(character not in "0123456789abcdef" for character in head):
        raise DockyardLocalRuntimePreconditionError("project HEAD is invalid")
    return head


def _api_base_url(address: DockyardServerAddress) -> str:
    host = f"[{address.host}]" if ":" in address.host else address.host
    return f"http://{host}:{address.port}"


def _provider_projection_evidence(
    project_root: Path,
    providers: Mapping[str, AgentCliProvider] | None,
    provider_cli_versions: tuple[tuple[str, str], ...],
    checked_at: str,
) -> tuple[DockyardProviderEvidence, ...]:
    if providers is None:
        return ()
    versions: dict[str, str] = {}
    for item in provider_cli_versions:
        if type(item) is not tuple or len(item) != 2:
            raise DockyardLocalRuntimeInputError("provider CLI version entry is invalid")
        provider_id = _text(item[0], "provider_id", 128)
        version = _text(item[1], "provider_cli_version", 128)
        if provider_id in versions:
            raise DockyardLocalRuntimeInputError("provider CLI versions contain duplicates")
        versions[provider_id] = version
    if set(versions) != set(providers):
        raise DockyardLocalRuntimeInputError("provider CLI versions diverge from providers")
    runtime = tuple(
        ProviderRuntimeEvidence(
            provider_id,
            ProviderEvidenceState.PRESENT,
            ProviderEvidenceState.PRESENT,
            versions[provider_id],
            ProviderEvidenceState.UNKNOWN,
            None,
            ProviderEvidenceState.UNKNOWN,
            (),
        )
        for provider_id in sorted(providers)
    )
    catalog = project_provider_health(project_root, runtime)
    status_map = {
        ProviderHealthStatus.READY: DockyardProviderHealth.READY,
        ProviderHealthStatus.DEGRADED: DockyardProviderHealth.DEGRADED,
        ProviderHealthStatus.NOT_READY: DockyardProviderHealth.NOT_READY,
        ProviderHealthStatus.UNKNOWN: DockyardProviderHealth.UNKNOWN,
    }
    evidence: list[DockyardProviderEvidence] = []
    for health in catalog.providers:
        bindings = tuple(sorted(health.bindings, key=lambda item: item.binding_id))
        if not bindings:
            continue
        binding = bindings[0]
        evidence.append(DockyardProviderEvidence(
            health.provider_id,
            binding.model_id,
            status_map[health.status],
            health.reason_code,
            health.executable_version,
            health.decoder_version,
            binding.binding_id,
            tuple(sorted(binding.capabilities)),
            checked_at,
        ))
    return tuple(evidence)


@dataclass(frozen=True, slots=True)
class DockyardLocalRuntimeConfig:
    runtime_id: str
    project_id: str
    plan_id: str
    project_root: Path
    registry_root: Path
    pairing_root: Path
    plan_store_root: Path
    task_cards_root: Path
    device_id: str
    device_token: str = field(repr=False)
    csrf_value: str = field(repr=False)
    browser_origin: str = "http://127.0.0.1:5173"
    bind_host: str = "127.0.0.1"
    port: int = 0
    started_at: str = "1970-01-01T00:00:00Z"
    web_assets_root: Path | None = None
    max_concurrency: int = 4

    def __post_init__(self) -> None:
        _text(self.runtime_id, "runtime_id", 128)
        _text(self.project_id, "project_id", 128)
        _text(self.plan_id, "plan_id", 128)
        for field_name in (
            "project_root", "registry_root", "pairing_root",
            "plan_store_root", "task_cards_root",
        ):
            _absolute_directory(getattr(self, field_name), field_name)
        _text(self.device_id, "device_id", 128)
        _text(self.device_token, "device_token", 512)
        _text(self.csrf_value, "csrf_value", 256)
        _origin(self.browser_origin)
        if self.bind_host not in {"127.0.0.1", "::1"}:
            raise DockyardLocalRuntimeInputError("bind_host must be loopback")
        if type(self.port) is not int or not 0 <= self.port <= 65535:
            raise DockyardLocalRuntimeInputError("port is invalid")
        _timestamp(self.started_at)
        if type(self.max_concurrency) is not int or isinstance(self.max_concurrency, bool) or not 1 <= self.max_concurrency <= 64:
            raise DockyardLocalRuntimeInputError("max_concurrency is invalid")
        if self.web_assets_root is not None:
            _absolute_directory(self.web_assets_root, "web_assets_root")
            parsed_origin = urlparse(self.browser_origin)
            if parsed_origin.port is None or parsed_origin.hostname != self.bind_host:
                raise DockyardLocalRuntimeInputError(
                    "browser_origin must bind the configured loopback host and port"
                )


@dataclass(frozen=True, slots=True)
class DockyardLocalRuntimeResult:
    schema_version: str
    runtime_id: str
    project_id: str
    address: DockyardServerAddress
    browser_bootstrap: DockyardBrowserBootstrap = field(repr=False)
    browser_url: str | None
    started_at: str
    content_digest: str

    def __post_init__(self) -> None:
        if self.schema_version != "dockyard.local-runtime/v1":
            raise DockyardLocalRuntimeInputError("runtime result schema is invalid")
        _text(self.runtime_id, "runtime_id", 128)
        _text(self.project_id, "project_id", 128)
        if type(self.address) is not DockyardServerAddress:
            raise DockyardLocalRuntimeInputError("runtime address is invalid")
        if type(self.browser_bootstrap) is not DockyardBrowserBootstrap:
            raise DockyardLocalRuntimeInputError("browser bootstrap is invalid")
        if self.browser_url is not None:
            _origin(self.browser_url)
        _timestamp(self.started_at)
        if (
            type(self.content_digest) is not str
            or len(self.content_digest) != 64
            or any(character not in "0123456789abcdef" for character in self.content_digest)
        ):
            raise DockyardLocalRuntimeInputError("runtime content digest is invalid")


_OWNERS_GUARD = threading.Lock()
_OWNERS: dict[tuple[str, str], str] = {}


class DockyardLocalRuntime:
    """Own one in-process loopback Control API for one registered project."""

    @classmethod
    def from_local_configuration(
        cls,
        config: DockyardLocalRuntimeConfig,
        *,
        executable_overrides: tuple[tuple[str, str], ...] = (),
        audit_config: MadGatewayConfig | None = None,
        integration_target_branch: str | None = None,
        clock: object | None = None,
        background_post_admission: bool = True,
    ) -> "DockyardLocalRuntime":
        """Inject providers from the gitignored local binding file.

        The normal constructor remains explicitly injectable for tests and
        embedders.  This composition-root entry point is the one-step local
        bootstrap: it reads non-secret model bindings, creates the immutable
        CLI provider objects, and passes them into the existing runtime.  No
        provider process or API call occurs during construction.
        """
        if type(config) is not DockyardLocalRuntimeConfig:
            raise DockyardLocalRuntimeInputError("config has an invalid type")
        configured: DockyardConfiguredProviders = build_configured_providers(
            config.project_root,
            executable_overrides=executable_overrides,
        )
        return cls(
            config,
            configured.as_mapping(),
            configured.provider_cli_versions,
            audit_config=audit_config,
            integration_target_branch=integration_target_branch,
            clock=clock,
            background_post_admission=background_post_admission,
        )

    def __init__(
        self,
        config: DockyardLocalRuntimeConfig,
        providers: Mapping[str, AgentCliProvider] | None = None,
        provider_cli_versions: tuple[tuple[str, str], ...] = (),
        *,
        audit_config: MadGatewayConfig | None = None,
        integration_target_branch: str | None = None,
        clock: object | None = None,
        background_post_admission: bool = False,
    ) -> None:
        if type(config) is not DockyardLocalRuntimeConfig:
            raise DockyardLocalRuntimeInputError("config has an invalid type")
        if type(background_post_admission) is not bool:
            raise DockyardLocalRuntimeInputError("background_post_admission is invalid")
        self._config = config
        if providers is not None and (not isinstance(providers, Mapping) or not providers):
            raise DockyardLocalRuntimeInputError("providers are invalid")
        if providers is not None and not provider_cli_versions:
            raise DockyardLocalRuntimeInputError("provider CLI versions are required")
        if audit_config is not None and type(audit_config) is not MadGatewayConfig:
            raise DockyardLocalRuntimeInputError("audit config is invalid")
        if (audit_config is None) != (integration_target_branch is None):
            raise DockyardLocalRuntimeInputError(
                "audit config and integration target branch must be configured together"
            )
        if audit_config is not None and providers is None:
            raise DockyardLocalRuntimeInputError(
                "terminal owner composition requires providers"
            )
        self._providers = providers
        self._provider_cli_versions = provider_cli_versions
        self._audit_config = audit_config
        self._integration_target_branch = integration_target_branch
        self._clock = clock
        self._background_post_admission = background_post_admission
        self._server: DockyardControlServer | None = None
        self._web_server: DockyardWebRuntime | None = None
        self._result: DockyardLocalRuntimeResult | None = None
        self._recovery_runtime: DockyardOwnerLossRecoveryRuntime | None = None
        self._owner_key = (
            str(config.registry_root.resolve(strict=True)).casefold(),
            config.project_id,
        )

    def start(self) -> DockyardLocalRuntimeResult:
        if self._result is not None:
            return self._result
        config = self._config
        project_root = _absolute_directory(config.project_root, "project_root")
        registry = DockyardProjectRegistry(config.registry_root)
        project = registry.read(config.project_id)
        if project is None:
            raise DockyardLocalRuntimePreconditionError("project is not registered")
        if Path(project.repository_root).resolve(strict=True) != project_root:
            raise DockyardLocalRuntimePreconditionError("registered project root diverged")
        pairing = DockyardPairingStore(config.pairing_root)
        device = pairing.read_device(config.device_id)
        if device is None or device.revoked_at is not None:
            raise DockyardLocalRuntimePreconditionError("paired device is unavailable")
        if DockyardDeviceScope.APPROVE not in device.scopes:
            raise DockyardLocalRuntimePreconditionError("paired device lacks approval scope")
        snapshot = _git_head(project_root)

        with _OWNERS_GUARD:
            owner = _OWNERS.get(self._owner_key)
            if owner is not None:
                raise DockyardLocalRuntimeConflictError("project runtime is already owned")
            _OWNERS[self._owner_key] = config.runtime_id

        server: DockyardControlServer | None = None
        web_server: DockyardWebRuntime | None = None
        try:
            try:
                pairing.verify_device_token(
                    config.device_id,
                    config.device_token,
                    DockyardDeviceScope.APPROVE,
                )
            except DockyardPairingError as exc:
                raise DockyardLocalRuntimePreconditionError(
                    "paired device authentication failed"
                ) from exc
            hub = DockyardSseHub()
            pm = DockyardPmService(
                DockyardPlanStore(config.plan_store_root),
                config.task_cards_root,
            )
            snapshot_commit = lambda: _git_head(project_root)
            post_admission = None
            terminal_owner = None
            recovery_runtime = None
            active_registry = DockyardActiveExecutionRegistry()
            agent_registry = AgentCapabilityRegistry.from_project(
                project_root,
                tuple(sorted(self._providers)) if self._providers is not None else (),
                config.max_concurrency,
            )
            if self._providers is not None:
                if self._audit_config is not None:
                    assert self._integration_target_branch is not None
                    terminal_owner = DockyardTerminalOwnerCompositionRuntime(
                        project_root,
                        self._audit_config,
                        self._integration_target_branch,
                        clock=self._clock,
                    )
                post_admission = DockyardPostAdmissionWorkerRuntime(
                    project_root, self._providers, self._provider_cli_versions,
                    clock=self._clock,
                    terminal_owner=terminal_owner,
                    active_registry=active_registry,
                )
                recovery_runtime = DockyardOwnerLossRecoveryRuntime(
                    project_root,
                    self._providers,
                    self._provider_cli_versions,
                    clock=self._clock,
                )
            read_service = DockyardPlanReadService(
                snapshot_commit,
                registry,
                pm,
                config.project_id,
                config.plan_id,
                terminal_owner,
                project_root,
                _provider_projection_evidence(
                    project_root,
                    self._providers,
                    self._provider_cli_versions,
                    config.started_at,
                ),
                active_registry=active_registry,
                retry_provider_ids=(
                    tuple(sorted(self._providers))
                    if self._providers is not None
                    else ()
                ),
            )
            gateway = DockyardCompositionGateway(
                pm,
                snapshot_commit,
                hub,
                registry,
                DockyardTaskAdmissionCompositionRuntime(
                    project_root,
                    post_admission=post_admission,
                    background_post_admission=self._background_post_admission,
                ),
                terminal_owner,
                plan_id=config.plan_id,
                project_root=project_root,
                active_registry=active_registry,
                ready_provider_ids=(
                    tuple(sorted(self._providers))
                    if self._providers is not None
                    else ()
                ),
                providers=self._providers,
                provider_cli_versions=(
                    self._provider_cli_versions if self._providers is not None else None
                ),
                agent_registry=agent_registry,
                plan_id_changed=read_service.set_current_plan_id,
                now=lambda: _runtime_now(self._clock),
            )
            server = DockyardControlServer(
                DockyardApiConfig(
                    config.bind_host,
                    config.port,
                    (config.bind_host, "localhost"),
                    (_origin(config.browser_origin),),
                ),
                read_service,
                gateway,
                DockyardPairingAuthorizer(pairing, config.csrf_value),
                hub,
            )
            address = server.start()
            bootstrap = DockyardBrowserBootstrap(
                _api_base_url(address),
                config.project_id,
                config.device_id,
                config.device_token,
                config.csrf_value,
            )
            browser_url = None
            if config.web_assets_root is not None:
                parsed_origin = urlparse(config.browser_origin)
                assert parsed_origin.port is not None
                api_upstream = bootstrap.api_base_url
                bootstrap = DockyardBrowserBootstrap(
                    config.browser_origin,
                    config.project_id,
                    config.device_id,
                    config.device_token,
                    config.csrf_value,
                )
                web_server = DockyardWebRuntime(DockyardWebRuntimeConfig(
                    config.web_assets_root,
                    bootstrap,
                    config.bind_host,
                    parsed_origin.port,
                    api_upstream,
                ))
                browser_url = web_server.start().browser_url
                if browser_url.rstrip("/") != config.browser_origin.rstrip("/"):
                    raise DockyardLocalRuntimePreconditionError(
                        "Web shell origin diverged"
                    )
            digest_values = (
                config.runtime_id,
                config.project_id,
                address.host,
                str(address.port),
                snapshot,
                config.started_at,
                config.device_id,
                config.device_token,
                config.csrf_value,
                browser_url or "",
            )
            digest = hashlib.sha256("\x1f".join(digest_values).encode("utf-8")).hexdigest()
            result = DockyardLocalRuntimeResult(
                "dockyard.local-runtime/v1",
                config.runtime_id,
                config.project_id,
                address,
                bootstrap,
                browser_url,
                config.started_at,
                digest,
            )
            self._server = server
            self._web_server = web_server
            self._recovery_runtime = recovery_runtime
            self._result = result
            if recovery_runtime is not None:
                recovery_runtime.start_background()
            return result
        except Exception:
            if web_server is not None:
                web_server.stop()
            if server is not None:
                server.stop()
            with _OWNERS_GUARD:
                if _OWNERS.get(self._owner_key) == config.runtime_id:
                    del _OWNERS[self._owner_key]
            raise

    def stop(self) -> None:
        server = self._server
        web_server = self._web_server
        self._server = None
        self._web_server = None
        self._recovery_runtime = None
        self._result = None
        try:
            if web_server is not None:
                web_server.stop()
            if server is not None:
                server.stop()
        finally:
            with _OWNERS_GUARD:
                if _OWNERS.get(self._owner_key) == self._config.runtime_id:
                    del _OWNERS[self._owner_key]

    def __enter__(self) -> DockyardLocalRuntimeResult:
        return self.start()

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.stop()
