"""并发安全测试：多线程消费与到期作业竞争。"""
from __future__ import annotations

import sys
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from points_governance import (  # noqa: E402
    Database,
    FixedClock,
    PointsService,
)
from points_governance.errors import InsufficientBalanceError  # noqa: E402


def make_service() -> PointsService:
    svc = PointsService(Database(":memory:"), FixedClock("2026-01-01T00:00:00Z"))
    svc.create_policy([
        {"rule_id": "E", "kind": "EXPIRE"},
        {"rule_id": "C", "kind": "CARRYOVER", "carry_days": 30},
    ])
    svc.create_account("ACC1")
    return svc


class ConcurrencyTest(unittest.TestCase):
    def test_parallel_consumptions_never_overspend(self) -> None:
        svc = make_service()
        svc.grant_lot("ACC1", 1000, source="批次A",
                      expires_at="2026-12-01T00:00:00Z")
        svc.grant_lot("ACC1", 500, source="批次B",
                      expires_at="2026-09-01T00:00:00Z")


        def consume(_: int) -> str:
            try:
                svc.consume("ACC1", 100)
                return "ok"
            except InsufficientBalanceError:
                return "short"
            except Exception as exc:  # 竞争重试类错误也算正常
                self.assertIn("竞争", str(exc))
                return "retry"

        with ThreadPoolExecutor(max_workers=16) as pool:
            results = list(pool.map(consume, range(20)))

        ok = results.count("ok")
        # 总额 1500，每次 100：恰好 15 笔成功，5 笔余额不足
        self.assertEqual(ok, 15)
        self.assertEqual(results.count("short"), 5)
        bal = svc.get_balance("ACC1")
        self.assertEqual(bal["available_balance"], 0)

        # 交叉核对：批次剩余 + 全部成功消费分配额 = 发放总额
        rows = svc.db.query_all(
            "SELECT l.lot_id, l.granted_amount, l.remaining,"
            " COALESCE((SELECT SUM(amount) FROM consumption_allocations"
            "            WHERE lot_id=l.lot_id),0) AS alloc"
            " FROM lots l WHERE l.account_id='ACC1'")
        for r in rows:
            self.assertEqual(r["remaining"] + r["alloc"], r["granted_amount"])

    def test_parallel_expiration_jobs_idempotent(self) -> None:
        svc = make_service()
        svc.grant_lot("ACC1", 100, source="到期批",
                      expires_at="2026-03-01T00:00:00Z", rule_id="C")
        svc.clock.advance(days=60)

        with ThreadPoolExecutor(max_workers=8) as pool:
            reports = list(pool.map(
                lambda _: svc.run_expiration(job_key="DAILY-1"), range(8)))

        processed = [r for r in reports if not r["idempotent_hit"]]
        hits = [r for r in reports if r["idempotent_hit"]]
        self.assertEqual(len(processed), 1)
        self.assertEqual(len(hits), 7)
        for r in reports:
            self.assertEqual(r["lots_carried"], 1)
            self.assertEqual(r["amount_carried"], 100)
        # 恰好生成一个结转批
        count = svc.db.query_one(
            "SELECT COUNT(*) AS c FROM lots WHERE source LIKE '%@结转'")
        self.assertEqual(count["c"], 1)

    def test_expiration_concurrent_with_consumption(self) -> None:
        svc = make_service()
        lot = svc.grant_lot("ACC1", 1000, source="到期批",
                            expires_at="2026-03-01T00:00:00Z", rule_id="E")

        def consume() -> str:
            try:
                svc.consume("ACC1", 100)
                return "ok"
            except InsufficientBalanceError:
                return "short"
            except Exception as exc:
                # 到期先提交导致的条件更新失败：该笔消费输掉竞争
                self.assertIn("状态已变化", str(exc))
                return "lost"

        def expire() -> dict:
            return svc.run_expiration(job_key="J1",
                                      as_of="2026-03-02T00:00:00Z")

        with ThreadPoolExecutor(max_workers=10) as pool:
            futures = []
            for i in range(8):
                futures.append(pool.submit(consume))
            futures.append(pool.submit(expire))
            results = [f.result() for f in futures]

        # 恒等校验：消费掉的 + 失效的 = 发放额
        spent = 0
        for r in results[:-1]:
            if r == "ok":
                spent += 100
        expired = results[-1]["amount_expired"]
        self.assertEqual(spent + expired, 1000)
        lot_row = svc.db.query_one(
            "SELECT remaining, status FROM lots WHERE lot_id=?", (lot["lot_id"],))
        self.assertEqual(lot_row["status"], "EXPIRED")
        self.assertEqual(lot_row["remaining"], 0)


if __name__ == "__main__":
    unittest.main()
