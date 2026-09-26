"""服务端业务模块。"""

from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

from .config import DATABASE_URL

_url = make_url(DATABASE_URL)
_connect_args: dict[str, object] = {}
if _url.get_backend_name() == "sqlite" and _url.database not in (None, ":memory:"):
    # 允许并发写入（如多请求同时签发批次）等待写锁而不是立即失败。
    _connect_args = {"timeout": 30}

engine = create_engine(
    DATABASE_URL, future=True, pool_pre_ping=True, connect_args=_connect_args
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


def get_db() -> Iterator[Session]:
    """执行确定性的业务处理。"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
