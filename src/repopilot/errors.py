"""Errors that may safely cross RepoPilot's HTTP seam."""

from __future__ import annotations


class RepoPilotError(Exception):
    """Base class for expected, user-visible failures."""

    code = "repopilot_error"
    status_code = 500

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class InvalidRepositoryError(RepoPilotError):
    code = "invalid_repository"
    status_code = 422


class RepositoryNotFoundError(RepoPilotError):
    code = "repository_not_found"
    status_code = 404


class RepositoryAccessError(RepoPilotError):
    code = "repository_access_denied"
    status_code = 403


class RepositoryUpstreamError(RepoPilotError):
    code = "repository_upstream_error"
    status_code = 502


class RepositoryRateLimitedError(RepoPilotError):
    code = "repository_rate_limited"
    status_code = 503


class RepositoryTimeoutError(RepoPilotError):
    code = "repository_timeout"
    status_code = 504


class InspectionLimitExceededError(RepoPilotError):
    code = "inspection_limit_exceeded"
    status_code = 413


class UnsupportedRepositoryError(RepoPilotError):
    code = "unsupported_repository"
    status_code = 422


class IssueRepositoryMismatchError(RepoPilotError):
    code = "issue_repository_mismatch"
    status_code = 422


class PlanNotFoundError(RepoPilotError):
    code = "plan_not_found"
    status_code = 404


class PlanVersionConflictError(RepoPilotError):
    code = "plan_version_conflict"
    status_code = 409


class InvalidPlanTransitionError(RepoPilotError):
    code = "invalid_plan_transition"
    status_code = 409


class StoredPlanCorruptError(RepoPilotError):
    code = "stored_plan_corrupt"
    status_code = 500
