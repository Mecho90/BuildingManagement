from django.contrib import admin
from .models import Building, Unit, WorkOrder

@admin.register(Building)
class BuildingAdmin(admin.ModelAdmin):
    list_display = ("name", "address", "owner")
    search_fields = ("name", "address", "owner__username")

@admin.register(Unit)
class UnitAdmin(admin.ModelAdmin):
    list_display = ("number", "building", "floor", "owner_name")
    search_fields = ("number", "owner_name", "building__name")
    list_filter = ("building",)

@admin.register(WorkOrder)
class WorkOrderAdmin(admin.ModelAdmin):
    list_display = ("title", "building", "unit", "priority", "status", "deadline", "archived_at")
    list_filter = ("building", "priority", "status", "archived_at")
    search_fields = ("title", "description")
