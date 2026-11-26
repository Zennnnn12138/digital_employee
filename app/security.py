"""密码哈希与 JWT。演示账号只用于本地原型。"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import jwt
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.errors import AppError
from app.models import User

PBKDF2_ALGORITHM = "sha256"


@dataclass(frozen=True)
class Actor:
    user_id: str
    username: str
    shops: tuple[str, ...]
    roles: tuple[str, ...]
    can_approve: bool
    display_name: str = ""

    def allows_shop(self, shop_id: str) -> bool:
        return shop_id in self.shops

    def allows_role(self, role: str) -> bool:
        return role in self.roles


def hash_password(password: str, iterations: int, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(PBKDF2_ALGORITHM, password.encode("utf-8"), salt, iterations)
    return f"{iterations}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        iterations_text, salt_hex, digest_hex = stored.split("$")
        iterations = int(iterations_text)
        salt = bytes.fromhex(salt_hex)
    except ValueError:
        return False
    digest = hashlib.pbkdf2_hmac(PBKDF2_ALGORITHM, password.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(digest.hex(), digest_hex)


def encode_token(actor: Actor, secret: str, minutes: int) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": actor.user_id,
        "username": actor.username,
        "exp": now + timedelta(minutes=minutes),
        "iat": now,
    }
    return jwt.encode(payload, secret, algorithm="HS256")


def decode_token(token: str, secret: str) -> dict:
    try:
        return jwt.decode(token, secret, algorithms=["HS256"])
    except jwt.PyJWTError as exc:
        raise AppError(401, "登录已失效") from exc


def to_actor(user: User) -> Actor:
    shops = tuple(part for part in user.shops.split(",") if part)
    roles = tuple(part for part in user.roles.split(",") if part)
    return Actor(
        user_id=user.id,
        username=user.username,
        shops=shops,
        roles=roles,
        can_approve=bool(user.can_approve),
        display_name=user.display_name,
    )


def authenticate(session: Session, username: str, password: str) -> Actor | None:
    user = session.scalar(select(User).where(User.username == username))
    if user is None or not verify_password(password, user.password_hash):
        return None
    return to_actor(user)


def load_actor(factory: sessionmaker, user_id: str) -> Actor:
    with factory() as session:
        user = session.get(User, user_id)
        if user is None:
            raise AppError(401, "用户不存在")
        return to_actor(user)


def load_actor_by_username(factory: sessionmaker, username: str) -> Actor:
    with factory() as session:
        user = session.scalar(select(User).where(User.username == username))
        if user is None:
            raise AppError(404, f"用户不存在: {username}")
        return to_actor(user)
