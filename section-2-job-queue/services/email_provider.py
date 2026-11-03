import uuid
import logging
from abc import ABC, abstractmethod
from typing import Dict, Any, List, Optional
from dataclasses import dataclass

logger = logging.getLogger(__name__)


class EmailProviderError(Exception):
    """Base exception for email provider errors."""
    pass


class TransientEmailProviderError(EmailProviderError):
    """
    Recoverable / temporary email provider failures.
    Examples: 429 Rate Limit Exceeded, 502/503/504 Upstream Gateway errors,
    socket timeouts, connection resets.
    Eligible for exponential backoff retries.
    """
    pass


class FatalEmailProviderError(EmailProviderError):
    """
    Non-recoverable permanent email provider failures.
    Examples: 400 Bad Request, invalid email format, blacklisted recipient,
    authentication revoked.
    Should NOT be retried; dispatched directly to Dead-Letter Queue.
    """
    pass


@dataclass
class ProviderResponse:
    message_id: str
    status: str
    provider: str = "mock_provider"


class BaseEmailProvider(ABC):
    """Abstract interface defining the contract for transactional email providers."""

    @abstractmethod
    def send(
        self,
        recipient: str,
        subject: str,
        body: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> ProviderResponse:
        """Dispatches an email. Raises TransientEmailProviderError or FatalEmailProviderError."""
        raise NotImplementedError


class MockEmailBackend(BaseEmailProvider):
    """
    Configurable in-memory mock email provider for testing.
    Supports simulating transient network drops, rate limits, and fatal exceptions.
    """

    def __init__(self):
        self.sent_emails: List[Dict[str, Any]] = []
        self._transient_failures_remaining: int = 0
        self._fail_permanently: bool = False
        self._fatal_error: bool = False

    def reset(self):
        """Clears state for testing."""
        self.sent_emails.clear()
        self._transient_failures_remaining = 0
        self._fail_permanently = False
        self._fatal_error = False

    def simulate_transient_failures(self, times: int = 1):
        """Configures the provider to fail 'times' times before succeeding."""
        self._transient_failures_remaining = times

    def simulate_permanent_transient_failure(self):
        """Configures the provider to continually raise TransientEmailProviderError."""
        self._fail_permanently = True

    def simulate_fatal_failure(self):
        """Configures the provider to raise a non-retryable FatalEmailProviderError."""
        self._fatal_error = True

    def send(
        self,
        recipient: str,
        subject: str,
        body: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> ProviderResponse:
        logger.info(f"MockEmailBackend attempting send to {recipient} - subject: {subject}")

        if self._fatal_error:
            raise FatalEmailProviderError(
                f"Permanent delivery failure: Recipient {recipient} rejected by provider policy."
            )

        if self._fail_permanently or self._transient_failures_remaining > 0:
            if self._transient_failures_remaining > 0:
                self._transient_failures_remaining -= 1
            logger.warning("MockEmailBackend triggering simulated TransientEmailProviderError")
            raise TransientEmailProviderError(
                "Upstream 429 Too Many Requests / 503 Service Unavailable"
            )

        # Successful dispatch
        msg_id = f"msg_{uuid.uuid4().hex[:12]}"
        record = {
            "message_id": msg_id,
            "recipient": recipient,
            "subject": subject,
            "body": body,
            "metadata": metadata or {},
        }
        self.sent_emails.append(record)
        return ProviderResponse(message_id=msg_id, status="DELIVERED")


# Global singleton instance for testing inspection
_default_mock_provider = MockEmailBackend()


def get_email_provider() -> BaseEmailProvider:
    """Returns the configured transactional email provider."""
    return _default_mock_provider
