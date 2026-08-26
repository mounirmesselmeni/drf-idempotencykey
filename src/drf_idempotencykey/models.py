from __future__ import annotations

from datetime import timedelta

from django.conf import settings
from django.db import models
from django.utils import timezone
from rest_framework.response import Response


class IdempotencyKeyQuerySet(models.QuerySet):
    def expired(self):
        expiration_minutes = getattr(settings, "IDEMPOTENCY_KEY_EXPIRATION_MINUTES", 60)
        cutoff = timezone.now() - timedelta(minutes=expiration_minutes)
        return self.filter(created_at__lt=cutoff)


class IdempotencyKeyManager(models.Manager.from_queryset(IdempotencyKeyQuerySet)):
    def expired(self):
        return self.get_queryset().expired()


class IdempotencyKey(models.Model):
    """Store request/response metadata for safe idempotent retries."""

    objects = IdempotencyKeyManager()

    class HttpMethods(models.TextChoices):
        POST = "POST"
        PUT = "PUT"
        PATCH = "PATCH"

    key = models.UUIDField()
    last_accessed_at = models.DateTimeField(null=True, blank=True)
    accessed_count = models.PositiveIntegerField(default=1)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, null=True, blank=True)

    request_method = models.CharField(max_length=16, choices=HttpMethods.choices)
    request_body = models.TextField()
    request_path = models.CharField(max_length=255)
    request_digest = models.BinaryField(max_length=32)

    response_code = models.PositiveSmallIntegerField(null=True, blank=True)
    response_body = models.TextField(blank=True)
    response_content_type = models.CharField(max_length=255, blank=True)
    response_saved_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    modified_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["key", "user"], name="unique_key_user"),
        ]
        indexes = [
            models.Index(fields=["created_at"]),
            models.Index(fields=["modified_at"]),
        ]

    def __str__(self):
        return f"IdempotencyKey({self.key}) for {self.user} #{self.request_method} {self.request_path}"

    def set_accessed(self):
        self.accessed_count += 1
        self.last_accessed_at = timezone.now()
        self.save(update_fields=["accessed_count", "last_accessed_at"])

    def save_response(self, response: Response) -> None:
        max_body_size = getattr(settings, "IDEMPOTENCY_KEY_MAX_BODY_SIZE", None)
        self.response_code = response.status_code
        if hasattr(response, "is_rendered") and not response.is_rendered:
            response.render()

        if max_body_size is not None:
            max_body_size = int(max_body_size)
            if len(response.content or b"") > max_body_size:
                self.response_body = ""
                self.response_content_type = response.headers.get("Content-Type", "")
                self.response_saved_at = timezone.now()
                self.save(
                    update_fields=[
                        "response_code",
                        "response_body",
                        "response_content_type",
                        "response_saved_at",
                    ]
                )
                return

        content_type = response.headers.get("Content-Type", "")
        is_text_like = content_type and (
            "json" in content_type or "text" in content_type or "application/xml" in content_type
        )
        if content_type and not is_text_like:
            self.response_body = ""
            self.response_content_type = content_type
            self.response_saved_at = timezone.now()
            self.save(update_fields=["response_code", "response_body", "response_content_type", "response_saved_at"])
            return

        try:
            self.response_body = response.content.decode("utf-8")
        except UnicodeDecodeError:
            self.response_body = response.content.decode("utf-8", errors="replace")

        if content_type:
            self.response_content_type = content_type
        elif self.response_body:
            raise ValueError("Response body is not empty but Content-Type is missing.")
        self.response_saved_at = timezone.now()
        self.save(update_fields=["response_code", "response_body", "response_content_type", "response_saved_at"])

    def is_expired(self) -> bool:
        expiration_minutes = getattr(settings, "IDEMPOTENCY_KEY_EXPIRATION_MINUTES", 60)
        return self.created_at < timezone.now() - timedelta(minutes=expiration_minutes)
