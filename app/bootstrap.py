"""进程资源：数据库、检查点、工具网关和编译好的图。"""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass

from sqlalchemy.orm import sessionmaker

from app.cache import build_cache
from app.config import Settings, resolve_paths
from app.domain import init_db, make_engine, seed, wait_until_ready
from app.graph.builder import build_graph
from app.services.tasks import TaskService
from app.tools.gateway import ToolGateway
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver


@dataclass
class Resources:
    engine: object
    factory: sessionmaker
    service: TaskService
    settings: Settings


@asynccontextmanager
async def open_resources(settings: Settings):
    settings = resolve_paths(settings)
    engine = make_engine(settings.database_url)
    try:
        wait_until_ready(engine, attempts=1 if settings.database_url.startswith("sqlite") else 30)
        init_db(engine)
        seed(engine, settings.password_iterations)
        factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
        async with AsyncSqliteSaver.from_conn_string(settings.checkpoint_path) as saver:
            await saver.setup()
            gateway = ToolGateway(
                build_cache(settings.redis_url),
                settings.tool_timeout_seconds,
                settings.cache_ttl_seconds,
            )
            graph = build_graph(saver, gateway, settings)
            yield Resources(
                engine=engine,
                factory=factory,
                service=TaskService(factory, graph, settings),
                settings=settings,
            )
    finally:
        engine.dispose()
