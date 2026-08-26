from django.contrib import admin

from drf_idempotencykey.models import IdempotencyKey


@admin.register(IdempotencyKey)
class IdempotencyKeyAdmin(admin.ModelAdmin):
    list_display = ("key", "user", "request_method", "request_path", "response_code", "created_at")
    list_filter = ("request_method", "response_code", "created_at")
    search_fields = ("key", "request_path", "user__username", "user__email")
    readonly_fields = ("created_at", "modified_at")
    list_select_related = ("user",)
    autocomplete_fields = ("user",)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
