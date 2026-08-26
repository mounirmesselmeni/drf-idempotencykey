from io import StringIO

import time_machine
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone

from drf_idempotencykey.models import IdempotencyKey
from drf_idempotencykey.tasks import cleanup_idempotency_keys


@override_settings(IDEMPOTENCY_KEY_CLEANUP_INTERVAL_HOURS=1)
class IdempotencyKeyCleanupTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = None

    def test_run_cleanup_task_successful(self):
        IdempotencyKey.objects.create(
            key="11111111-1111-1111-1111-111111111111",
            user=None,
            request_method="POST",
            request_body='{"key": "value"}',
            request_path="/api/v1/endpoint",
            request_digest=b"\x00",
            response_code=200,
            response_body='{"key": "value"}',
            response_content_type="application/json",
            response_saved_at=timezone.now(),
        )
        IdempotencyKey.objects.create(
            key="22222222-2222-2222-2222-222222222222",
            user=None,
            request_method="POST",
            request_body='{"key": "value"}',
            request_path="/api/v1/endpoint",
            request_digest=b"\x00",
            response_code=200,
            response_body='{"key": "value"}',
            response_content_type="application/json",
            response_saved_at=timezone.now(),
        )

        count_of_idempotency_keys = IdempotencyKey.objects.count()
        with time_machine.travel(timezone.now() + timezone.timedelta(minutes=55)):
            cleanup_idempotency_keys()
            self.assertEqual(IdempotencyKey.objects.count(), count_of_idempotency_keys)

        with time_machine.travel(timezone.now() + timezone.timedelta(minutes=70)):
            cleanup_idempotency_keys()
            self.assertEqual(IdempotencyKey.objects.count(), 0)

    def test_management_command_calls_cleanup_task(self):
        IdempotencyKey.objects.create(
            key="33333333-3333-3333-3333-333333333333",
            user=None,
            request_method="POST",
            request_body='{"key": "value"}',
            request_path="/api/v1/endpoint",
            request_digest=b"\x00",
            response_code=200,
            response_body='{"key": "value"}',
            response_content_type="application/json",
            response_saved_at=timezone.now(),
        )

        with time_machine.travel(timezone.now() + timezone.timedelta(minutes=70)):
            out = StringIO()
            call_command("cleanup_idempotency_keys", stdout=out)

        self.assertEqual(IdempotencyKey.objects.count(), 0)
        self.assertIn("Deleted 1 expired idempotency records.", out.getvalue())
