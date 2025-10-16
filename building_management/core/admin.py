from django.contrib import admin
from .models import Building, Unit, Tenant, WorkOrder


@admin.register(Building)
class BuildingAdmin(admin.ModelAdmin):
    list_display = ("name", "address", "owner")
    list_filter = ("owner",)
    search_fields = ("name", "address")


@admin.register(Unit)
class UnitAdmin(admin.ModelAdmin):
    list_display = ("building", "number", "floor", "is_occupied")
    list_filter = ("building", "is_occupied")
    search_fields = ("number",)


@admin.register(Tenant)
class TenantAdmin(admin.ModelAdmin):
    list_display = ("full_name", "email", "phone", "unit")
    search_fields = ("full_name", "email")


@admin.register(WorkOrder)
class WorkOrderAdmin(admin.ModelAdmin):
    list_display = ("title", "unit", "status", "created_at")
    list_filter = ("status", "unit__building")
    search_fields = ("title", "description")
