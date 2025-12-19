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
    BuildingMembershipManageView,
    BuildingMembershipDeleteView,
    BuildingListView,
    BuildingUpdateView,
    TechnicianSubroleUpdateView,
    UnitCreateView,
    UnitDeleteView,
    UnitDetailView,
    UnitUpdateView,
)
from .audit import AuditTrailView
from .dashboard import DashboardView
from .notifications import NotificationSnoozeView
from .work_orders import (
    ArchivedWorkOrderDetailView,
    ArchivedWorkOrderListView,
    LawyerWorkOrderListView,
    MassAssignWorkOrdersView,
    WorkOrderArchiveView,
    WorkOrderApprovalDecisionView,
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
    "BuildingMembershipManageView",
    "BuildingMembershipDeleteView",
    "TechnicianSubroleUpdateView",
    "BuildingListView",
    "BuildingUpdateView",
    "AuditTrailView",
    "DashboardView",
    "NotificationSnoozeView",
    "toggle_theme",
    "UnitCreateView",
    "UnitDeleteView",
    "UnitDetailView",
    "UnitUpdateView",
    "ArchivedWorkOrderDetailView",
    "ArchivedWorkOrderListView",
    "LawyerWorkOrderListView",
    "MassAssignWorkOrdersView",
    "WorkOrderArchiveView",
    "WorkOrderApprovalDecisionView",
    "WorkOrderCreateView",
    "WorkOrderAttachmentDeleteView",
    "WorkOrderDeleteView",
    "WorkOrderDetailView",
    "WorkOrderListView",
    "WorkOrderUpdateView",
]
