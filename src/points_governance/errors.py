"""领域错误类型。"""
from __future__ import annotations


class PointsError(Exception):
    """所有可预期业务错误的基类（API 层映射为 4xx）。"""

    code = "domain_error"
    http_status = 400

    def __init__(self, message: str, *, details: dict | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}

    def to_body(self) -> dict:
        body = {"error": self.code, "message": self.message}
        if self.details:
            body["details"] = self.details
        return body


class NotFoundError(PointsError):
    code = "not_found"
    http_status = 404


class ConflictError(PointsError):
    code = "conflict"
    http_status = 409


class ValidationError(PointsError):
    code = "validation_error"
    http_status = 422


class InsufficientBalanceError(PointsError):
    code = "insufficient_balance"
    http_status = 409

    def __init__(self, message: str, *, requested: int, available: int,
                 candidates: list[dict] | None = None) -> None:
        super().__init__(message, details={
            "requested": requested,
            "available": available,
            "candidates": candidates or [],
        })
        self.requested = requested
        self.available = available


class PolicyStateError(ConflictError):
    code = "policy_state_error"
