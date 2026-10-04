"""应用服务的业务行为测试：政策换版、消费追溯、退回、冻结、延期、定时到期。"""
from __future__ import annotations

import itertools
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from points_governance.clock import MutableClock
from points_governance.errors import InsufficientPointsError, InvalidStateError, NotFoundError, ValidationError
from points_governance.models import BatchStatus, CarryoverRule, EntryKind, GrantSource
from points_governance.service import PointsService

UTC = timezone.utc
START = datetime(2026, 1, 1, tzinfo=UTC)


def make_service() -> tuple[PointsService, MutableClock]:
    clock = MutableClock(START)
    counter = itertools.count(1)
    service = PointsService(clock=clock, id_factory=lambda kind: f"{kind}-{next(counter):04d}")
    return service, clock


def publish_default_policy(service: PointsService, **overrides):
    options = {
        "name": "2026 年度政策",
        "effective_from": START,
        "default_validity_days": 365,
        "default_carryover": CarryoverRule(allowed=False),
    }
    options.update(overrides)
    return service.publish_policy(**options)


class PolicyTest(unittest.TestCase):
    def test_grant_snapshots_policy_and_version_change_keeps_existing_batches(self) -> None:
        service, _ = make_service()
        old_policy = publish_default_policy(
            service,
            name="2025 政策",
            default_validity_days=180,
            default_carryover=CarryoverRule(allowed=True, cap_permille=500, validity_days=90),
        )
        old_batch = service.grant(account_id="ent-1", amount=1000, effective_from=START)
        self.assertEqual(old_batch.policy_id, old_policy.policy_id)
        self.assertEqual(old_batch.expires_at, START + timedelta(days=180))
        self.assertEqual(old_batch.carryover_rule, CarryoverRule(allowed=True, cap_permille=500, validity_days=90))

        # 政策换版：新版本只影响之后创建的批次
        new_policy = publish_default_policy(
            service,
            name="2026 政策",
            default_validity_days=365,
            default_carryover=CarryoverRule(allowed=False),
        )
        new_batch = service.grant(account_id="ent-1", amount=1000, effective_from=START)
        self.assertEqual(new_batch.policy_id, new_policy.policy_id)
        self.assertEqual(new_batch.expires_at, START + timedelta(days=365))
        self.assertFalse(new_batch.carryover_rule.allowed)

        # 既有批次不被换版改写
        self.assertEqual(old_batch.expires_at, START + timedelta(days=180))
        self.assertTrue(old_batch.carryover_rule.allowed)
        self.assertEqual(old_batch.carryover_rule.cap_permille, 500)

    def test_grant_requires_expiry_when_no_policy(self) -> None:
        service, _ = make_service()
        with self.assertRaises(ValidationError):
            service.grant(account_id="ent-1", amount=100, effective_from=START)
        batch = service.grant(
            account_id="ent-1",
            amount=100,
            effective_from=START,
            expires_at=START + timedelta(days=30),
        )
        self.assertEqual(batch.expires_at, START + timedelta(days=30))

    def test_grant_rejects_invalid_arguments(self) -> None:
        service, _ = make_service()
        publish_default_policy(service)
        with self.assertRaises(ValidationError):
            service.grant(account_id="ent-1", amount=0, effective_from=START)
        with self.assertRaises(ValidationError):
            service.grant(
                account_id="ent-1",
                amount=100,
                effective_from=START,
                expires_at=START - timedelta(days=1),
            )
        with self.assertRaises(NotFoundError):
            service.grant(account_id="ent-1", amount=100, effective_from=START, policy_id="policy-x")


class ConsumeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service, self.clock = make_service()
        publish_default_policy(self.service)

    def test_consume_allocates_by_earliest_expiry_and_keeps_detail(self) -> None:
        near = self.service.grant(
            account_id="ent-1",
            amount=100,
            effective_from=START,
            expires_at=START + timedelta(days=30),
        )
        far = self.service.grant(
            account_id="ent-1",
            amount=100,
            effective_from=START,
            expires_at=START + timedelta(days=300),
        )
        consumption = self.service.consume(account_id="ent-1", amount=150, scope="general")
        self.assertEqual(
            [(item.batch_id, item.amount) for item in consumption.allocations],
            [(near.batch_id, 100), (far.batch_id, 50)],
        )
        self.assertEqual(sum(item.amount for item in consumption.allocations), 150)
        self.assertEqual(near.remaining(), 0)
        self.assertEqual(far.remaining(), 50)
        # 分配明细可追溯：消费记录与账本都保留明细
        fetched = self.service.get_consumption(consumption.consumption_id)
        self.assertEqual(fetched, consumption)
        consume_entries = [e for e in self.service.ledger_of("ent-1") if e.kind == EntryKind.CONSUME]
        self.assertEqual(len(consume_entries), 1)
        self.assertEqual(consume_entries[0].detail, consumption.allocations)

    def test_consume_is_idempotent_by_request_id(self) -> None:
        self.service.grant(account_id="ent-1", amount=100, effective_from=START)
        first = self.service.consume(account_id="ent-1", amount=40, request_id="req-1")
        second = self.service.consume(account_id="ent-1", amount=40, request_id="req-1")
        self.assertEqual(first.consumption_id, second.consumption_id)
        self.assertEqual(self.service.balance("ent-1").total_remaining, 60)

    def test_insufficient_points_consumes_nothing(self) -> None:
        batch = self.service.grant(account_id="ent-1", amount=100, effective_from=START)
        with self.assertRaises(InsufficientPointsError) as ctx:
            self.service.consume(account_id="ent-1", amount=150)
        self.assertEqual(ctx.exception.available, 100)
        self.assertEqual(batch.remaining(), 100)
        self.assertEqual(self.service.list_consumptions("ent-1"), [])


class RefundTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service, self.clock = make_service()
        publish_default_policy(self.service)

    def test_partial_refund_returns_proportionally_and_keeps_consumption(self) -> None:
        near = self.service.grant(
            account_id="ent-1", amount=100, effective_from=START, expires_at=START + timedelta(days=30)
        )
        far = self.service.grant(
            account_id="ent-1", amount=300, effective_from=START, expires_at=START + timedelta(days=300)
        )
        consumption = self.service.consume(account_id="ent-1", amount=200)  # 100 + 100
        refund = self.service.refund(consumption_id=consumption.consumption_id, amount=100)
        # 按原分配 1:1 比例退回
        self.assertEqual(
            {(item.batch_id, item.amount) for item in refund.allocations},
            {(near.batch_id, 50), (far.batch_id, 50)},
        )
        self.assertEqual(near.remaining(), 50)
        self.assertEqual(far.remaining(), 250)
        # 既有消费记录不被修改
        self.assertEqual(self.service.get_consumption(consumption.consumption_id), consumption)
        self.assertEqual(consumption.amount, 200)

    def test_refund_share_rounding_is_deterministic(self) -> None:
        first = self.service.grant(
            account_id="ent-1", amount=1, effective_from=START, expires_at=START + timedelta(days=30)
        )
        second = self.service.grant(
            account_id="ent-1", amount=1, effective_from=START, expires_at=START + timedelta(days=60)
        )
        third = self.service.grant(
            account_id="ent-1", amount=1, effective_from=START, expires_at=START + timedelta(days=90)
        )
        consumption = self.service.consume(account_id="ent-1", amount=3)
        refund = self.service.refund(consumption_id=consumption.consumption_id, amount=2)
        # 每笔 floor(1*2/3)=0，余数 2 按原分配顺序补给先扣的两个批次
        self.assertEqual(
            [(item.batch_id, item.amount) for item in refund.allocations],
            [(first.batch_id, 1), (second.batch_id, 1)],
        )
        self.assertEqual(third.remaining(), 0)

    def test_over_refund_rejected(self) -> None:
        self.service.grant(account_id="ent-1", amount=100, effective_from=START)
        consumption = self.service.consume(account_id="ent-1", amount=60)
        self.service.refund(consumption_id=consumption.consumption_id, amount=40)
        with self.assertRaises(ValidationError):
            self.service.refund(consumption_id=consumption.consumption_id, amount=30)

    def test_refund_to_expired_batch_creates_compensation_batch(self) -> None:
        batch = self.service.grant(
            account_id="ent-1",
            amount=100,
            effective_from=START,
            expires_at=START + timedelta(days=30),
        )
        consumption = self.service.consume(account_id="ent-1", amount=60)
        self.clock.advance(days=31)
        self.service.run_expiration()
        self.assertEqual(batch.status, BatchStatus.EXPIRED)
        refund = self.service.refund(consumption_id=consumption.consumption_id, amount=60)
        target = refund.allocations[0]
        self.assertNotEqual(target.batch_id, batch.batch_id)
        compensation = self.service.list_batches("ent-1")[-1]
        self.assertEqual(compensation.batch_id, target.batch_id)
        self.assertEqual(compensation.source, GrantSource.COMPENSATION)
        self.assertEqual(compensation.remaining(), 60)
        # 原批次账面不受影响
        self.assertEqual(batch.remaining(), 0)


class FreezeExtensionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service, self.clock = make_service()
        publish_default_policy(self.service)

    def test_freeze_and_unfreeze(self) -> None:
        batch = self.service.grant(account_id="ent-1", amount=100, effective_from=START)
        self.service.freeze(batch_id=batch.batch_id, amount=40, reason="风控核查")
        self.assertEqual(batch.available(), 60)
        with self.assertRaises(ValidationError):
            self.service.freeze(batch_id=batch.batch_id, amount=70)
        with self.assertRaises(ValidationError):
            self.service.unfreeze(batch_id=batch.batch_id, amount=50)
        self.service.unfreeze(batch_id=batch.batch_id, amount=40)
        self.assertEqual(batch.available(), 100)
        kinds = [entry.kind for entry in self.service.ledger_of("ent-1")]
        self.assertIn(EntryKind.FREEZE, kinds)
        self.assertIn(EntryKind.UNFREEZE, kinds)

    def test_freeze_does_not_touch_existing_consumption(self) -> None:
        batch = self.service.grant(account_id="ent-1", amount=100, effective_from=START)
        consumption = self.service.consume(account_id="ent-1", amount=30)
        self.service.freeze(batch_id=batch.batch_id, amount=70)
        self.assertEqual(self.service.get_consumption(consumption.consumption_id), consumption)
        self.assertEqual(batch.available(), 0)
        with self.assertRaises(InsufficientPointsError):
            self.service.consume(account_id="ent-1", amount=1)

    def test_extension_delays_expiry_and_keeps_consumption(self) -> None:
        batch = self.service.grant(
            account_id="ent-1",
            amount=100,
            effective_from=START,
            expires_at=START + timedelta(days=30),
        )
        consumption = self.service.consume(account_id="ent-1", amount=20)
        self.service.extend(batch_id=batch.batch_id, days=60, approved_by="核算专员", reason="政策衔接")
        self.assertEqual(batch.effective_expiry(), START + timedelta(days=90))
        self.clock.advance(days=31)  # 原到期日已过，延期后仍有效
        report = self.service.run_expiration()
        self.assertEqual(report.expired, ())
        follow_up = self.service.consume(account_id="ent-1", amount=10)
        self.assertEqual(follow_up.allocations[0].batch_id, batch.batch_id)
        # 延期不修改既有消费
        self.assertEqual(self.service.get_consumption(consumption.consumption_id), consumption)

    def test_extension_on_expired_batch_rejected(self) -> None:
        batch = self.service.grant(
            account_id="ent-1",
            amount=100,
            effective_from=START,
            expires_at=START + timedelta(days=30),
        )
        self.clock.advance(days=31)
        self.service.run_expiration()
        with self.assertRaises(InvalidStateError):
            self.service.extend(batch_id=batch.batch_id, days=10, approved_by="核算专员")


class ExpirationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service, self.clock = make_service()
        publish_default_policy(self.service)

    def test_expiration_uses_injected_clock_and_is_idempotent(self) -> None:
        batch = self.service.grant(
            account_id="ent-1",
            amount=100,
            effective_from=START,
            expires_at=START + timedelta(days=30),
        )
        self.service.consume(account_id="ent-1", amount=40)
        self.clock.advance(days=31)
        report = self.service.run_expiration()
        self.assertEqual(report.total_expired, 60)
        self.assertEqual(batch.status, BatchStatus.EXPIRED)
        self.assertEqual(batch.remaining(), 0)
        self.assertEqual(self.service.balance("ent-1").total_remaining, 0)
        # 重复运行：同一时刻与更晚时刻都不再产生新效果
        again = self.service.run_expiration()
        self.assertEqual(again.expired, ())
        self.clock.advance(days=30)
        third = self.service.run_expiration()
        self.assertEqual(third.expired, ())
        self.assertEqual(batch.remaining(), 0)
        expire_entries = [e for e in self.service.ledger_of("ent-1") if e.kind == EntryKind.EXPIRE]
        self.assertEqual(len(expire_entries), 1)

    def test_expiration_respects_extension(self) -> None:
        batch = self.service.grant(
            account_id="ent-1",
            amount=100,
            effective_from=START,
            expires_at=START + timedelta(days=30),
        )
        self.service.extend(batch_id=batch.batch_id, days=30, approved_by="核算专员")
        self.clock.advance(days=31)
        self.assertEqual(self.service.run_expiration().expired, ())
        self.clock.advance(days=30)
        report = self.service.run_expiration()
        self.assertEqual([item.batch_id for item in report.expired], [batch.batch_id])

    def test_carryover_rule_moves_remaining_to_new_batch(self) -> None:
        publish_default_policy(
            self.service,
            name="可结转政策",
            effective_from=START,
            default_validity_days=30,
            default_carryover=CarryoverRule(allowed=True, cap_permille=500, validity_days=90),
        )
        batch = self.service.grant(account_id="ent-1", amount=101, effective_from=START)
        self.clock.advance(days=31)
        report = self.service.run_expiration()
        # 101 * 500 // 1000 = 50 结转，51 作废
        self.assertEqual(report.total_carried, 50)
        self.assertEqual(report.total_expired, 51)
        info = report.expired[0]
        self.assertIsNotNone(info.carryover_batch_id)
        carryover = [b for b in self.service.list_batches("ent-1") if b.batch_id == info.carryover_batch_id][0]
        self.assertEqual(carryover.source, GrantSource.CARRYOVER)
        self.assertEqual(carryover.remaining(), 50)
        # 结转批次自任务运行时刻（START + 31 天）起算 90 天有效期
        self.assertEqual(carryover.effective_from, START + timedelta(days=31))
        self.assertEqual(carryover.expires_at, START + timedelta(days=121))
        self.assertEqual(batch.carried_out, 50)
        self.assertEqual(batch.remaining(), 0)
        # 结转只发生一次
        self.assertEqual(self.service.run_expiration().expired, ())
        self.assertEqual(len(self.service.list_batches("ent-1")), 2)

    def test_late_run_does_not_cascade_expire_carryover_batches(self) -> None:
        # 任务远晚于到期日运行时，结转批次出生时不应已过期
        publish_default_policy(
            self.service,
            name="可结转政策",
            effective_from=START,
            default_validity_days=30,
            default_carryover=CarryoverRule(allowed=True, cap_permille=500, validity_days=30),
        )
        self.service.grant(account_id="ent-1", amount=100, effective_from=START)
        self.clock.advance(days=100)  # 远超原到期日与结转有效期
        first = self.service.run_expiration()
        self.assertEqual(first.total_carried, 50)
        second = self.service.run_expiration()
        self.assertEqual(second.expired, ())  # 同一时刻重复运行无新效果
        self.assertEqual(len(self.service.list_batches("ent-1")), 2)


class QueryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service, self.clock = make_service()
        publish_default_policy(self.service)

    def test_balance_composition(self) -> None:
        near = self.service.grant(
            account_id="ent-1", amount=100, effective_from=START, expires_at=START + timedelta(days=30)
        )
        far = self.service.grant(
            account_id="ent-1", amount=200, effective_from=START, expires_at=START + timedelta(days=300)
        )
        self.service.freeze(batch_id=far.batch_id, amount=50)
        self.service.consume(account_id="ent-1", amount=80)
        balance = self.service.balance("ent-1")
        self.assertEqual(balance.total_remaining, 220)
        self.assertEqual(balance.total_available, 170)
        by_id = {item.batch_id: item for item in balance.batches}
        self.assertEqual(by_id[near.batch_id].remaining, 20)
        self.assertEqual(by_id[far.batch_id].frozen, 50)
        self.assertEqual(by_id[far.batch_id].available, 150)

    def test_expiry_forecast_groups_by_expiry_date(self) -> None:
        self.service.grant(
            account_id="ent-1", amount=100, effective_from=START, expires_at=START + timedelta(days=10)
        )
        self.service.grant(
            account_id="ent-1", amount=200, effective_from=START, expires_at=START + timedelta(days=10)
        )
        self.service.grant(
            account_id="ent-1", amount=300, effective_from=START, expires_at=START + timedelta(days=40)
        )
        self.service.grant(
            account_id="ent-1", amount=400, effective_from=START, expires_at=START + timedelta(days=400)
        )
        forecast = self.service.expiry_forecast("ent-1", horizon_days=90)
        self.assertEqual(forecast.total_expiring, 600)
        self.assertEqual(
            [(bucket.date, bucket.amount) for bucket in forecast.buckets],
            [
                ((START + timedelta(days=10)).date().isoformat(), 300),
                ((START + timedelta(days=40)).date().isoformat(), 300),
            ],
        )
        self.assertEqual(len(forecast.buckets[0].batches), 2)
        # 已到期未处理的批次归入当日桶
        self.clock.advance(days=15)
        forecast = self.service.expiry_forecast("ent-1", horizon_days=90)
        self.assertEqual(forecast.buckets[0].date, self.clock.now().date().isoformat())
        self.assertEqual(forecast.buckets[0].amount, 300)


if __name__ == "__main__":
    unittest.main()
