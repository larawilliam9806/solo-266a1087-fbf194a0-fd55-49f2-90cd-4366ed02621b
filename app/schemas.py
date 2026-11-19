"""响应模型。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class SourceVersionInfo(BaseModel):
    kind: Literal["plan", "landing"]
    file_date: str
    version: int
    uploaded_at: str


class UploadResponse(BaseModel):
    kind: Literal["plan", "landing"]
    file_date: str
    version: int
    replaced: bool
    row_count: int
    recomputed_dates: list[str]


class FactOut(BaseModel):
    flight_no: str
    fact_type: Literal["arrived", "cancelled", "diverted", "review"]
    sched_airport: str | None = None
    actual_airport: str | None = None
    delay_min: int | None = None
    landing_count: int
    review_reasons: list[str] | None = None
    review_text: list[str] | None = None
    source_versions: dict[str, int]


class ReportResponse(BaseModel):
    plan_date: str
    status: Literal["complete", "incomplete"]
    missing_sources: list[str]
    source_versions: dict[str, int | None]
    facts: list[FactOut]


class ErrorBody(BaseModel):
    detail: str


class SourcesResponse(BaseModel):
    plan: list[SourceVersionInfo]
    landing: list[SourceVersionInfo]


class RepairResponse(BaseModel):
    checked_dates: list[str]
    results: list[dict] = Field(default_factory=list)
