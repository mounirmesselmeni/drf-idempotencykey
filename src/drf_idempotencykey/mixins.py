from __future__ import annotations

import hashlib
import logging
import re
import uuid
from functools import lru_cache
from typing import Any

from django.conf import settings
from django.db import IntegrityError, transaction
from django.http import HttpRequest, HttpResponse
from django.utils import timezone
from rest_framework import status
from rest_framework.exceptions import APIException, AuthenticationFailed

from drf_idempotencykey.models import IdempotencyKey

IDEMPOTENCY_CACHE_HEADER = "Cached-From-Idempotency-Key"


def get_idempotency_header_name() -> str:
    return getattr(settings, "IDEMPOTENCY_KEY_HEADER", "Idempotency-Key")


class Http409Error(APIException):
    status_code = status.HTTP_409_CONFLICT

    def __init__(self, message: str = "", code: str = ""):
        self.message = message
        self.code = code
        self.headers = None
        if code == "IDEMPOTENCY_KEY_ALREADY_IN_PROGRESS":
            self.headers = {"Retry-After": str(getattr(settings, "IDEMPOTENCY_KEY_RETRY_AFTER_SECONDS", 5))}
        super().__init__(
            detail={
                "status_code": self.status_code,
                "title": "Idempotency key error",
                "errors": [{"error_code": self.code, "reason": self.message}],
            },
            code=self.code,
        )


class Http400Error(APIException):
    status_code = status.HTTP_400_BAD_REQUEST

    def __init__(self, message: str = "", code: str = "INVALID_UUID_FORMAT"):
        self.message = message
        self.code = code
        self.headers = None
        super().__init__(
            detail={
                "status_code": self.status_code,
                "title": "Idempotency key error",
                "errors": [{"error_code": self.code, "reason": self.message}],
            },
            code=self.code,
        )


@lru_cache(maxsize=8)
def _get_compiled_exempt_pattern(pattern: str):
    return re.compile(pattern) if pattern else None


