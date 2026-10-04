"""命令行入口：``python -m points_governance --db ./data/points.db``。"""
from __future__ import annotations

import argparse
import signal

from .api import create_server
from .clock import SystemClock
from .scheduler import ExpirationScheduler
from .service import PointsService
from .storage import Database


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="积分结转到期治理服务端")
    parser.add_argument("--host", default="0.0.0.0", help="监听地址")
    parser.add_argument("--port", type=int, default=8080, help="监听端口")
    parser.add_argument("--db", default="./data/points.db",
                        help="SQLite 路径（:memory: 仅用于测试）")
    parser.add_argument("--expire-interval", type=float, default=0.0,
                        help="启用进程内到期调度器的间隔秒数（0=不启用，"
                             "由外部调度调用 API）")
    parser.add_argument("--verbose", action="store_true", help="输出访问日志")
    args = parser.parse_args(argv)

    db = Database(args.db)
    service = PointsService(db, SystemClock())
    server = create_server(service, host=args.host, port=args.port,
                           verbose=args.verbose)
    scheduler = None
    if args.expire_interval > 0:
        scheduler = ExpirationScheduler(
            service, interval_seconds=args.expire_interval)

    def _shutdown(_signum, _frame) -> None:
        server.shutdown()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    print(f"积分治理服务已启动：http://{args.host}:{args.port}  db={args.db}")
    if scheduler is not None:
        scheduler.start(run_immediately=True)
        print(f"进程内到期调度器已启用，间隔 {args.expire_interval}s")
    try:
        server.serve_forever()
    finally:
        if scheduler is not None:
            scheduler.stop()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
