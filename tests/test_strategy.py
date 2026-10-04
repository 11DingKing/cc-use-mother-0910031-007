"""确定性扣减策略的单元测试。"""
from __future__ import annotations

import random
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from points_governance.errors import InsufficientPointsError, ValidationError
from points_governance.models import BatchStatus, CarryoverRule, GrantSource, PointBatch
from points_governance.strategy import select_batches

UTC = timezone.utc
BASE = datetime(2026, 1, 1, tzinfo=UTC)


def make_batch(
    batch_id: str,
    *,
    amount: int = 100,
    effective_from: datetime = BASE,
    expires_at: datetime,
    scopes: tuple[str, ...] = (),
    frozen: int = 0,
    status: BatchStatus = BatchStatus.ACTIVE,
) -> PointBatch:
    return PointBatch(
        batch_id=batch_id,
        account_id="acc",
        source=GrantSource.ANNUAL_GRANT,
        scopes=frozenset(scopes),
        effective_from=effective_from,
        expires_at=expires_at,
        extension_days=0,
        carryover_rule=CarryoverRule(),
        policy_id="policy-1",
        initial_amount=amount,
        created_at=BASE,
        frozen=frozen,
        status=status,
    )


class SelectBatchesTest(unittest.TestCase):
    def test_earliest_expiry_first(self) -> None:
        later = make_batch("batch-late", expires_at=BASE + timedelta(days=300))
        sooner = make_batch("batch-soon", expires_at=BASE + timedelta(days=30))
        result = select_batches([later, sooner], account_id="acc", scope="general", at=BASE, amount=150)
        self.assertEqual(
            [(item.batch_id, item.amount) for item in result],
            [("batch-soon", 100), ("batch-late", 50)],
        )

    def test_tie_break_is_deterministic(self) -> None:
        expiry = BASE + timedelta(days=90)
        first = make_batch("batch-002", expires_at=expiry, effective_from=BASE)
        second = make_batch("batch-001", expires_at=expiry, effective_from=BASE)
        third = make_batch("batch-003", expires_at=expiry, effective_from=BASE + timedelta(days=1))
        at = BASE + timedelta(days=2)  # 三个批次均已生效
        result = select_batches([third, second, first], account_id="acc", scope="general", at=at, amount=250)
        # 到期相同先生效者优先；先生效也相同则按批次编号字典序
        self.assertEqual(
            [(item.batch_id, item.amount) for item in result],
            [("batch-001", 100), ("batch-002", 100), ("batch-003", 50)],
        )

    def test_input_order_does_not_change_result(self) -> None:
        batches = [
            make_batch(f"batch-{index:03d}", expires_at=BASE + timedelta(days=30 * (index + 1)))
            for index in range(8)
        ]
        expected = select_batches(batches, account_id="acc", scope="general", at=BASE, amount=500)
        for seed in range(5):
            shuffled = list(batches)
            random.Random(seed).shuffle(shuffled)
            result = select_batches(shuffled, account_id="acc", scope="general", at=BASE, amount=500)
            self.assertEqual(result, expected)

    def test_scope_filter(self) -> None:
        meal_only = make_batch("batch-meal", scopes=("meal",), expires_at=BASE + timedelta(days=10))
        universal = make_batch("batch-all", expires_at=BASE + timedelta(days=300))
        result = select_batches([meal_only, universal], account_id="acc", scope="transport", at=BASE, amount=50)
        self.assertEqual([(item.batch_id, item.amount) for item in result], [("batch-all", 50)])
        result = select_batches([meal_only, universal], account_id="acc", scope="meal", at=BASE, amount=150)
        self.assertEqual(
            [(item.batch_id, item.amount) for item in result],
            [("batch-meal", 100), ("batch-all", 50)],
        )

    def test_skips_not_effective_and_expired_batches(self) -> None:
        future = make_batch(
            "batch-future",
            effective_from=BASE + timedelta(days=60),
            expires_at=BASE + timedelta(days=90),
        )
        stale = make_batch(
            "batch-stale",
            expires_at=BASE - timedelta(days=1),  # 任务尚未运行但按时钟已到期
        )
        current = make_batch("batch-current", expires_at=BASE + timedelta(days=300))
        result = select_batches([future, stale, current], account_id="acc", scope="general", at=BASE, amount=50)
        self.assertEqual([(item.batch_id, item.amount) for item in result], [("batch-current", 50)])

    def test_frozen_amount_is_excluded(self) -> None:
        frozen = make_batch("batch-frozen", amount=100, frozen=60, expires_at=BASE + timedelta(days=10))
        normal = make_batch("batch-normal", expires_at=BASE + timedelta(days=300))
        result = select_batches([frozen, normal], account_id="acc", scope="general", at=BASE, amount=50)
        self.assertEqual(
            [(item.batch_id, item.amount) for item in result],
            [("batch-frozen", 40), ("batch-normal", 10)],
        )

    def test_insufficient_points_raises_without_partial_allocation(self) -> None:
        batches = [make_batch("batch-1", amount=100, expires_at=BASE + timedelta(days=10))]
        with self.assertRaises(InsufficientPointsError) as ctx:
            select_batches(batches, account_id="acc", scope="general", at=BASE, amount=150)
        self.assertEqual(ctx.exception.available, 100)
        self.assertEqual(ctx.exception.requested, 150)

    def test_non_positive_amount_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            select_batches([], account_id="acc", scope="general", at=BASE, amount=0)


if __name__ == "__main__":
    unittest.main()
