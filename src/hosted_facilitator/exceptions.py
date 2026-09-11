from __future__ import annotations

from typing import Any


class HostedFacilitatorError(Exception):
    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.details = details or {}


class ProtocolError(HostedFacilitatorError):
    """Raised when a seller/facilitator payment protocol exchange is invalid."""
