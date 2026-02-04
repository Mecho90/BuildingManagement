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
    api_todo_calendar,
    api_todo_completed_clear,
    api_todo_detail,
    api_todos,
    api_units,
    api_workorder_attachment_detail,
    api_workorder_attachments,
    todo_ics_feed,
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
from .todos import TodoListPageView, TodoCreateView, TodoUpdateView, TodoDeleteView
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
    "api_todos",
    "api_todo_calendar",
    "api_todo_completed_clear",
    "api_todo_detail",
    "api_units",
    "api_workorder_attachments",
    "api_workorder_attachment_detail",
    "todo_ics_feed",
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
    "TodoListPageView",
    "TodoCreateView",
    "TodoUpdateView",
    "TodoDeleteView",
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
