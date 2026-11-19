"""核心业务：按版号替换源文件、重算到达事实、查询与启动自愈。

待核原因（review_reasons）代码：
- CANCELLED_BUT_LANDED   取消却落地
- NORMAL_MISSING_LANDING 正常却缺落地
- ORPHAN_LANDING         有落地清单但计划清单中无对应航班
- MULTIPLE_LANDINGS      同航班多次落地
"""

from __future__ import annotations

import threading
from typing import Any

from . import csv_parser, db
from .csv_parser import LandingRow, PlanRow, add_days

# 进程内串行化所有写操作；RLock 允许启动修复与上传路径复用同一把锁。
_WRITE_LOCK = threading.RLock()

REASON_CANCELLED_BUT_LANDED = "CANCELLED_BUT_LANDED"
REASON_NORMAL_MISSING = "NORMAL_MISSING_LANDING"
REASON_ORPHAN = "ORPHAN_LANDING"
REASON_MULTIPLE = "MULTIPLE_LANDINGS"

REASON_TEXT = {
    REASON_CANCELLED_BUT_LANDED: "取消却落地",
    REASON_NORMAL_MISSING: "正常却缺落地",
    REASON_ORPHAN: "落地清单中存在但计划清单无对应航班",
    REASON_MULTIPLE: "同航班多次落地",
}


class VersionConflictError(Exception):
    """新版号不大于已存版号。"""

    def __init__(self, kind: str, file_date: str, old: int, new: int):
        self.kind = kind
        self.file_date = file_date
        self.old = old
        self.new = new
        super().__init__(
            f"{kind} 文件日期 {file_date} 已存在版号 {old}，版号 {new} 不更新（须严格递增）"
        )


def upload_plan(
    plan_date: str,
    version: int,
    rows: list[PlanRow],
    db_file: str | None = None,
) -> dict[str, Any]:
    """整批替换某计划起飞日的计划清单，并重算该日结论。"""
    with _WRITE_LOCK:
        conn = db.connect(db_file)
        try:
            with db.immediate_tx(conn):
                old = db.get_version(conn, "plan", plan_date)
                if old is not None and version <= old:
                    raise VersionConflictError("plan", plan_date, old, version)

                conn.execute(
                    "DELETE FROM plan_entries WHERE plan_date=?", (plan_date,)
                )
                db.insert_plan_rows(conn, plan_date, version, csv_parser.rows_to_dicts(rows))
                db.replace_source_version(conn, "plan", plan_date, version)
                # 计划只服务于本计划日。
                recompute_facts(conn, plan_date)
            return {
                "kind": "plan",
                "file_date": plan_date,
                "version": version,
                "replaced": old is not None,
                "row_count": len(rows),
                "recomputed_dates": [plan_date],
            }
        finally:
            conn.close()


def upload_landing(
    landing_date: str,
    version: int,
    rows: list[LandingRow],
    db_file: str | None = None,
) -> dict[str, Any]:
    """整批替换某落地日的落地清单。

    落地日 L 的报文可同时包含计划日 L 与 L-1 的航班，因此受影响的两个
    计划日结论必须在同一事务内一起重算。
    """
    with _WRITE_LOCK:
        conn = db.connect(db_file)
        try:
            with db.immediate_tx(conn):
                old = db.get_version(conn, "landing", landing_date)
                if old is not None and version <= old:
                    raise VersionConflictError("landing", landing_date, old, version)

                conn.execute(
                    "DELETE FROM landing_entries WHERE landing_date=?", (landing_date,)
                )
                db.insert_landing_rows(conn, version, rows)
                db.replace_source_version(conn, "landing", landing_date, version)

                affected = sorted({add_days(landing_date, -1), landing_date})
                for plan_date in affected:
                    recompute_facts(conn, plan_date)
            return {
                "kind": "landing",
                "file_date": landing_date,
                "version": version,
                "replaced": old is not None,
                "row_count": len(rows),
                "recomputed_dates": affected,
            }
        finally:
            conn.close()


