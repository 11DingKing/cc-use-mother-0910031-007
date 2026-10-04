"""HTTP API 端到端测试（真实监听本地端口）。"""
from __future__ import annotations

import json
import sys
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from points_governance import (  # noqa: E402
    Database,
    FixedClock,
    PointsService,
)
from points_governance.api import create_server  # noqa: E402


class ApiClient:
    def __init__(self, base: str) -> None:
        self.base = base

    def request(self, method: str, path: str, body: dict | None = None):
        data = None
        headers = {"Content-Type": "application/json"}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            self.base + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))


class ApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FixedClock("2026-01-01T00:00:00Z")
        self.service = PointsService(Database(":memory:"), self.clock)
        self.server = create_server(self.service, host="127.0.0.1", port=0)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       daemon=True)
        self.thread.start()
        self.api = ApiClient(f"http://127.0.0.1:{self.port}")
        # 基础数据
        _, p = self.api.request("POST", "/admin/policies", {"rules": [
            {"rule_id": "EXPIRE_RULE", "kind": "EXPIRE"},
            {"rule_id": "CARRY_RULE", "kind": "CARRYOVER", "carry_days": 30},
        ]})
        self.assertEqual(_, 200)
        self.api.request("POST", "/admin/accounts",
                         {"account_id": "ACC1", "name": "示例"})

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def test_health(self) -> None:
        status, body = self.api.request("GET", "/healthz")
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "ok")

    def test_full_lifecycle(self) -> None:
        # 发放两批
        _, near = self.api.request("POST", "/accounts/ACC1/lots", {
            "amount": 40, "source": "临期", "scope": "SVC_A",
            "expires_at": "2026-03-01T00:00:00Z", "rule_id": "CARRY_RULE"})
        _, far = self.api.request("POST", "/accounts/ACC1/lots", {
            "amount": 100, "source": "长期",
            "expires_at": "2027-01-01T00:00:00Z"})

        # 余额组成
        _, bal = self.api.request("GET", "/accounts/ACC1/balance")
        self.assertEqual(bal["available_balance"], 140)
        self.assertEqual(bal["lot_count"], 2)

        # 消费 60：FEFO 先临期 40 再长期 20
        _, cons = self.api.request("POST", "/accounts/ACC1/consumptions",
                                   {"amount": 60, "scope": "*"})
        self.assertEqual(
            [(a["lot_id"], a["amount"]) for a in cons["allocations"]],
            [(near["lot_id"], 40), (far["lot_id"], 20)])

        # 消费详情可追溯
        _, detail = self.api.request(
            "GET", f"/consumptions/{cons['consumption_id']}")
        self.assertEqual(detail["allocations"][0]["lot_policy_version_snapshot"], 1)

        # 冻结长期批 -> 余额不可用
        s, _ = self.api.request("POST", f"/lots/{far['lot_id']}/freeze",
                                {"reason": "稽核"})
        self.assertEqual(s, 200)
        s, err = self.api.request("POST", "/accounts/ACC1/consumptions",
                                  {"amount": 81})
        self.assertEqual(s, 409)
        self.assertEqual(err["error"], "insufficient_balance")
        self.assertEqual(err["details"]["available"], 0)
        self.api.request("POST", f"/lots/{far['lot_id']}/unfreeze", {})

        # 到期：临期批剩 0 -> 作废；推进到 2027-06 后长期批作废
        self.clock.advance(days=60)
        _, job1 = self.api.request("POST", "/admin/expiration/run", {})
        self.assertEqual(job1["lots_expired"], 1)
        _, job2 = self.api.request("POST", "/admin/expiration/run", {})
        self.assertTrue(job2["idempotent_hit"])

        self.clock.advance(days=500)
        _, job3 = self.api.request("POST", "/admin/expiration/run",
                                   {"job_key": "manual-1"})
        self.assertEqual(job3["lots_expired"], 1)
        self.assertEqual(job3["amount_expired"], 80)

        # 批次台账与作业记录
        _, jobs = self.api.request("GET", "/admin/expiration/jobs")
        self.assertEqual(len(jobs), 2)
        _, bal2 = self.api.request("GET", "/accounts/ACC1/balance")
        self.assertEqual(bal2["available_balance"], 0)

    def test_carryover_and_forecast_via_api(self) -> None:
        self.api.request("POST", "/accounts/ACC1/lots", {
            "amount": 50, "source": "结转批",
            "expires_at": "2026-03-01T00:00:00Z", "rule_id": "CARRY_RULE"})
        _, fc = self.api.request(
            "GET", "/accounts/ACC1/forecast?horizon_days=365")
        index = {t["at"][:10]: t for t in fc["timeline"]}
        self.assertEqual(index["2026-03-01"]["carry_amount"], 50)
        self.assertEqual(index["2026-03-31"]["expire_amount"], 50)
        self.assertEqual(fc["total_forecast_expire"], 50)

    def test_extend_and_refund_flow(self) -> None:
        _, lot = self.api.request("POST", "/accounts/ACC1/lots", {
            "amount": 100, "source": "X",
            "expires_at": "2026-06-01T00:00:00Z"})
        _, cons = self.api.request("POST", "/accounts/ACC1/consumptions",
                                   {"amount": 30})
        s, _ = self.api.request("POST", f"/lots/{lot['lot_id']}/extend", {
            "new_expires_at": "2027-01-01T00:00:00Z",
            "approved_by": "核算专员甲"})
        self.assertEqual(s, 200)
        # 既有消费快照不变
        _, detail = self.api.request(
            "GET", f"/consumptions/{cons['consumption_id']}")
        self.assertTrue(
            detail["allocations"][0]["lot_expires_at_snapshot"]
            .startswith("2026-06-01"))
        # 延期记录
        _, exts = self.api.request(
            "GET", f"/lots/{lot['lot_id']}/extensions")
        self.assertEqual(len(exts), 1)
        # 退回到仍开放的原批
        _, refund = self.api.request(
            "POST", f"/consumptions/{cons['consumption_id']}/refund",
            {"amount": 30})
        self.assertEqual(refund["refunds"][0]["target_kind"], "ORIGINAL")

    def test_validation_errors(self) -> None:
        # 余额不足
        s, err = self.api.request("POST", "/accounts/ACC1/consumptions",
                                  {"amount": 1})
        self.assertEqual(s, 409)
        self.assertEqual(err["error"], "insufficient_balance")
        # 未知路由
        s, err = self.api.request("GET", "/nope")
        self.assertEqual(s, 404)
        # 未知批次
        s, err = self.api.request("POST", "/lots/LOT_x/freeze", {})
        self.assertEqual(s, 404)
        # 非法金额
        self.api.request("POST", "/accounts/ACC1/lots", {
            "amount": 100, "source": "X",
            "expires_at": "2026-12-01T00:00:00Z"})
        s, err = self.api.request("POST", "/accounts/ACC1/consumptions",
                                  {"amount": -5})
        self.assertEqual(s, 422)


if __name__ == "__main__":
    unittest.main()
