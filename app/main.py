"""FastAPI 入口与路由。"""

from __future__ import annotations

import datetime as _dt
from contextlib import asynccontextmanager

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse

from . import config, csv_parser, db, services
from .schemas import ReportResponse, SourcesResponse, UploadResponse
from .services import VersionConflictError


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    # 幂等自愈：确保重启后不存在混版结论。
    services.repair_on_startup()
    yield


app = FastAPI(
    title="机场到达日报 API",
    version="1.0.0",
    description=(
        "按计划起飞日合并航班计划与跨午夜落地报文，生成到达事实与待核结论。"
        "文件按各自日期携带所见版号整体替换。"
    ),
    lifespan=lifespan,
)


@app.exception_handler(csv_parser.CSVParseError)
async def csv_error_handler(_request, exc: csv_parser.CSVParseError):
    return JSONResponse(status_code=422, content={"detail": str(exc)})


@app.exception_handler(VersionConflictError)
async def version_conflict_handler(_request, exc: VersionConflictError):
    return JSONResponse(status_code=409, content={"detail": str(exc)})


def _parse_iso_date(raw: str, field: str) -> str:
    try:
        return _dt.date.fromisoformat(raw).isoformat()
    except (ValueError, TypeError) as exc:
        raise HTTPException(
            status_code=400, detail=f"{field} 日期格式应为 YYYY-MM-DD：{raw!r}"
        ) from exc


async def _read_upload(file: UploadFile) -> bytes:
    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="上传文件为空")
    if len(content) > config.MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"文件超过 {config.MAX_UPLOAD_BYTES} 字节上限",
        )
    return content


@app.post(
    "/api/plans/{plan_date}",
    response_model=UploadResponse,
    status_code=200,
    summary="按日整批上传/替换航班计划 CSV",
)
async def upload_plan(
    plan_date: str,
    version: int = Form(..., description="所见版号，须严格递增"),
    file: UploadFile = File(..., description="计划 CSV"),
):
    plan_date = _parse_iso_date(plan_date, "plan_date")
    if version < 1:
        raise HTTPException(status_code=400, detail="version 必须为 >=1 的整数")
    content = await _read_upload(file)
    # 解析在锁外完成，坏文件不会触碰数据库。
    rows = csv_parser.parse_plan_csv(content)
    return services.upload_plan(plan_date, version, rows)


@app.post(
    "/api/landings/{landing_date}",
    response_model=UploadResponse,
    status_code=200,
    summary="按日整批上传/替换落地报文 CSV（含跨午夜落地）",
)
async def upload_landing(
    landing_date: str,
    version: int = Form(..., description="所见版号，须严格递增"),
    file: UploadFile = File(..., description="落地 CSV"),
):
    landing_date = _parse_iso_date(landing_date, "landing_date")
    if version < 1:
        raise HTTPException(status_code=400, detail="version 必须为 >=1 的整数")
    content = await _read_upload(file)
    rows = csv_parser.parse_landing_csv(content, landing_date)
    return services.upload_landing(landing_date, version, rows)


@app.get(
    "/api/reports/{plan_date}",
    response_model=ReportResponse,
    summary="查询某计划起飞日的到达事实与待核结论",
)
async def get_report(plan_date: str):
    plan_date = _parse_iso_date(plan_date, "plan_date")
    return services.get_report(plan_date)


@app.get(
    "/api/sources",
    response_model=SourcesResponse,
    summary="查看已接收的各类文件日期与版号",
)
async def list_sources():
    return services.list_sources()


@app.get("/health", summary="健康检查")
async def health():
    return {"status": "ok"}
