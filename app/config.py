"""运行配置。数据库路径通过环境变量 AIRPORT_DB_PATH 注入，便于测试与容器挂载。"""

from __future__ import annotations

import os
from pathlib import Path


def db_path() -> Path:
    raw = os.environ.get("AIRPORT_DB_PATH", "data/airport.db")
    p = Path(raw)
    # 目录提前创建，保证 SQLite WAL 模式可写旁路线程文件。
    if p.parent and str(p.parent) not in ("", "."):
        p.parent.mkdir(parents=True, exist_ok=True)
    return p


# 单个上传文件大小上限（字节）。
MAX_UPLOAD_BYTES = 10 * 1024 * 1024

# UTC 分钟合法区间（单日报文）。
MIN_UTC_MINUTES = 0
MAX_UTC_MINUTES = 24 * 60 - 1