def recompute_facts(conn, plan_date: str) -> dict[str, Any]:
    """按当前库内三清单重算一个计划起飞日的全部结论。

    清单不齐全（计划日清单、当日落地清单、次日落地清单缺一）时删除旧结论
    且不产出新结论——缺清单时绝不判定取消或正常到达。
    """
    plan_ver = db.get_version(conn, "plan", plan_date)
    d0_ver = db.get_version(conn, "landing", plan_date)
    d1_date = add_days(plan_date, 1)
    d1_ver = db.get_version(conn, "landing", d1_date)

    conn.execute("DELETE FROM arrival_facts WHERE plan_date=?", (plan_date,))
    if plan_ver is None or d0_ver is None or d1_ver is None:
        return {"plan_date": plan_date, "complete": False, "facts": 0}

    plan_rows = conn.execute(
        "SELECT flight_no, destination, sched_arr_min, status "
        "FROM plan_entries WHERE plan_date=? ORDER BY flight_no",
        (plan_date,),
    ).fetchall()

    landing_rows = conn.execute(
        "SELECT flight_no, actual_airport, actual_arr_min, plan_date, landing_date "
        "FROM landing_entries WHERE plan_date=? ORDER BY flight_no, actual_arr_min",
        (plan_date,),
    ).fetchall()

    landings: dict[str, list[Any]] = {}
    for lr in landing_rows:
        landings.setdefault(lr["flight_no"], []).append(lr)

    planned = {r["flight_no"]: r for r in plan_rows}
    facts: list[dict[str, Any]] = []

    for flight_no, plan in planned.items():
        hits = landings.get(flight_no, [])
        base = {
            "plan_date": plan_date,
            "flight_no": flight_no,
            "sched_airport": plan["destination"],
            "plan_version": plan_ver,
            "landing_d0_ver": d0_ver,
            "landing_d1_ver": d1_ver,
        }
        if not hits:
            if plan["status"] == "cancelled":
                facts.append(
                    {
                        **base,
                        **_fact(
                            "cancelled",
                            actual_airport=None,
                            delay_min=None,
                            reasons=None,
                            landing_count=0,
                        ),
                    }
                )
            else:
                facts.append(
                    {
                        **base,
                        **_fact(
                            "review",
                            actual_airport=None,
                            delay_min=None,
                            reasons=[REASON_NORMAL_MISSING],
                            landing_count=0,
                        ),
                    }
                )
            continue

        if len(hits) > 1:
            facts.append(
                {
                    **base,
                    **_fact(
                        "review",
                        actual_airport=hits[0]["actual_airport"],
                        delay_min=None,
                        reasons=_multi_reasons(plan["status"]),
                        landing_count=len(hits),
                    ),
                }
            )
            continue

        landing = hits[0]
        if plan["status"] == "cancelled":
            facts.append(
                {
                    **base,
                    **_fact(
                        "review",
                        actual_airport=landing["actual_airport"],
                        delay_min=None,
                        reasons=[REASON_CANCELLED_BUT_LANDED],
                        landing_count=1,
                    ),
                }
            )
            continue

        if landing["actual_airport"] != plan["destination"]:
            # 备降：记录实际机场，不计算原目的地延误。
            facts.append(
                {
                    **base,
                    **_fact(
                        "diverted",
                        actual_airport=landing["actual_airport"],
                        delay_min=None,
                        reasons=None,
                        landing_count=1,
                    ),
                }
            )
            continue

        delay = max(0, landing["actual_arr_min"] - plan["sched_arr_min"])
        facts.append(
            {
                **base,
                **_fact(
                    "arrived",
                    actual_airport=landing["actual_airport"],
                    delay_min=delay,
                    reasons=None,
                    landing_count=1,
                ),
            }
        )

    # 孤儿落地：落地清单里有、计划清单里没有的航班。
    for flight_no, hits in landings.items():
        if flight_no in planned:
            continue
        sample = hits[0]
        facts.append(
            {
                "plan_date": plan_date,
                "flight_no": flight_no,
                "sched_airport": None,
                "plan_version": plan_ver,
                "landing_d0_ver": d0_ver,
                "landing_d1_ver": d1_ver,
                **_fact(
                    "review",
                    actual_airport=sample["actual_airport"],
                    delay_min=None,
                    reasons=[REASON_ORPHAN],
                    landing_count=len(hits),
                ),
            }
        )

    for fact in facts:
        db.upsert_fact(conn, fact)
    return {"plan_date": plan_date, "complete": True, "facts": len(facts)}


