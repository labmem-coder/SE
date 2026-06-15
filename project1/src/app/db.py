"""SQLAlchemy 引擎、会话与依赖注入。"""
from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import DATABASE_URL


class Base(DeclarativeBase):
    pass


engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False},  # SQLite 多线程：后台 tick 与 API 共用
    future=True,
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


def init_db() -> None:
    """创建所有表（首次启动）。"""
    # 重要：必须先导入 models 让 Base 注册所有 mapper。
    from . import models  # noqa: F401
    Base.metadata.create_all(engine)
    _light_migrate()


def _light_migrate() -> None:
    """create_all 不会给现有表加列；这里检查并补齐新加的列。"""
    from sqlalchemy import text
    with engine.connect() as conn:
        cols = {row[1] for row in conn.execute(text("PRAGMA table_info(charging_requests)"))}
        if "batch_plan_order" not in cols:
            conn.execute(text("ALTER TABLE charging_requests ADD COLUMN batch_plan_order INTEGER"))
            conn.commit()


def get_db() -> Iterator[Session]:
    """FastAPI 依赖：每个请求一个会话。"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def session_scope() -> Iterator[Session]:
    """非 HTTP 上下文（如后台 tick）使用的会话管理器。"""
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
