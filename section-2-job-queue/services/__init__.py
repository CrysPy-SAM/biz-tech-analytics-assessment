# services package initialization
from .email_provider import (
    BaseEmailProvider,
    MockEmailBackend,
    EmailProviderError,
    TransientEmailProviderError,
    FatalEmailProviderError,
    get_email_provider,
)
from .idempotency import IdempotencyManager
from .dead_letter import DeadLetterService
from .email_service import EmailService

__all__ = [
    "BaseEmailProvider",
    "MockEmailBackend",
    "EmailProviderError",
    "TransientEmailProviderError",
    "FatalEmailProviderError",
    "get_email_provider",
    "IdempotencyManager",
    "DeadLetterService",
    "EmailService",
]
