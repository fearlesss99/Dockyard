"""RateLimitService — provider-neutral, stateless, pure-function policy evaluator (TC-13.14b).

A deterministic, in-memory service that evaluates rate-limit signals and
produces a typed decision: allow, wait, or fail-closed.  It owns zero state,
zero I/O, zero subprocess execution, and zero network access.

Frozen contract: §2.20 of ``001-mad-agentdesk-integration.md`` (TC-13.14a.2).
Production implementation: TC-13.14b.
"""

from __future__ import annotations

import enum
import math
from dataclasses import dataclass
from datetime import datetime, timezone

__all__ = [
    "RateLimitSignal",
    "RateLimitScope",
    "RateLimitSignalSource",
    "RateLimitCheckRequest",
    "RateLimitDecision",
    "RateLimitAction",
    "RateLimitReason",
    "RateLimitService",
    "RateLimitError",
    "RateLimitInputError",
    "RateLimitStateError",
    "RateLimitSecurityError",
]


# -- private helpers ---------------------------------------------------------


def _safe_type_name(value: object) -> str:
    """Return ``type(value).__name__`` — never ``repr()`` or ``str()``."""
    return type(value).__name__


def _validate_provider_string(value: object, field_name: str) -> str:
    """Validate a provider string: non-empty, no whitespace/NUL/CR/LF."""
    if not isinstance(value, str):
        raise RateLimitInputError(
            f"{field_name} must be a str, got {_safe_type_name(value)}"
        )
    if not value:
        raise RateLimitInputError(f"{field_name} must be non-empty")
    if value != value.strip():
        raise RateLimitInputError(
            f"{field_name} must not have leading/trailing whitespace"
        )
    if "\x00" in value or "\r" in value or "\n" in value:
        raise RateLimitInputError(
            f"{field_name} must not contain NUL, CR, or LF"
        )
    return value


def _validate_utc_datetime(value: object, field_name: str) -> datetime:
    """Validate a UTC-aware datetime with offset 0."""
    if not isinstance(value, datetime):
        raise RateLimitInputError(
            f"{field_name} must be a datetime, got {_safe_type_name(value)}"
        )
    if value.tzinfo is None:
        raise RateLimitInputError(
            f"{field_name} must be timezone-aware UTC"
        )
    if value.utcoffset() is None or value.utcoffset().total_seconds() != 0:
        raise RateLimitInputError(
            f"{field_name} must have UTC offset 0"
        )
    return value


def _validate_non_negative_int(
    value: object, field_name: str, *, allow_none: bool = False,
) -> int | None:
    """Validate a non-negative, non-bool integer (or None if allowed)."""
    if allow_none and value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise RateLimitInputError(
            f"{field_name} must be an int, got {_safe_type_name(value)}"
        )
    if value < 0:
        raise RateLimitInputError(
            f"{field_name} must be non-negative"
        )
    return value


# -- enums -------------------------------------------------------------------


@enum.unique
class RateLimitScope(str, enum.Enum):
    """What kind of limit the signal refers to."""

    REQUEST = "request"
    TOKEN = "token"
    CONCURRENCY = "concurrency"
    UNKNOWN = "unknown"

    def __str__(self) -> str:
        return self.value


@enum.unique
class RateLimitSignalSource(str, enum.Enum):
    """Where the rate-limit signal originated."""

    PROVIDER_429 = "provider_429"
    BUDGET_THROTTLE = "budget_throttle"
    MANUAL = "manual"

    def __str__(self) -> str:
        return self.value


@enum.unique
class RateLimitAction(str, enum.Enum):
    """What the caller should do."""

    ALLOW = "allow"
    WAIT = "wait"
    FAIL_CLOSED = "fail_closed"

    def __str__(self) -> str:
        return self.value


@enum.unique
class RateLimitReason(str, enum.Enum):
    """Typed reason for the decision — no arbitrary user strings."""

    NO_SIGNALS = "no_signals"
    WITHIN_LIMIT = "within_limit"
    RETRY_AFTER = "retry_after"
    INSUFFICIENT = "insufficient"
    EXHAUSTED = "exhausted"

    def __str__(self) -> str:
        return self.value


# -- exception hierarchy -----------------------------------------------------


class RateLimitError(Exception):
    """Base for all RateLimit errors."""


class RateLimitInputError(RateLimitError):
    """Invalid input — wrong type, missing field, non-UTC datetime."""


class RateLimitStateError(RateLimitError):
    """Inconsistent state — conflicting signals, invalid combination."""


class RateLimitSecurityError(RateLimitError):
    """Security boundary violation — unsafe content rejected."""


