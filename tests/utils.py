import uuid
from collections.abc import Callable
from functools import wraps

from rest_framework.test import APIClient

from drf_idempotencykey.models import IdempotencyKey


class IdempotencyAPIClient(APIClient):
    """APIClient helper for testing both repeated and non-repeated requests."""

    _latest_response = None
    _latest_request_kwargs = None
    _latest_idempotency_key = None

    def generic(self, method, path, data="", content_type="application/octet-stream", secure=False, **extra):
        if "HTTP_IDEMPOTENCY_KEY" not in extra and "headers" not in extra:
            self._latest_idempotency_key = str(uuid.uuid4())
            extra["HTTP_IDEMPOTENCY_KEY"] = self._latest_idempotency_key

        self._latest_request_kwargs = {
            "method": method,
            "path": path,
            "data": data,
            "content_type": content_type,
            "secure": secure,
            **extra,
        }
        self._latest_response = super().generic(method, path, data, content_type, secure, **extra)
        return self._latest_response

    def repeat_latest_request(self):
        return self.generic(**self._latest_request_kwargs)


def add_idempotency_test(original_test: Callable) -> Callable:
    """Decorator to mark a test as idempotency-aware and support the repeated-request flow."""

    original_test.__test_idempotency = True  # type: ignore[attr-defined]

    @wraps(original_test)
    def wrapper(self, *args, **kwargs):
        return original_test(self, *args, **kwargs)

    return wrapper


def _run_idempotent_test(self, test_method, *args, **kwargs):
    original_client = self.client
    patched_client = IdempotencyAPIClient()

    if hasattr(original_client, "handler") and getattr(original_client.handler, "_force_user", None):
        patched_client.force_authenticate(user=original_client.handler._force_user)

    try:
        self.client = patched_client
        test_method(self, *args, **kwargs)
        latest_response = patched_client._latest_response
        response = patched_client.repeat_latest_request()

        self.assertEqual(
            IdempotencyKey.objects.filter(key=patched_client._latest_idempotency_key).count(),
            1,
            "Idempotency key is not saved",
        )
        idempotency_obj = IdempotencyKey.objects.get(key=patched_client._latest_idempotency_key)
        self.assertEqual(str(idempotency_obj.key), patched_client._latest_idempotency_key)
        self.assertEqual(response.status_code, latest_response.status_code, response.content)
        self.assertEqual(response.content, latest_response.content, "Response content is not the same")
        self.assertIn("Cached-From-Idempotency-Key", response.headers, "Idempotency key cache was not used")
        self.assertEqual(
            response.headers["Cached-From-Idempotency-Key"],
            patched_client._latest_idempotency_key,
            "Idempotency key cache is not used correctly",
        )
    finally:
        self.client = original_client


class IdempotencyMeta(type):
    """Dynamically injects idempotent variants of marker-decorated tests."""

    def __new__(cls, name, bases, attrs):
        new_dct = {}

        for base in bases:
            for attr_name, attr_value in base.__dict__.items():
                if attr_name.startswith("test_") and hasattr(attr_value, "__test_idempotency"):

                    def idempotent_test(self, test_method=attr_value):
                        return _run_idempotent_test(self, test_method)

                    idempotent_test.__doc__ = (
                        f"{attr_value.__doc__ or ''} !! This is an automatically generated test case for idempotency."
                    )
                    new_dct[f"{attr_name}_idempotent"] = idempotent_test

        for attr_name, attr_value in attrs.items():
            if attr_name.startswith("test_") and hasattr(attr_value, "__test_idempotency"):

                def idempotent_test(self, test_method=attr_value):
                    return _run_idempotent_test(self, test_method)

                idempotent_test.__doc__ = (
                    f"{attr_value.__doc__ or ''} !! This is an automatically generated test case for idempotency."
                )
                new_dct[f"{attr_name}_idempotent"] = idempotent_test

        attrs.update(new_dct)
        return super().__new__(cls, name, bases, attrs)