class DrfIdempotencyKeyMixin:
    _cached_initialized_request: HttpRequest | None = None
    _idempotency_instance: IdempotencyKey | None = None

    @staticmethod
    def redact_body(body: str) -> str:
        return body

    def initialize_request(self, request, *args, **kwargs):
        if self._cached_initialized_request is None:
            self._cached_initialized_request = super().initialize_request(request, *args, **kwargs)
        return self._cached_initialized_request

    def dispatch(self, request, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        self.request = self.initialize_request(request, *args, **kwargs)
        try:
            if response := self._pre_idempotent_response():
                return response
            response = super().dispatch(request, *args, **kwargs)
            self._post_idempotent_response(response)
            return response
        except APIException as exc:
            return self.handle_exception(exc)

    def handle_exception(self, exc):
        response = super().handle_exception(exc)
        if response is None:
            return response

        if not getattr(self.request, "accepted_renderer", None):
            self.request.accepted_renderer, self.request.accepted_media_type = self.perform_content_negotiation(
                self.request,
                force=True,
            )
        if not getattr(response, "accepted_renderer", None):
            response.accepted_renderer = self.request.accepted_renderer
            response.accepted_media_type = self.request.accepted_media_type
            response.renderer_context = self.get_renderer_context()
        if isinstance(exc, Http409Error) and exc.code == "IDEMPOTENCY_KEY_ALREADY_IN_PROGRESS":
            response["Retry-After"] = str(getattr(settings, "IDEMPOTENCY_KEY_RETRY_AFTER_SECONDS", 5))
        return response

    def _pre_idempotent_response(self) -> HttpResponse | None:
        self._idempotency_instance = None
        try:
            should_skip = self._should_skip(self.request)
        except (Http400Error, Http409Error) as exc:
            raise exc

        if not should_skip:
            try:
                (
                    self._idempotency_instance,
                    is_new_idempotency_key,
                    is_expired,
                ) = self._prepare_idempotency_key(self.request)
            except (Http409Error, Http400Error) as exc:
                raise exc

            if self._idempotency_instance and not is_new_idempotency_key and not is_expired:
                return self._get_cached_response(self._idempotency_instance)
        return None


    def _post_idempotent_response(self, response: HttpResponse) -> None:
        if self._idempotency_instance:
            self._handle_response(self.request, response)

    def _get_cached_response(self, idempotency_instance: IdempotencyKey) -> HttpResponse:
        response = HttpResponse(
            status=idempotency_instance.response_code,
            content=idempotency_instance.response_body,
            content_type=idempotency_instance.response_content_type,
        )
        response[IDEMPOTENCY_CACHE_HEADER] = idempotency_instance.key
        return response

    def _handle_response(self, request: HttpRequest, response: HttpResponse) -> None:
        if not self._idempotency_instance:
            return

        if hasattr(response, "is_rendered") and not response.is_rendered:
            response.render()

        if self._response_exceeds_limit(response):
            logging.warning(
                "Skipping idempotency response storage for %s %s because response body "
                "length %s exceeds configured limit %s bytes.",
                request.method,
                request.path_info,
                len(response.content or b""),
                getattr(settings, "IDEMPOTENCY_KEY_MAX_BODY_SIZE", None),
            )
            self._idempotency_instance.delete()
            self._idempotency_instance = None
            return

        if response.status_code in range(200, 300):
            self._save_response(request, response)
        else:
            self._idempotency_instance.delete()

    @transaction.atomic
    def _response_exceeds_limit(self, response: HttpResponse) -> bool:
        max_size = getattr(settings, "IDEMPOTENCY_KEY_MAX_BODY_SIZE", None)
        if max_size is None:
            return False
        try:
            size = len(response.content or b"")
        except Exception:
            size = 0
        return size > int(max_size)

    def _save_response(self, request: HttpRequest, response: HttpResponse) -> None:
        if not self._idempotency_instance:
            raise ValueError("Idempotency instance is not set")
        with transaction.atomic():
            try:
                idempotency_key = IdempotencyKey.objects.select_for_update().get(
                    key=self._idempotency_instance.key,
                    user=request.user,
                    response_code__isnull=True,
                )
            except IdempotencyKey.DoesNotExist as exc:
                raise Http409Error(
                    "Request is already in progress.",
                    code="IDEMPOTENCY_KEY_ALREADY_IN_PROGRESS",
                ) from exc
            try:
                idempotency_key.save_response(response)
            except Exception:
                logging.exception("Error saving response to idempotency key %s", idempotency_key.key)
                transaction.set_rollback(True)

    @transaction.atomic
    def _prepare_idempotency_key(self, request: HttpRequest) -> tuple[IdempotencyKey | None, bool, bool]:
        header_name = get_idempotency_header_name()
        idempotency_key = request.headers[header_name]
        self._validate_uuid(idempotency_key)

        digest = self._make_digest(request.method, request.body, request.path_info)
        return self._get_or_create_key(request, idempotency_key, digest)

    @classmethod
    def _make_digest(cls, request_method: str, request_body: bytes | str, request_path: str) -> bytes:
        payload = request_body if isinstance(request_body, bytes) else request_body.encode("utf-8")
        sha256 = hashlib.sha256()
        sha256.update(request_method.encode("utf8"))
        sha256.update(request_path.encode("utf8"))
        sha256.update(payload)
        return sha256.digest()

    def _should_skip(self, request: HttpRequest) -> bool:
        def is_authenticated() -> bool:
            try:
                user = getattr(request, "user", None)
                return bool(user and user.is_authenticated)
            except AuthenticationFailed:
                return False
            except Exception:
                logging.exception("Error checking if user is authenticated")
                return False

        method = request.method.upper()
        allowed_methods = tuple(getattr(settings, "IDEMPOTENCY_KEY_METHODS", ("POST", "PUT", "PATCH")))
        if method not in allowed_methods:
            return True

        exempt_pattern = getattr(settings, "IDEMPOTENCY_KEY_EXEMPT_PATH_RE", "")
        compiled_pattern = _get_compiled_exempt_pattern(exempt_pattern)
        exempt_match = bool(compiled_pattern and compiled_pattern.match(request.path_info))
        require_header = getattr(self, "require_idempotency_key", False) or getattr(
            settings,
            "IDEMPOTENCY_KEY_REQUIRED",
            False,
        )

        header_name = get_idempotency_header_name()
        if header_name not in request.headers:
            if require_header:
                raise Http400Error(f"Missing {header_name} header.", code="MISSING_IDEMPOTENCY_KEY")
            return True

        max_body_size = getattr(settings, "IDEMPOTENCY_KEY_MAX_BODY_SIZE", None)
        request_body_size = len(request.body or b"")
        if max_body_size is not None and request_body_size > int(max_body_size):
            logging.warning(
                "Skipping idempotency request storage for %s %s because request body "
                "length %s exceeds configured limit %s bytes.",
                request.method,
                request.path_info,
                request_body_size,
                max_body_size,
            )
            return True

        return exempt_match or not is_authenticated()

    def _validate_uuid(self, idempotency_key: str):
        try:
            uuid.UUID(idempotency_key)
        except (ValueError, AttributeError, TypeError) as exc:
            raise Http400Error("Only valid UUID formats are allowed.") from exc

    def _get_or_create_key(
        self, request: HttpRequest, idempotency_key: str, digest: bytes
    ) -> tuple[IdempotencyKey, bool, bool]:
        request_body = self.redact_body(request.body.decode("utf-8", errors="replace"))
        try:
            obj, created = IdempotencyKey.objects.get_or_create(
                key=idempotency_key,
                user=request.user,
                defaults={
                    "request_method": request.method,
                    "request_body": request_body,
                    "request_path": request.path_info,
                    "request_digest": digest,
                    "last_accessed_at": timezone.now(),
                },
            )
        except IntegrityError:
            obj = IdempotencyKey.objects.select_for_update().get(key=idempotency_key, user=request.user)
            created = False

        is_expired = False
        if not created:
            obj = self._lock_and_validate_key(idempotency_key, request.user, digest)
            is_expired = self._check_expiration(obj)
            if is_expired:
                self._reset_for_retry(obj, request, digest)
            obj.set_accessed()

        return obj, created, is_expired

    def _reset_for_retry(self, obj: IdempotencyKey, request: HttpRequest, digest: bytes) -> None:
        obj.request_method = request.method
        obj.request_body = self.redact_body(request.body.decode("utf-8", errors="replace"))
        obj.request_path = request.path_info
        obj.request_digest = digest
        obj.response_code = None
        obj.response_body = ""
        obj.response_content_type = ""
        obj.response_saved_at = None
        obj.last_accessed_at = timezone.now()
        obj.save(
            update_fields=[
                "request_method",
                "request_body",
                "request_path",
                "request_digest",
                "response_code",
                "response_body",
                "response_content_type",
                "response_saved_at",
                "last_accessed_at",
            ]
        )

    def _lock_and_validate_key(self, idempotency_key: str, user: Any, digest: bytes):
        obj = IdempotencyKey.objects.select_for_update().get(key=idempotency_key, user=user)
        obj_digest = obj.request_digest.tobytes() if not isinstance(obj.request_digest, bytes) else obj.request_digest
        if obj_digest != digest:
            raise Http409Error(
                "Request parameters do not match the original request.",
                code="IDEMPOTENCY_KEY_IN_USE_WITH_DIFFERENT_REQUEST",
            )
        return obj

    def _check_expiration(self, obj: IdempotencyKey) -> bool:
        is_expired = obj.is_expired()
        if not is_expired and not obj.response_code:
            raise Http409Error("Request is already in progress.", code="IDEMPOTENCY_KEY_ALREADY_IN_PROGRESS")
        return is_expired
