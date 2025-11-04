"""
Modular view package for the core app.

This package re-exports the public-facing views so existing imports
(`from core import views`) continue to function.
"""

from .admin import (
    AdminUserCreateView,
    AdminUserDeleteView,
    AdminUserListView,
    AdminUserPasswordView,
    AdminUserUpdateView,
)
from .api import (
    api_buildings,
    api_units,
    api_workorder_attachment_detail,
    api_workorder_attachments,
)
from .buildings import (
    BuildingCreateView,
    BuildingDeleteView,
    BuildingDetailView,
    BuildingListView,
    BuildingUpdateView,
    UnitCreateView,
    UnitDeleteView,
    UnitDetailView,
    UnitUpdateView,
)
from .notifications import NotificationSnoozeView
from .work_orders import (
    ArchivedWorkOrderDetailView,
    ArchivedWorkOrderListView,
    MassAssignWorkOrdersView,
    WorkOrderArchiveView,
    WorkOrderCreateView,
    WorkOrderAttachmentDeleteView,
    WorkOrderDeleteView,
    WorkOrderDetailView,
    WorkOrderListView,
    WorkOrderUpdateView,
)
from ..views_theme import toggle_theme

__all__ = [
    "AdminUserCreateView",
    "AdminUserDeleteView",
    "AdminUserListView",
    "AdminUserPasswordView",
    "AdminUserUpdateView",
    "api_buildings",
    "api_units",
    "api_workorder_attachments",
    "api_workorder_attachment_detail",
    "BuildingCreateView",
    "BuildingDeleteView",
    "BuildingDetailView",
    "BuildingListView",
    "BuildingUpdateView",
    "NotificationSnoozeView",
    "toggle_theme",
    "UnitCreateView",
    "UnitDeleteView",
    "UnitDetailView",
    "UnitUpdateView",
    "ArchivedWorkOrderDetailView",
    "ArchivedWorkOrderListView",
    "MassAssignWorkOrdersView",
    "WorkOrderArchiveView",
    "WorkOrderCreateView",
    "WorkOrderAttachmentDeleteView",
    "WorkOrderDeleteView",
    "WorkOrderDetailView",
    "WorkOrderListView",
    "WorkOrderUpdateView",
]
