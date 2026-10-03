import uuid
from unittest import mock

import time_machine
from dateutil.relativedelta import relativedelta
from django.contrib import admin
from django.contrib.auth import get_user_model
from django.db import IntegrityError
from django.http import HttpRequest, HttpResponse, StreamingHttpResponse
from django.test import override_settings
from django.urls import path
from django.utils import timezone
from rest_framework.authtoken.models import Token
from rest_framework.exceptions import AuthenticationFailed
from rest_framework.response import Response
from rest_framework.test import APITestCase
from rest_framework.views import APIView

from drf_idempotencykey import admin as idempotency_admin
from drf_idempotencykey.mixins import DrfIdempotencyKeyMixin
from drf_idempotencykey.models import IdempotencyKey
from drf_idempotencykey.testing import IdempotencyAPIClient, IdempotencyMeta, add_idempotency_test


class TestAPIView(DrfIdempotencyKeyMixin, APIView):
    payload = {"message": "Hello, World!"}

    def get(self, request):
        return Response(self.payload)

    def post(self, request):
        return Response(self.payload)

    def put(self, request):
        return Response(self.payload)

    def patch(self, request):
        return Response(self.payload)

    def delete(self, request):
        return Response(self.payload)


urlpatterns = [
    path("test-api/", TestAPIView.as_view(), name="test-api"),
    path("auth/token/", TestAPIView.as_view(), name="token_obtain_pair"),
    path("auth/token/refresh/", TestAPIView.as_view(), name="token_refresh"),
    path("api/v24/auth/token/", TestAPIView.as_view(), name="token_obtain_pair_api_v24"),
    path("api/v24/auth/token/refresh/", TestAPIView.as_view(), name="token_refresh_api_v24"),
    path("admin/", admin.site.urls),
]


