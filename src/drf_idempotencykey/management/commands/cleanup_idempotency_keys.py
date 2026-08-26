from django.core.management.base import BaseCommand

from drf_idempotencykey.models import IdempotencyKey


class Command(BaseCommand):
    help = "Delete expired idempotency records using the shared queryset definition."

    def handle(self, *args, **options):
        deleted_count = IdempotencyKey.objects.expired().delete()[0]
        self.stdout.write(self.style.SUCCESS(f"Deleted {deleted_count} expired idempotency records."))
