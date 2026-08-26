from django.apps import AppConfig


class DrfIdempotencyConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "drf_idempotencykey"
    verbose_name = "DRF Idempotency Key"
