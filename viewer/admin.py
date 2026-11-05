from django.contrib import admin

from .models import Upload


@admin.register(Upload)
class UploadAdmin(admin.ModelAdmin):
    list_display = ["name", "kind", "status", "detected_crs", "feature_count", "created_at"]
    list_filter = ["kind", "status"]
    readonly_fields = [
        "kind",
        "status",
        "error",
        "detected_crs",
        "feature_count",
        "raster_info",
        "created_at",
    ]
    actions = ["reconvert"]

    def save_model(self, request, obj, form, change):
        if obj.source_url:
            obj.kind = Upload.kind_for_url(obj.source_url)
        elif obj.source_path:
            obj.kind = Upload.kind_for_path(obj.source_path)
        super().save_model(request, obj, form, change)
        if not change or {"source_path", "source_url", "source_crs"} & set(form.changed_data):
            obj.convert()

    @admin.action(description="Re-open selected layers")
    def reconvert(self, request, queryset):
        for upload in queryset:
            upload.convert()

    def delete_queryset(self, request, queryset):
        # Delete one at a time so each layer's cache is removed too.
        for upload in queryset:
            upload.delete()
