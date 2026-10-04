"""领域服务测试：覆盖契约四大不变量。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from points_governance import (  # noqa: E402
    Database,
    FixedClock,
    PointsService,
)
from points_governance.errors import (  # noqa: E402
    ConflictError,
    InsufficientBalanceError,
    NotFoundError,
    ValidationError,
)

T0 = "2026-01-01T00:00:00Z"


def make_service(start: str = T0) -> PointsService:
    svc = PointsService(Database(":memory:"), FixedClock(start))
    svc.create_policy([
        {"rule_id": "EXPIRE_RULE", "kind": "EXPIRE"},
        {"rule_id": "CARRY_RULE", "kind": "CARRYOVER", "carry_days": 30},
        {"rule_id": "CARRY2", "kind": "CARRYOVER", "carry_days": 15,
         "max_carry_hops": 2},
    ])
    svc.create_account("ACC1", "测试企业")
    return svc


class FEFOTest(unittest.TestCase):
    def setUp(self) -> None:
        self.svc = make_service()

    def test_consumes_nearest_expiry_first(self) -> None:
        # 三批额度：长期、临期、中期 —— FEFO 必须先吃临期。
        far = self.svc.grant_lot("ACC1", 100, source="长期",
                                 expires_at="2027-01-01T00:00:00Z")
        near = self.svc.grant_lot("ACC1", 40, source="临期",
                                  expires_at="2026-02-01T00:00:00Z")
        mid = self.svc.grant_lot("ACC1", 60, source="中期",
                                 expires_at="2026-06-01T00:00:00Z")
        result = self.svc.consume("ACC1", 120)
        self.assertEqual(
            [(a["lot_id"], a["amount"]) for a in result["allocations"]],
            [(near["lot_id"], 40), (mid["lot_id"], 60),
             (far["lot_id"], 20)])
        # 分配序号即确定性扣减顺序
        self.assertEqual([a["seq"] for a in result["allocations"]], [1, 2, 3])
        # 长期批剩余 80，临期/中期清零
        self.assertEqual(self.svc.lots.require(near["lot_id"]).remaining, 0)
        self.assertEqual(self.svc.lots.require(mid["lot_id"]).remaining, 0)
        self.assertEqual(self.svc.lots.require(far["lot_id"]).remaining, 80)

    def test_deterministic_tie_break(self) -> None:
        # 同到期日：按生效时间、创建时间、lot_id 稳定排序。
        a = self.svc.grant_lot("ACC1", 10, source="A", lot_id="LOT-A",
                               expires_at="2026-05-01T00:00:00Z",
                               effective_at="2026-01-01T00:00:00Z")
        b = self.svc.grant_lot("ACC1", 10, source="B", lot_id="LOT-B",
                               expires_at="2026-05-01T00:00:00Z",
                               effective_at="2026-02-01T00:00:00Z")
        result = self.svc.consume("ACC1", 10)
        self.assertEqual(result["allocations"][0]["lot_id"], a["lot_id"])

    def test_scope_isolation(self) -> None:
        self.svc.grant_lot("ACC1", 100, source="专属A", scope="SVC_A",
                           expires_at="2026-12-01T00:00:00Z")
        universal = self.svc.grant_lot("ACC1", 50, source="通用",
                                       expires_at="2027-12-01T00:00:00Z")
        # SVC_B 场景：专属 A 不可用，只能用通用批
        result = self.svc.consume("ACC1", 30, scope="SVC_B")
        self.assertEqual([a["lot_id"] for a in result["allocations"]],
                         [universal["lot_id"]])
        # 专属批消费时优先于通用批（同到期临近程度下）
        near_a = self.svc.grant_lot("ACC1", 20, source="临期专属A",
                                    scope="SVC_A",
                                    expires_at="2026-03-01T00:00:00Z")
        result2 = self.svc.consume("ACC1", 20, scope="SVC_A")
        self.assertEqual(result2["allocations"][0]["lot_id"], near_a["lot_id"])

    def test_insufficient_balance_reports_candidates(self) -> None:
        self.svc.grant_lot("ACC1", 5, source="少量",
                           expires_at="2026-12-01T00:00:00Z")
        with self.assertRaises(InsufficientBalanceError) as ctx:
            self.svc.consume("ACC1", 100)
        self.assertEqual(ctx.exception.requested, 100)
        self.assertEqual(ctx.exception.available, 5)
        self.assertEqual(len(ctx.exception.details["candidates"]), 1)

    def test_not_yet_effective_excluded(self) -> None:
        self.svc.grant_lot("ACC1", 100, source="未来批",
                           effective_at="2026-06-01T00:00:00Z",
                           expires_at="2027-01-01T00:00:00Z")
        with self.assertRaises(InsufficientBalanceError):
            self.svc.consume("ACC1", 1)

    def test_preview_does_not_mutate(self) -> None:
        self.svc.grant_lot("ACC1", 100, source="X",
                           expires_at="2026-12-01T00:00:00Z")
        preview = self.svc.preview_consume("ACC1", 40)
        self.assertTrue(preview["feasible"])
        bal = self.svc.get_balance("ACC1")
        self.assertEqual(bal["available_balance"], 100)


class FreezeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.svc = make_service()
        self.lot = self.svc.grant_lot(
            "ACC1", 100, source="X", expires_at="2026-12-01T00:00:00Z")

    def test_freeze_blocks_consumption(self) -> None:
        self.svc.freeze_lot(self.lot["lot_id"], reason="稽核")
        with self.assertRaises(InsufficientBalanceError):
            self.svc.consume("ACC1", 1)

    def test_unfreeze_restores(self) -> None:
        self.svc.freeze_lot(self.lot["lot_id"])
        self.svc.unfreeze_lot(self.lot["lot_id"])
        result = self.svc.consume("ACC1", 10)
        self.assertEqual(result["allocations"][0]["lot_id"],
                         self.lot["lot_id"])

    def test_double_freeze_rejected(self) -> None:
        self.svc.freeze_lot(self.lot["lot_id"])
        with self.assertRaises(ConflictError):
            self.svc.freeze_lot(self.lot["lot_id"])

    def test_unfreeze_after_expiry_rejected(self) -> None:
        self.svc.freeze_lot(self.lot["lot_id"])
        self.svc.clock.advance(days=400)
        with self.assertRaises(ConflictError):
            self.svc.unfreeze_lot(self.lot["lot_id"])

    def test_frozen_lot_still_expires(self) -> None:
        self.svc.freeze_lot(self.lot["lot_id"])
        self.svc.clock.advance(days=400)
        report = self.svc.run_expiration()
        self.assertEqual(report["lots_expired"], 1)
        self.assertEqual(report["amount_expired"], 100)
        self.assertEqual(
            self.svc.lots.require(self.lot["lot_id"]).status.value, "EXPIRED")


class ExpirationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.svc = make_service()

    def test_expire_sets_remaining_zero(self) -> None:
        lot = self.svc.grant_lot("ACC1", 100, source="作废批",
                                 expires_at="2026-03-01T00:00:00Z",
                                 rule_id="EXPIRE_RULE")
        self.svc.clock.advance(days=60)
        report = self.svc.run_expiration()
        self.assertEqual(report["lots_expired"], 1)
        self.assertEqual(report["amount_expired"], 100)
        got = self.svc.lots.require(lot["lot_id"])
        self.assertEqual(got.status.value, "EXPIRED")
        self.assertEqual(got.remaining, 0)
        self.assertIsNotNone(got.expire_job_id)

    def test_carryover_creates_new_lot_and_closes_old(self) -> None:
        lot = self.svc.grant_lot("ACC1", 80, source="结转批",
                                 expires_at="2026-03-01T00:00:00Z",
                                 rule_id="CARRY_RULE")
        self.svc.clock.advance(days=60)
        report = self.svc.run_expiration()
        self.assertEqual(report["lots_carried"], 1)
        self.assertEqual(report["amount_carried"], 80)
        old = self.svc.lots.require(lot["lot_id"])
        self.assertEqual(old.status.value, "CARRIED")
        self.assertEqual(old.remaining, 0)
        new_id = report["processed_lots"][0]["carried_to_lot_id"]
        new_lot = self.svc.lots.require(new_id)
        self.assertEqual(new_lot.remaining, 80)
        # 默认只允许结转一次：新批规则退化为作废
        self.assertEqual(new_lot.rule_kind.value, "EXPIRE")
        self.assertEqual(new_lot.remaining_carry_hops, 0)
        # as_of=2026-03-02，+30 天 -> 2026-04-01
        self.assertTrue(new_lot.expires_at.startswith("2026-04-01"))

    def test_multi_hop_carryover(self) -> None:
        lot = self.svc.grant_lot("ACC1", 80, source="多跳批",
                                 expires_at="2026-03-01T00:00:00Z",
                                 rule_id="CARRY2")
        self.svc.clock.advance(days=60)
        r1 = self.svc.run_expiration()
        hop1 = self.svc.lots.require(
            r1["processed_lots"][0]["carried_to_lot_id"])
        self.assertEqual(hop1.remaining_carry_hops, 1)
        self.assertEqual(hop1.rule_kind.value, "CARRYOVER")

        # 第二跳：新批仍可结转
        self.svc.clock.advance(days=15)
        r2 = self.svc.run_expiration()
        hop2 = self.svc.lots.require(
            r2["processed_lots"][0]["carried_to_lot_id"])
        self.assertEqual(hop2.remaining_carry_hops, 0)
        self.assertEqual(hop2.rule_kind.value, "EXPIRE")

        # 第三跳：作废
        self.svc.clock.advance(days=15)
        r3 = self.svc.run_expiration()
        self.assertEqual(r3["lots_expired"], 1)
        self.assertEqual(r3["amount_expired"], 80)

    def test_idempotent_same_as_of(self) -> None:
        self.svc.grant_lot("ACC1", 100, source="X",
                           expires_at="2026-03-01T00:00:00Z",
                           rule_id="EXPIRE_RULE")
        self.svc.clock.advance(days=60)
        r1 = self.svc.run_expiration()
        r2 = self.svc.run_expiration()  # 默认 key=JOB:{as_of}
        self.assertFalse(r1["idempotent_hit"])
        self.assertTrue(r2["idempotent_hit"])
        self.assertEqual(r2["processed_lots"], r1["processed_lots"])

    def test_explicit_job_key_idempotent(self) -> None:
        self.svc.grant_lot("ACC1", 100, source="X",
                           expires_at="2026-03-01T00:00:00Z",
                           rule_id="EXPIRE_RULE")
        kw = dict(as_of="2026-04-01T00:00:00Z", job_key="DAILY-20260401")
        r1 = self.svc.run_expiration(**kw)
        r2 = self.svc.run_expiration(**kw)
        self.assertTrue(r2["idempotent_hit"])
        self.assertEqual(r1["amount_expired"], r2["amount_expired"])

    def test_earlier_as_of_after_later_run_is_noop_for_closed_lots(self) -> None:
        lot = self.svc.grant_lot("ACC1", 100, source="X",
                                 expires_at="2026-03-01T00:00:00Z",
                                 rule_id="EXPIRE_RULE")
        self.svc.run_expiration(as_of="2026-06-01T00:00:00Z",
                                job_key="J1")
        # 用更早的时刻、不同 key 重放：批次已是终态，不得重复失效
        r2 = self.svc.run_expiration(as_of="2026-04-01T00:00:00Z",
                                     job_key="J2")
        self.assertEqual(r2["lots_expired"], 0)
        self.assertEqual(r2["processed_lots"], [])

    def test_zero_remaining_carryover_marks_expired(self) -> None:
        lot = self.svc.grant_lot("ACC1", 100, source="结转批",
                                 expires_at="2026-03-01T00:00:00Z",
                                 rule_id="CARRY_RULE")
        self.svc.consume("ACC1", 100)
        self.svc.clock.advance(days=60)
        r = self.svc.run_expiration()
        self.assertEqual(r["lots_expired"], 1)
        self.assertEqual(r["lots_carried"], 0)

    def test_expired_lot_not_spendable(self) -> None:
        self.svc.grant_lot("ACC1", 100, source="X",
                           expires_at="2026-03-01T00:00:00Z",
                           rule_id="EXPIRE_RULE")
        self.svc.clock.advance(days=60)
        self.svc.run_expiration()
        self.svc.grant_lot("ACC1", 50, source="新批",
                           expires_at="2027-01-01T00:00:00Z")
        with self.assertRaises(InsufficientBalanceError) as ctx:
            self.svc.consume("ACC1", 80)
        self.assertEqual(ctx.exception.available, 50)


class ImmutabilityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.svc = make_service()
        self.lot = self.svc.grant_lot(
            "ACC1", 100, source="X", expires_at="2026-06-01T00:00:00Z",
            rule_id="CARRY_RULE")
        self.cons = self.svc.consume("ACC1", 40, scope="SVC_A", note="订单1")

    def test_extension_does_not_change_consumption_snapshot(self) -> None:
        self.svc.extend_expiry(
            self.lot["lot_id"], "2027-01-01T00:00:00Z",
            approved_by="核算专员甲", reason="申诉通过")
        detail = self.svc.get_consumption(self.cons["consumption_id"])
        self.assertEqual(
            detail["allocations"][0]["lot_expires_at_snapshot"],
            "2026-06-01T00:00:00.000Z")
        # 批次本身的到期时间已变
        self.assertTrue(self.lot["lot_id"] and True)
        self.assertEqual(
            self.svc.lots.require(self.lot["lot_id"]).expires_at,
            "2027-01-01T00:00:00.000Z")

    def test_freeze_unfreeze_does_not_change_history(self) -> None:
        self.svc.freeze_lot(self.lot["lot_id"])
        self.svc.unfreeze_lot(self.lot["lot_id"])
        detail = self.svc.get_consumption(self.cons["consumption_id"])
        self.assertEqual(detail["amount"], 40)
        self.assertEqual(detail["allocations"][0]["amount"], 40)

    def test_policy_supersession_keeps_old_lot_snapshot(self) -> None:
        v2 = self.svc.create_policy(
            [{"rule_id": "NEW", "kind": "EXPIRE"}], policy_id="POLICY_V2")
        self.assertEqual(v2["version"], 2)
        detail = self.svc.get_consumption(self.cons["consumption_id"])
        self.assertEqual(
            detail["allocations"][0]["lot_policy_version_snapshot"], 1)
        old_lot = self.svc.lots.require(self.lot["lot_id"])
        self.assertEqual(old_lot.policy_version, 1)
        self.assertEqual(old_lot.rule_id, "CARRY_RULE")
        policies = self.svc.list_policies()
        self.assertEqual([p["state"] for p in policies],
                         ["SUPERSEDED", "ACTIVE"])

    def test_new_policy_applies_to_new_grants_only(self) -> None:
        self.svc.create_policy([{"rule_id": "NEW", "kind": "EXPIRE"}])
        new_lot = self.svc.grant_lot(
            "ACC1", 10, source="新版", expires_at="2028-01-01T00:00:00Z")
        self.assertEqual(new_lot["policy_version"], 2)
        self.assertEqual(new_lot["rule"]["rule_id"], "NEW")

    def test_extension_must_be_later(self) -> None:
        with self.assertRaises(ValidationError):
            self.svc.extend_expiry(
                self.lot["lot_id"], "2026-05-01T00:00:00Z",
                approved_by="x")

    def test_extension_history_is_append_only(self) -> None:
        self.svc.extend_expiry(
            self.lot["lot_id"], "2026-08-01T00:00:00Z", approved_by="甲")
        self.svc.extend_expiry(
            self.lot["lot_id"], "2026-10-01T00:00:00Z", approved_by="乙")
        history = self.svc.list_extensions(self.lot["lot_id"])
        self.assertEqual([h["approved_by"] for h in history], ["甲", "乙"])


class RefundTest(unittest.TestCase):
    def setUp(self) -> None:
        self.svc = make_service()

    def test_refund_to_open_original_lot(self) -> None:
        lot = self.svc.grant_lot("ACC1", 100, source="X",
                                 expires_at="2026-12-01T00:00:00Z")
        cons = self.svc.consume("ACC1", 60)
        report = self.svc.refund(cons["consumption_id"], 60)
        self.assertEqual(report["refunds"][0]["target_kind"], "ORIGINAL")
        self.assertEqual(self.svc.lots.require(lot["lot_id"]).remaining, 100)

    def test_refund_feFo_order_partial(self) -> None:
        near = self.svc.grant_lot("ACC1", 30, source="临期",
                                  expires_at="2026-03-01T00:00:00Z")
        far = self.svc.grant_lot("ACC1", 100, source="长期",
                                 expires_at="2027-01-01T00:00:00Z")
        cons = self.svc.consume("ACC1", 80)
        report = self.svc.refund(cons["consumption_id"], 50)
        # 按原分配顺序：先退临期批 30，再退长期批 20
        self.assertEqual([r["source_allocation_lot_id"] for r in report["refunds"]],
                         [near["lot_id"], far["lot_id"]])
        self.assertEqual([r["amount"] for r in report["refunds"]], [30, 20])

    def test_refund_to_closed_lot_reinstates(self) -> None:
        lot = self.svc.grant_lot("ACC1", 100, source="X",
                                 expires_at="2026-03-01T00:00:00Z",
                                 rule_id="EXPIRE_RULE")
        cons = self.svc.consume("ACC1", 40)
        self.svc.clock.advance(days=60)
        self.svc.run_expiration()
        report = self.svc.refund(cons["consumption_id"], 40,
                                 reinstatement_days=15)
        self.assertEqual(report["refunds"][0]["target_kind"], "REINSTATED")
        new_id = report["refunds"][0]["target_lot_id"]
        new_lot = self.svc.lots.require(new_id)
        self.assertEqual(new_lot.remaining, 40)
        self.assertTrue(new_lot.expires_at > "2026-03-01")
        # 原消费记录保持不变
        detail = self.svc.get_consumption(cons["consumption_id"])
        self.assertEqual(detail["allocations"][0]["amount"], 40)
        self.assertEqual(
            detail["allocations"][0]["refunded_amount"], 40)

    def test_over_refund_rejected(self) -> None:
        self.svc.grant_lot("ACC1", 100, source="X",
                           expires_at="2026-12-01T00:00:00Z")
        cons = self.svc.consume("ACC1", 50)
        with self.assertRaises(ConflictError):
            self.svc.refund(cons["consumption_id"], 60)
        self.svc.refund(cons["consumption_id"], 50)
        with self.assertRaises(ConflictError):
            self.svc.refund(cons["consumption_id"], 1)

    def test_refund_unknown_consumption(self) -> None:
        with self.assertRaises(NotFoundError):
            self.svc.refund("CNS_nope", 1)


class BalanceForecastTest(unittest.TestCase):
    def setUp(self) -> None:
        self.svc = make_service()
        self.svc.grant_lot("ACC1", 100, source="作废批", scope="SVC_A",
                           expires_at="2026-06-01T00:00:00Z",
                           rule_id="EXPIRE_RULE")
        self.svc.grant_lot("ACC1", 50, source="结转批",
                           expires_at="2026-03-01T00:00:00Z",
                           rule_id="CARRY_RULE")
        self.svc.grant_lot("ACC1", 70, source="长期通用",
                           expires_at="2028-01-01T00:00:00Z")

    def test_balance_composition(self) -> None:
        bal = self.svc.get_balance("ACC1")
        self.assertEqual(bal["available_balance"], 220)
        self.assertEqual(len(bal["components"]), 3)
        by_source = {c["source"]: c for c in bal["components"]}
        self.assertTrue(by_source["结转批"]["spendable_now"])
        self.assertEqual(by_source["作废批"]["policy_version"], 1)

    def test_balance_scope_filter(self) -> None:
        # 追加一批限定 SVC_B 的额度：在 SVC_A 视图中必须被排除，
        # 而通用批（scope=*）在任何场景视图中都保留。
        self.svc.grant_lot("ACC1", 90, source="专属B", scope="SVC_B",
                           expires_at="2026-09-01T00:00:00Z")
        bal = self.svc.get_balance("ACC1", scope="SVC_A")
        # SVC_A 可用：作废批 100（专属 A）+ 结转批 50 + 长期通用 70
        self.assertEqual(bal["available_balance"], 220)
        sources = {c["source"] for c in bal["components"]}
        self.assertNotIn("专属B", sources)
        self.assertIn("长期通用", sources)

    def test_forecast_timeline(self) -> None:
        fc = self.svc.expiration_forecast("ACC1", horizon_days=365)
        index = {t["at"][:10]: t for t in fc["timeline"]}
        self.assertEqual(index["2026-03-01"]["carry_amount"], 50)
        self.assertEqual(index["2026-03-31"]["expire_amount"], 50)
        self.assertEqual(index["2026-06-01"]["expire_amount"], 100)
        # 长期通用批 2028 年才到期，窗口内仍存活
        self.assertEqual(fc["surviving_after_horizon"], 70)
        self.assertEqual(fc["total_forecast_expire"], 150)

    def test_forecast_is_pure(self) -> None:
        before = self.svc.get_balance("ACC1")
        self.svc.expiration_forecast("ACC1")
        self.svc.expiration_forecast("ACC1")
        after = self.svc.get_balance("ACC1")
        self.assertEqual(before, after)
        self.assertEqual(self.svc.list_expire_jobs(), [])


class EventAuditTest(unittest.TestCase):
    def test_events_are_append_only(self) -> None:
        svc = make_service()
        lot = svc.grant_lot("ACC1", 100, source="X",
                            expires_at="2026-12-01T00:00:00Z")
        svc.consume("ACC1", 10)
        svc.freeze_lot(lot["lot_id"])
        svc.unfreeze_lot(lot["lot_id"])
        svc.extend_expiry(lot["lot_id"], "2027-06-01T00:00:00Z",
                          approved_by="甲")
        events = svc.list_events(account_id="ACC1")
        types = [e["event_type"] for e in events]
        for expected in ("LOT_GRANTED", "CONSUMPTION_CONFIRMED",
                         "LOT_FROZEN", "LOT_UNFROZEN",
                         "LOT_EXPIRY_EXTENDED", "ACCOUNT_CREATED"):
            self.assertIn(expected, types)
        # 事件只增：同一批次的事件链可独立查询
        lot_events = svc.list_events(lot_id=lot["lot_id"])
        self.assertTrue(all(e["lot_id"] == lot["lot_id"] for e in lot_events))


class SchedulerTest(unittest.TestCase):
    def test_scheduler_uses_injected_clock_and_is_idempotent(self) -> None:
        from points_governance.scheduler import ExpirationScheduler

        svc = make_service()
        svc.grant_lot("ACC1", 100, source="到期批",
                      expires_at="2026-03-01T00:00:00Z")
        svc.clock.advance(days=60)
        sched = ExpirationScheduler(svc, interval_seconds=0.01)
        r1 = sched.run_once()
        r2 = sched.run_once()  # 同一天重复触发
        self.assertTrue(r2["idempotent_hit"])
        self.assertEqual(r1["job_key"], "SCHED:2026-03-02")
        self.assertEqual(r1["amount_expired"], 100)

        # 循环模式可以正常启停
        sched.start()
        sched.stop(timeout=2)


if __name__ == "__main__":
    unittest.main()