# -- data classes ------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RateLimitSignal:
    """A single, immutable, Provider-neutral rate-limit observation."""

    provider: str
    scope: RateLimitScope
    observed_at: datetime
    retry_after_seconds: int | None
    reset_at: datetime | None
    limit: int | None
    remaining: int | None
    source: RateLimitSignalSource

    def __post_init__(self) -> None:
        # -- provider --
        _validate_provider_string(self.provider, "provider")

        # -- scope --
        if not isinstance(self.scope, RateLimitScope):
            raise RateLimitInputError(
                f"scope must be a RateLimitScope member, "
                f"got {_safe_type_name(self.scope)}"
            )

        # -- observed_at --
        _validate_utc_datetime(self.observed_at, "observed_at")

        # -- retry_after_seconds --
        _validate_non_negative_int(
            self.retry_after_seconds, "retry_after_seconds", allow_none=True,
        )

        # -- reset_at --
        if self.reset_at is not None:
            _validate_utc_datetime(self.reset_at, "reset_at")
            if self.reset_at < self.observed_at:
                raise RateLimitInputError(
                    "reset_at must be >= observed_at"
                )

        # -- limit --
        _validate_non_negative_int(self.limit, "limit", allow_none=True)

        # -- remaining --
        _validate_non_negative_int(self.remaining, "remaining", allow_none=True)

        # -- remaining <= limit when both present --
        if self.limit is not None and self.remaining is not None:
            if self.remaining > self.limit:
                raise RateLimitInputError(
                    "remaining must be <= limit"
                )

        # -- source --
        if not isinstance(self.source, RateLimitSignalSource):
            raise RateLimitInputError(
                f"source must be a RateLimitSignalSource member, "
                f"got {_safe_type_name(self.source)}"
            )


@dataclass(frozen=True, slots=True)
class RateLimitCheckRequest:
    """Immutable input for a single rate-limit evaluation."""

    provider: str
    scope: RateLimitScope
    now: datetime
    units_requested: int
    signals: tuple[RateLimitSignal, ...]

    def __post_init__(self) -> None:
        # -- provider --
        _validate_provider_string(self.provider, "provider")

        # -- scope --
        if not isinstance(self.scope, RateLimitScope):
            raise RateLimitInputError(
                f"scope must be a RateLimitScope member, "
                f"got {_safe_type_name(self.scope)}"
            )

        # -- now --
        _validate_utc_datetime(self.now, "now")

        # -- units_requested --
        if isinstance(self.units_requested, bool) or not isinstance(
            self.units_requested, int
        ):
            raise RateLimitInputError(
                f"units_requested must be an int, "
                f"got {_safe_type_name(self.units_requested)}"
            )
        if self.units_requested < 1:
            raise RateLimitInputError(
                "units_requested must be >= 1"
            )

        # -- signals --
        if not isinstance(self.signals, tuple):
            raise RateLimitInputError(
                f"signals must be a tuple, got {_safe_type_name(self.signals)}"
            )
        for idx, sig in enumerate(self.signals):
            if not isinstance(sig, RateLimitSignal):
                raise RateLimitInputError(
                    f"signals[{idx}] must be a RateLimitSignal, "
                    f"got {_safe_type_name(sig)}"
                )


@dataclass(frozen=True, slots=True)
class RateLimitDecision:
    """Immutable output of a single rate-limit evaluation."""

    action: RateLimitAction
    wait_seconds: int
    reason: RateLimitReason

    def __post_init__(self) -> None:
        # -- action type --
        if not isinstance(self.action, RateLimitAction):
            raise RateLimitInputError(
                f"action must be a RateLimitAction member, "
                f"got {_safe_type_name(self.action)}"
            )

        # -- reason type --
        if not isinstance(self.reason, RateLimitReason):
            raise RateLimitInputError(
                f"reason must be a RateLimitReason member, "
                f"got {_safe_type_name(self.reason)}"
            )

        # -- wait_seconds type --
        if isinstance(self.wait_seconds, bool) or not isinstance(
            self.wait_seconds, int
        ):
            raise RateLimitInputError(
                f"wait_seconds must be an int, "
                f"got {_safe_type_name(self.wait_seconds)}"
            )
        if self.wait_seconds < 0:
            raise RateLimitInputError(
                "wait_seconds must be non-negative"
            )

        # -- action / reason consistency --
        if self.action is RateLimitAction.ALLOW:
            if self.reason not in (
                RateLimitReason.NO_SIGNALS,
                RateLimitReason.WITHIN_LIMIT,
            ):
                raise RateLimitStateError(
                    "ALLOW must pair with NO_SIGNALS or WITHIN_LIMIT"
                )
            if self.wait_seconds != 0:
                raise RateLimitStateError(
                    "ALLOW must have wait_seconds == 0"
                )
        elif self.action is RateLimitAction.WAIT:
            if self.reason not in (
                RateLimitReason.RETRY_AFTER,
                RateLimitReason.INSUFFICIENT,
            ):
                raise RateLimitStateError(
                    "WAIT must pair with RETRY_AFTER or INSUFFICIENT"
                )
            if self.wait_seconds <= 0:
                raise RateLimitStateError(
                    "WAIT must have wait_seconds > 0"
                )
        elif self.action is RateLimitAction.FAIL_CLOSED:
            if self.reason is not RateLimitReason.EXHAUSTED:
                raise RateLimitStateError(
                    "FAIL_CLOSED must pair with EXHAUSTED"
                )
            if self.wait_seconds != 0:
                raise RateLimitStateError(
                    "FAIL_CLOSED must have wait_seconds == 0"
                )
        else:
            raise AssertionError(
                "unreachable: unknown RateLimitAction"
            )


