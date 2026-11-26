"""领域错误与 HTTP 错误。工具失败用业务码返回，不把堆栈抛给调用方。"""

from __future__ import annotations


class AppError(Exception):
    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


class BusinessError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class PermissionDenied(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class ValidationFailed(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message
