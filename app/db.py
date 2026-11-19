"""SQLite 连接、建表与启动自愈。

并发模型：
- 写操作由 services 层的进程内 RLock 串行化；
- SQLite 开启 WAL + busy_timeout，配合 ``BEGIN IMMEDIATE`` 事务；
- 每次上传在同一个事务内完成“替换源数据 + 重算结论”，任何步骤失败整体
  ROLLBACK，旧数据与旧结论原样保留，绝不会出现源数据已换而结论仍旧的混版。
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from . import config

SCHEMA_SQL = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

-- 每个 (kind, file_date) 保留所见的最新版号，文件按版号整体替换。
CREATE TABLE IF NOT EXISTS source_versions (
    kind        TEXT NOT NULL CHECK (kind IN ('plan', 'landing')),
    file_date   TEXT NOT NULL,
    version     INTEGER NOT NULL,
    uploaded_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    PRIMARY KEY (kind, file_date)
);

CREATE TABLE IF NOT EXISTS plan_entries (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_date       TEXT NOT NULL,
    version         INTEGER NOT NULL,
    flight_no       TEXT NOT NULL,
    destination     TEXT NOT NULL,
    sched_arr_min   INTEGER NOT NULL,
    status          TEXT NOT NULL CHECK (status IN ('normal', 'cancelled')),
    UNIQUE (plan_date, version, flight_no)
);

CREATE TABLE IF NOT EXISTS landing_entries (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    landing_date    TEXT NOT NULL,
    plan_date       TEXT NOT NULL,
    version         INTEGER NOT NULL,
    flight_no       TEXT NOT NULL,
    actual_airport  TEXT NOT NULL,
    actual_arr_min  INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_plan_lookup
    ON plan_entries (plan_date, flight_no);
CREATE INDEX IF NOT EXISTS idx_landing_plan_date
    ON landing_entries (plan_date, flight_no);
CREATE INDEX IF NOT EXISTS idx_landing_file_date
    ON landing_entries (landing_date);

-- 每个 (计划起飞日, 航班) 一行结论；源清单不齐全时该日没有任何结论行。
-- source_versions 记录结论所用的三个版号，杜绝混版。
CREATE TABLE IF NOT EXISTS arrival_facts (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_date        TEXT NOT NULL,
    flight_no        TEXT NOT NULL,
    fact_type        TEXT NOT NULL CHECK (fact_type IN
                         ('arrived', 'cancelled', 'diverted', 'review')),
    sched_airport    TEXT,
    actual_airport   TEXT,
    delay_min        INTEGER,
    review_reasons   TEXT,          -- JSON 数组，仅 fact_type='review' 时有值
    landing_count    INTEGER NOT NULL DEFAULT 0,
    plan_version     INTEGER NOT NULL,
    landing_d0_ver   INTEGER NOT NULL,
    landing_d1_ver   INTEGER NOT NULL,
    UNIQUE (plan_date, flight_no)
);
"""


def connect(db_file: Path | str | None = None) -> sqlite3.Connection:
    path = Path(db_file) if db_file is not None else config.db_path()
    conn = sqlite3.connect(str(path), timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_db(db_file: Path | str | None = None) -> None:
    conn = connect(db_file)
    try:
        conn.executescript(SCHEMA_SQL)
    finally:
        conn.close()


@contextmanager
def immediate_tx(conn: sqlite3.Connection):
    """立即拿写锁的事务，失败自动回滚。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def get_version(conn: sqlite3.Connection, kind: str, file_date: str) -> int | None:
    row = conn.execute(
        "SELECT version FROM source_versions WHERE kind=? AND file_date=?",
        (kind, file_date),
    ).fetchone()
    return None if row is None else row["version"]


def replace_source_version(
    conn: sqlite3.Connection,
    kind: str,
    file_date: str,
    version: int,
) -> None:
    conn.execute(
        """
        INSERT INTO source_versions (kind, file_date, version)
        VALUES (?, ?, ?)
        ON CONFLICT (kind, file_date) DO UPDATE SET
            version=excluded.version,
            uploaded_at=strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
        """,
        (kind, file_date, version),
    )


def insert_plan_rows(
    conn: sqlite3.Connection,
    plan_date: str,
    version: int,
    rows: Iterable[dict[str, Any]],
) -> None:
    conn.executemany(
        """
        INSERT INTO plan_entries
            (plan_date, version, flight_no, destination, sched_arr_min, status)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        [
            (
                plan_date,
                version,
                r["flight_no"],
                r["destination"],
                r["sched_arr_min"],
                r["status"],
            )
            for r in rows
        ],
    )


def insert_landing_rows(
    conn: sqlite3.Connection,
    version: int,
    rows: Iterable[Any],
) -> None:
    conn.executemany(
        """
        INSERT INTO landing_entries
            (landing_date, plan_date, version, flight_no,
             actual_airport, actual_arr_min)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        [
            (
                r.landing_date,
                r.plan_date,
                version,
                r.flight_no,
                r.actual_airport,
                r.actual_arr_min,
            )
            for r in rows
        ],
    )


def list_plan_dates(conn: sqlite3.Connection) -> list[str]:
    return [
        r["file_date"]
        for r in conn.execute(
            "SELECT file_date FROM source_versions WHERE kind='plan' ORDER BY 1"
        )
    ]


def upsert_fact(conn: sqlite3.Connection, fact: dict[str, Any]) -> None:
    conn.execute(
        """
        INSERT INTO arrival_facts (
            plan_date, flight_no, fact_type, sched_airport, actual_airport,
            delay_min, review_reasons, landing_count,
            plan_version, landing_d0_ver, landing_d1_ver)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (plan_date, flight_no) DO UPDATE SET
            flight_no=excluded.flight_no,
            fact_type=excluded.fact_type,
            sched_airport=excluded.sched_airport,
            actual_airport=excluded.actual_airport,
            delay_min=excluded.delay_min,
            review_reasons=excluded.review_reasons,
            landing_count=excluded.landing_count,
            plan_version=excluded.plan_version,
            landing_d0_ver=excluded.landing_d0_ver,
            landing_d1_ver=excluded.landing_d1_ver
        """,
        (
            fact["plan_date"],
            fact["flight_no"],
            fact["fact_type"],
            fact["sched_airport"],
            fact["actual_airport"],
            fact["delay_min"],
            json.dumps(fact["review_reasons"], ensure_ascii=False)
            if fact["review_reasons"] is not None
            else None,
            fact["landing_count"],
            fact["plan_version"],
            fact["landing_d0_ver"],
            fact["landing_d1_ver"],
        ),
    )
