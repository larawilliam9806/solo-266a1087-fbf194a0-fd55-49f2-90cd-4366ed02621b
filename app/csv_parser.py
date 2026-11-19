"""CSV 解析与字段校验。

计划 CSV 列：航班号、目的地、计划到达 UTC 分钟、正常/取消
落地 CSV 列：计划起飞日、航班号、实际机场、实际到达 UTC 分钟
同时兼容常见英文列名。
"""

from __future__ import annotations

import csv
import datetime as _dt
import io
from dataclasses import dataclass
from typing import Any

from . import config

PLAN_HEADER_ALIASES = {
    "flight_no": {"flight_no", "flight", "航班号", "航班", "flightnumber"},
    "destination": {"destination", "dest", "airport", "目的地", "计划目的地"},
    "sched_arr_min": {
        "sched_arr_min",
        "scheduled_arrival_min",
        "计划到达utc分钟",
        "计划到达utc分钟数",
        "计划到达分钟",
    },
    "status": {"status", "状态", "正常/取消"},
}

LANDING_HEADER_ALIASES = {
    "plan_date": {"plan_date", "scheduled_departure_date", "计划起飞日", "计划日期"},
    "flight_no": {"flight_no", "flight", "航班号", "航班", "flightnumber"},
    "actual_airport": {
        "actual_airport",
        "actual",
        "实际机场",
        "落地机场",
        "实际到达机场",
    },
    "actual_arr_min": {
        "actual_arr_min",
        "actual_arrival_min",
        "实际到达utc分钟",
        "实际到达utc分钟数",
        "实际到达分钟",
    },
}

STATUS_ALIASES = {
    "normal": "normal",
    "正常": "normal",
    "cancelled": "cancelled",
    "canceled": "cancelled",
    "取消": "cancelled",
}


class CSVParseError(ValueError):
    """文件内容不符合报文规范。"""


@dataclass(frozen=True)
class PlanRow:
    flight_no: str
    destination: str
    sched_arr_min: int
    status: str


@dataclass(frozen=True)
class LandingRow:
    plan_date: str
    flight_no: str
    actual_airport: str
    actual_arr_min: int

    @property
    def landing_date(self) -> str:
        """由实际到达 UTC 分钟反推落地日：>=1440 为次日。"""
        return add_days(self.plan_date, 1 if self.actual_arr_min >= 24 * 60 else 0)


def parse_iso_date(value: str, field: str) -> str:
    value = (value or "").strip()
    try:
        return _dt.date.fromisoformat(value).isoformat()
    except ValueError as exc:
        raise CSVParseError(f"{field} 日期格式应为 YYYY-MM-DD，实际为 {value!r}") from exc


def add_days(iso_date: str, days: int) -> str:
    return (_dt.date.fromisoformat(iso_date) + _dt.timedelta(days=days)).isoformat()


def _parse_minutes(raw: str, field: str) -> int:
    raw = (raw or "").strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise CSVParseError(f"{field} 必须是整数 UTC 分钟，实际为 {raw!r}") from exc
    # 落地报文允许跨午夜（0..2879）；计划报文仅当日（0..1439），由调用方收紧。
    if value < config.MIN_UTC_MINUTES or value > 2 * 24 * 60 - 1:
        raise CSVParseError(f"{field} UTC 分钟超出合法范围 [0, 2879]：{value}")
    return value


def _resolve_columns(
    header: list[str],
    aliases: dict[str, set[str]],
    kind_label: str,
) -> dict[str, int]:
    index: dict[str, str] = {}
    for idx, name in enumerate(header):
        index[(name or "").strip().lower().replace(" ", "")] = str(idx)
    cols: dict[str, int] = {}
    for canonical, names in aliases.items():
        found = [int(index[n]) for n in names if n in index]
        if not found:
            raise CSVParseError(
                f"{kind_label} CSV 缺少列 {canonical}（可接受列名：{sorted(names)}）"
            )
        cols[canonical] = found[0]
    return cols


def _cell(row: list[str], idx: int) -> str:
    return row[idx].strip() if idx < len(row) and row[idx] is not None else ""


