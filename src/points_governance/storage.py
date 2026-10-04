"""SQLite 持久化层。

设计约定
========

* 所有业务表只记录状态与只增流水；不做物理删除。
* ``domain_events`` 是追加式事件账本：发放/消费/退回/冻结/解冻/延期/
  政策换版/到期扫描/批次失效/结转，全部留痕，事件只增不改。
* 既有消费（``consumptions`` / ``consumption_allocations``）一经写入
  永不更新——延期、冻结、政策换版等操作只影响后续行为。
* 到期处理在单事务内以状态机推进（ACTIVE -> EXPIRED/CARRIED），
  配合 ``expire_jobs`` 的幂等键，保证重复执行不重复失效、不重复结转。
"""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS policies (
    policy_id       TEXT PRIMARY KEY,
    version         INTEGER NOT NULL UNIQUE,
    effective_from  TEXT NOT NULL,
    state           TEXT NOT NULL,
    default_rule_id TEXT NOT NULL DEFAULT '',
    rules_json      TEXT NOT NULL,
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS accounts (
    account_id TEXT PRIMARY KEY,
    name       TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS lots (
    lot_id            TEXT PRIMARY KEY,
    account_id        TEXT NOT NULL,
    policy_id         TEXT NOT NULL,
    policy_version    INTEGER NOT NULL,
    source            TEXT NOT NULL,
    scope             TEXT NOT NULL,
    granted_amount    INTEGER NOT NULL CHECK (granted_amount > 0),
    remaining         INTEGER NOT NULL CHECK (remaining >= 0),
    effective_at      TEXT NOT NULL,
    expires_at        TEXT NOT NULL,
    status            TEXT NOT NULL,
    rule_id           TEXT NOT NULL,
    rule_kind         TEXT NOT NULL,
    rule_carry_days   INTEGER,
    rule_carry_scope  TEXT,
    remaining_carry_hops INTEGER NOT NULL DEFAULT 0,
    created_at        TEXT NOT NULL,
    expire_job_id     TEXT,
    carried_to_lot_id TEXT,
    frozen_at         TEXT,
    CHECK (remaining <= granted_amount)
);
CREATE INDEX IF NOT EXISTS idx_lots_account ON lots(account_id, status);
CREATE INDEX IF NOT EXISTS idx_lots_expire ON lots(status, expires_at);

CREATE TABLE IF NOT EXISTS consumptions (
    consumption_id TEXT PRIMARY KEY,
    account_id     TEXT NOT NULL,
    amount         INTEGER NOT NULL CHECK (amount > 0),
    scope          TEXT NOT NULL,
    strategy       TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'CONFIRMED',
    note           TEXT NOT NULL DEFAULT '',
    created_at     TEXT NOT NULL,
    refunded_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_cons_account ON consumptions(account_id);

CREATE TABLE IF NOT EXISTS consumption_allocations (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    consumption_id TEXT NOT NULL REFERENCES consumptions(consumption_id),
    lot_id         TEXT NOT NULL,
    amount         INTEGER NOT NULL CHECK (amount > 0),
    lot_expires_at TEXT NOT NULL,
    lot_policy_version INTEGER NOT NULL,
    seq            INTEGER NOT NULL,
    UNIQUE(consumption_id, lot_id)
);
CREATE INDEX IF NOT EXISTS idx_alloc_lot ON consumption_allocations(lot_id);

CREATE TABLE IF NOT EXISTS refunds (
    refund_id      TEXT PRIMARY KEY,
    consumption_id TEXT NOT NULL REFERENCES consumptions(consumption_id),
    lot_id         TEXT NOT NULL,
    amount         INTEGER NOT NULL CHECK (amount > 0),
    target_lot_id  TEXT NOT NULL,
    target_kind    TEXT NOT NULL,
    created_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_refund_cons ON refunds(consumption_id);

CREATE TABLE IF NOT EXISTS lot_extensions (
    extension_id  TEXT PRIMARY KEY,
    lot_id        TEXT NOT NULL,
    old_expires_at TEXT NOT NULL,
    new_expires_at TEXT NOT NULL,
    approved_by   TEXT NOT NULL,
    reason        TEXT NOT NULL DEFAULT '',
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ext_lot ON lot_extensions(lot_id);

CREATE TABLE IF NOT EXISTS expire_jobs (
    job_key    TEXT PRIMARY KEY,
    as_of      TEXT NOT NULL,
    status     TEXT NOT NULL,
    lots_expired   INTEGER NOT NULL,
    lots_carried   INTEGER NOT NULL,
    amount_expired INTEGER NOT NULL,
    amount_carried INTEGER NOT NULL,
    details_json   TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    finished_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS domain_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id    TEXT NOT NULL UNIQUE,
    event_type  TEXT NOT NULL,
    account_id  TEXT,
    lot_id      TEXT,
    occurred_at TEXT NOT NULL,
    payload     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_account ON domain_events(account_id, id);
CREATE INDEX IF NOT EXISTS idx_events_lot ON domain_events(lot_id, id);
"""


class _LockedCursor:
    """对游标的取数操作也加锁，保证读链路上不与写事务交错。"""

    def __init__(self, cursor: sqlite3.Cursor, lock: threading.RLock) -> None:
        self._cursor = cursor
        self._lock = lock

    def fetchone(self):
        with self._lock:
            return self._cursor.fetchone()

    def fetchall(self):
        with self._lock:
            return self._cursor.fetchall()

    def fetchmany(self, size: int):
        with self._lock:
            return self._cursor.fetchmany(size)

    @property
    def rowcount(self) -> int:
        return self._cursor.rowcount

    @property
    def lastrowid(self):
        return self._cursor.lastrowid


class _LockedConnection:
    """单连接代理：每次 execute/取数都串行化。

    内存共享缓存模式下，SQLite 对同表读写冲突直接抛 SQLITE_LOCKED
    （不触发 busy 等待），因此所有连接访问都必须经同一把 RLock 串行化；
    RLock 可重入，事务内逐条 execute 不受影响。
    """

    def __init__(self, real: sqlite3.Connection, lock: threading.RLock) -> None:
        self._real = real
        self._lock = lock

    def execute(self, sql: str, params: Iterable[Any] = ()):
        with self._lock:
            cur = self._real.execute(sql, tuple(params))
        return _LockedCursor(cur, self._lock)

    def executescript(self, script: str) -> None:
        with self._lock:
            self._real.executescript(script)

    def commit(self) -> None:
        with self._lock:
            self._real.commit()

    def rollback(self) -> None:
        with self._lock:
            self._real.rollback()

    def __getattr__(self, name):
        return getattr(self._real, name)


class Database:
    """SQLite 封装：线程局部连接；写事务由服务层用 ``transaction()`` 管理。"""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        self.in_memory = self.path == ":memory:"
        if not self.in_memory:
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._local = threading.local()
        if self.in_memory:
            # 内存模式：全进程单连接 + 锁代理（共享缓存 URI 下同表读写
            # 会立即 SQLITE_LOCKED，不适用 busy_timeout）。
            real = sqlite3.connect(":memory:", isolation_level=None,
                                   check_same_thread=False)
            real.row_factory = sqlite3.Row
            real.executescript(SCHEMA)
            self._mem_conn = _LockedConnection(real, self._lock)
        else:
            init = sqlite3.connect(self.path, isolation_level=None)
            try:
                init.execute("PRAGMA journal_mode=WAL")
                init.executescript(SCHEMA)
            finally:
                init.close()

    def conn(self):
        if self.in_memory:
            return self._mem_conn
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, isolation_level=None,
                                   check_same_thread=False, timeout=30)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=30000")
            self._local.conn = conn
        return conn

    @property
    def lock(self) -> threading.RLock:
        """跨线程串行化写事务，保证到期作业与消费互不踩踏。"""
        return self._lock

    class _Tx:
        def __init__(self, db: "Database") -> None:
            self.db = db

        def __enter__(self) -> sqlite3.Connection:
            self.conn = self.db.conn()
            self.db._lock.acquire()
            self.conn.execute("BEGIN IMMEDIATE")
            return self.conn

        def __exit__(self, exc_type, exc, tb) -> None:
            try:
                if exc_type is None:
                    self.conn.execute("COMMIT")
                else:
                    self.conn.execute("ROLLBACK")
            finally:
                self.db._lock.release()

    def transaction(self) -> "Database._Tx":
        """开启串行化写事务（配合 db.lock 使用）。"""
        return Database._Tx(self)

    # -- 便捷读取 ----------------------------------------------------------

    def query_one(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
        return self.conn().execute(sql, tuple(params)).fetchone()

    def query_all(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        return self.conn().execute(sql, tuple(params)).fetchall()

    def insert_event(self, event_id: str, event_type: str, occurred_at: str,
                     payload: dict, account_id: str | None = None,
                     lot_id: str | None = None) -> None:
        self.conn().execute(
            "INSERT INTO domain_events(event_id, event_type, account_id, lot_id,"
            " occurred_at, payload) VALUES (?,?,?,?,?,?)",
            (event_id, event_type, account_id, lot_id, occurred_at,
             json.dumps(payload, ensure_ascii=False, sort_keys=True)),
        )
