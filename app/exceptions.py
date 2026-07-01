from typing import Any


class AppException(Exception):
    status_code = 400
    code = "app_error"
    message = "Application Error"

    def __init__(
        self, message: str | None = None, details: dict[str, Any] | None = None
    ):
        self.message = message or self.message
        self.details = details or {}


class NotFoundError(AppException):
    status_code = 404
    code = "not_found"
    message = "Resource not found"


class AuthError(AppException):
    status_code = 401
    code = "unauthorized"
    message = "Authentication required"


class ForbiddenError(AppException):
    status_code = 403
    code = "forbidden"
    message = "Not allowed"


class ConflictError(AppException):
    status_code = 409
    code = "conflict"
    message = "Resource already exists"


class RunnerBusyError(AppException):
    status_code = 503
    code = "runner_busy"
    message = "Runner is at capacity"
