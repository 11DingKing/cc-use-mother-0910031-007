"""领域错误。"""
from __future__ import annotations


class DomainError(Exception):
    """业务规则错误基类。"""


class NotFoundError(DomainError):
    """对象不存在。"""


class ValidationError(DomainError):
    """输入不合法。"""


class InvalidStateError(DomainError):
    """对象当前状态不允许该操作。"""


class InsufficientPointsError(DomainError):
    """可用积分不足。"""

    def __init__(self, account_id: str, requested: int, available: int) -> None:
        super().__init__(f"账户 {account_id} 可用积分不足：请求 {requested}，可用 {available}")
        self.account_id = account_id
        self.requested = requested
        self.available = available
