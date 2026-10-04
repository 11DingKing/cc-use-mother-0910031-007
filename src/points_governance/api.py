"""HTTP API（标准库 http.server，零三方依赖）。

路由概览
========

管理
* ``POST /admin/accounts``             开户
* ``POST /admin/policies``             发布政策新版本（旧版自动 SUPERSEDED）
* ``GET  /admin/policies``             政策版本列表
* ``POST /admin/expiration/run``       触发到期作业（可注入 as_of / job_key）
* ``GET  /admin/expiration/jobs``      到期作业台账（幂等记录）

批次与消费
* ``POST /accounts/{id}/lots``         发放批次（快照来源/范围/生效期/结转规则）
* ``GET  /accounts/{id}/lots``         批次列表
* ``GET  /lots/{id}``                  批次详情
* ``POST /lots/{id}/freeze``           冻结
* ``POST /lots/{id}/unfreeze``         解冻
* ``POST /lots/{id}/extend``           延期批准（不改既有消费）
* ``GET  /lots/{id}/extensions``       延期审批记录
* ``POST /accounts/{id}/consumptions`` 消费（FEFO 选批并落分配明细）
* ``GET  /accounts/{id}/consumptions`` 消费列表
* ``POST /accounts/{id}/consume-preview`` 消费选批预览
* ``GET  /consumptions/{id}``          消费详情（含分配/退回明细）
* ``POST /consumptions/{id}/refund``   退回

查询
* ``GET  /accounts/{id}/balance``      余额的批次组成（?scope=）
* ``GET  /accounts/{id}/forecast``     未来到期预测（?horizon_days=&scope=）
* ``GET  /events``                     事件审计（?account_id=&lot_id=）
* ``GET  /healthz``
"""
from __future__ import annotations

import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import parse_qs, urlsplit

from .errors import PointsError
from .service import PointsService

Handler = Callable[[BaseHTTPRequestHandler, dict, dict], Any]


class _BadRequest(PointsError):
    code = "bad_request"
    http_status = 400


class Router:
    def __init__(self) -> None:
        self._routes: list[tuple[str, re.Pattern, Handler]] = []

    def add(self, method: str, pattern: str, handler: Handler) -> None:
        self._routes.append((method, re.compile("^" + pattern + "$"), handler))

    def match(self, method: str, path: str):
        for m, regex, handler in self._routes:
            if m != method:
                continue
            match = regex.match(path)
            if match:
                return handler, match.groupdict()
        return None, None


