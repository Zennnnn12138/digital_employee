"""登录、任务、审批和 SSE 进度。"""

from __future__ import annotations

import json
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.errors import AppError
from app.security import Actor, authenticate, decode_token, encode_token, load_actor

router = APIRouter()


class LoginIn(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)


class TaskIn(BaseModel):
    message: str = Field(min_length=2, max_length=2000)
    idempotency_key: str | None = Field(default=None, max_length=80)


class DecisionIn(BaseModel):
    decision: str = Field(pattern=r"^(approved|rejected)$")
    note: str = Field(default="", max_length=500)


def get_actor(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
) -> Actor:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise AppError(401, "未登录")
    token = authorization.split(" ", 1)[1].strip()
    payload = decode_token(token, request.app.state.resources.settings.jwt_secret)
    return load_actor(request.app.state.resources.factory, str(payload["sub"]))


@router.post("/api/auth/login")
def login(body: LoginIn, request: Request):
    resources = request.app.state.resources
    with resources.factory() as session:
        actor = authenticate(session, body.username.strip(), body.password)
    if actor is None:
        raise AppError(401, "账号或密码错误")
    token = encode_token(actor, resources.settings.jwt_secret, resources.settings.jwt_expire_minutes)
    return {
        "access_token": token,
        "token_type": "bearer",
        "username": actor.username,
        "can_approve": actor.can_approve,
        "roles": list(actor.roles),
        "shops": list(actor.shops),
        "display_name": actor.display_name,
    }


@router.get("/api/me")
def me(actor: Actor = Depends(get_actor)):
    return {
        "username": actor.username,
        "can_approve": actor.can_approve,
        "roles": list(actor.roles),
        "shops": list(actor.shops),
        "display_name": actor.display_name,
    }


@router.get("/api/workspace")
def workspace(request: Request, actor: Actor = Depends(get_actor)):
    return request.app.state.resources.service.workspace(actor)


@router.get("/api/tasks")
def list_tasks(request: Request, actor: Actor = Depends(get_actor)):
    views = request.app.state.resources.service.list_tasks(actor)
    return {"items": [view.as_dict() for view in views]}


@router.post("/api/tasks")
async def create_task(body: TaskIn, request: Request, actor: Actor = Depends(get_actor)):
    view = await request.app.state.resources.service.create_task(actor, body.message, body.idempotency_key)
    return view.as_dict()


@router.get("/api/tasks/{task_id}")
def get_task(task_id: str, request: Request, actor: Actor = Depends(get_actor)):
    return request.app.state.resources.service.get_task(actor, task_id).as_dict()


@router.get("/api/tasks/{task_id}/events")
def task_events(task_id: str, request: Request, actor: Actor = Depends(get_actor)):
    events = request.app.state.resources.service.list_events(actor, task_id)

    def generate():
        for event in events:
            data = json.dumps(event["payload"], ensure_ascii=False)
            yield f"event: {event['event']}\ndata: {data}\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")


@router.post("/api/tasks/{task_id}/approvals")
async def decide(task_id: str, body: DecisionIn, request: Request, actor: Actor = Depends(get_actor)):
    view = await request.app.state.resources.service.decide(actor, task_id, body.decision, body.note)
    return view.as_dict()


@router.get("/api/approvals/pending")
def pending(request: Request, actor: Actor = Depends(get_actor)):
    views = request.app.state.resources.service.list_pending(actor)
    return {"items": [view.as_dict() for view in views]}
