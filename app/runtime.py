"""请求级上下文。工具通过 ContextVar 拿到当前操作者和数据库会话工厂。"""

from __future__ import annotations

from contextvars import ContextVar, Token
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sqlalchemy.orm import sessionmaker

    from app.config import Settings
    from app.security import Actor

_factory: ContextVar[sessionmaker | None] = ContextVar("de_session_factory", default=None)
_actor: ContextVar[Actor | None] = ContextVar("de_actor", default=None)
_settings: ContextVar[Settings | None] = ContextVar("de_settings", default=None)


def set_context(factory: sessionmaker, actor: Actor, settings: Settings) -> tuple[Token, Token, Token]:
    return _factory.set(factory), _actor.set(actor), _settings.set(settings)


def reset_context(tokens: tuple[Token, Token, Token]) -> None:
    _factory.reset(tokens[0])
    _actor.reset(tokens[1])
    _settings.reset(tokens[2])


def get_factory() -> sessionmaker:
    factory = _factory.get()
    if factory is None:
        raise RuntimeError("数据库上下文未设置")
    return factory


def get_actor() -> Actor:
    actor = _actor.get()
    if actor is None:
        raise RuntimeError("操作者上下文未设置")
    return actor


def current_settings() -> Settings:
    settings = _settings.get()
    if settings is None:
        raise RuntimeError("配置上下文未设置")
    return settings
