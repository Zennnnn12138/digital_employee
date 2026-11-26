"""运行配置。环境变量优先，未配置模型密钥时走可回归的规则解析。"""

from __future__ import annotations

import os
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[1]


class Settings(BaseSettings):
    """进程配置。显式传入的字段覆盖同名环境变量。"""

    app_name: str = "跨境电商运营数字员工协作系统"
    database_url: str = "sqlite:///./data/digital_employee.db"
    checkpoint_path: str = "./data/checkpoints.sqlite"
    redis_url: str = ""
    jwt_secret: str = "dev-only-change-me"
    jwt_expire_minutes: int = 720
    tool_timeout_seconds: float = 8.0
    cache_ttl_seconds: int = 30
    llm_provider: str = "auto"  # auto | rule | deepseek | qwen
    llm_base_url: str = ""
    llm_api_key: str = ""
    llm_model: str = ""
    deepseek_api_key: str = ""
    dashscope_api_key: str = ""
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    langfuse_host: str = ""
    password_iterations: int = 20_000

    model_config = SettingsConfigDict(
        env_file=str(ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )


def get_settings() -> Settings:
    return Settings()


def resolve_paths(settings: Settings) -> Settings:
    """把相对 sqlite / checkpoint 路径固定到项目目录，避免受启动目录影响。"""
    data = dict(settings.model_dump())
    data["database_url"] = _resolve_database_url(settings.database_url)
    data["checkpoint_path"] = str(_resolve_file(settings.checkpoint_path))
    return Settings(**data)


def _resolve_file(value: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        path = (ROOT / path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _resolve_database_url(url: str) -> str:
    prefix = "sqlite:///"
    if not url.startswith(prefix):
        return url
    raw = url[len(prefix) :]
    if raw == ":memory:" or raw.startswith("file:"):
        return url
    path = Path(raw)
    if not path.is_absolute():
        path = (ROOT / path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    return prefix + path.as_posix()


def resolve_llm(settings: Settings) -> tuple[str, str, str] | None:
    """返回 (base_url, api_key, model)。无密钥时返回 None，调用方改用规则。"""
    provider = (settings.llm_provider or "auto").lower()
    deepseek_key = settings.deepseek_api_key or os.getenv("DEEPSEEK_API_KEY", "")
    qwen_key = settings.dashscope_api_key or os.getenv("DASHSCOPE_API_KEY", "")

    if provider == "rule":
        return None
    if provider == "deepseek":
        key = settings.llm_api_key or deepseek_key
        if not key:
            return None
        return (
            settings.llm_base_url or "https://api.deepseek.com",
            key,
            settings.llm_model or "deepseek-chat",
        )
    if provider == "qwen":
        key = settings.llm_api_key or qwen_key
        if not key:
            return None
        return (
            settings.llm_base_url or "https://dashscope.aliyuncs.com/compatible-mode/v1",
            key,
            settings.llm_model or "qwen-plus",
        )
    if deepseek_key:
        return ("https://api.deepseek.com", deepseek_key, settings.llm_model or "deepseek-chat")
    if qwen_key:
        return (
            "https://dashscope.aliyuncs.com/compatible-mode/v1",
            qwen_key,
            settings.llm_model or "qwen-plus",
        )
    if settings.llm_api_key and settings.llm_base_url:
        return (settings.llm_base_url, settings.llm_api_key, settings.llm_model or "qwen-plus")
    return None
