"""Composition root for locally configured AgentDesk CLI providers.

The core Dockyard runtime intentionally accepts provider objects from its
caller.  This module is the small, explicit bridge from the gitignored local
``model-bindings.yaml`` to those immutable provider objects.  It never reads
credentials, starts a process, probes a network, or infers a provider from a
task.  The PM route and the provider mapping therefore use the same local
binding source without weakening the runtime's explicit injection boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import select_model as _select_model
from claude_code_provider import ClaudeCodeProvider
from codex_cli_provider import CodexCliProvider
from dispatcher_gateway import AgentCliProvider
from reasonix_cli_provider import ReasonixCliProvider

__all__ = [
    "DockyardConfiguredProviders",
    "DockyardProviderFactoryError",
    "build_configured_providers",
]


class DockyardProviderFactoryError(ValueError):
    """A local provider binding cannot be composed safely."""


_SUPPORTED_PROVIDERS = frozenset({"claude", "codex", "reasonix"})
_CLI_VERSIONS = {
    "claude": "2.1.214",
    "codex": "0.144.6",
    "reasonix": "1.19.1",
}
_CANONICAL_EXECUTABLES = {
    "claude": "claude",
    "codex": "codex",
    "reasonix": "reasonix",
}
_REASONIX_ALLOWED_TOOLS = ("Bash", "Read", "Write", "Edit")


def _absolute_project_root(value: object) -> Path:
    if not isinstance(value, Path) or not value.is_absolute():
        raise DockyardProviderFactoryError("project_root must be an absolute Path")
    try:
        resolved = value.resolve(strict=True)
    except OSError as exc:
        raise DockyardProviderFactoryError("project_root is unavailable") from exc
    if not resolved.is_dir() or resolved.is_symlink():
        raise DockyardProviderFactoryError("project_root must be a plain directory")
    return resolved


def _executable_overrides(
    overrides: tuple[tuple[str, str], ...],
) -> dict[str, str]:
    if type(overrides) is not tuple:
        raise DockyardProviderFactoryError("executable_overrides must be a tuple")
    result: dict[str, str] = {}
    for item in overrides:
        if type(item) is not tuple or len(item) != 2:
            raise DockyardProviderFactoryError(
                "executable_overrides must contain provider/path pairs"
            )
        provider_id, executable = item
        if type(provider_id) is not str or not provider_id:
            raise DockyardProviderFactoryError("provider executable key is invalid")
        if provider_id not in _SUPPORTED_PROVIDERS:
            raise DockyardProviderFactoryError("provider executable key is unsupported")
        if type(executable) is not str or not executable or executable != executable.strip():
            raise DockyardProviderFactoryError("provider executable is invalid")
        if provider_id in result:
            raise DockyardProviderFactoryError("duplicate provider executable override")
        result[provider_id] = executable
    return result


def _load_enabled_bindings(project_root: Path) -> tuple[dict[str, object], ...]:
    try:
        document = _select_model._load_worktree_json_object(
            project_root,
            _select_model.MODEL_BINDINGS_FILE,
            "model bindings",
        )
        validated = _select_model._validated_bindings(document)
    except _select_model.SelectionError as exc:
        raise DockyardProviderFactoryError("model bindings are invalid") from exc
    return tuple(
        item for item in validated
        if item["enabled"] and item["provider"] in _SUPPORTED_PROVIDERS
    )


def _best_binding(
    candidates: tuple[dict[str, object], ...],
) -> dict[str, object]:
    if not candidates:
        raise DockyardProviderFactoryError("provider has no enabled binding")
    return sorted(
        candidates,
        key=lambda item: (
            -_select_model.MODEL_TIER_INDEX[item["tier"]],
            -_select_model.DELIBERATION_TIER_INDEX[item["deliberation_tier"]],
            item["binding_id"],
        ),
    )[0]


def _provider(
    provider_id: str,
    binding: dict[str, object],
    executable: str,
) -> AgentCliProvider:
    if provider_id == "reasonix":
        if binding["model_id"] != "deepseek-v4-flash" or binding["tier"] != "basic":
            raise DockyardProviderFactoryError(
                "Reasonix binding must remain Basic deepseek-v4-flash"
            )
        return ReasonixCliProvider(
            provider_id="reasonix",
            executable=executable,
            permission_mode="acceptEdits",
            allowed_tools=_REASONIX_ALLOWED_TOOLS,
            max_steps=12,
        )
    if provider_id == "claude":
        return ClaudeCodeProvider(
            provider_id="claude",
            executable=executable,
            permission_mode="acceptEdits",
            allowed_tools=("Read", "Edit", "Write", "Bash"),
            disallowed_tools=(),
        )
    if provider_id == "codex":
        return CodexCliProvider(
            provider_id="codex",
            executable=executable,
            sandbox_mode="workspace-write",
        )
    raise DockyardProviderFactoryError("unsupported provider")


@dataclass(frozen=True, slots=True)
class DockyardConfiguredProviders:
    """Immutable provider objects and version evidence for LocalRuntime."""

    providers: tuple[tuple[str, AgentCliProvider], ...]
    provider_cli_versions: tuple[tuple[str, str], ...]
    binding_ids: tuple[tuple[str, str], ...]

    def __post_init__(self) -> None:
        if type(self.providers) is not tuple or type(self.provider_cli_versions) is not tuple:
            raise DockyardProviderFactoryError("configured providers must be tuples")
        provider_ids = tuple(provider_id for provider_id, _ in self.providers)
        if len(set(provider_ids)) != len(provider_ids):
            raise DockyardProviderFactoryError("configured provider IDs must be unique")
        if tuple(provider_id for provider_id, _ in self.provider_cli_versions) != provider_ids:
            raise DockyardProviderFactoryError("provider version evidence diverges")
        if len(self.binding_ids) != len(provider_ids):
            raise DockyardProviderFactoryError("binding evidence diverges")

    def as_mapping(self) -> dict[str, AgentCliProvider]:
        """Return the explicit mapping accepted by ``DockyardLocalRuntime``."""
        return dict(self.providers)


def build_configured_providers(
    project_root: Path,
    *,
    executable_overrides: tuple[tuple[str, str], ...] = (),
) -> DockyardConfiguredProviders:
    """Compose enabled local bindings without starting any provider."""
    root = _absolute_project_root(project_root)
    overrides = _executable_overrides(executable_overrides)
    bindings = _load_enabled_bindings(root)
    grouped: dict[str, list[dict[str, object]]] = {}
    for binding in bindings:
        grouped.setdefault(binding["provider"], []).append(binding)

    providers: list[tuple[str, AgentCliProvider]] = []
    versions: list[tuple[str, str]] = []
    binding_ids: list[tuple[str, str]] = []
    for provider_id in sorted(grouped):
        selected = _best_binding(tuple(grouped[provider_id]))
        executable = overrides.get(provider_id, _CANONICAL_EXECUTABLES[provider_id])
        try:
            provider = _provider(provider_id, selected, executable)
        except ValueError as exc:
            raise DockyardProviderFactoryError(
                f"provider {provider_id!r} binding cannot be composed"
            ) from exc
        providers.append((provider_id, provider))
        versions.append((provider_id, _CLI_VERSIONS[provider_id]))
        binding_ids.append((provider_id, selected["binding_id"]))

    return DockyardConfiguredProviders(
        tuple(providers),
        tuple(versions),
        tuple(binding_ids),
    )
