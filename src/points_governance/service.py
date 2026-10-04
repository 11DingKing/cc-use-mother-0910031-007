"""应用服务：政策换版、批次发放、确定性消费、退回、冻结、延期与定时到期。

核心保证：
- 消费记录创建后不可修改；退回/冻结/解冻/延期/换版全部以新记录表达；
- 消费分配顺序由 strategy.select_batches 决定，确定且可重放；
- 到期任务只把 ACTIVE 批次单向推进为 EXPIRED，重复运行不产生新效果；
- 所有时间读取经过注入的 Clock，定时任务可用任意时刻重放。
"""
from __future__ import annotations

import threading
import uuid
from collections.abc import Callable, Iterable
from datetime import datetime, timedelta

from .clock import Clock, SystemClock, ensure_aware
from .errors import InsufficientPointsError, InvalidStateError, NotFoundError, ValidationError
from .models import (
    AccountBalance,
    Allocation,
    BatchBalance,
    BatchExpiryItem,
    BatchStatus,
    CarryoverRule,
    Consumption,
    EntryKind,
    ExpirationReport,
    ExpiredBatchInfo,
    ExpiryExtension,
    ExpiryForecast,
    ForecastBucket,
    GrantSource,
    LedgerEntry,
    PointBatch,
    PolicyVersion,
    Refund,
)
from .store import InMemoryStore
from .strategy import select_batches, sort_key


def _default_id(kind: str) -> str:
    return f"{kind}-{uuid.uuid4().hex[:12]}"


