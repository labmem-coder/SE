"""FastAPI app —— 入口、路由、生命周期。"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .db import init_db
from .routers.admin_api import router as admin_router
from .routers.user_api import router as user_router
from .tick import start_scheduler


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    scheduler = start_scheduler()
    try:
        yield
    finally:
        scheduler.shutdown(wait=False)


app = FastAPI(
    title="智能充电桩调度计费系统",
    description=(
        "波普特大学 SE 课程项目。"
        "实现 hw1_report_v3.md 中的 11 个用例（提交请求 / 修改 / 取消 / 查看排队 / "
        "响应叫号 / 上报异常 / 支付 / 桩状态 / 故障确认 / 恢复 / 运营报表）。"
    ),
    version="1.0.0",
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(user_router)
app.include_router(admin_router)

# 静态前端
WEB_DIR = Path(__file__).resolve().parent.parent / "web"
if WEB_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(str(WEB_DIR / "index.html"))


@app.get("/health", include_in_schema=False)
def health() -> dict:
    return {"status": "ok"}
