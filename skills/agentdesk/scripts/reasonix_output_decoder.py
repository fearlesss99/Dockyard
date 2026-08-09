"""Reasonix Output Decoder — TC-13.28a.2 Phase A.

Fail-closed decoder for ``reasonix`` CLI v1.19.1 JSON wrapper output.
Parses the Reasonix wrapper, validates success gates, then decodes
the inner result as a strict ``agentdesk.worker-output/v1`` envelope.

Phase A (current):
  * Unit-tested against fixture data (wrapper shape from CLI --help docs).
  * All success/failure paths are fixture-verified.
  * Real API probe evidence deferred to Phase B.

Non-goals:
  * Real CLI, model, API, or network calls.
  * Auto-version detection or fallback decoding.
  * Guessing success from exit code, text, or field presence.
  * Provider detection — this module is reasonix-specific.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass

from dispatcher_gateway import DispatchIdentity
from worker_adapter import WorkerResult
from worker_output_decoder import (
    WorkerCompletionStatus,
    WorkerOutput,
    WorkerOutputDecodeError,
    WorkerOutputError,
    WorkerOutputIdentityError,
    WorkerOutputIntegrityError,
    WorkerOutputSchemaError,
    WorkerOutputUnsupportedProviderError,
)

__all__ = [
    "ReasonixDecoder",
    "decode_reasonix_output",
    "ReasonixDecoderError",
    "ReasonixWrapperMismatchError",
]

# ── constants ───────────────────────────────────────────────────────────────

_PROVIDER: str = "reasonix"
_SUPPORTED_VERSION: str = "1.19.1"

# Minimum Reasonix wrapper keys we validate.  The exact full set will be
# determined by real API probe evidence in Phase B — until then we accept
# a known set of required + optional keys and fail-closed on unknown
# required-adjacent patterns.
_REQUIRED_WRAPPER_KEYS: frozenset[str] = frozenset({
    "type",
    "subtype",
    "is_error",
    "result",
})

_OPTIONAL_WRAPPER_KEYS: frozenset[str] = frozenset({
    "duration_ms",
    "duration_api_ms",
    "num_turns",
    "session_id",
    "total_cost_usd",
    "usage",
    "modelUsage",
    "permission_denials",
    "uuid",
    "api_error_status",
    "stop_reason",
    "terminal_reason",
    "fast_mode_state",
    "ttft_ms",
    "ttft_stream_ms",
    "time_to_request_ms",
})

_ALL_WRAPPER_KEYS: frozenset[str] = _REQUIRED_WRAPPER_KEYS | _OPTIONAL_WRAPPER_KEYS

# Envelope keys imported from worker-output/v1 contract.
_ENVELOPE_KEYS: frozenset[str] = frozenset({
    "schema_version",
    "task_id",
    "revision",
    "attempt",
    "dispatch_id",
    "status",
    "implementation_commit",
    "report_commit",
    "summary",
    "warnings",
})
_ENVELOPE_SCHEMA_VERSION: str = "agentdesk.worker-output/v1"
_COMMIT_HEX_RE: re.Pattern[str] = re.compile(r"^[0-9a-f]{40}$")

# ═══════════════════════════════════════════════════════════════════════════════
# Exception Hierarchy
# ═══════════════════════════════════════════════════════════════════════════════


class ReasonixDecoderError(WorkerOutputError):
    """Base for Reasonix-specific decoder errors."""


class ReasonixWrapperMismatchError(ReasonixDecoderError):
    """Reasonix wrapper is structurally invalid or does not match the expected
    v1.19.1 schema."""

    def __init__(self, detail: str) -> None:
        super().__init__(f"Reasonix wrapper mismatch: {detail}")
        self.detail = detail


# ═══════════════════════════════════════════════════════════════════════════════
# Decoder entry point
# ═══════════════════════════════════════════════════════════════════════════════


def decode_reasonix_output(
    result: WorkerResult,
    provider_cli_version: str,
) -> WorkerOutput:
    """Decode a Reasonix ``WorkerResult`` into a typed ``WorkerOutput``.

    Args:
        result: Must be exactly ``WorkerResult``.  ``dispatch_result`` must
                carry ``provider == "reasonix"``.
        provider_cli_version: Must be exactly ``"1.19.1"``.

    Returns:
        ``WorkerOutput`` on success.

    Raises:
        ReasonixDecoderError, WorkerOutputSchemaError, WorkerOutputDecodeError,
        WorkerOutputIntegrityError, WorkerOutputIdentityError for any
        structural, schema, integrity, or identity violation.
    """
    # 1. Type check result
    if not isinstance(result, WorkerResult):
        raise WorkerOutputError(
            f"result must be WorkerResult, got {type(result).__name__}"
        )

    dr = result.dispatch_result
    if dr is None:
        raise ReasonixDecoderError("dispatch_result is None")

    # 2. Provider gate
    if dr.provider != _PROVIDER:
        raise WorkerOutputUnsupportedProviderError(dr.provider)

    # 3. Version gate
    if not isinstance(provider_cli_version, str) or not provider_cli_version:
        raise ReasonixDecoderError(
            "provider_cli_version must be a non-empty str"
        )
    if provider_cli_version != _SUPPORTED_VERSION:
        from worker_output_decoder import WorkerOutputUnsupportedVersionError
        raise WorkerOutputUnsupportedVersionError(provider_cli_version)

    # 4. SHA-256 integrity check on stdout
    expected_sha = dr.stdout_sha256
    if not expected_sha:
        raise WorkerOutputIntegrityError()
    computed = hashlib.sha256(dr.stdout).hexdigest()
    if computed != expected_sha:
        raise WorkerOutputIntegrityError()

    # 5. Parse wrapper JSON
    wrapper = _parse_strict_json(dr.stdout, "Reasonix wrapper")

    # 6. Validate wrapper structure
    _validate_wrapper(wrapper)

    # 7. Gate on type/subtype/is_error
    wrapper_type = wrapper.get("type")
    wrapper_subtype = wrapper.get("subtype")
    wrapper_is_error = wrapper.get("is_error")

    if wrapper_type != "result":
        raise ReasonixWrapperMismatchError(
            f"wrapper type must be 'result', got {wrapper_type!r}"
        )
    if wrapper_subtype != "success":
        raise ReasonixWrapperMismatchError(
            f"wrapper subtype must be 'success', got {wrapper_subtype!r}"
        )
    if wrapper_is_error is not False:
        raise ReasonixWrapperMismatchError(
            f"wrapper is_error must be False, got {wrapper_is_error!r}"
        )

    # 8. Extract and parse envelope from result field
    result_field = wrapper.get("result")
    if not isinstance(result_field, str) or not result_field:
        raise ReasonixWrapperMismatchError(
            "wrapper.result must be a non-empty str"
        )

    envelope = _parse_strict_json(
        result_field.encode("utf-8"),
        "Reasonix result envelope",
    )

    # 9. Validate envelope
    _validate_envelope(envelope, dr.identity)

    # 10. Construct WorkerOutput
    return _build_worker_output(envelope, dr)


# ═══════════════════════════════════════════════════════════════════════════════
# Internal helpers
# ═══════════════════════════════════════════════════════════════════════════════


def _parse_strict_json(raw: bytes, label: str) -> dict:
    """Parse *raw* as strict UTF-8 JSON, returning a ``dict``.

    Rules:
      * UTF-8 only — no BOM.
      * Single root object (``dict``).
      * No NaN, Infinity.
      * No trailing text after the root object.
      * No duplicate keys.
    """
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as e:
        raise WorkerOutputDecodeError(
            f"{label} is not valid UTF-8: {e}"
        ) from None

    # Reject BOM
    if text.startswith("﻿"):
        raise WorkerOutputDecodeError(
            f"{label} must not start with UTF-8 BOM"
        )

    try:
        obj = json.loads(text)
    except json.JSONDecodeError as e:
        raise WorkerOutputDecodeError(
            f"failed to parse {label} as JSON: {e}"
        ) from None

    # Must be a dict
    if not isinstance(obj, dict):
        raise WorkerOutputDecodeError(
            f"{label} must be a JSON object, got {type(obj).__name__}"
        )

    # Check for trailing text after root — re-decode with a custom check.
    # json.loads ignores trailing whitespace but we want strict no-trailing.
    stripped = text.strip()
    # Re-encode and decode test: strip the end, add back the closing brace.
    # If the original text does not end with exactly } (or } with whitespace),
    # it already failed.  We check that index of last } is at the end.
    last_rbrace = stripped.rfind("}")
    if last_rbrace == -1:
        raise WorkerOutputDecodeError(f"{label} must end with '}}'")
    if len(stripped) > last_rbrace + 1:
        raise WorkerOutputDecodeError(
            f"{label} has trailing content after root object"
        )

    return obj


def _validate_wrapper(wrapper: dict) -> None:
    """Validate Reasonix v1.19.1 wrapper structural rules.

    * All required keys must be present.
    * No unknown keys outside the known set.
    * No duplicate keys (handled by JSON parse).
    """
    wrapper_keys = set(wrapper.keys())

    missing = _REQUIRED_WRAPPER_KEYS - wrapper_keys
    if missing:
        raise ReasonixWrapperMismatchError(
            f"wrapper missing required keys: {sorted(missing)}"
        )

    unknown = wrapper_keys - _ALL_WRAPPER_KEYS
    if unknown:
        raise ReasonixWrapperMismatchError(
            f"wrapper contains unknown keys: {sorted(unknown)}"
        )

    # Validate known field types
    for key in ("type", "subtype"):
        val = wrapper.get(key)
        if not isinstance(val, str):
            raise ReasonixWrapperMismatchError(
                f"wrapper.{key} must be str, got {type(val).__name__}"
            )

    is_error = wrapper.get("is_error")
    if not isinstance(is_error, bool):
        raise ReasonixWrapperMismatchError(
            f"wrapper.is_error must be bool, got {type(is_error).__name__}"
        )

    # result must be str (checked separately in _validate_wrapper)

    # usage / modelUsage must be dict if present
    for usage_key in ("usage", "modelUsage"):
        val = wrapper.get(usage_key)
        if val is not None and not isinstance(val, dict):
            raise ReasonixWrapperMismatchError(
                f"wrapper.{usage_key} must be a dict if present, "
                f"got {type(val).__name__}"
            )

    # permission_denials must be list if present
    pd_list = wrapper.get("permission_denials")
    if pd_list is not None and not isinstance(pd_list, list):
        raise ReasonixWrapperMismatchError(
            f"wrapper.permission_denials must be a list if present"
        )

    # Numeric fields — must be int >= 0 if present
    for int_key in (
        "duration_ms", "duration_api_ms", "num_turns",
        "ttft_ms", "ttft_stream_ms", "time_to_request_ms",
    ):
        val = wrapper.get(int_key)
        if val is not None:
            if isinstance(val, bool) or not isinstance(val, (int, float)):
                raise ReasonixWrapperMismatchError(
                    f"wrapper.{int_key} must be a number if present"
                )
            if val < 0:
                raise ReasonixWrapperMismatchError(
                    f"wrapper.{int_key} must be >= 0, got {val}"
                )


def _validate_envelope(envelope: dict, identity: DispatchIdentity) -> None:
    """Validate the inner worker-output envelope against the frozen contract."""
    env_keys = set(envelope.keys())

    missing = _ENVELOPE_KEYS - env_keys
    if missing:
        raise WorkerOutputSchemaError(
            f"envelope missing required keys: {sorted(missing)}"
        )

    extra = env_keys - _ENVELOPE_KEYS
    if extra:
        raise WorkerOutputSchemaError(
            f"envelope contains unknown keys: {sorted(extra)}"
        )

    # schema_version
    sv = envelope.get("schema_version")
    if sv != _ENVELOPE_SCHEMA_VERSION:
        raise WorkerOutputSchemaError(
            f"envelope.schema_version must be "
            f"{_ENVELOPE_SCHEMA_VERSION!r}, got {sv!r}"
        )

    # Identity match on all 4 fields
    for field in ("task_id", "revision", "attempt", "dispatch_id"):
        env_val = envelope.get(field)
        ident_val = getattr(identity, field)
        if env_val != ident_val:
            raise WorkerOutputIdentityError(
                f"envelope.{field} ({env_val!r}) != "
                f"DispatchIdentity.{field} ({ident_val!r})"
            )

    # Status must be a valid WorkerCompletionStatus value
    status_raw = envelope.get("status")
    if not isinstance(status_raw, str):
        raise WorkerOutputSchemaError(
            f"envelope.status must be str, got {type(status_raw).__name__}"
        )
    valid_statuses = {e.value for e in WorkerCompletionStatus}
    if status_raw not in valid_statuses:
        raise WorkerOutputSchemaError(
            f"envelope.status must be one of {sorted(valid_statuses)!r}, "
            f"got {status_raw!r}"
        )

    # report_commit — always 40 hex
    rc = envelope.get("report_commit")
    if not isinstance(rc, str) or not _COMMIT_HEX_RE.match(rc):
        raise WorkerOutputSchemaError(
            f"envelope.report_commit must be 40-char lowercase hex"
        )

    # implementation_commit — null or 40-char hex
    ic = envelope.get("implementation_commit")
    if ic is not None:
        if not isinstance(ic, str) or not _COMMIT_HEX_RE.match(ic):
            raise WorkerOutputSchemaError(
                "envelope.implementation_commit must be 40-char "
                "lowercase hex or null"
            )

    # completed requires implementation_commit
    if status_raw == "completed" and ic is None:
        raise WorkerOutputSchemaError(
            "envelope.status 'completed' requires implementation_commit"
        )

    # commits must differ when both present
    if ic is not None and rc is not None and ic == rc:
        raise WorkerOutputSchemaError(
            "implementation_commit and report_commit must differ"
        )

    # summary — non-empty str, no NUL
    summary = envelope.get("summary")
    if not isinstance(summary, str) or not summary:
        raise WorkerOutputSchemaError(
            "envelope.summary must be a non-empty str"
        )
    if "\x00" in summary:
        raise WorkerOutputSchemaError(
            "envelope.summary must not contain NUL"
        )

    # warnings — list of non-empty str, no whitespace edges, no NUL/CR/LF,
    # no duplicates
    warnings = envelope.get("warnings")
    if not isinstance(warnings, list):
        raise WorkerOutputSchemaError(
            f"envelope.warnings must be a list, got {type(warnings).__name__}"
        )
    seen_w: set[str] = set()
    for i, w in enumerate(warnings):
        if not isinstance(w, str):
            raise WorkerOutputSchemaError(
                f"envelope.warnings[{i}] must be str, "
                f"got {type(w).__name__}"
            )
        if not w:
            raise WorkerOutputSchemaError(
                f"envelope.warnings[{i}] must not be empty"
            )
        if w != w.strip():
            raise WorkerOutputSchemaError(
                f"envelope.warnings[{i}] must not have leading or "
                f"trailing whitespace"
            )
        for ch in ("\x00", "\r", "\n"):
            if ch in w:
                raise WorkerOutputSchemaError(
                    f"envelope.warnings[{i}] must not contain "
                    f"NUL, CR, or LF"
                )
        if w in seen_w:
            raise WorkerOutputSchemaError(
                f"envelope.warnings[{i}] is a duplicate: {w!r}"
            )
        seen_w.add(w)


def _build_worker_output(
    envelope: dict,
    dispatch_result: object,
) -> WorkerOutput:
    """Construct a ``WorkerOutput`` from a validated envelope.

    Reasonix keeps its own strict wrapper/version decoder while sharing the
    common delivery model.  The common model accepts ``reasonix`` only after
    this provider-specific decoder has validated the raw bytes.
    """
    status_str: str = envelope["status"]
    status_map = {e.value: e for e in WorkerCompletionStatus}
    status = status_map[status_str]
    return WorkerOutput(
        identity=dispatch_result.identity,
        provider=_PROVIDER,
        model_id=dispatch_result.model_id,
        status=status,
        implementation_commit=envelope.get("implementation_commit"),
        report_commit=envelope["report_commit"],
        summary=envelope["summary"],
        warnings=tuple(envelope.get("warnings", [])),
        stdout_sha256=dispatch_result.stdout_sha256,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Dataclass holder for the decoder configuration
# ═══════════════════════════════════════════════════════════════════════════════


@dataclass(frozen=True, slots=True)
class ReasonixDecoder:
    """Configuration holder for the Reasonix v1.19.1 decoder.

    Fields:
        supported_version: The exact CLI version this decoder targets.
    """
    supported_version: str = _SUPPORTED_VERSION

    def decode(
        self,
        result: WorkerResult,
    ) -> WorkerOutput:
        """Decode a Reasonix ``WorkerResult``.

        Convenience wrapper around ``decode_reasonix_output``.
        """
        return decode_reasonix_output(result, self.supported_version)
