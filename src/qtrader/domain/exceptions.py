"""Typed exception hierarchy for qtrader.

Every error crossing the application boundary derives from :class:`QtraderError`
so the API layer can translate it deterministically to an HTTP response.
"""

from __future__ import annotations


class QtraderError(Exception):
    """Base class for all qtrader domain/application errors."""

    code: str = "qtrader_error"
    http_status: int = 500

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class NotFoundError(QtraderError):
    """The requested resource does not exist."""

    code = "not_found"
    http_status = 404


class ConflictError(QtraderError):
    """The request conflicts with the current system state."""

    code = "conflict"
    http_status = 409


class ValidationError(QtraderError):
    """The request violates domain rules."""

    code = "validation_error"
    http_status = 422


class ExternalServiceError(QtraderError):
    """An upstream dependency (broker, data provider, LLM) failed."""

    code = "external_service"
    http_status = 503


class OrderRejectedError(ValidationError):
    """The risk gate refused the order."""

    code = "order_rejected"

    def __init__(self, reasons: list[str]) -> None:
        self.reasons = reasons
        super().__init__("; ".join(reasons))


class NoPriceDataError(NotFoundError):
    """No market data is available for the requested symbol."""

    code = "no_price_data"