@override_settings(ROOT_URLCONF="tests.test_mixin")
class TestIdempotencyKeyDrfMixin(APITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user(username="demo", password="demo-pass")

    def setUp(self):
        self.client.force_authenticate(user=self.user)

    def test_post_reuses_same_idempotency_key(self):
        idempotency_key = str(uuid.uuid4())
        response = self.client.post(
            "/test-api/",
            {"data": "test"},
            headers={"Idempotency-Key": idempotency_key},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"message": "Hello, World!"})
        self.assertEqual(IdempotencyKey.objects.count(), 1)

        repeat = self.client.post(
            "/test-api/",
            {"data": "test"},
            headers={"Idempotency-Key": idempotency_key},
            content_type="application/json",
        )
        self.assertEqual(repeat.status_code, 200)
        self.assertTrue(repeat.has_header("Cached-From-Idempotency-Key"))
        self.assertEqual(repeat["Cached-From-Idempotency-Key"], idempotency_key)

    def test_query_string_is_part_of_request_fingerprint(self):
        key = str(uuid.uuid4())
        first = self.client.post(
            "/test-api/?account=one",
            {"data": "test"},
            headers={"Idempotency-Key": key},
            content_type="application/json",
        )
        second = self.client.post(
            "/test-api/?account=two",
            {"data": "test"},
            headers={"Idempotency-Key": key},
            content_type="application/json",
        )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 409)

    def test_cached_response_preserves_headers_and_cookies(self):
        class HeaderResponseAPIView(TestAPIView):
            def post(self, request):
                response = Response({"created": True})
                response["Location"] = "/resources/123/"
                response["ETag"] = '"resource-v1"'
                response.set_cookie("session_hint", "created", httponly=True, samesite="Lax")
                return response

        with override_settings(ROOT_URLCONF=__name__):
            urlpatterns.append(path("header-api/", HeaderResponseAPIView.as_view(), name="header-api"))
            key = str(uuid.uuid4())
            first = self.client.post(
                "/header-api/",
                {"data": "test"},
                headers={"Idempotency-Key": key},
                content_type="application/json",
            )
            repeated = self.client.post(
                "/header-api/",
                {"data": "test"},
                headers={"Idempotency-Key": key},
                content_type="application/json",
            )

        self.assertEqual(repeated.status_code, first.status_code)
        self.assertEqual(repeated["Location"], first["Location"])
        self.assertEqual(repeated["ETag"], first["ETag"])
        self.assertEqual(repeated.cookies["session_hint"].value, "created")
        self.assertTrue(repeated.cookies["session_hint"]["httponly"])

    @add_idempotency_test
    def test_post_repeated_request_returns_cached_response(self):
        response = self.client.post(
            "/test-api/",
            {"data": "test"},
            headers={"Idempotency-Key": str(uuid.uuid4())},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"message": "Hello, World!"})

    def test_put_reuses_same_idempotency_key(self):
        idempotency_key = str(uuid.uuid4())
        response = self.client.put(
            "/test-api/",
            {"data": "test"},
            headers={"Idempotency-Key": idempotency_key},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"message": "Hello, World!"})

        repeat = self.client.put(
            "/test-api/",
            {"data": "test"},
            headers={"Idempotency-Key": idempotency_key},
            content_type="application/json",
        )
        self.assertEqual(repeat.status_code, 200)
        self.assertEqual(repeat["Cached-From-Idempotency-Key"], idempotency_key)

    def test_patch_reuses_same_idempotency_key(self):
        idempotency_key = str(uuid.uuid4())
        response = self.client.patch(
            "/test-api/",
            {"data": "test"},
            headers={"Idempotency-Key": idempotency_key},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)

        repeat = self.client.patch(
            "/test-api/",
            {"data": "test"},
            headers={"Idempotency-Key": idempotency_key},
            content_type="application/json",
        )
        self.assertEqual(repeat.status_code, 200)
        self.assertEqual(repeat["Cached-From-Idempotency-Key"], idempotency_key)

    def test_idempotency_client_helper_replays_same_request(self):
        client = IdempotencyAPIClient()
        client.force_authenticate(user=self.user)
        response = client.post(
            "/test-api/",
            {"data": "helper"},
            format="json",
            HTTP_IDEMPOTENCY_KEY="550e8400-e29b-41d4-a716-446655440000",
        )
        self.assertEqual(response.status_code, 200)
        repeated = client.repeat_latest_request()
        self.assertEqual(repeated.status_code, 200)
        self.assertEqual(repeated["Cached-From-Idempotency-Key"], "550e8400-e29b-41d4-a716-446655440000")

    def test_same_idempotency_key_with_different_payload_fails(self):
        key = str(uuid.uuid4())
        self.client.post(
            "/test-api/",
            {"data": "test"},
            headers={"Idempotency-Key": key},
            content_type="application/json",
        )

        response = self.client.post(
            "/test-api/",
            {"data": "different"},
            headers={"Idempotency-Key": key},
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["errors"][0]["error_code"], "IDEMPOTENCY_KEY_IN_USE_WITH_DIFFERENT_REQUEST")

    def test_missing_idempotency_key_is_rejected_when_required(self):
        class RequiredKeyAPIView(TestAPIView):
            require_idempotency_key = True

        with override_settings(ROOT_URLCONF=__name__):
            urlpatterns.append(path("required-api/", RequiredKeyAPIView.as_view(), name="required-api"))
            response = self.client.post(
                "/required-api/",
                {"data": "test"},
                content_type="application/json",
            )
            self.assertEqual(response.status_code, 400)
            self.assertEqual(response.json()["errors"][0]["error_code"], "MISSING_IDEMPOTENCY_KEY")

    @override_settings(IDEMPOTENCY_KEY_REQUIRED=True, IDEMPOTENCY_KEY_EXEMPT_PATH_RE=r"^/exempt/")
    def test_exempt_path_does_not_require_idempotency_header(self):
        with override_settings(ROOT_URLCONF=__name__):
            urlpatterns.append(path("exempt/", TestAPIView.as_view(), name="exempt"))
            response = self.client.post("/exempt/", {"data": "test"}, content_type="application/json")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(IdempotencyKey.objects.count(), 0)

    def test_redact_body_hook_is_used_for_persistence(self):
        class RedactedAPIView(TestAPIView):
            @staticmethod
            def redact_body(body: str) -> str:
                return body.replace("secret", "[REDACTED]")

        with override_settings(ROOT_URLCONF=__name__):
            urlpatterns.append(path("redacted-api/", RedactedAPIView.as_view(), name="redacted-api"))
            response = self.client.post(
                "/redacted-api/",
                {"password": "secret"},
                headers={"Idempotency-Key": str(uuid.uuid4())},
                content_type="application/json",
            )

        self.assertEqual(response.status_code, 200)
        self.assertIn("[REDACTED]", IdempotencyKey.objects.get().request_body)
        self.assertNotIn("secret", IdempotencyKey.objects.get().request_body)

    @override_settings(IDEMPOTENCY_KEY_RETRY_AFTER_SECONDS=7)
    @override_settings(IDEMPOTENCY_KEY_RETRY_AFTER_SECONDS=7)
    def test_retry_after_is_present_for_in_progress_conflicts(self):
        key = str(uuid.uuid4())
        digest = DrfIdempotencyKeyMixin._make_digest("POST", b'{"data": "test"}', "/test-api/")
        first = IdempotencyKey.objects.create(
            key=key,
            user=self.user,
            request_method="POST",
            request_body='{"data": "test"}',
            request_path="/test-api/",
            request_digest=digest,
            response_code=None,
            last_accessed_at=timezone.now(),
        )

        response = self.client.post(
            "/test-api/",
            {"data": "test"},
            headers={"Idempotency-Key": key},
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.headers["Retry-After"], "7")
        first.delete()

    @override_settings(IDEMPOTENCY_KEY_HEADER="X-Idempotency-Key")
    def test_custom_header_name_is_used(self):
        response = self.client.post(
            "/test-api/",
            {"data": "test"},
            headers={"X-Idempotency-Key": str(uuid.uuid4())},
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(IdempotencyKey.objects.count(), 1)

    def test_non_idempotent_methods_and_exempt_paths_are_skipped(self):
        response = self.client.get("/test-api/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(IdempotencyKey.objects.count(), 0)

        class ExemptAPIView(TestAPIView):
            pass

        with override_settings(ROOT_URLCONF=__name__, IDEMPOTENCY_KEY_EXEMPT_PATH_RE=r"^/exempt/"):
            urlpatterns.append(path("exempt/", ExemptAPIView.as_view(), name="exempt"))
            exempt_response = self.client.post(
                "/exempt/",
                {"data": "test"},
                headers={"Idempotency-Key": str(uuid.uuid4())},
                content_type="application/json",
            )

        self.assertEqual(exempt_response.status_code, 200)
        self.assertEqual(IdempotencyKey.objects.count(), 0)

    def test_should_skip_handles_authentication_failures_and_custom_methods(self):
        request = HttpRequest()
        request.method = "POST"
        request.path_info = "/secure/"
        request.headers = {"Idempotency-Key": str(uuid.uuid4())}
        request.user = mock.Mock(is_authenticated=False)
        request._read_started = True
        request._body = b'{"data": "test"}'
        self.assertTrue(TestAPIView()._should_skip(request))

        request2 = HttpRequest()
        request2.method = "DELETE"
        request2.path_info = "/safe/"
        request2.headers = {"Idempotency-Key": str(uuid.uuid4())}
        request2.user = mock.Mock(is_authenticated=True)
        request2._read_started = True
        request2._body = b'{"data": "test"}'
        self.assertTrue(TestAPIView()._should_skip(request2))

        request3 = HttpRequest()
        request3.method = "POST"
        request3.path_info = "/safe/"
        request3.headers = {"Idempotency-Key": str(uuid.uuid4())}
        request3.user = mock.Mock(is_authenticated=True)
        request3._read_started = True
        request3._body = b'{"data": "test"}'
        self.assertFalse(TestAPIView()._should_skip(request3))

        class BadAuthUser:
            @property
            def is_authenticated(self):
                raise AuthenticationFailed("bad token")

        request4 = HttpRequest()
        request4.method = "POST"
        request4.path_info = "/safe/"
        request4.headers = {"Idempotency-Key": str(uuid.uuid4())}
        request4.user = BadAuthUser()
        request4._read_started = True
        request4._body = b'{"data": "test"}'
        self.assertTrue(TestAPIView()._should_skip(request4))

    def test_expired_queryset_and_string_representation(self):
        key = str(uuid.uuid4())
        record = IdempotencyKey.objects.create(
            key=key,
            user=self.user,
            request_method="POST",
            request_body='{"data": "test"}',
            request_path="/test-api/",
            request_digest=b"abc123",
        )

        self.assertIn("IdempotencyKey", str(record))
        self.assertEqual(IdempotencyKey.objects.expired().count(), 0)

        with time_machine.travel(timezone.now() + relativedelta(minutes=61), tick=True):
            self.assertEqual(IdempotencyKey.objects.expired().count(), 1)

    def test_invalid_uuid_is_rejected(self):
        response = self.client.post(
            "/test-api/",
            {"data": "test"},
            headers={"Idempotency-Key": "not-a-real-uuid"},
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["errors"][0]["error_code"], "INVALID_UUID_FORMAT")

    def test_unauthenticated_requests_are_ignored(self):
        self.client.logout()
        response = self.client.post(
            "/test-api/",
            {"data": "test"},
            headers={"Idempotency-Key": str(uuid.uuid4())},
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(IdempotencyKey.objects.count(), 0)

    def test_expired_record_can_be_reused_without_manual_reset(self):
        key = str(uuid.uuid4())
        response = self.client.post(
            "/test-api/",
            {"data": "test"},
            headers={"Idempotency-Key": key},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)

        with time_machine.travel(timezone.now() + relativedelta(minutes=61), tick=True):
            retry = self.client.post(
                "/test-api/",
                {"data": "changed"},
                headers={"Idempotency-Key": key},
                content_type="application/json",
            )
            repeated = self.client.post(
                "/test-api/",
                {"data": "changed"},
                headers={"Idempotency-Key": key},
                content_type="application/json",
            )

        self.assertEqual(retry.status_code, 200)
        self.assertEqual(repeated.status_code, 200)
        self.assertTrue(repeated.has_header("Cached-From-Idempotency-Key"))
        self.assertEqual(IdempotencyKey.objects.count(), 1)
        record = IdempotencyKey.objects.get(key=key)
        self.assertIn("changed", record.request_body)
        self.assertFalse(record.is_expired())

    def test_concurrent_first_create_race_uses_existing_record(self):
        key = str(uuid.uuid4())
        body = b'{"data":"test"}'
        digest = DrfIdempotencyKeyMixin._make_digest("POST", body, "/race/")

        first = IdempotencyKey.objects.create(
            key=key,
            user=self.user,
            request_method="POST",
            request_body=body.decode(),
            request_path="/race/",
            request_digest=digest,
            last_accessed_at=timezone.now(),
            response_code=200,
            response_body="{}",
            response_content_type="application/json",
        )
        request = HttpRequest()
        request.method = "POST"
        request.path = "/race/"
        request.path_info = "/race/"
        request.user = self.user
        request._read_started = True
        request._body = body

        with mock.patch.object(IdempotencyKey.objects, "get_or_create", side_effect=IntegrityError("duplicate key")):
            obj, created, expired = TestAPIView()._get_or_create_key(request, key, digest)

        self.assertFalse(created)
        self.assertFalse(expired)
        self.assertEqual(obj.pk, first.pk)

    @override_settings(IDEMPOTENCY_KEY_MAX_BODY_SIZE=10)
    def test_large_response_body_is_not_stored(self):
        response = self.client.post(
            "/test-api/",
            {"data": "test"},
            headers={"Idempotency-Key": str(uuid.uuid4())},
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(IdempotencyKey.objects.count(), 0)

    def test_binary_response_body_is_not_stored(self):
        class BinaryResponseAPIView(TestAPIView):
            def post(self, request):
                return HttpResponse(b"\x00\x01\x02binary-data", content_type="application/octet-stream")

        with override_settings(ROOT_URLCONF=__name__):
            urlpatterns.append(path("binary-api/", BinaryResponseAPIView.as_view(), name="binary-api"))
            response = self.client.post(
                "/binary-api/",
                {"data": "test"},
                headers={"Idempotency-Key": str(uuid.uuid4())},
                content_type="application/json",
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(IdempotencyKey.objects.count(), 1)
        self.assertEqual(IdempotencyKey.objects.get().response_body, "")

    def test_streaming_response_is_not_cached_as_an_empty_replay(self):
        class StreamingAPIView(TestAPIView):
            def post(self, request):
                return StreamingHttpResponse(iter([b"streamed"]), content_type="text/plain")

        with override_settings(ROOT_URLCONF=__name__):
            urlpatterns.append(path("streaming-api/", StreamingAPIView.as_view(), name="streaming-api"))
            response = self.client.post(
                "/streaming-api/",
                {"data": "test"},
                headers={"Idempotency-Key": str(uuid.uuid4())},
                content_type="application/json",
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(b"".join(response.streaming_content), b"streamed")
        self.assertEqual(IdempotencyKey.objects.count(), 0)

    def test_response_without_content_type_raises_value_error(self):
        key = str(uuid.uuid4())
        record = IdempotencyKey.objects.create(
            key=key,
            user=self.user,
            request_method="POST",
            request_body='{"data": "test"}',
            request_path="/test-api/",
            request_digest=b"abc123",
        )

        response = HttpResponse("hello")
        response["Content-Type"] = ""

        with self.assertRaises(ValueError):
            record.save_response(response)

    def test_cleanup_task_removes_expired_records(self):
        from drf_idempotencykey.tasks import cleanup_idempotency_keys

        key = str(uuid.uuid4())
        self.client.post(
            "/test-api/",
            {"data": "test"},
            headers={"Idempotency-Key": key},
            content_type="application/json",
        )

        with time_machine.travel(timezone.now() + relativedelta(hours=25), tick=True):
            cleanup_idempotency_keys()

        self.assertEqual(IdempotencyKey.objects.count(), 0)

    @mock.patch(
        "django.contrib.auth.models.AbstractUser.is_authenticated",
        new_callable=mock.PropertyMock,
        side_effect=ValueError("boom"),
    )
    def test_unknown_exception_is_logged(self, _):
        with self.assertLogs("root", level="ERROR") as captured:
            response = self.client.post(
                "/test-api/",
                {"data": "test"},
                headers={"Idempotency-Key": str(uuid.uuid4())},
                content_type="application/json",
            )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(any("Error checking if user is authenticated" in message for message in captured.output))

    def test_admin_has_expected_permissions(self):
        self.assertFalse(idempotency_admin.IdempotencyKeyAdmin.has_add_permission(None, None))
        self.assertFalse(idempotency_admin.IdempotencyKeyAdmin.has_change_permission(None, None))
        self.assertFalse(idempotency_admin.IdempotencyKeyAdmin.has_delete_permission(None, None, None))

    def test_token_auth_is_supported(self):
        self.client.logout()
        token = Token.objects.create(user=self.user)

        response = self.client.post(
            "/test-api/",
            {"data": "test"},
            headers={"Idempotency-Key": str(uuid.uuid4()), "Authorization": f"Token {token.key}"},
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(IdempotencyKey.objects.count(), 1)


class TestIdempotencyTestingHelpers(APITestCase, metaclass=IdempotencyMeta):
    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user(username="helper-user", password="demo-pass")

    def setUp(self):
        self.client.force_authenticate(user=self.user)

    @add_idempotency_test
    def test_generated_variant_replays_the_same_request(self):
        response = self.client.post(
            "/test-api/",
            {"data": "helper"},
            headers={"Idempotency-Key": str(uuid.uuid4())},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"message": "Hello, World!"})

    def test_decorator_creates_idempotent_sibling(self):
        self.assertTrue(hasattr(self.__class__, "test_generated_variant_replays_the_same_request_idempotent"))

    def test_idempotency_client_accepts_header_dict(self):
        client = IdempotencyAPIClient()
        client.force_authenticate(user=self.user)
        response = client.post(
            "/test-api/",
            {"data": "helper"},
            format="json",
            headers={"Idempotency-Key": "550e8400-e29b-41d4-a716-446655440000"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(client._latest_idempotency_key, "550e8400-e29b-41d4-a716-446655440000")
