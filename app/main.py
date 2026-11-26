"""FastAPI 入口。本地默认 SQLite，Docker 中切到 PostgreSQL。"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.api.routes import router
from app.bootstrap import open_resources
from app.config import Settings, get_settings, resolve_paths
from app.errors import AppError

logger = logging.getLogger("digital_employee")
WEB_DIR = Path(__file__).resolve().parent / "web"
WEB_INDEX = WEB_DIR / "index.html"


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with open_resources(app.state.settings) as resources:
        app.state.resources = resources
        yield


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved = resolve_paths(settings or get_settings())
    app = FastAPI(title=resolved.app_name, lifespan=lifespan)
    app.state.settings = resolved
    app.include_router(router)

    @app.exception_handler(AppError)
    async def handle_app_error(_request, exc: AppError):
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

    @app.get("/health")
    def health():
        return {"status": "ok", "service": resolved.app_name}

    @app.get("/")
    def index():
        return FileResponse(WEB_INDEX)

    app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")
    return app


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
app = create_app()