# -- service -----------------------------------------------------------------


class RateLimitService:
    """Deterministic, stateless, pure-function rate-limit policy evaluator."""

    __slots__ = ()

    def evaluate(
        self,
        request: RateLimitCheckRequest,
    ) -> RateLimitDecision:
        """Evaluate the rate-limit decision for *request*.

        Args:
            request: Frozen input carrying provider, scope, now,
                units_requested, and signals.

        Returns:
            A ``RateLimitDecision`` with action, wait_seconds, and reason.

        Raises:
            RateLimitInputError: Invalid request type or field values.
            RateLimitStateError: Future observation or inconsistent state.
        """
        if not isinstance(request, RateLimitCheckRequest):
            raise RateLimitInputError(
                f"request must be a RateLimitCheckRequest, "
                f"got {_safe_type_name(request)}"
            )

        # -- Step 1: Input validation is done by __post_init__ --

        # -- Step 2: Provider and scope filter --
        matching = [
            sig for sig in request.signals
            if sig.provider == request.provider
            and (
                request.scope is RateLimitScope.UNKNOWN
                or sig.scope is request.scope
                or sig.scope is RateLimitScope.UNKNOWN
            )
        ]

        if not matching:
            return RateLimitDecision(
                action=RateLimitAction.ALLOW,
                wait_seconds=0,
                reason=RateLimitReason.NO_SIGNALS,
            )

        # -- Step 3: Future observation rejection --
        for sig in matching:
            if sig.observed_at > request.now:
                raise RateLimitStateError(
                    "signal observed_at is in the future relative to request.now"
                )

        # -- Step 4: Per-signal normalization --
        wait_candidates: list[tuple[int, bool]] = []  # (effective_wait, has_retry)
        fail_candidates: list[None] = []
        allow_candidates: list[None] = []

        for sig in matching:
            retry_wait: int | None = None
            reset_wait: int | None = None
            has_time_info: bool = False

            if sig.retry_after_seconds is not None:
                has_time_info = True
                elapsed = (request.now - sig.observed_at).total_seconds()
                retry_wait = max(
                    0,
                    math.ceil(sig.retry_after_seconds - elapsed),
                )

            if sig.reset_at is not None:
                has_time_info = True
                reset_wait = max(
                    0,
                    math.ceil((sig.reset_at - request.now).total_seconds()),
                )

            effective_wait: int = 0
            if retry_wait is not None or reset_wait is not None:
                rw = retry_wait if retry_wait is not None else 0
                rsw = reset_wait if reset_wait is not None else 0
                effective_wait = max(rw, rsw)

            # Signal expiry: has time info but all expired
            if has_time_info and effective_wait == 0:
                # Expired signal — excluded from remaining/exhausted judgments
                continue

            if effective_wait > 0:
                has_retry = retry_wait is not None and retry_wait > 0
                wait_candidates.append((effective_wait, has_retry))
            elif (
                sig.remaining is None
                or sig.remaining == 0
                or sig.remaining < request.units_requested
            ):
                fail_candidates.append(None)
            else:
                allow_candidates.append(None)

        # -- Step 5: Aggregate candidates --
        if wait_candidates:
            max_wait = max(w for w, _ in wait_candidates)
            any_retry = any(has_retry for _, has_retry in wait_candidates)
            reason = (
                RateLimitReason.RETRY_AFTER
                if any_retry
                else RateLimitReason.INSUFFICIENT
            )
            return RateLimitDecision(
                action=RateLimitAction.WAIT,
                wait_seconds=max_wait,
                reason=reason,
            )

        if fail_candidates:
            return RateLimitDecision(
                action=RateLimitAction.FAIL_CLOSED,
                wait_seconds=0,
                reason=RateLimitReason.EXHAUSTED,
            )

        if allow_candidates:
            return RateLimitDecision(
                action=RateLimitAction.ALLOW,
                wait_seconds=0,
                reason=RateLimitReason.WITHIN_LIMIT,
            )

        # All matching signals expired
        return RateLimitDecision(
            action=RateLimitAction.ALLOW,
            wait_seconds=0,
            reason=RateLimitReason.NO_SIGNALS,
        )
