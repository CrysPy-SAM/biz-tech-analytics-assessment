"""
Celery configuration for biz-tech-analytics-assessment.
Configured for high reliability, worker crash resilience (SIGKILL),
exponential backoff, and idempotent task processing.
"""

import os
from celery import Celery

# Set default Django settings module
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

app = Celery("biztech")

# Read configuration from Django settings, using the CELERY_ namespace
app.config_from_object("django.conf:settings", namespace="CELERY")

# Worker crash resilience & reliability configurations
app.conf.update(
    # Acknowledge tasks ONLY after execution finishes (prevents loss on SIGKILL)
    task_acks_late=True,
    # Requeue tasks if the worker child process abruptly terminates or is SIGKILL'd
    task_reject_on_worker_lost=True,
    # Worker fetches only one task at a time (prevents hoarding during bursts)
    worker_prefetch_multiplier=1,
    # Redis broker visibility timeout (seconds): un-ACKed tasks re-enter the queue
    broker_transport_options={"visibility_timeout": 3600},
    # Ensure task results expire after 24 hours to prevent memory bloat
    result_expires=86400,
    # Task serialization format
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    # Timezone matching Django
    timezone="UTC",
    enable_utc=True,
    # Track when tasks start running
    task_track_started=True,
)

# Automatically discover tasks from all installed Django apps
app.autodiscover_tasks()


@app.task(bind=True, ignore_result=True)
def debug_task(self):
    """Diagnostic task for verifying Celery worker health."""
    print(f"Celery Debug Request: {self.request!r}")
