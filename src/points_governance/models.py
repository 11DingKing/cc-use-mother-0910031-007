"""领域模型：企业积分的批次化余额治理。

核心概念
========

* ``Policy``：政策版本。政策可以换版，批次在发放时记录自己属于哪一版，
  之后政策换版不会回溯修改已发放批次（不可变历史）。
* ``CarryoverRule``：结转规则，决定批次到期时余额作废（EXPIRE）还是
  结转到新的批次（CARRYOVER），以及结转批次的有效期与可用范围。
* ``Lot``：一批积分。快照发放时的来源、可用范围、生效/失效时间、
  到期日与结转规则，是余额的最小可追溯单元。
* ``Consumption`` / ``ConsumptionAllocation``：一次消费及其在各批次上的
  确定性分配明细，只增不改。
* ``Refund``：退回。退回把额度恢复到*原批次*（原批次状态决定去向），
  全程不修改既有消费记录。

所有金额一律使用整数最小单位，避免浮点误差。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class PolicyState(str, Enum):
    ACTIVE = "ACTIVE"        # 当前有效版本
    SUPERSEDED = "SUPERSEDED"  # 已被新版本替换（旧批次仍按旧规则运行）


class RuleKind(str, Enum):
    EXPIRE = "EXPIRE"        # 到期余额作废
    CARRYOVER = "CARRYOVER"  # 到期余额结转到新批次


class LotStatus(str, Enum):
    ACTIVE = "ACTIVE"        # 可消费
    FROZEN = "FROZEN"        # 冻结：暂停消费
    EXPIRED = "EXPIRED"      # 已到期失效（可能已触发结转）
    CARRIED = "CARRIED"      # 已结转（余额滚入结转批次）


# 消费时的确定性扣减策略。
# FEFO（First-Expire-First-Out，最早到期优先）优先消耗临近到期的额度，
# 从根本上避免"先用了长期额度、临期额度作废"。
DEDUCTION_STRATEGIES = ("FEFO",)
DEFAULT_STRATEGY = "FEFO"


@dataclass(frozen=True)
class CarryoverRule:
    """批次到期时的结转规则（作为政策版本的一部分，发放时快照进批次）。"""

    rule_id: str
    kind: RuleKind
    # CARRYOVER 时：结转批次在生成日之后多少天到期；None 表示沿用旧批次的 scope
    carry_days: int | None = None
    # 结转批次的可用范围；None 表示沿用旧批次的 scope
    carry_scope: str | None = None
    # 最多允许连续结转的跳数：1 表示结转一次后再到期即作废（常见政策）。
    max_carry_hops: int = 1
    description: str = ""

    def __post_init__(self) -> None:
        if self.kind == RuleKind.CARRYOVER:
            if self.carry_days is None or self.carry_days <= 0:
                raise ValueError("CARRYOVER 规则必须给出正整数 carry_days")
            if not isinstance(self.max_carry_hops, int) or self.max_carry_hops < 1:
                raise ValueError("max_carry_hops 必须是 >=1 的整数")
        else:
            if self.carry_days is not None or self.carry_scope is not None:
                raise ValueError("EXPIRE 规则不能携带结转参数")

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "kind": self.kind.value,
            "carry_days": self.carry_days,
            "carry_scope": self.carry_scope,
            "max_carry_hops": self.max_carry_hops,
            "description": self.description,
        }


@dataclass
class Policy:
    """政策版本。换版 = 发布新版本并标记旧版本 SUPERSEDED。"""

    policy_id: str
    version: int
    effective_from: str
    rules: dict[str, CarryoverRule] = field(default_factory=dict)
    default_rule_id: str = ""
    state: PolicyState = PolicyState.ACTIVE
    created_at: str = ""

    def __post_init__(self) -> None:
        if not self.default_rule_id:
            self.default_rule_id = next(iter(self.rules))
        if self.default_rule_id not in self.rules:
            raise ValueError("default_rule_id 必须指向政策内的规则")

    def to_dict(self) -> dict[str, Any]:
        return {
            "policy_id": self.policy_id,
            "version": self.version,
            "effective_from": self.effective_from,
            "state": self.state.value,
            "default_rule_id": self.default_rule_id,
            "created_at": self.created_at,
            "rules": {k: v.to_dict() for k, v in self.rules.items()},
        }


@dataclass
class Lot:
    """一批积分：余额的最小可追溯单元。"""

    lot_id: str
    account_id: str
    policy_id: str
    policy_version: int
    source: str                 # 来源（如 2024年度申报返还、活动奖励）
    scope: str                  # 可用范围，"*" 表示全场景通用
    granted_amount: int         # 发放总额（不变）
    remaining: int              # 当前剩余（消费减少、退回恢复、结转清零）
    effective_at: str           # 生效时间（含）
    expires_at: str             # 到期时间（不含：该时刻起不可消费并进入到期处理）
    status: LotStatus
    rule_id: str
    rule_kind: RuleKind
    rule_carry_days: int | None
    rule_carry_scope: str | None
    created_at: str
    # 剩余可结转跳数：发放时取规则 max_carry_hops，每结转折减 1；
    # 归零时结转批的 rule_kind 退化为 EXPIRE。EXPIRE 规则下为 0。
    remaining_carry_hops: int = 0
    # 结转链路：expire_job_id 记录由哪次到期作业处理；carried_to_lot_id 指向新批次
    expire_job_id: str | None = None
    carried_to_lot_id: str | None = None
    frozen_at: str | None = None

    @property
    def frozen(self) -> bool:
        return self.status == LotStatus.FROZEN

    def to_dict(self) -> dict[str, Any]:
        return {
            "lot_id": self.lot_id,
            "account_id": self.account_id,
            "policy_id": self.policy_id,
            "policy_version": self.policy_version,
            "source": self.source,
            "scope": self.scope,
            "granted_amount": self.granted_amount,
            "remaining": self.remaining,
            "consumed_amount": self.granted_amount - self.remaining,
            "effective_at": self.effective_at,
            "expires_at": self.expires_at,
            "status": self.status.value,
            "rule": {
                "rule_id": self.rule_id,
                "kind": self.rule_kind.value,
                "carry_days": self.rule_carry_days,
                "carry_scope": self.rule_carry_scope,
                "remaining_carry_hops": self.remaining_carry_hops,
            },
            "created_at": self.created_at,
            "expire_job_id": self.expire_job_id,
            "carried_to_lot_id": self.carried_to_lot_id,
            "frozen_at": self.frozen_at,
        }
