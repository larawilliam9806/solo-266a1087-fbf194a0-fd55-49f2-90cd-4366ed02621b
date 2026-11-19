"""pytest 夹具：每个用例独立临时数据库。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_file = tmp_path / "test_airport.db"
    monkeypatch.setenv("AIRPORT_DB_PATH", str(db_file))
    # 延迟导入，确保 config.db_path() 读到被 monkeypatch 的环境变量。
    from app import db as db_mod
    from app.main import app

    db_mod.init_db()
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def db_path(client, tmp_path):
    return str(tmp_path / "test_airport.db")


def csv_bytes(text: str) -> bytes:
    return text.encode("utf-8")


def upload(client, kind: str, file_date: str, version: int, content: bytes):
    return client.post(
        f"/api/{kind}/{file_date}",
        data={"version": str(version)},
        files={"file": ("f.csv", content, "text/csv")},
    )


def facts_by_flight(report: dict) -> dict:
    return {f["flight_no"]: f for f in report["facts"]}