def _fact(
    fact_type: str,
    *,
    actual_airport: str | None,
    delay_min: int | None,
    reasons: list[str] | None,
    landing_count: int,
) -> dict[str, Any]:
    return {
        "fact_type": fact_type,
        "actual_airport": actual_airport,
        "delay_min": delay_min,
        "review_reasons": reasons,
        "landing_count": landing_count,
    }


def _multi_reasons(plan_status: str) -> list[str]:
    reasons = [REASON_MULTIPLE]
    if plan_status == "cancelled":
        reasons.append(REASON_CANCELLED_BUT_LANDED)
    return reasons


def get_report(plan_date: str, db_file: str | None = None) -> dict[str, Any]:
    """查询某日结论；缺清单时返回缺失项，不返回事实。"""
    conn = db.connect(db_file)
    try:
        plan_ver = db.get_version(conn, "plan", plan_date)
        d0_ver = db.get_version(conn, "landing", plan_date)
        d1_date = add_days(plan_date, 1)
        d1_ver = db.get_version(conn, "landing", d1_date)

        missing: list[str] = []
        if plan_ver is None:
            missing.append(f"plan:{plan_date}")
        if d0_ver is None:
            missing.append(f"landing:{plan_date}")
        if d1_ver is None:
            missing.append(f"landing:{d1_date}")

        rows = conn.execute(
            "SELECT * FROM arrival_facts WHERE plan_date=? "
            "ORDER BY flight_no",
            (plan_date,),
        ).fetchall()
        facts = [_fact_to_output(r) for r in rows]
        return {
            "plan_date": plan_date,
            "status": "complete" if not missing else "incomplete",
            "missing_sources": missing,
            "source_versions": {
                "plan": plan_ver,
                f"landing_{plan_date}": d0_ver,
                f"landing_{d1_date}": d1_ver,
            },
            "facts": facts,
        }
    finally:
        conn.close()


def _fact_to_output(row) -> dict[str, Any]:
    import json

    reasons = json.loads(row["review_reasons"]) if row["review_reasons"] else None
    return {
        "flight_no": row["flight_no"],
        "fact_type": row["fact_type"],
        "sched_airport": row["sched_airport"],
        "actual_airport": row["actual_airport"],
        "delay_min": row["delay_min"],
        "landing_count": row["landing_count"],
        "review_reasons": reasons,
        "review_text": [REASON_TEXT[r] for r in reasons] if reasons else None,
        "source_versions": {
            "plan": row["plan_version"],
            "landing_d0": row["landing_d0_ver"],
            "landing_d1": row["landing_d1_ver"],
        },
    }


def list_sources(db_file: str | None = None) -> dict[str, list[dict[str, Any]]]:
    conn = db.connect(db_file)
    try:
        out: dict[str, list[dict[str, Any]]] = {"plan": [], "landing": []}
        for kind in ("plan", "landing"):
            out[kind] = [
                dict(r)
                for r in conn.execute(
                    "SELECT kind, file_date, version, uploaded_at "
                    "FROM source_versions WHERE kind=? ORDER BY file_date",
                    (kind,),
                ).fetchall()
            ]
        return out
    finally:
        conn.close()


def repair_on_startup(db_file: str | None = None) -> dict[str, Any]:
    """启动自愈：对每个有计划清单的日期按当前源版号重算。

    正常运行下结论与源在同一事务内提交，本步骤只是幂等兜底，保证重启后
    不会留下任何混版结论。
    """
    with _WRITE_LOCK:
        conn = db.connect(db_file)
        try:
            plan_dates = db.list_plan_dates(conn)
            repaired: list[dict[str, Any]] = []
            for plan_date in plan_dates:
                with db.immediate_tx(conn):
                    repaired.append(recompute_facts(conn, plan_date))
            return {"checked_dates": plan_dates, "results": repaired}
        finally:
            conn.close()
