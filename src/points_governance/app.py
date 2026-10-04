"""应用装配：创建服务与 HTTP 应用。"""
from __future__ import annotations

from fastapi import FastAPI

from .api import build_app
from .clock import Clock
from .service import PointsService
from .store import InMemoryStore


def create_app(*, clock: Clock | None = None, store: InMemoryStore | None = None) -> FastAPI:
    """装配应用：缺省使用系统时钟与内存仓储。"""
    service = PointsService(store=store, clock=clock)
    return build_app(service)


def main() -> None:
    import uvicorn

    uvicorn.run(create_app(), host="0.0.0.0", port=8000)