def parse_plan_csv(content: bytes) -> list[PlanRow]:
    text = _decode(content)
    reader = csv.reader(io.StringIO(text))
    try:
        header = next(reader)
    except StopIteration as exc:
        raise CSVParseError("计划 CSV 为空文件") from exc
    cols = _resolve_columns(header, PLAN_HEADER_ALIASES, "计划")

    result: list[PlanRow] = []
    seen: set[str] = set()
    for line_no, row in enumerate(reader, start=2):
        if not any(cell.strip() for cell in row):
            continue  # 容忍空行
        flight_no = _cell(row, cols["flight_no"])
        if not flight_no:
            raise CSVParseError(f"计划 CSV 第 {line_no} 行航班号为空")
        destination = _cell(row, cols["destination"])
        if not destination:
            raise CSVParseError(f"计划 CSV 第 {line_no} 行 {flight_no} 目的地为空")
        minutes = _parse_minutes(
            _cell(row, cols["sched_arr_min"]),
            f"计划 CSV 第 {line_no} 行 {flight_no} 计划到达分钟",
        )
        if minutes > config.MAX_UTC_MINUTES:
            raise CSVParseError(
                f"计划 CSV 第 {line_no} 行 {flight_no} 计划到达分钟必须在当日 "
                f"[0, 1439] 内：{minutes}"
            )
        raw_status = _cell(row, cols["status"]).lower()
        status = STATUS_ALIASES.get(raw_status)
        if status is None:
            raise CSVParseError(
                f"计划 CSV 第 {line_no} 行 {flight_no} 状态非法：{raw_status!r}"
                "（允许 normal/正常、cancelled/取消）"
            )
        if flight_no in seen:
            raise CSVParseError(f"计划 CSV 中航班号 {flight_no} 重复出现")
        seen.add(flight_no)
        result.append(
            PlanRow(
                flight_no=flight_no,
                destination=destination,
                sched_arr_min=minutes,
                status=status,
            )
        )
    # 允许仅含表头的计划文件：表示该计划日没有航班（可用于整批清空替换）。
    return result


def parse_landing_csv(content: bytes, file_date: str) -> list[LandingRow]:
    """解析落地报文。file_date 为上传接口声明的落地日。"""
    text = _decode(content)
    reader = csv.reader(io.StringIO(text))
    try:
        header = next(reader)
    except StopIteration as exc:
        raise CSVParseError("落地 CSV 为空文件") from exc
    cols = _resolve_columns(header, LANDING_HEADER_ALIASES, "落地")

    result: list[LandingRow] = []
    seen_keys: set[tuple[str, str, int]] = set()
    for line_no, row in enumerate(reader, start=2):
        if not any(cell.strip() for cell in row):
            continue
        plan_date = parse_iso_date(
            _cell(row, cols["plan_date"]),
            f"落地 CSV 第 {line_no} 行计划起飞日",
        )
        flight_no = _cell(row, cols["flight_no"])
        if not flight_no:
            raise CSVParseError(f"落地 CSV 第 {line_no} 行航班号为空")
        airport = _cell(row, cols["actual_airport"])
        if not airport:
            raise CSVParseError(f"落地 CSV 第 {line_no} 行 {flight_no} 实际机场为空")
        minutes = _parse_minutes(
            _cell(row, cols["actual_arr_min"]),
            f"落地 CSV 第 {line_no} 行 {flight_no} 实际到达分钟",
        )
        parsed = LandingRow(
            plan_date=plan_date,
            flight_no=flight_no,
            actual_airport=airport,
            actual_arr_min=minutes,
        )
        # 规则：落地日只允许计划日及次日。
        if parsed.landing_date not in (plan_date, add_days(plan_date, 1)):
            raise CSVParseError(
                f"落地 CSV 第 {line_no} 行 {flight_no}：落地日 {parsed.landing_date} "
                f"既不是计划日 {plan_date} 也不是其次日"
            )
        # 规则：文件日期须与到达时刻一致。
        if parsed.landing_date != file_date:
            raise CSVParseError(
                f"落地 CSV 第 {line_no} 行 {flight_no}：按到达分钟 {minutes} "
                f"推算落地日为 {parsed.landing_date}，与上传文件日期 {file_date} 不一致"
            )
        key = (plan_date, flight_no, minutes)
        if key in seen_keys:
            raise CSVParseError(
                f"落地 CSV 中 {flight_no}（计划日 {plan_date}）在 {minutes} 分钟"
                "存在完全重复的落地记录"
            )
        seen_keys.add(key)
        result.append(parsed)
    # 允许仅含表头的落地文件：表示该落地日没有落地（可用于整批清空替换）。
    return result


def _decode(content: bytes) -> str:
    for enc in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return content.decode(enc)
        except UnicodeDecodeError:
            continue
    raise CSVParseError("文件无法按 UTF-8/GB18030 解码")


def rows_to_dicts(rows: list[Any]) -> list[dict[str, Any]]:
    return [dict(r.__dict__) for r in rows]
