"""确定性扣减策略：先到期先扣（FEFO），平局按生效时间与批次编号。

排序键完全由批次自身字段决定，与传入集合的遍历顺序无关，
因此同样的批次集合与请求在任何时刻、任何节点上都得到同样的分配结果。
"""
from __future__ import annotations

from datetime import datetime
from typing import Iterable

from .errors import InsufficientPointsError, ValidationError
from .models import Allocation, BatchStatus, PointBatch


def sort_key(batch: PointBatch) -> tuple:
    """扣减顺序：到期时刻升序 → 生效时刻升序 → 批次编号字典序。"""
    return (batch.effective_expiry(), batch.effective_from, batch.batch_id)


def eligible_batches(batches: Iterable[PointBatch], *, scope: str, at: datetime) -> list[PointBatch]:
    """筛选可参与扣减的批次：生效中、范围匹配、有可扣额度。"""
    return [
        batch
        for batch in batches
        if batch.status == BatchStatus.ACTIVE
        and batch.effective_from <= at < batch.effective_expiry()
        and (not batch.scopes or scope in batch.scopes)
        and batch.available() > 0
    ]


def select_batches(
    batches: Iterable[PointBatch],
    *,
    account_id: str,
    scope: str,
    at: datetime,
    amount: int,
) -> list[Allocation]:
    """按确定顺序为一次消费挑选批次并生成扣减明细。

    额度不足时不产生任何部分分配，直接抛出 InsufficientPointsError。
    """
    if amount <= 0:
        raise ValidationError("消费数量必须为正数")
    ordered = sorted(eligible_batches(batches, scope=scope, at=at), key=sort_key)
    allocations: list[Allocation] = []
    outstanding = amount
    for batch in ordered:
        if outstanding == 0:
            break
        take = min(batch.available(), outstanding)
        allocations.append(Allocation(batch_id=batch.batch_id, amount=take))
        outstanding -= take
    if outstanding > 0:
        available_total = sum(batch.available() for batch in ordered)
        raise InsufficientPointsError(account_id, requested=amount, available=available_total)
    return allocations
