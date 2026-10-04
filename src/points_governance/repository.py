"""仓储层：领域对象与关系行之间的映射与读写。"""
from __future__ import annotations

import json
import sqlite3
from typing import Any, Iterable

from .models import (
    CarryoverRule,
    Lot,
    LotStatus,
    Policy,
    PolicyState,
    RuleKind,
)

LOT_COLUMNS = [
    "lot_id", "account_id", "policy_id", "policy_version", "source", "scope",
    "granted_amount", "remaining", "effective_at", "expires_at", "status",
    "rule_id", "rule_kind", "rule_carry_days", "rule_carry_scope",
    "remaining_carry_hops", "created_at",
    "expire_job_id", "carried_to_lot_id", "frozen_at",
]


def row_to_lot(row: sqlite3.Row) -> Lot:
    return Lot(
        lot_id=row["lot_id"],
        account_id=row["account_id"],
        policy_id=row["policy_id"],
        policy_version=row["policy_version"],
        source=row["source"],
        scope=row["scope"],
        granted_amount=row["granted_amount"],
        remaining=row["remaining"],
        effective_at=row["effective_at"],
        expires_at=row["expires_at"],
        status=LotStatus(row["status"]),
        rule_id=row["rule_id"],
        rule_kind=RuleKind(row["rule_kind"]),
        rule_carry_days=row["rule_carry_days"],
        rule_carry_scope=row["rule_carry_scope"],
        remaining_carry_hops=row["remaining_carry_hops"],
        created_at=row["created_at"],
        expire_job_id=row["expire_job_id"],
        carried_to_lot_id=row["carried_to_lot_id"],
        frozen_at=row["frozen_at"],
    )


def row_to_policy(row: sqlite3.Row) -> Policy:
    raw_rules = json.loads(row["rules_json"])
    rules: dict[str, CarryoverRule] = {}
    for key, value in raw_rules.items():
        rules[key] = CarryoverRule(
            rule_id=value["rule_id"],
            kind=RuleKind(value["kind"]),
            carry_days=value.get("carry_days"),
            carry_scope=value.get("carry_scope"),
            max_carry_hops=value.get("max_carry_hops", 1),
            description=value.get("description", ""),
        )
    keys = set(row.keys())
    default_rule_id = row["default_rule_id"] if "default_rule_id" in keys else ""
    return Policy(
        policy_id=row["policy_id"],
        version=row["version"],
        effective_from=row["effective_from"],
        rules=rules,
        default_rule_id=default_rule_id,
        state=PolicyState(row["state"]),
        created_at=row["created_at"],
    )


class PolicyRepository:
    def __init__(self, conn_provider) -> None:
        self._conn = conn_provider

    def save(self, policy: Policy) -> None:
        conn = self._conn()
        conn.execute(
            "INSERT INTO policies(policy_id, version, effective_from, state,"
            " default_rule_id, rules_json, created_at) VALUES (?,?,?,?,?,?,?)",
            (policy.policy_id, policy.version, policy.effective_from,
             policy.state.value, policy.default_rule_id,
             json.dumps({k: v.to_dict() for k, v in policy.rules.items()},
                        ensure_ascii=False, sort_keys=True),
             policy.created_at),
        )

    def get(self, policy_id: str) -> Policy | None:
        row = self._conn().execute(
            "SELECT * FROM policies WHERE policy_id=?", (policy_id,)
        ).fetchone()
        return row_to_policy(row) if row else None

    def require(self, policy_id: str) -> Policy:
        policy = self.get(policy_id)
        if policy is None:
            from .errors import NotFoundError
            raise NotFoundError(f"政策不存在：{policy_id}")
        return policy

    def active(self) -> Policy | None:
        row = self._conn().execute(
            "SELECT * FROM policies WHERE state='ACTIVE' ORDER BY version DESC LIMIT 1"
        ).fetchone()
        return row_to_policy(row) if row else None

    def mark_superseded(self, policy_id: str) -> None:
        self._conn().execute(
            "UPDATE policies SET state='SUPERSEDED' WHERE policy_id=?",
            (policy_id,),
        )

    def list_all(self) -> list[Policy]:
        rows = self._conn().execute(
            "SELECT * FROM policies ORDER BY version"
        ).fetchall()
        return [row_to_policy(r) for r in rows]


