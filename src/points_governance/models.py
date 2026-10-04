"""领域模型：批次化积分余额、不可变消费记录与只增账本。

不变量（对应 domain/contract.json）：
- 批次化余额：账户余额是各批次剩余额之和，不存在脱离批次的“总账数字”；
- 确定性扣减顺序：消费按批次自身字段排序分配，与遍历顺序无关；
- 消费分配追溯：消费与退回的批次明细随记录持久化，且记录本身不可变；
- 既有消费不可修改：延期、冻结、解冻、退回、政策换版只追加新记录。
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime, timedelta


class GrantSource(enum.StrEnum):
    """积分来源。"""

    ANNUAL_GRANT = "ANNUAL_GRANT"  # 年度发放
    CARRYOVER = "CARRYOVER"  # 到期结转
    COMPENSATION = "COMPENSATION"  # 补偿（含退回到已失效批次时生成的补偿批次）
    CAMPAIGN = "CAMPAIGN"  # 活动赠送


class BatchStatus(enum.StrEnum):
    """批次生命周期：单向流转 ACTIVE → EXPIRED，保证到期任务幂等。"""

    ACTIVE = "ACTIVE"
    EXPIRED = "EXPIRED"


class EntryKind(enum.StrEnum):
    """账本事件类型。"""

    POLICY_PUBLISH = "POLICY_PUBLISH"  # 政策换版
    GRANT = "GRANT"  # 批次发放
    CONSUME = "CONSUME"  # 消费扣减
    REFUND = "REFUND"  # 消费退回
    FREEZE = "FREEZE"  # 冻结
    UNFREEZE = "UNFREEZE"  # 解冻
    EXTEND = "EXTEND"  # 延期批准
    EXPIRE = "EXPIRE"  # 到期失效
    CARRYOVER_OUT = "CARRYOVER_OUT"  # 到期转出（旧批次）
    CARRYOVER_IN = "CARRYOVER_IN"  # 结转接收（新批次）


@dataclass(frozen=True)
class CarryoverRule:
    """结转规则快照：批次创建时从政策版本复制，此后不随政策换版变化。"""

    allowed: bool = False
    cap_permille: int = 1000  # 结转上限：相对到期剩余额的千分比（1000 = 全部结转）
    validity_days: int = 365  # 结转生成的新批次有效期天数

    def __post_init__(self) -> None:
        if not 0 <= self.cap_permille <= 1000:
            raise ValueError("结转上限千分比必须在 0..1000 之间")
        if self.validity_days <= 0:
            raise ValueError("结转有效期天数必须为正数")

    def carried_amount(self, remaining: int) -> int:
        """到期剩余额中可结转的数量（向下取整，结果确定）。"""
        if not self.allowed or remaining <= 0:
            return 0
        return remaining * self.cap_permille // 1000


@dataclass(frozen=True)
class PolicyVersion:
    """政策版本：换版只影响之后新建的批次，既有批次与既有消费不受影响。"""

    policy_id: str
    name: str
    effective_from: datetime
    default_validity_days: int  # 新批次默认有效期天数
    default_carryover: CarryoverRule  # 新批次默认结转规则
    published_at: datetime


@dataclass(frozen=True)
class Allocation:
    """分配明细：某个批次承担了多少数量。"""

    batch_id: str
    amount: int


@dataclass
class PointBatch:
    """积分批次：记录来源、可用范围、生效期与结转规则。

    聚合字段（consumed/refunded/expired_amount/carried_out/frozen）是账本事件的
    冗余汇总，便于读取；账本（LedgerEntry）是审计依据。批次基础字段创建后不变，
    延期通过 extension_days 累计表达，expires_at 原值永不改写。
    """

    batch_id: str
    account_id: str
    source: GrantSource
    scopes: frozenset[str]  # 可用范围（消费类目）；空集表示全场通用
    effective_from: datetime  # 生效起点（含）
    expires_at: datetime  # 基础到期时刻（不含），原始值不变
    extension_days: int  # 已批准延期天数累计
    carryover_rule: CarryoverRule  # 结转规则快照
    policy_id: str  # 创建时所属政策版本
    initial_amount: int
    created_at: datetime
    consumed: int = 0
    refunded: int = 0
    expired_amount: int = 0
    carried_out: int = 0
    frozen: int = 0
    status: BatchStatus = BatchStatus.ACTIVE

    def effective_expiry(self) -> datetime:
        """考虑延期后的实际到期时刻。"""
        return self.expires_at + timedelta(days=self.extension_days)

    def remaining(self) -> int:
        """账面剩余（含冻结部分）；到期后恒为 0。"""
        return self.initial_amount - self.consumed + self.refunded - self.expired_amount - self.carried_out

    def available(self) -> int:
        """可扣减额度：账面剩余减去冻结。"""
        return self.remaining() - self.frozen


@dataclass(frozen=True)
class Consumption:
    """消费记录：创建后不可修改；退回只追加 Refund，不改写本记录。"""

    consumption_id: str
    account_id: str
    amount: int
    scope: str  # 消费类目，用于匹配批次可用范围
    occurred_at: datetime
    allocations: tuple[Allocation, ...]  # 批次分配明细（追溯依据）
    note: str = ""


@dataclass(frozen=True)
class Refund:
    """退回记录：引用原消费，按原分配比例退回各批次；不修改原消费。"""

    refund_id: str
    consumption_id: str
    account_id: str
    amount: int
    occurred_at: datetime
    allocations: tuple[Allocation, ...]  # 实际落账批次（原批次已失效时指向补偿批次）
    note: str = ""


@dataclass(frozen=True)
class ExpiryExtension:
    """延期批准记录：只延长批次有效期，不触碰任何既有消费。"""

    extension_id: str
    batch_id: str
    account_id: str
    days: int
    approved_by: str
    reason: str
    granted_at: datetime


@dataclass(frozen=True)
class LedgerEntry:
    """账本事件：只增不改，全部余额变化的审计流。"""

    entry_id: str
    kind: EntryKind
    occurred_at: datetime
    account_id: str | None = None
    amount: int | None = None
    batch_id: str | None = None
    consumption_id: str | None = None
    refund_id: str | None = None
    detail: tuple[Allocation, ...] = ()
    note: str = ""


@dataclass(frozen=True)
class BatchBalance:
    """余额批次组成项。"""

    batch_id: str
    source: GrantSource
    scopes: tuple[str, ...]
    status: BatchStatus
    policy_id: str
    effective_from: datetime
    effective_expiry: datetime
    initial_amount: int
    consumed: int
    refunded: int
    expired_amount: int
    carried_out: int
    frozen: int
    remaining: int
    available: int


@dataclass(frozen=True)
class AccountBalance:
    """账户余额：总额 + 批次组成。"""

    account_id: str
    at: datetime
    total_remaining: int  # 全部批次账面剩余之和
    total_available: int  # 当前生效且未冻结部分之和
    batches: tuple[BatchBalance, ...]


@dataclass(frozen=True)
class BatchExpiryItem:
    """到期预测中的批次明细。"""

    batch_id: str
    amount: int


@dataclass(frozen=True)
class ForecastBucket:
    """同一到期日的聚合桶。"""

    date: str  # ISO 日期
    amount: int
    batches: tuple[BatchExpiryItem, ...]


@dataclass(frozen=True)
class ExpiryForecast:
    """未来到期预测：按到期日分桶。"""

    account_id: str
    as_of: datetime
    horizon_days: int
    total_expiring: int
    buckets: tuple[ForecastBucket, ...]


@dataclass(frozen=True)
class ExpiredBatchInfo:
    """单次到期运行中某个批次的处理结果。"""

    batch_id: str
    expired_amount: int
    carried_amount: int
    carryover_batch_id: str | None


@dataclass(frozen=True)
class ExpirationReport:
    """到期任务运行报告：重复运行同一时刻应得到空报告。"""

    run_at: datetime
    expired: tuple[ExpiredBatchInfo, ...]
    total_expired: int
    total_carried: int
