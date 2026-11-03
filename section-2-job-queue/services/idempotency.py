import logging
from typing import Optional
from django.conf import settings
from email_queue.models import EmailDeliveryLog, EmailDeliveryStatus

logger = logging.getLogger(__name__)

# Fallback in-memory storage for test isolation if Redis is unreachable
_memory_idempotency_store = {}


class IdempotencyManager:
    """
    Guarantees that re-delivered tasks (e.g., following a worker SIGKILL)
    do not trigger duplicate external email transmissions.
    """

    _shared_fake_redis = None

    @classmethod
    def get_redis_client(cls):
        """Retrieves configured Redis client or None if unreachable."""
        try:
            import redis

            redis_url = getattr(settings, "REDIS_URL", "redis://localhost:6379/0")
            client = redis.Redis.from_url(redis_url, socket_timeout=2.0)
            client.ping()
            return client
        except Exception:
            if getattr(settings, "IS_TESTING", False):
                import fakeredis

                if cls._shared_fake_redis is None:
                    cls._shared_fake_redis = fakeredis.FakeRedis()
                return cls._shared_fake_redis
            return None

    @classmethod
    def is_already_sent(cls, idempotency_key: str) -> bool:
        """
        Checks whether this job has already been successfully delivered.
        Queries the persistent database log.
        """
        return EmailDeliveryLog.objects.filter(
            idempotency_key=idempotency_key,
            status=EmailDeliveryStatus.SENT,
        ).exists()

    @classmethod
    def acquire_lock(cls, idempotency_key: str, task_id: str, ttl: int = 86400) -> bool:
        """
        Attempts to acquire an atomic execution lock for this task run.
        Uses Redis SET key value NX EX ttl when available.
        """
        redis_client = cls.get_redis_client()
        redis_key = f"idempotency:lock:{idempotency_key}"

        if redis_client:
            try:
                # Returns True if key was set (lock acquired), False if already exists
                return bool(redis_client.set(redis_key, task_id, nx=True, ex=ttl))
            except Exception as e:
                logger.warning(f"Redis lock check failed ({e}), falling back to database verification")

        # In-memory / DB fallback
        if idempotency_key in _memory_idempotency_store:
            return False
        _memory_idempotency_store[idempotency_key] = task_id
        return True

    @classmethod
    def release_lock(cls, idempotency_key: str):
        """Releases the atomic execution lock upon failure so retries can execute."""
        redis_client = cls.get_redis_client()
        redis_key = f"idempotency:lock:{idempotency_key}"

        if redis_client:
            try:
                redis_client.delete(redis_key)
            except Exception:
                pass

        _memory_idempotency_store.pop(idempotency_key, None)

    @classmethod
    def reset_in_memory_store(cls):
        """Helper for test cleanup."""
        _memory_idempotency_store.clear()
        if cls._shared_fake_redis is not None:
            try:
                cls._shared_fake_redis.flushall()
            except Exception:
                pass
