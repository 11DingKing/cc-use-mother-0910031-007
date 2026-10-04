"""HTTP API 的端到端测试：多年度政策并行下的完整业务流转。"""
from __future__ import annotations

import itertools
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from fastapi.testclient import TestClient

from points_governance.api import build_app
from points_governance.clock import MutableClock
from points_governance.service import PointsService

UTC = timezone.utc
START = datetime(2026, 6, 1, tzinfo=UTC)


def make_client() -> tuple[TestClient, MutableClock]:
    clock = MutableClock(START)
    counter = itertools.count(1)
    service = PointsService(clock=clock, id_factory=lambda kind: f"{kind}-{next(counter):04d}")
    return TestClient(build_app(service)), clock


class ApiFlowTest(unittest.TestCase):
    """多年度政策并行：两批不同来源与到期日的积分的完整生命周期。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.client, cls.clock = make_client()
        cls.old_batch: dict = {}
        cls.new_batch: dict = {}
        cls.consumption: dict = {}

    def test_00_health(self) -> None:
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)

    def test_01_publish_two_policy_versions(self) -> None:
        old = self.client.post(
            "/policies",
            json={
                "name": "2025 年度政策",
                "effective_from": "2025-07-01T00:00:00+00:00",
                "default_validity_days": 365,
                "default_carryover": {"allowed": False},
            },
        )
        self.assertEqual(old.status_code, 201)
        new = self.client.post(
            "/policies",
            json={
                "name": "2026 年度政策",
                "effective_from": "2026-01-01T00:00:00+00:00",
                "default_validity_days": 365,
                "default_carryover": {"allowed": True, "cap_permille": 1000, "validity_days": 180},
            },
        )
        self.assertEqual(new.status_code, 201)
        policies = self.client.get("/policies").json()
        self.assertEqual(len(policies), 2)

    def test_02_grant_two_annual_batches(self) -> None:
        # 2025 年度批次：2026-07-01 到期，不可结转（旧政策快照）
        old = self.client.post(
            "/accounts/ent-1/grants",
            json={"amount": 500, "source": "ANNUAL_GRANT", "effective_from": "2025-07-01T00:00:00+00:00"},
        )
        self.assertEqual(old.status_code, 201)
        self.assertEqual(old.json()["expires_at"], "2026-07-01T00:00:00+00:00")
        self.assertFalse(old.json()["carryover_rule"]["allowed"])
        # 2026 年度批次：2027-01-01 到期，按新政策可结转
        new = self.client.post(
            "/accounts/ent-1/grants",
            json={"amount": 800, "source": "ANNUAL_GRANT", "effective_from": "2026-01-01T00:00:00+00:00"},
        )
        self.assertEqual(new.status_code, 201)
        self.assertEqual(new.json()["expires_at"], "2027-01-01T00:00:00+00:00")
        self.assertTrue(new.json()["carryover_rule"]["allowed"])
        type(self).old_batch = old.json()
        type(self).new_batch = new.json()

    def test_03_consume_uses_near_expiry_batch_first(self) -> None:
        response = self.client.post(
            "/accounts/ent-1/consumptions",
            json={"amount": 600, "scope": "general", "request_id": "order-1"},
        )
        self.assertEqual(response.status_code, 201)
        body = response.json()
        # 先扣临近到期的 2025 批次，再扣 2026 批次
        self.assertEqual(
            [(item["batch_id"], item["amount"]) for item in body["allocations"]],
            [(self.old_batch["batch_id"], 500), (self.new_batch["batch_id"], 100)],
        )
        # 幂等：同一请求号重复提交不重复扣减
        replay = self.client.post(
            "/accounts/ent-1/consumptions",
            json={"amount": 600, "scope": "general", "request_id": "order-1"},
        )
        self.assertEqual(replay.json()["consumption_id"], body["consumption_id"])
        balance = self.client.get("/accounts/ent-1/balance").json()
        self.assertEqual(balance["total_remaining"], 700)
        type(self).consumption = body

    def test_04_balance_composition_and_forecast(self) -> None:
        balance = self.client.get("/accounts/ent-1/balance").json()
        self.assertEqual(balance["total_remaining"], 700)
        self.assertEqual(balance["total_available"], 700)
        by_id = {item["batch_id"]: item for item in balance["batches"]}
        self.assertEqual(by_id[self.old_batch["batch_id"]]["remaining"], 0)
        self.assertEqual(by_id[self.new_batch["batch_id"]]["remaining"], 700)
        forecast = self.client.get("/accounts/ent-1/expiry-forecast", params={"horizon_days": 300}).json()
        self.assertEqual(forecast["total_expiring"], 700)
        self.assertEqual(forecast["buckets"][0]["date"], "2027-01-01")

    def test_05_refund_keeps_original_consumption(self) -> None:
        before = self.client.get("/accounts/ent-1/consumptions").json()
        refund = self.client.post(f"/consumptions/{self.consumption['consumption_id']}/refunds", json={"amount": 100})
        self.assertEqual(refund.status_code, 201)
        # 原消费按 500:100 分配，退回 100 按 5:1 比例分摊：83+1 与 16
        allocations = {item["batch_id"]: item["amount"] for item in refund.json()["allocations"]}
        self.assertEqual(allocations[self.old_batch["batch_id"]], 84)
        self.assertEqual(allocations[self.new_batch["batch_id"]], 16)
        after = self.client.get("/accounts/ent-1/consumptions").json()
        self.assertEqual(before, after)  # 既有消费记录不变
        balance = self.client.get("/accounts/ent-1/balance").json()
        self.assertEqual(balance["total_remaining"], 800)

    def test_06_freeze_and_unfreeze(self) -> None:
        batch_id = self.new_batch["batch_id"]  # 剩余 700 + 退回 16 = 716
        frozen = self.client.post(f"/batches/{batch_id}/freezes", json={"amount": 200, "reason": "风控核查"})
        self.assertEqual(frozen.status_code, 201)
        self.assertEqual(frozen.json()["available"], 516)
        too_much = self.client.post(f"/batches/{batch_id}/freezes", json={"amount": 700})
        self.assertEqual(too_much.status_code, 400)
        unfrozen = self.client.post(f"/batches/{batch_id}/unfreezes", json={"amount": 200})
        self.assertEqual(unfrozen.status_code, 201)
        self.assertEqual(unfrozen.json()["frozen"], 0)

    def test_07_extension_delays_expiry(self) -> None:
        batch_id = self.old_batch["batch_id"]
        extended = self.client.post(
            f"/batches/{batch_id}/extensions",
            json={"days": 30, "approved_by": "核算专员", "reason": "政策衔接"},
        )
        self.assertEqual(extended.status_code, 201)
        self.assertEqual(extended.json()["days"], 30)
        batches = self.client.get("/accounts/ent-1/batches").json()
        by_id = {item["batch_id"]: item for item in batches}
        self.assertEqual(by_id[batch_id]["effective_expiry"], "2026-07-31T00:00:00+00:00")
        # 原到期日过后运行到期任务：延期批次不受影响
        self.clock.set(datetime(2026, 7, 15, tzinfo=UTC))
        report = self.client.post("/jobs/expire", json={}).json()
        self.assertEqual(report["expired"], [])

    def test_08_expiration_is_idempotent(self) -> None:
        self.clock.set(datetime(2026, 8, 1, tzinfo=UTC))
        first = self.client.post("/jobs/expire", json={}).json()
        # 2025 批次剩余 84 到期作废（旧政策快照不可结转）
        self.assertEqual(first["total_expired"], 84)
        self.assertEqual(first["total_carried"], 0)
        second = self.client.post("/jobs/expire", json={}).json()
        self.assertEqual(second["expired"], [])
        self.assertEqual(second["total_expired"], 0)
        balance = self.client.get("/accounts/ent-1/balance").json()
        self.assertEqual(balance["total_remaining"], 716)

    def test_09_ledger_and_traceability(self) -> None:
        ledger = self.client.get("/accounts/ent-1/ledger").json()
        kinds = [entry["kind"] for entry in ledger]
        for expected in ("GRANT", "CONSUME", "REFUND", "FREEZE", "UNFREEZE", "EXTEND", "EXPIRE"):
            self.assertIn(expected, kinds)
        consume = next(entry for entry in ledger if entry["kind"] == "CONSUME")
        self.assertEqual(
            [(item["batch_id"], item["amount"]) for item in consume["detail"]],
            [(self.old_batch["batch_id"], 500), (self.new_batch["batch_id"], 100)],
        )


class ApiErrorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.client, _ = make_client()
        self.client.post(
            "/policies",
            json={
                "name": "2026 年度政策",
                "effective_from": "2026-01-01T00:00:00+00:00",
                "default_validity_days": 365,
            },
        )
        grant = self.client.post(
            "/accounts/ent-1/grants",
            json={"amount": 100, "effective_from": "2026-01-01T00:00:00+00:00"},
        )
        self.batch_id = grant.json()["batch_id"]

    def test_not_found(self) -> None:
        response = self.client.get("/consumptions/consumption-x")
        self.assertEqual(response.status_code, 404)
        response = self.client.post("/consumptions/consumption-x/refunds", json={"amount": 1})
        self.assertEqual(response.status_code, 404)

    def test_insufficient_points_conflict(self) -> None:
        response = self.client.post("/accounts/ent-1/consumptions", json={"amount": 150})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["available"], 100)

    def test_validation_errors(self) -> None:
        response = self.client.post("/accounts/ent-1/consumptions", json={"amount": 0})
        self.assertEqual(response.status_code, 422)  # 请求体校验
        consumption = self.client.post("/accounts/ent-1/consumptions", json={"amount": 40}).json()
        response = self.client.post(
            f"/consumptions/{consumption['consumption_id']}/refunds", json={"amount": 50}
        )
        self.assertEqual(response.status_code, 400)  # 超出可退余额
        response = self.client.post(f"/batches/{self.batch_id}/extensions", json={"days": 0, "approved_by": "x"})
        self.assertEqual(response.status_code, 422)

    def test_expire_job_accepts_injected_now(self) -> None:
        report = self.client.post("/jobs/expire", json={"now": "2027-06-01T00:00:00+00:00"}).json()
        self.assertEqual(report["total_expired"], 100)
        again = self.client.post("/jobs/expire", json={"now": "2027-06-01T00:00:00+00:00"}).json()
        self.assertEqual(again["expired"], [])


if __name__ == "__main__":
    unittest.main()
