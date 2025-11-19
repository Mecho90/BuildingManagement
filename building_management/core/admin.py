from django.contrib import admin

from .models import (
    Building,
    BuildingMembership,
    Notification,
    RoleAuditLog,
    Unit,
    WorkOrder,
    WorkOrderAttachment,
)


class WorkOrderAttachmentInline(admin.TabularInline):
    model = WorkOrderAttachment
    extra = 0
    fields = ("file", "original_name", "content_type", "size", "created_at", "updated_at")
    readonly_fields = ("original_name", "content_type", "size", "created_at", "updated_at")
    show_change_link = True


class BuildingMembershipInline(admin.TabularInline):
    model = BuildingMembership
    extra = 0
    fields = ("user", "role")
    autocomplete_fields = ("user",)
    show_change_link = True

@admin.register(Building)
class BuildingAdmin(admin.ModelAdmin):
    list_display = ("name", "address", "owner")
    search_fields = ("name", "address", "owner__username")
    list_select_related = ("owner",)
    autocomplete_fields = ("owner",)
    inlines = (BuildingMembershipInline,)

@admin.register(Unit)
class UnitAdmin(admin.ModelAdmin):
    list_display = ("number", "building", "floor", "owner_name")
    search_fields = ("number", "owner_name", "building__name")
    list_filter = ("building",)
    list_select_related = ("building",)
    autocomplete_fields = ("building",)

@admin.register(WorkOrder)
class WorkOrderAdmin(admin.ModelAdmin):
    list_display = ("title", "building", "unit", "priority", "status", "deadline", "archived_at")
    list_filter = ("building", "priority", "status", "archived_at")
    search_fields = ("title", "description", "unit__number", "building__name")
    list_select_related = ("building", "unit")
    autocomplete_fields = ("building", "unit")
    inlines = (WorkOrderAttachmentInline,)


@admin.register(Notification)
class NotificationAdmin(admin.ModelAdmin):
    list_display = (
        "title",
        "category",
        "level",
        "user",
        "is_active_display",
        "snoozed_until",
        "acknowledged_at",
        "created_at",
    )
    list_filter = ("category", "level", "acknowledged_at", "snoozed_until")
    search_fields = ("title", "body", "key", "user__username")
    list_select_related = ("user",)
    date_hierarchy = "created_at"

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        return qs.select_related("user")

    @admin.display(boolean=True, description="Active")
    def is_active_display(self, obj):
        return obj.is_active()


@admin.register(BuildingMembership)
class BuildingMembershipAdmin(admin.ModelAdmin):
    list_display = ("user", "building", "role", "created_at")
    list_filter = ("role",)
    search_fields = ("user__username", "building__name")
    autocomplete_fields = ("user", "building")


@admin.register(RoleAuditLog)
class RoleAuditLogAdmin(admin.ModelAdmin):
    list_display = ("created_at", "actor", "target_user", "role", "action", "building")
    list_filter = ("role", "action")
    search_fields = ("actor__username", "target_user__username", "payload")
    autocomplete_fields = ("actor", "target_user", "building")
    readonly_fields = ("created_at", "updated_at")
