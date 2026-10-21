from __future__ import annotations


class SourceServiceError(Exception):
    def __init__(self, code: str, message: str, details: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details
