"""Domain exceptions.

Every failure the agent can hit has its own type, because the caller's reaction
differs by kind: a rate limit is retried, a bad tool input is sent back to the
model to fix, a step-limit breach stops the run. A single generic exception would
force every caller to parse message strings to tell them apart.
"""

from __future__ import annotations


class LeadflowError(Exception):
    """Base class, so callers can catch everything this package raises in one place."""


class ConfigError(LeadflowError):
    """Settings are missing or invalid. Raised at startup, never mid-run."""


class HubSpotError(LeadflowError):
    """HubSpot rejected a request.

    `retryable` separates transient failures (429, 5xx) from permanent ones
    (400, 401, 403, 404). Retrying a permanent failure only burns rate limit.
    """

    def __init__(self, message: str, *, status_code: int | None, retryable: bool) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retryable = retryable


class LeadRejectedError(LeadflowError):
    """The enquiry cannot be processed as given (empty or too long). Raised before any model call."""
