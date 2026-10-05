"""Typed application errors → structured JSON responses and job error categories."""

from __future__ import annotations

from typing import Any

from scout.db.enums import ErrorCategory


class AppError(Exception):
    status_code = 400
    code = "bad_request"

    def __init__(self, message: str, *, code: str | None = None, hint: str | None = None, **extra: Any):
        super().__init__(message)
        self.message = message
        if code:
            self.code = code
        self.hint = hint
        self.extra = extra

    def to_dict(self) -> dict[str, Any]:
        body: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.hint:
            body["hint"] = self.hint
        if self.extra:
            body["details"] = self.extra
        return {"error": body}


class NotFound(AppError):
    status_code = 404
    code = "not_found"


class Forbidden(AppError):
    status_code = 403
    code = "forbidden"


class Unauthorized(AppError):
    status_code = 401
    code = "unauthorized"


class Conflict(AppError):
    status_code = 409
    code = "conflict"


class ValidationFailed(AppError):
    status_code = 422
    code = "validation_failed"


class BudgetExceeded(AppError):
    status_code = 402
    code = "budget_exceeded"


class ConfirmationRequired(AppError):
    status_code = 409
    code = "confirmation_required"


# ---- job/pipeline errors ---------------------------------------------------------------


class JobError(Exception):
    category: ErrorCategory = ErrorCategory.internal
    retryable = True

    def __init__(self, message: str, *, category: ErrorCategory | None = None):
        super().__init__(message)
        if category is not None:
            self.category = category


class RetryableError(JobError):
    retryable = True


class PermanentError(JobError):
    retryable = False
    category = ErrorCategory.validation


class BlockedError(RetryableError):
    """Anti-bot / 403 / 429: limited retries with long delays, never infinite."""

    category = ErrorCategory.blocked


class RateLimitedError(RetryableError):
    category = ErrorCategory.rate_limited


class FetchError(RetryableError):
    category = ErrorCategory.network


class AIUnavailable(PermanentError):
    category = ErrorCategory.ai


class CampaignPaused(Exception):
    """Raised cooperatively between pipeline stages when the campaign is paused/cancelled."""
