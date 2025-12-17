# path: core/urls.py
# path: core/urls.py
from django.urls import path
from . import views

app_name = "core"

urlpatterns = [
    path("", views.DashboardView.as_view(), name="dashboard"),
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
    
    
    # Work Orders    
    path("work-orders/", views.WorkOrderListView.as_view(), name="work_orders_list"),
    path("work-orders/new/", views.WorkOrderCreateView.as_view(), name="work_order_create"),
    path("work-orders/<int:pk>/", views.WorkOrderDetailView.as_view(), name="work_order_detail"),
    path("work-orders/<int:pk>/edit/", views.WorkOrderUpdateView.as_view(), name="work_order_update"),
    path("work-orders/<int:pk>/delete/", views.WorkOrderDeleteView.as_view(), name="work_order_delete"),
    path("work-orders/<int:pk>/approval/", views.WorkOrderApprovalDecisionView.as_view(), name="work_order_approval_decide"),
    path("work-orders/<int:pk>/archive/", views.WorkOrderArchiveView.as_view(), name="work_order_archive"),
    path("work-orders/mass-assign/", views.MassAssignWorkOrdersView.as_view(), name="work_orders_mass_assign"),
    path("work-orders/archive/", views.ArchivedWorkOrderListView.as_view(), name="work_orders_archive"),
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

    # APIs (optional)
    path("api/buildings/", views.api_buildings, name="api_buildings"),
    path("api/buildings/<int:building_id>/units/", views.api_units, name="api_units"),
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
]
