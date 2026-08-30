from __future__ import annotations

import logging
import sys
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from io import TextIOWrapper

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.onebot import onebot_manager
from app.api.onebot import router as onebot_router
from app.api.routes import router
from app.core.config import get_settings
from app.db.session import initialize_database, verify_schema
from app.services.jobs import job_worker
from app.services.napcat_logs import napcat_connector
from app.services.operation_logs import operation_logs
from app.services.persona_store import get_persona_store
from app.services.runtime import bootstrap_runtime

for stream in (sys.stdout, sys.stderr):
    if isinstance(stream, TextIOWrapper):
        stream.reconfigure(encoding="utf-8", errors="backslashreplace")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
settings = get_settings()


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncGenerator[None]:
    operation_logs.install_logging_handler()
    operation_logs.emit(source="startup", kind="started", title="项目启动", message="后端生命周期开始")
    try:
        operation_logs.emit(source="startup", title="数据库", message="初始化数据库")
        initialize_database()
        verify_schema()
        from app.db.session import SessionLocal

        with SessionLocal() as session:
            get_persona_store().sync_db(session)
            session.commit()
            bootstrap_runtime(session)
        operation_logs.emit(
            source="startup", kind="succeeded", title="数据库", message="数据库和运行时配置已就绪"
        )
        job_worker.start()
        onebot_manager.start_monitor()
        await napcat_connector.start()
        operation_logs.emit(
            source="startup",
            kind="succeeded",
            title="后台服务",
            message="Worker、OneBot 状态监控和 NapCat 日志桥已启动",
        )
        operation_logs.emit(source="startup", kind="succeeded", title="项目启动", message="后端启动完成")
        yield
    finally:
        operation_logs.emit(
            source="shutdown", kind="started", title="项目停止", message="后端生命周期开始收尾"
        )
        await napcat_connector.stop()
        await onebot_manager.stop_monitor()
        await job_worker.stop()
        operation_logs.emit(source="shutdown", kind="succeeded", title="项目停止", message="后端资源已释放")
        operation_logs.flush()
        operation_logs.close()


app = FastAPI(
    title="PersonalAgent API",
    version="0.4.3",
    docs_url="/docs" if settings.app_env == "development" else None,
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.frontend_origin],
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["Content-Type", "Authorization"],
)
app.include_router(router, prefix="/api/v1")
app.include_router(onebot_router, prefix="/api/v1")


@app.get("/", include_in_schema=False)
def root() -> dict[str, str]:
    return {"service": "personal-agent", "docs": "/docs"}
