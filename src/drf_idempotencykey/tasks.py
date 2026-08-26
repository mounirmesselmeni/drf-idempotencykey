from __future__ import annotations

from drf_idempotencykey.models import IdempotencyKey

try:
    from celery import shared_task
except ImportError:  # pragma: no cover

    def shared_task(func=None, **_kwargs):
        def decorator(task_func):
            return task_func

        return decorator(func) if func else decorator


@shared_task
def cleanup_idempotency_keys():
    return IdempotencyKey.objects.expired().delete()[0]