class LotRepository:
    def __init__(self, conn_provider) -> None:
        self._conn = conn_provider

    def insert(self, lot: Lot) -> None:
        values = [
            lot.lot_id, lot.account_id, lot.policy_id, lot.policy_version,
            lot.source, lot.scope, lot.granted_amount, lot.remaining,
            lot.effective_at, lot.expires_at, lot.status.value, lot.rule_id,
            lot.rule_kind.value, lot.rule_carry_days, lot.rule_carry_scope,
            lot.remaining_carry_hops, lot.created_at, lot.expire_job_id,
            lot.carried_to_lot_id, lot.frozen_at,
        ]
        placeholders = ",".join("?" * len(values))
        self._conn().execute(
            f"INSERT INTO lots({','.join(LOT_COLUMNS)}) VALUES ({placeholders})",
            values,
        )

    def get(self, lot_id: str) -> Lot | None:
        row = self._conn().execute(
            "SELECT * FROM lots WHERE lot_id=?", (lot_id,)
        ).fetchone()
        return row_to_lot(row) if row else None

    def require(self, lot_id: str) -> Lot:
        lot = self.get(lot_id)
        if lot is None:
            from .errors import NotFoundError
            raise NotFoundError(f"批次不存在：{lot_id}")
        return lot

    def list_by_account(self, account_id: str,
                        statuses: Iterable[str] | None = None) -> list[Lot]:
        sql = "SELECT * FROM lots WHERE account_id=?"
        params: list[Any] = [account_id]
        statuses = list(statuses) if statuses is not None else None
        if statuses:
            sql += f" AND status IN ({','.join('?' * len(statuses))})"
            params.extend(statuses)
        sql += " ORDER BY expires_at, effective_at, lot_id"
        return [row_to_lot(r) for r in self._conn().execute(sql, params).fetchall()]

    def list_due(self, as_of_ts: str) -> list[Lot]:
        """已到到期时刻但仍处于 ACTIVE/FROZEN 的批次。"""
        rows = self._conn().execute(
            "SELECT * FROM lots WHERE status IN ('ACTIVE','FROZEN')"
            " AND expires_at <= ? ORDER BY expires_at, lot_id",
            (as_of_ts,),
        ).fetchall()
        return [row_to_lot(r) for r in rows]

    def update_status(self, lot: Lot, status: LotStatus, *,
                      expire_job_id: str | None = None,
                      carried_to_lot_id: str | None = None) -> None:
        lot.status = status
        if expire_job_id is not None:
            lot.expire_job_id = expire_job_id
        if carried_to_lot_id is not None:
            lot.carried_to_lot_id = carried_to_lot_id
        self._conn().execute(
            "UPDATE lots SET status=?, expire_job_id=COALESCE(?, expire_job_id),"
            " carried_to_lot_id=COALESCE(?, carried_to_lot_id)"
            " WHERE lot_id=?",
            (status.value, lot.expire_job_id, lot.carried_to_lot_id, lot.lot_id),
        )

    def update_expiry(self, lot: Lot, new_expires_at: str) -> None:
        lot.expires_at = new_expires_at
        self._conn().execute(
            "UPDATE lots SET expires_at=? WHERE lot_id=?",
            (new_expires_at, lot.lot_id),
        )

    def update_freeze(self, lot: Lot, status: LotStatus,
                      frozen_at: str | None) -> None:
        lot.status = status
        lot.frozen_at = frozen_at
        self._conn().execute(
            "UPDATE lots SET status=?, frozen_at=? WHERE lot_id=?",
            (status.value, frozen_at, lot.lot_id),
        )

    def add_remaining(self, lot: Lot, delta: int) -> None:
        lot.remaining += delta
        if lot.remaining < 0 or lot.remaining > lot.granted_amount:
            raise AssertionError(
                f"批次 {lot.lot_id} 余额越界：{lot.remaining}/{lot.granted_amount}"
            )
        self._conn().execute(
            "UPDATE lots SET remaining=? WHERE lot_id=?",
            (lot.remaining, lot.lot_id),
        )
