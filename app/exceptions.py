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


class ConflictError(AppException):
    status_code = 409
    code = "conflict"
    message = "Resource already exists"


class PayloadTooLargeError(AppException):
    status_code = 413
    code = "payload_too_large"
    message = "Upload is too large"


class RunnerBusyError(AppException):
    status_code = 503
    code = "runner_busy"
    message = "Runner is at capacity"
