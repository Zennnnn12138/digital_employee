"""OpenAI 兼容聊天接口。DeepSeek 与 Qwen 都走这里，失败时由解析器回退规则。"""

from __future__ import annotations

import json
import logging
import re

import httpx

from app.config import Settings, resolve_llm

logger = logging.getLogger("digital_employee.llm")


class LLMError(RuntimeError):
    pass


async def complete_json(settings: Settings, system: str, user: str, timeout: float = 20) -> dict | None:
    target = resolve_llm(settings)
    if target is None:
        return None
    base_url, api_key, model = target
    url = base_url.rstrip("/") + "/chat/completions"
    if not url.endswith("/v1/chat/completions") and base_url.rstrip("/").endswith("api.deepseek.com"):
        url = base_url.rstrip("/") + "/chat/completions"
    payload = {
        "model": model,
        "temperature": 0,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(url, headers=headers, json=payload)
            response.raise_for_status()
            body = response.json()
        content = body["choices"][0]["message"]["content"]
    except Exception as exc:
        logger.warning("模型调用失败，将回退规则解析")
        raise LLMError(str(exc)) from exc
    return _extract_json(content)


def _extract_json(content: str) -> dict:
    text = content.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1)
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        text = text[start : end + 1]
    data = json.loads(text)
    if not isinstance(data, dict):
        raise LLMError("模型没有返回 JSON 对象")
    return data
