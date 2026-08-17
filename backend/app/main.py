from __future__ import annotations

import logging
import sys
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from io import TextIOWrapper

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.onebot import router as onebot_router
from app.api.routes import router
from app.core.config import get_settings
from app.db.session import initialize_database, verify_schema
from app.services.jobs import job_worker
from app.services.runtime import bootstrap_runtime
from app.workflows.agent import close_checkpointer, initialize_checkpointer

for stream in (sys.stdout, sys.stderr):
    if isinstance(stream, TextIOWrapper):
        stream.reconfigure(encoding="utf-8", errors="backslashreplace")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
settings = get_settings()


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncGenerator[None]:
    initialize_database()
    verify_schema()
    from app.db.session import SessionLocal

    with SessionLocal() as session:
        bootstrap_runtime(session)
    await initialize_checkpointer()
    job_worker.start()
    yield
    await job_worker.stop()
    await close_checkpointer()


app = FastAPI(
    title="PersonalAgent API",
    version="0.2.0",
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
