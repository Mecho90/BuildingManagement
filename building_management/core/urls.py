# path: core/urls.py
# path: core/urls.py
from django.urls import path
from . import views

app_name = "core"

urlpatterns = [
    path("", views.DashboardView.as_view(), name="dashboard"),
    # Budgets
    path("budgets/", views.BudgetListView.as_view(), name="budget_list"),
    path("budgets/new/", views.BudgetCreateView.as_view(), name="budget_create"),
    path("budgets/<int:pk>/", views.BudgetDetailView.as_view(), name="budget_detail"),
    path("budgets/<int:pk>/edit/", views.BudgetUpdateView.as_view(), name="budget_update"),
    path("budgets/<int:pk>/expenses/new/", views.BudgetExpenseCreateView.as_view(), name="budget_expense_create"),
    path("budgets/<int:pk>/expenses/<int:expense_id>/delete/", views.BudgetExpenseDeleteView.as_view(), name="budget_expense_delete"),
    path("budgets/<int:pk>/archive/", views.BudgetArchiveView.as_view(), name="budget_archive"),
    path("budgets/archived/", views.BudgetArchivedListView.as_view(), name="budget_archived_list"),
    path("budgets/archived/purge/", views.BudgetArchivePurgeView.as_view(), name="budget_archived_purge"),
    path("budgets/archived/purge/preview/", views.BudgetArchivePurgePreviewView.as_view(), name="budget_archived_purge_preview"),
    path("budgets/archived/bulk-delete/", views.BudgetArchivedBulkDeleteView.as_view(), name="budget_archived_bulk_delete"),
    path(
        "budgets/archived/requesters/delete/",
        views.BudgetArchivedRequesterDeleteView.as_view(),
        name="budget_archived_requester_delete",
    ),
    path(
        "budgets/archived/<int:pk>/delete/",
        views.BudgetArchivedItemDeleteView.as_view(),
        name="budget_archived_item_delete",
    ),
    path("budgets/review/", views.BudgetReviewQueueView.as_view(), name="budget_review_queue"),
    path("budgets/<int:pk>/review/", views.BudgetReviewDecisionView.as_view(), name="budget_review_decision"),
    path("budgets/mass-assign/", views.BudgetMassAssignView.as_view(), name="budget_mass_assign"),
    path("budgets/technicians/", views.BudgetTechnicianSummaryView.as_view(), name="budget_technicians"),
    path("budgets/<int:pk>/delete/", views.BudgetDeleteView.as_view(), name="budget_delete"),
    path("budgets/export/", views.BudgetExportView.as_view(), name="budget_export"),
    path("budgets/<int:pk>/timeline.json", views.BudgetTimelineApiView.as_view(), name="budget_timeline"),
    # Buildings
    path("buildings/", views.BuildingListView.as_view(), name="buildings_list"),
    path("buildings/new/", views.BuildingCreateView.as_view(), name="building_create"),
    path("buildings/<int:pk>/", views.BuildingDetailView.as_view(), name="building_detail"),
    path("buildings/<int:pk>/edit/", views.BuildingUpdateView.as_view(), name="building_update"),
    path("buildings/<int:pk>/delete/", views.BuildingDeleteView.as_view(), name="building_delete"),
    path("buildings/<int:pk>/members/", views.BuildingMembershipManageView.as_view(), name="building_memberships"),
    path(
        "buildings/<int:building_pk>/members/<int:membership_pk>/remove/",
        views.BuildingMembershipDeleteView.as_view(),
        name="building_membership_remove",
    ),
    path("buildings/<int:pk>/my-role/", views.TechnicianSubroleUpdateView.as_view(), name="technician_subrole"),

    path("toggle-theme/", views.toggle_theme, name="toggle_theme"),

    # Notifications
    path("notifications/<str:key>/snooze/", views.NotificationSnoozeView.as_view(), name="notification_snooze"),

    # Units
    path("buildings/<int:building_pk>/units/new/", views.UnitCreateView.as_view(), name="unit_create"),
    path(
        "buildings/<int:building_pk>/units/<int:unit_pk>/",
        views.UnitDetailView.as_view(),
        name="unit_detail",
    ),
    path(
        "buildings/<int:building_pk>/units/<int:unit_pk>/edit/",
        views.UnitUpdateView.as_view(),
        name="unit_update",
    ),
    path(
        "buildings/<int:building_pk>/units/<int:unit_pk>/delete/",
        views.UnitDeleteView.as_view(),
        name="unit_delete",
    ),
    
    
    path("todos/", views.TodoListPageView.as_view(), name="todo_list"),
    path("todos/new/", views.TodoCreateView.as_view(), name="todo_create"),
    path("todos/<int:pk>/edit/", views.TodoUpdateView.as_view(), name="todo_edit"),
    path("todos/<int:pk>/delete/", views.TodoDeleteView.as_view(), name="todo_delete"),

    # Work Orders    
    path("work-orders/", views.WorkOrderListView.as_view(), name="work_orders_list"),
    path("work-orders/lawyer/", views.LawyerWorkOrderListView.as_view(), name="lawyer_work_orders"),
    path("work-orders/new/", views.WorkOrderCreateView.as_view(), name="work_order_create"),
    path("work-orders/<int:pk>/", views.WorkOrderDetailView.as_view(), name="work_order_detail"),
    path("work-orders/<int:pk>/edit/", views.WorkOrderUpdateView.as_view(), name="work_order_update"),
    path("work-orders/<int:pk>/add-budget/", views.WorkOrderBudgetChargeView.as_view(), name="work_order_budget_charge"),
    path("work-orders/<int:pk>/delete/", views.WorkOrderDeleteView.as_view(), name="work_order_delete"),
    path("work-orders/<int:pk>/approval/", views.WorkOrderApprovalDecisionView.as_view(), name="work_order_approval_decide"),
    path("work-orders/<int:pk>/archive/", views.WorkOrderArchiveView.as_view(), name="work_order_archive"),
    path("work-orders/<int:pk>/quick-status/", views.WorkOrderQuickStatusView.as_view(), name="work_order_quick_status"),
    path("work-orders/mass-assign/", views.MassAssignWorkOrdersView.as_view(), name="work_orders_mass_assign"),
    path("work-orders/archive/", views.ArchivedWorkOrderListView.as_view(), name="work_orders_archive"),
    path("work-orders/archive/purge/", views.ArchivedWorkOrderPurgeView.as_view(), name="work_orders_archive_purge"),
    path("work-orders/archive/purge/preview/", views.ArchivedWorkOrderPurgePreviewView.as_view(), name="work_orders_archive_purge_preview"),
    path(
        "work-orders/archive/buildings/delete/",
        views.ArchivedWorkOrderBuildingDeleteView.as_view(),
        name="work_orders_archive_building_delete",
    ),
    path(
        "work-orders/archive/<int:building_id>/",
        views.ArchivedWorkOrderDetailView.as_view(),
        name="work_orders_archive_building",
    ),
    path(
        "work-orders/<int:order_pk>/attachments/<int:attachment_pk>/delete/",
        views.WorkOrderAttachmentDeleteView.as_view(),
        name="workorder_attachment_delete",
    ),

    path("audit/", views.AuditTrailView.as_view(), name="audit_trail"),

    # Admin user management (superuser-only dashboard)
    path("manage/users/", views.AdminUserListView.as_view(), name="users_list"),
    path("manage/users/new/", views.AdminUserCreateView.as_view(), name="user_create"),
    path("manage/users/<int:pk>/edit/", views.AdminUserUpdateView.as_view(), name="user_update"),
    path("manage/users/<int:pk>/password/", views.AdminUserPasswordView.as_view(), name="user_password"),
    path("manage/users/<int:pk>/delete/", views.AdminUserDeleteView.as_view(), name="user_delete"),
    path("manage/mass-delete/buildings/", views.AdminBuildingBulkDeleteView.as_view(), name="mass_delete_buildings"),
    path("manage/mass-delete/work-orders/", views.AdminWorkOrderBulkDeleteView.as_view(), name="mass_delete_work_orders"),
    path("manage/mass-archive/work-orders/", views.AdminWorkOrderBulkArchiveView.as_view(), name="mass_archive_work_orders"),
    path("manage/mass-delete/lawyer-work-orders/", views.AdminLawyerWorkOrderBulkDeleteView.as_view(), name="mass_delete_lawyer_work_orders"),
    path("manage/mass-delete/users/", views.AdminUserBulkDeleteView.as_view(), name="mass_delete_users"),

    # APIs (optional)
    path("api/buildings/", views.api_buildings, name="api_buildings"),
    path("api/buildings/<int:building_id>/units/", views.api_units, name="api_units"),
    path("api/todos/", views.api_todos, name="api_todos"),
    path("api/todos/<int:pk>/", views.api_todo_detail, name="api_todo_detail"),
    path("api/todos/completed/", views.api_todo_completed_clear, name="api_todo_completed_clear"),
    path("api/todos/summary/", views.api_todo_summary, name="api_todo_summary"),
    path("api/todos/ics/", views.todo_ics_feed, name="todo_ics_feed"),
    path("api/todos/calendar/", views.api_todo_calendar, name="api_todo_calendar"),
    path(
        "api/work-orders/<int:pk>/attachments/",
        views.api_workorder_attachments,
        name="api_workorder_attachments",
    ),
    path(
        "api/work-orders/<int:pk>/attachments/<int:attachment_id>/",
        views.api_workorder_attachment_detail,
        name="api_workorder_attachment_detail",
    ),
    path("api/budgets/", views.api_budget_requests, name="api_budget_requests"),
    path("api/budgets/<int:pk>/", views.api_budget_request_detail, name="api_budget_request_detail"),
    path(
        "api/budgets/<int:budget_id>/expenses/",
        views.api_budget_expenses,
        name="api_budget_expenses",
    ),
    path(
        "api/budgets/<int:budget_id>/expenses/<int:expense_id>/attachments/",
        views.api_budget_expense_attachments,
        name="api_budget_expense_attachments",
    ),
    path("health/forwarding/", views.forwarding_health, name="forwarding_health"),
]