class PointsService:
    """积分结转到期治理的应用服务入口。"""

    def __init__(
        self,
        store: InMemoryStore | None = None,
        clock: Clock | None = None,
        id_factory: Callable[[str], str] | None = None,
    ) -> None:
        self._store = store or InMemoryStore()
        self._clock = clock or SystemClock()
        self._new_id = id_factory or _default_id
        self._lock = threading.RLock()

    # ------------------------------------------------------------------
    # 政策版本
    # ------------------------------------------------------------------

    def publish_policy(
        self,
        *,
        name: str,
        effective_from: datetime,
        default_validity_days: int,
        default_carryover: CarryoverRule | None = None,
    ) -> PolicyVersion:
        """发布新政策版本（政策换版）。

        只新增版本记录与账本事件；既有批次的有效期与结转规则快照、
        既有消费记录均不受影响。
        """
        if not name:
            raise ValidationError("政策名称不能为空")
        if default_validity_days <= 0:
            raise ValidationError("默认有效期天数必须为正数")
        with self._lock:
            now = self._clock.now()
            policy = PolicyVersion(
                policy_id=self._new_id("policy"),
                name=name,
                effective_from=ensure_aware(effective_from),
                default_validity_days=default_validity_days,
                default_carryover=default_carryover or CarryoverRule(),
                published_at=now,
            )
            self._store.policies[policy.policy_id] = policy
            self._append_ledger(EntryKind.POLICY_PUBLISH, occurred_at=now, note=f"发布政策版本 {name}")
            return policy

    def current_policy(self, at: datetime | None = None) -> PolicyVersion | None:
        """指定时刻生效的政策版本（effective_from 最新且不晚于该时刻）。"""
        at = ensure_aware(at) if at is not None else self._clock.now()
        candidates = [p for p in self._store.policies.values() if p.effective_from <= at]
        if not candidates:
            return None
        return max(candidates, key=lambda p: (p.effective_from, p.policy_id))

    def list_policies(self) -> list[PolicyVersion]:
        return sorted(self._store.policies.values(), key=lambda p: (p.effective_from, p.policy_id))

    # ------------------------------------------------------------------
    # 批次发放
    # ------------------------------------------------------------------

    def grant(
        self,
        *,
        account_id: str,
        amount: int,
        source: GrantSource = GrantSource.ANNUAL_GRANT,
        scopes: Iterable[str] = (),
        effective_from: datetime,
        expires_at: datetime | None = None,
        carryover_rule: CarryoverRule | None = None,
        policy_id: str | None = None,
    ) -> PointBatch:
        """发放一批积分：记录来源、可用范围、生效期与结转规则快照。

        未显式给定时，有效期与结转规则取自批次生效时的政策版本快照；
        此后政策换版不影响本批次。
        """
        if not account_id:
            raise ValidationError("账户编号不能为空")
        if amount <= 0:
            raise ValidationError("发放数量必须为正数")
        effective_from = ensure_aware(effective_from)
        expires_at = ensure_aware(expires_at) if expires_at is not None else None
        with self._lock:
            policy = self._resolve_policy(policy_id, at=effective_from)
            if expires_at is None:
                if policy is None:
                    raise ValidationError("未找到生效政策，必须显式指定到期时刻")
                expires_at = effective_from + timedelta(days=policy.default_validity_days)
            if expires_at <= effective_from:
                raise ValidationError("到期时刻必须晚于生效时刻")
            rule = carryover_rule or (policy.default_carryover if policy else CarryoverRule())
            batch = PointBatch(
                batch_id=self._new_id("batch"),
                account_id=account_id,
                source=GrantSource(source),
                scopes=frozenset(scopes),
                effective_from=effective_from,
                expires_at=expires_at,
                extension_days=0,
                carryover_rule=rule,
                policy_id=policy.policy_id if policy else "",
                initial_amount=amount,
                created_at=self._clock.now(),
            )
            self._store.batches[batch.batch_id] = batch
            self._append_ledger(
                EntryKind.GRANT,
                occurred_at=batch.created_at,
                account_id=account_id,
                amount=amount,
                batch_id=batch.batch_id,
                note=f"发放来源 {batch.source.value}",
            )
            return batch

    # ------------------------------------------------------------------
    # 消费与退回
    # ------------------------------------------------------------------

    def consume(
        self,
        *,
        account_id: str,
        amount: int,
        scope: str = "general",
        request_id: str | None = None,
        note: str = "",
    ) -> Consumption:
        """按确定策略扣减积分，并持久化批次分配明细。

        request_id 是幂等键：同一账户重复提交同一请求号返回首次的消费记录，
        不会重复扣减。
        """
        if amount <= 0:
            raise ValidationError("消费数量必须为正数")
        with self._lock:
            if request_id is not None:
                existing = self._store.request_index.get((account_id, request_id))
                if existing is not None:
                    return self._store.consumptions[existing]
            now = self._clock.now()
            allocations = select_batches(
                self._store.batches_of(account_id),
                account_id=account_id,
                scope=scope,
                at=now,
                amount=amount,
            )
            for allocation in allocations:
                self._store.batches[allocation.batch_id].consumed += allocation.amount
            consumption = Consumption(
                consumption_id=self._new_id("consumption"),
                account_id=account_id,
                amount=amount,
                scope=scope,
                occurred_at=now,
                allocations=tuple(allocations),
                note=note,
            )
            self._store.consumptions[consumption.consumption_id] = consumption
            if request_id is not None:
                self._store.request_index[(account_id, request_id)] = consumption.consumption_id
            self._append_ledger(
                EntryKind.CONSUME,
                occurred_at=now,
                account_id=account_id,
                amount=amount,
                consumption_id=consumption.consumption_id,
                detail=consumption.allocations,
                note=note,
            )
            return consumption

    def refund(self, *, consumption_id: str, amount: int, note: str = "") -> Refund:
        """退回消费：按原分配比例退回各批次，原消费记录保持不变。

        原批次仍生效时直接回补；已失效时为该部分生成补偿批次
        （来源 COMPENSATION，有效期与结转规则取当前政策快照）。
        """
        if amount <= 0:
            raise ValidationError("退回数量必须为正数")
        with self._lock:
            consumption = self._consumption_or_raise(consumption_id)
            already = sum(item.amount for item in self._store.refunds_of_consumption(consumption_id))
            if already + amount > consumption.amount:
                raise ValidationError(f"退回数量超出可退余额：已退 {already}，消费 {consumption.amount}")
            now = self._clock.now()
            allocations: list[Allocation] = []
            for batch_id, share in self._refund_shares(consumption, amount):
                batch = self._store.batches[batch_id]
                if batch.status == BatchStatus.ACTIVE:
                    batch.refunded += share
                    allocations.append(Allocation(batch_id=batch_id, amount=share))
                else:
                    compensation = self._create_compensation(batch, share, now)
                    allocations.append(Allocation(batch_id=compensation.batch_id, amount=share))
            refund = Refund(
                refund_id=self._new_id("refund"),
                consumption_id=consumption_id,
                account_id=consumption.account_id,
                amount=amount,
                occurred_at=now,
                allocations=tuple(allocations),
                note=note,
            )
            self._store.refunds[refund.refund_id] = refund
            self._append_ledger(
                EntryKind.REFUND,
                occurred_at=now,
                account_id=consumption.account_id,
                amount=amount,
                consumption_id=consumption_id,
                refund_id=refund.refund_id,
                detail=refund.allocations,
                note=note,
            )
            return refund

    # ------------------------------------------------------------------
    # 冻结 / 解冻 / 延期（均不触碰既有消费）
    # ------------------------------------------------------------------

    def freeze(self, *, batch_id: str, amount: int, reason: str = "") -> PointBatch:
        """冻结批次内部分额度：冻结部分不参与扣减，既有消费不受影响。"""
        if amount <= 0:
            raise ValidationError("冻结数量必须为正数")
        with self._lock:
            batch = self._batch_or_raise(batch_id)
            self._require_active(batch)
            if batch.frozen + amount > batch.remaining():
                raise ValidationError(f"冻结数量超出批次剩余：剩余 {batch.remaining()}，已冻结 {batch.frozen}")
            batch.frozen += amount
            self._append_ledger(
                EntryKind.FREEZE,
                occurred_at=self._clock.now(),
                account_id=batch.account_id,
                amount=amount,
                batch_id=batch_id,
                note=reason,
            )
            return batch

    def unfreeze(self, *, batch_id: str, amount: int, reason: str = "") -> PointBatch:
        """解冻批次内部分额度。"""
        if amount <= 0:
            raise ValidationError("解冻数量必须为正数")
        with self._lock:
            batch = self._batch_or_raise(batch_id)
            if amount > batch.frozen:
                raise ValidationError(f"解冻数量超出冻结额：已冻结 {batch.frozen}")
            batch.frozen -= amount
            self._append_ledger(
                EntryKind.UNFREEZE,
                occurred_at=self._clock.now(),
                account_id=batch.account_id,
                amount=amount,
                batch_id=batch_id,
                note=reason,
            )
            return batch

    def extend(self, *, batch_id: str, days: int, approved_by: str, reason: str = "") -> ExpiryExtension:
        """延期批准：延长批次有效期，不修改任何既有消费记录。"""
        if days <= 0:
            raise ValidationError("延期天数必须为正数")
        if not approved_by:
            raise ValidationError("审批人不能为空")
        with self._lock:
            batch = self._batch_or_raise(batch_id)
            self._require_active(batch)
            now = self._clock.now()
            extension = ExpiryExtension(
                extension_id=self._new_id("extension"),
                batch_id=batch_id,
                account_id=batch.account_id,
                days=days,
                approved_by=approved_by,
                reason=reason,
                granted_at=now,
            )
            self._store.extensions[extension.extension_id] = extension
            batch.extension_days += days
            self._append_ledger(
                EntryKind.EXTEND,
                occurred_at=now,
                account_id=batch.account_id,
                batch_id=batch_id,
                note=f"批准延期 {days} 天（{approved_by}）：{reason}",
            )
            return extension

    # ------------------------------------------------------------------
    # 定时到期（可注入时钟，重复运行幂等）
    # ------------------------------------------------------------------

    def run_expiration(self, now: datetime | None = None) -> ExpirationReport:
        """把到期的 ACTIVE 批次失效，并按批次结转规则生成结转批次。

        批次状态单向流转 ACTIVE → EXPIRED，因此同一时刻（或之后）重复运行
        不会重复失效、不会重复结转。now 缺省取注入时钟的当前时刻。
        """
        run_at = ensure_aware(now) if now is not None else self._clock.now()
        with self._lock:
            results: list[ExpiredBatchInfo] = []
            for batch in sorted(self._store.batches.values(), key=lambda item: item.batch_id):
                if batch.status is not BatchStatus.ACTIVE:
                    continue
                expiry = batch.effective_expiry()
                if expiry > run_at:
                    continue
                remaining = batch.remaining()
                carried = batch.carryover_rule.carried_amount(remaining)
                carryover_batch_id: str | None = None
                if carried > 0:
                    # 结转批次自任务运行时刻起算有效期：任务迟到时不会“出生即过期”，
                    # 同一时刻重复运行也不会级联重复失效。
                    carryover = self._create_carryover(batch, carried, start=run_at)
                    carryover_batch_id = carryover.batch_id
                    self._append_ledger(
                        EntryKind.CARRYOVER_OUT,
                        occurred_at=run_at,
                        account_id=batch.account_id,
                        amount=carried,
                        batch_id=batch.batch_id,
                        note=f"结转至批次 {carryover.batch_id}",
                    )
                    self._append_ledger(
                        EntryKind.CARRYOVER_IN,
                        occurred_at=run_at,
                        account_id=batch.account_id,
                        amount=carried,
                        batch_id=carryover.batch_id,
                        note=f"来自批次 {batch.batch_id} 的到期结转",
                    )
                batch.carried_out += carried
                batch.expired_amount += remaining - carried
                batch.frozen = 0  # 到期后冻结额随批次一并失效
                batch.status = BatchStatus.EXPIRED
                self._append_ledger(
                    EntryKind.EXPIRE,
                    occurred_at=run_at,
                    account_id=batch.account_id,
                    amount=remaining - carried,
                    batch_id=batch.batch_id,
                    note="到期失效",
                )
                results.append(
                    ExpiredBatchInfo(
                        batch_id=batch.batch_id,
                        expired_amount=remaining - carried,
                        carried_amount=carried,
                        carryover_batch_id=carryover_batch_id,
                    )
                )
            return ExpirationReport(
                run_at=run_at,
                expired=tuple(results),
                total_expired=sum(item.expired_amount for item in results),
                total_carried=sum(item.carried_amount for item in results),
            )

    # ------------------------------------------------------------------
    # 查询：余额批次组成、到期预测、追溯
    # ------------------------------------------------------------------

    def balance(self, account_id: str, at: datetime | None = None) -> AccountBalance:
        """任一账户余额的批次组成。"""
        at = ensure_aware(at) if at is not None else self._clock.now()
        items: list[BatchBalance] = []
        for batch in sorted(self._store.batches_of(account_id), key=sort_key):
            items.append(
                BatchBalance(
                    batch_id=batch.batch_id,
                    source=batch.source,
                    scopes=tuple(sorted(batch.scopes)),
                    status=batch.status,
                    policy_id=batch.policy_id,
                    effective_from=batch.effective_from,
                    effective_expiry=batch.effective_expiry(),
                    initial_amount=batch.initial_amount,
                    consumed=batch.consumed,
                    refunded=batch.refunded,
                    expired_amount=batch.expired_amount,
                    carried_out=batch.carried_out,
                    frozen=batch.frozen,
                    remaining=batch.remaining(),
                    available=batch.available(),
                )
            )
        total_remaining = sum(item.remaining for item in items)
        total_available = sum(
            item.available
            for item in items
            if item.status == BatchStatus.ACTIVE and item.effective_from <= at < item.effective_expiry
        )
        return AccountBalance(
            account_id=account_id,
            at=at,
            total_remaining=total_remaining,
            total_available=total_available,
            batches=tuple(items),
        )

    def expiry_forecast(self, account_id: str, *, horizon_days: int = 90, as_of: datetime | None = None) -> ExpiryForecast:
        """未来到期预测：按到期日分桶，已到期未处理的批次归入当日桶。"""
        if horizon_days <= 0:
            raise ValidationError("预测窗口天数必须为正数")
        as_of = ensure_aware(as_of) if as_of is not None else self._clock.now()
        horizon_end = as_of + timedelta(days=horizon_days)
        buckets: dict[str, list[BatchExpiryItem]] = {}
        for batch in self._store.batches_of(account_id):
            if batch.status is not BatchStatus.ACTIVE or batch.remaining() <= 0:
                continue
            expiry = batch.effective_expiry()
            if expiry > horizon_end:
                continue
            day = max(expiry, as_of).date().isoformat()
            buckets.setdefault(day, []).append(BatchExpiryItem(batch_id=batch.batch_id, amount=batch.remaining()))
        ordered = tuple(
            ForecastBucket(date=day, amount=sum(item.amount for item in items), batches=tuple(items))
            for day, items in sorted(buckets.items())
        )
        return ExpiryForecast(
            account_id=account_id,
            as_of=as_of,
            horizon_days=horizon_days,
            total_expiring=sum(bucket.amount for bucket in ordered),
            buckets=ordered,
        )

    def get_consumption(self, consumption_id: str) -> Consumption:
        return self._consumption_or_raise(consumption_id)

    def list_consumptions(self, account_id: str) -> list[Consumption]:
        return sorted(self._store.consumptions_of(account_id), key=lambda item: (item.occurred_at, item.consumption_id))

    def list_refunds(self, account_id: str) -> list[Refund]:
        return sorted(self._store.refunds_of(account_id), key=lambda item: (item.occurred_at, item.refund_id))

    def list_batches(self, account_id: str) -> list[PointBatch]:
        return sorted(self._store.batches_of(account_id), key=sort_key)

    def ledger_of(self, account_id: str) -> list[LedgerEntry]:
        return list(self._store.ledger_of(account_id))

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _resolve_policy(self, policy_id: str | None, *, at: datetime) -> PolicyVersion | None:
        if policy_id is not None:
            policy = self._store.policies.get(policy_id)
            if policy is None:
                raise NotFoundError(f"政策版本不存在：{policy_id}")
            return policy
        return self.current_policy(at)

    def _batch_or_raise(self, batch_id: str) -> PointBatch:
        batch = self._store.batches.get(batch_id)
        if batch is None:
            raise NotFoundError(f"批次不存在：{batch_id}")
        return batch

    def _consumption_or_raise(self, consumption_id: str) -> Consumption:
        consumption = self._store.consumptions.get(consumption_id)
        if consumption is None:
            raise NotFoundError(f"消费记录不存在：{consumption_id}")
        return consumption

    @staticmethod
    def _require_active(batch: PointBatch) -> None:
        if batch.status is not BatchStatus.ACTIVE:
            raise InvalidStateError(f"批次 {batch.batch_id} 已失效，不能执行该操作")

    @staticmethod
    def _refund_shares(consumption: Consumption, amount: int) -> list[tuple[str, int]]:
        """按原分配比例分摊退回额：向下取整，余数按原分配顺序逐批补 1。

        结果确定：同样的消费记录与退回额永远得到同样的分摊。
        """
        shares: list[list[object]] = [[item.batch_id, 0] for item in consumption.allocations]
        total = consumption.amount
        assigned = 0
        for index, item in enumerate(consumption.allocations):
            share = item.amount * amount // total
            shares[index][1] = share
            assigned += share
        remainder = amount - assigned  # 余数恒小于分配笔数，每批最多补 1
        index = 0
        while remainder > 0:
            target = index % len(shares)
            shares[target][1] = int(shares[target][1]) + 1
            remainder -= 1
            index += 1
        return [(str(batch_id), int(share)) for batch_id, share in shares if int(share) > 0]

    def _create_compensation(self, source_batch: PointBatch, amount: int, now: datetime) -> PointBatch:
        """为退回到已失效批次的额度生成补偿批次。"""
        policy = self.current_policy(now)
        validity_days = policy.default_validity_days if policy else 365
        rule = policy.default_carryover if policy else CarryoverRule()
        batch = PointBatch(
            batch_id=self._new_id("batch"),
            account_id=source_batch.account_id,
            source=GrantSource.COMPENSATION,
            scopes=source_batch.scopes,
            effective_from=now,
            expires_at=now + timedelta(days=validity_days),
            extension_days=0,
            carryover_rule=rule,
            policy_id=policy.policy_id if policy else source_batch.policy_id,
            initial_amount=amount,
            created_at=now,
        )
        self._store.batches[batch.batch_id] = batch
        self._append_ledger(
            EntryKind.GRANT,
            occurred_at=now,
            account_id=batch.account_id,
            amount=amount,
            batch_id=batch.batch_id,
            note=f"退回到已失效批次 {source_batch.batch_id} 的补偿批次",
        )
        return batch

    def _create_carryover(self, source_batch: PointBatch, amount: int, *, start: datetime) -> PointBatch:
        """按源批次结转规则生成结转批次：生效期自到期任务运行时刻起算。"""
        policy = self.current_policy(start)
        rule = policy.default_carryover if policy else CarryoverRule()
        batch = PointBatch(
            batch_id=self._new_id("batch"),
            account_id=source_batch.account_id,
            source=GrantSource.CARRYOVER,
            scopes=source_batch.scopes,
            effective_from=start,
            expires_at=start + timedelta(days=source_batch.carryover_rule.validity_days),
            extension_days=0,
            carryover_rule=rule,
            policy_id=policy.policy_id if policy else source_batch.policy_id,
            initial_amount=amount,
            created_at=start,
        )
        self._store.batches[batch.batch_id] = batch
        return batch

    def _append_ledger(
        self,
        kind: EntryKind,
        *,
        occurred_at: datetime,
        account_id: str | None = None,
        amount: int | None = None,
        batch_id: str | None = None,
        consumption_id: str | None = None,
        refund_id: str | None = None,
        detail: tuple[Allocation, ...] = (),
        note: str = "",
    ) -> LedgerEntry:
        entry = LedgerEntry(
            entry_id=self._new_id("entry"),
            kind=kind,
            occurred_at=occurred_at,
            account_id=account_id,
            amount=amount,
            batch_id=batch_id,
            consumption_id=consumption_id,
            refund_id=refund_id,
            detail=detail,
            note=note,
        )
        self._store.ledger.append(entry)
        return entry
