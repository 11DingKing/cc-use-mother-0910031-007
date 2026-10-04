"""核心领域服务。

落实契约四大不变量：

1. 批次化余额——所有额度以 :class:`Lot` 为最小单元，政策/规则在发放时快照；
2. 确定性扣减顺序——FEFO（最早到期优先）+ 多级确定性兜底排序；
3. 可注入到期时钟——所有时间判断走 ``Clock``，到期作业显式接收 ``as_of``；
4. 消费分配追溯——消费与批次分配只增不改，任何后续操作都不改写历史。
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta
from typing import Any

from .clock import Clock, SystemClock, parse_ts, to_ts
from .errors import (
    ConflictError,
    InsufficientBalanceError,
    NotFoundError,
    ValidationError,
)
from .models import (
    DEFAULT_STRATEGY,
    DEDUCTION_STRATEGIES,
    CarryoverRule,
    Lot,
    LotStatus,
    Policy,
    PolicyState,
    RuleKind,
)
from .repository import LotRepository, PolicyRepository
from .storage import Database

# 退回到已失效批次时，补发批次的默认有效期（天）。
DEFAULT_REINSTATEMENT_DAYS = 30
# 到期预测中结转链最多模拟的跳数，防止异常规则成环。
MAX_FORECAST_HOPS = 12


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def _scope_matches(lot_scope: str, required_scope: str) -> bool:
    return lot_scope == "*" or required_scope == "*" or lot_scope == required_scope


class PointsService:
    def __init__(self, db: Database, clock: Clock | None = None) -> None:
        self.db = db
        self.clock: Clock = clock or SystemClock()
        self.policies = PolicyRepository(db.conn)
        self.lots = LotRepository(db.conn)

    # ------------------------------------------------------------------
    # 基础工具
    # ------------------------------------------------------------------

    def _now_ts(self) -> str:
        return to_ts(self.clock.now())

    def _event(self, conn, event_type: str, payload: dict, *,
               account_id: str | None = None, lot_id: str | None = None,
               occurred_at: str | None = None) -> None:
        self.db.insert_event(
            _new_id("EVT"), event_type, occurred_at or self._now_ts(),
            payload, account_id=account_id, lot_id=lot_id,
        )

    def _require_account(self, conn, account_id: str) -> None:
        row = conn.execute(
            "SELECT 1 FROM accounts WHERE account_id=?", (account_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"账户不存在：{account_id}")

    def create_account(self, account_id: str, name: str = "") -> dict:
        now = self._now_ts()
        with self.db.lock, self.db.transaction() as conn:
            exists = conn.execute(
                "SELECT 1 FROM accounts WHERE account_id=?", (account_id,)
            ).fetchone()
            if exists:
                raise ConflictError(f"账户已存在：{account_id}")
            conn.execute(
                "INSERT INTO accounts(account_id, name, created_at) VALUES (?,?,?)",
                (account_id, name, now),
            )
            self._event(conn, "ACCOUNT_CREATED",
                        {"account_id": account_id, "name": name},
                        account_id=account_id, occurred_at=now)
        return {"account_id": account_id, "name": name, "created_at": now}

    # ------------------------------------------------------------------
    # 政策与结转规则
    # ------------------------------------------------------------------

    def create_policy(self, rules: list[dict], *,
                      policy_id: str | None = None,
                      effective_from: str | None = None,
                      default_rule_id: str | None = None) -> dict:
        """发布政策新版本；原子地把旧 ACTIVE 版本标记为 SUPERSEDED。

        旧版本上已发放的批次继续携带旧规则运行，政策换版不回溯历史。
        ``default_rule_id`` 决定发放批次未显式指定规则时使用哪一条；
        缺省取请求中的第一条规则（顺序敏感，不依赖字典序）。
        """
        now_ts = self._now_ts()
        eff = to_ts(parse_ts(effective_from)) if effective_from else now_ts
        parsed_rules = self._parse_rules(rules)
        if default_rule_id is None:
            default_rule_id = next(iter(parsed_rules))
        elif default_rule_id not in parsed_rules:
            raise ValidationError(
                f"default_rule_id 不存在：{default_rule_id}",
                details={"available": sorted(parsed_rules)},
            )
        with self.db.lock, self.db.transaction() as conn:
            row = conn.execute(
                "SELECT COALESCE(MAX(version), 0) AS v FROM policies"
            ).fetchone()
            version = row["v"] + 1
            pid = policy_id or f"POLICY_V{version}"
            try:
                policy = Policy(
                    policy_id=pid, version=version, effective_from=eff,
                    rules=parsed_rules, default_rule_id=default_rule_id,
                    state=PolicyState.ACTIVE, created_at=now_ts,
                )
                self.policies.save(policy)
            except Exception as exc:  # UNIQUE 等约束冲突
                raise ConflictError(f"政策创建失败：{exc}") from exc
            conn.execute(
                "UPDATE policies SET state='SUPERSEDED' WHERE state='ACTIVE'"
                " AND policy_id<>?", (pid,)
            )
            superseded = [
                r["policy_id"] for r in conn.execute(
                    "SELECT policy_id FROM policies WHERE state='SUPERSEDED'"
                ).fetchall()
            ]
            self._event(conn, "POLICY_PUBLISHED", {
                "policy_id": pid, "version": version,
                "effective_from": eff,
                "rules": {k: v.to_dict() for k, v in parsed_rules.items()},
                "superseded": superseded,
            }, occurred_at=now_ts)
        return self.policies.require(pid).to_dict()

    @staticmethod
    def _parse_rules(raw: list[dict]) -> dict[str, CarryoverRule]:
        if not raw:
            raise ValidationError("政策至少需要一条结转规则")
        rules: dict[str, CarryoverRule] = {}
        for item in raw:
            try:
                rule_id = str(item["rule_id"])
                kind = RuleKind(str(item["kind"]).upper())
                rule = CarryoverRule(
                    rule_id=rule_id,
                    kind=kind,
                    carry_days=item.get("carry_days"),
                    carry_scope=item.get("carry_scope"),
                    max_carry_hops=int(item.get("max_carry_hops", 1)),
                    description=item.get("description", ""),
                )
            except KeyError as exc:
                raise ValidationError(f"规则缺少字段：{exc.args[0]}") from exc
            except ValueError as exc:
                raise ValidationError(f"规则 {item.get('rule_id')} 非法：{exc}") from exc
            if rule_id in rules:
                raise ValidationError(f"规则编号重复：{rule_id}")
            rules[rule_id] = rule
        return rules

    def list_policies(self) -> list[dict]:
        return [p.to_dict() for p in self.policies.list_all()]

    # ------------------------------------------------------------------
    # 批次发放
    # ------------------------------------------------------------------

    def grant_lot(self, account_id: str, amount: int, *, source: str,
                  scope: str = "*", effective_at: str | None = None,
                  expires_at: str, policy_id: str | None = None,
                  rule_id: str | None = None,
                  lot_id: str | None = None) -> dict:
        """发放一批积分，并快照来源、可用范围、生效/到期时间与结转规则。"""
        if not isinstance(amount, int) or amount <= 0:
            raise ValidationError("发放金额必须是正整数（最小单位）")
        now = self._now_ts()
        eff = to_ts(parse_ts(effective_at)) if effective_at else now
        exp = to_ts(parse_ts(expires_at))
        if exp <= eff:
            raise ValidationError("到期时间必须晚于生效时间")
        if not source or not str(source).strip():
            raise ValidationError("source 不能为空")

        with self.db.lock, self.db.transaction() as conn:
            self._require_account(conn, account_id)
            policy = (self.policies.get(policy_id) if policy_id
                      else self.policies.active())
            if policy is None:
                raise ValidationError("没有可用政策，请先创建政策")
            rid = rule_id or policy.default_rule_id
            rule = policy.rules.get(rid)
            if rule is None:
                raise ValidationError(
                    f"政策 {policy.policy_id} 中不存在规则 {rid}",
                    details={"available": sorted(policy.rules)},
                )
            lot = Lot(
                lot_id=lot_id or _new_id("LOT"),
                account_id=account_id,
                policy_id=policy.policy_id,
                policy_version=policy.version,
                source=source,
                scope=scope,
                granted_amount=amount,
                remaining=amount,
                effective_at=eff,
                expires_at=exp,
                status=LotStatus.ACTIVE,
                rule_id=rule.rule_id,
                rule_kind=rule.kind,
                rule_carry_days=rule.carry_days,
                rule_carry_scope=rule.carry_scope,
                remaining_carry_hops=rule.max_carry_hops,
                created_at=now,
            )
            try:
                self.lots.insert(lot)
            except Exception as exc:
                raise ConflictError(f"批次发放失败：{exc}") from exc
            self._event(conn, "LOT_GRANTED", lot.to_dict(),
                        account_id=account_id, lot_id=lot.lot_id,
                        occurred_at=now)
        return lot.to_dict()

    # ------------------------------------------------------------------
    # 消费：确定性选批
    # ------------------------------------------------------------------

    def _eligible_lots(self, conn, account_id: str, scope: str,
                       now_ts: str) -> list[Lot]:
        """按 FEFO 选出可消费批次：已生效、未到期、未冻结、范围匹配。"""
        rows = conn.execute(
            "SELECT * FROM lots WHERE account_id=? AND status='ACTIVE'"
            " AND effective_at<=? AND expires_at>? ORDER BY expires_at,"
            " effective_at, created_at, lot_id",
            (account_id, now_ts, now_ts),
        ).fetchall()
        from .repository import row_to_lot
        lots = [row_to_lot(r) for r in rows]
        return [lot for lot in lots if _scope_matches(lot.scope, scope)]

    def _plan_allocation(self, account_id: str, amount: int, scope: str,
                         now_ts: str) -> tuple[list[tuple[Lot, int]], list[Lot]]:
        if not isinstance(amount, int) or amount <= 0:
            raise ValidationError("消费金额必须是正整数（最小单位）")
        conn = self.db.conn()
        candidates = self._eligible_lots(conn, account_id, scope, now_ts)
        plan: list[tuple[Lot, int]] = []
        need = amount
        for lot in candidates:
            if need == 0:
                break
            take = min(lot.remaining, need)
            if take > 0:
                plan.append((lot, take))
                need -= take
        return plan, candidates

    def preview_consume(self, account_id: str, amount: int, *,
                        scope: str = "*") -> dict:
        """预览本次消费将如何按批次分配（不落库）。"""
        now = self._now_ts()
        plan, candidates = self._plan_allocation(account_id, amount, scope, now)
        allocated = sum(a for _, a in plan)
        return {
            "account_id": account_id,
            "requested": amount,
            "scope": scope,
            "strategy": DEFAULT_STRATEGY,
            "as_of": now,
            "allocations": [
                {
                    "lot_id": lot.lot_id,
                    "amount": take,
                    "lot_expires_at": lot.expires_at,
                    "lot_policy_version": lot.policy_version,
                    "source": lot.source,
                    "scope": lot.scope,
                }
                for lot, take in plan
            ],
            "allocated": allocated,
            "shortfall": amount - allocated,
            "feasible": allocated == amount,
            "candidate_lot_ids": [lot.lot_id for lot in candidates],
        }

    def consume(self, account_id: str, amount: int, *, scope: str = "*",
                strategy: str = DEFAULT_STRATEGY, note: str = "",
                consumption_id: str | None = None) -> dict:
        if strategy not in DEDUCTION_STRATEGIES:
            raise ValidationError(
                f"不支持的扣减策略：{strategy}",
                details={"supported": list(DEDUCTION_STRATEGIES)},
            )
        now = self._now_ts()
        cid = consumption_id or _new_id("CNS")
        with self.db.lock, self.db.transaction() as conn:
            self._require_account(conn, account_id)
            plan, candidates = self._plan_allocation(account_id, amount,
                                                     scope, now)
            allocated = sum(a for _, a in plan)
            if allocated < amount:
                raise InsufficientBalanceError(
                    "可用余额不足（已按最早到期优先选批）",
                    requested=amount,
                    available=allocated,
                    candidates=[
                        {"lot_id": lot.lot_id, "remaining": lot.remaining,
                         "expires_at": lot.expires_at, "scope": lot.scope}
                        for lot in candidates
                    ],
                )
            # 条件式扣减：事务内再次确认批次状态与余额，防止并发/到期竞争。
            allocations: list[dict] = []
            for seq, (lot, take) in enumerate(plan, start=1):
                cur = conn.execute(
                    "UPDATE lots SET remaining=remaining-? WHERE lot_id=?"
                    " AND status='ACTIVE' AND remaining>=?",
                    (take, lot.lot_id, take),
                )
                if cur.rowcount != 1:
                    raise ConflictError(
                        f"批次 {lot.lot_id} 在扣减时状态已变化，请重试"
                    )
                allocations.append({
                    "lot_id": lot.lot_id, "amount": take, "seq": seq,
                    "lot_expires_at": lot.expires_at,
                    "lot_policy_version": lot.policy_version,
                })
            conn.execute(
                "INSERT INTO consumptions(consumption_id, account_id, amount,"
                " scope, strategy, note, created_at) VALUES (?,?,?,?,?,?,?)",
                (cid, account_id, amount, scope, strategy, note, now),
            )
            for item in allocations:
                conn.execute(
                    "INSERT INTO consumption_allocations(consumption_id, lot_id,"
                    " amount, lot_expires_at, lot_policy_version, seq)"
                    " VALUES (?,?,?,?,?,?)",
                    (cid, item["lot_id"], item["amount"], item["lot_expires_at"],
                     item["lot_policy_version"], item["seq"]),
                )
            self._event(conn, "CONSUMPTION_CONFIRMED", {
                "consumption_id": cid,
                "account_id": account_id,
                "amount": amount,
                "scope": scope,
                "strategy": strategy,
                "note": note,
                "allocations": allocations,
            }, account_id=account_id, occurred_at=now)
        return self.get_consumption(cid)

    def get_consumption(self, consumption_id: str) -> dict:
        conn = self.db.conn()
        row = conn.execute(
            "SELECT * FROM consumptions WHERE consumption_id=?",
            (consumption_id,),
        ).fetchone()
        if row is None:
            raise NotFoundError(f"消费不存在：{consumption_id}")
        allocs = conn.execute(
            "SELECT ca.*, l.source AS lot_source, l.scope AS lot_scope"
            " FROM consumption_allocations ca JOIN lots l ON l.lot_id=ca.lot_id"
            " WHERE consumption_id=? ORDER BY seq",
            (consumption_id,),
        ).fetchall()
        refunds = conn.execute(
            "SELECT * FROM refunds WHERE consumption_id=? ORDER BY rowid",
            (consumption_id,),
        ).fetchall()
        refunded_by_lot: dict[str, int] = {}
        for r in refunds:
            refunded_by_lot[r["lot_id"]] = (
                refunded_by_lot.get(r["lot_id"], 0) + r["amount"]
            )
        return {
            "consumption_id": row["consumption_id"],
            "account_id": row["account_id"],
            "amount": row["amount"],
            "scope": row["scope"],
            "strategy": row["strategy"],
            "status": row["status"],
            "note": row["note"],
            "created_at": row["created_at"],
            "refunded_at": row["refunded_at"],
            "allocations": [
                {
                    "seq": a["seq"],
                    "lot_id": a["lot_id"],
                    "lot_source": a["lot_source"],
                    "lot_scope": a["lot_scope"],
                    "amount": a["amount"],
                    "refunded_amount": refunded_by_lot.get(a["lot_id"], 0),
                    # 消费时快照：即使批次后来延期/换版也不改变
                    "lot_expires_at_snapshot": a["lot_expires_at"],
                    "lot_policy_version_snapshot": a["lot_policy_version"],
                }
                for a in allocs
            ],
            "refunds": [
                {
                    "refund_id": r["refund_id"],
                    "lot_id": r["lot_id"],
                    "amount": r["amount"],
                    "target_lot_id": r["target_lot_id"],
                    "target_kind": r["target_kind"],
                    "created_at": r["created_at"],
                }
                for r in refunds
            ],
        }

    def list_consumptions(self, account_id: str, limit: int = 100) -> list[dict]:
        rows = self.db.conn().execute(
            "SELECT consumption_id FROM consumptions WHERE account_id=?"
            " ORDER BY created_at DESC, consumption_id DESC LIMIT ?",
            (account_id, limit),
        ).fetchall()
        return [self.get_consumption(r["consumption_id"]) for r in rows]

    # ------------------------------------------------------------------
    # 退回：不改写既有消费，按原分配链路返还
    # ------------------------------------------------------------------

    def refund(self, consumption_id: str, amount: int | None = None, *,
               reinstatement_days: int = DEFAULT_REINSTATEMENT_DAYS,
               reason: str = "") -> dict:
        if amount is not None and (not isinstance(amount, int) or amount <= 0):
            raise ValidationError("退回金额必须是正整数")
        if reinstatement_days <= 0:
            raise ValidationError("补发有效期必须为正整数天数")
        now = self._now_ts()
        with self.db.lock, self.db.transaction() as conn:
            crow = conn.execute(
                "SELECT * FROM consumptions WHERE consumption_id=?",
                (consumption_id,),
            ).fetchone()
            if crow is None:
                raise NotFoundError(f"消费不存在：{consumption_id}")
            account_id = crow["account_id"]
            allocs = conn.execute(
                "SELECT * FROM consumption_allocations WHERE consumption_id=?"
                " ORDER BY seq", (consumption_id,)
            ).fetchall()
            refunded_rows = conn.execute(
                "SELECT lot_id, COALESCE(SUM(amount),0) AS done FROM refunds"
                " WHERE consumption_id=? GROUP BY lot_id",
                (consumption_id,),
            ).fetchall()
            done_by_lot = {r["lot_id"]: r["done"] for r in refunded_rows}
            total_alloc = sum(a["amount"] for a in allocs)
            total_refunded = sum(done_by_lot.values())
            refundable = total_alloc - total_refunded
            want = amount if amount is not None else refundable
            if want <= 0:
                raise ConflictError("该消费已全额退回")
            if want > refundable:
                raise ConflictError(
                    f"退回超额：可退 {refundable}，请求 {want}",
                )

            refunds: list[dict] = []
            need = want
            # 按原扣减顺序（FEFO seq）逐批次退回，保持确定性。
            for alloc in allocs:
                if need == 0:
                    break
                already = done_by_lot.get(alloc["lot_id"], 0)
                lot_refundable = alloc["amount"] - already
                if lot_refundable <= 0:
                    continue
                take = min(need, lot_refundable)
                target_lot_id, target_kind = self._refund_to_lot(
                    conn, alloc["lot_id"], take, account_id, now,
                    reinstatement_days,
                )
                rid = _new_id("RFD")
                conn.execute(
                    "INSERT INTO refunds(refund_id, consumption_id, lot_id,"
                    " amount, target_lot_id, target_kind, created_at)"
                    " VALUES (?,?,?,?,?,?,?)",
                    (rid, consumption_id, alloc["lot_id"], take,
                     target_lot_id, target_kind, now),
                )
                refunds.append({
                    "refund_id": rid,
                    "source_allocation_lot_id": alloc["lot_id"],
                    "amount": take,
                    "target_lot_id": target_lot_id,
                    "target_kind": target_kind,
                })
                need -= take

            if total_refunded + want == total_alloc:
                conn.execute(
                    "UPDATE consumptions SET refunded_at=? WHERE consumption_id=?",
                    (now, consumption_id),
                )
            self._event(conn, "REFUND_ISSUED", {
                "consumption_id": consumption_id,
                "account_id": account_id,
                "amount": want,
                "reason": reason,
                "refunds": refunds,
            }, account_id=account_id, occurred_at=now)
        return {
            "consumption_id": consumption_id,
            "refunded_amount": want,
            "total_refunded": total_refunded + want,
            "consumption_amount": total_alloc,
            "refunds": refunds,
            "created_at": now,
        }

    def _refund_to_lot(self, conn, lot_id: str, amount: int,
                       account_id: str, now: str,
                       reinstatement_days: int) -> tuple[str, str]:
        """把退回额度送回原批次。

        * 原批次仍开放（ACTIVE/FROZEN）：直接恢复原批余额，
          不超过其已消费量（退款额受分配明细约束，必然满足）；
        * 原批次已终态（EXPIRED/CARRIED）：被退回的额度早已不在
          结转额度内（结转只搬运到期时的剩余），因此补发一批新额度，
          沿用原批的范围与政策/规则快照，给予独立有效期。
        """
        from .repository import row_to_lot
        row = conn.execute(
            "SELECT * FROM lots WHERE lot_id=?", (lot_id,)
        ).fetchone()
        original = row_to_lot(row)

        if original.status in (LotStatus.ACTIVE, LotStatus.FROZEN):
            conn.execute(
                "UPDATE lots SET remaining=remaining+? WHERE lot_id=?",
                (amount, lot_id),
            )
            return lot_id, "ORIGINAL"

        # 已到期/已结转：补发新批。
        new_expires = to_ts(parse_ts(now) + timedelta(days=reinstatement_days))
        reinstate = Lot(
            lot_id=_new_id("LOT"),
            account_id=account_id,
            policy_id=original.policy_id,
            policy_version=original.policy_version,
            source=f"{original.source}@退回补发",
            scope=original.scope,
            granted_amount=amount,
            remaining=amount,
            effective_at=now,
            expires_at=new_expires,
            status=LotStatus.ACTIVE,
            rule_id=original.rule_id,
            rule_kind=original.rule_kind,
            rule_carry_days=original.rule_carry_days,
            rule_carry_scope=original.rule_carry_scope,
            remaining_carry_hops=original.remaining_carry_hops,
            created_at=now,
        )
        self.lots.insert(reinstate)
        self._event(conn, "LOT_REINSTATED_BY_REFUND", reinstate.to_dict(),
                    account_id=account_id, lot_id=reinstate.lot_id,
                    occurred_at=now)
        return reinstate.lot_id, "REINSTATED"

    # ------------------------------------------------------------------
    # 冻结 / 解冻
    # ------------------------------------------------------------------

    def freeze_lot(self, lot_id: str, *, reason: str = "") -> dict:
        now = self._now_ts()
        with self.db.lock, self.db.transaction() as conn:
            lot = self.lots.require(lot_id)
            if lot.status is not LotStatus.ACTIVE:
                raise ConflictError(
                    f"仅 ACTIVE 批次可冻结，当前状态 {lot.status.value}"
                )
            self.lots.update_freeze(lot, LotStatus.FROZEN, now)
            self._event(conn, "LOT_FROZEN", {
                "lot_id": lot_id, "reason": reason, "at": now,
            }, account_id=lot.account_id, lot_id=lot_id, occurred_at=now)
        return self.lots.require(lot_id).to_dict()

    def unfreeze_lot(self, lot_id: str, *, reason: str = "") -> dict:
        now = self._now_ts()
        with self.db.lock, self.db.transaction() as conn:
            lot = self.lots.require(lot_id)
            if lot.status is not LotStatus.FROZEN:
                raise ConflictError(
                    f"仅 FROZEN 批次可解冻，当前状态 {lot.status.value}"
                )
            if lot.expires_at <= now:
                raise ConflictError(
                    "批次已过到期时刻，不能解冻；请先运行到期处理作业",
                    details={"lot_id": lot_id, "expires_at": lot.expires_at,
                             "as_of": now},
                )
            self.lots.update_freeze(lot, LotStatus.ACTIVE, None)
            self._event(conn, "LOT_UNFROZEN", {
                "lot_id": lot_id, "reason": reason, "at": now,
            }, account_id=lot.account_id, lot_id=lot_id, occurred_at=now)
        return self.lots.require(lot_id).to_dict()

    # ------------------------------------------------------------------
    # 延期批准：只影响后续，不改写既有消费快照
    # ------------------------------------------------------------------

    def extend_expiry(self, lot_id: str, new_expires_at: str, *,
                      approved_by: str, reason: str = "") -> dict:
        now = self._now_ts()
        new_exp = to_ts(parse_ts(new_expires_at))
        with self.db.lock, self.db.transaction() as conn:
            lot = self.lots.require(lot_id)
            if lot.status not in (LotStatus.ACTIVE, LotStatus.FROZEN):
                raise ConflictError(
                    f"批次已终态（{lot.status.value}），不能延期"
                )
            if new_exp <= lot.expires_at:
                raise ValidationError(
                    "延期后的到期时间必须晚于当前到期时间",
                    details={"current_expires_at": lot.expires_at,
                             "requested": new_exp},
                )
            old_exp = lot.expires_at
            extension_id = _new_id("EXT")
            conn.execute(
                "INSERT INTO lot_extensions(extension_id, lot_id,"
                " old_expires_at, new_expires_at, approved_by, reason,"
                " created_at) VALUES (?,?,?,?,?,?,?)",
                (extension_id, lot_id, old_exp, new_exp,
                 approved_by, reason, now),
            )
            self.lots.update_expiry(lot, new_exp)
            # 仅追加延期事件；既有消费分配中快照的到期时间保持不变。
            self._event(conn, "LOT_EXPIRY_EXTENDED", {
                "lot_id": lot_id,
                "extension_id": extension_id,
                "old_expires_at": old_exp,
                "new_expires_at": new_exp,
                "approved_by": approved_by,
                "reason": reason,
            }, account_id=lot.account_id, lot_id=lot_id, occurred_at=now)
        return self.lots.require(lot_id).to_dict()

    def list_extensions(self, lot_id: str) -> list[dict]:
        self.lots.require(lot_id)
        rows = self.db.conn().execute(
            "SELECT * FROM lot_extensions WHERE lot_id=?"
            " ORDER BY rowid, extension_id",
            (lot_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # 定时到期：注入时钟 + 幂等
    # ------------------------------------------------------------------

    def run_expiration(self, *, as_of: str | datetime | None = None,
                       job_key: str | None = None,
                       account_id: str | None = None) -> dict:
        """执行到期处理。

        * ``as_of`` 显式注入判定时刻（定时调度传入，或测试重放）；
          默认取注入时钟的当前时刻。
        * ``job_key`` 为幂等键：同一键重复执行直接返回首次结果；
          默认键 ``JOB:{as_of}``——同一时刻重复运行绝不重复失效。
        * 每个到期批次在单事务内做状态机推进（ACTIVE/FROZEN ->
          EXPIRED/CARRIED），CARRYOVER 规则原子生成结转批次。
        """
        now = self._now_ts()
        if as_of is None:
            as_of_dt = self.clock.now()
        elif isinstance(as_of, datetime):
            as_of_dt = as_of
        else:
            as_of_dt = parse_ts(as_of)
        as_of_ts = to_ts(as_of_dt)
        key = job_key or f"JOB:{as_of_ts}"

        with self.db.lock:
            existing = self.db.query_one(
                "SELECT * FROM expire_jobs WHERE job_key=?", (key,)
            )
            if existing is not None:
                return {
                    "job_key": key,
                    "as_of": existing["as_of"],
                    "idempotent_hit": True,
                    "status": existing["status"],
                    "lots_expired": existing["lots_expired"],
                    "lots_carried": existing["lots_carried"],
                    "amount_expired": existing["amount_expired"],
                    "amount_carried": existing["amount_carried"],
                    "processed_lots": json.loads(existing["details_json"]),
                }

            due_lots = self.lots.list_due(as_of_ts)
            if account_id is not None:
                due_lots = [l for l in due_lots if l.account_id == account_id]

            processed: list[dict] = []
            amount_expired = 0
            amount_carried = 0
            lots_expired = 0
            lots_carried = 0
            for lot in due_lots:
                item = self._expire_one(lot, as_of_dt, now, key)
                processed.append(item)
                if item["outcome"] == "EXPIRED":
                    amount_expired += item["amount"]
                    lots_expired += 1
                elif item["outcome"] == "CARRIED":
                    amount_carried += item["amount"]
                    lots_carried += 1

            with self.db.transaction() as conn:
                conn.execute(
                    "INSERT INTO expire_jobs(job_key, as_of, status,"
                    " lots_expired, lots_carried, amount_expired,"
                    " amount_carried, details_json, created_at, finished_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (key, as_of_ts, "DONE", lots_expired, lots_carried,
                     amount_expired, amount_carried,
                     json.dumps(processed, ensure_ascii=False, sort_keys=True),
                     now, now),
                )
                self._event(conn, "EXPIRATION_JOB_RUN", {
                    "job_key": key,
                    "as_of": as_of_ts,
                    "processed": processed,
                }, occurred_at=now)

        return {
            "job_key": key,
            "as_of": as_of_ts,
            "idempotent_hit": False,
            "status": "DONE",
            "lots_expired": lots_expired,
            "lots_carried": lots_carried,
            "amount_expired": amount_expired,
            "amount_carried": amount_carried,
            "processed_lots": processed,
        }

    def _expire_one(self, lot: Lot, as_of_dt: datetime, now: str,
                    job_key: str) -> dict:
        """单批次到期状态机。返回处理摘要。"""
        with self.db.transaction() as conn:
            # 事务内重新加锁读取，跳过已被其它作业处理的批次。
            fresh = conn.execute(
                "SELECT * FROM lots WHERE lot_id=?", (lot.lot_id,)
            ).fetchone()
            from .repository import row_to_lot
            lot = row_to_lot(fresh)
            if lot.status not in (LotStatus.ACTIVE, LotStatus.FROZEN):
                return {"lot_id": lot.lot_id, "outcome": "SKIPPED",
                        "amount": 0, "reason": f"status={lot.status.value}"}

            amount = lot.remaining
            can_carry = (lot.rule_kind == RuleKind.CARRYOVER
                         and lot.remaining_carry_hops > 0)
            if can_carry and amount > 0:
                carry_days = lot.rule_carry_days or 0
                new_hops = lot.remaining_carry_hops - 1
                new_exp_dt = as_of_dt + timedelta(days=carry_days)
                if new_hops == 0:
                    # 结转次数用尽：新批快照退化为作废规则。
                    new_rule_kind = RuleKind.EXPIRE
                    new_carry_days: int | None = None
                    new_carry_scope: str | None = None
                else:
                    new_rule_kind = lot.rule_kind
                    new_carry_days = lot.rule_carry_days
                    new_carry_scope = lot.rule_carry_scope
                new_lot = Lot(
                    lot_id=_new_id("LOT"),
                    account_id=lot.account_id,
                    policy_id=lot.policy_id,
                    policy_version=lot.policy_version,
                    source=f"{lot.source}@结转",
                    scope=lot.rule_carry_scope or lot.scope,
                    granted_amount=amount,
                    remaining=amount,
                    effective_at=to_ts(as_of_dt),
                    expires_at=to_ts(new_exp_dt),
                    status=LotStatus.ACTIVE,
                    rule_id=lot.rule_id,
                    rule_kind=new_rule_kind,
                    rule_carry_days=new_carry_days,
                    rule_carry_scope=new_carry_scope,
                    remaining_carry_hops=new_hops,
                    created_at=now,
                )
                self.lots.insert(new_lot)
                # 条件式更新：只有仍处于开放状态才置为 CARRIED。
                cur = conn.execute(
                    "UPDATE lots SET remaining=0, status='CARRIED',"
                    " expire_job_id=?, carried_to_lot_id=?,"
                    " frozen_at=NULL WHERE lot_id=? AND status IN"
                    " ('ACTIVE','FROZEN') AND remaining=?",
                    (job_key, new_lot.lot_id, lot.lot_id, amount),
                )
                if cur.rowcount != 1:  # 极端竞争下放弃，交由外层重试
                    raise ConflictError(f"批次 {lot.lot_id} 结转竞争失败")
                lot.status = LotStatus.CARRIED
                lot.remaining = 0
                lot.expire_job_id = job_key
                lot.carried_to_lot_id = new_lot.lot_id
                self._event(conn, "LOT_CARRIED_OVER", {
                    "lot_id": lot.lot_id,
                    "carried_to_lot_id": new_lot.lot_id,
                    "amount": amount,
                    "as_of": to_ts(as_of_dt),
                    "job_key": job_key,
                }, account_id=lot.account_id, lot_id=lot.lot_id,
                    occurred_at=to_ts(as_of_dt))
                return {"lot_id": lot.lot_id, "outcome": "CARRIED",
                        "amount": amount,
                        "carried_to_lot_id": new_lot.lot_id,
                        "new_expires_at": new_lot.expires_at}

            cur = conn.execute(
                "UPDATE lots SET remaining=0, status='EXPIRED', frozen_at=NULL,"
                " expire_job_id=? WHERE lot_id=? AND status IN"
                " ('ACTIVE','FROZEN') AND remaining=?",
                (job_key, lot.lot_id, amount),
            )
            if cur.rowcount != 1:
                raise ConflictError(f"批次 {lot.lot_id} 失效竞争失败")
            lot.status = LotStatus.EXPIRED
            lot.remaining = 0
            lot.expire_job_id = job_key
            self._event(conn, "LOT_EXPIRED", {
                "lot_id": lot.lot_id,
                "amount_expired": amount,
                "as_of": to_ts(as_of_dt),
                "job_key": job_key,
            }, account_id=lot.account_id, lot_id=lot.lot_id,
                occurred_at=to_ts(as_of_dt))
            return {"lot_id": lot.lot_id, "outcome": "EXPIRED",
                    "amount": amount}

    def list_expire_jobs(self) -> list[dict]:
        rows = self.db.conn().execute(
            "SELECT job_key, as_of, status, lots_expired, lots_carried,"
            " amount_expired, amount_carried, created_at, finished_at"
            " FROM expire_jobs ORDER BY created_at"
        ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # 查询：余额批次组成 & 未来到期预测
    # ------------------------------------------------------------------

    def get_balance(self, account_id: str, *, scope: str | None = None,
                    include_terminal: bool = False) -> dict:
        """给出账户余额的批次组成。"""
        now = self._now_ts()
        conn = self.db.conn()
        self._require_account(conn, account_id)
        rows = conn.execute(
            "SELECT * FROM lots WHERE account_id=?"
            " ORDER BY expires_at, effective_at, lot_id",
            (account_id,),
        ).fetchall()
        from .repository import row_to_lot
        lots = [row_to_lot(r) for r in rows]
        if scope is not None:
            lots = [l for l in lots if _scope_matches(l.scope, scope)]

        components: list[dict] = []
        available = 0
        frozen = 0
        for lot in lots:
            if not include_terminal and lot.status in (
                LotStatus.EXPIRED, LotStatus.CARRIED
            ):
                continue
            item = {
                "lot_id": lot.lot_id,
                "source": lot.source,
                "scope": lot.scope,
                "policy_id": lot.policy_id,
                "policy_version": lot.policy_version,
                "remaining": lot.remaining,
                "granted_amount": lot.granted_amount,
                "status": lot.status.value,
                "effective_at": lot.effective_at,
                "expires_at": lot.expires_at,
                "rule_kind": lot.rule_kind.value,
                "remaining_carry_hops": lot.remaining_carry_hops,
                "carried_to_lot_id": lot.carried_to_lot_id,
                "spendable_now": (
                    lot.status == LotStatus.ACTIVE
                    and lot.effective_at <= now < lot.expires_at
                    and (scope is None or _scope_matches(lot.scope, scope))
                ),
            }
            components.append(item)
            if item["spendable_now"]:
                available += lot.remaining
            elif lot.status == LotStatus.FROZEN:
                frozen += lot.remaining

        total_remaining = sum(c["remaining"] for c in components)
        return {
            "account_id": account_id,
            "as_of": now,
            "scope": scope,
            "available_balance": available,
            "frozen_balance": frozen,
            "total_remaining": total_remaining,
            "lot_count": len(components),
            "components": components,
        }

    def expiration_forecast(self, account_id: str, *, horizon_days: int = 365,
                            scope: str | None = None) -> dict:
        """预测未来到期：对每个开放批次模拟结转链，给出各时点作废/结转额。

        预测是纯计算（不落库）：假设期间没有新的消费、退回或政策变化，
        按批次当前快照的到期时间与结转规则逐级推演。
        """
        if horizon_days <= 0 or horizon_days > 3650:
            raise ValidationError("horizon_days 应在 1..3650 之间")
        now_dt = self.clock.now()
        horizon_dt = now_dt + timedelta(days=horizon_days)
        balance = self.get_balance(account_id, scope=scope,
                                   include_terminal=False)
        timeline: dict[str, dict] = {}
        chain_projections: list[dict] = []

        for comp in balance["components"]:
            if comp["status"] not in (LotStatus.ACTIVE, LotStatus.FROZEN):
                continue
            if comp["remaining"] <= 0:
                continue
            chain = self._project_chain(comp, now_dt, horizon_dt)
            chain_projections.append(chain)
            for step in chain["steps"]:
                bucket = timeline.setdefault(step["at"], {
                    "at": step["at"],
                    "expire_amount": 0,
                    "carry_amount": 0,
                    "lot_ids": [],
                })
                if step["action"] == "EXPIRE":
                    bucket["expire_amount"] += step["amount"]
                else:
                    bucket["carry_amount"] += step["amount"]
                bucket["lot_ids"].append(step["lot_id"])

        timeline_items = sorted(timeline.values(), key=lambda x: x["at"])
        total_expire = sum(b["expire_amount"] for b in timeline_items)
        total_carry = sum(b["carry_amount"] for b in timeline_items)
        # 预测窗口结束时仍开放（未作废）的额度 = 当前余额 - 窗口内作废额。
        surviving = balance["total_remaining"] - total_expire
        return {
            "account_id": account_id,
            "as_of": to_ts(now_dt),
            "horizon_days": horizon_days,
            "scope": scope,
            "current_total_remaining": balance["total_remaining"],
            "total_forecast_expire": total_expire,
            "total_forecast_carry": total_carry,
            "surviving_after_horizon": max(surviving, 0),
            "timeline": timeline_items,
            "chains": chain_projections,
        }

    def _project_chain(self, comp: dict, now_dt: datetime,
                       horizon_dt: datetime) -> dict:
        """模拟单个批次的结转/作废链（纯计算）。

        结转跳数按批次快照的 ``remaining_carry_hops`` 逐跳递减；
        跳数用尽后，下一次到期即作废。
        """
        lot_id = comp["lot_id"]
        amount = comp["remaining"]
        expires_dt = parse_ts(comp["expires_at"])
        hops_left = int(comp["remaining_carry_hops"])
        can_carry = comp["rule_kind"] == RuleKind.CARRYOVER.value
        # 从批次行取结转天数，避免再依赖 components 形状。
        carry_days = self._rule_carry_days(comp["lot_id"])
        steps: list[dict] = []
        hops = 0
        while hops < MAX_FORECAST_HOPS:
            hops += 1
            if expires_dt > horizon_dt:
                break
            if can_carry and hops_left > 0 and amount > 0 and carry_days:
                steps.append({
                    "seq": len(steps) + 1,
                    "at": to_ts(expires_dt),
                    "action": "CARRY",
                    "lot_id": lot_id,
                    "amount": amount,
                })
                expires_dt = expires_dt + timedelta(days=carry_days)
                lot_id = f"{comp['lot_id']}>carry{hops}"
                hops_left -= 1
                if hops_left == 0:
                    can_carry = False
                continue
            steps.append({
                "seq": len(steps) + 1,
                "at": to_ts(expires_dt),
                "action": "EXPIRE",
                "lot_id": lot_id,
                "amount": amount,
            })
            amount = 0
            break
        return {
            "origin_lot_id": comp["lot_id"],
            "source": comp["source"],
            "remaining": comp["remaining"],
            "expires_at": comp["expires_at"],
            "rule_kind": comp["rule_kind"],
            "remaining_carry_hops": comp["remaining_carry_hops"],
            "steps": steps,
            "final_surviving": amount if expires_dt > horizon_dt else 0,
        }

    def _rule_carry_days(self, lot_id: str) -> int | None:
        row = self.db.query_one(
            "SELECT rule_carry_days, rule_kind FROM lots WHERE lot_id=?",
            (lot_id,),
        )
        return row["rule_carry_days"] if row else None

    # ------------------------------------------------------------------
    # 事件审计
    # ------------------------------------------------------------------

    def list_events(self, *, account_id: str | None = None,
                    lot_id: str | None = None, limit: int = 200) -> list[dict]:
        sql = "SELECT * FROM domain_events WHERE 1=1"
        params: list[Any] = []
        if account_id:
            sql += " AND account_id=?"
            params.append(account_id)
        if lot_id:
            sql += " AND lot_id=?"
            params.append(lot_id)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        rows = self.db.query_all(sql, params)
        return [{
            "event_id": r["event_id"],
            "event_type": r["event_type"],
            "account_id": r["account_id"],
            "lot_id": r["lot_id"],
            "occurred_at": r["occurred_at"],
            "payload": json.loads(r["payload"]),
        } for r in reversed(rows)]
