"""节点追踪。配置了 Langfuse 密钥就上报，否则只留在任务事件里。"""

from __future__ import annotations

import logging
import os

from app.config import Settings

logger = logging.getLogger("digital_employee.trace")


def tracing_enabled(settings: Settings) -> bool:
    public = settings.langfuse_public_key or os.getenv("LANGFUSE_PUBLIC_KEY", "")
    secret = settings.langfuse_secret_key or os.getenv("LANGFUSE_SECRET_KEY", "")
    return bool(public and secret)


def emit_span(settings: Settings, name: str, payload: dict) -> None:
    if not tracing_enabled(settings):
        return
    if settings.langfuse_public_key:
        os.environ.setdefault("LANGFUSE_PUBLIC_KEY", settings.langfuse_public_key)
    if settings.langfuse_secret_key:
        os.environ.setdefault("LANGFUSE_SECRET_KEY", settings.langfuse_secret_key)
    if settings.langfuse_host:
        os.environ.setdefault("LANGFUSE_HOST", settings.langfuse_host)
        os.environ.setdefault("LANGFUSE_BASE_URL", settings.langfuse_host)
    try:
        from langfuse import get_client

        client = get_client()
        with client.start_as_current_observation(as_type="span", name=name, input=payload) as span:
            span.update(output={"status": payload.get("status", "ok")})
    except Exception:
        logger.warning("Langfuse 上报失败，任务事件仍会保留", exc_info=True)
