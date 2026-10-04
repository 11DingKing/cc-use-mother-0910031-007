"""HTTP API：FastAPI 适配层，只做参数校验、序列化与错误映射。"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .errors import DomainError, InsufficientPointsError, InvalidStateError, NotFoundError, ValidationError
from .models import (
    AccountBalance,
    CarryoverRule,
    Consumption,
    ExpirationReport,
    ExpiryExtension,
    ExpiryForecast,
    GrantSource,
    LedgerEntry,
    PointBatch,
    PolicyVersion,
    Refund,
)
from .service import PointsService


# ----------------------------------------------------------------------
# 请求模型
# ----------------------------------------------------------------------


class CarryoverRuleIn(BaseModel):
    allowed: bool = False
    cap_permille: int = Field(default=1000, ge=0, le=1000)
    validity_days: int = Field(default=365, gt=0)

    def to_domain(self) -> CarryoverRule:
        return CarryoverRule(
            allowed=self.allowed,
            cap_permille=self.cap_permille,
            validity_days=self.validity_days,
        )


class PublishPolicyIn(BaseModel):
    name: str
    effective_from: datetime
    default_validity_days: int = Field(gt=0)
    default_carryover: CarryoverRuleIn = CarryoverRuleIn()


class GrantIn(BaseModel):
    amount: int = Field(gt=0)
    source: GrantSource = GrantSource.ANNUAL_GRANT
    scopes: list[str] = []
    effective_from: datetime
    expires_at: datetime | None = None
    carryover_rule: CarryoverRuleIn | None = None
    policy_id: str | None = None


class ConsumeIn(BaseModel):
    amount: int = Field(gt=0)
    scope: str = "general"
    request_id: str | None = None
    note: str = ""


class RefundIn(BaseModel):
    amount: int = Field(gt=0)
    note: str = ""


class FreezeIn(BaseModel):
    amount: int = Field(gt=0)
    reason: str = ""


class UnfreezeIn(BaseModel):
    amount: int = Field(gt=0)
    reason: str = ""


class ExtendIn(BaseModel):
    days: int = Field(gt=0)
    approved_by: str
    reason: str = ""


class ExpireJobIn(BaseModel):
    now: datetime | None = None  # 缺省使用服务端注入的时钟


# ----------------------------------------------------------------------
# 序列化
# ----------------------------------------------------------------------


def _dt(value: datetime) -> str:
    return value.isoformat()


def _rule_json(rule: CarryoverRule) -> dict[str, Any]:
    return {
        "allowed": rule.allowed,
        "cap_permille": rule.cap_permille,
        "validity_days": rule.validity_days,
    }


def _policy_json(policy: PolicyVersion) -> dict[str, Any]:
    return {
        "policy_id": policy.policy_id,
        "name": policy.name,
        "effective_from": _dt(policy.effective_from),
        "default_validity_days": policy.default_validity_days,
        "default_carryover": _rule_json(policy.default_carryover),
        "published_at": _dt(policy.published_at),
    }


def _batch_json(batch: PointBatch) -> dict[str, Any]:
    return {
        "batch_id": batch.batch_id,
        "account_id": batch.account_id,
        "source": batch.source.value,
        "scopes": sorted(batch.scopes),
        "effective_from": _dt(batch.effective_from),
        "expires_at": _dt(batch.expires_at),
        "extension_days": batch.extension_days,
        "effective_expiry": _dt(batch.effective_expiry()),
        "carryover_rule": _rule_json(batch.carryover_rule),
        "policy_id": batch.policy_id,
        "initial_amount": batch.initial_amount,
        "consumed": batch.consumed,
        "refunded": batch.refunded,
        "expired_amount": batch.expired_amount,
        "carried_out": batch.carried_out,
        "frozen": batch.frozen,
        "remaining": batch.remaining(),
        "available": batch.available(),
        "status": batch.status.value,
        "created_at": _dt(batch.created_at),
    }


def _allocations_json(allocations: tuple) -> list[dict[str, Any]]:
    return [{"batch_id": item.batch_id, "amount": item.amount} for item in allocations]


def _consumption_json(consumption: Consumption) -> dict[str, Any]:
    return {
        "consumption_id": consumption.consumption_id,
        "account_id": consumption.account_id,
        "amount": consumption.amount,
        "scope": consumption.scope,
        "occurred_at": _dt(consumption.occurred_at),
        "allocations": _allocations_json(consumption.allocations),
        "note": consumption.note,
    }


def _refund_json(refund: Refund) -> dict[str, Any]:
    return {
        "refund_id": refund.refund_id,
        "consumption_id": refund.consumption_id,
        "account_id": refund.account_id,
        "amount": refund.amount,
        "occurred_at": _dt(refund.occurred_at),
        "allocations": _allocations_json(refund.allocations),
        "note": refund.note,
    }


def _extension_json(extension: ExpiryExtension) -> dict[str, Any]:
    return {
        "extension_id": extension.extension_id,
        "batch_id": extension.batch_id,
        "account_id": extension.account_id,
        "days": extension.days,
        "approved_by": extension.approved_by,
        "reason": extension.reason,
        "granted_at": _dt(extension.granted_at),
    }


def _balance_json(balance: AccountBalance) -> dict[str, Any]:
    return {
        "account_id": balance.account_id,
        "at": _dt(balance.at),
        "total_remaining": balance.total_remaining,
        "total_available": balance.total_available,
        "batches": [
            {
                "batch_id": item.batch_id,
                "source": item.source.value,
                "scopes": list(item.scopes),
                "status": item.status.value,
                "policy_id": item.policy_id,
                "effective_from": _dt(item.effective_from),
                "effective_expiry": _dt(item.effective_expiry),
                "initial_amount": item.initial_amount,
                "consumed": item.consumed,
                "refunded": item.refunded,
                "expired_amount": item.expired_amount,
                "carried_out": item.carried_out,
                "frozen": item.frozen,
                "remaining": item.remaining,
                "available": item.available,
            }
            for item in balance.batches
        ],
    }


def _forecast_json(forecast: ExpiryForecast) -> dict[str, Any]:
    return {
        "account_id": forecast.account_id,
        "as_of": _dt(forecast.as_of),
        "horizon_days": forecast.horizon_days,
        "total_expiring": forecast.total_expiring,
        "buckets": [
            {
                "date": bucket.date,
                "amount": bucket.amount,
                "batches": [{"batch_id": item.batch_id, "amount": item.amount} for item in bucket.batches],
            }
            for bucket in forecast.buckets
        ],
    }


def _report_json(report: ExpirationReport) -> dict[str, Any]:
    return {
        "run_at": _dt(report.run_at),
        "total_expired": report.total_expired,
        "total_carried": report.total_carried,
        "expired": [
            {
                "batch_id": item.batch_id,
                "expired_amount": item.expired_amount,
                "carried_amount": item.carried_amount,
                "carryover_batch_id": item.carryover_batch_id,
            }
            for item in report.expired
        ],
    }


def _ledger_json(entry: LedgerEntry) -> dict[str, Any]:
    return {
        "entry_id": entry.entry_id,
        "kind": entry.kind.value,
        "occurred_at": _dt(entry.occurred_at),
        "account_id": entry.account_id,
        "amount": entry.amount,
        "batch_id": entry.batch_id,
        "consumption_id": entry.consumption_id,
        "refund_id": entry.refund_id,
        "detail": _allocations_json(entry.detail),
        "note": entry.note,
    }


# ----------------------------------------------------------------------
# 应用装配
# ----------------------------------------------------------------------


def build_app(service: PointsService) -> FastAPI:
    """把应用服务包装成 HTTP API。"""
    app = FastAPI(title="积分结转到期治理", version="0.2.0")

    @app.exception_handler(NotFoundError)
    async def _not_found(_: Request, exc: NotFoundError) -> JSONResponse:
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.exception_handler(InsufficientPointsError)
    async def _insufficient(_: Request, exc: InsufficientPointsError) -> JSONResponse:
        return JSONResponse(
            status_code=409,
            content={"detail": str(exc), "requested": exc.requested, "available": exc.available},
        )

    @app.exception_handler(InvalidStateError)
    async def _invalid_state(_: Request, exc: InvalidStateError) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(ValidationError)
    @app.exception_handler(DomainError)
    async def _domain_error(_: Request, exc: DomainError) -> JSONResponse:
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    # ---------------- 政策版本 ----------------

    @app.post("/policies", status_code=201)
    def publish_policy(payload: PublishPolicyIn) -> dict[str, Any]:
        policy = service.publish_policy(
            name=payload.name,
            effective_from=payload.effective_from,
            default_validity_days=payload.default_validity_days,
            default_carryover=payload.default_carryover.to_domain(),
        )
        return _policy_json(policy)

    @app.get("/policies")
    def list_policies() -> list[dict[str, Any]]:
        return [_policy_json(policy) for policy in service.list_policies()]

    # ---------------- 批次发放 ----------------

    @app.post("/accounts/{account_id}/grants", status_code=201)
    def grant(account_id: str, payload: GrantIn) -> dict[str, Any]:
        batch = service.grant(
            account_id=account_id,
            amount=payload.amount,
            source=payload.source,
            scopes=payload.scopes,
            effective_from=payload.effective_from,
            expires_at=payload.expires_at,
            carryover_rule=payload.carryover_rule.to_domain() if payload.carryover_rule else None,
            policy_id=payload.policy_id,
        )
        return _batch_json(batch)

    @app.get("/accounts/{account_id}/batches")
    def list_batches(account_id: str) -> list[dict[str, Any]]:
        return [_batch_json(batch) for batch in service.list_batches(account_id)]

    # ---------------- 消费与退回 ----------------

    @app.post("/accounts/{account_id}/consumptions", status_code=201)
    def consume(account_id: str, payload: ConsumeIn) -> dict[str, Any]:
        consumption = service.consume(
            account_id=account_id,
            amount=payload.amount,
            scope=payload.scope,
            request_id=payload.request_id,
            note=payload.note,
        )
        return _consumption_json(consumption)

    @app.get("/accounts/{account_id}/consumptions")
    def list_consumptions(account_id: str) -> list[dict[str, Any]]:
        return [_consumption_json(item) for item in service.list_consumptions(account_id)]

    @app.get("/consumptions/{consumption_id}")
    def get_consumption(consumption_id: str) -> dict[str, Any]:
        return _consumption_json(service.get_consumption(consumption_id))

    @app.post("/consumptions/{consumption_id}/refunds", status_code=201)
    def refund(consumption_id: str, payload: RefundIn) -> dict[str, Any]:
        refund = service.refund(consumption_id=consumption_id, amount=payload.amount, note=payload.note)
        return _refund_json(refund)

    @app.get("/accounts/{account_id}/refunds")
    def list_refunds(account_id: str) -> list[dict[str, Any]]:
        return [_refund_json(item) for item in service.list_refunds(account_id)]

    # ---------------- 冻结 / 解冻 / 延期 ----------------

    @app.post("/batches/{batch_id}/freezes", status_code=201)
    def freeze(batch_id: str, payload: FreezeIn) -> dict[str, Any]:
        return _batch_json(service.freeze(batch_id=batch_id, amount=payload.amount, reason=payload.reason))

    @app.post("/batches/{batch_id}/unfreezes", status_code=201)
    def unfreeze(batch_id: str, payload: UnfreezeIn) -> dict[str, Any]:
        return _batch_json(service.unfreeze(batch_id=batch_id, amount=payload.amount, reason=payload.reason))

    @app.post("/batches/{batch_id}/extensions", status_code=201)
    def extend(batch_id: str, payload: ExtendIn) -> dict[str, Any]:
        extension = service.extend(
            batch_id=batch_id,
            days=payload.days,
            approved_by=payload.approved_by,
            reason=payload.reason,
        )
        return _extension_json(extension)

    # ---------------- 定时到期 ----------------

    @app.post("/jobs/expire")
    def run_expiration(payload: ExpireJobIn | None = None) -> dict[str, Any]:
        return _report_json(service.run_expiration(payload.now if payload else None))

    # ---------------- 查询 ----------------

    @app.get("/accounts/{account_id}/balance")
    def balance(account_id: str, at: datetime | None = Query(default=None)) -> dict[str, Any]:
        return _balance_json(service.balance(account_id, at=at))

    @app.get("/accounts/{account_id}/expiry-forecast")
    def expiry_forecast(
        account_id: str,
        horizon_days: int = Query(default=90, gt=0),
        as_of: datetime | None = Query(default=None),
    ) -> dict[str, Any]:
        return _forecast_json(service.expiry_forecast(account_id, horizon_days=horizon_days, as_of=as_of))

    @app.get("/accounts/{account_id}/ledger")
    def ledger(account_id: str) -> list[dict[str, Any]]:
        return [_ledger_json(entry) for entry in service.ledger_of(account_id)]

    return app
