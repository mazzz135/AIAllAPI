from __future__ import annotations

from typing import Any


class OpenAIError(Exception):
    def __init__(
        self,
        message: str,
        *,
        status_code: int = 500,
        error_type: str = "api_error",
        code: str = "internal_error",
        param: str | None = None,
    ):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.error_type = error_type
        self.code = code
        self.param = param

    def body(self) -> dict[str, Any]:
        error: dict[str, Any] = {
            "message": self.message,
            "type": self.error_type,
            "code": self.code,
        }
        if self.param:
            error["param"] = self.param
        return {"error": error}