def build_router(service: PointsService) -> Router:
    r = Router()

    # -- 管理 -------------------------------------------------------------
    r.add("GET", "/healthz", lambda h, p, q: {"status": "ok"})

    def create_account(h, p, q):
        body = h.json_body
        return service.create_account(
            body["account_id"], name=body.get("name", "")
        )

    r.add("POST", "/admin/accounts", create_account)

    def create_policy(h, p, q):
        body = h.json_body
        return service.create_policy(
            body["rules"],
            policy_id=body.get("policy_id"),
            effective_from=body.get("effective_from"),
            default_rule_id=body.get("default_rule_id"),
        )

    r.add("POST", "/admin/policies", create_policy)
    r.add("GET", "/admin/policies", lambda h, p, q: service.list_policies())

    def run_expiration(h, p, q):
        body = h.json_body
        return service.run_expiration(
            as_of=body.get("as_of"),
            job_key=body.get("job_key"),
            account_id=body.get("account_id"),
        )

    r.add("POST", "/admin/expiration/run", run_expiration)
    r.add("GET", "/admin/expiration/jobs",
          lambda h, p, q: service.list_expire_jobs())

    # -- 批次 -------------------------------------------------------------
    def grant_lot(h, p, q):
        b = h.json_body
        return service.grant_lot(
            p["account_id"], int(b["amount"]),
            source=b["source"],
            scope=b.get("scope", "*"),
            effective_at=b.get("effective_at"),
            expires_at=b["expires_at"],
            policy_id=b.get("policy_id"),
            rule_id=b.get("rule_id"),
            lot_id=b.get("lot_id"),
        )

    r.add("POST", r"/accounts/(?P<account_id>[^/]+)/lots", grant_lot)

    def list_lots(h, p, q):
        include = q.get("include_terminal", ["false"])[0] == "true"
        lots = service.lots.list_by_account(p["account_id"])
        if not include:
            lots = [l for l in lots if l.status.value not in ("EXPIRED", "CARRIED")]
        return [l.to_dict() for l in lots]

    r.add("GET", r"/accounts/(?P<account_id>[^/]+)/lots", list_lots)
    r.add("GET", r"/lots/(?P<lot_id>[^/]+)",
          lambda h, p, q: service.lots.require(p["lot_id"]).to_dict())
    r.add("GET", r"/lots/(?P<lot_id>[^/]+)/extensions",
          lambda h, p, q: service.list_extensions(p["lot_id"]))

    def freeze(h, p, q):
        return service.freeze_lot(p["lot_id"], reason=h.json_body.get("reason", ""))

    def unfreeze(h, p, q):
        return service.unfreeze_lot(p["lot_id"], reason=h.json_body.get("reason", ""))

    r.add("POST", r"/lots/(?P<lot_id>[^/]+)/freeze", freeze)
    r.add("POST", r"/lots/(?P<lot_id>[^/]+)/unfreeze", unfreeze)

    def extend(h, p, q):
        b = h.json_body
        return service.extend_expiry(
            p["lot_id"], b["new_expires_at"],
            approved_by=b["approved_by"],
            reason=b.get("reason", ""),
        )

    r.add("POST", r"/lots/(?P<lot_id>[^/]+)/extend", extend)

    # -- 消费 -------------------------------------------------------------
    def consume(h, p, q):
        b = h.json_body
        return service.consume(
            p["account_id"], int(b["amount"]),
            scope=b.get("scope", "*"),
            strategy=b.get("strategy", "FEFO"),
            note=b.get("note", ""),
            consumption_id=b.get("consumption_id"),
        )

    r.add("POST", r"/accounts/(?P<account_id>[^/]+)/consumptions", consume)

    def preview(h, p, q):
        b = h.json_body
        return service.preview_consume(
            p["account_id"], int(b["amount"]),
            scope=b.get("scope", "*"),
        )

    r.add("POST", r"/accounts/(?P<account_id>[^/]+)/consume-preview", preview)

    def list_consumptions(h, p, q):
        limit = int(q.get("limit", ["100"])[0])
        return service.list_consumptions(p["account_id"], limit=limit)

    r.add("GET", r"/accounts/(?P<account_id>[^/]+)/consumptions",
          list_consumptions)
    r.add("GET", r"/consumptions/(?P<consumption_id>[^/]+)",
          lambda h, p, q: service.get_consumption(p["consumption_id"]))

    def refund(h, p, q):
        b = h.json_body
        return service.refund(
            p["consumption_id"],
            amount=int(b["amount"]) if b.get("amount") is not None else None,
            reinstatement_days=int(b.get("reinstatement_days", 30)),
            reason=b.get("reason", ""),
        )

    r.add("POST", r"/consumptions/(?P<consumption_id>[^/]+)/refund", refund)

    # -- 查询 -------------------------------------------------------------
    def balance(h, p, q):
        scope = q.get("scope", [None])[0]
        include = q.get("include_terminal", ["false"])[0] == "true"
        return service.get_balance(p["account_id"], scope=scope,
                                   include_terminal=include)

    r.add("GET", r"/accounts/(?P<account_id>[^/]+)/balance", balance)

    def forecast(h, p, q):
        scope = q.get("scope", [None])[0]
        horizon = int(q.get("horizon_days", ["365"])[0])
        return service.expiration_forecast(
            p["account_id"], horizon_days=horizon, scope=scope
        )

    r.add("GET", r"/accounts/(?P<account_id>[^/]+)/forecast", forecast)

    def events(h, p, q):
        return service.list_events(
            account_id=q.get("account_id", [None])[0],
            lot_id=q.get("lot_id", [None])[0],
            limit=int(q.get("limit", ["200"])[0]),
        )

    r.add("GET", "/events", events)
    return r


class APIHandler(BaseHTTPRequestHandler):
    server_version = "PointsGovernance/1.0"

    @property
    def service(self) -> PointsService:
        return self.server.service  # type: ignore[attr-defined]

    @property
    def router(self) -> Router:
        return self.server.router  # type: ignore[attr-defined]

    def _send(self, status: int, payload: Any) -> None:
        data = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            value = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise _BadRequest(str(exc)) from exc
        if not isinstance(value, dict):
            raise _BadRequest("请求体必须是 JSON 对象")
        return value

    @property
    def json_body(self) -> dict:
        cached = getattr(self, "_cached_body", None)
        if cached is None:
            cached = self._read_body()
            self._cached_body = cached
        return cached

    def _dispatch(self, method: str) -> None:
        parts = urlsplit(self.path)
        handler, params = self.router.match(method, parts.path)
        if handler is None:
            self._send(404, {"error": "not_found",
                             "message": f"无此路由：{method} {parts.path}"})
            return
        query = parse_qs(parts.query)
        try:
            result = handler(self, params, query)
        except PointsError as exc:
            self._send(exc.http_status, exc.to_body())
        except (KeyError, TypeError, ValueError) as exc:
            self._send(422, {"error": "validation_error", "message": str(exc)})
        else:
            if result is None:
                result = {"status": "ok"}
            self._send(200, result)

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def log_message(self, fmt: str, *args) -> None:
        # 访问日志交由宿主环境采集；这里保持简洁。
        if self.server.verbose:  # type: ignore[attr-defined]
            super().log_message(fmt, *args)


def create_server(service: PointsService, host: str = "127.0.0.1",
                  port: int = 8080, *, verbose: bool = False) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), APIHandler)
    server.service = service          # type: ignore[attr-defined]
    server.router = build_router(service)  # type: ignore[attr-defined]
    server.verbose = verbose          # type: ignore[attr-defined]
    return server
