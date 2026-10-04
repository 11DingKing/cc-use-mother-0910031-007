"""存储：内存仓储。

所有数据集中在 InMemoryStore，PointsService 只依赖这一个对象；
需要持久化时用同形状的实现（如 SQLite/PostgreSQL 仓储）替换即可，
服务层与 API 层不需要改动。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .models import (
    Consumption,
    ExpiryExtension,
    LedgerEntry,
    PointBatch,
    PolicyVersion,
    Refund,
)


@dataclass
class InMemoryStore:
    """进程内仓储：业务数据 + 只增账本。"""

    policies: dict[str, PolicyVersion] = field(default_factory=dict)
    batches: dict[str, PointBatch] = field(default_factory=dict)
    consumptions: dict[str, Consumption] = field(default_factory=dict)
    refunds: dict[str, Refund] = field(default_factory=dict)
    extensions: dict[str, ExpiryExtension] = field(default_factory=dict)
    ledger: list[LedgerEntry] = field(default_factory=list)
    # (账户, 请求号) -> 消费号：消费接口的幂等键，防止重复提交重复扣减
    request_index: dict[tuple[str, str], str] = field(default_factory=dict)

    def batches_of(self, account_id: str) -> list[PointBatch]:
        return [batch for batch in self.batches.values() if batch.account_id == account_id]

    def consumptions_of(self, account_id: str) -> list[Consumption]:
        return [item for item in self.consumptions.values() if item.account_id == account_id]

    def refunds_of(self, account_id: str) -> list[Refund]:
        return [item for item in self.refunds.values() if item.account_id == account_id]

    def refunds_of_consumption(self, consumption_id: str) -> list[Refund]:
        return [item for item in self.refunds.values() if item.consumption_id == consumption_id]

    def ledger_of(self, account_id: str) -> list[LedgerEntry]:
        return [entry for entry in self.ledger if entry.account_id == account_id]
